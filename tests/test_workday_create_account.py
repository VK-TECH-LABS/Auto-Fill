"""Workday Create Account is its own stop, never a Sign In stop.

Synthetic fixtures only. Every request is answered from tests/fixtures by a
Playwright route, so no employer site is reached and no account is created.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from tests.test_protocol_070 import AUTH, TOKEN, _start, fixture_origin, resolver_origin  # noqa: F401

from autofill import AutofillOptions, Status, autofill_application
from autofill.extract import extract_page
from autofill.login import account_fields, create_account_form, score_account, stop_account
from autofill.pagewait import classify_page, register_pane
from autofill.profile import CandidateProfile

FIXTURES = Path(__file__).parent / "fixtures"
URL = "https://example.wd1.myworkdayjobs.com/en-US/External/job/Example-Analyst_R1/apply/applyManually"
HOST = "example.wd1.myworkdayjobs.com"
EMAIL = "test.candidate@example.com"


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {
            "candidateId": "ref-a",
            "personal": {"full_name": "Test Candidate", "email": EMAIL, "phone": "+1 555 0100"},
        }
    )


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
    """Open a fixture at a Workday tenant URL. Every other request is refused."""
    contexts = []

    def open_page(name: str):
        html = (FIXTURES / name).read_text()
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
        return page

    try:
        yield open_page
    finally:
        for context in contexts:
            context.close()


def _run(page, **options):
    return autofill_application(
        URL,
        _profile(),
        None,
        AutofillOptions(page=page, session_id="sess-wd", candidate_id="ref-a", protocol_version="0.7.0", **options),
    )


def _assert_nothing_typed(page) -> None:
    for value in page.locator("input:not([type=checkbox])").evaluate_all("els => els.map(e => e.value)"):
        assert value == ""
    assert page.locator("input[type=checkbox]").evaluate_all("els => els.filter(e => e.checked).length") == 0


@pytest.mark.parametrize("name", ["workday_create_account.html", "workday_create_account_wrapped.html"])
def test_create_account_form_is_classified_as_account(workday, name):
    page = workday(name)
    snapshot = extract_page(page)
    assert create_account_form(snapshot)
    assert register_pane(snapshot)
    assert classify_page(snapshot, url=URL) == "ACCOUNT"
    score = score_account(snapshot, URL)
    assert score.mode == "register"
    # The Sign In link beside the form is the alternate, not the page's mode.
    assert score.alternate is True


def test_wrapped_create_account_flips_a_login_stop(workday):
    """The Barr shape: no ids on the inputs, heading past the text budget, no step bar."""
    page = workday("workday_create_account_wrapped.html")
    snapshot = extract_page(page)
    status, account = stop_account(snapshot, URL, "workday", Status.LOGIN_REQUIRED)
    assert status == Status.ACCOUNT_CREATION_REQUIRED
    assert account is not None
    assert account["mode"] == "register"
    assert account["tenant"]["host"] == HOST


@pytest.mark.parametrize("name", ["workday_create_account.html", "workday_create_account_wrapped.html"])
def test_create_account_stop_carries_register_details(workday, name):
    page = workday(name)
    result = _run(page)
    assert result.status == Status.ACCOUNT_CREATION_REQUIRED, result.messages
    account = result.account
    assert account is not None
    assert account["mode"] == "register"
    assert account["alternateModeAvailable"] is True
    assert account["tenant"] == {
        "ats": "workday",
        "host": HOST,
        "tenantKey": f"{HOST}/External",
        "loginOrigin": f"https://{HOST}",
    }
    policy = account["passwordPolicy"]
    assert policy["minLength"] == 8
    assert policy["requireUpper"] and policy["requireLower"] and policy["requireDigit"] and policy["requireSymbol"]
    assert "my information" not in policy["raw"].casefold()
    assert account["requiredFields"] == ["email", "password", "confirmPassword", "terms"]
    roles = {field["role"]: field for field in account["fields"]}
    assert roles["confirmPassword"]["label"] == "Verify New Password"
    assert roles["terms"]["required"] is True
    assert result.progress["stage"] == "ACCOUNT"
    assert result.messages == ["Create an employer account. Nothing was typed."]
    _assert_nothing_typed(page)
    assert page.evaluate("() => window.__created") == 0


def test_continue_does_not_bypass_create_account(workday):
    page = workday("workday_create_account.html")
    result = _run(page, human_resolved=True)
    assert result.status == Status.ACCOUNT_CREATION_REQUIRED, result.messages
    assert result.account and result.account["mode"] == "register"
    _assert_nothing_typed(page)
    assert page.evaluate("() => window.__created") == 0


def test_workday_sign_in_stays_login_required(workday):
    page = workday("workday_sign_in.html")
    snapshot = extract_page(page)
    assert not create_account_form(snapshot)
    assert not register_pane(snapshot)
    assert score_account(snapshot, URL).mode == "login"
    result = _run(page)
    assert result.status == Status.LOGIN_REQUIRED, result.messages
    assert result.account is not None
    assert result.account["mode"] == "login"
    assert result.account["alternateModeAvailable"] is True
    assert result.account["tenant"]["host"] == HOST
    _assert_nothing_typed(page)
    assert page.evaluate("() => window.__signedIn") == 0


def test_account_fields_never_carry_values(workday):
    page = workday("workday_create_account.html")
    page.fill("#input-4", EMAIL)
    fields = account_fields(extract_page(page))
    assert fields
    for field in fields:
        assert set(field) == {"role", "label", "required"}
        assert EMAIL not in str(field)


def test_application_step_is_not_create_account(workday):
    """An application step after the account pane never gets account details."""
    page = workday("workday_sign_in.html")
    page.set_content(
        "<h1>My Information</h1>"
        "<label for='f'>First Name</label><input id='f' name='firstName'>"
        "<label for='l'>Last Name</label><input id='l' name='lastName'>"
        "<label for='p'>Phone Number</label><input id='p' type='tel'>"
        "<button>Save and Continue</button>"
    )
    snapshot = extract_page(page)
    assert not create_account_form(snapshot)
    _status, account = stop_account(snapshot, URL, "workday", Status.LOGIN_REQUIRED)
    assert account is None or account["mode"] != "register"


def test_service_continue_rechecks_create_account(fixture_origin, resolver_origin):  # noqa: F811
    """Continue at the Create Account stop reruns and stops there again: nothing is typed."""
    from fastapi.testclient import TestClient

    from autofill.service.app import create_app
    from autofill.service.runner import PlaywrightRunner

    runner = PlaywrightRunner(enabled=True, headless=True, workers=1)
    app = create_app(token=TOKEN, runner=runner, run_browser=True)
    with TestClient(app) as client:
        stop = _start(client, resolver_origin, f"{fixture_origin}/workday_create_account.html")
        assert stop["status"] == Status.ACCOUNT_CREATION_REQUIRED, stop.get("messages")
        assert stop["account"]["mode"] == "register"
        assert stop["account"]["requiredFields"] == ["email", "password", "confirmPassword", "terms"]
        assert stop["account"]["passwordPolicy"]["minLength"] == 8
        session_id = stop["sessionId"]
        moved = client.post(
            f"/v1/sessions/{session_id}/continue",
            headers=AUTH,
            json={"candidateRef": "ref-a", "humanResolved": True},
        )
        assert moved.status_code == 200, moved.text
        body = moved.json()
        deadline = time.monotonic() + 40
        while body.get("status") in {Status.STARTED, "RUNNING", "FILLING"} and time.monotonic() < deadline:
            time.sleep(0.05)
            body = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH).json()
        assert body["status"] == Status.ACCOUNT_CREATION_REQUIRED, body.get("messages")
        assert body["account"]["mode"] == "register"

        signin = _start(client, resolver_origin, f"{fixture_origin}/workday_sign_in.html")
        assert signin["status"] == Status.LOGIN_REQUIRED, signin.get("messages")
        assert signin["account"]["mode"] == "login"
