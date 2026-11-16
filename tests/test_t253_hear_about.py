"""t253u Hear About leaf commit. Fixtures only. No live employer sites.

FAIL-HEAR-LEAF: a highlight, search string, open menu, or parent is not a commit.
FAIL-HEAR-AUTO: an approved leaf or sourceUrl host fills Hear About with no popup.
FAIL-HEAR-UNKNOWN: no saved answer and no sourceUrl stays a popup of leaves, not parents.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from autofill import AutofillOptions, Status, autofill_application
from autofill.combobox import match_source_host, select_combobox
from autofill.dom_ui import control_committed
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


_MENU = """
<main>
  <h1>My Information</h1>
  <div data-automation-id="legalNameSection">
    <div>First Name<abbr>*</abbr></div>
    <input id="first" data-automation-id="legalNameSection_firstName" aria-required="true">
    <div>Last Name<abbr>*</abbr></div>
    <input id="last" data-automation-id="legalNameSection_lastName" aria-required="true">
  </div>
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
    const input = document.getElementById("source");
    function leaf(label) {
      const row = document.createElement("div");
      row.setAttribute("data-automation-id", "promptOption");
      row.setAttribute("data-autofill-folder", "false");
      row.style.height = "24px";
      row.textContent = label;
      row.addEventListener("click", () => {
        const picked = document.getElementById("picked");
        picked.hidden = false;
        picked.textContent = label;
        input.value = "";
        input.setAttribute("aria-expanded", "false");
        const menu = document.getElementById("source-menu");
        if (menu) menu.remove();
      });
      return row;
    }
    function openTop() {
      input.setAttribute("aria-expanded", "true");
      if (document.getElementById("source-menu")) return;
      const menu = document.createElement("div");
      menu.id = "source-menu";
      const parent = document.createElement("div");
      parent.id = "job-board";
      parent.setAttribute("data-automation-id", "promptOption");
      parent.style.height = "24px";
      parent.textContent = "Job Board or Social Media";
      parent.addEventListener("click", () => {
        parent.setAttribute("aria-selected", "true");
        input.value = "Job Board or Social Media";
        window.setTimeout(() => {
          parent.remove();
          menu.appendChild(leaf("Indeed"));
          menu.appendChild(leaf("LinkedIn"));
        }, 120);
      });
      menu.appendChild(parent);
      menu.appendChild(leaf("Applied Materials Corporate Website"));
      document.body.appendChild(menu);
    }
    input.addEventListener("click", openTop);
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      const menu = document.getElementById("source-menu");
      if (menu) menu.remove();
      input.value = "";
      input.setAttribute("aria-expanded", "false");
    });
  </script>
</main>
"""


def test_live_answer_row_uses_the_leaf_path_and_not_the_apply_url():
    profile = CandidateProfile.from_dict(
        {
            "candidateId": "cand-jahnavi",
            "personal": {"first_name": "Jahnavi", "last_name": "Torra", "email": EMAIL},
            "applicationAnswers": [
                {
                    "question": "How Did You Hear About Us?",
                    "answer": "Job Board or Social Media | Indeed",
                    "values": ["Job Board or Social Media", "Indeed"],
                    "leaf": {"id": "indeed", "label": "Indeed"},
                    "leaves": [
                        {"id": "board", "label": "Job Board or Social Media"},
                        {"id": "indeed", "label": "Indeed"},
                    ],
                }
            ],
        }
    )
    assert profile.screening.how_heard == "Job Board or Social Media > Indeed"
    assert profile.application_answers[0].answer == "Job Board or Social Media > Indeed"
    assert profile.source_url == ""
    bare = _person()
    assert bare.screening.how_heard == ""
    assert bare.application_answers == []
    assert match_source_host("https://amat.wd1.myworkdayjobs.com/en-US/job/1", ["Indeed", "LinkedIn"]) == ""
    assert (
        match_source_host(
            "https://www.linkedin.com/jobs/view/1",
            ["Job Board or Social Media > Indeed", "Job Board or Social Media > LinkedIn"],
        )
        == "Job Board or Social Media > LinkedIn"
    )


def test_highlight_and_parent_are_not_a_committed_leaf(browser):
    page = browser.new_page()
    try:
        page.set_content(_MENU)
        page.locator("#source").click()
        page.locator("#job-board").click()
        assert control_committed(page, "#source") is False
        missed = select_combobox(page, "#source", "Job Board or Social Media")
        assert missed is None
        assert page.locator("#picked").inner_text().strip() == ""
        assert page.evaluate("() => document.getElementById('source-menu') === null") is True
    finally:
        page.close()


def test_approved_leaf_is_committed_and_not_asked(browser):
    page = browser.new_page()
    try:
        page.set_content(_MENU)
        profile = _person(
            applicationAnswers=[
                {
                    "question": "How did you hear about this job?",
                    "answer": "Indeed",
                    "leaf": {"id": "indeed", "label": "Indeed"},
                    "leaves": [
                        {"id": "board", "label": "Job Board or Social Media"},
                        {"id": "indeed", "label": "Indeed"},
                    ],
                }
            ]
        )
        result = autofill_application(
            "https://amat.wd1.myworkdayjobs.com/en-US/External/job/Austin/apply",
            profile,
            None,
            AutofillOptions(page=page, candidate_id="cand-jahnavi", session_id="t253-hear"),
        )
        assert page.locator("#first").input_value() == "Jahnavi"
        assert page.locator("#last").input_value() == "Torra"
        assert page.locator("#picked").inner_text().strip() == "Indeed"
        assert page.evaluate("() => document.getElementById('source-menu') === null") is True
        texts = " ".join(str(item.get("text") or "") for item in result.open_items).casefold()
        assert "how did you hear" not in texts
        assert "first name" not in texts
        assert "last name" not in texts
        assert result.status != Status.MANUAL_ANSWER_REQUIRED or "how did you hear" not in texts
    finally:
        page.close()


def test_unknown_hear_about_asks_leaves_and_closes_the_menu(browser):
    page = browser.new_page()
    try:
        page.set_content(_MENU)
        result = autofill_application(
            "https://amat.wd1.myworkdayjobs.com/en-US/External/job/Austin/apply",
            _person(),
            None,
            AutofillOptions(page=page, candidate_id="cand-jahnavi", session_id="t253-unknown"),
        )
        assert page.locator("#picked").inner_text().strip() == ""
        assert page.evaluate("() => document.getElementById('source-menu') === null") is True
        hear = [item for item in result.open_items if "how did you hear" in str(item.get("text") or "").casefold()]
        assert hear
        options = [str(option) for option in hear[0].get("options") or []]
        assert any("Indeed" in option for option in options)
        assert "Job Board or Social Media" not in options
        assert result.status == Status.MANUAL_ANSWER_REQUIRED
    finally:
        page.close()


def test_source_url_host_commits_the_matching_leaf(browser):
    page = browser.new_page()
    try:
        page.set_content(_MENU)
        profile = _person()
        profile.source_url = "https://www.linkedin.com/jobs/view/99"
        result = autofill_application(
            "https://amat.wd1.myworkdayjobs.com/en-US/External/job/Austin/apply",
            profile,
            None,
            AutofillOptions(page=page, candidate_id="cand-jahnavi", session_id="t253-source"),
        )
        assert page.locator("#picked").inner_text().strip() == "LinkedIn"
        texts = " ".join(str(item.get("text") or "") for item in result.open_items).casefold()
        assert "how did you hear" not in texts
    finally:
        page.close()
