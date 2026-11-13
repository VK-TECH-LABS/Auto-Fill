"""The first employer page has to move: cookies, a posting Apply, or a closed job.

These fixtures never reach a real employer and never click Submit application.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from autofill.engine import AutofillOptions, Status, autofill_application
from autofill.models import ButtonControl, Control, Option, PageSnapshot
from autofill.pagewait import classify_page, find_entry_control, is_job_closed, terminal_kind
from autofill.profile import CandidateProfile
from autofill.safeguards import is_entry_control

EMAIL = "river.example@example.com"

COOKIE_THEN_APPLY = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Power Systems Engineer</title></head>
<body>
  <div id="cookie" role="dialog" aria-modal="true" aria-label="Cookie Preferences">
    <h2>Cookie Preferences</h2>
    <p>We use cookies on this site. Choose accept or reject.</p>
    <button type="submit" id="accept">Accept</button>
    <button type="button" id="reject">Reject</button>
  </div>
  <header>
    <a href="/login">Sign In</a>
    <button id="apply" type="submit">Apply</button>
  </header>
  <main>
    <h1>Power Systems Engineer</h1>
    <p>Job description. Responsibilities and qualifications for this role.</p>
    <label>Language
      <select id="lang" name="language">
        <option>English</option>
        <option>Español</option>
      </select>
    </label>
  </main>
  <script>
    window.__clicked = [];
    document.getElementById("reject").addEventListener("click", (event) => {
      event.preventDefault();
      window.__clicked.push("Reject");
      document.getElementById("cookie").remove();
    });
    document.getElementById("accept").addEventListener("click", (event) => {
      event.preventDefault();
      window.__clicked.push("Accept");
      document.getElementById("cookie").remove();
    });
    document.getElementById("apply").addEventListener("click", (event) => {
      event.preventDefault();
      window.__clicked.push("Apply");
      document.querySelector("main").innerHTML =
        "<h1>Application</h1>" +
        "<label>Email <input id='email' type='email' name='email'></label>" +
        "<button id='submit' type='submit'>Submit application</button>";
      document.getElementById("submit").addEventListener("click", (ev) => {
        ev.preventDefault();
        window.__clicked.push("Submit application");
      });
    });
  </script>
</body>
</html>
"""

HEADER_APPLY = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Electrical Controls Engineer</title></head>
<body>
  <header>
    <a href="/login">Sign In</a>
    <button id="apply" type="submit">Apply</button>
  </header>
  <main>
    <h1>Electrical Controls Engineer</h1>
    <p>Job description. Responsibilities and qualifications for this role.</p>
    <label>Language
      <select id="lang" name="language">
        <option>English</option>
        <option>Español</option>
      </select>
    </label>
  </main>
  <script>
    window.__clicked = [];
    document.getElementById("apply").addEventListener("click", (event) => {
      event.preventDefault();
      window.__clicked.push("Apply");
      document.querySelector("main").innerHTML =
        "<h1>Application</h1>" +
        "<label>Email <input id='email' type='email' name='email'></label>" +
        "<button id='submit' type='submit'>Submit application</button>";
      document.getElementById("submit").addEventListener("click", (ev) => {
        ev.preventDefault();
        window.__clicked.push("Submit application");
      });
    });
  </script>
</body>
</html>
"""

EXPIRED_JOB = """<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Electrical Engineer</title></head>
<body>
  <h1>Sorry, this job has expired</h1>
  <p>This job has expired.</p>
  <button id="apply" type="button">Apply</button>
  <script>
    window.__clicked = [];
    document.getElementById("apply").addEventListener("click", () => {
      window.__clicked.push("Apply");
    });
  </script>
</body>
</html>
"""


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {"candidateId": "ref-a", "personal": {"full_name": "River Example", "email": EMAIL}}
    )


def _posting_with_language_and_header_apply() -> PageSnapshot:
    return PageSnapshot(
        heading="Electrical Controls Engineer",
        page_text="Job description. Responsibilities and qualifications.",
        controls=[
            Control(
                kind="select",
                label="Language",
                selector="#lang",
                options=[Option("en", "English"), Option("es", "Español")],
            )
        ],
        buttons=[
            ButtonControl(name="Sign In", selector="#signin", in_header=True, in_main=False),
            ButtonControl(
                name="Apply",
                selector="#apply",
                control_type="submit",
                in_header=True,
                in_main=False,
            ),
        ],
    )


def test_expired_copy_is_a_closed_job_and_not_a_form():
    expired = PageSnapshot(
        controls=[],
        buttons=[ButtonControl(name="Apply", selector="#apply")],
        heading="Sorry, this job has expired",
        page_text="This job has expired.",
    )
    assert is_job_closed(expired) is True
    assert classify_page(expired) == "JOB_CLOSED"
    assert terminal_kind(expired, gaps=[], required_total=0) == "JOB_CLOSED"
    filled = PageSnapshot(
        controls=[],
        buttons=[],
        heading="We're sorry",
        page_text="The job you are trying to apply for has been filled.",
    )
    assert is_job_closed(filled) is True
    assert classify_page(filled) == "JOB_CLOSED"


def test_language_menu_does_not_hide_a_posting_apply():
    posting = _posting_with_language_and_header_apply()
    assert classify_page(posting) == "JOB_DESCRIPTION"
    chosen = find_entry_control(posting, on_posting=True)
    assert chosen is not None
    assert chosen.selector == "#apply"
    assert find_entry_control(posting) is None


def test_posting_apply_stays_off_the_final_submit_path():
    assert is_entry_control("Apply", control_type="submit", role="button") is False
    assert is_entry_control("Apply", control_type="submit", role="button", on_posting=True) is True
    assert is_entry_control("Apply", role="button", in_header=True, in_main=False) is False
    assert is_entry_control("Apply", role="button", in_header=True, in_main=False, on_posting=True) is True
    assert is_entry_control("Submit application", control_type="submit", role="button", on_posting=True) is False
    form = PageSnapshot(
        heading="Application",
        page_text="Job description.",
        controls=[Control(kind="text", label="Email", selector="#email", input_type="email")],
        buttons=[ButtonControl(name="Apply", selector="#apply", control_type="submit")],
    )
    assert classify_page(form) == "APPLICATION_FORM"


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


def _run_html(browser, html: str, url: str):
    page = browser.new_page()
    try:
        page.route(url, lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        page.goto(url)
        result = autofill_application(
            url,
            _profile(),
            None,
            AutofillOptions(page=page, session_id="sess-first-page", candidate_id="ref-a", timings={}),
        )
        clicked = page.evaluate("() => window.__clicked")
        email = ""
        if page.locator("#email").count():
            email = page.locator("#email").input_value()
        return result, clicked, email
    finally:
        page.close()


def test_cookie_reject_then_apply_and_stop_before_submit(browser):
    result, clicked, email = _run_html(
        browser,
        COOKIE_THEN_APPLY,
        "https://jobs.dayforcehcm.com/example/jobs/1",
    )
    assert clicked[0] == "Reject"
    assert "Apply" in clicked
    assert "Accept" not in clicked
    assert "Submit application" not in clicked
    assert email == EMAIL
    assert result.stopped_before_submit is True
    assert result.status != Status.NAVIGATION_STALLED


def test_header_submit_apply_on_a_job_description_opens_the_form(browser):
    result, clicked, email = _run_html(
        browser,
        HEADER_APPLY,
        "https://myjobs.adp.com/example/jobs/1",
    )
    assert clicked[0] == "Apply"
    assert "Submit application" not in clicked
    assert email == EMAIL
    assert result.stopped_before_submit is True
    assert result.page_type != "JOB_DESCRIPTION"


def test_expired_job_stops_without_clicking_apply(browser):
    result, clicked, _email = _run_html(
        browser,
        EXPIRED_JOB,
        "https://jobs.smartrecruiters.com/example/1",
    )
    assert clicked == []
    assert result.status == Status.FAILED_FINAL
    assert result.messages == ["job_expired"]
    assert result.page_type == "JOB_CLOSED"
    assert result.progress["stage"] == "CLOSED"
    assert result.progress["currentLabel"] == "Job closed"
    assert result.progress["percent"] == 0
    assert result.stopped_before_submit is True
