"""Versioned HTTP API. Final Submit is not a route."""

from __future__ import annotations

import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, FastAPI, File, Form, HTTPException, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from autofill import __version__
from autofill.credentials import Credentials
from autofill.dom_ui import (
    choose_blocking,
    empty_progress,
    fit_choice_values,
    match_open_item,
    progress_percent,
    public_blocking,
)
from autofill.engine import ApplicationResult, Status
from autofill.login import protocol_at_least
from autofill.models import JobContext
from autofill.redact import install_redaction, redaction_filter
from autofill.resolver import ResolverBinding
from autofill.resume_manual import ResumeRejected, max_resume_bytes, mime_for, validate_resume
from autofill.safeguards import HUMAN_SUBMIT_ONLY, HumanSubmissionRequired
from autofill.service.auth import require_configured_token, tokens_match
from autofill.service.context import (
    ContextError,
    build_binding,
    build_profile,
    credential_domain,
    placeholder_profile,
    require_login_name,
    validate_application_url,
)
from autofill.service.runner import (
    DEFAULT_LAUNCH_ARGS,
    BrowserDisabled,
    FillRequest,
    FillRunner,
    PageUnavailable,
    PlaywrightRunner,
    elapsed_ms,
)
from autofill.service.schemas import (
    DEFAULT_PROTOCOL_VERSION,
    AnswerIn,
    AnswerOut,
    BlockingOut,
    CandidateCheckIn,
    ContinueIn,
    CreateSessionIn,
    CredentialsIn,
    CredentialsOut,
    HealthOut,
    HumanDoneIn,
    InteractIn,
    InteractOut,
    ManualQuestionOut,
    ProgressOut,
    RunStatusOut,
    TimingsOut,
)
from autofill.service.sessions import ServiceSession, ServiceSessionStore, monotonic, new_session_id

logger = logging.getLogger("autofill.service")

_REFUSED_STATUSES = frozenset({"SUBMITTED", "APPLIED", "SUBMIT_CLICKED", "APPLICATION_SUBMITTED"})
_STARTABLE = frozenset({Status.LOGIN_REQUIRED, Status.LOGIN_FAILED, Status.CREATED, "CREATED", Status.FAILED_RETRYABLE})
_HUMAN_STOP = frozenset(
    {
        Status.LOGIN_REQUIRED,
        Status.ACCOUNT_CREATION_REQUIRED,
        Status.EMAIL_VERIFICATION_REQUIRED,
        Status.CAPTCHA_REQUIRED,
        Status.MANUAL_ANSWER_REQUIRED,
        Status.RESUME_UPLOAD_REQUIRED,
        Status.NO_FORM_FOUND,
        Status.READY_FOR_HUMAN_SUBMIT,
    }
)
_VIEWPORT = "1280x800"
_CHECKPOINT_KIND = {
    Status.ACCOUNT_CREATION_REQUIRED: "ACCOUNT",
    Status.LOGIN_REQUIRED: "LOGIN",
    Status.LOGIN_FAILED: "LOGIN",
    Status.EMAIL_VERIFICATION_REQUIRED: "VERIFICATION",
    Status.CAPTCHA_REQUIRED: "CAPTCHA",
    Status.RESUME_UPLOAD_REQUIRED: "RESUME",
}


def _is_070(session: ServiceSession) -> bool:
    return protocol_at_least(session.protocol_version)


def _public_status(session: ServiceSession) -> str:
    """Older sessions keep the 0.6 status names."""
    status = session.status
    if _is_070(session):
        return status
    if status == Status.ACCOUNT_CREATION_REQUIRED:
        return Status.LOGIN_REQUIRED
    if status == Status.EMAIL_VERIFICATION_REQUIRED:
        return Status.MANUAL_REVIEW_REQUIRED
    return status


def _limit_interact(session) -> None:
    """30 human actions per minute before 0.7.0, and 60 after."""
    cap = 60 if _is_070(session) else 30
    now = time.monotonic()
    session.interact_marks = [mark for mark in session.interact_marks if now - mark < 60]
    if len(session.interact_marks) >= cap:
        raise HTTPException(status_code=429, detail="Too many interactions for this session.")
    session.interact_marks.append(now)


def _sync_checkpoint(session: ServiceSession) -> None:
    kind = _CHECKPOINT_KIND.get(session.status)
    if kind is None:
        session.checkpoint = None
        return
    current = session.checkpoint or {}
    if current.get("kind") != kind:
        session.checkpoint = {
            "kind": kind,
            "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "attempts": 0,
        }


def _bump_attempts(session: ServiceSession) -> None:
    _sync_checkpoint(session)
    if session.checkpoint is None:
        return
    session.checkpoint["attempts"] = int(session.checkpoint.get("attempts") or 0) + 1


def _resume_public(session: ServiceSession) -> dict:
    required = bool(session.resume_required or session.status == Status.RESUME_UPLOAD_REQUIRED)
    payload: dict = {"required": required, "verified": bool(session.resume_verified)}
    if session.resume_verified and session.resume_filename:
        payload["filename"] = session.resume_filename
    if session.resume_verified_at:
        payload["verifiedAt"] = session.resume_verified_at
    if session.resume_source:
        payload["source"] = session.resume_source
    return payload


_FORM_STAGES = frozenset(
    {"PERSONAL", "ADDRESS", "EMPLOYMENT", "EDUCATION", "QUESTIONS", "RESUME", "REVIEW", "READY"}
)


def _overlay_progress(session: ServiceSession, progress: dict) -> dict:
    """Protocol 0.7.0 stages and a percent that does not go backwards."""
    if not _is_070(session):
        return progress
    updated = dict(progress)
    labels = {
        Status.ACCOUNT_CREATION_REQUIRED: ("ACCOUNT", "Employer account"),
        Status.LOGIN_REQUIRED: ("ACCOUNT", "Employer account"),
        Status.LOGIN_FAILED: ("ACCOUNT", "Employer account"),
        Status.EMAIL_VERIFICATION_REQUIRED: ("VERIFICATION", "Email verification"),
        Status.CAPTCHA_REQUIRED: ("CAPTCHA", "Verification check"),
    }
    mapped = labels.get(session.status)
    if mapped is not None:
        updated["stage"], updated["currentLabel"] = mapped
    elif updated.get("stage") == "LOGIN":
        updated["stage"] = "ACCOUNT"
        updated["currentLabel"] = "Employer account"
    if (
        session.status != Status.NO_FORM_FOUND
        and updated.get("stage") == "OPENING"
        and session.form_seen
    ):
        updated["stage"] = "QUESTIONS"
        if updated.get("currentLabel") in {"", "Opening"}:
            updated["currentLabel"] = "Questions"
    if session.status != Status.NO_FORM_FOUND and str(updated.get("stage") or "") in _FORM_STAGES:
        session.form_seen = True
    page_key = str(session.result.current_step or "") if session.result is not None else ""
    done = int(updated.get("requiredDone") or 0)
    if page_key and page_key == session.required_page_key:
        done = max(done, session.required_done_high)
    else:
        session.required_page_key = page_key
    session.required_done_high = done
    updated["requiredDone"] = done
    percent = int(updated.get("percent") or 0)
    if not int(updated.get("requiredTotal") or 0):
        percent = progress_percent(str(updated.get("stage") or "OPENING"), 0, 0)
    if session.status == Status.NO_FORM_FOUND:
        percent = session.percent_high if session.form_seen else 0
    else:
        percent = max(percent, session.percent_high)
        session.percent_high = percent
    updated["percent"] = percent
    session.progress = dict(updated)
    return updated


def _shot_headers(session: ServiceSession, *, host: str = "", retry: bool = False) -> dict[str, str]:
    headers = {
        "X-Autofill-Status": _public_status(session),
        "X-Autofill-State-Version": str(session.state_version),
        "X-Autofill-Viewport": _VIEWPORT,
    }
    if host:
        headers["X-Autofill-Url-Host"] = host
    if retry:
        headers["Retry-After"] = "1"
    return headers


def _probe_challenge(session: ServiceSession, runner: FillRunner) -> str:
    probe = getattr(runner, "probe_challenge", None)
    if probe is None:
        return "clear"
    try:
        return str(probe(session.session_id) or "clear")
    except PageUnavailable as exc:
        if exc.status_code == 503:
            raise HTTPException(
                status_code=503,
                detail="Page is navigating.",
                headers=_shot_headers(session, retry=True),
            ) from None
        _mark_browser_crash(session)
        raise HTTPException(
            status_code=exc.status_code,
            detail="The page crashed.",
            headers=_shot_headers(session),
        ) from None


def _confirm_resume_or_422(session: ServiceSession, runner: FillRunner) -> None:
    confirm = getattr(runner, "confirm_resume", None)
    if confirm is None:
        return
    try:
        report = confirm(session.session_id)
    except PageUnavailable as exc:
        if exc.status_code == 503:
            raise HTTPException(
                status_code=503,
                detail="Page is navigating.",
                headers=_shot_headers(session, retry=True),
            ) from None
        _mark_browser_crash(session)
        raise HTTPException(status_code=exc.status_code, detail="The page crashed.") from None
    if report.get("verified"):
        session.resume_verified = True
        session.resume_required = True
        session.resume_source = session.resume_source or "manual"
        if not session.resume_verified_at:
            session.resume_verified_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return
    raise HTTPException(
        status_code=422,
        detail={
            "verified": False,
            "reason": str(report.get("reason") or "not_confirmed"),
            "siteErrors": [str(item) for item in report.get("siteErrors") or []],
        },
    )


def _require_same_origin(session: ServiceSession, site: str) -> None:
    if not site.strip():
        return
    site_host = (urlparse(site).hostname or "").lower()
    if not site_host:
        return
    origin = str(((session.account or {}).get("tenant") or {}).get("loginOrigin") or "")
    origin_host = (urlparse(origin).hostname or "").lower()
    if not origin_host or site_host != origin_host:
        raise HTTPException(status_code=409, detail={"reason": "origin_mismatch"})


def _finish_account_action(session: ServiceSession, report: dict, resume_run) -> CredentialsOut:
    """Store the classification. A paused session waits for /resume."""
    if not report.get("ok"):
        reason = str(report.get("reason") or "not_at_account_step")
        if reason in {"site_rejected", "account_exists"}:
            if report.get("account"):
                session.account = report["account"]
            if report.get("status"):
                session.status = str(report["status"])
            _sync_checkpoint(session)
            raise HTTPException(
                status_code=422,
                detail={"reason": reason, "siteErrors": [str(item) for item in report.get("siteErrors") or []]},
            )
        if reason == "mode_mismatch":
            raise HTTPException(
                status_code=409,
                detail={"reason": "mode_mismatch", "detected": str(report.get("detected") or "login")},
            )
        if reason == "navigating":
            raise HTTPException(
                status_code=503,
                detail="Page is navigating.",
                headers=_shot_headers(session, retry=True),
            )
        if reason == "not_at_account_step":
            raise HTTPException(status_code=409, detail={"reason": "not_at_account_step"})
        if reason == "input_failed":
            raise HTTPException(status_code=409, detail={"reason": "input_failed"})
        raise HTTPException(status_code=409, detail="No page is open for this session.")
    if report.get("account"):
        session.account = report["account"]
    outcome = str(report.get("outcome") or "pending")
    if report.get("status"):
        session.status = str(report["status"])
    if session.paused:
        session.pending_resume = outcome in {"signed_in", "registered", "verification_required"}
        _sync_checkpoint(session)
        return CredentialsOut(sessionId=session.session_id, outcome=outcome, status=_public_status(session))
    if outcome in {"signed_in", "registered", "verification_required"}:
        accepted = resume_run()
        if accepted.status == Status.EMAIL_VERIFICATION_REQUIRED:
            outcome = "verification_required"
        return CredentialsOut(sessionId=session.session_id, outcome=outcome, status=accepted.status)
    _sync_checkpoint(session)
    return CredentialsOut(sessionId=session.session_id, outcome=outcome, status=_public_status(session))


def _read_upload(file: UploadFile) -> bytes:
    limit = max_resume_bytes()
    chunks: list[bytes] = []
    total = 0
    while True:
        block = file.file.read(65536)
        if not block:
            break
        total += len(block)
        if total > limit:
            raise HTTPException(status_code=413, detail="too_large")
        chunks.append(block)
    return b"".join(chunks)


def _keep_resume_bytes(session: ServiceSession, name: str, data: bytes) -> None:
    session.clear_resume_bytes()
    directory = tempfile.mkdtemp(prefix="autofill-resume-")
    os.chmod(directory, 0o700)
    path = Path(directory) / name
    path.write_bytes(data)
    os.chmod(path, 0o600)
    session.resume_temp = str(path)
    session.resume_bytes = data
    session.resume_filename = name
    session.resume_mime = mime_for(name)


_DESCRIPTION = """
Public Auto-Fill HTTP service.

This process is the public Auto-Fill implementation (AGPL-3.0-only). Callers
are separate applications. This service does not import those applications,
does not open their databases, and does not accept a database URL.

It opens an application, signs in when this session has site credentials,
fills approved profile fields, and stops. Resume upload and the final Submit
or Apply stay with a person. CAPTCHA and other challenges are detected and
the run stops. They are not solved or bypassed.

Send the service token only from a server you control. Do not put it in a
URL, a browser page, or client-side JavaScript.
""".strip()

_bearer = HTTPBearer(auto_error=False)


def _questions_out(session: ServiceSession) -> list[ManualQuestionOut]:
    source = session.result.manual_questions if session.result is not None else session.manual_questions
    return [ManualQuestionOut(intent=item.get("intent"), text=item.get("text") or "") for item in source]


def _timings_out(session: ServiceSession) -> TimingsOut:
    raw = dict(session.timings)
    if session.result is not None:
        raw.update(session.result.timings)
    return TimingsOut(
        sessionCreatedMs=int(raw.get("sessionCreatedMs", 0)),
        browserReadyMs=int(raw.get("browserReadyMs", raw.get("contextReadyMs", 0))),
        firstFormInspectedMs=int(raw.get("firstFormInspectedMs", 0)),
        contextReadyMs=int(raw.get("contextReadyMs", 0)),
        browserPrewarmMs=int(raw.get("browserPrewarmMs", 0)),
        firstFillMs=int(raw.get("firstFillMs", 0)),
    )


def _status_out(session: ServiceSession, *, final: bool = False) -> RunStatusOut:
    result = session.result
    body = RunStatusOut(
        sessionId=session.session_id,
        status=_public_status(session),
        stopped_before_submit=True,
        candidateId=session.candidate_id,
        jobId=session.job_id,
        protocolVersion=session.protocol_version.strip() or DEFAULT_PROTOCOL_VERSION,
        manualQuestions=_questions_out(session),
        timings=_timings_out(session),
        humanOutcome=session.human_outcome,
        paused=session.paused,
    )
    # A run still inside the worker reports live progress. The response from that
    # run is built before the busy flag drops, so ``final`` uses the result.
    if result is None or (session.busy and not final):
        progress = dict(session.progress or empty_progress())
    else:
        progress = dict(result.progress or session.progress or empty_progress())
        if result.blocking:
            body.blocking = BlockingOut.model_validate(result.blocking)
    progress = _overlay_progress(session, progress)
    body.progress = ProgressOut.model_validate(progress)
    if _is_070(session) and body.blocking is None:
        synthetic = public_blocking(choose_blocking(session.status, []))
        if synthetic is not None and synthetic["kind"] in {"ACCOUNT", "VERIFICATION", "LOGIN", "CAPTCHA", "RESUME"}:
            body.blocking = BlockingOut.model_validate(synthetic)
    if not _is_070(session) and body.blocking is not None:
        if body.blocking.kind == "ACCOUNT":
            body.blocking.kind = "LOGIN"
            body.blocking.text = "Sign in"
        elif body.blocking.kind == "VERIFICATION":
            body.blocking = None
    if _is_070(session) and body.blocking is not None and body.blocking.kind == "LOGIN":
        host = str(((session.account or {}).get("tenant") or {}).get("host") or "")
        body.blocking.text = f"Sign in to {host}" if host else "Sign in"
    if _is_070(session):
        _sync_checkpoint(session)
        body.state_version = session.observe()
        body.account = session.account
        body.resume = _resume_public(session)
        body.completed = {
            "fieldKeys": list(session.completed_keys),
            "questionHashes": list(session.completed_hashes),
        }
        body.checkpoint = session.checkpoint
    if result is None:
        return body
    payload = result.to_dict()
    body.ats = payload["ats"]
    body.current_step = payload["currentStep"]
    body.login_status = payload["loginStatus"]
    body.fields_detected = payload["fieldsDetected"]
    body.fields_filled = payload["fieldsFilled"]
    body.fields_skipped = payload["fieldsSkipped"]
    body.manual_actions = list(payload["manualActions"])
    body.steps = list(payload["steps"])
    body.messages = list(payload["messages"])
    body.submit_controls = list(payload["submitControls"])
    return body


def _accept_result(session: ServiceSession, result: ApplicationResult) -> RunStatusOut:
    if not HUMAN_SUBMIT_ONLY or not result.stopped_before_submit:
        raise HumanSubmissionRequired("Final submit is not available.")
    if result.status.upper() in _REFUSED_STATUSES:
        raise HumanSubmissionRequired("Final submit is not available.")
    if result.candidate_id and result.candidate_id != session.candidate_id:
        raise HTTPException(status_code=409, detail="Candidate does not match this session.")
    if result.job_id and result.job_id != session.job_id:
        raise HTTPException(status_code=409, detail="Job does not match this session.")
    session.result = result
    session.status = result.status
    if result.account is not None:
        session.account = result.account
    for key in result.completed_keys:
        if key and key not in session.completed_keys:
            session.completed_keys.append(key)
    if result.status == Status.RESUME_UPLOAD_REQUIRED:
        session.resume_required = True
    _sync_checkpoint(session)
    session.manual_questions = [dict(item) for item in result.manual_questions]
    session.progress = dict(result.progress) if result.progress else empty_progress()
    session.open_items = [dict(item) for item in result.open_items]
    session.paused = session.pause_gate.paused
    if result.timings:
        for key, value in result.timings.items():
            if key == "sessionCreatedMs" and not value and session.timings.get(key):
                continue
            session.timings[key] = value
    logger.info(
        "session=%s status=%s ats=%s step=%s",
        session.session_id,
        result.status,
        result.ats or "",
        result.current_step,
    )
    return _status_out(session, final=True)


def _mark_browser_crash(session: ServiceSession) -> None:
    """A dead tab is a retryable crash, not the status the run had before it died."""
    session.status = Status.FAILED_RETRYABLE
    if session.result is None:
        session.result = ApplicationResult(
            status=Status.FAILED_RETRYABLE,
            ats=None,
            current_step=Status.FAILED_RETRYABLE,
            login_status="NOT_REQUIRED",
            session_id=session.session_id,
            candidate_id=session.candidate_id,
            job_id=session.job_id,
            messages=["browser_crash"],
        )
    else:
        session.result.status = Status.FAILED_RETRYABLE
        session.result.current_step = Status.FAILED_RETRYABLE
        session.result.messages = ["browser_crash"]
    logger.info("session=%s status=%s result=browser_crash", session.session_id, session.status)


def _same_blocker(blocking: dict, item: dict) -> bool:
    for key in ("questionId", "questionHash", "fieldKey"):
        left = str(blocking.get(key) or "")
        right = str(item.get(key) or "")
        if left and right and left == right:
            return True
    left_text = str(blocking.get("text") or "")
    right_text = str(item.get("text") or "")
    return bool(left_text) and left_text == right_text


_STUCK_TEXT = "Auto-Fill couldn't set this field; please set it in the live view, then Continue"
_LEVER_LOCATION_TEXT = (
    "Lever didn't offer location suggestions; pick Current location in the live view, then Continue"
)
_ANSWER_ATTEMPT_LIMIT = 3


def _attempt_key(item: dict) -> str:
    """One counter per field element for the whole session.

    The key is the question hash (which includes the element id) or the
    selector. A fieldKey is not an identity: the same control can be reported
    as ``location`` and ``address.city``.
    """
    for value in (item.get("questionHash"), item.get("questionId"), item.get("selector")):
        token = str(value or "").strip()
        if token:
            return token
    return ""


def _attempt_count(session: ServiceSession, item: dict, body: AnswerIn | None = None) -> int:
    del body
    key = _attempt_key(item)
    if not key:
        return 0
    return session.answer_attempts.get(key, 0)


def _bump_answer_attempt(session: ServiceSession, item: dict, body: AnswerIn | None = None) -> int:
    del body
    key = _attempt_key(item)
    if not key:
        return 0
    count = session.answer_attempts.get(key, 0) + 1
    session.answer_attempts[key] = count
    return count


def _freeze_stuck(session: ServiceSession, item: dict, text: str | None = None) -> None:
    """Stop automatic retries. A person sets the field, then continues."""
    stuck = dict(item)
    stuck["text"] = text or _STUCK_TEXT
    stuck["blocking"] = True
    session.stuck_manual = True
    session.status = Status.MANUAL_ANSWER_REQUIRED
    session.open_items = [dict(stuck)]
    if session.result is not None:
        session.result.status = Status.MANUAL_ANSWER_REQUIRED
        session.result.blocking = public_blocking(stuck)
        session.result.open_items = [dict(stuck)]


def _needs_manual(session: ServiceSession, item: dict, text: str | None = None) -> None:
    _freeze_stuck(session, item, text)
    raise HTTPException(status_code=409, detail={"reason": "needs_manual"})


def _lever_suggestions_missing(filled: dict) -> bool:
    """Lever's geocoder returned an error or an empty list. One try is enough."""
    if not filled.get("leverSearch"):
        return False
    status = filled.get("searchStatus")
    if isinstance(status, int) and status >= 400:
        return True
    return filled.get("suggestionCount") == 0


def _blocker_remains(session: ServiceSession, item: dict) -> bool:
    if session.status != Status.MANUAL_ANSWER_REQUIRED or session.result is None:
        return False
    blocking = session.result.blocking
    return isinstance(blocking, dict) and _same_blocker(blocking, item)


def _answer_filled(session: ServiceSession, item: dict, body: AnswerIn, started: float) -> AnswerOut:
    """Report a commit, or stop when the same blocker is still the one on the page."""
    count = _bump_answer_attempt(session, item, body)
    if _blocker_remains(session, item):
        logger.info("session=%s action=answer result=no_progress attempts=%s", session.session_id, count)
        if count >= _ANSWER_ATTEMPT_LIMIT:
            _needs_manual(session, item)
    else:
        logger.info("session=%s action=answer result=counted attempts=%s", session.session_id, count)
    spent = elapsed_ms(started)
    logger.info("session=%s action=answer result=filled ms=%s", session.session_id, spent)
    return AnswerOut(filled=True, ms=spent)


def _refresh_choice_options(session: ServiceSession, runner, item: dict) -> None:
    """Read a choice list that the scan stored empty, so a 422 can name the options."""
    answer_type = str(item.get("answerType") or "")
    if answer_type not in {"single_select", "multi_select"}:
        return
    if any(str(option).strip() for option in item.get("options") or []):
        return
    selector = str(item.get("selector") or "")
    reader = getattr(runner, "choice_options", None)
    if reader is None or not selector:
        return
    try:
        loaded = reader(session.session_id, selector) or []
    except PageUnavailable as exc:
        _mark_browser_crash(session)
        raise HTTPException(status_code=exc.status_code, detail="The page crashed.") from None
    options = [str(option) for option in loaded if str(option).strip()]
    if not options:
        return
    item["options"] = options
    result = session.result
    blocking = result.blocking if result is not None else None
    if isinstance(blocking, dict) and _same_blocker(blocking, item):
        blocking["options"] = list(options)


def _allow_browser_restart(session: ServiceSession) -> None:
    """A crashed session can be started again from its URL, a bounded number of times.

    A capacity refusal is retryable on its own and does not spend this budget.
    """
    messages = session.result.messages if session.result is not None else []
    if "capacity_busy" in messages:
        return
    if session.browser_restarts >= 2:
        raise HTTPException(status_code=409, detail="Browser crash retry limit reached.")
    session.browser_restarts += 1


def _failed(session: ServiceSession, exc: BaseException) -> RunStatusOut:
    logger.info("session=%s status=FAILED error_type=%s", session.session_id, type(exc).__name__)
    result = ApplicationResult(
        status=Status.FAILED,
        ats=None,
        current_step="FAILED",
        login_status="NOT_REQUIRED",
        session_id=session.session_id,
        candidate_id=session.candidate_id,
        job_id=session.job_id,
        messages=[f"Stopped because of {type(exc).__name__}."],
    )
    return _accept_result(session, result)


def _check_candidate(session: ServiceSession, candidate_id: str, candidate_ref: str = "") -> None:
    presented = candidate_ref.strip() or candidate_id.strip()
    if not presented:
        return
    allowed = {session.candidate_id}
    if session.candidate_ref:
        allowed.add(session.candidate_ref)
    if presented not in allowed:
        raise HTTPException(status_code=409, detail="Candidate does not match this session.")


def _binding(session: ServiceSession, limits: dict[str, float | int]) -> ResolverBinding | None:
    if session.mode != "resolver":
        return None
    if not session.resolver_token or not session.resolver_url:
        raise HTTPException(status_code=409, detail="Resolver credentials are no longer available.")
    return ResolverBinding(
        url=session.resolver_url,
        token=session.resolver_token,
        expires_at=session.resolver_expires_at,
        candidate_ref=session.candidate_ref,
        job_ref=session.job_ref,
        timeout_seconds=float(limits["timeout"]),
        retries=int(limits["retries"]),
        max_bytes=int(limits["max_bytes"]),
    )


def _request_for(
    session: ServiceSession,
    limits: dict[str, float | int],
    *,
    resume_uploaded: bool,
    human_resolved: bool = False,
    answers_updated: bool = False,
) -> FillRequest:
    return FillRequest(
        session_id=session.session_id,
        candidate_id=session.candidate_id,
        job_id=session.job_id,
        application_url=session.application_url,
        profile=session.profile,
        job=session.job,
        credentials=session.credentials,
        resume_uploaded=resume_uploaded,
        human_resolved=human_resolved,
        cover_letter_text=session.cover_letter_text,
        resolver=_binding(session, limits),
        answers_updated=answers_updated,
        session_created_ms=int(session.timings.get("sessionCreatedMs", 0)),
        max_pages=12 if session.mode == "resolver" else 8,
        pause_gate=session.pause_gate,
        held_selectors=session.held_selectors,
        open_items=session.open_items,
        progress=session.progress,
        protocol_version=session.protocol_version,
        resume_name=session.resume_filename if session.resume_verified else "",
        resume_mime=session.resume_mime if session.resume_verified else "",
        resume_bytes=session.resume_bytes if session.resume_verified else None,
    )


def _run(
    session: ServiceSession,
    store: ServiceSessionStore,
    runner: FillRunner,
    limits: dict[str, float | int],
    *,
    resume_uploaded: bool,
    human_resolved: bool = False,
    answers_updated: bool = False,
) -> RunStatusOut:
    try:
        session = store.begin_run(session.session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Session not found.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail="Session is already running.") from exc
    # A person confirms the resume or a login once. Later answer and continue
    # calls keep that confirmation instead of asking again.
    session.resume_uploaded = session.resume_uploaded or resume_uploaded
    session.human_resolved = session.human_resolved or human_resolved
    previous = session.status
    session.status = Status.STARTED
    try:
        try:
            result = runner.run(
                _request_for(
                    session,
                    limits,
                    resume_uploaded=session.resume_uploaded,
                    human_resolved=session.human_resolved,
                    answers_updated=answers_updated,
                )
            )
            accepted = _accept_result(session, result)
        except HTTPException:
            session.status = previous
            raise
        except BrowserDisabled as exc:
            session.status = previous
            raise HTTPException(status_code=409, detail="Browser execution is disabled.") from exc
        except HumanSubmissionRequired as exc:
            session.status = previous
            session.result = None
            raise HTTPException(
                status_code=409,
                detail="Final submit was refused. A person must submit.",
            ) from exc
        except Exception as exc:
            return _failed(session, exc)
        if session.status == Status.EXPIRED:
            session.wipe_secrets()
            runner.discard(session.session_id)
        return accepted
    finally:
        store.finish_run(session)


def create_app(
    *,
    token: str,
    runner: FillRunner | None = None,
    run_browser: bool = True,
    max_sessions: int = 32,
    browser_no_sandbox: bool = False,
    session_ttl_seconds: float = 1800,
    reaper_interval_seconds: float = 30,
    resolver_timeout_seconds: float = 5,
    resolver_retries: int = 2,
    resolver_max_bytes: int = 65536,
    browser_workers: int = 2,
) -> FastAPI:
    """Build the service. ``token`` is the bearer secret and is not stored in the schema."""
    expected = require_configured_token(token)
    install_redaction()
    store = ServiceSessionStore(max_sessions=max_sessions)
    launch_args = list(DEFAULT_LAUNCH_ARGS)
    if browser_no_sandbox:
        launch_args.append("--no-sandbox")
    active_runner = (
        runner
        if runner is not None
        else PlaywrightRunner(enabled=run_browser, launch_args=launch_args, workers=browser_workers)
    )
    limits: dict[str, float | int] = {
        "timeout": resolver_timeout_seconds,
        "retries": resolver_retries,
        "max_bytes": resolver_max_bytes,
    }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        prewarm = getattr(app.state.runner, "prewarm", None)
        if prewarm is not None:
            prewarm()
        app.state.store.start_reaper(app.state.runner.discard, interval_seconds=reaper_interval_seconds)
        yield
        app.state.store.stop_reaper()
        for session_id in app.state.store.ids():
            held = app.state.store.get(session_id)
            if held is not None:
                held.wipe_secrets()
        app.state.runner.shutdown()

    app = FastAPI(
        title="Auto-Fill",
        version=__version__,
        description=_DESCRIPTION,
        lifespan=lifespan,
        license_info={
            "name": "AGPL-3.0-only",
            "identifier": "AGPL-3.0-only",
        },
    )
    app.state.store = store
    app.state.runner = active_runner

    def require_auth(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> None:
        presented = ""
        if credentials is not None and credentials.scheme.lower() == "bearer":
            presented = credentials.credentials
        if not tokens_match(presented, expected):
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Bearer"},
            )

    router = APIRouter(prefix="/v1", dependencies=[Depends(require_auth)])

    def _session_or_404(session_id: str) -> ServiceSession:
        session = store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        return session

    def _reject_expired(session: ServiceSession) -> None:
        """Stop a call on an expired session before it pauses, fills, or stores a secret."""
        timed_out = bool(session.expires_at) and monotonic() >= session.expires_at
        if session.status != Status.EXPIRED and not timed_out:
            return
        if not session.busy and session.status != Status.EXPIRED:
            session.status = Status.EXPIRED
            session.wipe_secrets()
            active_runner.discard(session.session_id)
        raise HTTPException(status_code=410, detail="Session expired.")

    @app.exception_handler(RequestValidationError)
    async def _hide_rejected_input(_request, exc: RequestValidationError) -> JSONResponse:
        detail = [{key: value for key, value in error.items() if key not in {"input", "ctx"}} for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": detail})

    @app.get("/health", response_model=HealthOut, tags=["health"])
    def health() -> HealthOut:
        return HealthOut()

    @router.post("/sessions", response_model=RunStatusOut, status_code=201, tags=["sessions"])
    def create_session(body: CreateSessionIn) -> RunStatusOut:
        started = time.perf_counter()
        try:
            if body.resolver is not None:
                binding = build_binding(
                    body,
                    timeout_seconds=resolver_timeout_seconds,
                    retries=resolver_retries,
                    max_bytes=resolver_max_bytes,
                )
                application_url = validate_application_url(body.job_context.application_url)
                profile = placeholder_profile(binding.candidate_ref)
                job = JobContext(
                    title=body.job_context.title.strip(),
                    company=body.job_context.company.strip(),
                    url=application_url,
                )
                session = ServiceSession(
                    session_id=new_session_id(),
                    candidate_id=binding.candidate_ref,
                    job_id=binding.job_ref,
                    application_url=application_url,
                    profile=profile,
                    job=job,
                    cover_letter_text=None,
                    mode="resolver",
                    protocol_version=body.protocol_version.strip() or DEFAULT_PROTOCOL_VERSION,
                    candidate_ref=binding.candidate_ref,
                    job_ref=binding.job_ref,
                    resolver_url=binding.url,
                    resolver_token=binding.token,
                    resolver_expires_at=binding.expires_at,
                )
                redaction_filter().add(binding.token)
            else:
                profile, job, cover = build_profile(body)
                session = ServiceSession(
                    session_id=new_session_id(),
                    candidate_id=profile.candidate_id,
                    job_id=body.job_context.job_id.strip(),
                    application_url=job.url,
                    profile=profile,
                    job=job,
                    cover_letter_text=cover,
                    protocol_version=body.protocol_version.strip() or DEFAULT_PROTOCOL_VERSION,
                )
        except ContextError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        session.expires_at = monotonic() + session_ttl_seconds
        session.timings["sessionCreatedMs"] = elapsed_ms(started)
        try:
            store.create(session)
        except RuntimeError as exc:
            session.wipe_secrets()
            raise HTTPException(status_code=429, detail="Too many open sessions.") from exc
        logger.info("session=%s status=CREATED", session.session_id)
        return _status_out(session)

    @router.post("/sessions/{session_id}/start", response_model=RunStatusOut, tags=["sessions"])
    def start_session(session_id: str, body: CandidateCheckIn | None = None) -> RunStatusOut:
        session = _session_or_404(session_id)
        if body is not None:
            _check_candidate(session, body.candidate_id.strip(), body.candidate_ref.strip())
        if session.status not in _STARTABLE:
            raise HTTPException(status_code=409, detail=f"Cannot start a session in status {session.status}.")
        if session.status == Status.FAILED_RETRYABLE:
            _allow_browser_restart(session)
        return _run(session, store, active_runner, limits, resume_uploaded=False)

    @router.get("/sessions/{session_id}/status", response_model=RunStatusOut, tags=["sessions"])
    def session_status(session_id: str) -> RunStatusOut:
        session = _session_or_404(session_id)
        _reject_expired(session)
        return _status_out(session)

    @router.post("/sessions/{session_id}/continue", response_model=RunStatusOut, tags=["sessions"])
    def continue_session(session_id: str, body: ContinueIn) -> RunStatusOut:
        session = _session_or_404(session_id)
        _reject_expired(session)
        _check_candidate(session, body.candidate_id.strip(), body.candidate_ref.strip())
        if body.human_resolved:
            if session.status == Status.FAILED_RETRYABLE:
                _allow_browser_restart(session)
                return _run(session, store, active_runner, limits, resume_uploaded=False, human_resolved=True)
            if _is_070(session) and session.status in {Status.CAPTCHA_REQUIRED, Status.EMAIL_VERIFICATION_REQUIRED}:
                found = _probe_challenge(session, active_runner)
                if found in {"captcha", "verification"}:
                    _bump_attempts(session)
                    reason = "challenge_present" if found == "captcha" else "verification_pending"
                    raise HTTPException(status_code=409, detail={"reason": reason})
            if session.status == Status.MANUAL_ANSWER_REQUIRED and session.stuck_manual:
                session.stuck_manual = False
                return _run(session, store, active_runner, limits, resume_uploaded=False, human_resolved=True)
            allowed = {Status.LOGIN_REQUIRED, Status.CAPTCHA_REQUIRED, Status.NO_FORM_FOUND}
            if _is_070(session):
                allowed.add(Status.EMAIL_VERIFICATION_REQUIRED)
            if session.status not in allowed:
                raise HTTPException(
                    status_code=409,
                    detail="humanResolved continues only after login, a challenge, or a missing form.",
                )
            return _run(session, store, active_runner, limits, resume_uploaded=False, human_resolved=True)
        if session.status == Status.FAILED_RETRYABLE:
            _allow_browser_restart(session)
            return _run(session, store, active_runner, limits, resume_uploaded=False)
        if session.status == Status.READY_FOR_HUMAN_SUBMIT:
            raise HTTPException(
                status_code=409,
                detail="A person submits the application. This service does not submit.",
            )
        if session.status == Status.CAPTCHA_REQUIRED:
            raise HTTPException(
                status_code=409,
                detail="CAPTCHA stopped the run. This service does not solve or bypass challenges.",
            )
        if session.status == Status.LOGIN_REQUIRED:
            raise HTTPException(
                status_code=409,
                detail="Sign-in credentials are missing. Post credentials, then start again.",
            )
        if session.status == Status.MANUAL_ANSWER_REQUIRED:
            if not body.answers_updated:
                raise HTTPException(
                    status_code=409,
                    detail="Continue with answersUpdated after the saved answers change.",
                )
            return _run(session, store, active_runner, limits, resume_uploaded=False, answers_updated=True)
        if session.status != Status.RESUME_UPLOAD_REQUIRED:
            raise HTTPException(status_code=409, detail=f"Cannot continue a session in status {session.status}.")
        if not body.resume_uploaded:
            raise HTTPException(status_code=409, detail="Continue only after a person has uploaded the resume.")
        if _is_070(session):
            _confirm_resume_or_422(session, active_runner)
        return _run(session, store, active_runner, limits, resume_uploaded=True)

    @router.post("/sessions/{session_id}/pause", response_model=RunStatusOut, tags=["sessions"])
    def pause_session(session_id: str) -> RunStatusOut:
        """Stop between fields. A second pause is a no-op and does not start a fill."""
        session = _session_or_404(session_id)
        _reject_expired(session)
        session.pause_gate.pause()
        session.paused = True
        logger.info("session=%s action=pause", session.session_id)
        return _status_out(session)

    @router.post("/sessions/{session_id}/resume", response_model=RunStatusOut, tags=["sessions"])
    def resume_session(session_id: str) -> RunStatusOut:
        """Release a paused fill. This does not start a run that has already stopped."""
        session = _session_or_404(session_id)
        _reject_expired(session)
        session.pause_gate.resume()
        session.paused = False
        logger.info("session=%s action=resume", session.session_id)
        if _is_070(session) and session.pending_resume and not session.busy:
            session.pending_resume = False
            return _run(session, store, active_runner, limits, resume_uploaded=session.resume_uploaded)
        return _status_out(session)

    @router.post(
        "/sessions/{session_id}/answer",
        response_model=AnswerOut,
        response_model_exclude_none=True,
        tags=["sessions"],
    )
    def answer_session(session_id: str, body: AnswerIn) -> AnswerOut:
        """Fill one open field visibly, then continue. The value is not logged."""
        session = _session_or_404(session_id)
        _reject_expired(session)
        if session.status in {Status.LOGIN_REQUIRED, Status.CAPTCHA_REQUIRED}:
            logger.info("session=%s action=answer result=rejected", session.session_id)
            raise HTTPException(
                status_code=409,
                detail="Cannot answer while sign-in or a challenge is blocking.",
            )
        if session.busy:
            raise HTTPException(status_code=409, detail="Session is already running.")
        blocking = session.result.blocking if session.result is not None else None
        item = match_open_item(
            session.open_items,
            question_id=body.question_id,
            question_hash=body.question_hash,
            field_key=body.field_key,
            prefer=blocking if isinstance(blocking, dict) else None,
        )
        if item is None:
            logger.info("session=%s action=answer result=rejected", session.session_id)
            raise HTTPException(status_code=409, detail="No open item matches this answer.")
        if _attempt_count(session, item, body) >= _ANSWER_ATTEMPT_LIMIT:
            logger.info("session=%s action=answer result=needs_manual", session.session_id)
            _needs_manual(session, item)
        values = [text for text in body.values if str(text).strip()]
        if body.value.strip():
            values = values or [body.value]
        if not values:
            logger.info("session=%s action=answer result=rejected", session.session_id)
            raise HTTPException(status_code=422, detail={"reason": "empty", "options": list(item.get("options") or [])})
        _refresh_choice_options(session, active_runner, item)
        reason, canonical = fit_choice_values(item, values)
        if reason != "ok":
            logger.info("session=%s action=answer result=rejected", session.session_id)
            raise HTTPException(
                status_code=422,
                detail={"reason": reason, "options": [str(option) for option in item.get("options") or []]},
            )
        for value in values:
            redaction_filter().add(value)
            session.held_answers.append(value)
        target = dict(item)
        target["value"] = canonical[0]
        target["values"] = canonical
        apply = getattr(active_runner, "apply_answer", None)
        if apply is None:
            logger.info("session=%s action=answer result=rejected", session.session_id)
            return AnswerOut(filled=False, reason="no_page")
        started = time.perf_counter()
        try:
            filled = apply(session.session_id, target)
        except PageUnavailable as exc:
            _mark_browser_crash(session)
            raise HTTPException(status_code=exc.status_code, detail="The page crashed.") from None
        if not filled.get("filled"):
            count = _bump_answer_attempt(session, item, body)
            blocked = _lever_suggestions_missing(filled)
            logger.info(
                "session=%s action=answer result=rejected attempts=%s suggestions=%s search_status=%s",
                session.session_id,
                count,
                filled.get("suggestionCount", "none"),
                filled.get("searchStatus", "none"),
            )
            if blocked or count >= _ANSWER_ATTEMPT_LIMIT:
                _needs_manual(session, item, _LEVER_LOCATION_TEXT if blocked else None)
            return AnswerOut(filled=False, reason=str(filled.get("reason") or "not_committed"), ms=elapsed_ms(started))
        remembered = body.field_key.strip() or str(item.get("fieldKey") or "")
        if remembered and remembered not in session.completed_keys:
            session.completed_keys.append(remembered)
        remembered_hash = body.question_hash.strip() or str(item.get("questionHash") or "")
        if remembered_hash and remembered_hash not in session.completed_hashes:
            session.completed_hashes.append(remembered_hash)
        selector = str(item.get("selector") or "")
        if selector:
            session.held_selectors.add(selector)
        session.pause_gate.resume()
        session.paused = False
        advance = getattr(active_runner, "advance_after_answer", None)
        if advance is not None:
            try:
                outcome = advance(
                    _request_for(
                        session,
                        limits,
                        resume_uploaded=session.resume_uploaded,
                        human_resolved=session.human_resolved,
                        answers_updated=True,
                    )
                )
            except PageUnavailable as exc:
                _mark_browser_crash(session)
                raise HTTPException(status_code=exc.status_code, detail="The page crashed.") from None
            if outcome is not None:
                _accept_result(session, outcome)
                return _answer_filled(session, item, body, started)
        if session.status in {Status.MANUAL_ANSWER_REQUIRED, Status.RESUME_UPLOAD_REQUIRED}:
            _run(session, store, active_runner, limits, resume_uploaded=False, answers_updated=True)
        return _answer_filled(session, item, body, started)

    @router.post("/sessions/{session_id}/credentials", response_model=CredentialsOut, tags=["sessions"])
    def store_credentials(session_id: str, body: CredentialsIn) -> CredentialsOut:
        session = _session_or_404(session_id)
        _reject_expired(session)
        _check_candidate(session, body.candidate_id.strip(), body.candidate_ref.strip())
        if body.mode in {"login", "register"}:
            if not _is_070(session) or session.status not in {
                Status.LOGIN_REQUIRED,
                Status.ACCOUNT_CREATION_REQUIRED,
                Status.LOGIN_FAILED,
            }:
                raise HTTPException(status_code=409, detail={"reason": "not_at_account_step"})
            _require_same_origin(session, body.site)
        try:
            require_login_name(body)
            domain = credential_domain(session.application_url, body.site)
        except ContextError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        session.credentials.put(
            session.candidate_id,
            domain,
            Credentials(
                site=domain,
                email=body.email.strip(),
                username=body.username.strip(),
                password=body.password,
            ),
        )
        session.remember_secret(body.password)
        if body.confirm_password:
            session.remember_secret(body.confirm_password)
        if body.email.strip():
            session.remember_secret(body.email.strip())
        logger.info("session=%s credentials_stored=true mode=%s", session.session_id, body.mode)
        if body.mode == "store":
            return CredentialsOut(sessionId=session.session_id)
        action = getattr(active_runner, "account_action", None)
        if action is None:
            raise HTTPException(status_code=409, detail="No page is open for this session.")
        try:
            report = action(
                session.session_id,
                {
                    "mode": body.mode,
                    "email": body.email.strip(),
                    "username": body.username.strip(),
                    "password": body.password,
                    "confirm": body.confirm_password or body.password,
                },
            )
        except PageUnavailable as exc:
            if exc.status_code == 503:
                raise HTTPException(
                    status_code=503,
                    detail="Page is navigating.",
                    headers=_shot_headers(session, retry=True),
                ) from None
            _mark_browser_crash(session)
            raise HTTPException(status_code=exc.status_code, detail="The page crashed.") from None
        return _finish_account_action(
            session,
            report,
            lambda: _run(session, store, active_runner, limits, resume_uploaded=session.resume_uploaded),
        )

    @router.post("/sessions/{session_id}/resume-file", tags=["sessions"])
    def upload_resume_file(
        session_id: str,
        file: Annotated[UploadFile, File()],
        candidateId: Annotated[str, Form()] = "",
        candidateRef: Annotated[str, Form()] = "",
        fieldHint: Annotated[str, Form()] = "",
    ) -> JSONResponse:
        """Attach one resume file and require the ATS to accept it. This is not pause/resume."""
        session = _session_or_404(session_id)
        _reject_expired(session)
        if not candidateId.strip() and not candidateRef.strip():
            raise HTTPException(status_code=422, detail="candidateId or candidateRef is required.")
        _check_candidate(session, candidateId.strip(), candidateRef.strip())
        if session.resume_verified:
            raise HTTPException(status_code=409, detail={"reason": "already_verified"})
        allowed = session.status == Status.RESUME_UPLOAD_REQUIRED or (
            session.resume_required and not session.resume_verified and session.status in _HUMAN_STOP
        )
        if not allowed:
            raise HTTPException(status_code=409, detail={"reason": "resume_not_required"})
        raw = _read_upload(file)
        try:
            name = validate_resume(file.filename or "", raw)
        except ResumeRejected as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.reason) from exc
        attach = getattr(active_runner, "attach_resume_file", None)
        if attach is None:
            raise HTTPException(status_code=409, detail={"reason": "resume_not_required"})
        try:
            report = attach(session.session_id, name, raw, fieldHint.strip())
        except PageUnavailable as exc:
            if exc.status_code == 503:
                raise HTTPException(
                    status_code=503,
                    detail="Page is navigating.",
                    headers=_shot_headers(session, retry=True),
                ) from None
            _mark_browser_crash(session)
            raise HTTPException(status_code=exc.status_code, detail="The page crashed.") from None
        if not report.get("verified"):
            session.clear_resume_bytes()
            logger.info("session=%s resume_file=rejected", session.session_id)
            return JSONResponse(
                status_code=422,
                content={
                    "verified": False,
                    "reason": str(report.get("reason") or "not_confirmed"),
                    "siteErrors": [str(item) for item in report.get("siteErrors") or []],
                },
            )
        _keep_resume_bytes(session, name, raw)
        session.resume_verified = True
        session.resume_required = True
        session.resume_source = "manual"
        session.resume_verified_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        session.resume_uploaded = True
        logger.info("session=%s resume_file=verified", session.session_id)
        if session.paused:
            session.pending_resume = True
            status = _status_out(session)
            return JSONResponse(
                status_code=200,
                content={"verified": True, "filename": name, "status": status.status},
            )
        accepted = _run(session, store, active_runner, limits, resume_uploaded=True)
        return JSONResponse(
            status_code=200,
            content={"verified": True, "filename": name, "status": accepted.status},
        )

    @router.get("/sessions/{session_id}/screenshot", tags=["sessions"])
    def session_screenshot(session_id: str) -> Response:
        session = _session_or_404(session_id)
        _reject_expired(session)
        shot = getattr(active_runner, "screenshot", None)
        try:
            image = shot(session_id) if shot is not None else None
        except PageUnavailable as exc:
            if exc.status_code == 503:
                raise HTTPException(
                    status_code=503,
                    detail="Page is navigating.",
                    headers=_shot_headers(session, retry=True),
                ) from None
            _mark_browser_crash(session)
            raise HTTPException(
                status_code=exc.status_code,
                detail="The page crashed.",
                headers=_shot_headers(session),
            ) from None
        if not image:
            raise HTTPException(status_code=409, detail="No page is open for this session.")
        payload, host = image
        session.screenshot_version += 1
        logger.info("session=%s action=screenshot result=ok", session.session_id)
        return Response(
            content=payload,
            media_type="image/jpeg",
            headers=_shot_headers(session, host=host),
        )

    @router.post("/sessions/{session_id}/interact", response_model=InteractOut, tags=["sessions"])
    def interact_session(session_id: str, body: InteractIn) -> InteractOut:
        session = _session_or_404(session_id)
        _reject_expired(session)
        _check_candidate(session, body.candidate_id.strip(), body.candidate_ref.strip())
        if session.status not in _HUMAN_STOP:
            raise HTTPException(status_code=409, detail=f"Cannot interact with a session in status {session.status}.")
        _limit_interact(session)
        action = {
            "action": body.action,
            "x": body.x,
            "y": body.y,
            "button": body.button,
            "clickCount": body.click_count,
            "text": body.text,
            "key": body.key,
            "dy": body.dy,
            "dx": body.dx,
        }
        apply = getattr(active_runner, "interact", None)
        try:
            moved = apply(session_id, action) if apply is not None else False
        except PageUnavailable as exc:
            if exc.status_code == 503:
                raise HTTPException(
                    status_code=503,
                    detail="Page is navigating.",
                    headers=_shot_headers(session, retry=True),
                ) from None
            _mark_browser_crash(session)
            raise HTTPException(
                status_code=exc.status_code,
                detail="The page crashed.",
                headers=_shot_headers(session),
            ) from None
        if not moved:
            raise HTTPException(status_code=409, detail="No page is open for this session.")
        session.screenshot_version += 1
        logger.info("session=%s action=%s result=ok", session.session_id, body.action)
        version = session.observe() if _is_070(session) else None
        return InteractOut(
            status=_public_status(session),
            screenshotVersion=session.screenshot_version,
            stateVersion=version,
        )

    @router.post("/sessions/{session_id}/human-done", response_model=RunStatusOut, tags=["sessions"])
    def human_done(session_id: str, body: HumanDoneIn) -> RunStatusOut:
        session = _session_or_404(session_id)
        _check_candidate(session, body.candidate_id.strip(), body.candidate_ref.strip())
        session.human_outcome = body.outcome
        session.wipe_secrets()
        active_runner.discard(session_id)
        logger.info("session=%s human_outcome=%s", session.session_id, body.outcome)
        return _status_out(session)

    @router.delete("/sessions/{session_id}", status_code=204, tags=["sessions"])
    def delete_session(session_id: str) -> Response:
        removed = store.delete(session_id)
        if removed is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        removed.status = Status.CANCELLED
        removed.wipe_secrets()
        active_runner.discard(session_id)
        logger.info("session=%s status=%s", session_id, Status.CANCELLED)
        return Response(status_code=204)

    app.include_router(router)
    return app
