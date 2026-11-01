"""Standard terms / privacy acknowledgements: wording, verified click, no press over consent.

Regression for t21u / t23 (2026-10-26). Aptiv's Workday Create Account box reads
"Yes, I have read the Applicant Privacy Policy". The engine refused that wording,
left the required box unchecked, and still pressed Create Account; Workday answered
"Please check the box to continue" (422) and the employee had to tick the box and
press Continue by hand.

The vectors below are shared with vinayaka
``lib/autofill-integration/__tests__/autoConsentScope.test.ts`` so the Companion
labels exactly what the engine accepts. Synthetic fixtures only: every request is
answered from tests/fixtures by a Playwright route; no employer site is reached.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from autofill.account_flow import check_registered, run_account_action
from autofill.mapping import bind_standard_terms, consume_auto_consents, terms_category
from autofill.models import Control
from autofill.resolver import ResolverBinding
from autofill.stepfill import fill_resolved_page

FIXTURES = Path(__file__).parent / "fixtures"
EMAIL = "test.candidate@example.com"
SECRET = "Fixture-Pass-9!"

# Standard required ToS / Privacy wordings seen on real tenants (or their plain variants).
ACCEPT_VECTORS = [
    "Yes, I have read the Applicant Privacy Policy",  # Aptiv (Workday), Tarun screenshot t23ua0
    "I understand and acknowledge the terms of use for Faith Technologies Incorporated (FTI).",  # Faith
    "Yes, I have read and consent to the terms and conditions",  # Nordson
    "I have read the Privacy Policy",
    "I acknowledge that I have read and understand the Privacy Notice",
    "I have read and accept the Data Privacy Notice",
    "I have read and agree to the Global Data Privacy Notice",
    "I acknowledge that I have read the Candidate Privacy Statement",
    "I agree to the terms",
    "I agree to the terms and privacy policy",
    "I have read and understood the Applicant Privacy Policy",
    "I agree to the Terms & Conditions",
]

# Must stay CONSENT (manual): marketing, SMS, AI recording, arbitration, e-sign,
# background check, certify/attest, demographic, unusual or unknown documents.
DENY_VECTORS = [
    "I have read the privacy policy and agree to receive marketing emails",
    "I consent to receive SMS text messages about my application",
    "I agree to the use of AI notetakers to record my interviews",
    "I agree to the terms of arbitration",
    "I consent to the terms and conditions and to binding arbitration",
    "I have read the privacy policy and agree to e-sign my documents",
    "I agree to the terms and conditions of the background check",
    "I certify that I have read the privacy policy",
    "I attest that the information provided is true",
    "I agree to share my demographic information",
    "I agree to the terms of employment",
    "I agree",
]


def _box(label: str) -> Control:
    return Control(kind="checkbox", label=label, selector="#terms", required=True)


@pytest.mark.parametrize("label", ACCEPT_VECTORS)
def test_standard_wording_is_accepted(label):
    assert terms_category(_box(label)) == "STANDARD_TERMS"


@pytest.mark.parametrize("label", DENY_VECTORS)
def test_deny_wording_stays_manual(label):
    assert terms_category(_box(label)) == "CONSENT"


# --- browser fixtures ---------------------------------------------------------


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
def site(browser):
    contexts = []

    def open_page(name: str, url: str, *, label: str | None = None, replace: str | None = None):
        html = (FIXTURES / name).read_text()
        if label is not None:
            html = html.replace(replace or "__TERMS__", label)
        context = browser.new_context()
        contexts.append(context)
        host = url.split("/")[2]

        def handle(route):
            if route.request.url.startswith(f"https://{host}/"):
                route.fulfill(status=200, body=html, content_type="text/html")
            else:
                route.abort()

        context.route("**/*", handle)
        page = context.new_page()
        page.goto(url)
        return page

    try:
        yield open_page
    finally:
        for context in contexts:
            context.close()


def _register(page, ats: str | None) -> dict:
    return run_account_action(
        page,
        mode="register",
        email=EMAIL,
        username="",
        password=SECRET,
        confirm=SECRET,
        ats=ats,
        standard_terms=True,
    )


WORKDAY_URL = "https://example.wd5.myworkdayjobs.com/en-US/Careers/job/Example-Engineer_R2/apply/applyManually"
WORKDAY_LABEL = "I understand and acknowledge the terms of use for Example Technologies Incorporated (ETI)."
GENERIC_URL = "https://careers.example.org/candidate/account/register"


# --- Workday Create Account (Aptiv / Faith / Nordson / privacy variants) -------


@pytest.mark.parametrize(
    "label",
    [
        "Yes, I have read the Applicant Privacy Policy",
        "I understand and acknowledge the terms of use for Faith Technologies Incorporated (FTI).",
        "Yes, I have read and consent to the terms and conditions",
        "I acknowledge that I have read and understand the Privacy Notice",
        "I have read and accept the Data Privacy Notice",
    ],
)
def test_authorized_workday_register_checks_box_and_creates_account(site, label):
    page = site("workday_terms_react.html", WORKDAY_URL, label=label, replace=WORKDAY_LABEL)
    result = _register(page, "workday")
    assert page.evaluate("window.__rejected") == 0, result
    assert page.evaluate("window.__created") == 1
    assert page.evaluate("window.__accepted") is True  # the site's own state, not the paint
    assert result["ok"] is True
    assert result["status"] == "EMAIL_VERIFICATION_REQUIRED"
    assert result["consentRows"] == [{"textExcerpt": label, "category": "STANDARD_TERMS", "checked": True}]
    assert result["autoConsents"][0]["category"] == "STANDARD_TERMS"
    assert SECRET not in str(result)


@pytest.mark.parametrize(
    "label",
    [
        "I agree to receive marketing emails and job alerts from Example Co",
        "I consent to receive SMS text messages",
        "I agree to binding arbitration of any dispute",
        "I agree to the use of AI to record and analyse my interviews",
        "I consent to sign documents with an electronic signature",
        "I authorize a background check",
        "I certify that the information I provide is true",
    ],
)
def test_workday_deny_wording_is_not_checked_and_create_account_is_not_pressed(site, label):
    page = site("workday_terms_react.html", WORKDAY_URL, label=label, replace=WORKDAY_LABEL)
    result = _register(page, "workday")
    assert page.evaluate("window.__created") == 0
    assert page.evaluate("window.__rejected") == 0  # the button was never pressed
    assert page.locator("#input-7").is_checked() is False
    assert result["ok"] is False
    assert result["reason"] == "site_rejected"
    assert result["status"] == "ACCOUNT_CREATION_REQUIRED"
    assert result["siteErrors"] == [f"Consent needs a person: {label}"]
    assert result["consentRows"] == [{"textExcerpt": label, "category": "CONSENT", "checked": False}]
    assert result.get("autoConsents") == []
    assert SECRET not in str(result)


def test_box_the_person_already_checked_lets_create_account_proceed(site):
    label = "I agree to receive marketing emails and job alerts from Example Co"
    page = site("workday_terms_react.html", WORKDAY_URL, label=label, replace=WORKDAY_LABEL)
    page.locator("#input-7").click()  # the person ticked it in the live preview
    result = _register(page, "workday")
    assert page.evaluate("window.__created") == 1
    assert result["ok"] is True
    assert result["consentRows"][0] == {"textExcerpt": label, "category": "CONSENT", "checked": True}
    assert result.get("autoConsents") == []


# --- Generic (non-Workday) account page ------------------------------------------


@pytest.mark.parametrize(
    "label",
    ["I agree to the terms and privacy policy", "Yes, I have read the Applicant Privacy Policy"],
)
def test_generic_account_page_checks_covered_terms_box_and_creates_account(site, label):
    page = site("account_register_terms.html", GENERIC_URL, label=label)
    result = _register(page, None)
    assert page.evaluate("window.__rejected") == 0, result
    assert page.evaluate("window.__created") == 1
    assert page.evaluate("window.__accepted") is True
    assert result["ok"] is True
    rows = {row["textExcerpt"]: row for row in result["consentRows"]}
    assert rows[label]["checked"] is True
    assert rows[label]["category"] == "STANDARD_TERMS"
    marketing = next(row for text, row in rows.items() if "marketing" in text)
    assert marketing["checked"] is False
    assert page.evaluate("document.querySelector('#news') === null") is True  # form is gone after create


def test_generic_account_page_deny_wording_is_not_pressed(site):
    label = "I agree to the terms and conditions and to binding arbitration"
    page = site("account_register_terms.html", GENERIC_URL, label=label)
    result = _register(page, None)
    assert page.evaluate("window.__created") == 0
    assert page.evaluate("window.__rejected") == 0
    assert page.locator("#terms").is_checked() is False
    assert page.locator("#news").is_checked() is False
    assert result["reason"] == "site_rejected"
    assert result["siteErrors"] == [f"Consent needs a person: {label}"]


# --- check_registered must not trust an unreadable state (RC-4) -----------------


def test_unreadable_checkbox_state_is_not_counted_as_checked():
    calls: list[str] = []

    class Locator:
        def check(self, **_kwargs) -> None:
            calls.append("check")

    class Page:
        def locator(self, _selector: str) -> Locator:
            return Locator()

    assert check_registered(Page(), "#terms") is False
    assert calls  # it tried, but nothing proved the page kept the check


# --- Application step (Greenhouse / Lever / Ashby shape) -------------------------


def _resolver():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length:
                self.rfile.read(length)
            body = json.dumps({"fields": {}, "answers": []}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address
    binding = ResolverBinding(
        url=f"http://{host}:{port}/resolve",
        token="synthetic-resolver-token-0123456789abcdef",
        expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
        candidate_ref="ref-a",
        job_ref="job-a",
    )
    return server, binding


@pytest.mark.parametrize(
    ("ats", "url"),
    [
        ("greenhouse", "https://job-boards.greenhouse.io/example/jobs/1"),
        ("lever", "https://jobs.lever.co/example/1/apply"),
        ("ashby", "https://jobs.ashbyhq.com/example/1/application"),
    ],
)
def test_application_step_privacy_ack_is_checked_and_sms_stays_manual(site, ats, url):
    label = "Yes, I have read the Applicant Privacy Policy"
    page = site("application_privacy_ack.html", url, label=label)
    server, binding = _resolver()
    bind_standard_terms(True)
    try:
        outcome = fill_resolved_page(page, binding, session_id="sess-terms", ats_name=ats, step="questions")
        recorded = consume_auto_consents()
    finally:
        bind_standard_terms(False)
        server.shutdown()
    assert page.locator("#privacy_ack").is_checked() is True
    assert page.evaluate("window.__privacy") is True  # the form saw the change
    assert page.locator("#sms_ack").is_checked() is False
    assert page.locator("#news").is_checked() is False
    texts = " ".join(str(item.get("text") or "") for item in outcome.manual_questions)
    assert "privacy policy" not in texts.casefold()
    assert "sms" in texts.casefold()
    assert [entry["category"] for entry in recorded] == ["STANDARD_TERMS"]


def test_application_step_without_authorization_checks_nothing(site):
    label = "Yes, I have read the Applicant Privacy Policy"
    page = site("application_privacy_ack.html", "https://job-boards.greenhouse.io/example/jobs/1", label=label)
    server, binding = _resolver()
    bind_standard_terms(False)
    try:
        outcome = fill_resolved_page(page, binding, session_id="sess-terms", ats_name="greenhouse", step="questions")
    finally:
        server.shutdown()
    assert page.locator("#privacy_ack").is_checked() is False
    texts = " ".join(str(item.get("text") or "") for item in outcome.manual_questions)
    assert "privacy policy" in texts.casefold()
