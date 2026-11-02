"""AAON Workday defects: hierarchical prompts, mapping guards, and My Experience.

Final Submit stays human. These fixtures are local pages, not the live tenant.
"""

from datetime import UTC, datetime, timedelta

import pytest
from playwright.sync_api import sync_playwright

from autofill.models import ButtonControl, Control, PageSnapshot
from autofill.pagewait import classify_page, terminal_kind
from autofill.profile import CandidateProfile
from autofill.safeguards import HUMAN_SUBMIT_ONLY


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


def _person(**overrides) -> CandidateProfile:
    personal = {
        "full_name": "Jahnavi Torra",
        "email": "jahnavi@example.com",
        "phone": "9185550100",
        "address": "43-g, james street",
        "city": "Tulsa",
        "province_state": "Oklahoma",
        "postal_code": "74101",
        "country": "United States",
    }
    personal.update(overrides.pop("personal", {}))
    data = {
        "personal": personal,
        "employment": [{"company": "Other Co", "title": "Engineer"}],
    }
    data.update(overrides)
    return CandidateProfile.from_dict(data)


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


def test_middle_name_and_county_are_not_the_street_or_full_name():
    from autofill.mapping import map_field, previous_worker_value

    person = _person()
    middle = map_field(Control(kind="text", label="Middle Name", element_id="legalName--middleName"), person)
    assert middle.action == "skip"
    assert middle.text == ""
    assert middle.text != person.personal.full_name
    named = _person(personal={"middle_name": "Ann"})
    filled = map_field(Control(kind="text", label="Middle Name"), named)
    assert filled.action == "fill"
    assert filled.text == "Ann"
    county = map_field(
        Control(
            kind="text",
            label="County",
            element_id="address--regionSubdivision1",
            automation_id="formField-regionSubdivision1",
        ),
        person,
    )
    assert county.action == "skip"
    assert county.text == ""
    assert person.personal.address not in (county.text or "")
    device = map_field(
        Control(
            kind="buttons",
            role="listbox",
            label="Phone Device Type",
            element_id="phoneNumber--phoneType",
        ),
        person,
    )
    assert device.key != "phone"
    assert device.text != person.personal.phone
    assert previous_worker_value("Previously employed by AAON", ["Other Co"], "") == "No"
    assert previous_worker_value("Previously employed by AAON", ["AAON"], "") == "Yes"
    assert previous_worker_value("Are you a former employee of this company?", ["Other Co"], "") == ""
    from autofill.models import Option

    radio = map_field(
        Control(
            kind="radio",
            label="Previously employed by AAON",
            automation_id="formField-candidateIsPreviousWorker",
            options=[Option(value="Yes", label="Yes"), Option(value="No", label="No")],
        ),
        person,
    )
    assert radio.action == "select"
    assert radio.option_label == "No"


def test_empty_my_experience_with_save_and_continue_can_advance():
    assert HUMAN_SUBMIT_ONLY is True
    page = PageSnapshot(
        heading="My Experience",
        page_text="Resume Jahnavi.pdf",
        controls=[],
        buttons=[ButtonControl(name="Save and Continue", selector="#next")],
    )
    assert classify_page(page) == "APPLICATION_FORM"
    assert terminal_kind(page, gaps=[], required_total=0) == "FILLED"
    blank = PageSnapshot(heading="My Experience", controls=[], buttons=[], page_text="")
    assert classify_page(blank) != "APPLICATION_FORM"


_HIERARCHY = """
<h1>My Information</h1>
<div data-automation-id="formField-source">
  <label for="source">How Did You Hear About Us?<abbr>*</abbr></label>
  <div data-automation-id="multiSelectContainer">
    <input id="source" data-automation-id="searchBox" role="combobox" aria-haspopup="listbox"
      aria-label="Search" placeholder="Search">
    <button type="button" id="source-prompt" data-automation-id="promptIcon" aria-label="Search"
      style="width:24px;height:24px"></button>
    <span id="picked" data-automation-id="selectedItem" hidden></span>
  </div>
</div>
<button type="submit" id="submit">Submit</button>
<script>
  window.__submitted = false;
  document.getElementById("submit").addEventListener("click", (event) => {
    event.preventDefault();
    window.__submitted = true;
  });
  const input = document.getElementById("source");
  function closeMenu() {
    const menu = document.getElementById("source-menu");
    if (menu) menu.remove();
    input.setAttribute("aria-expanded", "false");
  }
  function commit(label) {
    const picked = document.getElementById("picked");
    picked.hidden = false;
    picked.textContent = label;
    input.value = "";
    closeMenu();
  }
  function leaf(label) {
    const row = document.createElement("div");
    row.id = "hear-leaf";
    row.setAttribute("data-automation-id", "promptLeafNode");
    row.style.height = "24px";
    row.textContent = label;
    row.addEventListener("click", () => commit(label));
    return row;
  }
  function openTop() {
    closeMenu();
    input.setAttribute("aria-expanded", "true");
    const menu = document.createElement("div");
    menu.id = "source-menu";
    const category = document.createElement("div");
    category.id = "hear-category";
    category.setAttribute("data-automation-id", "promptOption");
    category.setAttribute("data-autofill-folder", "true");
    category.style.height = "24px";
    category.appendChild(document.createTextNode("Career Websites"));
    const chevron = document.createElement("span");
    chevron.setAttribute("data-automation-id", "promptOptionChevron");
    chevron.className = "chevron";
    category.appendChild(chevron);
    category.addEventListener("click", () => {
      category.remove();
      menu.appendChild(leaf("Corporate Website"));
    });
    menu.appendChild(category);
    document.body.appendChild(menu);
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMenu();
  });
  document.getElementById("source-prompt").addEventListener("click", openTop);
  input.addEventListener("click", openTop);
</script>
"""


def test_hear_about_drills_into_the_category_and_accepts_a_leaf_path(browser):
    from autofill.combobox import list_combobox_options, select_combobox
    from autofill.dom_ui import commit_visible, fit_choice_values

    assert HUMAN_SUBMIT_ONLY is True
    page = browser.new_page()
    try:
        page.set_content(_HIERARCHY)
        labels = list_combobox_options(page, "#source")
        assert "Corporate Website" in labels
        assert "Career Websites > Corporate Website" in labels
        assert page.locator("#picked").inner_text().strip() == ""
        item = {
            "answerType": "single_select",
            "options": labels,
            "selector": "#source",
            "text": "How Did You Hear About Us?",
        }
        reason, canonical = fit_choice_values(item, ["Corporate Website"])
        assert reason == "ok"
        assert canonical == ["Corporate Website"]
        path_reason, path = fit_choice_values(item, ["Career Websites > Corporate Website"])
        assert path_reason == "ok"
        assert path == ["Career Websites > Corporate Website"]
        picked = select_combobox(page, "#source", "Career Websites > Corporate Website")
        assert picked is not None
        assert page.locator("#picked").inner_text().strip() == "Corporate Website"
        assert page.evaluate("() => window.__submitted") is False
    finally:
        page.close()
    leaf_page = browser.new_page()
    try:
        leaf_page.set_content(_HIERARCHY)
        filled = commit_visible(
            leaf_page,
            {
                "selector": "#source",
                "value": "Corporate Website",
                "values": ["Corporate Website"],
                "answerType": "single_select",
            },
        )
        assert filled["filled"] is True, filled
        assert leaf_page.locator("#picked").inner_text().strip() == "Corporate Website"
    finally:
        leaf_page.close()
    category_page = browser.new_page()
    try:
        category_page.set_content(_HIERARCHY)
        category = select_combobox(category_page, "#source", "Career Websites")
        assert category is not None
        assert category_page.locator("#picked").inner_text().strip() == "Corporate Website"
        assert category_page.evaluate("() => window.__submitted") is False
    finally:
        category_page.close()


_PHONE_TYPE = """
<h1>My Information</h1>
<label id="type-q">Phone Device Type<abbr>*</abbr></label>
<button id="phoneNumber--phoneType" type="button" aria-haspopup="listbox"
  aria-labelledby="type-q" aria-required="true">Select One</button>
<button type="submit" id="submit">Submit</button>
<script>
  window.__submitted = false;
  document.getElementById("submit").addEventListener("click", (event) => {
    event.preventDefault();
    window.__submitted = true;
  });
  const button = document.getElementById("phoneNumber--phoneType");
  button.addEventListener("click", () => {
    document.querySelectorAll("[data-autofill-menu]").forEach((node) => node.remove());
    const list = document.createElement("ul");
    list.setAttribute("role", "listbox");
    list.setAttribute("data-autofill-menu", "1");
    list.style.cssText = "display:block;min-height:48px";
    for (const label of ["Mobile", "Landline"]) {
      const item = document.createElement("li");
      item.setAttribute("role", "option");
      item.style.height = "24px";
      item.textContent = label;
      item.addEventListener("click", (event) => {
        event.stopPropagation();
        button.textContent = label;
        list.remove();
      });
      list.appendChild(item);
    }
    document.body.appendChild(list);
  });
</script>
"""


def test_phone_device_type_listbox_commits_mobile(browser):
    from autofill.dom_ui import commit_visible

    assert HUMAN_SUBMIT_ONLY is True
    page = browser.new_page()
    try:
        page.set_content(_PHONE_TYPE)
        filled = commit_visible(
            page,
            {
                "selector": "#phoneNumber--phoneType",
                "value": "Mobile",
                "values": ["Mobile"],
                "answerType": "single_select",
                "text": "Phone Device Type",
            },
        )
        assert filled["filled"] is True, filled
        assert page.locator("#phoneNumber--phoneType").inner_text().strip() == "Mobile"
        assert page.evaluate("() => window.__submitted") is False
    finally:
        page.close()


_PREVIOUS = """
<h1>My Information</h1>
<div data-automation-id="formField-candidateIsPreviousWorker">
  <label>Previously employed by AAON<abbr>*</abbr></label>
  <div role="radiogroup">
    <button id="prev-yes" type="button" role="radio" aria-checked="false">Yes</button>
    <button id="prev-no" type="button" role="radio" aria-checked="false">No</button>
  </div>
</div>
<button type="submit" id="submit">Submit</button>
<script>
  window.__submitted = false;
  document.getElementById("submit").addEventListener("click", (event) => {
    event.preventDefault();
    window.__submitted = true;
  });
  for (const id of ["prev-yes", "prev-no"]) {
    document.getElementById(id).addEventListener("click", () => {
      for (const other of ["prev-yes", "prev-no"]) {
        document.getElementById(other).setAttribute("aria-checked", other === id ? "true" : "false");
      }
    });
  }
</script>
"""


def test_previous_worker_radio_is_no_when_aaon_is_not_in_employment(browser, monkeypatch):
    from autofill.resolver import ResolverResponse
    from autofill.stepfill import fill_resolved_page

    def fake_resolve(binding, *, session_id, step, fields, questions):
        del binding, session_id, step, fields, questions
        return ResolverResponse(fields={}, answers=[])

    monkeypatch.setattr("autofill.stepfill.resolve_step", fake_resolve)
    page = browser.new_page()
    try:
        page.set_content(_PREVIOUS)
        outcome = fill_resolved_page(
            page,
            _binding(),
            session_id="session-a",
            ats_name="workday",
            step="my information",
        )
        assert page.locator("#prev-no").get_attribute("aria-checked") == "true"
        assert page.locator("#prev-yes").get_attribute("aria-checked") == "false"
        texts = " ".join(str(item.get("text") or "") for item in outcome.manual_questions).casefold()
        assert "previously employed" not in texts
        assert page.evaluate("() => window.__submitted") is False
    finally:
        page.close()


_CLEARED = """
<h1>My Information</h1>
<label for="line1">Address</label>
<input id="line1">
<label for="city">City</label>
<input id="city">
<label for="postal">Postal Code</label>
<input id="postal">
<label for="phone">Phone Number</label>
<input id="phone" type="tel">
<label for="state">State</label>
<select id="state" disabled><option value="">Select</option></select>
<label for="country">Country</label>
<select id="country">
  <option value="">Select</option>
  <option>United States of America</option>
</select>
<script>
  document.getElementById("country").addEventListener("change", () => {
    for (const id of ["line1", "city", "postal", "phone"]) {
      document.getElementById(id).value = "";
    }
    const state = document.getElementById("state");
    state.disabled = false;
    state.innerHTML = "<option value=''>Select</option><option>Oklahoma</option>";
  });
</script>
"""


def test_address_is_rewritten_after_country_clears_it(browser, monkeypatch):
    from autofill.resolver import ResolverResponse
    from autofill.stepfill import fill_resolved_page

    def fake_resolve(binding, *, session_id, step, fields, questions):
        del binding, session_id, step, fields, questions
        return ResolverResponse(
            fields={
                "address.line1": "43-g, james street",
                "address.city": "Tulsa",
                "address.postalCode": "74101",
                "phone": "9185550100",
                "address.country": "United States of America",
                "address.region": "Oklahoma",
                "address.state": "Oklahoma",
            },
            answers=[],
        )

    monkeypatch.setattr("autofill.stepfill.resolve_step", fake_resolve)
    page = browser.new_page()
    try:
        page.set_content(_CLEARED)
        outcome = fill_resolved_page(
            page,
            _binding(),
            session_id="session-a",
            ats_name="workday",
            step="my information",
        )
        assert page.locator("#line1").input_value() == "43-g, james street"
        assert page.locator("#city").input_value() == "Tulsa"
        assert page.locator("#postal").input_value() == "74101"
        assert page.locator("#phone").input_value() == "9185550100"
        assert page.locator("#state").input_value() == "Oklahoma"
        texts = " ".join(str(item.get("text") or "") for item in outcome.manual_questions).casefold()
        assert "address" not in texts
        assert "city" not in texts
        assert "postal" not in texts
    finally:
        page.close()


def test_middle_name_and_county_stay_blank_when_the_profile_has_no_middle_name(browser, monkeypatch):
    from autofill.resolver import ResolverResponse
    from autofill.stepfill import fill_resolved_page

    def fake_resolve(binding, *, session_id, step, fields, questions):
        del binding, session_id, step, fields, questions
        return ResolverResponse(
            fields={
                "fullName": "Jahnavi Torra",
                "firstName": "Jahnavi",
                "lastName": "Torra",
                "address.line1": "43-g, james street",
            },
            answers=[],
        )

    monkeypatch.setattr("autofill.stepfill.resolve_step", fake_resolve)
    page = browser.new_page()
    try:
        page.set_content(
            """
            <h1>My Information</h1>
            <label for="first">First Name</label><input id="first">
            <label for="middle">Middle Name</label><input id="middle" name="legalName--middleName">
            <label for="last">Last Name</label><input id="last">
            <div data-automation-id="formField-regionSubdivision1">
              <label for="county">County</label>
              <input id="county" name="address--regionSubdivision1">
            </div>
            <label for="line1">Address</label><input id="line1">
            """
        )
        fill_resolved_page(
            page,
            _binding(),
            session_id="session-a",
            ats_name="workday",
            step="my information",
        )
        assert page.locator("#first").input_value() == "Jahnavi"
        assert page.locator("#last").input_value() == "Torra"
        assert page.locator("#middle").input_value() == ""
        assert page.locator("#county").input_value() == ""
        assert page.locator("#line1").input_value() == "43-g, james street"
    finally:
        page.close()


_SCHOOL = """
<h1>My Experience</h1>
<div data-automation-id="formField-school">
  <label for="school">School or University</label>
  <p>If you cannot find your school, select School Not Found.</p>
  <div data-automation-id="multiSelectContainer">
    <input id="school" data-automation-id="searchBox" role="combobox" aria-haspopup="listbox" aria-label="Search">
    <button type="button" id="school-prompt" data-automation-id="promptIcon" aria-label="Search"
      style="width:24px;height:24px"></button>
    <span id="picked" data-automation-id="selectedItem" hidden></span>
  </div>
</div>
<script>
  window.__keys = [];
  const input = document.getElementById("school");
  let query = "";
  function commit(label) {
    const picked = document.getElementById("picked");
    picked.hidden = false;
    picked.textContent = label;
    input.value = "";
    input.setAttribute("aria-expanded", "false");
    const menu = document.getElementById("school-menu");
    if (menu) menu.remove();
  }
  function row(label, id) {
    const node = document.createElement("div");
    node.id = id;
    node.setAttribute("data-automation-id", label === "No Items." ? "promptOption" : "promptLeafNode");
    node.style.height = "24px";
    node.textContent = label;
    if (label !== "No Items.") node.addEventListener("click", () => commit(label));
    return node;
  }
  function show(q) {
    input.setAttribute("aria-expanded", "true");
    let menu = document.getElementById("school-menu");
    if (!menu) {
      menu = document.createElement("div");
      menu.id = "school-menu";
      document.body.appendChild(menu);
    }
    menu.replaceChildren();
    if (/harvard/i.test(q)) menu.appendChild(row("Harvard University", "school-hit"));
    else {
      menu.appendChild(row("No Items.", "school-empty"));
      menu.appendChild(row("School Not Found", "school-missing"));
    }
  }
  document.getElementById("school-prompt").addEventListener("click", () => show(""));
  input.addEventListener("keydown", (event) => {
    window.__keys.push(event.key);
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    if (event.key === "Backspace") query = query.slice(0, -1);
    else if (event.key && event.key.length === 1) query += event.key;
    show(query);
  });
</script>
"""


def test_school_search_uses_key_events_and_school_not_found(browser):
    from autofill.combobox import select_combobox

    assert HUMAN_SUBMIT_ONLY is True
    page = browser.new_page()
    try:
        page.set_content(_SCHOOL)
        picked = select_combobox(page, "#school", "Harvard University")
        assert picked is not None, page.locator("#picked").inner_text()
        assert page.locator("#picked").inner_text().strip() == "Harvard University"
        keys = page.evaluate("() => window.__keys.slice()")
        assert any(len(key) == 1 and key.isalpha() for key in keys)
    finally:
        page.close()
    missing_page = browser.new_page()
    try:
        missing_page.set_content(_SCHOOL)
        missing = select_combobox(missing_page, "#school", "Not A Real College")
        assert missing is not None
        assert missing_page.locator("#picked").inner_text().strip() == "School Not Found"
    finally:
        missing_page.close()


_EXPERIENCE = """
<h1>My Experience</h1>
<label for="resume">Resume</label>
<input id="resume" type="file">
<button type="button" id="add-work">Add</button>
<div id="work" hidden>
  <label for="company">Company</label>
  <input id="company">
  <label for="title">Job Title</label>
  <input id="title">
</div>
<div data-automation-id="formField-school">
  <label for="school">School or University<abbr>*</abbr></label>
  <input id="school" data-automation-id="searchBox" role="combobox" aria-required="true" aria-label="Search">
</div>
<button type="submit" id="submit">Submit</button>
<script>
  window.__added = false;
  window.__submitted = false;
  document.getElementById("submit").addEventListener("click", (event) => {
    event.preventDefault();
    window.__submitted = true;
  });
  document.getElementById("add-work").addEventListener("click", () => {
    document.getElementById("work").hidden = false;
    window.__added = true;
  });
</script>
"""


def test_experience_and_resume_are_not_blocked_by_the_school_search(browser, monkeypatch):
    from autofill.resolver import ResolverResponse
    from autofill.stepfill import fill_resolved_page

    def fake_resolve(binding, *, session_id, step, fields, questions):
        del binding, session_id, step, fields, questions
        return ResolverResponse(
            fields={
                "employment[]": [{"company": "Acme", "title": "Engineer"}],
                "education[]": [{"school": "Harvard University"}],
            },
            answers=[],
        )

    monkeypatch.setattr("autofill.stepfill.resolve_step", fake_resolve)
    page = browser.new_page()
    try:
        page.set_content(_EXPERIENCE)
        outcome = fill_resolved_page(
            page,
            _binding(),
            session_id="session-a",
            ats_name="workday",
            step="experience",
        )
        assert page.evaluate("() => window.__added") is True
        assert page.locator("#company").input_value() == "Acme"
        assert page.locator("#title").input_value() == "Engineer"
        assert outcome.resume_blocked is True
        assert page.evaluate("() => window.__submitted") is False
    finally:
        page.close()
