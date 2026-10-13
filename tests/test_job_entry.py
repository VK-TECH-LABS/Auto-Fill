"""Job-description Apply opens the form. Header sign-in does not block it."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from autofill.engine import AutofillOptions, Status, autofill_application
from autofill.models import ButtonControl, LinkControl, PageSnapshot
from autofill.pagewait import classify_page, find_entry_control, is_login_wall, sign_in_pane
from autofill.profile import CandidateProfile
from autofill.safeguards import classify_control, is_entry_control, is_entry_label

FIXTURES = Path(__file__).parent / "fixtures"
EMAIL = "river.example@example.com"


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {"candidateId": "ref-a", "personal": {"full_name": "River Example", "email": EMAIL}}
    )


def _run(page, url: str):
    return autofill_application(
        url,
        _profile(),
        None,
        AutofillOptions(page=page, session_id="sess-entry", candidate_id="ref-a", timings={}),
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


def test_header_sign_in_does_not_hide_an_apply_entry():
    posting = PageSnapshot(
        controls=[],
        buttons=[
            ButtonControl(name="Log in", selector="#login", in_header=True, in_main=False),
            ButtonControl(name="Apply", selector="#apply", control_type="button"),
        ],
        links=[
            LinkControl(name="Sign in", selector="#sign-in", href="/login", in_header=True, in_main=False),
            LinkControl(name="Create account", selector="#create", href="/register", in_header=True, in_main=False),
        ],
        heading="Warehouse Associate",
        page_text="Job description. Responsibilities and qualifications.",
    )
    assert is_login_wall(posting) is False
    assert sign_in_pane(posting) is False
    assert classify_page(posting) == "JOB_DESCRIPTION"
    assert find_entry_control(posting) is not None
    assert find_entry_control(posting).selector == "#apply"


@pytest.mark.parametrize(
    "label",
    [
        "Start application",
        "Continue to application",
        "Apply to job",
        "Apply online",
        "Apply for this position",
        "Apply for this role",
        "Easy Apply",
        "Apply here",
    ],
)
def test_entry_phrases(label: str):
    assert is_entry_label(label) is True
    assert is_entry_control(label, role="button") is True
    assert classify_control("Apply") == "submit"
    assert classify_control("Apply now") == "submit"
    assert classify_control("Send") == "submit"
    assert classify_control("Complete application") == "submit"


def test_entry_matches_aria_automation_and_href_and_skips_chrome():
    assert is_entry_control("Go", aria_label="Apply here", role="button") is True
    assert is_entry_control("Go", automation_id="applyButton", role="button") is True
    assert is_entry_control("", href="/jobs/1/apply", role="link") is True
    assert is_entry_control("Privacy", href="/privacy-apply", role="link") is False
    assert is_entry_control("Learn more", href="/jobs/apply", role="link") is False
    assert is_entry_control("Apply", role="button", in_ad=True) is False
    assert is_entry_control("Apply", role="button", in_header=True, in_main=False) is False
    assert is_entry_control("Apply", role="button", disabled=True) is False
    assert is_entry_control("Apply", selector="#apply", control_type="submit", role="button") is False
    assert is_entry_control("Apply", selector="#apply_button", control_type="submit", role="button") is True
    assert classify_control("Back") == "other"
    assert classify_control("Save and continue") == "continue"


def test_adp_header_sign_in_still_clicks_apply(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "adp_job_description.html").as_uri())
        result = _run(page, "https://myjobs.adp.com/example/jobs/1")
        clicked = page.evaluate("() => window.__clicked")
        assert result.status != Status.LOGIN_REQUIRED
        assert result.stopped_before_submit is True
        assert clicked == ["Apply"]
        assert page.locator("#email").input_value() == EMAIL
    finally:
        page.close()


def test_apply_opens_a_new_tab_on_the_same_session(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "apply_new_tab.html").as_uri())
        result = _run(page, "https://myjobs.adp.com/example/jobs/1")
        assert result.stopped_before_submit is True
        assert result.status != Status.LOGIN_REQUIRED
        assert page.evaluate("() => window.__clicked") == ["Apply"]
        popup = next(item for item in page.context.pages if item is not page)
        assert popup.locator("#email").input_value() == EMAIL
        assert popup.evaluate("() => window.__clicked") == []
    finally:
        page.context.close()


def test_apply_inside_an_iframe_is_clicked(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "apply_iframe_job.html").as_uri())
        page.frame_locator("#apply-frame").locator("#apply").wait_for()
        result = _run(page, "https://myjobs.adp.com/example/jobs/1")
        assert result.status != Status.LOGIN_REQUIRED
        assert result.stopped_before_submit is True
        assert page.evaluate("() => window.__clicked") == ["Apply"]
        assert page.locator("#email").input_value() == EMAIL
    finally:
        page.close()


def test_multistep_next_save_and_review_stop_before_submit(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "multistep_review.html").as_uri())
        result = _run(page, "https://boards.greenhouse.io/example/jobs/1")
        clicked = page.evaluate("() => window.__clicked")
        assert clicked == ["Next", "Save & Continue"]
        assert "Submit application" not in clicked
        assert "Send" not in clicked
        assert result.stopped_before_submit is True
        assert result.status != Status.NAVIGATION_STALLED
    finally:
        page.close()


def test_stalled_apply_is_clicked_once(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "stalled_apply.html").as_uri())
        result = _run(page, "https://myjobs.adp.com/example/jobs/1")
        assert result.status == Status.NAVIGATION_STALLED
        assert result.messages == ["navigation_stalled"]
        assert page.evaluate("() => window.__clicked") == ["Apply"]
        assert result.stopped_before_submit is True
    finally:
        page.close()


def test_validation_error_blocks_next(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "validation_error.html").as_uri())
        result = _run(page, "https://boards.greenhouse.io/example/jobs/1")
        assert result.status == Status.MANUAL_ANSWER_REQUIRED
        assert result.messages == ["This field is required"]
        assert page.evaluate("() => window.__clicked") == []
        assert result.stopped_before_submit is True
    finally:
        page.close()
