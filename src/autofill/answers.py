"""Match a form question to an approved applicationAnswers entry.

Order:
1. The same question text (after normalizing case and punctuation).
2. The same intent. Each side is classified with the engine's own intent
   classifier, and only a HIGH classification counts. A saved answer can also
   carry the ``intent`` TileArc stored with it. Sponsorship now, in the
   future, and now-or-future are separate intents, and so is "authorized
   without sponsorship". Work-authorization intents also need the same country.
   If two saved answers share the intent but disagree, nothing is reused.
3. Only when neither side has a known intent: the "how did you hear" synonyms,
   or one question containing almost all of the other's words.

Anything else is left unanswered, so the employee is asked instead of guessed for.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from contextvars import ContextVar, Token

from autofill.models import Control
from autofill.profile import ApplicationAnswer, CandidateProfile

# Same set as intents.JURISDICTION_INTENTS (intents imports mapping, which imports this module).
_JURISDICTION_INTENTS = frozenset(
    {
        "US_WORK_AUTHORIZATION",
        "AUTHORIZED_ANY_EMPLOYER",
        "AUTHORIZED_WITHOUT_SPONSORSHIP",
        "SPONSORSHIP_NOW",
        "SPONSORSHIP_NOW_OR_FUTURE",
        "SPONSORSHIP_FUTURE",
    }
)


def jurisdiction(text: str) -> frozenset[str]:
    """Countries named in a question. None named means the United States."""
    from autofill.intents import jurisdiction as _jurisdiction

    return _jurisdiction(text)


_HOW_HEARD = ("how did you hear", "how did you find", "where did you hear", "referral source")

# Words that carry no meaning for the containment check.
_FILLER = frozenset(
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
        "have",
        "has",
        "please",
        "any",
        "at",
        "on",
        "with",
        "if",
        "we",
        "our",
        "will",
        "can",
        "what",
        "which",
    }
)

# Share of the longer question's words that the shorter one must cover.
_CONTAIN_COVERAGE = 0.8


# Facts that describe this employer, not the candidate. REFERRAL stays unfilled.
EMPLOYER_SCOPED_INTENTS = frozenset({"PREVIOUSLY_EMPLOYED", "REFERRAL"})

_EMPLOYER_PHRASE = re.compile(r"\b(?:this|our) company\b")
_ATTESTATION_STEM = re.compile(
    r"\b(?:background check|e-?sign|electronic signature|arbitration|certify|attest|attestation"
    r"|text messages?|sms|recording)\b"
)
_SKIP_KINDS = frozenset({"CONSENT", "CONSENT_CHOICE", "DEMOGRAPHIC", "MISSING_PROFILE_VALUE"})
_PROFILE_FIELD_KEYS = frozenset(
    {
        "first_name",
        "last_name",
        "full_name",
        "legal_name",
        "preferred_name",
        "email",
        "phone",
        "phone_country",
        "address",
        "city",
        "state",
        "postal_code",
        "country",
        "linkedin_url",
        "github_url",
        "portfolio_url",
        "website_url",
        "resume",
        "cover_letter",
        "willing_background_check",
    }
)
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0"})

# Set for the duration of one fill so a verified write can be reused on the next page.
_CATALOGUE: ContextVar[tuple[CandidateProfile, tuple[str, ...]] | None] = ContextVar(
    "autofill_answer_catalogue",
    default=None,
)


def bind_catalogue(profile: CandidateProfile, employers: tuple[str, ...] = ()) -> Token:
    """Remember the profile that receives verified answers for this fill."""
    return _CATALOGUE.set((profile, employers))


def reset_catalogue(token: Token) -> None:
    _CATALOGUE.reset(token)


def current_employers() -> tuple[str, ...]:
    """Employer keys bound for this fill. Empty outside a fill."""
    bound = _CATALOGUE.get()
    if bound is None:
        return ()
    return bound[1]


def current_catalogue() -> tuple[CandidateProfile, tuple[str, ...]] | None:
    """The profile and employer keys for this fill, or None outside one."""
    return _CATALOGUE.get()


def employer_keys(*, company: str = "", url: str = "") -> tuple[str, ...]:
    """Company name and tenant scope that identify this employer.

    A loopback host is not an employer. Fixture pages keep untagged answers.
    A bare public host is an employer. A shared host uses its tenant scope.
    """
    keys: list[str] = []
    name = company.strip()
    if name:
        keys.append(name)
    if url:
        from autofill.credentials import credential_scope

        scope = credential_scope(url).strip()
        host = scope.split("/")[0]
        if scope and host not in _LOCAL_HOSTS and ("/" in scope or "." in host):
            keys.append(scope)
    unique: list[str] = []
    for key in keys:
        if key not in unique:
            unique.append(key)
    return tuple(unique)


def question_intent(texts: Sequence[str], *, us_on_page: bool = False) -> tuple[str | None, str]:
    """HIGH intent for the first text that has one, and that text."""
    from autofill.intents import classify_question

    for text in texts:
        if not text or not text.strip():
            continue
        match = classify_question(text, us_on_page=us_on_page)
        if match.intent is not None and match.confidence == "HIGH":
            return match.intent, text
    return None, ""


def _saved_intent(item: ApplicationAnswer, *, us_on_page: bool = False) -> str | None:
    """The saved answer's intent. A carried intent must not contradict its own text."""
    from autofill.intents import PROTOCOL_INTENTS

    classified, _ = question_intent([item.question], us_on_page=us_on_page)
    carried = (getattr(item, "intent", "") or "").strip().upper()
    if carried not in PROTOCOL_INTENTS:
        carried = ""
    if classified and carried and classified != carried:
        return None
    return classified or carried or None


def _how_heard(text: str) -> bool:
    return any(phrase in text for phrase in _HOW_HEARD)


def _words(text: str) -> set[str]:
    return {word for word in text.split() if word not in _FILLER and len(word) > 1}


def _contains(asked: str, known: str) -> bool:
    if not asked or not known or len(min(asked, known, key=len)) < 12:
        return False
    if not (known in asked or asked in known):
        return False
    short, long = (asked, known) if len(asked) <= len(known) else (known, asked)
    short_words = _words(short)
    long_words = _words(long)
    if len(short_words) < 2 or not long_words:
        return False
    return len(short_words & long_words) / len(long_words) >= _CONTAIN_COVERAGE


class _AnswerConflict(Exception):
    """Two saved rows name one question and do not say the same thing."""


def _one_value(items: list[ApplicationAnswer], normalize: Callable[[str], str]) -> str | None:
    """The answer when every candidate says the same thing. Disagreement is not a guess."""
    if not items:
        return None
    values = {normalize(item.answer) for item in items}
    if len(values) != 1:
        raise _AnswerConflict
    return items[0].answer


def _mentions_employer(text: str, employers: Sequence[str]) -> bool:
    """True when the wording is about this employer and is not a source question."""
    if not text or _how_heard(text):
        return False
    if _EMPLOYER_PHRASE.search(text):
        return True
    for name in employers:
        token = name.casefold().strip()
        if len(token) < 3 or token in _LOCAL_HOSTS:
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", text):
            return True
    return False


def _row_is_scoped(
    item: ApplicationAnswer,
    asked_forms: Sequence[str],
    employers: Sequence[str],
    *,
    us_on_page: bool,
) -> bool:
    """Previously-employed and 'this company' rows do not travel to another employer.

    A how-did-you-hear source stays portable, including when the wording says
    "our company".
    """
    saved = item.question.casefold()
    if any(_how_heard(form) for form in asked_forms) and _how_heard(saved):
        return False
    intent = _saved_intent(item, us_on_page=us_on_page)
    if intent in EMPLOYER_SCOPED_INTENTS:
        return True
    if _mentions_employer(saved, employers):
        return True
    return any(_mentions_employer(form, employers) for form in asked_forms)


def _employer_ok(
    item: ApplicationAnswer,
    *,
    scoped: bool,
    employers: Sequence[str],
) -> bool:
    """A scoped row matches only its own employer. A missing key is not another employer."""
    if not scoped:
        return True
    saved = (item.employer or "").casefold().strip()
    keys = {name.casefold().strip() for name in employers if name and name.strip()}
    if not keys:
        return not saved
    if not saved:
        return False
    return saved in keys


def catalogue_match(
    question: str | Sequence[str],
    answers: list[ApplicationAnswer],
    *,
    normalize: Callable[[str], str],
    us_on_page: bool = False,
    employers: Sequence[str] = (),
) -> tuple[str | None, str]:
    """The saved answer, and ``ok``, ``conflict``, or ``none``."""
    try:
        value = _match_answer_impl(
            question,
            answers,
            normalize=normalize,
            us_on_page=us_on_page,
            employers=employers,
        )
    except _AnswerConflict:
        return None, "conflict"
    if value is None:
        return None, "none"
    return value, "ok"


def match_answer(
    question: str | Sequence[str],
    answers: list[ApplicationAnswer],
    *,
    normalize: Callable[[str], str],
    us_on_page: bool = False,
    employers: Sequence[str] = (),
) -> str | None:
    """Return the approved answer for ``question``, or None.

    Two disagreeing saved values return None. ``catalogue_match`` reports that
    disagreement separately.
    """
    try:
        return _match_answer_impl(
            question,
            answers,
            normalize=normalize,
            us_on_page=us_on_page,
            employers=employers,
        )
    except _AnswerConflict:
        return None


def _match_answer_impl(
    question: str | Sequence[str],
    answers: list[ApplicationAnswer],
    *,
    normalize: Callable[[str], str],
    us_on_page: bool = False,
    employers: Sequence[str] = (),
) -> str | None:
    """Return the approved answer for ``question``, or None.

    ``question`` is one text or the control's texts in priority order
    (label, prompt, accessible name, ...). The first text is the main one.
    ``us_on_page`` lets a country-less work-authorization question use the
    U.S. fact. ``employers`` is this session's company and tenant scope.
    """
    texts = [question] if isinstance(question, str) else [text for text in question if text]
    asked_forms = [normalize(text) for text in texts]
    asked_forms = [form for form in asked_forms if form]
    if not asked_forms:
        return None
    usable = [item for item in answers if item.answer and (normalize(item.question) or item.intent)]
    if not usable:
        return None

    from autofill.intents import UNFILLED_INTENTS

    asked_intent, intent_text = question_intent(texts, us_on_page=us_on_page)
    if asked_intent in UNFILLED_INTENTS:
        # Referral names and demographic answers are never written, even when
        # the question text is an exact copy of a saved row.
        return None

    def allowed(item: ApplicationAnswer) -> bool:
        if _saved_intent(item, us_on_page=us_on_page) in UNFILLED_INTENTS:
            return False
        scoped = _row_is_scoped(item, asked_forms, employers, us_on_page=us_on_page)
        if asked_intent in EMPLOYER_SCOPED_INTENTS:
            scoped = True
        return _employer_ok(item, scoped=scoped, employers=employers)

    exact = [item for item in usable if normalize(item.question) in asked_forms and allowed(item)]
    if exact:
        return _one_value(exact, normalize)

    if asked_intent is not None:
        if asked_intent in UNFILLED_INTENTS:
            return None
        places = jurisdiction(intent_text) if asked_intent in _JURISDICTION_INTENTS else frozenset()
        same: list[ApplicationAnswer] = []
        for item in usable:
            if not allowed(item):
                continue
            if _saved_intent(item, us_on_page=us_on_page) != asked_intent:
                continue
            if places and jurisdiction(item.question) != places:
                continue
            same.append(item)
        return _one_value(same, normalize)

    if any(_how_heard(form) for form in asked_forms):
        heard = [item for item in usable if _how_heard(normalize(item.question)) and allowed(item)]
        if heard:
            return _one_value(heard, normalize)

    contained = [
        item
        for item in usable
        if _saved_intent(item, us_on_page=us_on_page) is None
        and allowed(item)
        and any(_contains(form, normalize(item.question)) for form in asked_forms)
    ]
    return _one_value(contained, normalize)


def note_session_answer(
    profile: CandidateProfile,
    *,
    stem: str,
    value: str,
    field_key: str = "",
    intent: str = "",
    kind: str = "",
    employers: Sequence[str] = (),
    control: Control | None = None,
) -> None:
    """Remember one verified answer on this profile for a later page of this fill.

    Consent, demographic, attestation, and profile fields (name, email, phone,
    address) are not stored. A host-supplied row is left in place. A second
    verified value for the same intent is stored beside it so the two disagree
    and nothing is guessed. A row this session already wrote is replaced.
    """
    from autofill.intents import PROTOCOL_INTENTS, UNFILLED_INTENTS, classify_question
    from autofill.mapping import acknowledgement_kind, is_consent_choice, us_context

    text = str(value or "").strip()
    question = str(stem or "").strip()
    if not text or not question or "\n" in text:
        return
    if kind in _SKIP_KINDS:
        return
    if field_key in _PROFILE_FIELD_KEYS:
        return
    if _ATTESTATION_STEM.search(question.casefold()):
        return
    if control is not None and (
        is_consent_choice(control) or acknowledgement_kind(control) in {"consent", "marketing"}
    ):
        return
    on_page = us_context(profile)
    match = classify_question(question, us_on_page=on_page)
    carried = intent.strip().upper()
    if carried not in PROTOCOL_INTENTS:
        carried = ""
    if field_key.strip().upper() in PROTOCOL_INTENTS and not carried:
        carried = field_key.strip().upper()
    resolved = match.intent if match.intent and match.confidence == "HIGH" else ""
    if resolved and carried and resolved != carried:
        return
    resolved = resolved or carried
    if resolved in UNFILLED_INTENTS:
        return
    if not resolved and match.intent:
        # MEDIUM or LOW is not a fact we will reuse.
        return
    employer = ""
    scoped = resolved in EMPLOYER_SCOPED_INTENTS or _mentions_employer(question.casefold(), employers)
    if scoped:
        employer = next((item.strip() for item in employers if item.strip()), "")
    fresh = ApplicationAnswer(
        question=question,
        answer=text,
        intent=resolved,
        employer=employer,
        from_session=True,
    )
    kept: list[ApplicationAnswer] = []
    replaced = False
    for existing in profile.application_answers:
        same_intent = bool(resolved) and _saved_intent(existing, us_on_page=on_page) == resolved
        same_text = not resolved and existing.question.casefold().strip() == question.casefold()
        if not same_intent and not same_text:
            kept.append(existing)
            continue
        if existing.from_session:
            if not replaced:
                kept.append(fresh)
                replaced = True
            continue
        kept.append(existing)
        if existing.answer.strip() != text and not replaced:
            kept.append(fresh)
            replaced = True
    if not replaced:
        # A host row with the same value was kept. A new fact is appended.
        already = False
        for item in kept:
            same_value = item.answer.strip() == text
            same_intent = bool(resolved) and _saved_intent(item, us_on_page=on_page) == resolved
            same_text = not resolved and item.question.casefold().strip() == question.casefold()
            if same_value and (same_intent or same_text):
                already = True
                break
        if not already:
            kept.append(fresh)
    profile.application_answers = kept
