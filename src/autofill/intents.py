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
    "SPONSORSHIP_NOW",
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

# Legal, work-authorization, and demographic intents are never filled below HIGH.
STRICT_INTENTS = frozenset(
    {
        "US_WORK_AUTHORIZATION",
        "AUTHORIZED_ANY_EMPLOYER",
        "SPONSORSHIP_NOW",
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
    ("SPONSORSHIP_NOW", "now or in the future require sponsorship"),
    ("SPONSORSHIP_NOW", "now or in the future require visa sponsorship"),
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
    "SPONSORSHIP_NOW": "visa sponsorship required now current",
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

    @property
    def known(self) -> bool:
        return self.intent is not None and self.confidence != "unknown"


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


def _level1(text: str) -> str | None:
    folded = normalize_question(text)
    if not folded:
        return None
    ordered = sorted(_SYNONYMS, key=lambda item: len(item[1]), reverse=True)
    for intent, phrase in ordered:
        if phrase in folded:
            return intent
    return None


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
        return IntentMatch(level1, "HIGH", "level1", sanitized)
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
    """HIGH local and HIGH resolver confidence are both required."""
    if local.intent is None or local.confidence != "HIGH":
        return False
    if resolver_confidence != "HIGH":
        return False
    if local.intent in STRICT_INTENTS and local.source == "level3":
        return False
    if local.intent in STRICT_INTENTS and local.confidence != "HIGH":
        return False
    return True


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
