"""Match a form question to an explicit applicationAnswers entry.

Synonyms cover the same intent (work authorization, sponsorship, referral
source). A question that is not on the list is left unanswered.
"""

from __future__ import annotations

from collections.abc import Callable

from autofill.profile import ApplicationAnswer

_GROUPS: tuple[frozenset[str], ...] = (
    frozenset(
        {
            "legally authorized to work",
            "authorized to work",
            "work authorization",
            "eligible to work",
        }
    ),
    frozenset({"require sponsorship", "sponsorship", "visa sponsorship"}),
    frozenset({"how did you hear", "how did you find", "where did you hear", "referral source"}),
)


def _shares_intent(left: str, right: str) -> bool:
    for group in _GROUPS:
        left_hit = any(phrase in left for phrase in group)
        right_hit = any(phrase in right for phrase in group)
        if left_hit and right_hit:
            return True
    return False


def match_answer(
    question: str,
    answers: list[ApplicationAnswer],
    *,
    normalize: Callable[[str], str],
) -> str | None:
    """Return the explicit answer for ``question``, or None."""
    asked = normalize(question)
    if not asked:
        return None
    for item in answers:
        if not item.answer:
            continue
        known = normalize(item.question)
        if not known:
            continue
        if asked == known:
            return item.answer
        if len(known) >= 12 and (known in asked or asked in known):
            return item.answer
        if _shares_intent(asked, known):
            return item.answer
    return None
