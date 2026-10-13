"""One real Chromium run through the HTTP service, using the local fixture only."""

from __future__ import annotations

import json
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from autofill.engine import Status
from autofill.service.app import create_app
from autofill.service.runner import PlaywrightRunner

TOKEN = "synthetic-service-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "profile.example.json"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _chromium_or_skip() -> None:
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:
        pytest.skip(f"Chromium is not installed: {exc}")


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args) -> None:
        return


@pytest.fixture
def fixture_origin():
    handler = partial(_QuietHandler, directory=str(FIXTURES))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()


def test_http_service_fills_local_form_and_stops_before_submit(fixture_origin: str):
    _chromium_or_skip()
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    raw["candidateId"] = "candidate-example"
    runner = PlaywrightRunner(enabled=True, headless=True, workers=1)
    app = create_app(token=TOKEN, runner=runner, run_browser=True)
    email = raw["personal"]["email"]
    with TestClient(app) as client:
        created = client.post(
            "/v1/sessions",
            headers=AUTH,
            json={
                "candidateContext": raw,
                "jobContext": {
                    "applicationUrl": f"{fixture_origin}/sample_application.html",
                    "jobId": "job-example",
                    "candidateId": "candidate-example",
                },
                "approvedAnswers": [],
            },
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["sessionId"]
        started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
        body = _settle(client, session_id, started)
        assert body["status"] == Status.RESUME_UPLOAD_REQUIRED
        assert body["stopped_before_submit"] is True
        assert "Submit application" in body["submitControls"]
        assert email not in started.text
        continued = client.post(
            f"/v1/sessions/{session_id}/continue",
            headers=AUTH,
            json={"resumeUploaded": True, "candidateId": "candidate-example"},
        )
        assert continued.status_code == 200, continued.text
        after = continued.json()
        assert after["stopped_before_submit"] is True
        assert after["status"] in {Status.READY_FOR_HUMAN_SUBMIT, Status.FILLED}
        assert "Submit application" in after["submitControls"]
        assert email not in continued.text
        wrong = client.post(
            f"/v1/sessions/{session_id}/continue",
            headers=AUTH,
            json={"resumeUploaded": True, "candidateId": "candidate-other"},
        )
        assert wrong.status_code == 409


def _candidate(source: dict, candidate_id: str, full_name: str, preferred: str, email: str, city: str) -> dict:
    profile = json.loads(json.dumps(source))
    profile["candidateId"] = candidate_id
    profile["personal"]["full_name"] = full_name
    profile["personal"]["preferred_name"] = preferred
    profile["personal"]["email"] = email
    profile["personal"]["city"] = city
    return profile


def _create_session(client: TestClient, origin: str, profile: dict, job_id: str) -> str:
    created = client.post(
        "/v1/sessions",
        headers=AUTH,
        json={
            "candidateContext": profile,
            "jobContext": {
                "applicationUrl": f"{origin}/sample_application.html",
                "jobId": job_id,
                "candidateId": profile["candidateId"],
            },
            "approvedAnswers": [],
        },
    )
    assert created.status_code == 201, created.text
    return created.json()["sessionId"]


def _settle(client: TestClient, session_id: str, started) -> dict:
    """Start returns when navigation begins. These tests want the finished status."""
    assert started.status_code == 200, started.text
    body = started.json()
    deadline = time.monotonic() + 40
    while body.get("status") == Status.STARTED and time.monotonic() < deadline:
        time.sleep(0.05)
        polled = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        assert polled.status_code == 200, polled.text
        body = polled.json()
    return body


def _start(client: TestClient, session_id: str) -> dict:
    started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
    return _settle(client, session_id, started)


def _eval(runner: PlaywrightRunner, session_id: str, expression: str, argument: object | None = None) -> object:
    """Run a Playwright expression on the browser thread that owns the shared driver."""

    def read() -> object:
        page = runner._sessions[session_id].page
        if argument is None:
            return page.evaluate(expression)
        return page.evaluate(expression, argument)

    return runner._submit(read, session_id)


_FORM_VALUES = """() => ({
  first: document.querySelector('#first').value,
  email: document.querySelector('#email').value,
  city: document.querySelector('#city').value
})"""


def test_two_open_sessions_stay_isolated_and_a_third_works_after_delete(fixture_origin: str):
    """A second session must not start another Playwright driver while the first browser is open."""
    _chromium_or_skip()
    source = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    first = _candidate(source, "candidate-a", "Casey Example", "Casey", "casey.example@example.com", "Casey City")
    second = _candidate(source, "candidate-b", "Blair Example", "Blair", "blair.example@example.com", "Blair City")
    third = _candidate(source, "candidate-c", "Drew Example", "Drew", "drew.example@example.com", "Drew City")
    runner = PlaywrightRunner(enabled=True, headless=True, workers=1)
    app = create_app(token=TOKEN, runner=runner, run_browser=True)
    with TestClient(app) as client:
        session_a = _create_session(client, fixture_origin, first, "job-a")
        session_b = _create_session(client, fixture_origin, second, "job-b")
        started: dict[str, dict] = {}
        errors: dict[str, BaseException] = {}

        def start_one(key: str, session_id: str) -> None:
            try:
                started[key] = _start(client, session_id)
            except BaseException as exc:
                errors[key] = exc

        threads = [
            threading.Thread(target=start_one, args=("a", session_a)),
            threading.Thread(target=start_one, args=("b", session_b)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
        assert not errors, errors
        assert not any(thread.is_alive() for thread in threads)
        assert started["a"]["status"] == Status.RESUME_UPLOAD_REQUIRED
        assert started["b"]["status"] == Status.RESUME_UPLOAD_REQUIRED
        assert started["a"]["stopped_before_submit"] is True
        assert started["b"]["stopped_before_submit"] is True
        assert "Submit application" in started["a"]["submitControls"]
        assert "Submit application" in started["b"]["submitControls"]

        values_a = _eval(runner, session_a, _FORM_VALUES)
        values_b = _eval(runner, session_b, _FORM_VALUES)
        assert values_a == {"first": "Casey", "email": "casey.example@example.com", "city": "Casey City"}
        assert values_b == {"first": "Blair", "email": "blair.example@example.com", "city": "Blair City"}
        _eval(runner, session_a, "(owner) => localStorage.setItem('autofill_owner', owner)", "candidate-a")
        _eval(runner, session_b, "(owner) => localStorage.setItem('autofill_owner', owner)", "candidate-b")
        assert _eval(runner, session_a, "() => localStorage.getItem('autofill_owner')") == "candidate-a"
        assert _eval(runner, session_b, "() => localStorage.getItem('autofill_owner')") == "candidate-b"
        driver = runner._driver._playwright
        assert driver is not None
        assert runner._driver._browser is not None

        continued: dict[str, dict] = {}

        def continue_one(key: str, session_id: str, candidate_id: str) -> None:
            try:
                response = client.post(
                    f"/v1/sessions/{session_id}/continue",
                    headers=AUTH,
                    json={"resumeUploaded": True, "candidateId": candidate_id},
                )
                assert response.status_code == 200, response.text
                continued[key] = response.json()
            except BaseException as exc:
                errors[key] = exc

        threads = [
            threading.Thread(target=continue_one, args=("a", session_a, "candidate-a")),
            threading.Thread(target=continue_one, args=("b", session_b, "candidate-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=90)
        assert not errors, errors
        assert not any(thread.is_alive() for thread in threads)
        for key in ("a", "b"):
            assert continued[key]["status"] in {Status.READY_FOR_HUMAN_SUBMIT, Status.FILLED}
            assert continued[key]["stopped_before_submit"] is True
            assert "Submit application" in continued[key]["submitControls"]
            rendered = json.dumps(continued[key])
            assert "casey.example@example.com" not in rendered
            assert "blair.example@example.com" not in rendered
        assert _eval(runner, session_a, _FORM_VALUES) == {
            "first": "Casey",
            "email": "casey.example@example.com",
            "city": "Casey City",
        }
        assert _eval(runner, session_b, _FORM_VALUES) == {
            "first": "Blair",
            "email": "blair.example@example.com",
            "city": "Blair City",
        }

        deleted = client.delete(f"/v1/sessions/{session_a}", headers=AUTH)
        assert deleted.status_code == 204
        assert session_a not in runner._sessions
        assert session_b in runner._sessions
        assert runner._driver._playwright is driver
        assert _eval(runner, session_b, _FORM_VALUES)["email"] == "blair.example@example.com"
        mismatch = client.post(
            f"/v1/sessions/{session_b}/continue",
            headers=AUTH,
            json={"resumeUploaded": True, "candidateId": "candidate-a"},
        )
        assert mismatch.status_code == 409
        assert mismatch.json()["detail"] == {"reason": "needs_manual"}
        still_there = client.get(f"/v1/sessions/{session_b}/status", headers=AUTH)
        assert still_there.status_code == 200
        assert still_there.json()["status"] in {Status.READY_FOR_HUMAN_SUBMIT, Status.FILLED}
        assert still_there.json()["stopped_before_submit"] is True

        removed = client.delete(f"/v1/sessions/{session_b}", headers=AUTH)
        assert removed.status_code == 204
        assert runner._driver._playwright is driver
        session_c = _create_session(client, fixture_origin, third, "job-c")
        started_c = _start(client, session_c)
        assert started_c["status"] == Status.RESUME_UPLOAD_REQUIRED
        assert started_c["stopped_before_submit"] is True
        assert runner._driver._playwright is driver
        assert _eval(runner, session_c, _FORM_VALUES) == {
            "first": "Drew",
            "email": "drew.example@example.com",
            "city": "Drew City",
        }


def test_unanswered_required_question_blocks_until_answer(fixture_origin: str):
    """READY waits until every visible required field is committed."""
    _chromium_or_skip()
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    raw["candidateId"] = "candidate-example"
    runner = PlaywrightRunner(enabled=True, headless=True, workers=1)
    app = create_app(token=TOKEN, runner=runner, run_browser=True)
    with TestClient(app) as client:
        created = client.post(
            "/v1/sessions",
            headers=AUTH,
            json={
                "candidateContext": raw,
                "jobContext": {
                    "applicationUrl": f"{fixture_origin}/required_question.html",
                    "jobId": "job-example",
                    "candidateId": "candidate-example",
                    "title": "Example Engineer",
                    "company": "Example Labs",
                },
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["protocolVersion"] == "0.6.0"
        session_id = created.json()["sessionId"]
        started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
        body = _settle(client, session_id, started)
        assert body["status"] == Status.MANUAL_ANSWER_REQUIRED, body.get("messages")
        blocking = body["blocking"]
        assert blocking["kind"] == "QUESTION"
        assert "favorite color" in blocking["text"].lower()
        progress = body["progress"]
        assert progress["requiredDone"] < progress["requiredTotal"]
        assert progress["percent"] != 100
        assert progress["percent"] == min(
            99,
            round(100 * progress["requiredDone"] / progress["requiredTotal"]),
        )
        highlighted = "() => document.querySelector('#color').getAttribute('data-af-highlight')"
        assert _eval(runner, session_id, highlighted) == "1"
        plain = "() => document.querySelector('#name').getAttribute('data-af-highlight')"
        assert _eval(runner, session_id, plain) is None
        assert _eval(runner, session_id, "() => document.querySelector('#color').value") == ""
        answered = client.post(
            f"/v1/sessions/{session_id}/answer",
            headers=AUTH,
            json={"questionHash": blocking["questionHash"], "value": "Blue"},
        )
        assert answered.status_code == 200, answered.text
        assert answered.json()["filled"] is True
        finished = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        assert finished.status_code == 200, finished.text
        done = finished.json()
        assert done["status"] == Status.READY_FOR_HUMAN_SUBMIT, done.get("messages")
        assert done["blocking"] is None
        assert done["progress"]["requiredDone"] == done["progress"]["requiredTotal"]
        assert done["progress"]["percent"] == 100
        assert done["stopped_before_submit"] is True
        assert _eval(runner, session_id, "() => document.querySelector('#color').value") == "Blue"
        assert _eval(runner, session_id, highlighted) is None
        assert _eval(runner, session_id, "() => window.__submitted") is not True


_HIGHLIGHT_BOX = """() => {
  const node = document.querySelector("[data-af-highlight]");
  if (!node) return null;
  const box = node.getBoundingClientRect();
  return {
    id: node.id,
    className: String(node.className || ""),
    height: box.height,
    shadow: node.style.boxShadow || "",
    inputMarked: document.querySelector("#desk").getAttribute("data-af-highlight"),
  };
}"""

_COUNTRY_VALUE = """() => ({
  hidden: document.querySelector("#country-hidden").value,
  shown: document.querySelector("#country-single").textContent,
})"""


def test_answer_does_not_refill_committed_fields(fixture_origin: str, monkeypatch: pytest.MonkeyPatch):
    """An answer types the new field and the next blocker, not fields that already committed."""
    _chromium_or_skip()
    import autofill.engine as engine

    fills = {"count": 0}
    original = engine.fill_one_page

    def counting(*args, **kwargs):
        fills["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(engine, "fill_one_page", counting)
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    raw["candidateId"] = "candidate-example"
    runner = PlaywrightRunner(enabled=True, headless=True, workers=1)
    app = create_app(token=TOKEN, runner=runner, run_browser=True)
    with TestClient(app) as client:
        created = client.post(
            "/v1/sessions",
            headers=AUTH,
            json={
                "candidateContext": raw,
                "jobContext": {
                    "applicationUrl": f"{fixture_origin}/incremental_answer.html",
                    "jobId": "job-example",
                    "candidateId": "candidate-example",
                    "title": "Example Engineer",
                    "company": "Example Labs",
                },
            },
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["sessionId"]
        started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
        body = _settle(client, session_id, started)
        assert body["status"] == Status.MANUAL_ANSWER_REQUIRED, body.get("messages")
        assert "color" in body["blocking"]["text"].lower()
        assert fills["count"] >= 1
        filled_pages = fills["count"]
        _eval(
            runner,
            session_id,
            """() => {
              window.__writes = [];
              document.addEventListener("input", (event) => {
                window.__writes.push(event.target.id);
              }, true);
            }""",
        )
        answered = client.post(
            f"/v1/sessions/{session_id}/answer",
            headers=AUTH,
            json={"questionHash": body["blocking"]["questionHash"], "value": "Blue"},
        )
        assert answered.status_code == 200, answered.text
        payload = answered.json()
        assert payload["filled"] is True
        assert isinstance(payload["ms"], int)
        assert payload["ms"] < 3000
        assert fills["count"] == filled_pages
        writes = _eval(runner, session_id, "() => window.__writes.slice()")
        assert "name" not in writes
        assert "email" not in writes
        assert "animal" not in writes
        assert "city" in writes
        values = _eval(
            runner,
            session_id,
            """() => ({
              name: document.querySelector('#name').value,
              email: document.querySelector('#email').value,
              color: document.querySelector('#color').value,
              city: document.querySelector('#city').value,
              animal: document.querySelector('#animal').value,
              highlighted: document.querySelector('[data-af-highlight]')
                ? document.querySelector('[data-af-highlight]').id
                : ""
            })""",
        )
        assert values["name"] == "Casey Example"
        assert values["email"] == "casey.example@example.com"
        assert values["color"] == "Blue"
        assert values["city"] == "Example City"
        assert values["animal"] == ""
        assert values["highlighted"] == "animal"
        follow = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        assert follow.status_code == 200, follow.text
        assert follow.json()["status"] == Status.MANUAL_ANSWER_REQUIRED
        assert "animal" in follow.json()["blocking"]["text"].lower()
        assert follow.json()["status"] != Status.RESUME_UPLOAD_REQUIRED


def test_react_select_outline_answer_and_country_alias(fixture_origin: str):
    """A Greenhouse-style combobox is outlined on its box, answered from the menu, and country aliases match."""
    _chromium_or_skip()
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    raw["candidateId"] = "candidate-example"
    raw["personal"]["full_name"] = "River Example"
    raw["personal"]["email"] = "river.example@example.com"
    raw["personal"]["country"] = "USA"
    runner = PlaywrightRunner(enabled=True, headless=True, workers=1)
    app = create_app(token=TOKEN, runner=runner, run_browser=True)
    with TestClient(app) as client:
        created = client.post(
            "/v1/sessions",
            headers=AUTH,
            json={
                "candidateContext": raw,
                "jobContext": {
                    "applicationUrl": f"{fixture_origin}/react_select_country.html",
                    "jobId": "job-example",
                    "candidateId": "candidate-example",
                    "title": "Example Engineer",
                    "company": "Example Labs",
                },
            },
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["sessionId"]
        started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
        body = _settle(client, session_id, started)
        assert body["status"] == Status.MANUAL_ANSWER_REQUIRED, body.get("messages")
        blocking = body["blocking"]
        assert blocking["kind"] == "QUESTION"
        assert "desk" in blocking["text"].lower()
        assert "Window" in blocking["options"]
        assert "Aisle" in blocking["options"]
        assert len(blocking["options"]) <= 200
        assert all(len(option) <= 200 for option in blocking["options"])
        country = _eval(runner, session_id, _COUNTRY_VALUE)
        shown = str(country["shown"]).casefold().strip()
        hidden = str(country["hidden"]).casefold().strip()
        aliases = {"united states", "united states of america", "usa", "us"}
        assert shown in aliases or hidden in aliases
        box = _eval(runner, session_id, _HIGHLIGHT_BOX)
        assert box["height"] > 20
        assert "select__control" in box["className"]
        assert box["id"] == "desk-control"
        assert box["inputMarked"] is None
        assert "255, 59, 48" in box["shadow"] or "FF3B30" in box["shadow"].upper()
        answered = client.post(
            f"/v1/sessions/{session_id}/answer",
            headers=AUTH,
            json={"questionHash": blocking["questionHash"], "value": "Window"},
        )
        assert answered.status_code == 200, answered.text
        assert answered.json()["filled"] is True
        assert isinstance(answered.json()["ms"], int)
        finished = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        assert finished.status_code == 200, finished.text
        done = finished.json()
        assert done["status"] == Status.READY_FOR_HUMAN_SUBMIT, done.get("messages")
        assert done["progress"]["percent"] == 100
        assert done["stopped_before_submit"] is True
        assert "river.example@example.com" not in finished.text
        desk = _eval(
            runner,
            session_id,
            "() => ({"
            " hidden: document.querySelector('#desk-hidden').value,"
            " shown: document.querySelector('#desk-single').textContent,"
            " mark: document.querySelector('[data-af-highlight]')"
            " })",
        )
        assert desk["hidden"] == "Window"
        assert desk["shown"] == "Window"
        assert desk["mark"] is None
        assert _eval(runner, session_id, "() => window.__submitted") is not True


def test_ttl_reaper_closes_the_live_browser_context(fixture_origin: str):
    """An expired session drops its browser context and rejects every follow-up."""
    _chromium_or_skip()
    raw = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    raw["candidateId"] = "candidate-example"
    runner = PlaywrightRunner(enabled=True, headless=True, workers=1)
    app = create_app(token=TOKEN, runner=runner, run_browser=True, reaper_interval_seconds=0.05)
    with TestClient(app) as client:
        created = client.post(
            "/v1/sessions",
            headers=AUTH,
            json={
                "candidateContext": raw,
                "jobContext": {
                    "applicationUrl": f"{fixture_origin}/required_question.html",
                    "jobId": "job-example",
                    "candidateId": "candidate-example",
                    "title": "Example Engineer",
                    "company": "Example Labs",
                },
            },
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["sessionId"]
        started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
        assert _settle(client, session_id, started)["status"] == Status.MANUAL_ANSWER_REQUIRED
        assert session_id in runner._sessions
        held = app.state.store.get(session_id)
        assert held is not None
        color_before = _eval(runner, session_id, "() => document.querySelector('#color').value")
        assert color_before == ""
        held.expires_at = time.monotonic() - 1
        deadline = time.monotonic() + 3
        while session_id in runner._sessions and time.monotonic() < deadline:
            time.sleep(0.05)
        assert session_id not in runner._sessions
        assert held.status == Status.EXPIRED
        status = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        shot = client.get(f"/v1/sessions/{session_id}/screenshot", headers=AUTH)
        interact = client.post(
            f"/v1/sessions/{session_id}/interact",
            headers=AUTH,
            json={"action": "key", "key": "Escape"},
        )
        pause = client.post(f"/v1/sessions/{session_id}/pause", headers=AUTH)
        resume = client.post(f"/v1/sessions/{session_id}/resume", headers=AUTH)
        answer = client.post(
            f"/v1/sessions/{session_id}/answer",
            headers=AUTH,
            json={"questionHash": "color", "value": "Blue"},
        )
        continued = client.post(
            f"/v1/sessions/{session_id}/continue",
            headers=AUTH,
            json={"resumeUploaded": True, "candidateId": "candidate-example"},
        )
        credentials = client.post(
            f"/v1/sessions/{session_id}/credentials",
            headers=AUTH,
            json={"email": "river.example@example.com", "password": "synthetic-password-expired-session"},
        )
        codes = [
            status.status_code,
            shot.status_code,
            interact.status_code,
            pause.status_code,
            resume.status_code,
            answer.status_code,
            continued.status_code,
            credentials.status_code,
        ]
        assert codes == [410] * len(codes)
        assert "synthetic-password-expired-session" not in credentials.text
        assert session_id not in runner._sessions
        assert held.paused is False
        assert held.held_answers == []
        missing = client.get("/v1/sessions/missing-session/status", headers=AUTH)
        assert missing.status_code == 404
