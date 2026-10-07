"""Run ``autofill_application`` on one browser thread per process.

Playwright sync objects stay on the thread that created them. The HTTP
handlers hop onto that thread and then return. Passwords are not logged.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from autofill.ats import is_blocked_sso, is_manual_ats
from autofill.credentials import MemoryCredentialProvider
from autofill.engine import ApplicationResult, AutofillOptions, autofill_application
from autofill.models import JobContext
from autofill.profile import CandidateProfile

_T = TypeVar("_T")


class BrowserDisabled(RuntimeError):
    """``AUTOFILL_RUN_BROWSER`` is off, so this process will not launch Chromium."""


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


class FillRunner(Protocol):
    """Starts and continues a fill. Tests supply a fake that never opens a browser."""

    def run(self, request: FillRequest) -> ApplicationResult:
        """Fill or continue. Resume bytes are never selected here."""

    def discard(self, session_id: str) -> None:
        """Close any browser resources for ``session_id``."""

    def shutdown(self) -> None:
        """Close every browser this runner still holds."""


class _PageSlot:
    """Playwright objects for one session. Only the browser thread touches these."""

    def __init__(self) -> None:
        self.playwright: object | None = None
        self.browser: object | None = None
        self.page: object | None = None

    def open(self, url: str, *, headless: bool, launch_args: list[str]) -> object:
        if self.page is not None:
            return self.page
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=headless, args=launch_args)
        page = browser.new_page()
        page.goto(url)
        self.playwright = playwright
        self.browser = browser
        self.page = page
        return page

    def close(self) -> None:
        browser = self.browser
        playwright = self.playwright
        self.page = None
        self.browser = None
        self.playwright = None
        if browser is not None:
            browser.close()  # type: ignore[attr-defined]
        if playwright is not None:
            playwright.stop()  # type: ignore[attr-defined]


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
        self._pages: dict[str, _PageSlot] = {}
        self._queue: queue.Queue = queue.Queue()
        self._stopped = False
        self._thread = threading.Thread(target=self._loop, name="autofill-browser", daemon=True)
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

    def _call(self, function: Callable[[], _T]) -> _T:
        done: queue.Queue = queue.Queue()
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
        )

    def _run_engine(self, request: FillRequest, page: object | None) -> ApplicationResult:
        return autofill_application(
            request.application_url,
            request.profile,
            request.credentials,
            self._options(request, page),
        )

    def _run_with_page(self, request: FillRequest) -> ApplicationResult:
        slot = self._pages.get(request.session_id)
        if slot is None:
            slot = _PageSlot()
            slot.open(request.application_url, headless=self.headless, launch_args=self.launch_args)
            self._pages[request.session_id] = slot
        return self._run_engine(request, slot.page)

    def _close(self, session_id: str) -> None:
        slot = self._pages.pop(session_id, None)
        if slot is not None:
            slot.close()

    def run(self, request: FillRequest) -> ApplicationResult:
        if is_manual_ats(request.application_url) or is_blocked_sso(request.application_url):
            return self._run_engine(request, None)
        if not self.enabled:
            raise BrowserDisabled("AUTOFILL_RUN_BROWSER is off. This process will not launch a browser.")
        return self._call(lambda: self._run_with_page(request))

    def discard(self, session_id: str) -> None:
        self._call(lambda: self._close(session_id))

    def shutdown(self) -> None:
        if self._stopped:
            return
        self._stopped = True

        def _close_all() -> None:
            for session_id in list(self._pages):
                self._close(session_id)

        try:
            self._call(_close_all)
        finally:
            self._queue.put(None)
            self._thread.join(timeout=5)
