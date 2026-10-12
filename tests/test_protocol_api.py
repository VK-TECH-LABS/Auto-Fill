"""Protocol 0.4.0 session API without a browser: TTL, continue, and log redaction."""

from __future__ import annotations

import json
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autofill.credentials import Credentials, SiteContext
from autofill.engine import ApplicationResult, Status
from autofill.redact import configure_stdout_logging, install_redaction, redaction_filter
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
        self.screenshots: list[str] = []
        self.interactions: list[tuple[str, str | None]] = []
        self.applied: list[tuple[str, object]] = []
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
        elif self.mode == "expired":
            status = Status.EXPIRED
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

    def screenshot(self, session_id: str):
        self.screenshots.append(session_id)
        return b"jpeg", "example.test"

    def interact(self, session_id: str, action: dict) -> bool:
        self.interactions.append((session_id, action.get("action")))
        return True

    def apply_answer(self, session_id: str, target: dict) -> dict:
        self.applied.append((session_id, target.get("value")))
        return {"filled": True}

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
    assert body["timings"]["sessionCreatedMs"] >= 1
    assert RESOLVER_TOKEN not in created.text
    assert "synthetic@example.com" not in created.text
    assert "Synthetic Candidate" not in created.text


def test_resolver_rejects_profiles_tokens_in_urls_and_oversized_refs(client):
    test_client, _runner, _app = client
    mixed = _resolver_body()
    mixed["candidateContext"] = {"candidateId": "ref-a", "personal": {"full_name": "A", "email": "a@example.com"}}
    ignored = test_client.post("/v1/sessions", headers=AUTH, json=mixed)
    assert ignored.status_code == 201, ignored.text
    assert "a@example.com" not in ignored.text
    answered = _resolver_body()
    answered["approvedAnswers"] = [{"question": "Sponsor", "answer": "No"}]
    rejected = test_client.post("/v1/sessions", headers=AUTH, json=answered)
    assert rejected.status_code == 422
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


class _ConfirmRunner:
    """Returns login, then resume, then a manual blocker. A forgotten confirmation falls back."""

    def __init__(self) -> None:
        self.flags: list[tuple[bool, bool, bool]] = []

    def run(self, request):
        self.flags.append((request.resume_uploaded, request.human_resolved, request.answers_updated))
        if not request.human_resolved:
            status = Status.LOGIN_REQUIRED
            items: list[dict] = []
        elif not request.resume_uploaded:
            status = Status.RESUME_UPLOAD_REQUIRED
            items = []
        else:
            status = Status.MANUAL_ANSWER_REQUIRED
            name = "degree" if request.answers_updated else "school"
            items = [
                {
                    "questionHash": name,
                    "selector": f"#{name}",
                    "text": name.title(),
                    "answerType": "text",
                    "options": [],
                    "kind": "QUESTION",
                    "fieldKey": name,
                    "blocking": True,
                }
            ]
        return ApplicationResult(
            status=status,
            ats="greenhouse",
            current_step=status,
            login_status="NOT_REQUIRED" if request.human_resolved else "LOGIN_REQUIRED",
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            open_items=items,
            manual_questions=[{"intent": None, "text": items[0]["text"]}] if items else [],
        )

    def apply_answer(self, session_id: str, target: dict) -> dict:
        return {"filled": True}

    def discard(self, session_id: str) -> None:
        return None

    def shutdown(self) -> None:
        return None


def test_resume_and_login_confirmations_survive_answer_pause_and_continue():
    runner = _ConfirmRunner()
    app = create_app(
        token=TOKEN,
        runner=runner,
        run_browser=False,
        session_ttl_seconds=1800,
        reaper_interval_seconds=60,
    )
    with TestClient(app) as test_client:
        created = test_client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
        assert created.status_code == 201, created.text
        session_id = created.json()["sessionId"]
        started = test_client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
        assert started.status_code == 200, started.text
        assert started.json()["status"] == Status.LOGIN_REQUIRED
        signed_in = test_client.post(
            f"/v1/sessions/{session_id}/continue",
            headers=AUTH,
            json={"humanResolved": True},
        )
        assert signed_in.status_code == 200, signed_in.text
        assert signed_in.json()["status"] == Status.RESUME_UPLOAD_REQUIRED
        confirmed = test_client.post(
            f"/v1/sessions/{session_id}/continue",
            headers=AUTH,
            json={"resumeUploaded": True},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == Status.MANUAL_ANSWER_REQUIRED
        held = app.state.store.get(session_id)
        assert held is not None
        assert held.resume_uploaded is True
        assert held.human_resolved is True
        paused = test_client.post(f"/v1/sessions/{session_id}/pause", headers=AUTH)
        assert paused.status_code == 200
        released = test_client.post(f"/v1/sessions/{session_id}/resume", headers=AUTH)
        assert released.status_code == 200
        assert released.json()["status"] == Status.MANUAL_ANSWER_REQUIRED
        assert held.resume_uploaded is True
        assert held.human_resolved is True
        answered = test_client.post(
            f"/v1/sessions/{session_id}/answer",
            headers=AUTH,
            json={"questionHash": "school", "value": "Example University"},
        )
        assert answered.status_code == 200, answered.text
        assert answered.json()["filled"] is True
        status = test_client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        assert status.status_code == 200, status.text
        assert status.json()["status"] != Status.RESUME_UPLOAD_REQUIRED
        assert status.json()["status"] != Status.LOGIN_REQUIRED
        assert status.json()["status"] == Status.MANUAL_ANSWER_REQUIRED
        assert held.resume_uploaded is True
        assert held.human_resolved is True
        assert runner.flags == [
            (False, False, False),
            (False, True, False),
            (True, True, False),
            (True, True, True),
        ]


class _ScanRunner:
    """Answers update the blocker in place. A full fill is the start call only."""

    def __init__(self) -> None:
        self.runs = 0
        self.advances: list[tuple[bool, bool]] = []

    def run(self, request):
        self.runs += 1
        if not request.resume_uploaded:
            status = Status.RESUME_UPLOAD_REQUIRED
            items: list[dict] = []
        else:
            status = Status.MANUAL_ANSWER_REQUIRED
            items = [
                {
                    "questionHash": "school",
                    "selector": "#school",
                    "text": "School",
                    "answerType": "text",
                    "options": [],
                    "kind": "QUESTION",
                    "fieldKey": "school",
                    "blocking": True,
                }
            ]
        return ApplicationResult(
            status=status,
            ats="greenhouse",
            current_step=status,
            login_status="NOT_REQUIRED",
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            open_items=items,
            manual_questions=[{"intent": None, "text": "School"}] if items else [],
        )

    def advance_after_answer(self, request):
        self.advances.append((request.resume_uploaded, request.human_resolved))
        item = {
            "questionHash": "degree",
            "selector": "#degree",
            "text": "Degree",
            "answerType": "text",
            "options": [],
            "kind": "QUESTION",
            "fieldKey": "degree",
            "blocking": True,
        }
        return ApplicationResult(
            status=Status.MANUAL_ANSWER_REQUIRED,
            ats="greenhouse",
            current_step=Status.MANUAL_ANSWER_REQUIRED,
            login_status="NOT_REQUIRED",
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            open_items=[item],
            manual_questions=[{"intent": None, "text": "Degree"}],
            progress={
                "stage": "QUESTIONS",
                "requiredTotal": 2,
                "requiredDone": 1,
                "percent": 50,
                "currentLabel": "Questions",
            },
            blocking={
                "kind": "QUESTION",
                "questionId": None,
                "questionHash": "degree",
                "text": "Degree",
                "fieldKey": "degree",
                "answerType": "text",
                "options": [],
                "required": True,
            },
        )

    def apply_answer(self, session_id: str, target: dict) -> dict:
        return {"filled": True}

    def discard(self, session_id: str) -> None:
        return None

    def shutdown(self) -> None:
        return None


def test_answer_scans_the_next_blocker_without_another_fill():
    runner = _ScanRunner()
    app = create_app(
        token=TOKEN,
        runner=runner,
        run_browser=False,
        session_ttl_seconds=1800,
        reaper_interval_seconds=60,
    )
    with TestClient(app) as test_client:
        created = test_client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
        assert created.status_code == 201, created.text
        session_id = created.json()["sessionId"]
        started = test_client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
        assert started.json()["status"] == Status.RESUME_UPLOAD_REQUIRED
        confirmed = test_client.post(
            f"/v1/sessions/{session_id}/continue",
            headers=AUTH,
            json={"resumeUploaded": True},
        )
        assert confirmed.status_code == 200, confirmed.text
        assert confirmed.json()["status"] == Status.MANUAL_ANSWER_REQUIRED
        runs_after_confirm = runner.runs
        answered = test_client.post(
            f"/v1/sessions/{session_id}/answer",
            headers=AUTH,
            json={"questionHash": "school", "value": "Example University"},
        )
        assert answered.status_code == 200, answered.text
        body = answered.json()
        assert body["filled"] is True
        assert isinstance(body["ms"], int)
        assert body["ms"] >= 0
        assert runner.runs == runs_after_confirm
        assert runner.advances == [(True, False)]
        status = test_client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        assert status.json()["status"] == Status.MANUAL_ANSWER_REQUIRED
        assert status.json()["status"] != Status.RESUME_UPLOAD_REQUIRED
        assert status.json()["blocking"]["text"] == "Degree"
        held = app.state.store.get(session_id)
        assert held is not None
        assert held.resume_uploaded is True


def test_status_returns_410_when_the_ttl_clock_passes(monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr("autofill.service.app.monotonic", lambda: clock["now"])
    runner = ScriptedRunner()
    app = create_app(
        token=TOKEN,
        runner=runner,
        run_browser=False,
        session_ttl_seconds=1800,
        reaper_interval_seconds=3600,
    )
    with TestClient(app) as test_client:
        created = test_client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
        assert created.status_code == 201, created.text
        session_id = created.json()["sessionId"]
        held = app.state.store.get(session_id)
        assert held is not None
        assert held.expires_at == 2800.0
        live = test_client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        assert live.status_code == 200
        assert live.json()["status"] == "CREATED"
        started = test_client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
        assert started.status_code == 200, started.text
        clock["now"] = 2799.0
        still = test_client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        assert still.status_code == 200
        clock["now"] = 2800.0
        expired = test_client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        assert expired.status_code == 410
        assert held.status == Status.EXPIRED
        assert held.resolver_token == ""
        assert session_id in runner.discarded
        assert runner.calls == [(session_id, False, False)]
        pause = test_client.post(f"/v1/sessions/{session_id}/pause", headers=AUTH)
        assert pause.status_code == 410
        assert held.paused is False


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
    assert status.status_code == 410
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


def test_stdout_logs_are_structured_and_redacted(capsys: pytest.CaptureFixture[str]):
    logger = logging.getLogger("autofill")
    previous_level = logger.level
    previous_propagate = logger.propagate
    previous_handlers = list(logger.handlers)
    secret = "synthetic-stdout-secret-0123456789"
    filt = redaction_filter()
    filt.add(secret)
    try:
        configure_stdout_logging()
        for handler in logger.handlers:
            handler.setStream(sys.stdout)
        logging.getLogger("autofill.stepfill").info(
            "session=%s step=%s field_keys=%s intents=%s result=%s latency_ms=%s %s",
            "sess-log",
            "questions",
            "email",
            "US_WORK_AUTHORIZATION",
            "ok",
            9,
            "river.example@example.com",
        )
        logging.getLogger("autofill.resolver").info("bearer %s", f"Bearer {secret}")
        for handler in logger.handlers:
            handler.flush()
        captured = capsys.readouterr().out
    finally:
        filt.discard(secret)
        logger.handlers.clear()
        for handler in previous_handlers:
            logger.addHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
    lines = [line for line in captured.splitlines() if line.startswith("{")]
    assert len(lines) >= 2
    payloads = [json.loads(line) for line in lines]
    assert payloads[0]["level"] == "INFO"
    assert payloads[0]["logger"] == "autofill.stepfill"
    assert "sess-log" in payloads[0]["message"]
    assert "questions" in payloads[0]["message"]
    assert "US_WORK_AUTHORIZATION" in payloads[0]["message"]
    assert "latency_ms=9" in payloads[0]["message"]
    assert "river.example@example.com" not in captured
    assert secret not in captured
    assert "Bearer [redacted]" in captured


_SECRET = "synthetic-password-expired-session"


def _follow_ups(test_client: TestClient, session_id: str) -> dict[str, int]:
    status = test_client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
    shot = test_client.get(f"/v1/sessions/{session_id}/screenshot", headers=AUTH)
    interact = test_client.post(
        f"/v1/sessions/{session_id}/interact",
        headers=AUTH,
        json={"action": "key", "key": "Escape"},
    )
    pause = test_client.post(f"/v1/sessions/{session_id}/pause", headers=AUTH)
    resume = test_client.post(f"/v1/sessions/{session_id}/resume", headers=AUTH)
    answer = test_client.post(
        f"/v1/sessions/{session_id}/answer",
        headers=AUTH,
        json={"questionHash": "desk", "value": "Window"},
    )
    continued = test_client.post(
        f"/v1/sessions/{session_id}/continue",
        headers=AUTH,
        json={"resumeUploaded": True},
    )
    credentials = test_client.post(
        f"/v1/sessions/{session_id}/credentials",
        headers=AUTH,
        json={"email": "avery.example@example.com", "password": _SECRET},
    )
    assert _SECRET not in credentials.text
    return {
        "status": status.status_code,
        "screenshot": shot.status_code,
        "interact": interact.status_code,
        "pause": pause.status_code,
        "resume": resume.status_code,
        "answer": answer.status_code,
        "continue": continued.status_code,
        "credentials": credentials.status_code,
    }


def test_due_session_rejects_every_follow_up_without_side_effects(client):
    test_client, runner, app = client
    created = test_client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
    session_id = created.json()["sessionId"]
    held = app.state.store.get(session_id)
    assert held is not None
    held.credentials.put(
        held.candidate_id,
        "boards.greenhouse.io",
        Credentials(site="boards.greenhouse.io", email="avery.example@example.com", password=_SECRET),
    )
    held.open_items = [
        {
            "questionHash": "desk",
            "selector": "#desk",
            "options": ["Window", "Aisle"],
            "answerType": "single_select",
            "text": "Preferred desk?",
            "kind": "QUESTION",
        }
    ]
    held.screenshot_version = 4
    held.paused = False
    held.expires_at = time.monotonic() - 1
    codes = _follow_ups(test_client, session_id)
    assert codes == {name: 410 for name in codes}
    assert held.status == Status.EXPIRED
    assert held.paused is False
    assert held.pause_gate.paused is False
    assert held.screenshot_version == 4
    assert held.held_answers == []
    assert held.resolver_token == ""
    site = SiteContext(
        url="https://boards.greenhouse.io/example/jobs/1",
        domain="boards.greenhouse.io",
        candidate_id=held.candidate_id,
    )
    assert held.credentials.get_credentials(site, held.candidate_id) is None
    assert runner.calls == []
    assert runner.screenshots == []
    assert runner.interactions == []
    assert runner.applied == []
    assert session_id in runner.discarded
    held.paused = True
    held.pause_gate.pause()
    resume = test_client.post(f"/v1/sessions/{session_id}/resume", headers=AUTH)
    assert resume.status_code == 410
    assert held.paused is True
    assert held.pause_gate.paused is True
    missing = test_client.get("/v1/sessions/missing-session/status", headers=AUTH)
    assert missing.status_code == 404


def test_ttl_reaper_closes_the_browser_before_the_next_call():
    runner = ScriptedRunner()
    app = create_app(
        token=TOKEN,
        runner=runner,
        run_browser=False,
        session_ttl_seconds=1800,
        reaper_interval_seconds=0.05,
    )
    with TestClient(app) as test_client:
        created = test_client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
        session_id = created.json()["sessionId"]
        held = app.state.store.get(session_id)
        assert held is not None
        held.expires_at = time.monotonic() - 1
        deadline = time.monotonic() + 2
        while session_id not in runner.discarded and time.monotonic() < deadline:
            time.sleep(0.02)
        assert session_id in runner.discarded
        assert held.status == Status.EXPIRED
        assert held.resolver_token == ""
        closed = list(runner.discarded)
        status = test_client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        assert status.status_code == 410
        assert runner.discarded == closed
        assert runner.calls == []
        assert runner.screenshots == []


def test_resolver_expiry_closes_the_context_and_rejects_follow_ups(client):
    test_client, runner, app = client
    runner.mode = "expired"
    created = test_client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
    session_id = created.json()["sessionId"]
    started = test_client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
    assert started.status_code == 200, started.text
    assert started.json()["status"] == Status.EXPIRED
    assert RESOLVER_TOKEN not in started.text
    held = app.state.store.get(session_id)
    assert held is not None
    assert held.resolver_token == ""
    assert session_id in runner.discarded
    calls = len(runner.calls)
    codes = _follow_ups(test_client, session_id)
    assert codes == {name: 410 for name in codes}
    assert len(runner.calls) == calls
    assert runner.screenshots == []
    assert runner.interactions == []
    assert runner.applied == []
    assert held.paused is False
    assert held.held_answers == []


def test_image_still_installs_chromium():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert "python -m playwright install --with-deps chromium" in dockerfile
    runner = Path("src/autofill/service/runner.py").read_text(encoding="utf-8")
    assert "launch_persistent_context" not in runner
    assert "user_data_dir" not in runner
    assert "user-data-dir" not in runner
