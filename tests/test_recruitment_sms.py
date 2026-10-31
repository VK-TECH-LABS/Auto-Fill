"""Recruitment SMS consent is an Application Answer, not a standard-terms checkbox.

Authorized for this candidate → auto Yes, recorded on autoConsents.
Missing authorization → one human stop. A different candidate is refused.
Marketing, recording, arbitration, e-sign, background checks, and certifications
stay manual.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autofill.intents import classify_question
from autofill.mapping import (
    bind_standard_terms,
    consume_auto_consents,
    map_field,
    terms_category,
)
from autofill.models import Control, Option
from autofill.profile import CandidateProfile
from autofill.service.schemas import ApprovedAnswerIn
from autofill.sms_consent import (
    SMS_INTENT,
    SMS_KEY,
    accept_recruitment_sms,
    answer_authorizes_sms,
    begin_sms_pass,
    bind_sms_scope,
    collapse_sms_gaps,
    is_recruitment_sms_text,
    learned_application_answers,
    remember_human_sms,
    sms_authorized,
    sms_stored_choice,
)

FIXTURES = Path(__file__).parent / "fixtures"
SMS_QUESTION = "Would you like to receive text messages about your application?"
OTHER_SMS = "Receive text message updates about this application"


def _profile(candidate_id: str = "cand-a", answers: list | None = None) -> CandidateProfile:
    return CandidateProfile.from_dict(
        {
            "candidateId": candidate_id,
            "personal": {"full_name": "River Example", "email": "river.example@example.com"},
            "applicationAnswers": answers or [],
        }
    )


def _yes_row(intent: str = SMS_INTENT, answer: str = "Yes", question: str = SMS_QUESTION) -> dict:
    return {"question": question, "answer": answer, "intent": intent}


def _radio(label: str, *, name: str = "sms", required: bool = True) -> Control:
    return Control(
        kind="radio",
        label=label,
        name=name,
        selector=f"#{name}-yes",
        required=required,
        options=[
            Option("Yes", "Yes", f"#{name}-yes"),
            Option("No", "No", f"#{name}-no"),
        ],
    )


def _box(label: str, selector: str = "#box") -> Control:
    return Control(kind="checkbox", label=label, selector=selector, required=True)


def test_workday_wording_is_the_sms_intent():
    match = classify_question("Would you like to receive text messages from GM about your application?")
    assert match.intent == SMS_INTENT
    assert match.confidence == "HIGH"
    assert is_recruitment_sms_text("I consent to receive SMS text messages about my application")
    assert not is_recruitment_sms_text("I agree to receive promotional SMS")
    assert not is_recruitment_sms_text("I agree to receive marketing emails")
    assert not is_recruitment_sms_text("I agree to binding arbitration")


def test_sms_consent_alias_is_canonical():
    profile = _profile(answers=[_yes_row("SMS_CONSENT")])
    assert profile.application_answers[0].intent == SMS_INTENT
    assert profile.application_answers[0].key == SMS_KEY
    approved = ApprovedAnswerIn(question=SMS_QUESTION, answer="yes", intent="SMS_CONSENT")
    assert approved.intent == SMS_INTENT
    assert approved.key == SMS_KEY
    assert approved.answer == "Yes"
    keyed = ApprovedAnswerIn(question=SMS_KEY, answer="No", key="SMS_CONSENT")
    assert keyed.key == SMS_KEY
    assert keyed.intent == SMS_INTENT
    assert keyed.answer == "No"
    with pytest.raises(ValueError):
        ApprovedAnswerIn(question="Anything", answer="Yes", intent="NOT_A_REAL_INTENT")


def test_catalogue_key_yes_authorizes_and_no_does_not():
    yes = _profile(answers=[{"key": "recruitment_sms_consent", "answer": "Yes"}])
    assert yes.application_answers[0].intent == SMS_INTENT
    assert yes.application_answers[0].key == SMS_KEY
    assert yes.application_answers[0].answer == "Yes"
    bind_sms_scope(yes, "cand-a")
    assert sms_authorized(yes, "cand-a")
    assert map_field(_radio(SMS_QUESTION), yes).action == "select"
    declined = _profile(answers=[{"key": "sms_consent", "answer": "No"}])
    assert declined.application_answers[0].key == SMS_KEY
    assert declined.application_answers[0].answer == "No"
    bind_sms_scope(declined, "cand-a")
    assert sms_stored_choice(declined, "cand-a") == "no"
    assert not sms_authorized(declined, "cand-a")
    assert map_field(_radio(SMS_QUESTION), declined).action == "skip"
    begin_sms_pass()
    assert collapse_sms_gaps(
        [{"intent": SMS_INTENT, "text": SMS_QUESTION, "kind": "CONSENT"}],
        skip_all=True,
    ) == []


def test_authorized_answer_maps_to_yes_for_this_candidate_only():
    profile = _profile(answers=[_yes_row()])
    bind_sms_scope(profile, "cand-a")
    bind_standard_terms(False)
    mapped = map_field(_radio(SMS_QUESTION), profile)
    assert mapped.action == "select"
    assert mapped.key == SMS_INTENT
    assert mapped.option_label == "Yes"
    # The same row must not fill a different candidate.
    bind_sms_scope(profile, "cand-b")
    refused = map_field(_radio(SMS_QUESTION), profile)
    assert refused.action == "skip"
    assert not sms_authorized(profile, "cand-b")
    consume_auto_consents()


def test_missing_authorization_stays_manual_and_one_stop_covers_both_fields():
    profile = _profile()
    bind_sms_scope(profile, "cand-a")
    assert map_field(_radio(SMS_QUESTION), profile).action == "skip"
    assert map_field(_radio(OTHER_SMS, name="sms2"), profile).action == "skip"
    begin_sms_pass()
    gaps = collapse_sms_gaps(
        [
            {"intent": SMS_INTENT, "text": SMS_QUESTION, "kind": "CONSENT_CHOICE"},
            {"intent": SMS_INTENT, "text": OTHER_SMS, "kind": "CONSENT_CHOICE"},
            {"intent": None, "text": "I agree to binding arbitration", "kind": "CONSENT"},
        ]
    )
    sms = [gap for gap in gaps if gap.get("intent") == SMS_INTENT]
    assert len(sms) == 1
    assert any("arbitration" in gap["text"] for gap in gaps)


def test_decline_is_not_asked_again_and_is_not_stored_as_authorization():
    profile = _profile()
    choice = remember_human_sms(
        profile,
        question=SMS_QUESTION,
        value="No",
        kind="CONSENT_CHOICE",
        candidate_id="cand-a",
    )
    assert choice == "no"
    assert profile.application_answers[-1].key == SMS_KEY
    assert profile.application_answers[-1].intent == SMS_INTENT
    assert profile.application_answers[-1].answer == "No"
    assert learned_application_answers(profile, "cand-a") == [
        {
            "key": SMS_KEY,
            "intent": SMS_INTENT,
            "question": SMS_QUESTION,
            "answer": "No",
            "candidateId": "cand-a",
        }
    ]
    assert not sms_authorized(profile, "cand-a")
    begin_sms_pass()
    gaps = collapse_sms_gaps(
        [
            {"intent": SMS_INTENT, "text": SMS_QUESTION, "kind": "CONSENT"},
            {"intent": SMS_INTENT, "text": OTHER_SMS, "kind": "CONSENT"},
        ],
        skip_all=True,
    )
    assert gaps == []


def test_human_yes_is_persisted_for_the_same_candidate():
    profile = _profile()
    choice = remember_human_sms(
        profile,
        question=SMS_QUESTION,
        value="confirm",
        kind="CONSENT",
        candidate_id="cand-a",
    )
    assert choice == "yes"
    saved = profile.application_answers[-1]
    assert saved.intent == SMS_INTENT
    assert saved.answer == "Yes"
    assert saved.from_session is True
    learned = learned_application_answers(profile, "cand-a")
    assert learned == [
        {
            "key": SMS_KEY,
            "intent": SMS_INTENT,
            "question": SMS_QUESTION,
            "answer": "Yes",
            "candidateId": "cand-a",
        }
    ]
    assert learned_application_answers(profile, "cand-b") == []
    assert remember_human_sms(
        profile,
        question=SMS_QUESTION,
        value="Yes",
        candidate_id="cand-b",
    ) == ""
    bind_sms_scope(profile, "cand-a")
    # A later apply, and a different wording, reuse the stored Yes.
    again = _profile(
        answers=[
            {
                "question": saved.question,
                "answer": saved.answer,
                "intent": saved.intent,
            }
        ]
    )
    bind_sms_scope(again, "cand-a")
    mapped = map_field(_radio(OTHER_SMS, name="later"), again)
    assert mapped.action == "select"
    assert mapped.option_label == "Yes"


def test_unrelated_consents_stay_manual_when_sms_is_authorized():
    profile = _profile(answers=[_yes_row()])
    bind_sms_scope(profile, "cand-a")
    bind_standard_terms(True)
    unrelated = [
        "I agree to receive marketing emails",
        "I agree to receive promotional SMS",
        "I agree to the use of AI notetakers to record my interviews",
        "I agree to binding arbitration",
        "I consent to sign documents with an electronic signature",
        "I authorize a background check",
        "I certify that the information I provide is true",
        "I agree to share my demographic information",
    ]
    for label in unrelated:
        assert terms_category(_box(label)) == "CONSENT"
        assert map_field(_box(label), profile).action == "skip"
    terms = map_field(_box("I agree to the Terms of Service", "#terms"), profile)
    assert terms.action == "skip"
    assert terms_category(_box("I agree to the Terms of Service")) == "STANDARD_TERMS"
    assert terms_category(_box("I consent to receive SMS text messages about my application")) == "CONSENT"
    consume_auto_consents()


def test_agree_phrases_authorize_and_opt_out_does_not():
    assert answer_authorizes_sms("Yes")
    assert answer_authorizes_sms("I agree")
    assert answer_authorizes_sms("Yes - I consent to receive text message updates")
    assert answer_authorizes_sms("Opt in")
    assert not answer_authorizes_sms("No")
    assert not answer_authorizes_sms("I do not consent")
    conflicting = _profile(
        answers=[
            _yes_row(answer="Yes"),
            _yes_row(answer="No", question="SMS consent"),
        ]
    )
    assert not sms_authorized(conflicting, "cand-a")


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        pytest.skip(f"Playwright is not installed: {exc}")
    try:
        with sync_playwright() as playwright:
            chromium = playwright.chromium.launch(headless=True)
            try:
                yield chromium
            finally:
                chromium.close()
    except Exception as exc:
        pytest.skip(f"Chromium is not installed: {exc}")


_YES = "Yes - I consent to receive text message updates"
_NO = "No - I do not consent to receive text message updates"
_PAGE = f"""
<fieldset>
  <legend>Would you like to receive text messages about your application?</legend>
  <label><input type="radio" name="sms-a" value="Yes" required> Yes</label>
  <label><input type="radio" name="sms-a" value="No" required> No</label>
</fieldset>
<fieldset>
  <legend>Receive text message updates about this application</legend>
  <label><input type="radio" name="sms-b" value="{_YES}" required> {_YES}</label>
  <label><input type="radio" name="sms-b" value="{_NO}" required> {_NO}</label>
</fieldset>
<label><input id="terms" type="checkbox" required> I agree to the Terms of Service</label>
<label><input id="promo" type="checkbox" required> I agree to receive promotional SMS</label>
<label><input id="mail" type="checkbox" required> I agree to receive marketing emails</label>
<label><input id="arb" type="checkbox" required> I agree to binding arbitration</label>
<label><input id="ai" type="checkbox" required> I agree to the use of AI notetakers to record my interviews</label>
<label><input id="sign" type="checkbox" required> I consent to sign documents with an electronic signature</label>
<label><input id="bg" type="checkbox" required> I authorize a background check</label>
<label><input id="cert" type="checkbox" required> I certify that the information I provide is true</label>
"""


def _open(browser):
    page = browser.new_page()
    page.set_content(_PAGE)
    return page


def test_authorized_fill_checks_yes_and_records_the_audit(browser):
    from autofill.fill import fill_one_page
    from autofill.stepfill import inspect_required

    profile = _profile(answers=[_yes_row()])
    bind_sms_scope(profile, "cand-a")
    bind_standard_terms(True)
    page = _open(browser)
    try:
        fill_one_page(
            page,
            profile,
            cover_letter_path=None,
            cover_letter_text=None,
            job=None,
            resume_uploaded=False,
        )
        assert page.locator("input[name='sms-a'][value='Yes']").is_checked()
        assert page.locator(f"input[name='sms-b'][value='{_YES}']").is_checked()
        assert page.locator("input[name='sms-a'][value='No']").is_checked() is False
        assert page.locator("#terms").is_checked()
        for selector in ("#promo", "#mail", "#arb", "#ai", "#sign", "#bg", "#cert"):
            assert page.locator(selector).is_checked() is False
        categories = [row["category"] for row in consume_auto_consents()]
        assert "RECRUITMENT_SMS_CONSENT" in categories
        assert "STANDARD_TERMS" in categories
        assert categories.count("RECRUITMENT_SMS_CONSENT") == 2
        gaps, _total, _done = inspect_required(page, page_snapshot(page), profile)
        sms_gaps = [gap for gap in gaps if "text message" in str(gap.get("text") or "").casefold()]
        assert sms_gaps == []
    finally:
        page.close()
        consume_auto_consents()


def test_unauthorized_fill_stops_once_and_leaves_other_consents(browser):
    from autofill.extract import extract_page
    from autofill.stepfill import inspect_required

    profile = _profile()
    bind_sms_scope(profile, "cand-a")
    bind_standard_terms(False)
    page = _open(browser)
    try:
        assert accept_recruitment_sms(page, _radio(SMS_QUESTION), profile) is False
        snapshot = extract_page(page)
        gaps, _total, _done = inspect_required(page, snapshot, profile)
        sms_gaps = [
            gap
            for gap in gaps
            if gap.get("intent") == SMS_INTENT or "text message" in str(gap.get("text") or "").casefold()
        ]
        assert len(sms_gaps) == 1
        assert sms_gaps[0]["kind"] in {"CONSENT", "CONSENT_CHOICE"}
        assert page.locator("input[name='sms-a'][value='Yes']").is_checked() is False
        assert page.locator("#terms").is_checked() is False
        assert page.locator("#promo").is_checked() is False
        assert page.locator("#arb").is_checked() is False
        assert consume_auto_consents() == []
    finally:
        page.close()


def page_snapshot(page):
    from autofill.extract import extract_page

    return extract_page(page)
