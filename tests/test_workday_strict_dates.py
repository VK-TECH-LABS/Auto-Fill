"""Workday date segments that only commit from real key presses.

Real Workday keeps each Month/Day/Year segment's value in its own state. That
state changes on key presses only, so a ``.value`` assignment shows digits but
leaves "The field From is required and must have a value." The readout
``current value is ...`` and the error are drawn from that state.
"""

from __future__ import annotations

import datetime

import pytest
from playwright.sync_api import sync_playwright
from tests.test_aaon_workday import _person

_STRICT = """
<main>
<h1>Workday</h1>
<div data-automation-id="formField-startDate" id="start-field">
  <label>From*</label>
  <input id="workExperience-1--startDate-dateSectionMonth-input" data-automation-id="dateSectionMonth-input"
    aria-label="Month" aria-required="true">
  <input id="workExperience-1--startDate-dateSectionYear-input" data-automation-id="dateSectionYear-input"
    aria-label="Year" aria-required="true">
  <div class="readout"></div><div class="err"></div>
</div>
<div data-automation-id="formField-endDate" id="end-field">
  <label>To*</label>
  <input id="workExperience-1--endDate-dateSectionMonth-input" data-automation-id="dateSectionMonth-input"
    aria-label="Month" aria-required="true">
  <input id="workExperience-1--endDate-dateSectionYear-input" data-automation-id="dateSectionYear-input"
    aria-label="Year" aria-required="true">
  <div class="readout"></div><div class="err"></div>
</div>
<div data-automation-id="formField-dateSignedOn" id="sign-field">
  <label>Date*</label>
  <input id="selfIdentifiedDisabilityData--dateSignedOn-dateSectionMonth-input"
    data-automation-id="dateSectionMonth-input" aria-label="Month" aria-required="true">
  <input id="selfIdentifiedDisabilityData--dateSignedOn-dateSectionDay-input"
    data-automation-id="dateSectionDay-input" aria-label="Day" aria-required="true">
  <input id="selfIdentifiedDisabilityData--dateSignedOn-dateSectionYear-input"
    data-automation-id="dateSectionYear-input" aria-label="Year" aria-required="true">
  <div class="readout"></div><div class="err"></div>
</div>
<script>
  for (const field of document.querySelectorAll("[data-automation-id^='formField']")) {
    const inputs = Array.from(field.querySelectorAll("input"));
    const state = new Map(inputs.map((input) => [input, ""]));
    const width = (input) => (/Year/.test(input.id) ? 4 : 2);
    function paint() {
      const values = inputs.map((input) => state.get(input));
      const holes = inputs.map((input) => (/Year/.test(input.id) ? "YYYY" : /Day/.test(input.id) ? "DD" : "MM"));
      const shown = values.map((value, i) => (value.length === width(inputs[i]) ? value : holes[i]));
      field.querySelector(".readout").textContent = values.some(Boolean) ? "current value is " + shown.join("/") : "";
      const done = values.every((value, i) => value.length === width(inputs[i]));
      field.querySelector(".err").innerHTML = done
        ? ""
        : '<span data-automation-id="errorMessage">Error: The field is required and must have a value.</span>';
    }
    for (const input of inputs) {
      input.addEventListener("keydown", (event) => {
        if (/^\\d$/.test(event.key)) {
          const next = (state.get(input) + event.key).slice(-width(input));
          state.set(input, next);
        } else if (event.key === "Backspace") {
          state.set(input, "");
        } else {
          return;
        }
        event.preventDefault();
        input.value = state.get(input);
        paint();
      });
    }
    paint();
  }
</script>
</main>
"""


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


def _profile():
    return _person(
        employment=[
            {
                "company": "VEM Technologies pvt ltd",
                "title": "engineer",
                "start_date": "07/2020",
                "end_date": "12/2025",
                "current": False,
            }
        ]
    )


def test_value_assignment_does_not_count_as_a_date(browser):
    from autofill.split_dates import split_pair_committed

    page = browser.new_page()
    try:
        page.set_content(_STRICT)
        page.evaluate(
            """() => {
              document.getElementById('workExperience-1--startDate-dateSectionMonth-input').value = '02';
              document.getElementById('workExperience-1--startDate-dateSectionYear-input').value = '2025';
            }"""
        )
        assert not split_pair_committed(page, "#workExperience-1--startDate-dateSectionMonth-input")
    finally:
        page.close()


def test_from_to_and_signature_date_commit_with_key_presses(browser):
    from autofill.fill import fill_one_page

    page = browser.new_page()
    try:
        page.set_content(_STRICT)
        fill_one_page(
            page,
            _profile(),
            cover_letter_path=None,
            cover_letter_text=None,
            job=None,
            resume_uploaded=False,
        )
        assert "current value is 07/2020" in page.locator("#start-field").inner_text()
        assert "current value is 12/2025" in page.locator("#end-field").inner_text()
        today = datetime.date.today()
        assert f"current value is {today:%m/%d/%Y}" in page.locator("#sign-field").inner_text()
        assert page.locator("[data-automation-id='errorMessage']").count() == 0
    finally:
        page.close()


def test_signature_date_is_retyped_as_today(browser):
    from autofill.fill import fill_one_page

    page = browser.new_page()
    try:
        page.set_content(_STRICT)
        for selector, digits in (
            ("#selfIdentifiedDisabilityData--dateSignedOn-dateSectionMonth-input", "01"),
            ("#selfIdentifiedDisabilityData--dateSignedOn-dateSectionDay-input", "02"),
            ("#selfIdentifiedDisabilityData--dateSignedOn-dateSectionYear-input", "2024"),
        ):
            page.locator(selector).click()
            page.keyboard.type(digits)
        fill_one_page(
            page,
            _profile(),
            cover_letter_path=None,
            cover_letter_text=None,
            job=None,
            resume_uploaded=False,
        )
        today = datetime.date.today()
        assert f"current value is {today:%m/%d/%Y}" in page.locator("#sign-field").inner_text()
    finally:
        page.close()


def test_card_answer_types_a_three_part_date(browser):
    from autofill.split_dates import commit_split_date_answer, is_split_date_answer

    page = browser.new_page()
    try:
        page.set_content(_STRICT)
        target = {
            "selector": "#selfIdentifiedDisabilityData--dateSignedOn-dateSectionMonth-input",
            "values": ["11/13/2026"],
        }
        assert is_split_date_answer(target)
        assert commit_split_date_answer(page, target) == {"filled": True, "verified": True}
        assert "current value is 11/13/2026" in page.locator("#sign-field").inner_text()
    finally:
        page.close()


def test_birth_date_is_not_a_signature_date():
    from autofill.models import Control
    from autofill.split_dates import is_signature_date

    parts = [
        Control(kind="text", selector=f"#dob-dateSection{name}-input", element_id=f"dob-dateSection{name}-input",
                label="Date of Birth")
        for name in ("Month", "Day", "Year")
    ]
    assert not is_signature_date(parts)


def test_stall_with_required_errors_names_the_fields(browser):
    from autofill.engine import Status, _stall_outcome

    page = browser.new_page()
    try:
        page.set_content(_STRICT)
        status, messages = _stall_outcome(page)
        assert status == Status.MANUAL_ANSWER_REQUIRED
        assert any("required" in item.casefold() for item in messages)
        page.set_content("<main><h1>Review</h1><button>Next</button></main>")
        assert _stall_outcome(page) == (Status.NAVIGATION_STALLED, ["navigation_stalled"])
    finally:
        page.close()
