"""Protocol 0.7.0 account, verification, and resume-file behavior. Synthetic fixtures only."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from autofill.account_flow import run_account_action
from autofill.credentials import Credentials, SiteContext
from autofill.engine import ApplicationResult, Status
from autofill.login import (
    SecretInputError,
    _type_secret,
    account_decision,
    password_policy,
    score_account,
    tenant_identity,
    verification_present,
)
from autofill.models import ButtonControl, Control, JobContext, LinkControl, PageSnapshot
from autofill.profile import CandidateProfile
from autofill.redact import redaction_filter
from autofill.safeguards import HumanSubmissionRequired, activate_account
from autofill.service.app import create_app
from autofill.service.runner import PageUnavailable
from autofill.service.schemas import CredentialsIn
from autofill.service.sessions import ServiceSession

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
        progress = None
        if "account" in request.application_url:
            status = Status.ACCOUNT_CREATION_REQUIRED
        if "otp" in request.application_url:
            status = Status.EMAIL_VERIFICATION_REQUIRED
        if "captcha" in request.application_url:
            status = Status.CAPTCHA_REQUIRED
        if "no-form" in request.application_url:
            status = Status.NO_FORM_FOUND
            progress = {
                "stage": "QUESTIONS",
                "requiredTotal": 0,
                "requiredDone": 0,
                "percent": 75,
                "currentLabel": "Questions",
            }
        return ApplicationResult(
            status=status,
            ats="greenhouse",
            current_step=status,
            login_status="NOT_REQUIRED",
            session_id=request.session_id,
            candidate_id=request.candidate_id,
            job_id=request.job_id,
            progress=progress or {},
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

    def attach_resume_file(self, session_id: str, name: str, data: bytes, hint: str = "", confirmed=None) -> dict:
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
            page_text="We use cookies. Accept cookies to continue.",
            password_hint=(
                "Use at least 8 characters, one uppercase letter, one lowercase letter, "
                "one digit, and one symbol. Do not include your email."
            ),
        )
    )
    assert policy is not None
    assert policy["minLength"] == 8
    assert policy["requireUpper"] is True
    assert policy["requireLower"] is True
    assert policy["requireDigit"] is True
    assert policy["requireSymbol"] is True
    assert policy["disallowEmail"] is True
    assert "cookie" not in policy["raw"].casefold()
    assert len(policy["raw"]) <= 300
    variants = password_policy(
        _snapshot(
            controls=[_control("password", "Password")],
            password_present=True,
            page_text="We use cookies. Accept cookies to continue.",
            password_hint=(
                "Include an uppercase alphabetic character, a lowercase alphabetic character, "
                "a numeric character, a numeral, and a character from 0-9, plus a special character."
            ),
        )
    )
    assert variants is not None
    assert variants["requireDigit"] is True
    assert variants["requireSymbol"] is True
    assert variants["requireUpper"] is True
    assert variants["requireLower"] is True
    assert "cookie" not in variants["raw"].casefold()
    alphabetic = password_policy(
        _snapshot(
            controls=[_control("password", "Password")],
            password_present=True,
            page_text="We use cookies.",
            password_hint="Use an alphabetic character.",
        )
    )
    assert alphabetic is not None
    assert alphabetic["requireUpper"] is True
    assert alphabetic["requireLower"] is True
    assert "cookie" not in alphabetic["raw"]
    tenant = tenant_identity("https://example.wd5.myworkdayjobs.com/External/job/1", "workday")
    assert tenant["tenantKey"] == "example.wd5.myworkdayjobs.com/External"
    assert tenant["loginOrigin"] == "https://example.wd5.myworkdayjobs.com"
    locale = tenant_identity(
        "https://hp.wd5.myworkdayjobs.com/en-US/ExternalCareerSite/job/1",
        "workday",
    )
    assert locale["tenantKey"] == "hp.wd5.myworkdayjobs.com/ExternalCareerSite"
    short = tenant_identity("https://hp.wd5.myworkdayjobs.com/en/ExternalCareerSite/job/1", "workday")
    assert short["tenantKey"] == "hp.wd5.myworkdayjobs.com/ExternalCareerSite"
    assert tenant_identity("https://jobs.lever.co/example/job", "lever")["tenantKey"] == "example"


def test_submit_application_is_not_an_account_button():
    with pytest.raises(HumanSubmissionRequired):
        activate_account(None, "#submit", "Submit Application")


def test_credentials_repr_and_logs_omit_the_secret():
    body = CredentialsIn(email="person@example.com", password=SECRET, mode="store")
    rendered = repr(body)
    assert SECRET not in rendered
    assert "password=" not in rendered


def test_failed_password_fill_drops_the_value_from_the_error():
    import traceback

    secret = "SuperSecretValue99"

    class Locator:
        def fill(self, value: str) -> None:
            raise RuntimeError(f'fill("{value}")')

    class Page:
        def locator(self, selector: str) -> Locator:
            return Locator()

    try:
        _type_secret(Page(), "#password", secret)
    except SecretInputError:
        rendered = traceback.format_exc()
    else:
        raise AssertionError("the fill failure should surface as SecretInputError")
    assert secret not in rendered
    assert "RuntimeError" in rendered

    snapshot = {
        "controls": [
            {"kind": "text", "label": "Email", "selector": "#email", "inputType": "email"},
            {"kind": "password", "label": "Password", "selector": "#password", "inputType": "password"},
        ],
        "buttons": [{"name": "Sign In", "selector": "#go", "controlType": "submit"}],
        "password": True,
        "heading": "Sign in",
    }

    class FormPage:
        url = "https://boards.greenhouse.io/example/login"

        def evaluate(self, script, arg=None):
            return snapshot

        def locator(self, selector: str) -> Locator:
            return Locator()

    report = run_account_action(
        FormPage(),
        mode="login",
        email="person@example.com",
        username="",
        password=secret,
        confirm=secret,
        ats="greenhouse",
    )
    assert report == {"ok": False, "reason": "input_failed"}
    assert secret not in str(report)


def test_wipe_forgets_the_password_and_deletes_the_resume_file(tmp_path):
    secret = "SuperSecretValue99"
    profile = CandidateProfile.from_dict(
        {"candidateId": "ref-a", "personal": {"full_name": "Synthetic Candidate", "email": "synthetic@example.com"}}
    )
    session = ServiceSession(
        session_id="session-a",
        candidate_id="ref-a",
        job_id="job-a",
        application_url="https://boards.greenhouse.io/example",
        profile=profile,
        job=JobContext(title="Example", company="Example Labs", url="https://boards.greenhouse.io/example"),
        cover_letter_text=None,
    )
    session.credentials.put(
        "ref-a",
        "boards.greenhouse.io",
        Credentials(site="boards.greenhouse.io", email="person@example.com", password=secret),
    )
    session.remember_secret(secret)
    path = tmp_path / "slot" / "notes.pdf"
    path.parent.mkdir()
    path.write_bytes(b"%PDF-1.4\n")
    session.resume_temp = str(path)
    session.resume_bytes = b"%PDF-1.4\n"
    assert secret not in redaction_filter().scrub(secret)
    session.wipe_secrets()
    found = session.credentials.get_credentials(
        SiteContext(url="https://boards.greenhouse.io/example", domain="boards.greenhouse.io", candidate_id="ref-a")
    )
    assert found is None
    assert session.secret_values == []
    assert session.resume_bytes is None
    assert session.resume_temp == ""
    assert not path.exists()
    assert secret not in redaction_filter()._secrets


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
    assert too_big.json()["detail"]["reason"] == "too_large"
    wrong = client.post(
        f"/v1/sessions/{session_id}/resume-file",
        headers=AUTH,
        data={"candidateRef": "ref-a"},
        files={"file": ("notes.pdf", b"not-a-pdf", "application/pdf")},
    )
    assert wrong.status_code == 415
    assert wrong.json()["detail"]["reason"] == "unsupported_type"
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
    since = status.json()["checkpoint"]["since"]
    assert isinstance(since, str)
    assert len(since) == 20 and since.endswith("Z") and "T" in since
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
    scrolled = client.post(
        f"/v1/sessions/{session_id}/interact",
        headers=AUTH,
        json={"action": "scroll", "dy": 10, "dx": 4, "candidateRef": "ref-a"},
    )
    assert scrolled.status_code == 200, scrolled.text
    limited = client.post(
        f"/v1/sessions/{session_id}/interact",
        headers=AUTH,
        json={"action": "key", "key": "End", "candidateRef": "ref-a"},
    )
    assert limited.status_code == 429


def test_openapi_has_resume_file_and_no_submit_path(api):
    client, _runner = api
    schema = client.get("/openapi.json").json()
    paths = "".join(schema["paths"])
    assert "submit" not in paths
    assert "/v1/sessions/{session_id}/suggestions" in schema["paths"]
    assert "/v1/sessions/{session_id}/suggest" in schema["paths"]
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
        assert register["progress"]["stage"] == "ACCOUNT"
        assert register["progress"]["currentLabel"] == "Employer account"
        assert register["account"]["passwordPolicy"]["minLength"] == 8
        assert "cookie" not in register["account"]["passwordPolicy"]["raw"].casefold()
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
        assert sign_in["progress"]["stage"] == "ACCOUNT"
        assert sign_in["progress"]["currentLabel"] == "Employer account"
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


def test_sign_in_reports_the_account_stage_and_no_form_does_not_jump_to_75(api):
    client, _runner = api
    created = client.post("/v1/sessions", headers=AUTH, json=_body("0.7.0"))
    assert created.status_code == 201, created.text
    session_id = created.json()["sessionId"]
    started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
    body = started.json()
    assert body["status"] == Status.LOGIN_REQUIRED
    assert body["progress"]["stage"] == "ACCOUNT"
    assert body["progress"]["currentLabel"] == "Employer account"
    assert body["checkpoint"]["since"].endswith("Z")

    empty = client.post("/v1/sessions", headers=AUTH, json=_body("0.7.0", "https://jobs.example.test/no-form"))
    empty_id = empty.json()["sessionId"]
    missed = client.post(f"/v1/sessions/{empty_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
    assert missed.status_code == 200, missed.text
    assert missed.json()["status"] == Status.NO_FORM_FOUND
    assert missed.json()["progress"]["percent"] == 0


def test_resolver_session_ignores_candidate_context(api):
    client, _runner = api
    payload = _body("0.7.0")
    payload["candidateContext"] = {
        "candidateId": "ref-a",
        "personal": {"full_name": "Avery Example", "email": "person@example.com"},
    }
    created = client.post("/v1/sessions", headers=AUTH, json=payload)
    assert created.status_code == 201, created.text
    assert "person@example.com" not in created.text
    answered = _body("0.7.0")
    answered["approvedAnswers"] = [{"question": "Sponsor", "answer": "No"}]
    rejected = client.post("/v1/sessions", headers=AUTH, json=answered)
    assert rejected.status_code == 422


def test_entry_link_can_open_a_landing_page_and_a_submit_apply_is_left():
    from autofill.pagewait import choose_entry

    landing = _snapshot(
        buttons=[ButtonControl(name="Submit application", selector="#submit", control_type="submit")],
        links=[LinkControl(name="I'm interested", selector="#go")],
    )
    chosen = choose_entry(landing)
    assert chosen is not None
    assert chosen.selector == "#go"
    assert choose_entry(
        _snapshot(buttons=[ButtonControl(name="Apply", selector="#apply", control_type="submit")])
    ) is None
    manual = choose_entry(
        _snapshot(buttons=[ButtonControl(name="Apply Manually", selector="#manual", control_type="button")])
    )
    assert manual is not None
    assert manual.name == "Apply Manually"


def test_workday_create_account_waits_until_the_pane_settles():
    import time

    from autofill.engine import AutofillOptions, autofill_application

    shell = {
        "controls": [],
        "buttons": [{"name": "Sign In", "selector": "#sign", "controlType": "button"}],
        "captcha": False,
        "password": False,
        "heading": "",
        "pageText": "We use cookies. Accept cookies to continue.",
        "passwordHint": "",
        "links": [],
        "automationIds": [],
    }
    create = {
        "controls": [
            {"kind": "text", "label": "Email", "selector": "#email", "inputType": "email"},
            {"kind": "password", "label": "Password", "selector": "#password", "inputType": "password"},
            {
                "kind": "password",
                "label": "Verify password",
                "selector": "#verify",
                "inputType": "password",
                "ariaLabel": "Verify password",
                "automationId": "verifyPassword",
            },
        ],
        "buttons": [
            {
                "name": "Create Account",
                "selector": "#create",
                "controlType": "button",
                "automationId": "createAccountSubmitButton",
            }
        ],
        "captcha": False,
        "password": True,
        "heading": "Create Account",
        "pageText": "We use cookies. Accept cookies to continue.",
        "passwordHint": "Must include a numeric character, an uppercase letter, and a special character.",
        "links": [],
        "automationIds": ["createAccountSubmitButton", "verifyPassword"],
    }

    class Page:
        url = "https://hp.wd5.myworkdayjobs.com/en-US/ExternalCareerSite/login"

        def __init__(self) -> None:
            self.calls = 0

        def wait_for_load_state(self, state, timeout=0):
            return None

        def wait_for_timeout(self, millis):
            time.sleep(millis / 1000)

        def evaluate(self, script, arg=None):
            self.calls += 1
            if self.calls < 3:
                return shell
            return create

    page = Page()
    result = autofill_application(
        page.url,
        CandidateProfile.from_dict(
            {"candidateId": "ref-a", "personal": {"full_name": "Avery Example", "email": "a@example.com"}}
        ),
        options=AutofillOptions(page=page, protocol_version="0.7.0", session_id="session-wd", candidate_id="ref-a"),
    )
    assert result.status == Status.ACCOUNT_CREATION_REQUIRED
    assert result.account is not None
    assert result.account["mode"] == "register"
    assert result.account["tenant"]["tenantKey"] == "hp.wd5.myworkdayjobs.com/ExternalCareerSite"
    assert "cookie" not in result.account["passwordPolicy"]["raw"].casefold()
    assert result.account["passwordPolicy"]["requireDigit"] is True
    assert result.progress["stage"] == "ACCOUNT"
    assert result.progress["currentLabel"] == "Employer account"
    assert page.calls >= 3


def _start(client: TestClient, resolver_origin: str, url: str) -> dict:
    payload = _body("0.7.0", url)
    payload["resolver"]["url"] = f"{resolver_origin}/resolve"
    created = client.post("/v1/sessions", headers=AUTH, json=payload)
    assert created.status_code == 201, created.text
    session_id = created.json()["sessionId"]
    started = client.post(f"/v1/sessions/{session_id}/start", headers=AUTH, json={"candidateRef": "ref-a"})
    assert started.status_code == 200, started.text
    body = started.json()
    deadline = time.monotonic() + 40
    while body.get("status") == Status.STARTED and time.monotonic() < deadline:
        time.sleep(0.05)
        polled = client.get(f"/v1/sessions/{session_id}/status", headers=AUTH)
        assert polled.status_code == 200, polled.text
        body = polled.json()
    return body


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


def test_progress_uses_one_field_set():
    """A smaller scan must not keep an older done count or percent."""
    from autofill.service.app import _overlay_progress

    session = ServiceSession(
        session_id="sess-progress",
        candidate_id="candidate-a",
        job_id="job-a",
        application_url="https://jobs.ashbyhq.com/example/application",
        profile=CandidateProfile.from_dict(
            {"personal": {"full_name": "River Example", "email": "river.example@example.com"}}
        ),
        job=JobContext(),
        cover_letter_text=None,
        protocol_version="0.7.0",
        status=Status.MANUAL_ANSWER_REQUIRED,
        result=ApplicationResult(
            status=Status.MANUAL_ANSWER_REQUIRED,
            ats="ashby",
            current_step=Status.MANUAL_ANSWER_REQUIRED,
            login_status="NOT_REQUIRED",
        ),
    )
    session.required_done_high = 3
    session.percent_high = 30
    session.required_page_key = Status.MANUAL_ANSWER_REQUIRED
    session.required_total_mark = 10
    session.form_seen = True
    shrunk = _overlay_progress(
        session,
        {
            "stage": "QUESTIONS",
            "requiredTotal": 1,
            "requiredDone": 0,
            "percent": 0,
            "currentLabel": "Questions",
        },
    )
    assert shrunk["requiredTotal"] == 1
    assert shrunk["requiredDone"] == 0
    assert shrunk["requiredDone"] <= shrunk["requiredTotal"]
    assert shrunk["percent"] == 20
    assert shrunk["fieldPercent"] == 0
    held = _overlay_progress(
        session,
        {
            "stage": "QUESTIONS",
            "requiredTotal": 1,
            "requiredDone": 0,
            "percent": 0,
            "currentLabel": "Questions",
        },
    )
    assert held["requiredDone"] == 0
    assert held["requiredDone"] <= held["requiredTotal"]
    grown = _overlay_progress(
        session,
        {
            "stage": "QUESTIONS",
            "requiredTotal": 4,
            "requiredDone": 4,
            "percent": 100,
            "currentLabel": "Questions",
        },
    )
    assert grown["requiredDone"] == grown["requiredTotal"] == 4
    assert grown["percent"] == 80
    assert grown["fieldPercent"] == 100
    held_back = _overlay_progress(
        session,
        {
            "stage": "QUESTIONS",
            "requiredTotal": 4,
            "requiredDone": 2,
            "percent": 50,
            "currentLabel": "Questions",
        },
    )
    assert held_back["requiredTotal"] == 4
    assert held_back["requiredDone"] == 4
    assert held_back["requiredDone"] <= held_back["requiredTotal"]
    assert held_back["percent"] == 80
    assert held_back["fieldPercent"] == 100


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
