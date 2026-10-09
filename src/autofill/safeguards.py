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
    }
)


def is_entry_label(name: str) -> bool:
    """True for a job-description entry control, never for Submit on a form.

    ``Apply`` on a posting with no application form is an entry. ``Submit``
    and ``Submit application`` are not, and stay on the final-submit path.
    """
    label = " ".join(name.split()).casefold().replace("\u2019", "'")
    if "submit" in label:
        return False
    return label in _ENTRY_LABELS


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


def activate(page, selector: str, name: str, *, control_type: str = "") -> None:
    """Click a continue control after the safeguard check.

    ``page`` is a Playwright ``Page``. The click lives in this module so the
    rest of the package cannot press a button without the check.
    """
    perform_click(page, selector, name, control_type=control_type, purpose="navigation")


def activate_login(page, selector: str, name: str, *, control_type: str = "") -> None:
    """Click Log in or Sign in. Final application submit stays forbidden."""
    perform_click(page, selector, name, control_type=control_type, purpose="login")


def activate_entry(page, selector: str, name: str, *, control_type: str = "") -> None:
    """Open a job description into the application. Submit on a form stays forbidden.

    The caller must already know the page has no application form. This does
    not classify ``Apply`` as safe navigation for later pages.
    """
    if not HUMAN_SUBMIT_ONLY:
        raise HumanSubmissionRequired("HUMAN_SUBMIT_ONLY was disabled. That is not a supported configuration.")
    if not is_entry_label(name):
        raise HumanSubmissionRequired(
            f"Refusing to activate {name!r}. A person must click the final Submit or Apply control."
        )
    page.locator(selector).click()


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


def perform_click(page, selector: str, name: str, *, control_type: str = "", purpose: str) -> None:
    """Central click guard. ``purpose`` is ``navigation`` or ``login``.

    If the control is a final application submission, this raises
    :class:`HumanSubmissionRequired` before any click. That is true for both
    purposes, so an adapter cannot reach Submit by asking for a login click.
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
        page.locator(selector).click()
        return
    if purpose == "navigation":
        if klass != ActionClass.NAVIGATION:
            raise SubmitBlockedError(
                f"Refusing to activate {name!r}. Only Next, Continue, Review, or Add-row navigation is automatic."
            )
        page.locator(selector).click()
        return
    raise SubmitBlockedError(f"Unknown click purpose {purpose!r}.")
