"""Account follow-ups from the 2026-10-26 staging smoke after #69.

* Faith / Hendrick: Create Account was declared "registered" about a second
  after the click and the run signed in at once with the new password. Workday
  answered "You may have entered the wrong email address or password or your
  account might be locked." and Companion showed the generic "did not accept".
* A Workday cookie banner marked only by ``data-automation-id`` stayed open over
  Create Account and Sign In.
* A saved login on a Create Account pane was refused with ``mode_mismatch``
  instead of opening the pane's Sign In link.
* LOGIN_FAILED could not be resolved with Continue.

Synthetic fixtures only. Every request is answered by a Playwright route or a
fake runner, so no employer site is reached and no account is created.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from autofill import AutofillOptions, Status, autofill_application
from autofill.account_flow import run_account_action
from autofill.credentials import Credentials, MemoryCredentialProvider
from autofill.engine import ApplicationResult
from autofill.login import LOGIN_FAILED_MESSAGES, account_exists_text, login_failure_reason
from autofill.models import PageSnapshot
from autofill.profile import CandidateProfile
from autofill.service.app import create_app
from test_protocol_070 import AUTH, TOKEN, _body, _Runner

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "example.wd5.myworkdayjobs.com"
URL = f"https://{HOST}/en-US/Careers/job/Example-Analyst_R9/apply/applyManually"
EMAIL = "test.candidate@example.com"
SECRET = "Fixture-Pass-9!"
LOCKED = "Error: You may have entered the wrong email address or password or your account might be locked."


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

    def open_page(*, start: str = "", locked: bool = False):
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
            "([start, locked]) => { localStorage.setItem('start', start); localStorage.setItem('locked', locked); }",
            [start, "1" if locked else ""],
        )
        page.reload()
        return page

    try:
        yield open_page
    finally:
        for context in contexts:
            context.close()


def _action(page, mode: str) -> dict:
    return run_account_action(
        page,
        mode=mode,
        email=EMAIL,
        username="",
        password=SECRET,
        confirm=SECRET if mode == "register" else "",
        ats="workday",
        standard_terms=False,
    )


def _snap(**kwargs) -> PageSnapshot:
    return PageSnapshot(controls=[], buttons=[], **kwargs)


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {"candidateId": "ref-a", "personal": {"full_name": "Test Candidate", "email": EMAIL, "phone": "+1 555 0100"}}
    )


# --- Text classification ----------------------------------------------------


def test_workday_wrong_or_locked_banner_is_classified():
    assert login_failure_reason(_snap(alerts=[LOCKED])) == "wrong_or_locked"
    assert login_failure_reason(_snap(banner="Too many failed sign-in attempts.")) == "wrong_or_locked"
    assert login_failure_reason(_snap(alerts=["Your account has not been verified."])) == "unverified"
    assert login_failure_reason(_snap(alerts=["Something went wrong."])) == "rejected"
    assert "did not accept" not in LOGIN_FAILED_MESSAGES["wrong_or_locked"]


def test_workday_email_already_in_use_counts_as_existing_account():
    assert account_exists_text(_snap(alerts=["Error: This email is already in use."]))
    # The Create Account pane's own "Already have an account? Sign In" is not an error.
    assert not account_exists_text(_snap(page_text="Already have an account? Sign In"))


# --- Browser fixtures -------------------------------------------------------


def test_create_account_returning_to_sign_in_waits_for_verification(workday):
    page = workday()
    result = _action(page, "register")
    assert result["ok"] is True, result
    assert result["outcome"] == "registered"
    assert result["status"] == "EMAIL_VERIFICATION_REQUIRED"
    assert result["verifyBeforeSignIn"] is True
    assert page.evaluate("window.__created") == 1
    # The new password is never typed into Sign In before the verification email.
    assert page.evaluate("window.__signedIn") == 0
    assert page.locator("#input-15").input_value() == ""
    assert SECRET not in str(result)


def test_cookie_banner_is_closed_before_the_account_form(workday):
    page = workday()
    assert page.locator("[data-automation-id='legalNoticeBanner']").is_visible()
    _action(page, "register")
    assert page.locator("[data-automation-id='legalNoticeBanner']").count() == 0
    # Existing policy: decline non-essential cookies when a decline button exists.
    assert page.evaluate("window.__cookie") == "Decline"


def test_saved_login_on_create_account_opens_sign_in_instead_of_registering(workday):
    page = workday()
    result = _action(page, "login")
    assert result["ok"] is True, result
    assert result["outcome"] == "signed_in"
    assert page.evaluate("window.__created") == 0
    assert page.evaluate("window.__signedIn") == 1
    assert SECRET not in str(result)


def test_visible_sign_in_with_saved_credentials_signs_in_without_continue(workday):
    page = workday(start="signin")
    provider = MemoryCredentialProvider()
    provider.put("ref-a", HOST, Credentials(site=HOST, email=EMAIL, password=SECRET))
    result = autofill_application(
        URL,
        _profile(),
        provider,
        AutofillOptions(page=page, session_id="sess-kla2", candidate_id="ref-a", protocol_version="0.7.0"),
    )
    assert page.evaluate("window.__signedIn") == 1
    assert result.login_status == "AUTHENTICATED"
    assert result.status not in {Status.LOGIN_REQUIRED, Status.LOGIN_FAILED}
    assert result.stopped_before_submit is True
    assert SECRET not in str(result)


def test_locked_sign_in_reports_the_site_banner_and_a_clear_message(workday):
    page = workday(start="signin", locked=True)
    provider = MemoryCredentialProvider()
    provider.put("ref-a", HOST, Credentials(site=HOST, email=EMAIL, password=SECRET))
    result = autofill_application(
        URL,
        _profile(),
        provider,
        AutofillOptions(page=page, session_id="sess-faith", candidate_id="ref-a", protocol_version="0.7.0"),
    )
    assert result.status == Status.LOGIN_FAILED
    assert page.evaluate("window.__signedIn") == 1
    assert result.messages == [LOGIN_FAILED_MESSAGES["wrong_or_locked"]]
    assert result.account is not None
    assert result.account["loginFailure"] == "wrong_or_locked"
    assert any("might be locked" in item for item in result.account["siteErrors"])
    assert SECRET not in str(result)
    assert EMAIL not in str(result.account)


# --- Service --------------------------------------------------------------


class _VerifyFirstRunner(_Runner):
    def __init__(self) -> None:
        super().__init__()
        self.runs = 0
        self.next_status = ""

    def run(self, request):
        self.runs += 1
        result = super().run(request)
        if self.next_status:
            result.status = self.next_status
            result.current_step = self.next_status
        return result

    def account_action(self, session_id: str, payload: dict) -> dict:
        self.seen_secret = str(payload.get("password") or "")
        return {
            "ok": True,
            "outcome": "registered",
            "status": Status.EMAIL_VERIFICATION_REQUIRED,
            "verifyBeforeSignIn": True,
            "siteErrors": [],
            "account": None,
        }


def _session(client: TestClient, url: str) -> str:
    created = client.post("/v1/sessions", headers=AUTH, json=_body("0.7.0", url))
    assert created.status_code == 201, created.text
    session_id = created.json()["sessionId"]
    started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
    assert started.status_code == 200, started.text
    return session_id


def test_service_does_not_sign_in_before_verification(caplog):
    caplog.set_level(logging.INFO)
    runner = _VerifyFirstRunner()
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as client:
        session_id = _session(client, "https://boards.greenhouse.io/example/account")
        assert runner.runs == 1
        stored = client.post(
            f"/v1/sessions/{session_id}/credentials",
            headers=AUTH,
            json={
                "candidateRef": "ref-a",
                "email": "person@example.com",
                "password": SECRET,
                "confirmPassword": SECRET,
                "mode": "register",
            },
        )
        assert stored.status_code == 200, stored.text
        assert stored.json()["outcome"] == "registered"
        assert stored.json()["status"] == Status.EMAIL_VERIFICATION_REQUIRED
        # No resume run: the new password is not typed into Sign In yet.
        assert runner.runs == 1
        assert SECRET not in stored.text
        assert SECRET not in caplog.text
        resumed = client.post(
            f"/v1/sessions/{session_id}/continue",
            headers=AUTH,
            json={"candidateRef": "ref-a", "humanResolved": True},
        )
        assert resumed.status_code == 200, resumed.text
        assert runner.runs == 2


def test_login_failed_drops_the_refused_password_and_allows_continue():
    runner = _VerifyFirstRunner()
    runner.next_status = Status.LOGIN_FAILED
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as client:
        created = client.post("/v1/sessions", headers=AUTH, json=_body("0.7.0"))
        session_id = created.json()["sessionId"]
        session = app.state.store.get(session_id)
        session.credentials.put("ref-a", "boards.greenhouse.io", Credentials(site="x", email=EMAIL, password=SECRET))
        started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
        assert started.json()["status"] == Status.LOGIN_FAILED
        assert session.credentials._records == {}
        runner.next_status = ""
        resumed = client.post(
            f"/v1/sessions/{session_id}/continue",
            headers=AUTH,
            json={"candidateRef": "ref-a", "humanResolved": True},
        )
        assert resumed.status_code == 200, resumed.text
        assert runner.runs == 2


def test_result_type_is_unchanged():
    assert "account" in ApplicationResult.__dataclass_fields__
