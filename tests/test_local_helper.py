"""Loopback helper. Final Submit stays off, and idle detach leaves Chrome up."""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from autofill.engine import ApplicationResult, Status
from autofill.local_attach import (
    ENV_FLAG,
    FeatureDisabled,
    LocalAttachError,
    LoopbackRequired,
    fetch_version,
    find_chrome,
    start_fixture_server,
)
from autofill.local_helper import LocalHelper, main, serve

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
