"""Detect a login form and sign in when the host supplied credentials.

Login submission is allowed. Final application Submit is not. The automatic
fill never creates an account. Protocol 0.7.0 can score a Create Account page
separately, and only an explicit credentials call with ``mode=register`` uses
that form. Passwords are written into the password field and nowhere else:
they are not returned, logged, or stored on the session.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from autofill.credentials import Credentials
from autofill.extract import extract_page
from autofill.models import Control, PageSnapshot
from autofill.safeguards import activate, activate_login

logger = logging.getLogger("autofill.login")

_LOGIN_PATH = re.compile(r"/(login|signin|sign-in|sign_in)(/|$)", re.IGNORECASE)
_APPLICATION_RE = re.compile(r"\b(resume|cover letter|work authorization|employer)\b", re.IGNORECASE)
_HEADING_RE = re.compile(r"\b(sign[\s-]*in|log[\s-]*in)\b", re.IGNORECASE)
_ERROR_RE = re.compile(r"invalid|incorrect|unavailable|does not match|try again", re.IGNORECASE)


class LoginDetector:
    """Decide whether the current page is a login, and which flow it is."""

    def mode(self, snapshot: PageSnapshot, url: str) -> str:
        """Return ``password``, ``email_first``, ``account_creation``, or ``none``."""
        if self._looks_like_application(snapshot) and snapshot.password_present:
            return "account_creation"
        if snapshot.password_present:
            return "password"
        if self._email_first(snapshot, url):
            return "email_first"
        return "none"

    def _looks_like_application(self, snapshot: PageSnapshot) -> bool:
        blob = " ".join(control.label for control in snapshot.controls if not control.hidden)
        return bool(_APPLICATION_RE.search(blob))

    def _email_first(self, snapshot: PageSnapshot, url: str) -> bool:
        if snapshot.password_present or self._looks_like_application(snapshot):
            return False
        email = _email_control(snapshot)
        if email is None:
            return False
        heading = snapshot.heading or ""
        path_login = bool(_LOGIN_PATH.search(url or ""))
        if not path_login and not _HEADING_RE.search(heading):
            return False
        return any(
            "continue" in button.name.casefold() or "next" in button.name.casefold() for button in snapshot.buttons
        )


def _email_control(snapshot: PageSnapshot) -> Control | None:
    for control in snapshot.controls:
        if control.hidden or control.kind not in {"text", "combobox"}:
            continue
        parts = (
            control.label,
            control.name,
            control.element_id,
            control.autocomplete,
            control.placeholder,
        )
        blob = " ".join(part for part in parts if part).casefold()
        if control.input_type == "email" or "email" in blob or "username" in blob or "user name" in blob:
            return control
    return None


def _password_control(snapshot: PageSnapshot) -> Control | None:
    for control in snapshot.controls:
        if not control.hidden and control.kind == "password":
            return control
    return None


def _login_button(snapshot: PageSnapshot):
    for button in snapshot.buttons:
        name = button.name.casefold()
        if "sign up" in name or "register" in name or "create account" in name:
            continue
        if "sign in" in name or "log in" in name or name == "login":
            return button
    return None


def _continue_button(snapshot: PageSnapshot):
    for button in snapshot.buttons:
        name = button.name.casefold()
        if "continue" in name or name == "next":
            return button
    return None


class SecretInputError(Exception):
    """A credential field could not be typed. The message is an error type, never the value."""

    def __init__(self, error_type: str) -> None:
        super().__init__(error_type)


def _type_secret(page, selector: str, value: str) -> None:
    """Put ``value`` into a login field. A failure does not keep the value."""
    try:
        page.locator(selector).fill(value)
    except Exception as exc:
        raise SecretInputError(type(exc).__name__) from None


def _identity(creds: Credentials, control: Control) -> str:
    blob = " ".join((control.label, control.autocomplete, control.name)).casefold()
    if "user" in blob and "email" not in blob and creds.username:
        return creds.username
    return creds.email or creds.username


def run_login(page, creds: Credentials) -> tuple[str, list[str]]:
    """Sign in. Returns ``(AUTHENTICATED|LOGIN_FAILED|CAPTCHA_REQUIRED, steps)``.

    On failure the caller must not continue into the application.
    """
    steps: list[str] = []
    detector = LoginDetector()
    snapshot = extract_page(page)
    if snapshot.captcha_present:
        return "CAPTCHA_REQUIRED", steps
    mode = detector.mode(snapshot, page.url or "")
    if mode == "none":
        return "AUTHENTICATED", steps
    if mode == "account_creation":
        logger.info("login refused: page mixes a password with an application")
        return "LOGIN_FAILED", steps
    if not creds.password or not (creds.email or creds.username):
        return "LOGIN_FAILED", steps

    if mode == "email_first":
        email = _email_control(snapshot)
        if email is None:
            return "LOGIN_FAILED", steps
        steps.append("LOGIN_EMAIL")
        _type_secret(page, email.selector, _identity(creds, email))
        button = _continue_button(snapshot)
        if button is None:
            return "LOGIN_FAILED", steps
        steps.append("LOGIN_CONTINUE")
        activate(page, button.selector, button.name, control_type=button.control_type)
        snapshot = extract_page(page)
        if snapshot.captcha_present:
            return "CAPTCHA_REQUIRED", steps

    password = _password_control(snapshot)
    identity = _email_control(snapshot)
    if password is None or identity is None:
        return "LOGIN_FAILED", steps
    if "LOGIN_EMAIL" not in steps:
        steps.append("LOGIN_EMAIL")
    steps.append("LOGIN_PASSWORD")
    _type_secret(page, identity.selector, _identity(creds, identity))
    _type_secret(page, password.selector, creds.password)
    button = _login_button(snapshot)
    if button is None:
        return "LOGIN_FAILED", steps
    steps.append("LOGIN_SUBMIT")
    activate_login(page, button.selector, button.name, control_type=button.control_type)
    steps.append("WAIT_FOR_LOGIN")
    snapshot = extract_page(page)
    if snapshot.captcha_present:
        return "CAPTCHA_REQUIRED", steps
    if snapshot.password_present or _ERROR_RE.search(snapshot.banner or ""):
        logger.info("login failed: form still asking for credentials")
        return "LOGIN_FAILED", steps
    steps.append("AUTHENTICATED")
    logger.info("login finished: authenticated")
    return "AUTHENTICATED", steps


def protocol_at_least(version: str, target: str = "0.7.0") -> bool:
    """True when ``version`` is at least ``target``. Missing versions are older."""

    def parse(value: str) -> tuple[int, ...]:
        parts: list[int] = []
        for piece in (value or "").strip().split("."):
            if not piece.isdigit():
                return (0,)
            parts.append(int(piece))
        return tuple(parts) or (0,)

    return parse(version) >= parse(target)


_CONFIRM_RE = re.compile(
    r"(verify|confirm|re-?enter|repeat).{0,24}password|password.{0,24}(verify|confirm|re-?enter|repeat)",
    re.IGNORECASE,
)
_REGISTER_RE = re.compile(r"\b(create account|register|sign up|join)\b", re.IGNORECASE)
_LOGIN_RE = re.compile(r"\b(sign[\s-]*in|log[\s-]*in)\b", re.IGNORECASE)
_FORGOT_RE = re.compile(r"forgot password", re.IGNORECASE)
_TERMS_RE = re.compile(r"\b(terms|privacy)\b", re.IGNORECASE)
_OTP_TEXT_RE = re.compile(
    r"verify your email|check your (?:inbox|email)|we sent (?:you )?(?:a|an) (?:code|email|link)",
    re.IGNORECASE,
)
_OTP_LABEL_RE = re.compile(r"\b(verification code|passcode|otp|code)\b", re.IGNORECASE)
_EXISTS_RE = re.compile(r"already exists|account exists|already registered|email is taken", re.IGNORECASE)
_EMAIL_REDACT_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_DIGIT_RUN_RE = re.compile(r"\d{3,}")
_WORKDAY_HOST_RE = re.compile(r"^[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com$", re.IGNORECASE)
_LOCALE_SEGMENT_RE = re.compile(r"^[a-z]{2}(?:-[a-z]{2})?$", re.IGNORECASE)
_POLICY_MIN_RE = re.compile(r"(?:at least|minimum(?: of)?|min(?:imum)? of)\s+(\d+)\s+character", re.IGNORECASE)
_POLICY_DIGIT_RE = re.compile(
    r"\b(?:digits?|numbers?|numerals?|numeric(?:\s+character)?)\b|\ba\s+numeral\b|0\s*[-–]\s*9",
    re.IGNORECASE,
)
_POLICY_SYMBOL_RE = re.compile(r"\b(?:symbols?|special\s+characters?)\b", re.IGNORECASE)
_POLICY_UPPER_RE = re.compile(r"upper\s*-?\s*case|capital\s+letters?|uppercase", re.IGNORECASE)
_POLICY_LOWER_RE = re.compile(r"lower\s*-?\s*case|lowercase", re.IGNORECASE)
_POLICY_ALPHA_RE = re.compile(r"\balphabetic(?:\s+character)?s?\b", re.IGNORECASE)
_POLICY_MAX_RE = re.compile(
    r"(?:at most|maximum(?: of)?|max(?:imum)? of|no more than)\s+(\d+)\s+character",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AccountScore:
    """Create Account versus Sign In. Ties are Sign In. An application is neither."""

    mode: str
    alternate: bool
    register_score: int
    login_score: int
    application: bool = False


@dataclass(frozen=True)
class AccountStop:
    """A 0.7.0 stop the automatic fill must not type into."""

    status: str
    message: str
    account: dict | None


def _passwords(snapshot: PageSnapshot) -> list[Control]:
    return [control for control in snapshot.controls if control.kind == "password" and not control.hidden]


def _automation_ids(snapshot: PageSnapshot) -> set[str]:
    """Visible automation ids only. A hidden pane is not the active form."""
    found = {item for item in snapshot.automation_ids if item}
    for control in snapshot.controls:
        if control.hidden:
            continue
        if control.automation_id:
            found.add(control.automation_id)
    for button in snapshot.buttons:
        if button.automation_id:
            found.add(button.automation_id)
    for link in snapshot.links:
        if link.automation_id:
            found.add(link.automation_id)
    return found


def _control_blob(control: Control) -> str:
    return " ".join(
        part for part in (control.label, control.placeholder, control.aria_label, control.name, control.nearby) if part
    )


def score_account(snapshot: PageSnapshot, url: str = "") -> AccountScore:
    """Highest score wins. A tie is Sign In. A real application form is not an account page."""
    del url
    if LoginDetector()._looks_like_application(snapshot) and snapshot.password_present:
        return AccountScore(mode="none", alternate=False, register_score=0, login_score=0, application=True)
    passwords = _passwords(snapshot)
    register = 0
    login = 0
    if len(passwords) >= 2:
        register += 2
    elif len(passwords) == 1:
        login += 2
    blobs = " ".join(_control_blob(control) for control in snapshot.controls if not control.hidden)
    if _CONFIRM_RE.search(blobs):
        register += 2
    heading = snapshot.heading or ""
    button_names = [button.name for button in snapshot.buttons]
    if _REGISTER_RE.search(heading) or any(_REGISTER_RE.search(name) for name in button_names):
        register += 2
    if _LOGIN_RE.search(heading) or any(_LOGIN_RE.search(name) for name in button_names):
        login += 2
    link_names = " ".join(link.name for link in snapshot.links)
    page_text = snapshot.page_text or ""
    if _FORGOT_RE.search(link_names) or _FORGOT_RE.search(page_text):
        login += 2
    ids = _automation_ids(snapshot)
    register_pane = "createAccountSubmitButton" in ids or "verifyPassword" in ids
    if "createAccountSubmitButton" in ids:
        register += 3
    if "verifyPassword" in ids:
        register += 3
    if "signInSubmitButton" in ids:
        login += 3
    if "createAccountLink" in ids and register_pane:
        register += 2
    for control in snapshot.controls:
        if control.hidden or control.kind != "checkbox":
            continue
        if _TERMS_RE.search(_control_blob(control) + " " + control.prompt + " " + control.group):
            register += 1
            break
    create_link = bool(_REGISTER_RE.search(link_names) or "createAccountLink" in ids)
    signin_link = bool(_LOGIN_RE.search(link_names))
    signin_pane = ("signInSubmitButton" in ids or (len(passwords) == 1 and login > 0)) and not register_pane
    if signin_pane and create_link:
        return AccountScore(
            mode="login",
            alternate=True,
            register_score=register,
            login_score=login,
        )
    if register > login and register > 0:
        return AccountScore(mode="register", alternate=signin_link, register_score=register, login_score=login)
    if login > 0 and login >= register:
        return AccountScore(
            mode="login",
            alternate=create_link and not register_pane,
            register_score=register,
            login_score=login,
        )
    return AccountScore(mode="none", alternate=False, register_score=register, login_score=login)


def verification_present(snapshot: PageSnapshot) -> bool:
    """OTP or an email-verification notice. A password form is not this page."""
    if snapshot.password_present:
        return False
    for control in snapshot.controls:
        if control.hidden:
            continue
        if (control.autocomplete or "").casefold() == "one-time-code":
            return True
        if control.max_length is not None and 4 <= control.max_length <= 8 and _OTP_LABEL_RE.search(
            _control_blob(control)
        ):
            return True
    text = " ".join(part for part in (snapshot.page_text, snapshot.heading, snapshot.banner) if part)
    return bool(_OTP_TEXT_RE.search(text))


def tenant_identity(url: str, ats: str | None) -> dict:
    """Host identity for the page that holds the password field. No candidate data."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    origin = f"{parsed.scheme}://{host}" if parsed.scheme and host else ""
    parts = [part for part in (parsed.path or "").split("/") if part]
    key = host
    if _WORKDAY_HOST_RE.match(host):
        site = next((part for part in parts if not _LOCALE_SEGMENT_RE.match(part)), "")
        key = f"{host}/{site}" if site else host
    elif parts and (
        host.endswith("greenhouse.io")
        or host.endswith("lever.co")
        or "ashbyhq.com" in host
        or "smartrecruiters.com" in host
    ):
        key = parts[0]
    return {"ats": ats, "host": host, "tenantKey": key or host, "loginOrigin": origin}


def password_policy(snapshot: PageSnapshot) -> dict | None:
    """Hints from the requirements block next to the password field.

    Page text is not used. A cookie banner at the top of the page is not a policy.
    """
    text = (snapshot.password_hint or "").strip()
    if not snapshot.password_present and not text:
        return None
    policy: dict = {}
    minimum = _POLICY_MIN_RE.search(text)
    if minimum:
        policy["minLength"] = int(minimum.group(1))
    maximum = _POLICY_MAX_RE.search(text)
    if maximum:
        policy["maxLength"] = int(maximum.group(1))
    if "maxLength" not in policy:
        lengths = [control.max_length for control in _passwords(snapshot) if control.max_length]
        if lengths:
            policy["maxLength"] = min(lengths)
    folded = text.casefold()
    if _POLICY_UPPER_RE.search(folded):
        policy["requireUpper"] = True
    if _POLICY_LOWER_RE.search(folded):
        policy["requireLower"] = True
    if _POLICY_ALPHA_RE.search(folded):
        policy.setdefault("requireUpper", True)
        policy.setdefault("requireLower", True)
    if _POLICY_DIGIT_RE.search(folded):
        policy["requireDigit"] = True
    if _POLICY_SYMBOL_RE.search(folded):
        policy["requireSymbol"] = True
    if re.search(r"(cannot|must not|do not|don't).{0,40}email", folded):
        policy["disallowEmail"] = True
    if not policy:
        return None
    if text:
        policy["raw"] = text[:300]
    return policy


def redact_site_error(text: str, secrets: list[str] | None = None) -> str:
    """Short site text with emails, digit runs, and submitted values removed."""
    cleaned = " ".join((text or "").split())
    for secret in secrets or []:
        if secret and len(secret) >= 3 and secret in cleaned:
            cleaned = cleaned.replace(secret, "[redacted]")
    cleaned = _EMAIL_REDACT_RE.sub("[redacted]", cleaned)
    cleaned = _DIGIT_RUN_RE.sub("[redacted]", cleaned)
    return cleaned[:200]


def account_view(
    snapshot: PageSnapshot,
    url: str,
    ats: str | None,
    *,
    mode: str,
    alternate: bool,
    site_errors: list[str] | None = None,
) -> dict:
    """Public account description. It never includes a password or email value."""
    email = _email_control(snapshot)
    chosen = mode if mode in {"register", "login"} else "login"
    return {
        "mode": chosen,
        "alternateModeAvailable": bool(alternate),
        "tenant": tenant_identity(url, ats),
        "passwordPolicy": password_policy(snapshot) if snapshot.password_present or chosen == "register" else None,
        "emailPrefillAllowed": not bool(email is not None and email.read_only),
        "siteErrors": [redact_site_error(item) for item in (site_errors or [])[:5]],
    }


def account_decision(snapshot: PageSnapshot, url: str, ats: str | None) -> AccountStop | None:
    """Register and email-verification stops. Sign-in stays on the existing login path."""
    if verification_present(snapshot):
        return AccountStop(
            status="EMAIL_VERIFICATION_REQUIRED",
            message="Email verification is required. Auto-Fill does not read mail.",
            account=None,
        )
    scored = score_account(snapshot, url)
    if scored.application or scored.mode != "register":
        return None
    return AccountStop(
        status="ACCOUNT_CREATION_REQUIRED",
        message="Create an employer account. Nothing was typed.",
        account=account_view(snapshot, url, ats, mode="register", alternate=scored.alternate),
    )


def login_account_view(snapshot: PageSnapshot, url: str, ats: str | None) -> dict | None:
    """Account description for a Sign In stop. Application forms are omitted."""
    scored = score_account(snapshot, url)
    if scored.application or scored.mode != "login":
        return None
    return account_view(snapshot, url, ats, mode="login", alternate=scored.alternate)


def account_exists_text(snapshot: PageSnapshot) -> bool:
    blob = " ".join(part for part in [snapshot.banner, snapshot.heading, snapshot.page_text, *snapshot.alerts] if part)
    return bool(_EXISTS_RE.search(blob))
