"""Application state machine.

OPEN_URL → DETECT_SITE → DETECT_ATS → CHECK_AUTH → login states →
APPLICATION_READY → FILL_PAGE → NEXT_PAGE → FILL_NEXT_PAGE →
RESUME_UPLOAD_REQUIRED → CONTINUE_AFTER_RESUME → REVIEW_PAGE →
READY_FOR_HUMAN_SUBMIT, or a failure status.

The final Submit control is never clicked. CAPTCHA widgets stop the run.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from autofill.adapters import adapter_for, page_step
from autofill.ats import detect_ats, is_blocked_sso, is_manual_ats, site_domain
from autofill.credentials import CredentialProvider, SiteContext
from autofill.extract import extract_page
from autofill.fill import fill_one_page
from autofill.login import LoginDetector, run_login
from autofill.models import FillResult, JobContext
from autofill.pagewait import choose_entry, is_login_wall, is_review, visible_fields, wait_for_render
from autofill.profile import CandidateProfile
from autofill.resolver import ResolverBinding, ResolverCallError
from autofill.safeguards import (
    HUMAN_SUBMIT_ONLY,
    activate,
    activate_entry,
    field_class_for_button,
    is_add_row,
    is_forward_navigation,
)
from autofill.session import SessionStore
from autofill.stepfill import FillFlags, fill_resolved_page, flags_for_alerts, validation_blob

logger = logging.getLogger("autofill.engine")


class Status:
    CREATED = "CREATED"
    STARTED = "STARTED"
    FILLED = "FILLED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
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
    NO_FORM_FOUND = "NO_FORM_FOUND"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


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
    headless: bool = True
    page: Any = None
    job: JobContext | None = None
    resolver: ResolverBinding | None = None
    answers_updated: bool = False
    timings: dict[str, int] | None = None
    intent_hook: Any = None


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
        }


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
    return ApplicationResult(
        status=status,
        ats=ats,
        current_step=session.current_step,
        login_status=login_status,
        steps=list(session.steps),
        session_id=session.session_id,
        candidate_id=session.candidate_id,
        job_id=session.job_id,
        **kwargs,
    )


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
    if opts.max_pages < 1:
        raise ValueError("max_pages must be at least 1.")
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
        return _result(
            session,
            status=Status.MANUAL_REVIEW_REQUIRED,
            ats=ats_name,
            login_status="NOT_REQUIRED",
            messages=["This host is left for a person. Auto-Fill does not bypass its challenge."],
        )
    if is_blocked_sso(application_url):
        session.mark("FAILED")
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
    try:
        if page is None:
            from playwright.sync_api import sync_playwright

            playwright = sync_playwright().start()
            browser = playwright.chromium.launch(headless=opts.headless)
            page = browser.new_page()
            page.goto(application_url)

        snapshot = _timed_extract(page, opts, run_started, inspect_state)
        if snapshot.captcha_present:
            session.mark("FAILED")
            return _result(
                session,
                status=Status.CAPTCHA_REQUIRED,
                ats=ats_name,
                login_status="NOT_REQUIRED",
                messages=["CAPTCHA or challenge widget detected. Auto-Fill does not solve CAPTCHAs."],
            )
        snapshot = _follow_entry(page, snapshot, session, opts, run_started, inspect_state)
        if snapshot.captcha_present:
            session.mark("FAILED")
            return _result(
                session,
                status=Status.CAPTCHA_REQUIRED,
                ats=ats_name,
                login_status="NOT_REQUIRED",
                messages=["CAPTCHA or challenge widget detected. Auto-Fill does not solve CAPTCHAs."],
            )
        if not visible_fields(snapshot) and not is_review(snapshot):
            if is_login_wall(snapshot):
                session.mark("LOGIN_FAILED")
                return _result(
                    session,
                    status=Status.LOGIN_REQUIRED,
                    ats=ats_name,
                    login_status="LOGIN_REQUIRED",
                    messages=["Sign-in is required before the application form. Nothing was typed."],
                )
            session.mark(Status.NO_FORM_FOUND)
            return _result(
                session,
                status=Status.NO_FORM_FOUND,
                ats=ats_name,
                login_status="NOT_REQUIRED",
                messages=["No application form was found. A person can open the page and continue."],
            )

        session.mark("CHECK_AUTH")
        detector = LoginDetector()
        mode = detector.mode(snapshot, application_url)
        login_status = "NOT_REQUIRED"
        if mode == "account_creation":
            session.mark("FAILED")
            return _result(
                session,
                status=Status.MANUAL_REVIEW_REQUIRED,
                ats=ats_name,
                login_status="NOT_REQUIRED",
                messages=["Password field is mixed with the application. Auto-Fill will not create an account."],
            )
        if mode in {"password", "email_first"}:
            context = SiteContext(
                url=application_url,
                domain=domain,
                ats=ats_name,
                candidate_id=session.candidate_id,
                session_id=session.session_id,
            )
            creds = credential_provider.get_credentials(context, session.candidate_id) if credential_provider else None
            if creds is None or not creds.password or not (creds.email or creds.username):
                session.mark("LOGIN_FAILED")
                return _result(
                    session,
                    status=Status.LOGIN_REQUIRED,
                    ats=ats_name,
                    login_status="LOGIN_REQUIRED",
                    messages=["No credentials for this candidate and site. Nothing was typed."],
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
        for index in range(opts.max_pages):
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
                    messages=["CAPTCHA or challenge widget detected. Auto-Fill does not solve CAPTCHAs."],
                    submit_controls=submit_controls,
                )
            snapshot = _follow_entry(page, snapshot, session, opts, run_started, inspect_state)
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
                    messages=["CAPTCHA or challenge widget detected. Auto-Fill does not solve CAPTCHAs."],
                    submit_controls=submit_controls,
                )
            if not visible_fields(snapshot) and not is_review(snapshot):
                detected, filled, skipped = _counts(fields)
                if is_login_wall(snapshot):
                    session.mark("LOGIN_FAILED")
                    return _result(
                        session,
                        status=Status.LOGIN_REQUIRED,
                        ats=ats_name,
                        login_status="LOGIN_REQUIRED",
                        fields_detected=detected,
                        fields_filled=filled,
                        fields_skipped=skipped,
                        manual_actions=manual,
                        messages=["Sign-in is required before the application form. Nothing was typed."],
                        submit_controls=submit_controls,
                    )
                session.mark(Status.NO_FORM_FOUND)
                return _result(
                    session,
                    status=Status.NO_FORM_FOUND,
                    ats=ats_name,
                    login_status=login_status,
                    fields_detected=detected,
                    fields_filled=filled,
                    fields_skipped=skipped,
                    manual_actions=manual,
                    messages=["No application form was found. A person can open the page and continue."],
                    submit_controls=submit_controls,
                )
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
                _note_fill(page_fields, opts, run_started, inspect_state)
                session.mark("REVIEW_PAGE")
                session.mark("READY_FOR_HUMAN_SUBMIT")
                detected, filled, skipped = _counts(fields)
                return _result(
                    session,
                    status=Status.READY_FOR_HUMAN_SUBMIT,
                    ats=ats_name,
                    login_status=login_status,
                    fields_detected=detected,
                    fields_filled=filled,
                    fields_skipped=skipped,
                    manual_actions=manual,
                    messages=["Review page reached. A person clicks Submit."],
                    submit_controls=submit_controls,
                )

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
            _note_fill(page_fields, opts, run_started, inspect_state)
            for button in snapshot.buttons:
                if field_class_for_button(button.name, control_type=button.control_type) == "FINAL_SUBMIT":
                    if button.name not in submit_controls:
                        submit_controls.append(button.name)

            if resume_blocked and not opts.resume_uploaded:
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
                    messages=["Upload the resume that was already downloaded, then call again with resume_uploaded."],
                    submit_controls=submit_controls,
                )
            if opts.resume_uploaded and any(item.field_class == "RESUME_FIELD" for item in page_fields):
                resume_seen = True
                session.mark("CONTINUE_AFTER_RESUME")

            nxt = next(
                (
                    button
                    for button in snapshot.buttons
                    if not is_add_row(button.name)
                    and field_class_for_button(button.name, control_type=button.control_type) == "NAVIGATION_CONTROL"
                ),
                None,
            )
            if nxt is None:
                break
            session.mark("NEXT_PAGE")
            activate(page, nxt.selector, nxt.name, control_type=nxt.control_type)

        if resume_seen:
            session.mark("CONTINUE_AFTER_RESUME")
        snapshot = _timed_extract(page, opts, run_started, inspect_state)
        if "review" in (snapshot.heading or "").casefold():
            session.mark("REVIEW_PAGE")
        detected, filled, skipped = _counts(fields)
        on_review = "review" in (snapshot.heading or "").casefold()
        if not visible_fields(snapshot) and not on_review:
            session.mark(Status.NO_FORM_FOUND)
            return _result(
                session,
                status=Status.NO_FORM_FOUND,
                ats=ats_name,
                login_status=login_status,
                fields_detected=detected,
                fields_filled=filled,
                fields_skipped=skipped,
                manual_actions=manual,
                messages=["No application form was found. A person can open the page and continue."],
                submit_controls=submit_controls,
            )
        session.mark("READY_FOR_HUMAN_SUBMIT")
        terminal = Status.READY_FOR_HUMAN_SUBMIT if submit_controls or on_review else Status.FILLED
        return _result(
            session,
            status=terminal,
            ats=ats_name,
            login_status=login_status,
            fields_detected=detected,
            fields_filled=filled,
            fields_skipped=skipped,
            manual_actions=manual,
            messages=["Filled what it could. A person clicks Submit or Apply."],
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
        if owns_browser and browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()


_RESOLVER_STATUS = {
    "unauthorized": Status.FAILED_FINAL,
    "mismatch": Status.FAILED_FINAL,
    "expired": Status.EXPIRED,
    "retryable": Status.FAILED_RETRYABLE,
    "invalid": Status.FAILED_FINAL,
        "too_large": Status.FAILED_RETRYABLE,
}

_VALIDATION = ("invalid phone", "valid date", "select one", "required")


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


def _note_fill(fields, opts: AutofillOptions, started: float, state: dict) -> None:
    if state.get("filled"):
        return
    if any(getattr(item, "action", "") in {"fill", "select", "check", "uncheck", "upload"} for item in fields):
        state["filled"] = True
        _mark_ms(opts, "firstFillMs", started)


def _timed_extract(page, opts: AutofillOptions, started: float, state: dict) -> Any:
    wait_for_render(page)
    snapshot = extract_page(page)
    if not state.get("seen"):
        state["seen"] = True
        _mark_ms(opts, "firstFormInspectedMs", started)
    return snapshot


def _follow_entry(page, snapshot, session, opts: AutofillOptions, started: float, state: dict):
    """Click at most two entry controls while the page still has no form."""
    for _ in range(2):
        if snapshot.captcha_present or visible_fields(snapshot):
            return snapshot
        button = choose_entry(snapshot)
        if button is None:
            return snapshot
        logger.info("session=%s action=entry", session.session_id)
        activate_entry(page, button.selector, button.name, control_type=button.control_type)
        snapshot = _timed_extract(page, opts, started, state)
    return snapshot


def _public_questions(items: list[dict]) -> list[dict]:
    """Intent and question text only. Blocking is an internal flag."""
    published: list[dict] = []
    for item in items:
        entry = {"intent": item.get("intent"), "text": item.get("text") or ""}
        if entry not in published:
            published.append(entry)
    return published


def _validation_hit(snapshot) -> bool:
    blob = validation_blob(snapshot)
    return any(phrase in blob for phrase in _VALIDATION)


def _remember_submit(snapshot, submit_controls: list[str]) -> None:
    for button in snapshot.buttons:
        if field_class_for_button(button.name, control_type=button.control_type) == "FINAL_SUBMIT":
            if button.name not in submit_controls:
                submit_controls.append(button.name)


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
        published = _public_questions(list(questions or []) + carried)
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

    for index in range(opts.max_pages):
        snapshot = _timed_extract(page, opts, run_started, inspect_state)
        if snapshot.captcha_present:
            session.mark("FAILED")
            return finish(
                Status.CAPTCHA_REQUIRED,
                ["CAPTCHA or challenge widget detected. Auto-Fill does not solve CAPTCHAs."],
            )
        snapshot = _follow_entry(page, snapshot, session, opts, run_started, inspect_state)
        if snapshot.captcha_present:
            session.mark("FAILED")
            return finish(
                Status.CAPTCHA_REQUIRED,
                ["CAPTCHA or challenge widget detected. Auto-Fill does not solve CAPTCHAs."],
            )
        if not visible_fields(snapshot) and not is_review(snapshot):
            if is_login_wall(snapshot):
                session.mark("LOGIN_FAILED")
                return finish(
                    Status.LOGIN_REQUIRED,
                    ["Sign-in is required before the application form. Nothing was typed."],
                )
            session.mark(Status.NO_FORM_FOUND)
            return finish(
                Status.NO_FORM_FOUND,
                ["No application form was found. A person can open the page and continue."],
            )
        visible = tuple(control.selector for control in snapshot.controls if not control.hidden)
        if visible in seen:
            break
        seen.add(visible)
        _remember_submit(snapshot, submit_controls)
        step_name = page_step(ats_name, snapshot.heading, session.application_url)
        session.mark("FILL_PAGE" if index == 0 else "FILL_NEXT_PAGE")
        on_review = step_name == "review" or "review" in snapshot.heading.casefold()
        flags = FillFlags()
        for attempt in range(3):
            outcome = fill_resolved_page(
                page,
                binding,
                session_id=session.session_id,
                ats_name=ats_name,
                step=step_name,
                flags=flags,
                hook=opts.intent_hook,
            )
            fields.extend(outcome.fields)
            manual.extend(outcome.manual_actions)
            _note_fill(outcome.fields, opts, run_started, inspect_state)
            if outcome.snapshot is not None:
                snapshot = outcome.snapshot
                _remember_submit(snapshot, submit_controls)
            blocking = [item for item in outcome.manual_questions if item.get("blocking", True)]
            optional = [item for item in outcome.manual_questions if not item.get("blocking", True)]
            if outcome.resume_blocked and not opts.resume_uploaded:
                if blocking:
                    session.mark(Status.MANUAL_ANSWER_REQUIRED)
                    return finish(
                        Status.MANUAL_ANSWER_REQUIRED,
                        ["A saved answer is required. Nothing below HIGH confidence was written."],
                        questions=blocking,
                    )
                carried.extend(optional)
                session.mark(Status.RESUME_UPLOAD_REQUIRED)
                return finish(
                    Status.RESUME_UPLOAD_REQUIRED,
                    ["Upload the resume that was already downloaded, then continue with resumeUploaded."],
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
                session.mark("REVIEW_PAGE")
                session.mark(Status.READY_FOR_HUMAN_SUBMIT)
                return finish(Status.READY_FOR_HUMAN_SUBMIT, ["Review page reached. A person clicks Submit."])
            if blocking or (outcome.manual_questions and not opts.answers_updated):
                session.mark(Status.MANUAL_ANSWER_REQUIRED)
                pending = blocking if opts.answers_updated else outcome.manual_questions
                return finish(
                    Status.MANUAL_ANSWER_REQUIRED,
                    ["A saved answer is required. Nothing below HIGH confidence was written."],
                    questions=pending,
                )
            carried.extend(optional)
            nxt = next(
                (
                    button
                    for button in snapshot.buttons
                    if is_forward_navigation(button.name, control_type=button.control_type)
                ),
                None,
            )
            if nxt is None:
                break
            session.mark("NEXT_PAGE")
            activate(page, nxt.selector, nxt.name, control_type=nxt.control_type)
            snapshot = _timed_extract(page, opts, run_started, inspect_state)
            if snapshot.captcha_present:
                session.mark("FAILED")
                return finish(
                    Status.CAPTCHA_REQUIRED,
                    ["CAPTCHA or challenge widget detected. Auto-Fill does not solve CAPTCHAs."],
                )
            if not _validation_hit(snapshot):
                break
            flags = flags_for_alerts(validation_blob(snapshot), flags)
            if attempt == 2:
                session.mark(Status.FAILED_RETRYABLE)
                return finish(
                    Status.FAILED_RETRYABLE,
                    ["Validation could not be corrected within the attempt limit."],
                )

    snapshot = _timed_extract(page, opts, run_started, inspect_state)
    _remember_submit(snapshot, submit_controls)
    if "review" in (snapshot.heading or "").casefold():
        session.mark("REVIEW_PAGE")
    on_review = "review" in (snapshot.heading or "").casefold()
    if not visible_fields(snapshot) and not on_review:
        session.mark(Status.NO_FORM_FOUND)
        return finish(
            Status.NO_FORM_FOUND,
            ["No application form was found. A person can open the page and continue."],
        )
    session.mark(Status.READY_FOR_HUMAN_SUBMIT)
    terminal = Status.READY_FOR_HUMAN_SUBMIT if submit_controls or on_review else Status.FILLED
    return finish(terminal, ["Filled what it could. A person clicks Submit or Apply."])


def autofill_from_fill_result(result: FillResult) -> str:
    """Map the lower-level fill status onto the public status vocabulary."""
    if result.captcha_present and result.pages_filled == 0:
        return Status.CAPTCHA_REQUIRED
    if result.resume_required:
        return Status.RESUME_UPLOAD_REQUIRED
    if result.login_wall and result.pages_filled == 0:
        return Status.LOGIN_REQUIRED
    return Status.FILLED
