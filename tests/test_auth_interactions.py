"""Create Account, terms checkbox, and SSO-chooser interactions on Workday-shaped pages.

Regression tests for three staging stops seen on 2026-10-26:

* Hendrick: the authorized Create Account was not clicked. The tenant's Create
  Account is an aria-hidden button under a nameless ``click_filter``; the account
  call found no button and returned ``not_at_account_step``.
* Faith Technologies: the terms box looked checked but the site said
  "Error: Please check the box to continue". A script wrote ``input.checked``,
  which a React form ignores; the tenant wording was also not read as standard terms.
* KLA: an Apple / Google / LinkedIn chooser hid the email form behind
  "Sign in with email", so the run stopped on "Sign in inside the preview".

Synthetic fixtures only. Every request is answered from tests/fixtures by a
Playwright route, so no employer site is reached and no account is created.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from autofill import AutofillOptions, Status, autofill_application
from autofill.account_flow import check_registered, run_account_action
from autofill.credentials import Credentials, MemoryCredentialProvider, credential_scope
from autofill.dom_ui import _CHOOSE_JS
from autofill.extract import extract_page
from autofill.login import email_sign_in_button
from autofill.mapping import terms_category
from autofill.models import Control
from autofill.profile import CandidateProfile
from autofill.safeguards import SubmitBlockedError, activate_account

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "example.wd5.myworkdayjobs.com"
URL = f"https://{HOST}/en-US/Careers/job/Example-Engineer_R2/apply/applyManually"
EMAIL = "test.candidate@example.com"
SECRET = "Fixture-Pass-9!"


def _box(label: str, **extra) -> Control:
    return Control(kind="checkbox", label=label, selector="#terms", required=True, **extra)


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


def _register(page, *, standard_terms: bool) -> dict:
    return run_account_action(
        page,
        mode="register",
        email=EMAIL,
        username="",
        password=SECRET,
        confirm=SECRET,
        ats="workday",
        standard_terms=standard_terms,
    )


# --- Standard terms wording -------------------------------------------------


@pytest.mark.parametrize(
    "label",
    [
        "Yes, I have read and consent to the terms and conditions",
        "I understand and acknowledge the terms of use for Faith Technologies Incorporated (FTI).",
        "I have read and accept the Terms and Conditions",
        "I acknowledge the Candidate Privacy Notice",
        "I have read and understood the Applicant Privacy Policy",
        "I agree to the Terms & Conditions",
    ],
)
def test_workday_tenant_terms_wording_is_standard(label):
    assert terms_category(_box(label)) == "STANDARD_TERMS"


@pytest.mark.parametrize(
    "label",
    [
        "I understand and acknowledge the terms of use for Example Co and agree to receive job alerts",
        "I agree to the terms of use of Example Co and to share my data",
        "I consent to the terms and conditions and to binding arbitration",
        "Yes, I have read and consent to the terms and conditions and the use of AI",
        "I acknowledge the terms of use for Example Co that I certify are true",
        "I consent to a background check",
        "I agree",
        "I agree to receive marketing emails",
    ],
)
def test_wider_terms_wording_still_leaves_other_promises_manual(label):
    assert terms_category(_box(label)) == "CONSENT"


# --- Faith: the box must be accepted by the site, not just painted -----------


def test_script_checked_box_is_accepted_by_a_controlled_form(workday):
    page = workday("workday_terms_react.html")
    assert page.evaluate(_CHOOSE_JS, {"selector": "#input-7", "expected": "Yes", "multi": False}) is True
    assert page.evaluate("document.querySelector('#input-7').checked") is True
    assert page.evaluate("window.__accepted") is True
    # A second call with the same answer leaves the box checked (no toggle back).
    assert page.evaluate(_CHOOSE_JS, {"selector": "#input-7", "expected": "Yes", "multi": False}) is True
    assert page.evaluate("window.__accepted") is True


def test_check_registered_confirms_the_site_state(workday):
    page = workday("workday_terms_react.html")
    assert check_registered(page, "#input-7") is True
    assert page.evaluate("window.__accepted") is True


def test_box_the_site_refuses_is_reported_unchecked(browser):
    context = browser.new_context()
    try:
        page = context.new_page()
        page.set_content(
            "<label for='t'>I agree to the Terms of Service</label>"
            "<input id='t' type='checkbox' onclick='return false'>"
        )
        assert check_registered(page, "#t") is False
    finally:
        context.close()


def test_authorized_register_checks_tenant_terms_and_creates_account(workday):
    page = workday("workday_terms_react.html")
    result = _register(page, standard_terms=True)
    assert page.evaluate("window.__rejected") == 0
    assert page.evaluate("window.__created") == 1
    assert result["ok"] is True
    assert result["status"] == "EMAIL_VERIFICATION_REQUIRED"
    assert result["autoConsents"][0]["category"] == "STANDARD_TERMS"
    assert result["consentRows"][0]["checked"] is True
    assert SECRET not in str(result)
    assert result["status"] != "READY_FOR_HUMAN_SUBMIT"


# --- Hendrick: nameless click_filter over the aria-hidden Create Account ----


def test_nameless_click_filter_create_account_is_clicked(workday):
    page = workday("workday_create_account_wrapped.html")
    result = _register(page, standard_terms=True)
    assert result["ok"] is True, result
    assert result["outcome"] in {"registered", "verification_required"}
    assert page.evaluate("window.__created") == 1
    assert result["consentRows"][0]["checked"] is True


def test_flag_off_does_not_check_terms_or_press_create_account(workday):
    page = workday("workday_create_account_wrapped.html")
    result = _register(page, standard_terms=False)
    assert result["ok"] is False, result
    assert result["reason"] == "site_rejected"
    assert page.evaluate("window.__created") == 0
    assert page.locator("#input-7").is_checked() is False


def test_covered_click_refuses_a_foreign_overlay(browser):
    context = browser.new_context()
    try:
        page = context.new_page()
        page.set_content(
            "<div style='position:relative'>"
            "<button id='create' type='submit' aria-hidden='true' data-automation-id='createAccountSubmitButton'"
            " onclick='window.__created=1'>Create Account</button></div>"
            "<div id='modal' style='position:fixed;inset:0;background:#fff'>Cookie settings</div>"
        )
        with pytest.raises(SubmitBlockedError):
            activate_account(
                page, "[data-automation-id='createAccountSubmitButton']", "Create Account", at_point=True
            )
        assert page.evaluate("window.__created || 0") == 0
    finally:
        context.close()


def test_covered_click_never_reaches_submit_application(browser):
    context = browser.new_context()
    try:
        page = context.new_page()
        page.set_content("<button id='s' onclick='window.__sent=1'>Submit Application</button>")
        with pytest.raises(SubmitBlockedError):
            activate_account(page, "#s", "Submit Application", at_point=True)
        assert page.evaluate("window.__sent || 0") == 0
    finally:
        context.close()


# --- KLA: SSO chooser hides the email form ---------------------------------


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {
            "candidateId": "ref-a",
            "personal": {"full_name": "Test Candidate", "email": EMAIL, "phone": "+1 555 0100"},
        }
    )


def _run(page, provider=None):
    return autofill_application(
        URL,
        _profile(),
        provider,
        AutofillOptions(page=page, session_id="sess-kla", candidate_id="ref-a", protocol_version="0.7.0"),
    )


def test_email_button_is_found_and_sso_buttons_are_not(workday):
    page = workday("workday_sso_sign_in.html")
    found = email_sign_in_button(extract_page(page))
    assert found is not None
    assert found.name == "Sign in with email"


def test_sso_chooser_without_credentials_opens_email_form_and_types_nothing(workday):
    page = workday("workday_sso_sign_in.html")
    result = _run(page)
    assert "OPEN_EMAIL_SIGN_IN" in result.steps
    assert page.evaluate("window.__sso") == 0
    assert page.evaluate("window.__signedIn") == 0
    assert result.status in {Status.LOGIN_REQUIRED, Status.ACCOUNT_CREATION_REQUIRED}
    assert page.locator("#input-5").is_visible()
    assert page.locator("#input-4").input_value() == ""
    assert page.locator("#input-5").input_value() == ""


def test_sso_chooser_with_saved_credentials_signs_in_and_continues(workday):
    page = workday("workday_sso_sign_in.html")
    provider = MemoryCredentialProvider()
    provider.put("ref-a", credential_scope(URL), Credentials(site=credential_scope(URL), email=EMAIL, password=SECRET))
    result = _run(page, provider)
    assert page.evaluate("window.__sso") == 0
    assert page.evaluate("window.__signedIn") == 1
    assert result.login_status == "AUTHENTICATED"
    assert result.status not in {Status.LOGIN_REQUIRED, Status.LOGIN_FAILED}
    assert SECRET not in str(result)
