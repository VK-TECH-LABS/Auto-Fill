"""Account panes are not empty pages, and READY has one predicate."""

from __future__ import annotations

import logging

from autofill.account_flow import run_account_action
from autofill.dom_ui import progress_percent, workflow_percent
from autofill.engine import AutofillOptions, Status, autofill_application
from autofill.mapping import STANDARD_TERMS_DENY, bind_standard_terms, consume_auto_consents, terms_category
from autofill.models import ButtonControl, Control, PageSnapshot
from autofill.pagewait import classify_page, ready_to_submit, register_pane, sign_in_pane, terminal_kind
from autofill.profile import CandidateProfile
from autofill.stepfill import _accept_standard_term, _Ask


def _snapshot(**kwargs) -> PageSnapshot:
    return PageSnapshot(controls=kwargs.pop("controls", []), buttons=kwargs.pop("buttons", []), **kwargs)


def _control(kind: str, label: str, selector: str, **kwargs) -> Control:
    return Control(kind=kind, label=label, selector=selector, **kwargs)


def test_ready_predicate_rejects_each_forbidden_case():
    common = {
        "required_total": 0,
        "required_done": 0,
        "unresolved": 0,
        "blocked": "",
        "has_submit": True,
    }
    assert ready_to_submit(page_type="APPLICATION_FORM", fields_detected=0, **common) is False
    assert ready_to_submit(page_type="UNKNOWN", fields_detected=0, has_submit=False, **{
        "required_total": 0,
        "required_done": 0,
        "unresolved": 0,
        "blocked": "",
    }) is False
    assert ready_to_submit(page_type="APPLY_ENTRY", fields_detected=0, has_submit=False, **{
        "required_total": 0,
        "required_done": 0,
        "unresolved": 0,
        "blocked": "",
    }) is False
    assert ready_to_submit(page_type="APPLICATION_FORM", fields_detected=0, has_submit=False, **{
        "required_total": 0,
        "required_done": 0,
        "unresolved": 0,
        "blocked": "",
    }) is False
    assert ready_to_submit(page_type="APPLICATION_FORM", fields_detected=0, **common) is False
    assert ready_to_submit(page_type="ACCOUNT", fields_detected=3, blocked="account", **{
        "required_total": 0,
        "required_done": 0,
        "unresolved": 0,
        "has_submit": True,
    }) is False
    assert ready_to_submit(page_type="JOB_DESCRIPTION", fields_detected=0, has_submit=True, **{
        "required_total": 0,
        "required_done": 0,
        "unresolved": 0,
        "blocked": "",
    }) is False
    assert ready_to_submit(page_type="LOGIN", fields_detected=2, blocked="login", **{
        "required_total": 0,
        "required_done": 0,
        "unresolved": 0,
        "has_submit": True,
    }) is False
    assert ready_to_submit(page_type="APPLICATION_FORM", fields_detected=4, blocked="captcha", **{
        "required_total": 1,
        "required_done": 1,
        "unresolved": 0,
        "has_submit": True,
    }) is False
    assert ready_to_submit(page_type="APPLICATION_FORM", fields_detected=2, blocked="resume", **{
        "required_total": 1,
        "required_done": 1,
        "unresolved": 0,
        "has_submit": True,
    }) is False
    assert ready_to_submit(
        page_type="APPLICATION_FORM",
        fields_detected=2,
        required_total=2,
        required_done=2,
        unresolved=0,
        blocked="",
        has_submit=True,
    ) is True
    assert ready_to_submit(
        page_type="REVIEW",
        fields_detected=1,
        required_total=1,
        required_done=1,
        unresolved=0,
        blocked="",
        has_submit=True,
    ) is True
    assert progress_percent("READY", 0, 0) == 0


def test_workflow_percent_is_separate_from_the_field_ratio():
    assert workflow_percent("JOB_DESCRIPTION", "JOB_DESCRIPTION", 0, 0) == 5
    assert workflow_percent("LOGIN_REQUIRED", "LOGIN", 0, 0) == 10
    assert workflow_percent("ACCOUNT_CREATION_REQUIRED", "ACCOUNT", 0, 0) == 15
    assert workflow_percent("MANUAL_ANSWER_REQUIRED", "APPLICATION_FORM", 0, 0) == 20
    assert workflow_percent("MANUAL_ANSWER_REQUIRED", "APPLICATION_FORM", 4, 4) == 80
    assert workflow_percent("MANUAL_ANSWER_REQUIRED", "APPLICATION_FORM", 4, 2) == 50
    assert workflow_percent("RESUME_UPLOAD_REQUIRED", "APPLICATION_FORM", 0, 0) == 80
    assert workflow_percent("RESUME_UPLOAD_REQUIRED", "APPLICATION_FORM", 4, 4) == 85
    assert workflow_percent("READY_FOR_HUMAN_SUBMIT", "REVIEW", 4, 4) == 100
    assert workflow_percent("NO_FORM_FOUND", "UNKNOWN", 0, 0) == 0
    assert workflow_percent("CAPTCHA_REQUIRED", "UNKNOWN", 0, 0) == 12
    assert progress_percent("QUESTIONS", 4, 4) == 100
    assert workflow_percent("LOGIN_REQUIRED", "ACCOUNT", 0, 0) == 10


def test_create_account_text_is_account_and_sign_in_is_login():
    create = _snapshot(
        heading="Create Account",
        page_text="Email Address Password Verify New Password Create Account",
        buttons=[ButtonControl(name="Create Account", selector="#create", control_type="button")],
    )
    assert register_pane(create) is True
    assert classify_page(create) == "ACCOUNT"
    assert terminal_kind(create, gaps=[], required_total=0) == "ACCOUNT"
    sign_in = _snapshot(
        heading="Sign In",
        page_text="Email Address Password",
        password_present=True,
        controls=[
            _control("text", "Email Address", "#email", input_type="email"),
            _control("password", "Password", "#password", input_type="password"),
        ],
        buttons=[ButtonControl(name="Sign In", selector="#sign", control_type="button")],
    )
    assert sign_in_pane(sign_in) is True
    assert classify_page(sign_in) == "LOGIN"
    assert terminal_kind(sign_in, gaps=[], required_total=0) == "LOGIN"
    blank = _snapshot(heading="Example", page_text="Nothing here.")
    assert terminal_kind(blank, gaps=[], required_total=0) == "NO_FORM"


def test_text_only_create_account_stops_before_no_form():
    payload = {
        "controls": [],
        "buttons": [{"name": "Create Account", "selector": "#create", "controlType": "button"}],
        "captcha": False,
        "password": False,
        "heading": "Create Account",
        "pageText": "Email Address\nPassword\nVerify New Password\nCreate Account",
        "links": [],
        "automationIds": [],
    }

    class Page:
        url = "https://example.test/create-account"

        def wait_for_load_state(self, state, timeout=0):
            return None

        def wait_for_timeout(self, millis):
            return None

        def evaluate(self, script, arg=None):
            return payload

    result = autofill_application(
        Page.url,
        CandidateProfile.from_dict(
            {"candidateId": "ref-a", "personal": {"full_name": "River Example", "email": "river.example@example.com"}}
        ),
        options=AutofillOptions(page=Page(), protocol_version="0.7.0", session_id="sess-create", candidate_id="ref-a"),
    )
    assert result.status == Status.ACCOUNT_CREATION_REQUIRED
    assert result.page_type == "ACCOUNT"
    assert result.account is not None
    assert result.account["mode"] == "register"
    assert result.progress["percent"] == 15
    assert result.status != Status.NO_FORM_FOUND
    assert result.status != Status.READY_FOR_HUMAN_SUBMIT


def test_register_reads_both_passwords_and_stops_on_a_mismatch():
    secret = "Correct-Horse-1!"
    other = "Other-Horse-2!"
    values = {"#email": "", "#password": "", "#verify": ""}
    clicked: list[str] = []
    form = {
        "controls": [
            {"kind": "text", "label": "Email Address", "selector": "#email", "inputType": "email"},
            {"kind": "password", "label": "Password", "selector": "#password", "inputType": "password"},
            {"kind": "password", "label": "Verify New Password", "selector": "#verify", "inputType": "password"},
        ],
        "buttons": [{"name": "Create Account", "selector": "#create", "controlType": "button"}],
        "captcha": False,
        "password": True,
        "heading": "Create Account",
        "pageText": "Email Address Password Verify New Password",
        "links": [],
        "automationIds": [],
    }
    otp = {
        "controls": [
            {"kind": "text", "label": "Verification code", "selector": "#otp", "inputType": "text", "maxLength": 6}
        ],
        "buttons": [],
        "captcha": False,
        "password": False,
        "heading": "Verify your email",
        "pageText": "Check your inbox. We sent a code.",
        "links": [],
        "automationIds": [],
    }

    class Locator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def fill(self, value: str) -> None:
            values[self.selector] = value

        def input_value(self) -> str:
            return values[self.selector]

        def click(self) -> None:
            clicked.append(self.selector)

        def check(self) -> None:
            return None

    class Page:
        url = "https://example.test/register"
        phase = "form"

        def locator(self, selector: str) -> Locator:
            return Locator(selector)

        def evaluate(self, script, arg=None):
            return otp if self.phase == "otp" else form

    page = Page()
    mismatch = run_account_action(
        page,
        mode="register",
        email="river.example@example.com",
        username="",
        password=secret,
        confirm=other,
        ats="workday",
    )
    assert mismatch["ok"] is False
    assert mismatch["reason"] == "password_mismatch"
    assert mismatch["fieldProof"]["passwordsMatch"] is False
    assert clicked == []
    assert secret not in str(mismatch)
    assert other not in str(mismatch)
    values["#password"] = ""
    values["#verify"] = ""
    clicked.clear()

    def click(self) -> None:
        clicked.append(self.selector)
        page.phase = "otp"

    Locator.click = click  # type: ignore[method-assign]
    matched = run_account_action(
        page,
        mode="register",
        email="river.example@example.com",
        username="",
        password=secret,
        confirm=secret,
        ats="workday",
    )
    assert clicked == ["#create"]
    assert matched["status"] == "EMAIL_VERIFICATION_REQUIRED"
    assert matched["status"] != "READY_FOR_HUMAN_SUBMIT"
    assert secret not in str(matched)
    assert matched["fieldProof"]["passwordsMatch"] is True
    assert matched["fieldProof"]["passwordLength"] == matched["fieldProof"]["verifyLength"] == len(secret)
    assert matched["fieldProof"]["emailFilled"] is True
    assert matched["fieldProof"]["emailMatches"] is True
    assert values["#password"] == values["#verify"] == secret


def test_standard_terms_are_checked_and_unusual_statements_stay_consent():
    terms = _control(
        "checkbox",
        "I agree to the Terms of Use and Privacy Policy",
        "#terms",
        required=True,
    )
    promo = _control("checkbox", "I agree to receive promotional SMS", "#sms", required=True)
    certify = _control("checkbox", "I certify this is true under penalty of perjury", "#cert", required=True)
    arbitration = _control("checkbox", "I agree to binding arbitration", "#arb", required=True)
    esign = _control("checkbox", "I agree to electronic signature", "#esign", required=True)
    background = _control("checkbox", "I consent to a background check", "#bg", required=True)
    demographic = _control("checkbox", "Share my demographic information", "#demo", required=True)
    assert terms_category(terms) == "STANDARD_TERMS"
    for control in (promo, certify, arbitration, esign, background, demographic):
        assert terms_category(control) == "CONSENT"
    ambiguous = _control("checkbox", "I agree", "#plain", required=True)
    mixed = _control(
        "checkbox",
        "I agree to the Terms of Use and to receive promotional SMS",
        "#mixed",
        required=True,
    )
    assert terms_category(ambiguous) == "CONSENT"
    assert terms_category(mixed) == "CONSENT"
    deny = set(STANDARD_TERMS_DENY)
    assert {
        "marketing",
        "newsletter",
        "sms",
        "promotional",
        "recording",
        "notetaker",
        "note taker",
        "artificial intelligence",
        "ai",
        "arbitration",
        "signature",
        "background check",
        "certify",
        "attest",
        "declare",
        "under penalty",
        "demographic",
        "job alerts",
        "email updates",
        "talent community",
        "talent network",
        "calls",
        "autodialed",
        "phone calls",
        "third parties",
        "partners",
    } <= deny

    checked: list[str] = []

    class Locator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def check(self) -> None:
            checked.append(self.selector)

    class Page:
        def locator(self, selector: str) -> Locator:
            return Locator(selector)

    bind_standard_terms(False)
    assert _accept_standard_term(Page(), _Ask(controls=[terms], consent=True)) is False
    assert checked == []
    bind_standard_terms(True)
    assert _accept_standard_term(Page(), _Ask(controls=[terms], consent=True)) is True
    assert _accept_standard_term(Page(), _Ask(controls=[promo], consent=True)) is False
    assert _accept_standard_term(Page(), _Ask(controls=[certify], consent=True)) is False
    choice = _control("radio", "I consent", "#choice", required=True)
    assert _accept_standard_term(Page(), _Ask(controls=[choice], consent=True, consent_choice=True)) is False
    assert checked == ["#terms"]
    recorded = consume_auto_consents()
    assert len(recorded) == 1
    assert recorded[0]["category"] == "STANDARD_TERMS"
    assert recorded[0]["questionHash"]
    assert "Privacy Policy" in recorded[0]["textExcerpt"]
    assert "promotional" not in recorded[0]["textExcerpt"]


def test_ai_deny_matches_a_whole_word_only():
    privacy = _control("checkbox", "I agree to the privacy policy", "#privacy", required=True)
    embedded = _control(
        "checkbox",
        "I agree to the terms and will maintain this email",
        "#keep",
        required=True,
    )
    whole = _control("checkbox", "I agree to the terms and to the use of AI", "#ai", required=True)
    assert terms_category(privacy) == "STANDARD_TERMS"
    assert terms_category(embedded) == "STANDARD_TERMS"
    assert terms_category(whole) == "CONSENT"


def test_plain_submission_consent_is_its_own_category():
    allowed = (
        "I consent to submit my information",
        "I consent to the processing of my personal data for this application",
        "I agree to share my information with Snap Finance for recruitment purposes",
    )
    for label in allowed:
        assert terms_category(_control("checkbox", label, "#submit", required=True)) == "STANDARD_SUBMISSION_CONSENT"
    blocked = (
        "I consent",
        "I consent to submit my information for any future use",
        "I agree to share my information",
        "I agree to share my information with anyone worldwide for recruitment purposes",
        "I consent to submit my information and receive job alerts",
        "I consent to the processing of my personal data for this application and SMS",
        "I agree to share my information with Snap Finance for recruitment purposes and AI screening",
        "I certify that I consent to submit my information",
        "I consent to a background check and to submit my information",
    )
    for label in blocked:
        assert terms_category(_control("checkbox", label, "#other", required=True)) == "CONSENT", label


def test_register_checks_submission_consent_and_logs_only_the_category(caplog):
    secret = "Correct-Horse-1!"
    email = "river.example@example.com"
    checked: list[str] = []
    clicked: list[str] = []
    values = {"#email": "", "#password": "", "#verify": ""}
    form = {
        "controls": [
            {"kind": "text", "label": "Email Address", "selector": "#email", "inputType": "email"},
            {"kind": "password", "label": "Password", "selector": "#password", "inputType": "password"},
            {"kind": "password", "label": "Verify New Password", "selector": "#verify", "inputType": "password"},
            {
                "kind": "checkbox",
                "label": "I consent to submit my information",
                "selector": "#submit-info",
                "inputType": "checkbox",
                "required": True,
            },
            {
                "kind": "checkbox",
                "label": "I agree to receive promotional SMS",
                "selector": "#promo",
                "inputType": "checkbox",
                "required": True,
            },
        ],
        "buttons": [{"name": "Create Account", "selector": "#create", "controlType": "button"}],
        "captcha": False,
        "password": True,
        "heading": "Create Account",
        "pageText": "Email Address Password Verify New Password Create Account",
        "links": [],
        "automationIds": [],
    }

    class Locator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def fill(self, value: str) -> None:
            values[self.selector] = value

        def input_value(self) -> str:
            return values.get(self.selector, "")

        def check(self) -> None:
            checked.append(self.selector)

        def click(self) -> None:
            clicked.append(self.selector)

    class Page:
        url = "https://example.test/register"

        def locator(self, selector: str) -> Locator:
            return Locator(selector)

        def evaluate(self, script, arg=None):
            if clicked:
                return {
                    "controls": [],
                    "buttons": [],
                    "captcha": False,
                    "password": False,
                    "heading": "Check your inbox",
                    "pageText": "Verify your email address",
                    "links": [],
                    "automationIds": [],
                }
            return form

    caplog.set_level(logging.INFO, logger="autofill.mapping")
    accepted = run_account_action(
        Page(),
        mode="register",
        email=email,
        username="",
        password=secret,
        confirm=secret,
        ats="workday",
        standard_terms=True,
    )
    assert checked == ["#submit-info"]
    assert clicked == ["#create"]
    assert accepted["status"] == "EMAIL_VERIFICATION_REQUIRED"
    assert accepted["autoConsents"][0]["category"] == "STANDARD_SUBMISSION_CONSENT"
    assert accepted["autoConsents"][0]["textExcerpt"] == "I consent to submit my information"
    promo = next(row for row in accepted["consentRows"] if row["textExcerpt"].startswith("I agree to receive"))
    assert promo["category"] == "CONSENT"
    assert promo["checked"] is False
    assert secret not in caplog.text
    assert email not in caplog.text
    assert "submit my information" not in caplog.text
    assert "auto_consent category=STANDARD_SUBMISSION_CONSENT result=checked" in caplog.text


def test_register_checks_standard_terms_and_leaves_other_boxes():
    secret = "Correct-Horse-1!"
    values = {"#email": "", "#password": "", "#verify": ""}
    checked: list[str] = []
    clicked: list[str] = []
    form = {
        "controls": [
            {"kind": "text", "label": "Email Address", "selector": "#email", "inputType": "email"},
            {"kind": "password", "label": "Password", "selector": "#password", "inputType": "password"},
            {"kind": "password", "label": "Verify New Password", "selector": "#verify", "inputType": "password"},
            {
                "kind": "checkbox",
                "label": "I agree to the Terms of Use and Privacy Policy",
                "selector": "#terms",
                "inputType": "checkbox",
                "required": True,
            },
            {
                "kind": "checkbox",
                "label": "I agree to receive promotional SMS and job alerts",
                "selector": "#promo",
                "inputType": "checkbox",
                "required": True,
            },
        ],
        "buttons": [{"name": "Create Account", "selector": "#create", "controlType": "button"}],
        "captcha": False,
        "password": True,
        "heading": "Create Account",
        "pageText": "Email Address Password Verify New Password Create Account",
        "links": [],
        "automationIds": [],
    }

    class Locator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def fill(self, value: str) -> None:
            values[self.selector] = value

        def input_value(self) -> str:
            return values.get(self.selector, "")

        def check(self) -> None:
            checked.append(self.selector)

        def click(self) -> None:
            clicked.append(self.selector)

    class Page:
        url = "https://example.test/register"

        def locator(self, selector: str) -> Locator:
            return Locator(selector)

        def evaluate(self, script, arg=None):
            if clicked:
                return {
                    "controls": [
                        {
                            "kind": "text",
                            "label": "Verification code",
                            "selector": "#otp",
                            "inputType": "text",
                            "maxLength": 6,
                        }
                    ],
                    "buttons": [],
                    "captcha": False,
                    "password": False,
                    "heading": "Verify your email",
                    "pageText": "Check your inbox. We sent a code.",
                    "links": [],
                    "automationIds": [],
                }
            return form

    blocked = run_account_action(
        Page(),
        mode="register",
        email="river.example@example.com",
        username="",
        password=secret,
        confirm=secret,
        ats="workday",
    )
    assert blocked["reason"] == "site_rejected"
    assert values["#email"] == ""
    assert checked == []
    assert clicked == []

    checked.clear()
    clicked.clear()
    accepted = run_account_action(
        Page(),
        mode="register",
        email="river.example@example.com",
        username="",
        password=secret,
        confirm=secret,
        ats="workday",
        standard_terms=True,
    )
    assert checked == ["#terms"]
    assert clicked == ["#create"]
    assert accepted["status"] == "EMAIL_VERIFICATION_REQUIRED"
    assert accepted["status"] != "READY_FOR_HUMAN_SUBMIT"
    proof = accepted["fieldProof"]
    assert proof["emailFilled"] is True
    assert proof["emailMatches"] is True
    assert proof["passwordsMatch"] is True
    assert proof["passwordLength"] == proof["verifyLength"] == len(secret)
    assert secret not in str(accepted)
    assert "river.example@example.com" not in str(proof)
    excerpts = {row["textExcerpt"]: row for row in accepted["consentRows"]}
    assert excerpts["I agree to the Terms of Use and Privacy Policy"]["checked"] is True
    assert excerpts["I agree to the Terms of Use and Privacy Policy"]["category"] == "STANDARD_TERMS"
    assert excerpts["I agree to receive promotional SMS and job alerts"]["checked"] is False
    assert excerpts["I agree to receive promotional SMS and job alerts"]["category"] == "CONSENT"
    assert accepted["autoConsents"][0]["category"] == "STANDARD_TERMS"
    assert "Privacy Policy" in accepted["autoConsents"][0]["textExcerpt"]
    assert "promotional" not in accepted["autoConsents"][0]["textExcerpt"]


def test_compound_privacy_and_job_alerts_stay_consent():
    statements = (
        "I agree to the Privacy Policy and to receive job alerts",
        "I agree to the Privacy Policy and to receive the newsletter",
        "I agree to the Privacy Policy and to receive email updates",
        "I agree to the Terms of Use and to join the talent community",
        "I agree to the Terms of Use and to join our talent network",
        "Join our talent community/network",
        "I agree to the Privacy Policy and to receive calls",
        "I agree to the Privacy Policy and to receive autodialed phone calls",
        "I agree to receive auto-dialed calls",
        "I agree to the Privacy Policy and to share my information with third parties",
        "I agree to the Privacy Policy and to share with partners",
    )
    checked: list[str] = []

    class Locator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def check(self) -> None:
            checked.append(self.selector)

    class Page:
        def locator(self, selector: str) -> Locator:
            return Locator(selector)

    bind_standard_terms(True)
    for statement in statements:
        control = _control("checkbox", statement, "#compound", required=True)
        assert terms_category(control) == "CONSENT"
        assert _accept_standard_term(Page(), _Ask(controls=[control], consent=True)) is False
    assert checked == []
    assert consume_auto_consents() == []
