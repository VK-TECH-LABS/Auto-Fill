"""One real Chromium run through the HTTP service, using the local fixture only."""

from __future__ import annotations

import json
import threading
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
        assert started.status_code == 200, started.text
        body = started.json()
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


def _start(client: TestClient, session_id: str) -> dict:
    started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
    assert started.status_code == 200, started.text
    return started.json()


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
        assert mismatch.json()["detail"] == "Candidate does not match this session."
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
