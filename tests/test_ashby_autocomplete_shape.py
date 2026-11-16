"""Ashby application controls as the live Base Power form renders them (2026-11-13).

- ``role=combobox`` autocompletes ignore clicks and open on ArrowDown or typing.
- Search results arrive in waves and React reuses row nodes, so a row's text
  can change after it was read.
- "How did you hear about us?" is a checkbox group whose option is
  "Base Power Company website", which is a real answer, not a honeypot.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from autofill.combobox import list_combobox_options, select_combobox
from autofill.mapping import is_honeypot
from autofill.models import Control

_AUTH = [
    "I am a U.S. citizen or permanent resident (Green Card holder)",
    "I currently hold an H-1B visa and would need a transfer to Base",
    "I am not currently authorized to work in the U.S.",
]

_PAGE = """
<main>
  <div data-field-path="auth"><label>Are you legally authorized to work in the U.S.?*</label>
    <input id="auth" role="combobox" aria-autocomplete="list" aria-expanded="false" placeholder="Start typing..."></div>
  <div data-field-path="loc"><label>Current Location*</label>
    <input id="loc" role="combobox" aria-autocomplete="list" aria-expanded="false" placeholder="Start typing..."></div>
  <div id="popup"></div>
  <script>
    const AUTH = %AUTH%;
    const popup = document.getElementById("popup");
    function draw(input, labels) {
      // Rows are reused in place, like React keyed by index.
      let list = popup.querySelector("[role=listbox]");
      if (!list) {
        list = document.createElement("div");
        list.setAttribute("role", "listbox");
        popup.appendChild(list);
      }
      while (list.children.length > labels.length) list.lastChild.remove();
      labels.forEach((label, i) => {
        let row = list.children[i];
        if (!row) {
          row = document.createElement("div");
          row.setAttribute("role", "option");
          row.id = ":r" + i + ":";
          row.style.height = "20px";
          row.addEventListener("mousedown", (event) => {
            event.preventDefault();
            const owner = document.getElementById(list.dataset.owner);
            owner.value = row.textContent;
            owner.dataset.committed = row.textContent;
            owner.setAttribute("aria-expanded", "false");
            list.remove();
          });
          list.appendChild(row);
        }
        row.textContent = label;
      });
      list.dataset.owner = input.id;
      input.setAttribute("aria-expanded", "true");
      // Live Ashby highlights the first row; Enter commits it.
      if (list.children[0]) input.setAttribute("aria-activedescendant", list.children[0].id);
    }
    function close(input) {
      const list = popup.querySelector("[role=listbox]");
      if (list) list.remove();
      input.setAttribute("aria-expanded", "false");
      // An autocomplete keeps only a committed row.
      if (input.value !== (input.dataset.committed || "")) input.value = input.dataset.committed || "";
    }
    const auth = document.getElementById("auth");
    auth.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") draw(auth, AUTH);
      if (event.key === "Escape") close(auth);
    });
    auth.addEventListener("input", () => {
      const query = auth.value.toLowerCase();
      draw(auth, AUTH.filter((label) => label.toLowerCase().includes(query)));
    });
    auth.addEventListener("blur", () => setTimeout(() => close(auth), 0));
    const loc = document.getElementById("loc");
    loc.addEventListener("input", () => {
      if (!loc.value) return;
      // First wave: Illinois on top. Second wave 250 ms later: Georgia on top.
      draw(loc, ["Atlanta, Illinois, United States", "Atlanta, Georgia, United States"]);
      setTimeout(() => draw(loc, ["Atlanta, Georgia, United States", "Atlanta, Illinois, United States"]), 250);
    });
    loc.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        const first = popup.querySelector("[role=option]");
        if (first) first.dispatchEvent(new MouseEvent("mousedown", { bubbles: true }));
      }
      if (event.key === "Escape") close(loc);
    });
    loc.addEventListener("blur", () => setTimeout(() => close(loc), 0));
  </script>
</main>
""".replace("%AUTH%", str(_AUTH).replace("'", '"'))


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


def test_keyboard_only_autocomplete_lists_its_options(browser):
    page = browser.new_page()
    try:
        page.set_content(_PAGE)
        assert list_combobox_options(page, "#auth") == _AUTH
        assert page.locator("#auth").input_value() == ""
    finally:
        page.close()


def test_saved_option_is_committed_in_keyboard_only_autocomplete(browser):
    page = browser.new_page()
    try:
        page.set_content(_PAGE)
        chosen = select_combobox(page, "#auth", _AUTH[0])
        assert chosen is not None
        assert page.locator("#auth").get_attribute("data-committed") == _AUTH[0]
    finally:
        page.close()


def test_reordered_results_never_commit_a_different_city(browser):
    page = browser.new_page()
    try:
        page.set_content(_PAGE)
        chosen = select_combobox(page, "#loc", "Atlanta, Georgia, United States")
        committed = page.locator("#loc").get_attribute("data-committed")
        assert committed != "Atlanta, Illinois, United States"
        assert chosen is not None and committed == "Atlanta, Georgia, United States"
    finally:
        page.close()


def test_company_website_option_is_not_a_honeypot():
    group = "How did you hear about Base Power Company? Base Power Company website LinkedIn"
    option = Control(kind="checkbox", label="Base Power Company website", group=group)
    question = Control(kind="textarea", label="How did you hear about Base Power Company?", group=group)
    trap = Control(kind="text", label="Company Website", name="company_website")
    assert not is_honeypot(option)
    assert not is_honeypot(question)
    assert is_honeypot(trap)
