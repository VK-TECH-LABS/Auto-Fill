"""An employer outage page is a retryable stop, and a job page with a chat box is never READY."""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from autofill.engine import AutofillOptions, Status, autofill_application
from autofill.models import ButtonControl, Control, PageSnapshot
from autofill.pagewait import application_controls, site_error_messages, terminal_kind
from autofill.profile import CandidateProfile

WORKDAY_OUTAGE = (
    "<html><head><title>Workday</title></head><body><h1>Workday is currently unavailable.</h1>"
    "<p>We are experiencing a service interruption. Please check back soon.</p></body></html>"
)
VIEGA_CLOSED = (
    "<html><body><h1>We're sorry… the job you are trying to apply for has been filled.</h1>"
    "<button>Sign up</button><a href='#'>Explore Jobs</a>"
    "<div><input type='text' aria-label='Chatbot User Input Box With Send Button'><button>send</button></div>"
    "</body></html>"
)


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {"candidateId": "ref-a", "personal": {"full_name": "River Example", "email": "r@example.com"}}
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


def _run_html(browser, html: str):
    page = browser.new_page()
    try:
        page.route(
            "https://employer.example/**", lambda route: route.fulfill(status=200, content_type="text/html", body=html)
        )
        page.goto("https://employer.example/job/1")
        return autofill_application(
            "https://employer.example/job/1",
            _profile(),
            None,
            AutofillOptions(page=page, session_id="sess-outage", candidate_id="ref-a", timings={}),
        )
    finally:
        page.close()


def test_outage_text_names_employer_site_unavailable():
    snap = PageSnapshot(
        controls=[], buttons=[], heading="Workday is currently unavailable", page_text="service interruption"
    )
    assert site_error_messages(snap) == ["site_error", "employer_site_unavailable"]
    assert terminal_kind(snap, url="https://x.wd1.myworkdayjobs.com/a", gaps=[], required_total=0) == "SITE_ERROR"


def test_maintenance_work_in_a_job_description_is_not_an_outage():
    snap = PageSnapshot(
        controls=[],
        buttons=[ButtonControl(name="Apply")],
        heading="Maintenance Technician",
        page_text="Responsibilities: perform scheduled maintenance on equipment under maintenance contracts.",
    )
    assert site_error_messages(snap) == ["site_error"]
    assert terminal_kind(snap, url="https://example.com/job", gaps=[], required_total=0) != "SITE_ERROR"


def test_workday_outage_page_is_retryable_not_no_form(browser):
    result = _run_html(browser, WORKDAY_OUTAGE)
    assert result.status == Status.FAILED_RETRYABLE
    assert "employer_site_unavailable" in result.messages


def test_chat_box_is_not_an_application_field():
    chat = Control(kind="text", input_type="text", aria_label="Chatbot User Input Box With Send Button")
    assert application_controls(PageSnapshot(controls=[chat], buttons=[])) == []


def test_closed_posting_with_chat_box_is_never_ready(browser):
    result = _run_html(browser, VIEGA_CLOSED)
    assert result.status != Status.READY_FOR_HUMAN_SUBMIT
