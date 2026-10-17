"""Wait for a real ATS page to render before the first inspection.

A delayed form and a bot wall both look empty at the first paint. The wait
is bounded. An invisible reCAPTCHA or hCaptcha widget is not a wall.
"""

from __future__ import annotations

import contextvars
import logging
import re
import time
from urllib.parse import urlparse

from autofill.ats import WORKDAY_HOST_RE
from autofill.extract import extract_page
from autofill.login import _CONFIRM_RE as _VERIFY_RE
from autofill.login import _LOGIN_RE, _REGISTER_RE
from autofill.models import ButtonControl, Control, PageSnapshot
from autofill.safeguards import classify_control, is_entry_control, semantic_action

logger = logging.getLogger("autofill.pagewait")

_WORKDAY_HOST = WORKDAY_HOST_RE
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
_CHAT_RE = re.compile(r"\b(chat ?bot|chat window|chat message|ask (a|me) question|type your message)\b", re.IGNORECASE)
_SITE_ERROR_STRONG = re.compile(
    r"\b("
    r"problem with the service"
    r"|something went wrong"
    r"|temporarily unavailable"
    r"|internal server error"
    r"|bad gateway"
    r"|service unavailable"
    r"|gateway time-?out"
    r"|(?:error|http)\s*5\d\d"
    r"|5\d\d\s+error"
    r"|service interruption"
    r"|(?:down|offline) for (?:scheduled )?maintenance"
    r"|(?:site|website|system|portal) is (?:currently )?undergoing (?:scheduled )?maintenance"
    r"|(?:workday|site|website|service|system|application|portal) is "
    r"(?:currently |temporarily )?(?:unavailable|down|offline)"
    r")\b",
    re.IGNORECASE,
)
_SITE_ERROR_SOFT = re.compile(r"\bplease try again\b", re.IGNORECASE)


_OUTAGE_RE = re.compile(
    r"\b("
    r"service interruption"
    r"|(?:down|offline) for (?:scheduled )?maintenance"
    r"|(?:site|website|system|portal) is (?:currently )?undergoing (?:scheduled )?maintenance"
    r"|(?:workday|site|website|service|system|application|portal) is "
    r"(?:currently |temporarily )?(?:unavailable|down|offline)"
    r"|temporarily unavailable"
    r"|service unavailable"
    r")\b",
    re.IGNORECASE,
)


def site_error_messages(snapshot: PageSnapshot) -> list[str]:
    """``site_error``, plus ``employer_site_unavailable`` for a maintenance or outage page."""
    text = f"{snapshot.heading} {snapshot.page_text}"
    if _OUTAGE_RE.search(text):
        return ["site_error", "employer_site_unavailable"]
    return ["site_error"]


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
        if control.input_type == "search" or _SEARCH_RE.search(blob) or _CHAT_RE.search(blob):
            continue
        if control.kind in {"file", "textarea", "select", "radio", "checkbox", "combobox", "buttons"}:
            found.append(control)
            continue
        if control.kind == "text" or control.input_type in {"email", "tel", "text", "url", "number"}:
            found.append(control)
    return found


def is_site_error(snapshot: PageSnapshot) -> bool:
    """True when the page is the site's own failure, not a login wall.

    A header Sign in link does not change that. ``please try again`` on a page
    that already has application fields is left to validation handling.
    """
    text = f"{snapshot.heading} {snapshot.page_text}"
    if _SITE_ERROR_STRONG.search(text):
        return True
    return bool(_SITE_ERROR_SOFT.search(text) and not application_controls(snapshot))


def classify_page(snapshot: PageSnapshot, *, url: str = "") -> str:
    """One of the public page types. A posting is not an application form."""
    if snapshot.captcha_present:
        note_page_type("UNKNOWN")
        return "UNKNOWN"
    if is_site_error(snapshot):
        note_page_type("SITE_ERROR")
        return "SITE_ERROR"
    text = f"{snapshot.heading} {snapshot.page_text}"
    if _CONFIRM_RE.search(text):
        note_page_type("CONFIRMATION")
        return "CONFIRMATION"
    if register_pane(snapshot):
        note_page_type("ACCOUNT")
        return "ACCOUNT"
    # A posting with an Apply control is a job page even when the header
    # offers Sign in. A page that already has application fields stays a form.
    entry = find_entry_control(snapshot)
    fields = application_controls(snapshot)
    formish = [
        control
        for control in fields
        if control.kind in {"file", "textarea", "select", "radio", "combobox"}
        or control.input_type in {"email", "tel"}
    ]
    job_text = bool(_JOB_RE.search(text))
    posting = entry is not None and not fields and not formish
    if posting and not _password_controls(snapshot) and not snapshot.password_present and job_text:
        note_page_type("JOB_DESCRIPTION")
        return "JOB_DESCRIPTION"
    if sign_in_pane(snapshot):
        note_page_type("LOGIN")
        return "LOGIN"
    if is_review(snapshot):
        note_page_type("REVIEW")
        return "REVIEW"
    # A search box is not an application. One real field with no Apply control is.
    if formish or (fields and entry is None):
        note_page_type("APPLICATION_FORM")
        return "APPLICATION_FORM"
    if entry is not None:
        note_page_type("APPLY_ENTRY")
        return "APPLY_ENTRY"
    if not fields and any(
        "create account" in button.name.casefold() or "register" in button.name.casefold()
        for button in _content_buttons(snapshot)
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
    if is_site_error(snapshot):
        note_page_type("SITE_ERROR")
        return "SITE_ERROR"
    if register_pane(snapshot):
        note_page_type("ACCOUNT")
        return "ACCOUNT"
    if sign_in_pane(snapshot):
        note_page_type("LOGIN")
        return "LOGIN"
    kind = classify_page(snapshot, url=url)
    if kind == "SITE_ERROR":
        return "SITE_ERROR"
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
    merely mentions an account is not this pane. Two visible password fields
    are Create Account unless the page is only a Sign In form.
    """
    if snapshot.captcha_present:
        return False
    text = _pane_text(snapshot)
    passwords = _password_controls(snapshot)
    ids = {item for item in snapshot.automation_ids if item}
    if {"createAccountSubmitButton", "verifyPassword"} & ids and (passwords or snapshot.password_present):
        return True
    verify = len(passwords) >= 2 or bool(_VERIFY_RE.search(text))
    create = bool(_REGISTER_RE.search(snapshot.heading or "")) or any(
        _REGISTER_RE.search(button.name) for button in _content_buttons(snapshot)
    )
    if not create and _REGISTER_RE.search(snapshot.page_text or ""):
        create = verify
    if len(passwords) >= 2 and not create:
        heading = snapshot.heading or ""
        loginish = bool(_LOGIN_RE.search(heading)) or any(
            _LOGIN_RE.search(button.name) for button in _content_buttons(snapshot)
        )
        create = bool(_REGISTER_RE.search(text)) or not loginish
    passwordish = bool(passwords) or snapshot.password_present or bool(re.search(r"\bpassword\b", text, re.IGNORECASE))
    return bool(verify and create and passwordish)


def sign_in_pane(snapshot: PageSnapshot) -> bool:
    """A sign-in form. A create-account pane, a posting, or an application is not one."""
    if snapshot.captcha_present or is_review(snapshot) or register_pane(snapshot):
        return False
    if find_entry_control(snapshot) is not None or _application_fields(snapshot):
        return False
    passwords = _password_controls(snapshot)
    login_button = any(_LOGIN_RE.search(button.name) for button in _content_buttons(snapshot))
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
    # Header Sign In is site chrome. It must not end the wait before the form paints.
    names = " ".join(
        button.name.casefold() for button in snapshot.buttons if not button.in_header and not button.in_ad
    )
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


def _document_complete(page) -> bool:
    return _ready_state(page) == "complete"


def _document_settled(page) -> bool:
    """True once the document can be inspected. Network idle is not required."""
    state = _ready_state(page)
    if state is None:
        return True
    return state in {"interactive", "complete"}


def _ready_state(page) -> str | None:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return None
    try:
        state = evaluate("() => document.readyState")
    except Exception as exc:
        if _closed(exc) or _timeout(exc):
            raise
        return None
    if not isinstance(state, str):
        return None
    return state


def _note_render_wait(started: float) -> None:
    elapsed = int((time.perf_counter() - started) * 1000)
    if elapsed >= 200:
        logger.info("action=render_wait elapsed_ms=%s", elapsed)


_PROBE_JS = r"""() => {
  function count(node) {
    if (!node || !node.querySelectorAll) return 0;
    const found = node.querySelectorAll("input, textarea, select, button, [role='button'], iframe").length;
    if (found) return found;
    for (const el of node.querySelectorAll("*")) {
      if (el.shadowRoot) {
        const nested = count(el.shadowRoot);
        if (nested) return nested;
      }
    }
    return 0;
  }
  if (count(document) > 0) return true;
  const text = ((document.body && document.body.innerText) || "").slice(0, 1500).toLowerCase();
  return text.indexOf("create account") !== -1 || text.indexOf("sign in") !== -1 || text.indexOf("apply") !== -1;
}"""


def _probe_controls(page) -> bool | None:
    """True when a cheap look sees controls. ``None`` when the page cannot say."""
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return None
    try:
        found = evaluate(_PROBE_JS)
    except Exception as exc:
        if _closed(exc) or _timeout(exc):
            raise
        return None
    if isinstance(found, bool):
        return found
    return None


def _pump_between_waits() -> None:
    try:
        from autofill.dom_ui import _pump_queue

        _pump_queue()
    except Exception:
        return


# ATS shells that paint the posting and the Apply entry inside a content iframe
# after DOMContentLoaded (iCIMS ``icims_content_iframe``). The outer page has
# site chrome with a search box, which must not end the render wait early.
_CONTENT_FRAME_JS = r"""() => {
  const frames = document.querySelectorAll("iframe#icims_content_iframe, iframe[name='icims_content_iframe']");
  const icims = /(^|\.)icims\.com$/i.test(location.hostname) && !/[?&]in_iframe=1/.test(location.search);
  // The shell script inserts the iframe late. Wait a bounded time for it.
  if (icims && !frames.length && !document.querySelector("form input:not([type=hidden]):not([name=q])")) {
    if (document.readyState !== "complete" || performance.now() < 8000) return true;
  }
  for (const f of frames) {
    let doc = null;
    try { doc = f.contentDocument; } catch (e) { return false; }
    if (!doc || !doc.body) return true;
    const href = String((doc.location && doc.location.href) || "");
    if (!href || href === "about:blank") return true;
    if (doc.readyState === "loading") return true;
    if (!doc.querySelector("a, button, input, select, textarea")) return true;
  }
  return false;
}"""


def content_frame_pending(page) -> bool:
    """True while a known ATS content iframe exists but has not painted yet."""
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return False
    try:
        return evaluate(_CONTENT_FRAME_JS) is True
    except Exception as exc:
        if _closed(exc):
            raise
        # iCIMS reloads the shell with ?mobile=false&needsRedirect=false. A read
        # during that redirect is not a settled page.
        text = str(exc).casefold()
        return "context was destroyed" in text or "navigat" in text


def wait_for_render(page, *, timeout_s: float = 12.0) -> None:
    """Poll until a form or a known marker appears.

    ``timeout_s`` caps the poll. A page that already has controls returns
    on the first look. A document that has reached ``interactive`` does not
    wait for network idle: an open Greenhouse page keeps connections up.
    The loop uses a small probe and runs the full DOM scan only when that
    probe sees controls, or once at the deadline. A dead browser or a
    timeout while reading the page is raised.
    """
    try:
        if not content_frame_pending(page) and render_ready(extract_page(page)):
            return
    except Exception as exc:
        if _closed(exc) or _timeout(exc):
            raise
    started = time.perf_counter()
    if not _document_settled(page):
        load = getattr(page, "wait_for_load_state", None)
        if load is not None:
            try:
                load("domcontentloaded", timeout=2000)
            except Exception as exc:
                if _closed(exc):
                    raise
    deadline = time.monotonic() + timeout_s
    snapshot = None
    while True:
        probed = _probe_controls(page)
        if probed is not False:
            try:
                snapshot = extract_page(page)
            except Exception as exc:
                if _closed(exc) or _timeout(exc):
                    raise
                _note_render_wait(started)
                return
            if render_ready(snapshot) and not content_frame_pending(page):
                _settle_account_pane(page, snapshot)
                _note_render_wait(started)
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if snapshot is None:
                try:
                    snapshot = extract_page(page)
                except Exception as exc:
                    if _closed(exc) or _timeout(exc):
                        raise
                    _note_render_wait(started)
                    return
            _settle_account_pane(page, snapshot)
            _note_render_wait(started)
            return
        _pump_between_waits()
        pause = getattr(page, "wait_for_timeout", None)
        try:
            if pause is None:
                time.sleep(min(0.1, remaining))
            else:
                pause(int(min(100, remaining * 1000)))
        except Exception as exc:
            if _closed(exc) or _timeout(exc):
                raise
            _note_render_wait(started)
            return


def _entry_name(name: str) -> str:
    return " ".join(name.split()).casefold().replace("\u2019", "'").replace("\u2018", "'")


def _known_entry(control) -> bool:
    """An employer entry control. Submit application is never one.

    A bare ``Apply`` whose type is submit stays on the final-submit path.
    Greenhouse ``#apply_button``, a Lever apply posting button, and the
    labeled entry phrases are entry controls. Header and ad controls are not.
    """
    return is_entry_control(
        getattr(control, "name", "") or "",
        selector=getattr(control, "selector", "") or "",
        control_type=getattr(control, "control_type", "") or "",
        aria_label=getattr(control, "aria_label", "") or "",
        automation_id=getattr(control, "automation_id", "") or "",
        href=getattr(control, "href", "") or "",
        role=getattr(control, "role", "") or "",
        disabled=bool(getattr(control, "disabled", False)),
        in_header=bool(getattr(control, "in_header", False)),
        in_ad=bool(getattr(control, "in_ad", False)),
        in_main=bool(getattr(control, "in_main", True)),
    )


def find_entry_control(snapshot: PageSnapshot) -> ButtonControl | None:
    """The employer's Apply control, including on a posting that also has a search box.

    ``Apply Manually`` wins over a generic ``Apply``. Submit application is never chosen.
    """
    if snapshot.captcha_present or is_review(snapshot):
        return None
    candidates: list[ButtonControl] = []
    for button in snapshot.buttons:
        if _known_entry(button):
            candidates.append(button)
    for link in snapshot.links:
        if link.selector and _known_entry(link):
            candidates.append(
                ButtonControl(
                    name=link.name,
                    selector=link.selector,
                    automation_id=link.automation_id,
                    aria_label=link.aria_label,
                    role=link.role or "link",
                    href=link.href,
                    in_header=link.in_header,
                    in_ad=link.in_ad,
                    in_main=link.in_main,
                    disabled=link.disabled,
                    frame_url=link.frame_url,
                )
            )
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


def _content_buttons(snapshot: PageSnapshot) -> list[ButtonControl]:
    """Buttons in the page body. Header and ad chrome are not the form."""
    return [button for button in snapshot.buttons if not button.in_header and not button.in_ad]


def _account_chrome(snapshot: PageSnapshot) -> bool:
    names = " ".join(button.name.casefold() for button in _content_buttons(snapshot))
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
    workday = bool(_WORKDAY_HOST.search(_host(page)))
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
    if _account_decisive(snapshot) and (register_pane(snapshot) or sign_in_pane(snapshot)):
        return
    workday = bool(_WORKDAY_HOST.search(_host(page)))
    budget = 2.0 if workday and not _account_decisive(snapshot) else 0.4
    deadline = time.monotonic() + budget
    previous = _account_signature(snapshot)
    seen_decisive = _account_decisive(snapshot)
    while time.monotonic() < deadline:
        _pause(page, 100)
        if _probe_controls(page) is False:
            continue
        try:
            current = extract_page(page)
        except Exception as exc:
            if _closed(exc):
                raise
            return
        signature = _account_signature(current)
        decisive = _account_decisive(current)
        if decisive and (register_pane(current) or sign_in_pane(current) or seen_decisive):
            if signature == previous or register_pane(current):
                return
        previous = signature
        seen_decisive = decisive


def is_login_wall(snapshot: PageSnapshot) -> bool:
    """Sign-in or create-account choices with no form and no entry button.

    A header Sign in, Log in, or Create account link is site chrome. It does
    not make a job description a login wall.
    """
    if snapshot.captcha_present or visible_fields(snapshot) or is_review(snapshot):
        return False
    if choose_entry(snapshot) is not None:
        return False
    for button in _content_buttons(snapshot):
        name = button.name.casefold()
        if "sign in" in name or "log in" in name or "create account" in name:
            return True
    return False


def icims_login_gate(snapshot: PageSnapshot, url: str) -> bool:
    """iCIMS email-first sign-in: ``/login`` with a lone Email box and Next.

    Apply on an iCIMS posting opens this gate inside ``icims_content_iframe``.
    Typing the email starts an account sign-in or registration, so it is a
    login wall, not an application form.
    """
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").casefold()
    if "icims.com" not in host or not re.search(r"/login(?:$|[/?#])", parsed.path + "/"):
        return False
    if snapshot.captcha_present and not snapshot.controls:
        return False
    framed = [c for c in snapshot.controls if not c.hidden and c.frame_url]
    if not framed:
        return False
    return all(c.kind == "email" or "email" in (c.label or "").casefold() for c in framed)


_VALIDATION_ERROR = (
    "required field",
    "this field is required",
    "is required",
    "invalid",
    "select an option",
    "please select",
    "enter a valid",
    "select one",
    "valid date",
)


def validation_message(snapshot: PageSnapshot) -> str:
    """The first visible validation alert, or an empty string.

    Job text that mentions a requirement is not an alert. The returned text
    is the alert itself, capped so a page dump cannot land in the log.
    """
    for alert in snapshot.alerts:
        text = " ".join(str(alert).split())
        folded = text.casefold()
        if text and any(phrase in folded for phrase in _VALIDATION_ERROR):
            return text[:300]
    return ""


def navigation_signature(page, snapshot: PageSnapshot) -> tuple:
    """URL route, visible fields, and heading. Used to see whether a click moved."""
    url = ""
    try:
        url = str(getattr(page, "url", "") or "")
    except Exception:
        url = ""
    parsed = urlparse(url)
    route = (parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)
    fields = tuple(
        (control.selector, getattr(control, "frame_url", "") or "")
        for control in snapshot.controls
        if not control.hidden
    )
    heading = " ".join((snapshot.heading or "").split()).casefold()
    return (route, fields, heading)


def navigation_changed(before: tuple, page, snapshot: PageSnapshot) -> bool:
    """True when the route, the step heading, or most of the form changed.

    A validation redraw keeps the same fields on screen and is not an advance.
    A new step adds fields, most of the old ones leave, or the step had no fields.
    """
    after = navigation_signature(page, snapshot)
    if after == before:
        return False
    if len(before) < 3 or len(after) < 3:
        return True
    if after[0] != before[0] or after[2] != before[2]:
        return True
    old_fields = set(before[1])
    if not old_fields:
        return bool(after[1])
    new_fields = set(after[1])
    if new_fields - old_fields and not validation_message(snapshot):
        # New fields with no employer error: the click opened the next section.
        # The same fields plus an error is a validation redraw, not an advance.
        return True
    kept = old_fields & new_fields
    return len(kept) * 2 < len(old_fields)


_INVALID_FIELDS_JS = r"""() => {
  const vis = (e) => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const out = [];
  const nodes = document.querySelectorAll(
    "[aria-invalid=true], input.ng-invalid, select.ng-invalid, textarea.ng-invalid"
  );
  for (const el of nodes) {
    if (!vis(el) || out.length >= 5) continue;
    const label = (el.labels && el.labels[0] && el.labels[0].innerText) || el.getAttribute("aria-label") || "";
    const box = el.closest(".jv-form-field, .field, .form-group, [data-automation-id]") || el.parentElement;
    let error = "";
    const described = el.getAttribute("aria-describedby");
    if (described) {
      const node = document.getElementById(described.split(/\s+/)[0]);
      if (node) error = node.innerText || "";
    }
    if (!error && box) {
      const hint = box.querySelector("[role=alert], .error, [class*=error]");
      if (hint) error = hint.innerText || "";
    }
    const name = String(label || "").replace(/\s+/g, " ").replace(/\*/g, "").trim().slice(0, 80);
    const text = String(error || "").replace(/\s+/g, " ").trim().slice(0, 160);
    if (name || text) out.push(text ? name + ": " + text : name);
  }
  const notes = document.querySelectorAll(
    "[role=alert], .jv-form-field-error, .field-error, .error-message, [class*=error-message], .error"
  );
  for (const note of notes) {
    if (!vis(note) || out.length >= 5) continue;
    const text = String(note.innerText || "").replace(/\s+/g, " ").trim();
    if (!text || text.length > 120) continue;
    let name = "";
    let node = note.parentElement;
    for (let depth = 0; node && depth < 4 && !name; depth += 1, node = node.parentElement) {
      const title = node.querySelector("label, legend, h2, h3, h4");
      if (title && !title.contains(note)) name = title.innerText || "";
    }
    name = String(name || "").replace(/\s+/g, " ").replace(/\*/g, "").trim().slice(0, 80);
    if (!out.some((item) => item.indexOf(text) !== -1)) out.push(name ? name + ": " + text : text);
  }
  return out;
}"""


def invalid_field_messages(page) -> list[str]:
    """Name each field the employer marked invalid, with its error text. Values are not read."""
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return []
    try:
        found = evaluate(_INVALID_FIELDS_JS)
    except Exception:
        return []
    return [str(item) for item in found or [] if str(item).strip()]


def wait_for_navigation(page, before: tuple, *, timeout_s: float = 1.5):
    """Poll until the page moves or the budget ends. Returns ``(advanced, snapshot)``."""
    deadline = time.monotonic() + timeout_s
    snapshot = None
    while True:
        snapshot = extract_page(page)
        if navigation_changed(before, page, snapshot):
            return True, snapshot
        if time.monotonic() >= deadline:
            return False, snapshot
        _pause(page, 150)
