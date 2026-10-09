"""Screenshot, human interaction, and protocol 0.5.0 continue. Synthetic data only."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from autofill.engine import ApplicationResult, Status
from autofill.service.app import create_app
from autofill.service.runner import PlaywrightRunner

TOKEN = "synthetic-service-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
RESOLVER_TOKEN = "synthetic-resolver-token-0123456789abcdef"
FIXTURES = Path(__file__).parent / "fixtures"
TYPED = "synthetic-typed-secret-value"


def _future() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def _profile_body(url: str) -> dict:
    return {
        "candidateContext": {
            "candidateId": "ref-a",
            "personal": {"full_name": "River Example", "email": "river.example@example.com"},
        },
        "jobContext": {"applicationUrl": url, "jobId": "job-a", "candidateId": "ref-a"},
    }


def _resolver_body() -> dict:
    return {
        "protocolVersion": "0.5.0",
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


class _Runner:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.discarded: list[str] = []
        self.typed: list[str] = []
        self.page_open = False

    def run(self, request):
        self.calls.append(request.session_id)
        self.page_open = True
        status = Status.CAPTCHA_REQUIRED if len(self.calls) == 1 else Status.FILLED
        return ApplicationResult(
            status=status,
            ats="greenhouse",
            current_step=status,
            login_status="NOT_REQUIRED",
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            fields_detected=0 if status == Status.CAPTCHA_REQUIRED else 1,
        )

    def discard(self, session_id: str) -> None:
        self.discarded.append(session_id)
        self.page_open = False

    def shutdown(self) -> None:
        return None

    def screenshot(self, session_id: str):
        if not self.page_open:
            return None
        return b"\xff\xd8\xff\xd9", "example.test"

    def interact(self, session_id: str, action: dict) -> bool:
        if not self.page_open:
            return False
        if action.get("action") == "type":
            self.typed.append(str(action.get("text") or ""))
        return True


@pytest.fixture
def api():
    runner = _Runner()
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as client:
        yield client, runner


def test_screenshot_requires_auth_and_an_open_page(api):
    client, runner = api
    missing = client.get("/v1/sessions/missing/screenshot")
    assert missing.status_code == 401
    created = client.post("/v1/sessions", headers=AUTH, json=_profile_body("https://boards.greenhouse.io/example/jobs/1"))
    session_id = created.json()["sessionId"]
    unknown = client.get("/v1/sessions/does-not-exist/screenshot", headers=AUTH)
    assert unknown.status_code == 404
    closed = client.get(f"/v1/sessions/{session_id}/screenshot", headers=AUTH)
    assert closed.status_code == 409
    started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
    assert started.json()["status"] == Status.CAPTCHA_REQUIRED
    shot = client.get(f"/v1/sessions/{session_id}/screenshot", headers=AUTH)
    assert shot.status_code == 200, shot.text
    assert shot.headers["content-type"].startswith("image/jpeg")
    assert shot.content.startswith(b"\xff\xd8")
    assert shot.headers["x-autofill-status"] == Status.CAPTCHA_REQUIRED
    assert shot.headers["x-autofill-url-host"] == "example.test"
    assert "/" not in shot.headers["x-autofill-url-host"]
    assert runner.page_open is True


def test_interact_is_limited_to_human_stops_and_does_not_log_text(api, caplog):
    client, runner = api
    caplog.set_level(logging.INFO)
    created = client.post("/v1/sessions", headers=AUTH, json=_profile_body("https://boards.greenhouse.io/example/jobs/1"))
    session_id = created.json()["sessionId"]
    early = client.post(
        f"/v1/sessions/{session_id}/interact",
        headers=AUTH,
        json={"action": "type", "text": TYPED, "candidateId": "ref-a"},
    )
    assert early.status_code == 409
    assert TYPED not in early.text
    client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
    mismatch = client.post(
        f"/v1/sessions/{session_id}/interact",
        headers=AUTH,
        json={"action": "type", "text": TYPED, "candidateRef": "ref-other"},
    )
    assert mismatch.status_code == 409
    typed = client.post(
        f"/v1/sessions/{session_id}/interact",
        headers=AUTH,
        json={"action": "type", "text": TYPED, "candidateRef": "ref-a"},
    )
    assert typed.status_code == 200, typed.text
    assert typed.json()["status"] == Status.CAPTCHA_REQUIRED
    assert typed.json()["screenshotVersion"] == 1
    assert TYPED not in caplog.text
    assert TYPED not in typed.text
    assert runner.typed == [TYPED]
    for _ in range(29):
        assert (
            client.post(
                f"/v1/sessions/{session_id}/interact",
                headers=AUTH,
                json={"action": "key", "key": "Tab", "candidateRef": "ref-a"},
            ).status_code
            == 200
        )
    limited = client.post(
        f"/v1/sessions/{session_id}/interact",
        headers=AUTH,
        json={"action": "key", "key": "Enter", "candidateRef": "ref-a"},
    )
    assert limited.status_code == 429


def test_human_done_and_human_resolved_continue(api):
    client, runner = api
    created = client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
    assert created.status_code == 201, created.text
    assert created.json()["protocolVersion"] == "0.5.0"
    assert RESOLVER_TOKEN not in created.text
    session_id = created.json()["sessionId"]
    started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
    assert started.json()["status"] == Status.CAPTCHA_REQUIRED
    blocked = client.post(
        f"/v1/sessions/{session_id}/continue",
        headers=AUTH,
        json={"candidateRef": "ref-a"},
    )
    assert blocked.status_code == 409
    continued = client.post(
        f"/v1/sessions/{session_id}/continue",
        headers=AUTH,
        json={"candidateRef": "ref-a", "humanResolved": True},
    )
    assert continued.status_code == 200, continued.text
    assert continued.json()["status"] == Status.FILLED
    assert len(runner.calls) == 2
    done = client.post(
        f"/v1/sessions/{session_id}/human-done",
        headers=AUTH,
        json={"candidateRef": "ref-a", "outcome": "abandoned"},
    )
    assert done.status_code == 200, done.text
    assert done.json()["humanOutcome"] == "abandoned"
    assert runner.discarded == [session_id]
    schema = client.get("/openapi.json").json()
    assert not any("submit" in path.casefold() for path in schema["paths"])


def _chromium_or_skip() -> None:
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:
        pytest.skip(f"Chromium is not installed: {exc}")


def test_live_screenshot_and_human_click_can_reach_submit(tmp_path):
    _chromium_or_skip()
    handler = partial(SimpleHTTPRequestHandler, directory=str(FIXTURES))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    origin = f"http://{host}:{port}"
    runner = PlaywrightRunner(enabled=True, headless=True, workers=1)
    app = create_app(token=TOKEN, runner=runner, run_browser=True)
    try:
        with TestClient(app) as client:
            created = client.post(
                "/v1/sessions",
                headers=AUTH,
                json=_profile_body(f"{origin}/job_description_apply.html"),
            )
            assert created.status_code == 201, created.text
            session_id = created.json()["sessionId"]
            started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
            assert started.status_code == 200, started.text
            assert started.json()["status"] == Status.READY_FOR_HUMAN_SUBMIT
            shot = client.get(f"/v1/sessions/{session_id}/screenshot", headers=AUTH)
            assert shot.status_code == 200
            assert shot.content.startswith(b"\xff\xd8")
            assert shot.headers["x-autofill-url-host"] == "127.0.0.1"
            assert shot.headers["x-autofill-status"] == Status.READY_FOR_HUMAN_SUBMIT

            def box():
                return runner._sessions[session_id].page.locator("#submit").bounding_box()

            rect = runner._submit(box, session_id)
            clicked = client.post(
                f"/v1/sessions/{session_id}/interact",
                headers=AUTH,
                json={
                    "action": "click",
                    "x": rect["x"] + 4,
                    "y": rect["y"] + 4,
                    "candidateId": "ref-a",
                },
            )
            assert clicked.status_code == 200, clicked.text
            names = runner._submit(
                lambda: runner._sessions[session_id].page.evaluate("() => window.__clicked"),
                session_id,
            )
            assert names == ["Apply", "Submit application"]
            finished = client.post(
                f"/v1/sessions/{session_id}/human-done",
                headers=AUTH,
                json={"candidateId": "ref-a", "outcome": "submitted"},
            )
            assert finished.status_code == 200
            assert finished.json()["humanOutcome"] == "submitted"
            again = client.get(f"/v1/sessions/{session_id}/screenshot", headers=AUTH)
            assert again.status_code == 409
    finally:
        server.shutdown()
        runner.shutdown()


def test_crashed_page_is_retryable_and_can_restart(tmp_path):
    _chromium_or_skip()
    handler = partial(SimpleHTTPRequestHandler, directory=str(FIXTURES))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    origin = f"http://{host}:{port}"
    runner = PlaywrightRunner(enabled=True, headless=True, workers=1)
    app = create_app(token=TOKEN, runner=runner, run_browser=True)
    try:
        with TestClient(app) as client:
            created = client.post(
                "/v1/sessions",
                headers=AUTH,
                json=_profile_body(f"{origin}/job_description_apply.html"),
            )
            session_id = created.json()["sessionId"]
            started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
            assert started.json()["status"] == Status.READY_FOR_HUMAN_SUBMIT

            def crash_shot() -> None:
                page = runner._sessions[session_id].page

                def boom(*_args, **_kwargs):
                    raise RuntimeError("Page.screenshot: Target crashed")

                page.screenshot = boom

            runner._submit(crash_shot, session_id)
            shot = client.get(f"/v1/sessions/{session_id}/screenshot", headers=AUTH)
            assert shot.status_code in {409, 410}
            assert shot.status_code != 500
            assert shot.headers["x-autofill-status"] == Status.FAILED_RETRYABLE
            status = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
            assert status.json()["status"] == Status.FAILED_RETRYABLE
            assert "browser_crash" in status.json()["messages"]
            restarted = client.post(
                f"/v1/sessions/{session_id}/continue",
                headers=AUTH,
                json={"candidateId": "ref-a", "humanResolved": True},
            )
            assert restarted.status_code == 200, restarted.text
            assert restarted.json()["status"] == Status.READY_FOR_HUMAN_SUBMIT
            runner._submit(lambda: runner._sessions[session_id].page.close(), session_id)
            closed = client.get(f"/v1/sessions/{session_id}/screenshot", headers=AUTH)
            assert closed.status_code == 410
            assert closed.headers["x-autofill-status"] == Status.FAILED_RETRYABLE
    finally:
        server.shutdown()
        runner.shutdown()
