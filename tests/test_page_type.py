"""A job posting is not a finished application, and startup does not hang the preview."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright
from tests.helpers import empty_credentials, job_context

from autofill.dom_ui import progress_percent
from autofill.engine import ApplicationResult, AutofillOptions, Status, autofill_application
from autofill.models import ButtonControl, Control, PageSnapshot
from autofill.pagewait import choose_entry, classify_page, find_entry_control, terminal_kind
from autofill.profile import CandidateProfile
from autofill.service.app import create_app
from autofill.service.runner import FillRequest, PlaywrightRunner, StartupError

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "synthetic-service-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
EMAIL = "river.example@example.com"


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {"candidateId": "ref-a", "personal": {"full_name": "River Example", "email": EMAIL}}
    )


def _snapshot(**kwargs) -> PageSnapshot:
    return PageSnapshot(controls=kwargs.pop("controls", []), buttons=kwargs.pop("buttons", []), **kwargs)


def _run(page, url: str):
    return autofill_application(
        url,
        _profile(),
        None,
        AutofillOptions(page=page, session_id="sess-page", candidate_id="ref-a", timings={}),
    )


@pytest.fixture(scope="module")
def browser():
    try:
        with sync_playwright() as playwright:
            chromium = playwright.chromium.launch(headless=True)
            try:
                yield chromium
            finally:
                chromium.close()
    except Exception as exc:
        pytest.skip(f"Chromium is not installed: {exc}")


def test_zero_required_fields_are_not_100_percent():
    assert progress_percent("READY", 0, 0) == 0
    assert progress_percent("READY", 4, 4) == 100


def test_a_posting_apply_control_is_not_ready_and_a_bare_submit_apply_is_left():
    posting = _snapshot(
        heading="Example Engineer",
        page_text="Job description. Responsibilities and qualifications.",
        buttons=[ButtonControl(name="Apply for this Job", selector="#apply_button", control_type="submit")],
        controls=[Control(kind="text", label="Search", selector="#search", input_type="search")],
    )
    assert classify_page(posting) == "JOB_DESCRIPTION"
    assert terminal_kind(posting, gaps=[], required_total=0) == "JOB"
    chosen = find_entry_control(
        _snapshot(buttons=[ButtonControl(name="Apply", selector="#apply_button", control_type="submit")])
    )
    assert chosen is not None
    assert chosen.selector == "#apply_button"
    assert (
        choose_entry(_snapshot(buttons=[ButtonControl(name="Apply", selector="#apply", control_type="submit")]))
        is None
    )
    lever = _snapshot(
        links=[],
        buttons=[ButtonControl(name="Apply", selector="a.postings-btn", control_type="button")],
    )
    assert find_entry_control(lever) is not None
    assert find_entry_control(lever).selector == "a.postings-btn"


def test_a_review_step_with_a_submit_can_be_ready_and_a_blank_page_cannot():
    review = _snapshot(
        heading="Review your application",
        controls=[Control(kind="text", label="Email", selector="#email", input_type="email")],
        buttons=[ButtonControl(name="Submit application", selector="#submit", control_type="submit")],
    )
    assert classify_page(review) == "REVIEW"
    assert terminal_kind(review, gaps=[], required_total=1) == "READY"
    empty_review = _snapshot(
        heading="Review your application",
        buttons=[ButtonControl(name="Submit application", selector="#submit", control_type="submit")],
    )
    assert terminal_kind(empty_review, gaps=[], required_total=0) != "READY"
    blank = _snapshot(heading="Example", page_text="Nothing here.")
    assert terminal_kind(blank, gaps=[], required_total=0) == "NO_FORM"
    # After Next, the last step can be only Submit application. The heading
    # does not have to say Review. A Next button on the same page is not ready.
    submit_only = _snapshot(
        heading="Check and send",
        buttons=[ButtonControl(name="Submit application", selector="#final", control_type="submit")],
    )
    assert terminal_kind(submit_only, gaps=[], required_total=0) == "READY"
    still_forward = _snapshot(
        heading="Check and send",
        buttons=[
            ButtonControl(name="Save & Continue", selector="#next", control_type="button"),
            ButtonControl(name="Submit application", selector="#final", control_type="submit"),
        ],
    )
    assert terminal_kind(still_forward, gaps=[], required_total=0) == "NO_FORM"
    form = _snapshot(
        heading="Application",
        controls=[
            Control(kind="text", label="Email", selector="#email", input_type="email"),
        ],
        buttons=[ButtonControl(name="Submit application", selector="#submit", control_type="submit")],
    )
    assert classify_page(form) == "APPLICATION_FORM"
    assert terminal_kind(form, gaps=[], required_total=1) == "READY"
    assert terminal_kind(form, gaps=[{"text": "Email"}], required_total=1) == "MANUAL"


@pytest.mark.parametrize(
    ("fixture", "url", "entry"),
    [
        ("greenhouse_job_description.html", "https://job-boards.greenhouse.io/example/jobs/1", "Apply for this Job"),
        ("lever_job_description.html", "https://jobs.lever.co/example/role", "Apply for this job"),
        ("ashby_job_description.html", "https://jobs.ashbyhq.com/example/role", "I'm interested"),
    ],
)
def test_real_posting_shapes_click_apply_and_stay_not_ready(browser, fixture, url, entry):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / fixture).as_uri())
        result = _run(page, url)
        clicked = page.evaluate("() => window.__clicked")
        assert result.status == Status.NAVIGATION_STALLED, result.messages
        assert "navigation_stalled" in result.messages
        assert result.status != Status.READY_FOR_HUMAN_SUBMIT
        assert result.stopped_before_submit is True
        assert clicked == [entry]
        assert "Submit application" not in clicked
        assert "Send application" not in clicked
    finally:
        page.close()


def test_a_page_with_no_form_is_not_ready(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "no_form.html").as_uri())
        result = _run(page, "https://example.test/no-form")
        assert result.status == Status.NO_FORM_FOUND
        assert result.progress["percent"] == 0
        assert result.page_type != "APPLICATION_FORM"
        assert result.fields_detected == 0
    finally:
        page.close()


def _body() -> dict:
    return {
        "candidateContext": {
            "candidateId": "candidate-a",
            "personal": {"full_name": "Avery Example", "email": "avery.example@example.com"},
        },
        "jobContext": {
            "applicationUrl": "https://boards.greenhouse.io/example/jobs/1",
            "jobId": "job-1",
            "candidateId": "candidate-a",
            "title": "Example Engineer",
            "company": "Example Labs",
        },
    }


def test_create_returns_503_capacity_when_admission_refuses(monkeypatch):
    monkeypatch.setattr("autofill.service.app.memory_allows_session", lambda: False)

    class Idle:
        def run(self, request):
            raise AssertionError("a refused create must not start")

        def discard(self, session_id: str) -> None:
            return None

        def shutdown(self) -> None:
            return None

    client = TestClient(create_app(token=TOKEN, runner=Idle(), run_browser=False))
    created = client.post("/v1/sessions", headers=AUTH, json=_body())
    assert created.status_code == 503
    assert created.json()["detail"] == {"reason": "capacity"}
    assert created.headers["retry-after"] == "30"


class _Frame:
    url = "https://boards.greenhouse.io/example/jobs/1"

    def screenshot(self, **kwargs):
        return b"\xff\xd8\xff\xd9"


class _StartupRunner:
    def __init__(self) -> None:
        self.calls = 0
        self.discarded: list[str] = []
        self.painted = threading.Event()
        self.release = threading.Event()

    def run(self, request: FillRequest):
        self.calls += 1
        if request.on_phase is not None:
            request.on_phase("navigating")
        if request.on_paint is not None:
            request.on_paint(_Frame())
        self.painted.set()
        if self.calls == 1:
            self.release.wait(timeout=5)
            return ApplicationResult(
                status=Status.FAILED_RETRYABLE,
                ats=None,
                current_step=Status.FAILED_RETRYABLE,
                login_status="NOT_REQUIRED",
                session_id=request.session_id,
                candidate_id=request.candidate_id,
                job_id=request.job_id,
                messages=["navigation_timeout"],
                page_type="UNKNOWN",
            )
        return ApplicationResult(
            status=Status.NO_FORM_FOUND,
            ats="greenhouse",
            current_step=Status.NO_FORM_FOUND,
            login_status="NOT_REQUIRED",
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            messages=[
                "No application form was found. A person can open the page and continue.",
                "job_description",
            ],
            page_type="JOB_DESCRIPTION",
            progress={
                "stage": "OPENING",
                "requiredTotal": 0,
                "requiredDone": 0,
                "percent": 5,
                "currentLabel": "Opening",
            },
        )

    def screenshot(self, session_id: str):
        raise AssertionError("the cached frame should answer while the fill is running")

    def discard(self, session_id: str) -> None:
        self.discarded.append(session_id)
        self.release.set()

    def shutdown(self) -> None:
        self.release.set()


def _resolver_body() -> dict:
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    return {
        "protocolVersion": "0.7.0",
        "candidateRef": "ref-a",
        "jobRef": "job-a",
        "jobContext": {
            "applicationUrl": "https://boards.greenhouse.io/example/jobs/1",
            "title": "Example Engineer",
            "company": "Example Labs",
        },
        "resolver": {
            "url": "https://resolver.example.test/v1/resolve",
            "token": "synthetic-resolver-token-0123456789abcdef",
            "expiresAt": expires,
        },
    }


def test_preview_phase_and_retry_use_the_same_session():
    runner = _StartupRunner()
    client = TestClient(create_app(token=TOKEN, runner=runner, run_browser=False))
    created = client.post("/v1/sessions", headers=AUTH, json=_resolver_body())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["phase"] == "launching"
    assert body["startedAt"] is None
    assert body["pageType"] == "UNKNOWN"
    session_id = body["sessionId"]
    box: dict = {}

    def start() -> None:
        began = time.monotonic()
        box["response"] = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
        box["elapsed"] = time.monotonic() - began

    thread = threading.Thread(target=start)
    thread.start()
    assert runner.painted.wait(timeout=5)
    thread.join(timeout=5)
    started = box["response"]
    assert started.status_code == 200, started.text
    opening = started.json()
    assert box["elapsed"] < 5
    assert opening["status"] == Status.STARTED
    assert opening["phase"] in {"navigating", "loaded"}
    shot = client.get(f"/v1/sessions/{session_id}/screenshot", headers=AUTH)
    assert shot.status_code == 200, shot.text
    assert shot.content.startswith(b"\xff\xd8")
    runner.release.set()
    deadline = time.monotonic() + 5
    failed = opening
    while failed["status"] == Status.STARTED and time.monotonic() < deadline:
        time.sleep(0.05)
        failed = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH).json()
    assert failed["status"] == Status.FAILED_RETRYABLE
    assert failed["messages"] == ["navigation_timeout"]
    assert failed["phase"] == "loaded"
    assert failed["startedAt"]
    schema = client.get("/openapi.json").json()
    assert "/v1/sessions/{session_id}/retry" in schema["paths"]
    assert "submit" not in "".join(schema["paths"])
    retried = client.post(f"/v1/sessions/{session_id}/retry", headers=AUTH)
    assert retried.status_code == 200, retried.text
    again = retried.json()
    assert again["status"] == Status.NO_FORM_FOUND
    assert again["pageType"] == "JOB_DESCRIPTION"
    assert again["progress"]["percent"] == 0
    assert again["checkpoint"]["kind"] == "NO_FORM"
    assert again["checkpoint"]["reason"] == "job_description"
    assert again["phase"] == "loaded"
    assert runner.discarded == [session_id]


def test_startup_timeout_frees_the_attempt(monkeypatch):
    def fail(self, driver, url, **kwargs):
        raise StartupError("navigation_timeout")

    monkeypatch.setattr("autofill.service.runner._SessionContext.open", fail)
    monkeypatch.setattr("autofill.service.runner.memory_allows_session", lambda: True)
    runner = PlaywrightRunner(enabled=True, headless=True, workers=1)
    try:
        result = runner.run(
            FillRequest(
                session_id="sess-startup",
                candidate_id="ref-a",
                job_id="job-1",
                application_url="https://boards.greenhouse.io/example/jobs/1",
                profile=_profile(),
                job=job_context(),
                credentials=empty_credentials(),
                resume_uploaded=False,
                cover_letter_text=None,
            )
        )
    finally:
        runner.shutdown()
    assert result.status == Status.FAILED_RETRYABLE
    assert result.messages == ["navigation_timeout"]
