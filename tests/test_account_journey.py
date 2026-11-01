"""Synthetic employer journeys: no real employer account is created.

Create Account -> application -> multi-step -> review, saved-login Sign In,
account already exists, and the OTP stop. No secret may reach the logs,
the response bodies, or the employer's Submit.
"""

from __future__ import annotations

import logging
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.test_protocol_070 import AUTH, TOKEN, _start, resolver_origin  # noqa: F401

from autofill.engine import Status
from autofill.service.app import create_app

POOL_PASSWORD = "Pool-Pass-14c!"  # 14 chars, meets the synthetic Workday-style rules
SAVED_LOGIN = "Saved-Login-7!"
EMAIL = "synthetic.candidate@example.com"
SETTLED = {Status.STARTED, "RUNNING", "FILLING"}


@pytest.fixture
def employer():
    hits: list[str] = []

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            if self.path.startswith("/submitted"):
                self.send_response(204)
                self.end_headers()
                return
            super().do_GET()

        def log_message(self, format, *args):
            return

    handler = partial(Handler, directory=str(Path(__file__).parent / "fixtures"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", hits
    finally:
        server.shutdown()


@pytest.fixture
def client():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from autofill.service.runner import PlaywrightRunner

    try:
        with sync_playwright() as playwright:
            playwright.chromium.launch(headless=True).close()
    except Exception as exc:
        pytest.skip(f"Chromium is not installed: {exc}")
    runner = PlaywrightRunner(enabled=True, headless=True, workers=1)
    app = create_app(token=TOKEN, runner=runner, run_browser=True)
    with TestClient(app) as test_client:
        yield test_client


def _send(client: TestClient, session_id: str, mode: str, password: str):
    return client.post(
        f"/v1/sessions/{session_id}/credentials",
        headers=AUTH,
        json={"candidateRef": "ref-a", "email": EMAIL, "password": password, "confirmPassword": password, "mode": mode},
    )


def _settle(client: TestClient, session_id: str) -> dict:
    deadline = time.monotonic() + 40
    body = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH).json()
    while body.get("status") in SETTLED and time.monotonic() < deadline:
        time.sleep(0.1)
        body = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH).json()
    return body


def _no_secret(caplog, *texts: str) -> None:
    for secret in (POOL_PASSWORD, SAVED_LOGIN):
        assert secret not in caplog.text
        for text in texts:
            assert secret not in text


def test_signup_then_application_steps_to_review(caplog, client, employer, resolver_origin):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    origin, hits = employer
    stop = _start(client, resolver_origin, f"{origin}/account_journey.html?mode=register")
    assert stop["status"] == Status.ACCOUNT_CREATION_REQUIRED
    assert stop["account"]["mode"] == "register"
    policy = stop["account"]["passwordPolicy"]
    assert policy["minLength"] == 8
    assert policy["requireUpper"] and policy["requireLower"] and policy["requireDigit"] and policy["requireSymbol"]
    sent = _send(client, stop["sessionId"], "register", POOL_PASSWORD)
    assert sent.status_code == 200, sent.text
    assert sent.json()["outcome"] == "registered"
    final = _settle(client, stop["sessionId"])
    assert final["status"] not in {Status.ACCOUNT_CREATION_REQUIRED, Status.LOGIN_REQUIRED}
    walked = _walk_to_review(client, stop["sessionId"], final)
    end = client.get(f"/v1/sessions/{stop['sessionId']}/status", headers=AUTH).json()
    assert end.get("pageType") == "REVIEW", end.get("pageType")
    assert end["status"] != "SUBMITTED"
    assert walked, f"never reached review: {final.get('status')}"
    assert not any(path.startswith("/submitted") for path in hits), "Submit must stay with a human"
    _no_secret(caplog, sent.text, str(final))


def _walk_to_review(client: TestClient, session_id: str, body: dict) -> bool:
    """Continue through the steps the way the Companion does, never submitting."""
    for _ in range(6):
        status = str(body.get("status"))
        progress = body.get("progress") or {}
        if "REVIEW" in status or "READY" in status or progress.get("stage") in {"REVIEW", "READY"}:
            return True
        moved = client.post(
            f"/v1/sessions/{session_id}/continue",
            headers=AUTH,
            json={"candidateRef": "ref-a", "humanResolved": True},
        )
        if moved.status_code >= 400:
            body = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH).json()
        body = _settle(client, session_id)
    status = str(body.get("status"))
    return "REVIEW" in status or "READY" in status


def test_saved_login_signs_in_and_continues(caplog, client, employer, resolver_origin):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    origin, hits = employer
    stop = _start(client, resolver_origin, f"{origin}/account_journey.html?mode=signin")
    assert stop["status"] == Status.LOGIN_REQUIRED
    sent = _send(client, stop["sessionId"], "login", SAVED_LOGIN)
    assert sent.status_code == 200, sent.text
    assert sent.json()["outcome"] == "signed_in"
    final = _settle(client, stop["sessionId"])
    assert final["status"] not in {Status.LOGIN_REQUIRED, "LOGIN_FAILED"}
    assert not any(path.startswith("/submitted") for path in hits)
    _no_secret(caplog, sent.text, str(final))


def test_account_exists_is_left_to_a_human(caplog, client, employer, resolver_origin):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    origin, _hits = employer
    stop = _start(client, resolver_origin, f"{origin}/account_journey.html?mode=exists")
    assert stop["status"] == Status.ACCOUNT_CREATION_REQUIRED
    sent = _send(client, stop["sessionId"], "register", POOL_PASSWORD)
    assert sent.status_code == 422, sent.text
    assert sent.json()["detail"]["reason"] == "account_exists"
    after = client.get(f"/v1/sessions/{stop['sessionId']}/status", headers=AUTH).json()
    assert after["status"] in {Status.ACCOUNT_CREATION_REQUIRED, Status.LOGIN_REQUIRED}
    _no_secret(caplog, sent.text, str(after))


def test_otp_page_stops_and_types_nothing(caplog, client, employer, resolver_origin):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    origin, _hits = employer
    otp = _start(client, resolver_origin, f"{origin}/account_otp.html")
    assert otp["status"] == Status.EMAIL_VERIFICATION_REQUIRED
    assert otp["blocking"]["kind"] == "VERIFICATION"
    refused = _send(client, otp["sessionId"], "register", POOL_PASSWORD)
    assert refused.status_code == 409
    _no_secret(caplog, refused.text)
