"""Candidate isolation, latest profile, and Save & Continue confirm-before-fill.

No Chrome. The helper is exercised with the in-memory session doubles.
"""

from __future__ import annotations

from autofill.credentials import Credentials, SiteContext, credential_scope
from autofill.engine import Status
from autofill.local_helper import _profile_basic_value
from autofill.profile import CandidateProfile
from test_local_helper import (
    _COLOR,
    _PDF,
    _URL,
    _binding,
    _FieldSession,
    _manual_result,
    _profile,
    _RecoverySession,
    _request,
    _serve,
    _stage,
    _wait,
    _wait_status,
)
from test_save_continue import _PopupSession


def test_profile_for_another_candidate_is_refused(monkeypatch):
    fake = _FieldSession()
    fake.release.set()
    helper, server, port = _serve(monkeypatch, fake)
    try:
        profile = _profile("Avery Alpha", "avery@example.com")
        profile["candidateId"] = "someone-else"
        code, body = _request(port, "POST", "/v1/sessions", {
            "applicationUrl": _URL,
            "profile": profile,
            "binding": _binding("sess-a", "101"),
        })
        assert code == 409
        assert body["reason"] == "candidate_mismatch"
        assert body["stopped_before_submit"] is True
        assert fake.calls == []
    finally:
        server.shutdown()
        helper.close()


def test_candidate_switch_drops_the_previous_secret_and_resume(monkeypatch):
    fake = _FieldSession()
    helper, server, port = _serve(monkeypatch, fake)
    try:
        code, first = _request(port, "POST", "/v1/sessions", {
            "applicationUrl": _URL,
            "profile": _profile("Avery Alpha", "avery@example.com"),
            "binding": _binding("sess-a", "101"),
        })
        assert code == 202
        assert fake.started.wait(timeout=5)
        scope = credential_scope(_URL)
        helper._credentials.put(
            "101",
            scope,
            Credentials(site=scope, email="avery@example.com", password="not-the-next-candidate"),
        )
        staged_code, staged = _stage(port, _PDF, session="sess-a", candidate="101")
        assert staged_code == 201
        code, second = _request(port, "POST", "/v1/sessions", {
            "applicationUrl": _URL,
            "profile": _profile("Blake Beta", "blake@example.com"),
            "binding": _binding("sess-b", "202"),
        })
        assert code == 202
        assert second["sessionId"] != first["sessionId"]
        assert second["binding"]["candidateId"] == "202"
        context = SiteContext(url=_URL, domain=scope, candidate_id="101")
        assert helper._credentials.get_credentials(context, "101") is None
        assert staged["resumeRef"] not in helper._staged
        assert _wait(lambda: "Blake Beta" in fake.calls)
        assert "Avery Alpha" not in fake.calls[fake.calls.index("Blake Beta"):]
        old = helper.get(first["sessionId"])
        assert old is not None and old.profile is None
    finally:
        fake.release.set()
        server.shutdown()
        helper.close()


def test_idle_create_uses_the_latest_profile_for_the_same_candidate(monkeypatch):
    fake = _FieldSession()
    fake.release.set()
    helper, server, port = _serve(monkeypatch, fake)
    try:
        body = {
            "applicationUrl": _URL,
            "profile": _profile("Avery Alpha", "avery@example.com"),
            "binding": _binding("sess-a", "101"),
        }
        code, first = _request(port, "POST", "/v1/sessions", body)
        assert code == 202
        sid = first["sessionId"]
        _wait_status(port, sid, lambda row: row.get("status") == Status.MANUAL_ANSWER_REQUIRED)
        again_code, again = _request(port, "POST", "/v1/sessions", body)
        assert again_code == 202 and again["sessionId"] == sid
        assert fake.calls == ["Avery Alpha"]

        latest = _profile("Avery Updated", "avery.updated@example.com")
        latest["applicationAnswers"] = [
            {"question": "Favorite color?", "answer": "Blue", "candidateId": "101"},
            {"question": "Other person's note", "answer": "nope", "candidateId": "202"},
        ]
        code, refreshed = _request(port, "POST", "/v1/sessions", {
            "applicationUrl": _URL,
            "profile": latest,
            "binding": _binding("sess-a", "101"),
        })
        assert code == 202 and refreshed["sessionId"] == sid
        assert _wait(lambda: "Avery Updated" in fake.calls)
        job = helper.get(sid)
        assert job is not None and job.profile is not None
        assert job.profile.personal.email == "avery.updated@example.com"
        assert job.profile.candidate_id == "101"
        answers = {row.question: row.answer for row in job.profile.application_answers}
        assert answers["Favorite color?"] == "Blue"
        assert "Other person's note" not in answers
    finally:
        server.shutdown()
        helper.close()


def test_foreign_application_answers_are_not_loaded():
    profile = CandidateProfile.from_dict(
        {
            "candidateId": "101",
            "personal": {"full_name": "Avery Alpha", "email": "avery@example.com"},
            "applicationAnswers": [
                {"question": "Favorite color?", "answer": "Blue", "candidateId": "101"},
                {"question": "Visa?", "answer": "No"},
                {"question": "Secret", "answer": "other", "candidateId": "202"},
            ],
        }
    )
    questions = [row.question for row in profile.application_answers]
    assert questions == ["Favorite color?", "Visa?"]


def test_save_is_confirmed_on_the_candidate_before_the_page_is_written(monkeypatch):
    class _Order(_PopupSession):
        def __init__(self) -> None:
            super().__init__()
            self.job = None
            self.seen: list[dict] = []

        def apply_answer(self, url, profile, target, **_kwargs):
            popup = self.job.popup if self.job is not None else None
            self.seen.append(
                {
                    "value": target.get("value"),
                    "confirmed": bool(popup and popup.get("confirmed")),
                    "candidate": (popup or {}).get("candidateId"),
                    "saved": any(
                        row.answer == "Blue" and row.question == _COLOR["text"]
                        for row in profile.application_answers
                    ),
                }
            )
            return {
                "verified": True,
                "filled": True,
                "status": Status.READY_FOR_HUMAN_SUBMIT,
                "open_items": [],
                "blocking": None,
            }

    fake = _Order()
    helper, server, port = _serve(monkeypatch, fake)
    try:
        created = _start_bound(port)
        sid = created["sessionId"]
        fake.job = helper.get(sid)
        fake.release.set()
        _wait_status(port, sid, lambda row: row.get("status") == Status.MANUAL_ANSWER_REQUIRED)
        fake.click(["Blue"])
        _code, body = _request(port, "GET", f"/v1/sessions/{sid}")
        assert body["status"] == Status.READY_FOR_HUMAN_SUBMIT
        assert fake.seen and fake.seen[0]["saved"] is True
        assert fake.seen[0]["confirmed"] is True
        assert fake.seen[0]["candidate"] == "101"
        assert body["answerSave"] == {"confirmed": True, "candidateId": "101"}
        learned = body["learnedApplicationAnswers"]
        assert learned[0]["candidateId"] == "101"
        assert learned[0]["answer"] == "Blue"
    finally:
        helper.close()
        server.shutdown()


def test_profile_basics_are_written_instead_of_asked(monkeypatch):
    email = {
        "text": "Confirm your email",
        "questionId": "q-email",
        "questionHash": "q-email",
        "fieldKey": "email",
        "answerType": "text",
        "options": [],
        "selector": "#confirm-email",
        "kind": "MISSING_PROFILE_VALUE",
    }
    city = {
        "text": "City",
        "questionId": "q-city",
        "questionHash": "q-city",
        "fieldKey": "city",
        "answerType": "text",
        "options": [],
        "selector": "#city",
        "kind": "MISSING_PROFILE_VALUE",
    }

    class _Basics(_PopupSession):
        def fill(self, url, profile, **_kwargs):
            self.started.set()
            assert self.release.wait(timeout=5)
            manual, submitted = super().fill(url, profile)
            manual.open_items = [dict(email), dict(city), dict(_COLOR)]
            manual.blocking = dict(email)
            return manual, submitted

        def apply_answer(self, url, profile, target, **_kwargs):
            self.applied.append({"value": target.get("value"), "selector": target.get("selector")})
            return {"verified": True, "filled": True}

    fake = _Basics()
    fake.release.set()
    helper, server, port = _serve(monkeypatch, fake)
    try:
        created = _start_bound(port)
        body = _wait_status(
            port,
            created["sessionId"],
            lambda row: row.get("status") == Status.MANUAL_ANSWER_REQUIRED and row.get("question"),
        )
        assert [row["id"] for row in body["questions"]] == ["q-color"]
        assert body["question"]["id"] == "q-color"
        written = [item["value"] for item in fake.applied]
        assert "avery.alpha@example.com" in written
        person = CandidateProfile.from_dict(_profile("Avery Alpha", "avery.alpha@example.com"))
        assert person.personal.city in written
        assert "Blue" not in written
    finally:
        helper.close()
        server.shutdown()


def test_save_continue_uses_separate_first_and_last(monkeypatch):
    """A First/Last card is filled from the handoff parts, not a split full name."""
    first = {
        "text": "First name",
        "questionId": "q-first",
        "questionHash": "q-first",
        "fieldKey": "first_name",
        "answerType": "text",
        "options": [],
        "selector": "#first",
        "kind": "MISSING_PROFILE_VALUE",
    }
    last = {
        "text": "Last name",
        "questionId": "q-last",
        "questionHash": "q-last",
        "fieldKey": "last_name",
        "answerType": "text",
        "options": [],
        "selector": "#last",
        "kind": "MISSING_PROFILE_VALUE",
    }

    class _Names(_PopupSession):
        def fill(self, url, profile, **_kwargs):
            self.started.set()
            assert self.release.wait(timeout=5)
            manual, submitted = super().fill(url, profile)
            manual.open_items = [dict(first), dict(last), dict(_COLOR)]
            manual.blocking = dict(first)
            return manual, submitted

        def apply_answer(self, url, profile, target, **_kwargs):
            self.applied.append({"value": target.get("value"), "selector": target.get("selector")})
            return {"verified": True, "filled": True}

    fake = _Names()
    fake.release.set()
    helper, server, port = _serve(monkeypatch, fake)
    try:
        code, created = _request(port, "POST", "/v1/sessions", {
            "applicationUrl": _URL,
            "profile": {
                "basics": {
                    "firstName": "Mary Ann",
                    "lastName": "Smith",
                    "email": "mary.ann.smith@example.com",
                }
            },
            "binding": _binding("sess-a", "101"),
        })
        assert code == 202, created
        body = _wait_status(
            port,
            created["sessionId"],
            lambda row: row.get("status") == Status.MANUAL_ANSWER_REQUIRED and row.get("question"),
        )
        assert [row["id"] for row in body["questions"]] == ["q-color"]
        written = [item["value"] for item in fake.applied]
        assert written == ["Mary Ann", "Smith"]
    finally:
        helper.close()
        server.shutdown()


def test_profile_basics_come_from_the_profile_and_consent_does_not():
    profile = CandidateProfile.from_dict(_profile("Avery Alpha", "avery.alpha@example.com"))
    assert _profile_basic_value(profile, {"fieldKey": "email", "text": "Confirm your email"}) == (
        "avery.alpha@example.com"
    )
    assert _profile_basic_value(profile, {"text": "City", "kind": "MISSING_PROFILE_VALUE"}) == profile.personal.city
    assert _profile_basic_value(profile, {"text": "First name"}) == "Avery"
    assert _profile_basic_value(profile, {"kind": "CONSENT", "text": "I agree to the terms", "fieldKey": "email"}) == ""
    assert _profile_basic_value(profile, {"text": "Favorite color?", "kind": "QUESTION"}) == ""


def test_continue_uses_the_profile_on_the_request(monkeypatch):
    class _Named(_RecoverySession):
        def __init__(self) -> None:
            super().__init__(crashes=1)
            self.emails: list[str] = []

        def fill(self, url, profile, *, cancel=None, fresh=False, resume=None, on_observed=None,
                 on_nav_block=None, nav_block=None, **_kwargs):
            self.emails.append(profile.personal.email)
            return super().fill(
                url,
                profile,
                cancel=cancel,
                fresh=fresh,
                resume=resume,
                on_observed=on_observed,
                on_nav_block=on_nav_block,
                nav_block=nav_block,
            )

    fake = _Named()
    helper, server, port = _serve(monkeypatch, fake)
    try:
        code, created = _request(port, "POST", "/v1/sessions", {
            "applicationUrl": _URL,
            "profile": _profile("Avery Alpha", "avery@example.com"),
            "binding": _binding("sess-a", "101"),
        })
        assert code == 202
        sid = created["sessionId"]
        failed = _wait_status(port, sid, lambda row: row.get("status") == "FAILED")
        assert failed["status"] == "FAILED"
        mismatch = _profile("Avery Alpha", "avery@example.com")
        mismatch["candidateId"] = "someone-else"
        code, body = _request(port, "POST", f"/v1/sessions/{sid}/continue", {
            "profile": mismatch,
            "binding": _binding("sess-a", "101"),
        })
        assert code == 409 and body["reason"] == "candidate_mismatch"
        assert fake.emails == ["avery@example.com"]
        code, continued = _request(port, "POST", f"/v1/sessions/{sid}/continue", {
            "profile": _profile("Avery Alpha", "avery.latest@example.com"),
            "binding": _binding("sess-a", "101"),
        })
        assert code == 200, continued
        assert fake.emails[-1] == "avery.latest@example.com"
        job = helper.get(sid)
        assert job is not None and job.profile is not None
        assert job.profile.personal.email == "avery.latest@example.com"
        assert job.profile.candidate_id == "101"
    finally:
        server.shutdown()
        helper.close()


def test_saved_question_is_not_asked_again(monkeypatch):
    """A verified write that the follow-up still lists is not put back on the card."""

    class _Stale(_PopupSession):
        def apply_answer(self, url, profile, target, **_kwargs):
            self.applied.append({"value": target.get("value")})
            return {
                "verified": True,
                "filled": True,
                "status": Status.MANUAL_ANSWER_REQUIRED,
                "open_items": [dict(_COLOR)],
                "blocking": dict(_COLOR),
            }

    fake = _Stale()
    helper, server, port = _serve(monkeypatch, fake)
    try:
        created = _start_bound(port)
        sid = created["sessionId"]
        fake.release.set()
        _wait_status(port, sid, lambda row: row.get("status") == Status.MANUAL_ANSWER_REQUIRED)
        fake.click(["Blue"])
        body = _wait_status(port, sid, lambda row: row.get("status") != Status.MANUAL_ANSWER_REQUIRED)
        assert body["status"] == Status.READY_FOR_HUMAN_SUBMIT
        assert "question" not in body
        assert [item["value"] for item in fake.applied] == ["Blue"]
    finally:
        helper.close()
        server.shutdown()


def test_idle_pause_adopts_a_newer_profile_email(monkeypatch):
    """A resume pause is not a reason to keep the email from the previous Apply."""

    class _Resume(_FieldSession):
        def fill(self, url, profile, *, cancel=None):
            self.calls.append(profile.personal.email)
            self.release.set()
            result = _manual_result(_COLOR)
            result.status = Status.RESUME_UPLOAD_REQUIRED
            result.open_items = []
            result.blocking = None
            result.manual_questions = []
            return result, False

    fake = _Resume()
    helper, server, port = _serve(monkeypatch, fake)
    try:
        code, first = _request(port, "POST", "/v1/sessions", {
            "applicationUrl": _URL,
            "profile": _profile("Avery Alpha", "avery@example.com"),
            "binding": _binding("sess-a", "101"),
        })
        assert code == 202
        sid = first["sessionId"]
        _wait_status(port, sid, lambda row: row.get("status") == Status.RESUME_UPLOAD_REQUIRED)
        latest = _profile("Avery Alpha", "avery.updated@example.com")
        code, refreshed = _request(port, "POST", "/v1/sessions", {
            "applicationUrl": _URL,
            "profile": latest,
            "binding": _binding("sess-a", "101"),
        })
        assert code == 202 and refreshed["sessionId"] == sid
        assert _wait(lambda: "avery.updated@example.com" in fake.calls)
        job = helper.get(sid)
        assert job is not None and job.profile is not None
        assert job.profile.personal.email == "avery.updated@example.com"
    finally:
        server.shutdown()
        helper.close()


def test_confirmed_answer_follows_the_same_candidate_only(monkeypatch):
    fake = _FieldSession()
    fake.release.set()
    helper, server, port = _serve(monkeypatch, fake)
    try:
        code, first = _request(port, "POST", "/v1/sessions", {
            "applicationUrl": _URL,
            "profile": _profile("Avery Alpha", "avery@example.com"),
            "binding": _binding("sess-a", "101"),
        })
        assert code == 202
        sid = first["sessionId"]
        _wait_status(port, sid, lambda row: row.get("status") == Status.MANUAL_ANSWER_REQUIRED)
        job = helper.get(sid)
        assert job is not None and job.profile is not None
        from autofill.local_helper import _profile_digest
        from autofill.profile import ApplicationAnswer

        job.profile.application_answers.append(
            ApplicationAnswer(question="Favorite color?", answer="Blue", from_session=True)
        )
        job.adopted_digest = _profile_digest(job.profile)
        other_url = "https://boards.greenhouse.io/example/jobs/2"
        code, again = _request(port, "POST", "/v1/sessions", {
            "applicationUrl": other_url,
            "profile": _profile("Avery Alpha", "avery@example.com"),
            "binding": _binding("sess-a", "101", job="12", fingerprint="fp2"),
        })
        assert code == 202 and again["sessionId"] != sid
        nxt = helper.get(again["sessionId"])
        assert nxt is not None and nxt.profile is not None
        saved = {row.question: row.answer for row in nxt.profile.application_answers}
        assert saved.get("Favorite color?") == "Blue"
        code, stranger = _request(port, "POST", "/v1/sessions", {
            "applicationUrl": other_url,
            "profile": _profile("Blake Beta", "blake@example.com"),
            "binding": _binding("sess-b", "202", job="12", fingerprint="fp3"),
        })
        assert code == 202
        other = helper.get(stranger["sessionId"])
        assert other is not None and other.profile is not None
        assert all(row.question != "Favorite color?" for row in other.profile.application_answers)
    finally:
        server.shutdown()
        helper.close()


def _start_bound(port: int) -> dict:
    code, created = _request(port, "POST", "/v1/sessions", {
        "applicationUrl": _URL,
        "profile": _profile("Avery Alpha", "avery.alpha@example.com"),
        "binding": _binding("sess-a", "101"),
    })
    assert code == 202, created
    return created
