"""Protocol 0.6.0: visible typing, progress, and pause / answer. Local pages only."""

from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from autofill import AutofillOptions, Status, autofill_application
from autofill.dom_ui import (
    HIGHLIGHT_KINDS,
    PauseGate,
    _committed,
    begin_run,
    checkpoint,
    choose_blocking,
    commit_visible,
    end_run,
    focus_field,
    present_blocking,
    progress_percent,
    public_blocking,
    safe_label,
    stage_for,
    type_visible,
    typing_delays,
)
from autofill.engine import ApplicationResult
from autofill.fill import apply_mapped
from autofill.follow import follow_answer, read_page_mark
from autofill.models import Control, MappedField, Option, PageSnapshot
from autofill.profile import CandidateProfile
from autofill.resolver import ResolverBinding
from autofill.service.app import create_app
from autofill.stepfill import required_gaps

TOKEN = "synthetic-service-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
RESOLVER_TOKEN = "synthetic-resolver-token-0123456789abcdef"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "profile.example.json"
SECRET = "River Example Answer"


def _future() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def _resolver_body() -> dict:
    return {
        "protocolVersion": "0.6.0",
        "candidateRef": "ref-a",
        "jobRef": "job-a",
        "jobContext": {
            "applicationUrl": "https://boards.greenhouse.io/example/jobs/1",
            "title": "Example Engineer",
            "company": "Example Labs",
        },
        "resolver": {
            "url": "https://resolver.example.test/v1/resolve",
            "token": RESOLVER_TOKEN,
            "expiresAt": _future(),
        },
    }


def _question(**extra) -> dict:
    item = {
        "kind": "QUESTION",
        "questionId": "abc123",
        "questionHash": "abc123",
        "text": "Years of experience",
        "fieldKey": "yearsExperience",
        "answerType": "text",
        "options": [],
        "required": True,
        "selector": "#years",
        "blocking": True,
    }
    item.update(extra)
    return item


class _AnswerRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[bool, bool]] = []
        self.applied: list[dict] = []
        self.mode = "manual"
        self.item = _question()

    def run(self, request):
        self.calls.append((request.resume_uploaded, request.answers_updated))
        if self.mode == "login":
            status = Status.LOGIN_REQUIRED
        elif self.mode == "captcha":
            status = Status.CAPTCHA_REQUIRED
        elif self.mode == "ready" or request.answers_updated:
            status = Status.READY_FOR_HUMAN_SUBMIT
        else:
            status = Status.MANUAL_ANSWER_REQUIRED
        blocking = public_blocking(self.item) if status == Status.MANUAL_ANSWER_REQUIRED else None
        return ApplicationResult(
            status=status,
            ats="greenhouse",
            current_step=status,
            login_status="NOT_REQUIRED",
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            manual_questions=[{"intent": None, "text": self.item["text"]}],
            open_items=[dict(self.item)] if status == Status.MANUAL_ANSWER_REQUIRED else [],
            blocking=blocking,
            progress={
                "stage": "QUESTIONS",
                "requiredTotal": 2,
                "requiredDone": 1,
                "percent": 50,
                "currentLabel": "Questions",
            },
        )

    def apply_answer(self, session_id: str, target: dict) -> dict:
        self.applied.append({"session": session_id, "value": target.get("value"), "selector": target.get("selector")})
        return {"filled": True}

    def discard(self, session_id: str) -> None:
        return None

    def shutdown(self) -> None:
        return None


class _GateRunner:
    def __init__(self) -> None:
        self.calls = 0

    def run(self, request):
        self.calls += 1
        return ApplicationResult(
            status=Status.READY_FOR_HUMAN_SUBMIT,
            ats="greenhouse",
            current_step=Status.READY_FOR_HUMAN_SUBMIT,
            login_status="NOT_REQUIRED",
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            job_id=request.job_id,
        )

    def discard(self, session_id: str) -> None:
        return None

    def shutdown(self) -> None:
        return None


@pytest.fixture
def answer_app():
    runner = _AnswerRunner()
    app = create_app(token=TOKEN, runner=runner, run_browser=False, reaper_interval_seconds=60)
    with TestClient(app) as client:
        yield client, runner


@pytest.fixture
def gate_app():
    runner = _GateRunner()
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


def test_progress_percent_is_computed():
    assert progress_percent("READY", 0, 0) == 100
    assert progress_percent("READY", 4, 1) == 25
    assert progress_percent("READY", 3, 2) == 67
    assert progress_percent("EMPLOYMENT", 0, 0) == 45
    assert progress_percent("QUESTIONS", 0, 0) == 75
    assert progress_percent("OPENING", 0, 0) == 5
    assert progress_percent("PERSONAL", 4, 1) == 25
    assert progress_percent("PERSONAL", 3, 1) == 33
    assert progress_percent("PERSONAL", 2, 9) == 100
    assert stage_for("employment", "Employment History", "") == "EMPLOYMENT"
    assert stage_for("application", "", Status.READY_FOR_HUMAN_SUBMIT) == "READY"
    assert "river.example@example.com" not in safe_label("Contact river.example@example.com today")
    assert safe_label("Employment History") == "Employment History"


def test_required_gaps_classify_empty_fields_in_page_order():
    profile = CandidateProfile.from_dict(
        {
            "personal": {
                "full_name": "River Example",
                "email": "river.example@example.com",
                "country": "Exampleland",
            },
            "eeo_voluntary": {"veteran_status": "I am not a protected veteran"},
        }
    )
    snapshot = PageSnapshot(
        controls=[
            Control(kind="text", label="Phone", selector="#phone", required=True, autocomplete="tel"),
            Control(kind="text", label="What is your favorite color?", selector="#color", required=True),
            Control(
                kind="select",
                label="Country",
                selector="#country",
                required=True,
                options=[Option("Canada", "Canada"), Option("Mexico", "Mexico")],
            ),
            Control(
                kind="select",
                label="Veteran status",
                selector="#veteran",
                required=True,
                options=[Option("I am not a protected veteran", "I am not a protected veteran")],
            ),
        ],
        buttons=[],
    )

    class Page:
        def evaluate(self, script, arg=None):
            del script
            return False

    gaps = required_gaps(Page(), snapshot, profile)
    assert [item["kind"] for item in gaps] == [
        "MISSING_PROFILE_VALUE",
        "QUESTION",
        "OPTION_MISMATCH",
        "DEMOGRAPHIC",
    ]
    assert gaps[0]["fieldKey"] == "phone"
    assert gaps[1]["text"] == "What is your favorite color?"
    assert gaps[3]["intent"] == "VETERAN_STATUS"


def test_blocking_picks_status_then_the_first_human_item():
    question = _question()
    optional = _question(text="Nickname", blocking=False, questionId="opt")
    missing = _question(kind="MISSING_PROFILE_VALUE", fieldKey="address.city", text="City", questionId="city")
    assert choose_blocking(Status.CAPTCHA_REQUIRED, [question])["kind"] == "CAPTCHA"
    assert choose_blocking(Status.LOGIN_REQUIRED, [question])["kind"] == "LOGIN"
    assert choose_blocking(Status.NO_FORM_FOUND, [question])["kind"] == "NO_FORM"
    assert choose_blocking(Status.RESUME_UPLOAD_REQUIRED, [question])["kind"] == "RESUME"
    assert choose_blocking(Status.MANUAL_ANSWER_REQUIRED, [optional, missing, question])["questionId"] == "city"
    assert choose_blocking(Status.MANUAL_ANSWER_REQUIRED, [optional]) is None
    public = public_blocking(question)
    assert public is not None
    assert "selector" not in public
    assert set(public) == {
        "kind",
        "questionId",
        "questionHash",
        "text",
        "fieldKey",
        "answerType",
        "options",
        "required",
    }


def test_typing_delays_stay_in_range():
    seen: list[int] = []

    def randint(low: int, high: int) -> int:
        assert (low, high) == (30, 70)
        seen.append(len(seen))
        return 30 if len(seen) % 2 else 70

    short = typing_delays("abcd", randint)
    assert short == [30, 70, 30, 70]
    assert all(30 <= delay <= 70 for delay in short)
    assert len(typing_delays("x" * 200, randint)) == 200
    assert len(typing_delays("x" * 201, randint)) == 40


def test_checkpoint_pauses_between_fields():
    gate = PauseGate()
    gate.pause()
    gate.pause()
    token = begin_run(pause_gate=gate)
    started = threading.Event()
    copied = contextvars.copy_context()

    def wait() -> None:
        started.set()
        assert checkpoint()

    thread = threading.Thread(target=copied.run, args=(wait,))
    thread.start()
    assert started.wait(2)
    time.sleep(0.05)
    assert thread.is_alive()
    gate.resume()
    gate.resume()
    thread.join(2)
    assert not thread.is_alive()
    end_run(token)


def test_focus_waits_before_typing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AUTOFILL_TYPE_DELAY", "1")
    waits: list[int] = []

    class Page:
        def evaluate(self, script, arg=None):
            return True

        def add_init_script(self, script):
            return None

        def wait_for_timeout(self, millis: int) -> None:
            waits.append(millis)

    assert focus_field(Page(), "#name") is True
    assert waits == [300]


def test_protocol_060_status_keeps_050_fields(answer_app):
    client, _runner = answer_app
    created = client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
    assert created.status_code == 201
    body = created.json()
    assert body["protocolVersion"] == "0.6.0"
    assert body["stopped_before_submit"] is True
    assert body["status"] == "CREATED"
    assert body["paused"] is False
    assert body["blocking"] is None
    assert body["progress"] == {
        "stage": "OPENING",
        "requiredTotal": 0,
        "requiredDone": 0,
        "percent": 5,
        "currentLabel": "Opening",
    }
    assert "manualQuestions" in body
    schema = client.get("/openapi.json").json()
    assert "submit" not in "".join(schema["paths"])
    assert "/v1/sessions/{session_id}/pause" in schema["paths"]
    assert "/v1/sessions/{session_id}/answer" in schema["paths"]


def test_pause_and_resume_do_not_start_a_fill(gate_app):
    client, runner = gate_app
    created = client.post(
        "/v1/sessions",
        headers=AUTH,
        json={
            "candidateContext": {
                "candidateId": "candidate-a",
                "personal": {"full_name": "Avery Example", "email": "avery.example@example.com"},
            },
            "jobContext": {
                "applicationUrl": "https://boards.greenhouse.io/example/jobs/1",
                "jobId": "job-1",
                "title": "Example Engineer",
                "company": "Example Labs",
            },
        },
    )
    session_id = created.json()["sessionId"]
    first = client.post(f"/v1/sessions/{session_id}/pause", headers=AUTH)
    second = client.post(f"/v1/sessions/{session_id}/pause", headers=AUTH)
    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["paused"] is True
    assert runner.calls == 0
    resumed = client.post(f"/v1/sessions/{session_id}/resume", headers=AUTH)
    again = client.post(f"/v1/sessions/{session_id}/resume", headers=AUTH)
    assert resumed.json()["paused"] is False
    assert again.json()["paused"] is False
    assert again.json()["status"] == "CREATED"
    assert runner.calls == 0


def test_answer_fills_and_continues_without_logging_the_value(answer_app, caplog):
    client, runner = answer_app
    caplog.set_level(logging.INFO)
    created = client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
    session_id = created.json()["sessionId"]
    started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH)
    assert started.status_code == 200
    assert started.json()["blocking"]["kind"] == "QUESTION"
    assert started.json()["progress"]["percent"] == 50
    replied = client.post(
        f"/v1/sessions/{session_id}/answer",
        headers=AUTH,
        json={"questionHash": "abc123", "value": SECRET},
    )
    assert replied.status_code == 200
    body = replied.json()
    assert body["filled"] is True
    assert body["verified"] is True
    assert isinstance(body["ms"], int)
    assert body["ms"] >= 0
    assert runner.applied[0]["value"] == SECRET
    assert runner.calls[-1] == (False, True)
    assert SECRET not in caplog.text
    assert "action=answer result=filled" in caplog.text
    follow = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
    assert follow.json()["status"] == Status.READY_FOR_HUMAN_SUBMIT


def test_answer_accepts_a_unique_option_and_rejects_an_ambiguous_one(answer_app):
    client, runner = answer_app

    def answer(options: list[str], value: str):
        runner.item = _question(answerType="single_select", text="Choice", options=options)
        created = client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
        session_id = created.json()["sessionId"]
        client.post(f"/v1/sessions/{session_id}/start", headers=AUTH)
        return client.post(
            f"/v1/sessions/{session_id}/answer",
            headers=AUTH,
            json={"fieldKey": "yearsExperience", "value": value},
        )

    degree = answer(["Bachelor's Degree", "Master's Degree"], "Bachelor's")
    assert degree.status_code == 200
    assert degree.json()["filled"] is True
    assert runner.applied[-1]["value"] == "Bachelor's Degree"
    country = answer(["Canada +1", "United States +1", "United Kingdom +44"], "United States")
    assert country.status_code == 200
    assert runner.applied[-1]["value"] == "United States +1"
    ambiguous = answer(["Computer Science", "Political Science"], "Science")
    assert ambiguous.status_code == 422
    assert ambiguous.json()["detail"]["reason"] == "needs_option"
    assert ambiguous.json()["detail"]["options"] == ["Computer Science", "Political Science"]


def test_answer_rejects_unknown_targets_and_option_mismatches(answer_app):
    client, runner = answer_app
    created = client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
    session_id = created.json()["sessionId"]
    client.post(f"/v1/sessions/{session_id}/start", headers=AUTH)
    unknown = client.post(
        f"/v1/sessions/{session_id}/answer",
        headers=AUTH,
        json={"questionId": "missing", "value": "5"},
    )
    assert unknown.status_code == 409
    assert len(runner.calls) == 1
    assert runner.applied == []
    runner.item = _question(answerType="single_select", options=["1", "3", "5"])
    other = client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
    other_id = other.json()["sessionId"]
    started = client.post(f"/v1/sessions/{other_id}/start", headers=AUTH)
    assert started.status_code == 200
    mismatch = client.post(
        f"/v1/sessions/{other_id}/answer",
        headers=AUTH,
        json={"fieldKey": "yearsExperience", "value": "9"},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["detail"]["reason"] == "needs_option"
    assert mismatch.json()["detail"]["options"] == ["1", "3", "5"]
    assert runner.applied == []
    status = client.get(f"/v1/sessions/{other_id}/status", headers=AUTH)
    assert status.json()["blocking"]["kind"] == "QUESTION"
    assert status.json()["status"] == Status.MANUAL_ANSWER_REQUIRED


def test_answer_does_not_fill_during_login_or_captcha(answer_app):
    client, runner = answer_app
    runner.mode = "login"
    created = client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
    session_id = created.json()["sessionId"]
    client.post(f"/v1/sessions/{session_id}/start", headers=AUTH)
    denied = client.post(
        f"/v1/sessions/{session_id}/answer",
        headers=AUTH,
        json={"questionHash": "abc123", "value": SECRET},
    )
    assert denied.status_code == 409
    assert runner.applied == []
    assert len(runner.calls) == 1
    runner.mode = "captcha"
    other = client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
    other_id = other.json()["sessionId"]
    client.post(f"/v1/sessions/{other_id}/start", headers=AUTH)
    blocked = client.post(
        f"/v1/sessions/{other_id}/answer",
        headers=AUTH,
        json={"questionHash": "abc123", "value": SECRET},
    )
    assert blocked.status_code == 409
    assert runner.applied == []


def _spy(page) -> None:
    page.evaluate(
        """() => {
          window.__events = [];
          const orig = Element.prototype.scrollIntoView;
          Element.prototype.scrollIntoView = function(opts) {
            window.__events.push({
              type: "scroll",
              behavior: opts && opts.behavior,
              block: opts && opts.block,
              id: this.id,
            });
            return orig.call(this, opts);
          };
          document.addEventListener("input", (event) => {
            window.__events.push({type: "input", id: event.target.id, value: event.target.value});
          });
        }"""
    )


def test_text_fill_scrolls_smoothly_before_typing_and_skips_highlight(browser):
    page = browser.new_page()
    try:
        page.set_content("<label for='name'>Full name</label><input id='name'>")
        _spy(page)
        outcome = apply_mapped(
            page,
            Control(kind="text", selector="#name", label="Full name"),
            MappedField(key="full_name", action="fill", text="Casey Example"),
        )
        assert outcome.action == "fill"
        assert page.locator("#name").input_value() == "Casey Example"
        assert page.locator("#name").get_attribute("data-af-highlight") is None
        events = page.evaluate("() => window.__events")
        assert events[0] == {"type": "scroll", "behavior": "smooth", "block": "center", "id": "name"}
        assert any(item["type"] == "input" for item in events)
        assert events.index(next(item for item in events if item["type"] == "input")) > 0
    finally:
        page.close()


def test_visible_typing_uses_the_mock_clock(browser):
    page = browser.new_page()
    try:
        page.set_content("<input id='note'><textarea id='long'></textarea>")
        page.evaluate(
            """() => {
              window.__afDelays = [];
              window.__afClock = {
                sleep(ms) { window.__afDelays.push(ms); return Promise.resolve(); },
                nextDelay() { return 30 + (window.__afDelays.length % 41); },
              };
            }"""
        )
        assert type_visible(page, "#note", "abcd") is True
        delays = page.evaluate("() => window.__afDelays.slice()")
        assert delays == [30, 31, 32, 33]
        assert all(30 <= delay <= 70 for delay in delays)
        assert page.locator("#note").input_value() == "abcd"
        long_text = "R" + ("x" * 200)
        page.evaluate("() => { window.__afDelays = []; }")
        assert type_visible(page, "#long", long_text) is True
        assert page.evaluate("() => window.__afDelays.length") == 40
        assert page.locator("#long").input_value() == long_text
    finally:
        page.close()


def test_highlight_only_human_kinds_and_answer_clears_it(browser):
    page = browser.new_page()
    try:
        page.set_content(
            "<label for='years'>Years</label><input id='years'>"
            "<label for='name'>Full name</label><input id='name'>"
        )
        for kind in ("QUESTION", "MISSING_PROFILE_VALUE", "OPTION_MISMATCH", "DEMOGRAPHIC"):
            assert kind in HIGHLIGHT_KINDS
            present_blocking(page, {"kind": kind, "selector": "#years", "text": "Years"})
            assert page.locator("#years").get_attribute("data-af-highlight") == "1"
            outline = page.locator("#years").evaluate("el => el.style.outline")
            offset = page.locator("#years").evaluate("el => el.style.outlineOffset")
            assert "3px" in outline
            assert "255, 59, 48" in outline or "#FF3B30" in outline.upper()
            assert offset == "2px"
            assert page.locator("#name").get_attribute("data-af-highlight") is None
        for kind in ("LOGIN", "CAPTCHA", "RESUME", "NO_FORM"):
            present_blocking(page, {"kind": kind, "selector": "#years", "text": kind})
            assert page.locator("#years").get_attribute("data-af-highlight") is None
        present_blocking(page, {"kind": "QUESTION", "selector": "#years", "text": "Years"})
        filled = commit_visible(
            page,
            {"selector": "#years", "answerType": "text", "value": "5", "values": ["5"]},
        )
        assert filled == {"filled": True}
        assert page.locator("#years").input_value() == "5"
        assert page.locator("#years").get_attribute("data-af-highlight") is None
    finally:
        page.close()


def test_phone_commit_compares_digits_and_other_fields_fold(browser):
    page = browser.new_page()
    try:
        page.set_content(
            "<label for='phone'>Phone</label><input id='phone' type='tel'>"
            "<label for='city'>City</label><input id='city'>"
            "<label for='region'>State</label><select id='region'><option value='tx'>Texas</option></select>"
        )
        page.evaluate(
            """() => {
              const phone = document.querySelector('#phone');
              const valueOf = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
              phone.addEventListener('input', () => {
                const digits = phone.value.replace(/\\D/g, '');
                const national = digits.length > 10 ? digits.slice(digits.length - 10) : digits;
                if (national.length === 10) {
                  const formatted = '(' + national.slice(0, 3) + ') ' + national.slice(3, 6) + '-' + national.slice(6);
                  valueOf.call(phone, formatted);
                }
              });
              const city = document.querySelector('#city');
              city.addEventListener('input', () => {
                valueOf.call(city, city.value.replace(/\\s+/g, ' ').trim().toLowerCase());
              });
            }"""
        )
        filled = commit_visible(
            page,
            {
                "selector": "#phone",
                "answerType": "text",
                "fieldKey": "phone",
                "value": "5550100000",
                "values": ["5550100000"],
            },
        )
        assert filled == {"filled": True}
        assert page.locator("#phone").input_value() == "(555) 010-0000"
        page.evaluate("() => { document.querySelector('#phone').value = ''; }")
        international = commit_visible(
            page,
            {
                "selector": "#phone",
                "answerType": "text",
                "fieldKey": "phone",
                "value": "+1 5550100000",
                "values": ["+1 5550100000"],
            },
        )
        assert international == {"filled": True}
        assert page.locator("#phone").input_value() == "(555) 010-0000"
        assert _committed(page, "#phone", ["5550100001"], multi=False, field_key="phone") is False
        assert _committed(page, "#phone", ["+44 5550100000"], multi=False, field_key="") is True
        assert _committed(page, "#phone", ["12345550100000"], multi=False, field_key="phone") is False
        city = commit_visible(
            page,
            {
                "selector": "#city",
                "answerType": "text",
                "fieldKey": "city",
                "value": "  Austin   TX ",
                "values": ["  Austin   TX "],
            },
        )
        assert city == {"filled": True}
        assert page.locator("#city").input_value() == "austin tx"
        page.select_option("#region", "tx")
        assert _committed(page, "#region", ["texas"], multi=False, field_key="state") is True
        assert _committed(page, "#region", ["  TEXAS "], multi=False, field_key="state") is True
    finally:
        page.close()


def test_type_visible_types_into_an_inner_input(browser):
    page = browser.new_page()
    try:
        page.set_content("<div id='wrap'><input id='inner'></div>")
        page.evaluate(
            """() => {
              window.__writes = 0;
              document.getElementById('inner').addEventListener('input', () => { window.__writes += 1; });
            }"""
        )
        assert type_visible(page, "#wrap", "Casey") is True
        assert page.locator("#inner").input_value() == "Casey"
        assert page.evaluate("() => window.__writes") > 1
    finally:
        page.close()


def test_resolver_fill_types_each_character(browser, monkeypatch):
    from autofill.resolver import ResolverBinding, ResolverResponse
    from autofill.stepfill import fill_resolved_page

    def fake_resolve(binding, *, session_id, step, fields, questions):
        del binding, session_id, step, fields, questions
        return ResolverResponse(fields={"firstName": "Casey"})

    monkeypatch.setattr("autofill.stepfill.resolve_step", fake_resolve)
    page = browser.new_page()
    try:
        page.set_content("<label for='given'>First name</label><input id='given' autocomplete='given-name' required>")
        page.evaluate(
            """() => {
              window.__writes = 0;
              document.getElementById('given').addEventListener('input', () => { window.__writes += 1; });
            }"""
        )
        expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
        outcome = fill_resolved_page(
            page,
            ResolverBinding(
                url="https://resolver.example.test/v1/resolve",
                token=RESOLVER_TOKEN,
                expires_at=expires,
                candidate_ref="ref-a",
                job_ref="job-a",
            ),
            session_id="session-a",
            ats_name=None,
            step="personal",
        )
        assert page.locator("#given").input_value() == "Casey"
        assert page.evaluate("() => window.__writes") > 1
        assert any(field.action == "fill" for field in outcome.fields)
    finally:
        page.close()


def test_next_blocker_scan_fills_choice_options(browser):
    page = browser.new_page()
    try:
        page.set_content(
            """
            <label for="note">Notes</label><input id="note" required>
            <label for="degree">Degree</label>
            <select id="degree" required>
              <option value="">Select</option>
              <option>Bachelor's Degree</option>
              <option>Master's Degree</option>
            </select>
            <label for="code">Phone code</label>
            <div id="code" role="combobox" aria-required="true" aria-controls="code-list">Open</div>
            <ul id="code-list" role="listbox" hidden>
              <li id="us" role="option">United States +1</li>
              <li id="uk" role="option">United Kingdom +44</li>
            </ul>
            <fieldset>
              <legend>Work authorization</legend>
              <label><input type="radio" name="auth" value="yes" required> Yes</label>
              <label><input type="radio" name="auth" value="no"> No</label>
            </fieldset>
            """
        )
        page.evaluate(
            """() => {
              document.getElementById('code').addEventListener('click', () => {
                document.getElementById('code-list').hidden = false;
              });
            }"""
        )
        profile = CandidateProfile.from_dict(
            {"personal": {"full_name": "Casey Example", "email": "casey.example@example.com"}}
        )
        follow = follow_answer(page, profile=profile, job=None, held=set(), prior=[], previous=None)
        assert follow.blocking is not None
        assert follow.blocking["answerType"] == "text"
        choices = [
            item
            for item in follow.open_items
            if item.get("answerType") in {"single_select", "multi_select"}
        ]
        assert len(choices) >= 3
        for item in choices:
            assert item["options"]
        degree = next(item for item in choices if item["selector"] == "#degree")
        assert "Bachelor's Degree" in degree["options"]
        code = next(item for item in choices if item["selector"] == "#code")
        assert "United States +1" in code["options"]
        radio = next(item for item in choices if "Yes" in item["options"])
        assert "No" in radio["options"]
    finally:
        page.close()


def test_screenshot_runs_between_fields_on_the_worker():
    from autofill.service.runner import _Worker

    worker = _Worker(0, headless=True, launch_args=[])
    order: list[str] = []
    try:

        def fill() -> None:
            deadline = time.time() + 2
            while worker._queue.empty() and not worker._front and time.time() < deadline:
                time.sleep(0.01)
            checkpoint()
            order.append("after")

        def shot() -> None:
            order.append("shot")

        started = threading.Thread(target=lambda: worker.submit(fill))
        started.start()
        time.sleep(0.05)
        captured = threading.Thread(target=lambda: worker.submit(shot, drainable=True))
        captured.start()
        started.join(timeout=5)
        captured.join(timeout=5)
        assert not started.is_alive()
        assert not captured.is_alive()
        assert order == ["shot", "after"]
    finally:
        worker.shutdown()


def test_answer_rescan_sets_the_section_and_does_not_return_to_opening():
    from autofill.follow import _progress

    snapshot = PageSnapshot(heading="Apply", controls=[], buttons=[])
    city = _progress(
        snapshot,
        Status.MANUAL_ANSWER_REQUIRED,
        4,
        1,
        step="application",
        blocker={"fieldKey": "city", "text": "City"},
        previous_stage="PERSONAL",
    )
    assert city["stage"] == "ADDRESS"
    assert city["currentLabel"] == "Address"
    assert city["percent"] == progress_percent("ADDRESS", 4, 1)
    question = _progress(
        snapshot,
        Status.MANUAL_ANSWER_REQUIRED,
        4,
        2,
        step="application",
        blocker={"text": "What is your favorite color?"},
        previous_stage="PERSONAL",
    )
    assert question["stage"] == "QUESTIONS"
    kept = _progress(
        PageSnapshot(heading="", controls=[], buttons=[]),
        Status.MANUAL_ANSWER_REQUIRED,
        4,
        2,
        step="",
        blocker={"text": "What is your favorite color?"},
        previous_stage="EDUCATION",
    )
    assert kept["stage"] == "EDUCATION"
    ready = _progress(
        snapshot,
        Status.READY_FOR_HUMAN_SUBMIT,
        4,
        4,
        step="application",
        previous_stage="QUESTIONS",
    )
    assert ready["stage"] == "READY"


def test_follow_answer_reports_the_blocker_section(browser):
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Apply</h1>"
            "<label for='name'>Full name</label><input id='name' required value='Casey Example'>"
            "<label for='city'>City</label><input id='city' required>"
        )
        profile = CandidateProfile.from_dict(
            {"personal": {"full_name": "Casey Example", "email": "casey.example@example.com"}}
        )
        follow = follow_answer(
            page,
            profile=profile,
            job=None,
            held=set(),
            prior=[],
            previous=None,
            previous_stage="PERSONAL",
        )
        assert follow.status == Status.MANUAL_ANSWER_REQUIRED
        assert follow.blocking is not None
        assert follow.blocking["fieldKey"] == "city"
        assert follow.progress["stage"] == "ADDRESS"
        assert follow.progress["stage"] != "OPENING"
        assert follow.progress["requiredTotal"] >= 1
    finally:
        page.close()


def test_mid_type_flag_pumps_during_a_long_field(browser, monkeypatch):
    monkeypatch.setenv("AUTOFILL_MID_TYPE", "1")
    page = browser.new_page()
    text = "Casey Example wrote a long note for the form."
    seen: list[str] = []

    def pump() -> None:
        seen.append(page.locator("#note").input_value())

    from autofill.dom_ui import bind_queue_pump, release_queue_pump

    bind_queue_pump(pump)
    try:
        page.set_content("<textarea id='note'></textarea>")
        assert type_visible(page, "#note", text) is True
        assert page.locator("#note").input_value() == text
        assert seen[0] == text[:8]
        assert seen[-1] == text
    finally:
        release_queue_pump()
        page.close()


def test_follow_answer_does_not_type_when_the_step_changes(browser):
    page = browser.new_page()
    try:
        page.set_content("<h1>Questions</h1><label for='name'>Full name</label><input id='name' required>")
        page.locator("#name").fill("Casey Example")
        mark, _snapshot = read_page_mark(page)
        page.set_content("<h1>Review</h1><label for='note'>Note</label><input id='note' required>")
        profile = CandidateProfile.from_dict(
            {"personal": {"full_name": "Casey Example", "email": "casey.example@example.com"}}
        )
        follow = follow_answer(page, profile=profile, job=None, held=set(), prior=[], previous=mark)
        assert follow.changed is True
        assert follow.typed == []
        assert page.locator("#note").input_value() == ""
    finally:
        page.close()


def test_profile_mode_blocks_on_the_first_empty_required_field(browser):
    profile = CandidateProfile.from_dict(
        {"personal": {"full_name": "River Example", "email": "river.example@example.com"}}
    )
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='name'>Full name</label><input id='name' autocomplete='name' required>"
            "<label for='email'>Email</label><input id='email' type='email' autocomplete='email' required>"
            "<label for='phone'>Phone</label><input id='phone' autocomplete='tel' required>"
            "<label for='country'>Country</label><input id='country' autocomplete='country-name' required>"
            "<label for='auth'>Are you legally authorized to work?</label>"
            "<select id='auth' required><option value=''>Select...</option>"
            "<option>Yes</option><option>No</option></select>"
            "<label for='sponsor'>Will you now or in the future require sponsorship?</label>"
            "<select id='sponsor' required><option value=''>Select...</option>"
            "<option>Yes</option><option>No</option></select>"
            "<label for='veteran'>Veteran status</label>"
            "<select id='veteran'><option value=''>Select</option>"
            "<option>I am not a protected veteran</option></select>"
            "<button type='submit'>Submit application</button>"
            "</form>"
        )
        result = autofill_application(
            "https://boards.greenhouse.io/example/jobs/1",
            profile,
            None,
            AutofillOptions(page=page, session_id="sess-ready", candidate_id="candidate-a", job_id="job-1"),
        )
        assert result.status == Status.MANUAL_ANSWER_REQUIRED, result.messages
        assert result.blocking is not None
        assert result.blocking["kind"] == "MISSING_PROFILE_VALUE"
        assert result.blocking["fieldKey"] == "phone"
        assert result.progress["requiredDone"] < result.progress["requiredTotal"]
        assert result.progress["percent"] != 100
        assert result.progress["percent"] == progress_percent(
            result.progress["stage"],
            result.progress["requiredTotal"],
            result.progress["requiredDone"],
        )
        assert page.locator("#phone").get_attribute("data-af-highlight") == "1"
        assert page.locator("#name").input_value()
        assert page.locator("#email").input_value() == "river.example@example.com"
        assert page.locator("#country").input_value() == ""
        assert page.locator("#auth").input_value() == ""
        assert page.locator("#sponsor").input_value() == ""
        assert page.locator("#veteran").input_value() == ""
    finally:
        page.close()


def test_pause_holds_the_page_until_resume(browser):
    profile = CandidateProfile.load(EXAMPLE)
    page = browser.new_page()
    gate = PauseGate()
    gate.pause()
    page.set_content(
        "<h1>Personal information</h1>"
        "<label for='name'>Full name</label><input id='name' name='name' autocomplete='name' required>"
        "<label for='email'>Email</label><input id='email' type='email' autocomplete='email' required>"
    )

    def release() -> None:
        time.sleep(0.4)
        gate.resume()

    thread = threading.Thread(target=release)
    thread.start()
    started = time.monotonic()
    try:
        result = autofill_application(
            "https://boards.greenhouse.io/example/jobs/1",
            profile,
            None,
            AutofillOptions(
                page=page,
                pause_gate=gate,
                session_id="sess-pause",
                candidate_id="candidate-a",
                job_id="job-1",
            ),
        )
        assert time.monotonic() - started >= 0.4
        assert result.status in {Status.FILLED, Status.READY_FOR_HUMAN_SUBMIT, Status.RESUME_UPLOAD_REQUIRED}
        assert page.locator("#name").input_value()
        assert page.locator("#name").get_attribute("data-af-highlight") is None
        assert page.locator("#email").get_attribute("data-af-highlight") is None
    finally:
        gate.resume()
        thread.join(2)
        page.close()


class _Resolver(ThreadingHTTPServer):
    calls: list[dict]


def test_blocking_question_is_centered_and_known_fields_stay_plain(browser):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            self.server.calls.append(body)  # type: ignore[attr-defined]
            payload = {
                "fields": {"phone": "5550100142"},
                "answers": [],
                "unresolved": ["yearsExperience"],
            }
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
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Employment History</h1><form>"
            "<label for='phone'>Phone</label><input id='phone' name='phone' required>"
            "<label for='years'>How many years of experience do you have?</label>"
            "<input id='years' name='years' required>"
            "</form>"
        )
        _spy(page)
        result = autofill_application(
            "https://jobs.lever.co/example/role",
            CandidateProfile.from_dict(
                {
                    "candidateId": "ref-a",
                    "personal": {"full_name": "River Example", "email": "river.example@example.com"},
                }
            ),
            None,
            AutofillOptions(
                page=page,
                session_id="sess-block",
                candidate_id="ref-a",
                resolver=ResolverBinding(
                    url=f"http://{host}:{port}/resolve",
                    token=RESOLVER_TOKEN,
                    expires_at=_future(),
                    candidate_ref="ref-a",
                    job_ref="job-a",
                ),
            ),
        )
        assert result.status == Status.MANUAL_ANSWER_REQUIRED
        assert result.blocking is not None
        assert result.blocking["kind"] in HIGHLIGHT_KINDS
        assert result.blocking["text"]
        assert SECRET not in json.dumps(result.to_dict())
        assert result.progress["stage"] == "EMPLOYMENT"
        assert result.progress["currentLabel"] == "Employment History"
        total = result.progress["requiredTotal"]
        done = result.progress["requiredDone"]
        assert total > 0
        assert result.progress["percent"] == progress_percent(result.progress["stage"], total, done)
        assert page.locator("#phone").get_attribute("data-af-highlight") is None
        assert page.locator("#years").get_attribute("data-af-highlight") == "1"
        events = page.evaluate("() => window.__events")
        phone_events = [item for item in events if item.get("id") == "phone"]
        assert phone_events[0]["type"] == "scroll"
        assert phone_events[0]["behavior"] == "smooth"
        assert phone_events[0]["block"] == "center"
        manual = result.manual_questions
        assert manual
        assert set(manual[0]) == {"intent", "text"}
    finally:
        page.close()
        server.shutdown()
        thread.join(2)


_STUCK = "Auto-Fill couldn't set this field; please set it in the live view, then Continue"
_LEVER_LOCATION = (
    "Lever didn't offer location suggestions; pick Current location in the live view, then Continue"
)


class _LoopRunner:
    def __init__(self, *, fill: bool) -> None:
        self.fill = fill
        self.applied: list[str] = []
        self.interacts = 0

    def run(self, request):
        if request.human_resolved:
            status = Status.READY_FOR_HUMAN_SUBMIT
            item = None
        else:
            status = Status.MANUAL_ANSWER_REQUIRED
            item = _question(text="Current location", fieldKey="address.city")
        return ApplicationResult(
            status=status,
            ats="lever",
            current_step=status,
            login_status="NOT_REQUIRED",
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            open_items=[dict(item)] if item else [],
            blocking=public_blocking(item) if item else None,
            manual_questions=[{"intent": None, "text": item["text"]}] if item else [],
        )

    def apply_answer(self, session_id: str, target: dict) -> dict:
        self.applied.append(str(target.get("value") or ""))
        return {"filled": self.fill}

    def advance_after_answer(self, request):
        return self.run(request)

    def interact(self, session_id: str, action: dict) -> bool:
        self.interacts += 1
        return True

    def discard(self, session_id: str) -> None:
        return None

    def shutdown(self) -> None:
        return None


def _answer_location(client: TestClient, session_id: str):
    return client.post(
        f"/v1/sessions/{session_id}/answer",
        headers=AUTH,
        json={"questionHash": "abc123", "fieldKey": "address.city", "value": "Example City"},
    )


def test_three_failed_answers_stop_for_a_person():
    runner = _LoopRunner(fill=False)
    app = create_app(token=TOKEN, runner=runner, run_browser=False, reaper_interval_seconds=60)
    with TestClient(app) as client:
        created = client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
        session_id = created.json()["sessionId"]
        client.post(f"/v1/sessions/{session_id}/start", headers=AUTH)
        first = _answer_location(client, session_id)
        assert first.status_code == 200
        assert first.json()["filled"] is False
        assert first.json()["verified"] is False
        early = client.post(
            f"/v1/sessions/{session_id}/continue",
            headers=AUTH,
            json={"candidateRef": "ref-a", "humanResolved": True},
        )
        assert early.status_code == 409
        assert _answer_location(client, session_id).status_code == 200
        stopped = _answer_location(client, session_id)
        assert stopped.status_code == 409
        assert stopped.json()["detail"]["reason"] == "needs_manual"
        assert len(runner.applied) == 3
        again = _answer_location(client, session_id)
        assert again.status_code == 409
        assert again.json()["detail"]["reason"] == "needs_manual"
        assert len(runner.applied) == 3
        status = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        assert status.json()["status"] == Status.MANUAL_ANSWER_REQUIRED
        assert status.json()["blocking"]["text"] == _STUCK
        moved = client.post(
            f"/v1/sessions/{session_id}/interact",
            headers=AUTH,
            json={"action": "key", "key": "Tab", "candidateRef": "ref-a"},
        )
        assert moved.status_code == 200
        assert runner.interacts == 1
        continued = client.post(
            f"/v1/sessions/{session_id}/continue",
            headers=AUTH,
            json={"candidateRef": "ref-a", "humanResolved": True},
        )
        assert continued.status_code == 200, continued.text
        assert continued.json()["status"] == Status.READY_FOR_HUMAN_SUBMIT


def test_lever_without_suggestions_stops_after_one_answer():
    class Runner(_LoopRunner):
        def apply_answer(self, session_id: str, target: dict) -> dict:
            self.applied.append(str(target.get("value") or ""))
            return {"filled": False, "suggestionCount": 0, "searchStatus": 403, "leverSearch": True}

    runner = Runner(fill=False)
    app = create_app(token=TOKEN, runner=runner, run_browser=False, reaper_interval_seconds=60)
    with TestClient(app) as client:
        created = client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
        session_id = created.json()["sessionId"]
        client.post(f"/v1/sessions/{session_id}/start", headers=AUTH)
        stopped = _answer_location(client, session_id)
        assert stopped.status_code == 409
        assert stopped.json()["detail"]["reason"] == "needs_manual"
        assert len(runner.applied) == 1
        status = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        assert status.json()["blocking"]["text"] == _LEVER_LOCATION


class _SplitRunner:
    """Location and another question take turns. The location key also changes."""

    def __init__(self) -> None:
        self.applied: list[str] = []
        self.location_answers = 0
        self.show_location = True

    def run(self, request):
        if self.show_location:
            key = "address.city" if self.location_answers % 2 == 0 else "location"
            item = _question(
                text="Current location",
                fieldKey=key,
                questionHash="loc-hash",
                questionId="loc-hash",
                selector="#location-input",
            )
        else:
            item = _question(
                text="Phone",
                fieldKey="phone",
                questionHash="phone-hash",
                questionId="phone-hash",
                selector="#phone",
            )
        return ApplicationResult(
            status=Status.MANUAL_ANSWER_REQUIRED,
            ats="lever",
            current_step=Status.MANUAL_ANSWER_REQUIRED,
            login_status="NOT_REQUIRED",
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            open_items=[dict(item)],
            blocking=public_blocking(item),
            manual_questions=[{"intent": None, "text": item["text"]}],
        )

    def apply_answer(self, session_id: str, target: dict) -> dict:
        digest = str(target.get("questionHash") or "")
        self.applied.append(digest)
        if digest == "loc-hash":
            self.location_answers += 1
            self.show_location = False
        else:
            self.show_location = True
        return {"filled": True}

    def advance_after_answer(self, request):
        return self.run(request)

    def interact(self, session_id: str, action: dict) -> bool:
        return True

    def discard(self, session_id: str) -> None:
        return None

    def shutdown(self) -> None:
        return None


def _answer(client: TestClient, session_id: str, *, digest: str, field_key: str, value: str):
    return client.post(
        f"/v1/sessions/{session_id}/answer",
        headers=AUTH,
        json={"questionHash": digest, "fieldKey": field_key, "value": value},
    )


def test_answer_attempts_follow_the_field_across_the_session():
    runner = _SplitRunner()
    app = create_app(token=TOKEN, runner=runner, run_browser=False, reaper_interval_seconds=60)
    with TestClient(app) as client:
        created = client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
        session_id = created.json()["sessionId"]
        client.post(f"/v1/sessions/{session_id}/start", headers=AUTH)
        first = _answer(client, session_id, digest="loc-hash", field_key="address.city", value="Austin")
        assert first.json()["filled"] is True
        phone = _answer(client, session_id, digest="phone-hash", field_key="phone", value="5550100142")
        assert phone.json()["filled"] is True
        second = _answer(client, session_id, digest="loc-hash", field_key="location", value="Austin")
        assert second.json()["filled"] is True
        phone = _answer(client, session_id, digest="phone-hash", field_key="phone", value="5550100142")
        assert phone.json()["filled"] is True
        third = _answer(client, session_id, digest="loc-hash", field_key="address.city", value="Austin")
        assert third.json()["filled"] is True
        phone = _answer(client, session_id, digest="phone-hash", field_key="phone", value="5550100142")
        assert phone.json()["filled"] is True
        stopped = _answer(client, session_id, digest="loc-hash", field_key="location", value="Austin")
        assert stopped.status_code == 409
        assert stopped.json()["detail"]["reason"] == "needs_manual"
        assert runner.applied.count("loc-hash") == 3


def test_three_answers_without_progress_stop_for_a_person():
    runner = _LoopRunner(fill=True)
    app = create_app(token=TOKEN, runner=runner, run_browser=False, reaper_interval_seconds=60)
    with TestClient(app) as client:
        created = client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
        session_id = created.json()["sessionId"]
        client.post(f"/v1/sessions/{session_id}/start", headers=AUTH)
        assert _answer_location(client, session_id).json()["filled"] is True
        assert _answer_location(client, session_id).json()["filled"] is True
        stopped = _answer_location(client, session_id)
        assert stopped.status_code == 409
        assert stopped.json()["detail"]["reason"] == "needs_manual"
        assert len(runner.applied) == 3
        status = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        assert status.json()["blocking"]["text"] == _STUCK


def test_filled_answer_rechecks_the_scan_commit(browser):
    from autofill.dom_ui import control_committed

    page = browser.new_page()
    try:
        page.set_content(
            "<div class='select'>"
            "<div id='box' class='select__control' role='combobox' aria-controls='list' "
            "style='width:220px;height:40px'></div>"
            "<input id='hidden-country' type='hidden'>"
            "<div id='list' role='listbox'>"
            "<div id='us' role='option' style='height:24px'>United States +1</div>"
            "</div></div>"
            "<script>"
            "document.getElementById('us').addEventListener('click', () => {"
            "document.getElementById('hidden-country').value = 'United States +1';"
            "});"
            "</script>"
        )
        filled = commit_visible(
            page,
            {
                "selector": "#box",
                "answerType": "single_select",
                "fieldKey": "address.country",
                "value": "United States +1",
                "values": ["United States +1"],
            },
        )
        assert page.locator("#hidden-country").input_value() == "United States +1"
        assert control_committed(page, "#box") is False
        assert filled == {"filled": False, "reason": "not_committed"}
    finally:
        page.close()
