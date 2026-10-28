"""F8 (t27u, BW Better World 2026-10-27): verify before sign in, system-wide for Workday.

RC-V1: after Create Account, Barry-Wehmiller left an empty pane (no form, no
password) for longer than the 4 s Workday settle, so ``_wait_for_outcome``
answered ``registered next=resume``. The resume run then found Sign In and typed
the brand-new password at once; Workday answered "Verify your account before you
sign in" and the session went LOGIN_FAILED (one failed try toward a lockout).

Now:
* the service marks the session ``verify_pending`` after such a register, and the
  engine stops on the existing EMAIL_VERIFICATION_REQUIRED card at the next Sign In
  pane instead of calling ``run_login``;
* a LOGIN_FAILED whose banner says the account is not verified becomes the same
  verify card (protocol 0.7.0), so Continue signs in once after the person verifies;
* Continue with ``humanResolved`` *and* ``verified`` clears ``verify_pending`` (R4: humanResolved
  alone keeps it); reaching the application clears it.

Synthetic fixtures only. No real employer account.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from autofill.account_flow import _VERIFY_FIRST_MESSAGE, run_account_action
from autofill.credentials import Credentials, MemoryCredentialProvider
from autofill.engine import AutofillOptions, Status, _verify_first, autofill_application
from autofill.login import LOGIN_FAILED_MESSAGES
from autofill.profile import CandidateProfile
from autofill.service.app import create_app
from test_protocol_070 import AUTH, TOKEN, _body, _Runner

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "example.wd1.myworkdayjobs.com"
URL = f"https://{HOST}/en-US/Careers/job/Example-Analyst_R9/apply/applyManually"
EMAIL = "test.candidate@example.com"
SECRET = "Fixture-Pass-9!"


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

    def open_page(**storage: str):
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
            "(items) => { for (const [k, v] of Object.entries(items)) localStorage.setItem(k, v); }",
            storage,
        )
        page.reload()
        return page

    try:
        yield open_page
    finally:
        for context in contexts:
            context.close()


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {"candidateId": "ref-a", "personal": {"full_name": "Test Candidate", "email": EMAIL, "phone": "+1 555 0100"}}
    )


def _provider() -> MemoryCredentialProvider:
    provider = MemoryCredentialProvider()
    provider.put("ref-a", HOST, Credentials(site=HOST, email=EMAIL, password=SECRET))
    return provider


def _fill(page, *, verify_pending: bool, protocol: str = "0.7.0", human: bool = False):
    return autofill_application(
        URL,
        _profile(),
        _provider(),
        AutofillOptions(
            page=page,
            session_id="sess-bw",
            candidate_id="ref-a",
            protocol_version=protocol,
            verify_pending=verify_pending,
            human_resolved=human,
        ),
    )


# --- Engine, real browser ----------------------------------------------------


def test_rc_v1_slow_sign_in_after_create_account_is_classified_registered_resume(workday):
    """Documents the trigger: Sign In opens after the 4 s settle, so the wait says next=resume."""
    page = workday(spinnerMs="300", blankMs="6000")
    result = run_account_action(
        page,
        mode="register",
        email=EMAIL,
        username="",
        password=SECRET,
        confirm=SECRET,
        ats="workday",
        standard_terms=False,
    )
    assert result["ok"] is True, result
    assert result["outcome"] == "registered"
    assert not result.get("verifyBeforeSignIn")
    assert page.evaluate("window.__created") == 1
    assert page.evaluate("window.__signedIn") == 0


def test_verify_pending_sign_in_pane_stops_on_verify_card_and_types_nothing(workday, caplog):
    caplog.set_level(logging.INFO)
    page = workday(start="signin", unverified="1")
    result = _fill(page, verify_pending=True)
    assert result.status == Status.EMAIL_VERIFICATION_REQUIRED
    assert result.messages == [_VERIFY_FIRST_MESSAGE]
    assert page.evaluate("window.__signedIn") == 0
    assert page.evaluate("document.querySelector('#input-15').value") == ""
    assert result.account is not None and result.account.get("verifyBeforeSignIn") is True
    assert result.account.get("mode") == "login"
    assert "verify_before_sign_in=true reason=registered_unverified" in caplog.text
    assert SECRET not in str(result) and SECRET not in caplog.text


def test_verify_pending_even_after_an_earlier_continue_does_not_sign_in(workday):
    """human_resolved is sticky on the session; only verify_pending decides here."""
    page = workday(start="signin")
    result = _fill(page, verify_pending=True, human=True)
    assert result.status == Status.EMAIL_VERIFICATION_REQUIRED
    assert page.evaluate("window.__signedIn") == 0


def test_after_verification_continue_signs_in_once_and_reaches_the_form(workday):
    page = workday(start="signin")
    result = _fill(page, verify_pending=False, human=True)
    assert page.evaluate("window.__signedIn") == 1
    assert result.login_status == "AUTHENTICATED"
    assert result.status not in {Status.LOGIN_FAILED, Status.EMAIL_VERIFICATION_REQUIRED, Status.LOGIN_REQUIRED}
    assert result.stopped_before_submit is True


def test_unverified_sign_in_banner_becomes_the_verify_card(workday):
    page = workday(start="signin", unverified="1")
    result = _fill(page, verify_pending=False)
    assert page.evaluate("window.__signedIn") == 1  # one try only, nothing retried
    assert result.status == Status.EMAIL_VERIFICATION_REQUIRED
    assert result.messages == [LOGIN_FAILED_MESSAGES["unverified"]]
    assert result.account is not None
    assert result.account["loginFailure"] == "unverified"
    assert result.account["verifyBeforeSignIn"] is True
    assert any("Verify your account" in item for item in result.account["siteErrors"])
    assert SECRET not in str(result)


def test_unverified_banner_stays_login_failed_before_protocol_070(workday):
    page = workday(start="signin", unverified="1")
    result = _fill(page, verify_pending=False, protocol="")
    assert result.status == Status.LOGIN_FAILED


def test_locked_banner_is_still_login_failed(workday):
    page = workday(start="signin", locked="1")
    result = _fill(page, verify_pending=False)
    assert result.status == Status.LOGIN_FAILED
    assert result.account["loginFailure"] == "wrong_or_locked"


def test_registered_straight_into_application_is_unaffected_by_verify_pending(workday):
    page = workday(start="signin")
    page.evaluate("document.querySelector('[data-automation-id=signInSubmitButton]').click()")
    result = _fill(page, verify_pending=True)
    assert result.status != Status.EMAIL_VERIFICATION_REQUIRED
    assert page.evaluate("window.__signedIn") == 1  # only the click above


def test_verify_first_scope_is_workday_and_protocol_070():
    on = SimpleNamespace(verify_pending=True, protocol_version="0.7.0")
    assert _verify_first(on, "workday", "https://x.example/a") is True
    assert _verify_first(on, None, URL) is True
    assert _verify_first(on, "greenhouse", "https://boards.greenhouse.io/x") is False
    assert _verify_first(SimpleNamespace(verify_pending=False, protocol_version="0.7.0"), "workday", URL) is False
    assert _verify_first(SimpleNamespace(verify_pending=True, protocol_version=""), "workday", URL) is False


# --- Service -----------------------------------------------------------------


class _ResumeRunner(_Runner):
    """Register answers ``registered`` with no verifyBeforeSignIn (RC-V1); runs are recorded."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list = []
        self.next_status = ""

    def run(self, request):
        self.requests.append(request)
        result = super().run(request)
        if self.next_status:
            result.status = self.next_status
            result.current_step = self.next_status
        return result

    def account_action(self, session_id: str, payload: dict) -> dict:
        return {"ok": True, "outcome": "registered", "status": "", "siteErrors": [], "account": None}


def _register(client: TestClient, session_id: str):
    return client.post(
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


def _started(client: TestClient, url: str) -> str:
    created = client.post("/v1/sessions", headers=AUTH, json=_body("0.7.0", url))
    assert created.status_code == 201, created.text
    session_id = created.json()["sessionId"]
    started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
    assert started.status_code == 200, started.text
    return session_id


def test_service_resume_after_register_is_verify_pending_and_keeps_outcome_registered():
    runner = _ResumeRunner()
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as client:
        session_id = _started(client, "https://boards.greenhouse.io/example/account")
        assert runner.requests[0].verify_pending is False
        runner.next_status = Status.EMAIL_VERIFICATION_REQUIRED
        stored = _register(client, session_id)
        assert stored.status_code == 200, stored.text
        # The resume run was told the account is waiting for its email.
        assert runner.requests[-1].verify_pending is True
        # Still "registered" so Companion saves the password; the status says verify first.
        assert stored.json()["outcome"] == "registered"
        assert stored.json()["status"] == Status.EMAIL_VERIFICATION_REQUIRED
        session = app.state.store.get(session_id)
        assert session.verify_pending is True
        # The register password stays for the one sign-in after verification.
        assert session.credentials._records
        runner.next_status = ""
        resumed = client.post(
            f"/v1/sessions/{session_id}/continue",
            headers=AUTH,
            json={"candidateRef": "ref-a", "humanResolved": True, "verified": True},
        )
        assert resumed.status_code == 200, resumed.text
        assert runner.requests[-1].verify_pending is False
        assert session.verify_pending is False
        assert SECRET not in stored.text and SECRET not in resumed.text


def test_service_registered_into_application_clears_verify_pending():
    runner = _ResumeRunner()
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as client:
        session_id = _started(client, "https://boards.greenhouse.io/example/account")
        runner.next_status = Status.MANUAL_ANSWER_REQUIRED
        stored = _register(client, session_id)
        assert stored.status_code == 200, stored.text
        assert stored.json()["outcome"] == "registered"
        assert runner.requests[-1].verify_pending is True
        assert app.state.store.get(session_id).verify_pending is False


def test_service_signed_in_does_not_set_verify_pending():
    class _Login(_ResumeRunner):
        def account_action(self, session_id: str, payload: dict) -> dict:
            return {"ok": True, "outcome": "signed_in", "status": "", "siteErrors": [], "account": None}

    runner = _Login()
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as client:
        session_id = _started(client, "https://boards.greenhouse.io/example/login")
        stored = client.post(
            f"/v1/sessions/{session_id}/credentials",
            headers=AUTH,
            json={"candidateRef": "ref-a", "email": "person@example.com", "password": SECRET, "mode": "login"},
        )
        assert stored.status_code == 200, stored.text
        assert runner.requests[-1].verify_pending is False
