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


def _run(page, url: str, **options):
    timings: dict[str, int] = {}
    result = autofill_application(
        url,
        _profile(),
        None,
        AutofillOptions(page=page, session_id="sess-ats", candidate_id="ref-a", timings=timings, **options),
    )
    return result, timings


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
        assert result.fields_detected == 0
        assert timings["firstFormInspectedMs"] < 8000
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
            if server.mode == "resume":
                fields = {
                    "resume.file": {
                        "url": server.resume_url,  # type: ignore[attr-defined]
                        "filename": "example-resume.pdf",
                        "contentType": "application/pdf",
                        "expiresAt": _future(),
                    }
                }
            elif server.mode == "file":
                fields = {"resume.file": {"url": "file:///tmp/resume.pdf", "filename": "resume.pdf"}}
            else:
                fields = {}
            answers = [
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
            encoded = json.dumps({"fields": fields, "answers": answers, "unresolved": []}).encode("utf-8")
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
            "<h1>Resume</h1><label for='resume'>Resume</label>"
            "<input id='resume' type='file' name='resume'>"
        )
        result, _timings = _run(page, "https://boards.greenhouse.io/example/jobs/1", resolver=_binding(url))
        assert result.status != Status.RESUME_UPLOAD_REQUIRED, result.messages
        assert page.locator("#resume").evaluate("el => el.files.length") == 1
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
