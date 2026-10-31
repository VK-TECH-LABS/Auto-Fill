"""After one answered field, find the next empty required control.

A full fill is reserved for a new page or step. On the same step, committed
fields are left alone. A control that was not visible before is filled on its
own, which covers a conditional question that just appeared.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from autofill.adapters import page_step
from autofill.ats import detect_ats
from autofill.dom_ui import (
    STAGE_TITLES,
    blocker_stage,
    control_committed,
    present_blocking,
    progress_percent,
    public_blocking,
    safe_label,
    stage_for,
    workflow_percent,
)
from autofill.engine import REVEAL_ATTEMPTS, Status
from autofill.extract import extract_page
from autofill.fill import apply_mapped
from autofill.mapping import (
    MapCursor,
    acknowledgement_kind,
    attestation_manual,
    is_consent_choice,
    is_demographic_control,
    is_honeypot,
    map_field,
)
from autofill.models import Control, JobContext, PageSnapshot
from autofill.pagewait import classify_page, current_page_type, site_error_messages, terminal_kind
from autofill.profile import CandidateProfile
from autofill.stepfill import inspect_required

_WRITE = frozenset({"fill", "select", "check", "uncheck"})


@dataclass(frozen=True)
class PageMark:
    """What was on the page the last time the filler looked."""

    url: str
    step: str
    selectors: frozenset[str]


@dataclass
class FollowUp:
    """The next blocker, or a request to fill the page again."""

    changed: bool
    status: str
    open_items: list[dict] = field(default_factory=list)
    progress: dict = field(default_factory=dict)
    blocking: dict | None = None
    manual_questions: list[dict] = field(default_factory=list)
    messages: list[str] = field(default_factory=list)
    mark: PageMark | None = None
    typed: list[str] = field(default_factory=list)
    account: dict | None = None


def read_page_mark(page) -> tuple[PageMark, PageSnapshot]:
    """URL, step, and the selectors that are visible. This does not type."""
    snapshot = extract_page(page)
    url = _page_url(page)
    info = detect_ats(url)
    step = page_step(info.name if info else None, snapshot.heading, url)
    selectors = frozenset(control.selector for control in snapshot.controls if control.selector and not control.hidden)
    return PageMark(url, step, selectors), snapshot


def follow_answer(
    page,
    *,
    profile: CandidateProfile,
    job: JobContext | None,
    held: set[str],
    prior: list[dict],
    previous: PageMark | None,
    cover_letter_text: str | None = None,
    cover_letter_path: str | None = None,
    previous_stage: str = "",
) -> FollowUp:
    """Scan this page, then at most a few reveals, and return one gap set."""
    outcome = _follow_once(
        page,
        profile=profile,
        job=job,
        held=held,
        prior=prior,
        previous=previous,
        cover_letter_text=cover_letter_text,
        cover_letter_path=cover_letter_path,
        previous_stage=previous_stage,
    )
    attempt = 1
    while (
        attempt < REVEAL_ATTEMPTS
        and outcome.typed
        and not outcome.changed
        and outcome.mark is not None
    ):
        outcome = _follow_once(
            page,
            profile=profile,
            job=job,
            held=held,
            prior=prior,
            previous=outcome.mark,
            cover_letter_text=cover_letter_text,
            cover_letter_path=cover_letter_path,
            previous_stage=previous_stage,
        )
        attempt += 1
    return outcome


def _follow_once(
    page,
    *,
    profile: CandidateProfile,
    job: JobContext | None,
    held: set[str],
    prior: list[dict],
    previous: PageMark | None,
    cover_letter_text: str | None = None,
    cover_letter_path: str | None = None,
    previous_stage: str = "",
) -> FollowUp:
    """Outline the next empty required field. Type only controls that just appeared."""
    mark, snapshot = read_page_mark(page)
    if snapshot.captcha_present:
        present_blocking(page, None)
        return FollowUp(
            changed=False,
            status=Status.CAPTCHA_REQUIRED,
            messages=["A visible challenge is on the page."],
            mark=mark,
            progress=_progress(
                snapshot,
                Status.CAPTCHA_REQUIRED,
                0,
                0,
                step=mark.step,
                previous_stage=previous_stage,
            ),
        )
    if _page_changed(previous, mark):
        return FollowUp(changed=True, status="", mark=mark)
    typed: list[str] = []
    fresh = mark.selectors - previous.selectors if previous is not None else frozenset()
    if fresh:
        typed = _fill_new(
            page,
            snapshot,
            fresh,
            profile,
            job,
            held,
            cover_letter_text=cover_letter_text,
            cover_letter_path=cover_letter_path,
        )
        mark, snapshot = read_page_mark(page)
    classify_page(snapshot, url=_page_url(page))
    gaps, total, done = inspect_required(page, snapshot, profile, prior, option_reads=1)
    if not gaps:
        # Re-read every required control before READY. The answer that just
        # landed can change another field, and that field has to be judged too.
        from autofill.extract import extract_page

        gaps, total, done = inspect_required(page, extract_page(page), profile, prior)
    account: dict | None = None
    if gaps:
        status = Status.MANUAL_ANSWER_REQUIRED
        present_blocking(page, gaps[0])
        messages = ["A required field is still empty."]
    else:
        decision = terminal_kind(
            snapshot,
            url=_page_url(page),
            gaps=gaps,
            required_total=total,
        )
        if decision == "READY":
            status = Status.READY_FOR_HUMAN_SUBMIT
            present_blocking(page, None)
            messages = ["Every required field on this page is filled. A person clicks Submit."]
        elif decision in {"ACCOUNT", "LOGIN"}:
            from autofill.login import stop_account

            info = detect_ats(_page_url(page))
            wanted = Status.ACCOUNT_CREATION_REQUIRED if decision == "ACCOUNT" else Status.LOGIN_REQUIRED
            # A Create Account form is never a Sign In stop, even with a Sign In link beside it.
            status, account = stop_account(snapshot, _page_url(page), info.name if info else None, wanted)
            present_blocking(page, None)
            if status == Status.ACCOUNT_CREATION_REQUIRED:
                messages = ["Create an employer account. Nothing was typed."]
            else:
                messages = ["Sign-in is required before the application form. Nothing was typed."]
        elif decision == "JOB":
            status = Status.JOB_DESCRIPTION
            present_blocking(page, None)
            reason = "job_description" if current_page_type() == "JOB_DESCRIPTION" else "apply_entry"
            messages = [
                "This page is a job description. Auto-Fill did not find an application form.",
                reason,
            ]
        elif decision == "JOB_CLOSED":
            status = Status.FAILED_FINAL
            present_blocking(page, None)
            messages = ["job_expired"]
        elif decision == "SITE_ERROR":
            status = Status.FAILED_RETRYABLE
            present_blocking(page, None)
            messages = site_error_messages(snapshot)
        elif decision == "CAPTCHA":
            status = Status.CAPTCHA_REQUIRED
            present_blocking(page, None)
            messages = ["A challenge is on the page. Auto-Fill stopped."]
        elif decision == "NO_FORM":
            status = Status.NO_FORM_FOUND
            present_blocking(page, None)
            reason = {
                "JOB_DESCRIPTION": "job_description",
                "APPLY_ENTRY": "apply_entry",
                "CONFIRMATION": "confirmation",
            }.get(current_page_type(), "no_application_form")
            messages = [
                "No application form was found. A person can open the page and continue.",
                reason,
            ]
        else:
            status = Status.FILLED
            present_blocking(page, None)
            messages = ["Every required field on this page is filled. A person clicks Submit."]
    return FollowUp(
        changed=False,
        status=status,
        open_items=gaps,
        progress=_progress(
            snapshot,
            status,
            total,
            done,
            step=mark.step,
            blocker=gaps[0] if gaps else None,
            previous_stage=previous_stage,
        ),
        blocking=public_blocking(gaps[0]) if gaps else None,
        manual_questions=_public_questions(gaps),
        messages=messages,
        account=account,
        mark=mark,
        typed=typed,
    )


def _page_url(page) -> str:
    try:
        return str(getattr(page, "url", "") or "")
    except Exception:
        return ""


def _page_changed(previous: PageMark | None, mark: PageMark) -> bool:
    if previous is None:
        return False
    if previous.url != mark.url or previous.step != mark.step:
        return True
    return bool(previous.selectors) and not mark.selectors


def _progress(
    snapshot: PageSnapshot,
    status: str,
    total: int,
    done: int,
    *,
    step: str = "",
    blocker: dict | None = None,
    previous_stage: str = "",
) -> dict:
    """Same section rules as a full fill, plus the field that is blocking now.

    A scan must not put the stage back to OPENING after the form has been found.
    """
    stage = stage_for(step, snapshot.heading, status)
    if stage not in {"READY", "LOGIN", "ACCOUNT", "VERIFICATION", "CAPTCHA"}:
        section = blocker_stage(blocker)
        if section:
            stage = section
    if stage == "OPENING" and previous_stage not in {"", "OPENING"}:
        stage = previous_stage
    heading_stage = stage_for(step, snapshot.heading, "")
    label = safe_label(snapshot.heading)
    if not label or stage != heading_stage:
        label = STAGE_TITLES.get(stage, STAGE_TITLES["QUESTIONS"])
    percent = progress_percent(stage, total, done)
    if current_page_type() == "JOB_CLOSED":
        return {
            "stage": "CLOSED",
            "requiredTotal": total,
            "requiredDone": done,
            "percent": 0,
            "currentLabel": "Job closed",
        }
    if status in {
        "JOB_DESCRIPTION",
        "ACCOUNT_CREATION_REQUIRED",
        "LOGIN_REQUIRED",
        "LOGIN_FAILED",
        "EMAIL_VERIFICATION_REQUIRED",
        "CAPTCHA_REQUIRED",
        "RESUME_UPLOAD_REQUIRED",
        "NO_FORM_FOUND",
        "READY_FOR_HUMAN_SUBMIT",
    }:
        percent = workflow_percent(status, current_page_type(), total, done)
    return {
        "stage": stage,
        "requiredTotal": total,
        "requiredDone": done,
        "percent": percent,
        "currentLabel": label,
    }


def _public_questions(items: list[dict]) -> list[dict]:
    published: list[dict] = []
    for item in items:
        entry = {"intent": item.get("intent"), "text": item.get("text") or ""}
        if entry not in published:
            published.append(entry)
    return published


def _fill_new(
    page,
    snapshot: PageSnapshot,
    fresh: frozenset[str],
    profile: CandidateProfile,
    job: JobContext | None,
    held: set[str],
    *,
    cover_letter_text: str | None,
    cover_letter_path: str | None,
) -> list[str]:
    """Write profile values into controls that were not visible on the last scan."""
    typed: list[str] = []
    cursor = MapCursor(page_heading=snapshot.heading)
    for control in snapshot.controls:
        if not _should_fill(page, control, fresh, held):
            continue
        mapped = map_field(
            control,
            profile,
            cover_letter_path=cover_letter_path,
            cover_letter_text=cover_letter_text,
            job=job,
            cursor=cursor,
        )
        if _leave_for_person(control, mapped) or mapped.action not in _WRITE:
            continue
        outcome = apply_mapped(page, control, mapped)
        if outcome.action in _WRITE and outcome.detail != "already set":
            typed.append(control.selector)
    return typed


def _leave_for_person(control: Control, mapped) -> bool:
    """A reveal must not auto-answer a row a normal scan would pause on."""
    if getattr(mapped, "challenge", "") == "confirm":
        return True
    if is_demographic_control(control) or is_consent_choice(control) or attestation_manual(control):
        return True
    return acknowledgement_kind(control) in {"consent", "marketing"}


def _should_fill(page, control: Control, fresh: frozenset[str], held: set[str]) -> bool:
    selector = control.selector
    if not selector or selector not in fresh or selector in held:
        return False
    if control.hidden or control.disabled or control.read_only:
        return False
    if control.kind in {"password", "hidden", "file"} or control.input_type == "password":
        return False
    if is_honeypot(control):
        return False
    return not control_committed(page, selector)
