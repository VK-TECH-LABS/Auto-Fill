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
    runner = PlaywrightRunner(enabled=True, headless=True)
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
