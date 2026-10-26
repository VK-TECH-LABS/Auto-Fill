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
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from autofill.account_flow import run_account_action
from autofill.engine import Status
from autofill.service.app import create_app
from test_protocol_070 import AUTH, TOKEN, _body, _Runner

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


# --- F6: service recovery ---------------------------------------------------


class _StuckRunner(_Runner):
    """Session sits at ACCOUNT_CREATION_REQUIRED; the page already shows the form."""

    def __init__(self, report: dict) -> None:
        super().__init__()
        self.runs = 0
        self.report = report
        self.after_status = ""

    def run(self, request):
        self.runs += 1
        result = super().run(request)
        if self.runs > 1 and self.after_status:
            result.status = self.after_status
            result.current_step = self.after_status
        return result

    def account_action(self, session_id: str, payload: dict) -> dict:
        return dict(self.report)


def _start(client: TestClient) -> str:
    created = client.post(
        "/v1/sessions", headers=AUTH, json=_body("0.7.0", "https://boards.greenhouse.io/example/account")
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["sessionId"]
    started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
    assert started.json()["status"] == Status.ACCOUNT_CREATION_REQUIRED
    return session_id


def _credentials(client: TestClient, session_id: str, mode: str = "register"):
    return client.post(
        f"/v1/sessions/{session_id}/credentials",
        headers=AUTH,
        json={
            "candidateRef": "ref-a",
            "email": "person@example.com",
            "password": SECRET,
            "confirmPassword": SECRET,
            "mode": mode,
        },
    )


def test_stuck_account_step_with_application_visible_resumes(caplog):
    caplog.set_level(logging.INFO)
    runner = _StuckRunner({"ok": False, "reason": "not_at_account_step", "applicationVisible": True})
    runner.after_status = Status.MANUAL_ANSWER_REQUIRED
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as client:
        session_id = _start(client)
        stored = _credentials(client, session_id)
        assert stored.status_code == 200, stored.text
        assert stored.json()["outcome"] == "registered"
        assert runner.runs == 2
        assert stored.json()["status"] == Status.MANUAL_ANSWER_REQUIRED
        assert "account_recovered=true" in caplog.text
        assert SECRET not in stored.text and SECRET not in caplog.text


def test_not_at_account_step_without_application_is_still_409():
    runner = _StuckRunner({"ok": False, "reason": "not_at_account_step"})
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as client:
        session_id = _start(client)
        stored = _credentials(client, session_id)
        assert stored.status_code == 409, stored.text
        assert stored.json()["detail"] == {"reason": "not_at_account_step"}
        assert runner.runs == 1


def test_site_rejected_is_unchanged():
    runner = _StuckRunner({"ok": False, "reason": "site_rejected", "siteErrors": ["Bad"], "account": None})
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as client:
        session_id = _start(client)
        stored = _credentials(client, session_id)
        assert stored.status_code == 422, stored.text
        assert runner.runs == 1
