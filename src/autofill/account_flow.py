"""Type into an account form only after the caller asks for login or register.

The automatic fill does not call this module. Submit Application stays forbidden.
Password values are passed through and are not logged.
"""

from __future__ import annotations

import logging
import re
import time

from autofill.extract import extract_page
from autofill.login import (
    _CONFIRM_RE,
    _LOGIN_RE,
    _REGISTER_RE,
    SecretInputError,
    account_exists_text,
    account_view,
    redact_site_error,
    score_account,
    verification_present,
)
from autofill.mapping import (
    AUTO_CONSENT_CATEGORIES,
    bind_standard_terms,
    consume_auto_consents,
    record_auto_consent,
    standard_terms_enabled,
    terms_category,
)
from autofill.models import Control, PageSnapshot
from autofill.safeguards import SubmitBlockedError, activate_account, activate_account_switch

_logger = logging.getLogger("autofill.account")
_EEO_RE = re.compile(r"\b(eeo|equal employment|self-identif|voluntary|work authorization)\b", re.IGNORECASE)
_TERMS_RE = re.compile(r"\b(terms|privacy)\b", re.IGNORECASE)
_WAIT_SECONDS = 15.0


def run_account_action(
    page,
    *,
    mode: str,
    email: str,
    username: str,
    password: str,
    confirm: str,
    ats: str | None,
    standard_terms: bool = False,
) -> dict:
    """Fill the current account form and classify what the site did next."""
    bind_standard_terms(bool(standard_terms))
    try:
        return _perform_account_action(
            page,
            mode=mode,
            email=email,
            username=username,
            password=password,
            confirm=confirm,
            ats=ats,
            standard_terms=standard_terms,
        )
    finally:
        if standard_terms_enabled():
            consume_auto_consents()


def _perform_account_action(
    page,
    *,
    mode: str,
    email: str,
    username: str,
    password: str,
    confirm: str,
    ats: str | None,
    standard_terms: bool = False,
) -> dict:
    secrets = [value for value in (email, username, password, confirm) if value]
    snapshot = extract_page(page)
    if snapshot.captcha_present:
        return _challenge(snapshot, page.url or "", ats)
    scored = score_account(snapshot, page.url or "")
    if scored.application or scored.mode == "none":
        return {"ok": False, "reason": "not_at_account_step"}
    if mode == "register" and scored.mode == "login":
        if not scored.alternate:
            return {"ok": False, "reason": "mode_mismatch", "detected": "login"}
        switch = _switch_target(snapshot)
        if switch is None:
            return {"ok": False, "reason": "mode_mismatch", "detected": "login"}
        selector, name = switch
        try:
            activate_account_switch(page, selector, name)
        except SubmitBlockedError:
            return {"ok": False, "reason": "mode_mismatch", "detected": "login"}
        snapshot = extract_page(page)
        scored = score_account(snapshot, page.url or "")
        if scored.mode != "register":
            return {"ok": False, "reason": "mode_mismatch", "detected": scored.mode or "login"}
    if mode == "login" and scored.mode == "register":
        return {"ok": False, "reason": "mode_mismatch", "detected": "register"}
    if scored.mode != mode:
        return {"ok": False, "reason": "mode_mismatch", "detected": scored.mode or "login"}
    terms = None
    if not standard_terms:
        terms, terms_error = _terms_checkbox(snapshot)
        if terms_error:
            return _rejected(snapshot, page.url or "", ats, mode, scored.alternate, [terms_error], secrets)
    identity = _email_control(snapshot)
    primary, confirm_box = _password_targets(snapshot, mode)
    if primary is None:
        return {"ok": False, "reason": "not_at_account_step"}
    try:
        if identity is not None and not identity.read_only:
            _type_secret(page, identity.selector, username if _username_field(identity) and username else email)
        _type_secret(page, primary.selector, password)
        if mode == "register" and confirm_box is not None:
            _type_secret(page, confirm_box.selector, confirm or password)
    except SecretInputError:
        return {"ok": False, "reason": "input_failed"}
    proof = _field_proof(page, identity, primary, confirm_box, password, confirm, email, username)
    if mode == "register" and not proof["passwordsMatch"]:
        return {"ok": False, "reason": "password_mismatch", "fieldProof": proof}
    rows: list[dict] = []
    if standard_terms:
        rows = _accept_account_terms(page, snapshot)
    elif terms is not None:
        page.locator(terms.selector).check()
    button = _primary_button(snapshot, mode)
    if button is None:
        return {"ok": False, "reason": "not_at_account_step"}
    try:
        activate_account(page, button.selector, button.name, control_type=button.control_type)
    except SubmitBlockedError:
        rejected = _rejected(
            snapshot,
            page.url or "",
            ats,
            mode,
            scored.alternate,
            ["The account button could not be used."],
            secrets,
        )
        return _attach_proof(rejected, proof, rows, standard_terms=standard_terms)
    outcome = _wait_for_outcome(page, mode=mode, ats=ats, alternate=scored.alternate, secrets=secrets)
    return _attach_proof(outcome, proof, rows, standard_terms=standard_terms)


def _field_proof(
    page,
    identity: Control | None,
    primary: Control | None,
    confirm_box: Control | None,
    password: str,
    confirm: str,
    email: str,
    username: str,
) -> dict:
    """Lengths and equality only. The typed values are dropped and not returned."""
    email_value = ""
    left = ""
    right = ""
    try:
        if identity is not None and identity.selector:
            email_value = page.locator(identity.selector).input_value() or ""
        if primary is not None and primary.selector:
            left = page.locator(primary.selector).input_value() or ""
        if confirm_box is not None and confirm_box.selector:
            right = page.locator(confirm_box.selector).input_value() or ""
    except Exception:
        email_value = ""
        left = ""
        right = ""
    expected_email = username if identity is not None and _username_field(identity) and username else email
    expected_password = confirm or password
    proof = {
        "emailFilled": bool(email_value),
        "emailLength": len(email_value),
        "emailMatches": bool(email_value) and email_value == expected_email,
        "passwordLength": len(left),
        "verifyLength": len(right),
        "passwordsMatch": bool(left) and left == right and left == password and right == expected_password,
    }
    del email_value, left, right, expected_email, expected_password
    return proof


def _statement(control: Control) -> str:
    return " ".join((control.label or control.prompt or control.group or control.aria_label or "").split())[:160]


def _accept_account_terms(page, snapshot: PageSnapshot) -> list[dict]:
    """Check required plain terms. Every other checkbox stays unchecked."""
    rows: list[dict] = []
    for control in snapshot.controls:
        if control.hidden or control.kind != "checkbox" or not control.selector:
            continue
        category = terms_category(control)
        checked = False
        if category in AUTO_CONSENT_CATEGORIES and control.required:
            try:
                page.locator(control.selector).check()
            except Exception:
                _logger.info("auto_consent category=%s result=failed", category)
                category = "CONSENT"
            else:
                record_auto_consent(control, category)
                checked = True
        else:
            category = "CONSENT"
        rows.append({"textExcerpt": _statement(control), "category": category, "checked": checked})
    return rows


def _attach_proof(result: dict, proof: dict, rows: list[dict], *, standard_terms: bool) -> dict:
    body = dict(result)
    body["fieldProof"] = proof
    if standard_terms:
        body["autoConsents"] = consume_auto_consents()
        body["consentRows"] = rows
    if body.get("status") == "READY_FOR_HUMAN_SUBMIT":
        body["status"] = "ACCOUNT_CREATION_REQUIRED"
    return body


def _email_control(snapshot: PageSnapshot) -> Control | None:
    from autofill.login import _email_control as find_email

    return find_email(snapshot)


def _username_field(control: Control) -> bool:
    blob = " ".join((control.label, control.autocomplete, control.name)).casefold()
    return "user" in blob and "email" not in blob


def _type_secret(page, selector: str, value: str) -> None:
    if not selector or not value:
        return
    from autofill.login import _type_secret as type_secret

    type_secret(page, selector, value)


def _password_targets(snapshot: PageSnapshot, mode: str) -> tuple[Control | None, Control | None]:
    passwords = [control for control in snapshot.controls if control.kind == "password" and not control.hidden]
    primary = None
    confirm_box = None
    for control in passwords:
        blob = " ".join((control.label, control.placeholder, control.aria_label, control.name))
        if control.automation_id == "verifyPassword" or _CONFIRM_RE.search(blob):
            confirm_box = control
        elif primary is None:
            primary = control
    if primary is None and passwords:
        primary = passwords[0]
    if mode == "register" and confirm_box is None and len(passwords) >= 2:
        confirm_box = passwords[1] if passwords[1] is not primary else None
    return primary, confirm_box


def _primary_button(snapshot: PageSnapshot, mode: str):
    wanted = "createAccountSubmitButton" if mode == "register" else "signInSubmitButton"
    for button in snapshot.buttons:
        if button.automation_id == wanted:
            return button
    prefer = _REGISTER_RE if mode == "register" else _LOGIN_RE
    for button in snapshot.buttons:
        if prefer.search(button.name):
            return button
    for button in snapshot.buttons:
        folded = button.name.casefold()
        if "save" in folded or "submit" in folded or "apply" in folded:
            continue
        if re.search(r"\b(continue|next)\b", button.name, re.IGNORECASE):
            return button
    return None


def _switch_target(snapshot: PageSnapshot) -> tuple[str, str] | None:
    for link in snapshot.links:
        if link.automation_id == "createAccountLink" or _REGISTER_RE.search(link.name):
            if link.selector and link.name:
                return link.selector, link.name
    for button in snapshot.buttons:
        if button.automation_id == "createAccountLink" or _REGISTER_RE.search(button.name):
            if "submit application" in button.name.casefold():
                continue
            if button.selector and button.name:
                return button.selector, button.name
    return None


def _terms_checkbox(snapshot: PageSnapshot) -> tuple[Control | None, str | None]:
    matches: list[Control] = []
    unsure = False
    for control in snapshot.controls:
        if control.hidden or control.kind != "checkbox":
            continue
        blob = " ".join(
            part
            for part in (control.label, control.nearby, control.prompt, control.group, control.aria_label)
            if part
        )
        if _EEO_RE.search(blob):
            if control.required:
                unsure = True
            continue
        if _TERMS_RE.search(blob):
            matches.append(control)
            continue
        if control.required:
            unsure = True
    required = [control for control in matches if control.required]
    if len(required) == 1 and not unsure:
        return required[0], None
    if required or unsure:
        return None, "Terms consent needs a person"
    return None, None


def _errors(snapshot: PageSnapshot, secrets: list[str]) -> list[str]:
    seen: list[str] = []
    for raw in [snapshot.banner, *snapshot.alerts]:
        cleaned = redact_site_error(raw, secrets)
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
        if len(seen) >= 5:
            break
    return seen


def _view(snapshot: PageSnapshot, url: str, ats: str | None, mode: str, alternate: bool, errors: list[str]) -> dict:
    return account_view(snapshot, url, ats, mode=mode, alternate=alternate, site_errors=errors)


def _rejected(
    snapshot: PageSnapshot,
    url: str,
    ats: str | None,
    mode: str,
    alternate: bool,
    errors: list[str],
    secrets: list[str],
) -> dict:
    cleaned = [redact_site_error(item, secrets) for item in errors if item][:5]
    status = "ACCOUNT_CREATION_REQUIRED" if mode == "register" else "LOGIN_FAILED"
    return {
        "ok": False,
        "reason": "account_exists" if account_exists_text(snapshot) else "site_rejected",
        "status": status,
        "siteErrors": cleaned or _errors(snapshot, secrets),
        "account": _view(snapshot, url, ats, mode, alternate, cleaned),
    }


def _challenge(snapshot: PageSnapshot, url: str, ats: str | None) -> dict:
    return {
        "ok": True,
        "outcome": "pending",
        "status": "CAPTCHA_REQUIRED",
        "siteErrors": [],
        "account": None,
        "url": url,
        "ats": ats,
    }


def _wait_for_outcome(page, *, mode: str, ats: str | None, alternate: bool, secrets: list[str]) -> dict:
    deadline = time.monotonic() + _WAIT_SECONDS
    last = extract_page(page)
    while True:
        last = extract_page(page)
        if last.captcha_present:
            return _challenge(last, page.url or "", ats)
        if verification_present(last):
            return {
                "ok": True,
                "outcome": "verification_required",
                "status": "EMAIL_VERIFICATION_REQUIRED",
                "siteErrors": [],
                "account": None,
            }
        if account_exists_text(last):
            rejected = _rejected(last, page.url or "", ats, mode, alternate, _errors(last, secrets), secrets)
            rejected["reason"] = "account_exists"
            return rejected
        errors = _errors(last, secrets)
        if errors and last.password_present:
            return _rejected(last, page.url or "", ats, mode, alternate, errors, secrets)
        if not last.password_present:
            outcome = "registered" if mode == "register" else "signed_in"
            return {"ok": True, "outcome": outcome, "status": "", "siteErrors": [], "account": None}
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    if last.password_present:
        return {
            "ok": True,
            "outcome": "pending",
            "status": "ACCOUNT_CREATION_REQUIRED" if mode == "register" else "LOGIN_REQUIRED",
            "siteErrors": [],
            "account": _view(last, page.url or "", ats, mode, alternate, []),
        }
    outcome = "registered" if mode == "register" else "signed_in"
    return {"ok": True, "outcome": outcome, "status": "", "siteErrors": [], "account": None}
