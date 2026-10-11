"""Location suggestions, prepared resumes, consent, and machine error reasons."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from autofill.dom_ui import public_blocking
from autofill.engine import ApplicationResult, AutofillOptions, Status, autofill_application
from autofill.extract import extract_page
from autofill.mapping import acknowledgement_kind, map_field
from autofill.models import Control
from autofill.profile import CandidateProfile
from autofill.resolver import ResolverBinding
from autofill.service.app import create_app
from autofill.stepfill import (
    commit_location_answer,
    fill_resolved_page,
    inspect_required,
    location_shape,
    suggest_control,
)

TOKEN = "synthetic-service-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
RESOLVER_TOKEN = "synthetic-resolver-token-0123456789abcdef"
FIXTURES = Path(__file__).parent / "fixtures"
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def _future() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {
            "personal": {"full_name": "River Example", "email": "river.example@example.com", "city": "Austin"},
            "work_authorization": {"legally_authorized_to_work": "Yes", "require_sponsorship": "No"},
        }
    )


def _question(**overrides) -> dict:
    item = {
        "kind": "QUESTION",
        "text": "Where are you currently located?",
        "fieldKey": "location",
        "questionId": "loc-1",
        "questionHash": "loc-1",
        "answerType": "single_select",
        "options": ["Florida City, Florida, United States", "Miami, Florida, United States"],
        "required": True,
        "selector": "#current-location",
        "blocking": True,
    }
    item.update(overrides)
    return item


class _Runner:
    def __init__(self) -> None:
        self.applied: list[dict] = []
        self.suggests: list[str] = []
        self.item = _question()
        self.hold = threading.Event()
        self.entered = threading.Event()
        self.block = False

    def run(self, request):
        status = Status.MANUAL_ANSWER_REQUIRED
        return ApplicationResult(
            status=status,
            ats="ashby",
            current_step=status,
            login_status="NOT_REQUIRED",
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            open_items=[dict(self.item)],
            blocking=public_blocking(self.item),
            progress={
                "stage": "QUESTIONS",
                "requiredTotal": 1,
                "requiredDone": 0,
                "percent": 0,
                "currentLabel": "Questions",
            },
        )

    def apply_answer(self, session_id: str, target: dict) -> dict:
        if self.block:
            self.entered.set()
            self.hold.wait(2)
        self.applied.append(target)
        return {"filled": True}

    def suggest(self, session_id: str, selector: str, query: str, *, clear: bool) -> dict:
        self.suggests.append(query)
        return {
            "found": True,
            "source": "live",
            "options": [{"label": "Florida City, Florida, United States"}, {"label": "Miami, Florida, United States"}],
        }

    def discard(self, session_id: str) -> None:
        return None

    def shutdown(self) -> None:
        return None


def _body() -> dict:
    return {
        "protocolVersion": "0.7.0",
        "candidateRef": "ref-a",
        "jobRef": "job-a",
        "jobContext": {
            "applicationUrl": "https://jobs.ashbyhq.com/example/application",
            "title": "Example Engineer",
            "company": "Example Labs",
        },
        "resolver": {
            "url": "https://resolver.example.test/v1/resolve",
            "token": RESOLVER_TOKEN,
            "expiresAt": _future(),
        },
    }


@pytest.fixture
def api():
    runner = _Runner()
    app = create_app(token=TOKEN, runner=runner, run_browser=False, reaper_interval_seconds=60)
    with TestClient(app) as client:
        yield client, runner


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


def test_location_shape_separates_city_state_and_place():
    assert location_shape("Austin") == "city"
    assert location_shape("florida") == "state"
    assert location_shape("FL") == "state"
    assert location_shape("Florida, United States") == "state"
    assert location_shape("Austin, TX") == "place"
    assert location_shape("Florida City, Florida, United States") == "place"


def test_suggest_returns_live_options_without_filling(api):
    client, runner = api
    created = client.post("/v1/sessions", headers=AUTH, json=_body())
    session_id = created.json()["sessionId"]
    client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
    suggested = client.post(
        f"/v1/sessions/{session_id}/suggest",
        headers=AUTH,
        json={"fieldKey": "location", "query": "florida"},
    )
    assert suggested.status_code == 200, suggested.text
    body = suggested.json()
    assert body["source"] == "live"
    assert body["options"][0] == {"label": "Florida City, Florida, United States"}
    assert "value" not in body["options"][0]
    assert runner.suggests == ["florida"]
    assert runner.applied == []
    plural = client.post(
        f"/v1/sessions/{session_id}/suggestions",
        headers=AUTH,
        json={"questionId": "loc-1", "query": "miami"},
    )
    assert plural.status_code == 200, plural.text
    assert plural.json()["source"] == "live"
    assert plural.json()["options"][1]["label"] == "Miami, Florida, United States"
    assert runner.suggests == ["florida", "miami"]
    stale = client.post(
        f"/v1/sessions/{session_id}/suggest",
        headers=AUTH,
        json={"questionId": "other", "query": "miami"},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["reason"] == "stale_blocker"


def test_answer_replays_an_idempotency_key_and_a_second_call_is_busy(api):
    client, runner = api
    created = client.post("/v1/sessions", headers=AUTH, json=_body())
    session_id = created.json()["sessionId"]
    client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
    headers = {**AUTH, "Idempotency-Key": "answer-florida-city"}
    first = client.post(
        f"/v1/sessions/{session_id}/answer",
        headers=headers,
        json={"fieldKey": "location", "value": "Florida City, Florida, United States"},
    )
    assert first.status_code == 200, first.text
    again = client.post(
        f"/v1/sessions/{session_id}/answer",
        headers=headers,
        json={"fieldKey": "location", "value": "Florida City, Florida, United States"},
    )
    assert again.status_code == 200
    assert again.json() == first.json()
    assert len(runner.applied) == 1
    runner.block = True
    other = client.post("/v1/sessions", headers=AUTH, json=_body())
    other_id = other.json()["sessionId"]
    client.post(f"/v1/sessions/{other_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
    results: list[int] = []

    def post() -> None:
        response = client.post(
            f"/v1/sessions/{other_id}/answer",
            headers=AUTH,
            json={"fieldKey": "location", "value": "Florida City, Florida, United States"},
        )
        results.append(response.status_code)

    first_thread = threading.Thread(target=post)
    first_thread.start()
    assert runner.entered.wait(2)
    second = client.post(
        f"/v1/sessions/{other_id}/answer",
        headers=AUTH,
        json={"fieldKey": "location", "value": "Miami, Florida, United States"},
    )
    runner.hold.set()
    first_thread.join(2)
    assert second.status_code == 409
    assert second.json()["detail"]["reason"] == "busy"
    assert 200 in results


def test_consent_requires_an_explicit_confirm(api):
    client, runner = api
    runner.item = _question(
        kind="CONSENT",
        text="I agree to the candidate privacy policy and the Terms and Conditions.",
        fieldKey=None,
        answerType="boolean",
        options=[],
    )
    created = client.post("/v1/sessions", headers=AUTH, json=_body())
    session_id = created.json()["sessionId"]
    client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
    refused = client.post(
        f"/v1/sessions/{session_id}/answer",
        headers=AUTH,
        json={"questionId": "loc-1", "value": "confirm"},
    )
    assert refused.status_code == 422
    assert refused.json()["detail"] == {"reason": "needs_option", "options": ["confirm"]}
    assert runner.applied == []
    accepted = client.post(
        f"/v1/sessions/{session_id}/answer",
        headers=AUTH,
        json={"questionId": "loc-1", "value": "confirm", "confirm": True},
    )
    assert accepted.status_code == 200, accepted.text
    assert runner.applied[-1]["check"] is True


def test_prepare_holds_a_resume_before_the_control_and_can_be_replaced(api):
    client, _runner = api
    created = client.post("/v1/sessions", headers=AUTH, json=_body())
    assert created.status_code == 201, created.text
    session_id = created.json()["sessionId"]
    prepared = client.post(
        f"/v1/sessions/{session_id}/resume-file",
        headers=AUTH,
        data={"candidateRef": "ref-a", "prepare": "true"},
        files={"file": ("notes.pdf", PDF, "application/pdf")},
    )
    assert prepared.status_code == 200, prepared.text
    assert prepared.json()["prepared"] is True
    assert prepared.json()["verified"] is False
    status = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
    assert status.json()["resume"]["prepared"] is True
    assert status.json()["resume"]["verified"] is False
    replaced = client.post(
        f"/v1/sessions/{session_id}/resume-file",
        headers=AUTH,
        data={"candidateRef": "ref-a", "prepare": "true"},
        files={"file": ("other.pdf", PDF, "application/pdf")},
    )
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["verified"] is False


def test_ashby_state_is_not_a_city_and_an_exact_place_commits(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "ashby_location_live.html").as_uri())
        target = {
            "fieldKey": "location",
            "text": "Where are you currently located?",
            "selector": "#current-location",
            "answerType": "single_select",
            "value": "florida",
        }
        refused = commit_location_answer(page, target)
        assert refused["filled"] is False
        assert refused["reason"] == "needs_option"
        assert "Florida City, Florida, United States" in refused["options"]
        assert "Florida, United States" in refused["options"]
        assert page.locator("#current-location").input_value() == ""
        payroll = "[data-field-path='_systemfield_location'] [role='combobox']"
        suggested = suggest_control(page, payroll, "florida", clear=True)
        assert suggested["source"] == "live"
        assert suggested["options"][0]["label"] == "Florida, United States"
        assert page.locator("[data-field-path='_systemfield_location'] [role='combobox']").input_value() == ""
        target["value"] = "Florida City, Florida, United States"
        committed = commit_location_answer(page, target)
        assert committed["filled"] is True
        assert page.locator("#current-location").input_value() == "Florida City, Florida, United States"
    finally:
        page.close()


def test_live_choice_lists_drop_junk_and_keep_every_real_option(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "lever_select_live.html").as_uri())
        gaps, _total, _done = inspect_required(page, extract_page(page), _profile())
        heard = next(item for item in gaps if "heard" in str(item.get("text") or "").casefold())
        assert "LinkedIn" in heard["options"]
        assert "Select..." not in heard["options"]
        assert all("click here" not in option.casefold() for option in heard["options"])
        school = next(item for item in gaps if "university" in str(item.get("text") or "").casefold())
        assert "Aalto University" in school["options"]
        assert all("click here" not in option.casefold() for option in school["options"])
        static = suggest_control(page, "#heard", "linked", clear=True)
        assert static["source"] == "static"
        assert any(option["label"] == "LinkedIn" for option in static["options"])

        page.goto((FIXTURES / "lever_radios_live.html").as_uri())
        radio_gaps, _total, _done = inspect_required(page, extract_page(page), _profile())
        radio = radio_gaps[0]
        assert radio["options"] == ["Yes", "No"]

        page.goto((FIXTURES / "lever_multiselect_live.html").as_uri())
        multi_gaps, _total, _done = inspect_required(page, extract_page(page), _profile())
        languages = multi_gaps[0]
        assert languages["answerType"] == "multi_select"
        assert len(languages["options"]) >= 24
        assert "English (ENG)" in languages["options"]
        assert "Marathi (MAR)" in languages["options"]
    finally:
        page.close()


def test_consent_blocks_ready_and_marketing_stays_unchecked(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "consent_marketing_live.html").as_uri())
        snapshot = extract_page(page)
        by_selector = {control.selector: control for control in snapshot.controls}
        terms = next(control for control in snapshot.controls if "terms and conditions" in control.label.casefold())
        news = next(control for control in snapshot.controls if "marketing" in control.label.casefold())
        assert acknowledgement_kind(terms) == "consent"
        assert terms.required is True
        assert acknowledgement_kind(news) == "marketing"
        assert news.required is False
        assert map_field(news, _profile()).action == "skip"
        factual = Control(
            kind="checkbox",
            label="Are you legally authorized to work?",
            selector="#auth",
            required=True,
        )
        assert map_field(factual, _profile()).action == "check"
        privacy = next(
            control
            for control in snapshot.controls
            if any("consent" in (option.label or "").casefold() for option in control.options)
        )
        assert acknowledgement_kind(privacy) == "consent"
        assert privacy.required is True
        assert "privacy policy" in privacy.label.casefold()
        assert map_field(privacy, _profile()).action == "skip"
        assert map_field(terms, _profile()).action == "skip"
        gaps, total, done = inspect_required(page, snapshot, _profile())
        consent_text = " ".join(str(item.get("text") or "") for item in gaps if item.get("kind") == "CONSENT")
        assert "privacy policy" in consent_text.casefold()
        assert all("marketing" not in str(item.get("text") or "").casefold() for item in gaps)
        assert done < total
        assert by_selector
        assert page.locator("#terms").is_checked() is False
        assert page.locator("#news").is_checked() is False
        assert page.locator("input[value='Yes, I consent']").is_checked() is False
    finally:
        page.close()


def test_resolver_yes_does_not_check_consent(browser):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            incoming = json.loads(raw)
            answers = [
                {
                    "id": question.get("id") or "",
                    "text": question.get("text") or "",
                    "value": "Yes",
                    "confidence": "HIGH",
                    "source": "approved_answer",
                }
                for question in incoming.get("questions") or []
                if isinstance(question, dict)
            ]
            body = json.dumps({"fields": {}, "answers": answers}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "consent_marketing_live.html").as_uri())
        binding = ResolverBinding(
            url=f"http://{host}:{port}/resolve",
            token="synthetic-resolver-token-0123456789abcdef",
            expires_at=_future(),
            candidate_ref="ref-a",
            job_ref="job-a",
        )
        fill_resolved_page(page, binding, session_id="sess-consent", ats_name="lever", step="questions")
        assert page.locator("input[value='Yes, I consent']").is_checked() is False
        assert page.locator("input[value='No, I do not consent']").is_checked() is False
        assert page.locator("#terms").is_checked() is False
        assert page.locator("#news").is_checked() is False
    finally:
        page.close()
        server.shutdown()


def test_prepared_resume_attaches_when_the_control_appears(browser):
    page = browser.new_page()
    try:
        url = (FIXTURES / "resume_accept.html").as_uri()
        page.goto(url)
        missing = autofill_application(
            url,
            _profile(),
            None,
            AutofillOptions(page=page, session_id="sess-resume", candidate_id="ref-a"),
        )
        assert missing.status == Status.RESUME_UPLOAD_REQUIRED
        assert missing.resume_verified_now is False
        page.goto(url)
        attached = autofill_application(
            url,
            _profile(),
            None,
            AutofillOptions(
                page=page,
                session_id="sess-resume",
                candidate_id="ref-a",
                resume_name="notes.pdf",
                resume_mime="application/pdf",
                resume_bytes=PDF,
                resume_prepared=True,
            ),
        )
        assert attached.resume_verified_now is True
        assert attached.status != Status.RESUME_UPLOAD_REQUIRED
        assert page.locator("#resume").evaluate("el => el.files.length") == 1
    finally:
        page.close()
