"""Fill an application form and stop.

The final Submit / Apply control is never clicked. Multi-page applications
may advance with Next, Continue, or Review when the caller opts in.
``fill_application`` does not type passwords. Authorized login lives in
``autofill_application``. A visible CAPTCHA widget stops the run before
any field is written. Resume file inputs are reported and never selected.
"""

from __future__ import annotations

import logging
import time
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

logger = logging.getLogger("autofill.fill")

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
    from autofill.combobox import clear_satisfied_invalid

    host = getattr(locator, "page", None)
    if host is not None:
        clear_satisfied_invalid(host, control.selector)
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
        if mapped.action == "fill" and control.kind == "buttons" and control.role == "listbox":
            return _outcome(
                control,
                mapped,
                action="unanswered",
                detail="A choice button is not a text field.",
            )
        if mapped.action == "fill":
            if mapped.key == "city" and control.kind in {"text", "textarea", "combobox"}:
                from autofill.stepfill import commit_city_text

                report = commit_city_text(target, control.selector, mapped.text or "")
                if not report.get("filled"):
                    return _outcome(
                        control,
                        mapped,
                        action="unanswered",
                        detail="Value did not stay on the page.",
                    )
                remember_confirmed(control, mapped.text or "", field_key=mapped.key or "")
                refresh_preview(page)
                return _outcome(control, mapped, detail="filled")
            if mapped.key == "email":
                from autofill.dom_ui import prime_primary_email
                from autofill.mapping import is_confirm_email

                if is_confirm_email(control):
                    prime_primary_email(target, control.selector, mapped.text or "")
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
                    from autofill.mapping import attestation_manual, is_consent_choice, is_demographic_control

                    if (
                        is_consent_choice(control)
                        or is_demographic_control(control)
                        or attestation_manual(control)
                    ):
                        return _outcome(
                            control,
                            mapped,
                            action="unanswered",
                            detail="This statement stays manual.",
                        )
                    from autofill.dom_ui import await_aria_checked
                    from autofill.safeguards import press_aria_radio

                    if not press_aria_radio(target, selector) and not await_aria_checked(target, selector):
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
            if control.kind == "buttons" and control.role == "listbox":
                from autofill.combobox import select_listbox_button
                from autofill.dom_ui import listbox_choice_for

                intended = mapped.option_label or mapped.text or ""
                chosen = select_listbox_button(
                    target,
                    control.selector,
                    intended,
                    question=control.label or control.prompt or "",
                )
                if chosen is None:
                    return _outcome(control, mapped, action="unanswered", detail="No matching option in this list.")
                remember_confirmed(control, chosen.label, field_key=mapped.key or "")
                refresh_preview(page)
                audit = listbox_choice_for(control.selector)
                detail = chosen.label
                if audit:
                    detail = (
                        f"intended={audit.get('intended') or ''} "
                        f"clicked={audit.get('clicked') or ''} "
                        f"shown={audit.get('shown') or ''}"
                    )
                return _outcome(control, mapped, detail=detail)
            if control.kind == "buttons" and mapped.option_selector:
                from autofill.safeguards import click_choice

                click_choice(target, mapped.option_selector, mapped.option_label)
                if not _choice_selected(target, mapped.option_selector):
                    return _outcome(control, mapped, action="unanswered", detail="Value did not stay on the page.")
                remember_confirmed(control, mapped.option_label or mapped.text or "", field_key=mapped.key or "")
                refresh_preview(page)
                return _outcome(control, mapped, detail=mapped.option_label or "selected")
            expected = mapped.option_label or mapped.option_value or mapped.text or ""
            shown = _shown_value(locator)
            if _value_verified(shown, expected) or _value_verified(shown, mapped.option_label or "") or _value_verified(
                shown, mapped.option_value or ""
            ):
                remember_confirmed(control, shown.strip(), field_key=mapped.key or "")
                return _outcome(control, mapped, detail="already set")
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
            from autofill.safeguards import commit_choice

            if not commit_choice(target, control.selector, on=True):
                return _outcome(control, mapped, action="unanswered", detail="Value did not stay on the page.")
            remember_confirmed(control, "yes", field_key=mapped.key or "", choice="checkbox")
            refresh_preview(page)
            return _outcome(control, mapped, detail="checked")
        if mapped.action == "uncheck":
            from autofill.safeguards import commit_choice

            if not commit_choice(target, control.selector, on=False):
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
    from autofill.safeguards import commit_choice

    if not standard_terms_enabled() or control.kind != "checkbox":
        return False
    if not control.required or not control.selector:
        return False
    category = terms_category(control)
    if category not in AUTO_CONSENT_CATEGORIES:
        return False
    if not commit_choice(page, control.selector, on=True):
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
    if chosen is None:
        reason = mapped.reason or "No option matched this group."
        if values and "disagree" not in reason and "Several options" not in reason:
            reason = f"No option matched profile value {mapped.text!r}."
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


_ADDRESS_RESCAN_KEYS = frozenset({"address", "city", "state", "postal_code", "phone"})


def _section_add_button(snapshot: PageSnapshot, token: str):
    """The Add control for one My Experience section, such as Education."""
    wanted = token.casefold()
    for button in snapshot.buttons:
        if button.disabled or button.in_ad or button.in_header or not button.in_main:
            continue
        if not is_add_row(button.name):
            continue
        blob = " ".join(
            part
            for part in (button.name, button.aria_label, button.automation_id, button.selector)
            if part
        ).casefold()
        if wanted in blob:
            return button
    return None


def _address_pending(page, snapshot: PageSnapshot) -> bool:
    """Country is committed and a street, city, region, postal, or phone box is still empty.

    Workday clears or reveals those inputs when the country is chosen. County
    and phone device type are not part of that block.
    """
    from autofill.mapping import (
        _is_county,
        _is_middle_name,
        _is_phone_device,
        _is_phone_extension,
        match_field_key,
    )

    country_set = False
    pending = False
    for control in snapshot.controls:
        if control.hidden:
            continue
        if _is_county(control) or _is_middle_name(control) or _is_phone_device(control) or _is_phone_extension(control):
            continue
        key = match_field_key(control)
        if key == "country":
            if control.disabled:
                continue
            if not control.selector or control_committed(page, control.selector):
                country_set = True
            continue
        if key not in _ADDRESS_RESCAN_KEYS:
            continue
        if control.disabled or not control.selector or not control_committed(page, control.selector):
            pending = True
    return country_set and pending


def _css_hidden_on_this_page(page, selector: str) -> bool:
    """True when the control's own style hides it and no ancestor step is hidden.

    Workday's resume input is ``display: none`` on the current My Experience
    page. A file inside a later ``hidden`` section is not that input.
    """
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None or not selector:
        return False
    script = r"""(selector) => {
      let el = null;
      try { el = document.querySelector(selector); } catch (error) { return false; }
      if (!el) return false;
      const style = window.getComputedStyle(el);
      if (style.display !== "none" && style.visibility !== "hidden") return false;
      let node = el.parentElement;
      while (node) {
        if (node.hidden) return false;
        const parentStyle = window.getComputedStyle(node);
        if (parentStyle.display === "none" || parentStyle.visibility === "hidden") return false;
        node = node.parentElement;
      }
      return true;
    }"""
    try:
        return bool(evaluate(script, selector))
    except Exception:
        return False


def _click_add(page, button) -> bool:
    """Open one repeatable section. A bad selector must not fail the page."""
    try:
        activate(page, button.selector, button.name, control_type=button.control_type)
    except Exception as exc:
        logger.info("add_row result=error error_type=%s", type(exc).__name__)
        return False
    return True


def click_add_and_wait(page, button, count_rows) -> bool:
    """Click Add once. True only when that section's row count increases.

    Workday paints the new row after the click. The wait is about three
    seconds. A click that adds nothing stops the caller from clicking again.
    """
    try:
        before = int(count_rows(extract_page(page)))
    except Exception:
        before = 0
    if not _click_add(page, button):
        return False
    deadline = time.monotonic() + 3.0
    while True:
        try:
            fresh = extract_page(page)
            current = int(count_rows(fresh))
        except Exception:
            return False
        if current > before:
            return True
        pause = getattr(page, "wait_for_timeout", None)
        if pause is None or time.monotonic() >= deadline:
            return False
        try:
            pause(int(min(100, max(1, (deadline - time.monotonic()) * 1000))))
        except Exception:
            return False


def _count_work_rows(snapshot: PageSnapshot) -> int:
    from autofill.mapping import is_current_company_label

    return sum(1 for control in snapshot.controls if not control.hidden and is_current_company_label(control))


def _count_education_rows(snapshot: PageSnapshot) -> int:
    return len(_school_controls(snapshot))


def _school_controls(snapshot: PageSnapshot):
    found = []
    for control in snapshot.controls:
        if control.hidden:
            continue
        blob = f"{control.label} {control.aria_label} {control.element_id} {control.automation_id}".casefold()
        if "school" in blob or "university" in blob or "college" in blob:
            found.append(control)
    return found


def _profile_work_rows(profile: CandidateProfile) -> int:
    return sum(
        1
        for item in profile.employment
        if item.company or item.title or item.start_date or item.end_date
    )


def _profile_education_rows(profile: CandidateProfile) -> int:
    return sum(
        1
        for item in profile.education_history
        if item.school or item.degree or item.field_of_study or item.start_date or item.end_date
    )


def _offer_add(page, snapshot, state, kind, needed, count_rows, button_fn) -> bool:
    """Click Add at most (profile rows − visible rows) times for this section.

    The cap is fixed from the first look at the page. A click that does not
    increase the row count stops the section. True when a row was added.
    """
    slot = state[kind]
    if slot["stopped"]:
        return False
    try:
        visible = int(count_rows(snapshot))
    except Exception:
        slot["stopped"] = True
        return False
    if visible >= needed:
        slot["stopped"] = True
        return False
    if slot["cap"] is None:
        slot["cap"] = max(0, needed - visible)
    if slot["clicks"] >= slot["cap"]:
        slot["stopped"] = True
        return False
    button = button_fn(snapshot)
    if button is None:
        slot["stopped"] = True
        return False
    if not click_add_and_wait(page, button, count_rows):
        slot["stopped"] = True
        return False
    slot["clicks"] += 1
    return True


def _education_needed(profile: CandidateProfile, snapshot: PageSnapshot) -> bool:
    rows = [item for item in profile.education_history if item.school or item.degree or item.field_of_study]
    if not rows:
        return False
    visible = 0
    for control in snapshot.controls:
        if control.hidden:
            continue
        blob = f"{control.label} {control.aria_label} {control.element_id} {control.automation_id}".casefold()
        if "school" in blob or "university" in blob or "college" in blob:
            visible += 1
    return visible < len(rows)


_CLEAR_ROW_JS = r"""(selector) => {
  let el = null;
  try { el = document.querySelector(selector); } catch (error) { return false; }
  if (!el) return false;
  const current = "value" in el ? String(el.value || "") : "";
  if (!current.trim()) return false;
  const proto = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
  if (proto && proto.set) proto.set.call(el, "");
  else el.value = "";
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return true;
}"""


def _clear_blank_profile_row(page, control: Control, mapped: MappedField) -> bool:
    """Clear a work-row title or location the profile does not have.

    Workday copies the previous row when Add is clicked. An empty title must
    not keep ``current_job_title``, and an empty job location must not keep
    the home address.
    """
    if mapped.action != "unanswered" or mapped.key not in {"current_job_title", "employment_location"}:
        return False
    if "no value" not in (mapped.reason or "").casefold():
        return False
    if not control.selector:
        return False
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return False
    try:
        return bool(evaluate(_CLEAR_ROW_JS, control.selector))
    except Exception:
        return False


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
    from autofill.sms_consent import SMS_INTENT, accept_recruitment_sms, apply_stored_sms_no, begin_sms_pass

    rescans = 0
    add_state = {
        "work": {"clicks": 0, "cap": None, "stopped": False},
        "education": {"clicks": 0, "cap": None, "stopped": False},
    }
    passes = max(8, _profile_work_rows(profile) + _profile_education_rows(profile) + 4)
    for _ in range(passes):
        fields = []
        manual = []
        resume_blocked = False
        snapshot = extract_page(page)
        note_page_country(page, snapshot)
        from autofill.stepfill import _experience_add_button

        grew = _offer_add(
            page, snapshot, add_state, "work", _profile_work_rows(profile), _count_work_rows, _experience_add_button
        )
        snapshot = extract_page(page)
        grew = (
            _offer_add(
                page,
                snapshot,
                add_state,
                "education",
                _profile_education_rows(profile),
                _count_education_rows,
                lambda current: _section_add_button(current, "education"),
            )
            or grew
        )
        snapshot = extract_page(page)
        note_page_country(page, snapshot)
        from autofill.split_dates import fill_split_dates

        handled_dates = fill_split_dates(page, profile, snapshot)
        cursor = MapCursor(page_heading=snapshot.heading)
        from autofill.stepfill import _clusters

        begin_sms_pass()
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
            if control.selector and control.selector in handled_dates:
                continue
            # A listbox that already shows the wrong Yes/No is selected again.
            # Other held controls stay as the person left them. The repeater
            # cursor still advances so the next row does not reuse this one.
            if control.role != "listbox" and selector_held(control.selector) and (
                not refill_cleared() or control_committed(page, control.selector)
            ):
                map_field(
                    control,
                    profile,
                    cover_letter_path=cover_letter_path,
                    cover_letter_text=cover_letter_text,
                    job=job,
                    cursor=cursor,
                )
                continue
            from autofill.combobox import attach_listbox_options

            attach_listbox_options(page, control)
            if accept_recruitment_sms(page, control, profile):
                fields.append(
                    _outcome(
                        control,
                        MappedField(
                            key=SMS_INTENT,
                            action="check" if control.kind == "checkbox" else "select",
                            field_class="APPROVED_QUESTION",
                        ),
                        detail="recruitment SMS",
                    )
                )
                continue
            if apply_stored_sms_no(page, control, profile):
                fields.append(
                    _outcome(
                        control,
                        MappedField(key=SMS_INTENT, action="select", text="No", field_class="APPROVED_QUESTION"),
                        detail="recruitment SMS declined",
                    )
                )
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
            if _clear_blank_profile_row(page, control, mapped):
                fields.append(
                    _outcome(
                        control,
                        mapped,
                        action="skip",
                        detail="Cleared a value that is not on this profile row.",
                    )
                )
                continue
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
        if not resume_blocked:
            for control in snapshot.controls:
                if control.kind != "file" or not control.hidden:
                    continue
                if not _css_hidden_on_this_page(page, control.selector):
                    continue
                shown = Control(
                    kind=control.kind,
                    name=control.name,
                    element_id=control.element_id,
                    label=control.label,
                    placeholder=control.placeholder,
                    aria_label=control.aria_label,
                    autocomplete=control.autocomplete,
                    required=control.required,
                    options=control.options,
                    selector=control.selector,
                    input_type=control.input_type,
                    role=control.role,
                    nearby=control.nearby,
                    group=control.group,
                    prompt=control.prompt,
                    automation_id=control.automation_id,
                )
                mapped = map_field(
                    shown,
                    profile,
                    cover_letter_path=cover_letter_path,
                    cover_letter_text=cover_letter_text,
                    job=job,
                    cursor=cursor,
                )
                if mapped.action != "resume_required":
                    continue
                if resume_uploaded:
                    fields.append(
                        _outcome(
                            control,
                            mapped,
                            action="skip",
                            detail="Resume left for the person who uploaded it.",
                        )
                    )
                else:
                    resume_blocked = True
                    _note_manual(control, mapped, manual)
                    fields.append(_apply_mapped(page, control, mapped))
                break
        if grew:
            continue
        if resume_blocked:
            break
        if rescans < 2:
            try:
                fresh = extract_page(page)
            except Exception:
                fresh = snapshot
            if _address_pending(page, fresh):
                rescans += 1
                pause = getattr(page, "wait_for_timeout", None)
                if pause is not None:
                    try:
                        pause(80)
                    except Exception:
                        pass
                continue
        break
    from autofill.combobox import resettle_listboxes

    resettle_listboxes(page)
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
