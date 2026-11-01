"""After the last required field, Next advances and Final Submit stays human.

A review step whose heading does not say Review, and whose only control is
Submit application, is READY_FOR_HUMAN_SUBMIT. Enter and a mislabeled click
must not set ``window.__submitted``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autofill.engine import Status
from autofill.local_attach import DedicatedApplySession, VisibleTab, start_fixture_server
from autofill.profile import CandidateProfile
from autofill.safeguards import HumanSubmissionRequired, hold_final_submit, perform_click

ROOT = Path(__file__).resolve().parents[1]
PROFILE = ROOT / "examples" / "profile.example.json"

_PAGE = """<!DOCTYPE html>
<html><body>
<form id="application" onsubmit="window.__submitted = true; return false;">
  <section id="p1">
    <h1>My information</h1>
    <label for="country">Country *</label>
    <select id="country" name="country" required>
      <option value="">Select</option>
      <option>United States</option>
      <option>Canada</option>
    </select>
    <button type="button" id="next1">Save &amp; Continue</button>
  </section>
  <section id="p2" hidden>
    <h1>Questions</h1>
    <label><input type="checkbox" id="tos" required> I agree to the Terms of Service and Privacy Policy</label>
    <fieldset>
      <legend>Are you legally authorized to work in the United States? *</legend>
      <label><input type="radio" name="auth" id="auth-yes" value="Yes" required> Yes</label>
      <label><input type="radio" name="auth" id="auth-no" value="No"> No</label>
    </fieldset>
    <label for="level">Experience level *</label>
    <select id="level" name="level" required>
      <option value="">Select</option>
      <option>Internship</option>
      <option>Entry level</option>
      <option>Mid-level</option>
      <option>Senior</option>
    </select>
    <button type="button" id="next2">Save &amp; Continue</button>
  </section>
  <section id="review" hidden>
    <h1>Check and send</h1>
    <button type="submit" id="final">Submit application</button>
  </section>
</form>
<script>
  window.__submitted = false;
  window.__clicked = [];
  document.getElementById("final").addEventListener("click", () => {
    window.__submitted = true;
    window.__clicked.push("Submit application");
  });
  function turn(id, show) {
    document.getElementById(id).addEventListener("click", () => {
      window.__clicked.push("Save & Continue");
      document.getElementById("p1").hidden = true;
      document.getElementById("p2").hidden = id !== "next1";
      document.getElementById("review").hidden = id !== "next2";
      if (show) document.getElementById(show).hidden = false;
    });
  }
  turn("next1", "p2");
  turn("next2", "review");
</script>
</body></html>
"""


def _launch(playwright):
    try:
        return playwright.chromium.launch(headless=True)
    except Exception:
        return playwright.chromium.launch(headless=True, executable_path="/opt/google/chrome/chrome")


@pytest.fixture
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        pytest.skip(f"Playwright is not installed: {exc}")
    try:
        playwright = sync_playwright().start()
        chrome = _launch(playwright)
    except Exception as exc:
        pytest.skip(f"Chromium is not installed: {exc}")
    try:
        yield chrome
    finally:
        chrome.close()
        playwright.stop()


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(json.loads(PROFILE.read_text(encoding="utf-8")))


def _session(playwright, browser, page, url: str) -> DedicatedApplySession:
    session = DedicatedApplySession(user_data_dir=Path("/tmp/last-field-submit"), debug_port=9)
    session._playwright = playwright
    session.tab = VisibleTab(
        browser=browser,
        context=page.context,
        page=page,
        endpoint="http://127.0.0.1:9",
        target_id="t",
        page_url=url,
        context_count=1,
        page_urls=[url],
    )
    return session


def test_live_label_refuses_submit_application_when_the_caller_says_next():
    class _Page:
        def evaluate(self, _script, selector):
            if selector == "#final":
                return {"name": "Submit application", "controlType": "submit"}
            return {"name": "Save & Continue", "controlType": "button"}

        def locator(self, _selector):
            raise AssertionError("clicked")

    page = _Page()
    with pytest.raises(HumanSubmissionRequired):
        perform_click(page, "#final", "Save & Continue", control_type="button", purpose="navigation")


def test_last_required_field_advances_and_does_not_click_submit(browser, monkeypatch):
    monkeypatch.setenv("AUTOFILL_LOCAL_ATTACH", "1")
    server = start_fixture_server(_PAGE.encode())
    url = f"http://127.0.0.1:{server.server_address[1]}/t172-local-tab.html"
    profile = _profile()
    try:
        page = browser.new_page()
        try:
            page.goto(url)
            session = _session(object(), browser, page, url)
            result, submitted = session.fill(url, profile, standard_terms_authorized=True, fresh=False)
            assert submitted is not True
            assert result.status == Status.MANUAL_ANSWER_REQUIRED
            assert result.open_items[0]["text"] == "Country"
            country = session.apply_answer(
                url,
                profile,
                {
                    "selector": "#country",
                    "answerType": "single_select",
                    "value": "United States",
                    "values": ["United States"],
                },
            )
            assert country["verified"] is True
            assert country["result"].pages_advanced == 1
            assert country["result"].last_navigation == {"action": "next", "result": "advanced"}
            assert [item["text"] for item in country["result"].open_items] == ["Experience level"]
            experience = session.apply_answer(
                url,
                profile,
                {
                    "selector": "#level",
                    "answerType": "single_select",
                    "value": "Senior",
                    "values": ["Senior"],
                },
            )
            assert experience["verified"] is True
            ready = experience["result"]
            assert ready.status == Status.READY_FOR_HUMAN_SUBMIT
            assert ready.stopped_before_submit is True
            assert ready.pages_advanced == 1
            assert ready.last_navigation == {"action": "next", "result": "advanced"}
            state = page.evaluate("() => ({clicked: window.__clicked, submitted: window.__submitted})")
            assert state["clicked"] == ["Save & Continue", "Save & Continue"]
            assert "Submit application" not in state["clicked"]
            assert state["submitted"] is False
            assert page.evaluate("() => window.__submitted") is False
        finally:
            page.close()
    finally:
        server.shutdown()


def test_enter_does_not_activate_submit_application(browser):
    page = browser.new_page()
    try:
        page.set_content(
            """
            <form onsubmit="window.__submitted = true; return false;">
              <label for="note">Note</label>
              <input id="note" name="note">
              <button type="submit" id="final">Submit application</button>
            </form>
            <script>
              window.__submitted = false;
              window.__clicked = [];
              document.getElementById("final").addEventListener("click", () => {
                window.__submitted = true;
                window.__clicked.push("Submit application");
              });
            </script>
            """
        )
        with hold_final_submit(page):
            page.locator("#note").fill("hello")
            page.locator("#note").press("Enter")
        state = page.evaluate("() => ({clicked: window.__clicked, submitted: !!window.__submitted})")
        assert state == {"clicked": [], "submitted": False}
    finally:
        page.close()
