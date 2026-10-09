"""Question-intent classification for protocol 0.4.0.

Level 1 is deterministic normalization and synonyms. Level 2 is a local
token-overlap matcher with no network and no language model. Level 3 is an
optional in-process hook, off by default: it may see sanitized question text,
option labels, and the intent catalogue, and it may return an intent only.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from autofill.mapping import normalize
from autofill.models import Option

PROTOCOL_INTENTS: tuple[str, ...] = (
    "US_WORK_AUTHORIZATION",
    "AUTHORIZED_ANY_EMPLOYER",
    "AUTHORIZED_WITHOUT_SPONSORSHIP",
    "SPONSORSHIP_NOW",
    "SPONSORSHIP_NOW_OR_FUTURE",
    "SPONSORSHIP_FUTURE",
    "AGE_18_PLUS",
    "RELOCATE",
    "TRAVEL_PERCENT",
    "SHIFT_AVAILABILITY",
    "SECURITY_CLEARANCE",
    "PE_LICENSE",
    "DRIVERS_LICENSE",
    "SALARY_EXPECTATION",
    "EMPLOYMENT_TYPE",
    "START_DATE",
    "PREVIOUSLY_EMPLOYED",
    "GENDER",
    "RACE_ETHNICITY",
    "VETERAN_STATUS",
    "DISABILITY_STATUS",
)

# Demographic questions are never written by the engine.
DEMOGRAPHIC_INTENTS = frozenset(
    {
        "GENDER",
        "RACE_ETHNICITY",
        "VETERAN_STATUS",
        "DISABILITY_STATUS",
    }
)

# Legal, work-authorization, and demographic intents are never filled below HIGH.
STRICT_INTENTS = frozenset(
    {
        "US_WORK_AUTHORIZATION",
        "AUTHORIZED_ANY_EMPLOYER",
        "AUTHORIZED_WITHOUT_SPONSORSHIP",
        "SPONSORSHIP_NOW",
        "SPONSORSHIP_NOW_OR_FUTURE",
        "SPONSORSHIP_FUTURE",
        "AGE_18_PLUS",
        "SECURITY_CLEARANCE",
        "PE_LICENSE",
        "DRIVERS_LICENSE",
        "GENDER",
        "RACE_ETHNICITY",
        "VETERAN_STATUS",
        "DISABILITY_STATUS",
    }
)

_SYNONYMS: tuple[tuple[str, str], ...] = (
    ("AUTHORIZED_ANY_EMPLOYER", "authorized to work for any employer"),
    ("AUTHORIZED_ANY_EMPLOYER", "any employer without restriction"),
    ("AUTHORIZED_ANY_EMPLOYER", "work for any employer"),
    ("US_WORK_AUTHORIZATION", "legally authorized to work in the united states"),
    ("US_WORK_AUTHORIZATION", "authorized to work in the united states"),
    ("US_WORK_AUTHORIZATION", "eligible to work in the united states"),
    ("US_WORK_AUTHORIZATION", "united states work authorization"),
    ("US_WORK_AUTHORIZATION", "work authorization"),
    ("US_WORK_AUTHORIZATION", "legally authorized to work"),
    ("US_WORK_AUTHORIZATION", "authorized to work"),
    ("US_WORK_AUTHORIZATION", "eligible to work"),
    ("US_WORK_AUTHORIZATION", "work auth"),
    ("SPONSORSHIP_NOW_OR_FUTURE", "now or in the future require sponsorship"),
    ("SPONSORSHIP_NOW_OR_FUTURE", "now or in the future require visa sponsorship"),
    ("SPONSORSHIP_NOW_OR_FUTURE", "now or will you in the future require sponsorship"),
    ("SPONSORSHIP_NOW_OR_FUTURE", "now or will you in the future require visa sponsorship"),
    ("SPONSORSHIP_FUTURE", "require sponsorship in the future"),
    ("SPONSORSHIP_FUTURE", "require visa sponsorship in the future"),
    ("SPONSORSHIP_FUTURE", "sponsorship in the future"),
    ("SPONSORSHIP_FUTURE", "future visa sponsorship"),
    ("SPONSORSHIP_FUTURE", "future sponsorship"),
    ("SPONSORSHIP_NOW", "now require visa sponsorship"),
    ("SPONSORSHIP_NOW", "require visa sponsorship"),
    ("SPONSORSHIP_NOW", "require sponsorship now"),
    ("SPONSORSHIP_NOW", "sponsorship now"),
    ("SPONSORSHIP_NOW", "currently require sponsorship"),
    ("SPONSORSHIP_NOW", "visa sponsorship"),
    ("SPONSORSHIP_NOW", "require sponsorship"),
    ("AGE_18_PLUS", "18 or older"),
    ("AGE_18_PLUS", "at least 18"),
    ("AGE_18_PLUS", "over 18"),
    ("AGE_18_PLUS", "age of 18"),
    ("AGE_18_PLUS", "18 years of age"),
    ("AGE_18_PLUS", "18+"),
    ("RELOCATE", "willing to relocate"),
    ("RELOCATE", "open to relocation"),
    ("RELOCATE", "relocation"),
    ("RELOCATE", "relocate"),
    ("TRAVEL_PERCENT", "percentage of time are you willing to travel"),
    ("TRAVEL_PERCENT", "percentage of the time are you willing to travel"),
    ("TRAVEL_PERCENT", "willing to travel"),
    ("TRAVEL_PERCENT", "travel percentage"),
    ("TRAVEL_PERCENT", "percent travel"),
    ("TRAVEL_PERCENT", "how much travel"),
    ("SHIFT_AVAILABILITY", "shifts are you available"),
    ("SHIFT_AVAILABILITY", "shift availability"),
    ("SHIFT_AVAILABILITY", "which shifts"),
    ("SHIFT_AVAILABILITY", "available shifts"),
    ("SECURITY_CLEARANCE", "security clearance"),
    ("SECURITY_CLEARANCE", "active clearance"),
    ("PE_LICENSE", "professional engineer license"),
    ("PE_LICENSE", "pe license"),
    ("PE_LICENSE", "licensed pe"),
    ("DRIVERS_LICENSE", "driver s license"),
    ("DRIVERS_LICENSE", "drivers license"),
    ("DRIVERS_LICENSE", "driver license"),
    ("SALARY_EXPECTATION", "salary expectation"),
    ("SALARY_EXPECTATION", "desired salary"),
    ("SALARY_EXPECTATION", "compensation expectation"),
    ("SALARY_EXPECTATION", "pay expectation"),
    ("SALARY_EXPECTATION", "expected salary"),
    ("EMPLOYMENT_TYPE", "employment type"),
    ("EMPLOYMENT_TYPE", "type of employment"),
    ("EMPLOYMENT_TYPE", "full time or part time"),
    ("START_DATE", "earliest start date"),
    ("START_DATE", "when can you start"),
    ("START_DATE", "start date"),
    ("START_DATE", "date available"),
    ("PREVIOUSLY_EMPLOYED", "previously been employed"),
    ("PREVIOUSLY_EMPLOYED", "previously employed"),
    ("PREVIOUSLY_EMPLOYED", "previously worked"),
    ("PREVIOUSLY_EMPLOYED", "worked here before"),
    ("PREVIOUSLY_EMPLOYED", "former employee"),
    ("PREVIOUSLY_EMPLOYED", "worked for this company"),
    ("GENDER", "gender"),
    ("RACE_ETHNICITY", "race ethnicity"),
    ("RACE_ETHNICITY", "ethnicity"),
    ("VETERAN_STATUS", "veteran status"),
    ("VETERAN_STATUS", "protected veteran"),
    ("DISABILITY_STATUS", "disability status"),
    ("DISABILITY_STATUS", "disability"),
)

_PROTOTYPES: dict[str, str] = {
    "US_WORK_AUTHORIZATION": "legally authorized work united states eligibility authorization",
    "AUTHORIZED_ANY_EMPLOYER": "authorized work any employer unrestricted",
    "AUTHORIZED_WITHOUT_SPONSORSHIP": "authorized work without sponsorship visa",
    "SPONSORSHIP_NOW": "visa sponsorship required now current",
    "SPONSORSHIP_NOW_OR_FUTURE": "visa sponsorship required now or future",
    "SPONSORSHIP_FUTURE": "visa sponsorship required future later",
    "AGE_18_PLUS": "age eighteen older years",
    "RELOCATE": "relocate relocation move city willing",
    "TRAVEL_PERCENT": "travel percentage percent time willing",
    "SHIFT_AVAILABILITY": "shift availability nights weekends days schedule",
    "SECURITY_CLEARANCE": "security clearance secret top secret",
    "PE_LICENSE": "professional engineer pe license",
    "DRIVERS_LICENSE": "driver license driving valid",
    "SALARY_EXPECTATION": "salary compensation pay expectation desired",
    "EMPLOYMENT_TYPE": "employment type full time part time contract",
    "START_DATE": "start date available begin earliest",
    "PREVIOUSLY_EMPLOYED": "previously employed worked before former",
    "GENDER": "gender identity demographic",
    "RACE_ETHNICITY": "race ethnicity demographic",
    "VETERAN_STATUS": "veteran military status demographic",
    "DISABILITY_STATUS": "disability status demographic",
}

_STOP = frozenset(
    {
        "a",
        "an",
        "the",
        "to",
        "of",
        "in",
        "for",
        "and",
        "or",
        "you",
        "your",
        "are",
        "do",
        "does",
        "is",
        "be",
        "this",
        "that",
        "role",
        "position",
        "job",
        "please",
        "any",
        "at",
        "on",
        "with",
        "if",
        "we",
        "our",
    }
)

_YES = frozenset({"yes", "y", "true", "authorized", "authorised"})
_NO = frozenset({"no", "n", "false", "not authorized", "not authorised", "unauthorized", "unauthorised"})
_BUCKETS = {
    "0": "0%",
    "0%": "0%",
    "25": "25%",
    "25%": "25%",
    "50": "50%",
    "50%": "50%",
    "75": "75%",
    "75%": "75%",
    "100": "100%",
    "100%": "100%",
}

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b")

Level3Hook = Callable[..., str | None]


@dataclass(frozen=True)
class IntentMatch:
    """One classification. ``intent`` is None when the question is unknown."""

    intent: str | None
    confidence: str
    source: str
    text: str = ""
    fallback_intents: tuple[str, ...] = ()
    combine: str = ""

    @property
    def known(self) -> bool:
        return self.intent is not None and self.confidence != "unknown"


_NOW_RE = re.compile(r"\b(?:now|currently)\b")
_FUTURE_RE = re.compile(r"\bfuture\b")
_WITHOUT_SPONSOR_RE = re.compile(
    r"\bwithout\b(?:\s+\w+){0,6}\s+sponsorship\b"
    r"|\bno\s+(?:visa\s+)?sponsorship\b"
    r"|\b(?:do\s+not|not)\s+(?:require|need)\b(?:\s+\w+){0,4}\s+sponsorship\b"
)
_WORK_AUTH_RE = re.compile(
    r"\b(?:authorized|authorised)\s+to\s+work\b"
    r"|\blegally\s+authorized\b"
    r"|\beligible\s+to\s+work\b"
    r"|\bwork\s+authorization\b"
)


def normalize_question(text: str) -> str:
    """Case, punctuation, and U.S. / USA abbreviations folded to one form."""
    folded = normalize(text)
    folded = re.sub(r"\bu s a\b", "united states", folded)
    folded = re.sub(r"\bu s\b", "united states", folded)
    folded = re.sub(r"\busa\b", "united states", folded)
    return re.sub(r"\s+", " ", folded).strip()


def sanitize_question(text: str, *, limit: int = 300) -> str:
    """Question text safe to send onward: no emails, no phone numbers, bounded."""
    cleaned = _EMAIL_RE.sub(" ", text)
    cleaned = _PHONE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rstrip()
    return cleaned


def _tokens(text: str) -> set[str]:
    return {token for token in normalize_question(text).split() if token not in _STOP and len(token) > 1}


def _compound_intent(folded: str) -> str | None:
    """Compound legal questions before a shorter phrase can claim them.

    "Now or in the future" is not the same fact as sponsorship now. "Authorized
    to work without sponsorship" is not work authorization alone.
    """
    if "sponsorship" not in folded:
        return None
    if _WITHOUT_SPONSOR_RE.search(folded):
        return "AUTHORIZED_WITHOUT_SPONSORSHIP"
    now = _NOW_RE.search(folded) is not None
    future = _FUTURE_RE.search(folded) is not None
    if now and future:
        return "SPONSORSHIP_NOW_OR_FUTURE"
    if future:
        return "SPONSORSHIP_FUTURE"
    if now:
        return "SPONSORSHIP_NOW"
    return None


def compound_plan(text: str, intent: str | None) -> tuple[tuple[str, ...], str]:
    """Component intents used when the resolver does not know the compound."""
    if intent == "SPONSORSHIP_NOW_OR_FUTURE":
        return (("SPONSORSHIP_NOW", "SPONSORSHIP_FUTURE"), "sponsorship_or")
    if intent != "AUTHORIZED_WITHOUT_SPONSORSHIP":
        return ((), "")
    folded = normalize_question(text)
    authorizations: list[str] = []
    if _WORK_AUTH_RE.search(folded):
        if "any employer" in folded:
            authorizations.append("AUTHORIZED_ANY_EMPLOYER")
        united_states = "united states" in folded or "legally authorized" in folded or "work authorization" in folded
        if united_states or not authorizations:
            authorizations.append("US_WORK_AUTHORIZATION")
    now = _NOW_RE.search(folded) is not None
    future = _FUTURE_RE.search(folded) is not None
    if future and not now:
        sponsorships = ["SPONSORSHIP_FUTURE"]
    elif now and not future:
        sponsorships = ["SPONSORSHIP_NOW"]
    else:
        sponsorships = ["SPONSORSHIP_NOW", "SPONSORSHIP_FUTURE"]
    combine = "authorized_without" if authorizations else "sponsorship_negated"
    return (tuple(authorizations + sponsorships), combine)


def _level1(text: str) -> str | None:
    folded = normalize_question(text)
    if not folded:
        return None
    compound = _compound_intent(folded)
    if compound is not None:
        return compound
    ordered = sorted(_SYNONYMS, key=lambda item: len(item[1]), reverse=True)
    for intent, phrase in ordered:
        if phrase in folded:
            return intent
    return None


def _planned(match: IntentMatch) -> IntentMatch:
    fallback, combine = compound_plan(match.text, match.intent)
    if not combine:
        return match
    return IntentMatch(match.intent, match.confidence, match.source, match.text, fallback, combine)


def _level2(text: str) -> IntentMatch | None:
    asked = _tokens(text)
    if len(asked) < 2:
        return None
    best_intent = ""
    best_score = 0.0
    for intent, prototype in _PROTOTYPES.items():
        known = _tokens(prototype)
        if not known:
            continue
        score = len(asked & known) / len(asked | known)
        if score > best_score:
            best_score = score
            best_intent = intent
    if not best_intent or best_score < 0.22:
        return None
    if best_score >= 0.55:
        confidence = "HIGH"
    elif best_score >= 0.34:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"
    if best_intent in STRICT_INTENTS and confidence == "HIGH" and best_score < 0.72:
        confidence = "MEDIUM"
    return IntentMatch(best_intent, confidence, "level2")


def classify_question(
    text: str,
    options: list[str] | None = None,
    *,
    hook: Level3Hook | None = None,
) -> IntentMatch:
    """Classify one question. The hook is skipped unless the caller passes it."""
    sanitized = sanitize_question(text)
    if not sanitized:
        return IntentMatch(None, "unknown", "none", "")
    level1 = _level1(sanitized)
    if level1 is not None:
        return _planned(IntentMatch(level1, "HIGH", "level1", sanitized))
    semantic = _level2(sanitized)
    if semantic is not None and semantic.confidence == "HIGH":
        return IntentMatch(semantic.intent, semantic.confidence, semantic.source, sanitized)
    if hook is not None:
        catalogue = list(PROTOCOL_INTENTS)
        option_labels = [sanitize_question(label, limit=80) for label in (options or [])[:20]]
        try:
            returned = hook(question=sanitized, options=option_labels, catalogue=catalogue)
        except TypeError:
            returned = hook(sanitized, option_labels, catalogue)
        if isinstance(returned, str) and returned in PROTOCOL_INTENTS:
            # The hook returns an intent only. Confidence stays below HIGH so
            # the value is never written from this source.
            return IntentMatch(returned, "MEDIUM", "level3", sanitized)
    if semantic is not None:
        return IntentMatch(semantic.intent, semantic.confidence, semantic.source, sanitized)
    return IntentMatch(None, "unknown", "none", sanitized)


def may_fill(*, local: IntentMatch, resolver_confidence: str) -> bool:
    """HIGH local and HIGH resolver confidence are both required.

    Demographic intents are never filled. An unknown question is not filled
    here; a saved answer for that exact question is handled by the step filler.
    """
    if local.intent in DEMOGRAPHIC_INTENTS:
        return False
    if local.intent is None or local.confidence != "HIGH":
        return False
    if resolver_confidence != "HIGH":
        return False
    if local.intent in STRICT_INTENTS and local.source == "level3":
        return False
    if local.intent in STRICT_INTENTS and local.confidence != "HIGH":
        return False
    return True


def _high_bool(confidence: str, value: str) -> str | None:
    """Yes or no only when the resolver confidence is HIGH."""
    if confidence != "HIGH" or not value:
        return None
    if options_equivalent(value, "Yes"):
        return "yes"
    if options_equivalent(value, "No"):
        return "no"
    return None


def combine_yes_if_either(parts: list[tuple[str, str]]) -> str | None:
    """Yes when any part is HIGH Yes. No only when every part is HIGH No."""
    flags = [_high_bool(confidence, value) for confidence, value in parts]
    if any(flag == "yes" for flag in flags):
        return "Yes"
    if parts and all(flag == "no" for flag in flags):
        return "No"
    return None


def combine_authorized_without(
    authorizations: list[tuple[str, str]],
    sponsorships: list[tuple[str, str]],
) -> str | None:
    """Yes only when every authorization is HIGH Yes and every sponsorship is HIGH No.

    A HIGH No authorization, or a HIGH Yes sponsorship, answers the compound No.
    Anything incomplete stays unanswered.
    """
    auth_flags = [_high_bool(confidence, value) for confidence, value in authorizations]
    sponsor_flags = [_high_bool(confidence, value) for confidence, value in sponsorships]
    if any(flag == "no" for flag in auth_flags) or any(flag == "yes" for flag in sponsor_flags):
        return "No"
    if (
        authorizations
        and sponsorships
        and all(flag == "yes" for flag in auth_flags)
        and all(flag == "no" for flag in sponsor_flags)
    ):
        return "Yes"
    return None


def _compact(value: str) -> str:
    token = normalize_question(value).replace(" ", "")
    if token.endswith("percent"):
        token = token[: -len("percent")] + "%"
    return token


def percent_bucket(value: str) -> str | None:
    """Exact 0/25/50/75/100 buckets. Nearby numbers are not rounded."""
    return _BUCKETS.get(_compact(value))


def options_equivalent(desired: str, option: str) -> bool:
    """True only for an exact label or a listed equivalent. No approximation."""
    left = normalize_question(desired)
    right = normalize_question(option)
    if not left or not right:
        return False
    if left == right:
        return True
    left_bucket = percent_bucket(desired)
    right_bucket = percent_bucket(option)
    if left_bucket is not None and left_bucket == right_bucket:
        return True
    if left in _NO or right in _NO:
        return left in _NO and right in _NO
    if left in _YES and right in _YES:
        return True
    return False


def match_option_exact(desired: str, options: list[Option]) -> Option | None:
    """Pick the option whose label or value is exact or an allowed equivalent."""
    if not desired or not options:
        return None
    for option in options:
        if options_equivalent(desired, option.label) or options_equivalent(desired, option.value):
            return option
    return None


def match_all_options(values: list[str], options: list[Option]) -> list[Option] | None:
    """Map every saved value, or return None when any value has no exact option."""
    if not values:
        return None
    chosen: list[Option] = []
    for value in values:
        match = match_option_exact(value, options)
        if match is None:
            return None
        chosen.append(match)
    return chosen
