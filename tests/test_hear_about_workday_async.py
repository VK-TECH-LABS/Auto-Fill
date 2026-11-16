"""Workday Hear About as KBR renders it.

- The leaf pill is painted a few frames after the click, and the multiselect
  menu can stay open.
- The phone-code pill list on the same page is ``role=listbox``, which is not a
  menu.
- The saved answer can be ``Parent | Leaf`` with no ``leaf`` field.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from autofill.combobox import _MENU_OPEN_JS, select_combobox
from autofill.profile import CandidateProfile
from autofill.tilearc_sync import parse_read_body

_PAGE = """
<main>
  <h1>My Information</h1>
  <div data-automation-id="formField-phoneCode">
    <label>Country Phone Code*</label>
    <ul data-automation-id="selectedItemList" role="listbox">
      <li data-automation-id="selectedItem" role="option">United States of America (+1)</li>
    </ul>
  </div>
  <div data-automation-id="formField-source">
    <label for="source">How Did You Hear About Us?<abbr>*</abbr></label>
    <div data-automation-id="multiselectInputContainer">
      <ul data-automation-id="selectedItemList" role="listbox" aria-label="items selected">
        <li id="picked" data-automation-id="selectedItem" role="option" hidden></li>
      </ul>
      <input id="source" data-automation-id="searchBox" role="combobox" aria-required="true"
        placeholder="Search" aria-expanded="false" aria-describedby="source-state">
      <span id="source-state">Minimized</span>
    </div>
  </div>
  <script>
    const input = document.getElementById("source");
    function leaf(label) {
      const row = document.createElement("div");
      row.setAttribute("data-automation-id", "promptOption");
      row.setAttribute("data-autofill-folder", "false");
      row.setAttribute("role", "option");
      row.style.height = "24px";
      row.textContent = label;
      row.addEventListener("click", () => {
        // Workday commits on a later frame and leaves the multiselect open.
        window.setTimeout(() => {
          const picked = document.getElementById("picked");
          picked.hidden = false;
          picked.textContent = label;
          input.value = "";
        }, 180);
      });
      return row;
    }
    function openTop() {
      input.setAttribute("aria-expanded", "true");
      if (document.getElementById("source-menu")) return;
      const menu = document.createElement("div");
      menu.id = "source-menu";
      menu.setAttribute("role", "listbox");
      const parent = document.createElement("div");
      parent.setAttribute("data-automation-id", "promptOption");
      parent.setAttribute("role", "option");
      parent.style.height = "24px";
      parent.textContent = "Job Board or Social Media";
      parent.addEventListener("click", () => {
        window.setTimeout(() => {
          parent.remove();
          menu.appendChild(leaf("Indeed"));
          menu.appendChild(leaf("LinkedIn"));
        }, 120);
      });
      menu.appendChild(parent);
      menu.appendChild(leaf("KBR Website"));
      document.body.appendChild(menu);
    }
    input.addEventListener("click", openTop);
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      const menu = document.getElementById("source-menu");
      if (menu) menu.remove();
      input.setAttribute("aria-expanded", "false");
    });
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


def test_pipe_joined_answer_is_a_parent_leaf_path():
    profile = CandidateProfile.from_dict(
        {
            "candidateId": "cand-jahnavi",
            "personal": {"first_name": "Jahnavi", "last_name": "Torra", "email": "j@example.com"},
            "applicationAnswers": [
                {"question": "How did you hear about this position?", "answer": "Job Board or Social Media | LinkedIn"}
            ],
        }
    )
    assert profile.screening.how_heard == "Job Board or Social Media > LinkedIn"


def test_referral_source_wording_is_hear_about():
    profile = CandidateProfile.from_dict(
        {
            "candidateId": "cand-jahnavi",
            "personal": {"first_name": "Jahnavi", "last_name": "Torra", "email": "j@example.com"},
            "applicationAnswers": [{"question": "Referral Source", "answer": "KBR Website"}],
        }
    )
    assert profile.screening.how_heard == "KBR Website"


def test_pill_list_listbox_is_not_an_open_menu(browser):
    page = browser.new_page()
    try:
        page.set_content(_PAGE)
        assert page.evaluate(_MENU_OPEN_JS) is False
    finally:
        page.close()


def test_async_leaf_pill_with_open_menu_is_committed(browser):
    page = browser.new_page()
    try:
        page.set_content(_PAGE)
        chosen = select_combobox(page, "#source", "Job Board or Social Media > LinkedIn")
        assert chosen is not None
        assert page.locator("#picked").inner_text().strip() == "LinkedIn"
    finally:
        page.close()


def test_tilearc_found_read_back_is_trusted_shape():
    ack = parse_read_body(
        {
            "found": True,
            "candidateId": "cand-jahnavi",
            "keys": ["k"],
            "version": 2,
            "updatedAt": "2026-11-13T00:00:00Z",
            "readBack": {"key": "k", "question": "Veteran status", "answer": "I AM NOT A VETERAN", "version": 2},
        }
    )
    assert ack is not None and ack.verified and ack.candidate_id == "cand-jahnavi"
    assert ack.read_back is not None and ack.read_back.answer == "I AM NOT A VETERAN"
    assert parse_read_body({"found": False, "candidateId": "cand-jahnavi", "readBack": None}) is None
    other = {"found": True, "candidateId": "cand-a", "readBack": {"candidateId": "cand-b", "answer": "x"}}
    assert parse_read_body(other) is None
