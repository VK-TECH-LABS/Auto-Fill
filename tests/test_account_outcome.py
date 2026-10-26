"""Create Account outcome after #73 (Aptiv, 2026-10-27).

Aptiv: auto-consent checked the privacy box, Create Account was pressed, and
Workday moved on to "My Information" (signed in). The engine had already read an
alert on the first poll while the password pane was still mounted and answered
422 site_rejected. Every later Continue then got 409 not_at_account_step.

* F5: alert text that was on screen before the press is ignored; a new alert counts
  only when it persists (~0.5 s, no spinner) and the account form is still there.
* F6: /credentials on a stuck ACCOUNT_*/LOGIN_* session whose page already shows the
  application form resumes the run instead of answering 409.

Synthetic fixtures only. No employer site is reached and no account is created.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from autofill.account_flow import run_account_action
from autofill.engine import Status

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "example.wd5.myworkdayjobs.com"
URL = f"https://{HOST}/en-US/Careers/job/Example-Analyst_R9/apply/applyManually"
EMAIL = "test.candidate@example.com"
SECRET = "Fixture-Pass-9!"
FLASH = "Error: Something went wrong. Please try again."
STALE = "Error: Please check the box to continue."


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


@pytest.fixture
def workday(browser):
    contexts = []

    def open_page(**knobs: str | int):
        html = (FIXTURES / "workday_create_then_sign_in.html").read_text()
        context = browser.new_context()
        contexts.append(context)

        def handle(route):
            if route.request.url.startswith(f"https://{HOST}/"):
                route.fulfill(status=200, body=html, content_type="text/html")
            else:
                route.abort()

        context.route("**/*", handle)
        page = context.new_page()
        page.goto(URL)
        page.evaluate(
            "(knobs) => { for (const [k, v] of Object.entries(knobs)) localStorage.setItem(k, String(v)); }",
            {key: value for key, value in knobs.items()},
        )
        page.reload()
        return page

    try:
        yield open_page
    finally:
        for context in contexts:
            context.close()


def _register(page) -> dict:
    return run_account_action(
        page,
        mode="register",
        email=EMAIL,
        username="",
        password=SECRET,
        confirm=SECRET,
        ats="workday",
        standard_terms=False,
    )


# --- F5: _wait_for_outcome -------------------------------------------------


def test_flashing_alert_then_my_information_is_registered(workday):
    # The alert is on screen for the first poll after the press, then the site
    # moves on to My Information. Before F5 this returned 422 site_rejected.
    page = workday(flash=FLASH, holdMs=350, spinnerMs=400, after="app")
    result = _register(page)
    assert result["ok"] is True, result
    assert result["outcome"] == "registered"
    assert result.get("reason") != "site_rejected"
    assert result["status"] == ""
    assert page.evaluate("window.__created") == 1
    assert page.locator("[data-automation-id='applyFlowMyInfoPage']").count() == 1
    assert SECRET not in str(result)


def test_alert_already_on_screen_before_the_press_is_ignored(workday):
    # A stale alert stays on the still-mounted form for over a second after the
    # press, then the site moves on. It was there before the press, so it is not
    # the site's answer.
    page = workday(stale=STALE, holdMs=1200, spinnerMs=300, after="app")
    result = _register(page)
    assert result["ok"] is True, result
    assert result["outcome"] == "registered"
    assert page.evaluate("window.__created") == 1


def test_flashing_alert_then_sign_in_still_waits_for_verification(workday):
    page = workday(flash=FLASH, holdMs=350, spinnerMs=400)
    result = _register(page)
    assert result["ok"] is True, result
    assert result["outcome"] == "registered"
    assert result["status"] == Status.EMAIL_VERIFICATION_REQUIRED
    assert result["verifyBeforeSignIn"] is True
    assert page.evaluate("window.__signedIn") == 0


def test_lasting_new_error_is_still_site_rejected_and_logged_redacted(workday, caplog):
    caplog.set_level(logging.INFO, logger="autofill.account")
    reject = f"Error: The password for {EMAIL} does not meet the requirements."
    page = workday(reject=reject)
    result = _register(page)
    assert result["ok"] is False, result
    assert result["reason"] == "site_rejected"
    assert result["status"] == Status.ACCOUNT_CREATION_REQUIRED
    assert any("does not meet the requirements" in item for item in result["siteErrors"])
    assert page.evaluate("window.__created") == 0
    lines = [rec.getMessage() for rec in caplog.records if "account_outcome=site_rejected" in rec.getMessage()]
    assert lines, caplog.text
    assert "does not meet the requirements" in lines[0]
    assert EMAIL not in caplog.text and SECRET not in caplog.text
    assert EMAIL not in str(result) and SECRET not in str(result)


def test_new_error_on_top_of_a_stale_one_still_counts(workday):
    page = workday(stale=STALE, reject="Error: Password is too weak.")
    result = _register(page)
    assert result["reason"] == "site_rejected", result
    assert "Error: Password is too weak." in result["siteErrors"]


def test_application_page_reports_application_visible(workday):
    page = workday(spinnerMs=100, after="app")
    assert _register(page)["outcome"] == "registered"
    again = _register(page)
    assert again == {"ok": False, "reason": "not_at_account_step", "applicationVisible": True}
