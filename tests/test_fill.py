"""Fill a local static application and prove Submit is not clicked."""

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from autofill.fill import fill_application
from autofill.profile import CandidateProfile

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "profile.example.json"
SAMPLE = Path(__file__).resolve().parent / "fixtures" / "sample_application.html"

LOGIN_HTML = """<!DOCTYPE html>
<html><body>
<form onsubmit="window.__submitted = true; return false;">
  <label for="email">Email</label>
  <input id="email" name="email" type="email" autocomplete="email">
  <label for="password">Password</label>
  <input id="password" name="password" type="password">
  <button type="submit" id="login">Log in</button>
</form>
<script>window.__submitted = false;</script>
</body></html>
"""

CAPTCHA_HTML = """<!DOCTYPE html>
<html><body>
<form onsubmit="window.__submitted = true; return false;">
  <label for="email">Email address</label>
  <input id="email" name="email" type="email" autocomplete="email">
  <div class="g-recaptcha" data-sitekey="not-a-real-sitekey"></div>
  <button type="button" id="next">Next</button>
  <button type="submit" id="submit">Submit application</button>
</form>
<script>
  window.__submitted = false;
  window.__continued = false;
  document.getElementById("next").addEventListener("click", () => { window.__continued = true; });
</script>
</body></html>
"""


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch(headless=True)
        try:
            yield chromium
        finally:
            chromium.close()


def test_manual_ats_and_sso_do_not_need_a_browser():
    profile = CandidateProfile.load(EXAMPLE)
    manual = fill_application(profile, url="https://ibegin.tcsapps.com/candidate/apply")
    assert manual.status == "skipped_manual_ats"
    assert manual.stopped_before_submit
    assert manual.pages_filled == 0

    sso = fill_application(profile, url="https://accounts.google.com/o/oauth2/v2/auth")
    assert sso.status == "skipped_sso"
    assert sso.stopped_before_submit


def test_sample_form_is_filled_and_not_submitted(browser, tmp_path):
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4\n% fictional resume for the autofill fixture\n")
    profile = CandidateProfile.load(EXAMPLE)
    page = browser.new_page()
    try:
        page.goto(SAMPLE.as_uri())
        result = fill_application(
            profile,
            page=page,
            resume_path=resume,
            advance_pages=True,
        )
        assert result.stopped_before_submit is True
        assert result.status == "filled"
        assert result.pages_filled == 2
        assert result.captcha_present is False
        assert "Submit application" in result.submit_controls
        assert "Send application" in result.submit_controls
        assert "Apply now" in result.submit_controls
        assert result.continued_controls == ["Save and continue"]

        assert page.locator("#first").input_value() == "Casey"
        assert page.locator("#last").input_value() == "Example"
        assert page.locator("#email").input_value() == "casey.example@example.com"
        assert page.locator("#phone").input_value() == "555-010-0199"
        assert page.locator("#city").input_value() == "Example City"
        assert page.locator("#auth").input_value() == "Yes"
        assert page.locator("#sponsor-no").is_checked()
        assert page.locator("#sponsor-yes").is_checked() is False
        assert page.locator("#background").is_checked()
        assert page.locator("#gender").input_value() == "Decline to self-identify"
        assert "fictional" in page.locator("#cover").input_value()
        assert page.locator("#pronouns").input_value() == ""
        assert page.locator("#company-website").input_value() == ""
        assert page.locator("#github").input_value() == "https://github.com/casey-example"
        assert page.locator("#hear").input_value() == "Online job board"
        uploaded = page.evaluate("() => document.querySelector('#resume').files[0].name")
        assert uploaded == "resume.pdf"

        assert page.evaluate("() => window.__submitted") is False
        assert page.evaluate("() => window.__applied") is False
        assert page.evaluate("() => window.__continued") is True
    finally:
        page.close()


def test_login_wall_is_not_filled_or_submitted(browser, tmp_path):
    html = tmp_path / "login.html"
    html.write_text(LOGIN_HTML, encoding="utf-8")
    page = browser.new_page()
    try:
        page.goto(html.as_uri())
        result = fill_application(CandidateProfile.load(EXAMPLE), page=page, advance_pages=True)
        assert result.status == "skipped_login"
        assert result.login_wall is True
        assert result.stopped_before_submit is True
        assert result.pages_filled == 0
        assert page.locator("#email").input_value() == ""
        assert page.locator("#password").input_value() == ""
        assert page.evaluate("() => window.__submitted") is False
    finally:
        page.close()


def test_captcha_stops_the_run_without_a_solver(browser, tmp_path):
    html = tmp_path / "captcha.html"
    html.write_text(CAPTCHA_HTML, encoding="utf-8")
    page = browser.new_page()
    try:
        page.goto(html.as_uri())
        result = fill_application(CandidateProfile.load(EXAMPLE), page=page, advance_pages=True)
        assert result.captcha_present is True
        assert result.stopped_before_submit is True
        assert result.continued_controls == []
        assert page.locator("#email").input_value() == "casey.example@example.com"
        assert page.evaluate("() => window.__continued") is False
        assert page.evaluate("() => window.__submitted") is False
    finally:
        page.close()
