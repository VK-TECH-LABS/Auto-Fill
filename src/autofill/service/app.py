"""Versioned HTTP API. Final Submit is not a route."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from autofill import __version__
from autofill.credentials import Credentials
from autofill.engine import ApplicationResult, Status
from autofill.safeguards import HUMAN_SUBMIT_ONLY, HumanSubmissionRequired
from autofill.service.auth import require_configured_token, tokens_match
from autofill.service.context import ContextError, build_profile, credential_domain, require_login_name
from autofill.service.runner import BrowserDisabled, FillRequest, FillRunner, PlaywrightRunner
from autofill.service.schemas import (
    CandidateCheckIn,
    ContinueIn,
    CreateSessionIn,
    CredentialsIn,
    CredentialsOut,
    HealthOut,
    RunStatusOut,
)
from autofill.service.sessions import ServiceSession, ServiceSessionStore, new_session_id

logger = logging.getLogger("autofill.service")

_REFUSED_STATUSES = frozenset({"SUBMITTED", "APPLIED", "SUBMIT_CLICKED", "APPLICATION_SUBMITTED"})
_STARTABLE = frozenset({Status.LOGIN_REQUIRED, Status.LOGIN_FAILED, "CREATED"})
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


def _status_out(session: ServiceSession) -> RunStatusOut:
    result = session.result
    if result is None:
        return RunStatusOut(
            sessionId=session.session_id,
            status=session.status,
            stopped_before_submit=True,
            candidateId=session.candidate_id,
            jobId=session.job_id,
        )
    payload = result.to_dict()
    return RunStatusOut(
        sessionId=session.session_id,
        status=session.status,
        stopped_before_submit=True,
        candidateId=session.candidate_id,
        jobId=session.job_id,
        ats=payload["ats"],
        currentStep=payload["currentStep"],
        loginStatus=payload["loginStatus"],
        fieldsDetected=payload["fieldsDetected"],
        fieldsFilled=payload["fieldsFilled"],
        fieldsSkipped=payload["fieldsSkipped"],
        manualActions=list(payload["manualActions"]),
        steps=list(payload["steps"]),
        messages=list(payload["messages"]),
        submitControls=list(payload["submitControls"]),
    )


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
    logger.info(
        "session=%s status=%s ats=%s step=%s",
        session.session_id,
        result.status,
        result.ats or "",
        result.current_step,
    )
    return _status_out(session)


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


def _check_candidate(session: ServiceSession, candidate_id: str) -> None:
    if candidate_id and candidate_id != session.candidate_id:
        raise HTTPException(status_code=409, detail="Candidate does not match this session.")


def _request_for(session: ServiceSession, *, resume_uploaded: bool) -> FillRequest:
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
    )


def _run(
    session: ServiceSession,
    store: ServiceSessionStore,
    runner: FillRunner,
    *,
    resume_uploaded: bool,
) -> RunStatusOut:
    try:
        session = store.begin_run(session.session_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Session not found.") from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail="Session is already running.") from exc
    try:
        try:
            result = runner.run(_request_for(session, resume_uploaded=resume_uploaded))
            return _accept_result(session, result)
        except HTTPException:
            raise
        except BrowserDisabled as exc:
            raise HTTPException(status_code=409, detail="Browser execution is disabled.") from exc
        except HumanSubmissionRequired as exc:
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
) -> FastAPI:
    """Build the service. ``token`` is the bearer secret and is not stored in the schema."""
    expected = require_configured_token(token)
    store = ServiceSessionStore(max_sessions=max_sessions)
    launch_args = ["--disable-dev-shm-usage"]
    if browser_no_sandbox:
        launch_args.append("--no-sandbox")
    active_runner = runner if runner is not None else PlaywrightRunner(enabled=run_browser, launch_args=launch_args)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
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
        try:
            profile, job, cover = build_profile(body)
        except ContextError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        session = ServiceSession(
            session_id=new_session_id(),
            candidate_id=profile.candidate_id,
            job_id=body.job_context.job_id.strip(),
            application_url=job.url,
            profile=profile,
            job=job,
            cover_letter_text=cover,
        )
        try:
            store.create(session)
        except RuntimeError as exc:
            raise HTTPException(status_code=429, detail="Too many open sessions.") from exc
        logger.info("session=%s status=CREATED", session.session_id)
        return _status_out(session)

    @router.post("/sessions/{session_id}/start", response_model=RunStatusOut, tags=["sessions"])
    def start_session(session_id: str, body: CandidateCheckIn | None = None) -> RunStatusOut:
        session = _session_or_404(session_id)
        _check_candidate(session, body.candidate_id.strip() if body is not None else "")
        if session.status not in _STARTABLE:
            raise HTTPException(status_code=409, detail=f"Cannot start a session in status {session.status}.")
        return _run(session, store, active_runner, resume_uploaded=False)

    @router.get("/sessions/{session_id}/status", response_model=RunStatusOut, tags=["sessions"])
    def session_status(session_id: str) -> RunStatusOut:
        return _status_out(_session_or_404(session_id))

    @router.post("/sessions/{session_id}/continue", response_model=RunStatusOut, tags=["sessions"])
    def continue_session(session_id: str, body: ContinueIn) -> RunStatusOut:
        session = _session_or_404(session_id)
        _check_candidate(session, body.candidate_id.strip())
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
        if session.status != Status.RESUME_UPLOAD_REQUIRED:
            raise HTTPException(status_code=409, detail=f"Cannot continue a session in status {session.status}.")
        if not body.resume_uploaded:
            raise HTTPException(status_code=409, detail="Continue only after a person has uploaded the resume.")
        return _run(session, store, active_runner, resume_uploaded=True)

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
        logger.info("session=%s credentials_stored=true", session.session_id)
        return CredentialsOut(sessionId=session.session_id)

    @router.delete("/sessions/{session_id}", status_code=204, tags=["sessions"])
    def delete_session(session_id: str) -> Response:
        removed = store.delete(session_id)
        if removed is None:
            raise HTTPException(status_code=404, detail="Session not found.")
        active_runner.discard(session_id)
        logger.info("session=%s status=DELETED", session_id)
        return Response(status_code=204)

    app.include_router(router)
    return app
