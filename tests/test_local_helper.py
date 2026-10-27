"""Loopback helper. Final Submit stays off, and idle detach leaves Chrome up."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from autofill.dom_ui import public_blocking
from autofill.engine import ApplicationResult, Status
from autofill.local_attach import (
    ENV_FLAG,
    FeatureDisabled,
    LocalAttachError,
    LoopbackRequired,
    apply_visible_answer,
    fetch_version,
    find_chrome,
    start_fixture_server,
)
from autofill.local_helper import (
    AnswerRejected,
    LocalHelper,
    LocalJob,
    _prepared_target,
    main,
    pending_question,
    question_view,
    serve,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tools" / "local-cdp-poc" / "fixture" / "application.html"
PROFILE = ROOT / "examples" / "profile.example.json"


class _FakeSession:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.detached = 0
        self.stopped = 0
        self.owns_chrome = False
        self.tab = None

    def fill(self, url: str, profile: object) -> tuple[ApplicationResult, bool]:
        self.started.set()
        assert self.release.wait(timeout=5)
        result = ApplicationResult(
            status=Status.READY_FOR_HUMAN_SUBMIT,
            ats=None,
            current_step="done",
            login_status="NOT_REQUIRED",
            fields_detected=3,
            fields_filled=3,
            stopped_before_submit=True,
            submit_controls=["Submit application"],
        )
        return result, False

    def detach(self) -> None:
        self.detached += 1

    def stop_launched_chrome(self) -> None:
        self.stopped += 1

    def browser_running(self) -> bool:
        return True


def _request(port: int, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            body = json.loads(response.read().decode("utf-8"))
            return response.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_flag_off_refuses_to_bind(monkeypatch):
    monkeypatch.delenv(ENV_FLAG, raising=False)
    with pytest.raises(FeatureDisabled):
        serve(port=0)
    assert main() == 2


class _ReportedSubmitSession(_FakeSession):
    def fill(self, url: str, profile: object) -> tuple[ApplicationResult, bool]:
        self.started.set()
        assert self.release.wait(timeout=5)
        raise LocalAttachError("The page reported a submit. The local driver does not submit applications.")


def test_helper_refuses_a_public_bind(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    with pytest.raises(LoopbackRequired):
        serve(host="0.0.0.0", port=0)
    with pytest.raises(LoopbackRequired):
        serve(host="localhost", port=0)


def test_idle_detach_does_not_stop_chrome_or_submit(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    fake = _FakeSession()
    helper = LocalHelper(session=fake, idle_detach_seconds=0)  # type: ignore[arg-type]
    server = serve(port=0, helper=helper)
    port = int(server.server_address[1])
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    try:
        health, health_body = _request(port, "GET", "/health")
        assert health == 200
        assert health_body["submit"] == "human"
        status, created = _request(
            port,
            "POST",
            "/v1/sessions",
            {"applicationUrl": "https://boards.greenhouse.io/example/jobs/1", "profile": profile},
        )
        assert status == 202
        assert created["stopped_before_submit"] is True
        assert fake.started.wait(timeout=5)
        busy, busy_body = _request(
            port,
            "POST",
            "/v1/sessions",
            {"applicationUrl": "https://boards.greenhouse.io/example/jobs/1", "profile": profile},
        )
        assert busy == 409
        assert busy_body["stopped_before_submit"] is True
        fake.release.set()
        session_id = created["sessionId"]
        body: dict = {}
        for _ in range(50):
            code, body = _request(port, "GET", f"/v1/sessions/{session_id}")
            if body.get("detached"):
                break
            threading.Event().wait(0.05)
        assert code == 200
        assert body["status"] == "READY_FOR_HUMAN_SUBMIT"
        assert body["stopped_before_submit"] is True
        assert body["submitted"] is False
        assert body["fill"]["submit_controls"] == ["Submit application"]
        assert body["fill"]["fields_filled"] == 3
        assert body["browserRunning"] is True
        assert fake.detached >= 1
        assert fake.stopped == 0
    finally:
        helper.close()
        server.shutdown()


def test_reported_submit_is_failed_and_not_stopped_before_submit(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    fake = _ReportedSubmitSession()
    helper = LocalHelper(session=fake, idle_detach_seconds=0)  # type: ignore[arg-type]
    server = serve(port=0, helper=helper)
    port = int(server.server_address[1])
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    try:
        status, created = _request(
            port,
            "POST",
            "/v1/sessions",
            {"applicationUrl": "https://boards.greenhouse.io/example/jobs/1", "profile": profile},
        )
        assert status == 202
        assert created["status"] == "queued"
        assert created["stopped_before_submit"] is True
        assert created["submitted"] is None
        assert fake.started.wait(timeout=5)
        fake.release.set()
        session_id = created["sessionId"]
        body: dict = {}
        for _ in range(50):
            code, body = _request(port, "GET", f"/v1/sessions/{session_id}")
            if body.get("detached"):
                break
            threading.Event().wait(0.05)
        assert code == 200
        assert body["status"] == "FAILED"
        assert body["submitted"] is True
        assert body["stopped_before_submit"] is False
        assert body["status"] not in {"SUBMITTED", "FILLED"}
        assert body["error"] == "LocalAttachError"
        assert "fill" not in body
        assert body["detached"] is True
        assert fake.stopped == 0
    finally:
        helper.close()
        server.shutdown()


@pytest.mark.skipif(os.environ.get("T172_RUN_CHROME_POC") != "1", reason="Set T172_RUN_CHROME_POC=1 to launch Chrome")
def test_dedicated_helper_fills_without_clicking_submit(monkeypatch, tmp_path):
    monkeypatch.setenv(ENV_FLAG, "1")
    try:
        executable = find_chrome(None)
    except Exception:
        pytest.skip("Google Chrome is not installed")
    if "google-chrome" not in executable and "chrome" not in Path(executable).name:
        pytest.skip("branded Chrome is not installed")
    fixture = start_fixture_server(FIXTURE.read_bytes())
    form_port = int(fixture.server_address[1])
    form_url = f"http://127.0.0.1:{form_port}/t172-local-tab.html"
    server = serve(
        port=0,
        user_data_dir=tmp_path / "apply-profile",
        headless=True,
        idle_detach_seconds=0,
        executable=executable,
    )
    port = int(server.server_address[1])
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    try:
        status, created = _request(port, "POST", "/v1/sessions", {"applicationUrl": form_url, "profile": profile})
        assert status == 202
        session_id = created["sessionId"]
        body: dict = {}
        for _ in range(300):
            _code, body = _request(port, "GET", f"/v1/sessions/{session_id}")
            if body.get("detached") or body.get("status") == "FAILED":
                break
            threading.Event().wait(0.15)
        assert body["status"] == "READY_FOR_HUMAN_SUBMIT"
        assert body["stopped_before_submit"] is True
        assert body["submitted"] is False
        assert body["fill"]["fields_filled"] >= 1
        assert "Submit application" in body["fill"]["submit_controls"]
        assert body["browserRunning"] is True
        assert body["ownsBrowser"] is True
        debug_port = server.helper._session.debug_port
        assert fetch_version(debug_port) is not None
    finally:
        server.helper.stop_launched_chrome()
        server.helper.close()
        server.shutdown()
        fixture.shutdown()


_COLOR = {
    "intent": None,
    "text": "What is your favorite color?",
    "blocking": True,
    "questionId": "q-color",
    "questionHash": "q-color",
    "fieldKey": None,
    "answerType": "single_select",
    "options": ["Red", "Blue"],
    "required": True,
    "selector": "#color",
    "kind": "QUESTION",
}
_VISA = {
    "intent": None,
    "text": "Will you now or in the future require sponsorship?",
    "blocking": True,
    "questionId": "q-visa",
    "questionHash": "q-visa",
    "fieldKey": None,
    "answerType": "single_select",
    "options": ["Sponsorship yes", "Sponsorship no"],
    "required": True,
    "selector": "#visa",
    "kind": "QUESTION",
}
_NOTE = {
    "intent": None,
    "text": "Anything else we should know?",
    "blocking": True,
    "questionId": "q-note",
    "questionHash": "q-note",
    "fieldKey": None,
    "answerType": "text",
    "options": [],
    "required": True,
    "selector": "#note",
    "kind": "QUESTION",
}


def _manual_result(item: dict) -> ApplicationResult:
    return ApplicationResult(
        status=Status.MANUAL_ANSWER_REQUIRED,
        ats=None,
        current_step="questions",
        login_status="NOT_REQUIRED",
        fields_detected=2,
        fields_filled=1,
        stopped_before_submit=True,
        submit_controls=["Submit application"],
        blocking=public_blocking(item),
        open_items=[dict(item)],
        manual_questions=[{"intent": None, "text": item["text"]}],
    )


class _AnswerSession(_FakeSession):
    """Fill stops on a question. Answers are recorded and never submitted."""

    def __init__(self, *, verify: bool) -> None:
        super().__init__()
        self.verify = verify
        self.applied: list[dict] = []
        self._step = 0

    def fill(self, url: str, profile: object) -> tuple[ApplicationResult, bool]:
        self.started.set()
        assert self.release.wait(timeout=5)
        item = _COLOR if self.verify else _NOTE
        return _manual_result(item), False

    def apply_answer(self, url: str, profile: object, target: dict) -> dict:
        self.applied.append(
            {
                "selector": target.get("selector"),
                "value": target.get("value"),
                "check": bool(target.get("check")),
            }
        )
        if not self.verify:
            return {"verified": False, "filled": False, "reason": "not_committed"}
        self._step += 1
        if self._step == 1:
            return {
                "verified": True,
                "filled": True,
                "status": Status.MANUAL_ANSWER_REQUIRED,
                "open_items": [dict(_VISA)],
                "blocking": public_blocking(_VISA),
            }
        return {
            "verified": True,
            "filled": True,
            "status": Status.READY_FOR_HUMAN_SUBMIT,
            "open_items": [],
            "blocking": None,
        }


def _wait_status(port: int, session_id: str, predicate) -> dict:
    body: dict = {}
    for _ in range(50):
        _code, body = _request(port, "GET", f"/v1/sessions/{session_id}")
        if predicate(body):
            return body
        threading.Event().wait(0.05)
    return body


def test_question_view_uses_the_stable_shape():
    view = question_view(
        {
            "questionId": "abc",
            "text": "Favorite color?",
            "answerType": "single_select",
            "options": ["Red", "Blue"],
            "kind": "QUESTION",
            "selector": "#color",
            "fieldKey": "color",
        }
    )
    assert view == {
        "id": "abc",
        "prompt": "Favorite color?",
        "type": "single_select",
        "options": ["Red", "Blue"],
    }
    assert question_view({"kind": "LOGIN", "text": "Sign in", "questionId": "login", "answerType": "text"}) is None
    phone = question_view(
        {"fieldKey": "phone", "text": "Phone", "answerType": "text", "kind": "MISSING_PROFILE_VALUE"}
    )
    assert phone == {"id": "phone", "prompt": "Phone", "type": "text", "options": []}
    assert pending_question(Status.READY_FOR_HUMAN_SUBMIT, public_blocking(_COLOR), [_COLOR]) is None


def test_consent_without_confirm_is_not_verified():
    job = LocalJob("s", "https://boards.greenhouse.io/example/jobs/1")
    job.result = {"status": Status.MANUAL_ANSWER_REQUIRED, "stopped_before_submit": True}
    job.question = {
        "id": "c1",
        "prompt": "I agree to the terms",
        "type": "boolean",
        "options": [],
    }
    item = {
        "kind": "CONSENT",
        "questionId": "c1",
        "questionHash": "c1",
        "text": "I agree to the terms",
        "selector": "#agree",
        "options": [],
        "answerType": "boolean",
    }
    with pytest.raises(AnswerRejected) as caught:
        _prepared_target(job, item, {"id": "c1", "value": "confirm"})
    assert caught.value.status == 422
    assert caught.value.body["verified"] is False
    assert caught.value.body["filled"] is False
    assert caught.value.body["options"] == ["confirm"]
    assert "selector" not in json.dumps(caught.value.body)
    prepared = _prepared_target(job, item, {"id": "c1", "value": "confirm", "confirm": True})
    assert prepared["check"] is True
    assert prepared["selector"] == "#agree"


def test_apply_visible_answer_verified_follows_the_commit(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")

    class _Page:
        def __init__(self, submitted: bool) -> None:
            self.submitted = submitted

        def evaluate(self, script: str):
            if "__submitted" in str(script):
                return self.submitted
            return None

    monkeypatch.setattr("autofill.stepfill.is_location_answer", lambda page, target: False)
    monkeypatch.setattr(
        "autofill.dom_ui.commit_visible",
        lambda page, target: {"filled": True},
    )
    monkeypatch.setattr("autofill.dom_ui.control_committed", lambda page, selector: True)
    report = apply_visible_answer(_Page(False), {"selector": "#color", "value": "Blue", "answerType": "text"})
    assert report["verified"] is True
    assert report["filled"] is True
    assert "Blue" not in json.dumps(report)

    monkeypatch.setattr(
        "autofill.dom_ui.commit_visible",
        lambda page, target: {"filled": False, "reason": "not_committed"},
    )
    missed = apply_visible_answer(_Page(False), {"selector": "#color", "value": "Blue", "answerType": "text"})
    assert missed["verified"] is False
    assert missed["filled"] is False
    assert missed["reason"] == "not_committed"

    monkeypatch.setattr("autofill.dom_ui.commit_visible", lambda page, target: {"filled": True})
    monkeypatch.setattr("autofill.dom_ui.control_committed", lambda page, selector: False)
    unstuck = apply_visible_answer(_Page(False), {"selector": "#color", "value": "Blue", "answerType": "text"})
    assert unstuck["verified"] is False

    with pytest.raises(LocalAttachError, match="reported a submit"):
        apply_visible_answer(_Page(True), {"selector": "#color", "value": "Blue", "answerType": "text"})


def test_status_question_and_verified_answer(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    fake = _AnswerSession(verify=True)
    helper = LocalHelper(session=fake, idle_detach_seconds=0)  # type: ignore[arg-type]
    server = serve(port=0, helper=helper)
    port = int(server.server_address[1])
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    email = profile["personal"]["email"]
    try:
        status, created = _request(
            port,
            "POST",
            "/v1/sessions",
            {"applicationUrl": "https://boards.greenhouse.io/example/jobs/1", "profile": profile},
        )
        assert status == 202
        assert "question" not in created
        assert fake.started.wait(timeout=5)
        session_id = created["sessionId"]
        busy, busy_body = _request(
            port,
            "POST",
            f"/v1/sessions/{session_id}/answer",
            {"id": "q-color", "value": "Blue"},
        )
        assert busy == 409
        assert busy_body["verified"] is False
        assert busy_body["stopped_before_submit"] is True
        assert fake.applied == []
        fake.release.set()
        shown = _wait_status(
            port,
            session_id,
            lambda body: body.get("status") == "MANUAL_ANSWER_REQUIRED" and "question" in body,
        )
        assert shown["question"] == {
            "id": "q-color",
            "prompt": "What is your favorite color?",
            "type": "single_select",
            "options": ["Red", "Blue"],
        }
        assert shown["stopped_before_submit"] is True
        assert shown["submitted"] is False
        assert "selector" not in json.dumps(shown["question"])
        assert email not in json.dumps(shown)
        assert fake.detached == 0
        missing, missing_body = _request(
            port,
            "POST",
            "/v1/sessions/missing/answer",
            {"id": "q-color", "value": "Blue"},
        )
        assert missing == 404
        assert missing_body["verified"] is False
        stale, stale_body = _request(
            port,
            "POST",
            f"/v1/sessions/{session_id}/answer",
            {"id": "nope", "value": "Blue"},
        )
        assert stale == 409
        assert stale_body["verified"] is False
        assert stale_body["reason"] == "stale_blocker"
        assert stale_body["question"]["id"] == "q-color"
        rejected, rejected_body = _request(
            port,
            "POST",
            f"/v1/sessions/{session_id}/answer",
            {"id": "q-color", "value": "purple"},
        )
        assert rejected == 422
        assert rejected_body["verified"] is False
        assert rejected_body["filled"] is False
        assert rejected_body["reason"] == "needs_option"
        assert rejected_body["options"] == ["Red", "Blue"]
        assert "purple" not in json.dumps(rejected_body)
        assert fake.applied == []
        first, first_body = _request(
            port,
            "POST",
            f"/v1/sessions/{session_id}/answer",
            {"id": "q-color", "value": "Blue"},
        )
        assert first == 200
        assert first_body["verified"] is True
        assert first_body["filled"] is True
        assert first_body["stopped_before_submit"] is True
        assert first_body["submitted"] is False
        assert first_body["status"] == "MANUAL_ANSWER_REQUIRED"
        assert first_body["question"]["id"] == "q-visa"
        assert first_body["question"]["type"] == "single_select"
        assert "Blue" not in json.dumps(first_body)
        assert email not in json.dumps(first_body)
        assert fake.applied == [{"selector": "#color", "value": "Blue", "check": False}]
        second, second_body = _request(
            port,
            "POST",
            f"/v1/sessions/{session_id}/answer",
            {"questionId": "q-visa", "answer": "Sponsorship no"},
        )
        assert second == 200
        assert second_body["verified"] is True
        assert second_body["filled"] is True
        assert second_body["status"] == "READY_FOR_HUMAN_SUBMIT"
        assert "question" not in second_body
        assert second_body["submitted"] is False
        assert "Sponsorship no" not in json.dumps(second_body)
        assert fake.applied[-1]["selector"] == "#visa"
        assert fake.stopped == 0
        cleared = _wait_status(port, session_id, lambda body: body.get("detached") is True)
        assert cleared["status"] == "READY_FOR_HUMAN_SUBMIT"
        assert "question" not in cleared
        assert cleared["submitted"] is False
        assert cleared["stopped_before_submit"] is True
        assert fake.detached >= 1
    finally:
        helper.close()
        server.shutdown()


def test_unverified_answer_keeps_the_question(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    fake = _AnswerSession(verify=False)
    helper = LocalHelper(session=fake, idle_detach_seconds=0)  # type: ignore[arg-type]
    server = serve(port=0, helper=helper)
    port = int(server.server_address[1])
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    try:
        status, created = _request(
            port,
            "POST",
            "/v1/sessions",
            {"applicationUrl": "https://boards.greenhouse.io/example/jobs/1", "profile": profile},
        )
        assert status == 202
        assert fake.started.wait(timeout=5)
        fake.release.set()
        session_id = created["sessionId"]
        shown = _wait_status(
            port,
            session_id,
            lambda body: body.get("question", {}).get("id") == "q-note",
        )
        assert shown["question"] == {
            "id": "q-note",
            "prompt": "Anything else we should know?",
            "type": "text",
            "options": [],
        }
        assert shown["status"] == "MANUAL_ANSWER_REQUIRED"
        code, body = _request(
            port,
            "POST",
            f"/v1/sessions/{session_id}/answer",
            {"id": "q-note", "value": "cobalt-42"},
        )
        assert code == 200
        assert body["verified"] is False
        assert body["filled"] is False
        assert body["reason"] == "not_committed"
        assert body["stopped_before_submit"] is True
        assert body["submitted"] is False
        assert body["question"]["id"] == "q-note"
        assert "cobalt-42" not in json.dumps(body)
        follow = _request(port, "GET", f"/v1/sessions/{session_id}")[1]
        assert follow["question"]["id"] == "q-note"
        assert follow["status"] == "MANUAL_ANSWER_REQUIRED"
        assert fake.detached == 0
        assert fake.stopped == 0
        assert fake.applied == [{"selector": "#note", "value": "cobalt-42", "check": False}]
    finally:
        helper.close()
        server.shutdown()


class _AnswerThenSubmit(_AnswerSession):
    def apply_answer(self, url: str, profile: object, target: dict) -> dict:
        raise LocalAttachError("The page reported a submit. The local driver does not submit applications.")


def test_answer_reported_submit_is_failed_and_not_stopped_before_submit(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    fake = _AnswerThenSubmit(verify=True)
    helper = LocalHelper(session=fake, idle_detach_seconds=30)  # type: ignore[arg-type]
    server = serve(port=0, helper=helper)
    port = int(server.server_address[1])
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    try:
        status, created = _request(
            port,
            "POST",
            "/v1/sessions",
            {"applicationUrl": "https://boards.greenhouse.io/example/jobs/1", "profile": profile},
        )
        assert status == 202
        assert fake.started.wait(timeout=5)
        fake.release.set()
        session_id = created["sessionId"]
        shown = _wait_status(port, session_id, lambda body: body.get("question", {}).get("id") == "q-color")
        assert shown["stopped_before_submit"] is True
        code, body = _request(
            port,
            "POST",
            f"/v1/sessions/{session_id}/answer",
            {"id": "q-color", "value": "Blue"},
        )
        assert code == 409
        assert body["verified"] is False
        assert body["filled"] is False
        assert body["submitted"] is True
        assert body["stopped_before_submit"] is False
        assert body["status"] == "FAILED"
        assert "question" not in body
        assert "Blue" not in json.dumps(body)
        follow = _request(port, "GET", f"/v1/sessions/{session_id}")[1]
        assert follow["status"] == "FAILED"
        assert follow["submitted"] is True
        assert follow["stopped_before_submit"] is False
        assert "question" not in follow
        assert fake.stopped == 0
    finally:
        helper.close()
        server.shutdown()
