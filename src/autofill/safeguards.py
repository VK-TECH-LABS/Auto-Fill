"""Human-submit safeguard.

Auto-Fill fills controls and may click Next, Continue, Save and continue,
Proceed, or Review to reach later pages of a multi-page application. It never activates a final Submit
or Apply control, and it never activates login, sign-up, or account-creation
controls. There is no flag, environment variable, or dry-run switch that
turns submission on.
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


class SubmitBlockedError(RuntimeError):
    """Raised when code tries to activate a control a person must click."""


def classify_control(name: str, *, control_type: str = "") -> str:
    """Classify a button as ``submit``, ``auth``, ``continue``, or ``other``.

    Submit and auth controls are never clicked. Continue controls are the
    only ones the fill routine may activate, and only to open the next page
    of the same application. A type=submit control whose label is not a
    continue label is treated as a final submit.
    """
    label = " ".join(name.split())
    if _SUBMIT_RE.search(label):
        return "submit"
    if _AUTH_RE.search(label):
        return "auth"
    if _CONTINUE_RE.search(label):
        return "continue"
    if control_type.lower() == "submit":
        return "submit"
    return "other"


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
        raise SubmitBlockedError(
            f"Refusing to activate {name!r}. Auto-Fill never clicks the final Submit or Apply control."
        )
    if kind == "auth":
        raise SubmitBlockedError(
            f"Refusing to activate {name!r}. Auto-Fill does not log in, sign up, or create accounts."
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
    assert_safe_to_activate(name, control_type=control_type)
    page.locator(selector).click()
