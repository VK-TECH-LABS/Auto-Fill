"""Browser workers run sessions at the same time. A page timeout stays retryable."""

from __future__ import annotations

import threading
import time

import pytest
from playwright.sync_api import sync_playwright
from tests.helpers import empty_credentials, job_context

from autofill.engine import ApplicationResult, AutofillOptions, Status, autofill_application
from autofill.profile import CandidateProfile
from autofill.service.runner import FillRequest, PlaywrightRunner


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {"candidateId": "ref-workers", "personal": {"full_name": "Canary Name", "email": "canary@example.com"}}
    )


def _request(url: str, session_id: str) -> FillRequest:
    return FillRequest(
        session_id=session_id,
        candidate_id="ref-workers",
        job_id="job-workers",
        application_url=url,
        profile=_profile(),
        job=job_context(),
        credentials=empty_credentials(),
        resume_uploaded=False,
        cover_letter_text=None,
    )


def _chromium_or_skip() -> None:
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:
        pytest.skip(f"Chromium is not installed: {exc}")


def _filled(request: FillRequest) -> ApplicationResult:
    return ApplicationResult(
        status=Status.FILLED,
        ats=None,
        current_step=Status.FILLED,
        login_status="NOT_REQUIRED",
        session_id=request.session_id,
        candidate_id=request.candidate_id,
        job_id=request.job_id,
    )


def test_slow_session_does_not_delay_another(monkeypatch):
    _chromium_or_skip()
    entered = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def engine(_self, request, _page, _slot=None):
        if request.session_id == "slow":
            entered.set()
            release.wait(timeout=60)
        order.append(request.session_id)
        return _filled(request)

    monkeypatch.setattr(PlaywrightRunner, "_run_engine", engine)
    runner = PlaywrightRunner(enabled=True, headless=True, workers=2)
    slow_box: dict = {}
    fast_box: dict = {}

    def run_slow() -> None:
        slow_box["result"] = runner.run(_request("about:blank", "slow"))

    def run_fast() -> None:
        fast_box["result"] = runner.run(_request("about:blank", "fast"))

    slow = threading.Thread(target=run_slow)
    fast = threading.Thread(target=run_fast)
    try:
        slow.start()
        assert entered.wait(timeout=30)
        started = time.perf_counter()
        fast.start()
        fast.join(timeout=20)
        elapsed = time.perf_counter() - started
        assert not fast.is_alive()
        assert elapsed < 20
        assert fast_box["result"].status == Status.FILLED
        assert order == ["fast"]
    finally:
        release.set()
        slow.join(timeout=20)
        fast.join(timeout=5)
        runner.shutdown()
    assert slow_box["result"].status == Status.FILLED
    assert order == ["fast", "slow"]


def test_page_timeout_is_retryable(monkeypatch):
    def boom(_self, _request):
        raise TimeoutError("Timeout 30000ms exceeded.")

    monkeypatch.setattr(PlaywrightRunner, "_run_with_page", boom)
    runner = PlaywrightRunner(enabled=True, headless=True, workers=1)
    try:
        result = runner.run(_request("https://boards.greenhouse.io/example/jobs/1", "sess-timeout"))
    finally:
        runner.shutdown()
    assert result.status == Status.FAILED_RETRYABLE
    assert result.messages == ["ats_timeout"]


def test_navigation_timeout_from_the_page_is_retryable(caplog):
    class Page:
        def evaluate(self, *_args, **_kwargs):
            raise TimeoutError("Timeout 30000ms exceeded. https://secret.example/apply")

    caplog.set_level("INFO")
    result = autofill_application(
        "https://boards.greenhouse.io/example/jobs/1",
        _profile(),
        None,
        AutofillOptions(page=Page(), session_id="sess-ats-timeout"),
    )
    assert result.status == Status.FAILED_RETRYABLE
    assert result.messages == ["ats_timeout"]
    assert "ats_timeout" in caplog.text
    assert "secret.example" not in caplog.text


def test_default_browser_pool_is_two_workers():
    import inspect

    from autofill.service.app import create_app
    from autofill.service.runner import DEFAULT_LAUNCH_ARGS

    assert inspect.signature(create_app).parameters["browser_workers"].default == 2
    assert inspect.signature(PlaywrightRunner.__init__).parameters["workers"].default == 2
    joined = " ".join(DEFAULT_LAUNCH_ARGS)
    assert "--disable-dev-shm-usage" in joined
    assert "--disable-gpu" in joined
    assert "--renderer-process-limit=2" in joined
    assert "--max-old-space-size=" in joined
