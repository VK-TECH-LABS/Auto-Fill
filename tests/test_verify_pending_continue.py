"""R4 (t182u, KLA 2026-10-27): Continue alone must not clear ``verify_pending``.

KLA run #3: Create Account returned to the Sign In pane (``verifyBeforeSignIn``),
the service parked on the verify card, and 8 s later a ``humanResolved`` Continue
cleared ``verify_pending``. The engine then typed the password registered seconds
earlier into Sign In and Workday answered "wrong email or password or locked".

Now:
* the ``verifyBeforeSignIn`` register reply marks the session ``verify_pending``;
* a run that parks on the verify card keeps ``verify_pending``;
* Continue with only ``humanResolved`` keeps it (the run re-checks the page and
  types nothing into Sign In);
* only Continue with ``humanResolved`` and ``verified: true`` clears it, so the
  one sign-in happens after the person says they opened the verification email;
* reaching the application still clears it.

Synthetic runner only. No real employer account, no Sign In.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from autofill.engine import Status
from autofill.service.app import create_app
from autofill.service.schemas import ContinueIn
from test_protocol_070 import AUTH, TOKEN, _body, _Runner

SECRET = "Fixture-Pass-9!"
VERIFY_ACCOUNT = {
    "mode": "login",
    "verifyBeforeSignIn": True,
    "tenant": {
        "ats": "workday",
        "host": "kla.wd1.myworkdayjobs.com",
        "tenantKey": "kla",
        "loginOrigin": "https://kla.wd1.myworkdayjobs.com",
    },
    "siteErrors": [],
}


class _VerifyRunner(_Runner):
    """Register returns to Sign In (verifyBeforeSignIn). Each run is recorded."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list = []
        self.next_status = ""
        self.next_account: dict | None = None

    def run(self, request):
        self.requests.append(request)
        result = super().run(request)
        if self.next_status:
            result.status = self.next_status
            result.current_step = self.next_status
        if self.next_account is not None:
            result.account = dict(self.next_account)
        return result

    def account_action(self, session_id: str, payload: dict) -> dict:
        return {
            "ok": True,
            "outcome": "registered",
            "status": "",
            "siteErrors": [],
            "account": dict(VERIFY_ACCOUNT),
            "verifyBeforeSignIn": True,
        }


def _started(client: TestClient) -> str:
    created = client.post(
        "/v1/sessions", headers=AUTH, json=_body("0.7.0", "https://boards.greenhouse.io/example/account")
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["sessionId"]
    started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
    assert started.status_code == 200, started.text
    return session_id


def _register(client: TestClient, session_id: str):
    return client.post(
        f"/v1/sessions/{session_id}/credentials",
        headers=AUTH,
        json={
            "candidateRef": "ref-a",
            "email": "person@example.com",
            "password": SECRET,
            "confirmPassword": SECRET,
            "mode": "register",
        },
    )


def _continue(client: TestClient, session_id: str, **extra):
    return client.post(
        f"/v1/sessions/{session_id}/continue",
        headers=AUTH,
        json={"candidateRef": "ref-a", "humanResolved": True, **extra},
    )


def _parked(runner: _VerifyRunner) -> None:
    """The engine (verify_pending on a Sign In pane) parks on the verify card again."""
    runner.next_status = Status.EMAIL_VERIFICATION_REQUIRED
    runner.next_account = dict(VERIFY_ACCOUNT)


def test_verify_before_sign_in_register_marks_verify_pending():
    runner = _VerifyRunner()
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as client:
        session_id = _started(client)
        stored = _register(client, session_id)
        assert stored.status_code == 200, stored.text
        assert stored.json()["outcome"] == "registered"
        assert stored.json()["status"] == Status.EMAIL_VERIFICATION_REQUIRED
        assert app.state.store.get(session_id).verify_pending is True
        assert SECRET not in stored.text


def test_continue_without_verified_keeps_verify_pending_and_the_run_is_told(caplog):
    caplog.set_level("INFO")
    runner = _VerifyRunner()
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as client:
        session_id = _started(client)
        assert _register(client, session_id).status_code == 200
        _parked(runner)
        resumed = _continue(client, session_id)
        assert resumed.status_code == 200, resumed.text
        # The engine run is told verify first: on a Sign In pane it types nothing.
        assert runner.requests[-1].verify_pending is True
        assert runner.requests[-1].human_resolved is True
        session = app.state.store.get(session_id)
        assert session.verify_pending is True
        assert resumed.json()["status"] == Status.EMAIL_VERIFICATION_REQUIRED
        assert "verify_pending=kept reason=continue_without_verified" in caplog.text
        # A second plain Continue still never clears it.
        assert _continue(client, session_id).status_code == 200
        assert runner.requests[-1].verify_pending is True
        assert SECRET not in caplog.text


def test_continue_with_verified_clears_verify_pending_once(caplog):
    caplog.set_level("INFO")
    runner = _VerifyRunner()
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as client:
        session_id = _started(client)
        assert _register(client, session_id).status_code == 200
        runner.next_status = Status.MANUAL_ANSWER_REQUIRED
        runner.next_account = None
        resumed = _continue(client, session_id, verified=True)
        assert resumed.status_code == 200, resumed.text
        assert runner.requests[-1].verify_pending is False
        assert app.state.store.get(session_id).verify_pending is False
        assert "verify_pending=cleared reason=human_verified" in caplog.text


def test_verified_without_human_resolved_does_not_clear():
    runner = _VerifyRunner()
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as client:
        session_id = _started(client)
        assert _register(client, session_id).status_code == 200
        out = client.post(
            f"/v1/sessions/{session_id}/continue",
            headers=AUTH,
            json={"candidateRef": "ref-a", "verified": True},
        )
        assert out.status_code in {200, 409}
        assert app.state.store.get(session_id).verify_pending is True


def test_person_signed_in_in_live_view_continue_reaches_form_and_clears():
    """The safe probe: plain Continue re-checks the page; an application clears the flag."""
    runner = _VerifyRunner()
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as client:
        session_id = _started(client)
        assert _register(client, session_id).status_code == 200
        runner.next_status = Status.MANUAL_ANSWER_REQUIRED
        runner.next_account = None
        resumed = _continue(client, session_id)
        assert resumed.status_code == 200, resumed.text
        assert runner.requests[-1].verify_pending is True
        assert app.state.store.get(session_id).verify_pending is False


def test_run_that_parks_on_verify_card_sets_verify_pending():
    """An "unverified" Sign In banner (engine verify card) also stays verify first."""
    runner = _VerifyRunner()
    runner.next_status = Status.EMAIL_VERIFICATION_REQUIRED
    runner.next_account = dict(VERIFY_ACCOUNT)
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as client:
        session_id = _started(client)
        assert app.state.store.get(session_id).verify_pending is True


def test_continue_schema_accepts_verified_and_still_forbids_unknown_keys():
    assert ContinueIn.model_validate({"humanResolved": True, "verified": True}).verified is True
    assert ContinueIn.model_validate({"humanResolved": True}).verified is False
    with pytest.raises(ValidationError):
        ContinueIn.model_validate({"humanResolved": True, "emailVerifiedMaybe": True})
