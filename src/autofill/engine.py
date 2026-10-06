"""Application state machine.

OPEN_URL → DETECT_SITE → DETECT_ATS → CHECK_AUTH → login states →
APPLICATION_READY → FILL_PAGE → NEXT_PAGE → FILL_NEXT_PAGE →
RESUME_UPLOAD_REQUIRED → CONTINUE_AFTER_RESUME → REVIEW_PAGE →
READY_FOR_HUMAN_SUBMIT, or a failure status.

The final Submit control is never clicked. CAPTCHA widgets stop the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from autofill.adapters import adapter_for, page_step
from autofill.ats import detect_ats, is_blocked_sso, is_manual_ats, site_domain
from autofill.credentials import CredentialProvider, SiteContext
from autofill.extract import extract_page
from autofill.fill import fill_one_page
from autofill.login import LoginDetector, run_login
from autofill.models import FillResult
from autofill.profile import CandidateProfile
from autofill.safeguards import HUMAN_SUBMIT_ONLY, activate, field_class_for_button, is_add_row
from autofill.session import SessionStore

logger = logging.getLogger("autofill.engine")


class Status:
    FILLED = "FILLED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    LOGIN_FAILED = "LOGIN_FAILED"
    APPLICATION_READY = "APPLICATION_READY"
    RESUME_UPLOAD_REQUIRED = "RESUME_UPLOAD_REQUIRED"
    MANUAL_REVIEW_REQUIRED = "MANUAL_REVIEW_REQUIRED"
    READY_FOR_HUMAN_SUBMIT = "READY_FOR_HUMAN_SUBMIT"
    UNSUPPORTED = "UNSUPPORTED"
    FAILED = "FAILED"
    CAPTCHA_REQUIRED = "CAPTCHA_REQUIRED"


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
    store = SessionStore()
    session = store.open(
        profile=candidate_profile,
        application_url=application_url,
        job_id=opts.job_id,
        candidate_id=opts.candidate_id,
        session_id=opts.session_id,
    )
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

        snapshot = extract_page(page)
        if snapshot.captcha_present:
            session.mark("FAILED")
            return _result(
                session,
                status=Status.CAPTCHA_REQUIRED,
                ats=ats_name,
                login_status="NOT_REQUIRED",
                messages=["CAPTCHA or challenge widget detected. Auto-Fill does not solve CAPTCHAs."],
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

        fields: list = []
        manual: list[str] = []
        submit_controls: list[str] = []
        seen: set[tuple[str, ...]] = set()
        resume_seen = False
        for index in range(opts.max_pages):
            snapshot = extract_page(page)
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
                    job=None,
                    resume_uploaded=opts.resume_uploaded,
                )
                fields.extend(page_fields)
                manual.extend(page_manual)
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
                job=None,
                resume_uploaded=opts.resume_uploaded,
            )
            fields.extend(page_fields)
            manual.extend(page_manual)
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
        snapshot = extract_page(page)
        if "review" in (snapshot.heading or "").casefold():
            session.mark("REVIEW_PAGE")
        session.mark("READY_FOR_HUMAN_SUBMIT")
        detected, filled, skipped = _counts(fields)
        on_review = "review" in (snapshot.heading or "").casefold()
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
    except Exception as exc:
        session.mark("FAILED")
        logger.info("session=%s status=FAILED error_type=%s", session.session_id, type(exc).__name__)
        return _result(
            session,
            status=Status.FAILED,
            ats=ats_name,
            login_status="NOT_REQUIRED",
            messages=[f"Stopped because of {type(exc).__name__}."],
        )
    finally:
        if owns_browser and browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()


def autofill_from_fill_result(result: FillResult) -> str:
    """Map the lower-level fill status onto the public status vocabulary."""
    if result.captcha_present and result.pages_filled == 0:
        return Status.CAPTCHA_REQUIRED
    if result.resume_required:
        return Status.RESUME_UPLOAD_REQUIRED
    if result.login_wall and result.pages_filled == 0:
        return Status.LOGIN_REQUIRED
    return Status.FILLED
