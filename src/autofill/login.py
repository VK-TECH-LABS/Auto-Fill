"""Detect a login form and sign in when the host supplied credentials.

Login submission is allowed. Final application Submit is not. Sign-up and
account creation are not. Passwords are written into the password field and
nowhere else: they are not returned, logged, or stored on the session.
"""

from __future__ import annotations

import logging
import re

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


def _type_secret(page, selector: str, value: str) -> None:
    """Put ``value`` into a login field. The value is not logged."""
    page.locator(selector).fill(value)


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
