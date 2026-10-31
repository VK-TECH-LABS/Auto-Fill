"""Loopback helper for the dedicated Apply Chrome.

Railway does not import this module. ``AUTOFILL_LOCAL_ATTACH`` defaults off, and
the hosted runner keeps launching its own headless Chromium. This process
binds ``127.0.0.1`` only, fills through ``autofill_application``, and never
clicks Final Submit.

When a fill stops on ``MANUAL_ANSWER_REQUIRED``, ``GET /v1/sessions/{id}``
includes ``question`` (``id``, ``prompt``, ``type``, ``options``, ``popup``).
The helper also shows a Save & Continue popup on the employer tab
(:mod:`autofill.answer_popup`). ``question.popup.state`` is ``shown`` while it
waits, and ``answered`` with ``values`` once the employee clicked Save &
Continue. The TileArc panel saves that answer to Application Answers, checks
the save, and then posts it to the answer route below. The helper never
stores an answer in TileArc itself.
``POST /v1/sessions/{id}/answer`` writes that answer in the same Chrome tab
and returns ``verified``. Callers persist ``confirmedByEmployee`` only when
``verified`` is true. This process does not store that flag.
``stopped_before_submit`` is false only when ``submitted`` is true.

Idle detach drops the CDP client. It does not close the browser object and
it does not kill Chrome, including a browser this process did not launch.
While a question is pending, idle detach waits so the answer lands on the
open tab. The current job keeps its resume bytes across that detach when the
application can still continue. Cancel, replace, and a binding mismatch drop
them.
``POST /v1/sessions/{id}/continue`` reattaches after a crash or a retryable
failure and fills the page already showing. It does not reload the Apply URL.
The same fill receives the in-memory credential provider, so a secret posted
earlier is still used after continue. ``verified: true`` on that body is the
person's confirmation that they opened the verification email.
``POST /v1/sessions/{id}/credentials`` remembers one secret for this candidate
and employer (``tenantKey``). ``mode`` ``store`` only remembers it. ``login``
and ``register`` type it on the open page. The password is not written to the
status JSON, the log, or the Chrome profile directory.
``POST /v1/sessions/{id}/sign-out`` clears that employer's cookies and leaves
the secret in memory so the next fill can sign in again.
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue
from typing import Any
from urllib.parse import urlparse

from autofill.answer_popup import new_nonce
from autofill.credentials import Credentials, MemoryCredentialProvider, credential_scope
from autofill.dom_ui import canonical_blocker_kind, fit_choice_values, match_open_item
from autofill.engine import ApplicationResult, Status, is_browser_crash
from autofill.local_attach import (
    ENV_FLAG,
    LOOPBACK,
    CancelGate,
    DedicatedApplySession,
    DefaultProfileBlocked,
    FeatureDisabled,
    FillCancelled,
    LocalAttachError,
    LocalResume,
    LoopbackRequired,
    _public_account,
    _result_brief,
    feature_enabled,
    is_default_user_data_dir,
    require_feature,
    same_document,
    validate_application_url,
)
from autofill.mapping import option_stance
from autofill.profile import CandidateProfile, ProfileError
from autofill.resume_fetch import MAX_RESUME_BYTES, resume_filename
from autofill.safeguards import HumanSubmissionRequired

logger = logging.getLogger("autofill.local_helper")

HELPER_PORT = 9410
_MAX_BODY = 1_048_576
_ANSWER_WAIT_SECONDS = 60
# A popup answer TileArc has not confirmed by then reopens the popup with an error.
_POPUP_SAVE_TIMEOUT_SECONDS = 45
_POPUP_SYNC_WAIT_SECONDS = 1.5
_NON_QUESTION_KINDS = frozenset({"CAPTCHA", "LOGIN", "ACCOUNT", "VERIFICATION", "NO_FORM", "RESUME"})
# Finished and replaced jobs kept so a late status/answer gets 410, not a fill.
_MAX_JOBS = 8
_BINDING_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
REPLACED = "REPLACED"
CANCELLED = "CANCELLED"
# A staged resume must be claimed by a create within this window.
_STAGED_RESUME_TTL_SECONDS = 120
_MAX_STAGED_RESUMES = 2
_RESUME_REF = re.compile(r"^[a-f0-9]{32}$")
# Recoveries of one job. The next continue after this stops without typing.
_CONTINUE_CAP = 2
_DETACH_KEEP_STATUSES = frozenset(
    {
        Status.FAILED_RETRYABLE,
        Status.MANUAL_ANSWER_REQUIRED,
        Status.RESUME_UPLOAD_REQUIRED,
    }
)


class StagedResume:
    """Resume bytes the extension posted for the next bound create. Memory only.

    The extension fetched them from TileArc with the employee's session. The
    helper keeps them until a create with the same TileArc session and
    candidate claims them, or until the TTL ends. They are never written to
    disk, never returned by any route, and never logged.
    """

    def __init__(self, ref: str, name: str, data: bytes, tilearc_session_id: str, candidate_id: str) -> None:
        self.ref = ref
        self.name = name
        self.data = data
        self.tilearc_session_id = tilearc_session_id
        self.candidate_id = candidate_id
        self.created = time.monotonic()

    def expired(self, now: float | None = None) -> bool:
        return ((now if now is not None else time.monotonic()) - self.created) > _STAGED_RESUME_TTL_SECONDS


def parse_resume_claim(raw: Any) -> str | None:
    """``resume`` on a create: ``{"resumeRef": <hex>}``. Absent means no resume."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BindingError("resume must be an object.")
    ref = str(raw.get("resumeRef") or "").strip()
    if not _RESUME_REF.match(ref):
        raise BindingError("resume.resumeRef is not valid.")
    return ref


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
        as_number = float(text)
    except ValueError:
        as_number = None
    if as_number is not None:
        return as_number / 1000 if as_number > 10_000_000_000 else as_number
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BindingError("binding.expiresAt is not a date.") from exc
    if parsed.tzinfo is None:
        raise BindingError("binding.expiresAt needs a timezone.")
    return parsed.timestamp()


def _same_live_job(job: LocalJob | None, url: str, binding: dict[str, str] | None) -> bool:
    return bool(
        job is not None
        and binding is not None
        and not job.superseded
        and job.application_url == url
        and same_binding(job.binding, binding)
    )


def _binding_matches(job: LocalJob, claimed: Any) -> bool:
    """True when the caller sent no binding, or the session and candidate match."""
    if claimed is None or not job.binding:
        return True
    if not isinstance(claimed, dict):
        return False
    return all(
        str(claimed.get(key) or "").strip() == job.binding.get(key, "")
        for key in ("tilearcSessionId", "candidateId")
    )


def _can_continue(job: LocalJob) -> bool:
    """True after a crashed fill or a retryable employer failure. A reported submit is not."""
    if job.superseded or job.submit_claimed():
        return False
    if job.state == "error":
        return True
    if job.state != "done" or job.result is None:
        return False
    return str(job.result.get("status") or "") == Status.FAILED_RETRYABLE


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


class LocalProgress:
    """Where this job last looked. Selectors stay in memory and off the status."""

    def __init__(
        self,
        binding: dict[str, str],
        target_id: str,
        url: str,
        step: str,
        selectors: frozenset[str],
        nav_block: tuple | None = None,
    ) -> None:
        self.binding = dict(binding)
        self.target_id = target_id
        self.url = url
        self.step = step
        self.selectors = selectors
        self.nav_block = nav_block

    def view(self) -> dict[str, str]:
        """Target, URL, and step. No selectors and no candidate values."""
        return {"targetId": self.target_id, "url": self.url, "step": self.step}


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
        resume: LocalResume | None = None,
    ) -> None:
        self.session_id = session_id
        self.application_url = application_url
        self.profile = profile
        self.binding = dict(binding) if binding else None
        # Selected TileArc resume. Dropped on end, detach, and prune.
        self.resume = resume
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
        # Employer-tab Save & Continue popup: nonce, questionId, state, values, answeredAt.
        self.popup: dict[str, Any] | None = None
        self.progress: LocalProgress | None = None
        self.continue_attempts = 0
        # Workday account registered here. Continue without verified does not sign in.
        self.verify_pending = False
        self._credential_candidate = ""
        # Candidate preference from session create. False checks no terms box.
        self.standard_terms_authorized = False

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
        self.drop_resume()
        self.question = None
        self.popup = None
        self.blocking = None
        self.open_items = []

    def drop_resume(self) -> None:
        if self.resume is not None:
            self.resume.drop()

    def resume_state(self) -> str:
        """``none`` (no file sent), ``held``, ``failed`` (held, the employer did not
        accept it), ``uploaded`` (verifier accepted it), or ``dropped``."""
        if self.resume is None:
            return "none"
        if self.resume.uploaded:
            return "uploaded"
        if not self.resume.held:
            return "dropped"
        return "failed" if self.resume.failure else "held"

    def note_progress(self, *, target_id: str, url: str, step: str, selectors: frozenset[str]) -> None:
        """Record the page we are about to fill. The binding is copied with it."""
        block = self.progress.nav_block if self.progress is not None else None
        kept_target = target_id or (self.progress.target_id if self.progress is not None else "")
        self.progress = LocalProgress(
            binding=dict(self.binding or {}),
            target_id=kept_target,
            url=url,
            step=step,
            selectors=selectors,
            nav_block=block,
        )
        if kept_target:
            self.target_id = kept_target

    def note_nav_block(self, block: tuple) -> None:
        """The Next that already moved this page. A later continue will not press it again."""
        if self.progress is None:
            self.note_progress(target_id=self.target_id, url=self.application_url, step="", selectors=frozenset())
        if self.progress is not None:
            self.progress.nav_block = block

    def resume_view(self) -> dict[str, Any]:
        """``{state, reason, siteErrors}``. No file name and no bytes."""
        state = self.resume_state()
        view: dict[str, Any] = {"state": state}
        failure = self.resume.failure if self.resume is not None else None
        if state == "failed" and failure:
            view["reason"] = str(failure.get("reason") or "not_confirmed")
            view["siteErrors"] = [str(item)[:200] for item in failure.get("siteErrors") or []][:5]
        return view

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
            # State and failure reason only. The file name and bytes are not part of the status.
            "resume": self.resume_view(),
        }
        if self.progress is not None:
            body["progress"] = self.progress.view()
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
            popup = _popup_view(self)
            if popup is not None:
                body["question"]["popup"] = popup
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
        self._staged: dict[str, StagedResume] = {}
        # Candidate + tenantKey. Memory only. Wiped for a candidate when a different one applies.
        self._credentials = MemoryCredentialProvider()
        # The worker's job (fill or answer). The newest live job is ``_current``.
        self._active: str | None = None
        self._current: str | None = None
        # True after any fill reached the window; the next job reloads first.
        self._window_used = False
        self._generation = 0
        self._peeking = False
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
        resume_ref: str | None = None,
        standard_terms_authorized: bool = False,
    ) -> LocalJob:
        """Queue one fill. A bound create replaces the previous job.

        ``resume_ref`` claims a resume staged by ``stage_resume``. It needs a
        binding with the same TileArc session and candidate the file was
        staged for. An unknown, expired, or mismatched ref is refused and no
        fill starts.

        Same binding and URL as the live job: that job is returned (a double
        click does not start a second fill). The same binding after ``error``
        or ``FAILED_RETRYABLE`` continues on the open page and does not reload.
        Different binding: the previous job is cancelled at its next field or
        page, marked ``replaced``, and its profile is dropped. An unbound
        create keeps the old rule and is refused while a live fill runs.
        """
        url = validate_application_url(application_url)
        if resume_ref is not None and binding is None:
            raise BindingError("A resume needs a TileArc binding.")
        recover = False
        with self._lock:
            current = self._jobs.get(self._current) if self._current else None
            same = _same_live_job(current, url, binding)
            if same and current is not None and current.state in {"queued", "running"}:
                if resume_ref is not None:
                    self._staged.pop(resume_ref, None)
                if standard_terms_authorized:
                    current.standard_terms_authorized = True
                return current
            if same and current is not None and _can_continue(current):
                active = self._jobs.get(self._active) if self._active else None
                if active is not None and not active.superseded and active.session_id != current.session_id:
                    raise LocalAttachError("A fill is already running on this dedicated profile.")
                if current.continue_attempts >= _CONTINUE_CAP:
                    raise AnswerRejected(409, _failure(current, "continue_limit"))
                if resume_ref is not None:
                    claimed = self._claim_resume(resume_ref, binding)
                    if current.resume is not None:
                        current.resume.drop()
                    current.resume = claimed
                current.profile = profile
                if standard_terms_authorized:
                    current.standard_terms_authorized = True
                current.continue_attempts += 1
                current.error = ""
                current.state = "queued"
                self._generation += 1
                self._active = current.session_id
                job = current
                recover = True
            elif same and current is not None:
                if resume_ref is not None:
                    # A double click restaged the same file. The live job keeps its copy.
                    self._staged.pop(resume_ref, None)
                if standard_terms_authorized:
                    current.standard_terms_authorized = True
                return current
            else:
                resume = self._claim_resume(resume_ref, binding) if resume_ref is not None else None
                active = self._jobs.get(self._active) if self._active else None
                if active is not None and not active.superseded and binding is None:
                    raise LocalAttachError("A fill is already running on this dedicated profile.")
                incoming = (binding or {}).get("candidateId") or ""
                for old in {job.session_id: job for job in (current, active) if job is not None}.values():
                    if not old.superseded:
                        previous = (old.binding or {}).get("candidateId") or ""
                        if incoming and previous and incoming != previous:
                            self._credentials.drop_candidate(previous)
                        old.end("replaced")
                        logger.info("session=%s superseded reason=replaced", old.session_id)
                self._generation += 1
                job = LocalJob(uuid.uuid4().hex, url, profile, binding, resume)
                job.standard_terms_authorized = bool(standard_terms_authorized)
                self._jobs[job.session_id] = job
                self._current = job.session_id
                self._active = job.session_id
                self._prune()
        if recover:
            logger.info("session=%s continue attempt=%s", job.session_id, job.continue_attempts)
        else:
            logger.info(
                "session=%s created bound=%s resume=%s",
                job.session_id,
                "1" if binding else "0",
                job.resume_state(),
            )
        timer = self._timer
        if timer is not None:
            timer.cancel()
        self._queue.put(("fill", {"job": job, "profile": profile, "recover": recover}))
        return job

    def stage_resume(self, data: bytes, filename: str, tilearc_session_id: str, candidate_id: str) -> StagedResume:
        """Hold one resume for the next bound create. PDF only, 10 MB cap, memory only."""
        if not data or len(data) > MAX_RESUME_BYTES:
            raise BindingError("resume must be a PDF up to 10 MB.")
        if not data.startswith(b"%PDF-"):
            raise BindingError("resume must be a PDF.")
        if not _BINDING_ID.match(tilearc_session_id or "") or not _BINDING_ID.match(candidate_id or ""):
            raise BindingError("resume needs the TileArc session and candidate.")
        name = resume_filename(filename)
        if not name.lower().endswith(".pdf"):
            name = "Resume.pdf"
        staged = StagedResume(uuid.uuid4().hex, name, bytes(data), tilearc_session_id, candidate_id)
        with self._lock:
            self._sweep_staged()
            # A newer file for another Apply replaces older staged copies first.
            while len(self._staged) >= _MAX_STAGED_RESUMES:
                oldest = min(self._staged.values(), key=lambda row: row.created)
                self._staged.pop(oldest.ref, None)
            self._staged[staged.ref] = staged
        logger.info("resume staged size_kb=%s", max(1, len(data) // 1024))
        return staged

    def _sweep_staged(self) -> None:
        """Drop expired staged files. Call with the lock held."""
        now = time.monotonic()
        for ref in [ref for ref, row in self._staged.items() if row.expired(now)]:
            self._staged.pop(ref, None)

    def staged_count(self) -> int:
        with self._lock:
            self._sweep_staged()
            return len(self._staged)

    def _claim_resume(self, ref: str, binding: dict[str, str] | None) -> LocalResume:
        """Single use. Call with the lock held."""
        self._sweep_staged()
        staged = self._staged.pop(ref, None)
        if staged is None:
            raise BindingError("resume expired or unknown.")
        if (
            not binding
            or staged.tilearc_session_id != binding.get("tilearcSessionId")
            or staged.candidate_id != binding.get("candidateId")
        ):
            raise BindingError("resume belongs to another Apply.")
        return LocalResume(name=staged.name, mime="application/pdf", data=staged.data)

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
            gone = self._jobs.pop(sid, None)
            if gone is not None:
                gone.drop_resume()

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
            if not _binding_matches(job, payload.get("binding")):
                job.drop_resume()
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

    def request_resume(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """``retry`` or ``confirm_manual`` at a RESUME_UPLOAD_REQUIRED stop, on the worker."""
        mode = str(payload.get("action") or "").strip()
        job = self.get(session_id)
        if job is None:
            raise LocalAttachError("No such session.")
        if mode not in {"retry", "confirm_manual"}:
            raise AnswerRejected(422, _failure(job, "action_required"))
        with self._lock:
            if job.superseded or (self._current is not None and self._current != session_id):
                raise AnswerRejected(410, _failure(job, "replaced"))
            if _binding_expired(job.binding):
                job.end("cancelled")
                if self._current == session_id:
                    self._current = None
                raise AnswerRejected(410, _failure(job, "expired"))
            if not _binding_matches(job, payload.get("binding")):
                job.drop_resume()
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
        self._queue.put(("resume", {"job": job, "mode": mode, "done": done, "box": box}))
        if not done.wait(timeout=_ANSWER_WAIT_SECONDS):
            with self._lock:
                if self._active == job.session_id:
                    self._active = None
            raise LocalAttachError("The resume check did not finish.")
        error = box.get("error")
        if isinstance(error, Exception):
            raise error
        body = box.get("body")
        if not isinstance(body, dict):
            raise LocalAttachError("The resume check did not finish.")
        return body

    def _run_resume(self, payload: dict[str, Any]) -> None:
        job: LocalJob = payload["job"]
        box: dict[str, Any] = payload["box"]
        try:
            box["body"] = self._resume_job(job, payload["mode"])
        except FillCancelled:
            box["error"] = AnswerRejected(410, _failure(job, "replaced"))
        except AnswerRejected as exc:
            box["error"] = exc
        except Exception as exc:
            if isinstance(exc, LocalAttachError) and "reported a submit" in str(exc):
                job.submitted = True
                job.error = type(exc).__name__
                job.state = "error"
                box["error"] = AnswerRejected(409, _failure(job, "submit_reported"))
            else:
                box["error"] = AnswerRejected(409, _failure(job, type(exc).__name__))
        finally:
            self._finish_active(job)
            payload["done"].set()

    def _resume_job(self, job: LocalJob, mode: str) -> dict[str, Any]:
        if job.superseded:
            raise AnswerRejected(410, _failure(job, "replaced"))
        if job.profile is None or _public_status(job) != Status.RESUME_UPLOAD_REQUIRED:
            raise AnswerRejected(409, _failure(job, "not_at_resume_stop"))
        if job.resume is None or (mode == "retry" and not job.resume.held):
            raise AnswerRejected(409, _failure(job, "not_held"))
        action = getattr(self._session, "resume_action", None)
        if action is None:
            raise AnswerRejected(409, _failure(job, "unsupported"))
        kwargs: dict[str, Any] = {}
        if _accepts_cancel(action):
            kwargs["cancel"] = job.gate
        report = action(job.application_url, job.profile, job.resume, mode, **kwargs)
        if job.superseded:
            raise AnswerRejected(410, _failure(job, "replaced"))
        if not isinstance(report, dict):
            report = {"verified": False, "reason": "not_confirmed"}
        if _optional_submit_flag(report):
            job.submitted = True
            job.error = "LocalAttachError"
            job.state = "error"
            raise AnswerRejected(409, _failure(job, "submit_reported"))
        verified = bool(report.get("verified"))
        if verified:
            self._adopt_follow(job, report, {})
            job.popup = None
            self._present_popup(job)
        body = job.public()
        body["verified"] = verified
        if not verified:
            body["reason"] = str(report.get("reason") or "not_confirmed")
        return body

    def request_continue(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Reattach and fill the page already showing. Does not reload the Apply URL."""
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
            if not _binding_matches(job, payload.get("binding")):
                job.drop_resume()
                raise AnswerRejected(409, _failure(job, "binding_mismatch"))
            if not _can_continue(job):
                raise AnswerRejected(409, _failure(job, "not_continuable"))
            if job.continue_attempts >= _CONTINUE_CAP:
                raise AnswerRejected(409, _failure(job, "continue_limit"))
            if job.profile is None:
                raise AnswerRejected(409, _failure(job, "not_continuable"))
            if payload.get("verified") is True:
                job.verify_pending = False
            if self._active is not None:
                raise LocalAttachError("A fill is already running on this dedicated profile.")
            if job.state in {"queued", "running"}:
                raise LocalAttachError("The fill is still running.")
            job.continue_attempts += 1
            job.error = ""
            job.state = "queued"
            self._generation += 1
            self._active = job.session_id
        timer = self._timer
        if timer is not None:
            timer.cancel()
        box: dict[str, Any] = {}
        done = threading.Event()
        logger.info("session=%s continue attempt=%s", job.session_id, job.continue_attempts)
        self._queue.put(("fill", {"job": job, "profile": job.profile, "recover": True, "done": done, "box": box}))
        if not done.wait(timeout=_ANSWER_WAIT_SECONDS):
            with self._lock:
                if self._active == job.session_id:
                    self._active = None
            raise LocalAttachError("The continue did not finish.")
        error = box.get("error")
        if isinstance(error, Exception):
            raise error
        body = box.get("body")
        if not isinstance(body, dict):
            raise LocalAttachError("The continue did not finish.")
        return body

    def request_credentials(self, session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Remember a secret, and type it when ``mode`` is ``login`` or ``register``.

        ``store`` does not touch the page. The password is not returned.
        """
        job = self.get(session_id)
        if job is None:
            raise LocalAttachError("No such session.")
        mode, email, username, password, confirm = _credential_fields(job, payload)
        if payload.get("standardTerms") is True:
            job.standard_terms_authorized = True
        _require_credential_candidate(job, payload)
        scope = credential_scope(job.application_url)
        if mode == "store":
            self._save_credential(job, email, username, password, scope)
            body = job.public()
            body["stored"] = True
            body["outcome"] = "stored"
            body["tenantKey"] = scope
            logger.info("session=%s credentials_stored=true mode=store", job.session_id)
            return body
        with self._lock:
            if job.superseded:
                raise AnswerRejected(410, _failure(job, "replaced"))
            if self._active is not None or job.state in {"queued", "running"}:
                raise LocalAttachError("A fill is already running on this dedicated profile.")
            self._active = job.session_id
            job.state = "queued"
        box: dict[str, Any] = {}
        done = threading.Event()
        self._queue.put(
            (
                "credentials",
                {
                    "job": job,
                    "mode": mode,
                    "email": email,
                    "username": username,
                    "password": password,
                    "confirm": confirm,
                    "scope": scope,
                    "standard_terms": payload.get("standardTerms") is True
                    or bool(getattr(job, "standard_terms_authorized", False)),
                    "verified": payload.get("verified") is True,
                    "done": done,
                    "box": box,
                },
            )
        )
        if not done.wait(timeout=_ANSWER_WAIT_SECONDS):
            with self._lock:
                if self._active == job.session_id:
                    self._active = None
            raise LocalAttachError("The credentials action did not finish.")
        error = box.get("error")
        if isinstance(error, Exception):
            raise error
        report = box.get("body")
        if not isinstance(report, dict):
            raise LocalAttachError("The credentials action did not finish.")
        return report

    def request_sign_out(self, session_id: str) -> dict[str, Any]:
        """Clear this employer's cookies. The remembered secret stays in memory."""
        job = self.get(session_id)
        if job is None:
            raise LocalAttachError("No such session.")
        with self._lock:
            if job.superseded:
                raise AnswerRejected(410, _failure(job, "replaced"))
            if self._active is not None or job.state in {"queued", "running"}:
                raise LocalAttachError("The fill is still running.")
            self._active = job.session_id
        box: dict[str, Any] = {}
        done = threading.Event()
        self._queue.put(("signout", {"job": job, "done": done, "box": box}))
        if not done.wait(timeout=30):
            with self._lock:
                if self._active == job.session_id:
                    self._active = None
            raise LocalAttachError("Sign out did not finish.")
        error = box.get("error")
        if isinstance(error, Exception):
            raise error
        body = box.get("body")
        if not isinstance(body, dict):
            raise LocalAttachError("Sign out did not finish.")
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
                self._run_fill(payload)
            elif kind == "credentials":
                self._run_credentials(payload)
            elif kind == "signout":
                self._run_sign_out(payload)
            elif kind == "answer":
                self._run_answer(payload)
            elif kind == "resume":
                self._run_resume(payload)
            elif kind == "popup":
                try:
                    self._sync_popup(payload["job"])
                except Exception as exc:
                    logger.info("session=%s popup_sync_failed error=%s", payload["job"].session_id, type(exc).__name__)
                finally:
                    with self._lock:
                        self._peeking = False
                    payload["done"].set()
            elif kind == "detach":
                self._detach(payload.get("session_id"), payload.get("generation"))
                payload["done"].set()
            elif kind == "stop":
                try:
                    self._session.stop_launched_chrome()
                finally:
                    payload["done"].set()

    def _progress_allows(self, job: LocalJob) -> bool:
        """Reattach, then refuse when the live tab is not the recorded page."""
        progress = job.progress
        if progress is None:
            return True
        ensure = getattr(self._session, "ensure", None)
        if ensure is not None:
            ensure(job.application_url)
        live = getattr(self._session, "live_identity", None)
        if live is None:
            return True
        target, url = live()
        if progress.target_id and target != progress.target_id:
            return False
        if progress.url and (not url or not same_document(progress.url, url)):
            return False
        if progress.binding and job.binding and not same_binding(progress.binding, job.binding):
            return False
        return True

    def _capture_progress(self, job: LocalJob, page: Any) -> None:
        """Store binding, target, URL, and PageMark for the page about to be filled."""
        url = ""
        step = ""
        selectors: frozenset[str] = frozenset()
        try:
            url = str(getattr(page, "url", "") or "")
        except Exception as exc:
            if is_browser_crash(exc):
                raise
        try:
            from autofill.follow import read_page_mark

            mark, _snapshot = read_page_mark(page)
            url = mark.url or url
            step = mark.step
            selectors = mark.selectors
        except Exception as exc:
            if is_browser_crash(exc):
                raise
            step = str(getattr(page, "step", "") or "")
            raw = getattr(page, "selectors", ()) or ()
            try:
                selectors = frozenset(str(item) for item in raw)
            except TypeError:
                selectors = frozenset()
        target = ""
        tab = getattr(self._session, "tab", None)
        if tab is not None:
            target = str(getattr(tab, "target_id", "") or "")
        if not target:
            target = str(getattr(page, "target_id", "") or "")
        job.note_progress(target_id=target, url=url, step=step, selectors=selectors)

    def _capture_open_page(self, job: LocalJob) -> None:
        tab = getattr(self._session, "tab", None)
        page = getattr(tab, "page", None) if tab is not None else None
        if page is None:
            return
        try:
            self._capture_progress(job, page)
        except Exception:
            return

    def _auth_kwargs(self, fill: Any, job: LocalJob) -> dict[str, Any]:
        """Provider and protocol for the existing fill. Continue uses this same dict."""
        kwargs: dict[str, Any] = {}
        if _accepts(fill, "credential_provider"):
            kwargs["credential_provider"] = self._credentials
        if _accepts(fill, "verify_pending"):
            kwargs["verify_pending"] = bool(job.verify_pending)
        if _accepts(fill, "protocol_version"):
            kwargs["protocol_version"] = "0.7.0"
        if _accepts(fill, "standard_terms_authorized"):
            kwargs["standard_terms_authorized"] = bool(getattr(job, "standard_terms_authorized", False))
        return kwargs

    def _save_credential(self, job: LocalJob, email: str, username: str, password: str, scope: str) -> None:
        """Keep one secret in memory. Nothing is written to disk or the status body."""
        from autofill.redact import redaction_filter

        candidate_id = str(getattr(job, "_credential_candidate", "") or "")
        if not candidate_id:
            raise AnswerRejected(422, _failure(job, "candidate_required"))
        self._credentials.put(
            candidate_id,
            scope,
            Credentials(site=scope, email=email, username=username, password=password),
        )
        redaction_filter().add(password)

    def _run_credentials(self, payload: dict[str, Any]) -> None:
        job: LocalJob = payload["job"]
        box: dict[str, Any] = payload["box"]
        try:
            box["body"] = self._credentials_on_worker(job, payload)
            if job.state == "queued":
                job.state = "done"
        except AnswerRejected as exc:
            if job.state == "queued":
                job.state = "done" if job.result is not None else "error"
            box["error"] = exc
        except Exception as exc:
            if job.state == "queued":
                job.state = "error"
            box["error"] = AnswerRejected(409, _failure(job, type(exc).__name__))
        finally:
            self._finish_active(job)
            payload["done"].set()

    def _credentials_on_worker(self, job: LocalJob, payload: dict[str, Any]) -> dict[str, Any]:
        mode = str(payload.get("mode") or "")
        email = str(payload.get("email") or "")
        username = str(payload.get("username") or "")
        password = str(payload.get("password") or "")
        confirm = str(payload.get("confirm") or "")
        scope = str(payload.get("scope") or "")
        page = self._open_page(job.application_url)
        if page is None:
            raise AnswerRejected(409, _failure(job, "not_at_account_step"))
        if mode == "register":
            from autofill.extract import extract_page
            from autofill.login import password_meets_policy, password_policy

            reason = password_meets_policy(password, password_policy(extract_page(page)), email)
            if reason:
                raise AnswerRejected(422, _credential_failure(job, reason, scope))
        if mode == "login" and job.verify_pending and not payload.get("verified"):
            raise AnswerRejected(409, _credential_failure(job, "verification_pending", scope))
        if payload.get("verified") is True:
            job.verify_pending = False
        self._save_credential(job, email, username, password, scope)
        from autofill.account_flow import run_account_action
        from autofill.ats import detect_ats

        ats_info = detect_ats(job.application_url)
        ats_name = ats_info.name if ats_info else None
        report = run_account_action(
            page,
            mode=mode,
            email=email,
            username=username,
            password=password,
            confirm=confirm or password,
            ats=ats_name,
            standard_terms=bool(payload.get("standard_terms")),
        )
        if not isinstance(report, dict):
            report = {"ok": False, "reason": "not_at_account_step"}
        if report.get("reason") == "password_policy":
            self._credentials.drop(str(getattr(job, "_credential_candidate", "") or ""), scope)
            raise AnswerRejected(422, _credential_failure(job, "password_policy", scope))
        if not report.get("ok"):
            raise AnswerRejected(
                422 if report.get("reason") in {"site_rejected", "account_exists"} else 409,
                _credential_failure(job, str(report.get("reason") or "not_at_account_step"), scope, report),
            )
        outcome = str(report.get("outcome") or "")
        verify_card = report.get("status") == Status.EMAIL_VERIFICATION_REQUIRED
        if report.get("verifyBeforeSignIn") or outcome == "registered" or verify_card:
            job.verify_pending = True
        if job.profile is not None and outcome in {"signed_in", "registered", "verification_required"}:
            fill = self._session.fill
            kwargs = self._auth_kwargs(fill, job)
            if _accepts(fill, "fresh"):
                kwargs["fresh"] = False
            if _accepts_cancel(fill):
                kwargs["cancel"] = job.gate
            identity = _fill_identity(job)
            for key, value in identity.items():
                if _accepts(fill, key):
                    kwargs[key] = value
            if job.resume is not None and _accepts(fill, "resume"):
                kwargs["resume"] = job.resume
            result, submitted = fill(job.application_url, job.profile, **kwargs)
            job.submitted = submitted
            self._remember_result(job, result)
            self._capture_open_page(job)
        body = job.public()
        body["stored"] = True
        body["outcome"] = outcome or "stored"
        body["tenantKey"] = scope
        proof = report.get("fieldProof")
        if isinstance(proof, dict):
            body["fieldProof"] = {
                key: proof[key]
                for key in (
                    "emailFilled",
                    "emailLength",
                    "emailMatches",
                    "passwordLength",
                    "verifyLength",
                    "passwordsMatch",
                )
                if key in proof
            }
        logger.info("session=%s credentials_stored=true mode=%s outcome=%s", job.session_id, mode, body["outcome"])
        job.state = "done"
        return body

    def _open_page(self, url: str) -> Any:
        ensure = getattr(self._session, "ensure", None)
        if ensure is None:
            tab = getattr(self._session, "tab", None)
            return getattr(tab, "page", None) if tab is not None else None
        try:
            tab = ensure(url)
        except Exception:
            return None
        return getattr(tab, "page", None)

    def _run_sign_out(self, payload: dict[str, Any]) -> None:
        job: LocalJob = payload["job"]
        box: dict[str, Any] = payload["box"]
        try:
            cleared = 0
            sign_out = getattr(self._session, "sign_out", None)
            if sign_out is not None:
                cleared = int(sign_out(job.application_url) or 0)
            body = job.public()
            body["signedOut"] = True
            body["cleared"] = cleared
            box["body"] = body
            logger.info("session=%s signed_out=true cleared=%s", job.session_id, cleared)
        except Exception as exc:
            box["error"] = AnswerRejected(409, _failure(job, type(exc).__name__))
        finally:
            self._finish_active(job)
            payload["done"].set()

    def _run_fill(self, payload: dict[str, Any]) -> None:
        job: LocalJob = payload["job"]
        profile: CandidateProfile = payload["profile"]
        recover = bool(payload.get("recover"))
        box: dict[str, Any] | None = payload.get("box")
        if job.superseded:
            job.state = "replaced"
            if box is not None:
                box["error"] = AnswerRejected(410, _failure(job, "replaced"))
            self._finish_active(job)
            done = payload.get("done")
            if done is not None:
                done.set()
            return
        job.state = "running"
        try:
            if recover and not self._progress_allows(job):
                job.error = "progress_mismatch"
                job.state = "error"
                logger.info("session=%s continue_stopped reason=progress_mismatch", job.session_id)
                if box is not None:
                    body = job.public()
                    body["reason"] = "progress_mismatch"
                    body["verified"] = False
                    box["body"] = body
                return
            fill = self._session.fill
            kwargs: dict[str, Any] = {}
            if _accepts_cancel(fill):
                kwargs["cancel"] = job.gate
            if _accepts(fill, "fresh"):
                # A different candidate reloads. A continue keeps the open page.
                kwargs["fresh"] = False if recover else self._window_used
            if job.resume is not None and _accepts(fill, "resume"):
                kwargs["resume"] = job.resume
            identity = _fill_identity(job)
            for key, value in identity.items():
                if _accepts(fill, key):
                    kwargs[key] = value
            if _accepts(fill, "on_observed"):
                kwargs["on_observed"] = lambda page: self._capture_progress(job, page)
            if _accepts(fill, "on_nav_block"):
                kwargs["on_nav_block"] = job.note_nav_block
            block = job.progress.nav_block if recover and job.progress is not None else None
            if block is not None and _accepts(fill, "nav_block"):
                kwargs["nav_block"] = block
            kwargs.update(self._auth_kwargs(fill, job))
            self._window_used = True
            result, submitted = fill(job.application_url, profile, **kwargs)
            job.submitted = submitted
            if job.superseded:
                job.state = "replaced"
            else:
                self._remember_result(job, result)
                job.state = "done"
                self._capture_open_page(job)
                self._present_popup(job)
            job.target_id = self._session.tab.target_id if self._session.tab is not None else job.target_id
            if box is not None:
                body = job.public()
                body["verified"] = not job.submit_claimed()
                box["body"] = body
        except FillCancelled:
            job.state = "replaced"
            logger.info("session=%s fill_stopped reason=%s", job.session_id, job.ended or "cancelled")
            if box is not None:
                box["error"] = AnswerRejected(410, _failure(job, "replaced"))
        except Exception as exc:
            reported_submit = isinstance(exc, LocalAttachError) and "reported a submit" in str(exc)
            if reported_submit:
                job.submitted = True
            crashed = is_browser_crash(exc)
            job.error = type(exc).__name__
            job.state = "error"
            logger.info(
                "session=%s fill_failed error=%s crash=%s detail=%s",
                job.session_id,
                type(exc).__name__,
                "1" if crashed else "0",
                exc,
            )
            if box is not None:
                box["error"] = exc
        finally:
            self._finish_active(job)
            done = payload.get("done")
            if done is not None:
                done.set()

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
        kwargs: dict[str, Any] = {}
        if _accepts_cancel(apply):
            kwargs["cancel"] = job.gate
        if job.resume is not None and _accepts(apply, "resume"):
            kwargs["resume"] = job.resume
        report = apply(job.application_url, job.profile, target, **kwargs)
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
            job.popup = None
            self._present_popup(
                job,
                message="The employer page did not accept that answer. Check it and click Save & Continue again.",
                error=True,
            )
            body = _answer_body(job, report, started)
            if code != 200:
                raise AnswerRejected(code, body)
            return body
        _remember_answer(job, item, target)
        self._adopt_follow(job, report, target)
        job.popup = None
        self._present_popup(job)
        return _answer_body(job, report, started)

    def refresh_popup(self, session_id: str) -> None:
        """Status poll hook: let the worker read or re-show the employer-tab popup.

        Runs only while the current job waits on a question and the worker is
        idle. Waits briefly so the same status response carries a fresh
        ``question.popup``. Never starts a fill.
        """
        if not hasattr(self._session, "read_question_popup"):
            return
        with self._lock:
            job = self._jobs.get(session_id)
            if (
                job is None
                or job.superseded
                or self._current != session_id
                or self._active is not None
                or self._peeking
                or job.state != "done"
                or job.question is None
            ):
                return
            self._peeking = True
        done = threading.Event()
        self._queue.put(("popup", {"job": job, "done": done}))
        done.wait(timeout=_POPUP_SYNC_WAIT_SECONDS)

    def _present_popup(
        self,
        job: LocalJob,
        *,
        message: str = "",
        error: bool = False,
    ) -> None:
        """Worker thread. Show the popup for ``job.question`` or remove it."""
        show = getattr(self._session, "show_question_popup", None)
        close = getattr(self._session, "close_question_popup", None)
        question = job.question if _public_status(job) == Status.MANUAL_ANSWER_REQUIRED else None
        if question is None or job.superseded or job.profile is None:
            job.popup = None
            if close is not None:
                try:
                    close()
                except Exception:
                    pass
            return
        if show is None:
            # No employer tab to draw on: the TileArc panel's own Save & Fill stays the path.
            job.popup = None
            return
        popup = job.popup
        if popup is None or popup.get("questionId") != question["id"] or popup.get("state") == "unavailable":
            popup = {"nonce": new_nonce(), "questionId": question["id"], "state": "shown"}
        item = _match_item(job, {"id": question["id"]}) or {}
        try:
            shown = show(
                question,
                nonce=popup["nonce"],
                selector=str(item.get("selector") or ""),
                candidate=_candidate_label(job),
                message=message,
                error=error,
                force=True,
            )
        except Exception:
            shown = False
        popup["state"] = "shown" if shown else "unavailable"
        popup.pop("values", None)
        popup.pop("answeredAt", None)
        job.popup = popup

    def _sync_popup(self, job: LocalJob) -> None:
        """Worker thread. Pick up a Save & Continue click, re-show a lost popup, time out a stale save."""
        if job.superseded or job.question is None or _public_status(job) != Status.MANUAL_ANSWER_REQUIRED:
            return
        popup = job.popup
        if popup is None or popup.get("state") == "unavailable" or popup.get("questionId") != job.question["id"]:
            self._present_popup(job)
            return
        if popup.get("state") == "answered":
            if time.monotonic() - float(popup.get("answeredAt") or 0) > _POPUP_SAVE_TIMEOUT_SECONDS:
                self._present_popup(
                    job,
                    message=(
                        "TileArc did not confirm the save. Keep the TileArc tab open, "
                        "then click Save & Continue again."
                    ),
                    error=True,
                )
            return
        read = self._session.read_question_popup(popup["nonce"])
        if read.get("detached"):
            return
        if not read.get("present"):
            # The page re-rendered or navigated. Same question, same nonce: show it again.
            self._present_popup(job)
            return
        if read.get("state") != "answered":
            return
        values = list(read.get("values") or [])
        item = _match_item(job, {"id": job.question["id"]})
        reason, canonical = ("needs_option", list[str]())
        if item is not None and values and read.get("questionId") == job.question["id"]:
            reason, canonical = fit_choice_values(item, values)
        if reason != "ok" or not canonical:
            message = getattr(self._session, "question_popup_message", None)
            if message is not None:
                message(
                    popup["nonce"],
                    "Pick one of the choices shown, then click Save & Continue.",
                    error=True,
                    reopen=True,
                )
            return
        popup["state"] = "answered"
        popup["values"] = [str(value) for value in canonical]
        popup["answeredAt"] = time.monotonic()
        logger.info("session=%s popup_answered values=%s", job.session_id, len(canonical))
        timer = threading.Timer(_POPUP_SAVE_TIMEOUT_SECONDS + 1, self.refresh_popup, args=(job.session_id,))
        timer.daemon = True
        timer.start()

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
            if not self._resume_survives_detach(job):
                job.drop_resume()
            job.browser_running = running
            job.owns_browser = self._session.owns_chrome

    def _resume_survives_detach(self, job: LocalJob) -> bool:
        """Keep bytes for the current job when a later continue or retry still needs them."""
        if job.session_id != self._current:
            return False
        if job.superseded or job.submit_claimed():
            return False
        if job.state == "error":
            return True
        return _public_status(job) in _DETACH_KEEP_STATUSES


def _credential_fields(job: LocalJob, payload: dict[str, Any]) -> tuple[str, str, str, str, str]:
    """Mode and identity fields. The password is returned to the caller and not logged."""
    mode = str(payload.get("mode") or "store").strip().lower()
    if mode not in {"store", "login", "register"}:
        raise AnswerRejected(422, _failure(job, "mode_invalid"))
    email = str(payload.get("email") or "").strip()
    username = str(payload.get("username") or "").strip()
    password = str(payload.get("password") or "")
    confirm = str(payload.get("confirmPassword") or payload.get("confirm") or "")
    if not password or not (email or username):
        raise AnswerRejected(422, _failure(job, "credentials_required"))
    return mode, email, username, password, confirm


def _require_credential_candidate(job: LocalJob, payload: dict[str, Any]) -> None:
    candidate = str(payload.get("candidateId") or "").strip()
    if not candidate:
        raise AnswerRejected(422, _failure(job, "candidate_required"))
    bound = (job.binding or {}).get("candidateId") or ""
    if bound and candidate != bound:
        raise AnswerRejected(409, _failure(job, "candidate_mismatch"))
    job._credential_candidate = candidate


def _credential_failure(
    job: LocalJob,
    reason: str,
    scope: str,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A credentials refusal. The secret is not copied onto the body."""
    body = _failure(job, reason)
    body["tenantKey"] = scope
    body["stored"] = False
    if not isinstance(report, dict):
        return body
    proof = report.get("fieldProof")
    if isinstance(proof, dict):
        body["fieldProof"] = {
            key: proof[key]
            for key in (
                "emailFilled",
                "emailLength",
                "emailMatches",
                "passwordLength",
                "verifyLength",
                "passwordsMatch",
            )
            if key in proof
        }
    account = report.get("account")
    if isinstance(account, dict):
        body["account"] = _public_account(account)
    return body


def _fill_identity(job: LocalJob) -> dict[str, str]:
    """Binding ids for the engine. The helper session id is only a fallback."""
    binding = job.binding or {}
    return {
        "session_id": binding.get("tilearcSessionId") or job.session_id,
        "candidate_id": binding.get("candidateId") or "",
        "job_id": binding.get("jobId") or "",
    }


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
        if not job.superseded:
            self.server.helper.refresh_popup(session_id)
        self._send(410 if job.superseded else 200, job.public())

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/v1/sessions":
            self._create()
            return
        if path == "/v1/resumes":
            self._stage_resume()
            return
        session_id = _action_id(path, "answer")
        if session_id is not None:
            self._answer(session_id)
            return
        session_id = _action_id(path, "resume")
        if session_id is not None:
            self._resume(session_id)
            return
        session_id = _action_id(path, "continue")
        if session_id is not None:
            self._continue(session_id)
            return
        session_id = _action_id(path, "credentials")
        if session_id is not None:
            self._credentials(session_id)
            return
        session_id = _action_id(path, "sign-out")
        if session_id is not None:
            self._sign_out(session_id)
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
            resume_ref = parse_resume_claim(payload.get("resume"))
            profile = CandidateProfile.from_dict(profile_raw)
            if resume_ref is None:
                job = self.server.helper.submit(
                url,
                profile,
                binding,
                standard_terms_authorized=payload.get("standardTermsAuthorized") is True,
            )
            else:
                job = self.server.helper.submit(
                    url,
                    profile,
                    binding,
                    resume_ref,
                    standard_terms_authorized=payload.get("standardTermsAuthorized") is True,
                )
        except AnswerRejected as exc:
            self._send(exc.status, exc.body)
            return
        except LocalAttachError as exc:
            busy = "already running" in str(exc)
            self._send(409 if busy else 400, {"detail": str(exc), "stopped_before_submit": True})
            return
        except (ProfileError, ValueError) as exc:
            self._send(400, {"detail": str(exc), "stopped_before_submit": True})
            return
        self._send(202, job.public())

    def _stage_resume(self) -> None:
        """``POST /v1/resumes``: raw PDF body. Returns a single-use ``resumeRef``.

        Only the extension background posts here, right before a bound create.
        Nothing is written to disk and no route returns the bytes.
        """
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            length = -1
        try:
            if length <= 0 or length > MAX_RESUME_BYTES:
                # Do not read an oversize body. The connection closes after the reply.
                self.close_connection = True
                raise BindingError("resume must be a PDF up to 10 MB.")
            data = self.rfile.read(length)
            ctype = str(self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/pdf":
                raise BindingError("resume must be application/pdf.")
            staged = self.server.helper.stage_resume(
                data,
                str(self.headers.get("X-Resume-Filename") or ""),
                str(self.headers.get("X-TileArc-Session") or "").strip(),
                str(self.headers.get("X-TileArc-Candidate") or "").strip(),
            )
        except (BindingError, ValueError) as exc:
            self._send(400, {"detail": str(exc), "stopped_before_submit": True})
            return
        self._send(201, {"resumeRef": staged.ref, "size": len(staged.data), "stopped_before_submit": True})

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

    def _resume(self, session_id: str) -> None:
        """``POST /v1/sessions/{id}/resume`` with ``{"action": "retry"|"confirm_manual"}``."""
        try:
            payload = _read_json(self)
            body = self.server.helper.request_resume(session_id, payload)
        except AnswerRejected as exc:
            self._send(exc.status, exc.body)
            return
        except LocalAttachError as exc:
            missing = "No such" in str(exc)
            busy = "already running" in str(exc) or "still running" in str(exc) or "did not finish" in str(exc)
            code = 404 if missing else 409 if busy else 400
            self._send(code, {"detail": str(exc), "verified": False, "stopped_before_submit": True})
            return
        self._send(200, body)

    def _continue(self, session_id: str) -> None:
        """``POST /v1/sessions/{id}/continue``. Reattach. Do not reload."""
        try:
            payload = _read_json(self)
            body = self.server.helper.request_continue(session_id, payload)
        except AnswerRejected as exc:
            self._send(exc.status, exc.body)
            return
        except LocalAttachError as exc:
            missing = "No such" in str(exc)
            busy = "already running" in str(exc) or "still running" in str(exc) or "did not finish" in str(exc)
            code = 404 if missing else 409 if busy else 400
            self._send(code, {"detail": str(exc), "verified": False, "stopped_before_submit": True})
            return
        except Exception as exc:
            self._send(
                409,
                {
                    "detail": type(exc).__name__,
                    "verified": False,
                    "stopped_before_submit": True,
                },
            )
            return
        self._send(200, body)

    def _credentials(self, session_id: str) -> None:
        """``POST /v1/sessions/{id}/credentials``. The password is not echoed."""
        try:
            payload = _read_json(self)
            body = self.server.helper.request_credentials(session_id, payload)
        except AnswerRejected as exc:
            self._send(exc.status, exc.body)
            return
        except LocalAttachError as exc:
            missing = "No such" in str(exc)
            busy = "already running" in str(exc) or "still running" in str(exc) or "did not finish" in str(exc)
            code = 404 if missing else 409 if busy else 400
            self._send(code, {"detail": str(exc), "stored": False, "stopped_before_submit": True})
            return
        self._send(200, body)

    def _sign_out(self, session_id: str) -> None:
        """``POST /v1/sessions/{id}/sign-out``. Clears employer cookies. Keeps the secret."""
        try:
            body = self.server.helper.request_sign_out(session_id)
        except AnswerRejected as exc:
            self._send(exc.status, exc.body)
            return
        except LocalAttachError as exc:
            missing = "No such" in str(exc)
            busy = "already running" in str(exc) or "still running" in str(exc) or "did not finish" in str(exc)
            code = 404 if missing else 409 if busy else 400
            self._send(code, {"detail": str(exc), "signedOut": False, "stopped_before_submit": True})
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
    if not path.startswith(prefix) or path.endswith(
        ("/detach", "/answer", "/cancel", "/resume", "/continue", "/credentials", "/sign-out")
    ):
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


def _popup_view(job: LocalJob) -> dict[str, Any] | None:
    """Public popup state. ``values`` only after the employee clicked Save & Continue."""
    popup = job.popup
    if not popup or job.question is None or popup.get("questionId") != job.question["id"]:
        return None
    state = str(popup.get("state") or "")
    view: dict[str, Any] = {"state": state, "source": "employer_tab"}
    if state == "answered" and popup.get("values"):
        view["nonce"] = str(popup.get("nonce") or "")
        view["values"] = list(popup["values"])
    return view


def _candidate_label(job: LocalJob) -> str:
    profile = job.profile
    if profile is None:
        return ""
    try:
        return str(profile.personal.full_name or "").strip()[:80]
    except Exception:
        return ""


_NO_SESSION_REUSE_KINDS = frozenset({"CONSENT", "CONSENT_CHOICE", "DEMOGRAPHIC", "MISSING_PROFILE_VALUE"})


def _remember_answer(job: LocalJob, item: dict[str, Any], target: dict[str, Any]) -> None:
    """A verified answer is offered to later pages of this same fill.

    Same candidate only (it lives on this job's profile, which is dropped on
    replace or cancel). Reuse still goes through the Req2 matcher, which
    refuses demographic intents and anything it cannot match with confidence.
    Consent and missing-profile values are not reused.
    """
    profile = job.profile
    if profile is None:
        return
    kind = canonical_blocker_kind(item.get("kind"))
    if kind in _NO_SESSION_REUSE_KINDS:
        return
    question = str(item.get("text") or "").strip()
    values = [str(value) for value in target.get("values") or [] if str(value).strip()]
    if not question or len(values) != 1:
        return
    from autofill.answers import employer_keys, note_session_answer

    note_session_answer(
        profile,
        stem=question,
        value=values[0],
        intent=str(item.get("intent") or ""),
        kind=kind,
        employers=employer_keys(url=job.application_url),
    )


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
    if (
        str(item.get("answerType") or "") == "multi_select"
        and len(values) == 1
        and " | " in values[0]
    ):
        # The TileArc panel sends a multi-select answer as one "A | B" value.
        values = [part.strip() for part in values[0].split(" | ") if part.strip()]
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
