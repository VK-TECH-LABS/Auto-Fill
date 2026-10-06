"""Map a discovered form control to a candidate-profile value.

Label synonyms cover the fields ApplyPilot's apply prompt fills from the
profile: identity, contact, address, links, work authorization, compensation,
experience, voluntary EEO, and the standard screening answers (age, background
check, convictions, prior employment, referral source). Honeypot and hidden
text inputs are skipped, matching that prompt's form notes.

This mapper does not guess. ApplyPilot told its agent to answer "yes" to
nearby skill questions. Auto-Fill answers a skill question only when that
skill is listed on the profile, and otherwise leaves the control unanswered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from autofill.models import Control, JobContext, MappedField, Option
from autofill.profile import CandidateProfile
from autofill.salary import resolve_salary

_DECLINE_HINTS = ("decline", "prefer not", "do not wish", "not to say", "not to answer")


@dataclass(frozen=True)
class _Rule:
    key: str
    patterns: tuple[re.Pattern[str], ...]
    kinds: frozenset[str] | None = None


def _compile(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    return tuple(re.compile(pattern) for pattern in patterns)


_RULES: tuple[_Rule, ...] = (
    _Rule("resume", _compile((r"\bresume\b", r"\bcv\b", r"\bcurriculum vitae\b")), frozenset({"file"})),
    _Rule(
        "cover_letter",
        _compile((r"\bcover letter\b",)),
        frozenset({"file", "textarea", "text"}),
    ),
    _Rule("first_name", _compile((r"\bfirst name\b", r"\bgiven name\b", r"\bforename\b"))),
    _Rule("last_name", _compile((r"\blast name\b", r"\bfamily name\b", r"\bsurname\b"))),
    _Rule("legal_name", _compile((r"\blegal name\b",))),
    _Rule("preferred_name", _compile((r"\bpreferred name\b", r"\bnickname\b"))),
    _Rule("email", _compile((r"\be mail\b", r"\bemail\b"))),
    _Rule("phone", _compile((r"\bphone\b", r"\bmobile\b", r"\btelephone\b", r"\bcell\b"))),
    _Rule("linkedin_url", _compile((r"\blinkedin\b",))),
    _Rule("github_url", _compile((r"\bgithub\b",))),
    _Rule("portfolio_url", _compile((r"\bportfolio\b",))),
    _Rule("city", _compile((r"\bcity\b", r"\btown\b"))),
    _Rule("state", _compile((r"\bprovince\b", r"\bstate\b"))),
    _Rule("postal_code", _compile((r"\bpostal\b", r"\bpost code\b", r"\bpostcode\b", r"\bzip\b"))),
    _Rule("country", _compile((r"\bcountry\b",))),
    _Rule("address", _compile((r"\bstreet\b", r"\baddress line\b", r"\baddress\b"))),
    _Rule(
        "legally_authorized_to_work",
        _compile(
            (
                r"\blegally authorized\b",
                r"\bauthorized to work\b",
                r"\bwork authorization\b",
                r"\beligible to work\b",
            )
        ),
    ),
    _Rule(
        "require_sponsorship",
        _compile((r"\bsponsorship\b", r"\bvisa sponsorship\b")),
    ),
    _Rule("work_permit_type", _compile((r"\bwork permit\b", r"\bvisa status\b"))),
    _Rule(
        "salary_range",
        _compile((r"\bsalary range\b", r"\bcompensation range\b", r"\bpay range\b")),
    ),
    _Rule(
        "salary",
        _compile(
            (
                r"\bsalary\b",
                r"\bcompensation\b",
                r"\bdesired pay\b",
                r"\bpay expectation\b",
                r"\bhourly rate\b",
                r"\bper hour\b",
            )
        ),
    ),
    _Rule(
        "years_of_experience_total",
        _compile((r"\byears of experience\b", r"\byears experience\b", r"\btotal experience\b")),
    ),
    _Rule(
        "education_level",
        _compile((r"\beducation level\b", r"\bhighest education\b", r"\bdegree\b")),
    ),
    _Rule(
        "current_job_title",
        _compile(
            (
                r"\bcurrent title\b",
                r"\bcurrent job title\b",
                r"\bjob title\b",
                r"\bmost recent title\b",
                r"\bposition title\b",
            )
        ),
    ),
    _Rule(
        "current_company",
        _compile(
            (
                r"\bcurrent company\b",
                r"\bcurrent employer\b",
                r"\bmost recent company\b",
                r"\bemployer\b",
                r"\bcompany name\b",
            )
        ),
    ),
    _Rule(
        "earliest_start_date",
        _compile((r"\bstart date\b", r"\bearliest start\b", r"\bavailable to start\b", r"\bdate available\b")),
    ),
    _Rule("gender", _compile((r"\bgender\b",))),
    _Rule("race_ethnicity", _compile((r"\brace\b", r"\bethnicity\b"))),
    _Rule("veteran_status", _compile((r"\bveteran\b",))),
    _Rule("disability_status", _compile((r"\bdisability\b",))),
    _Rule(
        "age_18_or_older",
        _compile((r"\bage 18\b", r"\b18 or older\b", r"\bat least 18\b", r"\bover 18\b")),
    ),
    _Rule("willing_background_check", _compile((r"\bbackground check\b",))),
    _Rule("felony_conviction", _compile((r"\bfelony\b", r"\bconvicted\b", r"\bcriminal\b"))),
    _Rule(
        "previously_employed_here",
        _compile(
            (
                r"\bpreviously worked\b",
                r"\bpreviously employed\b",
                r"\bformer employee\b",
                r"\bworked here before\b",
                r"\bworked for us\b",
            )
        ),
    ),
    _Rule(
        "how_heard",
        _compile((r"\bhow did you hear\b", r"\bhow did you find\b", r"\bwhere did you hear\b")),
    ),
    _Rule("available_for_full_time", _compile((r"\bfull time\b",))),
    _Rule(
        "available_for_contract",
        _compile((r"\bcontract work\b", r"\bavailable for contract\b", r"\bcontract basis\b", r"\bcontract role\b")),
    ),
    _Rule("website_url", _compile((r"\bwebsite\b", r"\bpersonal site\b", r"\bhomepage\b"))),
    _Rule("full_name", _compile((r"\bfull name\b", r"\byour name\b", r"\bapplicant name\b", r"\bname\b"))),
)

_AUTOCOMPLETE = {
    "name": "full_name",
    "given-name": "first_name",
    "family-name": "last_name",
    "email": "email",
    "tel": "phone",
    "tel-national": "phone",
    "street-address": "address",
    "address-line1": "address",
    "address-level2": "city",
    "address-level1": "state",
    "postal-code": "postal_code",
    "country": "country",
    "country-name": "country",
    "organization": "current_company",
    "organization-title": "current_job_title",
    "url": "website_url",
}

_SKILL_RE = re.compile(
    r"(?:experience with|proficient in|knowledge of|familiar with|worked with|do you know)\s+(.+)$"
)
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SPECIFIC_EXPERIENCE_RE = re.compile(r"\b(with|using)\b")


def normalize(text: str) -> str:
    """Lowercase a label and turn punctuation into spaces."""
    lowered = text.casefold().replace("_", " ").replace("-", " ").replace("/", " ")
    cleaned = re.sub(r"[^a-z0-9+\s]", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def yes_no(value: str) -> str | None:
    """Return ``yes``, ``no``, or None when the text is not a boolean answer."""
    token = normalize(value)
    if token in {"yes", "y", "true"}:
        return "yes"
    if token in {"no", "n", "false"}:
        return "no"
    return None


def haystack(control: Control) -> str:
    parts = [
        control.label,
        control.name,
        control.element_id,
        control.placeholder,
        control.aria_label,
        control.autocomplete,
    ]
    return normalize(" ".join(part for part in parts if part))


def is_honeypot(control: Control) -> bool:
    """Hidden text inputs and common trap labels are left blank."""
    blob = haystack(control)
    compact = blob.replace(" ", "")
    if "companywebsite" in compact or "company website" in blob:
        return True
    if any(token in blob for token in ("honeypot", "leave blank", "leave this field blank", "do not fill", "bot field")):
        return True
    if control.hidden and control.kind in {"text", "textarea"}:
        return True
    return False


def match_option(desired: str, options: list[Option]) -> Option | None:
    """Pick the option that best matches a profile answer. Never guess past it."""
    if not desired or not options:
        return None
    target = normalize(desired)
    for option in options:
        if normalize(option.label) == target or normalize(option.value) == target:
            return option
    answer = yes_no(desired)
    if answer:
        for option in options:
            if yes_no(option.label) == answer or yes_no(option.value) == answer:
                return option
        for option in options:
            label = normalize(option.label)
            if answer == "yes" and label.startswith("yes"):
                return option
            if answer == "no" and label.startswith("no"):
                return option
    if any(hint in target for hint in _DECLINE_HINTS):
        for option in options:
            label = normalize(option.label)
            if any(hint in label for hint in _DECLINE_HINTS):
                return option
    return None


def _wants_national_digits(control: Control) -> bool:
    if control.autocomplete == "tel-national" or control.input_mode == "numeric":
        return True
    blob = haystack(control)
    return "digits" in blob or "country code" in blob or "country prefix" in blob


def _profile_text(profile: CandidateProfile, key: str, control: Control, job: JobContext | None) -> str:
    personal = profile.personal
    if key == "full_name":
        return personal.public_name
    if key == "legal_name":
        return personal.full_name
    if key == "first_name":
        if re.search(r"\blegal\b", haystack(control)):
            return personal.first_token
        return personal.preferred_or_first
    if key == "last_name":
        return personal.last_name
    if key == "preferred_name":
        return personal.preferred_or_first
    if key == "email":
        return personal.email
    if key == "phone":
        if _wants_national_digits(control):
            return profile.phone_digits()
        return personal.phone
    if key == "address":
        return personal.address
    if key == "city":
        return personal.city
    if key == "state":
        return personal.province_state
    if key == "country":
        return personal.country
    if key == "postal_code":
        return personal.postal_code
    if key == "linkedin_url":
        return personal.linkedin_url
    if key == "github_url":
        return personal.github_url
    if key == "portfolio_url":
        return personal.portfolio_url
    if key == "website_url":
        return personal.website_url
    if key == "legally_authorized_to_work":
        return profile.work_authorization.legally_authorized_to_work
    if key == "require_sponsorship":
        return profile.work_authorization.require_sponsorship
    if key == "work_permit_type":
        return profile.work_authorization.work_permit_type
    if key == "earliest_start_date":
        return profile.availability.earliest_start_date
    if key == "available_for_full_time":
        return profile.availability.available_for_full_time
    if key == "available_for_contract":
        return profile.availability.available_for_contract
    if key in {"salary", "salary_range"}:
        posted_min = job.posted_salary_min if job else None
        posted_max = job.posted_salary_max if job else None
        blob = haystack(control)
        return resolve_salary(
            profile.compensation,
            posted_min=posted_min,
            posted_max=posted_max,
            hourly="hourly" in blob or "per hour" in blob,
            as_range=key == "salary_range" or "range" in blob,
        )
    if key == "years_of_experience_total":
        return profile.experience.years_of_experience_total
    if key == "education_level":
        return profile.experience.education_level
    if key == "current_job_title":
        return profile.experience.current_job_title
    if key == "current_company":
        return profile.experience.current_company
    if key == "gender":
        return profile.eeo_voluntary.gender
    if key == "race_ethnicity":
        return profile.eeo_voluntary.race_ethnicity
    if key == "veteran_status":
        return profile.eeo_voluntary.veteran_status
    if key == "disability_status":
        return profile.eeo_voluntary.disability_status
    if key == "age_18_or_older":
        return profile.screening.age_18_or_older
    if key == "willing_background_check":
        return profile.screening.willing_background_check
    if key == "felony_conviction":
        return profile.screening.felony_conviction
    if key == "previously_employed_here":
        return profile.screening.previously_employed_here
    if key == "how_heard":
        return profile.screening.how_heard
    if key == "cover_letter":
        return profile.documents.cover_letter_text
    return ""


def _specific_years_question(blob: str) -> bool:
    """Years-with-a-tool questions are not the profile's total years."""
    return bool(_SPECIFIC_EXPERIENCE_RE.search(blob))


def _match_rule(control: Control) -> str | None:
    autocomplete = control.autocomplete.strip().casefold()
    if autocomplete in _AUTOCOMPLETE and (control.kind != "file"):
        # Autocomplete is a stronger signal than a noisy label, except files.
        mapped = _AUTOCOMPLETE[autocomplete]
        if autocomplete == "tel-national":
            return "phone"
        return mapped
    blob = haystack(control)
    if "country code" in blob:
        return None
    for rule in _RULES:
        if rule.kinds is not None and control.kind not in rule.kinds:
            continue
        if rule.key == "years_of_experience_total" and _specific_years_question(blob):
            continue
        if any(pattern.search(blob) for pattern in rule.patterns):
            return rule.key
    return None


def _skill_answer(control: Control, profile: CandidateProfile) -> MappedField | None:
    match = _SKILL_RE.search(haystack(control))
    if not match:
        return None
    asked = re.split(r"\band\b|,|/", match.group(1))[0].strip(" ?.")
    if not asked or len(asked) > 40:
        return MappedField(key="skill", action="unanswered", reason="Skill question was not specific enough to map.")
    known = {normalize(skill) for skill in profile.skills}
    if asked in known:
        return _from_text(control, "skill", "Yes", reason=f"{asked} is listed on the profile.")
    return MappedField(
        key="skill",
        action="unanswered",
        reason=f"No explicit profile skill for {asked!r}; left for a person.",
    )


def _from_text(control: Control, key: str, text: str, *, reason: str = "") -> MappedField:
    if not text:
        return MappedField(key=key, action="unanswered", reason="Profile has no value for this field.")
    if control.kind in {"select", "radio"}:
        option = match_option(text, control.options)
        if option is None:
            return MappedField(
                key=key,
                action="unanswered",
                reason=f"No option matched profile value {text!r}.",
            )
        return MappedField(
            key=key,
            action="select",
            text=text,
            option_label=option.label,
            option_value=option.value,
            option_selector=option.selector,
            reason=reason,
        )
    if control.kind == "checkbox":
        answer = yes_no(text)
        if answer == "yes":
            return MappedField(key=key, action="check", text=text, reason=reason)
        if answer == "no":
            return MappedField(key=key, action="uncheck", text=text, reason=reason)
        return MappedField(key=key, action="unanswered", reason="Checkbox needs a yes or no profile value.")
    if control.input_type == "date" and not _ISO_DATE_RE.fullmatch(text):
        return MappedField(
            key=key,
            action="unanswered",
            reason=f"Date input needs YYYY-MM-DD; profile value is {text!r}.",
        )
    return MappedField(key=key, action="fill", text=text, reason=reason)


def map_field(
    control: Control,
    profile: CandidateProfile,
    *,
    resume_path: str | None = None,
    cover_letter_path: str | None = None,
    job: JobContext | None = None,
) -> MappedField:
    """Decide how to fill one control. Does not touch the page."""
    if control.kind == "password" or control.input_type == "password":
        return MappedField(key=None, action="skip", reason="Password fields are never filled.")
    if control.disabled:
        return MappedField(key=None, action="skip", reason="Control is disabled.")
    if control.read_only and control.kind in {"text", "textarea"}:
        return MappedField(key=None, action="skip", reason="Control is read-only.")
    if control.hidden and control.kind != "file":
        return MappedField(key=None, action="skip", reason="Control is hidden.")
    if is_honeypot(control):
        return MappedField(key=None, action="skip", reason="Honeypot or hidden text field left blank.")

    key = _match_rule(control)
    if key is not None and control.kind == "file" and key not in {"resume", "cover_letter"}:
        return MappedField(key=key, action="unanswered", reason="File input did not match a resume or cover letter.")
    if key is None:
        skill = _skill_answer(control, profile)
        if skill is not None:
            return skill
        return MappedField(key=None, action="unanswered", reason="No profile field matched this control.")

    if key == "resume":
        if not resume_path:
            return MappedField(key=key, action="unanswered", reason="No resume file was provided.")
        return MappedField(key=key, action="upload", text=resume_path)
    if key == "cover_letter" and control.kind == "file":
        if not cover_letter_path:
            return MappedField(key=key, action="unanswered", reason="No cover letter file was provided.")
        return MappedField(key=key, action="upload", text=cover_letter_path)

    if key in {"salary", "salary_range"} and not profile.compensation.salary_expectation:
        return MappedField(key=key, action="unanswered", reason="Profile has no salary expectation.")
    try:
        text = _profile_text(profile, key, control, job)
    except ValueError as exc:
        return MappedField(key=key, action="unanswered", reason=str(exc))
    return _from_text(control, key, text)
