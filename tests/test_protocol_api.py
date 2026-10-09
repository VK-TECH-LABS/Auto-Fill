"""Protocol 0.4.0 session API without a browser: TTL, continue, and log redaction."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autofill.engine import ApplicationResult, Status
from autofill.redact import install_redaction, redaction_filter
from autofill.service.app import create_app

TOKEN = "synthetic-service-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
RESOLVER_TOKEN = "synthetic-resolver-token-0123456789abcdef"
def _future() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def _resolver_body(url: str = "https://resolver.example.test/v1/resolve") -> dict:
    return {
        "protocolVersion": "0.4.0",
        "candidateRef": "ref-a",
        "jobRef": "job-a",
        "jobContext": {
            "applicationUrl": "https://boards.greenhouse.io/example/jobs/1",
            "title": "Example Engineer",
            "company": "Example Labs",
        },
        "resolver": {"url": url, "token": RESOLVER_TOKEN, "expiresAt": _future()},
    }


class ScriptedRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bool, bool]] = []
        self.discarded: list[str] = []
        self.mode = "manual"

    def run(self, request):
        self.calls.append((request.session_id, request.resume_uploaded, request.answers_updated))
        assert request.resolver is not None
        assert request.resolver.token == RESOLVER_TOKEN
        assert RESOLVER_TOKEN not in request.resolver.url
        if self.mode == "manual":
            status = Status.MANUAL_ANSWER_REQUIRED
            questions = [{"intent": "US_WORK_AUTHORIZATION", "text": "Are you legally authorized to work?"}]
        elif self.mode == "resume":
            status = Status.RESUME_UPLOAD_REQUIRED
            questions = []
        else:
            status = Status.READY_FOR_HUMAN_SUBMIT
            questions = []
        return ApplicationResult(
            status=status,
            ats="greenhouse",
            current_step=status,
            login_status="NOT_REQUIRED",
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            manual_questions=questions,
            timings={"browserReadyMs": 3, "firstFormInspectedMs": 7, "sessionCreatedMs": request.session_created_ms},
        )

    def discard(self, session_id: str) -> None:
        self.discarded.append(session_id)

    def shutdown(self) -> None:
        return None


@pytest.fixture
def client():
    runner = ScriptedRunner()
    app = create_app(
        token=TOKEN,
        runner=runner,
        run_browser=False,
        session_ttl_seconds=1800,
        reaper_interval_seconds=60,
    )
    with TestClient(app) as test_client:
        yield test_client, runner, app


def test_resolver_create_does_not_echo_the_token(client):
    test_client, _runner, _app = client
    created = test_client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["status"] == "CREATED"
    assert body["stopped_before_submit"] is True
    assert body["protocolVersion"] == "0.4.0"
    assert "timings" in body
    assert RESOLVER_TOKEN not in created.text
    assert "synthetic@example.com" not in created.text
    assert "Synthetic Candidate" not in created.text


def test_resolver_rejects_profiles_tokens_in_urls_and_oversized_refs(client):
    test_client, _runner, _app = client
    mixed = _resolver_body()
    mixed["candidateContext"] = {"candidateId": "ref-a", "personal": {"full_name": "A", "email": "a@example.com"}}
    rejected = test_client.post("/v1/sessions", headers=AUTH, json=mixed)
    assert rejected.status_code == 422
    assert "a@example.com" not in rejected.text
    leaked = _resolver_body(url=f"https://resolver.example.test/resolve?token={RESOLVER_TOKEN}")
    refused = test_client.post("/v1/sessions", headers=AUTH, json=leaked)
    assert refused.status_code == 422
    assert RESOLVER_TOKEN not in refused.text
    short = _resolver_body()
    short["resolver"]["token"] = "too-short"
    assert test_client.post("/v1/sessions", headers=AUTH, json=short).status_code == 422


def test_continue_requeries_then_resume(client):
    test_client, runner, _app = client
    created = test_client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
    session_id = created.json()["sessionId"]
    started = test_client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
    assert started.status_code == 200, started.text
    assert started.json()["status"] == Status.MANUAL_ANSWER_REQUIRED
    assert started.json()["manualQuestions"] == [
        {"intent": "US_WORK_AUTHORIZATION", "text": "Are you legally authorized to work?"}
    ]
    assert started.json()["timings"]["browserReadyMs"] == 3
    early = test_client.post(
        f"/v1/sessions/{session_id}/continue",
        headers=AUTH,
        json={"answersUpdated": False},
    )
    assert early.status_code == 409
    runner.mode = "resume"
    continued = test_client.post(
        f"/v1/sessions/{session_id}/continue",
        headers=AUTH,
        json={"candidateRef": "ref-a", "answersUpdated": True},
    )
    assert continued.status_code == 200, continued.text
    assert continued.json()["status"] == Status.RESUME_UPLOAD_REQUIRED
    assert runner.calls[-1][2] is True
    runner.mode = "ready"
    done = test_client.post(
        f"/v1/sessions/{session_id}/continue",
        headers=AUTH,
        json={"candidateRef": "ref-a", "resumeUploaded": True},
    )
    assert done.json()["status"] == Status.READY_FOR_HUMAN_SUBMIT
    mismatch = test_client.post(
        f"/v1/sessions/{session_id}/continue",
        headers=AUTH,
        json={"candidateRef": "ref-other", "resumeUploaded": True},
    )
    assert mismatch.status_code == 409


def test_ttl_reaper_wipes_the_resolver_token(client):
    _test_client, _runner, app = client
    store = app.state.store
    created = _test_client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
    session_id = created.json()["sessionId"]
    held = store.get(session_id)
    assert held is not None
    assert held.resolver_token == RESOLVER_TOKEN
    held.expires_at = time.monotonic() - 1
    expired = store.reap()
    assert session_id in expired
    assert held.status == Status.EXPIRED
    assert held.resolver_token == ""
    again = _test_client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
    assert again.status_code == 409
    status = _test_client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
    assert status.json()["status"] == Status.EXPIRED
    assert RESOLVER_TOKEN not in status.text


def test_log_redaction_keeps_keys_and_drops_values(caplog: pytest.LogCaptureFixture):
    filt = install_redaction()
    filt.add(RESOLVER_TOKEN)
    caplog.set_level(logging.INFO)
    logger = logging.getLogger("autofill.service")
    logger.info(
        "session=%s step=%s adapter=%s field_keys=%s intents=%s requested=%s returned=%s result=%s latency_ms=%s",
        "sess-1",
        "questions",
        "generic",
        "firstName,email",
        "US_WORK_AUTHORIZATION",
        3,
        2,
        "ok",
        12,
    )
    logger.info("leaked river.example@example.com 555-010-0199")
    logger.info("bearer %s", f"Bearer {RESOLVER_TOKEN}")
    text = caplog.text
    assert "sess-1" in text
    assert "questions" in text
    assert "generic" in text
    assert "firstName" in text
    assert "US_WORK_AUTHORIZATION" in text
    assert "latency_ms=12" in text or "latency_ms" in text
    assert "river.example@example.com" not in text
    assert "555-010-0199" not in text
    assert RESOLVER_TOKEN not in text
    redaction_filter().discard(RESOLVER_TOKEN)


def test_image_still_installs_chromium():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "python -m playwright install --with-deps chromium" in dockerfile
    runner = Path("src/autofill/service/runner.py").read_text(encoding="utf-8")
    assert "launch_persistent_context" not in runner
    assert "user_data_dir" not in runner
    assert "user-data-dir" not in runner
