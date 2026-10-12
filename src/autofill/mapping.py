"""Map a discovered form control to a candidate-profile value.

Label synonyms cover the fields ApplyPilot's apply prompt fills from the
profile: identity, contact, address, links, work authorization, compensation,
experience, and the standard screening answers (age, background
check, convictions, prior employment, referral source). Demographic questions
are never filled. Honeypot and hidden
text inputs are skipped, matching that prompt's form notes.

This mapper does not guess. ApplyPilot told its agent to answer "yes" to
nearby skill questions. Auto-Fill answers a skill question only when that
skill is listed on the profile, and otherwise leaves the control unanswered.
"""

from __future__ import annotations

import contextvars
import logging
import re
from dataclasses import dataclass, field

from autofill.answers import match_answer
from autofill.dates import format_for_control
from autofill.models import Control, JobContext, MappedField, Option
from autofill.profile import CandidateProfile
from autofill.salary import resolve_salary

_DECLINE_HINTS = ("decline", "prefer not", "do not wish", "not to say", "not to answer")
_LEGAL_RE = re.compile(
    r"\b(i agree|i certify|i acknowledge|terms of (service|use)|privacy policy|attest|e-?sign|legal attestation)\b"
)
_CONSENT_RE = re.compile(
    r"\b(i agree|i certify|i acknowledge|i consent|terms|privacy|attest|e sign|esign|legal declaration)\b"
)
_CONSENT_STRONG_RE = re.compile(
    r"\b(terms|privacy|attest|e sign|esign|legal declaration|certify)\b"
)
_MARKETING_RE = re.compile(
    r"\b(newsletter|promotional|marketing|product updates|special offers|opt in|receive emails|receive updates)\b"
)
_ALIAS_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"united states", "united states of america", "usa", "us"}),
    frozenset({"canada", "ca"}),
    frozenset({"california", "ca"}),
    frozenset({"new york", "ny"}),
    frozenset({"texas", "tx"}),
    frozenset({"washington", "wa"}),
)


@dataclass
class MapCursor:
    """Per-page counters so repeated employer/school fields use the next row."""

    counts: dict[str, int] = field(default_factory=dict)
    page_heading: str = ""

    def take(self, kind: str) -> int:
        index = self.counts.get(kind, 0)
        self.counts[kind] = index + 1
        return index


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
    _Rule("school", _compile((r"\bschool\b", r"\buniversity\b", r"\bcollege\b"))),
    _Rule("field_of_study", _compile((r"\bfield of study\b", r"\bmajor\b"))),
    _Rule("internship_company", _compile((r"\binternship (company|employer|organization)\b",))),
    _Rule("project_name", _compile((r"\bproject name\b",))),
    _Rule("project_description", _compile((r"\bproject description\b",)), frozenset({"textarea", "text"})),
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
                r"\bmost recent employer\b",
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
_SPECIFIC_EXPERIENCE_RE = re.compile(r"\b(with|using)\b")
_SCREENING_KEYS = frozenset(
    {
        "age_18_or_older",
        "willing_background_check",
        "felony_conviction",
        "previously_employed_here",
        "how_heard",
    }
)


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
        control.prompt,
        control.group,
    ]
    return normalize(" ".join(part for part in parts if part))


def option_stance(label: str) -> str:
    """``agree``, ``decline``, or empty for one opt-in or opt-out label.

    ``No, I do not consent``, ``I do not consent``, and ``Opt out`` decline.
    ``Yes, I consent`` and ``Opt in`` agree. A plain Yes or No is neither.
    """
    text = normalize(label)
    if not text:
        return ""
    declined = (
        "do not consent" in text
        or "dont consent" in text
        or "do not agree" in text
        or "opt out" in text
        or (text.startswith("no ") and ("consent" in text or "opt" in text))
        or text.startswith("decline")
    )
    agreed = (text.startswith("yes") and ("consent" in text or "opt" in text or "agree" in text)) or (
        "opt in" in text or ("i consent" in text and "not consent" not in text)
    )
    refusal = "do not" in text or "opt out" in text or text.startswith("decline") or (
        text.startswith("no ") and ("consent" in text or "opt" in text)
    )
    if declined and refusal:
        return "decline"
    if agreed and not declined:
        return "agree"
    if declined:
        return "decline"
    return ""


def is_consent_choice(control: Control) -> bool:
    """A radio or select that offers both a consent option and a decline option."""
    if control.kind not in {"radio", "select", "combobox"}:
        return False
    stances = {option_stance(option.label or option.value) for option in control.options}
    return "agree" in stances and "decline" in stances


_TERMS_FLAG: contextvars.ContextVar[bool] = contextvars.ContextVar("autofill_standard_terms", default=False)
_AUTO_CONSENTS: contextvars.ContextVar[list | None] = contextvars.ContextVar("autofill_auto_consents", default=None)
# Checked before the allow-list. Any hit stays CONSENT, including a sentence that
# also mentions ordinary terms or privacy.
STANDARD_TERMS_DENY: tuple[str, ...] = (
    "marketing",
    "newsletter",
    "promotional",
    "promotion",
    "sms",
    "text message",
    "text messages",
    "recording",
    "notetaker",
    "note taker",
    "artificial intelligence",
    "ai",
    "arbitration",
    "e sign",
    "esign",
    "signature",
    "background check",
    "certify",
    "attest",
    "attestation",
    "declare",
    "under penalty",
    "perjury",
    "demographic",
    "gender",
    "race",
    "ethnicity",
    "veteran",
    "disability",
    "product updates",
    "special offers",
    "opt in",
    "receive emails",
    "receive updates",
    "job alerts",
    "job alert",
    "email updates",
    "email update",
    "talent community",
    "talent network",
    "calls",
    "phone calls",
    "phone call",
    "autodialed",
    "auto dialed",
    "third parties",
    "third party",
    "partners",
    "sensitive",
    "biometric",
    "health",
    "medical",
    "criminal",
    "social security",
    "ssn",
    "contacted",
    "contact me",
    "drug test",
    "waive",
    "waiver",
    "jury",
    "transfer",
    "interview analysis",
    "video analysis",
    "salary",
)
# The whole statement, after normalize() drops a final period:
# (I agree to|I accept|I have read and agree to) the
# (Terms of Service|Terms and Conditions|Terms of Use|Privacy Policy|Privacy Notice)
# (and [the] <same document>)*
_TERMS_DOCUMENT = r"(?:terms of service|terms and conditions|terms of use|privacy policy|privacy notice)"
_STANDARD_TERMS_SENTENCE = re.compile(
    rf"(?:i have read and agree to|i agree to|i accept) the {_TERMS_DOCUMENT}"
    rf"(?: and (?:the )?{_TERMS_DOCUMENT})*"
)
# The whole statement, with no free-word gap:
# I (consent|agree) to (submit|the submission of|the processing of)
# my (personal )?(information|data)
# ( for this (job )?application | for the purposes of this (application|recruitment process))?
_SUBMISSION_CONSENT = re.compile(
    r"i (?:consent|agree) to (?:submit|the submission of|the processing of) "
    r"my (?:personal )?(?:information|data)"
    r"(?: for this (?:job )?application| for the purposes of this (?:application|recruitment process))?"
)
AUTO_CONSENT_CATEGORIES = frozenset({"STANDARD_TERMS", "STANDARD_SUBMISSION_CONSENT"})
_logger = logging.getLogger("autofill.mapping")


def _phrase_re(phrases: tuple[str, ...]) -> re.Pattern[str]:
    body = "|".join(re.escape(phrase) for phrase in sorted(phrases, key=len, reverse=True))
    return re.compile(rf"\b(?:{body})\b", re.IGNORECASE)


_DENY_TERMS_RE = _phrase_re(STANDARD_TERMS_DENY)


def bind_standard_terms(enabled: bool) -> None:
    """Remember the session flag for this fill. Missing or false checks nothing."""
    _TERMS_FLAG.set(bool(enabled))
    _AUTO_CONSENTS.set([])


def standard_terms_enabled() -> bool:
    return bool(_TERMS_FLAG.get())


def consume_auto_consents() -> list[dict]:
    """Return acceptances recorded during this fill and clear them."""
    found = list(_AUTO_CONSENTS.get() or [])
    _AUTO_CONSENTS.set([])
    _TERMS_FLAG.set(False)
    return found


def _is_standard_terms(statement: str) -> bool:
    """True only when the whole statement is a fixed terms or privacy sentence."""
    text = normalize(statement)
    return bool(text) and _STANDARD_TERMS_SENTENCE.fullmatch(text) is not None


def _is_submission_consent(statement: str) -> bool:
    """True only when the whole statement is the fixed submission wording."""
    text = normalize(statement)
    return bool(text) and _SUBMISSION_CONSENT.fullmatch(text) is not None


def terms_category(control: Control) -> str:
    """``STANDARD_TERMS`` or ``STANDARD_SUBMISSION_CONSENT`` for a plain checkbox.

    The deny-list is applied first. A fixed terms or privacy sentence is
    ``STANDARD_TERMS``. Fixed data-submission wording, with no free-word gap,
    is ``STANDARD_SUBMISSION_CONSENT``. Anything else stays ``CONSENT``.
    """
    if control.kind != "checkbox":
        return "CONSENT"
    option_text = " ".join(part for option in control.options for part in (option.label, option.value) if part)
    blob = normalize(f"{haystack(control)} {control.prompt} {control.group} {option_text}")
    if _DENY_TERMS_RE.search(blob) or _MARKETING_RE.search(blob):
        return "CONSENT"
    for part in (control.label, control.prompt, control.group, control.aria_label):
        if not part:
            continue
        if _is_standard_terms(part):
            return "STANDARD_TERMS"
        if _is_submission_consent(part):
            return "STANDARD_SUBMISSION_CONSENT"
    return "CONSENT"


def record_auto_consent(control: Control, category: str = "STANDARD_TERMS") -> dict:
    """One audit row. The excerpt is the statement, never a secret."""
    from autofill.intents import question_hash

    if category not in AUTO_CONSENT_CATEGORIES:
        category = "STANDARD_TERMS"
    text = " ".join((control.label or control.prompt or control.group or control.aria_label or "").split())
    token = control.element_id or control.name or control.selector or ""
    entry = {
        "questionHash": question_hash(text, token),
        "textExcerpt": text[:160],
        "category": category,
    }
    _logger.info("auto_consent category=%s result=checked", category)
    found = list(_AUTO_CONSENTS.get() or [])
    found.append(entry)
    _AUTO_CONSENTS.set(found)
    return entry


def acknowledgement_kind(control: Control) -> str:
    """``consent``, ``marketing``, or ``factual`` for a checkbox or radio statement.

    Marketing is checked first so "I agree to receive marketing emails" is an
    opt-in. Terms, privacy, attestation, and e-signature stay consent.
    """
    if control.kind not in {"checkbox", "radio", "select"}:
        return "factual"
    option_text = " ".join(
        part for option in control.options for part in (option.label, option.value) if part
    )
    blob = normalize(f"{haystack(control)} {option_text}")
    marketing = bool(_MARKETING_RE.search(blob))
    strong = bool(_CONSENT_STRONG_RE.search(blob))
    if marketing and not strong:
        return "marketing"
    if _CONSENT_RE.search(blob) or _LEGAL_RE.search(blob):
        return "consent"
    return "factual"


def mark_required_acknowledgements(controls: list[Control]) -> None:
    """A consent statement is required until a person confirms it."""
    for control in controls:
        if control.kind == "checkbox" and acknowledgement_kind(control) == "consent":
            control.required = True


def is_honeypot(control: Control) -> bool:
    """Hidden text inputs and common trap labels are left blank."""
    blob = haystack(control)
    compact = blob.replace(" ", "")
    if "companywebsite" in compact or "company website" in blob:
        return True
    honeypot_tokens = ("honeypot", "leave blank", "leave this field blank", "do not fill", "bot field")
    if any(token in blob for token in honeypot_tokens):
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
    aliases = _alias_set(target)
    if aliases:
        for option in options:
            if normalize(option.label) in aliases or normalize(option.value) in aliases:
                return option
    from autofill.combobox import match_country_option

    return match_country_option(desired, options)


def _alias_set(token: str) -> set[str]:
    matched = [group for group in _ALIAS_GROUPS if token in group]
    if not matched:
        return set()
    return set().union(*matched)


def _wants_national_digits(control: Control) -> bool:
    if control.autocomplete == "tel-national" or control.input_mode == "numeric":
        return True
    blob = haystack(control)
    return "digits" in blob or "country code" in blob or "country prefix" in blob


def _row(items: list, cursor: MapCursor | None, kind: str):
    if not items:
        return None
    index = 0 if cursor is None else cursor.take(kind)
    if index >= len(items):
        return None
    return items[index]


def _profile_text(
    profile: CandidateProfile,
    key: str,
    control: Control,
    job: JobContext | None,
    cursor: MapCursor | None,
) -> str:
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
        # An empty preferred name stays blank. Do not substitute the legal first name.
        return personal.preferred_name.strip()
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
    if key in {"country", "phone_country"}:
        if personal.country.strip():
            return personal.country
        from autofill.combobox import country_from_phone

        return country_from_phone(personal.phone)
    if key == "location":
        parts = [personal.city, personal.province_state, personal.country]
        return ", ".join(part.strip() for part in parts if part and part.strip())
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
        row = _row(profile.education_history, cursor, "degree")
        if profile.education_history:
            return row.degree if row else ""
        return profile.experience.education_level
    if key == "school":
        row = _row(profile.education_history, cursor, "school")
        return row.school if row else ""
    if key == "field_of_study":
        row = _row(profile.education_history, cursor, "field")
        return row.field_of_study if row else ""
    if key == "current_job_title":
        row = _row(profile.employment, cursor, "title")
        if profile.employment:
            return row.title if row else ""
        return profile.experience.current_job_title
    if key == "current_company":
        row = _row(profile.employment, cursor, "employer")
        if profile.employment:
            return row.company if row else ""
        return profile.experience.current_company
    if key == "employment_start":
        row = _row(profile.employment, cursor, "employment_start")
        return row.start_date if row else ""
    if key == "education_start":
        row = _row(profile.education_history, cursor, "education_start")
        return row.start_date if row else ""
    if key == "internship_company":
        row = _row(profile.internships, cursor, "internship")
        return row.company if row else ""
    if key == "project_name":
        row = _row(profile.projects, cursor, "project")
        return row.name if row else ""
    if key == "project_description":
        row = _row(profile.projects, cursor, "project_description")
        return row.description if row else ""
    if key in {"gender", "race_ethnicity", "veteran_status", "disability_status"}:
        return ""
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


def _nearby_key(control: Control) -> str | None:
    """A match that exists only in surrounding heading or legend text."""
    if not control.nearby.strip():
        return None
    probe = Control(kind=control.kind, label=control.nearby, options=control.options, input_type=control.input_type)
    return _match_rule(probe)


def _not_employer_question(blob: str) -> bool:
    """Referral, relative, and contact questions are not the employer field."""
    return bool(
        re.search(
            r"\b(referral|referrer|referred|relative|relatives|contact)\b",
            blob,
        )
        or "who referred" in blob
    )


def is_current_company_label(control: Control) -> bool:
    """True for a company box, not a question that merely mentions an employer.

    Short work-history labels (``Employer``, ``Company name``) still match.
    ``current company``, ``current employer``, and ``most recent employer`` match.
    A referral, a previous-employer contact, or relatives employed do not.
    """
    blob = haystack(control)
    if _not_employer_question(blob):
        return False
    label = normalize(control.label or control.aria_label or control.placeholder)
    if label in {
        "employer",
        "company",
        "company name",
        "current company",
        "current employer",
        "most recent employer",
        "most recent company",
    }:
        return True
    return bool(
        re.search(
            r"\b(current company|current employer|most recent employer|most recent company)\b",
            label,
        )
    )


def mentions_work_history(control: Control) -> bool:
    """A work-history company or title row, not a referral or contact question.

    A restrictive-covenant question mentions a former employer. It is not the
    company box.
    """
    blob = haystack(control)
    if _not_employer_question(blob):
        return False
    if re.search(r"\b(covenant|non-?compete|noncompete|non-?solicit|nonsolicit)\b", blob):
        return False
    if "?" in f"{control.label} {control.aria_label} {control.placeholder}":
        return False
    if is_current_company_label(control):
        return True
    label = normalize(control.label or control.aria_label or control.placeholder)
    if len(label) > 40:
        return False
    return bool(re.search(r"\b(employer|employment|company)\b", label))


def match_field_key(control: Control) -> str | None:
    """Internal profile key for a control, or None when nothing matched.

    This does not read a profile value. Resolver mode uses it only to decide
    which normalized key to request.
    """
    if control.kind == "password" or control.input_type == "password":
        return None
    if control.disabled or control.hidden or control.read_only:
        return None
    if is_honeypot(control):
        return None
    return _match_rule(control)


def _is_referral_question(blob: str) -> bool:
    """A referrer-name question. ``preferred`` does not contain the word referred.

    ``Referral source`` is how the candidate heard about the role, not the
    name of the person who referred them.
    """
    if "referral source" in blob or "source of referral" in blob:
        return False
    return bool(
        re.search(r"\b(referral|referrer|referred)\b", blob)
        or "who referred" in blob
        or "employee referral" in blob
    )


_DIAL_CODE = re.compile(r"\+\d{1,4}\b")


def _is_phone_country_menu(control: Control) -> bool:
    """A calling-code menu, or a Country select sitting on the phone input.

    Rows such as ``United States +1`` are the menu. A closed menu has no rows
    yet, so a Country combobox next to the phone input is the same field.
    """
    hits = 0
    for option in control.options:
        if _DIAL_CODE.search(f"{option.label} {option.value}"):
            hits += 1
            if hits >= 2:
                return True
    if control.kind not in {"select", "combobox"}:
        return False
    label = normalize(control.label or control.aria_label)
    if label not in {"country", "country code", "phone country"}:
        return False
    if control.phone_adjacent:
        return True
    nearby = normalize(control.nearby)
    return nearby in {"phone", "mobile", "telephone", "mobile phone"}


def _match_rule(control: Control) -> str | None:
    autocomplete = control.autocomplete.strip().casefold()
    blob = haystack(control)
    if _is_referral_question(blob):
        return None
    if re.search(r"\bpreferred\b", blob) and re.search(r"\bname\b", blob):
        return "preferred_name"
    if _is_phone_country_menu(control):
        return "phone_country"
    if autocomplete in _AUTOCOMPLETE and (control.kind != "file"):
        # Autocomplete is a stronger signal than a noisy label, except files.
        mapped = _AUTOCOMPLETE[autocomplete]
        if mapped == "current_company" and not is_current_company_label(control):
            mapped = ""
        if mapped:
            if autocomplete == "tel-national":
                return "phone"
            return mapped
    if "country code" in blob or "dial code" in blob or "calling code" in blob:
        return "country"
    label = normalize(control.label or control.aria_label or control.placeholder)
    if label in {"current location", "location", "your location"} and "relocat" not in label:
        return "location"
    if (
        re.search(r"\b(working from|payroll tax)\b", blob)
        and "relocat" not in blob
        and "sponsor" not in blob
        and "visa" not in blob
    ):
        return "location"
    for rule in _RULES:
        if rule.kinds is not None and control.kind not in rule.kinds:
            continue
        if rule.key == "years_of_experience_total" and _specific_years_question(blob):
            continue
        if rule.key == "current_company" and not is_current_company_label(control):
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
    # A closed country menu has no options yet. Choose it live instead of typing into the inner input.
    if key in {"country", "phone_country"} and control.kind == "combobox":
        if control.options:
            option = match_option(text, control.options)
            if option is not None:
                return MappedField(
                    key=key,
                    action="select",
                    text=text,
                    option_label=option.label,
                    option_value=option.value,
                    option_selector=option.selector,
                    reason=reason,
                    field_class="PROFILE_FIELD",
                )
        return MappedField(
            key=key,
            action="select",
            text=text,
            option_label=text,
            reason=reason or "Country is chosen from the menu.",
            field_class="PROFILE_FIELD",
        )
    if control.kind in {"select", "radio"} or (control.kind == "combobox" and control.options):
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
    if _is_date_key(key) or control.input_type in {"date", "month"} or _date_hint(control):
        formatted = format_for_control(
            text,
            input_type=control.input_type,
            placeholder=control.placeholder,
            label=control.label,
        )
        if formatted is None:
            return MappedField(
                key=key,
                action="unanswered",
                field_class="PROFILE_FIELD",
                reason=f"Date input needs a calendar date; profile value is {text!r}.",
            )
        text = formatted
    return MappedField(key=key, action="fill", text=text, reason=reason, field_class="PROFILE_FIELD")


def _is_date_key(key: str) -> bool:
    return key in {"earliest_start_date", "employment_start", "education_start"}


def _date_hint(control: Control) -> bool:
    hint = normalize(f"{control.placeholder} {control.label}")
    return "mm/dd/yyyy" in hint or "dd/mm/yyyy" in hint or "yyyy-mm-dd" in hint


def map_field(
    control: Control,
    profile: CandidateProfile,
    *,
    resume_path: str | None = None,
    cover_letter_path: str | None = None,
    cover_letter_text: str | None = None,
    job: JobContext | None = None,
    cursor: MapCursor | None = None,
) -> MappedField:
    """Decide how to fill one control. Does not touch the page.

    ``resume_path`` is ignored for uploading. A resume file control is always
    ``resume_required`` so a person uploads the file TileArc already downloaded.
    ``cover_letter_text`` is used only when the caller passes it explicitly.
    """
    del resume_path  # kept for callers; resumes are a human checkpoint
    if control.kind == "password" or control.input_type == "password":
        return MappedField(
            key=None,
            action="skip",
            field_class="LOGIN_FIELD",
            reason="Password fields are filled only by the login flow.",
        )
    if control.disabled:
        return MappedField(key=None, action="skip", reason="Control is disabled.")
    if control.read_only and control.kind in {"text", "textarea"}:
        return MappedField(key=None, action="skip", reason="Control is read-only.")
    if control.hidden:
        return MappedField(key=None, action="skip", reason="Control is hidden.")
    if is_honeypot(control):
        return MappedField(key=None, action="skip", reason="Honeypot or hidden text field left blank.")
    if is_consent_choice(control):
        return MappedField(
            key=None,
            action="skip",
            field_class="LEGAL_FIELD",
            reason="Consent choice left for a person.",
        )
    if control.kind == "checkbox" and acknowledgement_kind(control) == "marketing":
        return MappedField(key=None, action="skip", reason="Marketing opt-in left unchecked.")
    if control.kind in {"checkbox", "radio", "select"} and (
        acknowledgement_kind(control) == "consent" or _LEGAL_RE.search(haystack(control))
    ):
        return MappedField(
            key=None,
            action="skip",
            field_class="LEGAL_FIELD",
            reason="Legal attestation left for a person.",
        )

    own = Control(
        kind=control.kind,
        name=control.name,
        element_id=control.element_id,
        label=control.label,
        placeholder=control.placeholder,
        aria_label=control.aria_label,
        autocomplete=control.autocomplete,
        options=control.options,
        input_type=control.input_type,
        input_mode=control.input_mode,
        role=control.role,
        phone_adjacent=control.phone_adjacent
        or normalize(control.nearby) in {"phone", "mobile", "telephone", "mobile phone"},
    )
    key = _match_rule(own)
    if key is None and _nearby_key(control):
        return MappedField(
            key=None,
            action="unanswered",
            field_class="MANUAL_REVIEW_FIELD",
            confidence="low",
            reason="Low-confidence nearby text was not used.",
        )
    if key is not None and control.kind == "file" and key not in {"resume", "cover_letter"}:
        return MappedField(key=key, action="unanswered", reason="File input did not match a resume or cover letter.")
    if key is None:
        answered = _explicit_answer(control, profile)
        if answered is not None:
            return answered
        skill = _skill_answer(control, profile)
        if skill is not None:
            skill.field_class = "APPROVED_QUESTION" if skill.action != "unanswered" else "UNKNOWN_FIELD"
            return skill
        return MappedField(
            key=None,
            action="unanswered",
            field_class="UNKNOWN_FIELD",
            reason="No profile field matched this control.",
        )

    key = _retarget_date(key, cursor)
    if key in {"gender", "race_ethnicity", "veteran_status", "disability_status"}:
        return MappedField(
            key=key,
            action="unanswered",
            field_class="MANUAL_REVIEW_FIELD",
            reason="Demographic questions are left blank.",
        )
    if key == "resume":
        return MappedField(
            key=key,
            action="resume_required",
            field_class="RESUME_FIELD",
            reason="A person uploads the resume. Auto-Fill does not choose a file.",
        )
    if key == "cover_letter" and control.kind == "file":
        if not cover_letter_path:
            return MappedField(
                key=key,
                action="unanswered",
                field_class="MANUAL_REVIEW_FIELD",
                reason="No cover letter file was passed to the API.",
            )
        return MappedField(key=key, action="upload", text=cover_letter_path, field_class="PROFILE_FIELD")

    if key in {"salary", "salary_range"} and not profile.compensation.salary_expectation:
        return MappedField(
            key=key,
            action="unanswered",
            field_class="MANUAL_REVIEW_FIELD",
            reason="Profile has no salary expectation.",
        )
    # An explicit approved answer beats a screening.* value for the same question.
    # Other profile fields stay as mapped. A question with no approved answer is unchanged.
    if key in _SCREENING_KEYS:
        approved = _explicit_answer(control, profile)
        if approved is not None and approved.action != "unanswered":
            return approved
    try:
        text = _profile_text(profile, key, control, job, cursor)
    except ValueError as exc:
        return MappedField(key=key, action="unanswered", reason=str(exc))
    if not text:
        answered = _explicit_answer(control, profile)
        if answered is not None and answered.action != "unanswered":
            return answered
    if key == "cover_letter" and cover_letter_text is not None:
        text = cover_letter_text
    mapped = _from_text(control, key, text)
    if mapped.field_class == "":
        mapped.field_class = "PROFILE_FIELD" if mapped.action != "unanswered" else "UNKNOWN_FIELD"
    if key in {"legally_authorized_to_work", "require_sponsorship", "salary", "salary_range"}:
        mapped.field_class = "PROFILE_FIELD" if text else "MANUAL_REVIEW_FIELD"
    return mapped


def _retarget_date(key: str, cursor: MapCursor | None) -> str:
    if key != "earliest_start_date" or cursor is None:
        return key
    heading = normalize(cursor.page_heading)
    if "education" in heading:
        return "education_start"
    if "experience" in heading or "employment" in heading:
        return "employment_start"
    return key


def _explicit_answer(control: Control, profile: CandidateProfile) -> MappedField | None:
    if not profile.application_answers:
        return None
    text = match_answer(haystack(control), profile.application_answers, normalize=normalize)
    if text is None:
        return None
    mapped = _from_text(control, "application_answer", text, reason="Explicit application answer.")
    mapped.field_class = "APPROVED_QUESTION" if mapped.action != "unanswered" else "UNKNOWN_FIELD"
    return mapped
