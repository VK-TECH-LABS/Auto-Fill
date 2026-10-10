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
)
from autofill.engine import Status
from autofill.extract import extract_page
from autofill.fill import apply_mapped
from autofill.mapping import MapCursor, is_honeypot, map_field
from autofill.models import Control, JobContext, PageSnapshot
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
    gaps, total, done = inspect_required(page, snapshot, profile, prior, option_reads=1)
    if gaps:
        status = Status.MANUAL_ANSWER_REQUIRED
        present_blocking(page, gaps[0])
        messages = ["A required field is still empty."]
    else:
        status = Status.READY_FOR_HUMAN_SUBMIT
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
    if stage not in {"READY", "LOGIN"}:
        section = blocker_stage(blocker)
        if section:
            stage = section
    if stage == "OPENING" and previous_stage not in {"", "OPENING"}:
        stage = previous_stage
    heading_stage = stage_for(step, snapshot.heading, "")
    label = safe_label(snapshot.heading)
    if not label or stage != heading_stage:
        label = STAGE_TITLES.get(stage, STAGE_TITLES["QUESTIONS"])
    return {
        "stage": stage,
        "requiredTotal": total,
        "requiredDone": done,
        "percent": progress_percent(stage, total, done),
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
        if mapped.action not in _WRITE:
            continue
        outcome = apply_mapped(page, control, mapped)
        if outcome.action in _WRITE and outcome.detail != "already set":
            typed.append(control.selector)
    return typed


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
