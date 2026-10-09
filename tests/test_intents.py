"""Question intent, confidence, and exact option mapping. No network and no browser."""

from __future__ import annotations

import pytest

from autofill.intents import (
    PROTOCOL_INTENTS,
    IntentMatch,
    classify_question,
    match_all_options,
    may_fill,
    options_equivalent,
    sanitize_question,
)
from autofill.models import Option

_MATRIX = [
    ("Are you legally authorized to work in the United States?", "US_WORK_AUTHORIZATION", "exact"),
    ("U.S. work authorization", "US_WORK_AUTHORIZATION", "synonym"),
    ("Work auth?", "US_WORK_AUTHORIZATION", "short"),
    (
        "I certify that I am legally authorized to work in the United States.",
        "US_WORK_AUTHORIZATION",
        "long-legal",
    ),
    ("Are you authorized to work for any employer?", "AUTHORIZED_ANY_EMPLOYER", "exact"),
    ("Do you now require visa sponsorship?", "SPONSORSHIP_NOW", "exact"),
    ("Will you now or in the future require sponsorship?", "SPONSORSHIP_NOW", "synonym"),
    ("Will you require sponsorship in the future?", "SPONSORSHIP_FUTURE", "exact"),
    ("Are you 18 or older?", "AGE_18_PLUS", "exact"),
    ("18+", "AGE_18_PLUS", "short"),
    ("Are you willing to relocate?", "RELOCATE", "exact"),
    ("Relocation?", "RELOCATE", "short"),
    ("What percentage of the time are you willing to travel?", "TRAVEL_PERCENT", "exact"),
    ("Which shifts are you available to work?", "SHIFT_AVAILABILITY", "exact"),
    ("Do you hold an active security clearance?", "SECURITY_CLEARANCE", "exact"),
    ("Do you hold a PE license?", "PE_LICENSE", "exact"),
    ("Do you have a valid driver's license?", "DRIVERS_LICENSE", "exact"),
    ("What is your salary expectation?", "SALARY_EXPECTATION", "exact"),
    ("Employment type", "EMPLOYMENT_TYPE", "short"),
    ("When is your earliest start date?", "START_DATE", "exact"),
    ("Have you previously been employed here?", "PREVIOUSLY_EMPLOYED", "exact"),
]


@pytest.mark.parametrize(("text", "intent", "phrasing"), _MATRIX)
def test_level1_matrix(text: str, intent: str, phrasing: str):
    match = classify_question(text)
    assert match.intent == intent
    assert match.confidence == "HIGH"
    assert match.source == "level1"
    assert phrasing


@pytest.mark.parametrize(
    ("kind", "options"),
    [
        ("radio", ["Yes", "No"]),
        ("select", ["Yes", "No"]),
        ("multiselect", ["Day", "Night", "Weekend"]),
    ],
)
def test_control_kind_does_not_change_the_intent(kind: str, options: list[str]):
    text = "Which shifts are you available to work?" if kind == "multiselect" else "Are you 18 or older?"
    expected = "SHIFT_AVAILABILITY" if kind == "multiselect" else "AGE_18_PLUS"
    match = classify_question(text, options)
    assert match.intent == expected
    assert match.confidence == "HIGH"


@pytest.mark.parametrize("intent", ["GENDER", "RACE_ETHNICITY", "VETERAN_STATUS", "DISABILITY_STATUS"])
def test_demographic_intents_are_never_filled(intent: str):
    match = IntentMatch(intent, "HIGH", "level1", intent)
    assert may_fill(local=match, resolver_confidence="HIGH") is False


def test_unknown_question_stays_unknown():
    match = classify_question("What is your favorite prime number?")
    assert match.intent is None
    assert match.confidence == "unknown"
    assert may_fill(local=match, resolver_confidence="HIGH") is False


def test_below_high_is_not_filled_especially_for_authorization():
    medium = IntentMatch("US_WORK_AUTHORIZATION", "MEDIUM", "level2", "work papers")
    low = IntentMatch("RELOCATE", "LOW", "level2", "move")
    assert may_fill(local=medium, resolver_confidence="HIGH") is False
    assert may_fill(local=low, resolver_confidence="HIGH") is False
    high_local = IntentMatch("US_WORK_AUTHORIZATION", "HIGH", "level1", "work authorization")
    assert may_fill(local=high_local, resolver_confidence="MEDIUM") is False
    assert may_fill(local=high_local, resolver_confidence="HIGH") is True


def test_level2_can_score_without_a_network_or_level3():
    match = classify_question("Would you move to another city if the role required relocation later")
    assert match.source in {"level1", "level2", "none"}
    if match.confidence != "HIGH":
        assert may_fill(local=match, resolver_confidence="HIGH") is False


def test_level3_hook_is_off_by_default_and_returns_intent_only():
    seen: dict = {}

    def hook(*, question: str, options: list[str], catalogue: list[str]) -> str:
        seen["question"] = question
        seen["options"] = options
        seen["catalogue"] = catalogue
        return "RELOCATE"

    untouched = classify_question("What color is the office mug?")
    assert untouched.source != "level3"
    match = classify_question("What color is the office mug?", ["Blue", "Green"], hook=hook)
    assert match.intent == "RELOCATE"
    assert match.confidence == "MEDIUM"
    assert match.source == "level3"
    assert may_fill(local=match, resolver_confidence="HIGH") is False
    assert seen["options"] == ["Blue", "Green"]
    assert seen["catalogue"] == list(PROTOCOL_INTENTS)
    assert "answer" not in seen
    assert "river.example@example.com" not in seen["question"]


def test_level3_does_not_override_a_level1_hit():
    def hook(**_kwargs: object) -> str:
        raise AssertionError("hook should not run")

    match = classify_question("Are you willing to relocate?", hook=hook)
    assert match.source == "level1"
    assert match.intent == "RELOCATE"


def test_sanitize_drops_email_and_phone_and_bounds_length():
    text = sanitize_question("Call 555-010-0199 or river.example@example.com " + ("word " * 80))
    assert "river.example@example.com" not in text
    assert "555-010-0199" not in text
    assert len(text) <= 300


def test_option_mapping_is_exact_or_equivalent():
    assert options_equivalent("Yes", "True")
    assert options_equivalent("No", "False")
    assert options_equivalent("Authorized", "Yes")
    assert options_equivalent("Not Authorized", "No")
    assert options_equivalent("50%", "50%")
    assert options_equivalent("50", "50%")
    assert options_equivalent("100%", "100")
    assert not options_equivalent("49%", "50%")
    assert not options_equivalent("51%", "50%")
    assert not options_equivalent("about 50%", "50%")
    assert not options_equivalent("Yes, I think so", "Yes")
    options = [
        Option("0%", "0%"),
        Option("25%", "25%"),
        Option("50%", "50%"),
        Option("75%", "75%"),
        Option("100%", "100%"),
    ]
    shift_options = [
        Option("Day", "Day"),
        Option("Night", "Night"),
        Option("Weekend", "Weekend"),
    ]
    chosen = match_all_options(["Day", "Night"], shift_options)
    assert chosen is not None
    assert [item.label for item in chosen] == ["Day", "Night"]
    assert match_all_options(["Day", "Graveyard"], [Option("Day", "Day"), Option("Night", "Night")]) is None
    assert match_all_options(["49%"], options) is None
    assert match_all_options(["50%"], options) is not None
