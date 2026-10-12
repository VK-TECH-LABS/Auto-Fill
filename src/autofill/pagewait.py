"""Wait for a real ATS page to render before the first inspection.

A delayed form and a bot wall both look empty at the first paint. The wait
is bounded. An invisible reCAPTCHA or hCaptcha widget is not a wall.
"""

from __future__ import annotations

import contextvars
import re
import time
from urllib.parse import urlparse

from autofill.extract import extract_page
from autofill.login import _CONFIRM_RE as _VERIFY_RE
from autofill.login import _LOGIN_RE, _REGISTER_RE
from autofill.models import ButtonControl, Control, PageSnapshot
from autofill.safeguards import classify_control, is_entry_label, semantic_action

_WORKDAY_HOST = re.compile(r"^[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com$", re.IGNORECASE)
_DECISIVE_IDS = frozenset({"createAccountSubmitButton", "signInSubmitButton", "verifyPassword"})


def visible_fields(snapshot: PageSnapshot) -> list:
    """Fillable controls that are on screen. Buttons are not fields."""
    return [control for control in snapshot.controls if not control.hidden]


def is_review(snapshot: PageSnapshot) -> bool:
    """True when the heading says this is the review step."""
    return "review" in (snapshot.heading or "").casefold()


_PAGE_TYPE: contextvars.ContextVar[str] = contextvars.ContextVar("autofill_page_type", default="")
_CONFIRM_RE = re.compile(
    r"\b(thank you for applying|thanks for applying|application (has been |was )?submitted|"
    r"we (have )?received your application|application received)\b",
    re.IGNORECASE,
)
_JOB_RE = re.compile(
    r"\b(job description|responsibilities|qualifications|about the role|about this (job|role)|"
    r"what you'?ll do|apply for this job)\b",
    re.IGNORECASE,
)
_SEARCH_RE = re.compile(r"\b(search|query|filter jobs|keyword)\b", re.IGNORECASE)


def note_page_type(kind: str) -> None:
    """Remember the page classification for the status payload."""
    _PAGE_TYPE.set(kind or "")


def reset_page_type() -> None:
    """Drop a classification left by an earlier run on this thread."""
    _PAGE_TYPE.set("")


def current_page_type() -> str:
    return _PAGE_TYPE.get() or "UNKNOWN"


def application_controls(snapshot: PageSnapshot) -> list[Control]:
    """Controls that belong to an application. A site search box is not one."""
    found: list[Control] = []
    for control in snapshot.controls:
        if control.hidden or control.kind in {"hidden", "password", "button"}:
            continue
        blob = " ".join(
            part
            for part in (control.label, control.name, control.placeholder, control.aria_label, control.input_type)
            if part
        )
        if control.input_type == "search" or _SEARCH_RE.search(blob):
            continue
        if control.kind in {"file", "textarea", "select", "radio", "checkbox", "combobox", "buttons"}:
            found.append(control)
            continue
        if control.kind == "text" or control.input_type in {"email", "tel", "text", "url", "number"}:
            found.append(control)
    return found


def classify_page(snapshot: PageSnapshot, *, url: str = "") -> str:
    """One of the public page types. A posting is not an application form."""
    if snapshot.captcha_present:
        note_page_type("UNKNOWN")
        return "UNKNOWN"
    text = f"{snapshot.heading} {snapshot.page_text}"
    if _CONFIRM_RE.search(text):
        note_page_type("CONFIRMATION")
        return "CONFIRMATION"
    if register_pane(snapshot):
        note_page_type("ACCOUNT")
        return "ACCOUNT"
    if sign_in_pane(snapshot):
        note_page_type("LOGIN")
        return "LOGIN"
    if is_review(snapshot):
        note_page_type("REVIEW")
        return "REVIEW"
    fields = application_controls(snapshot)
    entry = find_entry_control(snapshot)
    formish = [
        control
        for control in fields
        if control.kind in {"file", "textarea", "select", "radio", "combobox"}
        or control.input_type in {"email", "tel"}
    ]
    # A search box is not an application. One real field with no Apply control is.
    if formish or (fields and entry is None):
        note_page_type("APPLICATION_FORM")
        return "APPLICATION_FORM"
    if entry is not None:
        note_page_type("APPLY_ENTRY")
        return "APPLY_ENTRY"
    if not fields and any(
        "create account" in button.name.casefold() or "register" in button.name.casefold()
        for button in snapshot.buttons
    ):
        note_page_type("ACCOUNT")
        return "ACCOUNT"
    if not fields and (_account_chrome(snapshot) or is_login_wall(snapshot)):
        kind = "ACCOUNT" if "create account" in text.casefold() else "LOGIN"
        note_page_type(kind)
        return kind
    if _JOB_RE.search(text):
        note_page_type("JOB_DESCRIPTION")
        return "JOB_DESCRIPTION"
    note_page_type("UNKNOWN")
    return "UNKNOWN"


def has_final_submit(snapshot: PageSnapshot, remembered: list[str] | None = None) -> bool:
    """True when this page shows a control a person would use to send the application.

    A remembered name counts only when that same control is still on the page.
    An Apply button that is only the posting's entry control is not enough on
    its own: the page type decides whether the run may become ready.
    """
    visible = {_entry_name(button.name) for button in snapshot.buttons}
    for button in snapshot.buttons:
        if classify_control(button.name, control_type=button.control_type or "") == "submit":
            return True
    for name in remembered or []:
        if _entry_name(str(name)) in visible and classify_control(str(name)) == "submit":
            return True
    return False


def terminal_kind(
    snapshot: PageSnapshot,
    *,
    url: str = "",
    gaps: list,
    required_total: int,
    submit_names: list[str] | None = None,
    required_done: int | None = None,
    fields_detected: int | None = None,
    blocked: str = "",
) -> str:
    """Page decision. READY only when :func:`ready_to_submit` agrees.

    Account, sign-in, and job pages are scored before a zero-field
    ``NO_FORM``. ``NO_FORM`` is only a page that is none of those.
    """
    if snapshot.captcha_present or blocked == "captcha":
        classify_page(snapshot, url=url)
        return "CAPTCHA"
    if register_pane(snapshot):
        note_page_type("ACCOUNT")
        return "ACCOUNT"
    if sign_in_pane(snapshot):
        note_page_type("LOGIN")
        return "LOGIN"
    kind = classify_page(snapshot, url=url)
    if kind == "ACCOUNT":
        return "ACCOUNT"
    if kind == "LOGIN":
        return "LOGIN"
    if kind in {"JOB_DESCRIPTION", "APPLY_ENTRY"}:
        return "JOB"
    if gaps or blocked == "resume":
        return "MANUAL"
    detected = fields_detected if fields_detected is not None else len(application_controls(snapshot))
    done = required_done if required_done is not None else (required_total if not gaps else 0)
    if ready_to_submit(
        page_type=kind,
        fields_detected=detected,
        required_total=required_total,
        required_done=done,
        unresolved=len(gaps),
        blocked=blocked,
        has_submit=has_submit_or_review(snapshot, submit_names),
    ):
        return "READY"
    if kind in {"APPLICATION_FORM", "REVIEW"}:
        return "FILLED"
    return "NO_FORM"


def _password_controls(snapshot: PageSnapshot) -> list[Control]:
    return [control for control in snapshot.controls if control.kind == "password" and not control.hidden]


def _pane_text(snapshot: PageSnapshot) -> str:
    names = " ".join(button.name for button in snapshot.buttons)
    labels = " ".join(
        " ".join(
            part
            for part in (control.label, control.placeholder, control.aria_label, control.name)
            if part
        )
        for control in snapshot.controls
        if not control.hidden
    )
    return " ".join(part for part in (snapshot.heading, snapshot.page_text, names, labels) if part)


def register_pane(snapshot: PageSnapshot) -> bool:
    """Email / password / verify password / Create Account, including page text.

    Scored before a zero-field or ``NO_FORM`` decision. A job posting that
    merely mentions an account is not this pane.
    """
    if snapshot.captcha_present:
        return False
    text = _pane_text(snapshot)
    passwords = _password_controls(snapshot)
    verify = len(passwords) >= 2 or bool(_VERIFY_RE.search(text))
    create = bool(_REGISTER_RE.search(snapshot.heading or "")) or any(
        _REGISTER_RE.search(button.name) for button in snapshot.buttons
    )
    if not create and _REGISTER_RE.search(snapshot.page_text or ""):
        create = verify
    passwordish = bool(passwords) or snapshot.password_present or bool(re.search(r"\bpassword\b", text, re.IGNORECASE))
    return bool(verify and create and passwordish)


def sign_in_pane(snapshot: PageSnapshot) -> bool:
    """A sign-in form. A create-account pane, a posting, or an application is not one."""
    if snapshot.captcha_present or is_review(snapshot) or register_pane(snapshot):
        return False
    if find_entry_control(snapshot) is not None or _application_fields(snapshot):
        return False
    passwords = _password_controls(snapshot)
    login_button = any(_LOGIN_RE.search(button.name) for button in snapshot.buttons)
    login_heading = bool(_LOGIN_RE.search(snapshot.heading or ""))
    if passwords or snapshot.password_present:
        return bool(login_button or login_heading or _account_chrome(snapshot))
    if application_controls(snapshot):
        return False
    return bool(login_button or login_heading)


def has_submit_or_review(snapshot: PageSnapshot, remembered: list[str] | None = None) -> bool:
    """A final submit or a review control on this page."""
    if has_final_submit(snapshot, remembered):
        return True
    for button in snapshot.buttons:
        if semantic_action(button.name, control_type=button.control_type or "") == "review":
            return True
    return False


def ready_to_submit(
    *,
    page_type: str,
    fields_detected: int,
    required_total: int,
    required_done: int,
    unresolved: int,
    blocked: str = "",
    has_submit: bool,
) -> bool:
    """The only READY decision. Every caller uses this predicate.

    READY requires an application or review page, at least one detected field,
    every required field committed, no unresolved blocker, no account, login,
    captcha, or resume checkpoint, and a submit or review control.
    """
    if page_type not in {"APPLICATION_FORM", "REVIEW"}:
        return False
    if fields_detected <= 0:
        return False
    if required_total > 0 and required_done < required_total:
        return False
    if unresolved:
        return False
    if (blocked or "") in {"account", "login", "captcha", "resume"}:
        return False
    return bool(has_submit)


def render_ready(snapshot: PageSnapshot) -> bool:
    """True when inspection would see a form, a wall, or an entry/login choice."""
    if snapshot.captcha_present or visible_fields(snapshot):
        return True
    if register_pane(snapshot) or sign_in_pane(snapshot):
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


def _entry_name(name: str) -> str:
    return " ".join(name.split()).casefold().replace("\u2019", "'").replace("\u2018", "'")


def _known_entry(name: str, selector: str, control_type: str = "") -> bool:
    """An employer entry control. Submit application is never one.

    A bare ``Apply`` whose type is submit stays on the final-submit path.
    Greenhouse ``#apply_button``, a Lever apply posting button, and the
    labeled entry phrases are entry controls.
    """
    label = _entry_name(name)
    if re.search(r"\b(submit|send(\s+my)?\s+application)\b", label):
        return False
    selector_text = (selector or "").casefold()
    if selector_text in {"#apply_button"} or selector_text.endswith("#apply_button"):
        return True
    if "postings-btn" in selector_text and "apply" in label:
        return True
    # A bare Apply whose type is submit stays on the final-submit path.
    if (control_type or "").casefold() == "submit" and label == "apply":
        return False
    if is_entry_label(name):
        return True
    if label.startswith("apply for this job") or label.startswith("apply now"):
        return True
    if label.startswith("i'm interested") or label.startswith("im interested"):
        return True
    return False


def find_entry_control(snapshot: PageSnapshot) -> ButtonControl | None:
    """The employer's Apply control, including on a posting that also has a search box.

    ``Apply Manually`` wins over a generic ``Apply``. Submit application is never chosen.
    """
    if snapshot.captcha_present or is_review(snapshot):
        return None
    candidates: list[ButtonControl] = []
    for button in snapshot.buttons:
        if _known_entry(button.name, button.selector, button.control_type):
            candidates.append(button)
    for link in snapshot.links:
        if link.selector and _known_entry(link.name, link.selector):
            candidates.append(ButtonControl(name=link.name, selector=link.selector))
    manual = [button for button in candidates if _entry_name(button.name) == "apply manually"]
    if manual:
        return manual[0]
    return candidates[0] if candidates else None


def choose_entry(snapshot: PageSnapshot) -> ButtonControl | None:
    """The entry button on a page that has no application form yet.

    ``Apply Manually`` wins over a generic ``Apply``. Submit is never chosen.
    """
    if snapshot.captcha_present or visible_fields(snapshot) or is_review(snapshot):
        return None
    return find_entry_control(snapshot)


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


def resettle_account(page, snapshot: PageSnapshot) -> bool:
    """Wait out an account shell. Returns true when a second extract is warranted."""
    if not _should_settle(page, snapshot):
        return False
    _settle_account_pane(page, snapshot)
    return True


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
