"""Run ``autofill_application`` on one browser thread per process.

Playwright's sync driver can be started only once on that thread. This runner
starts one driver lazily, gives every session its own browser context, and
stops the driver on shutdown. A session delete closes only that context, so
cookies, storage, and pages are never shared. Sync Playwright objects are
touched only on the browser thread. Passwords are not logged.
"""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeVar

from autofill.ats import is_blocked_sso, is_manual_ats
from autofill.credentials import MemoryCredentialProvider
from autofill.engine import ApplicationResult, AutofillOptions, Status, autofill_application, is_browser_crash
from autofill.models import JobContext
from autofill.profile import CandidateProfile
from autofill.resolver import ResolverBinding

_T = TypeVar("_T")


class BrowserDisabled(RuntimeError):
    """``AUTOFILL_RUN_BROWSER`` is off, so this process will not launch Chromium."""


class RunnerStopped(RuntimeError):
    """The runner has shut down and will not accept more browser work."""


@dataclass
class FillRequest:
    """Everything one start or continue call needs. The URL is not logged by this module."""

    session_id: str
    candidate_id: str
    job_id: str
    application_url: str
    profile: CandidateProfile
    job: JobContext
    credentials: MemoryCredentialProvider
    resume_uploaded: bool
    cover_letter_text: str | None
    resolver: ResolverBinding | None = None
    answers_updated: bool = False
    timings: dict[str, int] | None = None
    session_created_ms: int = 0
    max_pages: int = 8
    intent_hook: Any = None


class FillRunner(Protocol):
    """Starts and continues a fill. Tests supply a fake that never opens a browser."""

    def run(self, request: FillRequest) -> ApplicationResult:
        """Fill or continue. Resume bytes are never selected here."""

    def discard(self, session_id: str) -> None:
        """Close any browser resources for ``session_id``."""

    def shutdown(self) -> None:
        """Close every browser this runner still holds."""


class _SharedDriver:
    """One Playwright driver and one Chromium process. Browser thread only."""

    def __init__(self, *, headless: bool, launch_args: list[str]) -> None:
        self._headless = headless
        self._launch_args = list(launch_args)
        self._playwright: Any = None
        self._browser: Any = None

    def browser(self) -> Any:
        """Launch Chromium on first use. Later sessions reuse this browser."""
        if self._browser is not None:
            return self._browser
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.launch(headless=self._headless, args=self._launch_args)
        except BaseException:
            playwright.stop()
            raise
        self._playwright = playwright
        self._browser = browser
        return browser

    def stop(self) -> None:
        """Close Chromium and stop the driver. Safe when neither was started."""
        browser = self._browser
        playwright = self._playwright
        self._browser = None
        self._playwright = None
        try:
            if browser is not None:
                browser.close()
        finally:
            if playwright is not None:
                playwright.stop()


class _SessionContext:
    """One session's browser context. Only the browser thread touches it."""

    def __init__(self) -> None:
        self.context: Any = None
        self.page: Any = None

    def open(self, driver: _SharedDriver, url: str) -> Any:
        if self.page is not None:
            return self.page
        # A fresh context every time. No user-data directory and no stored profile.
        context = driver.browser().new_context()
        try:
            page = context.new_page()
            page.goto(url)
        except BaseException:
            context.close()
            raise
        self.context = context
        self.page = page
        return page

    def close(self) -> None:
        """Close this context only. The shared driver stays up for other sessions."""
        context = self.context
        self.page = None
        self.context = None
        if context is not None:
            context.close()


class PlaywrightRunner:
    """Default runner. Manual-ATS and SSO decisions do not launch a browser."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        headless: bool = True,
        launch_args: list[str] | None = None,
    ) -> None:
        self.enabled = enabled
        self.headless = headless
        self.launch_args = list(launch_args or [])
        self._sessions: dict[str, _SessionContext] = {}
        self._driver = _SharedDriver(headless=headless, launch_args=self.launch_args)
        self._queue: queue.Queue = queue.Queue()
        self._accept = threading.Lock()
        self._stopped = False
        self.browser_ready_ms = 0
        self._thread = threading.Thread(target=self._loop, name="autofill-browser", daemon=True)
        self._thread.start()

    def prewarm(self) -> int:
        """Launch the shared Chromium once. Later sessions only open a context."""
        if not self.enabled:
            self.browser_ready_ms = 0
            return 0
        started = time.perf_counter()
        self._submit(lambda: self._driver.browser())
        self.browser_ready_ms = int((time.perf_counter() - started) * 1000)
        return self.browser_ready_ms

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            function, done = item
            try:
                done.put((True, function()))
            except BaseException as exc:
                done.put((False, exc))

    def _submit(self, function: Callable[[], _T]) -> _T:
        """Run ``function`` on the browser thread. Playwright calls stay there."""
        done: queue.Queue = queue.Queue()
        with self._accept:
            if self._stopped:
                raise RunnerStopped("The browser runner has shut down.")
            self._queue.put((function, done))
        ok, value = done.get()
        if not ok:
            raise value
        return value

    def _options(self, request: FillRequest, page: object | None) -> AutofillOptions:
        return AutofillOptions(
            job_id=request.job_id,
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            page=page,
            resume_uploaded=request.resume_uploaded,
            cover_letter_text=request.cover_letter_text,
            headless=self.headless,
            job=request.job,
            resolver=request.resolver,
            answers_updated=request.answers_updated,
            timings=self._timings(request),
            max_pages=request.max_pages,
            intent_hook=request.intent_hook,
        )

    def _timings(self, request: FillRequest) -> dict[str, int]:
        timings = dict(request.timings or {})
        timings.setdefault("sessionCreatedMs", request.session_created_ms)
        timings.setdefault("browserReadyMs", self.browser_ready_ms)
        return timings

    def _run_engine(self, request: FillRequest, page: object | None) -> ApplicationResult:
        return autofill_application(
            request.application_url,
            request.profile,
            request.credentials,
            self._options(request, page),
        )

    def _recover_driver(self) -> None:
        """Close dead contexts and drop the driver so the next call relaunches."""
        for session_id in list(self._sessions):
            try:
                self._close(session_id)
            except Exception:
                self._sessions.pop(session_id, None)
        try:
            self._driver.stop()
        except Exception:
            self._driver._browser = None
            self._driver._playwright = None
        self._driver = _SharedDriver(headless=self.headless, launch_args=self.launch_args)

    def _retryable(self, request: FillRequest) -> ApplicationResult:
        """The session stays. A later start can run it again. No second session is created."""
        return ApplicationResult(
            status=Status.FAILED_RETRYABLE,
            ats=None,
            current_step=Status.FAILED_RETRYABLE,
            login_status="NOT_REQUIRED",
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            messages=["Browser stopped. The session was kept and can be retried."],
            timings=self._timings(request),
        )

    def _run_with_page(self, request: FillRequest) -> ApplicationResult:
        attempts = 0
        while True:
            try:
                slot = self._sessions.get(request.session_id)
                if slot is None or slot.page is None:
                    slot = _SessionContext()
                    slot.open(self._driver, request.application_url)
                    self._sessions[request.session_id] = slot
                return self._run_engine(request, slot.page)
            except Exception as exc:
                if not is_browser_crash(exc) or attempts >= 1:
                    if is_browser_crash(exc):
                        self._recover_driver()
                        return self._retryable(request)
                    raise
                attempts += 1
                self._recover_driver()

    def _close(self, session_id: str) -> None:
        slot = self._sessions.pop(session_id, None)
        if slot is not None:
            slot.close()

    def _shutdown_driver(self) -> None:
        try:
            for session_id in list(self._sessions):
                self._close(session_id)
        finally:
            self._driver.stop()

    def run(self, request: FillRequest) -> ApplicationResult:
        if is_manual_ats(request.application_url) or is_blocked_sso(request.application_url):
            return self._run_engine(request, None)
        if not self.enabled:
            raise BrowserDisabled("AUTOFILL_RUN_BROWSER is off. This process will not launch a browser.")
        return self._submit(lambda: self._run_with_page(request))

    def discard(self, session_id: str) -> None:
        """Close this session's context. Other sessions and the driver stay open."""
        with self._accept:
            if self._stopped:
                return
            done: queue.Queue = queue.Queue()
            self._queue.put((lambda: self._close(session_id), done))
        ok, value = done.get()
        if not ok:
            raise value

    def shutdown(self) -> None:
        """Close every session context, then stop the shared Playwright driver."""
        done: queue.Queue = queue.Queue()
        with self._accept:
            if self._stopped:
                return
            self._stopped = True
            self._queue.put((self._shutdown_driver, done))
        try:
            ok, value = done.get()
            if not ok:
                raise value
        finally:
            self._queue.put(None)
            self._thread.join(timeout=10)
