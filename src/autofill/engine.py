"""Application state machine.

OPEN_URL → DETECT_SITE → DETECT_ATS → CHECK_AUTH → login states →
APPLICATION_READY → FILL_PAGE → NEXT_PAGE → FILL_NEXT_PAGE →
RESUME_UPLOAD_REQUIRED → CONTINUE_AFTER_RESUME → REVIEW_PAGE →
READY_FOR_HUMAN_SUBMIT, or a failure status.

The final Submit control is never clicked. CAPTCHA widgets stop the run.
"""

from __future__ import annotations

import contextvars
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from autofill.adapters import adapter_for, page_step
from autofill.ats import detect_ats, is_blocked_sso, is_manual_ats, site_domain
from autofill.credentials import CredentialProvider, SiteContext
from autofill.dom_ui import (
    attach_page,
    begin_run,
    bind_preview,
    current_run,
    empty_progress,
    end_run,
    finalize_run,
    note_filled,
    note_snapshot,
    remember_open_items,
    workflow_percent,
)
from autofill.extract import challenge_message, extract_page
from autofill.fill import fill_one_page
from autofill.login import (
    LoginDetector,
    account_decision,
    account_view,
    run_login,
    score_account,
    stop_account,
    verification_present,
)
from autofill.mapping import bind_standard_terms, consume_auto_consents
from autofill.models import FillResult, JobContext
from autofill.pagewait import (
    classify_page,
    current_page_type,
    find_entry_control,
    icims_login_gate,
    invalid_field_messages,
    is_job_closed,
    is_login_wall,
    is_review,
    is_site_error,
    navigation_signature,
    note_page_type,
    register_pane,
    reset_page_type,
    resettle_account,
    sign_in_pane,
    site_error_messages,
    terminal_kind,
    validation_message,
    visible_fields,
    wait_for_navigation,
    wait_for_render,
)
from autofill.profile import CandidateProfile
from autofill.resolver import ResolverBinding, ResolverCallError
from autofill.safeguards import (
    HUMAN_SUBMIT_ONLY,
    HumanSubmissionRequired,
    activate,
    activate_entry,
    dismiss_cookie_banner,
    field_class_for_button,
    is_add_row,
    is_forward_navigation,
)
from autofill.session import SessionStore
from autofill.stepfill import (
    FillFlags,
    bind_location_profile,
    fill_resolved_page,
    flags_for_alerts,
    inspect_required,
    required_gaps,
    reset_location_profile,
    validation_blob,
)

logger = logging.getLogger("autofill.engine")


class Status:
    CREATED = "CREATED"
    STARTED = "STARTED"
    FILLED = "FILLED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    ACCOUNT_CREATION_REQUIRED = "ACCOUNT_CREATION_REQUIRED"
    EMAIL_VERIFICATION_REQUIRED = "EMAIL_VERIFICATION_REQUIRED"
    LOGIN_FAILED = "LOGIN_FAILED"
    APPLICATION_READY = "APPLICATION_READY"
    FORM_IN_PROGRESS = "FORM_IN_PROGRESS"
    MANUAL_ANSWER_REQUIRED = "MANUAL_ANSWER_REQUIRED"
    RESUME_UPLOAD_REQUIRED = "RESUME_UPLOAD_REQUIRED"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    READY_FOR_HUMAN_SUBMIT = "READY_FOR_HUMAN_SUBMIT"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_FINAL = "FAILED_FINAL"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"
    JOB_DESCRIPTION = "JOB_DESCRIPTION"
    NO_FORM_FOUND = "NO_FORM_FOUND"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    NAVIGATION_STALLED = "NAVIGATION_STALLED"


@dataclass
class AutofillOptions:
    """Per-call options. ``page`` is an existing Playwright page when the host has one."""

    job_id: str = ""
    session_id: str = ""
    candidate_id: str | None = None
    max_pages: int = 8
    cover_letter_text: str | None = None
    cover_letter_path: str | None = None
    resume_uploaded: bool = False
    human_resolved: bool = False
    headless: bool = True
    page: Any = None
    job: JobContext | None = None
    resolver: ResolverBinding | None = None
    answers_updated: bool = False
    timings: dict[str, int] | None = None
    intent_hook: Any = None
    pause_gate: Any = None
    held_selectors: set[str] | None = None
    open_items: list[dict[str, Any]] | None = None
    progress: dict[str, Any] | None = None
    protocol_version: str = ""
    resume_name: str = ""
    resume_mime: str = ""
    resume_bytes: bytes | None = None
    resume_prepared: bool = False
    resume_verified_now: bool = False
    resume_site_errors: list[str] = field(default_factory=list)
    on_paint: Any = None
    on_page: Any = None
    on_navigation: Any = None
    on_confirmed: Any = None
    confirmed_values: list | None = None
    standard_terms_authorized: bool = False


@dataclass
class ApplicationResult:
    """Public result. Field details name controls, not the values written into them."""

    status: str
    ats: str | None
    current_step: str
    login_status: str
    fields_detected: int = 0
    fields_filled: int = 0
    fields_skipped: int = 0
    manual_actions: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)
    session_id: str = ""
    candidate_id: str = ""
    job_id: str = ""
    stopped_before_submit: bool = True
    messages: list[str] = field(default_factory=list)
    submit_controls: list[str] = field(default_factory=list)
    manual_questions: list[dict[str, str | None]] = field(default_factory=list)
    timings: dict[str, int] = field(default_factory=dict)
    progress: dict[str, Any] = field(default_factory=empty_progress)
    blocking: dict[str, Any] | None = None
    open_items: list[dict[str, Any]] = field(default_factory=list)
    account: dict[str, Any] | None = None
    completed_keys: list[str] = field(default_factory=list)
    resume_verified_now: bool = False
    resume_site_errors: list[str] = field(default_factory=list)
    page_type: str = ""
    auto_consents: list[dict[str, Any]] = field(default_factory=list)
    entry_clicks: int = 0
    next_clicks: int = 0
    pages_advanced: int = 0
    last_navigation: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if not self.stopped_before_submit:
            raise ValueError("stopped_before_submit cannot be false.")
        if not HUMAN_SUBMIT_ONLY:
            raise ValueError("HUMAN_SUBMIT_ONLY cannot be false.")

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "ats": self.ats,
            "currentStep": self.current_step,
            "loginStatus": self.login_status,
            "fieldsDetected": self.fields_detected,
            "fieldsFilled": self.fields_filled,
            "fieldsSkipped": self.fields_skipped,
            "manualActions": list(self.manual_actions),
            "steps": list(self.steps),
            "sessionId": self.session_id,
            "candidateId": self.candidate_id,
            "jobId": self.job_id,
            "stopped_before_submit": True,
            "messages": list(self.messages),
            "submitControls": list(self.submit_controls),
            "manualQuestions": [dict(item) for item in self.manual_questions],
            "timings": dict(self.timings),
            "progress": dict(self.progress),
            "blocking": dict(self.blocking) if self.blocking else None,
        }


_ACTIVE_OPTIONS: contextvars.ContextVar[AutofillOptions | None] = contextvars.ContextVar(
    "autofill_active_options",
    default=None,
)
_NAVIGATION: contextvars.ContextVar[dict | None] = contextvars.ContextVar("autofill_navigation", default=None)


def _navigation_record() -> dict:
    record = _NAVIGATION.get()
    if record is None:
        record = {"entryClicks": 0, "nextClicks": 0, "pagesAdvanced": 0, "lastNavigation": None}
        _NAVIGATION.set(record)
    return record


def _reset_navigation() -> None:
    _NAVIGATION.set({"entryClicks": 0, "nextClicks": 0, "pagesAdvanced": 0, "lastNavigation": None})


def _result(session, *, status: str, ats: str | None, login_status: str, **kwargs) -> ApplicationResult:
    logger.info(
        "session=%s candidate=%s domain=%s ats=%s status=%s step=%s",
        session.session_id,
        session.candidate_id,
        site_domain(session.application_url),
        ats or "",
        status,
        session.current_step,
    )
    if "timings" not in kwargs:
        kwargs["timings"] = {key: int(value) for key, value in session.timings.items()}
    active = _ACTIVE_OPTIONS.get()
    if active is not None:
        kwargs.setdefault("resume_verified_now", active.resume_verified_now)
        kwargs.setdefault("resume_site_errors", list(active.resume_site_errors))
    pending = _PENDING_ACCOUNT.get()
    if pending is not None:
        _PENDING_ACCOUNT.set(None)
        if pending[0] == status and pending[1] is not None and kwargs.get("account") is None:
            if status == Status.ACCOUNT_CREATION_REQUIRED or (
                active is not None and _protocol_070(active)
            ):
                kwargs["account"] = pending[1]
    progress, blocking, open_items = finalize_run(status)
    state = current_run()
    if "completed_keys" not in kwargs and state is not None:
        kwargs["completed_keys"] = list(state.filled_keys)
    result = ApplicationResult(
        status=status,
        ats=ats,
        current_step=session.current_step,
        login_status=login_status,
        steps=list(session.steps),
        session_id=session.session_id,
        candidate_id=session.candidate_id,
        job_id=session.job_id,
        progress=progress,
        blocking=blocking,
        open_items=open_items,
        **kwargs,
    )
    result.page_type = current_page_type()
    if result.page_type == "JOB_CLOSED":
        result.progress["stage"] = "CLOSED"
        result.progress["currentLabel"] = "Job closed"
        result.progress["percent"] = 0
    result.auto_consents = consume_auto_consents()
    nav = _navigation_record()
    result.entry_clicks = int(nav["entryClicks"])
    result.next_clicks = int(nav["nextClicks"])
    result.pages_advanced = int(nav["pagesAdvanced"])
    last = nav.get("lastNavigation")
    result.last_navigation = dict(last) if isinstance(last, dict) else None
    total = int(result.progress.get("requiredTotal") or 0)
    done = int(result.progress.get("requiredDone") or 0)
    if status in {
        Status.JOB_DESCRIPTION,
        Status.ACCOUNT_CREATION_REQUIRED,
        Status.LOGIN_REQUIRED,
        Status.LOGIN_FAILED,
        Status.EMAIL_VERIFICATION_REQUIRED,
        Status.CAPTCHA_REQUIRED,
        Status.RESUME_UPLOAD_REQUIRED,
        Status.NO_FORM_FOUND,
        Status.READY_FOR_HUMAN_SUBMIT,
        Status.NAVIGATION_STALLED,
    }:
        result.progress["percent"] = workflow_percent(status, result.page_type, total, done)
    if status == Status.NO_FORM_FOUND and total <= 0 and result.page_type not in {"JOB_DESCRIPTION", "APPLY_ENTRY"}:
        result.progress["percent"] = 0
    elif status == Status.READY_FOR_HUMAN_SUBMIT and total <= 0:
        if int(result.progress.get("percent") or 0) >= 100:
            result.progress["percent"] = 0
    if state is not None:
        end_run(state.token)
    return result


def _protocol_070(opts: AutofillOptions) -> bool:
    from autofill.login import protocol_at_least

    return protocol_at_least(opts.protocol_version)


def _this_page_counts(extra: dict, snapshot) -> None:
    """Field totals for the page in front of us. Earlier pages are not included."""
    from autofill.pagewait import visible_fields

    extra["fields_detected"] = len(visible_fields(snapshot))
    extra["fields_filled"] = 0
    extra["fields_skipped"] = 0
    state = current_run()
    if state is None:
        return
    state.seen.clear()
    state.done.clear()
    state.progress["requiredTotal"] = 0
    state.progress["requiredDone"] = 0


def _pane_stop(page, snapshot, session, opts: AutofillOptions, ats_name: str | None, **extra):
    """Score account, sign-in, job, and empty pages before a zero-field NO_FORM.

    Returns the snapshot to keep filling, and a result when the run stops.
    Account text is scored even when the inputs have not been extracted yet.
    """
    if resettle_account(page, snapshot):
        snapshot = extract_page(page)
        note_snapshot(snapshot)
    url = _page_url(page)
    extra.setdefault("fields_detected", len(visible_fields(snapshot)))
    login_status = str(extra.pop("login_status", "NOT_REQUIRED"))
    if snapshot.captcha_present:
        session.mark("FAILED")
        return snapshot, _result(
            session,
            status=Status.CAPTCHA_REQUIRED,
            ats=ats_name,
            login_status=login_status,
            messages=[challenge_message(snapshot)],
            **extra,
        )
    if is_job_closed(snapshot):
        note_page_type("JOB_CLOSED")
        session.mark(Status.FAILED_FINAL)
        return snapshot, _result(
            session,
            status=Status.FAILED_FINAL,
            ats=ats_name,
            login_status=login_status,
            messages=["job_expired"],
            **extra,
        )
    if is_site_error(snapshot):
        note_page_type("SITE_ERROR")
        session.mark(Status.FAILED_RETRYABLE)
        return snapshot, _result(
            session,
            status=Status.FAILED_RETRYABLE,
            ats=ats_name,
            login_status=login_status,
            messages=site_error_messages(snapshot),
            **extra,
        )
    kind = classify_page(snapshot, url=url)
    # A Create Account form is checked before any Sign In rule, and Continue
    # (``human_resolved``) does not skip it: the form is still in front of us.
    # Only the weaker text-only ACCOUNT page type is left to the person.
    if register_pane(snapshot) or (not opts.human_resolved and kind == "ACCOUNT"):
        _this_page_counts(extra, snapshot)
        decision = account_decision(snapshot, url, ats_name)
        if decision is not None and decision.status == Status.ACCOUNT_CREATION_REQUIRED:
            message = decision.message
            account = decision.account
        else:
            scored = score_account(snapshot, url)
            message = "Create an employer account. Nothing was typed."
            account = account_view(snapshot, url, ats_name, mode="register", alternate=scored.alternate)
        session.mark(Status.ACCOUNT_CREATION_REQUIRED)
        return snapshot, _result(
            session,
            status=Status.ACCOUNT_CREATION_REQUIRED,
            ats=ats_name,
            login_status=login_status,
            messages=[message],
            account=account,
            **extra,
        )
    if _protocol_070(opts) and verification_present(snapshot) and not opts.human_resolved:
        session.mark(Status.EMAIL_VERIFICATION_REQUIRED)
        return snapshot, _result(
            session,
            status=Status.EMAIL_VERIFICATION_REQUIRED,
            ats=ats_name,
            login_status=login_status,
            messages=["Email verification is required. Auto-Fill does not read mail."],
            **extra,
        )
    password_fields = any(control.kind == "password" and not control.hidden for control in snapshot.controls)
    # A Sign In password form stays on the credential path so stored credentials can be used.
    # Create Account is handled above. Text-only and empty sign-in panes stop here.
    if not opts.human_resolved and (
        sign_in_pane(snapshot) or kind == "LOGIN" or is_login_wall(snapshot) or icims_login_gate(snapshot, url)
    ) and not (
        snapshot.password_present or password_fields
    ):
        _this_page_counts(extra, snapshot)
        return snapshot, _login_stop(
            session,
            snapshot,
            url,
            ats_name,
            opts,
            messages=["Sign-in is required before the application form. Nothing was typed."],
            login_status=login_status,
            **extra,
        )
    if not opts.human_resolved and kind in {"JOB_DESCRIPTION", "APPLY_ENTRY"}:
        _this_page_counts(extra, snapshot)
        session.mark(Status.JOB_DESCRIPTION)
        return snapshot, _result(
            session,
            status=Status.JOB_DESCRIPTION,
            ats=ats_name,
            login_status=login_status,
            messages=[
                "This page is a job description. Auto-Fill did not find an application form.",
                "job_description" if kind == "JOB_DESCRIPTION" else "apply_entry",
            ],
            **extra,
        )
    if kind == "CONFIRMATION" or (
        not opts.human_resolved and not visible_fields(snapshot) and not is_review(snapshot) and kind not in {
            "APPLICATION_FORM",
            "REVIEW",
        }
    ):
        if not opts.human_resolved and (sign_in_pane(snapshot) or is_login_wall(snapshot)):
            _this_page_counts(extra, snapshot)
            return snapshot, _login_stop(
                session,
                snapshot,
                url,
                ats_name,
                opts,
                messages=["Sign-in is required before the application form. Nothing was typed."],
                login_status=login_status,
                **extra,
            )
        _this_page_counts(extra, snapshot)
        session.mark(Status.NO_FORM_FOUND)
        return snapshot, _result(
            session,
            status=Status.NO_FORM_FOUND,
            ats=ats_name,
            login_status=login_status,
            messages=_no_form_messages(),
            **extra,
        )
    return snapshot, None


def _account_wall(page, snapshot, session, opts: AutofillOptions, ats_name: str | None, **extra):
    """Register and email-verification stops for protocol 0.7.0. Older sessions stay on the old path."""
    if not _protocol_070(opts):
        return None
    from autofill.login import account_decision

    decision = account_decision(snapshot, getattr(page, "url", "") or "", ats_name)
    if decision is None:
        return None
    session.mark("FAILED")
    return _result(
        session,
        status=decision.status,
        ats=ats_name,
        login_status="NOT_REQUIRED",
        messages=[decision.message],
        account=decision.account,
        **extra,
    )


_CREATE_ACCOUNT_MESSAGE = "Create an employer account. Nothing was typed."


def _login_stop(
    session,
    snapshot,
    url: str,
    ats_name: str | None,
    opts: AutofillOptions,
    *,
    messages: list[str],
    login_status: str = "NOT_REQUIRED",
    **extra,
) -> ApplicationResult:
    """A Sign In stop, unless the page is really a Create Account form.

    A page that scores as Create Account (Verify / Confirm password, Workday
    ``createAccount*`` ids) is reported as ``ACCOUNT_CREATION_REQUIRED`` with a
    ``mode=register`` account, even when a Sign In link or tab is visible.
    """
    status, account = stop_account(snapshot, url, ats_name, Status.LOGIN_REQUIRED)
    if status == Status.ACCOUNT_CREATION_REQUIRED:
        logger.info("session=%s account_stop=register source=sign_in_check", session.session_id)
        note_page_type("ACCOUNT")
        session.mark(Status.ACCOUNT_CREATION_REQUIRED)
        return _result(
            session,
            status=Status.ACCOUNT_CREATION_REQUIRED,
            ats=ats_name,
            login_status=login_status,
            messages=[_CREATE_ACCOUNT_MESSAGE],
            account=account,
            **extra,
        )
    session.mark("LOGIN_FAILED")
    return _attach_login_account(
        _result(
            session,
            status=Status.LOGIN_REQUIRED,
            ats=ats_name,
            login_status="LOGIN_REQUIRED",
            messages=messages,
            **extra,
        ),
        snapshot,
        url,
        ats_name,
        opts,
    )


def _attach_login_account(result: ApplicationResult, snapshot, url: str, ats_name: str | None, opts: AutofillOptions):
    if not _protocol_070(opts) or result.status != Status.LOGIN_REQUIRED:
        return result
    from autofill.login import login_account_view

    view = login_account_view(snapshot, url, ats_name)
    if view is not None:
        result.account = view
    result.progress["stage"] = "ACCOUNT"
    result.progress["currentLabel"] = "Employer account"
    if int(result.progress.get("requiredTotal") or 0) <= 0:
        result.progress["percent"] = workflow_percent(Status.LOGIN_REQUIRED, "LOGIN", 0, 0)
    return result


def _maybe_reattach(page, opts: AutofillOptions) -> None:
    if opts.resume_prepared and not opts.resume_uploaded:
        return
    if not opts.resume_bytes or not opts.resume_name:
        return
    from autofill.resume_manual import reattach_verified

    reattach_verified(
        page,
        opts.resume_name,
        opts.resume_mime,
        opts.resume_bytes,
        session_id=opts.session_id,
    )


def _attach_prepared(page, opts: AutofillOptions) -> str:
    """Attach a file held before the control appeared.

    ``attached`` means the same verifier accepted it. ``failed`` keeps the
    bytes and records site errors. ``absent`` means nothing was prepared.
    """
    if page is None or opts.resume_uploaded or not opts.resume_prepared:
        return "absent"
    if not opts.resume_bytes or not opts.resume_name:
        return "absent"
    from autofill.resume_manual import attach_and_confirm
    from autofill.stepfill import post_upload_refill

    upload_started = time.perf_counter()
    report = attach_and_confirm(page, name=opts.resume_name, data=opts.resume_bytes)
    _add_ms(opts, "uploadMs", upload_started)
    if report.get("verified"):
        opts.resume_uploaded = True
        opts.resume_verified_now = True
        opts.resume_prepared = False
        post_upload_refill(page, session_id=opts.session_id)
        return "attached"
    opts.resume_site_errors = [str(item) for item in report.get("siteErrors") or []]
    return "failed"


def _counts(fields) -> tuple[int, int, int]:
    detected = len(fields)
    filled = sum(1 for item in fields if item.action in {"fill", "select", "check", "uncheck", "upload"})
    skipped = sum(1 for item in fields if item.action in {"skip", "unanswered", "resume_required"})
    return detected, filled, skipped


def autofill_application(
    application_url: str,
    candidate_profile: CandidateProfile,
    credential_provider: CredentialProvider | None = None,
    options: AutofillOptions | None = None,
) -> ApplicationResult:
    """Open ``application_url``, log in when needed, fill, and stop before Submit.

    Resume files are not selected. The result is ``RESUME_UPLOAD_REQUIRED``
    until ``options.resume_uploaded`` is true, which means a person already
    attached the file. CAPTCHA widgets return ``CAPTCHA_REQUIRED`` and are
    not solved.
    """
    opts = options or AutofillOptions()
    reset_page_type()
    bind_standard_terms(opts.standard_terms_authorized)
    if opts.max_pages < 1:
        raise ValueError("max_pages must be at least 1.")
    _ACTIVE_OPTIONS.set(opts)
    _reset_navigation()
    run_started = time.perf_counter()
    inspect_state = {"seen": False}
    store = SessionStore()
    session = store.open(
        profile=candidate_profile,
        application_url=application_url,
        job_id=opts.job_id,
        candidate_id=opts.candidate_id,
        session_id=opts.session_id,
    )
    if opts.timings is not None:
        session.timings = opts.timings
    session.mark("OPEN_URL")
    session.mark("DETECT_SITE")
    domain = site_domain(application_url)
    ats_info = detect_ats(application_url)
    ats_name = ats_info.name if ats_info else None
    _run_token = begin_run(
        page=opts.page,
        pause_gate=opts.pause_gate,
        held_selectors=opts.held_selectors,
        open_items=opts.open_items,
        progress=opts.progress,
        url=application_url,
        ats=ats_name,
        refill_cleared=_protocol_070(opts),
    )
    bind_preview(lambda current: _paint(current, opts))
    _profile_token = bind_location_profile(candidate_profile)
    session.mark("DETECT_ATS")
    logger.info(
        "session=%s domain=%s ats=%s step=%s",
        session.session_id,
        domain,
        ats_name or "",
        session.current_step,
    )

    if is_manual_ats(application_url):
        session.mark("FAILED")
        reset_location_profile(_profile_token)
        return _result(
            session,
            status=Status.MANUAL_REVIEW_REQUIRED,
            ats=ats_name,
            login_status="NOT_REQUIRED",
            messages=["This host is left for a person. Auto-Fill does not bypass its challenge."],
        )
    if is_blocked_sso(application_url):
        session.mark("FAILED")
        reset_location_profile(_profile_token)
        return _result(
            session,
            status=Status.UNSUPPORTED,
            ats=ats_name,
            login_status="NOT_REQUIRED",
            messages=["Identity-provider SSO is not an ATS password login. A person signs in there."],
        )

    page = opts.page
    owns_browser = page is None
    playwright = None
    browser = None
    from autofill.fill import bind_confirmed, bind_session_id, reset_confirmed, reset_session_id

    confirmed_token = None
    session_token = None
    try:
        confirmed_token = bind_confirmed(opts.on_confirmed, opts.confirmed_values)
        session_token = bind_session_id(opts.session_id)
        if page is None:
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(headless=opts.headless)
            page = browser.new_page()
            attach_page(page)
            page.goto(application_url)

        snapshot = _timed_extract(page, opts, run_started, inspect_state)
        if is_site_error(snapshot):
            note_page_type("SITE_ERROR")
            session.mark(Status.FAILED_RETRYABLE)
            return _result(
                session,
                status=Status.FAILED_RETRYABLE,
                ats=ats_name,
                login_status="NOT_REQUIRED",
                messages=site_error_messages(snapshot),
            )
        if snapshot.captcha_present:
            session.mark("FAILED")
            return _result(
                session,
                status=Status.CAPTCHA_REQUIRED,
                ats=ats_name,
                login_status="NOT_REQUIRED",
                messages=[challenge_message(snapshot)],
            )
        try:
            page, snapshot = _follow_entry(page, snapshot, session, opts, run_started, inspect_state)
        except NavigationStalled:
            session.mark(Status.NAVIGATION_STALLED)
            return _result(
                session,
                status=Status.NAVIGATION_STALLED,
                ats=ats_name,
                login_status="NOT_REQUIRED",
                messages=["navigation_stalled"],
            )
        snapshot, stopped = _pane_stop(page, snapshot, session, opts, ats_name)
        if stopped is not None:
            return stopped

        session.mark("CHECK_AUTH")
        detector = LoginDetector()
        mode = detector.mode(snapshot, application_url)
        login_status = "NOT_REQUIRED"
        if mode == "account_creation" or register_pane(snapshot):
            if register_pane(snapshot):
                session.mark(Status.ACCOUNT_CREATION_REQUIRED)
                scored = score_account(snapshot, getattr(page, "url", "") or application_url)
                return _result(
                    session,
                    status=Status.ACCOUNT_CREATION_REQUIRED,
                    ats=ats_name,
                    login_status="NOT_REQUIRED",
                    messages=["Create an employer account. Nothing was typed."],
                    account=account_view(
                        snapshot,
                        getattr(page, "url", "") or application_url,
                        ats_name,
                        mode="register",
                        alternate=scored.alternate,
                    ),
                    fields_detected=len(visible_fields(snapshot)),
                    fields_filled=0,
                    fields_skipped=0,
                )
            session.mark("FAILED")
            return _result(
                session,
                status=Status.MANUAL_REVIEW_REQUIRED,
                ats=ats_name,
                login_status="NOT_REQUIRED",
                messages=["Password field is mixed with the application. Auto-Fill will not create an account."],
            )
        # Continue on a sign-in form that is still showing uses saved credentials,
        # then fills the application. It never creates an account. A Create Account
        # pane already returned above.
        if mode in {"password", "email_first"} and (not opts.human_resolved or sign_in_pane(snapshot)):
            context = SiteContext(
                url=application_url,
                domain=domain,
                ats=ats_name,
                candidate_id=session.candidate_id,
                session_id=session.session_id,
            )
            creds = credential_provider.get_credentials(context, session.candidate_id) if credential_provider else None
            if creds is None or not creds.password or not (creds.email or creds.username):
                return _login_stop(
                    session,
                    snapshot,
                    getattr(page, "url", "") or application_url,
                    ats_name,
                    opts,
                    messages=["No credentials for this candidate and site. Nothing was typed."],
                    login_status="NOT_REQUIRED",
                )
            outcome, login_steps = run_login(page, creds)
            session.steps.extend(login_steps)
            if login_steps:
                session.current_step = login_steps[-1]
            if outcome == "CAPTCHA_REQUIRED":
                return _result(
                    session,
                    status=Status.CAPTCHA_REQUIRED,
                    ats=ats_name,
                    login_status="CAPTCHA_REQUIRED",
                    messages=["CAPTCHA appeared during login. Auto-Fill stopped."],
                )
            if outcome != "AUTHENTICATED":
                session.mark("LOGIN_FAILED")
                return _result(
                    session,
                    status=Status.LOGIN_FAILED,
                    ats=ats_name,
                    login_status="LOGIN_FAILED",
                    messages=["Login failed. The application was not filled."],
                )
            login_status = "AUTHENTICATED"
        else:
            login_status = "NOT_REQUIRED"

        session.mark("APPLICATION_READY")
        adapter = adapter_for(ats_name)
        if adapter:
            logger.info("adapter=%s", adapter.name)

        if opts.resolver is not None:
            return _run_resolver_pages(
                page,
                session,
                opts,
                ats_name=ats_name,
                login_status=login_status,
                run_started=run_started,
                inspect_state=inspect_state,
            )

        fields: list = []
        manual: list[str] = []
        submit_controls: list[str] = []
        seen: set[tuple[str, ...]] = set()
        resume_seen = False
        nav_block: tuple | None = None
        for index in range(opts.max_pages):
            _maybe_reattach(page, opts)
            snapshot = _timed_extract(page, opts, run_started, inspect_state)
            if snapshot.captcha_present:
                session.mark("FAILED")
                detected, filled, skipped = _counts(fields)
                return _result(
                    session,
                    status=Status.CAPTCHA_REQUIRED,
                    ats=ats_name,
                    login_status=login_status,
                    fields_detected=detected,
                    fields_filled=filled,
                    fields_skipped=skipped,
                    manual_actions=manual,
                    messages=[challenge_message(snapshot)],
                    submit_controls=submit_controls,
                )
            try:
                page, snapshot = _follow_entry(page, snapshot, session, opts, run_started, inspect_state)
            except NavigationStalled:
                detected, filled, skipped = _counts(fields)
                session.mark(Status.NAVIGATION_STALLED)
                return _result(
                    session,
                    status=Status.NAVIGATION_STALLED,
                    ats=ats_name,
                    login_status=login_status,
                    fields_detected=detected,
                    fields_filled=filled,
                    fields_skipped=skipped,
                    manual_actions=manual,
                    messages=["navigation_stalled"],
                    submit_controls=submit_controls,
                )
            if snapshot.captcha_present:
                session.mark("FAILED")
                detected, filled, skipped = _counts(fields)
                return _result(
                    session,
                    status=Status.CAPTCHA_REQUIRED,
                    ats=ats_name,
                    login_status=login_status,
                    fields_detected=detected,
                    fields_filled=filled,
                    fields_skipped=skipped,
                    manual_actions=manual,
                    messages=[challenge_message(snapshot)],
                    submit_controls=submit_controls,
                )
            detected, filled, skipped = _counts(fields)
            snapshot, stopped = _pane_stop(
                page,
                snapshot,
                session,
                opts,
                ats_name,
                login_status=login_status,
                fields_detected=detected,
                fields_filled=filled,
                fields_skipped=skipped,
                manual_actions=manual,
                submit_controls=submit_controls,
            )
            if stopped is not None:
                return stopped
            visible = tuple(control.selector for control in snapshot.controls if not control.hidden)
            if visible in seen:
                break
            seen.add(visible)
            for button in snapshot.buttons:
                if field_class_for_button(button.name, control_type=button.control_type) == "FINAL_SUBMIT":
                    if button.name not in submit_controls:
                        submit_controls.append(button.name)

            step_name = page_step(ats_name, snapshot.heading, application_url)
            session.mark("FILL_PAGE" if index == 0 else "FILL_NEXT_PAGE")
            if step_name == "review" or "review" in snapshot.heading.casefold():
                fill_started = time.perf_counter()
                page_fields, snapshot, _, page_manual = fill_one_page(
                    page,
                    candidate_profile,
                    cover_letter_path=opts.cover_letter_path,
                    cover_letter_text=opts.cover_letter_text,
                    job=opts.job,
                    resume_uploaded=opts.resume_uploaded,
                )
                fields.extend(page_fields)
                manual.extend(page_manual)
                _add_ms(opts, "fillMs", fill_started)
                _note_fill(page_fields, opts, run_started, inspect_state)
                fresh = extract_page(page)
                gaps, total, _done = inspect_required(page, fresh, candidate_profile)
                if gaps:
                    return _manual_for_gaps(
                        session,
                        gaps,
                        ats=ats_name,
                        login_status=login_status,
                        fields=fields,
                        manual=manual,
                        submit_controls=submit_controls,
                    )
                status, messages = _terminal_status(
                    page,
                    fresh,
                    gaps=gaps,
                    required_total=total,
                    submit_names=submit_controls,
                    review=True,
                )
                session.mark("REVIEW_PAGE")
                session.mark(status)
                detected, filled, skipped = _counts(fields)
                return _result(
                    session,
                    status=status,
                    ats=ats_name,
                    login_status=login_status,
                    fields_detected=detected,
                    fields_filled=filled,
                    fields_skipped=skipped,
                    manual_actions=manual,
                    messages=messages,
                    submit_controls=submit_controls,
                )

            fill_started = time.perf_counter()
            page_fields, snapshot, resume_blocked, page_manual = fill_one_page(
                page,
                candidate_profile,
                cover_letter_path=opts.cover_letter_path,
                cover_letter_text=opts.cover_letter_text,
                job=opts.job,
                resume_uploaded=opts.resume_uploaded,
            )
            fields.extend(page_fields)
            manual.extend(page_manual)
            _add_ms(opts, "fillMs", fill_started)
            _note_fill(page_fields, opts, run_started, inspect_state)
            for button in snapshot.buttons:
                if field_class_for_button(button.name, control_type=button.control_type) == "FINAL_SUBMIT":
                    if button.name not in submit_controls:
                        submit_controls.append(button.name)

            if resume_blocked and not opts.resume_uploaded:
                prepared = _attach_prepared(page, opts)
                if prepared == "attached":
                    refilled, snapshot, resume_blocked, refill_manual = fill_one_page(
                        page,
                        candidate_profile,
                        cover_letter_path=opts.cover_letter_path,
                        cover_letter_text=opts.cover_letter_text,
                        job=opts.job,
                        resume_uploaded=opts.resume_uploaded,
                    )
                    fields.extend(refilled)
                    manual.extend(refill_manual)
                    _note_fill(refilled, opts, run_started, inspect_state)
                elif prepared != "attached":
                    session.mark("RESUME_UPLOAD_REQUIRED")
                    detected, filled, skipped = _counts(fields)
                    return _result(
                        session,
                        status=Status.RESUME_UPLOAD_REQUIRED,
                        ats=ats_name,
                        login_status=login_status,
                        fields_detected=detected,
                        fields_filled=filled,
                        fields_skipped=skipped,
                        manual_actions=manual,
                        messages=[
                            "Upload the resume that was already downloaded, then call again with resume_uploaded."
                        ],
                        submit_controls=submit_controls,
                    )
            if opts.resume_uploaded and any(item.field_class == "RESUME_FIELD" for item in page_fields):
                resume_seen = True
                session.mark("CONTINUE_AFTER_RESUME")

            gaps = required_gaps(page, snapshot, candidate_profile)
            if gaps:
                return _manual_for_gaps(
                    session,
                    gaps,
                    ats=ats_name,
                    login_status=login_status,
                    fields=fields,
                    manual=manual,
                    submit_controls=submit_controls,
                )

            alert = validation_message(snapshot)
            if alert:
                session.mark(Status.MANUAL_ANSWER_REQUIRED)
                detected, filled, skipped = _counts(fields)
                return _result(
                    session,
                    status=Status.MANUAL_ANSWER_REQUIRED,
                    ats=ats_name,
                    login_status=login_status,
                    fields_detected=detected,
                    fields_filled=filled,
                    fields_skipped=skipped,
                    manual_actions=manual,
                    messages=[alert],
                    submit_controls=submit_controls,
                )
            current_sig = navigation_signature(page, snapshot)
            if nav_block is not None and nav_block[2] != current_sig:
                nav_block = None
            nxt = _next_button(snapshot, nav_block if nav_block and nav_block[2] == current_sig else None)
            if nxt is None:
                break
            session.mark("NEXT_PAGE")
            try:
                page, snapshot = _activate_forward(page, nxt, session, opts, run_started, inspect_state, snapshot)
            except NavigationStalled:
                detected, filled, skipped = _counts(fields)
                session.mark(Status.NAVIGATION_STALLED)
                return _result(
                    session,
                    status=Status.NAVIGATION_STALLED,
                    ats=ats_name,
                    login_status=login_status,
                    fields_detected=detected,
                    fields_filled=filled,
                    fields_skipped=skipped,
                    manual_actions=manual,
                    messages=["navigation_stalled"],
                    submit_controls=submit_controls,
                )
            if validation_message(snapshot):
                session.mark(Status.MANUAL_ANSWER_REQUIRED)
                detected, filled, skipped = _counts(fields)
                return _result(
                    session,
                    status=Status.MANUAL_ANSWER_REQUIRED,
                    ats=ats_name,
                    login_status=login_status,
                    fields_detected=detected,
                    fields_filled=filled,
                    fields_skipped=skipped,
                    manual_actions=manual,
                    messages=[validation_message(snapshot)],
                    submit_controls=submit_controls,
                )
            nav_block = (nxt.selector, nxt.name, navigation_signature(page, snapshot))

        if resume_seen:
            session.mark("CONTINUE_AFTER_RESUME")
        snapshot = _timed_extract(page, opts, run_started, inspect_state)
        on_review = "review" in (snapshot.heading or "").casefold()
        if on_review:
            session.mark("REVIEW_PAGE")
        gaps, total, _done = inspect_required(page, snapshot, candidate_profile)
        if gaps:
            return _manual_for_gaps(
                session,
                gaps,
                ats=ats_name,
                login_status=login_status,
                fields=fields,
                manual=manual,
                submit_controls=submit_controls,
            )
        status, messages = _terminal_status(
            page,
            snapshot,
            gaps=gaps,
            required_total=total,
            submit_names=submit_controls,
            review=on_review,
        )
        session.mark(status)
        detected, filled, skipped = _counts(fields)
        return _result(
            session,
            status=status,
            ats=ats_name,
            login_status=login_status,
            fields_detected=detected,
            fields_filled=filled,
            fields_skipped=skipped,
            manual_actions=manual,
            messages=messages,
            submit_controls=submit_controls,
        )
    except ResolverCallError as exc:
        session.mark("FAILED")
        status = _RESOLVER_STATUS.get(exc.code, Status.FAILED_RETRYABLE)
        logger.info("session=%s status=%s result=%s", session.session_id, status, exc.code)
        return _result(
            session,
            status=status,
            ats=ats_name,
            login_status="NOT_REQUIRED",
            messages=[f"Resolver result {exc.code}."],
            timings=_timings(opts),
        )
    except Exception as exc:
        if is_browser_crash(exc):
            raise
        if is_ats_timeout(exc):
            session.mark(Status.FAILED_RETRYABLE)
            logger.info(
                "session=%s status=%s result=%s",
                session.session_id,
                Status.FAILED_RETRYABLE,
                "ats_timeout",
            )
            return _result(
                session,
                status=Status.FAILED_RETRYABLE,
                ats=ats_name,
                login_status="NOT_REQUIRED",
                messages=["ats_timeout"],
                timings=_timings(opts),
            )
        session.mark("FAILED")
        logger.info("session=%s status=FAILED error_type=%s", session.session_id, type(exc).__name__)
        return _result(
            session,
            status=Status.FAILED,
            ats=ats_name,
            login_status="NOT_REQUIRED",
            messages=[f"Stopped because of {type(exc).__name__}."],
            timings=_timings(opts),
        )
    finally:
        reset_location_profile(_profile_token)
        if confirmed_token is not None:
            reset_confirmed(confirmed_token)
        if session_token is not None:
            reset_session_id(session_token)
        if owns_browser and browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()
        end_run(_run_token)


_RESOLVER_STATUS = {
    "unauthorized": Status.FAILED_FINAL,
    "mismatch": Status.FAILED_FINAL,
    "expired": Status.EXPIRED,
    "retryable": Status.FAILED_RETRYABLE,
    "invalid": Status.FAILED_FINAL,
        "too_large": Status.FAILED_RETRYABLE,
}

def is_ats_timeout(exc: BaseException) -> bool:
    """True for a navigation or page timeout. The category is ``ats_timeout``."""
    if is_browser_crash(exc):
        return False
    if "timeout" in type(exc).__name__.casefold():
        return True
    text = str(exc).casefold()
    return "timeout" in text and "exceeded" in text


def is_browser_crash(exc: BaseException) -> bool:
    """True when Chromium or its driver has gone away and a relaunch can help."""
    text = f"{type(exc).__name__} {exc}".casefold()
    needles = (
        "browser has been closed",
        "browser closed",
        "target closed",
        "target page, context or browser has been closed",
        "connection closed",
        "has been closed",
        "target crashed",
        "page crashed",
    )
    return any(needle in text for needle in needles)


def _timings(opts: AutofillOptions) -> dict[str, int]:
    if not opts.timings:
        return {}
    return {key: int(value) for key, value in opts.timings.items()}


def _mark_ms(opts: AutofillOptions, key: str, started: float) -> None:
    if opts.timings is None:
        return
    elapsed = (time.perf_counter() - started) * 1000
    opts.timings[key] = 0 if elapsed <= 0 else max(1, int(elapsed))


def _add_ms(opts: AutofillOptions, key: str, started: float) -> None:
    """Add a stage duration. Repeated stages, such as Next, accumulate."""
    if opts.timings is None:
        return
    elapsed = (time.perf_counter() - started) * 1000
    extra = 0 if elapsed <= 0 else max(1, int(elapsed))
    opts.timings[key] = int(opts.timings.get(key) or 0) + extra


def _note_fill(fields, opts: AutofillOptions, started: float, state: dict) -> None:
    note_filled(fields)
    if state.get("filled"):
        return
    if any(getattr(item, "action", "") in {"fill", "select", "check", "uncheck", "upload"} for item in fields):
        state["filled"] = True
        _mark_ms(opts, "firstFillMs", started)


def _paint(page, opts: AutofillOptions) -> None:
    """Store a frame for the preview. A crash is reported by the caller."""
    paint = getattr(opts, "on_paint", None)
    if paint is None or page is None:
        return
    try:
        paint(page)
    except Exception as exc:
        if is_browser_crash(exc):
            raise


def _timed_extract(page, opts: AutofillOptions, started: float, state: dict) -> Any:
    detect_started = time.perf_counter()
    wait_for_render(page)
    snapshot = extract_page(page)
    note_snapshot(snapshot)
    _paint(page, opts)
    if not state.get("seen"):
        state["seen"] = True
        _mark_ms(opts, "firstFormInspectedMs", started)
        _mark_ms(opts, "detectMs", detect_started)
    return snapshot


def _page_url(page) -> str:
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:
        return ""


def _observe_page(page, snapshot) -> str:
    return classify_page(snapshot, url=_page_url(page))


def _no_form_messages() -> list[str]:
    kind = current_page_type()
    reason = {
        "JOB_DESCRIPTION": "job_description",
        "APPLY_ENTRY": "apply_entry",
        "CONFIRMATION": "confirmation",
    }.get(kind, "no_application_form")
    return [
        "No application form was found. A person can open the page and continue.",
        reason,
    ]


_PENDING_ACCOUNT: contextvars.ContextVar[tuple[str, dict | None] | None] = contextvars.ContextVar(
    "autofill_pending_account", default=None
)


def _ats_for(page) -> str | None:
    info = detect_ats(_page_url(page))
    return info.name if info else None


def _terminal_status(
    page,
    snapshot,
    *,
    gaps: list,
    required_total: int,
    submit_names: list[str],
    review: bool = False,
) -> tuple[str, list[str]]:
    """READY only when the shared predicate agrees. A posting is not a form."""
    _observe_page(page, snapshot)
    decision = terminal_kind(
        snapshot,
        url=_page_url(page),
        gaps=gaps,
        required_total=required_total,
        submit_names=submit_names,
    )
    _PENDING_ACCOUNT.set(None)
    if decision in {"ACCOUNT", "LOGIN"}:
        wanted = Status.ACCOUNT_CREATION_REQUIRED if decision == "ACCOUNT" else Status.LOGIN_REQUIRED
        status, account = stop_account(snapshot, _page_url(page), _ats_for(page), wanted)
        _PENDING_ACCOUNT.set((status, account))
        if status == Status.ACCOUNT_CREATION_REQUIRED:
            if decision == "LOGIN":
                note_page_type("ACCOUNT")
            return Status.ACCOUNT_CREATION_REQUIRED, [_CREATE_ACCOUNT_MESSAGE]
        return Status.LOGIN_REQUIRED, ["Sign-in is required before the application form. Nothing was typed."]
    if decision == "JOB":
        reason = "job_description" if current_page_type() == "JOB_DESCRIPTION" else "apply_entry"
        return Status.JOB_DESCRIPTION, [
            "This page is a job description. Auto-Fill did not find an application form.",
            reason,
        ]
    if decision == "JOB_CLOSED":
        return Status.FAILED_FINAL, ["job_expired"]
    if decision == "SITE_ERROR":
        return Status.FAILED_RETRYABLE, site_error_messages(snapshot)
    if decision == "CAPTCHA":
        return Status.CAPTCHA_REQUIRED, [challenge_message(snapshot)]
    if decision == "MANUAL":
        return Status.MANUAL_ANSWER_REQUIRED, ["A required field is still empty."]
    if decision == "NO_FORM":
        return Status.NO_FORM_FOUND, _no_form_messages()
    if decision == "READY":
        if review or current_page_type() == "REVIEW":
            return Status.READY_FOR_HUMAN_SUBMIT, ["Review page reached. A person clicks Submit."]
        return Status.READY_FOR_HUMAN_SUBMIT, ["Filled what it could. A person clicks Submit or Apply."]
    return Status.FILLED, ["Filled what it could. A person clicks Submit or Apply."]


class NavigationStalled(Exception):
    """A click did not change the page. The same control is not clicked again."""

    def __init__(self, action: str) -> None:
        self.action = action
        super().__init__(action)


def _adopt_page(opts: AutofillOptions, new_page):
    """Keep screenshot, interact, and status on a tab the entry click opened."""
    if new_page is None:
        return None
    hook = getattr(opts, "on_page", None)
    if hook is not None:
        try:
            hook(new_page)
        except Exception:
            pass
    return new_page


def _log_move(session_id: str, action: str, result: str) -> None:
    """``result`` is a token. The label, URL, and candidate values stay out of the log."""
    logger.info("session=%s action=%s result=%s", session_id, action, result)
    record = _navigation_record()
    if action == "entry":
        record["entryClicks"] = int(record["entryClicks"]) + 1
    elif action == "next":
        record["nextClicks"] = int(record["nextClicks"]) + 1
    if result in {"advanced", "new_page"}:
        record["pagesAdvanced"] = int(record["pagesAdvanced"]) + 1
    record["lastNavigation"] = {"action": action, "result": result}
    opts = _ACTIVE_OPTIONS.get()
    hook = getattr(opts, "on_navigation", None) if opts is not None else None
    if hook is None:
        return
    try:
        hook(
            {
                "entryClicks": int(record["entryClicks"]),
                "nextClicks": int(record["nextClicks"]),
                "pagesAdvanced": int(record["pagesAdvanced"]),
                "lastNavigation": dict(record["lastNavigation"]),
            }
        )
    except Exception:
        return


def _next_button(snapshot, block: tuple | None = None):
    """The forward control on this step. Submit, Send, and Complete stay unclicked.

    ``block`` is ``(selector, name, signature)`` for a click that already moved
    the page. The same control is not pressed again while that page is showing.
    """
    for button in snapshot.buttons:
        if button.disabled or button.in_ad or button.in_header or not button.in_main:
            continue
        if is_add_row(button.name):
            continue
        if not is_forward_navigation(button.name, control_type=button.control_type):
            continue
        if field_class_for_button(button.name, control_type=button.control_type) != "NAVIGATION_CONTROL":
            continue
        if block is not None and block[:2] == (button.selector, button.name):
            continue
        return button
    return None


def _confirm_move(page, before, opts: AutofillOptions, started: float, state: dict):
    """Return the snapshot when the click moved the page. Otherwise raise."""
    try:
        advanced, snapshot = wait_for_navigation(page, before)
    except Exception as exc:
        if is_browser_crash(exc) or is_ats_timeout(exc):
            raise
        return False, None
    if not advanced or snapshot is None:
        return False, snapshot
    fresh = _timed_extract(page, opts, started, state)
    return True, fresh


def _follow_entry(page, snapshot, session, opts: AutofillOptions, started: float, state: dict):
    """Click at most two entry controls while the page is still a posting.

    A click that does not open a form, change the route, or open a tab is not
    repeated. The caller stops with ``NAVIGATION_STALLED``.
    """
    for _ in range(2):
        if snapshot.captcha_present:
            return page, snapshot
        if is_job_closed(snapshot):
            note_page_type("JOB_CLOSED")
            return page, snapshot
        kind = _observe_page(page, snapshot)
        if kind not in {"JOB_DESCRIPTION", "APPLY_ENTRY"}:
            return page, snapshot
        button = find_entry_control(snapshot, on_posting=True)
        if button is None:
            return page, snapshot
        before = navigation_signature(page, snapshot)
        try:
            dismiss_cookie_banner(page)
        except Exception as exc:
            if is_browser_crash(exc) or is_ats_timeout(exc):
                raise
        try:
            new_page = activate_entry(
                page,
                button.selector,
                button.name,
                control_type=button.control_type,
                aria_label=button.aria_label,
                automation_id=button.automation_id,
                href=button.href,
                role=button.role,
                frame_url=button.frame_url,
                in_header=button.in_header,
                in_ad=button.in_ad,
                in_main=button.in_main,
                on_posting=True,
            )
        except HumanSubmissionRequired:
            raise
        except Exception as exc:
            if is_browser_crash(exc) or is_ats_timeout(exc):
                raise
            _log_move(session.session_id, "entry", "stalled")
            raise NavigationStalled("entry") from exc
        opened = _adopt_page(opts, new_page)
        if opened is not None:
            page = opened
            _log_move(session.session_id, "entry", "new_page")
            snapshot = _timed_extract(page, opts, started, state)
            continue
        try:
            advanced, snapshot = _confirm_move(page, before, opts, started, state)
        except Exception:
            raise
        if not advanced:
            _log_move(session.session_id, "entry", "stalled")
            raise NavigationStalled("entry")
        _log_move(session.session_id, "entry", "advanced")
    _observe_page(page, snapshot)
    return page, snapshot


_RESUME_NAMED = re.compile(r"\b(resume|résumé|cv|curriculum vitae)\b", re.IGNORECASE)


def _same_route(page, before: tuple) -> bool:
    """True when the URL route is the one recorded before the click."""
    from urllib.parse import urlparse

    try:
        parsed = urlparse(str(getattr(page, "url", "") or ""))
    except Exception:
        return False
    route = (parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    return bool(before) and before[0] == route


def _activate_forward(page, button, session, opts: AutofillOptions, started: float, state: dict, snapshot):
    """Click Next and require the page to move. A dead click is not repeated.

    A validation alert or a challenge that appears on the same route is
    returned so the caller can correct it or stop. It is not a second click.
    """
    before = navigation_signature(page, snapshot)
    next_started = time.perf_counter()
    activate(page, button.selector, button.name, control_type=button.control_type, frame_url=button.frame_url)
    advanced, fresh = _confirm_move(page, before, opts, started, state)
    _add_ms(opts, "nextMs", next_started)
    if fresh is None:
        _log_move(session.session_id, "next", "stalled")
        raise NavigationStalled("next")
    if (
        advanced
        and not fresh.captcha_present
        and validation_message(fresh)
        and _same_route(page, before)
        and invalid_field_messages(page)
    ):
        # The employer redrew this step and flagged a field. That is not a new page.
        # A stale banner with no flagged field does not hold back a real advance.
        _log_move(session.session_id, "next", "invalid")
        return page, fresh
    if advanced or fresh.captcha_present:
        _log_move(session.session_id, "next", "advanced")
        return page, fresh
    if validation_message(fresh):
        _log_move(session.session_id, "next", "invalid")
        return page, fresh
    _log_move(session.session_id, "next", "stalled")
    raise NavigationStalled("next")


def _manual_for_gaps(
    session,
    gaps: list[dict],
    *,
    ats: str | None,
    login_status: str,
    fields,
    manual: list[str],
    submit_controls: list[str],
) -> ApplicationResult:
    remember_open_items(gaps)
    session.mark(Status.MANUAL_ANSWER_REQUIRED)
    detected, filled, skipped = _counts(fields)
    return _result(
        session,
        status=Status.MANUAL_ANSWER_REQUIRED,
        ats=ats,
        login_status=login_status,
        fields_detected=detected,
        fields_filled=filled,
        fields_skipped=skipped,
        manual_actions=manual,
        messages=["A required field is still empty."],
        submit_controls=submit_controls,
        manual_questions=_public_questions(gaps),
    )


def _public_questions(items: list[dict]) -> list[dict]:
    """Intent and question text only. Blocking is an internal flag."""
    published: list[dict] = []
    for item in items:
        entry = {"intent": item.get("intent"), "text": item.get("text") or ""}
        if entry not in published:
            published.append(entry)
    return published


def _remember_submit(snapshot, submit_controls: list[str]) -> None:
    for button in snapshot.buttons:
        if field_class_for_button(button.name, control_type=button.control_type) == "FINAL_SUBMIT":
            if button.name not in submit_controls:
                submit_controls.append(button.name)


def _blocks(item: dict) -> bool:
    """A manual item stops Next only when it is blocking and the page requires it.

    A saved answer that did not fit the options still stops, so the person sees it.
    """
    if not item.get("blocking", True):
        return False
    return bool(item.get("required", True)) or item.get("kind") == "OPTION_MISMATCH"


_STRUCTURE_ACTIONS = frozenset({"select", "check", "uncheck"})


def _reveal_settle_ms(fields: list) -> int:
    """A choice can reveal the next controls after a short timer. Text does not.

    Jobvite reveals the rest of the step 300 ms after the country select, so a
    select, check, or uncheck still waits. A text-only step does not.
    """
    for item in fields:
        if getattr(item, "action", "") in _STRUCTURE_ACTIONS:
            return 600
    return 40


def _revealed_fields(page, snapshot, settle_ms: int = 600) -> bool:
    """True when visible fields appeared on the same step after the last write."""
    if page is None or snapshot is None or getattr(page, "wait_for_timeout", None) is None:
        return False
    before = {control.selector for control in snapshot.controls if not control.hidden}
    try:
        page.wait_for_timeout(settle_ms)
        fresh = extract_page(page)
    except Exception:
        return False
    after = {control.selector for control in fresh.controls if not control.hidden}
    return bool(after - before)


def _run_resolver_pages(
    page,
    session,
    opts: AutofillOptions,
    *,
    ats_name: str | None,
    login_status: str,
    run_started: float,
    inspect_state: dict,
) -> ApplicationResult:
    """Step through the form, asking the resolver only for the current step."""
    binding = opts.resolver
    if binding is None:
        raise RuntimeError("Resolver mode was entered without a binding.")
    session.mark(Status.STARTED)
    session.mark(Status.FORM_IN_PROGRESS)
    fields: list = []
    manual: list[str] = []
    submit_controls: list[str] = []
    seen: set[tuple[str, ...]] = set()
    carried: list[dict] = []

    def finish(status: str, messages: list[str], *, questions: list[dict] | None = None) -> ApplicationResult:
        detected, filled, skipped = _counts(fields)
        rich = list(questions or []) + carried
        remember_open_items(rich)
        published = _public_questions(rich)
        return _result(
            session,
            status=status,
            ats=ats_name,
            login_status=login_status,
            fields_detected=detected,
            fields_filled=filled,
            fields_skipped=skipped,
            manual_actions=manual,
            messages=messages,
            submit_controls=submit_controls,
            manual_questions=published,
            timings=_timings(opts),
        )

    def stop_required(current, existing: list[dict] | None = None):
        gaps = required_gaps(page, current, None, existing)
        if not gaps:
            return None
        session.mark(Status.MANUAL_ANSWER_REQUIRED)
        return finish(
            Status.MANUAL_ANSWER_REQUIRED,
            ["A required field is still empty."],
            questions=gaps,
        )

    nav_block: tuple | None = None
    for index in range(opts.max_pages):
        _maybe_reattach(page, opts)
        snapshot = _timed_extract(page, opts, run_started, inspect_state)
        if snapshot.captcha_present:
            session.mark("FAILED")
            return finish(
                Status.CAPTCHA_REQUIRED,
                [challenge_message(snapshot)],
            )
        try:
            page, snapshot = _follow_entry(page, snapshot, session, opts, run_started, inspect_state)
        except NavigationStalled:
            session.mark(Status.NAVIGATION_STALLED)
            return finish(Status.NAVIGATION_STALLED, ["navigation_stalled"])
        snapshot, stopped = _pane_stop(page, snapshot, session, opts, ats_name, login_status=login_status)
        if stopped is not None:
            return stopped
        visible = tuple(control.selector for control in snapshot.controls if not control.hidden)
        if visible in seen:
            break
        seen.add(visible)
        _remember_submit(snapshot, submit_controls)
        step_name = page_step(ats_name, snapshot.heading, session.application_url)
        session.mark("FILL_PAGE" if index == 0 else "FILL_NEXT_PAGE")
        on_review = step_name == "review" or "review" in snapshot.heading.casefold()
        flags = FillFlags()
        recheck = False
        for attempt in range(3):
            fill_started = time.perf_counter()
            outcome = fill_resolved_page(
                page,
                binding,
                session_id=session.session_id,
                ats_name=ats_name,
                step=step_name,
                flags=flags,
                hook=opts.intent_hook,
                resume_uploaded=opts.resume_uploaded,
            )
            _add_ms(opts, "fillMs", fill_started)
            fields.extend(outcome.fields)
            manual.extend(outcome.manual_actions)
            _note_fill(outcome.fields, opts, run_started, inspect_state)
            if outcome.snapshot is not None:
                snapshot = outcome.snapshot
                _remember_submit(snapshot, submit_controls)
            blocking = [item for item in outcome.manual_questions if _blocks(item)]
            optional = [item for item in outcome.manual_questions if not _blocks(item)]
            if attempt < 2 and _revealed_fields(
                page,
                outcome.snapshot or snapshot,
                settle_ms=_reveal_settle_ms(outcome.fields),
            ):
                # The write opened more of this step (a consent gate or a
                # conditional section). Fill what appeared before deciding.
                continue
            if outcome.resume_blocked and not opts.resume_uploaded:
                prepared = _attach_prepared(page, opts)
                if prepared != "attached":
                    # The resume stop is reported with every question this step already
                    # asked. HIGH answers were written above; unanswered ones stay listed.
                    pending = blocking if opts.answers_updated else list(outcome.manual_questions)
                    carried.extend(optional)
                    messages = ["Upload the resume that was already downloaded, then continue with resumeUploaded."]
                    if pending:
                        messages.append("A saved answer is required. Nothing below HIGH confidence was written.")
                    session.mark(Status.RESUME_UPLOAD_REQUIRED)
                    return finish(
                        Status.RESUME_UPLOAD_REQUIRED,
                        messages,
                        questions=pending,
                    )
            if on_review:
                if blocking or (outcome.manual_questions and not opts.answers_updated):
                    session.mark(Status.MANUAL_ANSWER_REQUIRED)
                    return finish(
                        Status.MANUAL_ANSWER_REQUIRED,
                        ["A saved answer is required. Nothing below HIGH confidence was written."],
                        questions=blocking or outcome.manual_questions,
                    )
                carried.extend(optional)
                fresh = extract_page(page)
                gaps, total, _done = inspect_required(page, fresh, None, outcome.manual_questions)
                if gaps:
                    session.mark(Status.MANUAL_ANSWER_REQUIRED)
                    return finish(
                        Status.MANUAL_ANSWER_REQUIRED,
                        ["A required field is still empty."],
                        questions=gaps,
                    )
                status, messages = _terminal_status(
                    page,
                    fresh,
                    gaps=gaps,
                    required_total=total,
                    submit_names=submit_controls,
                    review=True,
                )
                session.mark("REVIEW_PAGE")
                session.mark(status)
                return finish(status, messages)
            if blocking or (outcome.manual_questions and not opts.answers_updated):
                session.mark(Status.MANUAL_ANSWER_REQUIRED)
                pending = blocking if opts.answers_updated else outcome.manual_questions
                return finish(
                    Status.MANUAL_ANSWER_REQUIRED,
                    ["A saved answer is required. Nothing below HIGH confidence was written."],
                    questions=pending,
                )
            carried.extend(optional)
            held = stop_required(snapshot, outcome.manual_questions)
            if held is not None:
                return held
            alert = validation_message(snapshot)
            if alert and not recheck:
                updated = flags_for_alerts(validation_blob(snapshot), flags)
                if updated != flags and attempt < 2:
                    flags = updated
                    recheck = True
                    continue
                session.mark(Status.MANUAL_ANSWER_REQUIRED)
                return finish(Status.MANUAL_ANSWER_REQUIRED, [alert, *invalid_field_messages(page)])
            recheck = False
            current_sig = navigation_signature(page, snapshot)
            if nav_block is not None and nav_block[2] != current_sig:
                nav_block = None
            nxt = _next_button(snapshot, nav_block if nav_block and nav_block[2] == current_sig else None)
            if nxt is None:
                break
            session.mark("NEXT_PAGE")
            try:
                page, snapshot = _activate_forward(page, nxt, session, opts, run_started, inspect_state, snapshot)
            except NavigationStalled:
                session.mark(Status.NAVIGATION_STALLED)
                return finish(Status.NAVIGATION_STALLED, ["navigation_stalled"])
            if snapshot.captcha_present:
                session.mark("FAILED")
                return finish(
                    Status.CAPTCHA_REQUIRED,
                    [challenge_message(snapshot)],
                )
            if validation_message(snapshot):
                named = invalid_field_messages(page)
                if not opts.resume_uploaded and any(_RESUME_NAMED.search(item) for item in named):
                    # The employer flagged the resume, which this step hid behind
                    # its own picker. Attach the prepared file and try the step again.
                    if _attach_prepared(page, opts) == "attached":
                        continue
                    session.mark(Status.RESUME_UPLOAD_REQUIRED)
                    return finish(
                        Status.RESUME_UPLOAD_REQUIRED,
                        ["Upload the resume that was already downloaded, then continue with resumeUploaded.", *named],
                    )
                if attempt == 2:
                    session.mark(Status.MANUAL_ANSWER_REQUIRED)
                    return finish(
                        Status.MANUAL_ANSWER_REQUIRED,
                        [validation_message(snapshot), *invalid_field_messages(page)],
                    )
                continue
            nav_block = (nxt.selector, nxt.name, navigation_signature(page, snapshot))
            break

    snapshot = _timed_extract(page, opts, run_started, inspect_state)
    _remember_submit(snapshot, submit_controls)
    on_review = "review" in (snapshot.heading or "").casefold()
    if on_review:
        session.mark("REVIEW_PAGE")
    gaps, total, _done = inspect_required(page, snapshot, None)
    if gaps:
        session.mark(Status.MANUAL_ANSWER_REQUIRED)
        return finish(
            Status.MANUAL_ANSWER_REQUIRED,
            ["A required field is still empty."],
            questions=gaps,
        )
    status, messages = _terminal_status(
        page,
        snapshot,
        gaps=gaps,
        required_total=total,
        submit_names=submit_controls,
        review=on_review,
    )
    session.mark(status)
    return finish(status, messages)


def autofill_from_fill_result(result: FillResult) -> str:
    """Map the lower-level fill status onto the public status vocabulary."""
    if result.captcha_present and result.pages_filled == 0:
        return Status.CAPTCHA_REQUIRED
    if result.resume_required:
        return Status.RESUME_UPLOAD_REQUIRED
    if result.login_wall and result.pages_filled == 0:
        return Status.LOGIN_REQUIRED
    return Status.FILLED
