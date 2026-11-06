"""t315u direct-browser gaps. Fixtures only. No live employer sites.

FAIL tags:
- FAIL-SR-BASICS: SmartRecruiters first/last/email/confirm/city/phone/links
- FAIL-POPUP-KNOWN: known profile fields must not be asked
- FAIL-COMPANION: Hear About is not published to the Jobs-page panel
- FAIL-POPUP-DRAFT: a re-shown card keeps a partial answer
- FAIL-ASHBY-PAGE: full-page choices, no automatic Continue
- FAIL-WD-DATES: MM/YYYY stays, current employment is checked, location and role text
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from autofill import AutofillOptions, Status, autofill_application
from autofill.answer_popup import show_popup
from autofill.engine import Status as EngineStatus
from autofill.local_helper import LocalJob, _profile_basic_value
from autofill.profile import CandidateProfile

EMAIL = "jahnavi.torra@example.com"


def _person(**extra) -> CandidateProfile:
    body = {
        "candidateId": "cand-jahnavi",
        "personal": {
            "first_name": "Jahnavi",
            "last_name": "Torra",
            "full_name": "Jahnavi Torra",
            "email": EMAIL,
            "phone": "+1 612-555-0148",
            "city": "Burnsville",
            "linkedin_url": "https://www.linkedin.com/in/jahnavi-torra",
            "website_url": "https://jahnavi.example",
        },
    }
    body.update(extra)
    return CandidateProfile.from_dict(body)


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


def _run(page, url: str, profile: CandidateProfile):
    return autofill_application(
        url,
        profile,
        None,
        AutofillOptions(page=page, headless=True, max_pages=2, candidate_id=profile.candidate_id),
    )


def test_profile_accepts_live_and_upcoming_employment_keys():
    live = CandidateProfile.from_dict(
        {
            "candidateId": "cand-jahnavi",
            "personal": {"first_name": "Jahnavi", "last_name": "Torra", "email": EMAIL},
            "employment": [
                {
                    "company": "VEM Technologies pvt ltd",
                    "title": "engineer",
                    "start_month": "02",
                    "start_year": "2020",
                    "start_date": "2020-02-01",
                    "current": True,
                }
            ],
            "education": [
                {
                    "school": "State University",
                    "degree": "B.S.",
                    "startMonth": "08",
                    "startYear": "2016",
                    "to": "05/2020",
                }
            ],
        }
    )
    assert live.employment[0].start_date == "02/2020"
    assert live.employment[0].current is True
    assert live.employment[0].end_date == ""
    assert live.employment[0].location == ""
    assert live.employment[0].description == ""
    assert live.education_history[0].start_date == "08/2016"
    assert live.education_history[0].end_date == "05/2020"
    assert live.education_history[0].current is False

    upcoming = CandidateProfile.from_dict(
        {
            "candidateId": "cand-jahnavi",
            "personal": {"first_name": "Jahnavi", "last_name": "Torra", "email": EMAIL},
            "employment": [
                {
                    "company": "VEM Technologies pvt ltd",
                    "title": "engineer",
                    "from": "02/2020",
                    "location": "Hyderabad",
                    "city": "ignored when location is set",
                    "roleDescription": "Firmware bring-up",
                    "responsibilities": "not used when roleDescription is set",
                }
            ],
            "education": [
                {
                    "institution": "State University",
                    "degree": "B.S.",
                    "from": "08/2016",
                    "to": "05/2020",
                    "location": "Austin",
                    "description": "Power systems",
                    "currentlyAttending": False,
                }
            ],
        }
    )
    assert upcoming.employment[0].location == "Hyderabad"
    assert upcoming.employment[0].description == "Firmware bring-up"
    city_only = CandidateProfile.from_dict(
        {
            "candidateId": "cand-jahnavi",
            "personal": {"first_name": "Jahnavi", "last_name": "Torra", "email": EMAIL},
            "employment": [{"company": "VEM", "title": "engineer", "from": "02/2020", "city": "Hyderabad"}],
        }
    )
    assert city_only.employment[0].location == "Hyderabad"
    assert upcoming.education_history[0].school == "State University"
    assert upcoming.education_history[0].location == "Austin"
    assert upcoming.education_history[0].description == "Power systems"


def test_known_basics_are_not_popup_questions():
    profile = _person()
    prompts = [
        "First name",
        "Last name",
        "Email",
        "Confirm your email",
        "City",
        "Phone number",
        "LinkedIn",
        "Website",
    ]
    for prompt in prompts:
        assert _profile_basic_value(profile, {"text": prompt, "fieldKey": ""}), prompt
    assert _profile_basic_value(profile, {"text": "How Did You Hear About Us?"}) == ""


def test_companion_does_not_receive_hear_about_when_the_employer_popup_is_up():
    job = LocalJob("sess-hear", "https://jobs.example.com/apply", _person())
    job.result = {"status": EngineStatus.MANUAL_ANSWER_REQUIRED}
    job.state = "done"
    hear = {
        "text": "How Did You Hear About Us?",
        "questionId": "q-hear",
        "questionHash": "q-hear",
        "answerType": "single_select",
        "options": ["Indeed", "Internet", "LinkedIn"],
        "required": True,
        "blocking": True,
    }
    job.open_items = [hear]
    job.blocking = hear
    job.question = {
        "id": "q-hear",
        "prompt": "How Did You Hear About Us?",
        "type": "single_select",
        "options": ["Indeed", "Internet", "LinkedIn"],
    }
    job.popup = {"nonce": "same", "questionId": "q-hear", "questionIds": ["q-hear"], "state": "shown"}
    body = job.public()
    assert body["status"] == EngineStatus.MANUAL_ANSWER_REQUIRED
    assert body["question"]["id"] == "q-hear"
    assert body["question"]["popup"]["state"] == "shown"
    assert "prompt" not in body["question"]
    assert "options" not in body["question"]
    assert "questions" not in body

    job.popup = {"nonce": "same", "questionId": "q-hear", "questionIds": ["q-hear"], "state": "unavailable"}
    fallback = job.public()
    assert fallback["questions"][0]["prompt"] == "How Did You Hear About Us?"
    assert "Indeed" in fallback["questions"][0]["options"]


def test_popup_keeps_a_partial_answer_when_it_is_shown_again(browser):
    page = browser.new_page()
    try:
        page.set_content("<main><h1>Apply</h1></main>")
        question = {
            "id": "q-hear",
            "prompt": "How did you hear about us?",
            "type": "text",
            "options": [],
        }
        assert show_popup(page, question, nonce="nonce-1", questions=[question])
        page.evaluate(
            """() => {
              const area = document.getElementById("tilearc-answer-popup").shadowRoot.querySelector("textarea");
              area.value = "Campus fair";
            }"""
        )
        assert show_popup(page, question, nonce="nonce-1", questions=[question], force=True)
        kept = page.evaluate(
            """() => document.getElementById("tilearc-answer-popup").shadowRoot.querySelector("textarea").value"""
        )
        assert kept == "Campus fair"
    finally:
        page.close()


_SR_HTML = """
<main>
  <h1>Easy Apply</h1>
  <label for="first-host">First name*</label>
  <div id="first-host"></div>
  <label for="last-host">Last name*</label>
  <div id="last-host"></div>
  <label for="email">Email*</label>
  <input id="email" name="email" type="email" required>
  <label for="confirm">Confirm your email*</label>
  <input id="confirm" name="emailConfirmation" type="email" required>
  <label for="city">City*</label>
  <input id="city" name="city" role="combobox" placeholder="Search" required>
  <label for="phone">Phone number*</label>
  <select id="dial" aria-label="Country calling code" name="phoneCountry">
    <option value="1" selected>United States +1</option>
    <option value="44">United Kingdom +44</option>
  </select>
  <input id="phone" name="phoneNumber" type="tel" required>
  <h2>Your Profiles</h2>
  <label for="linkedin">LinkedIn</label>
  <input id="linkedin" name="linkedinUrl">
  <label for="website">Website</label>
  <input id="website" name="websiteUrl">
</main>
"""


def test_smartrecruiters_basics_are_filled_and_not_asked(browser):
    page = browser.new_page()
    try:
        page.set_content(_SR_HTML)
        page.evaluate(
            """() => {
              for (const id of ["first-host", "last-host"]) {
                const host = document.getElementById(id);
                const root = host.attachShadow({mode: "open"});
                const input = document.createElement("input");
                input.type = "text";
                input.required = true;
                root.appendChild(input);
              }
            }"""
        )
        result = _run(page, "https://jobs.smartrecruiters.com/oneclick-ui/company/Example/publication/1", _person())
        first = page.evaluate("() => document.getElementById('first-host').shadowRoot.querySelector('input').value")
        last = page.evaluate("() => document.getElementById('last-host').shadowRoot.querySelector('input').value")
        assert first == "Jahnavi"
        assert last == "Torra"
        assert page.locator("#email").input_value() == EMAIL
        assert page.locator("#confirm").input_value() == EMAIL
        assert page.locator("#city").input_value() == "Burnsville"
        phone = page.locator("#phone").input_value()
        assert "612" in phone and "0148" in phone
        assert "+1" not in phone
        assert "jahnavi-torra" in page.locator("#linkedin").input_value()
        assert "jahnavi.example" in page.locator("#website").input_value()
        asked = " ".join(str(item.get("text") or "") for item in result.open_items).casefold()
        for label in ("first name", "last name", "email", "confirm your email", "city", "phone", "linkedin", "website"):
            assert label not in asked
    finally:
        page.close()


_ASHBY_HTML = """
<main>
  <h1>Application</h1>
  <label>Email*<input id="email" type="email" required></label>
  <div class="ashby-application-form-field-entry">
    <label class="ashby-application-form-question-title">
      What type of employment are you seeking?<span class="ashby-application-form-question-required">*</span>
    </label>
    <label><input type="checkbox" name="emp" value="ft"> Full-Time, Regular</label>
    <label><input type="checkbox" name="emp" value="pt"> Part-Time, Regular</label>
    <label><input type="checkbox" name="emp" value="ftt"> Full-Time, Temporary</label>
    <label><input type="checkbox" name="emp" value="ptt"> Part-Time, Temporary</label>
  </div>
  <div class="ashby-application-form-field-entry">
    <label class="ashby-application-form-question-title">
      What work schedule are you seeking?<span>*</span>
    </label>
    <label><input type="radio" name="sched" value="onsite" required> On-Site</label>
    <label><input type="radio" name="sched" value="remote" required> Remote Only</label>
  </div>
  <div class="ashby-application-form-field-entry">
    <div class="ashby-application-form-question-title">Are you open to Per Diem / Business Travel?<span>*</span></div>
    <div class="ashby-application-form-input-yesno">
      <button type="button" id="travel-yes">Yes</button>
      <button type="button" id="travel-no">No</button>
    </div>
  </div>
  <div class="ashby-application-form-field-entry">
    <div class="ashby-application-form-question-title">Are you willing to relocate?<span>*</span></div>
    <label><input type="checkbox" name="move" value="nw"> Yes, to the Northwest US</label>
    <label><input type="checkbox" name="move" value="sw"> Yes, to the Southwest US</label>
    <label><input type="checkbox" name="move" value="mw"> Yes, to the Midwest US</label>
    <label><input type="checkbox" name="move" value="ne"> Yes, to the Northeast US</label>
    <label><input type="checkbox" name="move" value="se"> Yes, to the Southeast US</label>
    <label><input type="checkbox" name="move" value="no"> No, I am not willing to relocate</label>
  </div>
  <div style="height: 2400px"></div>
  <div class="ashby-application-form-field-entry">
    <div class="ashby-application-form-question-title">
      Are you open to or looking to live in any of the following cities (or surrounding areas)?<span>*</span>
    </div>
    <label><input type="checkbox" name="city" value="aiken"> Aiken, South Carolina</label>
    <label><input type="checkbox" name="city" value="austin"> Austin, Texas</label>
    <label><input type="checkbox" name="city" value="boise"> Boise, Idaho</label>
  </div>
  <button type="button" id="continue">Continue</button>
  <script>
    window.__continued = false;
    document.getElementById("continue").addEventListener("click", () => { window.__continued = true; });
    for (const id of ["travel-yes", "travel-no"]) {
      document.getElementById(id).addEventListener("click", () => {
        for (const other of ["travel-yes", "travel-no"]) {
          document.getElementById(other).setAttribute("aria-pressed", other === id ? "true" : "false");
        }
      });
    }
  </script>
</main>
"""


def test_ashby_scans_the_whole_page_and_does_not_continue(browser):
    page = browser.new_page()
    try:
        page.set_content(_ASHBY_HTML)
        profile = _person(
            applicationAnswers=[
                {"question": "What work schedule are you seeking?", "answer": "On-Site"},
                {"question": "Are you open to Per Diem / Business Travel?", "answer": "Yes"},
                {"question": "Are you willing to relocate?", "answer": "Yes, to the Southeast US"},
            ]
        )
        result = _run(page, "https://jobs.ashbyhq.com/gem-technologies/1/application", profile)
        assert page.locator("#email").input_value() == EMAIL
        assert page.locator("input[value='onsite']").is_checked()
        assert page.locator("input[value='remote']").is_checked() is False
        assert page.locator("#travel-yes").get_attribute("aria-pressed") == "true"
        assert page.locator("#travel-no").get_attribute("aria-pressed") == "false"
        assert page.locator("input[value='se']").is_checked()
        assert page.locator("input[value='nw']").is_checked() is False
        assert page.locator("input[value='ft']").is_checked() is False
        assert page.evaluate("() => window.__continued") is not True
        asked = " ".join(str(item.get("text") or "") for item in result.open_items).casefold()
        assert "employment" in asked
        assert "cities" in asked
        assert "per diem" not in asked
        assert "work schedule" not in asked
        options = []
        for item in result.open_items:
            options.extend(str(option) for option in item.get("options") or [])
        assert any("Aiken" in option for option in options)
        assert any("Boise" in option for option in options)
        assert result.status == Status.MANUAL_ANSWER_REQUIRED
    finally:
        page.close()


_WD_HTML = """
<main>
  <h1>My Experience</h1>
  <section>
    <h3>Work Experience 2</h3>
    <label>Job Title*<input id="title" data-automation-id="jobTitle" required></label>
    <label>Company*<input id="company" data-automation-id="company" required></label>
    <div data-automation-id="workExperience-1">
      <label for="loc">Location</label>
      <input id="loc" data-automation-id="location">
      <label><input id="current" type="checkbox"> I currently work here</label>
      <div data-automation-id="formField-startDate">
        <label for="from">From*</label>
        <input id="from" placeholder="MM/YYYY" aria-required="true">
        <button type="button" id="cal-from" aria-label="Calendar">calendar</button>
      </div>
      <div data-automation-id="formField-endDate">
        <label for="to">To*</label>
        <input id="to" placeholder="MM/YYYY" aria-required="true">
        <button type="button" id="cal-to" aria-label="Calendar">calendar</button>
      </div>
      <label for="role">Role Description</label>
      <textarea id="role"></textarea>
    </div>
  </section>
  <section>
    <h3>Education 1</h3>
    <label>School<input id="school"></label>
    <label><input id="attend" type="checkbox"> I currently attend</label>
    <div data-automation-id="education-1--startDate">
      <label for="edu-from">From</label>
      <input id="edu-from" placeholder="MM/YYYY">
    </div>
    <div data-automation-id="education-1--endDate">
      <label for="edu-to">To</label>
      <input id="edu-to" placeholder="MM/YYYY">
    </div>
  </section>
  <script>
    function lock(id, calId, monthName, shown) {
      const input = document.getElementById(id);
      let real = "";
      input._valueTracker = { setValue() { real = ""; } };
      Object.defineProperty(input, "value", {
        configurable: true,
        get() { return real; },
        set() {}
      });
      const cal = document.getElementById(calId);
      if (!cal) return;
      cal.addEventListener("click", () => {
        if (document.getElementById(id + "-month")) return;
        const month = document.createElement("button");
        month.type = "button";
        month.id = id + "-month";
        month.textContent = monthName;
        document.body.appendChild(month);
        month.addEventListener("click", () => { real = shown; });
      });
    }
    lock("from", "cal-from", "Feb", "02/2020");
    lock("to", "cal-to", "Nov", "11/2022");
  </script>
</main>
"""


def test_workday_current_job_keeps_from_checks_the_box_and_leaves_to_empty(browser):
    page = browser.new_page()
    try:
        page.set_content(_WD_HTML)
        profile = CandidateProfile.from_dict(
            {
                "candidateId": "cand-jahnavi",
                "personal": {"first_name": "Jahnavi", "last_name": "Torra", "email": EMAIL},
                "employment": [
                    {
                        "company": "VEM Technologies pvt ltd",
                        "title": "engineer",
                        "start_month": "02",
                        "start_year": "2020",
                        "start_date": "2020-02-01",
                        "current": True,
                        "location": "Hyderabad",
                        "role_description": "Firmware bring-up",
                    },
                ],
                "education": [
                    {
                        "school": "State University",
                        "degree": "B.S.",
                        "from": "08/2016",
                        "to": "05/2020",
                        "description": "Power systems",
                    }
                ],
            }
        )
        _run(page, "https://nidec.wd1.myworkdayjobs.com/en-US/Example/job/Role/apply", profile)
        assert page.locator("#title").input_value() == "engineer"
        assert "VEM" in page.locator("#company").input_value()
        assert page.locator("#from").input_value() == "02/2020"
        assert page.locator("#to").input_value() == ""
        assert page.locator("#current").is_checked()
        assert page.locator("#loc").input_value() == "Hyderabad"
        assert page.locator("#role").input_value() == "Firmware bring-up"
        assert page.locator("#edu-from").input_value() == "08/2016"
        assert page.locator("#edu-to").input_value() == "05/2020"
        assert page.locator("#attend").is_checked() is False
    finally:
        page.close()


def test_workday_past_job_fills_to_and_leaves_current_unchecked(browser):
    page = browser.new_page()
    try:
        page.set_content(_WD_HTML)
        profile = CandidateProfile.from_dict(
            {
                "candidateId": "cand-jahnavi",
                "personal": {"first_name": "Jahnavi", "last_name": "Torra", "email": EMAIL},
                "employment": [
                    {
                        "company": "VEM Technologies pvt ltd",
                        "title": "engineer",
                        "from": "02/2020",
                        "to": "11/2022",
                        "current": False,
                        "summary": "Board test",
                    }
                ],
            }
        )
        _run(page, "https://nidec.wd1.myworkdayjobs.com/en-US/Example/job/Role/apply", profile)
        assert page.locator("#from").input_value() == "02/2020"
        assert page.locator("#to").input_value() == "11/2022"
        assert page.locator("#current").is_checked() is False
        assert page.locator("#role").input_value() == "Board test"
        assert page.locator("#loc").input_value() == ""
    finally:
        page.close()
