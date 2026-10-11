"""Wait for a real ATS page to render before the first inspection.

A delayed form and a bot wall both look empty at the first paint. The wait
is bounded. An invisible reCAPTCHA or hCaptcha widget is not a wall.
"""

from __future__ import annotations

import re
import time
from urllib.parse import urlparse

from autofill.extract import extract_page
from autofill.models import ButtonControl, PageSnapshot
from autofill.safeguards import is_entry_label

_WORKDAY_HOST = re.compile(r"^[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com$", re.IGNORECASE)
_DECISIVE_IDS = frozenset({"createAccountSubmitButton", "signInSubmitButton", "verifyPassword"})


def visible_fields(snapshot: PageSnapshot) -> list:
    """Fillable controls that are on screen. Buttons are not fields."""
    return [control for control in snapshot.controls if not control.hidden]


def is_review(snapshot: PageSnapshot) -> bool:
    """True when the heading says this is the review step."""
    return "review" in (snapshot.heading or "").casefold()


def render_ready(snapshot: PageSnapshot) -> bool:
    """True when inspection would see a form, a wall, or an entry/login choice."""
    if snapshot.captcha_present or visible_fields(snapshot):
        return True
    names = " ".join(button.name.casefold() for button in snapshot.buttons)
    markers = ("apply", "sign in", "log in", "create account")
    return any(marker in names for marker in markers)


def _timeout(exc: BaseException) -> bool:
    if "timeout" in type(exc).__name__.casefold():
        return True
    text = str(exc).casefold()
    return "timeout" in text and "exceeded" in text


def _closed(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".casefold()
    return "has been closed" in text or "browser closed" in text or "target closed" in text


def wait_for_render(page, *, timeout_s: float = 12.0) -> None:
    """Network idle, then poll until a form or a known marker appears.

    ``timeout_s`` caps the poll. A page that already has controls returns
    on the first look. Timeouts from the idle wait are ignored. A dead
    browser or a timeout while reading the page is raised.
    """
    load = getattr(page, "wait_for_load_state", None)
    if load is not None:
        for state, budget in (("domcontentloaded", 2000), ("networkidle", 1500)):
            try:
                load(state, timeout=budget)
            except Exception as exc:
                if _closed(exc):
                    raise
                continue
    deadline = time.monotonic() + timeout_s
    while True:
        snapshot = extract_page(page)
        if render_ready(snapshot):
            _settle_account_pane(page, snapshot)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _settle_account_pane(page, snapshot)
            return
        pause = getattr(page, "wait_for_timeout", None)
        try:
            if pause is None:
                time.sleep(min(0.2, remaining))
            else:
                pause(int(min(200, remaining * 1000)))
        except Exception as exc:
            if _closed(exc) or _timeout(exc):
                raise
            return


def choose_entry(snapshot: PageSnapshot) -> ButtonControl | None:
    """The entry button on a page that has no application form yet.

    ``Apply Manually`` wins over a generic ``Apply``. Submit is never chosen.
    """
    if snapshot.captcha_present or visible_fields(snapshot) or is_review(snapshot):
        return None
    manual = [
        button
        for button in snapshot.buttons
        if is_entry_label(button.name) and button.name.casefold().strip() == "apply manually"
    ]
    if manual:
        return manual[0]
    for button in snapshot.buttons:
        if (button.control_type or "").casefold() == "submit":
            continue
        if is_entry_label(button.name):
            return button
    for link in snapshot.links:
        if is_entry_label(link.name) and link.selector:
            return ButtonControl(name=link.name, selector=link.selector)
    return None


def _host(page) -> str:
    return (urlparse(str(getattr(page, "url", "") or "")).hostname or "").lower()


def _account_chrome(snapshot: PageSnapshot) -> bool:
    names = " ".join(button.name.casefold() for button in snapshot.buttons)
    names = f"{names} {(snapshot.heading or '').casefold()}"
    return "sign in" in names or "log in" in names or "create account" in names


def _application_fields(snapshot: PageSnapshot) -> bool:
    for control in snapshot.controls:
        if control.hidden or control.kind in {"password", "hidden"}:
            continue
        if control.kind in {"file", "textarea", "select", "radio", "combobox"}:
            return True
        if control.kind == "text" and control.input_type not in {"email", "password"}:
            return True
    return False


def _account_signature(snapshot: PageSnapshot) -> tuple:
    ids = tuple(
        sorted(
            item for item in snapshot.automation_ids if item in _DECISIVE_IDS or item == "createAccountLink"
        )
    )
    passwords = tuple(
        (control.selector, control.automation_id, control.label, control.aria_label)
        for control in snapshot.controls
        if control.kind == "password" and not control.hidden
    )
    buttons = tuple(sorted(button.name.casefold() for button in snapshot.buttons))
    return (snapshot.heading or "", ids, passwords, buttons)


def _account_decisive(snapshot: PageSnapshot) -> bool:
    if set(snapshot.automation_ids) & _DECISIVE_IDS:
        return True
    if snapshot.password_present and _account_chrome(snapshot):
        return True
    heading = (snapshot.heading or "").casefold()
    if snapshot.password_present and ("create account" in heading or "sign in" in heading or "log in" in heading):
        return True
    return False


def _should_settle(page, snapshot: PageSnapshot) -> bool:
    """Wait only for an account shell. A landing Apply button is not one."""
    if snapshot.captcha_present or choose_entry(snapshot) is not None:
        return False
    if _application_fields(snapshot) and not snapshot.password_present:
        return False
    workday = bool(_WORKDAY_HOST.match(_host(page)))
    return bool(snapshot.password_present or workday or _account_chrome(snapshot))


def _pause(page, millis: int) -> None:
    pause = getattr(page, "wait_for_timeout", None)
    try:
        if pause is None:
            time.sleep(millis / 1000)
        else:
            pause(millis)
    except Exception as exc:
        if _closed(exc) or _timeout(exc):
            raise


def _settle_account_pane(page, snapshot: PageSnapshot) -> None:
    """Poll until a Workday account pane, or any sign-in form, stops changing.

    The first paint of a Workday SPA can be a Sign In shell. Classification
    waits until the Create Account or Sign In controls are stable.
    """
    if not _should_settle(page, snapshot):
        return
    workday = bool(_WORKDAY_HOST.match(_host(page)))
    budget = 3.5 if workday and not _account_decisive(snapshot) else 0.6
    deadline = time.monotonic() + budget
    previous = _account_signature(snapshot)
    seen_decisive = _account_decisive(snapshot)
    while time.monotonic() < deadline:
        _pause(page, 200)
        try:
            current = extract_page(page)
        except Exception as exc:
            if _closed(exc):
                raise
            return
        signature = _account_signature(current)
        decisive = _account_decisive(current)
        if decisive and seen_decisive and signature == previous:
            return
        previous = signature
        seen_decisive = decisive


def is_login_wall(snapshot: PageSnapshot) -> bool:
    """Sign-in or create-account choices with no form and no entry button."""
    if snapshot.captcha_present or visible_fields(snapshot) or is_review(snapshot):
        return False
    if choose_entry(snapshot) is not None:
        return False
    for button in snapshot.buttons:
        name = button.name.casefold()
        if "sign in" in name or "log in" in name or "create account" in name:
            return True
    return False
