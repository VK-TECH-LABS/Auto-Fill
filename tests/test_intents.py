"""Question intent, confidence, and exact option mapping. No network and no browser."""

from __future__ import annotations

import pytest

from autofill.intents import (
    PROTOCOL_INTENTS,
    IntentMatch,
    classify_question,
    combine_authorized_without,
    combine_yes_if_either,
    dedupe_label,
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
    ("Do you currently require sponsorship?", "SPONSORSHIP_NOW", "currently"),
    ("Will you now or in the future require sponsorship?", "SPONSORSHIP_NOW_OR_FUTURE", "compound"),
    (
        "Will you now or will you in the future require visa sponsorship?",
        "SPONSORSHIP_NOW_OR_FUTURE",
        "now-or-will-you",
    ),
    ("Will you require sponsorship in the future?", "SPONSORSHIP_FUTURE", "exact"),
    ("Will you require future visa sponsorship?", "SPONSORSHIP_FUTURE", "future-visa"),
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
    (
        "Will you now or in the future require sponsorship for a visa to remain in your country?",
        "SPONSORSHIP_NOW_OR_FUTURE",
        "greenhouse-country",
    ),
    (
        "Will you now or in the future require sponsorship for employment visa status?",
        "SPONSORSHIP_NOW_OR_FUTURE",
        "lever-visa-status",
    ),
    (
        "Will you now or in the future require visa sponsorship?",
        "SPONSORSHIP_NOW_OR_FUTURE",
        "visa-sponsorship",
    ),
    (
        "Do you now or will you in the future require sponsorship to work in the United States?",
        "SPONSORSHIP_NOW_OR_FUTURE",
        "ashby-united-states",
    ),
    (
        "Will you require sponsorship for a visa to remain in the United States?",
        "SPONSORSHIP_NOW",
        "remain-us",
    ),
    ("Will you require a visa to remain in the US?", "SPONSORSHIP_NOW", "visa-remain-us"),
    ("Do you need sponsorship for a visa?", "SPONSORSHIP_NOW", "sponsorship-for-a-visa"),
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


def test_compound_sponsorship_does_not_collapse_to_now_or_work_auth():
    compound = classify_question("Will you now or in the future require sponsorship for employment visa status?")
    assert compound.intent == "SPONSORSHIP_NOW_OR_FUTURE"
    assert compound.confidence == "HIGH"
    assert compound.fallback_intents == ("SPONSORSHIP_NOW", "SPONSORSHIP_FUTURE")
    assert compound.combine == "sponsorship_or"
    future = classify_question("Will you require sponsorship in the future?")
    assert future.intent == "SPONSORSHIP_FUTURE"
    assert future.combine == ""
    current = classify_question("Do you currently require visa sponsorship?")
    assert current.intent == "SPONSORSHIP_NOW"
    plain = classify_question("Will you require sponsorship?")
    assert plain.intent == "SPONSORSHIP_NOW"
    authorized = classify_question(
        "Are you legally authorized to work in the United States without the need for sponsorship now or in the future?"
    )
    assert authorized.intent == "AUTHORIZED_WITHOUT_SPONSORSHIP"
    assert authorized.combine == "authorized_without"
    assert "US_WORK_AUTHORIZATION" in authorized.fallback_intents
    assert "SPONSORSHIP_NOW" in authorized.fallback_intents
    assert "SPONSORSHIP_FUTURE" in authorized.fallback_intents
    any_employer = classify_question("Are you authorized to work for any employer?")
    assert any_employer.intent == "AUTHORIZED_ANY_EMPLOYER"
    without = classify_question("Are you authorized to work for any employer without sponsorship?")
    assert without.intent == "AUTHORIZED_WITHOUT_SPONSORSHIP"
    assert "AUTHORIZED_ANY_EMPLOYER" in without.fallback_intents
    assert "US_WORK_AUTHORIZATION" not in without.fallback_intents


def test_sponsorship_and_authorization_are_not_filled_below_high():
    for intent in (
        "SPONSORSHIP_NOW",
        "SPONSORSHIP_FUTURE",
        "SPONSORSHIP_NOW_OR_FUTURE",
        "AUTHORIZED_WITHOUT_SPONSORSHIP",
        "US_WORK_AUTHORIZATION",
    ):
        match = IntentMatch(intent, "HIGH", "level1", intent)
        assert may_fill(local=match, resolver_confidence="HIGH") is True
        assert may_fill(local=match, resolver_confidence="MEDIUM") is False
        medium = IntentMatch(intent, "MEDIUM", "level2", intent)
        assert may_fill(local=medium, resolver_confidence="HIGH") is False


def test_sponsorship_or_combines_only_high_answers():
    assert combine_yes_if_either([("HIGH", "No"), ("HIGH", "Yes")]) == "Yes"
    assert combine_yes_if_either([("HIGH", "Yes"), ("MEDIUM", "No")]) == "Yes"
    assert combine_yes_if_either([("HIGH", "No"), ("HIGH", "No")]) == "No"
    assert combine_yes_if_either([("HIGH", "No"), ("MEDIUM", "Yes")]) is None
    assert combine_yes_if_either([("HIGH", "No"), ("", "")]) is None
    assert combine_yes_if_either([("LOW", "Yes"), ("HIGH", "No")]) is None


def test_authorized_without_sponsorship_requires_every_part():
    auths = [("HIGH", "Yes")]
    assert combine_authorized_without(auths, [("HIGH", "No"), ("HIGH", "No")]) == "Yes"
    assert combine_authorized_without(auths, [("HIGH", "No"), ("HIGH", "Yes")]) == "No"
    assert combine_authorized_without([("HIGH", "No")], [("HIGH", "No"), ("HIGH", "No")]) == "No"
    assert combine_authorized_without(auths, [("HIGH", "No"), ("MEDIUM", "No")]) is None
    assert combine_authorized_without([("MEDIUM", "Yes")], [("HIGH", "No")]) is None


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


def test_repeated_labels_keep_one_copy_and_drop_a_trailing_star():
    assert dedupe_label("Gender Gender") == "Gender"
    assert dedupe_label("Question?* Question?") == "Question?"
    assert dedupe_label("Gender*") == "Gender"
    assert dedupe_label("What is your favorite prime number?") == "What is your favorite prime number?"
    citizen = classify_question("Are you a Singapore citizen?", ["Yes", "No"])
    assert citizen.intent is None
    without = classify_question("Are you authorized to work without sponsorship?", ["Yes", "No"])
    assert without.intent == "AUTHORIZED_WITHOUT_SPONSORSHIP"
    assert without.confidence == "HIGH"
