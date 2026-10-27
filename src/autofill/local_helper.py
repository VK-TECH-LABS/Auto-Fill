"""Loopback helper for the dedicated Apply Chrome.

Railway does not import this module. ``AUTOFILL_LOCAL_ATTACH`` defaults off, and
the hosted runner keeps launching its own headless Chromium. This process
binds ``127.0.0.1`` only, fills through ``autofill_application``, and never
clicks Final Submit.

Idle detach drops the CDP client. It does not close the browser object and
it does not kill Chrome, including a browser this process did not launch.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from typing import Any
from urllib.parse import urlparse

from autofill.local_attach import (
    ENV_FLAG,
    LOOPBACK,
    DedicatedApplySession,
    DefaultProfileBlocked,
    FeatureDisabled,
    LocalAttachError,
    LoopbackRequired,
    _result_brief,
    feature_enabled,
    is_default_user_data_dir,
    require_feature,
    validate_application_url,
)
from autofill.profile import CandidateProfile, ProfileError

HELPER_PORT = 9410
_MAX_BODY = 1_048_576


def default_profile_dir() -> Path:
    override = os.environ.get("AUTOFILL_LOCAL_PROFILE_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".local" / "share" / "auto-fill" / "apply-profile"


class LocalJob:
    """One fill request.

    ``stopped_before_submit`` stays true unless this job observed a submit
    (``submitted is True``). That report is status ``FAILED`` with the flag
    false. The helper still never clicks Final Submit and never marks the
    job applied.
    """

    def __init__(self, session_id: str, application_url: str) -> None:
        self.session_id = session_id
        self.application_url = application_url
        self.state = "queued"
        self.result: dict[str, Any] | None = None
        self.error = ""
        self.submitted: bool | None = None
        self.target_id = ""
        self.detached = False
        self.owns_browser = False
        self.browser_running = False

    def submit_claimed(self) -> bool:
        """True only when the page reported a submit. A missing flag is not a claim."""
        return self.submitted is True

    def public(self) -> dict[str, Any]:
        status = self.state
        if self.result is not None and not self.error:
            status = str(self.result.get("status") or status)
        if self.error:
            status = "FAILED"
        stopped_before_submit = not self.submit_claimed()
        body: dict[str, Any] = {
            "sessionId": self.session_id,
            "status": status,
            "stopped_before_submit": stopped_before_submit,
            "applicationUrl": self.application_url,
            "targetId": self.target_id,
            "submitted": self.submitted,
            "detached": self.detached,
            "ownsBrowser": self.owns_browser,
            "browserRunning": self.browser_running,
        }
        if self.result is not None:
            body["fill"] = dict(self.result)
            body["fill"]["stopped_before_submit"] = stopped_before_submit
        if self.error:
            body["error"] = self.error
        return body


class LocalHelper:
    """One dedicated profile, one worker thread, one fill at a time."""

    def __init__(
        self,
        *,
        user_data_dir: Path | None = None,
        debug_port: int | None = None,
        headless: bool = False,
        idle_detach_seconds: float = 30,
        executable: str | None = None,
        session: DedicatedApplySession | None = None,
    ) -> None:
        require_feature()
        self.idle_detach_seconds = idle_detach_seconds
        profile_dir = user_data_dir or default_profile_dir()
        if session is None and is_default_user_data_dir(profile_dir):
            raise DefaultProfileBlocked(
                "The dedicated Apply profile cannot be Chrome's default user-data-dir."
            )
        self._session = session or DedicatedApplySession(
            user_data_dir=profile_dir,
            debug_port=debug_port,
            headless=headless,
            executable=executable,
        )
        self._jobs: dict[str, LocalJob] = {}
        self._active: str | None = None
        self._generation = 0
        self._lock = threading.Lock()
        self._queue: Queue[tuple[str, dict[str, Any]] | None] = Queue()
        self._timer: threading.Timer | None = None
        self._thread = threading.Thread(target=self._loop, name="autofill-local-attach", daemon=True)
        self._thread.start()

    def submit(self, application_url: str, profile: CandidateProfile) -> LocalJob:
        url = validate_application_url(application_url)
        with self._lock:
            if self._active is not None:
                raise LocalAttachError("A fill is already running on this dedicated profile.")
            self._generation += 1
            job = LocalJob(uuid.uuid4().hex, url)
            self._jobs[job.session_id] = job
            self._active = job.session_id
        timer = self._timer
        if timer is not None:
            timer.cancel()
        self._queue.put(("fill", {"job": job, "profile": profile}))
        return job

    def get(self, session_id: str) -> LocalJob | None:
        with self._lock:
            return self._jobs.get(session_id)

    def request_detach(self, session_id: str) -> LocalJob:
        job = self.get(session_id)
        if job is None:
            raise LocalAttachError("No such session.")
        if job.state in {"queued", "running"}:
            raise LocalAttachError("The fill is still running.")
        done = threading.Event()
        self._queue.put(("detach", {"done": done, "session_id": session_id}))
        if not done.wait(timeout=30):
            raise LocalAttachError("Detach did not finish.")
        return job

    def close(self) -> None:
        """Stop the worker. Chrome stays up."""
        timer = self._timer
        if timer is not None:
            timer.cancel()
        self._queue.put(None)
        self._thread.join(timeout=5)

    def stop_launched_chrome(self) -> None:
        """Test teardown. Not used by idle detach or the HTTP detach route."""
        done = threading.Event()
        self._queue.put(("stop", {"done": done}))
        done.wait(timeout=15)

    def _loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                try:
                    self._session.detach()
                except Exception:
                    pass
                return
            kind, payload = item
            if kind == "fill":
                self._run_fill(payload["job"], payload["profile"])
            elif kind == "detach":
                self._detach(payload.get("session_id"), payload.get("generation"))
                payload["done"].set()
            elif kind == "stop":
                try:
                    self._session.stop_launched_chrome()
                finally:
                    payload["done"].set()

    def _run_fill(self, job: LocalJob, profile: CandidateProfile) -> None:
        job.state = "running"
        try:
            result, submitted = self._session.fill(job.application_url, profile)
            job.submitted = submitted
            job.result = _result_brief(result)
            job.result["stopped_before_submit"] = submitted is not True
            job.target_id = self._session.tab.target_id if self._session.tab is not None else ""
            job.state = "done"
        except Exception as exc:
            reported_submit = isinstance(exc, LocalAttachError) and "reported a submit" in str(exc)
            if reported_submit:
                job.submitted = True
            job.error = type(exc).__name__
            job.state = "error"
        finally:
            job.owns_browser = self._session.owns_chrome
            job.browser_running = self._session.browser_running()
            with self._lock:
                if self._active == job.session_id:
                    self._active = None
            self._arm_idle()

    def _arm_idle(self) -> None:
        timer = self._timer
        if timer is not None:
            timer.cancel()
        if self.idle_detach_seconds < 0:
            return
        generation = self._generation
        timer = threading.Timer(self.idle_detach_seconds, self._enqueue_detach, args=(generation,))
        timer.daemon = True
        self._timer = timer
        timer.start()

    def _enqueue_detach(self, generation: int) -> None:
        if generation != self._generation:
            return
        done = threading.Event()
        self._queue.put(("detach", {"done": done, "session_id": None, "generation": generation}))
        done.wait(timeout=30)

    def _detach(self, session_id: str | None, generation: int | None = None) -> None:
        with self._lock:
            if self._active is not None:
                return
            if generation is not None and generation != self._generation:
                return
        self._session.detach()
        running = self._session.browser_running()
        with self._lock:
            if session_id is None:
                jobs = list(self._jobs.values())
            elif session_id in self._jobs:
                jobs = [self._jobs[session_id]]
            else:
                jobs = []
        for job in jobs:
            if job.state in {"queued", "running"}:
                continue
            job.detached = True
            job.browser_running = running
            job.owns_browser = self._session.owns_chrome


class _HelperServer(ThreadingHTTPServer):
    helper: LocalHelper


class _Handler(BaseHTTPRequestHandler):
    server: _HelperServer

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/health":
            self._send(
                200,
                {
                    "status": "ok",
                    "service": "auto-fill-local",
                    "mode": "dedicated-profile",
                    "submit": "human",
                },
            )
            return
        session_id = _session_id(path)
        if session_id is None:
            self._send(404, {"detail": "Not found"})
            return
        job = self.server.helper.get(session_id)
        if job is None:
            self._send(404, {"detail": "Not found"})
            return
        self._send(200, job.public())

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/sessions":
            self._create()
            return
        session_id = _detach_id(path)
        if session_id is None:
            self._send(404, {"detail": "Not found"})
            return
        try:
            job = self.server.helper.request_detach(session_id)
        except LocalAttachError as exc:
            status = 404 if "No such" in str(exc) else 409
            self._send(status, {"detail": str(exc), "stopped_before_submit": True})
            return
        self._send(200, job.public())

    def _create(self) -> None:
        try:
            payload = _read_json(self)
            url = str(payload.get("applicationUrl") or "")
            profile_raw = payload.get("profile")
            if not isinstance(profile_raw, dict):
                raise LocalAttachError("profile must be an object.")
            profile = CandidateProfile.from_dict(profile_raw)
            job = self.server.helper.submit(url, profile)
        except LocalAttachError as exc:
            busy = "already running" in str(exc)
            self._send(409 if busy else 400, {"detail": str(exc), "stopped_before_submit": True})
            return
        except (ProfileError, ValueError) as exc:
            self._send(400, {"detail": str(exc), "stopped_before_submit": True})
            return
        self._send(202, job.public())

    def _send(self, status: int, body: dict[str, Any]) -> None:
        encoded = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: Any) -> None:
        return


def _session_id(path: str) -> str | None:
    prefix = "/v1/sessions/"
    if not path.startswith(prefix) or path.endswith("/detach"):
        return None
    session_id = path[len(prefix) :]
    if not session_id or "/" in session_id:
        return None
    return session_id


def _detach_id(path: str) -> str | None:
    suffix = "/detach"
    if not path.startswith("/v1/sessions/") or not path.endswith(suffix):
        return None
    session_id = path[len("/v1/sessions/") : -len(suffix)]
    if not session_id or "/" in session_id:
        return None
    return session_id


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length < 0 or length > _MAX_BODY:
        raise LocalAttachError("The request body is too large.")
    raw = handler.rfile.read(length)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise LocalAttachError("The request body is not JSON.") from exc
    if not isinstance(payload, dict):
        raise LocalAttachError("The request body must be an object.")
    return payload


def serve(
    *,
    host: str = LOOPBACK,
    port: int = 0,
    helper: LocalHelper | None = None,
    user_data_dir: Path | None = None,
    debug_port: int | None = None,
    headless: bool = False,
    idle_detach_seconds: float = 30,
    executable: str | None = None,
) -> _HelperServer:
    """Listen on ``127.0.0.1``. Any other host is refused."""
    if not feature_enabled():
        raise FeatureDisabled(
            f"Set {ENV_FLAG}=1 to run the local helper. The hosted service never sets this."
        )
    if host != LOOPBACK:
        raise LoopbackRequired(f"Refusing to bind {host!r}. The local helper listens on 127.0.0.1 only.")
    require_feature()
    server = _HelperServer((host, port), _Handler)
    server.helper = helper or LocalHelper(
        user_data_dir=user_data_dir,
        debug_port=debug_port,
        headless=headless,
        idle_detach_seconds=idle_detach_seconds,
        executable=executable,
    )
    threading.Thread(target=server.serve_forever, name="autofill-local-http", daemon=True).start()
    return server


def main() -> int:
    """CLI entry. Leaves the dedicated Chrome running after idle detach."""
    if not feature_enabled():
        print(f"Set {ENV_FLAG}=1 to run the local helper. Railway does not set this.", flush=True)
        return 2
    port = int(os.environ.get("AUTOFILL_LOCAL_HELPER_PORT", str(HELPER_PORT)))
    raw_cdp = os.environ.get("AUTOFILL_LOCAL_CDP_PORT", "").strip()
    debug_port = int(raw_cdp) if raw_cdp else None
    headless = os.environ.get("AUTOFILL_LOCAL_HEADLESS", "").strip() == "1"
    idle = float(os.environ.get("AUTOFILL_LOCAL_IDLE_DETACH_SECONDS", "30"))
    try:
        server = serve(
            host=LOOPBACK,
            port=port,
            debug_port=debug_port,
            headless=headless,
            idle_detach_seconds=idle,
        )
    except (FeatureDisabled, LoopbackRequired, OSError) as exc:
        print(str(exc), flush=True)
        return 2
    bound = int(server.server_address[1])
    print(f"listening http://{LOOPBACK}:{bound}", flush=True)
    try:
        server.helper._thread.join()
    except KeyboardInterrupt:
        server.helper.close()
        server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
