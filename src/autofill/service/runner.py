"""Run ``autofill_application`` on a pool of browser threads.

Playwright's sync driver cannot be shared across threads, and one thread
would run every session one after another. Each worker thread owns one
prewarmed Chromium. A session is pinned to one worker. Every session still
gets a new browser context that is never reused. Sync Playwright objects are
touched only on their worker thread. Passwords are not logged.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
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

DEFAULT_LAUNCH_ARGS = [
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--renderer-process-limit=2",
    "--js-flags=--max-old-space-size=256",
]
_DEFAULT_MEMORY_LIMIT_BYTES = 1536 * 1024 * 1024
_MEMORY_HEADROOM_NUMERATOR = 85
_MEMORY_HEADROOM_DENOMINATOR = 100
_UNLIMITED_CGROUP_BYTES = 1 << 60
_CAPACITY_ATTEMPTS = 3
_CAPACITY_PAUSE_SECONDS = 0.25
_BLOCKED_HOSTS = (
    "google-analytics.com",
    "googletagmanager.com",
    "doubleclick.net",
    "segment.io",
    "segment.com",
    "hotjar.com",
    "mixpanel.com",
)


class PageUnavailable(RuntimeError):
    """The session page is missing or the tab crashed. ``status_code`` is 409 or 410."""

    def __init__(self, status_code: int) -> None:
        super().__init__("page unavailable")
        self.status_code = status_code


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _parse_bytes(text: str) -> int | None:
    token = text.strip()
    if not token.isdigit():
        return None
    return int(token)


def _stat_bytes(text: str, key: str) -> int | None:
    prefix = key + " "
    for line in text.splitlines():
        if line.startswith(prefix):
            return _parse_bytes(line[len(prefix) :])
    return None


def _finite_limit(text: str) -> int | None:
    """A cgroup maximum in bytes. ``None`` means the controller reported no cap."""
    token = text.strip()
    if token == "max" or not token:
        return None
    value = _parse_bytes(token)
    if value is None or value <= 0 or value >= _UNLIMITED_CGROUP_BYTES:
        return None
    return value


def _env_memory_limit() -> int | None:
    raw = os.environ.get("AUTOFILL_MEMORY_LIMIT_MB", "").strip()
    if not raw:
        return None
    try:
        megabytes = float(raw)
    except ValueError:
        return None
    if megabytes <= 0:
        return None
    return int(megabytes * 1024 * 1024)


def _cgroup_relative() -> str | None:
    text = _read_text(Path("/proc/self/cgroup"))
    if text is None:
        return None
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[0] == "0" and parts[1] == "":
            return parts[2].lstrip("/")
    return None


def _walk_for(start: Path, filename: str, stop: Path) -> Path | None:
    node = start
    while True:
        if (node / filename).is_file():
            return node
        if node == stop or node.parent == node:
            return None
        node = node.parent


def _discover_cgroup_v2() -> Path | None:
    relative = _cgroup_relative()
    root = Path("/sys/fs/cgroup")
    if relative is not None:
        found = _walk_for(root / relative, "memory.current", root)
        if found is not None:
            return found
    if (root / "memory.current").is_file():
        return root
    return None


def _discover_cgroup_v1() -> Path | None:
    text = _read_text(Path("/proc/self/cgroup"))
    root = Path("/sys/fs/cgroup")
    if text is not None:
        for line in text.splitlines():
            parts = line.split(":", 2)
            if len(parts) != 3 or "memory" not in parts[1].split(","):
                continue
            relative = parts[2].lstrip("/")
            for start in (root / "memory" / relative, root / relative):
                found = _walk_for(start, "memory.usage_in_bytes", root)
                if found is not None:
                    return found
    fallback = root / "memory"
    if (fallback / "memory.usage_in_bytes").is_file():
        return fallback
    return None


def _v2_usage(directory: Path) -> int | None:
    current_text = _read_text(directory / "memory.current")
    if current_text is None:
        return None
    current = _parse_bytes(current_text)
    if current is None:
        return None
    stat = _read_text(directory / "memory.stat") or ""
    inactive = _stat_bytes(stat, "inactive_file")
    if inactive is None:
        return current
    return max(0, current - inactive)


def _v1_usage(directory: Path) -> int | None:
    usage_text = _read_text(directory / "memory.usage_in_bytes")
    if usage_text is None:
        return None
    usage = _parse_bytes(usage_text)
    if usage is None:
        return None
    stat = _read_text(directory / "memory.stat") or ""
    inactive = _stat_bytes(stat, "total_inactive_file")
    if inactive is None:
        inactive = _stat_bytes(stat, "inactive_file")
    if inactive is None:
        return usage
    return max(0, usage - inactive)


def _directory_limit(directory: Path) -> int | None:
    for name in ("memory.max", "memory.limit_in_bytes"):
        text = _read_text(directory / name)
        if text is None:
            continue
        return _finite_limit(text)
    return None


def _kb_field(line: str) -> int | None:
    parts = line.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return int(parts[1]) * 1024


def _rollup_bytes(text: str) -> int | None:
    """PSS when the kernel reports it, otherwise private clean plus private dirty."""
    pss: int | None = None
    private = 0
    saw_private = False
    for line in text.splitlines():
        if line.startswith("Pss:"):
            pss = _kb_field(line)
        elif line.startswith("Private_Clean:") or line.startswith("Private_Dirty:"):
            value = _kb_field(line)
            if value is not None:
                private += value
                saw_private = True
    if pss is not None:
        return pss
    if saw_private:
        return private
    return None


def _ppid(stat_text: str) -> str | None:
    end = stat_text.rfind(")")
    fields = stat_text[end + 2 :].split() if end >= 0 else []
    if len(fields) >= 2:
        return fields[1]
    return None


def proportional_memory_bytes(proc_root: Path | None = None, start_pid: int | None = None) -> int:
    """PSS or USS of one process and its descendants. Shared pages are not added per process."""
    root = proc_root or Path("/proc")
    if not root.is_dir():
        return 0
    pending = [start_pid if start_pid is not None else os.getpid()]
    seen: set[int] = set()
    total = 0
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        rollup = _read_text(root / str(pid) / "smaps_rollup")
        if rollup is not None:
            amount = _rollup_bytes(rollup)
            if amount is not None:
                total += amount
        try:
            entries = list(root.iterdir())
        except OSError:
            entries = []
        for entry in entries:
            if not entry.name.isdigit() or int(entry.name) in seen:
                continue
            stat = _read_text(entry / "stat")
            if stat is not None and _ppid(stat) == str(pid):
                pending.append(int(entry.name))
    return total


def container_memory_bytes(
    *,
    v2_dir: Path | None = None,
    v1_dir: Path | None = None,
    proc_root: Path | None = None,
    start_pid: int | None = None,
) -> int:
    """Container usage: cgroup v2, then cgroup v1, then PSS/USS. Never a sum of RSS."""
    if v2_dir is None and v1_dir is None and proc_root is None:
        discovered = _discover_cgroup_v2()
        if discovered is not None:
            usage = _v2_usage(discovered)
            if usage is not None:
                return usage
        discovered_v1 = _discover_cgroup_v1()
        if discovered_v1 is not None:
            usage = _v1_usage(discovered_v1)
            if usage is not None:
                return usage
        return proportional_memory_bytes()
    if v2_dir is not None:
        usage = _v2_usage(v2_dir)
        if usage is not None:
            return usage
    if v1_dir is not None:
        usage = _v1_usage(v1_dir)
        if usage is not None:
            return usage
    if proc_root is not None:
        return proportional_memory_bytes(proc_root, start_pid)
    return 0


def memory_limit_bytes(cgroup_dir: Path | None = None) -> int:
    """``AUTOFILL_MEMORY_LIMIT_MB``, else 85% of a finite cgroup max, else 1536 MB."""
    override = _env_memory_limit()
    if override is not None:
        return override
    directory = cgroup_dir
    if directory is None:
        directory = _discover_cgroup_v2() or _discover_cgroup_v1()
    if directory is not None:
        maximum = _directory_limit(directory)
        if maximum is not None:
            return maximum * _MEMORY_HEADROOM_NUMERATOR // _MEMORY_HEADROOM_DENOMINATOR
    return _DEFAULT_MEMORY_LIMIT_BYTES


def memory_allows_session() -> bool:
    """True when usage is within the limit. Recheck briefly before refusing."""
    limit = memory_limit_bytes()
    for attempt in range(_CAPACITY_ATTEMPTS):
        if container_memory_bytes() <= limit:
            return True
        if attempt + 1 < _CAPACITY_ATTEMPTS:
            time.sleep(_CAPACITY_PAUSE_SECONDS)
    return False


def _block_heavy(page) -> None:
    """Drop video, fonts, and analytics. Documents, scripts, and form calls stay."""

    def handle(route) -> None:
        request = route.request
        url = request.url.casefold()
        if request.resource_type in {"media", "font"} or any(host in url for host in _BLOCKED_HOSTS):
            route.abort()
            return
        route.continue_()

    page.route("**/*", handle)


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
        context = driver.browser().new_context(viewport={"width": 1280, "height": 800})
        self.context_ready_ms = elapsed_ms(started)
        try:
            page = context.new_page()
            _block_heavy(page)
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
        workers: int = 2,
    ) -> None:
        if workers < 1:
            raise ValueError("workers must be at least 1.")
        self.enabled = enabled
        self.headless = headless
        self.launch_args = list(DEFAULT_LAUNCH_ARGS if launch_args is None else launch_args)
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
                    if not memory_allows_session():
                        return self._retryable(request, category="capacity_busy")
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
                        return self._retryable(request, category="browser_crash")
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

    def _pinned_worker(self, session_id: str) -> _Worker | None:
        with self._assign:
            pinned = self._pins.get(session_id)
        if pinned is None:
            return None
        return self._workers[pinned]

    def screenshot(self, session_id: str) -> tuple[bytes, str] | None:
        """JPEG of the current page, taken on that session's worker. Not stored."""
        worker = self._pinned_worker(session_id)
        if worker is None:
            return None

        def take() -> tuple[bytes, str] | None:
            slot = worker.sessions.get(session_id)
            if slot is None or slot.page is None:
                return None
            from urllib.parse import urlparse

            try:
                host = urlparse(slot.page.url).hostname or ""
                image = slot.page.screenshot(type="jpeg", quality=60)
            except Exception as exc:
                if not is_browser_crash(exc):
                    raise
                try:
                    worker._close(session_id)
                except Exception:
                    worker.sessions.pop(session_id, None)
                raise PageUnavailable(410) from None
            return image, host

        return worker.submit(take)

    def interact(self, session_id: str, action: dict) -> bool:
        """One human action on the session page. Typed text is not logged."""
        worker = self._pinned_worker(session_id)
        if worker is None:
            return False

        def apply() -> bool:
            slot = worker.sessions.get(session_id)
            if slot is None or slot.page is None:
                return False
            page = slot.page
            try:
                kind = action.get("action")
                if kind == "click":
                    page.mouse.click(float(action["x"]), float(action["y"]))
                elif kind == "type":
                    page.keyboard.type(str(action.get("text") or ""))
                elif kind == "key":
                    key = str(action.get("key") or "")
                    page.keyboard.press(" " if key == "Space" else key)
                elif kind == "scroll":
                    page.mouse.wheel(0, float(action.get("dy") or 0))
                else:
                    return False
            except Exception as exc:
                if not is_browser_crash(exc):
                    raise
                try:
                    worker._close(session_id)
                except Exception:
                    worker.sessions.pop(session_id, None)
                raise PageUnavailable(410) from None
            return True

        return worker.submit(apply)

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
