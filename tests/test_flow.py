"""Synthetic login, ATS, field, workflow, and isolation cases. No live accounts."""

import logging
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from autofill import (
    AutofillOptions,
    CandidateProfile,
    Credentials,
    HumanSubmissionRequired,
    MemoryCredentialProvider,
    SiteContext,
    Status,
    autofill_application,
)
from autofill.adapters import ADAPTERS, page_step
from autofill.ats import detect_ats
from autofill.dates import format_for_control
from autofill.engine import autofill_application as engine_entry
from autofill.mapping import map_field
from autofill.models import Control, Option
from autofill.safeguards import ActionClass, action_class, perform_click
from autofill.session import SessionStore

FIXTURE = Path(__file__).parent / "fixtures" / "multi_step_application.html"
PASSWORD = "fake-password-candidate-a"
DOMAIN = "boards.greenhouse.io"
URL = "https://boards.greenhouse.io/example/jobs/1"


def candidate_a() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {
            "candidateId": "candidate-a",
            "basics": {
                "full_name": "Avery Example",
                "email": "avery.example@example.com",
                "phone": "555-010-0142",
            },
            "address": {
                "line1": "42 Example Street",
                "city": "Example City",
                "state": "CA",
                "postalCode": "00042",
                "country": "United States",
            },
            "employment": [
                {"company": "Example Labs", "title": "Example Engineer", "startDate": "2022-01-15", "current": True},
                {
                    "company": "Sample Studio",
                    "title": "Example Associate",
                    "startDate": "2020-06-01",
                    "endDate": "2021-08-01",
                },
            ],
            "education": [
                {"school": "Example University", "degree": "Bachelor's Degree", "field": "Computer Science"}
            ],
            "internships": [{"company": "Sample Studio", "title": "Example Intern"}],
            "projects": [{"name": "Example Portal", "description": "A fictional project."}],
            "applicationAnswers": [
                {"question": "Are you legally authorized to work in the United States?", "answer": "Yes"},
                {"question": "Will you now or in the future require sponsorship?", "answer": "No"},
                {"question": "How did you hear about this role?", "answer": "Online job board"},
            ],
            "workAuthorization": "Yes",
            "sponsorship": "No",
            "salaryExpectation": "85000",
            "skills": ["Python"],
        }
    )


def provider() -> MemoryCredentialProvider:
    store = MemoryCredentialProvider()
    store.put(
        "candidate-a",
        DOMAIN,
        Credentials(site=DOMAIN, email="avery.example@example.com", password=PASSWORD),
    )
    return store


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch(headless=True)
        try:
            yield chromium
        finally:
            chromium.close()


def test_login_action_is_not_final_submit():
    assert action_class("Sign in") == ActionClass.LOGIN_SUBMIT_ALLOWED
    assert action_class("Log in", control_type="submit") == ActionClass.LOGIN_SUBMIT_ALLOWED
    assert action_class("Submit application") == ActionClass.FINAL_APPLICATION_SUBMIT_FORBIDDEN
    assert action_class("Finish") == ActionClass.FINAL_APPLICATION_SUBMIT_FORBIDDEN
    assert action_class("Complete application") == ActionClass.FINAL_APPLICATION_SUBMIT_FORBIDDEN
    assert action_class("Sign up") == ActionClass.OTHER


def test_dates_and_country_aliases_and_unknown_questions():
    assert format_for_control("2022-01-15", placeholder="MM/DD/YYYY") == "01/15/2022"
    assert format_for_control("2022-01-15", input_type="date") == "2022-01-15"
    assert format_for_control("2022-01-15", label="Start month") == "January 2022"
    assert format_for_control("Immediately", input_type="date") is None
    profile = candidate_a()
    country = map_field(
        Control(
            kind="select",
            label="Country",
            options=[Option("Canada", "Canada"), Option("United States", "United States")],
        ),
        profile,
    )
    assert country.option_label == "United States"
    state = map_field(
        Control(
            kind="select",
            label="State",
            options=[Option("CA", "California"), Option("NY", "New York")],
        ),
        profile,
    )
    assert state.option_label == "California"
    unknown = map_field(Control(kind="text", label="Favorite animal?"), profile)
    assert unknown.action == "unanswered"
    assert unknown.field_class == "UNKNOWN_FIELD"
    legal = map_field(Control(kind="checkbox", label="I agree to the terms of service"), profile)
    assert legal.action == "skip"
    assert legal.field_class == "LEGAL_FIELD"
    low = map_field(Control(kind="text", label="Details", nearby="Project name"), profile)
    assert low.confidence == "low"
    assert low.action == "unanswered"


def test_salary_and_work_auth_are_not_invented():
    empty = CandidateProfile.from_dict(
        {"personal": {"full_name": "Avery Example", "email": "avery.example@example.com"}}
    )
    salary = map_field(Control(kind="text", label="Desired salary"), empty)
    assert salary.action == "unanswered"
    assert salary.text == ""
    auth = map_field(
        Control(
            kind="select",
            label="Are you legally authorized to work?",
            options=[Option("Yes", "Yes"), Option("No", "No")],
        ),
        empty,
    )
    assert auth.action == "unanswered"


def test_profile_extended_shape_and_original_shape():
    profile = candidate_a()
    assert profile.candidate_id == "candidate-a"
    assert profile.personal.address == "42 Example Street"
    assert profile.personal.province_state == "CA"
    assert profile.personal.country == "United States"
    assert profile.compensation.salary_expectation == "85000"
    assert profile.work_authorization.legally_authorized_to_work == "Yes"
    assert profile.work_authorization.require_sponsorship == "No"
    assert [job.company for job in profile.employment] == ["Example Labs", "Sample Studio"]
    assert profile.education_history[0].school == "Example University"
    assert profile.projects[0].name == "Example Portal"
    assert len(profile.application_answers) == 3


def test_ats_matrix_and_page_steps():
    cases = {
        "https://acme.wd5.myworkdayjobs.com/en-US/job/1": "workday",
        "https://boards.greenhouse.io/acme/jobs/1": "greenhouse",
        "https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555": "lever",
        "https://jobs.ashbyhq.com/acme/1111": "ashby",
        "https://jobs.smartrecruiters.com/Acme/1": "smartrecruiters",
        "https://fa-ext.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/1": "oracle",
        "https://careers-acme.icims.com/jobs/1/job": "icims",
        "https://acme.taleo.net/careersection/application": "taleo",
        "https://acme.successfactors.com/career": "successfactors",
        "https://acme.dayforcehcm.com/CandidatePortal": "dayforce",
    }
    for url, name in cases.items():
        info = detect_ats(url)
        assert info is not None and info.name == name
        assert name in ADAPTERS
    assert detect_ats("https://fa-ext.fa.ocs.oraclecloud.com/other/app") is None
    assert page_step("workday", "My Information") == "my information"
    assert page_step("oracle", "Review") == "review"
    assert page_step("greenhouse", "Education") == "education"


def test_credential_lookup_is_scoped():
    store = provider()
    other = SiteContext(url="https://jobs.lever.co/acme/1", domain="jobs.lever.co", candidate_id="candidate-a")
    assert store.get_credentials(other, "candidate-a") is None
    wrong_person = SiteContext(url=URL, domain=DOMAIN, candidate_id="candidate-b")
    assert store.get_credentials(wrong_person, "candidate-b") is None
    found = store.get_credentials(SiteContext(url=URL, domain=DOMAIN, candidate_id="candidate-a"), "candidate-a")
    assert found is not None
    assert "password" not in repr(found)
    assert PASSWORD not in repr(found)


def test_sessions_are_isolated_and_not_global():
    import autofill.engine as engine
    import autofill.session as session_mod

    assert not hasattr(engine, "last_profile")
    assert not hasattr(session_mod, "last_profile")
    store = SessionStore()
    first = store.open(profile=candidate_a(), application_url=URL, job_id="job-1", candidate_id="candidate-a")
    other = CandidateProfile.from_dict(
        {"candidateId": "candidate-b", "personal": {"full_name": "Blair Example", "email": "blair.example@example.com"}}
    )
    second = store.open(
        profile=other,
        application_url="https://jobs.lever.co/acme/2",
        job_id="job-2",
        candidate_id="candidate-b",
    )
    assert first.session_id != second.session_id
    assert first.profile.personal.email != second.profile.personal.email
    assert "avery.example@example.com" not in str(second.safe_log())
    assert set(first.safe_log()) == {"sessionId", "candidateId", "jobId", "domain", "step"}


def test_guard_blocks_a_hostile_final_submit(browser):
    page = browser.new_page()
    try:
        page.set_content("<button id='go' type='submit'>Submit application</button><script>window.__n=0</script>")
        page.locator("#go").evaluate("(el) => el.addEventListener('click', () => { window.__n += 1 })")
        with pytest.raises(HumanSubmissionRequired):
            perform_click(page, "#go", "Submit application", control_type="submit", purpose="login")
        with pytest.raises(HumanSubmissionRequired):
            perform_click(page, "#go", "Finish", control_type="submit", purpose="navigation")
        assert page.evaluate("() => window.__n") == 0
    finally:
        page.close()


def test_full_application_stops_for_a_person(browser, caplog):
    caplog.set_level(logging.DEBUG)
    profile = candidate_a()
    page = browser.new_page()
    try:
        page.goto(FIXTURE.as_uri())
        first = autofill_application(
            URL,
            profile,
            provider(),
            AutofillOptions(job_id="job-1", candidate_id="candidate-a", page=page, session_id="session-a"),
        )
        assert first.status == Status.RESUME_UPLOAD_REQUIRED
        assert first.login_status == "AUTHENTICATED"
        assert "LOGIN_SUBMIT" in first.steps
        assert "AUTHENTICATED" in first.steps
        assert "APPLICATION_READY" in first.steps
        assert first.current_step == "RESUME_UPLOAD_REQUIRED"
        assert first.ats == "greenhouse"
        assert first.stopped_before_submit is True
        assert page.locator("#first").input_value() == "Do Not Overwrite"
        assert page.locator("#last").input_value() == "Example"
        assert page.locator("#email").input_value() == "avery.example@example.com"
        assert page.locator("#city").input_value() == "Example City"
        assert page.locator("#city-mirror").inner_text() == "Example City"
        assert page.locator("#street").input_value() == "42 Example Street"
        assert page.locator("#state").input_value() == "CA"
        assert page.locator("#country").input_value() == "United States"
        assert page.locator("#employer-0").input_value() == "Example Labs"
        assert page.locator("#title-0").input_value() == "Example Engineer"
        assert page.locator("#start-0").input_value() == "01/15/2022"
        assert page.locator("#employer-1").input_value() == "Sample Studio"
        assert page.locator("#project-name").input_value() == "Example Portal"
        assert page.locator("#intern-org").input_value() == "Sample Studio"
        assert page.locator("#school").input_value() == "Example University"
        assert page.locator("#degree").input_value() == "Bachelor's Degree"
        assert page.locator("#major").input_value() == "Computer Science"
        assert page.locator("#auth").input_value() == "Yes"
        assert page.locator("#sponsor-no").is_checked()
        assert page.locator("#hear").input_value() == "Online job board"
        assert page.locator("#salary").input_value() == "85000"
        assert page.locator("#animal").input_value() == ""
        assert page.evaluate("() => document.querySelector('#resume').files.length") == 0
        assert page.evaluate("() => window.__submitCount") == 0
        assert PASSWORD not in caplog.text
        assert "Example Portal" not in caplog.text

        page.locator("#resume").set_input_files(
            {
                "name": "avery-example-resume.pdf",
                "mimeType": "application/pdf",
                "buffer": b"%PDF-1.4 fictional",
            }
        )
        second = engine_entry(
            URL,
            profile,
            provider(),
            AutofillOptions(
                job_id="job-1",
                candidate_id="candidate-a",
                page=page,
                session_id="session-a-resume",
                resume_uploaded=True,
            ),
        )
        assert second.status == Status.READY_FOR_HUMAN_SUBMIT
        assert "REVIEW_PAGE" in second.steps
        assert "CONTINUE_AFTER_RESUME" in second.steps
        assert page.locator("#terms").is_checked() is False
        assert page.evaluate("() => window.__submitCount") == 0
        assert "Submit application" in second.submit_controls
    finally:
        page.close()


def test_login_variants(browser):
    profile = candidate_a()
    creds = provider()

    def run(html: str, **options):
        page = browser.new_page()
        page.set_content(html)
        result = autofill_application(
            URL,
            profile,
            creds,
            AutofillOptions(candidate_id="candidate-a", page=page, **options),
        )
        return page, result

    user_html = """
    <h1>Sign in</h1>
    <label for="user">Username</label><input id="user" autocomplete="username">
    <label for="pw">Password</label><input id="pw" type="password" autocomplete="current-password">
    <button type="button" id="go">Sign in</button>
    <section id="app" hidden><h1>Personal information</h1>
      <label for="last">Last name</label><input id="last" autocomplete="family-name">
      <button type="submit" id="submit">Submit application</button>
    </section>
    <script>
      window.__submitCount = 0;
      document.getElementById("go").onclick = () => {
        if (document.getElementById("pw").value === "fake-password-candidate-a") {
          document.querySelector("h1").parentElement.hidden = false;
          document.getElementById("user").closest("body").querySelector("h1").hidden = true;
          document.getElementById("user").hidden = true;
          document.getElementById("pw").hidden = true;
          document.getElementById("go").hidden = true;
          document.getElementById("app").hidden = false;
        }
      };
      document.getElementById("submit").onclick = () => { window.__submitCount += 1; };
    </script>
    """
    # Username flow uses a dedicated provider record with a username.
    user_provider = MemoryCredentialProvider()
    user_provider.put(
        "candidate-a",
        DOMAIN,
        Credentials(site=DOMAIN, username="avery-example", password=PASSWORD),
    )
    page = browser.new_page()
    try:
        page.set_content(user_html)
        result = autofill_application(
            URL, profile, user_provider, AutofillOptions(candidate_id="candidate-a", page=page, session_id="user-pass")
        )
        assert result.login_status == "AUTHENTICATED"
        assert page.locator("#user").input_value() == "avery-example"
        assert page.locator("#last").input_value() == "Example"
        assert page.evaluate("() => window.__submitCount") == 0
    finally:
        page.close()

    email_first = """
    <section id="login"><h1>Sign in</h1>
      <label for="email">Email</label><input id="email" type="email" autocomplete="username">
      <button type="button" id="continue">Continue</button>
      <div id="pwbox" hidden>
        <label for="pw">Password</label><input id="pw" type="password" autocomplete="current-password">
        <button type="button" id="go">Sign in</button>
      </div>
      <p id="login-error" role="alert" hidden>Invalid credentials</p>
    </section>
    <section id="app" hidden><h1>Personal information</h1>
      <label for="last">Last name</label><input id="last" autocomplete="family-name">
    </section>
    <script>
      document.getElementById("continue").onclick = () => { document.getElementById("pwbox").hidden = false; };
      document.getElementById("go").onclick = () => {
        if (document.getElementById("pw").value === "fake-password-candidate-a") {
          document.getElementById("login").hidden = true;
          document.getElementById("app").hidden = false;
        } else {
          document.getElementById("login-error").hidden = false;
        }
      };
    </script>
    """
    page = browser.new_page()
    try:
        page.set_content(email_first)
        result = autofill_application(
            URL, profile, creds, AutofillOptions(candidate_id="candidate-a", page=page, session_id="email-first")
        )
        assert "LOGIN_CONTINUE" in result.steps
        assert result.login_status == "AUTHENTICATED"
        assert page.locator("#last").input_value() == "Example"
    finally:
        page.close()

    page = browser.new_page()
    try:
        page.goto(FIXTURE.as_uri())
        bad = MemoryCredentialProvider()
        bad.put(
            "candidate-a",
            DOMAIN,
            Credentials(site=DOMAIN, email="avery.example@example.com", password="wrong-password"),
        )
        result = autofill_application(
            URL, profile, bad, AutofillOptions(candidate_id="candidate-a", page=page, session_id="bad-login")
        )
        assert result.status == Status.LOGIN_FAILED
        assert page.locator("#last").input_value() == ""
        assert page.evaluate("() => window.__submitCount") == 0
    finally:
        page.close()

    page = browser.new_page()
    try:
        page.goto(FIXTURE.as_uri())
        result = autofill_application(
            URL, profile, None, AutofillOptions(candidate_id="candidate-a", page=page, session_id="missing")
        )
        assert result.status == Status.LOGIN_REQUIRED
        assert page.locator("#login-password").input_value() == ""
        assert page.evaluate("() => window.__loginClicks") == 0
        assert page.locator("#last").input_value() == ""
    finally:
        page.close()


def test_already_authenticated_skips_login(browser):
    page = browser.new_page()
    try:
        page.set_content(
            """
            <h1>My Information</h1>
            <label for="last">Last name</label><input id="last" autocomplete="family-name">
            <button type="submit" id="submit">Submit application</button>
            <script>window.__n=0; document.getElementById("submit").onclick=()=>{window.__n+=1};</script>
            """
        )
        result = autofill_application(
            URL,
            candidate_a(),
            None,
            AutofillOptions(candidate_id="candidate-a", page=page, session_id="already"),
        )
        assert result.login_status == "NOT_REQUIRED"
        assert result.status == Status.READY_FOR_HUMAN_SUBMIT
        assert page.locator("#last").input_value() == "Example"
        assert page.evaluate("() => window.__n") == 0
    finally:
        page.close()


def test_parallel_results_do_not_share_session_ids(browser):
    profile = candidate_a()
    page_a = browser.new_page()
    page_b = browser.new_page()
    try:
        page_a.set_content(
            "<h1>My Information</h1><label for='a'>Last name</label>"
            "<input id='a' autocomplete='family-name'>"
        )
        page_b.set_content(
            "<h1>My Information</h1><label for='b'>City</label><input id='b' autocomplete='address-level2'>"
        )
        left = autofill_application(
            URL,
            profile,
            None,
            AutofillOptions(candidate_id="candidate-a", job_id="job-1", page=page_a, session_id="left"),
        )
        right = autofill_application(
            "https://jobs.lever.co/acme/2",
            profile,
            None,
            AutofillOptions(candidate_id="candidate-a", job_id="job-2", page=page_b, session_id="right"),
        )
        assert left.session_id != right.session_id
        assert left.job_id != right.job_id
        assert left.ats == "greenhouse"
        assert right.ats == "lever"
        assert page_a.locator("#a").input_value() == "Example"
        assert page_b.locator("#b").input_value() == "Example City"
    finally:
        page_a.close()
        page_b.close()


@pytest.mark.parametrize(
    ("url", "ats_name"),
    [
        ("https://acme.wd5.myworkdayjobs.com/en-US/job/1", "workday"),
        ("https://boards.greenhouse.io/acme/jobs/1", "greenhouse"),
        ("https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555", "lever"),
        ("https://jobs.ashbyhq.com/acme/1111", "ashby"),
        ("https://jobs.smartrecruiters.com/Acme/1", "smartrecruiters"),
        ("https://fa-ext.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/1", "oracle"),
        ("https://careers-acme.icims.com/jobs/1/job", "icims"),
        ("https://acme.taleo.net/careersection/application", "taleo"),
        ("https://jobs.example.test/apply/1", "generic"),
    ],
)
def test_ats_synthetic_pages_fill_and_stop(browser, url, ats_name):
    """Each ATS URL is paired with a local page. Detection uses the URL."""
    fixture = Path(__file__).parent / "fixtures" / "ats" / f"{ats_name}.html"
    page = browser.new_page()
    try:
        page.goto(fixture.as_uri())
        result = autofill_application(
            url,
            candidate_a(),
            None,
            AutofillOptions(candidate_id="candidate-a", page=page, session_id=f"ats-{ats_name}"),
        )
        assert result.ats == (None if ats_name == "generic" else ats_name)
        assert result.status == Status.READY_FOR_HUMAN_SUBMIT
        assert result.stopped_before_submit is True
        assert page.locator("#last").input_value() == "Example"
        assert page.evaluate("() => window.__submitCount") == 0
        assert "Submit application" in result.submit_controls
    finally:
        page.close()


def test_public_api_stops_on_captcha_without_solving(browser):
    page = browser.new_page()
    try:
        page.set_content(
            """
            <h1>Sign in</h1>
            <label for="email">Email</label><input id="email" type="email">
            <div class="g-recaptcha" data-sitekey="not-a-real-sitekey"></div>
            <button type="button">Sign in</button>
            """
        )
        result = autofill_application(
            URL,
            candidate_a(),
            provider(),
            AutofillOptions(candidate_id="candidate-a", page=page, session_id="captcha"),
        )
        assert result.status == Status.CAPTCHA_REQUIRED
        assert result.login_status == "NOT_REQUIRED"
        assert page.locator("#email").input_value() == ""
        assert "LOGIN_SUBMIT" not in result.steps
    finally:
        page.close()


def test_package_source_does_not_log_password_values():
    root = Path(__file__).resolve().parents[1] / "src" / "autofill"
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "capsolver" not in text.casefold()
        assert "api.capsolver.com" not in text.casefold()
        for line in text.splitlines():
            if "logger." in line or "logging." in line:
                assert ".password" not in line
