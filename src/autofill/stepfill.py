"""Fill one application step from resolver values, then drop those values.

Profile records are not consulted. The only values written are the ones the
resolver returned for the keys and intents on this step.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from autofill.adapters import adapter_for
from autofill.dates import format_for_control
from autofill.extract import extract_page
from autofill.fill import apply_mapped
from autofill.intents import (
    IntentMatch,
    classify_question,
    match_all_options,
    match_option_exact,
    may_fill,
    sanitize_question,
)
from autofill.mapping import haystack, is_honeypot, match_field_key, normalize
from autofill.models import Control, FieldOutcome, MappedField, Option, PageSnapshot
from autofill.resolver import ResolverAnswer, ResolverBinding, ResolverResponse, resolve_step

logger = logging.getLogger("autofill.stepfill")

_LEGAL_RE = re.compile(
    r"\b(i agree|i certify|i acknowledge|terms of (service|use)|privacy policy|attest|e-?sign|legal attestation)\b"
)
_PROFILE_FIRST = frozenset(
    {
        "first_name",
        "last_name",
        "full_name",
        "preferred_name",
        "legal_name",
        "email",
        "phone",
        "address",
        "city",
        "state",
        "postal_code",
        "country",
        "linkedin_url",
        "github_url",
        "portfolio_url",
        "website_url",
        "current_company",
        "current_job_title",
        "school",
        "field_of_study",
        "education_level",
        "project_name",
        "project_description",
        "earliest_start_date",
        "years_of_experience_total",
    }
)
_PROTOCOL = {
    "first_name": "firstName",
    "last_name": "lastName",
    "full_name": "fullName",
    "preferred_name": "preferredName",
    "legal_name": "legalName",
    "email": "email",
    "phone": "phone",
    "address": "address.line1",
    "city": "address.city",
    "state": "address.region",
    "postal_code": "address.postalCode",
    "country": "address.country",
    "linkedin_url": "links.linkedin",
    "github_url": "links.github",
    "portfolio_url": "links.portfolio",
    "website_url": "links.website",
    "years_of_experience_total": "yearsExperience",
}
_ALIASES = (
    frozenset({"california", "ca"}),
    frozenset({"new york", "ny"}),
    frozenset({"texas", "tx"}),
    frozenset({"washington", "wa"}),
    frozenset({"united states", "united states of america", "usa", "us"}),
    frozenset({"canada", "ca"}),
)


@dataclass
class FillFlags:
    """Bounded corrections applied on a later attempt for this same step."""

    digits_phone: bool = False
    us_dates: bool = False
    aliases: bool = False


@dataclass
class ResolvedPage:
    """What one step did. Manual entries are intent plus question text only."""

    fields: list[FieldOutcome] = field(default_factory=list)
    snapshot: PageSnapshot | None = None
    resume_blocked: bool = False
    manual_actions: list[str] = field(default_factory=list)
    manual_questions: list[dict[str, str | None]] = field(default_factory=list)
    requested_fields: list[str] = field(default_factory=list)
    requested_intents: list[str | None] = field(default_factory=list)


def validation_blob(snapshot: PageSnapshot) -> str:
    """Alert text only. Labels that merely say a field is required are ignored."""
    parts = list(snapshot.alerts)
    return " ".join(parts).casefold()


def flags_for_alerts(blob: str, previous: FillFlags) -> FillFlags:
    """Turn a validation message into a bounded correction."""
    return FillFlags(
        digits_phone=previous.digits_phone or "invalid phone" in blob,
        us_dates=previous.us_dates or "valid date" in blob,
        aliases=previous.aliases or "select one" in blob or "required" in blob,
    )


def fill_resolved_page(
    page,
    binding: ResolverBinding,
    *,
    session_id: str,
    ats_name: str | None,
    step: str,
    flags: FillFlags | None = None,
    hook=None,
) -> ResolvedPage:
    """Inspect this step, ask the resolver only for it, fill, and wipe the values."""
    active = flags or FillFlags()
    snapshot = extract_page(page)
    outcome = ResolvedPage(snapshot=snapshot)
    clusters = _clusters(snapshot)
    requests: list[_Ask] = []
    for kind, controls in clusters:
        ask = _describe(kind, controls, snapshot.heading, hook)
        if ask is None:
            continue
        if ask.resume:
            outcome.resume_blocked = True
            continue
        requests.append(ask)

    field_keys = _unique([ask.field_key for ask in requests if ask.field_key and not ask.include_question])
    questions = [ask.question_body() for ask in requests if ask.include_question]
    outcome.requested_fields = field_keys
    outcome.requested_intents = [ask.match.intent if ask.match else None for ask in requests if ask.include_question]

    if not field_keys and not questions:
        return outcome

    response: ResolverResponse | None = None
    try:
        response = resolve_step(
            binding,
            session_id=session_id,
            step=step,
            fields=field_keys,
            questions=questions,
        )
        _apply(page, requests, response, active, outcome, snapshot.heading)
        adapter = adapter_for(ats_name)
        logger.info(
            "session=%s step=%s adapter=%s field_keys=%s intents=%s requested=%s returned=%s result=%s latency_ms=%s",
            session_id,
            step,
            adapter.name if adapter else "generic",
            ",".join(field_keys),
            ",".join(intent or "unknown" for intent in outcome.requested_intents),
            len(field_keys) + len(questions),
            len(response.fields) + len(response.answers),
            "ok",
            response.latency_ms,
        )
        return outcome
    finally:
        if response is not None:
            response.wipe()


@dataclass
class _Ask:
    controls: list[Control]
    field_key: str | None = None
    match: IntentMatch | None = None
    unknown: bool = False
    include_question: bool = False
    resume: bool = False
    subfield: str = ""

    def question_body(self) -> dict:
        control = self.controls[0]
        kind = _control_name(self.controls, control)
        labels = _option_labels(self.controls)
        text = self.match.text if self.match is not None else sanitize_question(_question_text(self.controls))
        return {
            "intent": self.match.intent if self.match is not None else None,
            "text": text,
            "options": labels,
            "control": kind,
        }


def _clusters(snapshot: PageSnapshot) -> list[tuple[str, list[Control]]]:
    grouped: dict[str, list[Control]] = {}
    ordered: list[tuple[str, list[Control]]] = []
    seen_groups: set[str] = set()
    for control in snapshot.controls:
        if control.hidden or control.disabled:
            continue
        if control.kind == "checkbox" and control.group:
            grouped.setdefault(control.group, []).append(control)
            continue
        ordered.append(("one", [control]))
    for control in snapshot.controls:
        if control.kind == "checkbox" and control.group and control.group not in seen_groups:
            items = grouped.get(control.group, [])
            if len(items) >= 2:
                ordered.append(("multi", items))
                seen_groups.add(control.group)
            elif len(items) == 1:
                ordered.append(("one", items))
                seen_groups.add(control.group)
    return ordered


def _describe(kind: str, controls: list[Control], heading: str, hook) -> _Ask | None:
    control = controls[0]
    if control.kind == "password" or control.input_type == "password":
        return None
    if is_honeypot(control):
        return None
    blob = haystack(control)
    if _LEGAL_RE.search(blob):
        return None
    if control.kind == "file" or any(item.kind == "file" for item in controls):
        if "resume" in blob or "cv" in blob or "curriculum vitae" in blob:
            return _Ask(controls=controls, resume=True)
        return None
    text = _question_text(controls if kind == "multi" else [control])
    options = _option_labels(controls)
    match = classify_question(text, options, hook=hook)
    internal = None if kind == "multi" else match_field_key(control)
    if kind != "multi" and _prefer_field(control, internal, match):
        key = _protocol_key(internal or "", heading, control)
        if key:
            return _Ask(controls=controls, field_key=key, subfield=_subfield(control, heading))
    if match.intent is not None:
        return _Ask(controls=controls, match=match, include_question=True)
    if kind == "multi":
        return _Ask(controls=controls, unknown=True, include_question=True)
    key = _protocol_key(internal or "", heading, control)
    if key:
        return _Ask(controls=controls, field_key=key, subfield=_subfield(control, heading))
    if control.kind in {"radio", "select", "combobox", "checkbox", "textarea"} or "?" in control.label:
        return _Ask(controls=controls, unknown=True, include_question=True)
    return None


def _prefer_field(control: Control, internal: str | None, match: IntentMatch) -> bool:
    if not internal or internal not in _PROFILE_FIRST:
        return False
    if "?" in control.label:
        return False
    if control.kind in {"radio", "checkbox"}:
        return False
    return True


def _protocol_key(internal: str, heading: str, control: Control) -> str | None:
    blob = haystack(control)
    head = normalize(heading)
    if internal in {"current_company", "current_job_title"} or "employ" in blob or (
        "start date" in blob and "employ" in head
    ):
        if internal in {"school", "education_level", "field_of_study"}:
            return "education[]"
        if "employ" in blob or internal in {"current_company", "current_job_title"} or (
            "start date" in blob and "employ" in head
        ):
            return "employment[]"
    if internal in {"school", "field_of_study", "education_level"} or (
        "educat" in head and ("date" in blob or "school" in blob or "degree" in blob or "major" in blob)
    ):
        return "education[]"
    if internal in {"project_name", "project_description"} or "project" in blob:
        if "project" in blob:
            return "projects[]"
    if internal == "earliest_start_date" and "educat" in head:
        return "education[]"
    if internal == "earliest_start_date" and ("employ" in head or "experience" in head):
        return "employment[]"
    if normalize(control.label) in {"skills", "skill"} or blob == "skills":
        return "skills"
    if internal in _PROTOCOL:
        return _PROTOCOL[internal]
    if "skill" in blob and "experience with" not in blob and normalize(control.label).startswith("skill"):
        return "skills"
    return None


def _subfield(control: Control, heading: str) -> str:
    blob = haystack(control)
    head = normalize(heading)
    if "project description" in blob:
        return "project.description"
    if "project name" in blob or (blob.startswith("project") and "name" in blob):
        return "project.name"
    if normalize(control.label) in {"skills", "skill"}:
        return "skills"
    if "employ" in blob or "company name" in blob:
        return "employment.company"
    if "job title" in blob or "position title" in blob or blob.strip() == "title":
        return "employment.title"
    if "start date" in blob and ("employ" in head or "experience" in head):
        return "employment.startDate"
    if "school" in blob or "university" in blob or "college" in blob:
        return "education.school"
    if "field of study" in blob or re.search(r"\bmajor\b", blob):
        return "education.field"
    if "degree" in blob or "education level" in blob:
        return "education.degree"
    if "end date" in blob or (control.input_type == "date" and "educat" in head):
        return "education.endDate"
    if "start date" in blob and "educat" in head:
        return "education.startDate"
    return ""


def _apply(
    page,
    requests: list[_Ask],
    response: ResolverResponse,
    flags: FillFlags,
    outcome: ResolvedPage,
    heading: str,
) -> None:
    answers = {item.intent: item for item in response.answers}
    employment_rows = _rows(response.fields.get("employment[]"))
    education_rows = _rows(response.fields.get("education[]"))
    project_rows = _rows(response.fields.get("projects[]"))
    employment_index = -1
    for ask in requests:
        if ask.include_question:
            _apply_question(page, ask, answers.get(ask.match.intent if ask.match else None), outcome)
            continue
        if not ask.field_key:
            continue
        if ask.field_key == "employment[]":
            sub = ask.subfield or _subfield(ask.controls[0], heading)
            if sub == "employment.company":
                employment_index += 1
            row = employment_rows[employment_index] if 0 <= employment_index < len(employment_rows) else None
            if row is None and employment_rows:
                row = employment_rows[0]
            _apply_row_field(page, ask.controls[0], row, sub, flags, outcome)
            continue
        if ask.field_key == "education[]":
            row = education_rows[0] if education_rows else None
            subfield = ask.subfield or _subfield(ask.controls[0], heading)
            _apply_row_field(page, ask.controls[0], row, subfield, flags, outcome)
            continue
        if ask.field_key == "projects[]":
            row = project_rows[0] if project_rows else None
            subfield = ask.subfield or _subfield(ask.controls[0], heading)
            _apply_row_field(page, ask.controls[0], row, subfield, flags, outcome)
            continue
        if ask.field_key == "skills":
            raw = response.fields.get("skills", "")
            text = ", ".join(raw) if isinstance(raw, list) else str(raw or "")
            _write_text(page, ask.controls[0], text, outcome, key=ask.field_key)
            continue
        if ask.field_key not in response.fields and ask.field_key not in response.unresolved:
            _skip(ask.controls[0], outcome, ask.field_key, "Resolver did not return this key.")
            continue
        raw_value = response.fields.get(ask.field_key, "")
        text = "" if raw_value is None else str(raw_value)
        if ask.field_key == "phone" and flags.digits_phone:
            text = "".join(character for character in text if character.isdigit())
        if flags.us_dates and _looks_iso(text):
            text = _force_us_date(text)
        _write_control(page, ask.controls[0], text, outcome, key=ask.field_key, flags=flags)


def _apply_question(
    page,
    ask: _Ask,
    answer: ResolverAnswer | None,
    outcome: ResolvedPage,
) -> None:
    match = ask.match or IntentMatch(None, "unknown", "none", sanitize_question(_question_text(ask.controls)))
    confidence = answer.confidence if answer is not None else "LOW"
    if answer is None or not may_fill(local=match, resolver_confidence=confidence):
        _manual(outcome, match)
        return
    control = ask.controls[0]
    if len(ask.controls) > 1 or (control.kind == "checkbox" and control.group and len(ask.controls) > 1):
        saved = list(answer.values) or ([answer.value] if answer.value else [])
        options = [
            Option(value=item.selector, label=_checkbox_label(item), selector=item.selector) for item in ask.controls
        ]
        chosen = match_all_options(saved, options)
        if chosen is None:
            _manual(outcome, match)
            return
        chosen_selectors = {item.selector for item in chosen}
        for item in ask.controls:
            if item.selector in chosen_selectors:
                mapped = MappedField(
                    key=match.intent,
                    action="check",
                    field_class="APPROVED_QUESTION",
                    confidence="high",
                )
                outcome.fields.append(apply_mapped(page, item, mapped))
        return
    if control.kind in {"radio", "select", "combobox"}:
        desired = answer.value or (answer.values[0] if answer.values else "")
        option = match_option_exact(desired, control.options)
        if option is None:
            _manual(outcome, match)
            return
        mapped = MappedField(
            key=match.intent,
            action="select",
            option_label=option.label,
            option_value=option.value,
            option_selector=option.selector,
            field_class="APPROVED_QUESTION",
            confidence="high",
        )
        outcome.fields.append(apply_mapped(page, control, mapped))
        return
    if control.kind == "checkbox":
        desired = answer.value or (answer.values[0] if answer.values else "")
        if options_yes(desired):
            mapped = MappedField(key=match.intent, action="check", field_class="APPROVED_QUESTION", confidence="high")
            outcome.fields.append(apply_mapped(page, control, mapped))
            return
        if options_no(desired):
            mapped = MappedField(key=match.intent, action="uncheck", field_class="APPROVED_QUESTION", confidence="high")
            outcome.fields.append(apply_mapped(page, control, mapped))
            return
        _manual(outcome, match)
        return
    if not answer.value:
        _manual(outcome, match)
        return
    _write_text(page, control, answer.value, outcome, key=match.intent)


def options_yes(value: str) -> bool:
    from autofill.intents import options_equivalent

    return options_equivalent(value, "Yes")


def options_no(value: str) -> bool:
    from autofill.intents import options_equivalent

    return options_equivalent(value, "No") and not options_equivalent(value, "Yes")


def _apply_row_field(
    page,
    control: Control,
    row: dict[str, str] | None,
    subfield: str,
    flags: FillFlags,
    outcome: ResolvedPage,
) -> None:
    if row is None:
        _skip(control, outcome, subfield or None, "Resolver did not return this repeater.")
        return
    member = subfield.split(".", 1)[-1] if subfield else ""
    aliases = {
        "company": ("company", "employer"),
        "title": ("title", "jobTitle"),
        "startDate": ("startDate", "start_date", "start"),
        "endDate": ("endDate", "end_date", "end"),
        "school": ("school", "institution"),
        "degree": ("degree",),
        "field": ("field", "fieldOfStudy", "major"),
        "name": ("name",),
        "description": ("description",),
    }
    text = ""
    for key in aliases.get(member, (member,)):
        if row.get(key):
            text = str(row[key])
            break
    if not text:
        _skip(control, outcome, subfield or None, "Resolver row has no value for this control.")
        return
    if flags.us_dates and _looks_iso(text):
        text = _force_us_date(text)
    elif member.lower().endswith("date"):
        formatted = format_for_control(
            text,
            input_type=control.input_type,
            placeholder=control.placeholder,
            label=control.label,
        )
        if formatted:
            text = formatted
    _write_control(page, control, text, outcome, key=subfield or None, flags=flags)


def _write_control(
    page,
    control: Control,
    text: str,
    outcome: ResolvedPage,
    *,
    key: str | None,
    flags: FillFlags,
) -> None:
    if not text:
        _skip(control, outcome, key, "No value for this control.")
        return
    if control.kind in {"select", "radio", "combobox"}:
        option = match_option_exact(text, control.options)
        if option is None and flags.aliases:
            option = _alias_option(text, control.options)
        if option is None:
            _skip(control, outcome, key, "No exact option for this value.")
            return
        mapped = MappedField(
            key=key,
            action="select",
            option_label=option.label,
            option_value=option.value,
            option_selector=option.selector,
            field_class="PROFILE_FIELD",
            confidence="high",
        )
        outcome.fields.append(apply_mapped(page, control, mapped))
        return
    if control.input_type == "date" or "date" in normalize(control.label):
        formatted = format_for_control(
            text,
            input_type=control.input_type,
            placeholder=control.placeholder,
            label=control.label,
        )
        if flags.us_dates and _looks_iso(text):
            formatted = _force_us_date(text)
        if not formatted:
            _skip(control, outcome, key, "Value is not a date this control accepts.")
            return
        text = formatted
    _write_text(page, control, text, outcome, key=key)


def _write_text(page, control: Control, text: str, outcome: ResolvedPage, *, key: str | None) -> None:
    if not str(text).strip():
        _skip(control, outcome, key, "Empty value left blank.")
        return
    mapped = MappedField(key=key, action="fill", text=str(text), field_class="PROFILE_FIELD", confidence="high")
    outcome.fields.append(apply_mapped(page, control, mapped, overwrite=True))


def _skip(control: Control, outcome: ResolvedPage, key: str | None, reason: str) -> None:
    outcome.fields.append(
        FieldOutcome(
            selector=control.selector,
            label=control.label,
            key=key,
            action="unanswered",
            detail=reason,
            field_class="UNKNOWN_FIELD",
        )
    )


def _manual(outcome: ResolvedPage, match: IntentMatch) -> None:
    text = sanitize_question(match.text or "")
    entry = {"intent": match.intent, "text": text}
    if entry not in outcome.manual_questions:
        outcome.manual_questions.append(entry)
    label = match.intent or "unknown"
    outcome.manual_actions.append(f"{label}: manual answer required")


def _rows(value: object) -> list[dict[str, str]]:
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        return []
    rows: list[dict[str, str]] = []
    for entry in value:
        if isinstance(entry, dict):
            rows.append({str(key): "" if item is None else str(item) for key, item in entry.items()})
    return rows


def _question_text(controls: list[Control]) -> str:
    if len(controls) > 1 and controls[0].group:
        return controls[0].group
    control = controls[0]
    if control.group and control.kind == "checkbox":
        return control.group
    return control.label or control.aria_label or control.placeholder


def _option_labels(controls: list[Control]) -> list[str]:
    if len(controls) > 1:
        return [_checkbox_label(item) for item in controls][:20]
    labels = []
    for option in controls[0].options:
        label = option.label or option.value
        if label and label not in labels:
            labels.append(sanitize_question(label, limit=80))
    return labels[:20]


def _checkbox_label(control: Control) -> str:
    """Option text with the fieldset legend removed from either end."""
    label = control.label.strip()
    group = control.group.strip()
    if group:
        folded = label.casefold()
        group_folded = group.casefold()
        if folded.startswith(group_folded):
            label = label[len(group) :].strip(" :-")
        elif folded.endswith(group_folded):
            label = label[: -len(group)].strip(" :-")
    return sanitize_question(label or control.aria_label or control.name, limit=80)


def _control_name(controls: list[Control], control: Control) -> str:
    if len(controls) > 1:
        return "multiselect"
    if control.kind == "radio":
        return "radio"
    if control.kind in {"select", "combobox"}:
        return "select"
    if control.kind == "checkbox":
        return "checkbox"
    return "text"


def _unique(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


def _looks_iso(value: str) -> bool:
    return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value.strip()))


def _force_us_date(value: str) -> str:
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", value.strip())
    if not match:
        return value
    return f"{match.group(2)}/{match.group(3)}/{match.group(1)}"


def _alias_option(desired: str, options: list[Option]) -> Option | None:
    token = normalize(desired)
    groups = [group for group in _ALIASES if token in group]
    if not groups:
        return None
    allowed = set().union(*groups)
    for option in options:
        if normalize(option.label) in allowed or normalize(option.value) in allowed:
            return option
    return None
