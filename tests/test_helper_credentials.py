"""Direct-browser sign-in: tenant scope, policy refusal, and a memory-only secret.

Synthetic fixtures only. No employer site is contacted and no real account is
created. Chrome itself is not launched; cookie and preference checks use fakes
and the profile directory on disk.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from autofill.account_flow import run_account_action
from autofill.credentials import Credentials, MemoryCredentialProvider, SiteContext, credential_scope
from autofill.engine import ApplicationResult, Status
from autofill.extract import extract_page
from autofill.local_attach import (
    ENV_FLAG,
    clear_employer_cookies,
    dedicated_launch_args,
    disarm_password_manager,
)
from autofill.local_helper import LocalHelper, serve
from autofill.login import password_meets_policy, password_policy, run_login
from autofill.profile import CandidateProfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
PROFILE = ROOT / "examples" / "profile.example.json"
HOST = "example.wd1.myworkdayjobs.com"
URL = f"https://{HOST}/en-US/External/job/Example-Analyst_R1/apply/applyManually"
EMAIL = "test.candidate@example.com"
CANDIDATE = "ref-a"


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {"candidateId": CANDIDATE, "personal": {"full_name": "Test Candidate", "email": EMAIL}}
    )


def _generate(policy: dict, email: str) -> str:
    """Stand-in for vinayaka's pool. The product does not generate passwords."""
    minimum = int(policy.get("minLength") or 8)
    secret = ("Aa1!" * ((minimum // 4) + 2))[: max(minimum, 8)]
    assert password_meets_policy(secret, policy, email) is None
    return secret


class _Jar:
    def __init__(self, cookies: list[dict]) -> None:
        self.cookies_now = list(cookies)
        self.cleared = 0

    def cookies(self) -> list[dict]:
        return list(self.cookies_now)

    def clear_cookies(self) -> None:
        self.cleared += 1
        self.cookies_now = []

    def add_cookies(self, cookies: list[dict]) -> None:
        self.cookies_now.extend(cookies)


class _RecordingSession:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.started = threading.Event()
        self.release = threading.Event()
        self.owns_chrome = False
        self.tab = None
        self.detached = 0

    def fill(
        self,
        url: str,
        profile: object,
        *,
        cancel: object = None,
        fresh: bool = False,
        resume: object = None,
        session_id: str = "",
        candidate_id: str = "",
        job_id: str = "",
        on_observed: object = None,
        on_nav_block: object = None,
        nav_block: object = None,
        credential_provider: object = None,
        verify_pending: bool = False,
        protocol_version: str = "",
    ) -> tuple[ApplicationResult, bool]:
        self.calls.append(
            {
                "provider": credential_provider,
                "verify_pending": verify_pending,
                "protocol": protocol_version,
                "fresh": fresh,
            }
        )
        if len(self.calls) == 1:
            self.started.set()
            assert self.release.wait(timeout=5)
        result = ApplicationResult(
            status=Status.LOGIN_REQUIRED,
            ats="workday",
            current_step="CHECK_AUTH",
            login_status="LOGIN_REQUIRED",
            stopped_before_submit=True,
            account={"mode": "login", "tenant": {"tenantKey": "kept"}, "passwordPolicy": {"minLength": 8}},
        )
        return result, False

    def detach(self) -> None:
        self.detached += 1

    def stop_launched_chrome(self) -> None:
        return None

    def browser_running(self) -> bool:
        return False

    def sign_out(self, url: str) -> int:
        return clear_employer_cookies(self.tab.context, url) if self.tab is not None else 0


def _request(port: int, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    import urllib.error
    import urllib.request

    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_scope_is_the_employer_not_the_bare_host():
    acme = "https://wd5.myworkdayjobs.com/en-US/Acme/job/1"
    other = "https://wd5.myworkdayjobs.com/en-US/Other/job/1"
    assert credential_scope(acme) == "wd5.myworkdayjobs.com/Acme"
    assert credential_scope(other) == "wd5.myworkdayjobs.com/Other"
    assert credential_scope(acme) != "wd5.myworkdayjobs.com"
    greenhouse = "https://boards.greenhouse.io/example/jobs/1"
    assert credential_scope(greenhouse) == "boards.greenhouse.io/example"
    store = MemoryCredentialProvider()
    secret = "Pool-Pass-14c!"
    store.put(CANDIDATE, credential_scope(acme), Credentials(site=credential_scope(acme), email=EMAIL, password=secret))
    assert store.get_credentials(_site(other, CANDIDATE)) is None
    found = store.get_credentials(_site(acme, CANDIDATE))
    assert found is not None and found.password == secret
    assert secret not in repr(found)


def _site(url: str, candidate: str) -> SiteContext:
    return SiteContext(url=url, domain=credential_scope(url), candidate_id=candidate)


def test_password_policy_tokens_do_not_include_the_secret():
    policy = {
        "minLength": 8,
        "requireUpper": True,
        "requireLower": True,
        "requireDigit": True,
        "requireSymbol": True,
        "disallowEmail": True,
    }
    assert password_meets_policy("short", policy, EMAIL) == "password_policy"
    assert password_meets_policy("longpassword", policy, EMAIL) == "password_policy"
    assert password_meets_policy(EMAIL + "Aa1!", policy, EMAIL) == "password_policy"
    good = _generate(policy, EMAIL)
    assert password_meets_policy(good, policy, EMAIL) is None
    assert password_meets_policy(good, None, EMAIL) is None


def test_launch_args_and_prefs_disable_the_password_manager(tmp_path):
    profile = tmp_path / "apply-profile"
    disarm_password_manager(profile)
    prefs = json.loads((profile / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert prefs["credentials_enable_service"] is False
    assert prefs["profile"]["password_manager_enabled"] is False
    args = dedicated_launch_args(
        executable="/usr/bin/google-chrome-stable",
        user_data_dir=profile,
        port=9222,
        start_url="about:blank",
    )
    assert "--disable-save-password-bubble" in args
    assert "--disable-features=PasswordManagerOnboarding" in args
    secret = "Pool-Pass-14c!"
    blob = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in profile.rglob("*") if path.is_file())
    assert secret not in blob
    assert not (profile / "Default" / "Login Data").exists()


def test_candidate_change_clears_only_that_employer():
    jar = _Jar(
        [
            {"name": "sid", "value": "one", "domain": HOST, "path": "/"},
            {"name": "other", "value": "keep", "domain": "boards.greenhouse.io", "path": "/"},
        ]
    )
    dropped = clear_employer_cookies(jar, URL)
    assert dropped == 1
    assert jar.cookies_now == [{"name": "other", "value": "keep", "domain": "boards.greenhouse.io", "path": "/"}]


def test_store_and_continue_keep_the_secret_out_of_status(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv(ENV_FLAG, "1")
    caplog.set_level(logging.INFO)
    secret = "Pool-Pass-14c!"
    disarm_password_manager(tmp_path / "apply-profile")
    fake = _RecordingSession()
    helper = LocalHelper(session=fake, idle_detach_seconds=-1, user_data_dir=tmp_path / "apply-profile")  # type: ignore[arg-type]
    server = serve(port=0, helper=helper)
    port = int(server.server_address[1])
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    try:
        status, created = _request(
            port,
            "POST",
            "/v1/sessions",
            {"applicationUrl": "https://boards.greenhouse.io/example/jobs/1", "profile": profile},
        )
        assert status == 202
        assert fake.started.wait(timeout=5)
        fake.release.set()
        session_id = created["sessionId"]
        body: dict = {}
        for _ in range(50):
            code, body = _request(port, "GET", f"/v1/sessions/{session_id}")
            if body.get("status") == "LOGIN_REQUIRED":
                break
            threading.Event().wait(0.05)
        assert code == 200
        assert fake.calls[0]["protocol"] == "0.7.0"
        assert fake.calls[0]["provider"] is helper._credentials
        stored, stored_body = _request(
            port,
            "POST",
            f"/v1/sessions/{session_id}/credentials",
            {
                "candidateId": CANDIDATE,
                "email": EMAIL,
                "password": secret,
                "confirmPassword": secret,
                "mode": "store",
            },
        )
        assert stored == 200
        assert stored_body["stored"] is True
        assert stored_body["tenantKey"] == "boards.greenhouse.io/example"
        encoded = json.dumps(stored_body)
        assert secret not in encoded
        assert secret not in caplog.text
        again, public = _request(port, "GET", f"/v1/sessions/{session_id}")
        assert again == 200
        assert secret not in json.dumps(public)
        account = (public.get("fill") or {}).get("account") or {}
        assert "password" not in account
        assert "secret" not in account
        job = helper.get(session_id)
        assert job is not None
        job.verify_pending = True
        job.state = "error"
        job.error = "Error"
        continued, continued_body = _request(
            port,
            "POST",
            f"/v1/sessions/{session_id}/continue",
            {"verified": False},
        )
        assert continued == 200, continued_body
        assert fake.calls[-1]["provider"] is helper._credentials
        assert fake.calls[-1]["verify_pending"] is True
        assert secret not in json.dumps(continued_body)
        job.state = "error"
        job.error = "Error"
        job.verify_pending = True
        verified, _verified_body = _request(
            port,
            "POST",
            f"/v1/sessions/{session_id}/continue",
            {"verified": True},
        )
        assert verified == 200
        assert fake.calls[-1]["verify_pending"] is False
        found = helper._credentials.get_credentials(
            _site("https://boards.greenhouse.io/example/jobs/1", CANDIDATE),
            CANDIDATE,
        )
        assert found is not None and found.password == secret
        blob = "\n".join(
            path.read_text(encoding="utf-8", errors="ignore")
            for path in (tmp_path / "apply-profile").rglob("*")
            if path.is_file()
        )
        assert secret not in blob
        assert secret not in caplog.text
    finally:
        helper.close()
        server.shutdown()


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


@pytest.fixture
def workday(browser):
    contexts = []

    def open_page(name: str):
        html = (FIXTURES / name).read_text(encoding="utf-8")
        context = browser.new_context()
        contexts.append(context)

        def handle(route):
            if route.request.url.startswith(f"https://{HOST}/"):
                route.fulfill(status=200, body=html, content_type="text/html")
            else:
                route.abort()

        context.route("**/*", handle)
        page = context.new_page()
        page.goto(URL)
        return page

    try:
        yield open_page
    finally:
        for context in contexts:
            context.close()


def test_register_rejects_a_weak_secret_before_click(workday):
    page = workday("workday_create_account.html")
    policy = password_policy(extract_page(page))
    assert policy is not None and policy.get("minLength") == 8
    result = run_account_action(
        page,
        mode="register",
        email=EMAIL,
        username="",
        password="short",
        confirm="short",
        ats="workday",
        standard_terms=False,
    )
    assert result["ok"] is False
    assert result["reason"] == "password_policy"
    assert "short" not in str(result)
    assert page.evaluate("window.__created") == 0
    assert page.locator("#input-5").input_value() == ""
    assert page.locator("#input-6").input_value() == ""


def test_generate_validate_create_save_sign_out_retrieve_sign_in(workday, caplog, tmp_path):
    caplog.set_level(logging.INFO)
    page = workday("workday_create_account.html")
    policy = password_policy(extract_page(page))
    assert policy is not None
    secret = _generate(policy, EMAIL)
    assert password_meets_policy("short", policy, EMAIL) == "password_policy"
    created = run_account_action(
        page,
        mode="register",
        email=EMAIL,
        username="",
        password=secret,
        confirm=secret,
        ats="workday",
        standard_terms=True,
    )
    assert created.get("ok") is True, created
    assert page.evaluate("window.__created") == 1
    assert secret not in str(created)
    assert secret not in caplog.text
    scope = credential_scope(URL)
    store = MemoryCredentialProvider()
    store.put(CANDIDATE, scope, Credentials(site=scope, email=EMAIL, password=secret))
    context = page.context
    context.add_cookies([{"name": "sid", "value": "employer", "domain": HOST, "path": "/"}])
    assert clear_employer_cookies(context, URL) >= 1
    assert all(cookie.get("domain", "").lstrip(".") != HOST for cookie in context.cookies())
    retrieved = store.get_credentials(_site(URL, CANDIDATE), CANDIDATE)
    assert retrieved is not None and retrieved.password == secret
    sign_in = workday("workday_sign_in.html")
    outcome, steps = run_login(sign_in, retrieved)
    assert "LOGIN" in "".join(steps) or outcome in {"AUTHENTICATED", "LOGIN_FAILED"}
    assert sign_in.evaluate("window.__signedIn") == 1
    assert sign_in.locator("#input-5").input_value() == secret
    assert secret not in caplog.text
    profile = tmp_path / "apply-profile"
    disarm_password_manager(profile)
    blob = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in profile.rglob("*") if path.is_file())
    assert secret not in blob
