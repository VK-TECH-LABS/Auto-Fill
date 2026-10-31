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

from autofill.dates import format_for_control
from autofill.models import Control, JobContext, MappedField, Option
from autofill.profile import CandidateProfile
from autofill.salary import SalaryAssessment, assess_salary, salary_shape, salary_value_covers

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
# Statements that stay manual even when they are not a fixed terms sentence.
# Demographic words are classified separately so they stay DEMOGRAPHIC.
_ATTESTATION_RE = re.compile(
    r"\b(background check|e-?sign|electronic signature|arbitration|certify|attest|attestation"
    r"|recording|text messages?|sms)\b"
)
_DEMOGRAPHIC_TEXT_RE = re.compile(r"\b(gender|race|ethnicity|veteran|disability)\b")
_TERMS_KINDS = frozenset({"checkbox", "radio", "select", "combobox", "buttons"})
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
        _compile(
            (
                r"\bsalary range\b",
                r"\bcompensation range\b",
                r"\bpay range\b",
                r"\bwage range\b",
            )
        ),
    ),
    _Rule(
        "salary",
        _compile(
            (
                r"\bsalary\b",
                r"\bcompensation\b",
                r"\bdesired pay\b",
                r"\bpay expectation\b",
                r"\bexpected pay\b",
                r"\bbase pay\b",
                r"\btarget pay\b",
                r"\bpay rate\b",
                r"\bhourly rate\b",
                r"\bper hour\b",
                r"\bwage expectation\b",
                r"\bremuneration\b",
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
# True once this fill has committed a United States address country.
_US_COMMITTED: contextvars.ContextVar[bool] = contextvars.ContextVar("autofill_us_committed", default=False)


def bind_us_page() -> contextvars.Token[bool]:
    """Start a fill with no committed address country. Reset the token when it ends."""
    return _US_COMMITTED.set(False)


def reset_us_page(token: contextvars.Token[bool]) -> None:
    _US_COMMITTED.reset(token)


def mark_us_committed() -> None:
    """The page's address country is the United States for the rest of this fill."""
    _US_COMMITTED.set(True)


def wrote_us_country(field_key: str, value: str) -> bool:
    """True when this write is an address country and the value is the United States."""
    from autofill.intents import country_is_us

    key = field_key.casefold()
    if "phone" in key:
        return False
    if key != "country" and not key.endswith(".country"):
        return False
    return country_is_us(value)


def note_page_country(page: object, snapshot: object) -> None:
    """If a country control already shows the United States, remember that."""
    from autofill.intents import country_is_us
    from autofill.stepfill import live_field_value

    if _US_COMMITTED.get() or page is None:
        return
    controls = getattr(snapshot, "controls", None) or []
    for control in controls:
        if getattr(control, "hidden", False) or match_field_key(control) != "country":
            continue
        shown = live_field_value(page, control) or ""
        if country_is_us(str(shown)):
            mark_us_committed()
            return


def us_context(profile: CandidateProfile | None = None) -> bool:
    """True when this application already has a United States address country.

    The profile country counts when it is the address we fill. A country
    control that already shows the United States counts too. A question that
    names no country is not assumed to be American without one of those.
    """
    from autofill.intents import country_is_us

    if _US_COMMITTED.get():
        return True
    person = profile
    if person is None:
        from autofill.stepfill import current_location_profile

        person = current_location_profile()
    if person is None:
        return False
    return country_is_us(person.personal.country)


def question_stem(control: Control) -> str:
    """The question a person would read. Not the haystack and not a placeholder.

    Order is group, prompt, label, then the accessible name. A section title
    in all capitals is skipped. When one of those is the question (it contains
    ``?``) it wins over a shorter option label such as ``Yes``.
    """
    options = {normalize(option.label) for option in control.options}
    options.update(normalize(option.value) for option in control.options if option.value)
    candidates: list[str] = []
    for raw in (control.group, control.prompt, control.label, control.aria_label):
        cleaned = (raw or "").strip()
        if not cleaned:
            continue
        letters = [char for char in cleaned if char.isalpha()]
        if "?" not in cleaned and len(letters) >= 3 and all(char.isupper() for char in letters):
            continue
        if len(options) > 1 and normalize(cleaned) in options:
            continue
        if cleaned not in candidates:
            candidates.append(cleaned)
    for text in candidates:
        if "?" in text:
            return text
    return candidates[0] if candidates else ""


def employer_keys_for(job: JobContext | None, url: str = "") -> tuple[str, ...]:
    """This session's company and tenant scope, for employer-scoped answers."""
    from autofill.answers import employer_keys

    company = job.company if job is not None else ""
    target = url or (job.url if job is not None else "")
    return employer_keys(company=company, url=target)


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
    """A choice control that offers both a consent option and a decline option."""
    if control.kind not in {"radio", "select", "combobox", "buttons"}:
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
# [yes] <verb> [the] <document> (and [the] <document>)* [for|of|at <owner>]
# for example "I agree to the Terms of Service", "Yes, I have read the Applicant
# Privacy Policy", "I acknowledge that I have read and understand the Privacy Notice".
# A bare "terms" is accepted only without an owner tail ("I agree to the terms",
# "I agree to the terms and privacy policy"); see ``_is_standard_terms``.
_PRIVACY_OWNER = (
    r"(?:candidate|applicant|job applicant|recruitment|recruiting|career|careers|global|data"
    r"|personal data|data protection|employee|hr)"
)
_TERMS_DOCUMENT = (
    r"(?:terms of service|terms and conditions|terms conditions|terms of use"
    rf"|(?:{_PRIVACY_OWNER} ){{0,2}}privacy (?:policy|notice|statement)"
    r"|data protection (?:policy|notice|statement)"
    r"|terms)"
)
# Workday tenants word the Create Account box as, for example,
# "Yes, I have read and consent to the terms and conditions" or
# "I understand and acknowledge the terms of use for Example Company (EC)".
# The optional tail names whose document it is, in at most eight plain words,
# and never carries another verb or a second promise (checked in code).
_READ_OBJECT = r"(?:agree to|agree with|accept|consent to|acknowledge|understand|understood)"
_TERMS_VERB = (
    rf"(?:i have (?:read|reviewed)(?: and {_READ_OBJECT})?"
    rf"|i have (?:read|reviewed) understood and {_READ_OBJECT}"
    rf"|i have (?:read|reviewed) and understood and {_READ_OBJECT}"
    rf"|i (?:acknowledge|confirm) that i have (?:read|reviewed)(?: and {_READ_OBJECT})?"
    r"|i acknowledge receipt of"
    r"|i understand and (?:agree to|accept|acknowledge)"
    r"|i agree to|i agree with|i accept|i consent to|i acknowledge)"
)
_STANDARD_TERMS_SENTENCE = re.compile(
    rf"(?:yes )?{_TERMS_VERB} (?:the )?{_TERMS_DOCUMENT}"
    rf"(?: and (?:the )?{_TERMS_DOCUMENT})*"
    r"(?P<tail> (?:for|of|at) (?:[a-z0-9]+ ){0,7}[a-z0-9]+)?"
)
# "terms" not followed by one of the named documents.
_BARE_TERMS = re.compile(r"\bterms\b(?! (?:of service|of use|and conditions|conditions)\b)")
_TERMS_TAIL_STOP = frozenset(
    {"and", "or", "to", "i", "me", "my", "that", "agree", "accept", "consent", "receive", "share", "allow", "use"}
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
# Recruitment SMS is a separate Application Answer, not a standard-terms checkbox.
AUTO_CONSENT_CATEGORIES = frozenset(
    {"STANDARD_TERMS", "STANDARD_SUBMISSION_CONSENT", "RECRUITMENT_SMS_CONSENT"}
)
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
    if not text:
        return False
    match = _STANDARD_TERMS_SENTENCE.fullmatch(text)
    if match is None:
        return False
    tail = match.group("tail") or ""
    if tail and _BARE_TERMS.search(text[: match.start("tail")]):
        # "I agree to the terms of <anything>" names an unknown document.
        return False
    return not (set(tail.split()[1:]) & _TERMS_TAIL_STOP)


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
    if control.kind not in _TERMS_KINDS:
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
    if control.kind not in _TERMS_KINDS:
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


def is_demographic_control(control: Control) -> bool:
    """Gender, race, veteran, and disability questions stay demographic."""
    return bool(_DEMOGRAPHIC_TEXT_RE.search(haystack(control)))


def attestation_manual(control: Control) -> bool:
    """Background-check, e-sign, arbitration, certification, SMS, and recording stay manual."""
    if control.kind not in _TERMS_KINDS:
        return False
    option_text = " ".join(part for option in control.options for part in (option.label, option.value) if part)
    blob = normalize(f"{haystack(control)} {option_text}")
    return bool(_ATTESTATION_RE.search(blob))


def mark_required_acknowledgements(controls: list[Control]) -> None:
    """A consent statement is required until a person confirms it.

    A statement the employer labels "(optional)" stays optional.
    """
    for control in controls:
        if control.kind == "checkbox" and acknowledgement_kind(control) == "consent":
            if _OPTIONAL_LABEL_RE.search(control.label or ""):
                continue
            control.required = True


_OPTIONAL_LABEL_RE = re.compile(r"\(\s*optional\s*\)", re.IGNORECASE)


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
        # "Yes, I am authorized" for Yes. Two such options ("Yes, ..." and
        # "Yes, but ...") are a choice for the employee, not a guess.
        leading = [option for option in options if normalize(option.label).split(" ")[:1] == [answer]]
        if len(leading) == 1:
            return leading[0]
        if len(leading) > 1:
            return None
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


_CALLING_CODES = ("44", "1")
_NANP_WITH_COUNTRY = re.compile(r"^1\d{10}$")


def national_phone_digits(phone: str) -> str:
    """Digits for a national box when the calling code is a separate control.

    ``+1 (555) 010-0199`` and ``1-555-010-0199`` become ``5550100199``.
    A number that is already national (``555-010-0199``) keeps those digits.
    An explicit ``+`` / ``00`` prefix drops a known calling code. A punctuated
    11-digit number that starts with 1 drops that leading 1. Other digit
    strings are left intact so an unformatted non-NANP number is not truncated.
    """
    raw = str(phone or "").strip()
    compact = re.sub(r"[^\d+]", "", raw)
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    digits = re.sub(r"\D", "", compact)
    if not digits:
        return ""
    if compact.startswith("+"):
        for code in _CALLING_CODES:
            rest = digits[len(code) :]
            if digits.startswith(code) and len(rest) >= 7:
                return rest
        return digits
    if re.search(r"[()\-.]", raw) and _NANP_WITH_COUNTRY.fullmatch(digits):
        return digits[1:]
    return digits


def _wants_national_digits(control: Control) -> bool:
    """True when the box wants a national number, not the full profile phone.

    ``tel-national``, a numeric keypad, or copy that says digits / country code
    are the explicit cases. A phone input beside a separate calling-code menu
    is the same rule: the country control already carries the prefix.
    """
    if control.national_phone:
        return True
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


def _salary_question_shape(control: Control, key: str) -> str:
    """Range, minimum, maximum, or a single figure. The rule key breaks a tie.

    The shape comes from the label. A selector such as ``#x`` is not part of
    the question, so it cannot turn "Salary range minimum" into a full range.
    """
    label = " ".join(
        part
        for part in (control.label, control.aria_label, control.placeholder, control.prompt)
        if part
    )
    shape = salary_shape(label or haystack(control))
    if key == "salary_range" and shape == "single":
        return "range"
    return shape


def _salary_decision(
    control: Control,
    profile: CandidateProfile,
    job: JobContext | None,
    key: str,
) -> SalaryAssessment:
    blob = haystack(control)
    return assess_salary(
        profile.compensation,
        shape=_salary_question_shape(control, key),
        posted_min=job.posted_salary_min if job else None,
        posted_max=job.posted_salary_max if job else None,
        hourly="hourly" in blob or "per hour" in blob,
    )


def _explicit_salary(
    control: Control,
    profile: CandidateProfile,
    shape: str,
    *,
    job: JobContext | None = None,
) -> MappedField | None:
    """An explicit answer for this question, when that answer covers the shape.

    A saved minimum does not satisfy a range question through this path.
    """
    approved = _explicit_answer(control, profile, job=job)
    if approved is None or approved.action == "unanswered":
        return None
    if not salary_value_covers(approved.text, shape):
        return None
    return approved


def _map_salary(
    control: Control,
    profile: CandidateProfile,
    job: JobContext | None,
    key: str,
) -> MappedField:
    """Fill a salary question, or ask a person to confirm an incomplete one."""
    shape = _salary_question_shape(control, key)
    try:
        decision = _salary_decision(control, profile, job, key)
    except ValueError as exc:
        return MappedField(key=key, action="unanswered", reason=str(exc))
    if decision.status == "answer":
        mapped = _from_text(control, key, decision.value)
        if mapped.action == "unanswered":
            return mapped
        mapped.field_class = "PROFILE_FIELD"
        mapped.confidence = "high"
        mapped.related_fields = decision.related_fields
        return mapped
    explicit = _explicit_salary(control, profile, shape, job=job)
    if explicit is not None:
        return explicit
    if decision.status == "confirm":
        return MappedField(
            key=key,
            action="unanswered",
            field_class="MANUAL_REVIEW_FIELD",
            confidence="low",
            reason=decision.reason,
            related_fields=decision.related_fields,
            challenge="confirm",
        )
    return MappedField(
        key=key,
        action="unanswered",
        field_class="MANUAL_REVIEW_FIELD",
        reason="Profile has no salary expectation.",
    )


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
            return national_phone_digits(personal.phone)
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
        decision = _salary_decision(control, profile, job, key)
        if decision.status != "answer":
            return ""
        return decision.value
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
_PHONE_COUNTRY_LABELS = frozenset(
    {
        "country code",
        "phone country",
        "country phone code",
        "phone code",
        "calling code",
        "dial code",
    }
)


def _is_phone_country_menu(control: Control) -> bool:
    """A calling-code menu, or a Country select sitting on the phone input.

    Rows such as ``United States +1`` are the menu. A closed menu has no rows
    yet, so a Country combobox next to the phone input is the same field.
    ``Country Phone Code`` is that menu even when it is closed. An address
    ``Country / Territory`` menu is not.
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
    if label in _PHONE_COUNTRY_LABELS:
        return True
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
        # A text dial-code box is not an address. Leave a prefilled +1 alone.
        if control.kind not in {"select", "combobox"}:
            return None
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
    # A closed search prompt has no rows until it is opened. Choose the saved
    # source from that menu instead of leaving the placeholder in the box.
    if key == "how_heard" and control.kind == "combobox" and not control.options:
        return MappedField(
            key=key,
            action="select",
            text=text,
            option_label=text,
            reason=reason or "Source is chosen from the menu.",
            field_class="PROFILE_FIELD",
        )
    # A Workday Application Questions dropdown is a button whose menu exists
    # only after it is opened. Do not type the answer into the button.
    if control.kind == "buttons" and control.role == "listbox" and not control.options:
        return MappedField(
            key=key,
            action="select",
            text=text,
            option_label=text,
            reason=reason or "Choice is taken from the menu.",
        )
    if control.kind in {"select", "radio"} or (control.kind in {"combobox", "buttons"} and control.options):
        from autofill.intents import pick_option

        status, option = pick_option(text, control.options)
        if status == "ambiguous":
            return MappedField(
                key=key,
                action="unanswered",
                reason="Several options match this value.",
            )
        if option is None:
            # Country and state aliases stay on the closed alias list.
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
        if control.input_type in {"aria-checkbox", "switch"} or control.role == "switch":
            return MappedField(
                key=key,
                action="unanswered",
                reason="Custom checkbox left for a person.",
            )
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
    from autofill.sms_consent import recruitment_sms_field

    sms = recruitment_sms_field(control, profile)
    if sms is not None:
        return sms
    if is_demographic_control(control):
        key = _match_rule(
            Control(
                kind=control.kind,
                label=control.label,
                name=control.name,
                aria_label=control.aria_label,
                prompt=control.prompt,
                group=control.group,
            )
        )
        return MappedField(
            key=key if key in {"gender", "race_ethnicity", "veteran_status", "disability_status"} else None,
            action="unanswered",
            field_class="MANUAL_REVIEW_FIELD",
            reason="Demographic questions are left blank.",
        )
    if attestation_manual(control) and acknowledgement_kind(control) == "factual":
        return MappedField(
            key=None,
            action="skip",
            field_class="LEGAL_FIELD",
            reason="This statement stays manual.",
        )
    if is_consent_choice(control):
        return MappedField(
            key=None,
            action="skip",
            field_class="LEGAL_FIELD",
            reason="Consent choice left for a person.",
        )
    if control.kind in _TERMS_KINDS and acknowledgement_kind(control) == "marketing":
        return MappedField(key=None, action="skip", reason="Marketing opt-in left unchecked.")
    if control.kind in _TERMS_KINDS and (
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
        answered = _explicit_answer(control, profile, job=job)
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

    if key in {"salary", "salary_range"}:
        return _map_salary(control, profile, job, key)
    if key in {"legally_authorized_to_work", "require_sponsorship"}:
        semantic = _map_work_authorization(control, profile, key, job=job)
        if semantic is not None:
            return semantic
    try:
        text = _profile_text(profile, key, control, job, cursor)
    except ValueError as exc:
        return MappedField(key=key, action="unanswered", reason=str(exc))
    # Screening questions: the Application Profile first, then an approved answer
    # for the same question. When both exist and disagree, the employee decides.
    if key in _SCREENING_KEYS:
        approved = _explicit_answer(control, profile, job=job)
        if approved is not None and approved.action != "unanswered":
            if text and not _same_answer(text, approved.text):
                return _ask_person(key, "Application Profile and an approved answer disagree for this question.")
            if not text:
                return approved
    if not text:
        answered = _explicit_answer(control, profile, job=job)
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


def _question_texts(control: Control) -> list[str]:
    """The question stem only. Placeholders and nearby text are not the question."""
    stem = question_stem(control)
    return [stem] if stem else []


def _same_answer(left: str, right: str) -> bool:
    left_flag, right_flag = yes_no(left), yes_no(right)
    if left_flag or right_flag:
        return left_flag == right_flag
    return normalize(left) == normalize(right)


def _ask_person(key: str | None, reason: str) -> MappedField:
    """Leave the field for the employee. A required one becomes MANUAL_ANSWER_REQUIRED."""
    return MappedField(
        key=key,
        action="unanswered",
        field_class="MANUAL_REVIEW_FIELD",
        confidence="low",
        reason=reason,
    )


def _work_value(intent: str, text: str, authorized: str, sponsorship: str) -> str | None:
    """The answer to one work-authorization intent from the two profile facts.

    ``sponsorship`` is TileArc's "needs sponsorship (now or later)" fact. None
    means the profile does not answer this exact question.
    """
    from autofill.intents import combine_authorized_without, compound_plan

    auth_flag, sponsor_flag = yes_no(authorized), yes_no(sponsorship)
    if intent == "US_WORK_AUTHORIZATION":
        return authorized
    if intent == "AUTHORIZED_ANY_EMPLOYER":
        if auth_flag == "no":
            return authorized
        if sponsor_flag == "yes":
            return None
        return authorized
    if intent in {"SPONSORSHIP_FUTURE", "SPONSORSHIP_NOW_OR_FUTURE"}:
        return sponsorship
    if intent == "SPONSORSHIP_NOW":
        # Needing sponsorship later does not say whether it is needed today.
        return None if sponsor_flag == "yes" else sponsorship
    if intent == "AUTHORIZED_WITHOUT_SPONSORSHIP":
        components, _ = compound_plan(text, intent)
        if sponsor_flag == "yes" and "SPONSORSHIP_FUTURE" not in components:
            return None
        authorizations = [("HIGH", authorized)] if authorized else []
        sponsorships = [("HIGH", sponsorship)] if sponsorship else []
        return combine_authorized_without(authorizations, sponsorships)
    return ""


def _map_work_authorization(
    control: Control,
    profile: CandidateProfile,
    key: str,
    *,
    job: JobContext | None = None,
) -> MappedField | None:
    """Answer the work-authorization question that is actually asked.

    Returns None when the question has no known intent; the caller then maps the
    profile field as before. A question that names no country does not use the
    U.S. profile facts unless this page's address country is the United States.
    """
    from autofill.answers import _JURISDICTION_INTENTS, question_intent
    from autofill.intents import countries_named

    texts = _question_texts(control)
    on_page = us_context(profile)
    approved = _explicit_answer(control, profile, job=job)
    if approved is not None and approved.action == "unanswered":
        approved = None
    named = countries_named(texts[0]) if texts else frozenset()
    if named and named != frozenset({"US"}):
        # The profile's work facts are for the United States only.
        if approved is not None:
            return approved
        return _ask_person(key, "This question asks about a country other than the United States.")
    if texts and not named and not on_page:
        if approved is not None:
            return approved
        return _ask_person(key, "This question does not name the United States.")
    intent, text = question_intent(texts, us_on_page=on_page)
    if intent not in _JURISDICTION_INTENTS:
        return None
    authorized = profile.work_authorization.legally_authorized_to_work.strip()
    sponsorship = profile.work_authorization.require_sponsorship.strip()
    value = _work_value(intent, text, authorized, sponsorship)
    if value is None:
        if approved is not None:
            return approved
        return _ask_person(key, "The Application Profile does not answer this exact work-authorization question.")
    # An empty work-authorization fact is not an answer. The saved Application
    # Answer is used instead, including by the caller when this returns None.
    if not value:
        return approved
    if approved is not None and not _same_answer(value, approved.text):
        return _ask_person(key, "Application Profile and an approved answer disagree for this question.")
    mapped = _from_text(control, key, value)
    mapped.field_class = "PROFILE_FIELD" if mapped.action != "unanswered" else "MANUAL_REVIEW_FIELD"
    return mapped


def _fact_key(question: str) -> str:
    """Catalogue key for a government-employment or legal-restriction question.

    Empty when the question is not one of those facts. A missing row is not a No.
    """
    folded = normalize(question)
    if "legal restriction" in folded:
        return "legal_restrictions"
    if "government" in folded and ("spouse" in folded or "partner" in folded):
        return "spouse_gov"
    if "government" in folded and any(word in folded for word in ("employee", "employed", "agency")):
        return "current_former_gov"
    return ""


def _keyed_fact_answer(question: str, answers: list) -> str | None:
    """The saved answer for this fact, only when the profile has that one row."""
    key = _fact_key(question)
    if not key:
        return None
    rows = [item for item in answers if getattr(item, "key", "") == key and str(item.answer or "").strip()]
    if len(rows) != 1:
        return None
    return rows[0].answer


def _explicit_answer(
    control: Control,
    profile: CandidateProfile,
    *,
    job: JobContext | None = None,
) -> MappedField | None:
    if not profile.application_answers:
        return None
    from autofill.answers import catalogue_match

    texts = _question_texts(control)
    if not texts:
        return None
    text, status = catalogue_match(
        texts,
        profile.application_answers,
        normalize=normalize,
        us_on_page=us_context(profile),
        employers=employer_keys_for(job),
    )
    if status == "conflict":
        return MappedField(
            key="application_answer",
            action="unanswered",
            field_class="MANUAL_REVIEW_FIELD",
            reason="Saved answers disagree for this question.",
        )
    if text is None:
        text = _keyed_fact_answer(texts[0], profile.application_answers)
    if text is None:
        return None
    mapped = _from_text(control, "application_answer", text, reason="Explicit application answer.")
    mapped.field_class = "APPROVED_QUESTION" if mapped.action != "unanswered" else "UNKNOWN_FIELD"
    return mapped
