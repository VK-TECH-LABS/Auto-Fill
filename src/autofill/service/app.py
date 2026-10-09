"""Versioned HTTP API. Final Submit is not a route."""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from autofill import __version__
from autofill.credentials import Credentials
from autofill.engine import ApplicationResult, Status
from autofill.models import JobContext
from autofill.redact import install_redaction, redaction_filter
from autofill.resolver import ResolverBinding
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
    RunStatusOut,
    TimingsOut,
)
from autofill.service.sessions import ServiceSession, ServiceSessionStore, new_session_id

logger = logging.getLogger("autofill.service")

_REFUSED_STATUSES = frozenset({"SUBMITTED", "APPLIED", "SUBMIT_CLICKED", "APPLICATION_SUBMITTED"})
_STARTABLE = frozenset({Status.LOGIN_REQUIRED, Status.LOGIN_FAILED, Status.CREATED, "CREATED", Status.FAILED_RETRYABLE})
_HUMAN_STOP = frozenset(
    {
        Status.LOGIN_REQUIRED,
        Status.CAPTCHA_REQUIRED,
        Status.MANUAL_ANSWER_REQUIRED,
        Status.RESUME_UPLOAD_REQUIRED,
        Status.NO_FORM_FOUND,
        Status.READY_FOR_HUMAN_SUBMIT,
    }
)


def _limit_interact(session) -> None:
    """At most 30 human actions per minute for one session."""
    now = time.monotonic()
    session.interact_marks = [mark for mark in session.interact_marks if now - mark < 60]
    if len(session.interact_marks) >= 30:
        raise HTTPException(status_code=429, detail="Too many interactions for this session.")
    session.interact_marks.append(now)


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


def _status_out(session: ServiceSession) -> RunStatusOut:
    result = session.result
    body = RunStatusOut(
        sessionId=session.session_id,
        status=session.status,
        stopped_before_submit=True,
        candidateId=session.candidate_id,
        jobId=session.job_id,
        protocolVersion=session.protocol_version,
        manualQuestions=_questions_out(session),
        timings=_timings_out(session),
        humanOutcome=session.human_outcome,
    )
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
    session.manual_questions = [dict(item) for item in result.manual_questions]
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
    return _status_out(session)


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
        cover_letter_text=session.cover_letter_text,
        resolver=_binding(session, limits),
        answers_updated=answers_updated,
        session_created_ms=int(session.timings.get("sessionCreatedMs", 0)),
        max_pages=12 if session.mode == "resolver" else 8,
    )


def _run(
    session: ServiceSession,
    store: ServiceSessionStore,
    runner: FillRunner,
    limits: dict[str, float | int],
    *,
    resume_uploaded: bool,
    answers_updated: bool = False,
) -> RunStatusOut:
    try:
        session = store.begin_run(session.session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Session not found.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail="Session is already running.") from exc
    previous = session.status
    session.status = Status.STARTED
    try:
        try:
            result = runner.run(
                _request_for(
                    session,
                    limits,
                    resume_uploaded=resume_uploaded,
                    answers_updated=answers_updated,
                )
            )
            return _accept_result(session, result)
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
                    protocol_version=body.protocol_version.strip(),
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
                    protocol_version=body.protocol_version.strip(),
                )
        except ContextError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        session.expires_at = time.monotonic() + session_ttl_seconds
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
        return _status_out(_session_or_404(session_id))

    @router.post("/sessions/{session_id}/continue", response_model=RunStatusOut, tags=["sessions"])
    def continue_session(session_id: str, body: ContinueIn) -> RunStatusOut:
        session = _session_or_404(session_id)
        _check_candidate(session, body.candidate_id.strip(), body.candidate_ref.strip())
        if body.human_resolved:
            if session.status == Status.FAILED_RETRYABLE:
                _allow_browser_restart(session)
                return _run(session, store, active_runner, limits, resume_uploaded=False)
            if session.status not in {Status.LOGIN_REQUIRED, Status.CAPTCHA_REQUIRED, Status.NO_FORM_FOUND}:
                raise HTTPException(
                    status_code=409,
                    detail="humanResolved continues only after login, a challenge, or a missing form.",
                )
            return _run(session, store, active_runner, limits, resume_uploaded=False)
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
        return _run(session, store, active_runner, limits, resume_uploaded=True)

    @router.post("/sessions/{session_id}/credentials", response_model=CredentialsOut, tags=["sessions"])
    def store_credentials(session_id: str, body: CredentialsIn) -> CredentialsOut:
        session = _session_or_404(session_id)
        _check_candidate(session, body.candidate_id.strip())
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
        redaction_filter().add(body.password)
        if body.email.strip():
            redaction_filter().add(body.email.strip())
        logger.info("session=%s credentials_stored=true", session.session_id)
        return CredentialsOut(sessionId=session.session_id)

    @router.get("/sessions/{session_id}/screenshot", tags=["sessions"])
    def session_screenshot(session_id: str) -> Response:
        session = _session_or_404(session_id)
        shot = getattr(active_runner, "screenshot", None)
        try:
            image = shot(session_id) if shot is not None else None
        except PageUnavailable as exc:
            _mark_browser_crash(session)
            raise HTTPException(
                status_code=exc.status_code,
                detail="The page crashed.",
                headers={"X-Autofill-Status": session.status},
            ) from None
        if not image:
            raise HTTPException(status_code=409, detail="No page is open for this session.")
        payload, host = image
        session.screenshot_version += 1
        logger.info("session=%s action=screenshot result=ok", session.session_id)
        return Response(
            content=payload,
            media_type="image/jpeg",
            headers={
                "X-Autofill-Status": session.status,
                "X-Autofill-Url-Host": host,
            },
        )

    @router.post("/sessions/{session_id}/interact", response_model=InteractOut, tags=["sessions"])
    def interact_session(session_id: str, body: InteractIn) -> InteractOut:
        session = _session_or_404(session_id)
        _check_candidate(session, body.candidate_id.strip(), body.candidate_ref.strip())
        if session.status not in _HUMAN_STOP:
            raise HTTPException(status_code=409, detail=f"Cannot interact with a session in status {session.status}.")
        _limit_interact(session)
        action = {"action": body.action, "x": body.x, "y": body.y, "text": body.text, "key": body.key, "dy": body.dy}
        apply = getattr(active_runner, "interact", None)
        try:
            moved = apply(session_id, action) if apply is not None else False
        except PageUnavailable as exc:
            _mark_browser_crash(session)
            raise HTTPException(
                status_code=exc.status_code,
                detail="The page crashed.",
                headers={"X-Autofill-Status": session.status},
            ) from None
        if not moved:
            raise HTTPException(status_code=409, detail="No page is open for this session.")
        session.screenshot_version += 1
        logger.info("session=%s action=%s result=ok", session.session_id, body.action)
        return InteractOut(status=session.status, screenshotVersion=session.screenshot_version)

    @router.post("/sessions/{session_id}/human-done", response_model=RunStatusOut, tags=["sessions"])
    def human_done(session_id: str, body: HumanDoneIn) -> RunStatusOut:
        session = _session_or_404(session_id)
        _check_candidate(session, body.candidate_id.strip(), body.candidate_ref.strip())
        session.human_outcome = body.outcome
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
