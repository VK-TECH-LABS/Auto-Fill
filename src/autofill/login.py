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
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from autofill.ats import WORKDAY_HOST_RE
from autofill.credentials import Credentials
from autofill.extract import extract_page
from autofill.models import Control, PageSnapshot
from autofill.safeguards import activate, activate_account_switch, activate_login, activate_login_at_point

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
        # A header Sign In reopens the dialog, and "Sign in with Google" hands
        # the candidate to an identity provider. Neither submits this form.
        if getattr(button, "in_header", False) or getattr(button, "in_ad", False):
            continue
        if getattr(button, "disabled", False) or "sign in with" in name or "continue with" in name:
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
        revealed = _await_password_step(page)
        if revealed == "CAPTCHA_REQUIRED":
            return "CAPTCHA_REQUIRED", steps
        snapshot = revealed if isinstance(revealed, PageSnapshot) else extract_page(page)
        if snapshot.captcha_present and not application_form_visible(snapshot):
            return "CAPTCHA_REQUIRED", steps
        if application_form_visible(snapshot) or verification_present(snapshot):
            steps.append("AUTHENTICATED")
            logger.info("login finished: authenticated")
            return "AUTHENTICATED", steps

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
    covered = False
    if button is None and "signInSubmitButton" in set(snapshot.automation_ids or []):
        # Workday keeps the real Sign In button aria-hidden under a nameless overlay.
        from autofill.models import ButtonControl

        button = ButtonControl(
            name="Sign In", selector="[data-automation-id='signInSubmitButton']", automation_id="signInSubmitButton"
        )
        covered = True
    if button is None:
        return "LOGIN_FAILED", steps
    steps.append("LOGIN_SUBMIT")
    if covered:
        activate_login_at_point(page, button.selector, button.name)
    else:
        activate_login(page, button.selector, button.name, control_type=button.control_type)
    steps.append("WAIT_FOR_LOGIN")
    outcome = _await_signed_in(page)
    if outcome == "CAPTCHA_REQUIRED":
        return "CAPTCHA_REQUIRED", steps
    if outcome != "AUTHENTICATED":
        logger.info("login failed: form still asking for credentials")
        return "LOGIN_FAILED", steps
    steps.append("AUTHENTICATED")
    logger.info("login finished: authenticated")
    return "AUTHENTICATED", steps


def _pause(page, millis: int) -> None:
    pause = getattr(page, "wait_for_timeout", None)
    try:
        if pause is None:
            time.sleep(millis / 1000)
        else:
            pause(millis)
    except Exception:
        return


def _login_rejected(snapshot: PageSnapshot) -> bool:
    """The password form is still up and the site showed a rejection."""
    text = " ".join(part for part in (snapshot.banner, *snapshot.alerts) if part)
    if _ERROR_RE.search(text):
        return True
    return bool(snapshot.password_present and (snapshot.banner or snapshot.alerts))


def _await_signed_in(page) -> str:
    """Wait for the application after Sign In. A rejection fails immediately.

    A slow employer keeps the password box for a moment after a good password.
    That wait is not a failed login, and it does not create an account.
    """
    deadline = time.monotonic() + _LOGIN_WAIT_SECONDS
    try:
        snapshot = extract_page(page)
    except Exception:
        return "LOGIN_FAILED"
    while True:
        if snapshot.captcha_present and not application_form_visible(snapshot):
            return "CAPTCHA_REQUIRED"
        if application_form_visible(snapshot) or verification_present(snapshot):
            return "AUTHENTICATED"
        if _login_rejected(snapshot):
            return "LOGIN_FAILED"
        heading = (snapshot.heading or "").casefold()
        still_login = snapshot.password_present or "sign in" in heading or "log in" in heading
        if not still_login and (snapshot.controls or snapshot.buttons):
            return "AUTHENTICATED"
        if time.monotonic() >= deadline:
            break
        _pause(page, 150)
        try:
            snapshot = extract_page(page)
        except Exception:
            return "LOGIN_FAILED"
    if snapshot.password_present or _login_rejected(snapshot):
        return "LOGIN_FAILED"
    return "AUTHENTICATED"


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
_OTP_PIN_RE = re.compile(r"\bpin\b", re.IGNORECASE)
_DIAL_OR_POSTAL_RE = re.compile(
    r"\b(?:country|area|postal|zip|dial|calling)\s*codes?\b",
    re.IGNORECASE,
)
_NOT_OTP_CODE_RE = re.compile(
    r"\b(?:referral|promo(?:tional)?|coupon|discount|employee|vendor|access|gift)\b",
    re.IGNORECASE,
)
_LOGIN_WAIT_SECONDS = 3.0
_CONTACT_NAME_RE = re.compile(
    r"\b(?:first name|last name|full name|given name|family name|surname)\b",
    re.IGNORECASE,
)
_CONTACT_REACH_RE = re.compile(r"\b(?:e-?mail|phone|mobile|telephone)\b", re.IGNORECASE)
_EXISTS_RE = re.compile(r"already exists|account exists|already registered|email is taken", re.IGNORECASE)
_EMAIL_REDACT_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_DIGIT_RUN_RE = re.compile(r"\d{3,}")
_WORKDAY_HOST_RE = WORKDAY_HOST_RE
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


_CONFIRM_ID_RE = re.compile(r"verify|confirm|retype|re-?enter|repeat", re.IGNORECASE)
_SIGNIN_ALTERNATE_IDS = frozenset({"signInLink", "signInButton"})


def _create_account_ids(ids: set[str]) -> set[str]:
    """Workday Create Account automation ids. ``createAccountLink`` sits on the Sign In pane."""
    return {item for item in ids if item.startswith("createAccount") and item != "createAccountLink"} | (
        {"verifyPassword"} & ids
    )


def confirm_password_control(snapshot: PageSnapshot) -> Control | None:
    """A visible Verify / Confirm password box next to another password box. Sign In never has one."""
    passwords = _passwords(snapshot)
    if len(passwords) < 2:
        return None
    for control in passwords:
        blob = " ".join(part for part in (_control_blob(control), control.automation_id, control.element_id) if part)
        if _CONFIRM_RE.search(blob) or (
            control.automation_id and _CONFIRM_ID_RE.search(control.automation_id)
        ):
            return control
    return None


def create_account_form(snapshot: PageSnapshot) -> bool:
    """A Create Account form, scored before any Sign In rule.

    Either a password box confirmed by a Verify / Confirm password box, or a
    Workday Create Account id (``createAccountSubmitButton``,
    ``createAccountCheckbox``, ``verifyPassword``) with a visible password box.
    A Sign In link, tab, or header button on the same page does not change this.
    An application form that mixes in a password is not an account page.
    """
    if snapshot.captcha_present:
        return False
    if LoginDetector()._looks_like_application(snapshot) and snapshot.password_present:
        return False
    if confirm_password_control(snapshot) is not None:
        return True
    ids = _automation_ids(snapshot)
    if not (_passwords(snapshot) or snapshot.password_present):
        return False
    if "signInSubmitButton" in ids and not ({"createAccountSubmitButton", "verifyPassword"} & ids):
        return False
    return bool(_create_account_ids(ids))


def _sign_in_alternate(snapshot: PageSnapshot) -> bool:
    """A Sign In link, tab, or button the person can use instead of Create Account."""
    if any(_LOGIN_RE.search(link.name) for link in snapshot.links):
        return True
    for button in snapshot.buttons:
        if button.automation_id in _SIGNIN_ALTERNATE_IDS:
            return True
        if not button.in_header and not button.in_ad and _LOGIN_RE.search(button.name):
            return True
    return False


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
    page_text = snapshot.page_text or ""
    if _CONFIRM_RE.search(blobs) or _CONFIRM_RE.search(page_text):
        register += 2
    if _CONFIRM_RE.search(page_text) and _REGISTER_RE.search(page_text):
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
    create_form = create_account_form(snapshot)
    register_pane = create_form or "createAccountSubmitButton" in ids or "verifyPassword" in ids
    if create_form:
        register += 4
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
    if create_form or (register > login and register > 0):
        return AccountScore(
            mode="register",
            alternate=signin_link or _sign_in_alternate(snapshot),
            register_score=register,
            login_score=login,
        )
    if login > 0 and login >= register:
        return AccountScore(
            mode="login",
            alternate=create_link and not register_pane,
            register_score=register,
            login_score=login,
        )
    return AccountScore(mode="none", alternate=False, register_score=register, login_score=login)


def _otp_control(control: Control) -> bool:
    """A real one-time-code box. A dial code, postal code, or referral code is not one."""
    if control.hidden:
        return False
    if (control.autocomplete or "").casefold() == "one-time-code":
        return True
    blob = _control_blob(control)
    if _DIAL_OR_POSTAL_RE.search(blob) or _NOT_OTP_CODE_RE.search(blob):
        return False
    if control.max_length is None or not 4 <= control.max_length <= 8:
        return False
    return bool(_OTP_LABEL_RE.search(blob) or _OTP_PIN_RE.search(blob))


def _contact_application(snapshot: PageSnapshot) -> bool:
    """Name plus email or phone. That page is an application, not an inbox check."""
    blobs = [_control_blob(control) for control in snapshot.controls if not control.hidden]
    blob = " ".join(blobs)
    return bool(_CONTACT_NAME_RE.search(blob) and _CONTACT_REACH_RE.search(blob))


def _job_application(snapshot: PageSnapshot) -> bool:
    """Questions or contact fields. A lone email or password box is not one."""
    if _contact_application(snapshot):
        return True
    for control in snapshot.controls:
        if control.hidden or control.disabled:
            continue
        if control.kind == "password" or (control.input_type or "").casefold() == "password":
            continue
        if _otp_control(control):
            continue
        if control.kind in {"file", "textarea", "select", "radio", "combobox", "checkbox"}:
            return True
        if control.kind == "text" or (control.input_type or "").casefold() in {"tel", "number", "url"}:
            blob = _control_blob(control)
            if (control.input_type or "").casefold() == "email" or re.search(
                r"\b(?:e-?mail|username|user name)\b", blob, re.IGNORECASE
            ):
                continue
            return True
    return False


def verification_present(snapshot: PageSnapshot) -> bool:
    """OTP or an email-verification notice. A password form is not this page.

    A country, area, postal, zip, or dial code is not an OTP. A referral,
    promo, or access code is not one either. A contact form or question page
    that mentions a code in page text is not one, unless a real one-time-code
    control is on the page.
    """
    if snapshot.password_present:
        return False
    if any(_otp_control(control) for control in snapshot.controls):
        return True
    if _job_application(snapshot):
        return False
    text = " ".join(part for part in (snapshot.page_text, snapshot.heading, snapshot.banner) if part)
    return bool(_OTP_TEXT_RE.search(text))


def application_form_visible(snapshot: PageSnapshot) -> bool:
    """A job form is on screen. Create Account, a real OTP, and a real captcha are not."""
    if snapshot.captcha_present or snapshot.password_present:
        return False
    if verification_present(snapshot):
        return False
    return _job_application(snapshot)


def _await_password_step(page) -> PageSnapshot | str:
    """After email-first Continue, wait for the password box or the application."""
    deadline = time.monotonic() + _LOGIN_WAIT_SECONDS
    snapshot = extract_page(page)
    while True:
        if snapshot.captcha_present and not application_form_visible(snapshot):
            return "CAPTCHA_REQUIRED"
        if _password_control(snapshot) is not None or application_form_visible(snapshot):
            return snapshot
        if time.monotonic() >= deadline:
            return snapshot
        _pause(page, 150)
        try:
            snapshot = extract_page(page)
        except Exception:
            return snapshot


def tenant_identity(url: str, ats: str | None) -> dict:
    """Host identity for the page that holds the password field. No candidate data."""
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    origin = f"{parsed.scheme}://{host}" if parsed.scheme and host else ""
    parts = [part for part in (parsed.path or "").split("/") if part]
    key = host
    if _WORKDAY_HOST_RE.search(host):
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
    fields = account_fields(snapshot)
    return {
        "mode": chosen,
        "alternateModeAvailable": bool(alternate),
        "tenant": tenant_identity(url, ats),
        "passwordPolicy": password_policy(snapshot) if snapshot.password_present or chosen == "register" else None,
        "emailPrefillAllowed": not bool(email is not None and email.read_only),
        "siteErrors": [redact_site_error(item) for item in (site_errors or [])[:5]],
        "fields": fields,
        "requiredFields": [item["role"] for item in fields if item["required"]],
    }


_FIELD_LABEL_MAX = 80


def _field_label(control: Control) -> str:
    """The page's own label for an account box. It is site text, never a value."""
    text = " ".join((control.label or control.aria_label or control.placeholder or "").split())
    return redact_site_error(text)[:_FIELD_LABEL_MAX]


def account_fields(snapshot: PageSnapshot) -> list[dict]:
    """Account boxes on this pane: email, password, confirmPassword, terms. No values.

    Workday marks every Create Account box required. Other sites use
    ``required`` / ``aria-required``. The account boxes themselves are always
    needed, so email and passwords count as required.
    """
    out: list[dict] = []
    email = _email_control(snapshot)
    if email is not None:
        out.append({"role": "email", "label": _field_label(email), "required": True})
    confirm = confirm_password_control(snapshot)
    for control in _passwords(snapshot):
        role = "confirmPassword" if confirm is not None and control.selector == confirm.selector else "password"
        if role == "password" and any(item["role"] == "password" for item in out):
            role = "confirmPassword"
        out.append({"role": role, "label": _field_label(control), "required": True})
    for control in snapshot.controls:
        if control.hidden or control.kind != "checkbox":
            continue
        blob = " ".join(part for part in (_control_blob(control), control.prompt, control.group) if part)
        account_box = control.automation_id.startswith("createAccount") if control.automation_id else False
        if account_box or _TERMS_RE.search(blob) or re.search(r"\bconsent\b", blob, re.IGNORECASE):
            required = bool(control.required) or account_box
            out.append({"role": "terms", "label": _field_label(control), "required": required})
            break
    return out[:6]


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


def stop_account(snapshot: PageSnapshot, url: str, ats: str | None, status: str) -> tuple[str, dict | None]:
    """Status and account description for an account stop.

    A page that scores as Create Account is never reported as Sign In: a
    ``LOGIN_REQUIRED`` stop on such a page becomes ``ACCOUNT_CREATION_REQUIRED``
    with ``mode=register``. A genuine Sign In page keeps ``LOGIN_REQUIRED`` and
    its sign-in description. An application form gets no account.
    """
    scored = score_account(snapshot, url)
    if scored.application:
        return status, None
    if status == "LOGIN_REQUIRED" and scored.mode == "register":
        return "ACCOUNT_CREATION_REQUIRED", account_view(
            snapshot, url, ats, mode="register", alternate=scored.alternate
        )
    if status == "ACCOUNT_CREATION_REQUIRED":
        return status, account_view(snapshot, url, ats, mode="register", alternate=scored.alternate)
    if status == "LOGIN_REQUIRED" and scored.mode == "login":
        return status, account_view(snapshot, url, ats, mode="login", alternate=scored.alternate)
    return status, None


def account_exists_text(snapshot: PageSnapshot) -> bool:
    blob = " ".join(part for part in [snapshot.banner, snapshot.heading, snapshot.page_text, *snapshot.alerts] if part)
    return bool(_EXISTS_RE.search(blob))


# "Sign in with email" beside Google / Apple / LinkedIn buttons. Workday tenants
# hide the email and password boxes behind it. Identity-provider buttons are not
# matched: Auto-Fill has no Google, Apple, or LinkedIn password for the candidate.
_EMAIL_PATH_RE = re.compile(
    r"^\s*(?:(?:sign\s*in|log\s*in|continue|sign\s*up|create account)\s+(?:with|using|via)\s+"
    r"(?:your\s+|my\s+)?(?:e-?mail(?:\s+address)?|password)"
    r"|use\s+(?:my\s+|your\s+)?e-?mail(?:\s+address)?(?:\s+instead)?)\s*$",
    re.IGNORECASE,
)
_EMAIL_PATH_IDS = frozenset({"SignInWithEmailButton", "signInWithEmailButton", "signInWithEmail"})
_EMAIL_PATH_WAIT_SECONDS = 4.0


def email_sign_in_button(snapshot: PageSnapshot):
    """The "Sign in with email" control on an SSO chooser, or None."""
    if snapshot.password_present or _passwords(snapshot):
        return None
    for item in [*snapshot.buttons, *snapshot.links]:
        if getattr(item, "in_header", False) or getattr(item, "in_ad", False) or getattr(item, "disabled", False):
            continue
        if not item.selector:
            continue
        if item.automation_id in _EMAIL_PATH_IDS or _EMAIL_PATH_RE.search(item.name or ""):
            return item
    return None


def open_email_sign_in(page, snapshot: PageSnapshot) -> PageSnapshot | None:
    """Click "Sign in with email" and wait for the email or password box.

    Returns the new snapshot, or None when there was nothing to click or the
    form did not appear. One click; no identity-provider button is touched.
    """
    target = email_sign_in_button(snapshot)
    if target is None:
        return None
    name = target.name or "Sign in with email"
    if not _EMAIL_PATH_RE.search(name):
        name = "Sign in with email"
    try:
        activate_account_switch(page, target.selector, name, control_type=getattr(target, "control_type", ""))
    except Exception:
        logger.info("email sign-in: click failed")
        return None
    deadline = time.monotonic() + _EMAIL_PATH_WAIT_SECONDS
    while True:
        try:
            fresh = extract_page(page)
        except Exception:
            fresh = None
        if fresh is not None and (fresh.password_present or _passwords(fresh) or fresh.captcha_present):
            logger.info("email sign-in: form opened")
            return fresh
        if time.monotonic() >= deadline:
            return fresh
        _pause(page, 150)
