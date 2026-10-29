"""Loopback helper for the dedicated Apply Chrome.

Railway does not import this module. ``AUTOFILL_LOCAL_ATTACH`` defaults off, and
the hosted runner keeps launching its own headless Chromium. This process
binds ``127.0.0.1`` only, fills through ``autofill_application``, and never
clicks Final Submit.

When a fill stops on ``MANUAL_ANSWER_REQUIRED``, ``GET /v1/sessions/{id}``
includes ``question`` (``id``, ``prompt``, ``type``, ``options``).
``POST /v1/sessions/{id}/answer`` writes that answer in the same Chrome tab
and returns ``verified``. Callers persist ``confirmedByEmployee`` only when
``verified`` is true. This process does not store that flag.
``stopped_before_submit`` is false only when ``submitted`` is true.

Idle detach drops the CDP client. It does not close the browser object and
it does not kill Chrome, including a browser this process did not launch.
While a question is pending, idle detach waits so the answer lands on the
open tab.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from typing import Any
from urllib.parse import urlparse

from autofill.dom_ui import canonical_blocker_kind, fit_choice_values, match_open_item
from autofill.engine import ApplicationResult, Status
from autofill.local_attach import (
    ENV_FLAG,
    LOOPBACK,
    CancelGate,
    DedicatedApplySession,
    DefaultProfileBlocked,
    FeatureDisabled,
    FillCancelled,
    LocalAttachError,
    LoopbackRequired,
    _result_brief,
    feature_enabled,
    is_default_user_data_dir,
    require_feature,
    validate_application_url,
)
from autofill.mapping import option_stance
from autofill.profile import CandidateProfile, ProfileError
from autofill.safeguards import HumanSubmissionRequired

logger = logging.getLogger("autofill.local_helper")

HELPER_PORT = 9410
_MAX_BODY = 1_048_576
_ANSWER_WAIT_SECONDS = 60
_NON_QUESTION_KINDS = frozenset({"CAPTCHA", "LOGIN", "ACCOUNT", "VERIFICATION", "NO_FORM", "RESUME"})
# Finished and replaced jobs kept so a late status/answer gets 410, not a fill.
_MAX_JOBS = 8
_BINDING_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
REPLACED = "REPLACED"
CANCELLED = "CANCELLED"


class BindingError(ValueError):
    """The create body carried a binding the helper cannot trust."""


def parse_binding(raw: Any) -> dict[str, str] | None:
    """TileArc Apply binding: session, candidate, job, profile fingerprint, expiry.

    Returns ``None`` when the caller sent no binding (older pages). A binding
    that is present but malformed or already expired is refused. Values are
    ids and digests only; no candidate PII is accepted here.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BindingError("binding must be an object.")
    out: dict[str, str] = {}
    for key, required in (
        ("tilearcSessionId", True),
        ("candidateId", True),
        ("jobId", False),
        ("profileFingerprint", True),
        ("employeeIdHash", False),
    ):
        value = raw.get(key)
        text = "" if value is None or isinstance(value, bool) else str(value).strip()
        if not text:
            if required:
                raise BindingError(f"binding.{key} is required.")
            continue
        if not _BINDING_ID.match(text):
            raise BindingError(f"binding.{key} is not a valid id.")
        out[key] = text
    expires = _expires_epoch(raw.get("expiresAt"))
    if expires is not None:
        if expires <= time.time():
            raise BindingError("binding expired.")
        out["expiresAt"] = str(int(expires))
    return out


def _expires_epoch(value: Any) -> float | None:
    """Epoch seconds from epoch ms, epoch seconds, or an ISO-8601 string."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000 if number > 10_000_000_000 else number
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None:
        return number / 1000 if number > 10_000_000_000 else number
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BindingError("binding.expiresAt is not a date.") from exc
    if parsed.tzinfo is None:
        raise BindingError("binding.expiresAt needs a timezone.")
    return parsed.timestamp()


def same_binding(left: dict[str, str] | None, right: dict[str, str] | None) -> bool:
    if not left or not right:
        return False
    return all(
        left.get(key, "") == right.get(key, "")
        for key in ("tilearcSessionId", "candidateId", "jobId", "profileFingerprint")
    )


def _binding_expired(binding: dict[str, str] | None) -> bool:
    if not binding or not binding.get("expiresAt"):
        return False
    try:
        return float(binding["expiresAt"]) <= time.time()
    except ValueError:
        return True


def _accepts(fn: Any, name: str) -> bool:
    try:
        return name in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


def _accepts_cancel(fn: Any) -> bool:
    return _accepts(fn, "cancel")


class AnswerRejected(LocalAttachError):
    """The answer was not applied. ``verified`` on the body is false."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        super().__init__(str(body.get("reason") or body.get("detail") or "rejected"))
        self.status = status
        self.body = body


def default_profile_dir() -> Path:
    """Dedicated Apply profile. Never Chrome's default user-data-dir.

    Windows uses ``%LOCALAPPDATA%\\TileArc\\AutoFill\\apply-profile``.
    macOS uses ``~/Library/Application Support/TileArc/AutoFill/apply-profile``.
    Other platforms keep ``~/.local/share/auto-fill/apply-profile``.
    ``AUTOFILL_LOCAL_PROFILE_DIR`` overrides these. ``LocalHelper`` still
    refuses a path that is the daily Chrome directory.
    """
    override = os.environ.get("AUTOFILL_LOCAL_PROFILE_DIR", "").strip()
    if override:
        return Path(override)
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(local) if local else Path.home() / "AppData" / "Local"
        return root / "TileArc" / "AutoFill" / "apply-profile"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "TileArc" / "AutoFill" / "apply-profile"
    return Path.home() / ".local" / "share" / "auto-fill" / "apply-profile"


class LocalJob:
    """One fill request.

    ``stopped_before_submit`` stays true unless this job observed a submit
    (``submitted is True``). That report is status ``FAILED`` with the flag
    false. The helper still never clicks Final Submit and never marks the
    job applied.

    ``profile`` and ``open_items`` stay on this object. They are not part of
    the public status. ``question`` is the pending prompt, and only that shape
    is returned.
    """

    def __init__(
        self,
        session_id: str,
        application_url: str,
        profile: CandidateProfile | None = None,
        binding: dict[str, str] | None = None,
    ) -> None:
        self.session_id = session_id
        self.application_url = application_url
        self.profile = profile
        self.binding = dict(binding) if binding else None
        self.gate = CancelGate()
        # "replaced" (a newer Apply) or "cancelled" (the page asked). Empty while live.
        self.ended = ""
        self.state = "queued"
        self.result: dict[str, Any] | None = None
        self.error = ""
        self.submitted: bool | None = None
        self.target_id = ""
        self.detached = False
        self.owns_browser = False
        self.browser_running = False
        self.open_items: list[dict[str, Any]] = []
        self.blocking: dict[str, Any] | None = None
        self.question: dict[str, Any] | None = None

    def submit_claimed(self) -> bool:
        """True only when the page reported a submit. A missing flag is not a claim."""
        return self.submitted is True

    @property
    def superseded(self) -> bool:
        return bool(self.ended)

    def end(self, reason: str) -> None:
        """Stop this job for good. The profile and the pending question are dropped."""
        if not self.ended:
            self.ended = reason
        self.gate.cancel(reason)
        self.profile = None
        self.question = None
        self.blocking = None
        self.open_items = []

    def public(self) -> dict[str, Any]:
        status = self.state
        if self.result is not None and not self.error:
            status = str(self.result.get("status") or status)
        if self.error:
            status = "FAILED"
        if self.ended and not self.submit_claimed():
            status = REPLACED if self.ended == "replaced" else CANCELLED
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
        if self.binding:
            body["binding"] = {
                key: self.binding[key]
                for key in ("tilearcSessionId", "candidateId", "jobId", "profileFingerprint")
                if key in self.binding
            }
        if self.ended:
            body["ended"] = self.ended
        if self.result is not None:
            body["fill"] = dict(self.result)
            body["fill"]["stopped_before_submit"] = stopped_before_submit
        if status == Status.MANUAL_ANSWER_REQUIRED and self.question is not None:
            body["question"] = {
                "id": self.question["id"],
                "prompt": self.question["prompt"],
                "type": self.question["type"],
                "options": list(self.question["options"]),
            }
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
        # The worker's job (fill or answer). The newest live job is ``_current``.
        self._active: str | None = None
        self._current: str | None = None
        # True after any fill reached the window; the next job reloads first.
        self._window_used = False
        self._generation = 0
        self._lock = threading.Lock()
        self._queue: Queue[tuple[str, dict[str, Any]] | None] = Queue()
        self._timer: threading.Timer | None = None
        self._thread = threading.Thread(target=self._loop, name="autofill-local-attach", daemon=True)
        self._thread.start()

    def submit(
        self,
        application_url: str,
        profile: CandidateProfile,
        binding: dict[str, str] | None = None,
    ) -> LocalJob:
        """Queue one fill. A bound create replaces the previous job.

        Same binding and URL as the live job: that job is returned (a double
        click does not start a second fill). Different binding: the previous
        job is cancelled at its next field or page, marked ``replaced``, and
        its profile is dropped. An unbound create keeps the old rule and is
        refused while a live fill runs.
        """
        url = validate_application_url(application_url)
        with self._lock:
            current = self._jobs.get(self._current) if self._current else None
            if (
                binding is not None
                and current is not None
                and not current.superseded
                and current.application_url == url
                and same_binding(current.binding, binding)
                and current.state not in {"error"}
            ):
                return current
            active = self._jobs.get(self._active) if self._active else None
            if active is not None and not active.superseded and binding is None:
                raise LocalAttachError("A fill is already running on this dedicated profile.")
            for old in {job.session_id: job for job in (current, active) if job is not None}.values():
                if not old.superseded:
                    old.end("replaced")
                    logger.info("session=%s superseded reason=replaced", old.session_id)
            self._generation += 1
            job = LocalJob(uuid.uuid4().hex, url, profile, binding)
            self._jobs[job.session_id] = job
            self._current = job.session_id
            self._active = job.session_id
            self._prune()
        logger.info(
            "session=%s created bound=%s",
            job.session_id,
            "1" if binding else "0",
        )
        timer = self._timer
        if timer is not None:
            timer.cancel()
        self._queue.put(("fill", {"job": job, "profile": profile}))
        return job

    def get(self, session_id: str) -> LocalJob | None:
        with self._lock:
            return self._jobs.get(session_id)

    def cancel(self, session_id: str) -> LocalJob:
        """Stop one job at its next field or page. Idempotent. Chrome stays up."""
        with self._lock:
            job = self._jobs.get(session_id)
            if job is None:
                raise LocalAttachError("No such session.")
            if not job.superseded:
                job.end("cancelled")
                logger.info("session=%s superseded reason=cancelled", job.session_id)
            if self._current == session_id:
                self._current = None
        return job

    def _prune(self) -> None:
        """Drop the oldest ended or finished jobs. Call with the lock held."""
        keep = {self._current, self._active}
        removable = [
            sid
            for sid, job in self._jobs.items()
            if sid not in keep and (job.superseded or job.state in {"done", "error", "replaced"})
        ]
        excess = len(self._jobs) - _MAX_JOBS
        for sid in removable[: max(0, excess)]:
            self._jobs.pop(sid, None)

    def request_answer(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Apply one employee answer on the worker thread. The value is not logged.

        Only the current job takes answers. A replaced, cancelled, or expired
        job gets 410 and nothing is written. A binding on the answer must
        match the job's candidate and TileArc session.
        """
        job = self.get(session_id)
        if job is None:
            raise LocalAttachError("No such session.")
        with self._lock:
            if job.superseded or (self._current is not None and self._current != session_id):
                raise AnswerRejected(410, _failure(job, "replaced"))
            if _binding_expired(job.binding):
                job.end("cancelled")
                if self._current == session_id:
                    self._current = None
                raise AnswerRejected(410, _failure(job, "expired"))
            claimed = payload.get("binding")
            if claimed is not None and job.binding:
                if not isinstance(claimed, dict) or any(
                    str(claimed.get(key) or "").strip() != job.binding.get(key, "")
                    for key in ("tilearcSessionId", "candidateId")
                ):
                    raise AnswerRejected(409, _failure(job, "binding_mismatch"))
            if self._active is not None:
                raise LocalAttachError("A fill is already running on this dedicated profile.")
            if job.state in {"queued", "running"}:
                raise LocalAttachError("The fill is still running.")
            self._generation += 1
            self._active = job.session_id
        timer = self._timer
        if timer is not None:
            timer.cancel()
        box: dict[str, Any] = {}
        done = threading.Event()
        self._queue.put(("answer", {"job": job, "payload": payload, "done": done, "box": box}))
        if not done.wait(timeout=_ANSWER_WAIT_SECONDS):
            with self._lock:
                if self._active == job.session_id:
                    self._active = None
            raise LocalAttachError("The answer did not finish.")
        error = box.get("error")
        if isinstance(error, Exception):
            raise error
        body = box.get("body")
        if not isinstance(body, dict):
            raise LocalAttachError("The answer did not finish.")
        return body

    def request_detach(self, session_id: str) -> LocalJob:
        job = self.get(session_id)
        if job is None:
            raise LocalAttachError("No such session.")
        if job.superseded:
            # The CDP client may belong to the newer job. Leave it alone.
            return job
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
            elif kind == "answer":
                self._run_answer(payload)
            elif kind == "detach":
                self._detach(payload.get("session_id"), payload.get("generation"))
                payload["done"].set()
            elif kind == "stop":
                try:
                    self._session.stop_launched_chrome()
                finally:
                    payload["done"].set()

    def _run_fill(self, job: LocalJob, profile: CandidateProfile) -> None:
        if job.superseded:
            job.state = "replaced"
            self._finish_active(job)
            return
        job.state = "running"
        try:
            fill = self._session.fill
            kwargs: dict[str, Any] = {}
            if _accepts_cancel(fill):
                kwargs["cancel"] = job.gate
            if _accepts(fill, "fresh"):
                # An earlier job wrote into this window. Start from a clean page.
                kwargs["fresh"] = self._window_used
            self._window_used = True
            result, submitted = fill(job.application_url, profile, **kwargs)
            job.submitted = submitted
            if job.superseded:
                job.state = "replaced"
            else:
                self._remember_result(job, result)
                job.state = "done"
            job.target_id = self._session.tab.target_id if self._session.tab is not None else ""
        except FillCancelled:
            job.state = "replaced"
            logger.info("session=%s fill_stopped reason=%s", job.session_id, job.ended or "cancelled")
        except Exception as exc:
            reported_submit = isinstance(exc, LocalAttachError) and "reported a submit" in str(exc)
            if reported_submit:
                job.submitted = True
            job.error = type(exc).__name__
            job.state = "error"
            logger.info("session=%s fill_failed error=%s detail=%s", job.session_id, type(exc).__name__, exc)
        finally:
            self._finish_active(job)

    def _run_answer(self, payload: dict[str, Any]) -> None:
        job: LocalJob = payload["job"]
        box: dict[str, Any] = payload["box"]
        try:
            box["body"] = self._answer_job(job, payload["payload"])
        except FillCancelled:
            box["error"] = AnswerRejected(410, _failure(job, "replaced"))
        except AnswerRejected as exc:
            box["error"] = exc
        except HumanSubmissionRequired:
            box["error"] = AnswerRejected(409, _failure(job, "not_committed"))
        except Exception as exc:
            reported_submit = isinstance(exc, LocalAttachError) and "reported a submit" in str(exc)
            if reported_submit:
                job.submitted = True
                job.error = type(exc).__name__
                job.state = "error"
                box["error"] = AnswerRejected(409, _failure(job, "submit_reported"))
            else:
                box["error"] = AnswerRejected(409, _failure(job, type(exc).__name__))
        finally:
            self._finish_active(job)
            payload["done"].set()

    def _answer_job(self, job: LocalJob, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        if job.superseded:
            raise AnswerRejected(410, _failure(job, "replaced"))
        if job.profile is None or job.question is None or _public_status(job) != Status.MANUAL_ANSWER_REQUIRED:
            raise AnswerRejected(409, _failure(job, "not_at_human_stop"))
        if not _has_identifier(payload):
            raise AnswerRejected(422, _failure(job, "question_required"))
        item = _match_item(job, payload)
        if item is None:
            raise AnswerRejected(409, _failure(job, "stale_blocker"))
        if not str(item.get("selector") or "").strip():
            raise AnswerRejected(409, _failure(job, "element_not_found"))
        target = _prepared_target(job, item, payload)
        apply = getattr(self._session, "apply_answer", None)
        if apply is None:
            raise AnswerRejected(409, _failure(job, "element_not_found"))
        if _accepts_cancel(apply):
            report = apply(job.application_url, job.profile, target, cancel=job.gate)
        else:
            report = apply(job.application_url, job.profile, target)
        if job.superseded:
            raise AnswerRejected(410, _failure(job, "replaced"))
        if not isinstance(report, dict):
            report = {"verified": False, "filled": False, "reason": "not_committed"}
        if _optional_submit_flag(report):
            job.submitted = True
            job.error = "LocalAttachError"
            job.state = "error"
            raise AnswerRejected(409, _failure(job, "submit_reported"))
        if not report.get("verified"):
            code = 422 if report.get("reason") == "needs_option" else 200
            body = _answer_body(job, report, started)
            if code != 200:
                raise AnswerRejected(code, body)
            return body
        self._adopt_follow(job, report, target)
        return _answer_body(job, report, started)

    def _remember_result(self, job: LocalJob, result: ApplicationResult) -> None:
        job.result = _result_brief(result)
        job.result["stopped_before_submit"] = not job.submit_claimed()
        job.open_items = [dict(item) for item in result.open_items]
        job.blocking = dict(result.blocking) if isinstance(result.blocking, dict) else None
        job.question = pending_question(result.status, job.blocking, job.open_items)

    def _adopt_follow(self, job: LocalJob, report: dict[str, Any], target: dict[str, Any]) -> None:
        result = report.get("result")
        if isinstance(result, ApplicationResult):
            self._remember_result(job, result)
            return
        if any(key in report for key in ("status", "open_items", "blocking")):
            if "open_items" in report:
                job.open_items = [dict(item) for item in report.get("open_items") or []]
            if "blocking" in report:
                blocking = report.get("blocking")
                job.blocking = dict(blocking) if isinstance(blocking, dict) else None
            status = str(report.get("status") or "")
            if status and job.result is not None:
                job.result["status"] = status
                job.result["stopped_before_submit"] = not job.submit_claimed()
            elif job.result is not None:
                status = str(job.result.get("status") or "")
            job.question = pending_question(status, job.blocking, job.open_items)
            return
        job.open_items = [item for item in job.open_items if not _same_target(item, target)]
        job.blocking = dict(job.open_items[0]) if job.open_items else None
        if job.open_items:
            if job.result is not None:
                job.result["status"] = Status.MANUAL_ANSWER_REQUIRED
                job.result["stopped_before_submit"] = not job.submit_claimed()
            job.question = pending_question(Status.MANUAL_ANSWER_REQUIRED, job.blocking, job.open_items)
            return
        job.blocking = None
        job.question = None
        if job.result is not None:
            job.result["status"] = Status.READY_FOR_HUMAN_SUBMIT
            job.result["stopped_before_submit"] = not job.submit_claimed()

    def _finish_active(self, job: LocalJob) -> None:
        if job.superseded:
            job.end(job.ended)
            with self._lock:
                if self._active == job.session_id:
                    self._active = None
            return
        job.owns_browser = self._session.owns_chrome
        job.browser_running = self._session.browser_running()
        tab = getattr(self._session, "tab", None)
        if tab is not None:
            job.detached = False
            target_id = str(getattr(tab, "target_id", "") or "")
            if target_id:
                job.target_id = target_id
        with self._lock:
            if self._active == job.session_id:
                self._active = None
        if job.question is None or job.state == "error":
            self._arm_idle()
            return
        timer = self._timer
        if timer is not None:
            timer.cancel()

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
        self._send(410 if job.superseded else 200, job.public())

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/sessions":
            self._create()
            return
        session_id = _action_id(path, "answer")
        if session_id is not None:
            self._answer(session_id)
            return
        session_id = _action_id(path, "cancel")
        if session_id is not None:
            try:
                job = self.server.helper.cancel(session_id)
            except LocalAttachError:
                self._send(404, {"detail": "Not found", "stopped_before_submit": True})
                return
            self._send(200, job.public())
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
            binding = parse_binding(payload.get("binding"))
            profile = CandidateProfile.from_dict(profile_raw)
            job = self.server.helper.submit(url, profile, binding)
        except LocalAttachError as exc:
            busy = "already running" in str(exc)
            self._send(409 if busy else 400, {"detail": str(exc), "stopped_before_submit": True})
            return
        except (ProfileError, ValueError) as exc:
            self._send(400, {"detail": str(exc), "stopped_before_submit": True})
            return
        self._send(202, job.public())

    def _answer(self, session_id: str) -> None:
        try:
            payload = _read_json(self)
            body = self.server.helper.request_answer(session_id, payload)
        except AnswerRejected as exc:
            self._send(exc.status, exc.body)
            return
        except LocalAttachError as exc:
            missing = "No such" in str(exc)
            busy = "already running" in str(exc) or "still running" in str(exc) or "did not finish" in str(exc)
            code = 404 if missing else 409 if busy else 400
            self._send(
                code,
                {
                    "detail": str(exc),
                    "verified": False,
                    "filled": False,
                    "stopped_before_submit": True,
                },
            )
            return
        self._send(200, body)

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
    if not path.startswith(prefix) or path.endswith(("/detach", "/answer", "/cancel")):
        return None
    session_id = path[len(prefix) :]
    if not session_id or "/" in session_id:
        return None
    return session_id


def _action_id(path: str, action: str) -> str | None:
    suffix = f"/{action}"
    prefix = "/v1/sessions/"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    session_id = path[len(prefix) : -len(suffix)]
    if not session_id or "/" in session_id:
        return None
    return session_id


def _detach_id(path: str) -> str | None:
    return _action_id(path, "detach")


def question_view(item: dict[str, Any] | None) -> dict[str, Any] | None:
    """Public question. ``id`` is the question id, else the hash, else the field key."""
    if not isinstance(item, dict):
        return None
    kind = canonical_blocker_kind(item.get("kind"))
    if kind in _NON_QUESTION_KINDS:
        return None
    qid = str(
        item.get("questionId") or item.get("questionHash") or item.get("fieldKey") or item.get("id") or ""
    ).strip()
    prompt = str(item.get("text") or item.get("prompt") or "").strip()
    if not qid or not prompt:
        return None
    options: list[str] = []
    for option in item.get("options") or []:
        if isinstance(option, dict):
            label = str(option.get("label") or option.get("value") or "").strip()
        else:
            label = str(option).strip()
        if label and label not in options:
            options.append(label)
    return {
        "id": qid,
        "prompt": prompt,
        "type": str(item.get("answerType") or item.get("type") or "text"),
        "options": options,
    }


def pending_question(
    status: str,
    blocking: dict[str, Any] | None,
    open_items: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The one question a caller should ask, or none when the fill is not waiting."""
    if status != Status.MANUAL_ANSWER_REQUIRED:
        return None
    view = question_view(blocking)
    if view is None:
        for item in open_items:
            view = question_view(item)
            if view is not None:
                break
    if view is None:
        return None
    if not view["options"]:
        for item in open_items:
            richer = question_view(item)
            if richer is not None and richer["id"] == view["id"] and richer["options"]:
                view["options"] = list(richer["options"])
                break
    return view


def _public_status(job: LocalJob) -> str:
    return str(job.public()["status"])


def _failure(job: LocalJob, reason: str, *, options: list[str] | None = None) -> dict[str, Any]:
    """An answer that must not be stored. The employee's text is not included."""
    body: dict[str, Any] = {
        "verified": False,
        "filled": False,
        "reason": reason,
        "stopped_before_submit": not job.submit_claimed(),
        "sessionId": job.session_id,
        "status": _public_status(job),
        "submitted": job.submitted,
    }
    if options:
        body["options"] = list(options)
    question = job.question if body["status"] == Status.MANUAL_ANSWER_REQUIRED else None
    if question is not None:
        body["question"] = {
            "id": question["id"],
            "prompt": question["prompt"],
            "type": question["type"],
            "options": list(question["options"]),
        }
    return body


def _answer_body(job: LocalJob, report: dict[str, Any], started: float) -> dict[str, Any]:
    verified = bool(report.get("verified"))
    spent = int((time.perf_counter() - started) * 1000)
    body: dict[str, Any] = {
        "verified": verified,
        "filled": verified,
        "stopped_before_submit": not job.submit_claimed(),
        "sessionId": job.session_id,
        "status": _public_status(job),
        "submitted": job.submitted,
        "ms": max(0, spent),
    }
    if not verified:
        body["reason"] = str(report.get("reason") or "not_committed")
        options = [str(option) for option in report.get("options") or [] if str(option).strip()]
        if options:
            body["options"] = options
    question = job.question if body["status"] == Status.MANUAL_ANSWER_REQUIRED else None
    if question is not None:
        body["question"] = {
            "id": question["id"],
            "prompt": question["prompt"],
            "type": question["type"],
            "options": list(question["options"]),
        }
    return body


def _optional_submit_flag(report: dict[str, Any]) -> bool:
    return report.get("submitted") is True


def _has_identifier(payload: dict[str, Any]) -> bool:
    return any(str(payload.get(key) or "").strip() for key in ("id", "questionId", "questionHash", "fieldKey"))


def _match_item(job: LocalJob, payload: dict[str, Any]) -> dict[str, Any] | None:
    public_id = str(payload.get("id") or "").strip()
    question_id = str(payload.get("questionId") or "").strip() or public_id
    question_hash = str(payload.get("questionHash") or "").strip()
    field_key = str(payload.get("fieldKey") or "").strip()
    item = match_open_item(
        job.open_items,
        question_id=question_id,
        question_hash=question_hash,
        field_key=field_key,
        prefer=job.blocking if isinstance(job.blocking, dict) else None,
    )
    if item is not None:
        return item
    if not public_id:
        return None
    for candidate in job.open_items:
        if public_id in {
            str(candidate.get("questionId") or ""),
            str(candidate.get("questionHash") or ""),
            str(candidate.get("fieldKey") or ""),
        }:
            return candidate
    return None


def _answer_values(job: LocalJob, payload: dict[str, Any]) -> list[str]:
    raw_values = payload.get("values", [])
    if raw_values is None:
        raw_values = []
    if not isinstance(raw_values, list):
        raise AnswerRejected(400, _failure(job, "invalid_values"))
    values: list[str] = []
    for item in raw_values:
        text = str(item)
        if len(text) > 2000:
            raise AnswerRejected(422, _failure(job, "value_too_long"))
        if text.strip():
            values.append(text)
    if len(values) > 20:
        raise AnswerRejected(422, _failure(job, "value_too_long"))
    value = payload.get("value")
    if value is None and "answer" in payload:
        value = payload.get("answer")
    if value is not None:
        text = str(value)
        if len(text) > 8000:
            raise AnswerRejected(422, _failure(job, "value_too_long"))
        if text.strip():
            values = values or [text]
    return values


def _prepared_target(job: LocalJob, item: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """The open item plus the employee's value. Consent still requires confirm."""
    kind_name = canonical_blocker_kind(item.get("kind"))
    listed = [str(option) for option in item.get("options") or [] if str(option).strip()]
    confirm = payload.get("confirm") is True
    if kind_name == "CONSENT":
        raw = payload.get("value")
        if raw is None:
            raw = payload.get("answer")
        if str(raw or "").strip().casefold() != "confirm" or not confirm:
            raise AnswerRejected(422, _failure(job, "needs_option", options=["confirm"]))
        target = dict(item)
        target["value"] = "confirm"
        target["values"] = ["confirm"]
        target["check"] = True
        return target
    values = _answer_values(job, payload)
    if not values:
        raise AnswerRejected(422, _failure(job, "needs_option", options=listed))
    reason, canonical = fit_choice_values(item, values)
    if reason != "ok" or not canonical:
        raise AnswerRejected(422, _failure(job, "needs_option", options=listed))
    if kind_name == "CONSENT_CHOICE":
        stance = option_stance(canonical[0])
        if stance == "agree" and not confirm:
            raise AnswerRejected(422, _failure(job, "needs_confirm", options=listed))
        if stance not in {"agree", "decline"}:
            raise AnswerRejected(422, _failure(job, "needs_option", options=listed))
    target = dict(item)
    target["value"] = canonical[0]
    target["values"] = list(canonical)
    return target


def _same_target(item: dict[str, Any], target: dict[str, Any]) -> bool:
    selector = str(target.get("selector") or "")
    if selector and selector == str(item.get("selector") or ""):
        return True
    digest = str(target.get("questionHash") or target.get("questionId") or "")
    return bool(digest) and digest in {item.get("questionId"), item.get("questionHash")}


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
