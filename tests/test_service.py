"""HTTP session protocol. Payloads are fictional. No browser is required here."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.helpers import empty_credentials as _empty_credentials
from tests.helpers import job_context as _job_object

from autofill.ats import site_domain
from autofill.credentials import SiteContext
from autofill.engine import ApplicationResult, Status
from autofill.safeguards import HumanSubmissionRequired
from autofill.service.app import create_app
from autofill.service.auth import AuthConfigurationError, require_configured_token
from autofill.service.runner import BrowserDisabled, FillRequest, PlaywrightRunner

TOKEN = "synthetic-service-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
PASSWORD = "synthetic-password-candidate-a"
EMAIL = "avery.example@example.com"
OTHER_EMAIL = "blair.example@example.com"


def profile(candidate_id: str = "candidate-a", email: str = EMAIL, name: str = "Avery Example") -> dict:
    return {
        "candidateId": candidate_id,
        "personal": {"full_name": name, "email": email, "phone": "555-010-0142"},
    }


def create_body(
    candidate_id: str = "candidate-a",
    job_id: str = "job-1",
    url: str = "https://boards.greenhouse.io/example/jobs/1",
    *,
    email: str = EMAIL,
    name: str = "Avery Example",
    job_candidate: str | None = None,
) -> dict:
    return {
        "candidateContext": profile(candidate_id, email=email, name=name),
        "jobContext": {
            "applicationUrl": url,
            "jobId": job_id,
            "candidateId": candidate_id if job_candidate is None else job_candidate,
            "title": "Example Engineer",
            "company": "Example Labs",
            "postedSalaryMin": 80000,
            "postedSalaryMax": 100000,
        },
        "approvedAnswers": [{"question": "Are you legally authorized to work?", "answer": "Yes"}],
    }


class RecordingRunner:
    """Stand-in for the browser. Records which session's credentials it was given."""

    def __init__(self) -> None:
        self.passwords: dict[str, str | None] = {}
        self.calls: list[tuple[str, bool]] = []
        self.salary: dict[str, int | None] = {}
        self.answers: dict[str, list[str]] = {}
        self.discarded: list[str] = []
        self.mode = "resume"

    def run(self, request: FillRequest) -> ApplicationResult:
        self.calls.append((request.session_id, request.resume_uploaded))
        domain = site_domain(request.application_url)
        creds = request.credentials.get_credentials(
            SiteContext(url=request.application_url, domain=domain, candidate_id=request.candidate_id),
            request.candidate_id,
        )
        self.passwords[request.session_id] = creds.password if creds else None
        self.salary[request.session_id] = request.job.posted_salary_min
        self.answers[request.session_id] = [item.question for item in request.profile.application_answers]
        if self.mode == "raise-submit":
            raise HumanSubmissionRequired("refusing submit")
        if self.mode == "boom":
            raise RuntimeError("synthetic failure")
        if self.mode == "mismatch":
            candidate_id = "candidate-other"
        else:
            candidate_id = request.candidate_id
        if self.mode == "submitted":
            status = "SUBMITTED"
            step = "SUBMITTED"
        elif self.mode == "captcha":
            status = Status.CAPTCHA_REQUIRED
            step = "FAILED"
        elif self.mode == "login":
            status = Status.LOGIN_REQUIRED
            step = "LOGIN_FAILED"
        elif request.resume_uploaded:
            status = Status.READY_FOR_HUMAN_SUBMIT
            step = "READY_FOR_HUMAN_SUBMIT"
        else:
            status = Status.RESUME_UPLOAD_REQUIRED
            step = "RESUME_UPLOAD_REQUIRED"
        return ApplicationResult(
            status=status,
            ats="greenhouse",
            current_step=step,
            login_status="NOT_REQUIRED",
            session_id=request.session_id,
            candidate_id=candidate_id,
            job_id=request.job_id,
            messages=["synthetic run"],
            submit_controls=["Submit application"],
        )

    def discard(self, session_id: str) -> None:
        self.discarded.append(session_id)
        self.passwords.pop(session_id, None)

    def shutdown(self) -> None:
        return None


@pytest.fixture
def runner() -> RecordingRunner:
    return RecordingRunner()


@pytest.fixture
def client(runner: RecordingRunner):
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as test_client:
        yield test_client


def _create(client: TestClient, **kwargs) -> dict:
    response = client.post("/v1/sessions", headers=AUTH, json=create_body(**kwargs))
    assert response.status_code == 201, response.text
    return response.json()


def test_service_token_is_required():
    with pytest.raises(AuthConfigurationError):
        require_configured_token("")
    with pytest.raises(AuthConfigurationError):
        create_app(token="short", runner=RecordingRunner())


def test_auth_rejects_missing_and_wrong_tokens(client: TestClient):
    created = _create(client)
    missing = client.get(f"/v1/sessions/{created['sessionId']}/status")
    wrong = client.get(
        f"/v1/sessions/{created['sessionId']}/status",
        headers={"Authorization": "Bearer not-the-service-token"},
    )
    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.json() == {"detail": "Unauthorized"}
    assert wrong.json() == {"detail": "Unauthorized"}
    assert PASSWORD not in missing.text
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok", "service": "auto-fill"}


def test_create_start_and_status(client: TestClient, runner: RecordingRunner):
    created = _create(client)
    assert created["status"] == "CREATED"
    assert created["stopped_before_submit"] is True
    assert created["candidateId"] == "candidate-a"
    assert EMAIL not in str(created)
    status = client.get(f"/v1/sessions/{created['sessionId']}/status", headers=AUTH)
    assert status.status_code == 200
    assert status.json()["status"] == "CREATED"
    started = client.post(f"/v1/sessions/{created['sessionId']}/start", headers=AUTH, json={})
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["status"] == Status.RESUME_UPLOAD_REQUIRED
    assert body["stopped_before_submit"] is True
    assert body["submitControls"] == ["Submit application"]
    assert body["sessionId"] == created["sessionId"]
    assert EMAIL not in started.text
    assert runner.salary[created["sessionId"]] == 80000
    assert "Are you legally authorized to work?" in runner.answers[created["sessionId"]]
    again = client.get(f"/v1/sessions/{created['sessionId']}/status", headers=AUTH)
    assert again.json()["status"] == Status.RESUME_UPLOAD_REQUIRED
    assert again.json()["stopped_before_submit"] is True


def test_candidate_mismatch_fails_closed(client: TestClient, runner: RecordingRunner):
    mismatch = client.post(
        "/v1/sessions",
        headers=AUTH,
        json=create_body(job_candidate="candidate-b"),
    )
    assert mismatch.status_code == 409
    assert "candidate-b" not in mismatch.text
    created = _create(client)
    start = client.post(
        f"/v1/sessions/{created['sessionId']}/start",
        headers=AUTH,
        json={"candidateId": "candidate-b"},
    )
    assert start.status_code == 409
    assert runner.calls == []
    assert client.get(f"/v1/sessions/{created['sessionId']}/status", headers=AUTH).json()["status"] == "CREATED"
    credentials = client.post(
        f"/v1/sessions/{created['sessionId']}/credentials",
        headers=AUTH,
        json={"email": EMAIL, "password": PASSWORD, "candidateId": "candidate-other"},
    )
    assert credentials.status_code == 409
    assert PASSWORD not in credentials.text
    runner.mode = "mismatch"
    rejected = client.post(f"/v1/sessions/{created['sessionId']}/start", headers=AUTH, json={})
    assert rejected.status_code == 409
    status = client.get(f"/v1/sessions/{created['sessionId']}/status", headers=AUTH).json()
    assert status["candidateId"] == "candidate-a"
    assert status["status"] == "CREATED"


def test_sessions_are_isolated(client: TestClient, runner: RecordingRunner):
    first = _create(client, job_id="job-1")
    second = _create(client, candidate_id="candidate-a", job_id="job-2", email=EMAIL)
    stored = client.post(
        f"/v1/sessions/{first['sessionId']}/credentials",
        headers=AUTH,
        json={"email": EMAIL, "password": PASSWORD, "site": "boards.greenhouse.io"},
    )
    assert stored.status_code == 200
    assert stored.json() == {"sessionId": first["sessionId"], "stored": True}
    assert PASSWORD not in stored.text
    client.post(f"/v1/sessions/{first['sessionId']}/start", headers=AUTH, json={})
    client.post(f"/v1/sessions/{second['sessionId']}/start", headers=AUTH, json={})
    assert runner.passwords[first["sessionId"]] == PASSWORD
    assert runner.passwords[second["sessionId"]] is None
    second_status = client.get(f"/v1/sessions/{second['sessionId']}/status", headers=AUTH).text
    assert PASSWORD not in second_status
    assert first["sessionId"] not in second_status
    other = _create(
        client,
        candidate_id="candidate-b",
        job_id="job-9",
        email=OTHER_EMAIL,
        name="Blair Example",
    )
    client.post(
        f"/v1/sessions/{other['sessionId']}/credentials",
        headers=AUTH,
        json={"email": OTHER_EMAIL, "password": "synthetic-password-candidate-b"},
    )
    client.post(f"/v1/sessions/{other['sessionId']}/start", headers=AUTH, json={})
    assert runner.passwords[other["sessionId"]] == "synthetic-password-candidate-b"
    assert runner.passwords[first["sessionId"]] == PASSWORD
    wrong_site = client.post(
        f"/v1/sessions/{first['sessionId']}/credentials",
        headers=AUTH,
        json={"email": EMAIL, "password": "synthetic-other-site", "site": "https://accounts.google.com"},
    )
    assert wrong_site.status_code == 409
    assert "synthetic-other-site" not in wrong_site.text


def test_resume_checkpoint_and_submit_guard(client: TestClient, runner: RecordingRunner):
    created = _create(client)
    session_id = created["sessionId"]
    started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={})
    assert started.json()["status"] == Status.RESUME_UPLOAD_REQUIRED
    early = client.post(
        f"/v1/sessions/{session_id}/continue",
        headers=AUTH,
        json={"resumeUploaded": False},
    )
    assert early.status_code == 409
    assert len(runner.calls) == 1
    continued = client.post(
        f"/v1/sessions/{session_id}/continue",
        headers=AUTH,
        json={"resumeUploaded": True},
    )
    assert continued.status_code == 200
    assert continued.json()["status"] == Status.READY_FOR_HUMAN_SUBMIT
    assert continued.json()["stopped_before_submit"] is True
    assert runner.calls[-1] == (session_id, True)
    blocked = client.post(
        f"/v1/sessions/{session_id}/continue",
        headers=AUTH,
        json={"resumeUploaded": True},
    )
    assert blocked.status_code == 409
    assert "does not submit" in blocked.json()["detail"]
    assert len(runner.calls) == 2
    fresh = _create(client, job_id="job-submit")
    runner.mode = "submitted"
    refused = client.post(f"/v1/sessions/{fresh['sessionId']}/start", headers=AUTH, json={})
    assert refused.status_code == 409
    assert client.get(f"/v1/sessions/{fresh['sessionId']}/status", headers=AUTH).json()["status"] == "CREATED"
    runner.mode = "raise-submit"
    raised = client.post(f"/v1/sessions/{fresh['sessionId']}/start", headers=AUTH, json={})
    assert raised.status_code == 409
    schema = client.get("/openapi.json").json()
    assert not any("submit" in path.casefold() for path in schema["paths"])
    assert "/v1/sessions/{session_id}/continue" in schema["paths"]
    assert "delete" in schema["paths"]["/v1/sessions/{session_id}"]
    assert "public Auto-Fill" in schema["info"]["description"]


def test_captcha_and_login_do_not_continue(client: TestClient, runner: RecordingRunner):
    captcha = _create(client, job_id="job-captcha")
    runner.mode = "captcha"
    client.post(f"/v1/sessions/{captcha['sessionId']}/start", headers=AUTH, json={})
    stopped = client.post(
        f"/v1/sessions/{captcha['sessionId']}/continue",
        headers=AUTH,
        json={"resumeUploaded": True},
    )
    assert stopped.status_code == 409
    assert "CAPTCHA" in stopped.json()["detail"]
    login = _create(client, job_id="job-login")
    runner.mode = "login"
    client.post(f"/v1/sessions/{login['sessionId']}/start", headers=AUTH, json={})
    blocked = client.post(
        f"/v1/sessions/{login['sessionId']}/continue",
        headers=AUTH,
        json={"resumeUploaded": True},
    )
    assert blocked.status_code == 409
    assert "credentials" in blocked.json()["detail"]


def test_delete_forgets_the_session(client: TestClient, runner: RecordingRunner):
    created = _create(client)
    session_id = created["sessionId"]
    client.post(
        f"/v1/sessions/{session_id}/credentials",
        headers=AUTH,
        json={"email": EMAIL, "password": PASSWORD},
    )
    deleted = client.delete(f"/v1/sessions/{session_id}", headers=AUTH)
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert session_id in runner.discarded
    missing = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
    assert missing.status_code == 404
    assert client.delete(f"/v1/sessions/{session_id}", headers=AUTH).status_code == 404


def test_rejected_bodies_do_not_echo_secrets(client: TestClient):
    database = client.post(
        "/v1/sessions",
        headers=AUTH,
        json={**create_body(), "databaseUrl": "postgres://user:synthetic-db-secret@db.example/app"},
    )
    assert database.status_code == 422
    assert "synthetic-db-secret" not in database.text
    leaked = client.post(
        "/v1/sessions",
        headers=AUTH,
        json=create_body()
        | {
            "candidateContext": {
                "candidateId": "candidate-a",
                "personal": {
                    "full_name": "Avery Example",
                    "email": EMAIL,
                    "password": "synthetic-profile-secret",
                },
            }
        },
    )
    assert leaked.status_code == 422
    assert "synthetic-profile-secret" not in leaked.text
    embedded = client.post(
        "/v1/sessions",
        headers=AUTH,
        json=create_body(url="https://user:synthetic-url-secret@boards.greenhouse.io/example/jobs/1"),
    )
    assert embedded.status_code == 422
    assert "synthetic-url-secret" not in embedded.text
    missing_id = client.post(
        "/v1/sessions",
        headers=AUTH,
        json={
            "candidateContext": {"personal": {"full_name": "Avery Example", "email": EMAIL}},
            "jobContext": {"applicationUrl": "https://boards.greenhouse.io/example/jobs/1"},
        },
    )
    assert missing_id.status_code == 422


def test_logs_omit_profile_and_secrets(client: TestClient, caplog: pytest.LogCaptureFixture):
    caplog.set_level(logging.DEBUG)
    created = _create(client)
    client.post(
        f"/v1/sessions/{created['sessionId']}/credentials",
        headers=AUTH,
        json={"email": EMAIL, "password": PASSWORD},
    )
    client.post(f"/v1/sessions/{created['sessionId']}/start", headers=AUTH, json={})
    text = caplog.text
    assert EMAIL not in text
    assert PASSWORD not in text
    assert TOKEN not in text
    assert "Avery Example" not in text


def test_session_cap(runner: RecordingRunner):
    app = create_app(token=TOKEN, runner=runner, max_sessions=1)
    with TestClient(app) as client:
        assert _create(client)["status"] == "CREATED"
        overflow = client.post("/v1/sessions", headers=AUTH, json=create_body(job_id="job-2"))
        assert overflow.status_code == 429


def test_real_engine_refuses_manual_ats_and_sso_without_a_browser():
    runner = PlaywrightRunner(enabled=False)
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as client:
        manual = _create(client, url="https://ibegin.tcsapps.com/candidate/apply", job_id="job-manual")
        started = client.post(f"/v1/sessions/{manual['sessionId']}/start", headers=AUTH, json={})
        assert started.status_code == 200, started.text
        assert started.json()["status"] == Status.MANUAL_REVIEW_REQUIRED
        assert started.json()["stopped_before_submit"] is True
        sso = _create(client, url="https://accounts.google.com/o/oauth2/v2/auth", job_id="job-sso")
        blocked = client.post(f"/v1/sessions/{sso['sessionId']}/start", headers=AUTH, json={})
        assert blocked.json()["status"] == Status.UNSUPPORTED
        assert blocked.json()["stopped_before_submit"] is True
        ordinary = _create(client, job_id="job-browser")
        disabled = client.post(f"/v1/sessions/{ordinary['sessionId']}/start", headers=AUTH, json={})
        assert disabled.status_code == 409
        assert disabled.json()["detail"] == "Browser execution is disabled."


def test_browser_disabled_error_is_explicit():
    runner = PlaywrightRunner(enabled=False)
    try:
        with pytest.raises(BrowserDisabled):
            runner.run(
                FillRequest(
                    session_id="s",
                    candidate_id="candidate-a",
                    job_id="job-1",
                    application_url="https://boards.greenhouse.io/example/jobs/1",
                    profile=_profile_object(),
                    job=_job_object(),
                    credentials=_empty_credentials(),
                    resume_uploaded=False,
                    cover_letter_text=None,
                )
            )
    finally:
        runner.shutdown()


def _profile_object():
    from autofill.profile import CandidateProfile

    return CandidateProfile.from_dict(profile())


def test_library_imports_without_a_host_checkout():
    env = os.environ.copy()
    env.pop("AUTOFILL_SERVICE_TOKEN", None)
    env.pop("DATABASE_URL", None)
    completed = subprocess.run(
        [sys.executable, "-c", "import autofill; print(autofill.__version__)"],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.stdout.strip() == "0.4.0"
    scan = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import autofill; names=' '.join(sys.modules).casefold();"
            "assert 'vinayaka' not in names; assert 'tilearc' not in names",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert scan.returncode == 0
    refused = subprocess.run(
        [sys.executable, "-m", "autofill.service"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert refused.returncode == 2
    assert "AUTOFILL_SERVICE_TOKEN" in refused.stderr
    assert "postgres://" not in refused.stderr


def test_service_source_has_no_host_application():
    service = Path("src/autofill/service")
    text = "\n".join(path.read_text(encoding="utf-8") for path in service.rglob("*.py"))
    folded = text.casefold()
    assert "vinayaka" not in folded
    assert "tilearc" not in folded
    assert "sqlalchemy" not in folded
    assert "psycopg" not in folded
    assert "DATABASE_URL" in text
    assert "ignored" in text
