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
from autofill.mapping import account_checkbox_required, terms_category
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


def test_bare_i_accept_is_the_create_account_box_without_required():
    box = Control(kind="checkbox", label="I ACCEPT", selector="#box", required=False)
    assert terms_category(box) == "STANDARD_TERMS"
    assert account_checkbox_required(box) is True
    consent = Control(kind="checkbox", label="I consent", selector="#c", required=False)
    assert terms_category(consent) == "CONSENT"
    assert account_checkbox_required(consent) is False


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


_COVERED_ACCEPT = """
<main>
  <h2>Create Account</h2>
  <form onsubmit="return false">
    <label for="email">Email Address</label>
    <input id="email" type="text" data-automation-id="email">
    <label for="pw">Password</label>
    <input id="pw" type="password" data-automation-id="password">
    <label for="pw2">Verify New Password</label>
    <input id="pw2" type="password" data-automation-id="verifyPassword">
    <div data-automation-id="formField-createAccountCheckbox">
      <input id="box" type="checkbox" style="opacity:0;width:1px;height:1px;position:absolute;pointer-events:none">
      <label for="box">I have received the
        <button type="button" id="notice">Boeing Applicant Privacy Notice</button>
      </label>
    </div>
    <button type="button" id="create" data-automation-id="createAccountSubmitButton">Create Account</button>
  </form>
</main>
<script>
window.__accepted = false;
window.__created = 0;
window.__notice = 0;
var box = document.getElementById("box");
var native = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "checked");
var tracked = false;
Object.defineProperty(box, "checked", {
  configurable: true,
  get: function () { return native.get.call(this); },
  set: function (value) { tracked = !!value; native.set.call(this, value); }
});
function sync() {
  var now = native.get.call(box);
  if (now === tracked) return;
  tracked = now;
  window.__accepted = now;
}
box.addEventListener("click", sync);
document.getElementById("notice").addEventListener("click", function (event) {
  event.preventDefault();
  window.__notice += 1;
});
document.getElementById("create").addEventListener("click", function () {
  if (!window.__accepted) return;
  window.__created += 1;
  document.querySelector("main").remove();
});
</script>
"""


def test_covered_privacy_link_box_is_checked_without_a_required_attribute(browser):
    """Boeing: the notice is a button inside the label, and the box is not marked required.

    A script that only sets ``checked`` does not count. The privacy link is not clicked.
    """
    context = browser.new_context()
    page = context.new_page()
    try:
        page.set_content(_COVERED_ACCEPT)
        result = run_account_action(
            page,
            mode="register",
            email=EMAIL,
            username="",
            password=SECRET,
            confirm=SECRET,
            ats="workday",
            standard_terms=True,
        )
        assert page.evaluate("window.__notice") == 0
        assert page.evaluate("window.__accepted") is True
        assert page.evaluate("window.__created") == 1
        assert result["ok"] is True
        assert result["consentRows"][0]["category"] == "STANDARD_TERMS"
        assert result["consentRows"][0]["checked"] is True
        assert SECRET not in str(result)
    finally:
        context.close()


_WRAPPER_CONSENT = """
<main>
  <h2>Create Account</h2>
  <form onsubmit="return false">
    <div data-automation-id="email">
      <input id="email" type="text">
    </div>
    <input id="pw" type="password">
    <input id="pw2" type="password" data-automation-id="verifyPassword">
    <div data-automation-id="createAccountCheckbox">
      <input id="box" type="checkbox">
      <label for="box">I consent</label>
    </div>
    <label><input id="news" type="checkbox"> I agree to receive marketing emails</label>
    <button type="button" id="create" data-automation-id="createAccountSubmitButton">Create Account</button>
  </form>
</main>
<script>
window.__created = 0;
document.getElementById("box").addEventListener("change", function () {});
document.getElementById("create").addEventListener("click", function () {
  if (!document.getElementById("box").checked) return;
  window.__box = true;
  window.__news = document.getElementById("news").checked;
  window.__created += 1;
  document.querySelector("main").remove();
});
</script>
"""


def test_wrapper_i_consent_is_checked_and_marketing_is_not(browser):
    """KBR: ``createAccountCheckbox`` is on the wrapper. JCI-style ``I ACCEPT`` is covered separately."""
    context = browser.new_context()
    page = context.new_page()
    try:
        page.set_content(_WRAPPER_CONSENT)
        result = run_account_action(
            page,
            mode="register",
            email=EMAIL,
            username="",
            password=SECRET,
            confirm=SECRET,
            ats="workday",
            standard_terms=True,
        )
        assert page.evaluate("window.__created") == 1
        assert page.evaluate("window.__box") is True
        assert page.evaluate("window.__news") is False
        assert result["ok"] is True
        rows = {row["textExcerpt"]: row for row in result["consentRows"]}
        assert rows["I consent"]["checked"] is True
        assert rows["I consent"]["category"] == "STANDARD_TERMS"
        assert rows["I agree to receive marketing emails"]["checked"] is False
        assert SECRET not in str(result)
    finally:
        context.close()


def test_sign_in_email_id_on_the_wrapper_is_the_email_box(browser):
    context = browser.new_context()
    page = context.new_page()
    try:
        page.set_content(
            "<h2>Sign In</h2>"
            "<div data-automation-id='email'><input id='email' type='text'></div>"
            "<input id='pw' type='password'>"
            "<button type='button'>Sign In</button>"
        )
        from autofill.extract import extract_page

        found = _email_control(extract_page(page))
        assert found is not None and found.selector == "#email"
    finally:
        context.close()


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


def test_verified_continue_signs_in_while_the_banner_is_still_showing(sign_in):
    """SNC leaves "An email has been sent" on Sign In after the person verifies.

    Continue with the verified flag types the saved login. The banner alone does not.
    """
    page = sign_in(notice="An email has been sent to you. Please verify your account.")
    provider = MemoryCredentialProvider()
    provider.put("ref-a", credential_scope(URL), Credentials(site=credential_scope(URL), email=EMAIL, password=SECRET))
    result = autofill_application(
        URL,
        _profile(),
        provider,
        AutofillOptions(
            page=page,
            session_id="sess-verified",
            candidate_id="ref-a",
            protocol_version="0.7.0",
            human_resolved=True,
            verify_pending=False,
        ),
    )
    assert page.evaluate("window.__signedIn") == 1
    assert page.locator("#input-4").input_value() == EMAIL
    assert len(page.locator("#input-5").input_value()) == len(SECRET)
    # The fixture keeps the banner after the press, so the engine may still
    # report verification. The regression was refusing to type at all.
    assert SECRET not in str(result)
