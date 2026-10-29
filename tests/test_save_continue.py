"""t60u Req3: Save & Continue popup on the employer tab, then the same fill continues."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from autofill.engine import ApplicationResult, Status
from autofill.local_attach import ENV_FLAG, DedicatedApplySession, find_chrome, start_fixture_server
from autofill.local_helper import serve

from test_local_helper import (
    _COLOR,
    _AnswerSession,
    _binding,
    _profile,
    _request,
    _serve,
    _wait_status,
)

ROOT = Path(__file__).resolve().parents[1]
TWO_PAGES = ROOT / "tests" / "fixtures" / "save_continue_two_pages.html"


class _PopupSession(_AnswerSession):
    """Answer session with an in-memory stand-in for the employer-tab popup."""

    def __init__(self) -> None:
        super().__init__(verify=True)
        self.popup: dict | None = None
        self.shown: list[dict] = []
        self.messages: list[tuple[str, bool, bool]] = []
        self.closed = 0

    def show_question_popup(self, question, *, nonce, selector="", candidate="", message="", error=False, force=False):
        self.popup = {"nonce": nonce, "questionId": question["id"], "state": "ask", "values": []}
        self.shown.append({"question": dict(question), "nonce": nonce, "selector": selector,
                           "candidate": candidate, "message": message, "error": error})
        return True

    def read_question_popup(self, nonce):
        popup = self.popup
        if popup is None or popup["nonce"] != nonce:
            return {"present": False}
        return {"present": True, "state": popup["state"], "questionId": popup["questionId"],
                "values": list(popup["values"])}

    def question_popup_message(self, nonce, message, *, error=False, reopen=False):
        self.messages.append((message, error, reopen))
        if reopen and self.popup is not None:
            self.popup["state"] = "ask"
            self.popup["values"] = []
        return True

    def close_question_popup(self):
        self.closed += 1
        self.popup = None

    def click(self, values, *, question_id=None):
        """What the employee's Save & Continue click leaves in the page."""
        assert self.popup is not None
        self.popup["state"] = "answered"
        self.popup["values"] = list(values)
        if question_id is not None:
            self.popup["questionId"] = question_id


def _start(port: int, session: str = "sess-a", candidate: str = "101") -> dict:
    code, created = _request(port, "POST", "/v1/sessions", {
        "applicationUrl": "https://jobs.example.com/apply",
        "profile": _profile("Avery Alpha", "avery.alpha@example.com"),
        "binding": _binding(session, candidate),
    })
    assert code == 202, created
    return created


def test_popup_payload_answer_and_continue(monkeypatch):
    fake = _PopupSession()
    helper, server, port = _serve(monkeypatch, fake)
    try:
        created = _start(port)
        sid = created["sessionId"]
        fake.release.set()
        body = _wait_status(port, sid, lambda b: b.get("status") == Status.MANUAL_ANSWER_REQUIRED)
        assert body["question"]["id"] == "q-color"
        assert body["question"]["popup"] == {"state": "shown", "source": "employer_tab"}
        shown = fake.shown[-1]
        assert shown["question"]["prompt"] == _COLOR["text"]
        assert shown["question"]["options"] == ["Red", "Blue"]
        assert shown["selector"] == "#color"
        assert shown["candidate"] == "Avery Alpha"
        nonce = shown["nonce"]

        # A value outside the employer's choices reopens the popup with an error.
        fake.click(["Green"])
        _code, body = _request(port, "GET", f"/v1/sessions/{sid}")
        assert body["question"]["popup"]["state"] == "shown"
        assert "values" not in body["question"]["popup"]
        assert fake.messages and fake.messages[-1][1:] == (True, True)

        # A click recorded for another question is ignored.
        fake.click(["Red"], question_id="q-other")
        _code, body = _request(port, "GET", f"/v1/sessions/{sid}")
        assert body["question"]["popup"]["state"] == "shown"

        # The real click: canonical option, nonce exposed for the panel.
        fake.popup["questionId"] = "q-color"
        fake.click(["red"])
        _code, body = _request(port, "GET", f"/v1/sessions/{sid}")
        assert body["question"]["popup"] == {
            "state": "answered", "source": "employer_tab", "nonce": nonce, "values": ["Red"],
        }
        assert fake.applied == []  # Nothing reaches the page until TileArc posts /answer.

        # TileArc saved and verified; the panel posts the answer.
        code, answered = _request(port, "POST", f"/v1/sessions/{sid}/answer", {
            "id": "q-color", "value": "Red", "binding": _binding("sess-a", "101"),
        })
        assert code == 200, answered
        assert answered["verified"] is True
        assert fake.applied[0]["value"] == "Red"
        # The next question gets a fresh popup with a new nonce.
        _code, body = _request(port, "GET", f"/v1/sessions/{sid}")
        assert body["question"]["id"] == "q-visa"
        assert body["question"]["popup"]["state"] == "shown"
        assert fake.shown[-1]["nonce"] != nonce
        # The verified answer is offered to later pages of this same fill.
        job = helper.get(sid)
        assert any(row.question == _COLOR["text"] and row.answer == "Red" for row in job.profile.application_answers)

        fake.click(["Sponsorship no"])
        code, answered = _request(port, "POST", f"/v1/sessions/{sid}/answer", {
            "id": "q-visa", "value": "Sponsorship no", "binding": _binding("sess-a", "101"),
        })
        assert code == 200
        _code, body = _request(port, "GET", f"/v1/sessions/{sid}")
        assert body["status"] == Status.READY_FOR_HUMAN_SUBMIT
        assert "question" not in body
        assert fake.popup is None  # Closed once nothing is left to ask.
    finally:
        helper.close()
        server.shutdown()


def test_popup_answer_for_a_replaced_session_is_refused(monkeypatch):
    fake = _PopupSession()
    helper, server, port = _serve(monkeypatch, fake)
    try:
        old = _start(port, "sess-a", "101")
        fake.release.set()
        _wait_status(port, old["sessionId"], lambda b: b.get("status") == Status.MANUAL_ANSWER_REQUIRED)
        fake.click(["Red"])
        _code, body = _request(port, "GET", f"/v1/sessions/{old['sessionId']}")
        assert body["question"]["popup"]["state"] == "answered"
        # Another candidate's Apply replaces the job before TileArc posts the answer.
        new = _start(port, "sess-b", "202")
        _wait_status(port, new["sessionId"], lambda b: b.get("status") == Status.MANUAL_ANSWER_REQUIRED)
        applied_before = len(fake.applied)
        code, body = _request(port, "POST", f"/v1/sessions/{old['sessionId']}/answer", {
            "id": "q-color", "value": "Red", "binding": _binding("sess-a", "101"),
        })
        assert code == 410
        code, body = _request(port, "GET", f"/v1/sessions/{old['sessionId']}")
        assert code == 410 and "question" not in body
        # The old candidate's binding cannot answer the new job either.
        code, _ = _request(port, "POST", f"/v1/sessions/{new['sessionId']}/answer", {
            "id": "q-color", "value": "Red", "binding": _binding("sess-a", "101"),
        })
        assert code in (403, 409, 410)
        assert len(fake.applied) == applied_before
        # The new job's popup starts unanswered.
        _code, body = _request(port, "GET", f"/v1/sessions/{new['sessionId']}")
        assert body["question"]["popup"]["state"] == "shown"
    finally:
        helper.close()
        server.shutdown()


def test_stale_popup_save_reopens_with_an_error(monkeypatch):
    import autofill.local_helper as lh

    monkeypatch.setattr(lh, "_POPUP_SAVE_TIMEOUT_SECONDS", 0.2)
    fake = _PopupSession()
    helper, server, port = _serve(monkeypatch, fake)
    try:
        created = _start(port)
        sid = created["sessionId"]
        fake.release.set()
        _wait_status(port, sid, lambda b: b.get("status") == Status.MANUAL_ANSWER_REQUIRED)
        fake.click(["Blue"])
        _code, body = _request(port, "GET", f"/v1/sessions/{sid}")
        assert body["question"]["popup"]["state"] == "answered"
        time.sleep(0.4)
        _code, body = _request(port, "GET", f"/v1/sessions/{sid}")
        assert body["question"]["popup"]["state"] == "shown"
        assert fake.shown[-1]["error"] is True
        assert "did not confirm" in fake.shown[-1]["message"]
        assert fake.applied == []
    finally:
        helper.close()
        server.shutdown()


def test_multi_select_value_from_the_panel_is_split(monkeypatch):
    from autofill.local_helper import LocalJob, _prepared_target

    item = dict(_COLOR, answerType="multi_select", options=["Red", "Blue", "Green"], questionId="q-multi", questionHash="q-multi")
    job = LocalJob("sid", "https://jobs.example.com/apply")
    job.open_items = [item]
    job.question = {"id": "q-multi", "prompt": item["text"], "type": "multi_select", "options": item["options"]}
    target = _prepared_target(job, item, {"id": "q-multi", "value": "Red | Green"})
    assert target["values"] == ["Red", "Green"]


def test_follow_continues_when_the_page_is_complete(monkeypatch):
    """Last required field answered on a page with Next: the same fill goes on."""
    import autofill.follow as follow_mod
    import autofill.local_attach as la

    calls: list[str] = []
    monkeypatch.setattr(follow_mod, "follow_answer", lambda page, **_k: SimpleNamespace(
        changed=False, status=Status.FILLED, open_items=[], blocking=None, mark=None,
    ))
    done = ApplicationResult(
        status=Status.READY_FOR_HUMAN_SUBMIT, ats=None, current_step="review", login_status="NOT_REQUIRED",
        fields_detected=1, fields_filled=1, stopped_before_submit=True, submit_controls=["Submit"],
    )

    def fake_fill(page, url, profile, **kwargs):
        calls.append(url)
        return done

    monkeypatch.setattr(la, "fill_visible_tab", fake_fill)
    monkeypatch.setattr(la, "_optional_submitted", lambda page: False)
    session = DedicatedApplySession.__new__(DedicatedApplySession)
    session._held = set()
    session._page_mark = None
    session._remember_mark = lambda page: None
    out = session._follow_answer(object(), "https://jobs.example.com/apply", object())
    assert calls == ["https://jobs.example.com/apply"]
    assert out["status"] == Status.READY_FOR_HUMAN_SUBMIT
    assert out["continued"] is True


def _shadow_click(page, value: str) -> None:
    host = page.locator("#tilearc-answer-popup")
    host.get_by_label(value, exact=True).check()
    host.get_by_role("button", name="Save & Continue").click()


@pytest.mark.skipif(os.environ.get("T172_RUN_CHROME_POC") != "1", reason="Set T172_RUN_CHROME_POC=1 to launch Chrome")
def test_real_chrome_save_and_continue_across_two_pages(monkeypatch, tmp_path):
    """Real helper and engine: popup on the employer tab, answer, Next, second popup, stop before Submit."""
    from playwright.sync_api import sync_playwright

    monkeypatch.setenv(ENV_FLAG, "1")
    try:
        executable = find_chrome(None)
    except Exception:
        pytest.skip("Chrome is not installed")
    fixture = start_fixture_server(TWO_PAGES.read_bytes())
    url = f"http://127.0.0.1:{int(fixture.server_address[1])}/t172-local-tab.html"
    server = serve(port=0, user_data_dir=tmp_path / "apply-profile", headless=True,
                   idle_detach_seconds=0, executable=executable)
    port = int(server.server_address[1])
    binding = _binding("sess-r3", "101")

    def wait(predicate, sid):
        body: dict = {}
        for _ in range(300):
            _c, body = _request(port, "GET", f"/v1/sessions/{sid}")
            if predicate(body) or body.get("status") == "FAILED" or body.get("detached"):
                return body
            time.sleep(0.15)
        return body

    try:
        code, created = _request(port, "POST", "/v1/sessions", {
            "applicationUrl": url, "profile": _profile("Avery Alpha", "avery.alpha@example.com"), "binding": binding,
        })
        assert code == 202
        sid = created["sessionId"]
        body = wait(lambda b: (b.get("question") or {}).get("popup", {}).get("state") == "shown", sid)
        question = body["question"]
        assert question["prompt"] == "Are you willing to work rotating night shifts?", body
        assert question["options"] == ["Yes", "No"]
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{server.helper._session.debug_port}")
            page = [p for c in browser.contexts for p in c.pages if "t172-local-tab" in p.url][0]
            host = page.locator("#tilearc-answer-popup")
            text = host.locator(".card").inner_text()
            assert "Are you willing to work rotating night shifts?" in text
            assert "Shifts rotate every two weeks." in text
            assert "Avery Alpha" in text
            _shadow_click(page, "Yes")
        body = wait(lambda b: (b.get("question") or {}).get("popup", {}).get("state") == "answered", sid)
        popup = body["question"]["popup"]
        assert popup["values"] == ["Yes"]
        # TileArc would save + verify here, then post the same value.
        code, answered = _request(port, "POST", f"/v1/sessions/{sid}/answer", {
            "id": question["id"], "value": popup["values"][0], "binding": binding,
        })
        assert code == 200, answered
        assert answered["verified"] is True
        body = wait(lambda b: (b.get("question") or {}).get("id") not in (None, question["id"])
                    and b["question"].get("popup", {}).get("state") == "shown", sid)
        second = body["question"]
        assert second["prompt"] == "Which forklift brand have you operated most?", body
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{server.helper._session.debug_port}")
            page = [p for c in browser.contexts for p in c.pages if "t172-local-tab" in p.url][0]
            assert page.evaluate("() => !!document.getElementById('page2')") is True
            host = page.locator("#tilearc-answer-popup")
            host.locator("textarea").fill("Toyota")
            host.get_by_role("button", name="Save & Continue").click()
        body = wait(lambda b: (b.get("question") or {}).get("popup", {}).get("state") == "answered", sid)
        assert body["question"]["popup"]["values"] == ["Toyota"]
        code, answered = _request(port, "POST", f"/v1/sessions/{sid}/answer", {
            "id": second["id"], "value": "Toyota", "binding": binding,
        })
        assert code == 200 and answered["verified"] is True, answered
        body = wait(lambda b: b.get("status") == Status.READY_FOR_HUMAN_SUBMIT, sid)
        assert body["status"] == Status.READY_FOR_HUMAN_SUBMIT, body
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{server.helper._session.debug_port}")
            page = [p for c in browser.contexts for p in c.pages if "t172-local-tab" in p.url][0]
            state = page.evaluate(
                "() => ({submitted: !!window.__submitted, popup: !!document.getElementById('tilearc-answer-popup'),"
                " brand: (document.getElementById('tool') || {}).value || ''})"
            )
        assert state == {"submitted": False, "popup": False, "brand": "Toyota"}, state
    finally:
        server.helper.stop_launched_chrome()
        server.helper.close()
        server.shutdown()
        fixture.shutdown()
