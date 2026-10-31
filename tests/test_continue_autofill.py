"""A visible ADP contact form is not a challenge, and Continue resumes the fill.

A real checkbox still stops. Final Submit stays unclicked.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from autofill import AutofillOptions, Status, autofill_application
from autofill.engine import _reveal_settle_ms
from autofill.login import verification_present
from autofill.mapping import map_field
from autofill.models import Control, FieldOutcome, PageSnapshot
from autofill.profile import CandidateProfile
from autofill.resolver import ResolverBinding
from autofill.service.app import create_app
from autofill.service.runner import PlaywrightRunner

FIXTURES = Path(__file__).resolve().parent / "fixtures"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "profile.example.json"
FALSE_CHALLENGE = FIXTURES / "adp_contact_false_challenge.html"
REAL_CAPTCHA = FIXTURES / "adp_contact_real_captcha.html"
TOKEN = "synthetic-service-token-0123456789"
RESOLVER_TOKEN = "synthetic-resolver-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
CONTACT_FIELDS = {
    "firstName": "Casey",
    "lastName": "Example",
    "email": "casey.example@example.com",
    "phone": "555-010-0199",
    "address.country": "Exampleland",
}


def _future() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def _control(kind: str, label: str, **kwargs) -> Control:
    return Control(kind=kind, label=label, selector=kwargs.pop("selector", f"#{kind}"), **kwargs)


@pytest.fixture
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


def _profile() -> CandidateProfile:
    return CandidateProfile.load(EXAMPLE)


def _run(page, url: str, *, resolver: ResolverBinding | None = None) -> tuple[object, dict[str, int], int]:
    timings: dict[str, int] = {}
    started = time.perf_counter()
    result = autofill_application(
        url,
        _profile(),
        None,
        AutofillOptions(
            page=page,
            session_id="sess-continue",
            candidate_id="ref-a",
            timings=timings,
            protocol_version="0.7.0",
            resolver=resolver,
            max_pages=2,
        ),
    )
    wall_ms = int((time.perf_counter() - started) * 1000)
    return result, timings, wall_ms


def test_country_code_and_contact_copy_are_not_email_verification():
    contact = PageSnapshot(
        buttons=[],
        controls=[
            _control("text", "First Name"),
            _control("text", "Last Name"),
            _control("text", "Email", input_type="email"),
            _control("text", "Country code", aria_label="Country code", max_length=4),
            _control("text", "Mobile Number", input_type="tel"),
        ],
        heading="Tell us about yourself",
        page_text="We sent you a code so we can notify you about this application.",
    )
    assert verification_present(contact) is False
    postal = PageSnapshot(
        buttons=[],
        controls=[
            _control("text", "First Name"),
            _control("text", "Email", input_type="email"),
            _control("text", "Zip code", max_length=5),
        ]
    )
    assert verification_present(postal) is False
    otp = PageSnapshot(
        buttons=[],
        controls=[_control("text", "Verification code", autocomplete="one-time-code", max_length=6)],
        heading="Email verification",
        page_text="We sent you a code. Check your inbox.",
    )
    assert verification_present(otp) is True
    beside_contact = PageSnapshot(
        buttons=[],
        controls=[
            _control("text", "First Name"),
            _control("text", "Email", input_type="email"),
            _control("text", "Code", autocomplete="one-time-code", max_length=6),
        ],
        page_text="We sent you a code.",
    )
    assert verification_present(beside_contact) is True
    bare = PageSnapshot(
        buttons=[],
        controls=[_control("text", "PIN", max_length=6)],
        page_text="Enter the PIN.",
    )
    assert verification_present(bare) is True
    dial = map_field(_control("text", "Country code", aria_label="Country code", max_length=4), _profile())
    assert dial.key != "country"


def test_text_fill_does_not_wait_for_a_choice_reveal():
    text = FieldOutcome(selector="#email", label="Email", key="email", action="fill")
    choice = FieldOutcome(selector="#country", label="Country", key="country", action="select")
    checked = FieldOutcome(selector="#ok", label="OK", key="consent", action="check")
    assert _reveal_settle_ms([text]) == 40
    assert _reveal_settle_ms([choice]) == 600
    assert _reveal_settle_ms([checked]) == 600


def test_visible_adp_contact_form_is_filled_and_continued(browser):
    page = browser.new_page()
    try:
        page.goto(FALSE_CHALLENGE.as_uri())
        result, timings, wall_ms = _run(page, "https://myjobs.adp.com/woodstream/cx/apply")
        assert result.status not in {Status.CAPTCHA_REQUIRED, Status.EMAIL_VERIFICATION_REQUIRED}, result.messages
        assert "site blocked automated access" not in result.messages
        values = page.evaluate("() => window.__values")
        assert values["first"] == "Casey"
        assert values["last"] == "Example"
        assert values["email"] == "casey.example@example.com"
        assert "555" in values["mobile"]
        assert values["cc"] == "+1"
        assert page.evaluate("() => window.__continued") is True
        assert page.evaluate("() => window.__submitted") is not True
        assert result.stopped_before_submit is True
        assert wall_ms < 8000, timings
        assert timings.get("fillMs", 0) < 2500, timings
    finally:
        page.close()


def test_resolver_contact_form_skips_the_choice_wait(browser):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            payload = {"fields": CONTACT_FIELDS, "answers": [], "unresolved": []}
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, fmt: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    binding = ResolverBinding(
        url=f"http://{host}:{port}/resolve",
        token=RESOLVER_TOKEN,
        expires_at=_future(),
        candidate_ref="ref-a",
        job_ref="job-a",
        timeout_seconds=5,
        retries=0,
        max_bytes=65536,
    )
    page = browser.new_page()
    try:
        page.goto(FALSE_CHALLENGE.as_uri())
        result, timings, wall_ms = _run(page, "https://myjobs.adp.com/woodstream/cx/apply", resolver=binding)
        assert result.status not in {Status.CAPTCHA_REQUIRED, Status.EMAIL_VERIFICATION_REQUIRED}, result.messages
        values = page.evaluate("() => window.__values")
        assert values["first"] == "Casey"
        assert values["email"] == "casey.example@example.com"
        assert values["cc"] == "+1"
        assert page.evaluate("() => window.__continued") is True
        assert page.evaluate("() => window.__submitted") is not True
        assert wall_ms < 8000, timings
        assert timings.get("fillMs", 0) < 2500, timings
    finally:
        page.close()
        server.shutdown()


def test_real_checkbox_still_stops_before_fill(browser):
    page = browser.new_page()
    try:
        page.goto(REAL_CAPTCHA.as_uri())
        result, _timings, _wall = _run(page, "https://myjobs.adp.com/woodstream/cx/apply")
        assert result.status == Status.CAPTCHA_REQUIRED, result.messages
        assert "site blocked automated access" not in result.messages
        assert page.locator("#email").input_value() == ""
        assert page.locator("#first").input_value() == ""
        assert page.evaluate("() => window.__continued") is False
        assert page.evaluate("() => window.__submitted") is not True
    finally:
        page.close()


def test_shadow_contact_fields_count_as_committed(browser):
    page = browser.new_page()
    try:
        page.set_content(
            """
            <h1>Tell us about yourself</h1>
            <div id="host"></div>
            <button type="button" id="continue">Continue</button>
            <button type="submit" id="submit">Submit application</button>
            <script>
              window.__continued = false;
              window.__submitted = false;
              const root = document.getElementById("host").attachShadow({mode: "open"});
              root.innerHTML = `
                <label for="first">First Name</label>
                <input id="first" required autocomplete="given-name">
                <label for="last">Last Name</label>
                <input id="last" required autocomplete="family-name">
                <label for="email">Email</label>
                <input id="email" type="email" required autocomplete="email">
                <label for="mobile">Mobile Number</label>
                <input id="mobile" type="tel" required autocomplete="tel">
              `;
              document.getElementById("continue").addEventListener("click", () => {
                window.__continued = true;
                window.__values = {
                  first: root.getElementById("first").value,
                  email: root.getElementById("email").value,
                  mobile: root.getElementById("mobile").value,
                };
              });
              document.getElementById("submit").addEventListener("click", () => {
                window.__submitted = true;
              });
            </script>
            """,
            wait_until="domcontentloaded",
        )
        result, _timings, _wall = _run(page, "https://myjobs.adp.com/woodstream/cx/apply")
        assert result.status not in {Status.CAPTCHA_REQUIRED, Status.EMAIL_VERIFICATION_REQUIRED}, result.messages
        values = page.evaluate("() => window.__values")
        assert values["first"] == "Casey"
        assert values["email"] == "casey.example@example.com"
        assert "555" in values["mobile"]
        assert page.evaluate("() => window.__continued") is True
        assert page.evaluate("() => window.__submitted") is not True
    finally:
        page.close()


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return


def _settle(client: TestClient, session_id: str, started) -> dict:
    assert started.status_code == 200, started.text
    body = started.json()
    deadline = time.monotonic() + 40
    while body.get("status") == Status.STARTED and time.monotonic() < deadline:
        time.sleep(0.05)
        polled = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        assert polled.status_code == 200, polled.text
        body = polled.json()
    return body


def _eval(runner: PlaywrightRunner, session_id: str, expression: str) -> object:
    def read() -> object:
        page = runner._sessions[session_id].page
        return page.evaluate(expression)

    return runner._submit(read, session_id)


def test_resume_restarts_after_a_cleared_challenge_and_a_real_one_stays_stopped():
    pages = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(FIXTURES)))
    page_thread = threading.Thread(target=pages.serve_forever, daemon=True)
    page_thread.start()

    class ResolverHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            payload = {"fields": CONTACT_FIELDS, "answers": [], "unresolved": []}
            encoded = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, fmt: str, *args) -> None:
            return

    resolver = ThreadingHTTPServer(("127.0.0.1", 0), ResolverHandler)
    resolver_thread = threading.Thread(target=resolver.serve_forever, daemon=True)
    resolver_thread.start()
    page_host, page_port = pages.server_address
    resolver_host, resolver_port = resolver.server_address
    origin = f"http://{page_host}:{page_port}"
    runner = PlaywrightRunner(enabled=True, headless=True, workers=1)
    app = create_app(token=TOKEN, runner=runner, run_browser=True)
    body = {
        "protocolVersion": "0.7.0",
        "candidateRef": "ref-a",
        "jobRef": "job-a",
        "jobContext": {
            "applicationUrl": f"{origin}/adp_contact_real_captcha.html",
            "title": "Electrical Design Engineer III",
            "company": "Woodstream",
        },
        "resolver": {
            "url": f"http://{resolver_host}:{resolver_port}/resolve",
            "token": RESOLVER_TOKEN,
            "expiresAt": _future(),
        },
    }
    try:
        with TestClient(app) as client:
            created = client.post("/v1/sessions", headers=AUTH, json=body)
            assert created.status_code == 201, created.text
            session_id = created.json()["sessionId"]
            started = _settle(
                client,
                session_id,
                client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"}),
            )
            assert started["status"] == Status.CAPTCHA_REQUIRED, started
            assert _eval(runner, session_id, "() => document.querySelector('#email').value") == ""
            blocked = client.post(
                f"/v1/sessions/{session_id}/continue",
                headers=AUTH,
                json={"candidateRef": "ref-a", "humanResolved": True},
            )
            assert blocked.status_code == 409
            assert blocked.json()["detail"]["reason"] == "challenge_present"
            held = client.post(f"/v1/sessions/{session_id}/resume", headers=AUTH)
            assert held.status_code == 200, held.text
            assert held.json()["status"] == Status.CAPTCHA_REQUIRED
            assert _eval(runner, session_id, "() => document.querySelector('#first').value") == ""
            _eval(
                runner,
                session_id,
                "() => { document.querySelectorAll('.g-recaptcha, iframe').forEach((node) => node.remove()); }",
            )
            resumed = client.post(f"/v1/sessions/{session_id}/resume", headers=AUTH)
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["status"] not in {Status.CAPTCHA_REQUIRED, Status.EMAIL_VERIFICATION_REQUIRED}
            assert _eval(runner, session_id, "() => document.querySelector('#first').value") == "Casey"
            assert _eval(runner, session_id, "() => document.querySelector('#email').value") == (
                "casey.example@example.com"
            )
            assert _eval(runner, session_id, "() => window.__submitted") is not True
    finally:
        pages.shutdown()
        resolver.shutdown()
