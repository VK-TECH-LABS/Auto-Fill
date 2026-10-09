"""Run ``autofill_application`` on a pool of browser threads.

Playwright's sync driver cannot be shared across threads, and one thread
would run every session one after another. Each worker thread owns one
prewarmed Chromium. A session is pinned to one worker. Every session still
gets a new browser context that is never reused. Sync Playwright objects are
touched only on their worker thread. Passwords are not logged.
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
from autofill.engine import (
    ApplicationResult,
    AutofillOptions,
    Status,
    autofill_application,
    is_ats_timeout,
    is_browser_crash,
)
from autofill.models import JobContext
from autofill.profile import CandidateProfile
from autofill.resolver import ResolverBinding

_T = TypeVar("_T")


def elapsed_ms(started: float) -> int:
    """Whole milliseconds, at least 1 when any time passed."""
    elapsed = (time.perf_counter() - started) * 1000
    if elapsed <= 0:
        return 0
    return max(1, int(elapsed))


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
    """One Playwright driver and one Chromium process. One worker thread only."""

    def __init__(self, *, headless: bool, launch_args: list[str]) -> None:
        self._headless = headless
        self._launch_args = list(launch_args)
        self._playwright: Any = None
        self._browser: Any = None

    def browser(self) -> Any:
        """Launch Chromium on first use. Later calls on this worker reuse it."""
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
    """One session's browser context. Only its worker thread touches it."""

    def __init__(self) -> None:
        self.context: Any = None
        self.page: Any = None
        self.context_ready_ms = 0

    def open(self, driver: _SharedDriver, url: str) -> Any:
        if self.page is not None:
            return self.page
        started = time.perf_counter()
        # A fresh context every time. No user-data directory and no stored profile.
        context = driver.browser().new_context()
        self.context_ready_ms = elapsed_ms(started)
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
        """Close this context only. The worker's browser stays up."""
        context = self.context
        self.page = None
        self.context = None
        if context is not None:
            context.close()


class _Worker:
    """One thread, one Chromium, and the sessions pinned to it."""

    def __init__(self, index: int, *, headless: bool, launch_args: list[str]) -> None:
        self.index = index
        self.driver = _SharedDriver(headless=headless, launch_args=launch_args)
        self.sessions: dict[str, _SessionContext] = {}
        self._queue: queue.Queue = queue.Queue()
        self._accept = threading.Lock()
        self._stopped = False
        self._thread = threading.Thread(target=self._loop, name=f"autofill-browser-{index}", daemon=True)
        self._thread.start()

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

    def submit(self, function: Callable[[], _T]) -> _T:
        done: queue.Queue = queue.Queue()
        with self._accept:
            if self._stopped:
                raise RunnerStopped("The browser runner has shut down.")
            self._queue.put((function, done))
        ok, value = done.get()
        if not ok:
            raise value
        return value

    def prewarm(self) -> None:
        self.submit(lambda: self.driver.browser())

    def shutdown(self) -> None:
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

    def _shutdown_driver(self) -> None:
        try:
            for session_id in list(self.sessions):
                self._close(session_id)
        finally:
            self.driver.stop()

    def _close(self, session_id: str) -> None:
        slot = self.sessions.pop(session_id, None)
        if slot is not None:
            slot.close()

    def _recover_driver(self) -> None:
        for session_id in list(self.sessions):
            try:
                self._close(session_id)
            except Exception:
                self.sessions.pop(session_id, None)
        try:
            self.driver.stop()
        except Exception:
            self.driver._browser = None
            self.driver._playwright = None
        self.driver = _SharedDriver(headless=self.driver._headless, launch_args=self.driver._launch_args)


class PlaywrightRunner:
    """Default runner. Manual-ATS and SSO decisions do not launch a browser."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        headless: bool = True,
        launch_args: list[str] | None = None,
        workers: int = 4,
    ) -> None:
        if workers < 1:
            raise ValueError("workers must be at least 1.")
        self.enabled = enabled
        self.headless = headless
        self.launch_args = list(launch_args or [])
        self.workers = workers
        self._assign = threading.Lock()
        self._loads = [0 for _ in range(workers)]
        self._pins: dict[str, int] = {}
        self._workers = [
            _Worker(index, headless=headless, launch_args=self.launch_args) for index in range(workers)
        ]
        self.browser_ready_ms = 0
        self.browser_prewarm_ms = 0

    @property
    def _sessions(self) -> dict[str, _SessionContext]:
        """Every open context, for tests. Mutations go through the owning worker."""
        merged: dict[str, _SessionContext] = {}
        for worker in self._workers:
            merged.update(worker.sessions)
        return merged

    @property
    def _driver(self) -> _SharedDriver:
        """Worker 0's driver. Tests that close one browser use ``workers=1``."""
        return self._workers[0].driver

    def _worker_for(self, session_id: str) -> _Worker:
        with self._assign:
            pinned = self._pins.get(session_id)
            if pinned is None:
                pinned = min(range(self.workers), key=lambda index: (self._loads[index], index))
                self._pins[session_id] = pinned
                self._loads[pinned] += 1
            return self._workers[pinned]

    def _unpin(self, session_id: str) -> None:
        with self._assign:
            pinned = self._pins.pop(session_id, None)
            if pinned is not None and self._loads[pinned] > 0:
                self._loads[pinned] -= 1

    def prewarm(self) -> int:
        """Launch each worker's Chromium. Wall time is the process prewarm."""
        if not self.enabled:
            self.browser_ready_ms = 0
            self.browser_prewarm_ms = 0
            return 0
        started = time.perf_counter()
        threads = [
            threading.Thread(target=worker.prewarm, name=f"autofill-prewarm-{worker.index}")
            for worker in self._workers
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.browser_prewarm_ms = elapsed_ms(started)
        self.browser_ready_ms = self.browser_prewarm_ms
        return self.browser_prewarm_ms

    def _submit(self, function: Callable[[], _T], session_id: str | None = None) -> _T:
        """Run ``function`` on the worker that owns ``session_id``."""
        worker = self._workers[0] if session_id is None else self._worker_for(session_id)
        return worker.submit(function)

    def _options(self, request: FillRequest, page: object | None, slot: _SessionContext | None) -> AutofillOptions:
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
            timings=self._timings(request, slot),
            max_pages=request.max_pages,
            intent_hook=request.intent_hook,
        )

    def _timings(self, request: FillRequest, slot: _SessionContext | None = None) -> dict[str, int]:
        timings = dict(request.timings or {})
        timings.setdefault("sessionCreatedMs", request.session_created_ms)
        timings.setdefault("browserPrewarmMs", self.browser_prewarm_ms)
        if slot is not None and slot.context_ready_ms:
            timings["contextReadyMs"] = slot.context_ready_ms
            timings.setdefault("browserReadyMs", slot.context_ready_ms)
        return timings

    def _run_engine(
        self,
        request: FillRequest,
        page: object | None,
        slot: _SessionContext | None = None,
    ) -> ApplicationResult:
        return autofill_application(
            request.application_url,
            request.profile,
            request.credentials,
            self._options(request, page, slot),
        )

    def _retryable(self, request: FillRequest, *, category: str) -> ApplicationResult:
        """The session stays. A later start can run it again. No second session is created."""
        return ApplicationResult(
            status=Status.FAILED_RETRYABLE,
            ats=None,
            current_step=Status.FAILED_RETRYABLE,
            login_status="NOT_REQUIRED",
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            messages=[category],
            timings=self._timings(request),
        )

    def _run_with_page(self, request: FillRequest) -> ApplicationResult:
        worker = self._worker_for(request.session_id)
        attempts = 0
        while True:
            try:
                slot = worker.sessions.get(request.session_id)
                if slot is None or slot.page is None:
                    slot = _SessionContext()
                    slot.open(worker.driver, request.application_url)
                    worker.sessions[request.session_id] = slot
                return self._run_engine(request, slot.page, slot)
            except Exception as exc:
                if is_ats_timeout(exc):
                    return self._retryable(request, category="ats_timeout")
                if not is_browser_crash(exc) or attempts >= 1:
                    if is_browser_crash(exc):
                        worker._recover_driver()
                        return self._retryable(request, category="browser_closed")
                    raise
                attempts += 1
                worker._recover_driver()

    def run(self, request: FillRequest) -> ApplicationResult:
        if is_manual_ats(request.application_url) or is_blocked_sso(request.application_url):
            return self._run_engine(request, None)
        if not self.enabled:
            raise BrowserDisabled("AUTOFILL_RUN_BROWSER is off. This process will not launch a browser.")
        try:
            return self._submit(lambda: self._run_with_page(request), request.session_id)
        except Exception as exc:
            if is_ats_timeout(exc):
                return self._retryable(request, category="ats_timeout")
            raise

    def discard(self, session_id: str) -> None:
        """Close this session's context. Other sessions and the browsers stay open."""
        worker = self._worker_for(session_id)
        try:
            worker.submit(lambda: worker._close(session_id))
        finally:
            self._unpin(session_id)

    def shutdown(self) -> None:
        """Close every session context, then stop each worker's Playwright driver."""
        for worker in self._workers:
            worker.shutdown()
