"""A resume parse must not leave approved fields empty or a stale gap."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from autofill.extract import extract_page
from autofill.resolver import ResolverBinding, ResolverResponse
from autofill.resume_fetch import ResumePayload
from autofill.stepfill import _resume_accepted, fill_resolved_page, required_gaps

FIXTURES = Path(__file__).parent / "fixtures"
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def _binding() -> ResolverBinding:
    return ResolverBinding(
        url="https://resolver.example/v1",
        token="synthetic-token",
        expires_at="2099-01-01T00:00:00Z",
        candidate_ref="candidate-a",
        job_ref="job-a",
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


def test_ashby_parse_refills_cleared_values_and_keeps_a_typed_one(browser, monkeypatch):
    monkeypatch.setattr(
        "autofill.stepfill.resolve_step",
        lambda *args, **kwargs: ResolverResponse(
            fields={
                "fullName": "River Example",
                "email": "river.example@example.com",
                "phone": "5550100199",
                "resume.file": {"url": "https://example.com/resume.pdf", "filename": "notes.pdf"},
            }
        ),
    )
    monkeypatch.setattr(
        "autofill.stepfill.fetch_resume_payload",
        lambda descriptor: ResumePayload(name="notes.pdf", mime_type="application/pdf", data=PDF),
    )
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "ashby_resume_rewrite.html").as_uri())
        outcome = fill_resolved_page(
            page,
            _binding(),
            session_id="sess-ashby",
            ats_name="ashby",
            step="application",
        )
        assert outcome.resume_blocked is False
        assert page.locator("#name").input_value() == "River Example"
        assert page.locator("#phone").input_value() == "5550100199"
        assert page.locator("#email").input_value() == "parsed@example.com"
        assert page.locator("#referral").input_value() == "FRIEND"
    finally:
        page.close()


def test_lever_gap_rereads_a_filled_name_after_upload(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "lever_stale_name.html").as_uri())
        stale = extract_page(page)
        page.locator("#resume").set_input_files(
            [{"name": "notes.pdf", "mimeType": "application/pdf", "buffer": PDF}]
        )
        page.locator("#chip").wait_for(state="visible")
        gaps = required_gaps(page, stale, None)
        texts = " ".join(str(item.get("text") or "") for item in gaps).casefold()
        assert "full name" not in texts
        assert page.locator("#name").input_value() == "River Example"
    finally:
        page.close()


def test_greenhouse_resume_confirm_returns_when_the_file_is_shown(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "greenhouse_resume.html").as_uri())
        page.locator("#resume").set_input_files(
            [{"name": "notes.pdf", "mimeType": "application/pdf", "buffer": PDF}]
        )
        started = time.monotonic()
        assert _resume_accepted(page, "#resume", "notes.pdf") is True
        assert time.monotonic() - started < 1.0
    finally:
        page.close()
