"""Confirm email, City, and residual Hear About commits.

Fixtures only. No employer site is contacted.
"""

import pytest
from playwright.sync_api import sync_playwright

from autofill.profile import CandidateProfile


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


def _person() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {
            "candidateId": "cand-commit",
            "personal": {
                "full_name": "Casey Example",
                "email": "casey.example@example.com",
                "phone": "9175550100",
                "city": "New York",
                "province_state": "NY",
                "country": "United States",
                "address": "100 Broadway",
                "postal_code": "10001",
            },
            "screening": {"how_heard": "LinkedIn"},
        }
    )


_SMARTRECRUITERS = """
<form onsubmit="window.__submitted = true; return false;">
  <h1>My Information</h1>
  <label for="email">Email</label>
  <input id="email" name="email" type="email" required>
  <label for="confirm">Confirm email</label>
  <input id="confirm" name="emailConfirmation" type="email" required>
  <label for="city">City</label>
  <input id="city" name="city" role="combobox" aria-autocomplete="list" aria-controls="city-list" required>
  <ul id="city-list" hidden></ul>
  <button type="submit" id="final-submit">Submit application</button>
</form>
<script>
  window.__submitted = false;
  const email = document.getElementById("email");
  const confirm = document.getElementById("confirm");
  const city = document.getElementById("city");
  const list = document.getElementById("city-list");
  let confirmModel = "";
  confirm.addEventListener("input", (event) => {
    if (event instanceof InputEvent) confirmModel = confirm.value;
  });
  confirm.addEventListener("blur", () => {
    if (!confirmModel || confirmModel !== email.value) {
      confirmModel = "";
      confirm.value = "";
    }
  });
  const places = ["New York, NY, United States", "New York, England, United Kingdom"];
  city.addEventListener("input", () => {
    list.hidden = false;
    list.replaceChildren();
    const query = String(city.value || "").toLowerCase();
    if (!query) return;
    places.forEach((place, index) => {
      if (place.toLowerCase().indexOf(query) === -1) return;
      const item = document.createElement("li");
      item.id = "place-" + index;
      item.setAttribute("role", "option");
      item.style.cssText = "display:block;min-height:24px";
      item.textContent = place;
      item.addEventListener("click", () => {
        city.dataset.place = place;
        city.value = place;
        list.hidden = true;
      });
      list.appendChild(item);
    });
  });
  city.addEventListener("blur", () => {
    if (!city.dataset.place) city.value = "";
  });
</script>
"""


def test_confirm_email_and_city_land_in_the_form(browser):
    """Save & Continue values stay in the employer inputs after blur."""
    from autofill.dom_ui import commit_visible
    from autofill.fill import fill_one_page
    from autofill.stepfill import bind_location_profile, reset_location_profile

    person = _person()
    page = browser.new_page()
    token = bind_location_profile(person)
    try:
        page.set_content(_SMARTRECRUITERS)
        fill_one_page(
            page,
            person,
            cover_letter_path=None,
            cover_letter_text=None,
            job=None,
            resume_uploaded=False,
        )
        assert page.locator("#email").input_value() == "casey.example@example.com"
        assert page.locator("#confirm").input_value() == "casey.example@example.com"
        assert page.locator("#city").input_value() == "New York, NY, United States"
        assert "United Kingdom" not in page.locator("#city").input_value()
        assert page.evaluate("() => window.__submitted") is False

        page.evaluate(
            """() => {
              document.getElementById("email").value = "";
              document.getElementById("confirm").value = "";
              const city = document.getElementById("city");
              city.value = "";
              delete city.dataset.place;
            }"""
        )
        email = commit_visible(
            page,
            {
                "selector": "#confirm",
                "fieldKey": "email",
                "text": "Confirm email",
                "answerType": "text",
                "value": "casey.example@example.com",
            },
        )
        city = commit_visible(
            page,
            {
                "selector": "#city",
                "fieldKey": "city",
                "text": "City",
                "answerType": "text",
                "value": "New York",
            },
        )
        assert email["filled"] is True
        assert city["filled"] is True
        assert page.locator("#email").input_value() == "casey.example@example.com"
        assert page.locator("#confirm").input_value() == "casey.example@example.com"
        assert page.locator("#city").input_value() == "New York, NY, United States"
        assert page.evaluate("() => window.__submitted") is False
    finally:
        reset_location_profile(token)
        page.close()


_HEAR_RADIOS = """
<div role="radiogroup" aria-label="How did you hear about us?" aria-required="true">
  <button type="button" role="radio" id="li" aria-checked="false">LinkedIn</button>
  <button type="button" role="radio" id="jb" aria-checked="false">Job Board</button>
</div>
<script>
  document.querySelectorAll("[role='radio']").forEach((node) => {
    node.addEventListener("click", () => {
      window.setTimeout(() => {
        document.querySelectorAll("[role='radio']").forEach((peer) => {
          peer.setAttribute("aria-checked", "false");
        });
        node.setAttribute("aria-checked", "true");
      }, 80);
    });
  });
</script>
"""


def test_hear_about_radio_waits_until_the_choice_sticks(browser):
    from autofill.fill import fill_one_page

    page = browser.new_page()
    try:
        page.set_content(_HEAR_RADIOS)
        fill_one_page(
            page,
            _person(),
            cover_letter_path=None,
            cover_letter_text=None,
            job=None,
            resume_uploaded=False,
        )
        assert page.locator("#li").get_attribute("aria-checked") == "true"
        assert page.locator("#jb").get_attribute("aria-checked") == "false"
    finally:
        page.close()


_NESTED_SEARCH = """
<div data-automation-id="multiSelectContainer">
  <ul data-automation-id="selectedItemList">
    <li id="picked" data-automation-id="selectedItem" hidden></li>
  </ul>
  <input id="source" data-automation-id="searchBox" role="combobox" aria-label="How did you hear about us?"
    aria-expanded="false">
  <button type="button" id="source-prompt" data-automation-id="promptIcon" style="width:24px;height:24px"></button>
</div>
<script>
  const input = document.getElementById("source");
  function openTop() {
    input.setAttribute("aria-expanded", "true");
    const old = document.getElementById("source-menu");
    if (old) old.remove();
    const menu = document.createElement("div");
    menu.id = "source-menu";
    const category = document.createElement("div");
    category.id = "job-board";
    category.setAttribute("data-automation-id", "promptOption");
    category.style.height = "24px";
    category.textContent = "Job Board";
    const chevron = document.createElement("span");
    chevron.setAttribute("data-automation-id", "promptOptionChevron");
    chevron.className = "chevron";
    category.appendChild(chevron);
    category.addEventListener("click", () => {
      category.remove();
      const list = document.createElement("ul");
      list.id = "nested";
      list.setAttribute("role", "listbox");
      list.style.cssText = "display:block;min-height:24px";
      menu.appendChild(list);
    });
    menu.appendChild(category);
    document.body.appendChild(menu);
  }
  input.addEventListener("click", openTop);
  input.addEventListener("input", () => {
    const list = document.getElementById("nested");
    if (!list) return;
    list.replaceChildren();
    if (String(input.value || "").toLowerCase().indexOf("linkedin") === -1) return;
    const leaf = document.createElement("li");
    leaf.id = "linkedin";
    leaf.setAttribute("role", "option");
    leaf.style.height = "24px";
    leaf.textContent = "LinkedIn";
    leaf.addEventListener("click", () => {
      const picked = document.getElementById("picked");
      picked.hidden = false;
      picked.textContent = "LinkedIn";
      input.value = "";
      input.setAttribute("aria-expanded", "false");
      const menu = document.getElementById("source-menu");
      if (menu) menu.remove();
    });
    list.appendChild(leaf);
  });
  document.getElementById("source-prompt").addEventListener("click", openTop);
</script>
"""


def test_hear_about_nested_search_types_the_leaf_and_keeps_it(browser):
    from autofill.combobox import select_combobox
    from autofill.safeguards import HUMAN_SUBMIT_ONLY

    assert HUMAN_SUBMIT_ONLY is True
    page = browser.new_page()
    try:
        page.set_content(_NESTED_SEARCH)
        picked = select_combobox(page, "#source", "LinkedIn")
        assert picked is not None
        assert page.locator("#picked").inner_text().strip() == "LinkedIn"
        assert page.locator("#source").input_value() == ""
    finally:
        page.close()
