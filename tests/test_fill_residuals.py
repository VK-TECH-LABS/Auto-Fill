"""Shared fill residuals: calling-code labels, nested menus, stale invalid, empty required."""

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
            "candidateId": "cand-residual",
            "personal": {
                "full_name": "Casey Example",
                "email": "casey.example@example.com",
                "phone": "+1 (917) 555-0100",
                "country": "United States",
            },
            "address": {
                "line1": "100 Broadway",
                "city": "New York",
                "state": "NY",
                "postalCode": "10001",
                "country": "US",
            },
        }
    )


_CLARK_PHONE = """
<div id="contact">
  <label>Postal Code</label>
  <div id="code-wrap">
    <input id="code" data-automation-id="searchBox" role="combobox" aria-label="Search"
      placeholder="Search" aria-required="true">
  </div>
  <input id="postal" required>
  <label for="phone">Phone Number</label>
  <input id="phone" type="tel" required>
</div>
"""


def test_calling_code_beside_postal_label_is_not_postal_code(browser):
    """A search box between the Postal Code title and the postal input is the phone country."""
    from autofill.extract import extract_page
    from autofill.mapping import map_field

    page = browser.new_page()
    try:
        page.set_content(_CLARK_PHONE)
        found = {control.element_id: control for control in extract_page(page).controls}
        code = found["code"]
        assert "postal" not in code.label.casefold()
        assert code.phone_adjacent is True
        assert map_field(code, _person()).key == "phone_country"
        assert map_field(found["postal"], _person()).key == "postal_code"
        assert map_field(found["phone"], _person()).key == "phone"
    finally:
        page.close()


def test_nested_listbox_under_an_open_prompt_is_selected(browser):
    """The second level is a role=listbox, not a promptLeafNode. The leaf still commits."""
    from autofill.combobox import select_combobox
    from autofill.safeguards import HUMAN_SUBMIT_ONLY

    assert HUMAN_SUBMIT_ONLY is True
    page = browser.new_page()
    try:
        page.set_content(
            """
            <div data-automation-id="multiSelectContainer">
              <ul data-automation-id="selectedItemList">
                <li id="picked" data-automation-id="selectedItem" hidden></li>
              </ul>
              <input id="source" data-automation-id="searchBox" role="combobox" aria-label="Search"
                aria-expanded="false">
              <button type="button" id="source-prompt" data-automation-id="promptIcon"
                style="width:24px;height:24px"></button>
            </div>
            <script>
              const input = document.getElementById("source");
              function openTop() {
                input.setAttribute("aria-expanded", "true");
                if (document.getElementById("source-menu")) return;
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
                    menu.remove();
                  });
                  list.appendChild(leaf);
                  menu.appendChild(list);
                });
                menu.appendChild(category);
                document.body.appendChild(menu);
              }
              input.addEventListener("click", openTop);
              document.getElementById("source-prompt").addEventListener("click", openTop);
            </script>
            """
        )
        picked = select_combobox(page, "#source", "LinkedIn")
        assert picked is not None
        assert page.locator("#picked").inner_text().strip() == "LinkedIn"
    finally:
        page.close()


def test_prompt_search_text_without_a_pill_is_not_committed(browser):
    from autofill.dom_ui import control_committed

    page = browser.new_page()
    try:
        page.set_content(
            """
            <div data-automation-id="multiSelectContainer">
              <input id="source" data-automation-id="searchBox" role="combobox" value="Social">
            </div>
            """
        )
        assert control_committed(page, "#source") is False
        page.evaluate(
            """() => {
              const list = document.createElement("ul");
              list.setAttribute("data-automation-id", "selectedItemList");
              const item = document.createElement("li");
              item.setAttribute("data-automation-id", "selectedItem");
              item.textContent = "Social Media";
              list.appendChild(item);
              document.querySelector("[data-automation-id='multiSelectContainer']").appendChild(list);
            }"""
        )
        assert control_committed(page, "#source") is True
    finally:
        page.close()


def test_satisfied_phone_clears_a_stale_required_error(browser):
    """A value that stayed on the box is not left aria-invalid with a required message."""
    from autofill.fill import fill_one_page

    page = browser.new_page()
    try:
        page.set_content(
            """
            <div data-automation-id="formField-phone">
              <label for="phone">Phone Number</label>
              <input id="phone" type="tel" required aria-invalid="true">
              <div id="err" role="alert">The field Phone Number is required and must have a value.</div>
            </div>
            """
        )
        fill_one_page(
            page,
            _person(),
            cover_letter_path=None,
            cover_letter_text=None,
            job=None,
            resume_uploaded=False,
        )
        assert page.locator("#phone").input_value().strip() != ""
        assert page.locator("#phone").get_attribute("aria-invalid") is None
        assert page.locator("#err").count() == 0
    finally:
        page.close()


def test_long_required_listbox_blocks_save_and_continue(browser):
    """A question longer than a short label is still required. Next is not pressed while it is Select One."""
    from autofill.engine import AutofillOptions, Status, autofill_application
    from autofill.safeguards import HUMAN_SUBMIT_ONLY

    assert HUMAN_SUBMIT_ONLY is True
    question = (
        "General Motors will need to contact you throughout the recruitment process, "
        "this may include SMS/text messaging from GM and/or GM's third party partners "
        "(message and data rates may apply). Do you agree to communications via text message/SMS "
        "for this application and for later recruiting messages about this role?"
    )
    assert len(question) > 240
    page = browser.new_page()
    try:
        page.set_content(
            f"""
            <h1>Application Questions</h1>
            <div>
              <div id="sms-q">{question}</div>
              <button id="sms" type="button" aria-haspopup="listbox" aria-required="true">Select One</button>
            </div>
            <button type="button" id="save">Save and Continue</button>
            <script>
              window.__next = 0;
              document.getElementById("save").addEventListener("click", () => {{ window.__next += 1; }});
            </script>
            """
        )
        result = autofill_application(
            "https://itw.wd5.myworkdayjobs.com/en-US/ITW_Careers/job/example/apply",
            _person(),
            None,
            AutofillOptions(page=page, session_id="long-q", candidate_id="cand-residual", max_pages=2),
        )
        assert page.locator("#sms").inner_text().strip() == "Select One"
        prompts = " ".join(str(item.get("text") or "") for item in result.manual_questions).casefold()
        assert "text message" in prompts
        assert result.status == Status.MANUAL_ANSWER_REQUIRED
        assert page.evaluate("() => window.__next") == 0
    finally:
        page.close()
