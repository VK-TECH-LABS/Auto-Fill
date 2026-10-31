"""Fill one application step from resolver values, then drop those values.

The city typed into a location box is the resolver's city. A profile state or
country is used only to choose among suggestions when the resolver omitted
them. Other profile records are not written.
"""

from __future__ import annotations

import json
import logging
import re
import time
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from typing import Any

from autofill.adapters import adapter_for
from autofill.combobox import list_combobox_options, select_combobox
from autofill.dates import format_for_control
from autofill.dom_ui import (
    answer_type_name,
    canonical_blocker_kind,
    checkpoint,
    control_committed,
    counts_required,
    refill_cleared,
    selector_held,
    sync_required_progress,
    type_char_delay,
    type_visible,
)
from autofill.extract import extract_page
from autofill.fill import apply_mapped, attach_resume, remember_confirmed
from autofill.intents import (
    DEMOGRAPHIC_INTENTS,
    UNFILLED_INTENTS,
    IntentMatch,
    classify_question,
    combine_authorized_without,
    combine_yes_if_either,
    dedupe_label,
    match_all_options,
    match_option_exact,
    may_fill,
    normalize_question,
    question_hash,
    sanitize_question,
)
from autofill.mapping import (
    AUTO_CONSENT_CATEGORIES,
    _wants_national_digits,
    acknowledgement_kind,
    attestation_manual,
    haystack,
    is_consent_choice,
    is_demographic_control,
    is_honeypot,
    map_field,
    match_field_key,
    mentions_work_history,
    national_phone_digits,
    normalize,
    record_auto_consent,
    standard_terms_enabled,
    terms_category,
)
from autofill.models import Control, FieldOutcome, MappedField, Option, PageSnapshot
from autofill.profile import CandidateProfile
from autofill.resolver import ResolverAnswer, ResolverBinding, ResolverResponse, resolve_step
from autofill.resume_fetch import ResumeFetchError, fetch_resume_payload
from autofill.safeguards import click_choice, dismiss_combobox
from autofill.salary import SALARY_RELATED_FIELDS, salary_fill_allowed, salary_shape

logger = logging.getLogger("autofill.stepfill")

_LOCATION_PROFILE: ContextVar[CandidateProfile | None] = ContextVar("autofill_location_profile", default=None)


def bind_location_profile(profile: CandidateProfile | None) -> Token:
    """Remember a profile for location matching. The city still comes from the resolver."""
    return _LOCATION_PROFILE.set(profile)


def reset_location_profile(token: Token) -> None:
    _LOCATION_PROFILE.reset(token)

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
        "phone_country",
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
    "phone_country": "phone.country",
    "linkedin_url": "links.linkedin",
    "github_url": "links.github",
    "portfolio_url": "links.portfolio",
    "website_url": "links.website",
    "years_of_experience_total": "yearsExperience",
}
_LOCATION_KEYS = ("address.city", "address.region", "address.state", "address.country", "location")
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
    manual_questions: list[dict[str, Any]] = field(default_factory=list)
    requested_fields: list[str] = field(default_factory=list)
    requested_intents: list[str | None] = field(default_factory=list)


def validation_blob(snapshot: PageSnapshot) -> str:
    """Alert text only. Labels that merely say a field is required are ignored."""
    parts = list(snapshot.alerts)
    return " ".join(parts).casefold()


def flags_for_alerts(blob: str, previous: FillFlags) -> FillFlags:
    """Turn a validation message into a bounded correction."""
    phone_format = "invalid phone" in blob or ("phone" in blob and "valid format" in blob)
    return FillFlags(
        digits_phone=previous.digits_phone or phone_format,
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
    resume_uploaded: bool = False,
) -> ResolvedPage:
    """Inspect this step, ask the resolver only for it, fill, and wipe the values."""
    active = flags or FillFlags()
    if resume_uploaded:
        settle_form(page)
    snapshot = extract_page(page)
    outcome = ResolvedPage(snapshot=snapshot)
    clusters = _clusters(snapshot)
    requests: list[_Ask] = []
    for kind, controls in clusters:
        ask = _describe(kind, controls, snapshot.heading, hook)
        if ask is None:
            continue
        if ask.field_key == "resume.file" and resume_uploaded:
            # The resume-file endpoint already attached this file. Uploading
            # it again restarts the ATS parse and the confirmation wait.
            continue
        if ask.resume:
            outcome.resume_blocked = True
            continue
        requests.append(ask)

    field_keys = _requested_field_keys(requests)
    if _dependent_region_pending(snapshot):
        # A state menu that is still disabled, or still only a placeholder, is
        # not a cluster yet. Ask for the region now so it can be chosen after
        # the country select enables it.
        for key in ("address.region", "address.state"):
            if key not in field_keys:
                field_keys.append(key)
    questions = [ask.question_body() for ask in requests if ask.include_question]
    outcome.requested_fields = field_keys
    outcome.requested_intents = [ask.match.intent if ask.match else None for ask in requests if ask.include_question]

    if not field_keys and not questions:
        _note_unfilled_required(outcome, page)
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
        response = _complete_compounds(binding, session_id=session_id, step=step, requests=requests, response=response)
        _apply(
            page,
            requests,
            response,
            active,
            outcome,
            snapshot.heading,
            session_id=session_id,
            preserve_filled=resume_uploaded,
        )
        _fill_region_after_country(page, response, outcome)
        _note_unfilled_required(outcome, page)
        adapter = adapter_for(ats_name)
        resume_code = "none"
        if "resume.file" in field_keys:
            resume_code = "blocked" if outcome.resume_blocked else "attached"
        logger.info(
            "session=%s step=%s adapter=%s field_keys=%s intents=%s "
            "requested=%s returned=%s returned_keys=%s result=%s resume=%s latency_ms=%s",
            session_id,
            step,
            adapter.name if adapter else "generic",
            ",".join(field_keys),
            ",".join(intent or "unknown" for intent in outcome.requested_intents),
            len(field_keys) + len(questions),
            len(response.fields) + len(response.answers),
            ",".join(sorted(str(key) for key in response.fields)),
            response.result_code,
            resume_code,
            response.latency_ms,
        )
        return outcome
    finally:
        outcome.manual_questions = _drop_live_answered(page, outcome.manual_questions)
        ensure_blocker_options(page, outcome.manual_questions)
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
    compose_location: bool = False
    consent: bool = False
    consent_choice: bool = False

    def question_body(self) -> dict:
        control = self.controls[0]
        kind = _control_name(self.controls, control)
        labels = _option_labels(self.controls)
        text = self.match.text if self.match is not None else sanitize_question(_question_text(self.controls))
        token = _control_token(control)
        body = {
            "intent": self.match.intent if self.match is not None else None,
            "text": text,
            "options": labels,
            "control": kind,
        }
        if token:
            body["fieldToken"] = token
        if self.match is not None and self.match.intent == "SALARY_EXPECTATION":
            # Related compensation keys. Values stay on the Companion side.
            body["salaryShape"] = salary_shape(text)
            body["relatedFields"] = list(SALARY_RELATED_FIELDS)
        return body


def _is_section_heading(text: str) -> bool:
    """True for a standalone title such as ``ADDITIONAL INFORMATION``."""
    cleaned = _clean_prompt(text)
    if not cleaned or "?" in cleaned:
        return False
    letters = [char for char in cleaned if char.isalpha()]
    return len(letters) >= 3 and all(char.isupper() for char in letters)


def _checkbox_key(control: Control) -> str:
    """One bucket per fieldset, group question, or shared name.

    Options that share any of those stay one question. A section heading is
    not a group question.
    """
    if control.kind != "checkbox":
        return ""
    if control.group and not _is_section_heading(control.group):
        return "fieldset:" + normalize(control.group)
    prompt = _clean_prompt(control.prompt)
    option = normalize(_checkbox_label(control))
    if prompt and not _is_section_heading(prompt) and normalize(prompt) != option:
        return "prompt:" + normalize(prompt)
    if control.name:
        return "name:" + control.name
    return ""


def _clusters(snapshot: PageSnapshot) -> list[tuple[str, list[Control]]]:
    grouped: dict[str, list[Control]] = {}
    ordered: list[tuple[str, list[Control]]] = []
    seen_groups: set[str] = set()
    for control in snapshot.controls:
        if control.hidden or control.disabled:
            continue
        key = _checkbox_key(control)
        if key:
            grouped.setdefault(key, []).append(control)
            continue
        ordered.append(("one", [control]))
    for control in snapshot.controls:
        if control.hidden or control.disabled:
            continue
        key = _checkbox_key(control)
        if not key or key in seen_groups:
            continue
        seen_groups.add(key)
        items = grouped.get(key, [])
        if len(items) >= 2:
            ordered.append(("multi", items))
        elif len(items) == 1:
            ordered.append(("one", items))
    return ordered


def _describe(kind: str, controls: list[Control], heading: str, hook) -> _Ask | None:
    control = controls[0]
    if control.kind == "password" or control.input_type == "password":
        return None
    if is_honeypot(control):
        return None
    blob = haystack(control)
    role = acknowledgement_kind(control)
    if not is_demographic_control(control):
        if is_consent_choice(control):
            return _Ask(controls=controls, consent=True, consent_choice=True)
        if role == "marketing" and not control.required:
            return None
        manual_statement = role in {"consent", "marketing"} or _LEGAL_RE.search(blob) or attestation_manual(control)
        if manual_statement:
            # An optional attestation that the employer did not mark required stays
            # unchecked and does not become a blocker. Required ones pause.
            if attestation_manual(control) and role == "factual" and not control.required:
                return None
            return _Ask(controls=controls, consent=True)
    if control.kind == "buttons":
        text = _question_text(controls)
        options = _option_labels(controls)
        match = classify_question(text, options, hook=hook)
        return _Ask(controls=controls, match=match, include_question=True)
    if control.kind == "file" or any(item.kind == "file" for item in controls):
        if _is_application_resume(control):
            return _Ask(controls=controls, field_key="resume.file")
        return None
    if kind != "multi" and _is_location_typeahead(control):
        return _Ask(controls=controls, field_key="location", compose_location=True)
    text = _question_text(controls if kind == "multi" else [control])
    options = _option_labels(controls)
    match = classify_question(text, options, hook=hook)
    if match.intent == "REFERRAL":
        return _Ask(controls=controls, match=match, include_question=True)
    internal = None if kind == "multi" else match_field_key(control)
    if kind != "multi" and _prefer_field(control, internal, match):
        key = _protocol_key(internal or "", heading, control)
        if key:
            return _Ask(controls=controls, field_key=key, subfield=_subfield(control, heading))
    if match.intent is not None:
        return _Ask(controls=controls, match=match, include_question=True)
    if kind == "multi":
        if not text or _is_section_heading(text):
            return None
        return _Ask(controls=controls, unknown=True, include_question=True)
    key = _protocol_key(internal or "", heading, control)
    if key:
        return _Ask(controls=controls, field_key=key, subfield=_subfield(control, heading))
    if control.kind in {"checkbox", "textarea"} and (not text or _is_section_heading(text)):
        return None
    if control.kind in {"radio", "select", "combobox", "checkbox", "textarea"} or "?" in control.label:
        if _is_section_heading(text):
            return None
        return _Ask(controls=controls, unknown=True, include_question=True)
    if _required_question(control) and _question_text(controls):
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
    work = mentions_work_history(control)
    if internal in {"current_company", "current_job_title"} or work or (
        "start date" in blob and "employ" in head
    ):
        if internal in {"school", "education_level", "field_of_study"}:
            return "education[]"
        if work or internal in {"current_company", "current_job_title"} or (
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
    if mentions_work_history(control):
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


def _is_application_resume(control: Control) -> bool:
    """The application Resume/CV input. The Autofill-from-resume widget is not it."""
    blob = haystack(control)
    if "autofill" in blob:
        return False
    return bool(re.search(r"\b(?:resume|cv|curriculum vitae)\b", blob))


def _is_location_typeahead(control: Control) -> bool:
    """A place box that commits only after a suggestion is chosen.

    Lever's "Current location" is one. Ashby's payroll location is a combobox
    whose label asks where you plan on working from.
    """
    if control.kind not in {"text", "textarea", "combobox"}:
        return False
    raw = " ".join(part for part in (control.label, control.aria_label, control.placeholder) if part)
    folded = raw.casefold()
    if any(token in folded for token in ("relocat", "sponsor", "visa")):
        return False
    if _is_current_location(control):
        return True
    if control.kind != "combobox":
        return False
    return bool(re.search(r"\b(working from|payroll tax)\b", normalize(raw)))


def _is_current_location(control: Control) -> bool:
    """A single location box, not a relocation question and not a longer question.

    "Current location" is the Lever autocomplete. A sponsorship question that
    merely mentions "your current location" is still a question.
    """
    if control.kind not in {"text", "textarea", "combobox"}:
        return False
    raw = " ".join(part for part in (control.label, control.aria_label, control.placeholder) if part)
    if not raw or "relocat" in raw.casefold() or "?" in raw:
        return False
    blob = normalize(raw)
    if blob in {"location", "your location", "current location", "your current location"}:
        return True
    if "current location" in blob and len(blob) <= 32 and "sponsor" not in blob and "visa" not in blob:
        return True
    return False


def _requested_field_keys(requests: list[_Ask]) -> list[str]:
    keys: list[str] = []
    for ask in requests:
        if not ask.field_key or ask.include_question:
            continue
        if ask.compose_location:
            keys.extend(_LOCATION_KEYS)
        else:
            keys.append(ask.field_key)
            if ask.field_key == "phone.country":
                keys.extend(("phone", "address.country"))
            # The client drops a key that was not requested. Greenhouse asks for
            # the parts and often receives only fullName. Ashby and Lever ask
            # for fullName and receive the parts only when those keys are asked.
            if ask.field_key in {"firstName", "lastName"}:
                keys.append("fullName")
            elif ask.field_key in {"fullName", "legalName"}:
                keys.extend(("firstName", "lastName", "fullName"))
    return _unique(keys)


def _compose_location(fields: dict) -> str:
    """A location string, or city, region, and country joined when that is what came back."""
    city, region, country = _location_parts(fields)
    parts = [part for part in (city, region, country) if part]
    return ", ".join(parts)


_STATES = {
    "al": "alabama",
    "ak": "alaska",
    "az": "arizona",
    "ar": "arkansas",
    "ca": "california",
    "co": "colorado",
    "ct": "connecticut",
    "de": "delaware",
    "dc": "district of columbia",
    "fl": "florida",
    "ga": "georgia",
    "hi": "hawaii",
    "id": "idaho",
    "il": "illinois",
    "in": "indiana",
    "ia": "iowa",
    "ks": "kansas",
    "ky": "kentucky",
    "la": "louisiana",
    "me": "maine",
    "md": "maryland",
    "ma": "massachusetts",
    "mi": "michigan",
    "mn": "minnesota",
    "ms": "mississippi",
    "mo": "missouri",
    "mt": "montana",
    "ne": "nebraska",
    "nv": "nevada",
    "nh": "new hampshire",
    "nj": "new jersey",
    "nm": "new mexico",
    "ny": "new york",
    "nc": "north carolina",
    "nd": "north dakota",
    "oh": "ohio",
    "ok": "oklahoma",
    "or": "oregon",
    "pa": "pennsylvania",
    "ri": "rhode island",
    "sc": "south carolina",
    "sd": "south dakota",
    "tn": "tennessee",
    "tx": "texas",
    "ut": "utah",
    "vt": "vermont",
    "va": "virginia",
    "wa": "washington",
    "wv": "west virginia",
    "wi": "wisconsin",
    "wy": "wyoming",
}
_COUNTRIES = {
    "us": "united states",
    "usa": "united states",
    "united states of america": "united states",
    "uk": "united kingdom",
    "gb": "united kingdom",
    "great britain": "united kingdom",
}
# City nicknames only. A bare "ny" is the state, not New York City.
_CITY_ALIASES = {
    "nyc": ("new york", "new york city"),
    "new york": ("nyc", "new york city"),
    "new york city": ("nyc", "new york"),
}

_REGION_LABELS = frozenset({"state", "province", "region", "state province", "province state"})


def _is_region_control(control: Control) -> bool:
    """An address state or province menu. A longer question that mentions a state is not one."""
    if control.kind not in {"select", "combobox"}:
        return False
    label = normalize(control.label or control.aria_label or "")
    return label in _REGION_LABELS


def _region_names(value: str) -> set[str]:
    """Abbreviation and full name from ``_STATES``. ``CA`` and ``California`` are one state."""
    token = normalize(value)
    if not token:
        return set()
    names = {token}
    canonical = _STATES.get(token, "")
    if not canonical:
        for short, name in _STATES.items():
            if token == name:
                canonical = name
                names.add(short)
                break
    if canonical:
        names.add(canonical)
        for short, name in _STATES.items():
            if name == canonical:
                names.add(short)
                names.add(name)
    return {item for item in names if item}


def match_region_option(desired: str, options: list[Option]) -> Option | None:
    """Match a state or province label using the full state table."""
    names = _region_names(desired)
    if not names:
        return None
    for option in options:
        if normalize(option.label) in names or normalize(option.value) in names:
            return option
    return None


def _dependent_region_pending(snapshot: PageSnapshot) -> bool:
    for control in snapshot.controls:
        if control.hidden or not _is_region_control(control):
            continue
        if control.disabled or not control.options:
            return True
    return False


def _find_region_control(snapshot: PageSnapshot) -> Control | None:
    found = [control for control in snapshot.controls if _is_region_control(control) and not control.hidden]
    if not found:
        return None
    for control in found:
        if control.disabled or not control.options:
            return control
    return found[0]


def _region_text(fields: dict) -> str:
    return str(fields.get("address.region") or fields.get("address.state") or "").strip()


def _country_was_set(outcome: ResolvedPage) -> bool:
    return any(item.action == "select" and item.key == "address.country" for item in outcome.fields)


def _drop_manual_selector(outcome: ResolvedPage, selector: str) -> None:
    before = len(outcome.manual_questions)
    outcome.manual_questions = [
        item for item in outcome.manual_questions if str(item.get("selector") or "") != selector
    ]
    removed = before - len(outcome.manual_questions)
    if not removed:
        return
    kept: list[str] = []
    for line in outcome.manual_actions:
        if removed and line.endswith("manual answer required"):
            removed -= 1
            continue
        kept.append(line)
    outcome.manual_actions = kept


def _fill_region_after_country(page, response: ResolverResponse, outcome: ResolvedPage) -> None:
    """Choose the state after the country select has enabled it or replaced its options.

    The first pass skips a disabled state menu and cannot see options that do
    not exist yet. This waits, then writes once. A state that already holds a
    value is left alone.
    """
    if page is None or not _country_was_set(outcome):
        return
    region = _region_text(response.fields)
    if not region:
        return
    try:
        snapshot = extract_page(page)
    except Exception:
        return
    control = _find_region_control(snapshot)
    if control is None:
        return
    if not control.disabled and control.selector and control_committed(page, control.selector):
        return
    deadline = time.monotonic() + 2.0
    option: Option | None = None
    while True:
        if not control.disabled:
            option = match_region_option(region, control.options)
            if option is not None:
                break
        if time.monotonic() >= deadline or getattr(page, "wait_for_timeout", None) is None:
            option = match_region_option(region, control.options) if not control.disabled else None
            break
        _pause(page, 100)
        try:
            snapshot = extract_page(page)
        except Exception:
            return
        nxt = _find_region_control(snapshot)
        if nxt is not None:
            control = nxt
    if control.disabled or option is None:
        return
    if control.selector and control_committed(page, control.selector):
        return
    mapped = MappedField(
        key="address.region",
        action="select",
        text=region,
        option_label=option.label,
        option_value=option.value,
        option_selector=option.selector,
        field_class="PROFILE_FIELD",
        confidence="high",
    )
    written = apply_mapped(page, control, mapped)
    outcome.fields.append(written)
    if written.action == "select":
        _drop_manual_selector(outcome, control.selector)


_LOCATION_OPTIONS_JS = r"""
(selector) => {
  const el = document.querySelector(selector);
  if (!el) return [];
  function visible(node) {
    for (let current = node; current && current.nodeType === 1; current = current.parentElement) {
      if (current.hidden) return false;
      const style = window.getComputedStyle(current);
      if (style.display === "none" || style.visibility === "hidden") return false;
    }
    return !!node;
  }
  function textOf(node) {
    return (node.innerText || node.textContent || "").replace(/\s+/g, " ").trim();
  }
  function isSuggestion(node) {
    if (!node || node === el || node.nodeType !== 1) return false;
    if (node.contains && node.contains(el)) return false;
    const role = (node.getAttribute("role") || "").toLowerCase();
    if (role === "option") return true;
    if (node.classList && node.classList.contains("dropdown-location")) return true;
    const id = node.id || "";
    return id.indexOf("location-") === 0;
  }
  function pack(nodes) {
    const found = [];
    for (const option of nodes) {
      if (!isSuggestion(option) || !visible(option)) continue;
      const label = textOf(option);
      if (!label) continue;
      if (!option.id) option.setAttribute("data-autofill-option", String(found.length));
      const itemSelector = option.id
        ? "#" + CSS.escape(option.id)
        : "[data-autofill-option='" + option.getAttribute("data-autofill-option") + "']";
      found.push({ label, selector: itemSelector });
    }
    return found;
  }
  const listId = el.getAttribute("aria-controls") || el.getAttribute("aria-owns") || "";
  if (listId) {
    const list = document.getElementById(listId);
    const owned = list ? pack(list.querySelectorAll("[role='option']")) : [];
    if (owned.length) return owned;
  }
  let root = el.parentElement;
  for (let depth = 0; root && depth < 6; depth += 1, root = root.parentElement) {
    const candidates = [];
    const selector = "[role='option'], .dropdown-location, [id^='location-']";
    for (const option of root.querySelectorAll(selector)) {
      if (!isSuggestion(option) || !visible(option)) continue;
      candidates.push(option);
    }
    const found = [];
    for (const option of candidates) {
      let nested = false;
      for (const other of candidates) {
        if (other !== option && option.contains(other)) nested = true;
      }
      if (nested) continue;
      const label = textOf(option);
      if (!label) continue;
      if (!option.id) option.setAttribute("data-autofill-option", String(found.length));
      const itemSelector = option.id
        ? "#" + CSS.escape(option.id)
        : "[data-autofill-option='" + option.getAttribute("data-autofill-option") + "']";
      found.push({ label, selector: itemSelector });
    }
    if (found.length) return found;
  }
  // Lever renders the geocoder list on document.body, outside the field.
  const portal = [];
  const extra = document.querySelectorAll(
    ".dropdown-location, [role='option'][id^='location-'], .ashby-application-form-input-autocomplete-popup-result"
  );
  for (const option of extra) {
    if (!isSuggestion(option) || !visible(option)) continue;
    const label = textOf(option);
    if (!label) continue;
    if (!option.id) option.setAttribute("data-autofill-option", String(portal.length));
    const itemSelector = option.id
      ? "#" + CSS.escape(option.id)
      : "[data-autofill-option='" + option.getAttribute("data-autofill-option") + "']";
    portal.push({ label, selector: itemSelector });
  }
  return portal;
}
"""
_SELECTED_LOCATION_JS = """
() => {
  const named = document.querySelector("input[name='selectedLocation'], input[name='selected_location']");
  return named ? String(named.value || "") : "";
}
"""


def _location_parts(fields: dict) -> tuple[str, str, str]:
    city = str(fields.get("address.city") or "").strip()
    region = str(fields.get("address.region") or fields.get("address.state") or "").strip()
    country = str(fields.get("address.country") or "").strip()
    raw = fields.get("location")
    if isinstance(raw, str) and raw.strip():
        pieces = [part.strip() for part in raw.split(",") if part.strip()]
        if not city and pieces:
            city = pieces[0]
        if not region and len(pieces) > 1:
            region = pieces[1]
        if not country and len(pieces) > 2:
            country = pieces[2]
    return city, region, country


def _with_profile_place(region: str, country: str) -> tuple[str, str]:
    """Fill a missing state or country from the profile. Never replace one the resolver sent."""
    profile = _LOCATION_PROFILE.get()
    personal = getattr(profile, "personal", None)
    if personal is None:
        return region, country
    if not region:
        region = str(getattr(personal, "province_state", "") or "").strip()
    if not country:
        country = str(getattr(personal, "country", "") or "").strip()
    return region, country


def _city_names(city: str) -> list[str]:
    token = normalize(city)
    if not token:
        return []
    names = [token]
    for alias in _CITY_ALIASES.get(token, ()):
        if alias not in names:
            names.append(alias)
    return names


def _city_in_text(text: str, city: str) -> bool:
    folded = normalize(text)
    return any(_phrase_in(folded, name) for name in _city_names(city))


def _phrase_in(folded: str, phrase: str) -> bool:
    token = normalize(phrase)
    if not token:
        return False
    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", folded) is not None


def _alias_phrases(value: str, table: dict[str, str]) -> list[str]:
    """Abbreviation and full name for one state or country, including USA for US."""
    token = normalize(value)
    if not token:
        return []
    canonical = table.get(token, "")
    if not canonical:
        for name in table.values():
            if token == name:
                canonical = name
                break
    phrases = [token]
    if not canonical:
        return phrases
    phrases.append(canonical)
    for short, name in table.items():
        if name == canonical:
            phrases.append(short)
            phrases.append(name)
    return phrases


def _alias_in(folded: str, value: str, table: dict[str, str]) -> bool:
    seen: set[str] = set()
    for phrase in _alias_phrases(value, table):
        if phrase in seen:
            continue
        seen.add(phrase)
        if _phrase_in(folded, phrase):
            return True
    return False


def _same_city(label: str, city: str) -> bool:
    """True when the suggestion's city equals ``city``, ignoring case."""
    names = _city_names(city)
    if not names:
        return False
    head = normalize(label.split(",")[0])
    if head in names:
        return True
    return _city_in_text(label, city)


def _label_tail(label: str) -> str:
    """The place after the city. The state name must be here, not inside the city."""
    pieces = [part.strip() for part in label.split(",") if part.strip()]
    if len(pieces) < 2:
        return ""
    return normalize(", ".join(pieces[1:]))


def _place_agrees(label: str, city: str, region: str, country: str) -> bool:
    """The shown place is the city, and the state and country when those are known."""
    if not _same_city(label, city):
        return False
    scope = _label_tail(label) or normalize(label)
    if region and not _alias_in(scope, region, _STATES):
        return False
    if country and not _alias_in(scope, country, _COUNTRIES):
        return False
    return True


def _prefer_same_place(labels: list[str], city: str) -> str | None:
    """One label when every hit is the same city with the same state and country.

    ``NYC, NY, USA`` and ``New York, NY, USA`` are that place. Two identical
    labels, or two different states, are not.
    """
    unique: list[str] = []
    seen: set[str] = set()
    for label in labels:
        key = normalize(label)
        if key in seen:
            continue
        seen.add(key)
        unique.append(label)
    # Two copies of one label are not a choice. Different wording of one place is.
    if len(unique) == 1:
        return None
    labels = unique

    def rank(label: str) -> int:
        head = normalize(label.split(",")[0])
        if head not in _city_names(city):
            return 0
        return len(head)

    best = max(rank(label) for label in labels)
    if best <= 0:
        return None
    top = [label for label in labels if rank(label) == best]
    if len(top) == 1:
        return top[0]
    return None


def choose_location_label(labels: list[str], *, city: str, region: str, country: str) -> str | None:
    """One suggestion that matches the city and, when present, the region and country."""
    winners: list[tuple[int, str]] = []
    for label in labels:
        if not _place_agrees(label, city, region, country):
            continue
        score = 4 + (2 if region else 0) + (1 if country else 0)
        winners.append((score, label))
    if not winners:
        return None
    best = max(score for score, _label in winners)
    chosen = [label for score, label in winners if score == best]
    if len(chosen) == 1:
        return chosen[0]
    return _prefer_same_place(chosen, city)


def _location_options(page, selector: str) -> list[tuple[str, str]]:
    try:
        raw = page.evaluate(_LOCATION_OPTIONS_JS, selector) or []
    except Exception:
        return []
    found: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        item_selector = str(item.get("selector") or "").strip()
        if label and item_selector:
            found.append((label, item_selector))
    return found


def _selected_location(page) -> str:
    try:
        return str(page.evaluate(_SELECTED_LOCATION_JS) or "").strip()
    except Exception:
        return ""


def _manual_location(outcome: ResolvedPage, control: Control, labels: list[str] | None = None) -> None:
    text = sanitize_question(control.label or control.aria_label or "Current location")
    _manual(
        outcome,
        IntentMatch(None, "unknown", "none", text),
        blocking=True,
        control=control,
        kind="MISSING_PROFILE_VALUE",
        field_key="address.city",
        option_labels=labels,
    )


def _pause(page, millis: int) -> None:
    pause = getattr(page, "wait_for_timeout", None)
    if pause is None:
        time.sleep(millis / 1000)
    else:
        pause(millis)


def _location_suggestion_log(labels: list[str]) -> str:
    """City and region text only. Street numbers and emails stay out of the log."""
    kept: list[str] = []
    for label in labels:
        if "@" in label:
            continue
        parts: list[str] = []
        for part in label.split(","):
            piece = part.strip()
            if not piece or "@" in piece or re.search(r"\d", piece):
                continue
            parts.append(piece)
        if parts:
            kept.append(", ".join(parts))
    return " | ".join(kept[:12])


def _poll_location_options(page, selector: str, attempts: int, delay_ms: int) -> list[tuple[str, str]]:
    options: list[tuple[str, str]] = []
    for _ in range(attempts):
        options = _location_options(page, selector)
        if options:
            return options
        _pause(page, delay_ms)
    return options


_LOCATION_SUGGESTION_WAIT_S = 5.0


def _await_location_options(page, selector: str, trace: dict[str, Any]) -> list[tuple[str, str]]:
    """Wait until the suggestion list is visible, or the search has failed, up to 5 seconds.

    A debounced geocoder request is not an empty list. A 4xx stops the wait.
    An empty list counts only after the deadline.
    """
    deadline = time.monotonic() + _LOCATION_SUGGESTION_WAIT_S
    options: list[tuple[str, str]] = []
    while True:
        status = trace.get("status")
        if isinstance(status, int) and status >= 400:
            return []
        options = _location_options(page, selector)
        if options:
            return options
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        _pause(page, min(100, max(1, int(remaining * 1000))))
    status = trace.get("status")
    if isinstance(status, int) and status >= 400:
        return []
    return _location_options(page, selector) or options


_CONTROL_KIND_JS = r"""
(selector) => {
  const el = document.querySelector(selector);
  if (!el) return "";
  const tag = String(el.tagName || "").toLowerCase();
  const type = String(el.getAttribute("type") || el.type || "").toLowerCase();
  if (tag === "select") return "select";
  if (type === "radio" || type === "checkbox") return type;
  return "combobox";
}
"""


def suggest_control(page, selector: str, query: str, *, clear: bool) -> dict[str, Any]:
    """Type ``query`` into a combobox and return the list without choosing one.

    A native select, radio, or checkbox group returns its labels as ``static``
    and is not typed into. Typed text is cleared unless the place was already
    committed before the probe.
    """
    if not selector:
        return {"found": False, "options": [], "source": "live"}
    evaluate = getattr(page, "evaluate", None)
    kind = ""
    if evaluate is not None:
        try:
            kind = str(evaluate(_CONTROL_KIND_JS, selector) or "")
        except Exception:
            kind = ""
        if not kind:
            return {"found": False, "options": [], "source": "live"}
    if kind in {"select", "radio", "checkbox"}:
        from autofill.dom_ui import read_choice_options

        labels = [label for label in read_choice_options(page, selector) if _keep_option(label)]
        return {
            "found": True,
            "options": [{"label": label} for label in labels],
            "source": "static",
        }
    control = Control(kind="combobox", label="Location", selector=selector)
    already = False
    if evaluate is not None:
        try:
            already = _typeahead_place_committed(page, control)
        except Exception:
            already = False
    field = page.locator(selector)
    typed = False
    seen: list[str] = []
    try:
        if query.strip() and not already:
            try:
                field.fill("")
            except Exception:
                return {"found": False, "options": [], "source": "live"}
            field.press_sequentially(query.strip(), delay=0)
            typed = True
        trace: dict[str, Any] = {}
        listener = _watch_location_search(page, trace)
        try:
            found = _await_location_options(page, selector, trace)
        finally:
            if listener is not None:
                remove = getattr(page, "remove_listener", None)
                if remove is not None:
                    try:
                        remove("response", listener)
                    except Exception:
                        pass
        for label, _item_selector in found:
            if label not in seen and _keep_option(label):
                seen.append(label)
    except Exception:
        if not typed and evaluate is None:
            return {"found": True, "options": [], "source": "live"}
        return {"found": False, "options": [], "source": "live"}
    if typed and (clear or not already):
        try:
            field.fill("")
        except Exception:
            pass
        try:
            dismiss_combobox(page)
        except Exception:
            pass
    return {"found": True, "options": [{"label": label} for label in seen], "source": "live"}


def _match_location(
    options: list[tuple[str, str]], *, city: str, region: str, country: str
) -> tuple[str, str] | None:
    label = choose_location_label([item[0] for item in options], city=city, region=region, country=country)
    if not label:
        return None
    return next((item for item in options if item[0] == label), None)


def _title_place(value: str) -> str:
    parts: list[str] = []
    for word in value.split():
        if word.isalpha() and len(word) <= 2:
            parts.append(word.upper())
        else:
            parts.append(word[:1].upper() + word[1:])
    return " ".join(parts)


def _place_name(value: str, table: dict[str, str]) -> str:
    token = normalize(value)
    if not token:
        return ""
    canonical = table.get(token, "")
    if not canonical:
        for name in table.values():
            if token == name:
                canonical = name
                break
    if canonical:
        return _title_place(canonical)
    return value.strip()


def _location_free_text(city: str, region: str, country: str) -> str:
    parts = [city.strip()]
    region_name = _place_name(region, _STATES)
    country_name = _place_name(country, _COUNTRIES)
    if region_name:
        parts.append(region_name)
    if country_name:
        parts.append(country_name)
    return ", ".join(part for part in parts if part)


def _keep_enter_from_submitting(page) -> None:
    """Enter confirms a list choice. It must not submit the application."""
    try:
        page.evaluate(_BLOCK_SUBMIT_JS)
    except Exception:
        return


_BLOCK_SUBMIT_JS = """
() => {
  if (window.__autofillBlockSubmit) return;
  window.__autofillBlockSubmit = true;
  document.addEventListener("submit", (event) => event.preventDefault(), true);
}
"""


def _wait_location_network(page) -> None:
    wait = getattr(page, "wait_for_load_state", None)
    if wait is None:
        return
    try:
        wait("networkidle", timeout=2500)
    except Exception:
        return


def _location_pick(page) -> dict[str, Any] | None:
    """The picked Lever place, or None.

    A resume parse writes ``{"name":""}`` into the hidden input while the box
    is still empty. That object has no suggestion id, so it is not a commit.
    """
    raw = _selected_location(page).strip()
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    if not str(parsed.get("id") or "").strip():
        return None
    return parsed


def _place_text_committed(shown: str, city: str, *, require_full: bool) -> bool:
    """A visible place matches the city. A bare city, or an empty proxy, does not."""
    folded = normalize(shown)
    names = _city_names(city)
    if not folded or not names or folded in names:
        return False
    if require_full and not _full_place(shown):
        return False
    return _city_in_text(shown, city)


def _location_committed(page, control: Control, city: str) -> bool:
    """True when a suggestion was recorded, or the visible control shows the place.

    Lever keeps the chosen place in a hidden ``selectedLocation`` input as JSON
    with an id. That proxy is often empty after the box already shows
    ``Austin, TX, USA``. A bare typed city is still not a commit. Ashby shows
    the chosen place on the combobox itself and closes the list.
    """
    proxy_empty = False
    if _has_selected_location(page):
        pick = _location_pick(page)
        if pick is not None:
            return _city_in_text(str(pick.get("name") or ""), city)
        proxy_empty = True
    if getattr(page, "evaluate", None) is None:
        try:
            typed = str(page.locator(control.selector).input_value() or "")
        except Exception:
            typed = ""
        return _place_text_committed(typed, city, require_full=proxy_empty)
    if not _settle_rendered_place(page, control):
        shown = live_field_value(page, control) or ""
        return _place_text_committed(shown, city, require_full=True)
    shown = str(_read_typeahead_state(page, control.selector).get("shown") or "")
    if not _full_place(shown):
        shown = live_field_value(page, control) or shown
    return _place_text_committed(shown, city, require_full=proxy_empty)


def _has_selected_location(page) -> bool:
    try:
        return bool(page.evaluate(_HAS_SELECTED_LOCATION_JS))
    except Exception:
        return False


_HAS_SELECTED_LOCATION_JS = """
() => !!document.querySelector("input[name='selectedLocation'], input[name='selected_location']")
"""

_WRITE_LOCATION_JS = """
({ selector, text }) => {
  const el = document.querySelector(selector);
  if (el) {
    el.value = text;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }
  const hidden = document.querySelector("input[name='selectedLocation'], input[name='selected_location']");
  if (hidden) {
    hidden.value = text;
    hidden.dispatchEvent(new Event("input", { bubbles: true }));
    hidden.dispatchEvent(new Event("change", { bubbles: true }));
  }
}
"""


def _city_hits(options: list[tuple[str, str]], city: str) -> int:
    return sum(1 for label, _selector in options if _same_city(label, city))


def _remember_location(outcome: ResolvedPage, control: Control, detail: str) -> None:
    outcome.fields.append(
        FieldOutcome(
            selector=control.selector,
            label=control.label,
            key="location",
            action="select",
            detail=detail,
            field_class="PROFILE_FIELD",
        )
    )


def _phone_country_value(fields: dict) -> str:
    """Country for a calling-code menu: an explicit country, else the phone's code."""
    for key in ("phone.country", "address.country"):
        text = str(fields.get(key) or "").strip()
        if text:
            return text
    from autofill.combobox import country_from_phone

    return country_from_phone(str(fields.get("phone") or ""))


def _apply_phone_country(page, control: Control, fields: dict, outcome: ResolvedPage) -> None:
    """Choose the calling-code row. This is not an address country field."""
    text = _phone_country_value(fields)
    if not text:
        _manual(
            outcome,
            IntentMatch(None, "unknown", "none", _manual_label(control)),
            blocking=True,
            control=control,
            kind="MISSING_PROFILE_VALUE",
            field_key="phone.country",
        )
        return
    from autofill.mapping import match_option

    option = match_option(text, control.options) if control.options else None
    if option is None and control.kind == "combobox":
        picked = select_combobox(page, control.selector, text)
        if picked is None or not control_committed(page, control.selector):
            _manual(
                outcome,
                IntentMatch(None, "unknown", "none", _manual_label(control)),
                blocking=True,
                control=control,
                kind="OPTION_MISMATCH",
                field_key="phone.country",
            )
            return
        outcome.fields.append(
            FieldOutcome(
                selector=control.selector,
                label=control.label,
                key="phone.country",
                action="select",
                detail=picked.label,
                field_class="PROFILE_FIELD",
            )
        )
        return
    if option is None:
        _manual(
            outcome,
            IntentMatch(None, "unknown", "none", _manual_label(control)),
            blocking=True,
            control=control,
            kind="OPTION_MISMATCH",
            field_key="phone.country",
        )
        return
    mapped = MappedField(
        key="phone.country",
        action="select",
        text=text,
        option_label=option.label,
        option_value=option.value,
        option_selector=option.selector,
        field_class="PROFILE_FIELD",
    )
    written = apply_mapped(page, control, mapped)
    outcome.fields.append(written)
    if written.action != "select" or not control_committed(page, control.selector):
        _manual(
            outcome,
            IntentMatch(None, "unknown", "none", _manual_label(control)),
            blocking=True,
            control=control,
            kind="MISSING_PROFILE_VALUE",
            field_key="phone.country",
        )


_LOCATION_ANSWER_KEYS = frozenset({"location", "address.city", "city"})
_LOCATION_LABELS = frozenset({"location", "your location", "current location", "your current location"})
_STATE_NAMES = frozenset(_STATES) | frozenset(_STATES.values())
_CITY_LOCATION_PHRASES = (
    "currently located",
    "where are you located",
    "current location",
    "working from",
    "payroll",
    "where do you live",
)


def location_shape(value: str) -> str:
    """``city``, ``state``, or ``place`` for one location answer.

    A comma and a second part is a full place. A state name or abbreviation
    alone is a state. Anything else is a city.
    """
    text = value.strip()
    if not text:
        return "city"
    pieces = [part.strip() for part in text.split(",") if part.strip()]
    if len(pieces) >= 2 and any(character.isalpha() for character in pieces[1]):
        # "Florida, United States" is a state, not a city. "Austin, TX" is a place.
        if len(pieces) == 2 and _is_state_token(pieces[0]) and _is_country_token(pieces[1]):
            return "state"
        return "place"
    token = normalize(text)
    if token in _STATE_NAMES:
        return "state"
    return "city"


def _is_state_token(value: str) -> bool:
    return normalize(value) in _STATE_NAMES


def _is_country_token(value: str) -> bool:
    token = normalize(value)
    if not token:
        return False
    if token in _COUNTRIES or token in set(_COUNTRIES.values()):
        return True
    return token in {"united states", "united kingdom", "canada", "mexico", "uruguay"}


def pickable_location_labels(target: dict, labels: list[str]) -> list[str]:
    """Labels a person can commit. A state alone is not a city on a place field."""
    cleaned = [label.strip() for label in labels if label.strip()]
    if not location_needs_city(target):
        return cleaned
    return [label for label in cleaned if location_shape(label) != "state"]


def location_needs_city(target: dict) -> bool:
    """True when the control is a place typeahead, not a state menu."""
    text = str(target.get("text") or "").strip().casefold()
    key = str(target.get("fieldKey") or "").strip()
    label_hit = any(phrase in text for phrase in _CITY_LOCATION_PHRASES) or text in _LOCATION_LABELS
    options = [str(option).strip() for option in target.get("options") or [] if str(option).strip()]
    place_menu = any("," in option for option in options)
    if options and not place_menu and all(location_shape(option) == "state" for option in options):
        return False
    if label_hit or key == "location" or place_menu:
        return True
    return key in _LOCATION_ANSWER_KEYS and place_menu


def is_location_answer(page, target: dict) -> bool:
    """A current-location box, or a city field backed by a location suggestion input."""
    key = str(target.get("fieldKey") or "").strip()
    text = str(target.get("text") or "").strip().casefold()
    label_hit = text in _LOCATION_LABELS or (
        "current location" in text and len(text) <= 40 and "sponsor" not in text and "visa" not in text
    ) or ("working from" in text and "sponsor" not in text and "visa" not in text)
    if key == "location" or label_hit:
        return True
    return key in _LOCATION_ANSWER_KEYS and _has_selected_location(page)


def commit_location_answer(page, target: dict) -> dict[str, Any]:
    """Type the city and keep a suggestion. A bare typed city is not a commit.

    A state with no city is not a commit on a place field. The suggestion
    labels are returned so the caller can ask for one of them.
    """
    value = str(target.get("value") or "").strip()
    if not value:
        values = [str(item).strip() for item in target.get("values") or [] if str(item).strip()]
        value = values[0] if values else ""
    selector = str(target.get("selector") or "")
    if not selector:
        return {"filled": False, "reason": "no_selector"}
    if not value:
        return {"filled": False, "reason": "empty"}
    if location_shape(value) == "state" and location_needs_city(target):
        probed = suggest_control(page, selector, value, clear=True)
        labels = [str(item.get("label") or "") for item in probed.get("options") or [] if str(item.get("label") or "")]
        return {
            "filled": False,
            "reason": "needs_option",
            "options": pickable_location_labels(target, labels),
        }
    fields = {"location": value, "address.city": value.split(",")[0].strip()}
    control = Control(kind="combobox", label=str(target.get("text") or "Current location"), selector=selector)
    outcome = ResolvedPage()
    trace: dict[str, Any] = {}
    # The caller named a city. When several places share it, keep the first
    # suggestion the geocoder ranked. A profile fill with no region still asks.
    trace["exact"] = value if location_shape(value) == "place" else ""
    # Several places can share a city. The first geocoder row is kept only when
    # no state or country is known. A known place never takes an unmatched row.
    _apply_location(page, control, fields, outcome, ranked=True, trace=trace)
    city, _region, _country = _location_parts(fields)
    labels = pickable_location_labels(
        target, [str(label) for label in trace.get("labels") or [] if str(label).strip()]
    )
    if city and _location_committed(page, control, city):
        committed: dict[str, Any] = {"filled": True}
        if labels:
            committed["options"] = labels
        return committed
    result: dict[str, Any] = {
        "filled": False,
        "reason": "not_committed",
        "suggestionCount": int(trace.get("count") or 0),
    }
    if labels:
        result["options"] = labels
    if trace.get("lever"):
        result["leverSearch"] = True
        result["suggestionsWaited"] = True
    if trace.get("status") is not None:
        result["searchStatus"] = trace.get("status")
    return result


def _watch_location_search(page, trace: dict[str, Any]):
    """Remember the Lever suggestion response. The city text is not logged."""
    try:
        url = str(getattr(page, "url", "") or "")
    except Exception:
        url = ""
    if "lever.co" in url:
        trace["lever"] = True

    def on_response(response) -> None:
        try:
            target = str(getattr(response, "url", "") or "")
        except Exception:
            return
        if "searchLocations" not in target:
            return
        trace["lever"] = True
        try:
            trace["status"] = int(response.status)
        except Exception:
            return

    listen = getattr(page, "on", None)
    if listen is None:
        return None
    try:
        listen("response", on_response)
    except Exception:
        return None
    return on_response


def _apply_location(
    page,
    control: Control,
    fields: dict,
    outcome: ResolvedPage,
    *,
    ranked: bool = False,
    trace: dict[str, Any] | None = None,
) -> None:
    """Type the city and keep a suggestion. A typed value that the list does not commit stays manual."""
    city, region, country = _location_parts(fields)
    region, country = _with_profile_place(region, country)
    if not city:
        # The resolver sent no city and no location string. Say so: this is a
        # profile gap, not a picker failure, and it was silent before.
        logger.info(
            "location left manual reason=no_city has_region=%s has_country=%s has_location=%s",
            bool(region),
            bool(country),
            bool(str(fields.get("location") or "").strip()),
        )
        _manual_location(outcome, control)
        return
    field = page.locator(control.selector)
    delay = type_char_delay()
    if trace is None:
        trace = {}
    listener = _watch_location_search(page, trace)
    try:
        _apply_location_query(
            page,
            control,
            outcome,
            field,
            city,
            region,
            country,
            delay,
            ranked=ranked,
            trace=trace,
        )
    finally:
        if listener is not None:
            remove = getattr(page, "remove_listener", None)
            if remove is not None:
                try:
                    remove("response", listener)
                except Exception:
                    pass


def _apply_location_query(
    page,
    control: Control,
    outcome: ResolvedPage,
    field,
    city: str,
    region: str,
    country: str,
    delay: int,
    *,
    ranked: bool,
    trace: dict[str, Any],
) -> None:
    try:
        field.fill("")
    except Exception as exc:
        logger.info("location left manual reason=type_error error=%s", type(exc).__name__)
        _manual_location(outcome, control)
        return
    typed = False
    try:
        # Real keystrokes open Lever's geocoder. Setting the value alone does not.
        field.press_sequentially(city, delay=delay)
        typed = True
    except Exception:
        typed = False
    if not typed and not type_visible(page, control.selector, city):
        logger.info("location left manual reason=type_error error=TypeError")
        _manual_location(outcome, control)
        return
    options = _await_location_options(page, control.selector, trace)
    match = _match_location(options, city=city, region=region, country=country)
    status = trace.get("status")
    if isinstance(status, int) and status >= 400:
        options = []
        match = None
    exact = str(trace.get("exact") or "").strip().casefold()
    if exact:
        for label, item_selector in options:
            if label.casefold() == exact:
                match = (label, item_selector)
                break
    trace["count"] = len(options)
    trace["labels"] = [label for label, _selector in options]
    logger.info("location suggestions status=%s count=%s", status if status is not None else "none", len(options))
    if match is None and ranked and not region and not country:
        hits = [item for item in options if _same_city(item[0], city)]
        if hits:
            match = hits[0]
            logger.info("location answer ranked=first count=%s", len(hits))
    if match is not None:
        click_choice(page, match[1], match[0])
        if not _location_committed(page, control, city) and _keyboard_may_commit(
            options, match, city, region, country
        ):
            try:
                _keep_enter_from_submitting(page)
                field.press("ArrowDown")
                field.press("Enter")
            except Exception:
                pass
            for _ in range(8):
                if _location_committed(page, control, city):
                    break
                _pause(page, 50)
        shown = _agreed_place(page, control, field, city, region, country)
        if shown:
            _remember_location(outcome, control, shown)
            return
    if not options:
        reason = "no_suggestions"
    elif match is None:
        reason = "ambiguous" if not region and _city_hits(options, city) > 1 else "no_match"
    else:
        reason = "not_committed"
    logger.info(
        "location left manual reason=%s suggestions=%s",
        reason,
        _location_suggestion_log([item[0] for item in options]),
    )
    _clear_location_entry(page, field)
    _manual_location(outcome, control, [label for label, _selector in options])


def _keyboard_may_commit(
    options: list[tuple[str, str]],
    match: tuple[str, str],
    city: str,
    region: str,
    country: str,
) -> bool:
    """Enter confirms the highlighted row, which is the first suggestion.

    That row is safe only when it is the matched place. Otherwise Enter would
    keep a different city, such as Stockholm for NYC.
    """
    if not options:
        return False
    first = options[0][0]
    if normalize(first) != normalize(match[0]):
        return False
    return _place_agrees(first, city, region, country)


def _agreed_place(page, control: Control, field, city: str, region: str, country: str) -> str:
    """The place now on the page, when it matches. The intended label is not a fallback."""
    if not _location_committed(page, control, city):
        return ""
    pick = _location_pick(page) or {}
    shown = str(pick.get("name") or "").strip()
    if not shown:
        try:
            shown = str(field.input_value() or "").strip()
        except Exception:
            shown = ""
    if shown and _place_agrees(shown, city, region, country):
        return shown
    return ""


def _clear_location_entry(page, field) -> None:
    try:
        field.fill("")
    except Exception:
        pass
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return
    try:
        evaluate(
            """() => {
              const hidden = document.querySelector(
                "input[name='selectedLocation'], input[name='selected_location']"
              );
              if (hidden) hidden.value = "";
            }"""
        )
    except Exception:
        return


def _fill_name_parts(response: ResolverResponse) -> None:
    """Put a writable name on the response before a missing key is skipped.

    A single name box is filled from first and last when that is all the
    resolver returned. Split boxes are filled from a full name when the parts
    were not returned. Nothing is logged.
    """
    first = str(response.fields.get("firstName") or "").strip()
    last = str(response.fields.get("lastName") or "").strip()
    full = str(response.fields.get("fullName") or "").strip()
    legal = str(response.fields.get("legalName") or "").strip()
    if not full:
        joined = " ".join(part for part in (first, last) if part)
        if not joined and legal:
            joined = legal
        if joined:
            response.fields["fullName"] = joined
            full = joined
    if full and not legal:
        response.fields["legalName"] = full
    if not full:
        return
    from autofill.profile import split_name

    split_first, split_last = split_name(full)
    if split_first and not first:
        response.fields["firstName"] = split_first
    if split_last and not last:
        response.fields["lastName"] = split_last


def _remember_shown(page, control: Control, key: str) -> None:
    """Record the text already on a control. The value is not logged."""
    shown = live_field_value(page, control)
    if shown is None or not str(shown).strip():
        return
    remember_confirmed(control, str(shown).strip(), field_key=key or "")


def _keep_filled(page, controls: list[Control]) -> bool:
    """True when a follow-up pass must leave this cluster alone."""
    if _cluster_committed(page, controls):
        return True
    shown = live_field_value(page, controls[0])
    return bool(shown and str(shown).strip())


def _apply(
    page,
    requests: list[_Ask],
    response: ResolverResponse,
    flags: FillFlags,
    outcome: ResolvedPage,
    heading: str,
    session_id: str = "",
    *,
    preserve_filled: bool = False,
) -> None:
    _fill_name_parts(response)
    by_intent, unnamed, by_key = _index_answers(response)
    unclassified = _unclassified_count(requests)
    rows = {
        "employment": _rows(response.fields.get("employment[]")),
        "education": _rows(response.fields.get("education[]")),
        "projects": _rows(response.fields.get("projects[]")),
    }
    employment_index = [-1]
    # A missing resume is recorded after the other controls. It must not skip them.
    ordered = [ask for ask in requests if ask.field_key != "resume.file"]
    ordered.extend(ask for ask in requests if ask.field_key == "resume.file")
    shared = (by_intent, unnamed, by_key, unclassified, rows, employment_index)
    for ask in ordered:
        if not checkpoint():
            break
        _apply_ask(
            page,
            ask,
            response,
            flags,
            outcome,
            heading,
            shared,
            preserve_filled=preserve_filled,
        )
    if _resume_was_attached(outcome):
        settle_after_resume(page)
        _refill_cleared_fields(page, ordered, response, flags, outcome, heading, shared, session_id=session_id)
        try:
            outcome.snapshot = extract_page(page)
        except Exception:
            pass


def _apply_ask(
    page,
    ask: _Ask,
    response: ResolverResponse,
    flags: FillFlags,
    outcome: ResolvedPage,
    heading: str,
    shared: tuple,
    *,
    preserve_filled: bool = False,
) -> None:
    """Write one ask. ``shared`` carries the step's answer index and repeater cursor."""
    by_intent, unnamed, by_key, unclassified, rows, employment_index = shared
    if preserve_filled and ask.controls and _keep_filled(page, ask.controls):
        control = ask.controls[0]
        key = ask.field_key or (ask.match.intent if ask.match else "") or ""
        if control.kind in {"text", "textarea", "combobox", "select"}:
            _remember_shown(page, control, key)
            outcome.fields.append(
                FieldOutcome(
                    selector=control.selector,
                    label=control.label,
                    key=key or None,
                    action="fill",
                    detail="already set",
                    field_class="PROFILE_FIELD",
                )
            )
        return
    if any(selector_held(control.selector) for control in ask.controls) and (
        not refill_cleared() or all(control_committed(page, control.selector) for control in ask.controls)
    ):
        return
    if ask.controls and ask.controls[0].kind == "buttons" and control_committed(page, ask.controls[0].selector):
        return
    if ask.field_key == "resume.file":
        _apply_resume(page, ask, response, outcome)
        return
    if ask.consent:
        required = any(control.required for control in ask.controls)
        if _cluster_committed(page, ask.controls):
            return
        if required and _accept_standard_term(page, ask):
            return
        if not required and ask.consent_choice:
            return
        statement = _consent_statement(ask.controls[0])
        _manual(
            outcome,
            IntentMatch(None, "unknown", "none", statement),
            blocking=required,
            controls=ask.controls,
            kind="CONSENT_CHOICE" if ask.consent_choice else "CONSENT",
        )
        return
    if ask.compose_location:
        _apply_location(page, ask.controls[0], response.fields, outcome)
        return
    if ask.field_key == "phone.country":
        _apply_phone_country(page, ask.controls[0], response.fields, outcome)
        return
    if ask.include_question:
        match = ask.match or IntentMatch(None, "unknown", "none", sanitize_question(_question_text(ask.controls)))
        answer = _take_answer(
            match,
            by_intent,
            unnamed,
            by_key,
            unclassified=unclassified,
            question_id=_question_digest(match.text, ask.controls[0]),
        )
        _apply_question(page, ask, answer, outcome)
        return
    if not ask.field_key:
        return
    if ask.field_key == "employment[]":
        sub = ask.subfield or _subfield(ask.controls[0], heading)
        employment_rows = rows["employment"]
        if sub == "employment.company":
            employment_index[0] += 1
        index = employment_index[0]
        row = employment_rows[index] if 0 <= index < len(employment_rows) else None
        if row is None and employment_rows:
            row = employment_rows[0]
        _apply_row_field(page, ask.controls[0], row, sub, flags, outcome)
        return
    if ask.field_key == "education[]":
        row = rows["education"][0] if rows["education"] else None
        subfield = ask.subfield or _subfield(ask.controls[0], heading)
        _apply_row_field(page, ask.controls[0], row, subfield, flags, outcome)
        return
    if ask.field_key == "projects[]":
        row = rows["projects"][0] if rows["projects"] else None
        subfield = ask.subfield or _subfield(ask.controls[0], heading)
        _apply_row_field(page, ask.controls[0], row, subfield, flags, outcome)
        return
    if ask.field_key == "skills":
        raw = response.fields.get("skills", "")
        text = ", ".join(raw) if isinstance(raw, list) else str(raw or "")
        _write_text(page, ask.controls[0], text, outcome, key=ask.field_key)
        return
    if ask.field_key not in response.fields and ask.field_key not in response.unresolved:
        _skip(ask.controls[0], outcome, ask.field_key, "Resolver did not return this key.", page=page)
        return
    raw_value = response.fields.get(ask.field_key, "")
    text = "" if raw_value is None else str(raw_value)
    # A national box beside a calling-code menu rejects "+1 …". A format alert
    # on a later pass uses the same rule, so the leading country digit is not
    # typed again as if it were part of the subscriber number.
    if ask.field_key == "phone" and (_wants_national_digits(ask.controls[0]) or flags.digits_phone):
        text = national_phone_digits(text)
    if flags.us_dates and _looks_iso(text):
        text = _force_us_date(text)
    _write_control(page, ask.controls[0], text, outcome, key=ask.field_key, flags=flags)


def _resume_was_attached(outcome: ResolvedPage) -> bool:
    return any(item.action == "upload" and item.key == "resume.file" for item in outcome.fields)


_FIELD_SIG_JS = r"""
() => {
  const parts = [];
  for (const el of document.querySelectorAll("input, textarea, select")) {
    const type = String(el.getAttribute("type") || "").toLowerCase();
    if (type === "file" || type === "hidden") continue;
    const id = el.id || el.getAttribute("name") || "";
    parts.push(id + "=" + ("value" in el ? el.value : ""));
  }
  return parts.join("\n");
}
"""


def settle_after_resume(page, *, timeout_s: float = 3.0) -> None:
    """Wait until a resume parse stops rewriting fields, then return.

    A page that does not change settles in about a second. A rewrite is
    followed until two reads match. The budget is a ceiling.
    """
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return
    try:
        started = str(evaluate(_FIELD_SIG_JS) or "")
    except Exception:
        return
    previous = started
    stable = 0
    saw_change = False
    quiet_after = time.monotonic() + 1.2
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _pause(page, 150)
        try:
            signature = str(evaluate(_FIELD_SIG_JS) or "")
        except Exception:
            return
        if signature != started:
            saw_change = True
        if signature == previous:
            stable += 1
        else:
            stable = 0
            previous = signature
        if saw_change and stable >= 2:
            return
        if not saw_change and time.monotonic() >= quiet_after and stable >= 2:
            return


def _frame_targets(page) -> list:
    frames = list(getattr(page, "frames", []) or [])
    return frames or [page]


_VISIBLE_FORM_JS = r"""() => {
  function shown(el) {
    if (!el || el.nodeType !== 1) return false;
    const type = String(el.getAttribute("type") || "").toLowerCase();
    if (type === "hidden" || type === "file" || type === "password") return false;
    if (el.hidden) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (parseFloat(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    if (rect.right <= 0) return false;
    if (rect.width < 2 && rect.height < 2) return false;
    return true;
  }
  const parts = [];
  let count = 0;
  for (const el of document.querySelectorAll("input, textarea, select")) {
    if (!shown(el)) continue;
    count += 1;
    const id = el.id || el.getAttribute("name") || "";
    const value = "value" in el ? el.value : "";
    parts.push(id + "=" + value);
  }
  return {count: count, signature: parts.join("\n")};
}"""


def _form_sample(page) -> tuple[int, str] | None:
    total = 0
    parts: list[str] = []
    saw = False
    for target in _frame_targets(page):
        evaluate = getattr(target, "evaluate", None)
        if evaluate is None:
            continue
        try:
            raw = evaluate(_VISIBLE_FORM_JS)
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        saw = True
        total += int(raw.get("count") or 0)
        parts.append(str(raw.get("signature") or ""))
    if not saw:
        return None
    return total, "\n".join(parts)


def settle_form(page, *, timeout_s: float = 12.0) -> None:
    """Wait until a resume re-render has put the visible fields back.

    The highest visible-field count is the mark. A drop below that mark keeps
    this waiting until the count returns and holds for about a second. A form
    that never changes returns in about a second. The budget is a ceiling.
    """
    sample = _form_sample(page)
    if sample is None:
        return
    high, previous = sample
    quiet_since: float | None = time.monotonic()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        _pause(page, 150)
        sample = _form_sample(page)
        if sample is None:
            return
        count, signature = sample
        if count > high:
            high = count
            previous = signature
            quiet_since = time.monotonic()
            continue
        if count < high:
            previous = signature
            quiet_since = None
            continue
        if signature != previous:
            previous = signature
            quiet_since = time.monotonic()
            continue
        if quiet_since is not None and time.monotonic() - quiet_since >= 1.2:
            return


_SNAPSHOT_VALUES_JS = r"""() => {
  function shown(el) {
    if (!el || el.nodeType !== 1) return false;
    const type = String(el.getAttribute("type") || "").toLowerCase();
    if (type === "hidden" || type === "file" || type === "password" || type === "checkbox" || type === "radio") {
      return false;
    }
    if (el.hidden) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0;
  }
  const saved = [];
  for (const el of document.querySelectorAll("input, textarea, select")) {
    if (!shown(el) || !("value" in el)) continue;
    const value = String(el.value || "");
    if (!value.trim()) continue;
    let selector = "";
    if (el.id) {
      selector = "#" + CSS.escape(el.id);
    } else {
      if (!el.getAttribute("data-autofill-restore")) {
        el.setAttribute("data-autofill-restore", Math.random().toString(36).slice(2));
      }
      selector = "[data-autofill-restore='" + el.getAttribute("data-autofill-restore") + "']";
    }
    saved.push([selector, value]);
  }
  return saved;
}"""

_VISIBLE_VALUE_JS = r"""(selector) => {
  function shown(el) {
    if (!el || el.nodeType !== 1) return false;
    const type = String(el.getAttribute("type") || "").toLowerCase();
    if (type === "hidden" || type === "file" || type === "password") return false;
    if (el.hidden) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0;
  }
  const nodes = Array.from(document.querySelectorAll(selector)).filter(shown);
  const el = nodes[0];
  if (!el) return null;
  const type = String(el.getAttribute("type") || "").toLowerCase();
  if (type === "radio" || type === "checkbox") {
    const checked = nodes.find((node) => node.checked);
    return checked ? String(checked.value || "yes") : "";
  }
  if (!("value" in el)) return null;
  return String(el.value || "");
}"""

_SET_SELECT_JS = r"""({ selector, value }) => {
  const el = document.querySelector(selector);
  if (!el || String(el.tagName || "").toLowerCase() !== "select") return false;
  const wanted = String(value || "");
  for (const option of el.options) {
    const label = String(option.text || "").replace(/\s+/g, " ").trim();
    if (option.value !== wanted && label !== wanted) continue;
    el.value = option.value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }
  return false;
}"""

_TAG_JS = r"""(selector) => {
  const el = document.querySelector(selector);
  return el ? String(el.tagName || "").toLowerCase() : "";
}"""


def snapshot_text_values(page) -> list[tuple[str, str]]:
    """Visible text, select, and textarea values currently on the page."""
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return []
    try:
        raw = evaluate(_SNAPSHOT_VALUES_JS) or []
    except Exception:
        return []
    saved: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) < 2:
            continue
        selector = str(item[0] or "")
        value = str(item[1] or "")
        if selector and value.strip():
            saved.append((selector, value))
    return saved


def _visible_value(page, selector: str) -> str | None:
    """The visible control's value. ``None`` when that control is not on the page."""
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None or not selector:
        return None
    try:
        found = evaluate(_VISIBLE_VALUE_JS, selector)
    except Exception:
        return None
    if not isinstance(found, str):
        return None
    return found


def _restore_value(page, selector: str, value: str) -> bool:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return False
    try:
        kind = str(evaluate(_TAG_JS, selector) or "")
    except Exception:
        return False
    if kind == "select":
        try:
            return bool(evaluate(_SET_SELECT_JS, {"selector": selector, "value": value}))
        except Exception:
            return False
    return type_visible(page, selector, value)


_MATCH_VALUE_JS = r"""(hint) => {
  function norm(value) {
    return String(value || "")
      .replace(/\s+/g, " ")
      .trim()
      .toLowerCase()
      .replace(/[\s*✱＊]+$/g, "")
      .trim();
  }
  function shown(el) {
    if (!el || el.nodeType !== 1) return false;
    const type = String(el.getAttribute("type") || "").toLowerCase();
    if (type === "hidden" || type === "file" || type === "password") return false;
    if (el.hidden) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0;
  }
  function labelOf(el) {
    if (el.id) {
      const lab = document.querySelector("label[for='" + CSS.escape(el.id) + "']");
      if (lab) return norm(lab.innerText || lab.textContent || "");
    }
    const parent = el.closest ? el.closest("label") : null;
    if (parent) return norm(parent.innerText || parent.textContent || "");
    return norm(el.getAttribute("aria-label") || "");
  }
  function placeholder(text) {
    const folded = String(text || "").replace(/\s+/g, " ").trim().toLowerCase();
    if (!folded) return true;
    return /^(select\.{0,3}|choose\.{0,3}|please select|please choose|[-–—]+)$/.test(folded);
  }
  function read(el) {
    if (!el) return "";
    const type = String(el.getAttribute("type") || "").toLowerCase();
    const tag = String(el.tagName || "").toLowerCase();
    if (type === "radio" || type === "checkbox") return el.checked ? String(el.value || "yes") : "";
    const role = String(el.getAttribute("role") || "").toLowerCase();
    if (tag === "select") {
      const option = el.options && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
      const label = option ? String(option.text || option.label || "") : "";
      const value = String(el.value || "");
      if (!value.trim() || placeholder(label) || placeholder(value)) return "";
      return value;
    }
    if (role === "combobox" || role === "listbox") {
      const typed = "value" in el ? String(el.value || "") : "";
      if (typed.trim() && !placeholder(typed)) return typed;
      const root = (el.closest && el.closest(".select__control")) || el.parentElement;
      const choice = root
        ? root.querySelector(".select__single-value, .select__multi-value, [class*='selected-value']")
        : null;
      const chosen = choice ? String(choice.textContent || "").replace(/\s+/g, " ").trim() : "";
      return placeholder(chosen) ? "" : chosen;
    }
    if ("value" in el) {
      const value = String(el.value || "");
      return placeholder(value) ? "" : value;
    }
    return "";
  }
  const nodes = Array.from(document.querySelectorAll("input, textarea, select, [role='combobox']")).filter(shown);
  const id = String(hint.elementId || "");
  const automation = String(hint.automationId || "");
  const name = String(hint.name || "");
  const label = norm(hint.label || "");
  function group(pred) {
    return nodes.filter(pred);
  }
  function pick(list) {
    if (!list.length) return null;
    return list.find((node) => read(node).trim()) || list[0];
  }
  let list = [];
  if (id) list = group((node) => node.id === id);
  if (!list.length && automation) {
    list = group((node) => (node.getAttribute("data-automation-id") || "") === automation);
  }
  if (!list.length && name) list = group((node) => (node.getAttribute("name") || "") === name);
  if (!list.length && label) list = group((node) => labelOf(node) === label);
  const el = pick(list);
  if (!el) return null;
  return read(el);
}"""

_RESTORE_MATCH_JS = r"""(hint) => {
  function norm(value) {
    return String(value || "")
      .replace(/\s+/g, " ")
      .trim()
      .toLowerCase()
      .replace(/[\s*✱＊]+$/g, "")
      .trim();
  }
  function shown(el) {
    if (!el || el.nodeType !== 1) return false;
    const type = String(el.getAttribute("type") || "").toLowerCase();
    if (type === "hidden" || type === "file" || type === "password" || type === "checkbox" || type === "radio") {
      return false;
    }
    if (el.hidden) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0;
  }
  function labelOf(el) {
    if (el.id) {
      const lab = document.querySelector("label[for='" + CSS.escape(el.id) + "']");
      if (lab) return norm(lab.innerText || lab.textContent || "");
    }
    const parent = el.closest ? el.closest("label") : null;
    if (parent) return norm(parent.innerText || parent.textContent || "");
    return norm(el.getAttribute("aria-label") || "");
  }
  function read(el) {
    const tag = String(el.tagName || "").toLowerCase();
    if (tag === "select") return String(el.value || "");
    return "value" in el ? String(el.value || "") : "";
  }
  function write(el, value) {
    const tag = String(el.tagName || "").toLowerCase();
    if (tag === "select") {
      const wanted = String(value || "");
      for (const option of el.options) {
        const label = String(option.text || "").replace(/\s+/g, " ").trim();
        if (option.value !== wanted && label !== wanted) continue;
        el.value = option.value;
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        return true;
      }
      return false;
    }
    const proto = el instanceof HTMLTextAreaElement
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) desc.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  }
  const nodes = Array.from(document.querySelectorAll("input, textarea, select")).filter(shown);
  const id = String(hint.elementId || "");
  const automation = String(hint.automationId || "");
  const name = String(hint.name || "");
  const label = norm(hint.label || "");
  let el = null;
  let via = "none";
  if (id) {
    el = nodes.find((node) => node.id === id) || null;
    if (el) via = "id";
  }
  if (!el && automation) {
    el = nodes.find((node) => (node.getAttribute("data-automation-id") || "") === automation) || null;
    if (el) via = "automation";
  }
  if (!el && name) {
    el = nodes.find((node) => (node.getAttribute("name") || "") === name) || null;
    if (el) via = "name";
  }
  if (!el && label) {
    el = nodes.find((node) => labelOf(node) === label) || null;
    if (el) via = "label";
  }
  if (!el) return {result: "miss", via: "none"};
  if (read(el).trim()) return {result: "kept", via: via};
  const wrote = write(el, String(hint.value || ""));
  return {result: wrote ? "restored" : "miss", via: via};
}"""


def _confirmed_hint(record: dict) -> dict[str, str]:
    return {
        "value": str(record.get("value") or ""),
        "label": str(record.get("label") or ""),
        "name": str(record.get("name") or ""),
        "elementId": str(record.get("elementId") or ""),
        "automationId": str(record.get("automationId") or ""),
        "selector": str(record.get("selector") or ""),
        "choice": str(record.get("choice") or ""),
    }


def live_field_value(page, control: Control | None = None, **hint) -> str | None:
    """The visible control's current text, matched by id, name, automation id, or label.

    ``None`` means that control is not on the page. An empty string is a control
    the parse cleared. A hidden input with the same name is not this value.
    """
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return None
    payload = {
        "elementId": str(hint.get("element_id") or (control.element_id if control is not None else "") or ""),
        "name": str(hint.get("name") or (control.name if control is not None else "") or ""),
        "automationId": str(hint.get("automation_id") or (control.automation_id if control is not None else "") or ""),
        "label": str(hint.get("label") or (control.label if control is not None else "") or ""),
    }
    try:
        found = evaluate(_MATCH_VALUE_JS, payload)
    except Exception:
        return None
    if not isinstance(found, str):
        return None
    return found


def _visible_control_text(page, control: Control) -> str:
    """Text on the visible control. A hidden proxy with the same name is ignored."""
    shown = live_field_value(page, control)
    if shown and str(shown).strip():
        return str(shown).strip()
    current = _visible_value(page, control.selector)
    if current and str(current).strip():
        return str(current).strip()
    return ""


def _gap_is_location(item: dict) -> bool:
    text = str(item.get("text") or "").casefold()
    key = str(item.get("fieldKey") or "")
    if key in {"location", "address.city"}:
        return True
    if "sponsor" in text or "visa" in text or "relocat" in text:
        return False
    return "current location" in text or "working from" in text


_NATIVE_CHOICE_JS = """(selector) => {
  const el = document.querySelector(selector);
  if (!el) return false;
  const tag = el.tagName.toLowerCase();
  return tag === "select" || el.type === "radio" || el.type === "checkbox";
}"""


def _native_choice(page, selector: str) -> bool:
    """A native select, radio, or checkbox. Custom comboboxes keep their own commit check."""
    try:
        return bool(page.evaluate(_NATIVE_CHOICE_JS, selector))
    except Exception:
        return False


def _drop_live_answered(page, questions: list[dict]) -> list[dict]:
    """Drop a gap whose visible control already holds a value.

    A location box needs the place itself (``Austin, TX, USA``). A bare city
    stays open. Any other field is answered when its visible control has a
    value and the hidden proxy is empty.
    """
    if page is None or getattr(page, "evaluate", None) is None:
        return questions
    kept: list[dict] = []
    for item in questions:
        if str(item.get("kind") or "") in {"CONSENT", "CONSENT_CHOICE"}:
            kept.append(item)
            continue
        if str(item.get("answerType") or "") not in {"text", "textarea", "number", "date"}:
            # A select, radio, checkbox, or combobox the person already set in
            # the preview is answered on the page. It does not block Next.
            selector = str(item.get("selector") or "")
            if selector and _native_choice(page, selector) and control_committed(page, selector):
                continue
            kept.append(item)
            continue
        found = live_field_value(
            page,
            element_id="",
            name="",
            automation_id="",
            label=str(item.get("text") or ""),
        )
        selector = str(item.get("selector") or "")
        if (found is None or not str(found).strip()) and selector:
            current = _visible_value(page, selector)
            if current and str(current).strip():
                found = current
        text = str(found or "").strip()
        if not text:
            kept.append(item)
            continue
        if _gap_is_location(item) and not _full_place(text):
            kept.append(item)
            continue
        continue
    return kept


_NAME_HARVEST_JS = r"""() => {
  function shown(el) {
    if (!el || el.nodeType !== 1) return false;
    const type = String(el.getAttribute("type") || "").toLowerCase();
    if (type === "hidden" || type === "file" || type === "password") return false;
    if (type && type !== "text" && type !== "search") return false;
    if (el.hidden) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    if (parseFloat(style.opacity) === 0) return false;
    const rect = el.getBoundingClientRect();
    if (rect.right <= 0) return false;
    if (rect.width < 2 && rect.height < 2) return false;
    return true;
  }
  function clean(value) {
    return String(value || "")
      .replace(/\s+/g, " ")
      .trim()
      .replace(/[\s*✱＊]+$/g, "")
      .trim();
  }
  function labelOf(el) {
    const entry = el.closest && el.closest(
      "[data-field-path], .ashby-application-form-field-entry, .application-question"
    );
    if (entry) {
      const title = entry.querySelector(
        ".ashby-application-form-question-title, .application-label"
      );
      if (title && !title.contains(el)) return clean(title.innerText || title.textContent || "");
    }
    if (el.id) {
      const lab = document.querySelector("label[for='" + CSS.escape(el.id) + "']");
      if (lab) return clean(lab.innerText || lab.textContent || "");
    }
    const parent = el.closest ? el.closest("label") : null;
    if (parent) return clean(parent.innerText || parent.textContent || "");
    return clean(el.getAttribute("aria-label") || "");
  }
  const exact = {
    "full name": "fullName",
    "name": "fullName",
    "your name": "fullName",
    "applicant name": "fullName",
    "legal name": "legalName",
    "first name": "firstName",
    "given name": "firstName",
    "forename": "firstName",
    "last name": "lastName",
    "family name": "lastName",
    "surname": "lastName",
    "preferred name": "preferredName",
    "preferred first name": "preferredName",
    "nickname": "preferredName"
  };
  const auto = {
    "given-name": "firstName",
    "family-name": "lastName",
    "name": "fullName",
    "nickname": "preferredName"
  };
  const found = [];
  for (const el of document.querySelectorAll("input, textarea")) {
    if (!shown(el)) continue;
    const value = "value" in el ? String(el.value || "").trim() : "";
    if (!value) continue;
    const label = labelOf(el);
    const folded = label.toLowerCase();
    if (!folded || folded.indexOf("referr") !== -1 || folded.indexOf("?") !== -1) continue;
    let key = exact[folded] || "";
    if (!key) {
      const tokens = String(el.getAttribute("autocomplete") || "").toLowerCase().split(/\s+/);
      for (const token of tokens) {
        if (auto[token]) key = auto[token];
      }
    }
    if (!key) continue;
    let selector = "";
    if (el.id) selector = "#" + CSS.escape(el.id);
    else if (el.getAttribute("name")) selector = "input[name='" + el.getAttribute("name") + "']";
    found.push({
      value: value,
      label: label,
      name: el.getAttribute("name") || "",
      elementId: el.id || "",
      automationId: el.getAttribute("data-automation-id") || "",
      fieldKey: key,
      selector: selector
    });
  }
  return found;
}"""


def _harvest_known(records: list[dict], item: dict) -> bool:
    key = str(item.get("fieldKey") or "")
    element = str(item.get("elementId") or "")
    name = str(item.get("name") or "")
    label = str(item.get("label") or "")
    for existing in records:
        if not isinstance(existing, dict):
            continue
        if key and key == str(existing.get("fieldKey") or ""):
            return True
        if element and element == str(existing.get("elementId") or ""):
            return True
        if name and name == str(existing.get("name") or "") and label and label == str(existing.get("label") or ""):
            return True
    return False


def _harvest_name_records(page, records: list[dict]) -> None:
    """Add visible name fields a parser filled and this run never recorded.

    The list grows in place. Values stay out of the logs. A key or id that is
    already recorded is left as it is.
    """
    found: list[dict] = []
    for target in _frame_targets(page):
        evaluate = getattr(target, "evaluate", None)
        if evaluate is None:
            continue
        try:
            raw = evaluate(_NAME_HARVEST_JS) or []
        except Exception:
            continue
        if isinstance(raw, list):
            found.extend(item for item in raw if isinstance(item, dict))
    for item in found:
        value = str(item.get("value") or "").strip()
        key = str(item.get("fieldKey") or "").strip()
        if not value or not key or _harvest_known(records, item):
            continue
        record = {
            "value": value,
            "label": str(item.get("label") or ""),
            "name": str(item.get("name") or ""),
            "elementId": str(item.get("elementId") or ""),
            "automationId": str(item.get("automationId") or ""),
            "fieldKey": key,
            "selector": str(item.get("selector") or ""),
        }
        records.append(record)
        remember_confirmed(
            Control(
                kind="text",
                selector=record["selector"],
                label=record["label"],
                name=record["name"],
                element_id=record["elementId"],
                automation_id=record["automationId"],
            ),
            value,
            field_key=key,
        )


def _choice_record_is_consent(record: dict) -> bool:
    """Consent and attestations are not written back. The next pass pauses them."""
    label = str(record.get("label") or "")
    value = str(record.get("value") or "")
    control = Control(
        kind="checkbox" if str(record.get("choice") or "") == "checkbox" else "radio",
        label=label,
        prompt=label,
        options=[Option(value, value)] if value else [],
    )
    if acknowledgement_kind(control) in {"consent", "marketing"}:
        return True
    return attestation_manual(control) or is_demographic_control(control)


_FIND_CHOICE_JS = r"""(hint) => {
  function visible(el) {
    if (!el || el.nodeType !== 1) return false;
    if (el.hidden) return false;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0;
  }
  function labelOf(el) {
    const wrap = el.closest ? el.closest("label") : null;
    const raw = wrap ? wrap.innerText : (el.getAttribute("aria-label") || el.innerText || el.value || "");
    return String(raw || "").replace(/\s+/g, " ").trim().toLowerCase();
  }
  function selected(el) {
    const type = String(el.getAttribute("type") || el.type || "").toLowerCase();
    if (type === "checkbox" || type === "radio") return !!el.checked;
    return el.getAttribute("aria-checked") === "true";
  }
  function cssOf(el) {
    if (el.id) return "#" + CSS.escape(el.id);
    return "";
  }
  const choice = String(hint.choice || "");
  const want = String(hint.value || "").replace(/\s+/g, " ").trim().toLowerCase();
  const nodes = Array.from(document.querySelectorAll(
    "input[type='checkbox'], input[type='radio'], [role='checkbox'], [role='radio'], [role='switch']"
  )).filter(visible);
  let el = null;
  let via = "none";
  if (hint.selector) {
    try {
      const found = document.querySelector(hint.selector);
      if (found && visible(found)) {
        el = found;
        via = "selector";
      }
    } catch (error) {
      el = null;
    }
  }
  if (!el && hint.elementId) {
    el = nodes.find((node) => node.id === hint.elementId) || null;
    if (el) via = "id";
  }
  if (!el && hint.name) {
    const group = nodes.filter((node) => (node.getAttribute("name") || "") === hint.name);
    el = group.find((node) => {
      const value = String(node.value || "").trim().toLowerCase();
      return labelOf(node) === want || value === want;
    }) || null;
    if (!el && choice === "checkbox" && group.length === 1) el = group[0];
    if (el) via = "name";
  }
  if (!el && hint.label) {
    const folded = String(hint.label || "").replace(/\s+/g, " ").trim().toLowerCase();
    el = nodes.find((node) => labelOf(node) === folded) || null;
    if (el) via = "label";
  }
  if (!el) return {selector: "", via: "none", selected: false, role: ""};
  const type = String(el.getAttribute("type") || el.type || "").toLowerCase();
  const role = String(el.getAttribute("role") || "").toLowerCase();
  const native = el.tagName === "INPUT" && (type === "checkbox" || type === "radio");
  return {
    selector: cssOf(el) || String(hint.selector || ""),
    via: via,
    selected: selected(el),
    role: role || type,
    native: native,
  };
}"""


def _restore_choice(page, record: dict) -> dict | None:
    """Put a factual checkbox or radio back. Consent is left empty so it pauses."""
    if _choice_record_is_consent(record):
        return {"via": "consent", "result": "skipped"}
    hint = _confirmed_hint(record)
    from autofill.safeguards import commit_native_choice, press_aria_radio

    missed: dict | None = None
    for target in _frame_targets(page):
        evaluate = getattr(target, "evaluate", None)
        if evaluate is None:
            continue
        try:
            found = evaluate(_FIND_CHOICE_JS, hint)
        except Exception:
            continue
        if not isinstance(found, dict):
            continue
        selector = str(found.get("selector") or "")
        via = str(found.get("via") or "none")
        if not selector:
            missed = {"via": via, "result": "miss"}
            continue
        role = str(found.get("role") or "")
        want = str(hint.get("value") or "")
        turn_on = hint.get("choice") == "radio" or want.casefold() not in {"no", "false", "unchecked"}
        if bool(found.get("selected")) is turn_on:
            return {"via": via, "result": "kept"}
        if found.get("native"):
            ok = commit_native_choice(target, selector, on=turn_on)
        elif role == "radio":
            ok = press_aria_radio(target, selector)
        else:
            ok = False
        if ok:
            return {"via": via, "result": "restored"}
        missed = {"via": via, "result": "miss"}
    return missed


def _restore_one(page, record: dict) -> dict | None:
    """Match one confirmed value. A miss on the top document tries the frames."""
    if str(record.get("choice") or "") in {"checkbox", "radio"}:
        return _restore_choice(page, record)
    hint = _confirmed_hint(record)
    missed: dict | None = None
    for target in _frame_targets(page):
        evaluate = getattr(target, "evaluate", None)
        if evaluate is None:
            continue
        try:
            outcome = evaluate(_RESTORE_MATCH_JS, hint)
        except Exception:
            continue
        if outcome is True:
            return {"via": "id", "result": "restored"}
        if not isinstance(outcome, dict):
            continue
        result = str(outcome.get("result") or "miss")
        if result in {"kept", "restored"}:
            return {"via": str(outcome.get("via") or "none"), "result": result}
        missed = {"via": str(outcome.get("via") or "none"), "result": "miss"}
    return missed


def post_upload_refill(
    page,
    confirmed: list[dict] | None = None,
    *,
    session_id: str = "",
    log: bool = True,
) -> int:
    """Settle a resume parse, then write back values the engine already confirmed.

    The record is the session's fills, not a snapshot taken at upload time. A
    value that is still visible stays, including one a person typed and one the
    parse replaced. An empty visible control is filled again, even when Ashby
    replaced the node. Name boxes the parser filled are added to ``confirmed``
    when that list was passed in.
    """
    from autofill.fill import confirmed_records, current_session_id

    settle_form(page)
    if confirmed is not None:
        records = confirmed
    else:
        records = confirmed_records()
    _harvest_name_records(page, records)
    usable = [record for record in records if isinstance(record, dict) and str(record.get("value") or "").strip()]
    restored = 0
    session = session_id or current_session_id() or "-"
    if log:
        logger.info("session=%s action=post_upload_refill records=%s", session, len(usable))
    for record in usable:
        field = str(record.get("fieldKey") or "").strip() or "unkeyed"
        via = "none"
        result = "miss"
        outcome = _restore_one(page, record)
        if isinstance(outcome, dict):
            via = str(outcome.get("via") or "none")
            result = str(outcome.get("result") or "miss")
            if result not in {"miss", "kept", "restored", "skipped"}:
                result = "miss"
        if result == "restored":
            restored += 1
        if log:
            logger.info(
                "session=%s action=post_upload_refill field=%s via=%s result=%s",
                session,
                field,
                via,
                result,
            )
    if log:
        logger.info("session=%s action=post_upload_refill restored=%s", session, restored)
    return restored


def _refill_cleared_fields(
    page,
    requests: list[_Ask],
    response: ResolverResponse,
    flags: FillFlags,
    outcome: ResolvedPage,
    heading: str,
    shared: tuple,
    session_id: str = "",
) -> None:
    """Write approved values again when a resume parse cleared them.

    A value that is still on the control stays, including one a person typed.
    A held control is left alone even when the parse emptied it.
    """
    written = {
        item.selector
        for item in outcome.fields
        if item.selector and item.action in _FILLED_ACTIONS and item.key != "resume.file"
    }
    restored = 0
    if written:
        by_intent, unnamed, by_key, unclassified, rows, _index = shared
        refill_shared = (by_intent, unnamed, by_key, unclassified, rows, [-1])
        for ask in requests:
            if ask.field_key == "resume.file" or not ask.controls:
                continue
            selector = ask.controls[0].selector
            if not selector or selector not in written:
                continue
            if any(selector_held(control.selector) for control in ask.controls):
                continue
            if _cluster_committed(page, ask.controls):
                continue
            _apply_ask(page, ask, response, flags, outcome, heading, refill_shared)
            if _cluster_committed(page, ask.controls):
                restored += 1
    from autofill.fill import current_session_id

    logger.info(
        "session=%s action=post_upload_refill restored=%s",
        session_id or current_session_id() or "-",
        restored,
    )


_EEO_RE = re.compile(
    r"\b(eeo|equal employment|self-identif(?:y|ication)|self identif(?:y|ication))\b",
    re.IGNORECASE,
)


def _eeo(text: str) -> bool:
    """Voluntary self-identification stays manual even when a value is saved."""
    return bool(_EEO_RE.search(text or ""))


_RESUME_VERIFY_SECONDS = 4.0
_RESUME_SHOWN_HOLD = 0.7
_RESUME_STATUS_JS = """
({ selector, name }) => {
  function shown(node) {
    if (!node || node.hidden) return false;
    const style = window.getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden") return false;
    return true;
  }
  function rawText(node) {
    return node.innerText || node.textContent || "";
  }
  function fold(value) {
    return String(value || "").toLowerCase().replace(/[_\\s]+/g, " ").replace(/\\s+/g, " ").trim();
  }
  function isSuccess(value) {
    return /(^|[^a-z0-9])success!(?![a-z0-9])/i.test(value || "");
  }
  function mentionsFile(value, needles) {
    const folded = fold(value);
    return needles.some((needle) => needle && folded.indexOf(needle) !== -1);
  }
  function isFailure(value) {
    const folded = fold(value);
    if (!folded) return false;
    if (folded.indexOf("failed to upload") !== -1) return true;
    return folded.indexOf("fail") !== -1 || folded.indexOf("error") !== -1;
  }
  const expected = fold(name || "");
  const alerts = document.querySelectorAll("[role='alert'], .field-error, .error, [class*='toast'], [class*='Toast']");
  for (const alert of alerts) {
    if (!shown(alert)) continue;
    const text = rawText(alert);
    if (!text.trim()) continue;
    if (fold(text).indexOf("failed to upload") !== -1) return "error";
    if (mentionsFile(text, [expected]) && isFailure(text)) return "error";
  }
  const el = document.querySelector(selector);
  if (!el || !el.files || !el.files.length) return "missing";
  const fileName = fold((el.files[0] && el.files[0].name) || "");
  const needles = [expected, fileName];
  const box = el.closest("div, section, fieldset, li") || el.parentElement;
  if (!box) return "missing";
  const local = box.querySelector("[role='alert'], .field-error, .error");
  if (local && shown(local)) {
    const note = rawText(local);
    if (note.trim() && !isSuccess(note)) return "error";
  }
  let node = el;
  for (let depth = 0; node && node !== document.body && depth < 6; depth += 1, node = node.parentElement) {
    const body = rawText(node);
    if (mentionsFile(body, needles)) return "shown";
    if (isSuccess(body)) return "shown";
  }
  const marks = document.querySelectorAll("[role='status'], [class*='success'], [class*='Success']");
  for (const mark of marks) {
    if (shown(mark) && isSuccess(rawText(mark))) return "shown";
  }
  return "missing";
}
"""


def _resume_accepted(page, selector: str, name: str) -> bool:
    """Poll until the file stays shown. An error toast fails the attach.

    A chip that is already visible does not hold the rest of the ceiling.
    The short hold still catches a toast that arrives just after the name.
    """
    deadline = time.monotonic() + _RESUME_VERIFY_SECONDS
    shown_since: float | None = None
    while True:
        try:
            status = str(page.evaluate(_RESUME_STATUS_JS, {"selector": selector, "name": name}) or "missing")
        except Exception:
            return False
        now = time.monotonic()
        if status == "error":
            return False
        if status == "shown":
            if shown_since is None:
                shown_since = now
            elif now - shown_since >= _RESUME_SHOWN_HOLD:
                return True
        else:
            shown_since = None
        if now >= deadline:
            return status == "shown"
        time.sleep(0.1)


def _apply_resume(
    page,
    ask: _Ask,
    response: ResolverResponse,
    outcome: ResolvedPage,
) -> None:
    """Attach the resume grant, or leave the input when none was granted."""
    control = ask.controls[0]
    descriptor = response.fields.get("resume.file")
    if not isinstance(descriptor, dict):
        outcome.resume_blocked = True
        _skip(control, outcome, "resume.file", "Resolver did not return a resume file.")
        return
    try:
        payload = fetch_resume_payload(descriptor)
        result = attach_resume(
            page,
            control,
            name=payload.name,
            mime_type=payload.mime_type,
            data=payload.data,
        )
        outcome.fields.append(result)
        if result.action == "error" or not _resume_accepted(page, control.selector, payload.name):
            outcome.resume_blocked = True
    except ResumeFetchError:
        outcome.resume_blocked = True
        _skip(control, outcome, "resume.file", "Resume file was not attached.")


def _salary_answer_ok(match: IntentMatch, answer: ResolverAnswer | None) -> bool:
    """True when a resolver salary answer is HIGH and covers the question shape."""
    if answer is None:
        return False
    if not (may_fill(local=match, resolver_confidence=answer.confidence) or _saved_unknown(match, answer)):
        return False
    value = answer.value or (answer.values[0] if answer.values else "")
    return salary_fill_allowed(match.text, value, confidence=answer.confidence)


def _saved_unknown(match: IntentMatch, answer: ResolverAnswer) -> bool:
    """TileArc already matched this exact question text to a saved answer."""
    return match.intent is None and answer.confidence == "HIGH" and answer.source == "saved_answer"


def _question_blocks(controls: list[Control], answer: ResolverAnswer | None) -> bool:
    """Required questions and unusable saved values stop a later continue."""
    if answer is not None:
        return True
    return any(item.required for item in controls)


def _apply_question(
    page,
    ask: _Ask,
    answer: ResolverAnswer | None,
    outcome: ResolvedPage,
) -> None:
    match = ask.match or IntentMatch(None, "unknown", "none", sanitize_question(_question_text(ask.controls)))
    if match.intent in UNFILLED_INTENTS or _eeo(match.text) or _eeo(_question_text(ask.controls)):
        _manual(
            outcome,
            match,
            blocking=any(item.required for item in ask.controls),
            controls=ask.controls,
        )
        return
    if match.intent == "SALARY_EXPECTATION" and not _salary_answer_ok(match, answer):
        # A minimum, or a HIGH single number, is not a range. Ask the person.
        shape = salary_shape(match.text)
        _manual(
            outcome,
            match,
            blocking=_question_blocks(ask.controls, answer),
            controls=ask.controls,
            challenge="confirm",
            related_fields=SALARY_RELATED_FIELDS,
            salary_shape_name=shape,
            answer_confidence="LOW",
            challenge_reason="salary_range_incomplete" if shape == "range" else "salary_needs_confirm",
        )
        return
    if answer is None or not (
        may_fill(local=match, resolver_confidence=answer.confidence) or _saved_unknown(match, answer)
    ):
        _manual(outcome, match, blocking=_question_blocks(ask.controls, answer), controls=ask.controls)
        return
    control = ask.controls[0]
    if len(ask.controls) > 1 or (control.kind == "checkbox" and control.group and len(ask.controls) > 1):
        saved = list(answer.values) or ([answer.value] if answer.value else [])
        options = [
            Option(value=item.selector, label=_checkbox_label(item), selector=item.selector) for item in ask.controls
        ]
        chosen = match_all_options(saved, options)
        if chosen is None:
            _manual(outcome, match, blocking=True, controls=ask.controls, kind="OPTION_MISMATCH")
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
    if control.kind == "buttons":
        desired = answer.value or (answer.values[0] if answer.values else "")
        option = match_option_exact(desired, control.options)
        if option is None or not option.selector:
            _manual(outcome, match, blocking=True, controls=ask.controls, kind="OPTION_MISMATCH")
            return
        click_choice(page, option.selector, option.label)
        if _button_requires_selected_state(page, option.selector) and not control_committed(
            page, option.selector
        ):
            # The pair advertises aria-pressed (or the same). A click that leaves
            # every button unselected did not commit. Only a required pair stops.
            if any(item.required for item in ask.controls):
                _manual(outcome, match, blocking=True, controls=ask.controls, kind="OPTION_MISMATCH")
            return
        outcome.fields.append(
            FieldOutcome(
                selector=option.selector,
                label=control.label,
                key=match.intent,
                action="select",
                detail=option.label,
                field_class="APPROVED_QUESTION",
            )
        )
        return
    if control.kind == "combobox":
        desired = answer.value or (answer.values[0] if answer.values else "")
        picked = select_combobox(page, control.selector, desired)
        if picked is None:
            _manual(outcome, match, blocking=True, controls=ask.controls, kind="OPTION_MISMATCH")
            return
        outcome.fields.append(
            FieldOutcome(
                selector=control.selector,
                label=control.label,
                key=match.intent,
                action="select",
                detail=picked.label,
                field_class="APPROVED_QUESTION",
            )
        )
        return
    if control.kind in {"radio", "select"}:
        desired = answer.value or (answer.values[0] if answer.values else "")
        option = match_option_exact(desired, control.options)
        if option is None:
            _manual(outcome, match, blocking=True, controls=ask.controls, kind="OPTION_MISMATCH")
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
        _manual(outcome, match, blocking=True, controls=ask.controls, kind="OPTION_MISMATCH")
        return
    if not answer.value:
        _manual(outcome, match, blocking=True, controls=ask.controls)
        return
    _write_text(page, control, answer.value, outcome, key=match.intent or "saved_answer")


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
        _skip(control, outcome, key, "No value for this control.", page=page)
        return
    if control.kind == "combobox" and not control.options:
        picked = select_combobox(page, control.selector, text)
        if picked is None:
            _skip(control, outcome, key, "No option matched this value.")
            return
        outcome.fields.append(
            FieldOutcome(
                selector=control.selector,
                label=control.label,
                key=key,
                action="select",
                detail=picked.label,
                field_class="PROFILE_FIELD",
            )
        )
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
        _skip(control, outcome, key, "Empty value left blank.", page=page)
        return
    mapped = MappedField(key=key, action="fill", text=str(text), field_class="PROFILE_FIELD", confidence="high")
    outcome.fields.append(apply_mapped(page, control, mapped, overwrite=True))


_FILLED_ACTIONS = frozenset({"fill", "select", "check", "uncheck", "upload"})


def _required_question(control: Control) -> bool:
    """A visible required answer that is not a file, a password, or a legal attestation."""
    if not control.required or control.hidden or control.disabled or control.read_only:
        return False
    if control.kind in {"file", "password"} or control.input_type == "password":
        return False
    if is_honeypot(control) or _LEGAL_RE.search(haystack(control)):
        return False
    return True


def _skip(control: Control, outcome: ResolvedPage, key: str | None, reason: str, page=None) -> None:
    if page is not None:
        shown = live_field_value(page, control)
        if shown is not None and shown.strip():
            remember_confirmed(control, shown.strip(), field_key=key or "")
            outcome.fields.append(
                FieldOutcome(
                    selector=control.selector,
                    label=control.label,
                    key=key,
                    action="skip",
                    detail="already set",
                    field_class="PROFILE_FIELD",
                )
            )
            return
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
    if _required_question(control):
        _manual(
            outcome,
            IntentMatch(None, "unknown", "none", _manual_label(control)),
            blocking=True,
            control=control,
            kind="MISSING_PROFILE_VALUE",
            field_key=key,
        )


def _item_kind(intent: str | None, explicit: str | None, field_key: str | None) -> str:
    if explicit:
        return canonical_blocker_kind(explicit)
    if intent in DEMOGRAPHIC_INTENTS:
        return "DEMOGRAPHIC"
    if field_key:
        return "MISSING_PROFILE_VALUE"
    return "QUESTION"


def _control_token(control: Control | None) -> str:
    """An id, name, or selector that distinguishes one field from another with the same label."""
    if control is None:
        return ""
    if control.element_id.strip():
        return control.element_id.strip()
    if control.name.strip():
        return control.name.strip()
    selector = control.selector.strip()
    if selector.startswith("#") or selector.startswith("[data-automation-id"):
        return selector
    return ""


def _question_digest(text: str, control: Control | None) -> str:
    return question_hash(text, _control_token(control))


def _same_question(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """True when two manual items are the same field, not merely the same label."""
    digest = str(right.get("questionHash") or "")
    if digest and digest == str(left.get("questionHash") or ""):
        return True
    selector = str(right.get("selector") or "")
    if selector and selector == str(left.get("selector") or ""):
        return True
    return False


def _manual(
    outcome: ResolvedPage,
    match: IntentMatch,
    *,
    blocking: bool,
    control: Control | None = None,
    controls: list[Control] | None = None,
    kind: str | None = None,
    field_key: str | None = None,
    option_labels: list[str] | None = None,
    challenge: str = "",
    related_fields: tuple[str, ...] = (),
    salary_shape_name: str = "",
    answer_confidence: str = "",
    challenge_reason: str = "",
) -> None:
    if kind:
        kind = canonical_blocker_kind(kind)
    limit = 600 if kind in {"CONSENT", "CONSENT_CHOICE"} else 300
    text = sanitize_question(match.text or "", limit=limit)
    if not text or _is_section_heading(text):
        return
    group = list(controls or [])
    if control is not None and control not in group:
        group.insert(0, control)
    primary = group[0] if group else None
    digest = _question_digest(text, primary) or None
    options = [label for label in option_labels if label.strip()] if option_labels else []
    if not options and group:
        options = _option_labels(group)
    multi = len(group) > 1 and any(item.kind == "checkbox" for item in group)
    entry: dict[str, Any] = {
        "intent": match.intent,
        "text": text,
        "blocking": blocking,
        "questionId": digest,
        "questionHash": digest,
        "fieldKey": field_key,
        "answerType": answer_type_name(
            primary.kind if primary is not None else "text",
            primary.input_type if primary is not None else "",
            multi=multi,
            options=options,
        ),
        "options": options,
        "required": any(item.required for item in group) if group else blocking,
        "selector": primary.selector if primary is not None else "",
        "kind": _item_kind(match.intent, kind, field_key),
    }
    if challenge:
        # Names only. The saved salary amount stays off this payload.
        entry["challenge"] = challenge
        entry["relatedFields"] = [name for name in related_fields if name]
        entry["salaryShape"] = salary_shape_name
        entry["confidence"] = answer_confidence or "LOW"
        entry["reason"] = challenge_reason
    if not any(_same_question(item, entry) for item in outcome.manual_questions):
        outcome.manual_questions.append(entry)
    label = match.intent or "unknown"
    outcome.manual_actions.append(f"{label}: manual answer required")


def _control_filled(control: Control, filled: set[str]) -> bool:
    selectors = [control.selector] if control.selector else []
    selectors.extend(option.selector for option in control.options if option.selector)
    return any(selector in filled for selector in selectors)


def _group_filled(control: Control, controls: list[Control], filled: set[str]) -> bool:
    if control.kind not in {"checkbox", "radio"}:
        return False
    token = control.group or control.name
    if not token:
        return False
    for other in controls:
        if other.kind != control.kind or (other.group or other.name) != token:
            continue
        if _control_filled(other, filled):
            return True
    return False


def _checkbox_buckets(controls: list[Control]) -> dict[str, list[Control]]:
    buckets: dict[str, list[Control]] = {}
    for control in controls:
        key = _checkbox_key(control)
        if key:
            buckets.setdefault(key, []).append(control)
    return buckets


def _already_asked(outcome: ResolvedPage, text: str, digest: str, selector: str) -> bool:
    """True when this field, or the same label with no field identity, is already listed.

    Two controls that share a label and both have selectors stay distinct.
    """
    if digest and any(str(item.get("questionHash") or "") == digest for item in outcome.manual_questions):
        return True
    folded = normalize_question(text)
    if not folded:
        return False
    for item in outcome.manual_questions:
        if normalize_question(str(item.get("text") or "")) != folded:
            continue
        prior = str(item.get("selector") or "")
        if not prior or not selector or prior == selector:
            return True
    return False


_BUTTON_SELECTION_JS = r"""(selector) => {
  const el = document.querySelector(selector);
  if (!el) return false;
  function marks(node) {
    return node.hasAttribute("aria-pressed")
      || node.hasAttribute("aria-selected")
      || node.hasAttribute("aria-checked");
  }
  let box = el;
  for (let depth = 0; box && depth < 6; depth += 1, box = box.parentElement) {
    const kids = box.querySelectorAll("button, [role='button']");
    if (kids.length < 2 || kids.length > 8) continue;
    for (const kid of kids) {
      if (marks(kid)) return true;
    }
    return false;
  }
  return marks(el);
}"""


def _button_requires_selected_state(page, selector: str) -> bool:
    """True when the Yes/No pair exposes a pressed or selected attribute."""
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None or not selector:
        return False
    try:
        return bool(evaluate(_BUTTON_SELECTION_JS, selector))
    except Exception:
        return False


def _note_unfilled_required(outcome: ResolvedPage, page=None) -> None:
    """Every required visible control that is still empty is a manual question.

    Options of a checkbox group stay on that one question. A section heading
    is not a question.
    """
    snapshot = _live_snapshot(page, outcome.snapshot) if outcome.snapshot is not None else None
    if snapshot is None:
        if page is not None:
            ensure_blocker_options(page, outcome.manual_questions)
        return
    outcome.snapshot = snapshot
    filled = {item.selector for item in outcome.fields if item.action in _FILLED_ACTIONS and item.selector}
    seen = {str(item.get("questionHash") or "") for item in outcome.manual_questions if item.get("questionHash")}
    for control in snapshot.controls:
        if not _required_question(control):
            continue
        if _control_filled(control, filled) or _group_filled(control, snapshot.controls, filled):
            continue
        if page is not None and control.selector and _cluster_committed(page, [control]):
            continue
        if control.kind == "checkbox":
            key_name = _checkbox_key(control)
            siblings = [
                item
                for item in snapshot.controls
                if item.kind == "checkbox" and key_name and _checkbox_key(item) == key_name
            ]
            if len(siblings) > 1:
                text = sanitize_question(_question_text(siblings))
                if not text or _matches_option(text, siblings) or _is_section_heading(text):
                    continue
                digest = _question_digest(text, siblings[0])
                if _already_asked(outcome, text, digest, siblings[0].selector):
                    continue
                if digest:
                    seen.add(digest)
                _manual(outcome, IntentMatch(None, "unknown", "none", text), blocking=True, controls=siblings)
                continue
        text = sanitize_question(_manual_label(control))
        if _is_section_heading(text):
            continue
        if not text:
            continue
        digest = _question_digest(text, control)
        if _already_asked(outcome, text, digest, control.selector):
            continue
        if digest:
            seen.add(digest)
        _manual(outcome, IntentMatch(None, "unknown", "none", text), blocking=True, control=control)
    _drop_emitted_options(outcome)
    if page is not None:
        ensure_blocker_options(page, outcome.manual_questions)


def _drop_emitted_options(outcome: ResolvedPage) -> None:
    """Remove option labels once their group question is already listed."""
    snapshot = outcome.snapshot
    if snapshot is None or not outcome.manual_questions:
        return
    listed = {normalize_question(str(item.get("text") or "")) for item in outcome.manual_questions}
    option_keys: set[str] = set()
    for items in _checkbox_buckets(snapshot.controls).values():
        if len(items) < 2:
            continue
        title = normalize_question(sanitize_question(_question_text(items)))
        if not title or title not in listed:
            continue
        for item in items:
            for raw in (item.label, item.aria_label, _checkbox_label(item)):
                folded = normalize_question(sanitize_question(raw))
                if folded and folded != title:
                    option_keys.add(folded)
    if not option_keys:
        return
    outcome.manual_questions = [
        item
        for item in outcome.manual_questions
        if normalize_question(str(item.get("text") or "")) not in option_keys
    ]


def _lookup_keys(text: str, question_id: str = "") -> list[str]:
    """Normalized text, the question hash, and an echoed id all name one question."""
    keys: list[str] = []
    normalized = normalize_question(text)
    if normalized:
        keys.append(normalized)
    digest = question_hash(text) if text else ""
    if digest:
        keys.append(digest)
    token = text.strip().lower()
    if re.fullmatch(r"[0-9a-f]{64}", token):
        keys.append(token)
    qid = question_id.strip().lower()
    if qid:
        keys.append(qid)
    unique: list[str] = []
    for key in keys:
        if key not in unique:
            unique.append(key)
    return unique


def _index_answers(
    response: ResolverResponse,
) -> tuple[dict[str, ResolverAnswer], list[ResolverAnswer], dict[str, ResolverAnswer]]:
    """Known intents by name. Unknown answers pair by text, id, or hash."""
    by_intent: dict[str, ResolverAnswer] = {}
    unnamed: list[ResolverAnswer] = []
    by_key: dict[str, ResolverAnswer] = {}
    for item in response.answers:
        if item.intent:
            by_intent.setdefault(item.intent, item)
            continue
        unnamed.append(item)
        for key in _lookup_keys(item.text, item.question_id):
            by_key.setdefault(key, item)
    return by_intent, unnamed, by_key


def _unclassified_count(requests: list[_Ask]) -> int:
    """Questions sent with no recognised intent."""
    count = 0
    for ask in requests:
        if not ask.include_question:
            continue
        if ask.match is None or ask.match.intent is None:
            count += 1
    return count


def _take_answer(
    match: IntentMatch,
    by_intent: dict[str, ResolverAnswer],
    unnamed: list[ResolverAnswer],
    by_key: dict[str, ResolverAnswer],
    *,
    unclassified: int = 1,
    question_id: str = "",
) -> ResolverAnswer | None:
    if match.intent:
        return by_intent.get(match.intent)
    keys = [question_id.strip().lower()] if question_id.strip() else []
    keys.extend(_lookup_keys(match.text))
    seen: set[str] = set()
    for key in keys:
        if not key or key in seen:
            continue
        seen.add(key)
        found = by_key.get(key)
        if found is not None and found in unnamed:
            unnamed.remove(found)
            return found
    # A keyless answer can only be the one open question. With two or more
    # unclassified questions it would land in the wrong field.
    if unclassified > 1:
        return None
    loose = [item for item in unnamed if not _lookup_keys(item.text, item.question_id)]
    if loose:
        found = loose[0]
        unnamed.remove(found)
        return found
    return None


def _complete_compounds(
    binding: ResolverBinding,
    *,
    session_id: str,
    step: str,
    requests: list[_Ask],
    response: ResolverResponse,
) -> ResolverResponse:
    """When a compound intent comes back unresolved, combine the saved parts.

    Older resolvers do not know ``SPONSORSHIP_NOW_OR_FUTURE``. A second call
    asks for the component intents. Yes if either sponsorship answer is Yes,
    No only if both are No. Authorization compounds stay manual until every
    part is HIGH.
    """
    pending = [ask for ask in requests if _needs_components(ask, response)]
    if not pending:
        return response
    questions: list[dict] = []
    seen: set[str] = set()
    for ask in pending:
        match = ask.match
        if match is None:
            continue
        for intent in match.fallback_intents:
            if intent in seen or _boolean_answer(response, intent) is not None:
                continue
            seen.add(intent)
            body = ask.question_body()
            body["intent"] = intent
            questions.append(body)
    extra: ResolverResponse | None = None
    try:
        if questions:
            extra = resolve_step(binding, session_id=session_id, step=step, fields=[], questions=questions)
            response.answers.extend(list(extra.answers))
            response.latency_ms += extra.latency_ms
        for ask in pending:
            _write_combined(response, ask)
    finally:
        if extra is not None:
            extra.wipe()
    return response


def _needs_components(ask: _Ask, response: ResolverResponse) -> bool:
    match = ask.match
    if match is None or match.confidence != "HIGH" or not match.combine or not match.fallback_intents:
        return False
    intent = match.intent or ""
    if intent in response.unresolved:
        return True
    return _boolean_answer(response, intent) is None


def _boolean_answer(response: ResolverResponse, intent: str) -> ResolverAnswer | None:
    """A HIGH Yes or No that the resolver did not list as unresolved."""
    if not intent or intent in response.unresolved:
        return None
    for item in response.answers:
        if item.intent != intent or item.confidence != "HIGH":
            continue
        value = item.value or (item.values[0] if item.values else "")
        if options_yes(value) or options_no(value):
            return item
    return None


def _answer_pair(response: ResolverResponse, intent: str) -> tuple[str, str]:
    found = _boolean_answer(response, intent)
    if found is None:
        return ("", "")
    return (found.confidence, found.value or (found.values[0] if found.values else ""))


def _combined_value(response: ResolverResponse, match: IntentMatch) -> str | None:
    parts = [_answer_pair(response, intent) for intent in match.fallback_intents]
    if match.combine == "sponsorship_or":
        return combine_yes_if_either(parts)
    if match.combine == "sponsorship_negated":
        positive = combine_yes_if_either(parts)
        if positive == "Yes":
            return "No"
        if positive == "No":
            return "Yes"
        return None
    if match.combine == "authorized_without":
        authorizations = [intent for intent in match.fallback_intents if not intent.startswith("SPONSORSHIP_")]
        sponsorships = [intent for intent in match.fallback_intents if intent.startswith("SPONSORSHIP_")]
        return combine_authorized_without(
            [_answer_pair(response, intent) for intent in authorizations],
            [_answer_pair(response, intent) for intent in sponsorships],
        )
    return None


def _write_combined(response: ResolverResponse, ask: _Ask) -> None:
    match = ask.match
    if match is None or not match.intent:
        return
    combined = _combined_value(response, match)
    response.answers = [item for item in response.answers if item.intent != match.intent]
    if match.intent in response.unresolved:
        response.unresolved = [item for item in response.unresolved if item != match.intent]
    if combined is None:
        return
    response.answers.append(
        ResolverAnswer(
            intent=match.intent,
            value=combined,
            confidence="HIGH",
            source="combined",
            text=match.text,
        )
    )


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


def _clean_prompt(text: str) -> str:
    return re.sub(r"[\s*✱＊]+$", "", text).strip()


def _matches_option(text: str, controls: list[Control]) -> bool:
    """True when ``text`` is an option label, not the question."""
    folded = normalize(text)
    if not folded:
        return True
    if len(controls) > 1:
        for control in controls:
            if folded in {normalize(_checkbox_label(control)), normalize(control.label)}:
                return True
        return False
    for option in controls[0].options:
        if folded in {normalize(option.label), normalize(option.value)}:
            return True
    return False


def _usable_question(text: str, controls: list[Control]) -> str:
    cleaned = dedupe_label(_clean_prompt(text))
    if not cleaned or _matches_option(cleaned, controls) or _is_section_heading(cleaned):
        return ""
    return cleaned


def _question_text(controls: list[Control]) -> str:
    control = controls[0]
    if len(controls) > 1 or control.kind == "radio" or control.kind == "checkbox":
        for raw in (control.group, control.prompt):
            text = _usable_question(raw, controls)
            if text:
                return text
        if len(controls) > 1 or control.kind == "radio":
            for raw in (control.label, control.aria_label):
                text = _usable_question(raw, controls)
                if text:
                    return text
            return ""
    for raw in (control.label, control.aria_label, control.placeholder, control.prompt):
        text = _usable_question(raw, controls)
        if text:
            return text
    return ""


def _manual_label(control: Control) -> str:
    """The question a person would read, not an option and not a bare asterisk."""
    return _question_text([control])


_CHOICE_ANSWER_TYPES = frozenset({"single_select", "multi_select"})
_PLACE_KEYS = frozenset({"location", "address.city", "city"})


def _place_blocker(item: dict[str, Any]) -> bool:
    """A location typeahead, not a city menu of bare names."""
    key = str(item.get("fieldKey") or "").strip()
    text = str(item.get("text") or "").strip().casefold()
    if key in _PLACE_KEYS:
        return True
    if "working from" in text or "payroll tax" in text:
        return True
    return text in {"location", "your location", "current location", "your current location"}


def ensure_blocker_options(page, items: list[dict[str, Any]]) -> None:
    """Fill empty choice lists from the live control, including a closed combobox."""
    if page is None or not items:
        return
    from autofill.dom_ui import read_choice_options

    for item in items:
        if str(item.get("answerType") or "") not in _CHOICE_ANSWER_TYPES:
            continue
        existing = [str(option).strip() for option in item.get("options") or [] if str(option).strip()]
        if existing:
            continue
        selector = str(item.get("selector") or "")
        if not selector:
            continue
        labels = read_choice_options(page, selector)
        if not labels:
            labels = list_combobox_options(page, selector)
        if not labels and _place_blocker(item):
            labels = _load_place_options(page, selector, "")
        labels = [label for label in labels if _keep_option(label)]
        if labels:
            item["options"] = labels


_JUNK_OPTION = re.compile(r"click here\b.{0,80}\b(?:issue|browser|cache|cookies)\b", re.IGNORECASE)
_PLACEHOLDER_OPTION = re.compile(
    r"^(?:select|choose|please select|choose one|select one|start typing)\.{0,3}$",
    re.IGNORECASE,
)


def _keep_option(label: str) -> bool:
    """Drop placeholders and instruction rows that are not choices."""
    text = label.strip()
    if not text or _PLACEHOLDER_OPTION.match(text):
        return False
    return not _JUNK_OPTION.search(text)


def _option_labels(controls: list[Control]) -> list[str]:
    if len(controls) > 1:
        return [label for label in (_checkbox_label(item) for item in controls) if _keep_option(label)]
    labels = []
    for option in controls[0].options:
        label = option.label or option.value
        if label and label not in labels:
            labels.append(sanitize_question(label, limit=80))
    return [label for label in labels if _keep_option(label)]


def _strip_affix(label: str, affix: str) -> str:
    if not affix:
        return label
    folded = label.casefold()
    affix_folded = affix.casefold()
    if folded.startswith(affix_folded):
        return label[len(affix) :].strip(" :-")
    if folded.endswith(affix_folded):
        return label[: -len(affix)].strip(" :-")
    return label


def _checkbox_label(control: Control) -> str:
    """Option text with the fieldset legend or question prompt removed from either end."""
    label = control.label.strip()
    for affix in (control.group.strip(), control.prompt.strip()):
        label = _strip_affix(label, affix)
    return sanitize_question(label or control.aria_label or control.name, limit=80)


def _control_name(controls: list[Control], control: Control) -> str:
    if len(controls) > 1:
        return "multiselect"
    if control.kind == "radio":
        return "radio"
    if control.kind in {"select", "combobox", "buttons"}:
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


_DEMOGRAPHIC_KEYS = frozenset({"gender", "race_ethnicity", "veteran_status", "disability_status"})


def _required_key(control: Control) -> str:
    return str(control.selector or control.name or control.label or "")


def _gap_clusters(snapshot: PageSnapshot) -> list[list[Control]]:
    """Required controls in page order. A checkbox group is one cluster."""
    clusters: list[list[Control]] = []
    seen: set[str] = set()
    buckets = _checkbox_buckets(snapshot.controls)
    for control in snapshot.controls:
        if control.hidden or control.disabled or control.read_only:
            continue
        if not counts_required(control) or is_honeypot(control):
            continue
        key = _checkbox_key(control)
        if key and len(buckets.get(key, [])) > 1:
            if key in seen:
                continue
            seen.add(key)
            clusters.append(buckets[key])
            continue
        clusters.append([control])
    return clusters


_TYPEAHEAD_STATE_JS = r"""
(selector) => {
  const el = document.querySelector(selector);
  if (!el) return { value: "", expanded: false, chip: false, shown: "" };
  function clean(text) {
    return String(text || "").replace(/\s+/g, " ").trim();
  }
  const placeholder = /^(select\.{0,3}|choose\.{0,3}|please select|start typing\.{0,3})$/i;
  function usable(text) {
    const value = clean(text);
    if (!value || placeholder.test(value) || value.charAt(0) === "{" || value.charAt(0) === "[") return "";
    return value;
  }
  const scope = (el.closest && el.closest(
    "[data-field-path], .ashby-application-form-field-entry, .select__control"
  )) || el.parentElement;
  const chipSelector = [
    ".select__single-value",
    ".select__multi-value",
    "[class*='selected-value']",
    "[class*='selectedValue']",
    "[data-selected-value]",
  ].join(", ");
  const chip = scope ? scope.querySelector(chipSelector) : null;
  const hidden = scope ? scope.querySelector("input[type='hidden']") : null;
  const direct = "value" in el ? usable(el.value) : "";
  const chipText = usable(chip ? chip.textContent : "");
  const hiddenText = usable(hidden ? hidden.value : "");
  const shownText = usable(el.innerText || el.textContent);
  const place = direct || chipText || shownText || hiddenText;
  function visible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden" || node.hidden) return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }
  function listOpen() {
    if (el.getAttribute("aria-expanded") !== "true") return false;
    const listId = el.getAttribute("aria-controls") || el.getAttribute("aria-owns") || "";
    const list = listId ? document.getElementById(listId) : null;
    if (list) return visible(list);
    const options = document.querySelectorAll("[role='option']");
    for (const option of options) {
      if (visible(option)) return true;
    }
    return false;
  }
  return {
    value: direct,
    chip: chipText.length > 0,
    shown: place,
    expanded: listOpen(),
  };
}
"""


def _full_place(text: str) -> bool:
    """A city plus a region, not a typed city and not a JSON blob."""
    shown = text.strip()
    if "," not in shown or shown.startswith("{") or shown.startswith("["):
        return False
    return bool(re.search(r"[A-Za-z]", shown))


def _read_typeahead_state(page, selector: str) -> dict[str, Any]:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None or not selector:
        return {}
    try:
        state = evaluate(_TYPEAHEAD_STATE_JS, selector) or {}
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def _settle_rendered_place(page, control: Control) -> bool:
    """Close a leftover suggestion list once the chosen place is already showing."""
    for _ in range(8):
        if _typeahead_place_committed(page, control):
            return True
        state = _read_typeahead_state(page, control.selector)
        shown = str(state.get("shown") or "")
        if _full_place(shown) and state.get("expanded"):
            try:
                dismiss_combobox(page)
            except Exception:
                return False
        _pause(page, 40)
    return _typeahead_place_committed(page, control)


def _typeahead_place_committed(page, control: Control) -> bool:
    """A typeahead place is committed when the page shows it and the list is closed.

    The place may be the input value, the text the control displays, a selected-value
    element, or a hidden input in the field. A bare city, or a list that is still open,
    is not a commit.
    """
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return True
    state = _read_typeahead_state(page, control.selector)
    if not state or state.get("expanded"):
        return False
    return _full_place(str(state.get("shown") or ""))


def _cluster_committed(page, controls: list[Control]) -> bool:
    if len(controls) > 1 and all(item.kind == "checkbox" for item in controls):
        return any(control_committed(page, item.selector) for item in controls)
    control = controls[0]
    if _is_location_typeahead(control):
        if _has_selected_location(page) and _location_pick(page) is not None:
            return True
        if _typeahead_place_committed(page, control):
            return True
        state = _read_typeahead_state(page, control.selector)
        if state.get("expanded"):
            return False
        # The hidden selectedLocation proxy is often empty while the box shows the place.
        return _full_place(_visible_control_text(page, control))
    if control_committed(page, control.selector):
        return True
    return bool(_visible_control_text(page, control))


def _accept_standard_term(page, ask) -> bool:
    """Check one required terms box when the session flag allows it.

    Consent choices, marketing, and every unusual attestation stay blockers.
    The check is the verified ``check_registered`` routine used on account
    pages (pointer check, forced check, then its label), and the box counts
    only when the page reports it checked afterwards.
    """
    if ask.consent_choice or not standard_terms_enabled():
        return False
    if len(ask.controls) != 1:
        return False
    control = ask.controls[0]
    if control.kind != "checkbox" or control.input_type in {"aria-checkbox", "switch"} or not control.required:
        return False
    category = terms_category(control)
    if category not in AUTO_CONSENT_CATEGORIES:
        return False
    from autofill.account_flow import check_registered

    try:
        verified = check_registered(page, control.selector)
    except Exception:
        verified = False
    if not verified:
        logger.info("auto_consent category=%s result=failed", category)
        return False
    record_auto_consent(control, category)
    return True


def _consent_statement(control: Control) -> str:
    """The checkbox statement a person confirms, bounded at 600 characters."""
    raw = control.label or control.prompt or control.group or control.aria_label or "Confirmation"
    return sanitize_question(raw, limit=600)


def _gap_kind(controls: list[Control], mapped: MappedField | None) -> str:
    text = _question_text(controls) or controls[0].label
    if is_demographic_control(controls[0]):
        return "DEMOGRAPHIC"
    if is_consent_choice(controls[0]):
        return "CONSENT_CHOICE"
    if acknowledgement_kind(controls[0]) == "consent" or attestation_manual(controls[0]):
        return "CONSENT"
    if mapped is not None and mapped.key in _DEMOGRAPHIC_KEYS:
        return "DEMOGRAPHIC"
    match = classify_question(text, _option_labels(controls))
    if match.intent in DEMOGRAPHIC_INTENTS or _eeo(text) or _eeo(controls[0].label):
        return "DEMOGRAPHIC"
    if mapped is not None and "No option matched" in (mapped.reason or ""):
        return "OPTION_MISMATCH"
    if (mapped is not None and mapped.key) or match_field_key(controls[0]):
        return "MISSING_PROFILE_VALUE"
    return "QUESTION"


def _load_place_options(page, selector: str, query: str) -> list[str]:
    """Read suggestion labels, typing ``query`` only when the box is empty.

    The typed probe is cleared and the list is closed. A Lever page keeps its
    own suggestion id and is not probed here.
    """
    if not selector or _has_selected_location(page):
        return []
    field = page.locator(selector)
    try:
        current = str(field.input_value() or "")
    except Exception:
        return []
    probe = current.strip() or query.strip()
    if not probe:
        return []
    typed = False
    labels: list[str] = []
    try:
        if not current.strip():
            field.press_sequentially(probe, delay=0)
            typed = True
        found = _poll_location_options(page, selector, 12, 100)
        for label, _item_selector in found:
            if label not in labels:
                labels.append(label)
    except Exception:
        labels = []
    if typed:
        try:
            field.fill("")
        except Exception:
            pass
    try:
        dismiss_combobox(page)
    except Exception:
        pass
    return labels


def _gap_item(controls: list[Control], profile: CandidateProfile | None, page=None) -> dict[str, Any]:
    primary = controls[0]
    if is_consent_choice(primary) or acknowledgement_kind(primary) == "consent":
        text = _consent_statement(primary)
    else:
        text = sanitize_question(
            _question_text(controls) or primary.label or primary.aria_label or "Required field"
        )
    mapped = map_field(primary, profile) if profile is not None and len(controls) == 1 else None
    kind = _gap_kind(controls, mapped)
    field_key = None
    if kind == "MISSING_PROFILE_VALUE":
        field_key = (mapped.key if mapped is not None and mapped.key else None) or match_field_key(primary)
    elif mapped is not None and mapped.key and kind != "DEMOGRAPHIC":
        field_key = mapped.key
    if field_key == "phone_country":
        field_key = "phone.country"
    options = _option_labels(controls)
    if page is not None and primary.kind == "combobox" and not options and primary.selector:
        options = [label for label in list_combobox_options(page, primary.selector) if _keep_option(label)]
        if options:
            from autofill.models import Option

            primary.options = [Option(value=label, label=label) for label in options]
            rematched = match_field_key(primary)
            if rematched:
                field_key = rematched
    if field_key == "phone_country":
        field_key = "phone.country"
    elif field_key == "country":
        field_key = "address.country"
    if page is not None and not options and primary.selector and _is_location_typeahead(primary):
        query = profile.personal.city.strip() if profile is not None else ""
        loaded = [label for label in _load_place_options(page, primary.selector, query) if _keep_option(label)]
        if loaded:
            options = [sanitize_question(label, limit=80) for label in loaded]
    match = classify_question(text, options)
    named = bool(match.intent) and (match.intent in DEMOGRAPHIC_INTENTS or match.confidence == "HIGH")
    intent = match.intent if named else None
    if kind != "DEMOGRAPHIC" and intent in DEMOGRAPHIC_INTENTS:
        intent = None
    names = {label.casefold() for label in options}
    if primary.kind == "buttons" and "yes" in names and "no" in names:
        field_key = None
        if kind == "MISSING_PROFILE_VALUE":
            kind = "QUESTION"
    digest = _question_digest(text, primary) or None
    multi = len(controls) > 1 and any(item.kind == "checkbox" for item in controls)
    result = {
        "intent": intent,
        "text": text,
        "blocking": True,
        "questionId": digest,
        "questionHash": digest,
        "fieldKey": field_key,
        "answerType": answer_type_name(primary.kind, primary.input_type, multi=multi, options=options),
        "options": options,
        "required": True,
        "selector": primary.selector,
        "kind": kind,
    }
    if mapped is not None and mapped.challenge == "confirm":
        # The profile has a related salary value that does not cover this question.
        result["challenge"] = "confirm"
        result["confidence"] = "LOW"
        result["relatedFields"] = list(mapped.related_fields)
        result["salaryShape"] = salary_shape(text)
        result["reason"] = mapped.reason
    return result


def _merge_gap_items(gaps: list[dict[str, Any]], existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_selector = {str(item.get("selector") or ""): item for item in existing if item.get("selector")}
    by_text: dict[str, dict[str, Any]] = {}
    for item in existing:
        folded = normalize_question(str(item.get("text") or ""))
        if folded:
            by_text[folded] = item
    merged: list[dict[str, Any]] = []
    for gap in gaps:
        prior = by_selector.get(str(gap.get("selector") or ""))
        if prior is None:
            textual = by_text.get(normalize_question(str(gap.get("text") or "")))
            prior_selector = str((textual or {}).get("selector") or "")
            gap_selector = str(gap.get("selector") or "")
            if textual is not None and (not prior_selector or not gap_selector or prior_selector == gap_selector):
                prior = textual
        if prior is None:
            merged.append(gap)
            continue
        chosen = dict(prior)
        chosen["blocking"] = True
        if not chosen.get("selector"):
            chosen["selector"] = gap.get("selector") or ""
        if not chosen.get("kind"):
            chosen["kind"] = gap.get("kind")
        if not chosen.get("options") and gap.get("options"):
            chosen["options"] = list(gap.get("options") or [])
        if gap.get("challenge") and not chosen.get("challenge"):
            for hint in ("challenge", "confidence", "relatedFields", "salaryShape", "reason"):
                if gap.get(hint):
                    chosen[hint] = gap.get(hint)
        merged.append(chosen)
    return merged


def _live_snapshot(page, snapshot: PageSnapshot) -> PageSnapshot:
    """Re-read the page so a gap check does not use values from before a rewrite."""
    if page is None or getattr(page, "evaluate", None) is None:
        return snapshot
    try:
        fresh = extract_page(page)
    except Exception:
        return snapshot
    return fresh if fresh is not None else snapshot


def inspect_required(
    page,
    snapshot: PageSnapshot,
    profile: CandidateProfile | None = None,
    prior: list[dict[str, Any]] | None = None,
    *,
    option_reads: int | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Empty required controls, plus how many required clusters are committed.

    The live field values are read again before the clusters are built.
    ``option_reads`` caps how many gaps open a menu while they are built.
    Choice items that are still missing a list are read afterwards. A full
    fill leaves the cap unset.
    """
    snapshot = _live_snapshot(page, snapshot)
    clusters = _gap_clusters(snapshot)
    committed: set[str] = set()
    open_keys: set[str] = set()
    gaps: list[dict[str, Any]] = []
    committed_clusters = 0
    reads = 0
    for cluster in clusters:
        keys = [key for key in (_required_key(item) for item in cluster) if key]
        if _cluster_committed(page, cluster):
            committed.update(keys)
            committed_clusters += 1
            continue
        open_keys.update(keys)
        use_page = page
        if option_reads is not None and reads >= option_reads:
            use_page = None
        gaps.append(_gap_item(cluster, profile, use_page))
        reads += 1
    sync_required_progress(committed, open_keys)
    if prior:
        gaps = _merge_gap_items(gaps, prior)
    if page is not None:
        ensure_blocker_options(page, gaps)
    return gaps, len(clusters), committed_clusters


def required_gaps(
    page,
    snapshot: PageSnapshot,
    profile: CandidateProfile | None = None,
    prior: list[dict[str, Any]] | None = None,
    *,
    option_reads: int | None = None,
) -> list[dict[str, Any]]:
    """Visible required controls that are still empty, in page order.

    ``profile`` classifies a profile-mode field. Resolver mode passes None and
    keeps a prior manual item when one already names the same control.
    """
    gaps, _total, _done = inspect_required(
        page,
        snapshot,
        profile,
        prior,
        option_reads=option_reads,
    )
    return gaps
