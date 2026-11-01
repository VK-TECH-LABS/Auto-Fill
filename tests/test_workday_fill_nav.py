"""Workday-style phone boxes and search prompts. Not one employer.

A phone input beside a calling-code menu gets the national number. A prompt
whose box says Search is still the question on its label. A former-employee
Yes/No is filled only from an approved answer.
"""

import logging
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


_HEAR_ABOUT = (
    "GM Careers Website",
    "Internet Job Board / Job Search",
    "Referred by GM Employee",
)


def _my_information_page(*, source_id: str) -> str:
    """Country Phone Code already shows India. Hear About options are a separate prompt.

    The phone pill is a visible ``selectedItemList``. The source menu is
    ``promptOption`` rows that appear only after the source prompt icon is
    clicked. Clicking the search box does not open it. There is no ``<form>``,
    so a list walk can otherwise climb into the phone field.
    """
    source_id_attr = f' id="{source_id}"' if source_id else ""
    options = "".join(
        f'<div id="opt-{index}" data-automation-id="promptOption" style="height:24px">{label}</div>'
        for index, label in enumerate(_HEAR_ABOUT)
    )
    return f"""
    <div id="wrap">
      <h1>My Information</h1>
      <div data-automation-id="formField-countryPhoneCode">
        <label>Country Phone Code<abbr>*</abbr></label>
        <div data-automation-id="multiSelectContainer">
          <ul id="phone-selected" data-automation-id="selectedItemList" role="listbox">
            <li id="india" data-automation-id="selectedItem" role="option" style="height:24px">India (+91)</li>
          </ul>
          <input data-automation-id="searchBox" role="combobox" aria-haspopup="listbox"
            aria-label="Search" placeholder="Search" aria-required="true">
          <button type="button" id="phone-prompt" data-automation-id="promptIcon" aria-label="Search"
            style="width:24px;height:24px"></button>
        </div>
      </div>
      <div data-automation-id="formField-phoneNumber">
        <label for="national">Phone Number</label>
        <input id="national" type="tel" data-automation-id="phoneNumber" value="9876543210">
      </div>
      <div data-automation-id="formField-source">
        <label>How Did You Hear About Us?<abbr>*</abbr></label>
        <div data-automation-id="multiSelectContainer">
          <input{source_id_attr} data-automation-id="searchBox" role="combobox" aria-haspopup="listbox"
            aria-label="Search" placeholder="Search" aria-required="true">
          <button type="button" id="source-prompt" data-automation-id="promptIcon" aria-label="Search"
            style="width:24px;height:24px"></button>
          <span id="source-picked" data-automation-id="selectedItem" hidden></span>
        </div>
      </div>
      <button type="submit" id="submit">Submit</button>
    </div>
    <script>
      window.__submitted = false;
      window.__opens = [];
      document.getElementById("submit").addEventListener("click", (event) => {{
        event.preventDefault();
        window.__submitted = true;
      }});
      const phoneInput = document.querySelector(
        "[data-automation-id='formField-countryPhoneCode'] [data-automation-id='searchBox']"
      );
      const sourceInput = document.querySelector(
        "[data-automation-id='formField-source'] [data-automation-id='searchBox']"
      );
      phoneInput.addEventListener("click", () => window.__opens.push("phone-input"));
      document.getElementById("phone-prompt").addEventListener("click", () => {{
        window.__opens.push("phone-prompt");
      }});
      sourceInput.addEventListener("click", () => window.__opens.push("source-input"));
      document.getElementById("source-prompt").addEventListener("click", () => {{
        window.__opens.push("source-prompt");
        sourceInput.setAttribute("aria-expanded", "true");
        setTimeout(() => {{
          if (document.getElementById("source-menu")) return;
          const menu = document.createElement("div");
          menu.id = "source-menu";
          menu.innerHTML = {options!r};
          for (const item of menu.children) {{
            item.addEventListener("click", () => {{
              const picked = document.getElementById("source-picked");
              picked.hidden = false;
              picked.textContent = item.textContent;
              sourceInput.value = "";
              sourceInput.setAttribute("aria-expanded", "false");
              menu.remove();
            }});
          }}
          document.body.appendChild(menu);
        }}, 300);
      }});
    </script>
    """


def _hear_about_gap(page):
    from autofill.extract import extract_page
    from autofill.stepfill import required_gaps

    gaps = required_gaps(page, extract_page(page), None)
    heard = [item for item in gaps if "how did you hear" in str(item.get("text") or "").casefold()]
    assert len(heard) == 1
    return heard[0], gaps


@pytest.mark.parametrize("source_id", ["", "source"])
def test_hear_about_options_are_the_source_prompt_not_the_phone_code(browser, source_id):
    from autofill.dom_ui import commit_visible, fit_choice_values
    from autofill.extract import extract_page
    from autofill.safeguards import HUMAN_SUBMIT_ONLY

    assert HUMAN_SUBMIT_ONLY is True
    page = browser.new_page()
    try:
        page.set_content(_my_information_page(source_id=source_id))
        controls = extract_page(page).controls
        source = next(control for control in controls if "how did you hear" in control.label.casefold())
        phone = next(control for control in controls if "country phone code" in control.label.casefold())
        assert source.selector != phone.selector
        assert "formField-source" in source.selector or source.selector == "#source"
        assert "India (+91)" not in [option.label for option in source.options]
        heard, gaps = _hear_about_gap(page)
        prompts = [str(item.get("text") or "").casefold() for item in gaps]
        assert not any("country phone code" in prompt for prompt in prompts)
        assert heard["options"] == list(_HEAR_ABOUT)
        assert "India (+91)" not in heard["options"]
        assert "phone-prompt" not in page.evaluate("() => window.__opens")
        assert "source-prompt" in page.evaluate("() => window.__opens")
        reason, canonical = fit_choice_values(heard, ["GM Careers Website"])
        assert reason == "ok"
        assert canonical == ["GM Careers Website"]
        rejected, listed = fit_choice_values(heard, ["India (+91)"])
        assert rejected == "needs_option"
        assert listed == list(_HEAR_ABOUT)
        filled = commit_visible(
            page,
            {
                "selector": heard["selector"],
                "value": "GM Careers Website",
                "values": ["GM Careers Website"],
                "answerType": heard["answerType"],
            },
        )
        assert filled["filled"] is True
        assert "GM Careers Website" in page.locator("#source-picked").inner_text()
        assert page.locator("#india").inner_text().strip() == "India (+91)"
        assert page.evaluate("() => window.__submitted") is False
        assert "phone-prompt" not in page.evaluate("() => window.__opens")
    finally:
        page.close()


_NESTED_HEAR = """
<h1>My Information</h1>
<div data-automation-id="formField-source">
  <label for="source">How Did You Hear About Us?<abbr>*</abbr></label>
  <div data-automation-id="multiSelectContainer">
    <ul data-automation-id="selectedItemList">
      <li id="source-picked" data-automation-id="selectedItem" hidden></li>
    </ul>
    <div data-automation-id="multiselectInputContainer">
      <input id="source" data-automation-id="searchBox" role="combobox" aria-haspopup="listbox"
        aria-label="Search" placeholder="Search" aria-required="true" aria-invalid="true">
    </div>
    <button type="button" id="source-prompt" data-automation-id="promptIcon" aria-label="Search"
      style="width:24px;height:24px"></button>
  </div>
</div>
<button type="submit" id="submit">Submit</button>
<script>
  window.__submitted = false;
  window.__opens = [];
  document.getElementById("submit").addEventListener("click", (event) => {
    event.preventDefault();
    window.__submitted = true;
  });
  const input = document.getElementById("source");
  document.getElementById("source-prompt").addEventListener("click", () => {
    window.__opens.push("source-prompt");
    input.setAttribute("aria-expanded", "true");
    if (document.getElementById("source-menu")) return;
    const menu = document.createElement("div");
    menu.id = "source-menu";
    const category = document.createElement("div");
    category.id = "hear-category";
    category.setAttribute("data-automation-id", "promptOption");
    category.style.height = "24px";
    category.textContent = "GM Careers Website";
    category.addEventListener("click", () => {
      category.remove();
      const leaf = document.createElement("div");
      leaf.id = "hear-leaf";
      leaf.setAttribute("data-automation-id", "promptLeafNode");
      leaf.style.height = "24px";
      leaf.textContent = "GM Careers Website";
      leaf.addEventListener("click", () => {
        const picked = document.getElementById("source-picked");
        picked.hidden = false;
        picked.textContent = "GM Careers Website";
        input.value = "";
        input.setAttribute("aria-expanded", "false");
        menu.remove();
      });
      menu.appendChild(leaf);
    });
    menu.appendChild(category);
    document.body.appendChild(menu);
  });
</script>
"""


def test_hear_about_commits_the_nested_leaf_and_keeps_a_selected_pill(browser):
    """The category and the leaf share a name. A pill already showing that name is committed."""
    from autofill.dom_ui import commit_visible, control_committed
    from autofill.safeguards import HUMAN_SUBMIT_ONLY

    assert HUMAN_SUBMIT_ONLY is True
    page = browser.new_page()
    try:
        page.set_content(_NESTED_HEAR)
        target = {
            "selector": "#source",
            "value": "GM Careers Website",
            "values": ["GM Careers Website"],
            "answerType": "single_select",
        }
        filled = commit_visible(page, target)
        assert filled["filled"] is True, filled
        assert page.locator("#source-picked").inner_text().strip() == "GM Careers Website"
        assert page.locator("#hear-leaf").count() == 0
        assert control_committed(page, "#source") is True
        assert page.evaluate("() => window.__submitted") is False
        opens = page.evaluate("() => window.__opens.slice()")
        again = commit_visible(page, target)
        assert again["filled"] is True, again
        assert page.evaluate("() => window.__opens.slice()") == opens
        assert page.locator("#source-picked").inner_text().strip() == "GM Careers Website"
    finally:
        page.close()


def test_selected_pill_outside_the_search_box_is_committed(browser):
    """The pill is a sibling of the search wrapper. It still counts, even when aria-invalid."""
    from autofill.dom_ui import commit_visible, control_committed
    from autofill.safeguards import HUMAN_SUBMIT_ONLY

    assert HUMAN_SUBMIT_ONLY is True
    page = browser.new_page()
    try:
        page.set_content(_NESTED_HEAR)
        page.evaluate(
            """() => {
              const picked = document.getElementById("source-picked");
              picked.hidden = false;
              picked.textContent = "GM Careers Website";
            }"""
        )
        assert control_committed(page, "#source") is True
        filled = commit_visible(
            page,
            {
                "selector": "#source",
                "value": "GM Careers Website",
                "values": ["GM Careers Website"],
                "answerType": "single_select",
            },
        )
        assert filled["filled"] is True, filled
        assert page.evaluate("() => window.__opens") == []
        assert page.evaluate("() => window.__submitted") is False
    finally:
        page.close()


_MY_INFO_CONTACT = """
<h1>My Information</h1>
<div id="phone-row">
  <div data-automation-id="formField-countryPhoneCode">
    <label for="code">Country Phone Code<abbr>*</abbr></label>
    <div data-automation-id="multiSelectContainer">
      <ul data-automation-id="selectedItemList">
        <li id="phone-picked" data-automation-id="selectedItem">India (+91)</li>
      </ul>
      <div data-automation-id="multiselectInputContainer">
        <input id="code" data-automation-id="searchBox" role="combobox" aria-haspopup="listbox"
          aria-label="Search" placeholder="Search" aria-required="true">
      </div>
      <button type="button" id="code-prompt" data-automation-id="promptIcon" aria-label="Search"
        style="width:24px;height:24px"></button>
    </div>
  </div>
  <label for="national">Phone Number<abbr>*</abbr></label>
  <input id="national" type="tel" data-automation-id="phoneNumber" required>
  <label for="ext">Phone Extension</label>
  <input id="ext" type="text">
</div>
<label for="line1">Address Line 1<abbr>*</abbr></label>
<input id="line1" required>
<label for="line2">Address Line 2</label>
<input id="line2">
<label for="state">State<abbr>*</abbr></label>
<select id="state" required>
  <option value="">Select</option>
  <option value="NY">New York</option>
  <option value="WA">Washington</option>
</select>
<label for="postal">Postal Code<abbr>*</abbr></label>
<input id="postal" required>
<button type="submit" id="submit">Submit</button>
<script>
  window.__submitted = false;
  document.getElementById("submit").addEventListener("click", (event) => {
    event.preventDefault();
    window.__submitted = true;
  });
  const input = document.getElementById("code");
  const rows = ["India (+91)", "Canada (+1)", "United States of America (+1)"];
  document.getElementById("code-prompt").addEventListener("click", () => {
    input.setAttribute("aria-expanded", "true");
    if (document.getElementById("code-menu")) return;
    const menu = document.createElement("div");
    menu.id = "code-menu";
    rows.forEach((label, index) => {
      const item = document.createElement("div");
      item.id = "country-" + index;
      item.setAttribute("data-automation-id", "promptLeafNode");
      item.style.height = "24px";
      item.textContent = label;
      item.addEventListener("click", () => {
        const picked = document.getElementById("phone-picked");
        picked.hidden = false;
        picked.textContent = label;
        input.value = "";
        input.setAttribute("aria-expanded", "false");
        menu.remove();
      });
      menu.appendChild(item);
    });
    document.body.appendChild(menu);
  });
</script>
"""


def test_phone_country_extension_and_address_line_2(browser):
    """+1 / US replaces India. Extension and address line 2 stay empty.

    Profile state "washington" selects Washington. Postal 10001 is left as
    given; this engine does not invent New York from the ZIP.
    """
    from autofill.fill import fill_one_page
    from autofill.safeguards import HUMAN_SUBMIT_ONLY

    assert HUMAN_SUBMIT_ONLY is True
    person = _person(
        "+1 (917) 555-0100",
        address={
            "line1": "100 Broadway",
            "city": "New York",
            "state": "washington",
            "postalCode": "10001",
            "country": "US",
        },
    )
    page = browser.new_page()
    try:
        page.set_content(_MY_INFO_CONTACT)
        fill_one_page(
            page,
            person,
            cover_letter_path=None,
            cover_letter_text=None,
            job=None,
            resume_uploaded=False,
        )
        assert page.locator("#phone-picked").inner_text().strip() == "United States of America (+1)"
        phone = page.locator("#national").input_value()
        assert phone == "9175550100"
        assert not phone.startswith("0")
        assert page.locator("#ext").input_value() == ""
        assert page.locator("#line1").input_value() == "100 Broadway"
        assert page.locator("#line2").input_value() == ""
        assert page.locator("#state").input_value() == "WA"
        assert page.locator("#postal").input_value() == "10001"
        assert page.evaluate("() => window.__submitted") is False
    finally:
        page.close()


_SMS_ERROR = (
    "Error-General Motors will need to contact you throughout the recruitment process, "
    "this may include SMS/text messaging from GM and/or GM's third party partners "
    "(message and data rates may apply.) Do you agree to communications via text message/SMS?"
)

_APP_QUESTIONS = (
    ("eligible", "Are you currently eligible to work in the United States of America?"),
    ("sponsor", "Will you require future sponsorship to work in the United States?"),
    ("spouse", "Are you (or your spouse) a Current Government Employee?"),
    ("former", "Are you a Current or Former Government Employee?"),
    ("legal", "Are you aware of any legal restrictions on your ability to accept and perform this job, "
     "such as an agreement with your current or former employer?"),
)


def _app_question_page(*, sms: bool = False, submit: bool = True) -> str:
    """Workday Application Questions. Yes/No rows exist only after the button is clicked."""
    questions = list(_APP_QUESTIONS)
    if sms:
        questions.append(
            (
                "sms",
                "General Motors will need to contact you throughout the recruitment process, "
                "this may include SMS/text messaging from GM and/or GM's third party partners "
                "(message and data rates may apply.) Do you agree to communications via text message/SMS?",
            )
        )
    fields = []
    for element_id, question in questions:
        fields.append(
            f'<div data-automation-id="formField-{element_id}">'
            f'<label id="{element_id}-q">{question}<abbr>*</abbr></label>'
            f'<button id="{element_id}" type="button" aria-haspopup="listbox" '
            f'aria-labelledby="{element_id}-q" aria-required="true">Select One</button>'
            f"</div>"
        )
    body = "\n".join(fields)
    return f"""
    <h1>Application Questions</h1>
    {body}
    <button type="button" id="error-link">{_SMS_ERROR}</button>
    <button type="button" id="save">Save and Continue</button>
    {"<button type='submit' id='final'>Submit application</button>" if submit else ""}
    <script>
      window.__next = 0;
      window.__submitted = 0;
      window.__opens = 0;
      function arm(id) {{
        const button = document.getElementById(id);
        button.addEventListener("click", () => {{
          window.__opens += 1;
          document.querySelectorAll("[data-autofill-menu]").forEach((node) => node.remove());
          const list = document.createElement("ul");
          list.setAttribute("role", "listbox");
          list.setAttribute("data-autofill-menu", "1");
          list.style.cssText = "display:block;min-height:48px";
          for (const label of ["Select One", "Yes", "No"]) {{
            const item = document.createElement("li");
            item.setAttribute("role", "option");
            item.style.height = "24px";
            item.textContent = label;
            item.addEventListener("click", (event) => {{
              event.stopPropagation();
              button.textContent = label;
              list.remove();
            }});
            list.appendChild(item);
          }}
          document.body.appendChild(list);
        }});
      }}
      for (const id of [{", ".join(repr(item[0]) for item in questions)}]) arm(id);
      document.addEventListener("keydown", (event) => {{
        if (event.key === "Escape") {{
          document.querySelectorAll("[data-autofill-menu]").forEach((node) => node.remove());
        }}
      }});
      document.getElementById("save").addEventListener("click", () => {{ window.__next += 1; }});
      const finalButton = document.getElementById("final");
      if (finalButton) {{
        finalButton.addEventListener("click", (event) => {{
          event.preventDefault();
          window.__submitted += 1;
        }});
      }}
    </script>
    """


def test_closed_listbox_without_options_is_a_select_not_a_typed_value():
    person = _person(
        work_authorization={"legally_authorized_to_work": "Yes"},
        applicationAnswers=[
            {
                "question": (
                    "Will you now or in the future require sponsorship for employment visa status "
                    "(e.g., H-1B visa status)?"
                ),
                "answer": "Yes",
                "intent": "SPONSORSHIP_FUTURE",
            }
        ],
    )
    closed = _control(
        "Will you require future sponsorship to work in the United States?",
        kind="buttons",
        role="listbox",
        input_type="button",
        selector="#sponsor",
    )
    mapped = map_field(closed, person)
    assert mapped.action == "select"
    assert mapped.option_label == "Yes"
    spouse = _control(
        "Are you (or your spouse) a Current Government Employee?",
        kind="buttons",
        role="listbox",
        input_type="button",
        selector="#spouse",
        options=[Option("Yes", "Yes", "#y"), Option("No", "No", "#n")],
    )
    guessed = map_field(spouse, person)
    assert guessed.action == "unanswered"
    assert (guessed.option_label or guessed.text or "") not in {"Yes", "No"}
    answered = _person(
        applicationAnswers=[
            {
                "question": "Is your spouse or partner employed by a government agency?",
                "answer": "No",
                "key": "spouse_gov",
            }
        ]
    )
    chosen = map_field(spouse, answered)
    assert chosen.action == "select"
    assert chosen.option_label == "No"


def test_validation_error_text_is_not_a_submit_control():
    from autofill.safeguards import HUMAN_SUBMIT_ONLY, classify_control, field_class_for_button

    assert HUMAN_SUBMIT_ONLY is True
    assert classify_control(_SMS_ERROR) == "other"
    assert field_class_for_button(_SMS_ERROR) != "FINAL_SUBMIT"
    assert classify_control("Apply") == "submit"
    assert classify_control("Apply now") == "submit"
    assert classify_control("Submit application") == "submit"
    assert classify_control("Save and Continue") == "continue"


def test_workday_app_questions_open_the_menu_and_do_not_guess(browser):
    """Eligible and sponsorship are chosen. Government and legal stay blank."""
    from autofill.engine import AutofillOptions, Status, autofill_application
    from autofill.safeguards import HUMAN_SUBMIT_ONLY

    assert HUMAN_SUBMIT_ONLY is True
    person = _person(
        work_authorization={"legally_authorized_to_work": "Yes"},
        applicationAnswers=[
            {
                "question": (
                    "Will you now or in the future require sponsorship for employment visa status "
                    "(e.g., H-1B visa status)?"
                ),
                "answer": "Yes",
                "intent": "SPONSORSHIP_FUTURE",
            }
        ],
    )
    person.candidate_id = "cand-appq"
    page = browser.new_page()
    try:
        page.set_content(_app_question_page())
        result = autofill_application(
            "https://generalmotors.wd5.myworkdayjobs.com/en-US/Careers_GM/job/example/apply",
            person,
            None,
            AutofillOptions(page=page, session_id="appq", candidate_id="cand-appq", max_pages=2),
        )
        assert page.locator("#eligible").inner_text().strip() == "Yes"
        assert page.locator("#sponsor").inner_text().strip() == "Yes"
        assert page.locator("#spouse").inner_text().strip() == "Select One"
        assert page.locator("#former").inner_text().strip() == "Select One"
        assert page.locator("#legal").inner_text().strip() == "Select One"
        assert result.fields_filled >= 2
        assert result.status == Status.MANUAL_ANSWER_REQUIRED
        assert result.status != Status.READY_FOR_HUMAN_SUBMIT
        assert result.status != Status.NAVIGATION_STALLED
        prompts = " ".join(str(item.get("text") or "") for item in result.manual_questions).casefold()
        assert "government employee" in prompts
        assert "legal restrictions" in prompts
        assert _SMS_ERROR not in result.submit_controls
        assert "Submit application" in result.submit_controls
        assert page.evaluate("() => window.__next") == 0
        assert page.evaluate("() => window.__submitted") == 0
        assert page.evaluate("() => window.__opens") > 0
    finally:
        page.close()


def test_saved_app_question_answers_are_selected_including_sms(browser):
    """Profile rows select eligible, sponsorship, both government facts, legal, and SMS."""
    from autofill.engine import AutofillOptions, Status, autofill_application
    from autofill.safeguards import HUMAN_SUBMIT_ONLY

    assert HUMAN_SUBMIT_ONLY is True
    person = _person(
        work_authorization={"legally_authorized_to_work": "Yes", "require_sponsorship": "Yes"},
        applicationAnswers=[
            {
                "question": (
                    "Will you now or in the future require sponsorship for employment visa status "
                    "(e.g., H-1B visa status)?"
                ),
                "answer": "Yes",
                "intent": "SPONSORSHIP_FUTURE",
            },
            {
                "question": "Have you ever been employed by a government agency?",
                "answer": "No",
                "key": "current_former_gov",
            },
            {
                "question": "Is your spouse or partner employed by a government agency?",
                "answer": "No",
                "key": "spouse_gov",
            },
            {
                "question": "Do you have any legal restrictions on employment?",
                "answer": "No",
                "key": "legal_restrictions",
            },
            {
                "question": "Do you agree to communications via text message/SMS for the recruitment process?",
                "answer": "Yes",
                "intent": "RECRUITMENT_SMS_CONSENT",
                "key": "recruitment_sms_consent",
            },
        ],
    )
    person.candidate_id = "2"
    page = browser.new_page()
    try:
        page.set_content(_app_question_page(sms=True, submit=False))
        result = autofill_application(
            "https://generalmotors.wd5.myworkdayjobs.com/en-US/Careers_GM/job/example/apply",
            person,
            None,
            AutofillOptions(page=page, session_id="appq-sms", candidate_id="2", max_pages=1),
        )
        assert page.locator("#eligible").inner_text().strip() == "Yes"
        assert page.locator("#sponsor").inner_text().strip() == "Yes"
        assert page.locator("#spouse").inner_text().strip() == "No"
        assert page.locator("#former").inner_text().strip() == "No"
        assert page.locator("#legal").inner_text().strip() == "No"
        assert page.locator("#sms").inner_text().strip() == "Yes"
        categories = [row["category"] for row in result.auto_consents]
        assert "RECRUITMENT_SMS_CONSENT" in categories
        assert result.status != Status.READY_FOR_HUMAN_SUBMIT
        assert _SMS_ERROR not in result.submit_controls
        assert page.evaluate("() => window.__submitted") == 0
    finally:
        page.close()


def _appq_person() -> CandidateProfile:
    person = _person(
        work_authorization={"legally_authorized_to_work": "Yes", "require_sponsorship": "Yes"},
        applicationAnswers=[
            {
                "question": (
                    "Will you now or in the future require sponsorship for employment visa status "
                    "(e.g., H-1B visa status)?"
                ),
                "answer": "Yes",
                "intent": "SPONSORSHIP_FUTURE",
            },
            {
                "question": "Have you ever been employed by a government agency?",
                "answer": "No",
                "key": "current_former_gov",
            },
            {
                "question": "Is your spouse or partner employed by a government agency?",
                "answer": "No",
                "key": "spouse_gov",
            },
            {
                "question": "Do you have any legal restrictions on employment?",
                "answer": "No",
                "key": "legal_restrictions",
            },
            {
                "question": "Do you agree to communications via text message/SMS for the recruitment process?",
                "answer": "Yes",
                "intent": "RECRUITMENT_SMS_CONSENT",
                "key": "recruitment_sms_consent",
            },
        ],
    )
    person.candidate_id = "2"
    return person


def _live_app_questions(*, mode: str = "ok", errors: bool = False, legal_value: str = "Select One") -> str:
    """GM Application Questions.

    ``pointer`` makes a trusted click commit Yes without opening the menu.
    ``flaky`` commits Yes on the first legal option click and the real label after that.
    ``stuck`` always commits Yes for legal. A clear control puts Select One back.
    ``errors`` paints Workday's required-field banners, including the SMS sentence.
    """
    questions = list(_APP_QUESTIONS) + [
        (
            "sms",
            "General Motors will need to contact you throughout the recruitment process, "
            "this may include SMS/text messaging from GM and/or GM's third party partners "
            "(message and data rates may apply.) Do you agree to communications via text message/SMS?",
        )
    ]
    fields = []
    for element_id, question in questions:
        initial = legal_value if element_id == "legal" else "Select One"
        labelled = f"{element_id}-q"
        if errors:
            labelled = f"{element_id}-q page-error"
        follow = ""
        clear = ""
        if element_id == "legal":
            hidden = "" if initial == "Yes" else " hidden"
            follow = (
                f'<label id="legal-detail-q">Please describe the nature of the restrictions. '
                f"Example - I have a non-compete agreement with my employer or previous employer.</label>"
                f'<textarea id="legal-detail" aria-labelledby="legal-detail-q"{hidden}></textarea>'
            )
            clear = (
                '<button id="legal-clear" type="button" data-automation-id="DELETE_charm" '
                'aria-label="Remove">Remove</button>'
            )
        error = ""
        if errors:
            error = (
                f'<div id="{element_id}-err" data-automation-id="errorMessage" role="alert">'
                f'<button type="button" aria-required="true">Error: The field {question} '
                f"is required and must have a value.</button></div>"
            )
        fields.append(
            f'<div data-automation-id="formField-{element_id}">'
            f'<label id="{element_id}-q">{question}<abbr>*</abbr></label>'
            f'<button id="{element_id}" type="button" aria-haspopup="listbox" '
            f'aria-labelledby="{labelled}" aria-required="true" aria-invalid="true">{initial}</button>'
            f"{clear}{follow}{error}</div>"
        )
    page_error = ""
    if errors:
        page_error = (
            '<div id="page-error" data-automation-id="errorMessage" role="alert">'
            '<button type="button" aria-required="true">'
            f"{_SMS_ERROR}"
            "</button></div>"
        )
    body = "\n".join(fields)
    ids = ", ".join(repr(item[0]) for item in questions)
    detail_hidden = "false" if legal_value == "Yes" else "true"
    return f"""
    <h1>Application Questions</h1>
    {page_error}
    {body}
    <button type="button" id="save">Save and Continue</button>
    <button type="submit" id="final">Submit application</button>
    <script>
      window.__opens = 0;
      window.__submitted = 0;
      window.__clears = 0;
      window.__legalClicks = 0;
      const mode = {mode!r};
      function syncDetail() {{
        const button = document.getElementById("legal");
        const detail = document.getElementById("legal-detail");
        const yes = (button.textContent || "").trim() === "Yes";
        detail.hidden = !yes;
        if (yes) detail.setAttribute("required", "true");
        else detail.removeAttribute("required");
      }}
      function arm(id) {{
        const button = document.getElementById(id);
        button.addEventListener("click", (event) => {{
          if (mode === "pointer" && event.isTrusted) {{
            button.textContent = "Yes";
            if (id === "legal") syncDetail();
            return;
          }}
          window.__opens += 1;
          document.querySelectorAll("[data-autofill-menu]").forEach((node) => node.remove());
          const list = document.createElement("ul");
          list.setAttribute("role", "listbox");
          list.setAttribute("data-autofill-menu", "1");
          list.style.cssText = "display:block;min-height:48px";
          for (const label of ["Yes", "No"]) {{
            const item = document.createElement("li");
            item.setAttribute("role", "option");
            item.style.height = "24px";
            item.textContent = label;
            item.addEventListener("click", (optionEvent) => {{
              optionEvent.stopPropagation();
              let shown = label;
              if (id === "legal") {{
                window.__legalClicks += 1;
                if (mode === "stuck" || (mode === "flaky" && window.__legalClicks === 1)) shown = "Yes";
              }}
              button.textContent = shown;
              list.remove();
              if (id === "legal") syncDetail();
            }});
            list.appendChild(item);
          }}
          document.body.appendChild(list);
        }});
      }}
      for (const id of [{ids}]) arm(id);
      document.getElementById("legal-clear").addEventListener("click", () => {{
        window.__clears += 1;
        document.getElementById("legal").textContent = "Select One";
        syncDetail();
      }});
      document.addEventListener("keydown", (event) => {{
        if (event.key === "Escape") {{
          document.querySelectorAll("[data-autofill-menu]").forEach((node) => node.remove());
        }}
      }});
      document.getElementById("save").addEventListener("click", () => {{ window.__next = (window.__next || 0) + 1; }});
      document.getElementById("final").addEventListener("click", (event) => {{
        event.preventDefault();
        window.__submitted += 1;
      }});
      document.getElementById("legal-detail").hidden = {detail_hidden};
      if ({detail_hidden}) document.getElementById("legal-detail").removeAttribute("required");
      else document.getElementById("legal-detail").setAttribute("required", "true");
    </script>
    """


def _fill_appq(page, session_id: str):
    from autofill.engine import AutofillOptions, autofill_application

    return autofill_application(
        "https://generalmotors.wd5.myworkdayjobs.com/en-US/Careers_GM/job/example/apply",
        _appq_person(),
        None,
        AutofillOptions(page=page, session_id=session_id, candidate_id="2", max_pages=1),
    )


def _legal_choice(result) -> dict:
    rows = [item for item in result.listbox_choices if "legal restriction" in item.get("label", "").casefold()]
    assert rows, result.listbox_choices
    return rows[-1]


def test_legal_listbox_verifies_no_when_a_pointer_click_would_hit_yes(browser, caplog):
    """A trusted click commits Yes. The fill must still leave the approved No."""
    from autofill.safeguards import HUMAN_SUBMIT_ONLY

    assert HUMAN_SUBMIT_ONLY is True
    caplog.set_level(logging.INFO, logger="autofill.combobox")
    page = browser.new_page()
    try:
        page.set_content(_live_app_questions(mode="pointer"))
        result = _fill_appq(page, "appq-pointer")
        assert page.locator("#eligible").inner_text().strip() == "Yes"
        assert page.locator("#sponsor").inner_text().strip() == "Yes"
        assert page.locator("#spouse").inner_text().strip() == "No"
        assert page.locator("#former").inner_text().strip() == "No"
        assert page.locator("#legal").inner_text().strip() == "No"
        assert page.locator("#sms").inner_text().strip() == "Yes"
        assert page.locator("#legal-detail").is_hidden()
        prompts = " ".join(str(item.get("text") or "") for item in result.manual_questions).casefold()
        assert "describe the nature" not in prompts
        choice = _legal_choice(result)
        assert choice["intended"] == "No"
        assert choice["clicked"] == "No"
        assert choice["shown"] == "No"
        assert any(
            "intended=No" in record.message and "clicked=No" in record.message and "shown=No" in record.message
            for record in caplog.records
        )
        assert page.evaluate("() => window.__clears") == 0
        assert page.evaluate("() => window.__opens") > 0
        assert page.evaluate("() => window.__submitted") == 0
        assert result.stopped_before_submit is True
    finally:
        page.close()


def test_legal_listbox_corrects_a_first_click_that_commits_yes(browser):
    """The first legal click lands on Yes. The button is read again and set to No."""
    page = browser.new_page()
    try:
        page.set_content(_live_app_questions(mode="flaky"))
        result = _fill_appq(page, "appq-flaky")
        assert page.locator("#legal").inner_text().strip() == "No"
        assert page.locator("#legal-detail").is_hidden()
        assert page.evaluate("() => window.__legalClicks") >= 2
        choice = _legal_choice(result)
        assert choice["intended"] == "No"
        assert choice["shown"] == "No"
        assert page.locator("#sms").inner_text().strip() == "Yes"
        assert page.evaluate("() => window.__submitted") == 0
    finally:
        page.close()


def test_wrong_legal_yes_is_overwritten_and_a_stuck_yes_is_cleared(browser):
    """A rerun replaces a committed Yes. A click that cannot leave No is cleared."""
    page = browser.new_page()
    try:
        page.set_content(_live_app_questions(legal_value="Yes"))
        result = _fill_appq(page, "appq-overwrite")
        assert page.locator("#legal").inner_text().strip() == "No"
        assert page.locator("#legal-detail").is_hidden()
        assert _legal_choice(result)["intended"] == "No"
        assert page.evaluate("() => window.__clears") == 0
        assert page.evaluate("() => window.__submitted") == 0
    finally:
        page.close()
    page = browser.new_page()
    try:
        page.set_content(_live_app_questions(mode="stuck"))
        result = _fill_appq(page, "appq-stuck")
        assert page.locator("#legal").inner_text().strip() != "Yes"
        assert page.locator("#legal-detail").is_hidden()
        prompts = " ".join(str(item.get("text") or "") for item in result.manual_questions).casefold()
        assert "describe the nature" not in prompts
        assert "legal restriction" in prompts
        choice = _legal_choice(result)
        assert choice["intended"] == "No"
        assert choice["shown"] != "Yes"
        assert page.evaluate("() => window.__clears") >= 1
        assert page.locator("#sms").inner_text().strip() == "Yes"
        assert page.evaluate("() => window.__submitted") == 0
    finally:
        page.close()


def test_required_errors_do_not_blank_app_questions_or_skip_sms(browser):
    """Red required errors stay on the page. Approved Yes/No values are still chosen."""
    page = browser.new_page()
    try:
        page.set_content(_live_app_questions(errors=True))
        result = _fill_appq(page, "appq-errors")
        assert page.locator("#eligible").inner_text().strip() == "Yes"
        assert page.locator("#sponsor").inner_text().strip() == "Yes"
        assert page.locator("#spouse").inner_text().strip() == "No"
        assert page.locator("#former").inner_text().strip() == "No"
        assert page.locator("#legal").inner_text().strip() == "No"
        assert page.locator("#sms").inner_text().strip() == "Yes"
        assert page.locator("#legal-detail").is_hidden()
        manual = " ".join(result.manual_actions).casefold()
        assert "this statement stays manual" not in manual
        assert _legal_choice(result)["intended"] == "No"
        assert page.evaluate("() => window.__submitted") == 0
        assert result.stopped_before_submit is True
    finally:
        page.close()
