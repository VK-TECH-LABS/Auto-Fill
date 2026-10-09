"""Eight-page synthetic application. The resolver is asked only for the current step."""

from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from autofill import AutofillOptions, Credentials, MemoryCredentialProvider, Status, autofill_application
from autofill.profile import CandidateProfile
from autofill.resolver import ResolverBinding

FIXTURE = Path(__file__).parent / "fixtures" / "eight_page_application.html"
PASSWORD = "synthetic-password-candidate-a"
TOKEN = "synthetic-resolver-token-0123456789abcdef"
URL = "https://boards.greenhouse.io/example/jobs/1"
DOMAIN = "boards.greenhouse.io"
CANARY = "CanaryNameShouldNotFill"

ANSWERS = {
    "US_WORK_AUTHORIZATION": {"value": "Yes", "values": []},
    "SPONSORSHIP_NOW": {"value": "No", "values": []},
    "SPONSORSHIP_FUTURE": {"value": "No", "values": []},
    "AGE_18_PLUS": {"value": "Yes", "values": []},
    "RELOCATE": {"value": "Yes", "values": []},
    "TRAVEL_PERCENT": {"value": "50%", "values": []},
    "SHIFT_AVAILABILITY": {"value": "", "values": ["Day", "Night"]},
    "SECURITY_CLEARANCE": {"value": "No", "values": []},
    "PE_LICENSE": {"value": "No", "values": []},
    "DRIVERS_LICENSE": {"value": "Yes", "values": []},
    "SALARY_EXPECTATION": {"value": "86420", "values": []},
    "EMPLOYMENT_TYPE": {"value": "Full-time", "values": []},
}

FIELDS = {
    "identity": {"firstName": "Riverstone", "lastName": "Example"},
    "contact": {
        "email": "river.example@example.com",
        "phone": "555-010-0199",
        "links.website": "https://example.com/river",
        "links.linkedin": "https://www.linkedin.com/in/example-river",
    },
    "address": {
        "address.line1": "1 Example Road",
        "address.region": "CA",
        "address.postalCode": "00042",
        "address.country": "United States",
    },
    "employment": {
        "employment[]": [{"company": "Example Labs", "title": "Example Engineer", "startDate": "2020-06-01"}],
        "yearsExperience": "4",
        "skills": "Python",
        "projects[]": [{"name": "Example Portal"}],
    },
    "education": {
        "education[]": [
            {"school": "Example University", "degree": "Bachelor", "field": "Computer Science", "endDate": "2019-05-01"}
        ]
    },
}


class _Resolver(ThreadingHTTPServer):
    calls: list[dict]
    mode: str


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


@pytest.fixture
def resolver():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
            server: _Resolver = self.server  # type: ignore[assignment]
            server.calls.append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization", ""),
                    "body": body,
                }
            )
            if server.mode in {"401", "409", "410", "422"}:
                self.send_response(int(server.mode))
                if server.mode == "422":
                    encoded = json.dumps(
                        {
                            "fields": {"email": "sentinel-must-not-fill@example.com"},
                            "answers": [{"intent": "RELOCATE", "value": "Yes", "confidence": "HIGH"}],
                        }
                    ).encode("utf-8")
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                    return
                self.end_headers()
                return
            step = body.get("step", "")
            known = FIELDS.get(step, {})
            fields = {key: known[key] for key in body.get("fields", []) if key in known}
            answers = []
            for question in body.get("questions", []):
                intent = question.get("intent")
                saved = ANSWERS.get(intent)
                if saved is None:
                    continue
                answers.append(
                    {
                        "intent": intent,
                        "value": saved["value"],
                        "values": saved["values"],
                        "confidence": "HIGH",
                        "source": "saved_answer",
                    }
                )
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
    server.mode = "ok"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield server, f"http://{host}:{port}/resolve"
    finally:
        server.shutdown()


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {
            "candidateId": "ref-a",
            "personal": {
                "full_name": CANARY,
                "email": "canary@example.com",
                "phone": "555-010-0000",
                "city": "Canary City",
            },
            "workAuthorization": "CANARY-AUTH",
            "salaryExpectation": "1",
        }
    )


def _provider() -> MemoryCredentialProvider:
    store = MemoryCredentialProvider()
    store.put("ref-a", DOMAIN, Credentials(site=DOMAIN, email="avery.example@example.com", password=PASSWORD))
    return store


def _binding(url: str) -> ResolverBinding:
    return ResolverBinding(
        url=url,
        token=TOKEN,
        expires_at=_future(),
        candidate_ref="ref-a",
        job_ref="job-a",
        timeout_seconds=5,
        retries=1,
        max_bytes=65536,
    )


def _values(page) -> str:
    return page.evaluate(
        """() => [...document.querySelectorAll('input,select,textarea')]
            .map((el) => (el.type === 'checkbox' || el.type === 'radio') ? '' : (el.value || ''))
            .join('\\n')"""
    )


def test_eight_page_fills_from_the_resolver_and_never_submits(browser, resolver, caplog):
    caplog.set_level(logging.DEBUG)
    server, url = resolver
    page = browser.new_page()
    try:
        page.goto(FIXTURE.as_uri())
        timings: dict[str, int] = {}
        first = autofill_application(
            URL,
            _profile(),
            _provider(),
            AutofillOptions(
                job_id="job-a",
                candidate_id="ref-a",
                session_id="sess-eight",
                page=page,
                resolver=_binding(url),
                timings=timings,
                max_pages=12,
            ),
        )
        assert first.status == Status.MANUAL_ANSWER_REQUIRED, first.messages
        assert first.stopped_before_submit is True
        assert {"intent": None, "text": "What is your favorite prime number?"} in first.manual_questions
        assert page.locator("#first").input_value() == "Riverstone"
        assert page.locator("#last").input_value() == "Example"
        assert page.locator("#email").input_value() == "river.example@example.com"
        assert page.locator("#phone").input_value() == "5550100199"
        assert page.locator("#state").input_value() == "california"
        assert page.locator("#start-date").input_value() == "06/01/2020"
        assert page.locator("#employer").input_value() == "Example Labs"
        assert page.locator("#school").input_value() == "Example University"
        assert page.locator("#end-date").input_value() == "2019-05-01"
        assert page.locator("#salary").input_value() == "86420"
        assert page.locator("#employment-type").input_value() == "Full-time"
        assert page.locator("#prime").input_value() == ""
        assert page.locator("#auth-yes").is_checked()
        assert page.locator("#sponsor-now-no").is_checked()
        assert page.locator("#shift-day").is_checked()
        assert page.locator("#shift-night").is_checked()
        assert page.locator("#shift-weekend").is_checked() is False
        assert page.locator("#travel").input_value() == "50%"
        assert CANARY not in _values(page)
        assert "canary@example.com" not in _values(page)
        assert "CANARY-AUTH" not in _values(page)
        assert timings["firstFormInspectedMs"] >= 0

        by_step: dict[str, list[dict]] = {}
        for call in server.calls:
            assert call["auth"] == f"Bearer {TOKEN}"
            assert TOKEN not in call["path"]
            body = call["body"]
            by_step.setdefault(body["step"], []).append(body)
            assert body["candidateRef"] == "ref-a"
            assert body["jobRef"] == "job-a"
        assert set(by_step["identity"][0]["fields"]) == {"firstName", "lastName"}
        assert by_step["identity"][0]["questions"] == []
        assert set(by_step["contact"][0]["fields"]) == {"email", "phone", "links.website", "links.linkedin"}
        assert by_step["contact"][0]["questions"] == []
        assert set(by_step["address"][0]["fields"]) == {
            "address.line1",
            "address.region",
            "address.postalCode",
            "address.country",
        }
        assert set(by_step["employment"][0]["fields"]) == {"employment[]", "yearsExperience", "skills", "projects[]"}
        assert set(by_step["education"][0]["fields"]) == {"education[]"}
        question_calls = by_step["questions"]
        intents = [item.get("intent") for item in question_calls[0]["questions"]]
        assert set(intents) == set(ANSWERS) | {None}
        assert question_calls[0]["fields"] == []
        assert "resume" not in by_step
        for step, calls in by_step.items():
            keys = [tuple(call["fields"]) for call in calls]
            assert len(set(keys)) == 1, step

        second = autofill_application(
            URL,
            _profile(),
            _provider(),
            AutofillOptions(
                job_id="job-a",
                candidate_id="ref-a",
                session_id="sess-eight",
                page=page,
                resolver=_binding(url),
                answers_updated=True,
                timings={},
                max_pages=12,
            ),
        )
        assert second.status == Status.RESUME_UPLOAD_REQUIRED, second.messages
        assert page.locator("#resume").input_value() == ""
        third = autofill_application(
            URL,
            _profile(),
            _provider(),
            AutofillOptions(
                job_id="job-a",
                candidate_id="ref-a",
                session_id="sess-eight",
                page=page,
                resolver=_binding(url),
                resume_uploaded=True,
                timings={},
                max_pages=12,
            ),
        )
        assert third.status == Status.READY_FOR_HUMAN_SUBMIT, third.messages
        assert page.evaluate("() => window.__submitCount") == 0
        clicked = page.evaluate("() => window.__clicked")
        assert "Submit application" not in clicked
        for forbidden in ("Withdraw", "Delete", "Decline", "Cancel Application", "Back"):
            assert forbidden not in clicked
        assert page.locator("#terms").is_checked() is False
        assert "Submit application" in third.submit_controls
        text = caplog.text
        assert "Riverstone" not in text
        assert "86420" not in text
        assert PASSWORD not in text
        assert TOKEN not in text
        assert "sess-eight" in text
    finally:
        page.close()


def test_resolver_401_expired_and_mismatch_stop_clean(browser, resolver):
    server, url = resolver
    page = browser.new_page()
    try:
        page.set_content("<h1>Contact</h1><label for='email'>Email</label><input id='email' type='email'>")
        server.mode = "401"
        denied = autofill_application(
            URL,
            _profile(),
            None,
            AutofillOptions(page=page, resolver=_binding(url), session_id="sess-401", candidate_id="ref-a"),
        )
        assert denied.status == Status.FAILED_FINAL
        assert page.locator("#email").input_value() == ""
        server.mode = "410"
        expired = autofill_application(
            URL,
            _profile(),
            None,
            AutofillOptions(page=page, resolver=_binding(url), session_id="sess-410", candidate_id="ref-a"),
        )
        assert expired.status == Status.EXPIRED
        server.mode = "409"
        mismatch = autofill_application(
            URL,
            _profile(),
            None,
            AutofillOptions(page=page, resolver=_binding(url), session_id="sess-409", candidate_id="ref-a"),
        )
        assert mismatch.status == Status.FAILED_FINAL
        assert page.locator("#email").input_value() == ""
    finally:
        page.close()


def test_resolver_422_stays_on_the_session_with_blanks(browser, resolver, caplog):
    server, url = resolver
    caplog.set_level(logging.INFO)
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Contact</h1>"
            "<label for='email'>Email</label><input id='email' type='email'>"
            "<label for='relocate'>Are you willing to relocate?</label>"
            "<select id='relocate'><option value=''>Select...</option><option>Yes</option><option>No</option></select>"
        )
        server.mode = "422"
        result = autofill_application(
            URL,
            _profile(),
            None,
            AutofillOptions(page=page, resolver=_binding(url), session_id="sess-422", candidate_id="ref-a"),
        )
        assert result.status == Status.MANUAL_ANSWER_REQUIRED
        assert result.status not in {Status.FAILED_FINAL, Status.FAILED_RETRYABLE, Status.EXPIRED, Status.FAILED}
        assert page.locator("#email").input_value() == ""
        assert page.locator("#relocate").input_value() == ""
        assert any(item.get("intent") == "RELOCATE" for item in result.manual_questions)
        assert "unprocessable" in caplog.text
        assert "sentinel-must-not-fill" not in caplog.text
    finally:
        page.close()
