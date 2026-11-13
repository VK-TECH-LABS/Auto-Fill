"""Failures from Windows Chrome testing on 2026-11-13. Fixtures only; no live employer sites.

- Only mandatory fields are filled or asked about when the page marks which fields are mandatory.
- Ashby Fluidstack: travel, work-visa, and start questions are known intents, not popups.
- Danaher Workday: Phone Device Type and the second Degree stayed on "Select One".
- Booz Allen Workday: a category with no matching leaf left the menu stuck inside it.
- HPE Create Account: the consent popup said only "Required checkbox".
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from autofill.account_flow import _consent_statement, _statement
from autofill.combobox import select_combobox
from autofill.intents import classify_question
from autofill.mapping import _degree_option, phone_device_label
from autofill.models import Control, Option, PageSnapshot
from autofill.stepfill import _clusters


def _snapshot(*controls: Control) -> PageSnapshot:
    return PageSnapshot(controls=list(controls), buttons=[])


def _labels(snapshot: PageSnapshot) -> list[str]:
    return [group[0].label for _, group in _clusters(snapshot)]


def test_optional_fields_are_ignored_when_the_page_marks_required_ones():
    snapshot = _snapshot(
        Control(kind="text", label="First Name", required=True, selector="#first"),
        Control(kind="text", label="LinkedIn", selector="#linkedin"),
        Control(kind="text", label="Phone Extension", selector="#ext"),
        Control(kind="buttons", role="listbox", label="Phone Device Type", required=True, selector="#device"),
        Control(kind="combobox", label="Field of Study", selector="#study"),
    )
    assert _labels(snapshot) == ["First Name", "Phone Device Type"]


def test_a_radio_group_with_one_required_member_is_kept():
    snapshot = _snapshot(
        Control(kind="text", label="Email", required=True, selector="#email"),
        Control(
            kind="radio", label="Yes", name="travel", group="Are you open to travel?", required=True, selector="#y"
        ),
        Control(kind="radio", label="No", name="travel", group="Are you open to travel?", selector="#n"),
        Control(kind="radio", label="Yes", name="pron", group="Pronouns", selector="#p1"),
        Control(kind="radio", label="No", name="pron", group="Pronouns", selector="#p2"),
    )
    kept = [[c.selector for c in group] for _, group in _clusters(snapshot)]
    assert ["#email"] in kept
    assert any("#y" in group for group in kept)
    assert not any("#p1" in group for group in kept)


def test_a_page_that_marks_nothing_required_keeps_every_field():
    snapshot = _snapshot(
        Control(kind="text", label="First Name", selector="#first"),
        Control(kind="text", label="LinkedIn", selector="#linkedin"),
    )
    assert _labels(snapshot) == ["First Name", "LinkedIn"]


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        ("Are you open to travel?", "TRAVEL_PERCENT"),
        ("I will now or in the future need assistance with a work visa.", "SPONSORSHIP_NOW_OR_FUTURE"),
        ("If presented with an offer, when would be the earliest you would be available to start?", "START_DATE"),
        ("I am authorized to work in the United States.", "US_WORK_AUTHORIZATION"),
        ("Are you open to relocation?", "RELOCATE"),
    ],
)
def test_ashby_screening_wordings_match_saved_answers(question, intent):
    match = classify_question(question, us_on_page=True)
    assert (match.intent, match.confidence) == (intent, "HIGH")


def _options(*labels: str) -> list[Option]:
    return [Option(value=label, label=label) for label in labels]


def test_phone_device_type_uses_the_employers_mobile_wording():
    assert phone_device_label(_options("Home", "Mobile", "Work")) == "Mobile"
    assert phone_device_label(_options("Home", "Work Mobile", "Personal Cell")) == "Personal Cell"
    assert phone_device_label(_options("Landline", "Cell Phone")) == "Cell Phone"


def test_second_degree_matches_one_option_and_never_a_different_level():
    menu = _options("High School", "Bachelor of Arts", "Bachelor of Science", "Bachelor's Degree", "Master's Degree")
    assert _degree_option("B.Tech", menu).label == "Bachelor's Degree"
    assert _degree_option("Bachelor of Technology", menu).label == "Bachelor's Degree"
    assert _degree_option("Bachelor of Science in Computer Science", menu).label == "Bachelor of Science"
    assert _degree_option("M.Tech", menu).label == "Master's Degree"
    assert _degree_option("B.Tech", _options("Associate", "Master")) is None


def test_bare_asterisk_consent_box_shows_its_declaration():
    box = Control(kind="checkbox", label="*", selector="#agree", required=True)
    assert _statement(box) == ""

    class _Page:
        def evaluate(self, script, selector):
            assert selector == "#agree"
            return "I declare that I have read the HPE Recruitment Notice and agree to HPE processing my personal data."

    assert _consent_statement(_Page(), box).startswith("I declare that I have read the HPE Recruitment Notice")


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


# Workday keeps a nested prompt on the level it was left at. Escape first
# clears typed search text, then steps up one level, then closes the menu.
_STUCK_MENU = """
<main>
  <div data-automation-id="formField-source">
    <label for="source">How Did You Hear About Us?<abbr>*</abbr></label>
    <div data-automation-id="multiSelectContainer">
      <ul data-automation-id="selectedItemList">
        <li id="picked" data-automation-id="selectedItem" hidden></li>
      </ul>
      <input id="source" data-automation-id="searchBox" role="combobox" aria-required="true"
        placeholder="Search" aria-expanded="false">
    </div>
  </div>
  <script>
    const TREE = {"Booz Allen Alum": ["Booz Allen Alum"], "Job Board": ["Indeed", "LinkedIn"]};
    const input = document.getElementById("source");
    let level = null;
    function close() {
      const menu = document.getElementById("source-menu");
      if (menu) menu.remove();
      input.setAttribute("aria-expanded", "false");
    }
    function row(label, isLeaf) {
      const el = document.createElement("div");
      el.setAttribute("data-automation-id", "promptOption");
      if (isLeaf) el.setAttribute("data-autofill-folder", "false");
      el.style.height = "24px";
      el.textContent = label;
      el.addEventListener("click", () => {
        if (!isLeaf) { level = label; input.value = ""; window.setTimeout(draw, 80); return; }
        const picked = document.getElementById("picked");
        picked.hidden = false;
        picked.textContent = label;
        input.value = "";
        level = null;
        close();
      });
      return el;
    }
    function draw() {
      let menu = document.getElementById("source-menu");
      if (!menu) { menu = document.createElement("div"); menu.id = "source-menu"; document.body.appendChild(menu); }
      menu.innerHTML = "";
      input.setAttribute("aria-expanded", "true");
      const query = input.value.toLowerCase();
      if (level === null) {
        Object.keys(TREE).forEach((name) => menu.appendChild(row(name, false)));
        return;
      }
      TREE[level]
        .filter((name) => name.toLowerCase().includes(query))
        .forEach((name) => menu.appendChild(row(name, true)));
    }
    input.addEventListener("click", draw);
    input.addEventListener("input", draw);
    input.addEventListener("keydown", (event) => { if (event.key === "ArrowDown") draw(); });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (input.value) { input.value = ""; draw(); return; }
      if (level !== null) { level = null; draw(); return; }
      close();
    });
  </script>
</main>
"""


def test_nested_hear_about_leaves_a_non_matching_category_before_the_next(browser):
    page = browser.new_page()
    try:
        page.set_content(_STUCK_MENU)
        chosen = select_combobox(page, "#source", "LinkedIn")
        assert chosen is not None
        assert page.locator("#picked").inner_text() == "LinkedIn"
    finally:
        page.close()
