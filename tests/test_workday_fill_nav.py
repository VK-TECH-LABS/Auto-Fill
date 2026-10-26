"""Workday-style phone boxes and search prompts. Not one employer.

A phone input beside a calling-code menu gets the national number. A prompt
whose box says Search is still the question on its label. A former-employee
Yes/No is filled only from an approved answer.
"""

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import sync_playwright

from autofill.intents import classify_question
from autofill.mapping import map_field, national_phone_digits
from autofill.models import Control, Option, PageSnapshot
from autofill.pagewait import application_controls
from autofill.profile import CandidateProfile
from autofill.stepfill import FillFlags, flags_for_alerts

EXAMPLE_PHONE = "+1 (555) 010-0199"
NATIONAL = "5550100199"


@pytest.fixture(scope="module")
def browser():
    try:
        with sync_playwright() as playwright:
            chromium = playwright.chromium.launch(headless=True)
            try:
                yield chromium
            finally:
                chromium.close()
    except Exception as exc:
        pytest.skip(f"Chromium is not installed: {exc}")


def _person(phone: str = EXAMPLE_PHONE, **sections) -> CandidateProfile:
    data = {
        "personal": {
            "full_name": "Casey Example",
            "email": "casey.example@example.com",
            "phone": phone,
        }
    }
    data.update(sections)
    return CandidateProfile.from_dict(data)


def _control(label: str, **kwargs) -> Control:
    data = {"kind": "text", "label": label, "selector": "#x"}
    data.update(kwargs)
    return Control(**data)


def test_national_digits_drop_a_separate_calling_code():
    assert national_phone_digits("+1 (555) 010-0199") == NATIONAL
    assert national_phone_digits("+15550100199") == NATIONAL
    assert national_phone_digits("1-555-010-0199") == NATIONAL
    assert national_phone_digits("555-010-0199") == NATIONAL
    assert national_phone_digits("+44 20 7946 0958") == "2079460958"
    assert national_phone_digits("13800138000") == "13800138000"


def test_phone_beside_a_calling_code_menu_is_national_and_a_single_box_is_not():
    person = _person()
    national = map_field(_control("Phone Number", input_type="tel", national_phone=True), person)
    assert national.key == "phone"
    assert national.action == "fill"
    assert national.text == NATIONAL
    full = map_field(_control("Phone", autocomplete="tel", input_type="tel"), person)
    assert full.text == EXAMPLE_PHONE
    explicit = map_field(
        _control("Phone", autocomplete="tel-national", input_mode="numeric"),
        _person("555-010-0199"),
    )
    assert explicit.text == NATIONAL
    international = map_field(
        _control("Phone", autocomplete="tel-national", input_mode="numeric"),
        person,
    )
    assert international.text == NATIONAL


def test_phone_format_alert_asks_for_national_digits():
    flags = flags_for_alerts("error - phone number enter a valid format for phone number.", FillFlags())
    assert flags.digits_phone is True
    assert flags_for_alerts("this field is required", FillFlags()).digits_phone is False


def test_country_phone_code_is_not_the_address_country():
    person = _person()
    code = map_field(_control("Country Phone Code", kind="combobox"), person)
    assert code.key == "phone_country"
    territory = map_field(
        _control(
            "Country / Territory",
            kind="select",
            options=[Option(value="US", label="United States of America")],
        ),
        person,
    )
    assert territory.key == "country"


def test_how_heard_prompt_is_a_menu_choice_and_is_not_invented():
    heard = map_field(
        _control("How did you hear about us?", kind="combobox"),
        _person(screening={"how_heard": "Job Board"}),
    )
    assert heard.key == "how_heard"
    assert heard.action == "select"
    assert heard.option_label == "Job Board"
    missing = map_field(_control("How did you hear about us?", kind="combobox"), _person())
    assert missing.action == "unanswered"
    assert missing.text == ""
    assert classify_question("How Did You Hear About Us?", []).intent is None


def test_former_employee_yes_no_is_not_guessed():
    radios = _control(
        "Are you a former employee of this company?",
        kind="radio",
        options=[Option(value="yes", label="Yes", selector="#yes"), Option(value="no", label="No", selector="#no")],
    )
    missing = map_field(radios, _person())
    assert missing.action == "unanswered"
    assert missing.text == ""
    assert missing.option_label == ""
    answered = map_field(
        radios,
        _person(screening={"previously_employed_here": "No"}),
    )
    assert answered.action == "select"
    assert answered.option_label == "No"


def test_a_search_prompt_with_a_question_is_an_application_field():
    prompt = Control(
        kind="combobox",
        label="How Did You Hear About Us?",
        placeholder="Search",
        aria_label="Search",
        selector="#source",
        required=True,
    )
    site = Control(kind="text", label="Search", placeholder="Search jobs", selector="#q", input_type="search")
    kept = application_controls(PageSnapshot(controls=[prompt, site], buttons=[]))
    assert [item.selector for item in kept] == ["#source"]


_PAGE = """
<header>
  <label for="q">Search</label>
  <input id="q" type="search" placeholder="Search jobs" aria-label="Search">
</header>
<form>
  <h1>My Information</h1>
  <div id="phone-row">
    <label for="code">Country Phone Code</label>
    <select id="code">
      <option value="">Select</option>
      <option value="US">United States of America</option>
      <option value="GB">United Kingdom</option>
    </select>
    <label for="national">Phone Number *</label>
    <input id="national" type="tel" data-automation-id="phoneNumber">
  </div>
  <div id="plain">
    <label for="full">Phone</label>
    <input id="full" type="tel" autocomplete="tel">
  </div>
  <div id="address">
    <label for="territory">Country / Territory</label>
    <select id="territory"><option>United States of America</option></select>
  </div>
  <div data-automation-id="formField-source">
    <label>How Did You Hear About Us?<abbr>*</abbr></label>
    <div data-automation-id="multiSelectContainer">
      <input id="source" aria-label="Search" placeholder="Search" data-automation-id="searchBox">
      <button type="button" data-automation-id="promptIcon" aria-label="Search"></button>
      <span id="picked" data-automation-id="selectedItem" hidden></span>
    </div>
  </div>
  <ul id="source-list" role="listbox" hidden style="display:block;min-height:24px">
    <li id="job-board" role="option" style="height:24px">Job Board</li>
    <li id="referral" role="option" style="height:24px">Employee Referral</li>
  </ul>
  <fieldset>
    <legend>Are you a former employee of this company?</legend>
    <label><input type="radio" name="prev" value="yes" required> Yes</label>
    <label><input type="radio" name="prev" value="no"> No</label>
  </fieldset>
</form>
<script>
  const list = document.getElementById("source-list");
  const source = document.getElementById("source");
  function showList() { list.hidden = false; }
  source.addEventListener("focus", showList);
  source.addEventListener("click", showList);
  source.addEventListener("input", showList);
  for (const option of list.querySelectorAll("[role='option']")) {
    option.addEventListener("click", () => {
      const picked = document.getElementById("picked");
      picked.hidden = false;
      picked.textContent = option.textContent;
      source.value = "";
      list.hidden = true;
    });
  }
</script>
"""


def _binding():
    from autofill.resolver import ResolverBinding

    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    return ResolverBinding(
        url="https://resolver.example.test/v1/resolve",
        token="token",
        expires_at=expires,
        candidate_ref="ref-a",
        job_ref="job-a",
    )


def _resolve(monkeypatch, *, source: str = "", former: str = ""):
    from autofill.resolver import ResolverAnswer, ResolverResponse

    def fake_resolve(binding, *, session_id, step, fields, questions):
        del binding, session_id, step, fields
        answers = []
        for question in questions:
            text = str(question.get("text") or "")
            if source and "how did you hear" in text.casefold():
                answers.append(
                    ResolverAnswer(
                        intent=None,
                        value=source,
                        confidence="HIGH",
                        source="saved_answer",
                        text=text,
                    )
                )
            if former and question.get("intent") == "PREVIOUSLY_EMPLOYED":
                answers.append(
                    ResolverAnswer(
                        intent="PREVIOUSLY_EMPLOYED",
                        value=former,
                        confidence="HIGH",
                        source="saved_answer",
                        text=text,
                    )
                )
        return ResolverResponse(fields={"phone": EXAMPLE_PHONE}, answers=answers)

    monkeypatch.setattr("autofill.stepfill.resolve_step", fake_resolve)


def _by_id(snapshot):
    return {control.element_id: control for control in snapshot.controls}


def test_workday_prompt_and_phone_are_extracted(browser):
    from autofill.extract import extract_page

    page = browser.new_page()
    try:
        page.set_content(_PAGE)
        found = _by_id(extract_page(page))
        national = found["national"]
        assert national.national_phone is True
        assert national.input_type == "tel"
        assert national.autocomplete != "tel-national"
        assert found["full"].national_phone is False
        assert found["code"].label == "Country Phone Code"
        source = found["source"]
        assert source.kind == "combobox"
        assert "how did you hear" in source.label.casefold()
        assert source.label.casefold() != "search"
        assert source.required is True
        assert found["q"].input_type == "search"
        kept = application_controls(extract_page(page))
        assert any(item.element_id == "source" for item in kept)
        assert all(item.element_id != "q" for item in kept)
    finally:
        page.close()


def test_resolver_fills_national_phone_and_source_and_does_not_guess_yes_no(browser, monkeypatch):
    from autofill.stepfill import fill_resolved_page

    _resolve(monkeypatch, source="Job Board", former="No")
    page = browser.new_page()
    try:
        page.set_content(_PAGE)
        outcome = fill_resolved_page(
            page,
            _binding(),
            session_id="session-a",
            ats_name="workday",
            step="my information",
        )
        assert page.locator("#national").input_value() == NATIONAL
        assert page.locator("#full").input_value() == EXAMPLE_PHONE
        assert page.locator("#code").input_value() == "US"
        assert "Job Board" in page.locator("#picked").inner_text()
        assert page.locator("input[name=prev][value=no]").is_checked()
        assert page.locator("input[name=prev][value=yes]").is_checked() is False
        texts = " ".join(str(item.get("text") or "") for item in outcome.manual_questions).casefold()
        assert "how did you hear" not in texts
        assert "former employee" not in texts
    finally:
        page.close()


def test_missing_source_and_former_employee_stay_manual(browser, monkeypatch):
    from autofill.stepfill import fill_resolved_page

    _resolve(monkeypatch)
    page = browser.new_page()
    try:
        page.set_content(_PAGE)
        outcome = fill_resolved_page(
            page,
            _binding(),
            session_id="session-a",
            ats_name="workday",
            step="my information",
        )
        assert page.locator("#picked").inner_text().strip() == ""
        assert page.locator("input[name=prev][value=yes]").is_checked() is False
        assert page.locator("input[name=prev][value=no]").is_checked() is False
        texts = [str(item.get("text") or "") for item in outcome.manual_questions]
        assert any("how did you hear" in text.casefold() for text in texts)
        former = [item for item in outcome.manual_questions if item.get("intent") == "PREVIOUSLY_EMPLOYED"]
        assert former
        assert all(item.get("blocking") for item in former)
        chosen = [item for item in outcome.fields if item.action == "select"]
        assert "yes" not in {str(item.detail or "").casefold() for item in chosen}
    finally:
        page.close()
