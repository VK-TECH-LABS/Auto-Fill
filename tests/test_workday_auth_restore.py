"""Workday Create Account consent and Sign In restore (staging).

JCI left "I ACCEPT" unchecked, Boeing left "I have received the Boeing Applicant
Privacy Notice" unchecked, and KBR left "I consent" unchecked, so Create Account
never ran. SNC showed "An email has been sent to you. Please verify your account"
on Sign In with empty fields. Clark, NVIDIA, and Micron stayed on an empty Sign
In instead of using a saved login or the Create Account button.

Synthetic fixtures only. No employer site is contacted.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from autofill import AutofillOptions, Status, autofill_application
from autofill.account_flow import run_account_action
from autofill.credentials import Credentials, MemoryCredentialProvider, credential_scope
from autofill.login import _email_control, score_account, sign_in_verification_holding
from autofill.mapping import terms_category
from autofill.models import ButtonControl, Control, PageSnapshot
from autofill.pagewait import sign_in_pane
from autofill.profile import CandidateProfile

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "example.wd5.myworkdayjobs.com"
URL = f"https://{HOST}/en-US/Careers/job/Example-Engineer_R2/apply/applyManually"
EMAIL = "test.candidate@example.com"
SECRET = "Fixture-Pass-9!"


def _box(label: str, **kwargs) -> Control:
    return Control(kind="checkbox", label=label, selector="#terms", required=True, **kwargs)


def test_screenshot_privacy_sentences_are_standard_terms():
    assert terms_category(_box("I ACCEPT")) == "STANDARD_TERMS"
    assert terms_category(_box("I have received the Boeing Applicant Privacy Notice")) == "STANDARD_TERMS"
    assert terms_category(_box("I have received the Applicant Privacy Notice.")) == "STANDARD_TERMS"


def test_bare_i_consent_stays_manual_unless_it_is_the_create_account_box():
    assert terms_category(_box("I consent")) == "CONSENT"
    assert terms_category(_box("I agree")) == "CONSENT"
    checked = _box("I consent", automation_id="createAccountCheckbox")
    assert terms_category(checked) == "STANDARD_TERMS"
    unusual = _box("I consent to export compliance screening", automation_id="createAccountCheckbox")
    assert terms_category(unusual) == "CONSENT"
    arbitration = _box("I agree to binding arbitration", automation_id="createAccountCheckbox")
    assert terms_category(arbitration) == "CONSENT"


def test_workday_email_box_is_found_without_a_copied_label():
    snapshot = PageSnapshot(
        controls=[
            Control(
                kind="text",
                selector="#input-4",
                input_type="text",
                automation_id="email",
                form_automation_id="formField-email",
            ),
            Control(kind="password", label="Password", selector="#input-5", input_type="password"),
        ],
        buttons=[],
    )
    found = _email_control(snapshot)
    assert found is not None and found.selector == "#input-4"


def test_sign_in_with_a_create_account_button_offers_register():
    snapshot = PageSnapshot(
        heading="Sign In",
        password_present=True,
        page_text="Don't have an account yet? Create Account Forgot your password?",
        controls=[
            Control(kind="text", label="Email Address", selector="#email", input_type="text", automation_id="email"),
            Control(kind="password", label="Password", selector="#password", input_type="password"),
        ],
        buttons=[
            ButtonControl(name="Sign In", selector="#sign", automation_id="signInSubmitButton"),
            ButtonControl(name="Create Account", selector="#create"),
            ButtonControl(name="Forgot your password?", selector="#forgot"),
        ],
        automation_ids=["signInSubmitButton", "email", "password"],
    )
    scored = score_account(snapshot, URL)
    assert scored.mode == "login"
    assert scored.alternate is True
    assert sign_in_pane(snapshot) is True


def test_email_already_sent_holds_sign_in_and_does_not_look_like_create_account():
    snapshot = PageSnapshot(
        heading="Sign In",
        password_present=True,
        page_text="An email has been sent to you. Please verify your account.",
        controls=[
            Control(kind="text", label="Email Address", selector="#email", input_type="text"),
            Control(kind="password", label="Password", selector="#password", input_type="password"),
        ],
        buttons=[ButtonControl(name="Sign In", selector="#sign")],
    )
    assert sign_in_verification_holding(snapshot) is True
    create = PageSnapshot(
        heading="Create Account",
        password_present=True,
        page_text="Verify New Password Create Account",
        controls=[
            Control(kind="password", label="Password", selector="#p"),
            Control(kind="password", label="Verify New Password", selector="#v", automation_id="verifyPassword"),
        ],
        buttons=[ButtonControl(name="Create Account", selector="#c")],
        automation_ids=["verifyPassword", "createAccountSubmitButton"],
    )
    assert sign_in_verification_holding(create) is False


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
def sign_in(browser):
    contexts = []

    def open_page(*, notice: str = ""):
        html = (FIXTURES / "workday_sign_in.html").read_text()
        if notice:
            html = html.replace(
                '<h2 data-automation-id="headingText">Sign In</h2>',
                '<h2 data-automation-id="headingText">Sign In</h2>'
                f'<p role="alert">{notice}</p>',
            )
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


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {"candidateId": "ref-a", "personal": {"full_name": "Test Candidate", "email": EMAIL, "phone": "+1 555 0100"}}
    )


def test_verification_notice_is_not_typed_into(sign_in):
    page = sign_in(notice="An email has been sent to you. Please verify your account.")
    result = run_account_action(
        page,
        mode="login",
        email=EMAIL,
        username="",
        password=SECRET,
        confirm="",
        ats="workday",
    )
    assert result["ok"] is True
    assert result["status"] == "EMAIL_VERIFICATION_REQUIRED"
    assert result.get("verifyBeforeSignIn") is True
    assert page.locator("#input-4").input_value() == ""
    assert page.locator("#input-5").input_value() == ""
    assert page.evaluate("window.__signedIn") == 0
    assert SECRET not in str(result)


def test_saved_credentials_fill_the_sign_in_pane(sign_in):
    page = sign_in()
    provider = MemoryCredentialProvider()
    provider.put("ref-a", credential_scope(URL), Credentials(site=credential_scope(URL), email=EMAIL, password=SECRET))
    result = autofill_application(
        URL,
        _profile(),
        provider,
        AutofillOptions(page=page, session_id="sess-signin", candidate_id="ref-a", protocol_version="0.7.0"),
    )
    # The pane's Sign In was pressed and both boxes were filled. This fixture
    # stays on the form, so the engine reports the page still asking for a login.
    assert page.evaluate("window.__signedIn") == 1
    assert page.locator("#input-4").input_value() == EMAIL
    assert len(page.locator("#input-5").input_value()) == len(SECRET)
    assert result.status != Status.LOGIN_REQUIRED
    assert SECRET not in str(result)


def test_verification_notice_blocks_saved_credentials_until_continue(sign_in):
    page = sign_in(notice="An email has been sent to you. Please verify your account.")
    provider = MemoryCredentialProvider()
    provider.put("ref-a", credential_scope(URL), Credentials(site=credential_scope(URL), email=EMAIL, password=SECRET))
    result = autofill_application(
        URL,
        _profile(),
        provider,
        AutofillOptions(page=page, session_id="sess-hold", candidate_id="ref-a", protocol_version="0.7.0"),
    )
    assert result.status == Status.EMAIL_VERIFICATION_REQUIRED
    assert page.evaluate("window.__signedIn") == 0
    assert page.locator("#input-4").input_value() == ""
    assert page.locator("#input-5").input_value() == ""
    assert SECRET not in str(result)
