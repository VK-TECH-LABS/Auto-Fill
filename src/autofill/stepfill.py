"""Fill one application step from resolver values, then drop those values.

Profile records are not consulted while writing. The only values written are
the ones the resolver returned for the keys and intents on this step.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from autofill.adapters import adapter_for
from autofill.combobox import list_combobox_options, select_combobox
from autofill.dates import format_for_control
from autofill.dom_ui import (
    answer_type_name,
    checkpoint,
    control_committed,
    counts_required,
    refill_cleared,
    selector_held,
    sync_required_progress,
    type_visible,
)
from autofill.extract import extract_page
from autofill.fill import apply_mapped, attach_resume
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
from autofill.mapping import haystack, is_honeypot, map_field, match_field_key, mentions_work_history, normalize
from autofill.models import Control, FieldOutcome, MappedField, Option, PageSnapshot
from autofill.profile import CandidateProfile
from autofill.resolver import ResolverAnswer, ResolverBinding, ResolverResponse, resolve_step
from autofill.resume_fetch import ResumeFetchError, fetch_resume_payload
from autofill.safeguards import click_choice, dismiss_combobox

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

    field_keys = _requested_field_keys(requests)
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
        _apply(page, requests, response, active, outcome, snapshot.heading)
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
    if control.kind == "buttons":
        text = _question_text(controls)
        options = _option_labels(controls)
        match = classify_question(text, options, hook=hook)
        return _Ask(controls=controls, match=match, include_question=True)
    if control.kind == "password" or control.input_type == "password":
        return None
    if is_honeypot(control):
        return None
    blob = haystack(control)
    if _LEGAL_RE.search(blob):
        return None
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
    wanted = normalize(city)
    if not wanted:
        return False
    head = normalize(label.split(",")[0])
    if head == wanted:
        return True
    return _phrase_in(normalize(label), city)


def choose_location_label(labels: list[str], *, city: str, region: str, country: str) -> str | None:
    """One suggestion that matches the city and, when present, the region and country."""
    winners: list[tuple[int, str]] = []
    for label in labels:
        folded = normalize(label)
        if not _same_city(label, city):
            continue
        score = 4
        if region:
            if not _alias_in(folded, region, _STATES):
                continue
            score += 2
        if country:
            if not _alias_in(folded, country, _COUNTRIES):
                continue
            score += 1
        winners.append((score, label))
    if not winners:
        return None
    best = max(score for score, _label in winners)
    chosen = [label for score, label in winners if score == best]
    if len(chosen) != 1:
        return None
    return chosen[0]


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


def _location_committed(page, control: Control, city: str) -> bool:
    """True when a suggestion was recorded, not when the city was only typed.

    Lever keeps the chosen place in a hidden ``selectedLocation`` input as JSON
    with an id. The visible box can hold the city, or the hidden input can hold
    an empty name, while no suggestion has been picked. Ashby shows the chosen
    place on the combobox itself and closes the list.
    """
    if _has_selected_location(page):
        pick = _location_pick(page)
        if pick is None:
            return False
        return bool(city) and _phrase_in(normalize(str(pick.get("name") or "")), city)
    if getattr(page, "evaluate", None) is None:
        try:
            typed = str(page.locator(control.selector).input_value() or "")
        except Exception:
            typed = ""
        folded = normalize(typed)
        if not folded or folded == normalize(city):
            return False
        return _phrase_in(folded, city)
    if not _settle_rendered_place(page, control):
        return False
    shown = str(_read_typeahead_state(page, control.selector).get("shown") or "")
    folded = normalize(shown)
    if not folded or folded == normalize(city):
        return False
    return _phrase_in(folded, city)


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
    """Type the city and keep a suggestion. A bare typed city is not a commit."""
    value = str(target.get("value") or "").strip()
    if not value:
        values = [str(item).strip() for item in target.get("values") or [] if str(item).strip()]
        value = values[0] if values else ""
    selector = str(target.get("selector") or "")
    if not selector:
        return {"filled": False, "reason": "no_selector"}
    if not value:
        return {"filled": False, "reason": "empty"}
    fields = {"location": value, "address.city": value.split(",")[0].strip()}
    control = Control(kind="combobox", label=str(target.get("text") or "Current location"), selector=selector)
    outcome = ResolvedPage()
    trace: dict[str, Any] = {}
    # The caller named a city. When several places share it, keep the first
    # suggestion the geocoder ranked. A profile fill with no region still asks.
    _apply_location(page, control, fields, outcome, ranked=True, trace=trace)
    city, _region, _country = _location_parts(fields)
    labels = [str(label) for label in trace.get("labels") or [] if str(label).strip()]
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
    delay = 20
    raw_delay = os.environ.get("AUTOFILL_TYPE_DELAY")
    if raw_delay is not None:
        try:
            delay = max(0, int(raw_delay))
        except ValueError:
            delay = 20
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
    _wait_location_network(page)
    options = _poll_location_options(page, control.selector, 20, 100)
    match = _match_location(options, city=city, region=region, country=country)
    if match is None:
        # One more wait. Lever's geocoder list sometimes arrives after the first pass.
        _pause(page, 1000)
        options = _location_options(page, control.selector) or options
        match = _match_location(options, city=city, region=region, country=country)
    status = trace.get("status")
    if isinstance(status, int) and status >= 400:
        options = []
        match = None
    trace["count"] = len(options)
    trace["labels"] = [label for label, _selector in options]
    logger.info("location suggestions status=%s count=%s", status if status is not None else "none", len(options))
    if match is None and ranked and not region:
        hits = [item for item in options if _same_city(item[0], city)]
        if hits:
            match = hits[0]
            logger.info("location answer ranked=first count=%s", len(hits))
    if match is not None:
        click_choice(page, match[1], match[0])
        if not _location_committed(page, control, city):
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
        if _location_committed(page, control, city):
            picked = _location_pick(page) or {}
            detail = str(picked.get("name") or match[0])
            _remember_location(outcome, control, detail)
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
    try:
        field.fill("")
    except Exception:
        pass
    _manual_location(outcome, control, [label for label, _selector in options])


def _apply(
    page,
    requests: list[_Ask],
    response: ResolverResponse,
    flags: FillFlags,
    outcome: ResolvedPage,
    heading: str,
) -> None:
    by_intent, unnamed, by_key = _index_answers(response)
    unclassified = _unclassified_count(requests)
    employment_rows = _rows(response.fields.get("employment[]"))
    education_rows = _rows(response.fields.get("education[]"))
    project_rows = _rows(response.fields.get("projects[]"))
    employment_index = -1
    # A missing resume is recorded after the other controls. It must not skip them.
    ordered = [ask for ask in requests if ask.field_key != "resume.file"]
    ordered.extend(ask for ask in requests if ask.field_key == "resume.file")
    for ask in ordered:
        if not checkpoint():
            break
        if any(selector_held(control.selector) for control in ask.controls) and (
            not refill_cleared() or all(control_committed(page, control.selector) for control in ask.controls)
        ):
            continue
        if ask.controls and ask.controls[0].kind == "buttons" and control_committed(page, ask.controls[0].selector):
            continue
        if ask.field_key == "resume.file":
            _apply_resume(page, ask, response, outcome)
            continue
        if ask.compose_location:
            _apply_location(page, ask.controls[0], response.fields, outcome)
            continue
        if ask.field_key == "phone.country":
            _apply_phone_country(page, ask.controls[0], response.fields, outcome)
            continue
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


_EEO_RE = re.compile(
    r"\b(eeo|equal employment|self-identif(?:y|ication)|self identif(?:y|ication))\b",
    re.IGNORECASE,
)


def _eeo(text: str) -> bool:
    """Voluntary self-identification stays manual even when a value is saved."""
    return bool(_EEO_RE.search(text or ""))


_RESUME_VERIFY_SECONDS = 4.0
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
  const body = rawText(box);
  if (mentionsFile(body, needles)) return "shown";
  if (isSuccess(body)) return "shown";
  const marks = document.querySelectorAll("[role='status'], [class*='success'], [class*='Success']");
  for (const mark of marks) {
    if (shown(mark) && isSuccess(rawText(mark))) return "shown";
  }
  return "missing";
}
"""


def _resume_accepted(page, selector: str, name: str) -> bool:
    """Wait a few seconds. An upload-error toast fails the attach; otherwise the name must show."""
    deadline = time.monotonic() + _RESUME_VERIFY_SECONDS
    status = "missing"
    while True:
        try:
            status = str(page.evaluate(_RESUME_STATUS_JS, {"selector": selector, "name": name}) or "missing")
        except Exception:
            return False
        if status == "error":
            return False
        if time.monotonic() >= deadline:
            return status == "shown"
        time.sleep(0.25)


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
            blocking=any(item.required for item in ask.controls) or match.intent == "REFERRAL",
            controls=ask.controls,
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
        _skip(control, outcome, key, "No value for this control.")
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
        _skip(control, outcome, key, "Empty value left blank.")
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
        return explicit
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
) -> None:
    text = sanitize_question(match.text or "")
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
    snapshot = outcome.snapshot
    if snapshot is None:
        if page is not None:
            ensure_blocker_options(page, outcome.manual_questions)
        return
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


def _keep_option(label: str) -> bool:
    """Drop instruction rows that are not choices, such as a browser-help link."""
    return not _JUNK_OPTION.search(label)


def _option_labels(controls: list[Control]) -> list[str]:
    if len(controls) > 1:
        return [label for label in (_checkbox_label(item) for item in controls) if _keep_option(label)][:20]
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
        if _has_selected_location(page):
            return _location_pick(page) is not None
        return _typeahead_place_committed(page, control)
    return control_committed(page, control.selector)


def _gap_kind(controls: list[Control], mapped: MappedField | None) -> str:
    text = _question_text(controls) or controls[0].label
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
    return {
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
        merged.append(chosen)
    return merged


def inspect_required(
    page,
    snapshot: PageSnapshot,
    profile: CandidateProfile | None = None,
    prior: list[dict[str, Any]] | None = None,
    *,
    option_reads: int | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Empty required controls, plus how many required clusters are committed.

    ``option_reads`` caps how many gaps open a menu while they are built.
    Choice items that are still missing a list are read afterwards. A full
    fill leaves the cap unset.
    """
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
