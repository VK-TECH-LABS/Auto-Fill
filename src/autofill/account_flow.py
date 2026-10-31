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
    application_form_visible,
    clear_cookie_banner,
    login_failure_reason,
    password_meets_policy,
    password_policy,
    redact_site_error,
    score_account,
    sign_in_switch_target,
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
from autofill.models import ButtonControl, Control, PageSnapshot
from autofill.safeguards import SubmitBlockedError, activate_account, activate_account_switch, commit_native_choice

_logger = logging.getLogger("autofill.account")
_WAIT_SECONDS = 15.0
# After Create Account the form disappears while the site works. A page without
# a password box must stay that way this long before it counts as registered.
_SETTLE_SECONDS = 1.5
# Workday swaps Create Account for a spinner, then usually for an empty Sign In pane
# (verification email first). The spinner can run 2-3 s (Ameren, 2026-10-26), so on
# Workday an empty pane must stay empty, with no spinner, this long before it counts
# as registered. An application form still ends the wait at once.
_WORKDAY_SETTLE_SECONDS = 4.0
# RC-S1 (Nordson / Barry-Wehmiller 2026-10-27, after #77): a Workday Sign In pane
# opened from Create Account ignores its Sign In press for about half a second
# after it renders. The engine typed the saved login and pressed at once, the site
# sent nothing, and the wait ended ``pending`` with the password still in the box.
# After a pane switch the new pane must stay rendered this long before typing.
_PANE_DWELL_SECONDS = 1.0
# A press that sent no request and left the pane exactly as it was is pressed once
# more after this long. Nothing reached the employer, so the retry cannot count as
# a second sign-in attempt or create a second account.
_PRESS_CONFIRM_SECONDS = 3.0
_BUSY_JS = """() => {
  const nodes = document.querySelectorAll(
    "[data-automation-id*='loadingSpinner' i], [data-automation-id*='spinner' i], [aria-busy='true']");
  for (const node of nodes) {
    const box = node.getBoundingClientRect();
    const style = getComputedStyle(node);
    if (box.width > 0 && box.height > 0 && style.visibility !== 'hidden' && style.display !== 'none') {
      return true;
    }
  }
  return false;
}"""
# A new alert after the press counts only once it has stayed on screen this long
# (two polls about 0.5 s apart) with no spinner. Workday flashes validation text
# while it swaps Create Account for My Information (Aptiv, 2026-10-27).
_ERROR_PERSIST_SECONDS = 0.5
_VERIFY_FIRST_MESSAGE = (
    "The employer created the account and returned to Sign In. Workday sends a verification "
    "email first; signing in before it is opened can lock the account. Open that email, then press Continue."
)


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
    clear_cookie_banner(page)
    snapshot = extract_page(page)
    if snapshot.captcha_present:
        return _challenge(snapshot, page.url or "", ats)
    scored = score_account(snapshot, page.url or "")
    if scored.application or scored.mode == "none":
        report: dict = {"ok": False, "reason": "not_at_account_step"}
        if application_form_visible(snapshot):
            # The site already moved past the account step (an earlier press
            # worked). The service can resume instead of answering 409.
            report["applicationVisible"] = True
        return report
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
        # A saved login on a Create Account pane: open the pane's Sign In link
        # instead of refusing, so an existing account is not created twice.
        target = sign_in_switch_target(snapshot)
        if target is None:
            return {"ok": False, "reason": "mode_mismatch", "detected": "register"}
        try:
            activate_account_switch(page, target.selector, target.name)
        except SubmitBlockedError:
            return {"ok": False, "reason": "mode_mismatch", "detected": "register"}
        snapshot = _settled_mode(page, "login", dwell_s=_PANE_DWELL_SECONDS)
        scored = score_account(snapshot, page.url or "")
        if scored.mode != "login":
            return {"ok": False, "reason": "mode_mismatch", "detected": scored.mode or "register"}
    if scored.mode != mode:
        return {"ok": False, "reason": "mode_mismatch", "detected": scored.mode or "login"}
    if mode == "register":
        # Refuse before any keystroke. A secret that misses the rules on this
        # pane is not typed and Create Account is not clicked.
        policy_reason = password_meets_policy(password, password_policy(snapshot), email)
        if policy_reason:
            return {"ok": False, "reason": policy_reason}
    identity = _email_control(snapshot)
    primary, confirm_box = _password_targets(snapshot, mode)
    if primary is None:
        return {"ok": False, "reason": "not_at_account_step"}
    rows: list[dict] = []
    if mode == "register" and not standard_terms:
        # The flag is off, so nothing will be checked. Stop before the password
        # is typed when a required box is still empty.
        rows, pending = _accept_account_terms(page, snapshot, accept=False)
        if pending:
            rejected = _rejected(
                snapshot,
                page.url or "",
                ats,
                mode,
                scored.alternate,
                [f"{_CONSENT_PREFIX}{statement}" for statement in pending],
                secrets,
            )
            rejected["reason"] = "site_rejected"
            return _attach_proof(rejected, {}, rows, standard_terms=False)
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
    if mode == "register" and standard_terms:
        rows, pending = _accept_account_terms(page, snapshot, accept=True)
        if pending:
            rejected = _rejected(
                snapshot,
                page.url or "",
                ats,
                mode,
                scored.alternate,
                [f"{_CONSENT_PREFIX}{statement}" for statement in pending],
                secrets,
            )
            rejected["reason"] = "site_rejected"
            return _attach_proof(rejected, proof, rows, standard_terms=True)
    button = _primary_button(snapshot, mode)
    if button is None:
        return {"ok": False, "reason": "not_at_account_step"}
    # Alert text already on screen before the press (stale validation, a banner
    # from an earlier attempt) says nothing about what the press did.
    baseline = _pre_click_errors(page, secrets)
    try:
        _press_account_button(page, snapshot, button, mode=mode, secrets=secrets, baseline=baseline)
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
    outcome = _wait_for_outcome(
        page, mode=mode, ats=ats, alternate=scored.alternate, secrets=secrets, baseline=baseline
    )
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


_CONSENT_PREFIX = "Consent needs a person: "


def _required_box(control: Control) -> bool:
    # Workday validates its Create Account box in script, so the input may not
    # carry ``required``; its automation id says the site needs it.
    return bool(control.required or control.automation_id == "createAccountCheckbox")


def _accept_account_terms(page, snapshot: PageSnapshot, *, accept: bool) -> tuple[list[dict], list[str]]:
    """Check required plain terms when ``accept`` is true. Every other box stays unchecked.

    Returns the audit rows and the statements of required boxes that are still
    unchecked. The caller must not press the account button while that list is
    non-empty. ``accept`` false never calls the press.
    """
    rows: list[dict] = []
    pending: list[str] = []
    for control in snapshot.controls:
        if control.hidden or control.kind != "checkbox" or not control.selector:
            continue
        category = terms_category(control) if accept else "CONSENT"
        checked = False
        required = _required_box(control)
        already = required and _is_checked(page, control.selector) is True
        if accept and category in AUTO_CONSENT_CATEGORIES and required and control.input_type in {"", "checkbox"}:
            if check_registered(page, control.selector):
                record_auto_consent(control, category)
                checked = True
            else:
                _logger.info("auto_consent category=%s result=failed", category)
                category = "CONSENT"
        else:
            category = "CONSENT"
            checked = already
        if required and not checked:
            _logger.info("auto_consent category=CONSENT result=manual")
            pending.append(_statement(control) or "Required checkbox")
        rows.append({"textExcerpt": _statement(control), "category": category, "checked": checked})
    return rows, pending


_CHECKED_JS = """(selector) => {
  const el = document.querySelector(selector);
  if (!el) return null;
  if (el.type === "checkbox" || el.type === "radio") return !!el.checked;
  const state = el.getAttribute("aria-checked");
  return state === null ? null : state === "true";
}"""


def _is_checked(page, selector: str) -> bool | None:
    """The page's own checked state, or None when it cannot be read."""
    locator = page.locator(selector)
    reader = getattr(locator, "is_checked", None)
    if reader is not None:
        try:
            return bool(reader())
        except Exception:
            pass
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return None
    try:
        state = evaluate(_CHECKED_JS, selector)
    except Exception:
        return None
    return state if isinstance(state, bool) else None


def check_registered(page, selector: str) -> bool:
    """Check one native box and confirm the page kept it.

    Pointer check, then a forced check, then the label. A script that sets
    ``input.checked`` paints the box but a React form such as Workday never
    sees it. An unreadable state is not proof.
    """
    return commit_native_choice(page, selector, on=True)


def _covered_pane_button(snapshot: PageSnapshot, button: ButtonControl) -> bool:
    """True for the Workday submit button kept aria-hidden under its overlay."""
    if button.automation_id not in _PANE_SUBMIT_IDS:
        return False
    return not any(item.selector == button.selector for item in snapshot.buttons)


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


_PANE_SUBMIT_IDS = {"createAccountSubmitButton": "Create Account", "signInSubmitButton": "Sign In"}


# Header / utility controls that open another account dialog. They never submit
# the pane the person sees (Workday ``utilityButtonSignIn`` opens a second Sign In
# modal over the page).
_UTILITY_IDS = frozenset({"utilityButtonSignIn", "utilityMenuButton"})


def _pane_candidate(button) -> bool:
    """A button that belongs to the account pane, not the site header or an ad."""
    if getattr(button, "in_header", False) or getattr(button, "in_ad", False):
        return False
    if getattr(button, "disabled", False):
        return False
    return getattr(button, "automation_id", "") not in _UTILITY_IDS


def _primary_button(snapshot: PageSnapshot, mode: str):
    """The account pane's own Create Account / Sign In button.

    RC-L1 (Nordson / Barry-Wehmiller / GE Vernova / Aptiv 2026-10-27): on a Workday
    Sign In pane the real ``signInSubmitButton`` is aria-hidden under its
    ``click_filter``, so the name fallback matched the header ``utilityButtonSignIn``
    first. That click opened a second Sign In modal, nothing was submitted, the
    pane's password box disappeared, and the press read as ``signed_in``. The
    resume run then hit two ``click_filter`` overlays (Playwright strict-mode
    Error, session FAILED). Header and utility buttons are never the primary.
    """
    wanted = "createAccountSubmitButton" if mode == "register" else "signInSubmitButton"
    for button in snapshot.buttons:
        if button.automation_id == wanted:
            return button
    covered = _covered_pane_target(snapshot, wanted)
    prefer = _REGISTER_RE if mode == "register" else _LOGIN_RE
    for button in snapshot.buttons:
        if _pane_candidate(button) and prefer.search(button.name):
            return button
    for button in snapshot.buttons:
        if not _pane_candidate(button):
            continue
        folded = button.name.casefold()
        if "save" in folded or "submit" in folded or "apply" in folded:
            continue
        if re.search(r"\b(continue|next)\b", button.name, re.IGNORECASE):
            return button
    return covered


def _covered_pane_target(snapshot: PageSnapshot, wanted: str) -> ButtonControl | None:
    """The aria-hidden Workday submit under a nameless ``click_filter``.

    Extraction skips both: the button is aria-hidden and the overlay has no name.
    The pane id is still recorded, so the button is clicked at its centre, where
    the overlay takes the pointer like a person's click.
    """
    if wanted not in set(snapshot.automation_ids or []):
        return None
    return ButtonControl(
        name=_PANE_SUBMIT_IDS[wanted],
        selector=f"[data-automation-id='{wanted}']",
        control_type="button",
        automation_id=wanted,
        role="button",
    )


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
    if mode == "login" and login_failure_reason(snapshot) == "unverified":
        # F8 on the saved-login path: "verify your account before you sign in" is
        # the verify card, not a wrong password. Continue signs in once after the
        # person opens the verification email; nothing is typed again before that.
        status = "EMAIL_VERIFICATION_REQUIRED"
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


def _settled_mode(page, wanted: str, timeout_s: float = 4.0, *, dwell_s: float = 0.0) -> PageSnapshot:
    """Poll until the account pane shows ``wanted`` mode, or the timeout passes.

    RC-L3 (Aptiv 2026-10-27): right after the pane's Sign In link the old pane is
    gone and the new one has not rendered, so only the header "Sign In" is on
    screen and the page already scores as ``login``. The pane is settled only
    once its password box is there too.

    RC-S1: with ``dwell_s`` the settled pane must also stay settled that long, so
    the press is not made while Workday is still wiring the new pane.
    """
    deadline = time.monotonic() + timeout_s
    snapshot = extract_page(page)
    while not _pane_settled(snapshot, page.url or "", wanted) and time.monotonic() < deadline:
        time.sleep(0.15)
        snapshot = extract_page(page)
    if dwell_s <= 0 or not _pane_settled(snapshot, page.url or "", wanted):
        return snapshot
    _pause(page, dwell_s)
    again = extract_page(page)
    return again if _pane_settled(again, page.url or "", wanted) else snapshot


def _pause(page, seconds: float) -> None:
    """Wait while still letting Playwright deliver page events (requests)."""
    wait = getattr(page, "wait_for_timeout", None)
    if callable(wait):
        try:
            wait(seconds * 1000)
            return
        except Exception:
            pass
    time.sleep(seconds)


class _RequestWatch:
    """Counts non-GET requests and document loads the page starts.

    Disabled (``active`` False) on page objects without ``on`` / ``remove_listener``.
    """

    def __init__(self, page) -> None:
        self.count = 0
        self._page = page
        self.active = False
        on = getattr(page, "on", None)
        if callable(on) and callable(getattr(page, "remove_listener", None)):
            try:
                on("request", self._seen)
                self.active = True
            except Exception:
                self.active = False

    def _seen(self, request) -> None:
        try:
            method = str(getattr(request, "method", "") or "").upper()
            kind = str(getattr(request, "resource_type", "") or "")
        except Exception:
            method, kind = "", ""
        if method != "GET" or kind == "document":
            self.count += 1

    def close(self) -> None:
        if not self.active:
            return
        try:
            self._page.remove_listener("request", self._seen)
        except Exception:
            pass
        self.active = False


def _pane_unchanged(page, before: PageSnapshot, url: str, mode: str, secrets: list[str], baseline) -> bool:
    """The press did nothing visible: same pane, password still there, no new alert."""
    if _page_busy(page):
        return False
    try:
        now = extract_page(page)
    except Exception:
        return False
    if (page.url or "") != url or not now.password_present or now.captcha_present:
        return False
    if verification_present(now) or application_form_visible(now) or account_exists_text(now):
        return False
    if score_account(now, page.url or "").mode not in {mode, score_account(before, url).mode}:
        return False
    return not [item for item in _errors(now, secrets) if item not in baseline]


def _press_account_button(page, snapshot: PageSnapshot, button, *, mode: str, secrets: list[str], baseline) -> None:
    """Press the pane button; press once more only if the first press sent nothing.

    RC-S1 (Nordson / Barry-Wehmiller 2026-10-27): a press the site ignored left the
    typed saved login in a Sign In pane that never submitted. A second press is made
    only when the page started no request (no POST, no navigation) and still shows
    the same pane with no new alert, so nothing reached the employer the first time.
    Never more than two presses.
    """
    url = page.url or ""
    watch = _RequestWatch(page)
    try:
        activate_account(
            page,
            button.selector,
            button.name,
            control_type=button.control_type,
            at_point=_covered_pane_button(snapshot, button),
        )
        if not watch.active:
            return
        deadline = time.monotonic() + _PRESS_CONFIRM_SECONDS
        while time.monotonic() < deadline:
            _pause(page, 0.25)
            if watch.count:
                return
            if not _pane_unchanged(page, snapshot, url, mode, secrets, baseline):
                return
        if watch.count:
            return
        fresh = extract_page(page)
        retry = _primary_button(fresh, mode)
        if retry is None:
            return
        _logger.info("account_press_retry mode=%s reason=no_request", mode)
        try:
            activate_account(
                page,
                retry.selector,
                retry.name,
                control_type=retry.control_type,
                at_point=_covered_pane_button(fresh, retry),
            )
        except SubmitBlockedError:
            # The first press was allowed; the outcome wait reports what the page shows.
            _logger.info("account_press_retry mode=%s result=blocked", mode)
    finally:
        watch.close()


def _pane_settled(snapshot: PageSnapshot, url: str, wanted: str) -> bool:
    return score_account(snapshot, url).mode == wanted and bool(snapshot.password_present)


def _returned_to_sign_in(snapshot: PageSnapshot, url: str) -> bool:
    """After Create Account the site shows its Sign In pane with no error."""
    if not snapshot.password_present:
        return False
    scored = score_account(snapshot, url)
    return not scored.application and scored.mode == "login"


def _is_workday(ats: str | None, url: str) -> bool:
    lowered = (url or "").lower()
    return (ats or "").lower() == "workday" or "myworkdayjobs." in lowered or ".workday.com" in lowered


def _page_busy(page) -> bool:
    """A loading spinner or aria-busy region is on screen."""
    try:
        return bool(page.evaluate(_BUSY_JS))
    except Exception:
        return False


def _confirm_rejection(page, mode: str, secrets: list[str], baseline, seen: set[str]) -> dict | list[str]:
    """Re-read the page once before reporting a rejection.

    Returns a success dict when the site moved on (application form), an empty
    list when the account form is gone or the error went away (keep waiting),
    or the still-visible new errors.
    """
    try:
        fresh = extract_page(page)
    except Exception:
        return []
    if application_form_visible(fresh):
        _logger.info("account_outcome=%s reason=form_after_alert", _success(mode)["outcome"])
        return _success(mode)
    if not fresh.password_present or _page_busy(page):
        return []
    return [item for item in _errors(fresh, secrets) if item not in baseline and item in seen]


_NAVIGATING_NEEDLES = ("context was destroyed", "frame was detached", "navigat")


def _read_settling(page) -> PageSnapshot | None:
    """``extract_page``, or None while the document is being replaced."""
    try:
        return extract_page(page)
    except Exception as exc:
        text = f"{type(exc).__name__} {exc}".casefold()
        if "closed" not in text and any(needle in text for needle in _NAVIGATING_NEEDLES):
            return None
        raise


def _pre_click_errors(page, secrets: list[str]) -> frozenset[str]:
    """Redacted alert text on screen just before the account button is pressed."""
    try:
        return frozenset(_errors(extract_page(page), secrets))
    except Exception:
        return frozenset()


def _success(mode: str) -> dict:
    outcome = "registered" if mode == "register" else "signed_in"
    return {"ok": True, "outcome": outcome, "status": "", "siteErrors": [], "account": None}


def _wait_for_outcome(
    page,
    *,
    mode: str,
    ats: str | None,
    alternate: bool,
    secrets: list[str],
    baseline: frozenset[str] | set[str] = frozenset(),
) -> dict:
    deadline = time.monotonic() + _WAIT_SECONDS
    last = extract_page(page)
    clear_since: float | None = None
    # New alert text first seen at this time; it must persist before it counts.
    pending_errors: set[str] = set()
    pending_since: float | None = None
    workday = _is_workday(ats, page.url or "")
    settle = _WORKDAY_SETTLE_SECONDS if workday else _SETTLE_SECONDS
    while True:
        fresh = _read_settling(page)
        if fresh is None:
            # The press started a document navigation (a Sign In that reloads
            # the tenant). Keep waiting; the next read sees the new page.
            clear_since = None
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
            continue
        last = fresh
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
        errors = [item for item in _errors(last, secrets) if item not in baseline]
        if errors and last.password_present and not _page_busy(page):
            now = time.monotonic()
            since = pending_since
            persisted = since is not None and bool(pending_errors.intersection(errors))
            if since is not None and persisted and now - since >= _ERROR_PERSIST_SECONDS:
                confirmed = _confirm_rejection(page, mode, secrets, baseline, set(errors))
                if isinstance(confirmed, dict):
                    return confirmed
                if confirmed:
                    _logger.info(
                        "account_outcome=site_rejected mode=%s errors=%s",
                        mode,
                        "; ".join(confirmed)[:300],
                    )
                    return _rejected(extract_page(page), page.url or "", ats, mode, alternate, confirmed, secrets)
                pending_errors, pending_since = set(), None
            elif not persisted:
                pending_errors, pending_since = set(errors), now
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
            continue
        pending_errors, pending_since = set(), None
        if mode == "register" and _returned_to_sign_in(last, page.url or ""):
            # Workday accepted the form and went back to Sign In. Signing in
            # now, before the verification email, fails with "wrong email or
            # password or your account might be locked" and can lock it.
            _logger.info("account_outcome=registered next=verify_before_sign_in")
            return {
                "ok": True,
                "outcome": "registered",
                "status": "EMAIL_VERIFICATION_REQUIRED",
                "verifyBeforeSignIn": True,
                "message": _VERIFY_FIRST_MESSAGE,
                "siteErrors": [],
                "account": _view(last, page.url or "", ats, "login", False, []),
            }
        if not last.password_present:
            if application_form_visible(last):
                return _success(mode)
            if mode != "register" and not workday:
                return _success(mode)
            now = time.monotonic()
            if _page_busy(page):
                # The site is still working; an empty pane now says nothing yet.
                clear_since = None
            else:
                clear_since = clear_since if clear_since is not None else now
                if now - clear_since >= settle:
                    if mode != "register":
                        # RC-L2: a Workday Sign In press clears the pane at once and
                        # shows its spinner; a wrong-password or "verify your account"
                        # banner can still come back. Report signed_in only after the
                        # pane stayed empty and idle, so the resume run never types the
                        # password a second time into a Sign In that failed.
                        _logger.info("account_outcome=signed_in next=resume settle_s=%.1f", settle)
                        return _success(mode)
                    _logger.info("account_outcome=registered next=resume settle_s=%.1f", settle)
                    return {"ok": True, "outcome": "registered", "status": "", "siteErrors": [], "account": None}
        else:
            clear_since = None
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
    if mode == "register" and workday and not application_form_visible(last):
        # Still no form after the full wait: do not resume into Sign In with the new
        # password. The person checks the verification email, then presses Continue.
        _logger.info("account_outcome=registered next=verify_before_sign_in reason=no_settle")
        return {
            "ok": True,
            "outcome": "registered",
            "status": "EMAIL_VERIFICATION_REQUIRED",
            "verifyBeforeSignIn": True,
            "message": _VERIFY_FIRST_MESSAGE,
            "siteErrors": [],
            "account": None,
        }
    outcome = "registered" if mode == "register" else "signed_in"
    return {"ok": True, "outcome": outcome, "status": "", "siteErrors": [], "account": None}
