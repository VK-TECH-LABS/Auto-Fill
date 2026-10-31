"""Slices 1, 6, 3, and 4: one catalogue, no silent U.S., employer scope, next page."""

from __future__ import annotations

import json
from pathlib import Path

from autofill.answers import employer_keys, match_answer, note_session_answer
from autofill.intents import IntentMatch, classify_question
from autofill.mapping import normalize, question_stem
from autofill.models import Control, JobContext, Option
from autofill.profile import ApplicationAnswer, CandidateProfile
from autofill.resolver import ResolverAnswer, ResolverResponse
from autofill.service.context import apply_approved_answers
from autofill.service.schemas import ApprovedAnswerIn
from autofill.stepfill import _index_answers, _take_answer

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "examples" / "profile.example.json"
YES_NO = [Option("Yes", "Yes"), Option("No", "No")]


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(json.loads(PROFILE.read_text()))


def _radio(label: str) -> Control:
    return Control(kind="radio", label=label, options=YES_NO, selector="#q")


def _match(question: str, answers: list[ApplicationAnswer], **kwargs) -> str | None:
    return match_answer(question, answers, normalize=normalize, **kwargs)


def test_approved_answer_keeps_its_intent():
    person = _profile()
    apply_approved_answers(
        person,
        [ApprovedAnswerIn(question="Ever been on our payroll?", answer="No", intent="PREVIOUSLY_EMPLOYED")],
    )
    assert person.application_answers[0].intent == "PREVIOUSLY_EMPLOYED"
    assert person.application_answers[0].from_session is False


def test_unknown_approved_intent_is_rejected():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ApprovedAnswerIn(question="Ever been on our payroll?", answer="No", intent="NOT_AN_INTENT")


def test_placeholder_is_not_the_question():
    from autofill.mapping import map_field

    person = _profile()
    person.application_answers = [
        ApplicationAnswer("Which programming paradigm do you enjoy most?", "Functional")
    ]
    control = Control(
        kind="text",
        label="Notes",
        placeholder="Which programming paradigm do you enjoy most?",
    )
    mapped = map_field(control, person)
    assert mapped.action == "unanswered"


def test_question_stem_prefers_the_question_over_an_option():
    control = Control(
        kind="radio",
        group="VOLUNTARY",
        label="Yes",
        prompt="Are you authorized to work in the United States?",
        options=YES_NO,
    )
    assert question_stem(control) == "Are you authorized to work in the United States?"


def test_greenhouse_and_workday_sponsorship_share_one_value():
    saved = [ApplicationAnswer("Will you require sponsorship in the future?", "No")]
    greenhouse = "Will you require visa sponsorship in the future?"
    workday = "Will you need future sponsorship?"
    assert _match(greenhouse, saved, us_on_page=True) == "No"
    assert _match(workday, saved, us_on_page=True) == "No"
    saved.append(ApplicationAnswer("Will you need future sponsorship?", "Yes"))
    assert _match(greenhouse, saved, us_on_page=True) is None


def test_resolver_disagreement_returns_no_answer():
    response = ResolverResponse(
        answers=[
            ResolverAnswer(
                intent="SPONSORSHIP_FUTURE",
                value="Yes",
                text="Will you require sponsorship in the future?",
                confidence="HIGH",
            ),
            ResolverAnswer(
                intent="SPONSORSHIP_FUTURE",
                value="No",
                text="Will you need future sponsorship?",
                confidence="HIGH",
            ),
        ]
    )
    by_intent, unnamed, by_key = _index_answers(response)
    match = classify_question("Will you require visa sponsorship in the future?", us_on_page=True)
    assert _take_answer(match, by_intent, unnamed, by_key, us_on_page=True) is None


def test_resolver_agreement_returns_the_shared_value():
    response = ResolverResponse(
        answers=[
            ResolverAnswer(
                intent="SPONSORSHIP_FUTURE",
                value="No",
                text="Will you require sponsorship in the future?",
                confidence="HIGH",
            )
        ]
    )
    by_intent, unnamed, by_key = _index_answers(response)
    match = IntentMatch("SPONSORSHIP_FUTURE", "HIGH", "level1", "Will you need future sponsorship?")
    taken = _take_answer(match, by_intent, unnamed, by_key, us_on_page=True)
    assert taken is not None
    assert taken.value == "No"


def test_referral_is_not_reused():
    saved = [ApplicationAnswer("Who referred you?", "Ada", intent="REFERRAL")]
    assert _take_answer(
        IntentMatch("REFERRAL", "HIGH", "level1", "Employee referral"),
        {"REFERRAL": [ResolverAnswer(intent="REFERRAL", value="Ada", text="Who referred you?")]},
        [],
        {},
    ) is None
    assert _match("Who referred you?", saved) is None


def test_previously_employed_does_not_cross_company():
    saved = [
        ApplicationAnswer(
            "Have you previously worked for Acme?",
            "No",
            intent="PREVIOUSLY_EMPLOYED",
            employer="Acme",
        )
    ]
    assert _match("Have you previously worked for this company?", saved, employers=("Beta",)) is None
    assert _match("Have you previously worked for this company?", saved, employers=("Acme",)) == "No"
    untagged = [ApplicationAnswer("Have you previously worked here?", "No", intent="PREVIOUSLY_EMPLOYED")]
    assert _match("Have you worked at Beta before?", untagged, employers=("Beta",)) is None


def test_how_heard_stays_portable():
    saved = [ApplicationAnswer("How did you hear about us?", "LinkedIn", employer="Acme")]
    assert _match("Where did you hear about this job?", saved, employers=("Beta",)) == "LinkedIn"


def test_country_less_work_auth_needs_a_us_address():
    from autofill.mapping import map_field

    person = _profile()
    person.work_authorization.legally_authorized_to_work = "Yes"
    person.work_authorization.require_sponsorship = "No"
    assert person.personal.country == "Exampleland"
    withheld = map_field(_radio("Are you legally authorized to work?"), person)
    assert withheld.action == "unanswered"
    person.personal.country = "United States"
    filled = map_field(_radio("Are you legally authorized to work?"), person)
    assert filled.action == "select"
    assert filled.option_label == "Yes"
    person.personal.country = "Canada"
    canada = map_field(_radio("Are you authorized to work in Canada?"), person)
    assert canada.action == "unanswered"
    person.application_answers = [ApplicationAnswer("Are you legally authorized to work?", "No")]
    exact = map_field(_radio("Are you legally authorized to work?"), person)
    assert exact.action == "select"
    assert exact.option_label == "No"


def test_verified_answer_is_reused_on_the_next_question():
    from autofill.mapping import map_field

    person = _profile()
    person.work_authorization.legally_authorized_to_work = ""
    person.work_authorization.require_sponsorship = ""
    person.personal.country = "United States"
    note_session_answer(
        person,
        stem="Are you authorized to work in the United States?",
        value="Yes",
        field_key="legally_authorized_to_work",
    )
    mapped = map_field(_radio("Legally authorized to work in the U.S.?"), person)
    assert mapped.action == "select"
    assert mapped.option_label == "Yes"
    assert mapped.field_class == "APPROVED_QUESTION"


def test_session_answer_skips_consent_demographic_and_profile_fields():
    person = _profile()
    note_session_answer(person, stem="I agree to the Terms of Service", value="Yes", kind="CONSENT")
    note_session_answer(person, stem="Gender", value="Woman", field_key="gender")
    note_session_answer(person, stem="Email", value="casey.example@example.com", field_key="email")
    note_session_answer(
        person,
        stem="I am willing to undergo a background check",
        value="Yes",
        field_key="willing_background_check",
    )
    assert person.application_answers == []


def test_a_host_answer_that_disagrees_is_not_replaced():
    person = _profile()
    person.personal.country = "United States"
    person.application_answers = [
        ApplicationAnswer("Are you authorized to work in the United States?", "No", intent="US_WORK_AUTHORIZATION")
    ]
    note_session_answer(
        person,
        stem="Legally authorized to work in the U.S.?",
        value="Yes",
        field_key="US_WORK_AUTHORIZATION",
    )
    conflict = _match(
        "Are you legally authorized to work in the United States?",
        person.application_answers,
        us_on_page=True,
    )
    assert conflict is None


def test_loopback_is_not_an_employer_key():
    assert employer_keys(url="http://127.0.0.1:8765/apply") == ()
    assert employer_keys(company="Acme", url="https://acme.wd5.myworkdayjobs.com/en-US/Acme")[0] == "Acme"


def test_map_field_uses_the_job_company_for_previously_employed():
    from autofill.mapping import map_field

    person = _profile()
    person.screening.previously_employed_here = ""
    person.application_answers = [
        ApplicationAnswer(
            "Have you previously worked for Acme?",
            "No",
            intent="PREVIOUSLY_EMPLOYED",
            employer="Acme",
        )
    ]
    beta = map_field(
        _radio("Have you previously worked for this company?"),
        person,
        job=JobContext(company="Beta"),
    )
    assert beta.action == "unanswered"
    acme = map_field(
        _radio("Have you previously worked for this company?"),
        person,
        job=JobContext(company="Acme"),
    )
    assert acme.action == "select"
    assert acme.option_label == "No"
