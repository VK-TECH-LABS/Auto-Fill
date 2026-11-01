"""t60u Req2: accurate question mapping on the local Direct-Browser path.

Profile first, then approved answers. Equivalent wording is reused by intent.
Anything the bot cannot answer exactly is left for the employee, and every
write is read back from the page.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from autofill.answers import _JURISDICTION_INTENTS, match_answer
from autofill.intents import JURISDICTION_INTENTS, classify_question, jurisdiction
from autofill.mapping import map_field, match_option, normalize
from autofill.models import Control, Option
from autofill.profile import ApplicationAnswer, CandidateProfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "question_mapping_r2.html"
PROFILE = ROOT / "examples" / "profile.example.json"
YES_NO = [Option("Yes", "Yes"), Option("No", "No")]


def _match(question: str, answers: list[ApplicationAnswer], **kwargs) -> str | None:
    return match_answer(question, answers, normalize=normalize, **kwargs)


def _profile(authorized: str, sponsorship: str, answers: list[dict] | None = None) -> CandidateProfile:
    data = json.loads(PROFILE.read_text())
    data["work_authorization"] = {
        "legally_authorized_to_work": authorized,
        "require_sponsorship": sponsorship,
    }
    data["applicationAnswers"] = answers or []
    return CandidateProfile.from_dict(data)


def _radio(label: str, options: list[Option] | None = None) -> Control:
    return Control(kind="radio", label=label, options=options or YES_NO, selector="#q")


# --- saved-answer matching -------------------------------------------------


def test_jurisdiction_sets_match():
    assert _JURISDICTION_INTENTS == JURISDICTION_INTENTS


def test_sponsorship_now_future_and_combined_are_not_interchangeable():
    saved = [ApplicationAnswer("Will you now or in the future require visa sponsorship?", "Yes")]
    assert _match("Will you now or in the future require sponsorship?", saved, us_on_page=True) == "Yes"
    assert _match("Do you currently require sponsorship?", saved) is None
    assert _match("Will you require sponsorship in the future?", saved) is None
    assert _match("Are you authorized to work in the US without sponsorship?", saved) is None


def test_us_answer_is_not_reused_for_another_country():
    saved = [ApplicationAnswer("Are you legally authorized to work in the United States?", "Yes")]
    assert _match("Are you authorized to work in the U.S.?", saved) == "Yes"
    assert _match("Are you authorized to work in Canada?", saved) is None
    assert _match("Are you eligible to work in the UK?", saved) is None
    canada = [ApplicationAnswer("Are you authorized to work in Canada?", "No")]
    assert _match("Are you authorized to work in Canada?", canada) == "No"
    assert _match("Are you authorized to work in the United States?", canada) is None


def test_canada_question_has_no_us_intent():
    assert classify_question("Are you authorized to work in Canada?").intent is None
    assert classify_question("Are you authorized to work in the United States?").intent == "US_WORK_AUTHORIZATION"
    assert jurisdiction("Do you require sponsorship?") == frozenset({"US"})
    assert jurisdiction("Do you require sponsorship to work in the European Union?") == frozenset({"EU"})


@pytest.mark.parametrize(
    ("asked", "saved_question", "answer"),
    [
        ("Are you at least 18 years of age?", "Are you 18 or older?", "Yes"),
        ("Are you over 18?", "Are you age 18 or older?", "Yes"),
        ("Desired salary", "What are your salary expectations?", "120000"),
        ("When can you start?", "Earliest start date", "2026-12-01"),
        ("Where did you hear about this job?", "How did you hear about us?", "LinkedIn"),
        ("Will you require visa sponsorship in the future?", "Will you need future sponsorship?", "No"),
    ],
)
def test_equivalent_wording_is_reused(asked: str, saved_question: str, answer: str):
    assert _match(asked, [ApplicationAnswer(saved_question, answer)], us_on_page=True) == answer


def test_tilearc_intent_is_used_when_the_saved_text_has_no_intent_of_its_own():
    saved = [ApplicationAnswer("Ever been on our payroll?", "No", intent="PREVIOUSLY_EMPLOYED")]
    assert _match("Have you previously worked for Example Corp?", saved) == "No"


def test_a_carried_intent_that_contradicts_the_saved_text_is_ignored():
    saved = [ApplicationAnswer("Will you require sponsorship in the future?", "No", intent="SPONSORSHIP_NOW")]
    assert _match("Do you currently require sponsorship?", saved) is None


def test_unknown_carried_intent_is_ignored():
    saved = [ApplicationAnswer("Ever been on our payroll?", "No", intent="NOT_AN_INTENT")]
    assert _match("Have you previously worked for Example Corp?", saved) is None


def test_two_saved_answers_for_one_intent_that_disagree_are_not_guessed():
    saved = [
        ApplicationAnswer("Are you 18 or older?", "Yes"),
        ApplicationAnswer("Are you at least 18?", "No"),
    ]
    assert _match("Are you over 18 years of age?", saved) is None


def test_loose_substring_does_not_answer_a_narrower_question():
    saved = [ApplicationAnswer("Do you have a degree", "Yes")]
    assert _match("Do you have a degree in Computer Science?", saved) is None
    long_saved = [ApplicationAnswer("Do you have experience leading a team of engineers", "Yes")]
    assert _match("Do you have experience leading a team of engineers?", long_saved) == "Yes"


def test_referral_and_demographic_questions_are_never_reused_by_intent():
    saved = [
        ApplicationAnswer("Were you referred by an employee?", "Yes"),
        ApplicationAnswer("What is your gender identity?", "Woman"),
    ]
    assert _match("Who referred you?", saved) is None
    assert _match("Gender", saved) is None


def test_exact_text_still_wins():
    saved = [ApplicationAnswer("Which programming paradigm do you enjoy most?", "Functional")]
    assert _match("Which programming paradigm do you enjoy most?", saved) == "Functional"


def test_profile_payload_carries_the_intent():
    person = _profile("Yes", "No", [{"question": "Q", "answer": "No", "intent": "PREVIOUSLY_EMPLOYED"}])
    assert person.application_answers[0].intent == "PREVIOUSLY_EMPLOYED"


# --- profile work-authorization semantics ---------------------------------


@pytest.mark.parametrize(
    ("authorized", "sponsorship", "label", "expected"),
    [
        ("Yes", "No", "Are you authorized to work in the US without sponsorship?", "Yes"),
        ("Yes", "Yes", "Are you authorized to work in the US without sponsorship?", "No"),
        ("No", "", "Are you authorized to work in the US without sponsorship?", "No"),
        ("Yes", "", "Are you authorized to work in the US without sponsorship?", None),
        ("Yes", "No", "Do you currently require visa sponsorship?", "No"),
        ("Yes", "Yes", "Do you currently require visa sponsorship?", None),
        ("Yes", "Yes", "Will you now or in the future require sponsorship?", "Yes"),
        ("Yes", "No", "Will you require sponsorship in the future?", "No"),
        ("Yes", "No", "Are you authorized to work in Canada?", None),
        ("Yes", "Yes", "Are you authorized to work for any employer?", None),
        ("Yes", "No", "Are you authorized to work for any employer?", "Yes"),
        ("Yes", "Yes", "Are you legally authorized to work in the United States?", "Yes"),
    ],
)
def test_work_authorization_answers_the_question_that_is_asked(authorized, sponsorship, label, expected):
    person = _profile(authorized, sponsorship)
    person.personal.country = "United States"
    mapped = map_field(_radio(label), person)
    if expected is None:
        assert mapped.action == "unanswered"
        assert mapped.field_class == "MANUAL_REVIEW_FIELD"
    else:
        assert mapped.action == "select"
        assert mapped.option_label == expected


def test_non_us_question_uses_an_approved_answer_for_that_country():
    person = _profile("Yes", "No", [{"question": "Are you authorized to work in Canada?", "answer": "No"}])
    mapped = map_field(_radio("Are you authorized to work in Canada?"), person)
    assert mapped.action == "select"
    assert mapped.option_label == "No"


def test_profile_and_approved_work_answer_disagree_asks_the_employee():
    person = _profile(
        "Yes",
        "No",
        [{"question": "Will you now or in the future require sponsorship?", "answer": "Yes"}],
    )
    person.personal.country = "United States"
    mapped = map_field(_radio("Will you now or in the future require visa sponsorship?"), person)
    assert mapped.action == "unanswered"
    assert "disagree" in mapped.reason


def test_approved_answer_fills_a_compound_the_profile_cannot_answer():
    person = _profile(
        "Yes",
        "",
        [
            {
                "question": "Are you legally authorized to work in the U.S. without visa sponsorship?",
                "answer": "Yes",
                "intent": "AUTHORIZED_WITHOUT_SPONSORSHIP",
            }
        ],
    )
    mapped = map_field(_radio("Are you authorized to work in the US without sponsorship?"), person)
    assert mapped.action == "select"
    assert mapped.option_label == "Yes"


# --- options ----------------------------------------------------------------


def test_several_yes_options_are_a_choice_for_the_employee():
    options = [
        Option(label="Yes, a US license", value="us"),
        Option(label="Yes, an international license", value="intl"),
        Option(label="No", value="no"),
    ]
    assert match_option("Yes", options) is None
    assert match_option("No", options).value == "no"


def test_a_single_leading_yes_option_is_still_chosen():
    options = [Option(label="Yes, I am authorized", value="y"), Option(label="No, I am not", value="n")]
    assert match_option("Yes", options).value == "y"
    assert match_option("No", options).value == "n"


def test_no_does_not_pick_not_applicable():
    options = [
        Option(label="Not applicable", value="na"),
        Option(label="Nope", value="nope"),
        Option(label="Yes", value="y"),
    ]
    assert match_option("No", options) is None


def test_yes_no_buttons_are_selected_not_typed():
    control = Control(
        kind="buttons",
        label="Are you legally authorized to work in the United States?",
        options=[Option("Yes", "Yes", "#a-yes"), Option("No", "No", "#a-no")],
        selector="#a-yes",
    )
    mapped = map_field(control, _profile("Yes", "No"))
    assert mapped.action == "select"
    assert mapped.option_selector == "#a-yes"


# --- verification -----------------------------------------------------------


class _Locator:
    def __init__(self, state: bool) -> None:
        self.state = state
        self.clicked = 0
        self.files: list[str] = []

    def click(self) -> None:
        self.clicked += 1

    def set_input_files(self, path: str) -> None:
        self.files.append(path)

    def evaluate(self, _script: str, *_args) -> bool:
        return self.state


class _Page:
    def __init__(self, state: bool) -> None:
        self.loc = _Locator(state)

    def locator(self, _selector: str) -> _Locator:
        return self.loc


@pytest.mark.parametrize(("state", "action"), [(True, "select"), (False, "unanswered")])
def test_buttons_choice_is_read_back(monkeypatch, state, action):
    from autofill import fill
    from autofill.models import MappedField

    monkeypatch.setattr(fill, "_bound_target", lambda page, _frame: page)
    monkeypatch.setattr(fill, "refresh_preview", lambda *_a, **_k: None)
    monkeypatch.setattr(fill, "remember_confirmed", lambda *_a, **_k: None)
    control = Control(kind="buttons", label="Q", options=YES_NO, selector="#yes")
    mapped = MappedField(key="k", action="select", text="Yes", option_label="Yes", option_selector="#yes")
    outcome = fill.apply_mapped(_Page(state), control, mapped)
    assert outcome.action == action


@pytest.mark.parametrize(("state", "action"), [(True, "upload"), (False, "unanswered")])
def test_cover_letter_upload_is_read_back(monkeypatch, state, action):
    from autofill import fill
    from autofill.models import MappedField

    monkeypatch.setattr(fill, "_bound_target", lambda page, _frame: page)
    control = Control(kind="file", label="Cover letter", selector="#cl")
    mapped = MappedField(key="cover_letter", action="upload", text="/tmp/cover.pdf")
    outcome = fill.apply_mapped(_Page(state), control, mapped)
    assert outcome.action == action


# --- real DOM, two candidates ---------------------------------------------

_VALUES_JS = """() => {
  const out = {};
  for (const el of document.querySelectorAll('input, select, textarea')) {
    if (el.type === 'radio' || el.type === 'checkbox') { if (el.checked) out[el.id] = 'checked'; }
    else out[el.id] = el.value;
  }
  for (const b of document.querySelectorAll('[aria-pressed=true]')) out[b.id] = 'pressed';
  return out;
}"""


def _candidate(name: str) -> dict:
    data = json.loads(PROFILE.read_text())
    data["personal"]["country"] = "United States"
    if name == "authorized":
        data["screening"]["previously_employed_here"] = ""
        data["applicationAnswers"] = [
            {"question": "Ever been on our payroll?", "answer": "No", "intent": "PREVIOUSLY_EMPLOYED"},
            {"question": "Do you have a driver's license?", "answer": "Yes"},
        ]
        return data
    data = copy.deepcopy(data)
    data["personal"].update(full_name="Riley Sample", preferred_name="Riley", email="riley@example.org")
    data["work_authorization"] = {"legally_authorized_to_work": "Yes", "require_sponsorship": "Yes"}
    data["screening"]["how_heard"] = "LinkedIn"
    data["applicationAnswers"] = [
        {"question": "Where did you hear about us?", "answer": "Employee referral"},
        {"question": "Are you authorized to work in Canada?", "answer": "No"},
    ]
    return data


def _run_fixture(profile: dict) -> tuple[object, dict, bool]:
    from playwright.sync_api import sync_playwright

    from autofill.local_attach import fill_visible_tab, start_fixture_server

    server = start_fixture_server(FIXTURE.read_bytes())
    url = f"http://127.0.0.1:{int(server.server_address[1])}/t172-local-tab.html"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded")
                result = fill_visible_tab(page, url, CandidateProfile.from_dict(profile))
                return result, page.evaluate(_VALUES_JS), bool(page.evaluate("() => window.__submitted"))
            finally:
                browser.close()
    finally:
        server.shutdown()


def _open(result) -> dict[str, dict]:
    return {str(item.get("text") or ""): item for item in result.open_items}


def test_real_dom_candidate_who_is_authorized_without_sponsorship(monkeypatch):
    pytest.importorskip("playwright")
    from autofill.engine import Status
    from autofill.local_attach import ENV_FLAG

    monkeypatch.setenv(ENV_FLAG, "1")
    result, values, submitted = _run_fixture(_candidate("authorized"))
    assert submitted is False
    assert result.stopped_before_submit is True
    assert result.status == Status.MANUAL_ANSWER_REQUIRED
    assert values["first"] == "Casey"
    assert values["email"] == "casey.example@example.com"
    assert values.get("auth-yes") == "checked"
    assert values["sponsor"] == "No"
    assert values.get("without-yes") == "pressed"
    assert "canada-yes" not in values and "canada-no" not in values
    assert values["age"] == "Yes"
    assert values["heard"] == "Online job board"
    assert values["prev"] == "No"
    assert not any(key.startswith("dl-") for key in values)
    assert values["paradigm"] == ""
    open_items = _open(result)
    assert set(open_items) == {
        "Are you authorized to work in Canada?",
        "Do you hold a valid driver's license?",
        "Which programming paradigm do you enjoy most, and why?",
    }
    assert open_items["Are you authorized to work in Canada?"]["intent"] is None
    assert len(open_items["Do you hold a valid driver's license?"]["options"]) == 3


def test_real_dom_candidate_who_needs_sponsorship(monkeypatch):
    pytest.importorskip("playwright")
    from autofill.engine import Status
    from autofill.local_attach import ENV_FLAG

    monkeypatch.setenv(ENV_FLAG, "1")
    result, values, submitted = _run_fixture(_candidate("sponsored"))
    assert submitted is False
    assert result.status == Status.MANUAL_ANSWER_REQUIRED
    assert values["first"] == "Riley"
    assert values.get("auth-yes") == "checked"
    assert values["sponsor"] == "Yes"
    assert values.get("without-no") == "pressed"
    assert values.get("canada-no") == "checked"
    assert values["heard"] == ""
    open_items = _open(result)
    assert "How did you hear about this role?" in open_items
    assert "Are you authorized to work in Canada?" not in open_items
    assert "Which programming paradigm do you enjoy most, and why?" in open_items
