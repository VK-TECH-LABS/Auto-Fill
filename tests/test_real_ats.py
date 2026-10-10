"""Synthetic pages shaped like real ATS stops. No live sites and no personal data."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from autofill import AutofillOptions, Status, autofill_application
from autofill.profile import CandidateProfile
from autofill.resolver import ResolverBinding
from autofill.service.context import placeholder_profile

FIXTURES = Path(__file__).parent / "fixtures"
EMAIL = "river.example@example.com"
TOKEN = "synthetic-resolver-token-0123456789abcdef"
PDF = b"%PDF-1.1\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {"candidateId": "ref-a", "personal": {"full_name": "River Example", "email": EMAIL}}
    )


def _future() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


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


def _run(page, url: str, *, profile: CandidateProfile | None = None, **options):
    timings: dict[str, int] = {}
    result = autofill_application(
        url,
        profile or _profile(),
        None,
        AutofillOptions(page=page, session_id="sess-ats", candidate_id="ref-a", timings=timings, **options),
    )
    return result, timings


def test_unknown_radio_answer_matches_by_question_hash(browser, resolver):
    server, url = resolver

    def dynamic(body: dict) -> dict:
        answers = []
        for question in body.get("questions", []):
            text = str(question.get("text") or "")
            folded = text.casefold()
            if "singapore" in folded:
                answers.append(
                    {
                        "intent": question.get("id"),
                        "value": "No",
                        "confidence": "HIGH",
                        "source": "saved_answer",
                    }
                )
            elif "prime" in folded:
                answers.append(
                    {
                        "intent": None,
                        "text": "what is your favorite prime number",
                        "value": "17",
                        "confidence": "HIGH",
                        "source": "saved_answer",
                    }
                )
        return {"fields": {}, "answers": answers, "unresolved": []}

    server.dynamic = dynamic  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='color'>What is your favorite color?</label>"
            "<input id='color'>"
            "<label for='prime'>What is your favorite prime number?</label>"
            "<input id='prime'>"
            "<div class='application-question'>"
            "<div class='application-label'>Are you a Singapore citizen?</div>"
            "<ul>"
            "<li><label><input type='radio' name='citizen' value='Yes'> Yes</label></li>"
            "<li><label><input type='radio' name='citizen' value='No'> No</label></li>"
            "</ul></div>"
            "</form>"
        )
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        questions = [item for call in server.calls for item in call.get("questions", [])]
        citizen = next(item for item in questions if "Singapore" in str(item.get("text")))
        assert citizen.get("intent") is None
        assert citizen.get("text") == "Are you a Singapore citizen?"
        assert citizen.get("id")
        assert citizen.get("options") == ["Yes", "No"]
        assert page.locator("input[value='No']").is_checked()
        assert page.locator("input[value='Yes']").is_checked() is False
        assert page.locator("#prime").input_value() == "17"
        assert page.locator("#color").input_value() == ""
        texts = [item.get("text") for item in result.manual_questions]
        assert "Are you a Singapore citizen?" not in texts
        assert "What is your favorite prime number?" not in texts
    finally:
        page.close()


def test_keyless_saved_answer_is_not_applied_to_the_first_open_question(browser, resolver):
    server, url = resolver
    server.custom_answers = [  # type: ignore[attr-defined]
        {"intent": None, "value": "No", "confidence": "HIGH", "source": "saved_answer"},
    ]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='color'>What is your favorite color?</label>"
            "<input id='color'>"
            "<div class='application-question'>"
            "<div class='application-label'>Are you a Singapore citizen?</div>"
            "<ul>"
            "<li><label><input type='radio' name='citizen' value='Yes'> Yes</label></li>"
            "<li><label><input type='radio' name='citizen' value='No'> No</label></li>"
            "</ul></div></form>"
        )
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        assert page.locator("input[value='No']").is_checked() is False
        assert page.locator("#color").input_value() == ""
        texts = [item.get("text") for item in result.manual_questions]
        assert "Are you a Singapore citizen?" in texts
        assert "What is your favorite color?" in texts
    finally:
        page.close()


def test_echoed_question_hash_fills_the_matching_unknown_question(browser, resolver):
    server, url = resolver

    def dynamic(body: dict) -> dict:
        answers = []
        for question in body.get("questions", []):
            text = str(question.get("text") or "")
            if "Singapore" not in text:
                continue
            answers.append(
                {
                    "intent": None,
                    "questionHash": question.get("id"),
                    "value": "No",
                    "confidence": "HIGH",
                    "source": "saved_answer",
                }
            )
        return {"fields": {}, "answers": answers, "unresolved": []}

    server.dynamic = dynamic  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='color'>What is your favorite color?</label>"
            "<input id='color'>"
            "<div class='application-question'>"
            "<div class='application-label'>Are you a Singapore citizen?</div>"
            "<ul>"
            "<li><label><input type='radio' name='citizen' value='Yes'> Yes</label></li>"
            "<li><label><input type='radio' name='citizen' value='No'> No</label></li>"
            "</ul></div></form>"
        )
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        assert page.locator("input[value='No']").is_checked()
        assert page.locator("#color").input_value() == ""
        texts = [item.get("text") for item in result.manual_questions]
        assert "Are you a Singapore citizen?" not in texts
        assert "What is your favorite color?" in texts
    finally:
        page.close()


def test_greenhouse_labels_are_not_doubled_and_visa_sponsorship_is_classified(browser, resolver):
    server, url = resolver
    server.custom_answers = [  # type: ignore[attr-defined]
        {
            "intent": "SPONSORSHIP_NOW_OR_FUTURE",
            "value": "No",
            "confidence": "HIGH",
            "source": "saved_answer",
        },
        {
            "intent": None,
            "text": "what is your favorite prime number",
            "value": "17",
            "confidence": "HIGH",
            "source": "saved_answer",
        },
    ]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label id='gender-label' for='gender'>Gender*</label>"
            "<select id='gender' aria-labelledby='gender-label' aria-label='Gender'>"
            "<option value=''>Select</option>"
            "<option>Decline to self-identify</option></select>"
            "<label id='sponsor-label' for='sponsor'>"
            "Will you now or in the future require sponsorship for a visa to remain in your country?*"
            "</label>"
            "<select id='sponsor' aria-labelledby='sponsor-label' "
            "aria-label='Will you now or in the future require sponsorship for a visa to remain in your country?'>"
            "<option value=''>Select</option><option>Yes</option><option>No</option></select>"
            "<label id='q-label' for='prime'>What is your favorite prime number?*</label>"
            "<input id='prime' aria-labelledby='q-label' aria-label='What is your favorite prime number?'>"
            "</form>"
        )
        result, _timings = _run(page, "https://boards.greenhouse.io/example/jobs/1", resolver=_binding(url))
        questions = [item for call in server.calls for item in call.get("questions", [])]
        texts = [str(item.get("text") or "") for item in questions]
        sponsor = "Will you now or in the future require sponsorship for a visa to remain in your country?"
        assert texts.count("Gender") == 1
        assert "Gender Gender" not in texts
        assert all("*" not in text for text in texts)
        assert texts.count(sponsor) == 1
        assert texts.count("What is your favorite prime number?") == 1
        intents = [item.get("intent") for item in questions]
        assert "SPONSORSHIP_NOW_OR_FUTURE" in intents
        assert page.locator("#sponsor").input_value() == "No"
        assert page.locator("#prime").input_value() == "17"
        assert page.locator("#gender").input_value() == ""
        assert any(item.get("intent") == "GENDER" and item.get("text") == "Gender" for item in result.manual_questions)
    finally:
        page.close()


def test_workday_entry_clicks_apply_manually_and_not_submit(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "workday_entry.html").as_uri())
        result, _timings = _run(page, "https://example.wd5.myworkdayjobs.com/en-US/Example/job/Role")
        assert result.status == Status.READY_FOR_HUMAN_SUBMIT, result.messages
        assert page.evaluate("() => window.__clicked") == ["Apply Manually"]
        assert page.locator("#email").input_value() == EMAIL
        assert "Submit" in result.submit_controls
    finally:
        page.close()


def test_workday_login_wall_stops_for_credentials(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "workday_login_wall.html").as_uri())
        result, _timings = _run(page, "https://example.wd5.myworkdayjobs.com/en-US/Example/job/Role")
        assert result.status == Status.LOGIN_REQUIRED, result.messages
        assert page.evaluate("() => window.__clicked") == ["Apply Manually"]
        assert page.locator("#password").input_value() == ""
    finally:
        page.close()


def test_delayed_form_is_not_ready_with_zero_fields(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "ashby_delayed.html").as_uri())
        result, timings = _run(page, "https://jobs.ashbyhq.com/example/role")
        assert result.status != Status.NO_FORM_FOUND
        assert result.status != Status.READY_FOR_HUMAN_SUBMIT or result.fields_detected > 0
        assert result.fields_detected > 0
        assert page.locator("#email").input_value() == EMAIL
        assert timings["firstFormInspectedMs"] >= 400
        assert timings["firstFillMs"] >= timings["firstFormInspectedMs"]
    finally:
        page.close()


def test_datadome_block_is_captcha_not_ready(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "datadome_block.html").as_uri())
        result, timings = _run(page, "https://jobs.smartrecruiters.com/example/role")
        assert result.status == Status.CAPTCHA_REQUIRED, result.messages
        assert result.messages == ["site blocked automated access"]
        assert result.fields_detected == 0
        assert timings["firstFormInspectedMs"] < 8000
    finally:
        page.close()


def test_invisible_recaptcha_outside_the_badge_does_not_stop(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "invisible_recaptcha.html").as_uri())
        result, _timings = _run(page, "https://jobs.ashbyhq.com/example/role")
        assert result.status == Status.READY_FOR_HUMAN_SUBMIT, result.messages
        assert result.fields_filled > 0
        assert page.locator("#email").input_value() == EMAIL
        assert page.evaluate("() => window.__submitted") is not True
    finally:
        page.close()


def test_invisible_hcaptcha_does_not_stop(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "invisible_hcaptcha.html").as_uri())
        result, _timings = _run(page, "https://jobs.lever.co/example/role")
        assert result.status == Status.READY_FOR_HUMAN_SUBMIT, result.messages
        assert result.fields_filled > 0
        assert page.locator("#email").input_value() == EMAIL
    finally:
        page.close()


def test_visible_hcaptcha_checkbox_stops(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "visible_hcaptcha.html").as_uri())
        result, _timings = _run(page, "https://jobs.lever.co/example/role")
        assert result.status == Status.CAPTCHA_REQUIRED, result.messages
        assert "site blocked automated access" not in result.messages
        assert any("CAPTCHA" in message for message in result.messages)
        assert page.locator("#email").input_value() == ""
    finally:
        page.close()


def test_visible_challenge_after_next_stops(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "captcha_after_next.html").as_uri())
        result, _timings = _run(page, "https://jobs.ashbyhq.com/example/role")
        assert result.status == Status.CAPTCHA_REQUIRED, result.messages
        assert page.locator("#email").input_value() == EMAIL
        assert page.evaluate("() => window.__continued") is True
        assert result.fields_filled > 0
    finally:
        page.close()


def test_invisible_recaptcha_badge_does_not_stop(browser):
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Contact</h1>"
            "<label for='email'>Email</label><input id='email' type='email'>"
            "<div class='grecaptcha-badge'>"
            "<iframe src='/recaptcha/enterprise/anchor' title='badge'></iframe>"
            "</div>"
        )
        result, _timings = _run(page, "https://boards.greenhouse.io/example/jobs/1")
        assert result.status != Status.CAPTCHA_REQUIRED
        assert page.locator("#email").input_value() == EMAIL
    finally:
        page.close()


def test_job_description_apply_then_submit_stays_unclicked(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "job_description_apply.html").as_uri())
        result, _timings = _run(page, "https://boards.greenhouse.io/example/jobs/1")
        assert result.status == Status.READY_FOR_HUMAN_SUBMIT, result.messages
        assert page.evaluate("() => window.__clicked") == ["Apply"]
        assert page.locator("#email").input_value() == EMAIL
        assert "Submit application" in result.submit_controls
    finally:
        page.close()


class _Resolver(ThreadingHTTPServer):
    calls: list[dict]
    mode: str


@pytest.fixture
def resolver():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            server: _Resolver = self.server  # type: ignore[assignment]
            server.calls.append(body)
            custom_fields = getattr(server, "custom_fields", None)
            if server.mode == "resume":
                grant_name = getattr(server, "resume_filename", "Granted_Resume.pdf")
                descriptor = {
                    "url": server.resume_url,  # type: ignore[attr-defined]
                    "contentType": "application/pdf",
                    "expiresAt": _future(),
                }
                if isinstance(grant_name, str):
                    descriptor["filename"] = grant_name
                fields = {"resume.file": descriptor}
            elif server.mode == "file":
                fields = {"resume.file": {"url": "file:///tmp/resume.pdf", "filename": "resume.pdf"}}
            elif isinstance(custom_fields, dict):
                fields = custom_fields
            else:
                fields = {}
            custom = getattr(server, "custom_answers", None)
            answers = custom if isinstance(custom, list) else [
                {
                    "intent": "RELOCATE",
                    "value": "Yes",
                    "confidence": "HIGH",
                    "source": "saved_answer",
                },
                {
                    "intent": "GENDER",
                    "value": "Decline to self-identify",
                    "confidence": "HIGH",
                    "source": "saved_answer",
                },
                {
                    "intent": None,
                    "text": "Voluntary Self-Identification",
                    "value": "Yes",
                    "confidence": "HIGH",
                    "source": "saved_answer",
                },
            ]
            dynamic = getattr(server, "dynamic", None)
            if callable(dynamic):
                payload = dynamic(body)
            else:
                payload = {"fields": fields, "answers": answers, "unresolved": []}
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, fmt: str, *args) -> None:
            return

    server = _Resolver(("127.0.0.1", 0), Handler)
    server.calls = []
    server.mode = "questions"
    server.resume_url = ""  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield server, f"http://{host}:{port}/resolve"
    finally:
        server.shutdown()


def _binding(url: str) -> ResolverBinding:
    return ResolverBinding(
        url=url,
        token=TOKEN,
        expires_at=_future(),
        candidate_ref="ref-a",
        job_ref="job-a",
        timeout_seconds=5,
        retries=0,
        max_bytes=65536,
    )


def test_greenhouse_react_select_fills_and_eeo_stays_manual(browser, resolver):
    server, url = resolver
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "greenhouse_react_select.html").as_uri())
        result, _timings = _run(
            page,
            "https://boards.greenhouse.io/example/jobs/1",
            resolver=_binding(url),
        )
        assert result.status != Status.FAILED, result.messages
        assert "relocate-yes" in page.evaluate("() => window.__picked")
        picked = page.evaluate("() => window.__picked")
        assert "gender-decline" not in picked
        assert "eeo-yes" not in picked
        assert any(item.get("intent") == "GENDER" for item in result.manual_questions)
        assert any("Self-Identification" in (item.get("text") or "") for item in result.manual_questions)
    finally:
        page.close()


def test_referral_is_not_filled_from_employment(browser, resolver):
    server, url = resolver
    server.custom_answers = []  # type: ignore[attr-defined]
    server.custom_fields = {  # type: ignore[attr-defined]
        "employment[]": [{"company": "Example Works", "title": "Example Engineer"}],
        "firstName": "River",
    }
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='employer'>Employer</label><input id='employer' name='employer'>"
            "<label for='referral'>If an employee referred you, please list their name</label>"
            "<input id='referral' name='referral'>"
            "</form>"
        )
        result, _timings = _run(page, "https://boards.greenhouse.io/example/jobs/1", resolver=_binding(url))
        questions = list(server.calls[0].get("questions", []))
        referral = next(item for item in questions if "referred you" in str(item.get("text")))
        assert referral.get("intent") == "REFERRAL"
        requested = [key for call in server.calls for key in call.get("fields", [])]
        assert "employment[]" in requested
        assert page.locator("#employer").input_value() == "Example Works"
        assert page.locator("#referral").input_value() == ""
        manual = [item for item in result.manual_questions if item.get("intent") == "REFERRAL"]
        assert manual
        assert "referred you" in str(manual[0].get("text"))
        assert "Example Works" not in page.locator("#referral").input_value()
    finally:
        page.close()


def test_preferred_first_name_stays_blank_without_preferred_name(browser, resolver):
    server, url = resolver
    server.custom_answers = []  # type: ignore[attr-defined]
    server.custom_fields = {"firstName": "River"}  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='first'>First name</label><input id='first'>"
            "<label for='preferred'>Preferred First Name</label><input id='preferred'>"
            "</form>"
        )
        result, _timings = _run(page, "https://boards.greenhouse.io/example/jobs/1", resolver=_binding(url))
        requested = [key for call in server.calls for key in call.get("fields", [])]
        assert "firstName" in requested
        assert "preferredName" in requested
        assert page.locator("#first").input_value() == "River"
        assert page.locator("#preferred").input_value() == ""
        assert "Preferred First Name" not in [item.get("text") for item in result.manual_questions]
    finally:
        page.close()


def test_select2_demographics_are_listed_and_not_filled(browser, resolver):
    server, url = resolver
    server.custom_answers = []  # type: ignore[attr-defined]
    server.custom_fields = {}  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='gender'>Gender</label>"
            "<select id='gender'><option value=''>Select</option>"
            "<option>Decline to self-identify</option></select>"
            "<div class='field'>"
            "<label>Are you Hispanic/Latino?</label>"
            "<select id='hispanic-native' aria-hidden='true' style='display:none'>"
            "<option value=''>Select...</option><option>Yes</option><option>No</option></select>"
            "<span class='select2'><span class='selection'>"
            "<span id='hispanic-box' role='combobox' tabindex='0'>Select...</span>"
            "</span></span></div>"
            "<label for='race'>Race</label>"
            "<select id='race'><option value=''>Select</option><option>Decline</option></select>"
            "<div class='field'>"
            "<label>Veteran status</label>"
            "<select id='veteran-native' aria-hidden='true' style='display:none'>"
            "<option value=''>Select...</option><option>I am not a protected veteran</option></select>"
            "<span class='select2'><span class='selection'>"
            "<span id='veteran-box' role='combobox' tabindex='0'>Select...</span>"
            "</span></span></div>"
            "<div class='field'>"
            "<label>Disability status</label>"
            "<select id='disability-native' aria-hidden='true' style='display:none'>"
            "<option value=''>Select...</option><option>I do not wish to answer</option></select>"
            "<span class='select2'><span class='selection'>"
            "<span id='disability-box' role='combobox' tabindex='0'>Select...</span>"
            "</span></span></div>"
            "</form>"
        )
        result, _timings = _run(page, "https://boards.greenhouse.io/example/jobs/1", resolver=_binding(url))
        questions = list(server.calls[0].get("questions", []))
        by_intent = {item.get("intent"): item.get("text") for item in questions}
        assert by_intent.get("GENDER") == "Gender"
        assert by_intent.get("RACE_ETHNICITY") in {"Are you Hispanic/Latino?", "Race"}
        texts = [str(item.get("text") or "") for item in questions]
        assert "Are you Hispanic/Latino?" in texts
        assert "Race" in texts
        assert "Veteran status" in texts
        assert "Disability status" in texts
        manual = {item.get("intent"): item.get("text") for item in result.manual_questions}
        assert manual.get("GENDER") == "Gender"
        assert "Are you Hispanic/Latino?" in [item.get("text") for item in result.manual_questions]
        assert "Race" in [item.get("text") for item in result.manual_questions]
        assert manual.get("VETERAN_STATUS") == "Veteran status"
        assert manual.get("DISABILITY_STATUS") == "Disability status"
        assert page.locator("#gender").input_value() == ""
        assert page.locator("#race").input_value() == ""
        assert page.locator("#hispanic-native").input_value() == ""
        assert page.locator("#hispanic-box").inner_text() == "Select..."
    finally:
        page.close()


_SPONSOR = (
    "Will you now or in the future require sponsorship for a visa to remain in your current location?"
)


def _sponsorship_select(commit_on_enter: bool) -> str:
    enter = (
        "if ((input.value || '').trim().toLowerCase() === 'no') {"
        "hidden.value = 'No';"
        "error.hidden = true;"
        "error.textContent = '';"
        "input.removeAttribute('aria-invalid');"
        "}"
        if commit_on_enter
        else ""
    )
    return (
        "<h1>Application</h1><form><div class='field'>"
        f"<label for='sponsor'>{_SPONSOR}</label>"
        "<div class='select'><div class='select__control'>"
        "<div class='select__single-value' id='sponsor-shown' hidden></div>"
        "<input id='sponsor' class='select__input' role='combobox' aria-controls='sponsor-list'>"
        "</div>"
        "<input type='hidden' id='sponsor-hidden' name='sponsorship'>"
        "<div class='field-error' id='sponsor-error' hidden></div></div>"
        "<ul id='sponsor-list' role='listbox'>"
        "<li id='sponsor-yes' role='option'>Yes</li>"
        "<li id='sponsor-no' role='option'>No</li>"
        "</ul></div></form>"
        "<script>"
        "const input = document.getElementById('sponsor');"
        "const hidden = document.getElementById('sponsor-hidden');"
        "const shown = document.getElementById('sponsor-shown');"
        "const error = document.getElementById('sponsor-error');"
        "document.getElementById('sponsor-no').addEventListener('click', () => {"
        "shown.hidden = false;"
        "shown.textContent = 'No';"
        "hidden.value = '';"
        "error.hidden = false;"
        "error.textContent = 'Please select an option';"
        "input.setAttribute('aria-invalid', 'true');"
        "});"
        "input.addEventListener('keydown', (event) => {"
        "if (event.key !== 'Enter') return;"
        "event.preventDefault();"
        f"{enter}"
        "});"
        "document.querySelector('form').addEventListener('submit', (event) => event.preventDefault());"
        "</script>"
    )


def test_react_select_retries_with_enter_when_the_click_does_not_commit(browser, resolver):
    server, url = resolver
    server.custom_answers = [_saved("SPONSORSHIP_NOW_OR_FUTURE", "No")]  # type: ignore[attr-defined]
    server.custom_fields = {}  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(_sponsorship_select(True))
        result, _timings = _run(page, "https://boards.greenhouse.io/example/jobs/1", resolver=_binding(url))
        assert page.locator("#sponsor-hidden").input_value() == "No"
        assert page.locator("#sponsor-error").is_hidden()
        assert _SPONSOR not in [item.get("text") for item in result.manual_questions]
    finally:
        page.close()


def test_react_select_stays_manual_when_enter_does_not_commit(browser, resolver):
    server, url = resolver
    server.custom_answers = [_saved("SPONSORSHIP_NOW_OR_FUTURE", "No")]  # type: ignore[attr-defined]
    server.custom_fields = {}  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(_sponsorship_select(False))
        result, _timings = _run(page, "https://boards.greenhouse.io/example/jobs/1", resolver=_binding(url))
        assert page.locator("#sponsor-hidden").input_value() == ""
        assert page.locator("#sponsor-error").is_hidden() is False
        assert _SPONSOR in [item.get("text") for item in result.manual_questions]
    finally:
        page.close()


def test_lever_location_portal_uses_keyboard_when_the_click_does_not_commit(browser, resolver, caplog):
    server, url = resolver
    caplog.set_level("INFO")
    server.custom_fields = {  # type: ignore[attr-defined]
        "address.city": "Austin",
        "address.state": "TX",
        "address.country": "US",
    }
    server.custom_answers = []  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='location-input'>Current location</label>"
            "<input id='location-input' name='location' type='text' autocomplete='off'>"
            "<input id='selected-location' type='hidden' name='selectedLocation'>"
            "</form>"
            "<script>"
            "const input = document.getElementById('location-input');"
            "const hidden = document.getElementById('selected-location');"
            "let generation = 0;"
            "input.addEventListener('input', () => {"
            "const token = ++generation;"
            "document.querySelectorAll('.dropdown-location').forEach((node) => node.remove());"
            "hidden.value = '';"
            "setTimeout(() => {"
            "if (token !== generation) return;"
            "['Austin, TX, USA', 'Austin, MN, USA'].forEach((place, index) => {"
            "const item = document.createElement('div');"
            "item.className = 'dropdown-location';"
            "item.id = 'location-' + index;"
            "item.textContent = place;"
            "item.addEventListener('mousedown', (event) => event.preventDefault());"
            "document.body.appendChild(item);"
            "});"
            "}, 400);"
            "});"
            "input.addEventListener('keydown', (event) => {"
            "if (event.key !== 'Enter') return;"
            "event.preventDefault();"
            "const first = document.querySelector('.dropdown-location');"
            "if (!first) return;"
            "input.value = first.textContent;"
            "hidden.value = first.textContent;"
            "});"
            "</script>"
        )
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        picked = "Austin, TX, USA"
        assert page.locator("#location-input").input_value() == picked
        assert page.locator("#selected-location").input_value() == picked
        assert [item.get("text") for item in result.manual_questions] == []
        assert "location left manual" not in caplog.text
    finally:
        page.close()


def test_lever_location_free_text_when_no_suggestions_appear(browser, resolver, caplog):
    server, url = resolver
    caplog.set_level("INFO")
    server.custom_fields = {  # type: ignore[attr-defined]
        "address.city": "Austin",
        "address.state": "TX",
        "address.country": "US",
    }
    server.custom_answers = []  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='location-input'>Current location</label>"
            "<input id='location-input' name='location' type='text' autocomplete='off'>"
            "<input id='selected-location' type='hidden' name='selectedLocation'>"
            "</form>"
        )
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        picked = "Austin, Texas, United States"
        assert page.locator("#location-input").input_value() == picked
        assert page.locator("#selected-location").input_value() == picked
        assert [item.get("text") for item in result.manual_questions] == []
        assert "location left manual" not in caplog.text
    finally:
        page.close()


def test_closed_greenhouse_selects_are_sent_to_the_resolver(browser, resolver):
    server, url = resolver
    server.custom_answers = []  # type: ignore[attr-defined]
    server.custom_fields = {}  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='prime'>What is your favorite prime number?</label><input id='prime'>"
            "<label for='color'>What is your favorite color?</label><input id='color'>"
            "<label for='team'>Which team do you prefer?</label><input id='team'>"
            "<label for='notes'>Any notes for the hiring team?</label><input id='notes'>"
            "<div class='field'>"
            "<div class='label'><label>"
            "Will you now or in the future require sponsorship for a visa to remain in your current location?"
            "</label></div>"
            "<div class='select'><div class='css-container'><div class='select__control'>"
            "<div class='select__value-container'>"
            "<div class='select__placeholder'>Select...</div>"
            "<div class='select__input-container'>"
            "<input id='sponsor' class='select__input' autocomplete='off' aria-autocomplete='list'>"
            "</div></div></div></div></div></div>"
            "<div class='field'>"
            "<div class='label'><label>Are you Hispanic/Latino?</label></div>"
            "<div class='select'><div class='css-container'>"
            "<div class='select__control' id='hisp-box' role='combobox'>"
            "<div class='select__value-container'>"
            "<div class='select__placeholder'>Select...</div>"
            "<input id='hispanic' class='select__input' aria-hidden='true' tabindex='-1'>"
            "</div></div></div></div></div>"
            "</form>"
        )
        result, _timings = _run(page, "https://boards.greenhouse.io/example/jobs/1", resolver=_binding(url))
        questions = list(server.calls[0].get("questions", []))
        texts = [str(item.get("text") or "") for item in questions]
        sponsor = "Will you now or in the future require sponsorship for a visa to remain in your current location?"
        assert sponsor in texts
        assert "Are you Hispanic/Latino?" in texts
        assert "Select..." not in texts
        assert texts.count(sponsor) == 1
        assert texts.count("Are you Hispanic/Latino?") == 1
        assert len(questions) >= 6
        by_text = {str(item.get("text")): item for item in questions}
        assert by_text[sponsor].get("intent") == "SPONSORSHIP_NOW_OR_FUTURE"
        assert by_text[sponsor].get("control") == "select"
        assert by_text["Are you Hispanic/Latino?"].get("intent") == "RACE_ETHNICITY"
        requested = [key for call in server.calls for key in call.get("fields", [])]
        assert "address.city" not in requested
        assert page.locator("#sponsor").input_value() == ""
        assert page.locator("#hispanic").input_value() == ""
        manual = [item.get("text") for item in result.manual_questions]
        assert "Are you Hispanic/Latino?" in manual
        assert sponsor in manual
    finally:
        page.close()


@pytest.fixture
def files():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/missing"):
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(PDF)))
            self.end_headers()
            self.wfile.write(PDF)

        def log_message(self, fmt: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()


def test_resume_file_is_attached_from_a_remote_descriptor(browser, resolver, files):
    server, url = resolver
    server.mode = "resume"
    server.resume_url = f"{files}/example-resume.pdf"  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Resume</h1><div id='resume-field'>"
            "<label for='resume'>Resume</label>"
            "<input id='resume' type='file' name='resume'>"
            "<div id='resume-name'></div></div>"
            "<script>document.getElementById('resume').addEventListener('change', () => {"
            "const file = document.getElementById('resume').files[0];"
            "document.getElementById('resume-name').textContent = file ? file.name : '';"
            "});</script>"
        )
        result, _timings = _run(page, "https://boards.greenhouse.io/example/jobs/1", resolver=_binding(url))
        assert result.status != Status.RESUME_UPLOAD_REQUIRED, result.messages
        assert page.locator("#resume").evaluate("el => el.files.length") == 1
        assert page.locator("#resume-name").inner_text() == "Granted_Resume.pdf"
        assert any("resume.file" in call.get("fields", []) for call in server.calls)
    finally:
        page.close()


def test_resume_file_failure_stops_for_a_person(browser, resolver, files):
    server, url = resolver
    server.mode = "resume"
    server.resume_url = f"{files}/missing.pdf"  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Resume</h1><label for='resume'>Resume</label>"
            "<input id='resume' type='file' name='resume'>"
        )
        result, _timings = _run(page, "https://boards.greenhouse.io/example/jobs/1", resolver=_binding(url))
        assert result.status == Status.RESUME_UPLOAD_REQUIRED, result.messages
        assert page.locator("#resume").evaluate("el => el.files.length") == 0
    finally:
        page.close()


def test_resume_error_near_the_input_stops(browser, resolver, files):
    server, url = resolver
    server.mode = "resume"
    server.resume_url = f"{files}/example-resume.pdf"  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Resume</h1><div id='resume-field'>"
            "<label for='resume'>Resume</label>"
            "<input id='resume' type='file' name='resume'>"
            "<div role='alert'>Upload failed</div></div>"
        )
        result, _timings = _run(page, "https://jobs.ashbyhq.com/example/role", resolver=_binding(url))
        assert result.status == Status.RESUME_UPLOAD_REQUIRED, result.messages
    finally:
        page.close()


def test_scoped_react_select_ignores_the_phone_list(browser, resolver):
    server, url = resolver
    server.custom_answers = [  # type: ignore[attr-defined]
        {"intent": "RELOCATE", "value": "Yes", "confidence": "HIGH", "source": "saved_answer"},
    ]
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "greenhouse_scoped_select.html").as_uri())
        result, _timings = _run(
            page,
            "https://boards.greenhouse.io/example/jobs/1",
            resolver=_binding(url),
        )
        picked = page.evaluate("() => window.__picked")
        assert picked == ["relocate-yes"], picked
        assert "country-us" not in picked
        assert any("Preferred desk" in (item.get("text") or "") for item in result.manual_questions)
        assert result.status == Status.MANUAL_ANSWER_REQUIRED
    finally:
        page.close()


def test_yes_no_buttons_and_label_before_placeholder(browser, resolver):
    server, url = resolver
    server.custom_answers = [  # type: ignore[attr-defined]
        {
            "intent": "US_WORK_AUTHORIZATION",
            "value": "Yes",
            "confidence": "HIGH",
            "source": "saved_answer",
        },
        {
            "intent": None,
            "text": "Where are you located?",
            "value": "Example City",
            "confidence": "HIGH",
            "source": "saved_answer",
        },
    ]
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "ashby_buttons.html").as_uri())
        result, _timings = _run(page, "https://jobs.ashbyhq.com/example/role", resolver=_binding(url))
        assert page.evaluate("() => window.__picked") == ["auth-yes"]
        assert page.locator("#loc").input_value() == "Example City"
        asked = [item.get("text") for call in server.calls for item in call.get("questions", [])]
        assert "Where are you located?" in asked
        assert "Start typing..." not in asked
        assert result.status != Status.FAILED
    finally:
        page.close()


def test_captcha_below_the_fold_stops_before_ready(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "captcha_below_fold.html").as_uri())
        result, _timings = _run(page, "https://jobs.ashbyhq.com/example/role")
        assert result.status == Status.CAPTCHA_REQUIRED, result.messages
        assert result.status != Status.READY_FOR_HUMAN_SUBMIT
        assert page.locator("#email").input_value() == ""
    finally:
        page.close()


def test_ashby_resume_uses_the_application_field_and_a_real_name(browser, resolver, files):
    server, url = resolver
    server.mode = "resume"
    server.resume_url = f"{files}/example-resume.pdf"  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1>"
            "<div id='autofill-zone'>"
            "<label for='autofill-resume'>Autofill from resume</label>"
            "<input id='autofill-resume' type='file' name='autofill'>"
            "<div id='toast' role='alert'></div></div>"
            "<div id='resume-field'>"
            "<label for='resume'>Resume</label>"
            "<input id='resume' type='file' name='resume'>"
            "<div id='resume-name'></div></div>"
            "<script>"
            "document.getElementById('autofill-resume').addEventListener('change', () => {"
            "const file = document.getElementById('autofill-resume').files[0];"
            "document.getElementById('toast').textContent = (file ? file.name : 'file') + ' failed to upload';"
            "});"
            "document.getElementById('resume').addEventListener('change', () => {"
            "const file = document.getElementById('resume').files[0];"
            "document.getElementById('resume-name').textContent = file ? file.name : '';"
            "});"
            "</script>"
        )
        result, _timings = _run(page, "https://jobs.ashbyhq.com/example/role", resolver=_binding(url))
        assert result.status != Status.RESUME_UPLOAD_REQUIRED, result.messages
        assert page.locator("#autofill-resume").evaluate("el => el.files.length") == 0
        assert page.locator("#toast").inner_text() == ""
        assert page.locator("#resume").evaluate("el => el.files.length") == 1
        shown = page.locator("#resume-name").inner_text()
        assert shown == "Granted_Resume.pdf"
        assert not shown.startswith("autofill-resume")
    finally:
        page.close()


def test_lever_checkbox_group_and_current_location(browser, resolver):
    server, url = resolver
    server.custom_fields = {  # type: ignore[attr-defined]
        "address.city": "Example City",
        "address.region": "CA",
        "address.country": "US",
    }
    server.custom_answers = [  # type: ignore[attr-defined]
        {
            "intent": None,
            "text": "Which languages do you speak?",
            "values": ["English", "Spanish"],
            "confidence": "HIGH",
            "source": "saved_answer",
        }
    ]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='current-location'>Current location</label>"
            "<input id='current-location' name='location' type='text' autocomplete='off'>"
            "<input id='selected-location' type='hidden' name='selectedLocation'>"
            "<ul id='loc-list' role='listbox' hidden></ul>"
            "<div class='application-question'>"
            "<div class='application-label'>Which languages do you speak?</div>"
            "<ul>"
            "<li><label><input type='checkbox' name='languages' value='English'> English</label></li>"
            "<li><label><input type='checkbox' name='languages' value='Spanish'> Spanish</label></li>"
            "<li><label><input type='checkbox' name='languages' value='French'> French</label></li>"
            "</ul></div></form>"
            "<script>"
            "const input = document.getElementById('current-location');"
            "const list = document.getElementById('loc-list');"
            "const hidden = document.getElementById('selected-location');"
            "const places = ["
            "'Example City, California, United States',"
            "'Example City, Texas, United States',"
            "'Other Town, California, United States'"
            "];"
            "input.addEventListener('input', () => {"
            "hidden.value = '';"
            "list.innerHTML = '';"
            "const q = input.value.trim().toLowerCase();"
            "if (!q) { list.hidden = true; return; }"
            "places.forEach((place, index) => {"
            "if (place.toLowerCase().indexOf(q) === -1) return;"
            "const li = document.createElement('li');"
            "li.setAttribute('role', 'option');"
            "li.id = 'loc-opt-' + index;"
            "li.textContent = place;"
            "li.addEventListener('mousedown', (event) => {"
            "event.preventDefault();"
            "input.value = place;"
            "hidden.value = place;"
            "list.hidden = true;"
            "});"
            "list.appendChild(li);"
            "});"
            "list.hidden = list.children.length === 0;"
            "});"
            "input.addEventListener('blur', () => {"
            "setTimeout(() => { if (!hidden.value) input.value = ''; }, 30);"
            "});"
            "</script>"
        )
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        questions = [item for call in server.calls for item in call.get("questions", [])]
        assert len(questions) == 1, questions
        assert questions[0].get("text") == "Which languages do you speak?"
        assert questions[0].get("options") == ["English", "Spanish", "French"]
        requested = [key for call in server.calls for key in call.get("fields", [])]
        assert "address.city" in requested
        assert "address.region" in requested
        assert "address.country" in requested
        picked = "Example City, California, United States"
        assert page.locator("#current-location").input_value() == picked
        assert page.locator("#selected-location").input_value() == picked
        assert page.locator("input[value='English']").is_checked()
        assert page.locator("input[value='Spanish']").is_checked()
        assert page.locator("input[value='French']").is_checked() is False
        assert [item.get("text") for item in result.manual_questions] == []
    finally:
        page.close()


def test_lever_location_without_a_match_stays_manual(browser, resolver, caplog):
    server, url = resolver
    caplog.set_level("INFO")
    server.custom_fields = {  # type: ignore[attr-defined]
        "address.city": "Example City",
        "address.region": "CA",
        "address.country": "US",
    }
    server.custom_answers = []  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='current-location'>Current location</label>"
            "<input id='current-location' type='text'>"
            "<input id='selected-location' type='hidden' name='selectedLocation'>"
            "<ul id='loc-list' role='listbox' hidden></ul></form>"
            "<script>"
            "const input = document.getElementById('current-location');"
            "const list = document.getElementById('loc-list');"
            "const labels = ["
            "'Far Town, Texas, United States',"
            "'123 Secret Road, Austin, TX',"
            "'person@example.com, Austin, TX'"
            "];"
            "input.addEventListener('input', () => {"
            "list.innerHTML = '';"
            "if (!input.value.trim()) { list.hidden = true; return; }"
            "labels.forEach((label, index) => {"
            "const li = document.createElement('li');"
            "li.setAttribute('role', 'option');"
            "li.id = 'loc-' + index;"
            "li.textContent = label;"
            "list.appendChild(li);"
            "});"
            "list.hidden = false;"
            "});"
            "</script>"
        )
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        assert page.locator("#selected-location").input_value() == ""
        assert page.locator("#current-location").input_value() == ""
        assert "Current location" in [item.get("text") for item in result.manual_questions]
        assert result.status == Status.MANUAL_ANSWER_REQUIRED
        assert "Far Town, Texas, United States" in caplog.text
        assert "123 Secret Road" not in caplog.text
        assert "person@example.com" not in caplog.text
    finally:
        page.close()


def test_lever_dropdown_location_uses_address_state(browser, resolver):
    server, url = resolver
    server.custom_fields = {  # type: ignore[attr-defined]
        "address.city": "Austin",
        "address.state": "TX",
        "address.country": "US",
    }
    server.custom_answers = []  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='current-location'>Current location</label>"
            "<input id='current-location' name='location' type='text' autocomplete='off'>"
            "<input id='selected-location' type='hidden' name='selectedLocation'>"
            "<div id='location-results' hidden>"
            "<div class='dropdown-location' id='location-0'>Austin, TX, USA</div>"
            "<div class='dropdown-location' id='location-1'>Austin, MN, USA</div>"
            "<div class='dropdown-location' id='location-2'>Austin, IN, USA</div>"
            "<div class='dropdown-location' id='location-3'>Austin, AR, USA</div>"
            "</div></form>"
            "<script>"
            "const input = document.getElementById('current-location');"
            "const dropdown = document.getElementById('location-results');"
            "const hidden = document.getElementById('selected-location');"
            "input.addEventListener('input', () => {"
            "hidden.value = '';"
            "dropdown.hidden = !input.value.trim();"
            "});"
            "dropdown.querySelectorAll('.dropdown-location').forEach((item) => {"
            "item.addEventListener('mousedown', (event) => {"
            "event.preventDefault();"
            "input.value = item.textContent;"
            "hidden.value = item.textContent;"
            "dropdown.hidden = true;"
            "});"
            "});"
            "input.addEventListener('blur', () => {"
            "setTimeout(() => { if (!hidden.value) input.value = ''; }, 30);"
            "});"
            "</script>"
        )
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        requested = [key for call in server.calls for key in call.get("fields", [])]
        assert "address.state" in requested
        assert "address.region" in requested
        picked = "Austin, TX, USA"
        assert page.locator("#current-location").input_value() == picked
        assert page.locator("#selected-location").input_value() == picked
        assert [item.get("text") for item in result.manual_questions] == []
    finally:
        page.close()


def test_lever_location_retries_a_late_list_and_ignores_city_case(browser, resolver, caplog):
    server, url = resolver
    caplog.set_level("INFO")
    server.custom_fields = {  # type: ignore[attr-defined]
        "address.city": "Austin",
        "address.state": "TX",
        "address.country": "US",
    }
    server.custom_answers = []  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='current-location'>Current location</label>"
            "<input id='current-location' name='location' type='text' autocomplete='off'>"
            "<input id='selected-location' type='hidden' name='selectedLocation'>"
            "<div id='location-results' hidden>"
            "<div class='dropdown-location' id='location-0'>austin, tx, usa</div>"
            "<div class='dropdown-location' id='location-1'>austin, mn, usa</div>"
            "<div class='dropdown-location' id='location-2'>austin, in, usa</div>"
            "<div class='dropdown-location' id='location-3'>austin, ar, usa</div>"
            "</div></form>"
            "<script>"
            "const input = document.getElementById('current-location');"
            "const dropdown = document.getElementById('location-results');"
            "const hidden = document.getElementById('selected-location');"
            "input.addEventListener('input', () => {"
            "hidden.value = '';"
            "dropdown.hidden = true;"
            "setTimeout(() => { if (input.value.trim()) dropdown.hidden = false; }, 1900);"
            "});"
            "dropdown.querySelectorAll('.dropdown-location').forEach((item) => {"
            "item.addEventListener('mousedown', (event) => {"
            "event.preventDefault();"
            "input.value = item.textContent;"
            "hidden.value = item.textContent;"
            "dropdown.hidden = true;"
            "});"
            "});"
            "</script>"
        )
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        picked = "austin, tx, usa"
        assert page.locator("#current-location").input_value() == picked
        assert page.locator("#selected-location").input_value() == picked
        assert [item.get("text") for item in result.manual_questions] == []
        assert "location left manual" not in caplog.text
    finally:
        page.close()


def test_ashby_required_questions_stay_in_manual(browser, resolver):
    server, url = resolver
    server.custom_answers = []  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='email'>Email</label>"
            "<input id='email' type='email' autocomplete='email'>"
            "<div class='ashby-application-form-field-entry'>"
            "<label id='work-label' class='ashby-application-form-question-title' for='work-from'>"
            "Where do you plan on working from for this role?</label>"
            "<button id='work-from' type='button' aria-haspopup='listbox' aria-required='true' "
            "aria-labelledby='work-label'>Select...</button>"
            "</div>"
            "<div class='ashby-application-form-field-entry'>"
            "<label id='yoe-sec-label' class='ashby-application-form-question-title' for='yoe-sec'>"
            "How many years of experience do you have with security engineering?</label>"
            "<button id='yoe-sec' type='button' aria-haspopup='listbox' aria-required='true' "
            "aria-labelledby='yoe-sec-label'>Select...</button>"
            "</div>"
            "<div class='ashby-application-form-field-entry'>"
            "<div class='ashby-application-form-question-title'>"
            "How many years of experience do you have using Python?</div>"
            "<input id='yoe-py' type='number' required>"
            "</div>"
            "<label for='yoe-total'>How many years of experience do you have?</label>"
            "<input id='yoe-total' type='text' required>"
            "</form>"
        )
        result, _timings = _run(page, "https://jobs.ashbyhq.com/example/role", resolver=_binding(url))
        texts = [item.get("text") for item in result.manual_questions]
        assert "Where do you plan on working from for this role?" in texts
        assert "How many years of experience do you have with security engineering?" in texts
        assert "How many years of experience do you have using Python?" in texts
        assert "How many years of experience do you have?" in texts
        assert "Email" not in texts
        assert "Select..." not in texts
        assert page.locator("#yoe-py").input_value() == ""
        assert page.locator("#yoe-total").input_value() == ""
        assert page.locator("#work-from").inner_text() == "Select..."
        assert page.locator("#yoe-sec").inner_text() == "Select..."
        assert result.status == Status.MANUAL_ANSWER_REQUIRED
    finally:
        page.close()


def test_missing_resume_still_fills_and_lists_questions(browser, resolver):
    server, url = resolver
    server.custom_fields = {"fullName": "River Example", "email": EMAIL}  # type: ignore[attr-defined]
    server.custom_answers = [_saved("US_WORK_AUTHORIZATION", "Yes")]  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='name'>Full name</label>"
            "<input id='name' name='name' autocomplete='name'>"
            "<label for='email'>Email</label>"
            "<input id='email' type='email' autocomplete='email'>"
            "<label for='resume'>Resume</label>"
            "<input id='resume' type='file' required>"
            "<div class='ashby-application-form-field-entry'>"
            "<label class='ashby-application-form-question-title'>"
            "Are you authorized to work in the United States?<span>*</span></label>"
            "<div><div><button type='button' id='auth-yes'>Yes</button></div>"
            "<div><button type='button' id='auth-no'>No</button></div></div></div>"
            "<div class='ashby-application-form-field-entry'>"
            "<label class='ashby-application-form-question-title'>"
            "Will you now or in the future require sponsorship for employment visa status?<span>*</span></label>"
            "<div><div><button type='button' id='sponsor-yes'>Yes</button></div>"
            "<div><button type='button' id='sponsor-no'>No</button></div></div></div>"
            "<div class='ashby-application-form-field-entry'>"
            "<label class='ashby-application-form-question-title'>"
            "Are you willing to relocate?<span>*</span></label>"
            "<div><div><button type='button' id='move-yes'>Yes</button></div>"
            "<div><button type='button' id='move-no'>No</button></div></div></div>"
            "</form><script>window.__picked = [];"
            "for (const id of ['auth-yes','auth-no','sponsor-yes','sponsor-no','move-yes','move-no']) {"
            "document.getElementById(id).addEventListener('click', () => window.__picked.push(id));"
            "}</script>"
        )
        result, _timings = _run(page, "https://jobs.ashbyhq.com/example/role", resolver=_binding(url))
        intents = [item.get("intent") for call in server.calls for item in call.get("questions", [])]
        fields = [key for call in server.calls for key in call.get("fields", [])]
        assert "resume.file" in fields
        assert "fullName" in fields
        assert "email" in fields
        assert "US_WORK_AUTHORIZATION" in intents
        assert "SPONSORSHIP_NOW_OR_FUTURE" in intents
        assert "RELOCATE" in intents
        assert page.locator("#name").input_value() == "River Example"
        assert page.locator("#email").input_value() == EMAIL
        assert page.locator("#resume").evaluate("el => el.files.length") == 0
        assert page.evaluate("() => window.__picked") == ["auth-yes"]
        texts = [item.get("text") for item in result.manual_questions]
        assert "Will you now or in the future require sponsorship for employment visa status?" in texts
        assert "Are you willing to relocate?" in texts
        assert "Are you authorized to work in the United States?" not in texts
        assert result.status == Status.RESUME_UPLOAD_REQUIRED
        assert any("resume" in message.lower() for message in result.messages)
    finally:
        page.close()


def test_radio_and_checkbox_questions_use_the_group_label(browser, resolver):
    server, url = resolver
    server.custom_answers = []  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<div class='application-question'>"
            "<div class='application-label' id='consent-q'>Do you consent to a background check?</div>"
            "<ul aria-labelledby='consent-q'>"
            "<li><label><input type='radio' name='consent' value='yes'> Yes, I consent</label></li>"
            "<li><label><input type='radio' name='consent' value='no'> No</label></li>"
            "</ul></div>"
            "<div class='application-question'>"
            "<div class='application-label'>Which languages do you speak?</div>"
            "<ul>"
            "<li><label><input type='checkbox' name='languages' value='English'> English</label></li>"
            "<li><label><input type='checkbox' name='languages' value='Spanish'> Spanish</label></li>"
            "</ul></div></form>"
        )
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        texts = [item.get("text") for item in result.manual_questions]
        assert "Do you consent to a background check?" in texts
        assert "Which languages do you speak?" in texts
        assert "Yes, I consent" not in texts
        assert "Yes" not in texts
        assert "English" not in texts
    finally:
        page.close()


def test_lever_checkbox_options_and_section_headings_are_not_questions(browser, resolver):
    server, url = resolver
    server.custom_answers = []  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<h4>ADDITIONAL INFORMATION</h4>"
            "<div><textarea id='notes' name='notes' required></textarea></div>"
            "<div class='application-question'>"
            "<div class='application-label'>Language Skill(s)<span class='required'>✱</span></div>"
            "<div class='application-field'><ul>"
            "<li><input type='checkbox' name='cards[lang][field0]' value='English (ENG)' required>"
            "<label>English (ENG)</label></li>"
            "<li><input type='checkbox' name='cards[lang][field1]' value='Spanish (SPA)' required>"
            "<span class='application-answer-alternative'>Spanish (SPA)</span></li>"
            "<li><label><input type='checkbox' name='cards[lang][field2]' value='French (FRA)' required>"
            " French (FRA)</label></li>"
            "</ul></div></div>"
            "<fieldset><legend>Languages</legend>"
            "<div><input type='checkbox' name='spoken-en' value='English' required><label>English</label></div>"
            "<div><input type='checkbox' name='spoken-es' value='Spanish' required><label>Spanish</label></div>"
            "</fieldset>"
            "<label for='prime'>What is your favorite prime number?</label>"
            "<textarea id='prime' name='prime' required></textarea>"
            "</form>"
        )
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        questions = [item for call in server.calls for item in call.get("questions", [])]
        by_text = {item.get("text"): item.get("options") for item in questions}
        assert by_text.get("Language Skill(s)") == ["English (ENG)", "Spanish (SPA)", "French (FRA)"]
        assert by_text.get("Languages") == ["English", "Spanish"]
        assert "What is your favorite prime number?" in by_text
        assert "ADDITIONAL INFORMATION" not in by_text
        assert "English (ENG)" not in by_text
        assert "Spanish (SPA)" not in by_text
        assert "French (FRA)" not in by_text
        texts = [item.get("text") for item in result.manual_questions]
        assert texts.count("Language Skill(s)") == 1
        assert texts.count("Languages") == 1
        assert "What is your favorite prime number?" in texts
        assert "ADDITIONAL INFORMATION" not in texts
        assert "English (ENG)" not in texts
        assert "Spanish (SPA)" not in texts
        assert "French (FRA)" not in texts
        assert "English" not in texts
        assert "Spanish" not in texts
        assert page.locator("input[name='cards[lang][field0]']").is_checked() is False
        assert page.locator("input[name='spoken-en']").is_checked() is False
    finally:
        page.close()


def test_resume_bytes_survive_a_later_read(browser, resolver, files):
    server, url = resolver
    server.mode = "resume"
    server.resume_url = f"{files}/example-resume.pdf"  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><div id='resume-field'>"
            "<label for='resume'>Resume</label>"
            "<input id='resume' type='file' name='resume'>"
            "<div id='resume-name'></div>"
            "<div id='toast' role='alert'></div></div>"
            "<script>"
            "document.getElementById('resume').addEventListener('change', () => {"
            "const file = document.getElementById('resume').files[0];"
            "document.getElementById('resume-name').textContent = file ? file.name : '';"
            "setTimeout(() => {"
            "file.arrayBuffer().then((buf) => {"
            "if (!buf || !buf.byteLength) throw new Error('empty');"
            "}).catch(() => {"
            "document.getElementById('toast').textContent = file.name + ' failed to upload';"
            "});"
            "}, 300);"
            "});"
            "</script>"
        )
        result, _timings = _run(page, "https://jobs.ashbyhq.com/example/role", resolver=_binding(url))
        assert result.status != Status.RESUME_UPLOAD_REQUIRED, result.messages
        assert page.locator("#toast").inner_text() == ""
        assert page.locator("#resume-name").inner_text() == "Granted_Resume.pdf"
    finally:
        page.close()


def test_late_upload_toast_rejects_the_resume(browser, resolver, files):
    server, url = resolver
    server.mode = "resume"
    server.resume_url = f"{files}/example-resume.pdf"  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><div id='resume-field'>"
            "<label for='resume'>Resume</label>"
            "<input id='resume' type='file'>"
            "<div id='resume-name'></div></div>"
            "<div id='toast' role='alert'></div>"
            "<script>"
            "document.getElementById('resume').addEventListener('change', () => {"
            "const file = document.getElementById('resume').files[0];"
            "document.getElementById('resume-name').textContent = file ? file.name : '';"
            "setTimeout(() => {"
            "document.getElementById('toast').textContent = file.name + ' failed to upload';"
            "}, 400);"
            "});"
            "</script>"
        )
        result, _timings = _run(page, "https://jobs.ashbyhq.com/example/role", resolver=_binding(url))
        assert result.status == Status.RESUME_UPLOAD_REQUIRED, result.messages
        assert "failed to upload" in page.locator("#toast").inner_text()
    finally:
        page.close()


def _yes_no_question(legend: str) -> str:
    return (
        "<h1>Application</h1><form><fieldset>"
        f"<legend>{legend}</legend>"
        "<label><input type='radio' name='answer' value='Yes' required> Yes</label>"
        "<label><input type='radio' name='answer' value='No'> No</label>"
        "</fieldset></form>"
    )


def _saved(intent: str, value: str, confidence: str = "HIGH") -> dict:
    return {"intent": intent, "value": value, "confidence": confidence, "source": "saved_answer"}


def test_lever_now_or_future_sponsorship_follows_either_yes(browser, resolver):
    server, url = resolver

    def respond(body: dict) -> dict:
        intents = [item.get("intent") for item in body.get("questions") or []]
        if "SPONSORSHIP_NOW" in intents or "SPONSORSHIP_FUTURE" in intents:
            return {
                "fields": {},
                "answers": [_saved("SPONSORSHIP_NOW", "No"), _saved("SPONSORSHIP_FUTURE", "Yes")],
                "unresolved": [],
            }
        return {"fields": {}, "answers": [], "unresolved": ["SPONSORSHIP_NOW_OR_FUTURE"]}

    server.dynamic = respond  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            _yes_no_question("Will you now or in the future require sponsorship for employment visa status?")
        )
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        first = [item.get("intent") for item in server.calls[0].get("questions", [])]
        second = [item.get("intent") for item in server.calls[1].get("questions", [])]
        assert first == ["SPONSORSHIP_NOW_OR_FUTURE"]
        assert set(second) == {"SPONSORSHIP_NOW", "SPONSORSHIP_FUTURE"}
        assert page.locator("input[value='Yes']").is_checked()
        assert page.locator("input[value='No']").is_checked() is False
        assert result.status != Status.MANUAL_ANSWER_REQUIRED
    finally:
        page.close()


def test_known_compound_sponsorship_does_not_ask_again(browser, resolver):
    server, url = resolver

    def respond(body: dict) -> dict:
        return {"fields": {}, "answers": [_saved("SPONSORSHIP_NOW_OR_FUTURE", "No")], "unresolved": []}

    server.dynamic = respond  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(_yes_no_question("Will you now or will you in the future require sponsorship?"))
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        assert len(server.calls) == 1
        assert page.locator("input[value='No']").is_checked()
        assert result.status != Status.MANUAL_ANSWER_REQUIRED
    finally:
        page.close()


def test_sponsorship_below_high_stays_manual(browser, resolver):
    server, url = resolver

    def respond(body: dict) -> dict:
        intents = [item.get("intent") for item in body.get("questions") or []]
        if "SPONSORSHIP_NOW" in intents or "SPONSORSHIP_FUTURE" in intents:
            return {
                "fields": {},
                "answers": [
                    _saved("SPONSORSHIP_NOW", "No", "MEDIUM"),
                    _saved("SPONSORSHIP_FUTURE", "No", "HIGH"),
                ],
                "unresolved": [],
            }
        return {
            "fields": {},
            "answers": [_saved("SPONSORSHIP_NOW_OR_FUTURE", "No", "MEDIUM")],
            "unresolved": ["SPONSORSHIP_NOW_OR_FUTURE"],
        }

    server.dynamic = respond  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(_yes_no_question("Will you now or in the future require sponsorship?"))
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        assert page.locator("input[value='Yes']").is_checked() is False
        assert page.locator("input[value='No']").is_checked() is False
        assert result.status == Status.MANUAL_ANSWER_REQUIRED
    finally:
        page.close()


def test_authorized_without_sponsorship_does_not_use_work_auth_alone(browser, resolver):
    server, url = resolver
    legend = (
        "Are you legally authorized to work in the United States without the need for "
        "sponsorship now or in the future?"
    )

    def respond(body: dict) -> dict:
        intents = [item.get("intent") for item in body.get("questions") or []]
        if "US_WORK_AUTHORIZATION" in intents:
            return {
                "fields": {},
                "answers": [
                    _saved("US_WORK_AUTHORIZATION", "Yes"),
                    _saved("SPONSORSHIP_NOW", "No"),
                    _saved("SPONSORSHIP_FUTURE", "Yes"),
                ],
                "unresolved": [],
            }
        return {"fields": {}, "answers": [], "unresolved": ["AUTHORIZED_WITHOUT_SPONSORSHIP"]}

    server.dynamic = respond  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(_yes_no_question(legend))
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        asked = [item.get("intent") for item in server.calls[0].get("questions", [])]
        assert asked == ["AUTHORIZED_WITHOUT_SPONSORSHIP"]
        assert page.locator("input[value='No']").is_checked()
        assert page.locator("input[value='Yes']").is_checked() is False
        assert result.status != Status.MANUAL_ANSWER_REQUIRED
    finally:
        page.close()


def test_lever_resume_accepts_an_uppercased_filename(browser, resolver, files):
    server, url = resolver
    server.mode = "resume"
    server.resume_url = f"{files}/example-resume.pdf"  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><div id='resume-field'>"
            "<label for='resume'>Resume</label>"
            "<input id='resume' type='file' name='resume'>"
            "<div id='resume-name' style='text-transform: uppercase'></div></div>"
            "<script>document.getElementById('resume').addEventListener('change', () => {"
            "const file = document.getElementById('resume').files[0];"
            "document.getElementById('resume-name').textContent = file ? file.name.toUpperCase() : '';"
            "});</script>"
        )
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        assert result.status != Status.RESUME_UPLOAD_REQUIRED, result.messages
        assert page.locator("#resume-name").inner_text() == "GRANTED_RESUME.PDF"
    finally:
        page.close()


def test_lever_resume_accepts_the_success_indicator(browser, resolver, files):
    server, url = resolver
    server.mode = "resume"
    server.resume_url = f"{files}/example-resume.pdf"  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><div id='resume-field'>"
            "<label for='resume'>Resume</label>"
            "<input id='resume' type='file' name='resume'></div>"
            "<div id='done' class='upload-success' role='status'></div>"
            "<script>document.getElementById('resume').addEventListener('change', () => {"
            "document.getElementById('done').textContent = 'Success!';"
            "});</script>"
        )
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        assert result.status != Status.RESUME_UPLOAD_REQUIRED, result.messages
        assert page.locator("#done").inner_text() == "Success!"
        assert page.locator("#resume").evaluate("el => el.files[0].name") == "Granted_Resume.pdf"
    finally:
        page.close()


def test_ashby_resume_accepts_a_spaced_uppercased_name(browser, resolver, files):
    server, url = resolver
    server.mode = "resume"
    server.resume_url = f"{files}/example-resume.pdf"  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><div id='resume-field'>"
            "<label for='resume'>Resume</label>"
            "<input id='resume' type='file' name='resume'>"
            "<div id='resume-name'></div></div>"
            "<script>document.getElementById('resume').addEventListener('change', () => {"
            "const file = document.getElementById('resume').files[0];"
            "const shown = file ? file.name.replaceAll('_', ' ').toUpperCase() : '';"
            "document.getElementById('resume-name').textContent = shown;"
            "});</script>"
        )
        result, _timings = _run(page, "https://jobs.ashbyhq.com/example/role", resolver=_binding(url))
        assert result.status != Status.RESUME_UPLOAD_REQUIRED, result.messages
        assert page.locator("#resume-name").inner_text() == "GRANTED RESUME.PDF"
    finally:
        page.close()


def test_upload_error_still_blocks_when_success_is_shown(browser, resolver, files):
    server, url = resolver
    server.mode = "resume"
    server.resume_url = f"{files}/example-resume.pdf"  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><div id='resume-field'>"
            "<label for='resume'>Resume</label>"
            "<input id='resume' type='file' name='resume'>"
            "<div id='resume-name'></div>"
            "<div id='done' class='upload-success'>Success!</div></div>"
            "<div id='toast' role='alert'></div>"
            "<script>document.getElementById('resume').addEventListener('change', () => {"
            "const file = document.getElementById('resume').files[0];"
            "document.getElementById('resume-name').textContent = file ? file.name.toUpperCase() : '';"
            "document.getElementById('toast').textContent = "
            "(file ? file.name.toUpperCase() : 'FILE') + ' failed to upload';"
            "});</script>"
        )
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        assert result.status == Status.RESUME_UPLOAD_REQUIRED, result.messages
        assert "failed to upload" in page.locator("#toast").inner_text().lower()
    finally:
        page.close()


def _uploaded_bytes(page) -> bytes:
    values = page.locator("#resume").evaluate(
        """async (el) => {
          const file = el.files && el.files[0];
          if (!file) return [];
          const buf = await file.arrayBuffer();
          return Array.from(new Uint8Array(buf));
        }"""
    )
    return bytes(values)


def _placeholder_application() -> str:
    return (
        "<h1>Application</h1>"
        "<label for='name'>Full name</label><input id='name' name='name'>"
        "<label for='email'>Email</label><input id='email' type='email' name='email'>"
        "<div id='resume-field'><label for='resume'>Resume</label>"
        "<input id='resume' type='file' name='resume'>"
        "<div id='resume-name'></div></div>"
        "<script>document.getElementById('resume').addEventListener('change', () => {"
        "const file = document.getElementById('resume').files[0];"
        "document.getElementById('resume-name').textContent = file ? file.name : '';"
        "});</script>"
    )


def _assert_placeholder_stayed_off_the_page(page, result) -> None:
    assert page.locator("#name").input_value() == ""
    assert page.locator("#email").input_value() == ""
    published = json.dumps(result.to_dict())
    assert "Synthetic Candidate" not in published
    assert "synthetic@example.com" not in published
    visible = page.locator("body").inner_text()
    assert "Synthetic Candidate" not in visible
    assert "synthetic@example.com" not in visible


def test_resolver_upload_uses_the_grant_not_the_placeholder_profile(browser, resolver, files):
    server, url = resolver
    server.mode = "resume"
    server.resume_url = f"{files}/example-resume.pdf"  # type: ignore[attr-defined]
    server.resume_filename = "First_Last_Resume.pdf"  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(_placeholder_application())
        result, _timings = _run(
            page,
            "https://jobs.ashbyhq.com/example/role",
            profile=placeholder_profile("ref-a"),
            resolver=_binding(url),
        )
        assert result.status != Status.RESUME_UPLOAD_REQUIRED, result.messages
        assert page.locator("#resume").evaluate("el => el.files[0].name") == "First_Last_Resume.pdf"
        assert _uploaded_bytes(page) == PDF
        _assert_placeholder_stayed_off_the_page(page, result)
    finally:
        page.close()


def test_missing_grant_filename_uses_resume_pdf_not_the_placeholder(browser, resolver, files):
    server, url = resolver
    server.mode = "resume"
    server.resume_url = f"{files}/example-resume.pdf"  # type: ignore[attr-defined]
    server.resume_filename = ""  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(_placeholder_application())
        result, _timings = _run(
            page,
            "https://jobs.ashbyhq.com/example/role",
            profile=placeholder_profile("ref-a"),
            resolver=_binding(url),
        )
        assert result.status != Status.RESUME_UPLOAD_REQUIRED, result.messages
        assert page.locator("#resume").evaluate("el => el.files[0].name") == "Resume.pdf"
        assert _uploaded_bytes(page) == PDF
        _assert_placeholder_stayed_off_the_page(page, result)
    finally:
        page.close()


_LEVER_AUSTINS = (
    "<h1>Application</h1><form>"
    "<label for='current-location'>Current location</label>"
    "<input id='current-location' name='location' type='text' autocomplete='off'>"
    "<input id='selected-location' type='hidden' name='selectedLocation'>"
    "<div id='location-results' hidden>"
    "<div class='dropdown-location' id='location-0'>Austin, TX, USA</div>"
    "<div class='dropdown-location' id='location-1'>Austin, MN, USA</div>"
    "<div class='dropdown-location' id='location-2'>Austin, IN, USA</div>"
    "<div class='dropdown-location' id='location-3'>Austin, AR, USA</div>"
    "</div></form>"
    "<script>"
    "const input = document.getElementById('current-location');"
    "const dropdown = document.getElementById('location-results');"
    "input.addEventListener('input', () => { dropdown.hidden = !input.value.trim(); });"
    "</script>"
)


def test_lever_location_without_a_city_says_why(browser, resolver, caplog):
    """No city from the resolver is a profile gap. It is logged, not silent."""
    server, url = resolver
    caplog.set_level("INFO")
    server.custom_fields = {"fullName": "River Example"}  # type: ignore[attr-defined]
    server.custom_answers = []  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(_LEVER_AUSTINS)
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        assert page.locator("#current-location").input_value() == ""
        assert "Current location" in [item.get("text") for item in result.manual_questions]
        assert "location left manual reason=no_city" in caplog.text
        assert "has_region=False" in caplog.text
        assert "River Example" not in caplog.text
        step = next(line for line in caplog.text.splitlines() if "returned_keys=" in line)
        requested = step.split("field_keys=")[1].split(" intents=")[0].split(",")
        returned = step.split("returned_keys=")[1].split(" result=")[0].split(",")
        assert "address.city" in requested
        assert "address.city" not in returned
        assert all(key != "River Example" for key in returned)
    finally:
        page.close()


def test_lever_location_city_without_state_is_ambiguous(browser, resolver, caplog):
    """Four Austins and no state: nothing is guessed, and the reason is logged."""
    server, url = resolver
    caplog.set_level("INFO")
    server.custom_fields = {"address.city": "Austin"}  # type: ignore[attr-defined]
    server.custom_answers = []  # type: ignore[attr-defined]
    page = browser.new_page()
    try:
        page.set_content(_LEVER_AUSTINS)
        result, _timings = _run(page, "https://jobs.lever.co/example/role", resolver=_binding(url))
        assert page.locator("#selected-location").input_value() == ""
        assert "Current location" in [item.get("text") for item in result.manual_questions]
        assert "location left manual reason=ambiguous" in caplog.text
        assert "Austin, TX, USA" in caplog.text
    finally:
        page.close()
