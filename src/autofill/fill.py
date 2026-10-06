"""Fill an application form and stop.

The final Submit / Apply control is never clicked. Multi-page applications
may advance with Next, Continue, or Review when the caller opts in.
``fill_application`` does not type passwords. Authorized login lives in
``autofill_application``. A visible CAPTCHA widget stops the run before
any field is written. Resume file inputs are reported and never selected.
"""

from __future__ import annotations

from pathlib import Path

from autofill.ats import detect_ats, is_blocked_sso, is_manual_ats
from autofill.extract import extract_page
from autofill.mapping import MapCursor, map_field
from autofill.models import Control, FieldOutcome, FillResult, JobContext, MappedField, PageSnapshot
from autofill.profile import CandidateProfile
from autofill.safeguards import activate, choose_option, classify_control, is_add_row

_HUMAN_MESSAGE = (
    "Filled what it could and stopped. A person must review the form and click Submit or Apply."
)
_BLANK_URLS = {"", "about:blank"}


def _path_text(path: str | Path | None) -> str | None:
    if path is None or path == "":
        return None
    return str(path)


def _outcome(
    control: Control,
    mapped: MappedField,
    *,
    action: str | None = None,
    detail: str | None = None,
) -> FieldOutcome:
    return FieldOutcome(
        selector=control.selector,
        label=control.label,
        key=mapped.key,
        action=action or mapped.action,
        detail=detail if detail is not None else mapped.reason,
        field_class=mapped.field_class,
    )


_SET_NATIVE = """
(el, value) => {
  const proto = el instanceof HTMLTextAreaElement
    ? window.HTMLTextAreaElement.prototype
    : window.HTMLInputElement.prototype;
  const desc = Object.getOwnPropertyDescriptor(proto, "value");
  if (desc && desc.set) desc.set.call(el, value);
  else el.value = value;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  el.dispatchEvent(new Event("blur", { bubbles: true }));
}
"""


def _apply_mapped(page, control: Control, mapped: MappedField) -> FieldOutcome:
    if mapped.action in {"skip", "unanswered", "resume_required"}:
        return _outcome(control, mapped)
    try:
        locator = page.locator(control.selector)
        if mapped.action == "fill":
            current = locator.input_value()
            if current.strip() and current.strip() != mapped.text:
                return _outcome(
                    control,
                    mapped,
                    action="skip",
                    detail="Left an existing value in place.",
                )
            if current.strip() == mapped.text:
                return _outcome(control, mapped, detail="already set")
            if control.kind in {"text", "textarea", "combobox"}:
                locator.evaluate(_SET_NATIVE, mapped.text)
            else:
                locator.fill(mapped.text)
            return _outcome(control, mapped, detail="filled")
        if mapped.action == "select":
            if control.kind == "radio":
                page.locator(mapped.option_selector).check()
            elif control.kind == "combobox" and mapped.option_selector:
                choose_option(page, control.selector, mapped.option_selector, mapped.option_label)
            elif mapped.option_value:
                locator.select_option(value=mapped.option_value)
            else:
                locator.select_option(label=mapped.option_label)
            return _outcome(control, mapped, detail=mapped.option_label or "selected")
        if mapped.action == "check":
            locator.check()
            return _outcome(control, mapped, detail="checked")
        if mapped.action == "uncheck":
            locator.uncheck()
            return _outcome(control, mapped, detail="unchecked")
        if mapped.action == "upload":
            locator.set_input_files(mapped.text)
            return _outcome(control, mapped, detail=Path(mapped.text).name)
    except Exception as exc:
        return _outcome(control, mapped, action="error", detail=str(exc))
    return _outcome(control, mapped, action="unanswered", detail=f"Unsupported action {mapped.action}.")


def _note_manual(control: Control, mapped: MappedField, manual: list[str]) -> None:
    if mapped.field_class in {"MANUAL_REVIEW_FIELD", "LEGAL_FIELD"} or mapped.action == "resume_required":
        label = control.label or control.selector or control.name
        manual.append(f"{label}: {mapped.reason}")


def fill_one_page(
    page,
    profile: CandidateProfile,
    *,
    cover_letter_path: str | None,
    cover_letter_text: str | None,
    job: JobContext | None,
    resume_uploaded: bool,
) -> tuple[list[FieldOutcome], PageSnapshot, bool, list[str]]:
    """Fill the current page. Does not click Next or Submit.

    Repeated employment rows are added only via an Add control, then filled.
    A resume file is never chosen by this function.
    """
    fields: list[FieldOutcome] = []
    manual: list[str] = []
    resume_blocked = False
    snapshot = extract_page(page)
    for _ in range(4):
        fields = []
        manual = []
        resume_blocked = False
        snapshot = extract_page(page)
        cursor = MapCursor(page_heading=snapshot.heading)
        for control in snapshot.controls:
            mapped = map_field(
                control,
                profile,
                cover_letter_path=cover_letter_path,
                cover_letter_text=cover_letter_text,
                job=job,
                cursor=cursor,
            )
            if mapped.action == "resume_required" and resume_uploaded:
                fields.append(
                    _outcome(
                        control,
                        mapped,
                        action="skip",
                        detail="Resume left for the person who uploaded it.",
                    )
                )
                continue
            if mapped.action == "resume_required":
                resume_blocked = True
            _note_manual(control, mapped, manual)
            fields.append(_apply_mapped(page, control, mapped))
        if resume_blocked:
            break
        employer_slots = [
            control
            for control in snapshot.controls
            if not control.hidden and control.kind in {"text", "combobox"} and "employ" in control.label.casefold()
        ]
        add = next((button for button in snapshot.buttons if is_add_row(button.name)), None)
        if add and len(profile.employment) > len(employer_slots):
            activate(page, add.selector, add.name, control_type=add.control_type)
            continue
        break
    return fields, snapshot, resume_blocked, manual


def _remember_blocked(snapshot: PageSnapshot, submit_controls: list[str]) -> None:
    for button in snapshot.buttons:
        kind = classify_control(button.name, control_type=button.control_type)
        if kind == "submit" and button.name not in submit_controls:
            submit_controls.append(button.name)


def _fill_pages(
    page,
    profile: CandidateProfile,
    *,
    resume_path: str | None,
    cover_letter_path: str | None,
    cover_letter_text: str | None,
    job: JobContext | None,
    advance_pages: bool,
    max_pages: int,
    ats_name: str | None,
    resume_uploaded: bool,
) -> FillResult:
    del resume_path  # Resume files are a human checkpoint, never auto-selected.
    fields: list[FieldOutcome] = []
    submit_controls: list[str] = []
    continued: list[str] = []
    manual: list[str] = []
    messages = [_HUMAN_MESSAGE]
    seen_pages: set[tuple[str, ...]] = set()
    captcha_present = False
    login_wall = False
    resume_blocked = False
    pages_filled = 0
    clicked: set[str] = set()

    info = detect_ats(page.url)
    if info and info.notes:
        messages.append(info.notes)
    if info and info.resume_first:
        messages.append(f"{info.name} reaches a resume step a person completes. Auto-Fill does not choose the file.")

    for _ in range(max_pages):
        snapshot = extract_page(page)
        captcha_present = captcha_present or snapshot.captcha_present
        _remember_blocked(snapshot, submit_controls)
        visible_key = tuple(control.selector for control in snapshot.controls if not control.hidden)
        if visible_key in seen_pages:
            break
        seen_pages.add(visible_key)

        if snapshot.captcha_present:
            messages.append("CAPTCHA or challenge widget detected. Auto-Fill does not solve CAPTCHAs.")
            break

        if snapshot.password_present:
            login_wall = True
            messages.append(
                "Password field detected. fill_application does not submit credentials; use autofill_application."
            )
            break

        pages_filled += 1
        page_fields, snapshot, page_resume, page_manual = fill_one_page(
            page,
            profile,
            cover_letter_path=cover_letter_path,
            cover_letter_text=cover_letter_text,
            job=job,
            resume_uploaded=resume_uploaded,
        )
        fields.extend(page_fields)
        manual.extend(page_manual)
        _remember_blocked(snapshot, submit_controls)
        if page_resume:
            resume_blocked = True
            messages.append("Stopped at resume upload. A person must upload the resume.")
            break
        if "review" in snapshot.heading.casefold():
            messages.append("Stopped on the review page. A person must click Submit.")
            break
        if not advance_pages:
            break

        nxt = next(
            (
                button
                for button in snapshot.buttons
                if button.selector not in clicked
                and not is_add_row(button.name)
                and classify_control(button.name, control_type=button.control_type) == "continue"
            ),
            None,
        )
        if nxt is None:
            break
        activate(page, nxt.selector, nxt.name, control_type=nxt.control_type)
        clicked.add(nxt.selector)
        continued.append(nxt.name)

    unanswered = sum(1 for item in fields if item.action == "unanswered")
    if unanswered:
        messages.append(f"{unanswered} control(s) left unanswered for a person to review.")
    if submit_controls:
        messages.append(
            "Detected and did not click: " + ", ".join(submit_controls) + "."
        )

    status = "filled"
    if captcha_present and pages_filled == 0:
        status = "captcha_required"
    elif login_wall and pages_filled == 0:
        status = "skipped_login"
    elif resume_blocked:
        status = "resume_required"
    return FillResult(
        status=status,
        stopped_before_submit=True,
        ats=ats_name or (info.name if info else None),
        captcha_present=captcha_present,
        login_wall=login_wall,
        pages_filled=pages_filled,
        fields=fields,
        submit_controls=submit_controls,
        continued_controls=continued,
        messages=messages,
        resume_required=resume_blocked,
        manual_actions=manual,
    )


def _skipped(status: str, message: str, *, ats: str | None = None) -> FillResult:
    return FillResult(
        status=status,
        stopped_before_submit=True,
        ats=ats,
        messages=[_HUMAN_MESSAGE, message],
    )


def fill_application(
    profile: CandidateProfile,
    *,
    url: str | None = None,
    page=None,
    resume_path: str | Path | None = None,
    cover_letter_path: str | Path | None = None,
    cover_letter_text: str | None = None,
    job: JobContext | None = None,
    advance_pages: bool = False,
    max_pages: int = 5,
    headless: bool = True,
    resume_uploaded: bool = False,
) -> FillResult:
    """Fill ``page`` or ``url`` from ``profile`` and stop before submit.

    Pass a Playwright page TileArc (or another host app) already has open, or
    a URL and this function will open Chromium. Either way it returns after
    filling. It will not click Submit, Apply, Log in, or Sign up.

    ``advance_pages`` clicks Next / Continue / Review so later pages of a
    Workday, Taleo, or iCIMS-style form can be filled. That flag does not
    enable submission.

    Args:
        profile: Candidate answers. Use fictional data in tests and examples.
        url: Application form URL. Ignored for navigation when ``page`` is
            already on a real document.
        page: Existing Playwright page. It is left open for a person to review.
        resume_path: Ignored. Resume upload is a human checkpoint.
        cover_letter_path: File to place in a cover-letter upload control when explicitly passed.
        cover_letter_text: Paste this text only when the caller supplies it.
        job: Optional posted salary range. Auto-Fill does not fetch the job.
        advance_pages: Follow Next / Continue / Review, never Submit / Apply.
        max_pages: Cap on how many pages to fill when advancing.
        headless: Used only when this function launches the browser.

    Returns:
        A :class:`FillResult` with ``stopped_before_submit`` set.
    """
    if page is None and not url:
        raise ValueError("Provide a url or an open Playwright page.")
    if max_pages < 1:
        raise ValueError("max_pages must be at least 1.")

    target = url or (getattr(page, "url", "") if page is not None else "")
    if target and is_manual_ats(target):
        return _skipped(
            "skipped_manual_ats",
            "This host is left for a person to complete (manual ATS, often a CAPTCHA wall).",
        )
    if target and is_blocked_sso(target):
        return _skipped(
            "skipped_sso",
            "This is an identity-provider login. Auto-Fill does not sign in through SSO.",
        )

    ats = detect_ats(target)
    resume = _path_text(resume_path)
    cover = _path_text(cover_letter_path)

    if page is not None:
        current = getattr(page, "url", "") or ""
        if url and current in _BLANK_URLS:
            page.goto(url)
        return _fill_pages(
            page,
            profile,
            resume_path=resume,
            cover_letter_path=cover,
            cover_letter_text=cover_letter_text,
            job=job,
            advance_pages=advance_pages,
            max_pages=max_pages,
            ats_name=ats.name if ats else None,
            resume_uploaded=resume_uploaded,
        )

    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    browser = playwright.chromium.launch(headless=headless)
    try:
        if not url:
            raise ValueError("Provide a url or an open Playwright page.")
        opened = browser.new_page()
        opened.goto(url)
        return _fill_pages(
            opened,
            profile,
            resume_path=resume,
            cover_letter_path=cover,
            cover_letter_text=cover_letter_text,
            job=job,
            advance_pages=advance_pages,
            max_pages=max_pages,
            ats_name=ats.name if ats else None,
            resume_uploaded=resume_uploaded,
        )
    finally:
        browser.close()
        playwright.stop()
