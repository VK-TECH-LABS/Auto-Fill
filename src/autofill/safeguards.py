"""Human-submit safeguard.

Final job-application Submit / Apply is always human-controlled. There is no
flag that turns it on. Adapters call :func:`perform_click`, which raises
:class:`HumanSubmissionRequired` when the action is a final submission.

Login is a different action class. :func:`activate_login` may click
Log in / Sign in on a detected login form. It still refuses final application
submit, sign-up, and account creation. Page turns (Next, Continue, Save and
continue, Proceed, Review, Add another) go through :func:`activate`.
"""

from __future__ import annotations

import re

# Permanent. Read by tests so a future "just this once" flag fails CI.
HUMAN_SUBMIT_ONLY = True

_SUBMIT_RE = re.compile(
    r"\b("
    r"submit(\s+application)?"
    r"|apply(\s+now)?"
    r"|send(\s+my)?\s+application"
    r"|send"
    r"|finish(\s+application)?"
    r"|complete\s+application"
    r")\b",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(
    r"\b(log\s*in|sign\s*in|sign\s*up|create\s+account|register)\b",
    re.IGNORECASE,
)
_CONTINUE_RE = re.compile(
    r"\b(next|continue|save\s*(and|&)\s*continue|proceed|review)\b",
    re.IGNORECASE,
)
_FORBIDDEN_RE = re.compile(
    r"\b(withdraw|delete|decline|cancel\s+application)\b",
    re.IGNORECASE,
)
_FORWARD = frozenset({"next", "continue", "save_and_continue", "review"})
_ADD_RE = re.compile(
    r"\badd (another|experience|employment|education|position|job)\b",
    re.IGNORECASE,
)
_SIGNUP_RE = re.compile(r"\b(sign\s*up|create\s+account|register)\b", re.IGNORECASE)
_LOGIN_OK_RE = re.compile(r"\b(log\s*in|sign\s*in)\b", re.IGNORECASE)


class ActionClass:
    """Separate login submission from the final application submission."""

    LOGIN_SUBMIT_ALLOWED = "login_submit_allowed"
    FINAL_APPLICATION_SUBMIT_FORBIDDEN = "final_application_submit_forbidden"
    NAVIGATION = "navigation"
    OTHER = "other"


class SubmitBlockedError(RuntimeError):
    """Raised when code tries to activate a control it is not allowed to use."""


class HumanSubmissionRequired(SubmitBlockedError):
    """A final application Submit or Apply was attempted.

    The central guard raises this. ATS adapters cannot bypass it.
    """


def classify_control(name: str, *, control_type: str = "") -> str:
    """Classify a button as ``submit``, ``auth``, ``continue``, or ``other``.

    Submit and auth controls are never clicked. Continue controls are the
    only ones the fill routine may activate, and only to open the next page
    of the same application. A type=submit control whose label is not a
    continue label is treated as a final submit.
    """
    label = " ".join(name.split())
    if _SUBMIT_RE.search(label) or _FORBIDDEN_RE.search(label):
        return "submit"
    if _AUTH_RE.search(label):
        return "auth"
    if _CONTINUE_RE.search(label) or _ADD_RE.search(label):
        return "continue"
    if control_type.lower() == "submit":
        return "submit"
    return "other"


def semantic_action(name: str, *, control_type: str = "") -> str:
    """Classify a button without activating it.

    Forward steps are Next, Continue, Save and Continue, and Review.
    Back and Save are named so they are not treated as a final submit and
    are not used to advance. Withdraw, Delete, Decline, and Cancel
    Application stay forbidden.
    """
    label = " ".join(name.split())
    if _SUBMIT_RE.search(label) or _FORBIDDEN_RE.search(label):
        return "forbidden_submit"
    if _SIGNUP_RE.search(label):
        return "signup"
    if _LOGIN_OK_RE.search(label):
        return "login"
    if re.search(r"\bback\b", label, re.IGNORECASE):
        return "back"
    if re.search(r"\bsave\s*(and|&)\s*continue\b", label, re.IGNORECASE):
        return "save_and_continue"
    if re.search(r"\bnext\b", label, re.IGNORECASE):
        return "next"
    if re.search(r"\b(continue|proceed)\b", label, re.IGNORECASE):
        return "continue"
    if re.search(r"\breview\b", label, re.IGNORECASE):
        return "review"
    if re.search(r"\bsave\b", label, re.IGNORECASE):
        return "save"
    if control_type.lower() == "submit":
        return "forbidden_submit"
    return "other"


def is_forward_navigation(name: str, *, control_type: str = "") -> bool:
    """True for a page-turn that is not Back, Save, or a final submit."""
    if is_add_row(name):
        return False
    return semantic_action(name, control_type=control_type) in _FORWARD


def is_add_row(name: str) -> bool:
    """True for Add-experience style controls, which are navigation, not submit."""
    return bool(_ADD_RE.search(" ".join(name.split())))


_ENTRY_LABELS = frozenset(
    {
        "apply",
        "apply now",
        "apply for this job",
        "apply manually",
        "i'm interested",
        "start application",
        "continue to application",
        "apply to job",
        "apply online",
        "apply for this position",
        "apply for this role",
        "easy apply",
        "apply here",
    }
)
_ENTRY_PREFIXES = (
    "apply for this job",
    "apply now",
    "apply for this position",
    "apply for this role",
    "apply to job",
    "apply online",
    "apply here",
    "easy apply",
    "start application",
    "continue to application",
    "i'm interested",
    "im interested",
)
_UNRELATED_ENTRY = re.compile(
    r"\b(privacy|cookie|cookies|terms|sign in|log in|login|create account|sponsor|sponsored|"
    r"advertisement|learn more|share|facebook|linkedin|twitter|instagram)\b",
    re.IGNORECASE,
)
_ENTRY_POPUP_MS = 1000


def _fold_label(name: str) -> str:
    return " ".join((name or "").split()).casefold().replace("\u2019", "'").replace("\u2018", "'")


def _fold_token(value: str) -> str:
    spaced = re.sub(r"([a-z])([A-Z])", r"\1 \2", value or "")
    spaced = spaced.replace("-", " ").replace("_", " ").replace("/", " ")
    return _fold_label(spaced)


def is_entry_label(name: str) -> bool:
    """True for a job-description entry control, never for Submit on a form.

    ``Apply`` on a posting with no application form is an entry. ``Submit``,
    ``Send``, and ``Complete application`` are not, and stay on the final-submit
    path. A longer label that starts with a known entry phrase is the same control.
    """
    label = _fold_label(name)
    if not label:
        return False
    if re.search(r"\b(submit|send(\s+my)?\s+application|complete\s+application)\b", label):
        return False
    if label in _ENTRY_LABELS:
        return True
    return any(label.startswith(prefix) for prefix in _ENTRY_PREFIXES)


def is_entry_control(
    name: str,
    *,
    selector: str = "",
    control_type: str = "",
    aria_label: str = "",
    automation_id: str = "",
    href: str = "",
    role: str = "",
    disabled: bool = False,
    in_header: bool = False,
    in_ad: bool = False,
    in_main: bool = True,
) -> bool:
    """A visible, enabled Apply control in the main content.

    Header sign-in, ads, and unrelated links are not entries. A bare ``Apply``
    whose type is submit stays on the final-submit path unless the selector is
    the known Greenhouse or Lever entry control.
    """
    if disabled or in_header or in_ad or not in_main:
        return False
    role_text = (role or "").casefold()
    if role_text and role_text not in {"button", "link"}:
        return False
    label = _fold_label(name)
    if re.search(r"\b(submit|send(\s+my)?\s+application|complete\s+application)\b", label):
        return False
    selector_text = (selector or "").casefold()
    if selector_text == "#apply_button" or selector_text.endswith("#apply_button"):
        return True
    if "postings-btn" in selector_text and "apply" in label:
        return True
    if (control_type or "").casefold() == "submit" and label == "apply":
        return False
    token = _fold_token(automation_id)
    if is_entry_label(name) or is_entry_label(aria_label) or is_entry_label(token):
        return True
    if token and re.search(r"\bapply\b", token) and not _UNRELATED_ENTRY.search(token):
        return True
    href_text = (href or "").casefold()
    if "apply" not in href_text:
        return False
    if (
        _UNRELATED_ENTRY.search(href_text)
        or _UNRELATED_ENTRY.search(label)
        or _UNRELATED_ENTRY.search(_fold_label(aria_label))
    ):
        return False
    visible = label or _fold_label(aria_label)
    if visible and "apply" not in visible:
        return False
    return True


def field_class_for_button(name: str, *, control_type: str = "") -> str:
    """Field class for a button. Final submit and navigation stay distinct from login."""
    klass = action_class(name, control_type=control_type)
    if klass == ActionClass.FINAL_APPLICATION_SUBMIT_FORBIDDEN:
        return "FINAL_SUBMIT"
    if klass == ActionClass.LOGIN_SUBMIT_ALLOWED:
        return "LOGIN_FIELD"
    if klass == ActionClass.NAVIGATION:
        return "NAVIGATION_CONTROL"
    return "UNKNOWN_FIELD"


def action_class(name: str, *, control_type: str = "") -> str:
    """Map a control label onto an action class."""
    kind = classify_control(name, control_type=control_type)
    if kind == "submit":
        return ActionClass.FINAL_APPLICATION_SUBMIT_FORBIDDEN
    if kind == "auth":
        if _LOGIN_OK_RE.search(name) and not _SIGNUP_RE.search(name):
            return ActionClass.LOGIN_SUBMIT_ALLOWED
        return ActionClass.OTHER
    if kind == "continue":
        return ActionClass.NAVIGATION
    return ActionClass.OTHER


def assert_safe_to_activate(name: str, *, control_type: str = "") -> str:
    """Return ``continue`` or raise :class:`SubmitBlockedError`.

    The fill routine is not allowed to click arbitrary buttons. Navigation
    that is not Next / Continue / Review stays in human hands, same as the
    final submit.
    """
    if not HUMAN_SUBMIT_ONLY:
        raise SubmitBlockedError("HUMAN_SUBMIT_ONLY was disabled. That is not a supported configuration.")
    kind = classify_control(name, control_type=control_type)
    if kind == "submit":
        raise HumanSubmissionRequired(
            f"Refusing to activate {name!r}. Auto-Fill never clicks the final Submit or Apply control."
        )
    if kind == "auth":
        raise SubmitBlockedError(
            f"Refusing to activate {name!r}. Page turns do not include Log in, Sign in, or account creation."
        )
    if kind != "continue":
        raise SubmitBlockedError(
            f"Refusing to activate {name!r}. Only Next, Continue, or Review navigation is automatic."
        )
    return kind


def activate(page, selector: str, name: str, *, control_type: str = "", frame_url: str = "") -> None:
    """Click a continue control after the safeguard check.

    ``page`` is a Playwright ``Page``. The click lives in this module so the
    rest of the package cannot press a button without the check.
    """
    perform_click(page, selector, name, control_type=control_type, purpose="navigation", frame_url=frame_url)


def activate_login(page, selector: str, name: str, *, control_type: str = "") -> None:
    """Click Log in or Sign in. Final application submit stays forbidden."""
    perform_click(page, selector, name, control_type=control_type, purpose="login")


def activate_account(page, selector: str, name: str, *, control_type: str = "") -> None:
    """Click the account form's primary button. Submit Application stays forbidden."""
    perform_click(page, selector, name, control_type=control_type, purpose="account")


def activate_account_switch(page, selector: str, name: str, *, control_type: str = "") -> None:
    """Open the other account pane. This is not an application submit."""
    perform_click(page, selector, name, control_type=control_type, purpose="account_switch")


def attach_resume_control(page, selector: str, files: list[dict]) -> None:
    """Put files on a resume control. A drop zone click stays inside this guard."""
    if not HUMAN_SUBMIT_ONLY:
        raise HumanSubmissionRequired("HUMAN_SUBMIT_ONLY was disabled. That is not a supported configuration.")
    try:
        page.locator(selector).set_input_files(files)
        return
    except Exception:
        pass
    with page.expect_file_chooser() as chooser:
        page.locator(selector).click()
    chooser.value.set_files(files)


def _bound_target(page, frame_url: str):
    """The frame that owns ``frame_url``, or the page when the control is on top."""
    if not frame_url:
        return page
    for frame in list(getattr(page, "frames", []) or []):
        try:
            if str(getattr(frame, "url", "") or "") == frame_url:
                return frame
        except Exception:
            continue
    return page


def _popup_timeout(exc: BaseException) -> bool:
    return "timeout" in type(exc).__name__.casefold()


def _click_for_popup(page, selector: str, frame_url: str = ""):
    """Click and return a page opened by that click.

    A click that stays on this tab returns ``None``. The popup wait is short
    so a same-tab Apply is not held for a full navigation timeout.
    """
    target = _bound_target(page, frame_url)
    locator = target.locator(selector).first
    context = getattr(page, "context", None)
    expect = getattr(context, "expect_page", None) if context is not None else None
    opened: list = []

    def _remember(new_page) -> None:
        if new_page is not None and new_page not in opened:
            opened.append(new_page)

    listening = False
    if context is not None and getattr(context, "on", None) is not None:
        try:
            context.on("page", _remember)
            listening = True
        except Exception:
            listening = False
    try:
        if expect is None:
            locator.click()
            return opened[-1] if opened else None
        clicked = False
        try:
            with expect(timeout=_ENTRY_POPUP_MS) as incoming:
                locator.click()
                clicked = True
            _remember(getattr(incoming, "value", None))
        except Exception as exc:
            if not clicked or not _popup_timeout(exc):
                raise
        return opened[-1] if opened else None
    finally:
        if listening:
            remove = getattr(context, "remove_listener", None)
            if remove is not None:
                try:
                    remove("page", _remember)
                except Exception:
                    pass


def activate_entry(
    page,
    selector: str,
    name: str,
    *,
    control_type: str = "",
    aria_label: str = "",
    automation_id: str = "",
    href: str = "",
    role: str = "",
    frame_url: str = "",
):
    """Open a job description into the application. Submit on a form stays forbidden.

    The caller must already know the page has no application form. This does
    not classify ``Apply`` as safe navigation for later pages. A new tab or
    popup opened by the click is returned so the same session can follow it.
    """
    if not HUMAN_SUBMIT_ONLY:
        raise HumanSubmissionRequired("HUMAN_SUBMIT_ONLY was disabled. That is not a supported configuration.")
    if not is_entry_control(
        name,
        selector=selector,
        control_type=control_type,
        aria_label=aria_label,
        automation_id=automation_id,
        href=href,
        role=role,
    ):
        raise HumanSubmissionRequired(
            f"Refusing to activate {name!r}. A person must click the final Submit or Apply control."
        )
    # Lever renders two Apply links with the same class. The first is the posting entry.
    return _click_for_popup(page, selector, frame_url)


def open_combobox(page, selector: str) -> None:
    """Open one combobox. The click is not a form submit."""
    if not HUMAN_SUBMIT_ONLY:
        raise HumanSubmissionRequired("HUMAN_SUBMIT_ONLY was disabled. That is not a supported configuration.")
    page.locator(selector).click()


def click_choice(page, selector: str, name: str) -> None:
    """Choose a list option or a Yes/No button. Submit labels are refused."""
    if not HUMAN_SUBMIT_ONLY:
        raise HumanSubmissionRequired("HUMAN_SUBMIT_ONLY was disabled. That is not a supported configuration.")
    if action_class(name) == ActionClass.FINAL_APPLICATION_SUBMIT_FORBIDDEN:
        raise HumanSubmissionRequired(
            f"Refusing to choose {name!r}. That label is a final Submit or Apply."
        )
    page.locator(selector).click()


def dismiss_combobox(page) -> None:
    """Close an open list without choosing. Escape is not a submit."""
    page.keyboard.press("Escape")


def choose_option(page, opener_selector: str, option_selector: str, option_name: str) -> None:
    """Open a listbox and choose one option. This sets a field value.

    Option labels that read as a final Submit or Apply are refused, so a
    combobox cannot be used as a side path around the submit guard.
    """
    if not HUMAN_SUBMIT_ONLY:
        raise HumanSubmissionRequired("HUMAN_SUBMIT_ONLY was disabled. That is not a supported configuration.")
    if action_class(option_name) == ActionClass.FINAL_APPLICATION_SUBMIT_FORBIDDEN:
        raise HumanSubmissionRequired(
            f"Refusing to choose {option_name!r}. That label is a final Submit or Apply."
        )
    page.locator(opener_selector).click()
    page.locator(option_selector).click()


_ACCOUNT_NAME_RE = re.compile(
    r"\b(create account|register|sign\s*up|join|sign\s*in|log\s*in|login)\b",
    re.IGNORECASE,
)


def _account_button_allowed(name: str, *, control_type: str = "") -> bool:
    """Create Account, Sign In, Continue, or Next. Submit Application is not."""
    klass = action_class(name, control_type=control_type)
    if klass == ActionClass.FINAL_APPLICATION_SUBMIT_FORBIDDEN:
        return False
    if klass == ActionClass.LOGIN_SUBMIT_ALLOWED:
        return True
    if semantic_action(name, control_type=control_type) == "signup":
        return True
    if re.search(r"\bjoin\b", name, re.IGNORECASE) and klass != ActionClass.FINAL_APPLICATION_SUBMIT_FORBIDDEN:
        return True
    if klass == ActionClass.NAVIGATION and re.search(r"\b(continue|next)\b", name, re.IGNORECASE):
        if re.search(r"\bsave\b", name, re.IGNORECASE):
            return False
        return True
    return False


def perform_click(
    page,
    selector: str,
    name: str,
    *,
    control_type: str = "",
    purpose: str,
    frame_url: str = "",
) -> None:
    """Central click guard.

    If the control is a final application submission, this raises
    :class:`HumanSubmissionRequired` before any click. That is true for every
    purpose, so an adapter cannot reach Submit by asking for a login click.
    """
    if not HUMAN_SUBMIT_ONLY:
        raise HumanSubmissionRequired("HUMAN_SUBMIT_ONLY was disabled. That is not a supported configuration.")
    klass = action_class(name, control_type=control_type)
    if klass == ActionClass.FINAL_APPLICATION_SUBMIT_FORBIDDEN:
        raise HumanSubmissionRequired(
            f"Refusing to activate {name!r}. A person must click the final Submit or Apply control."
        )
    if purpose == "login":
        if klass != ActionClass.LOGIN_SUBMIT_ALLOWED:
            raise SubmitBlockedError(
                f"Refusing to activate {name!r}. Only an existing-account Log in or Sign in is allowed."
            )
        _bound_target(page, frame_url).locator(selector).click()
        return
    if purpose == "navigation":
        if klass != ActionClass.NAVIGATION:
            raise SubmitBlockedError(
                f"Refusing to activate {name!r}. Only Next, Continue, Review, or Add-row navigation is automatic."
            )
        _bound_target(page, frame_url).locator(selector).click()
        return
    if purpose == "account":
        if not _account_button_allowed(name, control_type=control_type):
            raise SubmitBlockedError(
                f"Refusing to activate {name!r}. Only the account form's primary button is allowed."
            )
        _bound_target(page, frame_url).locator(selector).click()
        return
    if purpose == "account_switch":
        if not _ACCOUNT_NAME_RE.search(name):
            raise SubmitBlockedError(
                f"Refusing to activate {name!r}. Only a Create Account or Sign In switch is allowed."
            )
        _bound_target(page, frame_url).locator(selector).click()
        return
    raise SubmitBlockedError(f"Unknown click purpose {purpose!r}.")
