"""One Save & Continue card per page set: extract skip, reveal guard, hosted stop."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright

from autofill.answer_popup import show_popup
from autofill.dom_ui import public_blocking
from autofill.engine import ApplicationResult, Status, _next_button
from autofill.extract import _fingerprint, extract_page
from autofill.follow import _fill_new
from autofill.models import Control, PageSnapshot
from autofill.profile import CandidateProfile
from autofill.service.app import create_app

TOKEN = "synthetic-service-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _future() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def _item(qid: str, text: str, selector: str) -> dict:
    return {
        "kind": "QUESTION",
        "text": text,
        "questionId": qid,
        "questionHash": qid,
        "answerType": "text",
        "options": [],
        "required": True,
        "selector": selector,
        "blocking": True,
    }


class _Runner:
    def __init__(self) -> None:
        self.applied: list[dict] = []
        self.advances = 0
        self.signature = (("https", "jobs.example.test", "/apply", "", ""), (("color", ""),), "questions")
        self.items = [
            _item("q-color", "Favorite color?", "#color"),
            _item("q-visa", "Sponsorship?", "#visa"),
        ]

    def run(self, request):
        return ApplicationResult(
            status=Status.MANUAL_ANSWER_REQUIRED,
            ats="greenhouse",
            current_step=Status.MANUAL_ANSWER_REQUIRED,
            login_status="NOT_REQUIRED",
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            open_items=[dict(item) for item in self.items],
            blocking=public_blocking(self.items[0]),
            next_clicks=2,
            pages_advanced=1,
            page_signature=self.signature,
        )

    def apply_answer(self, session_id: str, target: dict) -> dict:
        self.applied.append(target)
        return {"filled": True, "verified": True}

    def advance_after_answer(self, request):
        self.advances += 1
        if len(self.applied) == 1:
            return ApplicationResult(
                status=Status.MANUAL_ANSWER_REQUIRED,
                ats="greenhouse",
                current_step=Status.MANUAL_ANSWER_REQUIRED,
                login_status="NOT_REQUIRED",
                session_id=request.session_id,
                candidate_id=request.candidate_id,
                job_id=request.job_id,
                open_items=[dict(self.items[1])],
                blocking=public_blocking(self.items[1]),
                next_clicks=0,
                pages_advanced=0,
                page_signature=self.signature,
            )
        return ApplicationResult(
            status=Status.READY_FOR_HUMAN_SUBMIT,
            ats="greenhouse",
            current_step="review",
            login_status="NOT_REQUIRED",
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            next_clicks=1,
            pages_advanced=1,
        )

    def discard(self, session_id: str) -> None:
        return None

    def shutdown(self) -> None:
        return None


def _body() -> dict:
    return {
        "protocolVersion": "0.7.0",
        "candidateRef": "ref-a",
        "jobRef": "job-a",
        "jobContext": {
            "applicationUrl": "https://boards.greenhouse.io/example/jobs/1",
            "title": "Example",
            "company": "Example",
        },
        "resolver": {
            "url": "https://resolver.example.test/v1/resolve",
            "token": "synthetic-resolver-token-0123456789abcdef",
            "expiresAt": _future(),
        },
    }


def test_hosted_stop_lists_the_page_set_and_holds_next_clicks():
    runner = _Runner()
    app = create_app(token=TOKEN, runner=runner, run_browser=False, reaper_interval_seconds=60)
    with TestClient(app) as client:
        created = client.post("/v1/sessions", headers=AUTH, json=_body())
        assert created.status_code == 201, created.text
        session_id = created.json()["sessionId"]
        started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
        assert started.status_code == 200, started.text
        body = started.json()
        assert [row["id"] for row in body["questions"]] == ["q-color", "q-visa"]
        assert body["blocking"]["questionId"] == "q-color"
        assert body["nextClicks"] == 2
        assert body["pagesAdvanced"] == 1
        first = client.post(
            f"/v1/sessions/{session_id}/answer",
            headers=AUTH,
            json={"questionId": "q-color", "value": "Blue"},
        )
        assert first.status_code == 200, first.text
        mid = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        assert mid.status_code == 200
        assert mid.json()["nextClicks"] == 2
        assert mid.json()["pagesAdvanced"] == 1
        assert [row["id"] for row in mid.json()["questions"]] == ["q-visa"]
        second = client.post(
            f"/v1/sessions/{session_id}/answer",
            headers=AUTH,
            json={"questionId": "q-visa", "value": "No"},
        )
        assert second.status_code == 200, second.text
        done = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH).json()
        assert done["status"] == Status.READY_FOR_HUMAN_SUBMIT
        assert done["nextClicks"] == 3
        assert "questions" not in done
        assert runner.advances == 2


def test_reveal_fill_leaves_consent_demographic_and_salary_confirm():
    page = type("Page", (), {"evaluate": lambda self, _script, _selector: False})()
    consent = Control(
        kind="checkbox",
        label="I agree to the Privacy Policy",
        selector="#agree",
        required=True,
    )
    demographic = Control(kind="select", label="Gender", selector="#gender", required=True)
    salary = Control(kind="text", label="Desired salary range", selector="#pay", required=True)
    snapshot = PageSnapshot(controls=[consent, demographic, salary], buttons=[], heading="Questions")
    profile = CandidateProfile.from_dict(
        {
            "personal": {"full_name": "River Example", "email": "river.example@example.com"},
            "compensation": {"salary_range_min": "80000", "salary_currency": "USD"},
        }
    )
    typed = _fill_new(
        page,
        snapshot,
        frozenset({"#agree", "#gender", "#pay"}),
        profile,
        None,
        set(),
        cover_letter_text=None,
        cover_letter_path=None,
    )
    assert typed == []


@pytest.fixture
def browser_page():
    try:
        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception:
            browser = playwright.chromium.launch(headless=True, executable_path="/usr/local/bin/google-chrome")
    except Exception as exc:
        pytest.skip(f"Chromium is not installed: {exc}")
    page = browser.new_page()
    try:
        yield page
    finally:
        page.close()
        browser.close()
        playwright.stop()


def test_tilearc_card_is_not_the_employer_save_and_continue(browser_page):
    browser_page.set_content(
        "<main><button id='employer' type='button'>Save and Continue</button>"
        "<label>Favorite color<input id='color' required></label></main>"
    )
    before = _fingerprint(browser_page)
    assert show_popup(
        browser_page,
        {"id": "q-color", "prompt": "Favorite color?", "type": "text", "options": []},
        nonce="abc123",
    )
    after = _fingerprint(browser_page)
    assert before == after
    snapshot = extract_page(browser_page)
    names = [button.name for button in snapshot.buttons]
    assert "Save & Continue" not in names
    assert "Save and Continue" in names
    nxt = _next_button(snapshot)
    assert nxt is not None
    assert nxt.name == "Save and Continue"
    assert all("tilearc" not in (control.selector or "") for control in snapshot.controls)
