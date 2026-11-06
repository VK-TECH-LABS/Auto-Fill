"""Saved-login Sign In on a Workday Create Account step (staging smoke 2026-10-27).

RC-L1: Nordson / Barry-Wehmiller / GE Vernova used a saved login on the Create
Account pane (Companion ``account_register_skipped reason=saved_login``, then
``mode=login``). The pane's real ``signInSubmitButton`` is aria-hidden under its
``click_filter`` overlay, so ``_primary_button`` fell back to the first button
named "Sign In": the header ``utilityButtonSignIn``. That click opened a second
Sign In modal and submitted nothing; the pane's password box disappeared, so the
press read as ``signed_in`` (Companion ``saved=no``). The resume run then found
two ``click_filter`` overlays and Playwright raised ``Error: strict mode
violation`` -> ``status=FAILED error_type=Error`` about 1.5 s after the POST.
Reproduced on live barrywehmiller.wd1 / aptiv.wd5 with every POST blocked.

RC-L2: a Workday Sign In press clears the pane and shows its spinner at once, so
an empty pane was read as signed in before a wrong-password or "verify your
account" banner could come back.

Synthetic fixture only. No real employer account.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from autofill.account_flow import _primary_button, run_account_action
from autofill.models import ButtonControl, PageSnapshot

FIXTURES = Path(__file__).parent / "fixtures"
HOST = "example.wd5.myworkdayjobs.com"
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
        html = (FIXTURES / "workday_saved_login_header.html").read_text()
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
        page.wait_for_selector("[data-automation-id='createAccountPage']")
        return page

    yield open_page
    for context in contexts:
        context.close()


def _login(page) -> dict:
    return run_account_action(
        page,
        mode="login",
        email=EMAIL,
        username="",
        password=SECRET,
        confirm="",
        ats="workday",
    )


def _button(name: str, automation_id: str, *, header: bool = False) -> ButtonControl:
    return ButtonControl(
        name=name,
        selector=f"[data-automation-id='{automation_id}']",
        automation_id=automation_id,
        in_header=header,
    )


def test_primary_button_skips_header_sign_in():
    snapshot = PageSnapshot(
        controls=[],
        buttons=[
            _button("Sign In", "utilityButtonSignIn", header=True),
            _button("Sign In", "click_filter"),
            _button("Create Account", "createAccountLink"),
        ],
        automation_ids=["utilityButtonSignIn", "click_filter", "signInSubmitButton"],
    )
    chosen = _primary_button(snapshot, "login")
    assert chosen is not None and chosen.automation_id == "click_filter"


def test_primary_button_uses_covered_pane_submit_when_only_header_is_named():
    snapshot = PageSnapshot(
        controls=[],
        buttons=[_button("Sign In", "utilityButtonSignIn", header=True)],
        automation_ids=["utilityButtonSignIn", "signInSubmitButton"],
    )
    chosen = _primary_button(snapshot, "login")
    assert chosen is not None and chosen.automation_id == "signInSubmitButton"


def test_primary_button_never_picks_utility_even_outside_header():
    snapshot = PageSnapshot(
        controls=[],
        buttons=[_button("Sign In", "utilityButtonSignIn")],
        automation_ids=["utilityButtonSignIn"],
    )
    assert _primary_button(snapshot, "login") is None


def test_saved_login_presses_pane_sign_in_not_header(workday):
    page = workday()
    report = _login(page)
    assert page.evaluate("window.__modalOpened") == 0
    assert page.evaluate("window.__paneSignIn") == 1
    assert report["ok"] is True and report["outcome"] == "signed_in"
    assert page.locator("[data-automation-id='applyFlowMyInfoPage']").count() == 1
    # One click_filter at most: the resume run's Sign In lookup stays unambiguous.
    assert page.locator("[data-automation-id='click_filter']").count() <= 1


def test_saved_login_unverified_is_verify_card_and_one_press(workday):
    page = workday(result="unverified")
    report = _login(page)
    assert page.evaluate("window.__modalOpened") == 0
    assert page.evaluate("window.__paneSignIn") == 1
    assert report["ok"] is False
    assert report["reason"] == "site_rejected"
    assert report["status"] == "EMAIL_VERIFICATION_REQUIRED"
    assert any("verify your account" in item.casefold() for item in report["siteErrors"])
    assert SECRET not in repr(report) and EMAIL not in repr(report)


def test_saved_login_that_reloads_the_document_is_not_an_error(workday):
    page = workday(result="reload")
    report = _login(page)
    assert page.evaluate("window.__modalOpened") == 0
    assert report["ok"] is True and report["outcome"] == "signed_in"


def test_slow_spinner_is_not_read_as_signed_in_early(workday):
    page = workday(result="unverified", spinnerMs="1500")
    report = _login(page)
    assert report["ok"] is False and report["status"] == "EMAIL_VERIFICATION_REQUIRED"
    assert page.evaluate("window.__paneSignIn") == 1


def test_switch_waits_for_sign_in_pane_to_render(workday):
    """RC-L3: the empty pane between Create Account and Sign In is not the Sign In pane."""
    page = workday(switchMs="700")
    report = _login(page)
    assert report.get("reason") != "not_at_account_step", report
    assert report["outcome"] == "signed_in", report
    assert page.evaluate("window.__paneSignIn") == 1
    assert page.evaluate("window.__modalOpened") == 0


# RC-S1 (Nordson / Barry-Wehmiller smoke after #77, 2026-10-27): Companion skipped
# register for a saved login and sent mode=login. The engine opened Sign In from
# Create Account, typed the saved login, and pressed at once. Live Workday ignores
# that press for about 0.5 s after the pane renders: no POST, the password stayed
# in the box, and the wait ended ``pending`` (Companion outcome=pending saved=no).


def test_saved_login_waits_for_new_sign_in_pane_before_pressing(workday):
    page = workday(armMs="600")
    report = _login(page)
    assert report["ok"] is True and report["outcome"] == "signed_in", report
    assert page.evaluate("window.__swallowed") == 0
    assert page.evaluate("window.__paneSignIn") == 1
    assert page.evaluate("window.__loginPosts") == 1
    assert page.locator("[data-automation-id='applyFlowMyInfoPage']").count() == 1


def test_ignored_press_is_pressed_once_more(workday):
    page = workday(armMs="1800")
    report = _login(page)
    assert report["ok"] is True and report["outcome"] == "signed_in", report
    assert page.evaluate("window.__swallowed") == 1
    assert page.evaluate("window.__presses") == 2
    assert page.evaluate("window.__loginPosts") == 1


def test_press_that_sent_a_request_is_never_repeated(workday):
    """A Sign In that reached the employer is one attempt, even if the pane stays."""
    page = workday(result="silent")
    report = _login(page)
    assert page.evaluate("window.__presses") == 1
    assert page.evaluate("window.__loginPosts") == 1
    assert report.get("outcome") != "signed_in", report


def test_never_more_than_two_presses(workday):
    page = workday(armMs="600000")
    report = _login(page)
    assert page.evaluate("window.__presses") == 2
    assert page.evaluate("window.__loginPosts") == 0
    assert report["ok"] is True and report["outcome"] == "pending", report
    assert report["status"] == "LOGIN_REQUIRED"
