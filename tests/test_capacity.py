"""Idle TTL, dead-end browser release, one session per owner, and capacity logs."""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from autofill.credentials import MemoryCredentialProvider
from autofill.engine import ApplicationResult, Status
from autofill.models import JobContext
from autofill.profile import CandidateProfile
from autofill.service.app import create_app
from autofill.service.runner import FillRequest, PlaywrightRunner, _SessionContext, log_capacity_refusal
from autofill.service.sessions import ServiceSession, ServiceSessionStore

TOKEN = "synthetic-service-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _profile(candidate_id: str = "candidate-a") -> CandidateProfile:
    return CandidateProfile.from_dict(
        {
            "candidateId": candidate_id,
            "personal": {"full_name": "Avery Example", "email": "avery.example@example.com"},
        }
    )


def _session(session_id: str = "sess-1", candidate_id: str = "candidate-a") -> ServiceSession:
    return ServiceSession(
        session_id=session_id,
        candidate_id=candidate_id,
        job_id="job-1",
        application_url="https://boards.greenhouse.io/example/jobs/1",
        profile=_profile(candidate_id),
        job=JobContext(
            title="Example Engineer",
            company="Example Labs",
            url="https://boards.greenhouse.io/example/jobs/1",
        ),
        cover_letter_text=None,
    )


def _body(candidate_id: str = "candidate-a", job_id: str = "job-1") -> dict:
    return {
        "candidateContext": {
            "candidateId": candidate_id,
            "personal": {"full_name": "Avery Example", "email": "avery.example@example.com"},
        },
        "jobContext": {
            "applicationUrl": "https://boards.greenhouse.io/example/jobs/1",
            "jobId": job_id,
            "candidateId": candidate_id,
            "title": "Example Engineer",
            "company": "Example Labs",
        },
    }


class _Runner:
    def __init__(self) -> None:
        self.discarded: list[str] = []

    def run(self, request: FillRequest) -> ApplicationResult:
        raise AssertionError("this test does not start a fill")

    def discard(self, session_id: str) -> None:
        self.discarded.append(session_id)

    def shutdown(self) -> None:
        return None


def _clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    clock = {"now": 1000.0}
    monkeypatch.setattr("autofill.service.sessions.monotonic", lambda: clock["now"])
    monkeypatch.setattr("autofill.service.app.monotonic", lambda: clock["now"])
    return clock


def test_idle_ttl_expires_a_quiet_session_and_a_poll_resets_it(monkeypatch: pytest.MonkeyPatch):
    clock = _clock(monkeypatch)
    runner = _Runner()
    app = create_app(
        token=TOKEN,
        runner=runner,
        run_browser=False,
        session_ttl_seconds=1800,
        reaper_interval_seconds=3600,
    )
    with TestClient(app) as client:
        created = client.post("/v1/sessions", headers=AUTH, json=_body())
        assert created.status_code == 201, created.text
        session_id = created.json()["sessionId"]
        held = app.state.store.get(session_id)
        assert held is not None
        held.status = Status.CAPTCHA_REQUIRED
        assert app.state.store.reap(now=1299) == []
        assert app.state.store.reap(now=1300) == [session_id]
        assert held.status == Status.EXPIRED
        assert held.browser_released is True

        clock["now"] = 1000.0
        again = client.post("/v1/sessions", headers=AUTH, json=_body(job_id="job-2"))
        assert again.status_code == 201, again.text
        quiet_id = again.json()["sessionId"]
        quiet = app.state.store.get(quiet_id)
        assert quiet is not None
        quiet.status = Status.LOGIN_REQUIRED
        clock["now"] = 1290.0
        polled = client.get(f"/v1/sessions/{quiet_id}/status", headers=AUTH)
        assert polled.status_code == 200
        assert quiet.last_activity_mono == 1290.0
        assert app.state.store.reap(now=1589) == []
        assert app.state.store.reap(now=1590) == [quiet_id]


def test_absolute_ttl_still_expires_a_session_that_was_just_polled(monkeypatch: pytest.MonkeyPatch):
    clock = _clock(monkeypatch)
    app = create_app(
        token=TOKEN,
        runner=_Runner(),
        run_browser=False,
        session_ttl_seconds=1800,
        reaper_interval_seconds=3600,
    )
    with TestClient(app) as client:
        created = client.post("/v1/sessions", headers=AUTH, json=_body())
        session_id = created.json()["sessionId"]
        held = app.state.store.get(session_id)
        assert held is not None
        held.status = Status.CAPTCHA_REQUIRED
        clock["now"] = 2700.0
        assert client.get(f"/v1/sessions/{session_id}/status", headers=AUTH).status_code == 200
        assert held.last_activity_mono == 2700.0
        assert app.state.store.reap(now=2799) == []
        expired = app.state.store.reap(now=2800)
        assert expired == [session_id]
        assert held.status == Status.EXPIRED


def test_dead_end_releases_the_browser_and_human_states_keep_it():
    store = ServiceSessionStore()
    quiet = _session("quiet")
    quiet.status = Status.NO_FORM_FOUND
    quiet.created_mono = 1000
    quiet.last_activity_mono = 1000
    quiet.expires_at = 10_000
    store.create(quiet)
    assert store.release_idle_browsers(now=1119) == []
    assert store.release_idle_browsers(now=1120) == ["quiet"]
    assert quiet.status == Status.NO_FORM_FOUND
    assert quiet.browser_released is True
    assert store.release_idle_browsers(now=5000) == []

    for status in (
        Status.CAPTCHA_REQUIRED,
        Status.LOGIN_REQUIRED,
        Status.LOGIN_FAILED,
        Status.ACCOUNT_CREATION_REQUIRED,
        Status.EMAIL_VERIFICATION_REQUIRED,
        Status.MANUAL_ANSWER_REQUIRED,
        Status.MANUAL_REVIEW_REQUIRED,
    ):
        human = ServiceSessionStore()
        held = _session(status)
        held.status = status
        held.created_mono = 1000
        held.last_activity_mono = 1000
        held.expires_at = 10_000
        human.create(held)
        assert human.release_idle_browsers(now=1120) == [], status
        assert human.reap(now=1300) == [status]
        assert held.status == Status.EXPIRED


def test_failed_and_site_error_are_dead_ends_and_a_poll_postpones_release(monkeypatch: pytest.MonkeyPatch):
    store = ServiceSessionStore()
    failed = _session("failed")
    failed.status = Status.FAILED_FINAL
    failed.created_mono = 1000
    failed.last_activity_mono = 1000
    store.create(failed)
    assert store.release_idle_browsers(now=1120) == ["failed"]

    site = _session("site")
    site.status = Status.FAILED_RETRYABLE
    site.page_type = "SITE_ERROR"
    site.result = ApplicationResult(
        status=Status.FAILED_RETRYABLE,
        ats=None,
        current_step=Status.FAILED_RETRYABLE,
        login_status="NOT_REQUIRED",
        messages=["site_error"],
    )
    site.created_mono = 1000
    site.last_activity_mono = 1000
    store.create(site)
    assert store.release_idle_browsers(now=1119) == []
    assert store.release_idle_browsers(now=1120) == ["site"]
    assert site.status == Status.FAILED_RETRYABLE

    clock = _clock(monkeypatch)
    app = create_app(
        token=TOKEN,
        runner=_Runner(),
        run_browser=False,
        reaper_interval_seconds=3600,
        session_ttl_seconds=1800,
    )
    with TestClient(app) as client:
        created = client.post("/v1/sessions", headers=AUTH, json=_body())
        session_id = created.json()["sessionId"]
        held = app.state.store.get(session_id)
        assert held is not None
        held.status = Status.NO_FORM_FOUND
        clock["now"] = 1119.0
        assert client.get(f"/v1/sessions/{session_id}/status", headers=AUTH).status_code == 200
        assert held.last_activity_mono == 1119.0
        assert app.state.store.release_idle_browsers(now=1238) == []
        assert app.state.store.release_idle_browsers(now=1239) == [session_id]
        assert held.status == Status.NO_FORM_FOUND


def test_finish_run_refreshes_activity_so_a_long_fill_is_not_idle(monkeypatch: pytest.MonkeyPatch):
    clock = {"now": 5000.0}
    monkeypatch.setattr("autofill.service.sessions.monotonic", lambda: clock["now"])
    store = ServiceSessionStore()
    session = _session()
    session.created_mono = 1000
    session.last_activity_mono = 1000
    session.expires_at = 100_000
    session.status = Status.FORM_IN_PROGRESS
    session.busy = True
    store.create(session)
    store.finish_run(session)
    assert session.busy is False
    assert session.last_activity_mono == 5000
    assert store.reap(now=5299) == []
    assert store.reap(now=5300) == [session.session_id]


def test_zero_windows_disable_idle_and_dead_end_release():
    store = ServiceSessionStore()
    store.idle_ttl_seconds = 0
    store.dead_end_browser_seconds = 0
    session = _session()
    session.status = Status.NO_FORM_FOUND
    session.created_mono = 1000
    session.last_activity_mono = 1000
    session.expires_at = 10_000
    store.create(session)
    assert store.reap(now=9999) == []
    assert store.release_idle_browsers(now=9999) == []
    assert session.status == Status.NO_FORM_FOUND


def test_same_owner_replaces_the_previous_live_session(caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)
    runner = _Runner()
    app = create_app(token=TOKEN, runner=runner, run_browser=False, reaper_interval_seconds=3600)
    with TestClient(app) as client:
        first = client.post("/v1/sessions", headers=AUTH, json=_body())
        assert first.status_code == 201, first.text
        first_id = first.json()["sessionId"]
        held = app.state.store.get(first_id)
        assert held is not None
        second = client.post("/v1/sessions", headers=AUTH, json=_body(job_id="job-2"))
        assert second.status_code == 201, second.text
        assert held.status == Status.EXPIRED
        assert first_id in runner.discarded
        assert second.json()["status"] == "CREATED"
        lines = [record.getMessage() for record in caplog.records if "replaced_owner" in record.getMessage()]
        assert lines == [f"session={first_id} status=EXPIRED reason=replaced_owner"]
        assert "candidate-a" not in lines[0]
        assert "avery.example@example.com" not in caplog.text


def test_owner_cap_skips_a_busy_fill_and_can_be_turned_off():
    runner = _Runner()
    app = create_app(token=TOKEN, runner=runner, run_browser=False, reaper_interval_seconds=3600)
    with TestClient(app) as client:
        first = client.post("/v1/sessions", headers=AUTH, json=_body())
        first_id = first.json()["sessionId"]
        held = app.state.store.get(first_id)
        assert held is not None
        held.busy = True
        second = client.post("/v1/sessions", headers=AUTH, json=_body(job_id="job-2"))
        assert second.status_code == 201, second.text
        assert held.status == "CREATED"
        assert first_id not in runner.discarded

    open_runner = _Runner()
    open_app = create_app(
        token=TOKEN,
        runner=open_runner,
        run_browser=False,
        reaper_interval_seconds=3600,
        one_session_per_owner=False,
    )
    with TestClient(open_app) as client:
        left = client.post("/v1/sessions", headers=AUTH, json=_body())
        right = client.post("/v1/sessions", headers=AUTH, json=_body(job_id="job-2"))
        assert left.status_code == 201
        assert right.status_code == 201
        assert open_runner.discarded == []
        left_held = open_app.state.store.get(left.json()["sessionId"])
        right_held = open_app.state.store.get(right.json()["sessionId"])
        assert left_held is not None and left_held.status == "CREATED"
        assert right_held is not None and right_held.status == "CREATED"


def test_blank_owner_does_not_expire_other_sessions():
    store = ServiceSessionStore()
    named = _session("named", "candidate-a")
    named.created_mono = 1000
    named.expires_at = 10_000
    blank = _session("blank", "")
    blank.candidate_ref = ""
    blank.created_mono = 1000
    blank.expires_at = 10_000
    store.create(named)
    store.create(blank)
    assert store.expire_owner("") == []
    assert store.expire_owner("   ") == []
    assert named.status == "CREATED"
    assert blank.status == "CREATED"
    assert store.expire_owner("candidate-b") == []
    assert named.status == "CREATED"


def test_discard_closes_pages_and_a_context_whose_pin_was_lost():
    runner = PlaywrightRunner(enabled=False, workers=2)
    try:
        log: list[str] = []

        class _Page:
            def close(self) -> None:
                log.append("page")

        class _Context:
            def __init__(self, page: _Page) -> None:
                self.pages = [page]

            def close(self) -> None:
                log.append("context")

        page = _Page()
        slot = _SessionContext()
        slot.page = page
        slot.context = _Context(page)
        runner._workers[1].sessions["sess"] = slot
        runner.discard("sess")
        assert log == ["page", "context"]
        assert "sess" not in runner._workers[1].sessions
        assert "sess" not in runner._pins

        broken: list[str] = []

        class _BadPage:
            def close(self) -> None:
                raise RuntimeError("page already gone")

        class _BrokenContext:
            @property
            def pages(self):
                raise RuntimeError("pages")

            def close(self) -> None:
                broken.append("context")

        bad = _SessionContext()
        bad.page = _BadPage()
        bad.context = _BrokenContext()
        runner._pins["lost"] = 0
        runner._loads[0] = 1
        runner._workers[0].sessions["lost"] = bad
        runner.discard("lost")
        assert broken == ["context"]
        assert "lost" not in runner._workers[0].sessions
        assert "lost" not in runner._pins
        assert runner._loads[0] == 0
    finally:
        runner.shutdown()


def test_capacity_refusal_logs_session_count_and_rss(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr("autofill.service.runner.container_memory_bytes", lambda: 900)
    monkeypatch.setattr("autofill.service.runner.memory_limit_bytes", lambda: 850)
    log_capacity_refusal(6)
    assert "capacity=refused sessions=6 rss_bytes=900 limit_bytes=850" in caplog.text

    monkeypatch.setattr("autofill.service.app.memory_allows_session", lambda: False)
    client = TestClient(create_app(token=TOKEN, runner=_Runner(), run_browser=False))
    created = client.post("/v1/sessions", headers=AUTH, json=_body())
    assert created.status_code == 503
    assert created.json()["detail"] == {"reason": "capacity"}
    assert "capacity=refused sessions=0 rss_bytes=900 limit_bytes=850" in caplog.text
    assert "avery.example@example.com" not in caplog.text


def test_in_run_capacity_refusal_logs_open_browsers(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr("autofill.service.runner.memory_allows_session", lambda: False)
    monkeypatch.setattr("autofill.service.runner.container_memory_bytes", lambda: 42)
    monkeypatch.setattr("autofill.service.runner.memory_limit_bytes", lambda: 10)
    runner = PlaywrightRunner(enabled=False, workers=1)
    try:
        result = runner._run_with_page(
            FillRequest(
                session_id="sess-busy",
                candidate_id="candidate-a",
                job_id="job-1",
                application_url="https://boards.greenhouse.io/example/jobs/1",
                profile=_profile(),
                job=JobContext(
                    title="Example Engineer",
                    company="Example Labs",
                    url="https://boards.greenhouse.io/example/jobs/1",
                ),
                credentials=MemoryCredentialProvider(),
                resume_uploaded=False,
                cover_letter_text=None,
            )
        )
    finally:
        runner.shutdown()
    assert result.messages == ["capacity_busy"]
    assert "capacity=refused sessions=0 rss_bytes=42 limit_bytes=10" in caplog.text


def test_server_reads_capacity_env(monkeypatch: pytest.MonkeyPatch):
    captured: dict = {}

    def fake_create_app(**kwargs):
        captured.update(kwargs)

        class _App:
            pass

        return _App()

    monkeypatch.setenv("AUTOFILL_SERVICE_TOKEN", TOKEN)
    monkeypatch.setenv("AUTOFILL_IDLE_TTL_SECONDS", "15")
    monkeypatch.setenv("AUTOFILL_DEAD_END_BROWSER_SECONDS", "7")
    monkeypatch.setenv("AUTOFILL_ONE_SESSION_PER_OWNER", "0")
    monkeypatch.setattr("autofill.service.server.create_app", fake_create_app)
    monkeypatch.setattr("autofill.service.server.uvicorn.run", lambda *args, **kwargs: None)
    from autofill.service.server import main

    main()
    assert captured["idle_ttl_seconds"] == 15.0
    assert captured["dead_end_browser_seconds"] == 7.0
    assert captured["one_session_per_owner"] is False
