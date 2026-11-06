"""Continue fills a visible form, and a successful sign-in continues into that form.

Create Account stays a pause. Nothing in these tests registers an account or
clicks Submit.
"""

from __future__ import annotations

import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from autofill import AutofillOptions, Credentials, MemoryCredentialProvider, Status, autofill_application
from autofill.credentials import credential_scope
from autofill.extract import extract_page
from autofill.login import verification_present
from autofill.models import Control, PageSnapshot
from autofill.profile import CandidateProfile
from autofill.service.app import create_app
from autofill.service.runner import PlaywrightRunner

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "profile.example.json"
QUESTIONS = FIXTURES / "questions_false_challenge.html"
SIGNIN = FIXTURES / "signin_then_apply.html"
TOKEN = "synthetic-service-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
URL = "https://boards.greenhouse.io/example/jobs/1"
PASSWORD = "Saved-Login-7!"


def _control(kind: str, label: str, **kwargs) -> Control:
    return Control(kind=kind, label=label, selector=kwargs.pop("selector", f"#{kind}-{label}"), **kwargs)


def _profile() -> CandidateProfile:
    return CandidateProfile.load(EXAMPLE)


@pytest.fixture
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


def test_question_page_text_is_not_a_verification_stop():
    questions = PageSnapshot(
        buttons=[],
        controls=[
            _control("radio", "Yes", name="auth"),
            _control("radio", "No", name="auth"),
            _control("text", "Referral code", max_length=6),
        ],
        heading="Application questions",
        page_text="Check your email. We sent you a code so we can notify you about this application.",
    )
    assert verification_present(questions) is False
    otp = PageSnapshot(
        buttons=[],
        controls=[_control("text", "Verification code", autocomplete="one-time-code", max_length=6)],
        heading="Email verification",
        page_text="Check your email. We sent you a code.",
    )
    assert verification_present(otp) is True


def test_radio_question_page_is_not_a_wall_and_is_filled(browser):
    page = browser.new_page()
    try:
        page.set_content(
            """
            <h1>Application questions</h1>
            <p>Just a moment while we check your browser.</p>
            <p>Check your email. We sent you a code so we can notify you.</p>
            <label><input type="radio" name="auth" value="Yes"> Yes</label>
            <label><input type="radio" name="auth" value="No"> No</label>
            <input id="resume" type="file">
            """,
            wait_until="domcontentloaded",
        )
        snapshot = extract_page(page)
        assert snapshot.captcha_present is False
        assert verification_present(snapshot) is False

        page.goto(QUESTIONS.as_uri())
        result = autofill_application(
            URL,
            _profile(),
            None,
            AutofillOptions(page=page, session_id="sess-questions", candidate_id="ref-a", protocol_version="0.7.0"),
        )
        assert result.status not in {Status.CAPTCHA_REQUIRED, Status.EMAIL_VERIFICATION_REQUIRED}, result.messages
        assert page.locator("#first").input_value() == "Casey"
        assert page.evaluate("() => window.__submitted") is not True
    finally:
        page.close()


def test_saved_sign_in_waits_for_the_application_and_fills_it(browser):
    provider = MemoryCredentialProvider()
    provider.put(
        "ref-a",
        credential_scope(URL),
        Credentials(site=credential_scope(URL), email="casey.example@example.com", password=PASSWORD),
    )
    page = browser.new_page()
    try:
        page.goto(SIGNIN.as_uri())
        result = autofill_application(
            URL,
            _profile(),
            provider,
            AutofillOptions(
                page=page,
                session_id="sess-after-login",
                candidate_id="ref-a",
                protocol_version="0.7.0",
            ),
        )
        assert result.login_status == "AUTHENTICATED", result.messages
        assert result.status not in {
            Status.LOGIN_REQUIRED,
            Status.LOGIN_FAILED,
            Status.ACCOUNT_CREATION_REQUIRED,
            Status.EMAIL_VERIFICATION_REQUIRED,
            Status.CAPTCHA_REQUIRED,
        }, result.messages
        assert page.locator("#first").input_value() == "Casey"
        assert page.evaluate("() => window.__submitted") is not True
        assert page.evaluate("() => window.__signedIn") == 1
    finally:
        page.close()


def test_continue_on_a_sign_in_form_uses_saved_credentials_and_fills(browser):
    provider = MemoryCredentialProvider()
    provider.put(
        "ref-a",
        credential_scope(URL),
        Credentials(site=credential_scope(URL), email="casey.example@example.com", password=PASSWORD),
    )
    page = browser.new_page()
    try:
        page.goto(SIGNIN.as_uri())
        result = autofill_application(
            URL,
            _profile(),
            provider,
            AutofillOptions(
                page=page,
                session_id="sess-continue-login",
                candidate_id="ref-a",
                protocol_version="0.7.0",
                human_resolved=True,
            ),
        )
        assert result.login_status == "AUTHENTICATED", result.messages
        assert page.locator("#first").input_value() == "Casey"
        assert page.evaluate("() => window.__submitted") is not True
    finally:
        page.close()


def test_create_account_is_not_auto_registered(browser):
    provider = MemoryCredentialProvider()
    provider.put(
        "ref-a",
        credential_scope(URL),
        Credentials(site=credential_scope(URL), email="casey.example@example.com", password=PASSWORD),
    )
    page = browser.new_page()
    try:
        page.set_content(
            """
            <h1>Create Account</h1>
            <label for="email">Email</label><input id="email" type="email">
            <label for="password">Password</label><input id="password" type="password">
            <label for="verify">Verify New Password</label><input id="verify" type="password">
            <button type="button" id="create">Create Account</button>
            <script>
              window.__created = 0;
              document.getElementById("create").onclick = () => { window.__created += 1; };
            </script>
            """,
            wait_until="domcontentloaded",
        )
        result = autofill_application(
            URL,
            _profile(),
            provider,
            AutofillOptions(page=page, session_id="sess-no-register", candidate_id="ref-a", protocol_version="0.7.0"),
        )
        assert result.status == Status.ACCOUNT_CREATION_REQUIRED, result.messages
        assert page.locator("#password").input_value() == ""
        assert page.evaluate("() => window.__created") == 0
    finally:
        page.close()


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return


def _settle(client: TestClient, session_id: str, started) -> dict:
    assert started.status_code == 200, started.text
    body = started.json()
    deadline = time.monotonic() + 40
    while body.get("status") == Status.STARTED and time.monotonic() < deadline:
        time.sleep(0.05)
        polled = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        assert polled.status_code == 200, polled.text
        body = polled.json()
    return body


def _eval(runner: PlaywrightRunner, session_id: str, expression: str) -> object:
    def read() -> object:
        page = runner._sessions[session_id].page
        return page.evaluate(expression)

    return runner._submit(read, session_id)


def test_resume_fills_when_the_application_replaced_sign_in():
    pages = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(FIXTURES)))
    thread = threading.Thread(target=pages.serve_forever, daemon=True)
    thread.start()
    host, port = pages.server_address
    origin = f"http://{host}:{port}"
    runner = PlaywrightRunner(enabled=True, headless=True, workers=1)
    app = create_app(token=TOKEN, runner=runner, run_browser=True)
    body = {
        "candidateContext": {
            "candidateId": "ref-a",
            "personal": {"full_name": "River Example", "email": "river.example@example.com"},
        },
        "jobContext": {
            "applicationUrl": f"{origin}/signin_then_apply.html",
            "jobId": "job-a",
            "candidateId": "ref-a",
        },
    }
    try:
        with TestClient(app) as client:
            created = client.post("/v1/sessions", headers=AUTH, json=body)
            assert created.status_code == 201, created.text
            session_id = created.json()["sessionId"]
            started = _settle(
                client,
                session_id,
                client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateId": "ref-a"}),
            )
            assert started["status"] == Status.LOGIN_REQUIRED, started
            assert _eval(runner, session_id, "() => document.querySelector('#email').value") == ""
            _eval(
                runner,
                session_id,
                """() => {
                  document.getElementById('login').hidden = true;
                  document.getElementById('app').hidden = false;
                }""",
            )
            resumed = client.post(f"/v1/sessions/{session_id}/resume", headers=AUTH)
            assert resumed.status_code == 200, resumed.text
            if resumed.json().get("status") == Status.STARTED:
                final = _settle(client, session_id, resumed)
            else:
                final = resumed.json()
            assert final["status"] not in {
                Status.LOGIN_REQUIRED,
                Status.EMAIL_VERIFICATION_REQUIRED,
                Status.CAPTCHA_REQUIRED,
                Status.ACCOUNT_CREATION_REQUIRED,
            }, final
            assert _eval(runner, session_id, "() => document.querySelector('#first').value") == "River"
            assert _eval(runner, session_id, "() => window.__submitted") is not True
    finally:
        pages.shutdown()
        runner.shutdown()
