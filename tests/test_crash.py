"""A dead Chromium is relaunched. The session id is not replaced."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright
from tests.helpers import empty_credentials, job_context

from autofill.engine import ApplicationResult, Status
from autofill.profile import CandidateProfile
from autofill.service.runner import PlaywrightRunner

FIXTURE = Path(__file__).parent / "fixtures" / "leak_form.html"


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {"candidateId": "ref-crash", "personal": {"full_name": "Canary Name", "email": "canary@example.com"}}
    )


def _request(url: str, session_id: str = "sess-crash"):
    from autofill.service.runner import FillRequest

    return FillRequest(
        session_id=session_id,
        candidate_id="ref-crash",
        job_id="job-crash",
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


def test_closed_browser_relaunches_for_the_same_session(tmp_path):
    _chromium_or_skip()
    page = tmp_path / "one.html"
    page.write_text("<h1>Contact</h1><label for='email'>Email</label><input id='email' type='email'>", encoding="utf-8")
    runner = PlaywrightRunner(enabled=True, headless=True)
    try:
        runner.prewarm()
        assert runner.browser_ready_ms >= 0
        runner._submit(lambda: runner._driver._browser.close())
        result = runner.run(_request(page.as_uri()))
        assert result.session_id == "sess-crash"
        assert result.status in {Status.FILLED, Status.READY_FOR_HUMAN_SUBMIT}
        assert list(runner._sessions) == ["sess-crash"]
    finally:
        runner.shutdown()


def test_repeated_browser_fault_keeps_one_session(monkeypatch):
    _chromium_or_skip()
    calls: list[str] = []

    def explode(_self, request, _page):
        calls.append(request.session_id)
        raise RuntimeError("browser has been closed")

    monkeypatch.setattr(PlaywrightRunner, "_run_engine", explode)
    runner = PlaywrightRunner(enabled=True, headless=True)
    try:
        result = runner.run(_request("about:blank", session_id="sess-retry"))
        assert isinstance(result, ApplicationResult)
        assert result.status == Status.FAILED_RETRYABLE
        assert result.session_id == "sess-retry"
        assert calls == ["sess-retry", "sess-retry"]
        assert len(runner._sessions) <= 1
    finally:
        runner.shutdown()
