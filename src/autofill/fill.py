"""Fill an application form and stop.

The final Submit / Apply control is never clicked. Multi-page applications
may advance with Next, Continue, or Review when the caller opts in.
``fill_application`` does not type passwords. Authorized login lives in
``autofill_application``. A visible CAPTCHA widget stops the run before
any field is written. Resume file inputs are reported and never selected.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from pathlib import Path

from autofill.ats import detect_ats, is_blocked_sso, is_manual_ats
from autofill.dom_ui import (
    checkpoint,
    choose_native_select,
    control_committed,
    focus_field,
    refill_cleared,
    refresh_preview,
    selector_held,
    type_visible,
)
from autofill.extract import challenge_message, extract_page
from autofill.mapping import MapCursor, map_field
from autofill.models import Control, FieldOutcome, FillResult, JobContext, MappedField, Option, PageSnapshot
from autofill.pagewait import navigation_signature, validation_message, wait_for_navigation
from autofill.profile import CandidateProfile
from autofill.safeguards import HumanSubmissionRequired, _bound_target, activate, classify_control, is_add_row

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


_CONFIRMED: ContextVar = ContextVar("autofill_confirmed", default=None)
_CONFIRMED_STORE: ContextVar = ContextVar("autofill_confirmed_store", default=None)
_SESSION_ID: ContextVar[str] = ContextVar("autofill_session_id", default="")


def bind_confirmed(hook, store: list | None = None) -> tuple[Token, Token]:
    """Remember values this run writes. ``store`` is the session's record."""
    return _CONFIRMED.set(hook), _CONFIRMED_STORE.set(store)


def reset_confirmed(token: tuple[Token, Token]) -> None:
    hook_token, store_token = token
    _CONFIRMED.reset(hook_token)
    _CONFIRMED_STORE.reset(store_token)


def confirmed_records() -> list[dict]:
    store = _CONFIRMED_STORE.get()
    if not store:
        return []
    return [dict(item) for item in store if isinstance(item, dict)]


def bind_session_id(session_id: str) -> Token:
    return _SESSION_ID.set(session_id or "")


def reset_session_id(token: Token) -> None:
    _SESSION_ID.reset(token)


def current_session_id() -> str:
    return _SESSION_ID.get() or ""


def remember_confirmed(control: Control, value: str, *, field_key: str = "", choice: str = "") -> None:
    """Keep a value the engine wrote and then read back on the control.

    The confirmed-value store puts the same control back after a rewrite.
    ``choice`` marks a checkbox or radio for that restore. A separate catalogue
    on the profile reuses a question answer on a later page. Profile fields
    and consent are not copied into that catalogue.
    """
    text = str(value or "").strip()
    if not text:
        return
    hook = _CONFIRMED.get()
    if hook is not None:
        record = {
            "value": text,
            "label": control.label or "",
            "name": control.name or "",
            "elementId": control.element_id or "",
            "automationId": control.automation_id or "",
            "fieldKey": field_key or "",
            "selector": control.selector or "",
        }
        if choice:
            record["choice"] = choice
        hook(record)
    from autofill.answers import current_catalogue, note_session_answer
    from autofill.mapping import question_stem, wrote_us_country

    if wrote_us_country(field_key, text):
        from autofill.mapping import mark_us_committed

        mark_us_committed()
    bound = current_catalogue()
    if bound is None:
        return
    profile, employers = bound
    note_session_answer(
        profile,
        stem=question_stem(control),
        value=text,
        field_key=field_key,
        employers=employers,
        control=control,
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


def attach_resume(page, control: Control, *, name: str, mime_type: str, data: bytes) -> FieldOutcome:
    """Put resume bytes on the file input. The browser keeps the buffer after this returns."""
    mapped = MappedField(
        key="resume.file",
        action="upload",
        text=name,
        field_class="RESUME_FIELD",
        confidence="high",
    )
    try:
        page.locator(control.selector).set_input_files(
            [{"name": name, "mimeType": mime_type or "application/octet-stream", "buffer": data}]
        )
    except Exception as exc:
        return _outcome(control, mapped, action="error", detail=str(exc))
    return _outcome(control, mapped, detail=name)


def apply_mapped(page, control: Control, mapped: MappedField, *, overwrite: bool = False) -> FieldOutcome:
    """Write one mapped value. Clicks still go through the submit guard.

    ``overwrite`` replaces a value this step already wrote, which a validation
    correction needs. The default still leaves a pre-filled value alone.
    """
    return _apply_mapped(page, control, mapped, overwrite=overwrite)


def _value_matches(current: str, expected: str) -> bool:
    """The page already shows this exact text. A reformatted phone is not exact."""
    got = current.strip()
    want = expected.strip()
    return bool(got) and bool(want) and (got == want or got.casefold() == want.casefold())


def _value_verified(current: str, expected: str) -> bool:
    """True when the page shows the value that was written.

    A site may regroup a phone number. That still counts after the write.
    It does not count as the value we were about to type.
    """
    if _value_matches(current, expected):
        return True
    got = current.strip()
    want = expected.strip()
    if not got or not want:
        return False
    want_digits = "".join(character for character in want if character.isdigit())
    got_digits = "".join(character for character in got if character.isdigit())
    return len(want_digits) >= 7 and want_digits == got_digits


def _shown_value(locator) -> str:
    try:
        return str(locator.input_value() or "")
    except Exception:
        return ""


def _verified_or_blank(control: Control, mapped: MappedField, locator, expected: str) -> FieldOutcome:
    shown = _shown_value(locator)
    if not _value_verified(shown, expected):
        return _outcome(control, mapped, action="unanswered", detail="Value did not stay on the page.")
    remember_confirmed(control, shown.strip(), field_key=mapped.key or "")
    refresh_preview(getattr(locator, "page", None))
    return _outcome(control, mapped, detail="filled")


_CHOICE_STATE_JS = r"""(el) => {
  function selected(node) {
    if (!node || !node.getAttribute) return false;
    for (const name of ["aria-pressed", "aria-selected", "aria-checked"]) {
      if (node.getAttribute(name) === "true") return true;
    }
    if (node.checked === true) return true;
    const cls = String(node.className || "").toLowerCase();
    return /(^|\s)(selected|is-selected|active|is-active|checked)(\s|$)/.test(cls);
  }
  return selected(el) || selected(el.querySelector("input[type='radio'], input[type='checkbox']"));
}"""


def _native_checkbox(control: Control) -> bool:
    """A real checkbox input. Custom ``role=checkbox`` and switches are reported only."""
    if control.kind != "checkbox":
        return False
    if control.input_type in {"aria-checkbox", "switch", "aria-radio"}:
        return False
    if control.role == "switch":
        return False
    return True


def _yes_no_labels(control: Control) -> bool:
    names = {(option.label or option.value or "").strip().casefold() for option in control.options}
    names.discard("")
    return names == {"yes", "no"}


def _choice_selected(target, selector: str) -> bool:
    """True when the clicked Yes/No button itself reads as chosen.

    A page double without ``evaluate`` counts as chosen, like ``control_committed``.
    """
    locator = target.locator(selector)
    evaluate = getattr(locator, "evaluate", None)
    if evaluate is None:
        return True
    try:
        return bool(evaluate(_CHOICE_STATE_JS))
    except Exception:
        return False


def _file_attached(locator) -> bool:
    evaluate = getattr(locator, "evaluate", None)
    if evaluate is None:
        return True
    try:
        return bool(evaluate("el => !!(el.files && el.files.length)"))
    except Exception:
        return False


def _apply_mapped(page, control: Control, mapped: MappedField, *, overwrite: bool = False) -> FieldOutcome:
    if mapped.action in {"skip", "unanswered", "resume_required"}:
        return _outcome(control, mapped)
    try:
        target = _bound_target(page, getattr(control, "frame_url", "") or "")
        locator = target.locator(control.selector)
        if mapped.action == "fill":
            current = _shown_value(locator)
            if not overwrite and current.strip() and not _value_matches(current, mapped.text or ""):
                return _outcome(
                    control,
                    mapped,
                    action="skip",
                    detail="Left an existing value in place.",
                )
            if _value_matches(current, mapped.text or ""):
                remember_confirmed(control, current.strip(), field_key=mapped.key or "")
                return _outcome(control, mapped, detail="already set")
            if control.kind in {"text", "textarea", "combobox"}:
                if not type_visible(target, control.selector, mapped.text):
                    if hasattr(locator, "evaluate"):
                        locator.evaluate(_SET_NATIVE, mapped.text)
                    elif hasattr(locator, "fill"):
                        locator.fill(mapped.text)
            elif hasattr(locator, "fill"):
                locator.fill(mapped.text)
            return _verified_or_blank(control, mapped, locator, mapped.text or "")
        if mapped.action == "select":
            if control.kind == "radio":
                selector = mapped.option_selector or control.selector
                if control.input_type == "aria-radio":
                    if not _yes_no_labels(control):
                        return _outcome(
                            control,
                            mapped,
                            action="unanswered",
                            detail="Custom radio left for a person.",
                        )
                    from autofill.safeguards import press_aria_radio

                    if not press_aria_radio(target, selector):
                        return _outcome(
                            control,
                            mapped,
                            action="unanswered",
                            detail="Value did not stay on the page.",
                        )
                else:
                    from autofill.safeguards import commit_native_choice

                    if not commit_native_choice(target, selector, on=True):
                        return _outcome(
                            control,
                            mapped,
                            action="unanswered",
                            detail="Value did not stay on the page.",
                        )
                picked = Control(
                    kind=control.kind,
                    name=control.name,
                    label=control.label,
                    selector=selector,
                    automation_id=control.automation_id,
                    input_type=control.input_type,
                )
                remember_confirmed(
                    picked,
                    mapped.option_label or mapped.text or "",
                    field_key=mapped.key or "",
                    choice="radio",
                )
                refresh_preview(page)
                return _outcome(control, mapped, detail=mapped.option_label or "selected")
            if control.kind == "combobox":
                from autofill.combobox import select_combobox

                chosen = select_combobox(target, control.selector, mapped.option_label or mapped.text)
                if chosen is None:
                    return _outcome(control, mapped, action="unanswered", detail="No matching option in this list.")
                remember_confirmed(control, chosen.label, field_key=mapped.key or "")
                refresh_preview(page)
                return _outcome(control, mapped, detail=chosen.label)
            if control.kind == "buttons" and mapped.option_selector:
                from autofill.safeguards import click_choice

                click_choice(target, mapped.option_selector, mapped.option_label)
                if not _choice_selected(target, mapped.option_selector):
                    return _outcome(control, mapped, action="unanswered", detail="Value did not stay on the page.")
                remember_confirmed(control, mapped.option_label or mapped.text or "", field_key=mapped.key or "")
                refresh_preview(page)
                return _outcome(control, mapped, detail=mapped.option_label or "selected")
            expected = mapped.option_label or mapped.option_value or mapped.text or ""
            focus_field(target, control.selector)
            if not choose_native_select(
                target,
                control.selector,
                label=mapped.option_label or "",
                value=mapped.option_value or "",
            ):
                return _outcome(control, mapped, action="unanswered", detail="Value did not stay on the page.")
            shown = _shown_value(locator)
            if not (
                _value_verified(shown, expected)
                or _value_verified(shown, mapped.option_value or "")
                or _value_verified(shown, mapped.option_label or "")
            ):
                return _outcome(control, mapped, action="unanswered", detail="Value did not stay on the page.")
            remember_confirmed(control, shown.strip(), field_key=mapped.key or "")
            refresh_preview(page)
            return _outcome(control, mapped, detail=mapped.option_label or "selected")
        if mapped.action == "check":
            if not _native_checkbox(control):
                return _outcome(control, mapped, action="unanswered", detail="Custom checkbox left for a person.")
            from autofill.safeguards import commit_native_choice

            if not commit_native_choice(target, control.selector, on=True):
                return _outcome(control, mapped, action="unanswered", detail="Value did not stay on the page.")
            remember_confirmed(control, "yes", field_key=mapped.key or "", choice="checkbox")
            refresh_preview(page)
            return _outcome(control, mapped, detail="checked")
        if mapped.action == "uncheck":
            if not _native_checkbox(control):
                return _outcome(control, mapped, action="unanswered", detail="Custom checkbox left for a person.")
            from autofill.safeguards import commit_native_choice

            if not commit_native_choice(target, control.selector, on=False):
                return _outcome(control, mapped, action="unanswered", detail="Value did not stay on the page.")
            remember_confirmed(control, "no", field_key=mapped.key or "", choice="checkbox")
            refresh_preview(page)
            return _outcome(control, mapped, detail="unchecked")
        if mapped.action == "upload":
            locator.set_input_files(mapped.text)
            if not _file_attached(locator):
                return _outcome(control, mapped, action="unanswered", detail="File did not stay on the page.")
            return _outcome(control, mapped, detail=Path(mapped.text).name)
    except Exception as exc:
        return _outcome(control, mapped, action="error", detail=str(exc))
    return _outcome(control, mapped, action="unanswered", detail=f"Unsupported action {mapped.action}.")


def _accept_standard_checkbox(page, control: Control) -> bool:
    """Check one required standard-terms box when the session flag is on."""
    from autofill.mapping import AUTO_CONSENT_CATEGORIES, record_auto_consent, standard_terms_enabled, terms_category
    from autofill.safeguards import commit_native_choice

    if not standard_terms_enabled() or not _native_checkbox(control):
        return False
    if not control.required or not control.selector:
        return False
    category = terms_category(control)
    if category not in AUTO_CONSENT_CATEGORIES:
        return False
    if not commit_native_choice(page, control.selector, on=True):
        return False
    record_auto_consent(control, category)
    remember_confirmed(control, "yes", field_key=category, choice="checkbox")
    return True


def _note_manual(control: Control, mapped: MappedField, manual: list[str]) -> None:
    if mapped.field_class in {"MANUAL_REVIEW_FIELD", "LEGAL_FIELD"} or mapped.action == "resume_required":
        label = control.label or control.selector or control.name
        manual.append(f"{label}: {mapped.reason}")


def _fill_checkbox_group(
    page,
    controls: list[Control],
    profile: CandidateProfile,
    *,
    job: JobContext | None,
    cursor: MapCursor | None,
    manual: list[str],
) -> list[FieldOutcome]:
    """One checkbox group is one question. A partial option list checks nothing.

    Grouping is the step filler's ``_clusters`` helper. This does not build a
    second radio grouper. Custom checkboxes stay unclicked.
    """
    from autofill.intents import match_all_options
    from autofill.mapping import map_field, question_stem
    from autofill.stepfill import _checkbox_label

    visible = [
        control
        for control in controls
        if not (
            selector_held(control.selector)
            and (not refill_cleared() or control_committed(page, control.selector))
        )
    ]
    if not visible:
        return []
    stem = question_stem(visible[0])
    synthetic = Control(
        kind="textarea",
        label=stem,
        prompt=visible[0].prompt,
        group=visible[0].group,
        name=visible[0].name,
    )
    mapped = map_field(synthetic, profile, job=job, cursor=cursor)
    options = [
        Option(value=item.selector, label=_checkbox_label(item), selector=item.selector) for item in visible
    ]
    values: list[str] = []
    if mapped.action == "fill" and mapped.text.strip():
        raw = mapped.text.strip()
        values = [part.strip() for part in raw.split(",") if part.strip()] if "," in raw else [raw]
    chosen = match_all_options(values, options) if values else None
    if chosen is None or any(not _native_checkbox(item) for item in visible):
        reason = mapped.reason or "No option matched this group."
        if values and chosen is None and "disagree" not in reason and "Several options" not in reason:
            reason = f"No option matched profile value {mapped.text!r}."
        if any(not _native_checkbox(item) for item in visible):
            reason = "Custom checkbox left for a person."
        blank = MappedField(
            key=mapped.key,
            action="unanswered",
            field_class=mapped.field_class or "UNKNOWN_FIELD",
            reason=reason,
        )
        outcomes: list[FieldOutcome] = []
        for control in visible:
            _note_manual(control, blank, manual)
            outcomes.append(_outcome(control, blank))
        return outcomes
    chosen_selectors = {item.selector for item in chosen}
    outcomes = []
    for control in visible:
        if control.selector not in chosen_selectors:
            continue
        check = MappedField(
            key=mapped.key or "application_answer",
            action="check",
            text=_checkbox_label(control),
            field_class="APPROVED_QUESTION",
            confidence="high",
        )
        outcomes.append(_apply_mapped(page, control, check))
    return outcomes


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
    from autofill.mapping import note_page_country

    for _ in range(4):
        fields = []
        manual = []
        resume_blocked = False
        snapshot = extract_page(page)
        note_page_country(page, snapshot)
        cursor = MapCursor(page_heading=snapshot.heading)
        from autofill.stepfill import _clusters

        for kind, group in _clusters(snapshot):
            if not checkpoint():
                break
            if kind == "multi":
                fields.extend(
                    _fill_checkbox_group(
                        page,
                        group,
                        profile,
                        job=job,
                        cursor=cursor,
                        manual=manual,
                    )
                )
                continue
            control = group[0]
            if selector_held(control.selector) and (
                not refill_cleared() or control_committed(page, control.selector)
            ):
                continue
            if _accept_standard_checkbox(page, control):
                fields.append(
                    _outcome(
                        control,
                        MappedField(key="STANDARD_TERMS", action="check", field_class="LEGAL_FIELD"),
                        detail="checked",
                    )
                )
                continue
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
            messages.append(challenge_message(snapshot))
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

        alert = validation_message(snapshot)
        if alert:
            messages.append(alert)
            break
        nxt = next(
            (
                button
                for button in snapshot.buttons
                if button.selector not in clicked
                and not button.disabled
                and button.in_main
                and not button.in_ad
                and not button.in_header
                and not is_add_row(button.name)
                and classify_control(button.name, control_type=button.control_type) == "continue"
            ),
            None,
        )
        if nxt is None:
            break
        before = navigation_signature(page, snapshot)
        try:
            activate(page, nxt.selector, nxt.name, control_type=nxt.control_type, frame_url=nxt.frame_url)
        except HumanSubmissionRequired:
            break
        clicked.add(nxt.selector)
        advanced, _moved = wait_for_navigation(page, before)
        if not advanced:
            messages.append("navigation_stalled")
            break
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

    from autofill.mapping import bind_us_page, reset_us_page

    ats = detect_ats(target)
    resume = _path_text(resume_path)
    cover = _path_text(cover_letter_path)
    us_token = bind_us_page()
    try:
        return _open_and_fill(
            profile,
            url=url,
            page=page,
            resume=resume,
            cover=cover,
            cover_letter_text=cover_letter_text,
            job=job,
            advance_pages=advance_pages,
            max_pages=max_pages,
            headless=headless,
            resume_uploaded=resume_uploaded,
            ats_name=ats.name if ats else None,
        )
    finally:
        reset_us_page(us_token)


def _open_and_fill(
    profile: CandidateProfile,
    *,
    url: str | None,
    page,
    resume: str | None,
    cover: str | None,
    cover_letter_text: str | None,
    job: JobContext | None,
    advance_pages: bool,
    max_pages: int,
    headless: bool,
    resume_uploaded: bool,
    ats_name: str | None,
) -> FillResult:
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
            ats_name=ats_name,
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
            ats_name=ats_name,
            resume_uploaded=resume_uploaded,
        )
    finally:
        browser.close()
        playwright.stop()
