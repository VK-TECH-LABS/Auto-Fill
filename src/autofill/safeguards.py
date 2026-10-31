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

import contextvars
import logging
import re
from contextlib import contextmanager

logger = logging.getLogger("autofill.safeguards")

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


def _posting_entry_name(name: str, aria_label: str, automation_id: str) -> bool:
    """An Apply label on a job posting. Href-only matches are not enough in the header."""
    return bool(
        is_entry_label(name) or is_entry_label(aria_label) or is_entry_label(_fold_token(automation_id))
    )


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
    on_posting: bool = False,
) -> bool:
    """A visible, enabled Apply control in the main content.

    Header sign-in, ads, and unrelated links are not entries. A bare ``Apply``
    whose type is submit stays on the final-submit path unless the selector is
    the known Greenhouse or Lever entry control, or the caller is still on the
    job posting. ``on_posting`` also accepts that Apply in the posting header.
    """
    if disabled or in_ad:
        return False
    posting_named = bool(on_posting and _posting_entry_name(name, aria_label, automation_id))
    if (in_header or not in_main) and not posting_named:
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
    if (control_type or "").casefold() == "submit" and label == "apply" and not posting_named:
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


def activate_account(page, selector: str, name: str, *, control_type: str = "", at_point: bool = False) -> None:
    """Click the account form's primary button. Submit Application stays forbidden.

    ``at_point`` clicks the centre of the control with the mouse. Workday draws
    Create Account / Sign In as an aria-hidden button under a ``click_filter``
    overlay that may have no name; the overlay only reacts to a real pointer click.
    """
    perform_click(page, selector, name, control_type=control_type, purpose="account", at_point=at_point)


def activate_login_at_point(page, selector: str, name: str, *, control_type: str = "") -> None:
    """Sign In through a covered Workday submit button. Same guard as ``activate_login``."""
    perform_click(page, selector, name, control_type=control_type, purpose="login", at_point=True)


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
    in_header: bool = False,
    in_ad: bool = False,
    in_main: bool = True,
    on_posting: bool = False,
):
    """Open a job description into the application. Submit on a form stays forbidden.

    The caller must already know the page has no application form. This does
    not classify ``Apply`` as safe navigation for later pages. A new tab or
    popup opened by the click is returned so the same session can follow it.
    ``on_posting`` is only for that job-description Apply, including one in the
    posting header or with type submit.
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
        in_header=in_header,
        in_ad=in_ad,
        in_main=in_main,
        on_posting=on_posting,
    ):
        raise HumanSubmissionRequired(
            f"Refusing to activate {name!r}. A person must click the final Submit or Apply control."
        )
    # Lever renders two Apply links with the same class. The first is the posting entry.
    return _click_for_popup(page, selector, frame_url)


_COOKIE_DISMISS_JS = r"""() => {
  function textOf(el) {
    return (el.innerText || el.value || el.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
  }
  function hidden(el) {
    if (!el || el.nodeType !== 1) return true;
    const style = window.getComputedStyle(el);
    if (style.display === "none" || style.visibility === "hidden") return true;
    const box = el.getBoundingClientRect();
    return box.width < 2 || box.height < 2;
  }
  function eachRoot(root, visit) {
    visit(root);
    if (!root.querySelectorAll) return;
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) eachRoot(el.shadowRoot, visit);
    }
  }
  function isCookieBox(node) {
    const id = ((node.id || "") + " " + String(node.className || "") + " "
      + (node.getAttribute("data-automation-id") || "")).toLowerCase();
    const blob = (node.innerText || node.textContent || "").replace(/\s+/g, " ").trim().slice(0, 180).toLowerCase();
    const marked = /cookie|onetrust|cookiebot|trustarc|consent-banner|privacy-banner|legalnotice/.test(id);
    const titled = /\b(cookie|cookies|privacy preference|we value your privacy)\b/.test(blob);
    if (!marked && !titled) return false;
    if (/\b(submit application|create account|send application)\b/.test(blob)) return false;
    return true;
  }
  function score(name) {
    const n = name.toLowerCase();
    if (/\b(submit|application|account|sign[\s-]?in|log[\s-]?in|register|apply)\b/.test(n)) return 100;
    if (n.length > 48) return 100;
    const order = [
      "reject all", "decline all", "deny all", "reject", "decline", "deny",
      "only necessary", "necessary only", "essential only", "use necessary",
      "accept all", "allow all", "accept", "allow", "i agree", "agree",
      "got it", "okay", "ok", "save preferences", "confirm my choices", "close"
    ];
    for (let i = 0; i < order.length; i += 1) {
      if (n === order[i] || n.indexOf(order[i] + " ") === 0) return i;
    }
    return 100;
  }
  const boxes = [];
  eachRoot(document, (root) => {
    if (!root.querySelectorAll) return;
    const selector = [
      "[role='dialog']", "[role='alertdialog']", "[aria-modal='true']",
      "[id*='cookie' i]", "[class*='cookie' i]", "[id*='onetrust' i]", "[class*='onetrust' i]",
      "[id*='consent' i]", "[class*='consent' i]",
      // Workday marks its banner with data-automation-id; its classes are hashed.
      "[data-automation-id*='cookie' i]", "[data-automation-id*='legalNotice' i]"
    ].join(", ");
    for (const node of root.querySelectorAll(selector)) {
      if (isCookieBox(node)) boxes.push(node);
    }
  });
  let best = null;
  let bestScore = 100;
  for (const box of boxes) {
    const controls = box.querySelectorAll("button, [role='button'], input[type='button'], input[type='submit'], a");
    for (const el of controls) {
      if (hidden(el) || el.getAttribute("data-af-cookie-tried") === "1") continue;
      const name = textOf(el);
      const rank = score(name);
      if (rank < bestScore) {
        best = el;
        bestScore = rank;
      }
    }
  }
  if (!best) return "";
  const name = textOf(best);
  best.setAttribute("data-af-cookie-tried", "1");
  if ((best.getAttribute("type") || "").toLowerCase() === "submit") best.setAttribute("type", "button");
  best.click();
  return name;
}"""


def dismiss_cookie_banner(page) -> bool:
    """Dismiss a cookie or consent dialog. This is not an application submit.

    Reject or decline is preferred. Accept is used when that is the only
    control that closes the dialog. Application Submit is never chosen.
    """
    if not HUMAN_SUBMIT_ONLY:
        return False
    targets = []
    main = getattr(page, "main_frame", None)
    if main is not None:
        targets.append(main)
    else:
        targets.append(page)
    for frame in list(getattr(page, "frames", []) or []):
        if frame not in targets:
            targets.append(frame)
    for target in targets:
        evaluate = getattr(target, "evaluate", None)
        if evaluate is None:
            continue
        try:
            name = evaluate(_COOKIE_DISMISS_JS)
        except Exception:
            continue
        if isinstance(name, str) and name.strip():
            logger.info("action=cookie_dismiss result=clicked")
            return True
    return False


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
    r"\b(create account|register|sign\s*up|join|sign\s*in|log\s*in|login"
    r"|continue with e-?mail|use (?:my |your )?e-?mail)\b",
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


_POINT_HIT_JS = """([x, y, selector]) => {
  const target = document.querySelector(selector);
  const hit = document.elementFromPoint(x, y);
  if (!target || !hit) return false;
  if (hit === target || target.contains(hit) || hit.contains(target)) return true;
  // The Workday overlay is a sibling of the covered button inside one wrapper.
  const filter = hit.closest("[data-automation-id='click_filter']");
  return !!(filter && filter.parentElement && filter.parentElement.contains(target));
}"""


def _click(page, selector: str, frame_url: str, at_point: bool) -> None:
    locator = _bound_target(page, frame_url).locator(selector)
    if not at_point:
        locator.click()
        return
    target = locator.first
    try:
        target.scroll_into_view_if_needed(timeout=3000)
    except Exception:
        pass
    box = target.bounding_box()
    if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
        raise SubmitBlockedError("The account button is not on screen.")
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    # Only click when the pointer lands on this button or its own overlay, never a
    # different control drawn on top of it.
    if not page.evaluate(_POINT_HIT_JS, [x, y, selector]):
        raise SubmitBlockedError("Another element covers the account button.")
    page.mouse.click(x, y)


_LIVE_CONTROL_JS = """(selector) => {
  let el = null;
  try { el = document.querySelector(selector); } catch (error) { return null; }
  if (!el) return null;
  const name = (el.innerText || el.value || el.getAttribute("aria-label") || "")
    .replace(/\\s+/g, " ")
    .trim();
  return {name, controlType: (el.getAttribute("type") || "").toLowerCase()};
}"""


def _live_control(page, selector: str, frame_url: str) -> tuple[str, str]:
    """The label and type on the element ``selector`` points at now.

    A snapshot name can be stale, or the selector can point at a different
    control than the one the caller named. An unreadable target does not
    invent a label.
    """
    if not selector:
        return "", ""
    target = _bound_target(page, frame_url)
    evaluate = getattr(target, "evaluate", None)
    if evaluate is None:
        return "", ""
    try:
        raw = evaluate(_LIVE_CONTROL_JS, selector)
    except Exception:
        return "", ""
    if not isinstance(raw, dict):
        return "", ""
    return str(raw.get("name") or ""), str(raw.get("controlType") or "")


def _refuse_live_submit(page, selector: str, name: str, *, control_type: str, frame_url: str) -> None:
    """Refuse when the element that would be clicked is a final submit."""
    live_name, live_type = _live_control(page, selector, frame_url)
    if not live_name and not live_type:
        return
    shown = live_name or name
    if action_class(shown, control_type=live_type or control_type) == ActionClass.FINAL_APPLICATION_SUBMIT_FORBIDDEN:
        raise HumanSubmissionRequired(
            f"Refusing to activate {shown!r}. A person must click the final Submit or Apply control."
        )


_GUARD_DEPTH: contextvars.ContextVar[int] = contextvars.ContextVar("autofill_submit_guard_depth", default=0)

# Active only while a fill or an answer is running, then removed so the person
# can click Submit. Continue labels and Apply entry stay clickable. A click or
# an Enter that would activate Submit application is cancelled before the
# button's own handler or the form's onsubmit can record a submit.
_INSTALL_SUBMIT_GUARD_JS = r"""() => {
  if (window.__autofillSubmitGuard) return;
  function labelOf(node) {
    return String(node.innerText || node.value || node.getAttribute("aria-label") || "")
      .replace(/\s+/g, " ")
      .trim();
  }
  function isFinal(node) {
    if (!node || node.nodeType !== 1) return false;
    const tag = String(node.tagName || "").toLowerCase();
    const type = String(node.getAttribute("type") || "").toLowerCase();
    const role = String(node.getAttribute("role") || "").toLowerCase();
    if (tag !== "button" && tag !== "input" && role !== "button") return false;
    const name = labelOf(node);
    if (/\b(next|continue|save\s*(and|&)\s*continue|proceed|review)\b/i.test(name)) return false;
    if (/\b(submit(\s+application)?|send(\s+my)?\s+application|send)\b/i.test(name)) return true;
    if (/\b(finish(\s+application)?|complete\s+application)\b/i.test(name)) return true;
    return type === "submit" && !name;
  }
  function stop(event) {
    event.preventDefault();
    event.stopPropagation();
  }
  function onClick(event) {
    const path = typeof event.composedPath === "function" ? event.composedPath() : [event.target];
    for (const node of path) {
      if (isFinal(node)) {
        stop(event);
        return;
      }
    }
  }
  function onSubmit(event) {
    const submitter = event.submitter || null;
    if (submitter && isFinal(submitter)) {
      stop(event);
      return;
    }
    const form = event.target;
    if (submitter || !form || !form.querySelectorAll) return;
    for (const button of form.querySelectorAll("button, input")) {
      const type = String(button.getAttribute("type") || (button.tagName === "BUTTON" ? "submit" : "")).toLowerCase();
      if (type === "submit" && isFinal(button)) {
        stop(event);
        return;
      }
    }
  }
  document.addEventListener("click", onClick, true);
  document.addEventListener("submit", onSubmit, true);
  window.__autofillSubmitGuard = true;
  window.__autofillSubmitGuardOff = () => {
    document.removeEventListener("click", onClick, true);
    document.removeEventListener("submit", onSubmit, true);
    window.__autofillSubmitGuard = false;
  };
}"""

_REMOVE_SUBMIT_GUARD_JS = """() => {
  if (window.__autofillSubmitGuardOff) window.__autofillSubmitGuardOff();
}"""


def install_final_submit_guard(page) -> None:
    """Hold Submit application clicks and implicit submits for one nested scope."""
    depth = _GUARD_DEPTH.get()
    _GUARD_DEPTH.set(depth + 1)
    if depth != 0 or page is None:
        return
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return
    try:
        evaluate(_INSTALL_SUBMIT_GUARD_JS)
    except Exception:
        return


def release_final_submit_guard(page) -> None:
    """Drop one scope. The listener comes off when the outermost fill finishes."""
    depth = _GUARD_DEPTH.get()
    if depth <= 0:
        return
    depth -= 1
    _GUARD_DEPTH.set(depth)
    if depth != 0 or page is None:
        return
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return
    try:
        evaluate(_REMOVE_SUBMIT_GUARD_JS)
    except Exception:
        return


@contextmanager
def hold_final_submit(page):
    """Cancel final-submit activation until this fill or answer returns."""
    install_final_submit_guard(page)
    try:
        yield
    finally:
        release_final_submit_guard(page)


def perform_click(
    page,
    selector: str,
    name: str,
    *,
    control_type: str = "",
    purpose: str,
    frame_url: str = "",
    at_point: bool = False,
) -> None:
    """Central click guard.

    If the control is a final application submission, this raises
    :class:`HumanSubmissionRequired` before any click. That is true for every
    purpose, so an adapter cannot reach Submit by asking for a login click.
    The live label is checked as well: a caller that says Next must not
    activate an element that now reads Submit application.
    """
    if not HUMAN_SUBMIT_ONLY:
        raise HumanSubmissionRequired("HUMAN_SUBMIT_ONLY was disabled. That is not a supported configuration.")
    klass = action_class(name, control_type=control_type)
    if klass == ActionClass.FINAL_APPLICATION_SUBMIT_FORBIDDEN:
        raise HumanSubmissionRequired(
            f"Refusing to activate {name!r}. A person must click the final Submit or Apply control."
        )
    _refuse_live_submit(page, selector, name, control_type=control_type, frame_url=frame_url)
    if purpose == "login":
        if klass != ActionClass.LOGIN_SUBMIT_ALLOWED:
            raise SubmitBlockedError(
                f"Refusing to activate {name!r}. Only an existing-account Log in or Sign in is allowed."
            )
        _click(page, selector, frame_url, at_point)
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
        _click(page, selector, frame_url, at_point)
        return
    if purpose == "account_switch":
        if not _ACCOUNT_NAME_RE.search(name):
            raise SubmitBlockedError(
                f"Refusing to activate {name!r}. Only a Create Account or Sign In switch is allowed."
            )
        _bound_target(page, frame_url).locator(selector).click()
        return
    raise SubmitBlockedError(f"Unknown click purpose {purpose!r}.")


# A checkbox or radio is toggled with a native click. Setting ``.checked`` from
# script paints the box, but React (Workday, Greenhouse, Ashby) keeps its own
# state and the site still reports the box as unchecked. This press refuses any
# node that is not a checkbox or radio input, so it cannot reach a button.
PRESS_CHOICE_JS = """(node, on) => {
  if (!node || node.tagName !== "INPUT" || (node.type !== "checkbox" && node.type !== "radio")) return false;
  if (node.disabled) return false;
  if (node.checked === on) return true;
  node.click();
  return node.checked === on;
}"""


def press_choice_label(page, selector: str) -> None:
    """Click the label of one checkbox or radio input.

    Used when a styled wrapper covers the box itself. The label may be
    ``label[for]``, a wrapping label, or a sibling label. A button, link, or
    submit is never clicked. An unreadable target is refused.
    """
    if not selector:
        raise SubmitBlockedError("label press needs a selector")
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        raise SubmitBlockedError("label press refused")
    try:
        result = evaluate(_LABEL_PRESS_JS, selector)
    except Exception as exc:
        raise SubmitBlockedError("label press refused") from exc
    if result != "clicked":
        raise SubmitBlockedError("label press refused")


_LABEL_PRESS_JS = r"""(selector) => {
  let el = null;
  try {
    el = document.querySelector(selector);
  } catch (error) {
    return "refused";
  }
  if (!el || el.tagName !== "INPUT" || (el.type !== "checkbox" && el.type !== "radio")) return "refused";
  if (el.disabled) return "refused";
  const action = "button, a[href], [role='button'], [role='link'], input[type='submit'], input[type='button']";
  if (el.closest(action)) return "refused";
  let label = el.closest("label");
  if (!label && el.id) {
    label = document.querySelector("label[for='" + CSS.escape(el.id) + "']");
  }
  if (!label && el.parentElement) {
    for (const child of el.parentElement.children) {
      if (!child || child.tagName !== "LABEL" || child.contains(el)) continue;
      if (child.querySelector("input, select, textarea, button")) continue;
      label = child;
      break;
    }
  }
  if (!label) return "missing";
  if (label.closest(action) || label.querySelector(action)) return "refused";
  label.click();
  return "clicked";
}"""


_CHOICE_KIND_JS = r"""(selector) => {
  let el = null;
  try {
    el = document.querySelector(selector);
  } catch (error) {
    return "";
  }
  if (!el || el.tagName !== "INPUT") return "";
  const type = String(el.type || el.getAttribute("type") || "").toLowerCase();
  if (type === "checkbox" || type === "radio") return type;
  return "";
}"""


_CHOICE_STATE_JS = r"""(selector) => {
  let el = null;
  try {
    el = document.querySelector(selector);
  } catch (error) {
    return null;
  }
  if (!el) return null;
  const type = String(el.type || el.getAttribute("type") || "").toLowerCase();
  const role = String(el.getAttribute("role") || "").toLowerCase();
  if (el.tagName === "INPUT" && (type === "checkbox" || type === "radio")) return !!el.checked;
  if (role === "checkbox" || role === "radio" || role === "switch") {
    const state = el.getAttribute("aria-checked");
    if (state === "true") return true;
    if (state === "false" || state === "mixed") return false;
  }
  return null;
}"""


def _choice_state(page, selector: str) -> bool | None:
    """The page's checked or aria-checked state, or None when it cannot be read."""
    evaluate = getattr(page, "evaluate", None)
    if evaluate is not None:
        try:
            state = evaluate(_CHOICE_STATE_JS, selector)
        except Exception:
            state = None
        if isinstance(state, bool) or state is None:
            return state if isinstance(state, bool) else None
    reader = getattr(page.locator(selector), "is_checked", None)
    if reader is None:
        return None
    try:
        return bool(reader())
    except Exception:
        return None


def commit_native_choice(page, selector: str, *, on: bool = True) -> bool:
    """Check or uncheck one native checkbox or radio, then reread the page.

    Order is a pointer check, a forced check, then the label. True only when
    ``checked`` or ``aria-checked`` still matches. A button, link, or submit
    is not clicked. An unreadable state is not success.
    """
    if not selector:
        return False
    state = _choice_state(page, selector)
    if on and state is True:
        return True
    if not on and state is False:
        return True
    evaluate = getattr(page, "evaluate", None)
    kind = ""
    if evaluate is not None:
        try:
            raw_kind = evaluate(_CHOICE_KIND_JS, selector)
        except Exception:
            raw_kind = ""
        # A unit-test page returns its snapshot for every evaluate. Only a
        # real string result can say this node is not a checkbox or radio.
        if isinstance(raw_kind, str):
            kind = raw_kind
            if kind not in {"checkbox", "radio"}:
                return False
            if kind == "radio" and not on:
                return False

    def pointer() -> None:
        locator = page.locator(selector)
        if on:
            locator.check(timeout=1500)
        else:
            locator.uncheck(timeout=1500)

    def forced() -> None:
        locator = page.locator(selector)
        if on:
            locator.check(force=True, timeout=3000)
        else:
            locator.uncheck(force=True, timeout=3000)

    def label() -> None:
        press_choice_label(page, selector)

    for attempt in (pointer, forced, label):
        state = _choice_state(page, selector)
        if on and state is True:
            return True
        if not on and state is False:
            return True
        try:
            attempt()
        except TypeError:
            try:
                if on:
                    page.locator(selector).check()
                else:
                    page.locator(selector).uncheck()
            except Exception:
                continue
        except Exception:
            continue
        state = _choice_state(page, selector)
        if on and state is True:
            return True
        if not on and state is False:
            return True
    state = _choice_state(page, selector)
    if on:
        return state is True
    return state is False


_ARIA_RADIO_JS = r"""(selector) => {
  let el = null;
  try {
    el = document.querySelector(selector);
  } catch (error) {
    return false;
  }
  if (!el) return false;
  const tag = String(el.tagName || "").toLowerCase();
  const type = String(el.getAttribute("type") || "").toLowerCase();
  const role = String(el.getAttribute("role") || "").toLowerCase();
  if (tag === "button" || tag === "a" || type === "submit" || type === "button") return false;
  if (role === "button" || role === "link") return false;
  if (el.closest("button, a[href], [role='button'], [role='link'], input[type='submit']")) return false;
  if (role !== "radio") return false;
  if (el.getAttribute("aria-checked") === "true") return true;
  el.click();
  return el.getAttribute("aria-checked") === "true";
}"""


def press_aria_radio(page, selector: str) -> bool:
    """Click one ``role=radio`` node that is not a button, link, or submit.

    Success is ``aria-checked=true`` after the click.
    """
    if not selector:
        return False
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return False
    try:
        return evaluate(_ARIA_RADIO_JS, selector) is True
    except Exception:
        return False
