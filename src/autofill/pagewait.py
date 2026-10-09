"""Wait for a real ATS page to render before the first inspection.

A delayed form and a bot wall both look empty at the first paint. The wait
is bounded. An invisible reCAPTCHA badge is not a form and is not a wall.
"""

from __future__ import annotations

import time

from autofill.extract import extract_page
from autofill.models import ButtonControl, PageSnapshot
from autofill.safeguards import is_entry_label


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
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
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
        if is_entry_label(button.name):
            return button
    return None


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
