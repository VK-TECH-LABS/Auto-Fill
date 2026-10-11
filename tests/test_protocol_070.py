"""Protocol 0.7.0 account, verification, and resume-file behavior. Synthetic fixtures only."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from autofill.engine import ApplicationResult, Status
from autofill.login import account_decision, password_policy, score_account, tenant_identity, verification_present
from autofill.models import ButtonControl, Control, LinkControl, PageSnapshot
from autofill.safeguards import HumanSubmissionRequired, activate_account
from autofill.service.app import create_app
from autofill.service.runner import PageUnavailable
from autofill.service.schemas import CredentialsIn

TOKEN = "synthetic-service-token-0123456789"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
RESOLVER_TOKEN = "synthetic-resolver-token-0123456789abcdef"
SECRET = "Correct-Horse-1"
WEAK = "weak-secret-value"


def _future() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def _body(version: str, url: str = "https://boards.greenhouse.io/example/jobs/1") -> dict:
    return {
        "protocolVersion": version,
        "candidateRef": "ref-a",
        "jobRef": "job-a",
        "jobContext": {
            "applicationUrl": url,
            "title": "Example Engineer",
            "company": "Example Labs",
        },
        "resolver": {
            "url": "https://resolver.example.test/v1/resolve",
            "token": RESOLVER_TOKEN,
            "expiresAt": _future(),
        },
    }


def _control(kind: str, label: str, **kwargs) -> Control:
    return Control(kind=kind, label=label, selector=kwargs.pop("selector", f"#{kind}"), **kwargs)


def _snapshot(**kwargs) -> PageSnapshot:
    return PageSnapshot(controls=kwargs.pop("controls", []), buttons=kwargs.pop("buttons", []), **kwargs)


class _Runner:
    def __init__(self) -> None:
        self.page_open = False
        self.navigating = False
        self.challenge = "clear"
        self.resume_ok = True
        self.seen_secret = ""

    def run(self, request):
        self.page_open = True
        status = Status.RESUME_UPLOAD_REQUIRED if "resume" in request.application_url else Status.LOGIN_REQUIRED
        if "account" in request.application_url:
            status = Status.ACCOUNT_CREATION_REQUIRED
        if "otp" in request.application_url:
            status = Status.EMAIL_VERIFICATION_REQUIRED
        if "captcha" in request.application_url:
            status = Status.CAPTCHA_REQUIRED
        return ApplicationResult(
            status=status,
            ats="greenhouse",
            current_step=status,
            login_status="NOT_REQUIRED",
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            account={
                "mode": "register" if status == Status.ACCOUNT_CREATION_REQUIRED else "login",
                "alternateModeAvailable": True,
                "tenant": {
                    "ats": "greenhouse",
                    "host": "boards.greenhouse.io",
                    "tenantKey": "example",
                    "loginOrigin": "https://boards.greenhouse.io",
                },
                "passwordPolicy": None,
                "emailPrefillAllowed": True,
                "siteErrors": [],
            },
        )

    def discard(self, session_id: str) -> None:
        self.page_open = False

    def shutdown(self) -> None:
        return None

    def screenshot(self, session_id: str):
        if self.navigating:
            raise PageUnavailable(503)
        if not self.page_open:
            return None
        return b"\xff\xd8\xff\xd9", "boards.greenhouse.io"

    def interact(self, session_id: str, action: dict) -> bool:
        return self.page_open

    def account_action(self, session_id: str, payload: dict) -> dict:
        self.seen_secret = str(payload.get("password") or "")
        if WEAK in self.seen_secret:
            return {
                "ok": False,
                "reason": "site_rejected",
                "status": Status.ACCOUNT_CREATION_REQUIRED,
                "siteErrors": ["Password does not meet the policy."],
                "account": {
                    "mode": "register",
                    "alternateModeAvailable": False,
                    "tenant": {
                        "ats": "greenhouse",
                        "host": "boards.greenhouse.io",
                        "tenantKey": "example",
                        "loginOrigin": "https://boards.greenhouse.io",
                    },
                    "passwordPolicy": {"minLength": 8},
                    "emailPrefillAllowed": True,
                    "siteErrors": ["Password does not meet the policy."],
                },
            }
        return {"ok": True, "outcome": "verification_required", "status": Status.EMAIL_VERIFICATION_REQUIRED}

    def probe_challenge(self, session_id: str) -> str:
        return self.challenge

    def attach_resume_file(self, session_id: str, name: str, data: bytes, hint: str = "") -> dict:
        if not self.resume_ok:
            return {"verified": False, "reason": "ats_rejected", "siteErrors": ["failed to upload"]}
        return {"verified": True, "reason": "", "siteErrors": []}

    def confirm_resume(self, session_id: str) -> dict:
        if not self.resume_ok:
            return {"verified": False, "reason": "not_confirmed", "siteErrors": []}
        return {"verified": True, "reason": "", "siteErrors": []}


@pytest.fixture
def api():
    runner = _Runner()
    app = create_app(token=TOKEN, runner=runner, run_browser=False)
    with TestClient(app) as client:
        yield client, runner


def test_register_outranks_sign_in_and_an_application_is_neither():
    register = _snapshot(
        controls=[
            _control("text", "Email", input_type="email"),
            _control("password", "Password", selector="#p1"),
            _control("password", "Verify password", selector="#p2", aria_label="Verify password"),
        ],
        buttons=[ButtonControl(name="Create Account", selector="#create")],
        password_present=True,
        heading="Create account",
    )
    scored = score_account(register)
    assert scored.mode == "register"
    assert scored.register_score > scored.login_score

    sign_in = _snapshot(
        controls=[_control("password", "Password")],
        buttons=[ButtonControl(name="Sign In", selector="#go", automation_id="signInSubmitButton")],
        links=[LinkControl(name="Create Account", selector="#create", automation_id="createAccountLink")],
        password_present=True,
        heading="Sign in",
        page_text="Forgot password",
        automation_ids=["signInSubmitButton", "createAccountLink"],
    )
    both = score_account(sign_in)
    assert both.mode == "login"
    assert both.alternate is True
    hidden_pane = _snapshot(
        controls=[
            _control("password", "Password", selector="#visible"),
            _control(
                "password",
                "Verify password",
                selector="#hidden",
                hidden=True,
                automation_id="verifyPassword",
                aria_label="Verify password",
            ),
        ],
        buttons=[ButtonControl(name="Sign In", selector="#go", automation_id="signInSubmitButton")],
        links=[LinkControl(name="Create Account", selector="#create", automation_id="createAccountLink")],
        password_present=True,
        heading="Sign in",
        page_text="Forgot password",
        automation_ids=["signInSubmitButton", "createAccountLink"],
    )
    parked = score_account(hidden_pane)
    assert parked.mode == "login"
    assert parked.alternate is True

    application = _snapshot(
        controls=[
            _control("password", "Password"),
            _control("file", "Resume"),
        ],
        password_present=True,
        heading="Application",
    )
    assert score_account(application).application is True
    assert score_account(application).mode == "none"
    tied = _snapshot(
        controls=[_control("password", "Password")],
        buttons=[ButtonControl(name="Create Account", selector="#create")],
        password_present=True,
    )
    tie = score_account(tied)
    assert tie.login_score == tie.register_score
    assert tie.mode == "login"


def test_otp_policy_and_tenant_key_use_page_text_only():
    otp = _snapshot(
        controls=[_control("text", "Verification code", autocomplete="one-time-code", max_length=6)],
        heading="Email verification",
        page_text="We sent you a code. Check your inbox.",
    )
    assert verification_present(otp) is True
    assert account_decision(otp, "https://jobs.example.test/join", None) is not None
    assert account_decision(otp, "https://jobs.example.test/join", None).status == "EMAIL_VERIFICATION_REQUIRED"
    policy = password_policy(
        _snapshot(
            controls=[_control("password", "Password")],
            password_present=True,
            page_text=(
                "Use at least 8 characters, one uppercase letter, one lowercase letter, "
                "one digit, and one symbol. Do not include your email."
            ),
        )
    )
    assert policy is not None
    assert policy["minLength"] == 8
    assert policy["requireUpper"] is True
    assert policy["requireDigit"] is True
    assert policy["disallowEmail"] is True
    assert len(policy["raw"]) <= 300
    tenant = tenant_identity("https://example.wd5.myworkdayjobs.com/External/job/1", "workday")
    assert tenant["tenantKey"] == "example.wd5.myworkdayjobs.com/External"
    assert tenant["loginOrigin"] == "https://example.wd5.myworkdayjobs.com"
    assert tenant_identity("https://jobs.lever.co/example/job", "lever")["tenantKey"] == "example"


def test_submit_application_is_not_an_account_button():
    with pytest.raises(HumanSubmissionRequired):
        activate_account(None, "#submit", "Submit Application")


def test_credentials_repr_and_logs_omit_the_secret():
    body = CredentialsIn(email="person@example.com", password=SECRET, mode="store")
    rendered = repr(body)
    assert SECRET not in rendered
    assert "password=" not in rendered


def test_older_sessions_keep_the_old_status_and_store_shape(api, caplog):
    client, _runner = api
    caplog.set_level(logging.INFO)
    created = client.post("/v1/sessions", headers=AUTH, json=_body("0.6.0", "https://jobs.example.test/account"))
    assert created.status_code == 201, created.text
    assert "stateVersion" not in created.json()
    assert created.json()["protocolVersion"] == "0.6.0"
    session_id = created.json()["sessionId"]
    started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
    assert started.json()["status"] == Status.LOGIN_REQUIRED
    assert "stateVersion" not in started.json()
    stored = client.post(
        f"/v1/sessions/{session_id}/credentials",
        headers=AUTH,
        json={"candidateRef": "ref-a", "email": "person@example.com", "password": SECRET},
    )
    assert stored.status_code == 200, stored.text
    assert stored.json() == {"sessionId": session_id, "stored": True}
    assert SECRET not in stored.text
    assert SECRET not in caplog.text
    refused = client.post(
        f"/v1/sessions/{session_id}/credentials",
        headers=AUTH,
        json={"candidateRef": "ref-a", "email": "person@example.com", "password": SECRET, "mode": "login"},
    )
    assert refused.status_code == 409
    assert refused.json()["detail"]["reason"] == "not_at_account_step"


def test_070_account_action_redacts_the_secret_and_reports_site_errors(api, caplog):
    client, runner = api
    caplog.set_level(logging.INFO)
    created = client.post(
        "/v1/sessions",
        headers=AUTH,
        json=_body("0.7.0", "https://boards.greenhouse.io/example/account"),
    )
    session_id = created.json()["sessionId"]
    assert created.json()["stateVersion"] == 1
    assert created.json()["resume"]["verified"] is False
    assert created.json()["completed"] == {"fieldKeys": [], "questionHashes": []}
    started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
    body = started.json()
    assert body["status"] == Status.ACCOUNT_CREATION_REQUIRED
    assert body["blocking"]["kind"] == "ACCOUNT"
    assert body["blocking"]["text"] == "Create employer account"
    assert body["progress"]["stage"] == "ACCOUNT"
    assert body["progress"]["currentLabel"] == "Employer account"
    assert body["account"]["mode"] == "register"
    assert body["stateVersion"] >= 2
    rejected = client.post(
        f"/v1/sessions/{session_id}/credentials",
        headers=AUTH,
        json={
            "candidateRef": "ref-a",
            "email": "person@example.com",
            "password": WEAK,
            "confirmPassword": WEAK,
            "mode": "register",
            "site": "https://boards.greenhouse.io/login",
        },
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["detail"]["reason"] == "site_rejected"
    assert WEAK not in rejected.text
    assert WEAK not in caplog.text
    assert runner.seen_secret == WEAK
    mismatch = client.post(
        f"/v1/sessions/{session_id}/credentials",
        headers=AUTH,
        json={
            "candidateRef": "ref-a",
            "email": "person@example.com",
            "password": SECRET,
            "mode": "register",
            "site": "https://other.example.test/login",
        },
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"]["reason"] == "origin_mismatch"
    assert SECRET not in mismatch.text


def test_resume_file_caps_magic_and_rejection_allow_retry(api):
    client, runner = api
    created = client.post("/v1/sessions", headers=AUTH, json=_body("0.7.0", "https://jobs.example.test/resume"))
    session_id = created.json()["sessionId"]
    client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
    missing = client.post(
        f"/v1/sessions/{session_id}/resume-file",
        headers=AUTH,
        files={"file": ("notes.pdf", b"%PDF-1.4 synthetic", "application/pdf")},
    )
    assert missing.status_code == 422
    too_big = client.post(
        f"/v1/sessions/{session_id}/resume-file",
        headers=AUTH,
        data={"candidateRef": "ref-a"},
        files={"file": ("notes.pdf", b"%PDF-" + b"x" * (10 * 1024 * 1024), "application/pdf")},
    )
    assert too_big.status_code == 413
    wrong = client.post(
        f"/v1/sessions/{session_id}/resume-file",
        headers=AUTH,
        data={"candidateRef": "ref-a"},
        files={"file": ("notes.pdf", b"not-a-pdf", "application/pdf")},
    )
    assert wrong.status_code == 415
    runner.resume_ok = False
    rejected = client.post(
        f"/v1/sessions/{session_id}/resume-file",
        headers=AUTH,
        data={"candidateRef": "ref-a"},
        files={"file": ("notes.pdf", b"%PDF-1.4 synthetic", "application/pdf")},
    )
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["verified"] is False
    assert rejected.json()["reason"] == "ats_rejected"
    status = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
    assert status.json()["status"] == Status.RESUME_UPLOAD_REQUIRED
    assert status.json()["resume"]["verified"] is False
    runner.resume_ok = True
    accepted = client.post(
        f"/v1/sessions/{session_id}/resume-file",
        headers=AUTH,
        data={"candidateRef": "ref-a"},
        files={"file": ("notes.pdf", b"%PDF-1.4 synthetic", "application/pdf")},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["verified"] is True
    assert accepted.json()["filename"] == "notes.pdf"
    again = client.post(
        f"/v1/sessions/{session_id}/resume-file",
        headers=AUTH,
        data={"candidateRef": "ref-a"},
        files={"file": ("notes.pdf", b"%PDF-1.4 synthetic", "application/pdf")},
    )
    assert again.status_code == 409
    assert again.json()["detail"]["reason"] == "already_verified"


def test_human_resolved_rechecks_and_screenshot_navigation_does_not_change_status(api):
    client, runner = api
    created = client.post("/v1/sessions", headers=AUTH, json=_body("0.7.0", "https://jobs.example.test/captcha"))
    session_id = created.json()["sessionId"]
    started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
    assert started.json()["status"] == Status.CAPTCHA_REQUIRED
    version = started.json()["stateVersion"]
    runner.challenge = "captcha"
    blocked = client.post(
        f"/v1/sessions/{session_id}/continue",
        headers=AUTH,
        json={"candidateRef": "ref-a", "humanResolved": True},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["reason"] == "challenge_present"
    status = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
    assert status.json()["status"] == Status.CAPTCHA_REQUIRED
    assert status.json()["checkpoint"]["attempts"] == 1
    assert status.json()["stateVersion"] >= version
    runner.navigating = True
    shot = client.get(f"/v1/sessions/{session_id}/screenshot", headers=AUTH)
    assert shot.status_code == 503
    assert shot.headers["retry-after"] == "1"
    assert shot.headers["x-autofill-viewport"] == "1280x800"
    assert shot.headers["x-autofill-status"] == Status.CAPTCHA_REQUIRED
    again = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
    assert again.json()["status"] == Status.CAPTCHA_REQUIRED


def test_interact_allows_the_new_statuses_and_a_higher_rate(api):
    client, _runner = api
    created = client.post("/v1/sessions", headers=AUTH, json=_body("0.7.0", "https://jobs.example.test/otp"))
    session_id = created.json()["sessionId"]
    client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
    typed = client.post(
        f"/v1/sessions/{session_id}/interact",
        headers=AUTH,
        json={"action": "key", "key": "Shift+Tab", "candidateRef": "ref-a"},
    )
    assert typed.status_code == 200, typed.text
    assert "stateVersion" in typed.json()
    clicked = client.post(
        f"/v1/sessions/{session_id}/interact",
        headers=AUTH,
        json={"action": "click", "x": 10, "y": 10, "clickCount": 2, "candidateRef": "ref-a"},
    )
    assert clicked.status_code == 200, clicked.text
    for _ in range(58):
        assert (
            client.post(
                f"/v1/sessions/{session_id}/interact",
                headers=AUTH,
                json={"action": "key", "key": "Delete", "candidateRef": "ref-a"},
            ).status_code
            == 200
        )
    limited = client.post(
        f"/v1/sessions/{session_id}/interact",
        headers=AUTH,
        json={"action": "scroll", "dy": 10, "dx": 4, "candidateRef": "ref-a"},
    )
    assert limited.status_code == 429


def test_openapi_has_resume_file_and_no_submit_path(api):
    client, _runner = api
    schema = client.get("/openapi.json").json()
    paths = "".join(schema["paths"])
    assert "submit" not in paths
    assert "/v1/sessions/{session_id}/resume-file" in schema["paths"]
    assert "/v1/sessions/{session_id}/resume" in schema["paths"]


def test_local_fixtures_drive_account_verification_and_resume(caplog, fixture_origin, resolver_origin):
    """The engine reads only the local pages. No employer account is created."""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from autofill.service.runner import PlaywrightRunner

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:
        pytest.skip(f"Chromium is not installed: {exc}")

    caplog.set_level(logging.INFO)
    runner = PlaywrightRunner(enabled=True, headless=True, workers=1)
    app = create_app(token=TOKEN, runner=runner, run_browser=True)
    strong = "Correct-Horse-1!"
    with TestClient(app) as client:
        register = _start(client, resolver_origin, f"{fixture_origin}/account_register.html")
        assert register["status"] == Status.ACCOUNT_CREATION_REQUIRED
        assert register["account"]["mode"] == "register"
        assert register["account"]["passwordPolicy"]["minLength"] == 8
        session_id = register["sessionId"]
        rejected = _credentials(client, session_id, mode="register", password=WEAK)
        assert rejected.status_code == 422, rejected.text
        assert WEAK not in rejected.text
        assert WEAK not in caplog.text
        assert strong not in caplog.text
        accepted = _credentials(client, session_id, mode="register", password=strong)
        assert accepted.status_code == 200, accepted.text
        assert strong not in accepted.text
        assert strong not in caplog.text
        assert accepted.json()["outcome"] == "verification_required"

        sign_in = _start(client, resolver_origin, f"{fixture_origin}/account_signin.html")
        assert sign_in["status"] == Status.LOGIN_REQUIRED
        assert sign_in["account"]["alternateModeAvailable"] is True
        switched = _credentials(client, sign_in["sessionId"], mode="register", password=strong)
        assert switched.status_code == 200, switched.text
        assert switched.json()["outcome"] == "verification_required"
        assert strong not in switched.text

        otp = _start(client, resolver_origin, f"{fixture_origin}/account_otp.html")
        assert otp["status"] == Status.EMAIL_VERIFICATION_REQUIRED
        assert otp["blocking"]["kind"] == "VERIFICATION"
        pending = client.post(
            f"/v1/sessions/{otp['sessionId']}/continue",
            headers=AUTH,
            json={"candidateRef": "ref-a", "humanResolved": True},
        )
        assert pending.status_code == 409
        assert pending.json()["detail"]["reason"] == "verification_pending"
        assert client.get(f"/v1/sessions/{otp['sessionId']}/status", headers=AUTH).json()["status"] == (
            Status.EMAIL_VERIFICATION_REQUIRED
        )

        captcha = _start(client, resolver_origin, f"{fixture_origin}/captcha_placeholder.html")
        assert captcha["status"] == Status.CAPTCHA_REQUIRED
        assert captcha["progress"]["stage"] == "CAPTCHA"
        still = client.post(
            f"/v1/sessions/{captcha['sessionId']}/continue",
            headers=AUTH,
            json={"candidateRef": "ref-a", "humanResolved": True},
        )
        assert still.status_code == 409
        assert still.json()["detail"]["reason"] == "challenge_present"

        uploaded = _start(client, resolver_origin, f"{fixture_origin}/resume_accept.html")
        assert uploaded["status"] == Status.RESUME_UPLOAD_REQUIRED
        pdf = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"
        saved = client.post(
            f"/v1/sessions/{uploaded['sessionId']}/resume-file",
            headers=AUTH,
            data={"candidateRef": "ref-a"},
            files={"file": ("notes.pdf", pdf, "application/pdf")},
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["verified"] is True
        assert saved.json()["filename"] == "notes.pdf"

        rejected_file = _start(client, resolver_origin, f"{fixture_origin}/resume_reject.html")
        assert rejected_file["status"] == Status.RESUME_UPLOAD_REQUIRED
        failed = client.post(
            f"/v1/sessions/{rejected_file['sessionId']}/resume-file",
            headers=AUTH,
            data={"candidateRef": "ref-a"},
            files={"file": ("notes.pdf", pdf, "application/pdf")},
        )
        assert failed.status_code == 422, failed.text
        assert failed.json()["reason"] == "ats_rejected"
        stayed = client.get(f"/v1/sessions/{rejected_file['sessionId']}/status", headers=AUTH)
        assert stayed.json()["status"] == Status.RESUME_UPLOAD_REQUIRED
        assert stayed.json()["resume"]["verified"] is False


def _start(client: TestClient, resolver_origin: str, url: str) -> dict:
    payload = _body("0.7.0", url)
    payload["resolver"]["url"] = f"{resolver_origin}/resolve"
    created = client.post("/v1/sessions", headers=AUTH, json=payload)
    assert created.status_code == 201, created.text
    session_id = created.json()["sessionId"]
    started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
    assert started.status_code == 200, started.text
    return started.json()


def _credentials(client: TestClient, session_id: str, *, mode: str, password: str):
    return client.post(
        f"/v1/sessions/{session_id}/credentials",
        headers=AUTH,
        json={
            "candidateRef": "ref-a",
            "email": "person@example.com",
            "password": password,
            "confirmPassword": password,
            "mode": mode,
        },
    )


@pytest.fixture
def fixture_origin():
    import threading
    from functools import partial
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, format, *args):
            return

    handler = partial(Quiet, directory=str(Path(__file__).parent / "fixtures"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()


@pytest.fixture
def resolver_origin():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length:
                self.rfile.read(length)
            body = json.dumps({"fields": {}, "answers": [], "unresolved": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
