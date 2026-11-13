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
<label for="resume">Resume<abbr>*</abbr></label>
<input id="resume" type="file" aria-required="true">
<button type="button" id="add-work">Add</button>
<div id="work" hidden>
  <label for="company">Company<abbr>*</abbr></label>
  <input id="company" aria-required="true">
  <label for="title">Job Title<abbr>*</abbr></label>
  <input id="title" aria-required="true">
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


_LIVE_HIERARCHY = """
<h1>My Information</h1>
<div data-automation-id="formField-source">
  <label for="source">How Did You Hear About Us?</label>
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
    const stale = document.createElement("div");
    stale.id = "stale-phone";
    stale.setAttribute("data-automation-id", "promptLeafNode");
    stale.style.height = "24px";
    stale.textContent = "United States of America (+1)";
    menu.appendChild(stale);
    const category = document.createElement("div");
    category.id = "hear-category";
    category.setAttribute("data-automation-id", "promptLeafNode");
    category.setAttribute("data-uxi-multiselectlistitem-type", "2");
    category.setAttribute("data-uxi-hassidecharm", "true");
    category.style.height = "24px";
    category.appendChild(document.createTextNode("Career Websites"));
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("class", "wd-icon-chevron-right-small");
    category.appendChild(icon);
    category.addEventListener("click", () => {
      category.remove();
      stale.remove();
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


def test_hear_about_drills_a_type_2_category_and_ignores_the_phone_pill(browser):
    from autofill.combobox import list_combobox_options, select_combobox

    assert HUMAN_SUBMIT_ONLY is True
    page = browser.new_page()
    try:
        page.set_content(_LIVE_HIERARCHY)
        labels = list_combobox_options(page, "#source")
        assert "United States of America (+1)" not in labels
        assert "Corporate Website" in labels
        assert "Career Websites > Corporate Website" in labels
        picked = select_combobox(page, "#source", "Corporate Website")
        assert picked is not None
        assert page.locator("#picked").inner_text().strip() == "Corporate Website"
        assert page.evaluate("() => window.__submitted") is False
    finally:
        page.close()
    path_page = browser.new_page()
    try:
        path_page.set_content(_LIVE_HIERARCHY)
        picked = select_combobox(path_page, "#source", "Career Websites > Corporate Website")
        assert picked is not None
        assert path_page.locator("#picked").inner_text().strip() == "Corporate Website"
        category = select_combobox(path_page, "#source", "Career Websites")
        assert category is not None
        assert path_page.locator("#picked").inner_text().strip() == "Corporate Website"
    finally:
        path_page.close()


_PREVIOUS_NOTE = """
<h1>My Information</h1>
<div data-automation-id="formField-candidateIsPreviousWorker">
  <label id="prev-legend">Have you previously been employed by AAON?<br>
    Current Employees: Please apply via your internal Workday account.</label>
  <div role="radiogroup" aria-labelledby="prev-legend">
    <input id="prev-yes" type="radio" name="candidateIsPreviousWorker" value="true">
    <label for="prev-yes">Yes</label>
    <input id="prev-no" type="radio" name="candidateIsPreviousWorker" value="false">
    <label for="prev-no">No</label>
  </div>
</div>
<button type="submit" id="submit">Submit</button>
<script>
  window.__submitted = false;
  document.getElementById("submit").addEventListener("click", (event) => {
    event.preventDefault();
    window.__submitted = true;
  });
</script>
"""


def test_previous_worker_note_still_answers_no_and_answer_commits(browser, monkeypatch):
    from autofill.dom_ui import commit_visible, control_committed
    from autofill.extract import extract_page
    from autofill.resolver import ResolverResponse
    from autofill.stepfill import fill_resolved_page

    def fake_resolve(binding, *, session_id, step, fields, questions):
        del binding, session_id, step, fields, questions
        return ResolverResponse(fields={}, answers=[])

    monkeypatch.setattr("autofill.stepfill.resolve_step", fake_resolve)
    page = browser.new_page()
    try:
        page.set_content(_PREVIOUS_NOTE)
        outcome = fill_resolved_page(
            page,
            _binding(),
            session_id="session-a",
            ats_name="workday",
            step="my information",
        )
        assert page.locator("#prev-no").is_checked()
        assert page.locator("#prev-yes").is_checked() is False
        texts = " ".join(str(item.get("text") or "") for item in outcome.manual_questions).casefold()
        assert "previously" not in texts
        assert page.evaluate("() => window.__submitted") is False
    finally:
        page.close()
    answer_page = browser.new_page()
    try:
        answer_page.set_content(_PREVIOUS_NOTE)
        control = next(item for item in extract_page(answer_page).controls if item.kind == "radio")
        filled = commit_visible(
            answer_page,
            {
                "selector": control.selector,
                "value": "No",
                "values": ["No"],
                "answerType": "single_select",
                "text": control.label,
            },
        )
        assert filled["filled"] is True, filled
        assert control_committed(answer_page, control.selector) is True
        assert answer_page.locator("#prev-no").is_checked()
        assert answer_page.evaluate("() => window.__submitted") is False
    finally:
        answer_page.close()


def test_phone_device_type_is_mobile_without_an_answer(browser):
    from autofill.fill import fill_one_page

    assert HUMAN_SUBMIT_ONLY is True
    page = browser.new_page()
    try:
        page.set_content(_PHONE_TYPE)
        fill_one_page(
            page,
            _person(),
            cover_letter_path=None,
            cover_letter_text=None,
            job=None,
            resume_uploaded=False,
        )
        assert page.locator("#phoneNumber--phoneType").inner_text().strip() == "Mobile"
        assert page.evaluate("() => window.__submitted") is False
    finally:
        page.close()


def test_fill_one_page_rewrites_address_after_country_clears_it(browser):
    from autofill.fill import fill_one_page

    page = browser.new_page()
    try:
        page.set_content(_CLEARED)
        person = _person(personal={"country": "United States of America"})
        fill_one_page(
            page,
            person,
            cover_letter_path=None,
            cover_letter_text=None,
            job=None,
            resume_uploaded=False,
        )
        assert page.locator("#country").input_value() == "United States of America"
        assert page.locator("#line1").input_value() == "43-g, james street"
        assert page.locator("#city").input_value() == "Tulsa"
        assert page.locator("#postal").input_value() == "74101"
        assert page.locator("#phone").input_value() == "9185550100"
        assert page.locator("#state").input_value() == "Oklahoma"
    finally:
        page.close()


_MULTI_ADD = """
<main>
<h1>My Experience</h1>
<div role="group" aria-labelledby="Work-Experience-section">
  <h4 id="Work-Experience-section">Work Experience</h4>
  <button type="button" data-automation-id="add-button">Add</button>
  <div id="work" hidden>
    <label for="company">Company</label>
    <input id="company">
    <label for="title">Job Title</label>
    <input id="title">
  </div>
</div>
<div role="group" aria-labelledby="Education-section">
  <h4 id="Education-section">Education</h4>
  <button type="button" data-automation-id="add-button">Add</button>
  <div id="edu" hidden>
    <label for="school">School</label>
    <input id="school">
  </div>
</div>
<div role="group" aria-labelledby="Languages-section">
  <h4 id="Languages-section">Languages</h4>
  <button type="button" data-automation-id="add-button">Add</button>
</div>
<button type="button" id="next" data-automation-id="pageFooterNextButton">Save and Continue</button>
<button type="submit" id="submit">Submit</button>
<script>
  window.__clicked = [];
  window.__submitted = false;
  document.getElementById("submit").addEventListener("click", (event) => {
    event.preventDefault();
    window.__submitted = true;
  });
  document.getElementById("next").addEventListener("click", () => window.__clicked.push("next"));
  function addButton(section) {
    return document.querySelector("[aria-labelledby='" + section + "'] [data-automation-id='add-button']");
  }
  addButton("Work-Experience-section").addEventListener("click", () => {
    window.__clicked.push("add-work");
    document.getElementById("work").hidden = false;
  });
  addButton("Education-section").addEventListener("click", () => {
    window.__clicked.push("add-edu");
    document.getElementById("edu").hidden = false;
  });
  addButton("Languages-section").addEventListener("click", () => window.__clicked.push("add-lang"));
</script>
</main>
"""


def test_my_experience_add_buttons_are_scoped_and_do_not_fail_the_page(browser):
    from autofill.engine import AutofillOptions, autofill_application
    from autofill.extract import extract_page

    assert HUMAN_SUBMIT_ONLY is True
    page = browser.new_page()
    try:
        page.set_content(_MULTI_ADD)
        buttons = [button for button in extract_page(page).buttons if button.automation_id == "add-button"]
        assert len(buttons) == 3
        assert len({button.selector for button in buttons}) == 3
        assert any("Work-Experience-section" in button.selector for button in buttons)
        person = _person(
            employment=[{"company": "Other Co", "title": "Engineer"}],
            education=[{"school": "State University", "degree": "BS"}],
        )
        result = autofill_application(
            "https://aaon.wd108.myworkdayjobs.com/AAON/job/Redmond-OR/Controls-Engineer_JR102838/apply",
            person,
            options=AutofillOptions(page=page, max_pages=1),
        )
        clicked = page.evaluate("() => window.__clicked.slice()")
        assert result.status != "FAILED"
        assert "add-work" in clicked
        assert "add-edu" in clicked
        assert "add-lang" not in clicked
        assert page.locator("#company").input_value() == "Other Co"
        assert page.locator("#title").input_value() == "Engineer"
        assert page.locator("#school").input_value() == "State University"
        assert page.evaluate("() => window.__submitted") is False
    finally:
        page.close()


_PREFILLED = """
<main>
<h1>My Information</h1>
<label for="email">Email<abbr>*</abbr></label>
<input id="email" type="email" required value="">
<button type="button" id="next" data-automation-id="pageFooterNextButton" aria-disabled="true">
  Save and Continue
</button>
<button type="submit" id="submit">Submit</button>
<script>
  window.__clicked = [];
  window.__submitted = false;
  const email = document.getElementById("email");
  const next = document.getElementById("next");
  email.addEventListener("input", () => {
    if (email.value.indexOf("@") !== -1) next.removeAttribute("aria-disabled");
  });
  next.addEventListener("click", () => {
    window.__clicked.push("next");
    document.querySelector("h1").textContent = "Review";
    email.remove();
    next.remove();
  });
  document.getElementById("submit").addEventListener("click", (event) => {
    event.preventDefault();
    window.__submitted = true;
  });
</script>
</main>
"""


def test_hidden_resume_input_still_blocks_and_experience_is_added(browser):
    from autofill.fill import fill_one_page

    page = browser.new_page()
    try:
        page.set_content(
            _MULTI_ADD.replace(
                "</main>",
                """
                <div role="group" aria-labelledby="Resume/CV-section">
                  <h4 id="Resume/CV-section">Resume/CV</h4>
                  <label>Resume/CV</label>
                  <input data-automation-id="file-upload-input-ref" type="file">
                </div>
                <style>[data-automation-id="file-upload-input-ref"] { display: none; }</style>
                </main>
                """,
            )
        )
        fields, _snapshot, blocked, _manual = fill_one_page(
            page,
            _person(employment=[{"company": "Other Co", "title": "Engineer"}]),
            cover_letter_path=None,
            cover_letter_text=None,
            job=None,
            resume_uploaded=False,
        )
        assert blocked is True
        assert any(item.action == "resume_required" for item in fields)
        assert "add-work" in page.evaluate("() => window.__clicked.slice()")
        assert page.locator("#company").input_value() == "Other Co"
        assert page.evaluate("() => window.__submitted") is False
    finally:
        page.close()


def test_reentered_page_presses_save_and_continue(browser):
    from autofill.engine import AutofillOptions, autofill_application

    assert HUMAN_SUBMIT_ONLY is True
    page = browser.new_page()
    try:
        page.set_content(_PREFILLED)
        result = autofill_application(
            "https://aaon.wd108.myworkdayjobs.com/AAON/job/Redmond-OR/Controls-Engineer_JR102838/apply",
            _person(),
            options=AutofillOptions(page=page, max_pages=3),
        )
        assert "next" in page.evaluate("() => window.__clicked.slice()")
        assert result.next_clicks >= 1
        assert page.locator("h1").inner_text().strip() == "Review"
        assert page.evaluate("() => window.__submitted") is False
        assert result.status != "FAILED"
    finally:
        page.close()


def test_delete_is_not_a_submit_control():
    from autofill.safeguards import classify_control

    assert classify_control("Delete") == "other"
    assert classify_control("Delete row") == "other"
    assert classify_control("Submit application") == "submit"


def test_empty_past_job_title_does_not_copy_the_current_title():
    from autofill.mapping import MapCursor, map_field

    person = _person(
        employment=[
            {"company": "Current Co", "title": "Lead", "current": True},
            {"company": "VEM", "title": "", "current": False},
        ],
        experience={"current_job_title": "engineer"},
    )
    cursor = MapCursor()
    first = map_field(Control(kind="text", label="Job Title", element_id="job-1"), person, cursor=cursor)
    second = map_field(Control(kind="text", label="Job Title", element_id="job-2"), person, cursor=cursor)
    assert first.text == "Lead"
    assert second.action == "unanswered"
    assert second.text == ""


def test_parse_workday_month_year_from_profile_dates():
    from autofill.dates import parse_month_year

    assert parse_month_year("01/10/2023") == (10, 2023)
    assert parse_month_year("10/2023") == (10, 2023)
    assert parse_month_year("2023-10-01") == (10, 2023)


_DELAYED_ADD = """
<main>
<h1>My Experience</h1>
<div role="group" aria-labelledby="Work-Experience-section">
  <h4 id="Work-Experience-section">Work Experience</h4>
  <button type="button" id="add-work" data-automation-id="add-button">Add</button>
  <div id="work-rows"></div>
</div>
<div role="group" aria-labelledby="Education-section">
  <h4 id="Education-section">Education</h4>
  <button type="button" id="add-edu" data-automation-id="add-button">Add</button>
  <div id="edu-rows"></div>
</div>
<button type="button" id="delete-row">Delete</button>
<button type="submit" id="submit">Submit</button>
<script>
  window.__clicked = [];
  window.__submitted = false;
  document.getElementById("submit").addEventListener("click", (event) => {
    event.preventDefault();
    window.__submitted = true;
  });
  let workN = 0;
  let eduN = 0;
  document.getElementById("add-work").addEventListener("click", () => {
    window.__clicked.push("add-work");
    window.setTimeout(() => {
      workN += 1;
      const box = document.createElement("div");
      box.innerHTML = '<label for="company-' + workN + '">Company</label>'
        + '<input id="company-' + workN + '">'
        + '<label for="title-' + workN + '">Job Title</label>'
        + '<input id="title-' + workN + '">';
      document.getElementById("work-rows").appendChild(box);
    }, 400);
  });
  document.getElementById("add-edu").addEventListener("click", () => {
    window.__clicked.push("add-edu");
    window.setTimeout(() => {
      eduN += 1;
      const box = document.createElement("div");
      box.innerHTML = '<label for="school-' + eduN + '">School</label><input id="school-' + eduN + '">';
      document.getElementById("edu-rows").appendChild(box);
    }, 400);
  });
</script>
</main>
"""


def test_delayed_add_stops_at_the_profile_row_count(browser):
    from autofill.fill import fill_one_page

    page = browser.new_page()
    try:
        page.set_content(_DELAYED_ADD)
        person = _person(
            employment=[
                {"company": "Alpha", "title": "Lead"},
                {"company": "VEM", "title": ""},
            ],
            education=[{"school": "State University", "degree": "BS"}],
            experience={"current_job_title": "engineer"},
        )
        fill_one_page(
            page,
            person,
            cover_letter_path=None,
            cover_letter_text=None,
            job=None,
            resume_uploaded=False,
        )
        clicked = page.evaluate("() => window.__clicked.slice()")
        assert clicked.count("add-work") == 2
        assert clicked.count("add-edu") == 1
        assert page.locator("#company-1").input_value() == "Alpha"
        assert page.locator("#company-2").input_value() == "VEM"
        assert page.locator("#title-1").input_value() == "Lead"
        assert page.locator("#title-2").input_value() == ""
        assert page.locator("#school-1").input_value() == "State University"
        assert page.evaluate("() => window.__submitted") is False
    finally:
        page.close()


_SPLIT_DATES = """
<main>
<h1>My Experience</h1>
<div data-automation-id="formField-startDate" id="start-field">
  <label>From</label>
  <input id="workExperience-1--startDate-dateSectionMonth-input"
    data-automation-id="dateSectionMonth-input" aria-label="Month">
  <input id="workExperience-1--startDate-dateSectionYear-input"
    data-automation-id="dateSectionYear-input" aria-label="Year">
  <div id="start-readout"></div>
</div>
<div data-automation-id="formField-endDate" id="end-field">
  <label>To</label>
  <input id="workExperience-1--endDate-dateSectionMonth-input"
    data-automation-id="dateSectionMonth-input" aria-label="Month">
  <input id="workExperience-1--endDate-dateSectionYear-input"
    data-automation-id="dateSectionYear-input" aria-label="Year">
  <div id="end-readout"></div>
</div>
<script>
  function watch(fieldId, monthId, yearId, outId) {
    const month = document.getElementById(monthId);
    const year = document.getElementById(yearId);
    const out = document.getElementById(outId);
    function paint() {
      if (month.value && year.value) {
        const mm = month.value.padStart(2, "0");
        out.textContent = "current value is " + mm + "/" + year.value;
      } else {
        out.textContent = "";
      }
    }
    month.addEventListener("input", paint);
    year.addEventListener("input", paint);
  }
  watch("start-field", "workExperience-1--startDate-dateSectionMonth-input",
    "workExperience-1--startDate-dateSectionYear-input", "start-readout");
  watch("end-field", "workExperience-1--endDate-dateSectionMonth-input",
    "workExperience-1--endDate-dateSectionYear-input", "end-readout");
  document.getElementById("workExperience-1--endDate-dateSectionYear-input").addEventListener("input", () => {
    document.getElementById("workExperience-1--endDate-dateSectionMonth-input").value = "";
  });
</script>
</main>
"""


def test_workday_split_dates_fill_as_one_group(browser):
    from autofill.dom_ui import commit_visible
    from autofill.fill import fill_one_page

    page = browser.new_page()
    try:
        page.set_content(_SPLIT_DATES)
        person = _person(
            employment=[
                {
                    "company": "Alpha",
                    "title": "Lead",
                    "start_date": "01/10/2023",
                    "end_date": "31/10/2026",
                    "current": False,
                }
            ]
        )
        fill_one_page(
            page,
            person,
            cover_letter_path=None,
            cover_letter_text=None,
            job=None,
            resume_uploaded=False,
        )
        assert "current value is 10/2023" in page.locator("#start-field").inner_text()
        assert "current value is 10/2026" in page.locator("#end-field").inner_text()
        answered = commit_visible(
            page,
            {
                "selector": "#workExperience-1--endDate-dateSectionYear-input",
                "value": "2027",
                "answerType": "text",
            },
        )
        assert answered["filled"] is True, answered
        assert page.locator("#workExperience-1--endDate-dateSectionMonth-input").input_value() == "10"
        assert page.locator("#workExperience-1--endDate-dateSectionYear-input").input_value() == "2027"
    finally:
        page.close()


def test_current_job_leaves_the_end_date_empty(browser):
    from autofill.fill import fill_one_page

    page = browser.new_page()
    try:
        page.set_content(_SPLIT_DATES)
        person = _person(
            employment=[
                {
                    "company": "Alpha",
                    "title": "Lead",
                    "start_date": "10/2023",
                    "end_date": "10/2026",
                    "current": True,
                }
            ]
        )
        fill_one_page(
            page,
            person,
            cover_letter_path=None,
            cover_letter_text=None,
            job=None,
            resume_uploaded=False,
        )
        assert "current value is 10/2023" in page.locator("#start-field").inner_text()
        assert page.locator("#workExperience-1--endDate-dateSectionMonth-input").input_value() == ""
        assert page.locator("#workExperience-1--endDate-dateSectionYear-input").input_value() == ""
    finally:
        page.close()


_SCHOOL_SEARCH = """
<h1>My Experience</h1>
<div data-automation-id="formField-school">
  <label for="school">School or University</label>
  <div data-automation-id="multiSelectContainer">
    <input id="school" data-automation-id="searchBox">
    <span id="picked" data-automation-id="selectedItem" hidden></span>
  </div>
</div>
<script>
  const input = document.getElementById("school");
  let query = "";
  function commit(label) {
    const picked = document.getElementById("picked");
    picked.hidden = false;
    picked.textContent = label;
    input.value = "";
    const menu = document.getElementById("school-menu");
    if (menu) menu.remove();
  }
  function row(label, id) {
    const node = document.createElement("div");
    node.id = id;
    node.setAttribute("data-automation-id", "promptOption");
    node.style.height = "24px";
    node.textContent = label;
    node.addEventListener("click", () => commit(label));
    return node;
  }
  function show(q) {
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
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      show(query);
      return;
    }
    if (event.key && event.key.length === 1) query += event.key;
  });
</script>
"""


def test_school_answer_searches_then_picks_the_prompt_option(browser):
    from autofill.dom_ui import commit_visible

    page = browser.new_page()
    try:
        page.set_content(_SCHOOL_SEARCH)
        filled = commit_visible(
            page,
            {
                "selector": "#school",
                "value": "harvard",
                "answerType": "single_select",
                "text": "School or University",
                "fieldKey": "school",
                "options": [],
            },
        )
        assert filled["filled"] is True, filled
        assert page.locator("#picked").inner_text().strip() == "Harvard University"
    finally:
        page.close()
    missing = browser.new_page()
    try:
        missing.set_content(_SCHOOL_SEARCH)
        fallback = commit_visible(
            missing,
            {
                "selector": "#school",
                "value": "Not A Real College",
                "answerType": "single_select",
                "text": "School or University",
                "fieldKey": "school",
                "options": [],
            },
        )
        assert fallback["filled"] is True, fallback
        assert missing.locator("#picked").inner_text().strip() == "School Not Found"
    finally:
        missing.close()


_START = """
<main>
<h2>My Information</h2>
<label for="email">Email</label>
<input id="email" type="email">
<button type="button" id="next">Next</button>
<button type="submit" id="submit">Submit</button>
<script>
  window.__clicked = [];
  window.__submitted = false;
  document.getElementById("submit").addEventListener("click", (event) => {
    event.preventDefault();
    window.__submitted = true;
  });
  document.getElementById("next").addEventListener("click", () => {
    window.__clicked.push("next");
    document.body.innerHTML = `
      <h1>Start Your Application</h1>
      <button type="button" id="manual">Apply Manually
Create a new application</button>
      <button type="submit" id="submit">Submit</button>
    `;
    document.getElementById("submit").addEventListener("click", (event) => {
      event.preventDefault();
      window.__submitted = true;
    });
    document.getElementById("manual").addEventListener("click", () => {
      window.__clicked.push("Apply Manually");
      document.querySelector("h1").textContent = "My Information";
      const label = document.createElement("label");
      label.htmlFor = "email2";
      label.textContent = "Email";
      const input = document.createElement("input");
      input.id = "email2";
      input.type = "email";
      document.body.appendChild(label);
      document.body.appendChild(input);
      document.getElementById("manual").remove();
    });
  });
</script>
</main>
"""


def test_start_your_application_clicks_apply_manually(browser):
    from autofill.engine import AutofillOptions, Status, autofill_application

    page = browser.new_page()
    try:
        page.set_content(_START)
        result = autofill_application(
            "https://aaon.wd108.myworkdayjobs.com/AAON/job/Redmond-OR/Controls-Engineer_JR102838/apply",
            _person(),
            options=AutofillOptions(page=page, max_pages=3),
        )
        clicked = page.evaluate("() => window.__clicked.slice()")
        assert "Apply Manually" in clicked
        assert result.status != Status.NO_FORM_FOUND
        assert page.evaluate("() => window.__submitted") is False
    finally:
        page.close()


def test_my_information_without_fields_waits_and_error_page_stays_put(monkeypatch):
    from autofill.local_attach import adopt_employer_tab, should_reload_application
    from autofill.models import ButtonControl
    from autofill.pagewait import render_ready

    waiting = PageSnapshot(
        controls=[],
        buttons=[ButtonControl(name="Sign In", in_header=False, in_main=True)],
        heading="My Information",
    )
    assert render_ready(waiting) is False
    ready = PageSnapshot(
        controls=[Control(kind="text", label="Email", selector="#email", input_type="email")],
        buttons=[],
        heading="My Information",
    )
    assert render_ready(ready) is True

    live_url = "https://aaon.wd108.myworkdayjobs.com/en-US/AAON/job/Role/apply"
    apply = "https://aaon.wd108.myworkdayjobs.com/AAON/job/Role/apply"
    monkeypatch.setattr("autofill.local_attach._tab_is_posting", lambda _page: False)

    class _Page:
        def __init__(self, url: str) -> None:
            self.url = url
            self.gotos: list[str] = []

        def goto(self, url: str, **_kwargs: object) -> None:
            self.gotos.append(url)
            self.url = url

    page = _Page(live_url)
    assert should_reload_application(page, apply, candidate_changed=False) is False
    adopted = adopt_employer_tab(type("Ctx", (), {"pages": [page]})(), apply)
    assert adopted is page
    assert page.gotos == []



def test_error_page_refreshes_in_place(monkeypatch):
    from autofill.engine import _ERROR_RELOADS, _refresh_error_page

    _ERROR_RELOADS.set(0)
    error = PageSnapshot(
        controls=[],
        buttons=[],
        heading="Something went wrong",
        page_text="Please refresh the page and then try again.",
    )
    form = PageSnapshot(
        controls=[Control(kind="radio", label="Gender", selector="#g", required=True)],
        buttons=[],
        heading="Voluntary Disclosures",
    )

    class _Page:
        def __init__(self) -> None:
            self.snapshot = error
            self.calls: list[str] = []

        def reload(self, **_kwargs: object) -> None:
            self.calls.append("reload")
            self.snapshot = form

        def goto(self, url: str, **_kwargs: object) -> None:
            self.calls.append("goto:" + url)

    page = _Page()
    monkeypatch.setattr("autofill.engine.extract_page", lambda current: current.snapshot)
    monkeypatch.setattr("autofill.engine.wait_for_render", lambda _page: None)
    monkeypatch.setattr("autofill.engine.note_snapshot", lambda _snap: None)
    fresh = _refresh_error_page(page, error)
    assert fresh is form
    assert page.calls == ["reload"]
    assert _refresh_error_page(page, error) is None


def test_empty_job_title_and_location_are_not_copied(browser):
    from autofill.fill import fill_one_page

    page = browser.new_page()
    try:
        page.set_content(
            """
            <h1>My Experience</h1>
            <label for="title-1">Job Title</label><input id="title-1" value="engineer">
            <label for="company-1">Company</label><input id="company-1">
            <label for="loc-1">Location</label>
            <input id="loc-1" name="workExperience-1--location" value="New York, New York, US">
            <label for="title-2">Job Title</label><input id="title-2" value="engineer">
            <label for="company-2">Company</label><input id="company-2">
            <label for="loc-2">Location</label>
            <input id="loc-2" name="workExperience-2--location" value="New York, New York, US">
            """
        )
        person = _person(
            personal={"city": "New York", "province_state": "New York", "country": "US"},
            employment=[
                {"company": "Standard electric company, inc.", "title": "engineer", "current": False},
                {"company": "VEM Technologies pvt ltd", "title": "", "current": False},
            ],
            experience={"current_job_title": "engineer"},
        )
        fill_one_page(
            page,
            person,
            cover_letter_path=None,
            cover_letter_text=None,
            job=None,
            resume_uploaded=False,
        )
        assert page.locator("#title-1").input_value() == "engineer"
        assert page.locator("#company-2").input_value() == "VEM Technologies pvt ltd"
        assert page.locator("#title-2").input_value() == ""
        assert page.locator("#loc-1").input_value() == ""
        assert page.locator("#loc-2").input_value() == ""
    finally:
        page.close()


def test_empty_work_dates_are_labeled_and_filled_rows_are_kept(browser):
    from autofill.extract import extract_page
    from autofill.split_dates import commit_split_date_answer
    from autofill.stepfill import inspect_required

    html = """
    <h1>My Experience</h1>
    <div data-automation-id="formField-startDate" id="start-1">
      <label>From*</label>
      <input id="workExperience-1--startDate-dateSectionMonth-input" required value="10">
      <input id="workExperience-1--startDate-dateSectionYear-input" required value="2023">
      <div>current value is 10/2023</div>
    </div>
    <div data-automation-id="formField-endDate" id="end-1">
      <label>To*</label>
      <input id="workExperience-1--endDate-dateSectionMonth-input" required value="10">
      <input id="workExperience-1--endDate-dateSectionYear-input" required value="2026">
      <div>current value is 10/2026</div>
    </div>
    <div data-automation-id="formField-startDate" id="start-2">
      <label>From*</label>
      <input id="workExperience-2--startDate-dateSectionMonth-input" required value="">
      <input id="workExperience-2--startDate-dateSectionYear-input" required value="">
    </div>
    <div data-automation-id="formField-endDate" id="end-2">
      <label>To*</label>
      <input id="workExperience-2--endDate-dateSectionMonth-input" required value="">
      <input id="workExperience-2--endDate-dateSectionYear-input" required value="">
    </div>
    """
    page = browser.new_page()
    try:
        page.set_content(html)
        person = _person(
            employment=[
                {
                    "company": "Standard electric company, inc.",
                    "title": "engineer",
                    "start_date": "01/10/2023",
                    "end_date": "31/10/2026",
                },
                {"company": "VEM Technologies pvt ltd", "title": ""},
            ]
        )
        gaps, _total, _done = inspect_required(page, extract_page(page), person)
        prompts = [str(item.get("text") or "") for item in gaps]
        assert len(prompts) == 2, prompts
        assert any("Work Experience 2" in text and "VEM" in text and text.endswith("From") for text in prompts)
        assert any("Work Experience 2" in text and text.endswith("To") for text in prompts)
        assert all("Standard electric" not in text for text in prompts)
        assert all(item.get("kind") == "MISSING_PROFILE_VALUE" for item in gaps)
        assert all(str(item.get("fieldKey") or "").startswith("employment.1.") for item in gaps)
        before = page.locator("#workExperience-1--startDate-dateSectionYear-input").input_value()
        target = next(item for item in gaps if str(item.get("fieldKey")).endswith("startDate"))
        report = commit_split_date_answer(
            page,
            {"selector": target["selector"], "fieldKey": target["fieldKey"], "value": "01/2021"},
        )
        assert report["verified"] is True
        assert page.locator("#workExperience-1--startDate-dateSectionYear-input").input_value() == before
        assert page.locator("#workExperience-2--startDate-dateSectionMonth-input").input_value() == "01"
        assert page.locator("#workExperience-2--startDate-dateSectionYear-input").input_value() == "2021"
    finally:
        page.close()


def test_field_of_study_search_and_unmatched_flag(browser):
    from autofill.combobox import select_combobox
    from autofill.extract import extract_page
    from autofill.stepfill import unmatched_study_gaps

    html = """
    <h1>My Experience</h1>
    <div data-automation-id="formField-fieldOfStudy">
      <label for="study">Field of Study</label>
      <div data-automation-id="multiSelectContainer">
        <input id="study" data-automation-id="searchBox" role="combobox" aria-label="Search">
        <span id="picked" data-automation-id="selectedItem" hidden></span>
      </div>
    </div>
    <script>
      const input = document.getElementById("study");
      let query = "";
      input.addEventListener("keydown", (event) => {
        if (event.key && event.key.length === 1) query += event.key;
        input.setAttribute("aria-expanded", "true");
        let menu = document.getElementById("menu");
        if (!menu) {
          menu = document.createElement("div");
          menu.id = "menu";
          document.body.appendChild(menu);
        }
        menu.replaceChildren();
        if (/engineer/i.test(query)) {
          const row = document.createElement("div");
          row.id = "eng";
          row.setAttribute("data-automation-id", "promptLeafNode");
          row.textContent = "Engineering";
          row.style.height = "24px";
          row.addEventListener("click", () => {
            const picked = document.getElementById("picked");
            picked.hidden = false;
            picked.textContent = "Engineering";
            menu.remove();
          });
          menu.appendChild(row);
        }
      });
    </script>
    """
    page = browser.new_page()
    try:
        page.set_content(html)
        picked = select_combobox(page, "#study", "engineer")
        assert picked is not None
        assert page.locator("#picked").inner_text().strip() == "Engineering"
        person = _person(education=[{"school": "Harvard", "degree": "MS", "field_of_study": "dance"}])
        page.set_content(html.replace("/engineer/i", "/nomatch/"))
        gaps = unmatched_study_gaps(page, extract_page(page), person)
        assert gaps and gaps[0]["kind"] == "MISSING_PROFILE_VALUE"
        assert gaps[0]["fieldKey"] == "field_of_study"
    finally:
        page.close()


def test_phone_country_prompt_is_not_postal_code(browser):
    from autofill.extract import extract_page
    from autofill.mapping import map_field, workday_phone_text

    page = browser.new_page()
    try:
        page.set_content(
            """
            <label for="postal">Postal Code</label>
            <input id="postal" value="">
            <div data-automation-id="formField-countryPhoneCode">
              <span data-automation-id="formLabel">Country Phone Code</span>
              <input id="code" data-automation-id="searchBox" role="combobox" aria-label="Search">
            </div>
            <label for="phone">Phone Number</label>
            <input id="phone" type="tel">
            """
        )
        found = {control.element_id: control for control in extract_page(page).controls}
        assert "postal" not in found["code"].label.casefold()
        assert "country" in found["code"].label.casefold()
        mapped = map_field(found["code"], _person())
        assert mapped.key == "phone_country"
    finally:
        page.close()
    formatted = workday_phone_text("+1475-441-1744", country="United States", national=True)
    assert formatted == "475-441-1744"
    assert not formatted.startswith("+")
    assert "4754411744" == "".join(character for character in formatted if character.isdigit())


def test_hear_about_answer_is_an_application_answer_not_a_work_date():
    from autofill.local_helper import _keep_profile_row, _remember_hear_answer

    person = _person(
        employment=[
            {"company": "Alpha", "title": "Lead"},
            {"company": "VEM Technologies pvt ltd", "title": ""},
        ]
    )
    job = type(
        "Job",
        (),
        {"application_url": "https://aaon.wd108.myworkdayjobs.com/AAON/job/x/apply", "profile": person},
    )()
    _remember_hear_answer(
        job,
        person,
        {"text": "How Did You Hear About Us?", "fieldKey": "how_heard", "kind": "MISSING_PROFILE_VALUE"},
        {"values": ["Social Media"]},
    )
    assert person.screening.how_heard == "Social Media"
    assert any(item.answer == "Social Media" for item in person.application_answers)
    _keep_profile_row(
        person,
        {"fieldKey": "employment.1.startDate", "kind": "MISSING_PROFILE_VALUE", "text": "From"},
        {"values": ["01/2021"]},
    )
    assert person.employment[1].start_date == "01/2021"
    assert all(item.answer != "01/2021" for item in person.application_answers)
