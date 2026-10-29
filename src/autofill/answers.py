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

from collections.abc import Callable, Sequence

from autofill.profile import ApplicationAnswer

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


def question_intent(texts: Sequence[str]) -> tuple[str | None, str]:
    """HIGH intent for the first text that has one, and that text."""
    from autofill.intents import classify_question

    for text in texts:
        if not text or not text.strip():
            continue
        match = classify_question(text)
        if match.intent is not None and match.confidence == "HIGH":
            return match.intent, text
    return None, ""


def _saved_intent(item: ApplicationAnswer) -> str | None:
    """The saved answer's intent. A carried intent must not contradict its own text."""
    from autofill.intents import PROTOCOL_INTENTS

    classified, _ = question_intent([item.question])
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


def _one_value(items: list[ApplicationAnswer], normalize: Callable[[str], str]) -> str | None:
    """The answer when every candidate says the same thing. Disagreement is not a guess."""
    if not items:
        return None
    values = {normalize(item.answer) for item in items}
    if len(values) != 1:
        return None
    return items[0].answer


def match_answer(
    question: str | Sequence[str],
    answers: list[ApplicationAnswer],
    *,
    normalize: Callable[[str], str],
) -> str | None:
    """Return the approved answer for ``question``, or None.

    ``question`` is one text or the control's texts in priority order
    (label, prompt, accessible name, ...). The first text is the main one.
    """
    texts = [question] if isinstance(question, str) else [text for text in question if text]
    asked_forms = [normalize(text) for text in texts]
    asked_forms = [form for form in asked_forms if form]
    if not asked_forms:
        return None
    usable = [item for item in answers if item.answer and (normalize(item.question) or item.intent)]
    if not usable:
        return None

    exact = [item for item in usable if normalize(item.question) in asked_forms]
    if exact:
        return _one_value(exact, normalize)

    from autofill.intents import UNFILLED_INTENTS

    asked_intent, intent_text = question_intent(texts)
    if asked_intent is not None:
        if asked_intent in UNFILLED_INTENTS:
            return None
        places = jurisdiction(intent_text) if asked_intent in _JURISDICTION_INTENTS else frozenset()
        same: list[ApplicationAnswer] = []
        for item in usable:
            if _saved_intent(item) != asked_intent:
                continue
            if places and jurisdiction(item.question) != places:
                continue
            same.append(item)
        return _one_value(same, normalize)

    if any(_how_heard(form) for form in asked_forms):
        heard = [item for item in usable if _how_heard(normalize(item.question))]
        if heard:
            return _one_value(heard, normalize)

    contained = [
        item
        for item in usable
        if _saved_intent(item) is None
        and any(_contains(form, normalize(item.question)) for form in asked_forms)
    ]
    return _one_value(contained, normalize)
