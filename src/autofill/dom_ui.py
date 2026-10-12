"""Python wrapper for the visible-automation script.

Text is typed through ``window.__afUI``. The red outline is reserved for the
human-action blocker. Answer values are not logged here.
"""

from __future__ import annotations

import contextvars
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from autofill.adapters import page_step

_JS_PATH = Path(__file__).with_name("dom_ui.js")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b")

HIGHLIGHT_KINDS = frozenset({"QUESTION", "MISSING_PROFILE_VALUE", "OPTION_MISMATCH", "DEMOGRAPHIC"})
_CONSENT_KIND_ALIASES = frozenset({"ACKNOWLEDGMENT", "ACKNOWLEDGEMENT"})
_FILLED_ACTIONS = frozenset({"fill", "select", "check", "uncheck", "upload"})
_STATUS_BLOCKERS = {
    "CAPTCHA_REQUIRED": ("CAPTCHA", "Complete the challenge", None),
    "LOGIN_REQUIRED": ("LOGIN", "Sign in", None),
    "ACCOUNT_CREATION_REQUIRED": ("ACCOUNT", "Create employer account", None),
    "EMAIL_VERIFICATION_REQUIRED": ("VERIFICATION", "Email verification", None),
    "NO_FORM_FOUND": ("NO_FORM", "No application form", None),
    "RESUME_UPLOAD_REQUIRED": ("RESUME", "Resume", "resume.file"),
}
_READY = frozenset({"READY_FOR_HUMAN_SUBMIT", "FILLED"})

STAGE_PERCENT = {
    "OPENING": 5,
    "ACCOUNT": 8,
    "LOGIN": 10,
    "VERIFICATION": 12,
    "CAPTCHA": 12,
    "PERSONAL": 20,
    "ADDRESS": 30,
    "EMPLOYMENT": 45,
    "EDUCATION": 60,
    "QUESTIONS": 75,
    "RESUME": 85,
    "REVIEW": 95,
    "READY": 100,
}
STAGE_TITLES = {
    "OPENING": "Opening",
    "ACCOUNT": "Employer account",
    "LOGIN": "Sign in",
    "VERIFICATION": "Email verification",
    "CAPTCHA": "Verification check",
    "PERSONAL": "Personal information",
    "ADDRESS": "Address",
    "EMPLOYMENT": "Employment History",
    "EDUCATION": "Education",
    "QUESTIONS": "Questions",
    "RESUME": "Resume",
    "REVIEW": "Review",
    "READY": "Ready",
}

_FOCUS_JS = """(selector) => {
  const el = document.querySelector(selector);
  if (!el || !window.__afUI) return false;
  window.__afUI.focusField(el);
  return true;
}"""

_TYPE_JS = """async ({ selector, text, append }) => {
  const el = document.querySelector(selector);
  if (!el || !window.__afUI) return false;
  return await window.__afUI.typeVisible(el, text, !!append);
}"""

_HIGHLIGHT_JS = """(selector) => {
  const el = document.querySelector(selector);
  if (!el || !window.__afUI) return false;
  window.__afUI.highlight(el);
  return true;
}"""

_CLEAR_JS = """(selector) => {
  if (!window.__afUI) return false;
  if (!selector) {
    window.__afUI.clearHighlight(null);
    return true;
  }
  const el = document.querySelector(selector);
  window.__afUI.clearHighlight(el);
  return true;
}"""

_COMBOBOX_JS = r"""(selector) => {
  const el = document.querySelector(selector);
  if (!el || el.tagName === "SELECT") return false;
  const role = String(el.getAttribute("role") || "").toLowerCase();
  const cls = String(el.className || "");
  if (role === "combobox" || cls.indexOf("select__input") !== -1) return true;
  return !!(el.closest && el.closest("[class*='select__control']"));
}"""

_CHOOSE_JS = """({ selector, expected, multi }) => {
  const root = document.querySelector(selector);
  if (!root) return false;
  const wanted = multi && Array.isArray(expected) ? expected : [expected];
  function labelOf(node) {
    const wrap = node.closest("label");
    const raw = wrap ? wrap.innerText : (node.getAttribute("aria-label") || node.value || "");
    return String(raw || "").replace(/\\s+/g, " ").trim();
  }
  function paint(node) {
    node.dispatchEvent(new Event("input", { bubbles: true }));
    node.dispatchEvent(new Event("change", { bubbles: true }));
  }
  if (root.tagName === "SELECT") {
    const match = Array.from(root.options).find((option) => {
      const label = String(option.label || option.text || "").trim();
      return label === wanted[0] || String(option.value || "").trim() === wanted[0];
    });
    if (!match) return false;
    root.value = match.value;
    paint(root);
    return true;
  }
  const name = root.name || "";
  const kind = root.type === "checkbox" ? "checkbox" : "radio";
  const group = name
    ? Array.from(document.querySelectorAll("input")).filter((node) => node.type === kind && node.name === name)
    : [root];
  if (root.type === "checkbox" && !multi) {
    const word = String(wanted[0] || "").trim();
    const on = word === "Yes" || word === "True" || word === "true";
    root.checked = on;
    paint(root);
    return true;
  }
  let hits = 0;
  for (const word of wanted) {
    const match = group.find((node) => String(node.value || "").trim() === word || labelOf(node) === word);
    if (!match) return false;
    match.checked = true;
    paint(match);
    hits += 1;
  }
  return hits === wanted.length && hits > 0;
}"""

_VERIFY_JS = """({ selector, expected, multi, fieldKey }) => {
  const root = document.querySelector(selector);
  if (!root) return false;
  const wanted = (multi && Array.isArray(expected) ? expected : [expected]).map((item) => String(item || "").trim());
  function fold(value) {
    return String(value || "").replace(/\\s+/g, " ").trim().toLowerCase();
  }
  function digits(value) {
    return String(value || "").replace(/\\D/g, "");
  }
  function labelOf(node) {
    const wrap = node.closest("label");
    let raw = wrap ? wrap.innerText : "";
    if (!raw && node.id) {
      const labelled = document.querySelector("label[for='" + CSS.escape(node.id) + "']");
      raw = labelled ? labelled.innerText : "";
    }
    if (!raw) raw = node.getAttribute("aria-label") || "";
    return String(raw || "").replace(/\\s+/g, " ").trim();
  }
  function phoneField(node) {
    const key = String(fieldKey || "").toLowerCase();
    if (key === "phone") return true;
    const type = String(node.getAttribute("type") || node.type || "").toLowerCase();
    const auto = String(node.getAttribute("autocomplete") || "").toLowerCase();
    if (type === "tel" || auto === "tel" || auto === "tel-national" || auto.indexOf("tel") === 0) return true;
    const hint = [node.id, node.name, node.getAttribute("placeholder") || "", labelOf(node)].join(" ").toLowerCase();
    return /\\b(phone|mobile|telephone|cell)\\b/.test(hint);
  }
  function phonesEqual(left, right) {
    const a = digits(left);
    const b = digits(right);
    if (!a || !b) return false;
    if (a === b) return true;
    const longer = a.length >= b.length ? a : b;
    const shorter = a.length >= b.length ? b : a;
    const extra = longer.length - shorter.length;
    return shorter.length >= 7 && extra >= 1 && extra <= 3 && longer.slice(extra) === shorter;
  }
  if (root.tagName === "SELECT") {
    const chosen = root.options[root.selectedIndex];
    if (!chosen) return false;
    const label = String(chosen.label || chosen.text || "");
    const value = String(root.value || "");
    return fold(label) === fold(wanted[0]) || fold(value) === fold(wanted[0]);
  }
  if (root.type === "radio" || (root.type === "checkbox" && multi)) {
    const name = root.name || "";
    const kind = root.type;
    const group = name
      ? Array.from(document.querySelectorAll("input")).filter((node) => node.type === kind && node.name === name)
      : [root];
    return wanted.every((word) => group.some((node) => {
      return node.checked && (fold(node.value) === fold(word) || fold(labelOf(node)) === fold(word));
    }));
  }
  if (root.type === "checkbox") {
    const word = wanted[0];
    const on = word === "Yes" || word === "True" || word === "true";
    return root.checked === on;
  }
  if (phoneField(root)) return phonesEqual(root.value, wanted[0]);
  return fold(root.value) === fold(wanted[0]);
}"""


class PauseGate:
    """Stops between fields. ``wait`` blocks until ``resume``. Both calls are idempotent."""

    def __init__(self) -> None:
        self._resume = threading.Event()
        self._resume.set()
        self._lock = threading.Lock()
        self.paused = False

    def pause(self) -> None:
        with self._lock:
            self.paused = True
            self._resume.clear()

    def resume(self) -> None:
        with self._lock:
            self.paused = False
            self._resume.set()

    def wait(self) -> None:
        self._resume.wait()


class RunState:
    """Mutable progress shared with the HTTP thread for one fill."""

    def __init__(
        self,
        *,
        page: Any,
        pause_gate: PauseGate | None,
        held_selectors: set[str],
        open_items: list[dict[str, Any]],
        progress: dict[str, Any],
        url: str,
        ats: str | None,
    ) -> None:
        self.page = page
        self.pause_gate = pause_gate
        self.refill_cleared = False
        self.filled_keys: list[str] = []
        self.held_selectors = held_selectors
        self.open_items = open_items
        self.progress = progress
        self.url = url
        self.ats = ats
        self.seen: set[str] = set()
        self.done: set[str] = set()
        self.selector_to_key: dict[str, str] = {}
        self.halted = ""
        self.token: contextvars.Token | None = None


_RUN: contextvars.ContextVar[RunState | None] = contextvars.ContextVar("af_run", default=None)


def empty_progress() -> dict[str, Any]:
    return {
        "stage": "OPENING",
        "requiredTotal": 0,
        "requiredDone": 0,
        "percent": STAGE_PERCENT["OPENING"],
        "currentLabel": STAGE_TITLES["OPENING"],
    }


def load_script() -> str:
    return _JS_PATH.read_text(encoding="utf-8")


def fast_pace() -> bool:
    return os.environ.get("AUTOFILL_TYPE_DELAY") == "0"


def install_source() -> str:
    script = load_script()
    if fast_pace():
        return script + "\nwindow.__afFast = true;\n"
    return script


_PUMP = threading.local()


def bind_queue_pump(pump: Callable[[], None]) -> None:
    """Let a field boundary run calls queued on this worker, such as a screenshot."""
    _PUMP.fn = pump


def release_queue_pump() -> None:
    _PUMP.fn = None


def _pump_queue() -> None:
    pump = getattr(_PUMP, "fn", None)
    if pump is not None:
        pump()


def install(page: Any) -> None:
    """Inject ``window.__afUI``. Safe to call on every field."""
    script = install_source()
    add = getattr(page, "add_init_script", None)
    if add is not None and not getattr(page, "_af_ui_init", False):
        try:
            add(script)
            page._af_ui_init = True
        except Exception:
            pass
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return
    try:
        evaluate(script)
    except Exception:
        return


def current_run() -> RunState | None:
    return _RUN.get()


def begin_run(
    *,
    page: Any = None,
    pause_gate: PauseGate | None = None,
    held_selectors: set[str] | None = None,
    open_items: list[dict[str, Any]] | None = None,
    progress: dict[str, Any] | None = None,
    url: str = "",
    ats: str | None = None,
    refill_cleared: bool = False,
) -> contextvars.Token:
    shared = progress if progress is not None else empty_progress()
    shared.clear()
    shared.update(empty_progress())
    items = open_items if open_items is not None else []
    items.clear()
    state = RunState(
        page=page,
        pause_gate=pause_gate,
        held_selectors=held_selectors if held_selectors is not None else set(),
        open_items=items,
        progress=shared,
        url=url,
        ats=ats,
    )
    state.refill_cleared = refill_cleared
    token = _RUN.set(state)
    state.token = token
    return token


def end_run(token: contextvars.Token | None) -> None:
    if token is None:
        return
    state = _RUN.get()
    if state is not None and state.token is token:
        _RUN.reset(token)


def attach_page(page: Any) -> None:
    state = current_run()
    if state is not None:
        state.page = page


def typing_delays(text: str, randint) -> list[int]:
    """Per-character delays in the closed range 30..70. Long text types 40 of them."""
    count = 40 if len(text) > 200 else len(text)
    return [int(randint(30, 70)) for _ in range(count)]


def progress_percent(stage: str, required_total: int, required_done: int) -> int:
    """Required progress when there are required fields. Otherwise a fixed stage value.

    100 means every counted field is done. A partial count never rounds up to 100.
    """
    if required_total > 0:
        done = min(max(required_done, 0), required_total)
        if done >= required_total:
            return 100
        return min(99, max(0, int(round(100 * done / required_total))))
    percent = STAGE_PERCENT.get(stage, STAGE_PERCENT["OPENING"])
    # A stage value of 100 with nothing counted is the false Ready the posting page produced.
    if percent >= 100:
        return 0
    return percent


def safe_label(text: str) -> str:
    """A short section label. Emails and phone numbers are removed."""
    cleaned = _EMAIL_RE.sub("", text or "")
    cleaned = _PHONE_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -,:;")
    if len(cleaned) > 80:
        cleaned = cleaned[:80].rstrip()
    return cleaned


_PERSONAL_KEYS = frozenset(
    {
        "first_name",
        "last_name",
        "full_name",
        "legal_name",
        "preferred_name",
        "email",
        "phone",
        "linkedin_url",
        "github_url",
        "portfolio_url",
        "website_url",
        "firstname",
        "lastname",
        "fullname",
        "legalname",
        "preferredname",
    }
)
_ADDRESS_KEYS = frozenset({"address", "city", "state", "postal_code", "country", "location"})
_EMPLOYMENT_KEYS = frozenset(
    {"current_company", "current_job_title", "internship_company", "company", "employer", "job_title"}
)
_EDUCATION_KEYS = frozenset({"school", "field_of_study", "education_level", "degree", "major"})
_RESUME_KEYS = frozenset({"resume", "cv", "resume.file"})


def blocker_stage(item: dict[str, Any] | None) -> str | None:
    """Section of the field a person is answering, or None when it is not a known section."""
    if not item:
        return None
    raw = str(item.get("fieldKey") or "").strip()
    folded = raw.casefold()
    compact = folded.replace(".", "").replace("_", "").replace("-", "")
    if folded.startswith("address.") or folded in _ADDRESS_KEYS or compact in _ADDRESS_KEYS:
        return "ADDRESS"
    if folded.startswith("employment") or folded in _EMPLOYMENT_KEYS or compact in _EMPLOYMENT_KEYS:
        return "EMPLOYMENT"
    if folded.startswith("education") or folded in _EDUCATION_KEYS or compact in _EDUCATION_KEYS:
        return "EDUCATION"
    if folded.startswith("resume") or folded in _RESUME_KEYS or compact in {"resume", "cv"}:
        return "RESUME"
    if folded.startswith("links.") or folded in _PERSONAL_KEYS or compact in _PERSONAL_KEYS:
        return "PERSONAL"
    named = stage_for("", str(item.get("text") or ""), "")
    if named != "OPENING":
        return named
    return None


def stage_for(step: str, heading: str, status: str) -> str:
    if status in _READY:
        return "READY"
    if status == "ACCOUNT_CREATION_REQUIRED":
        return "ACCOUNT"
    if status == "LOGIN_REQUIRED":
        return "LOGIN"
    if status == "EMAIL_VERIFICATION_REQUIRED":
        return "VERIFICATION"
    if status == "RESUME_UPLOAD_REQUIRED" and not heading and not step:
        return "RESUME"
    blob = f"{step} {heading}".casefold()
    if any(token in blob for token in ("log in", "login", "sign in", "sign-in")):
        return "LOGIN"
    if "address" in blob:
        return "ADDRESS"
    if any(token in blob for token in ("employment", "experience", "work history")):
        return "EMPLOYMENT"
    if "education" in blob:
        return "EDUCATION"
    if any(token in blob for token in ("resume", "cv")):
        return "RESUME"
    if "review" in blob:
        return "REVIEW"
    if any(token in blob for token in ("question", "voluntary", "eeo", "self-ident", "additional information")):
        return "QUESTIONS"
    if any(token in blob for token in ("basic", "contact", "identity", "personal", "my information", "profile")):
        return "PERSONAL"
    if step == "application":
        return "QUESTIONS"
    return "OPENING"


def _yes_no_options(options: list[Any] | None) -> bool:
    if not options:
        return False
    names: set[str] = set()
    for option in options:
        label = option.label if hasattr(option, "label") else str(option)
        token = label.strip().casefold()
        if token:
            names.add(token)
    return "yes" in names and "no" in names


def answer_type_name(kind: str, input_type: str = "", *, multi: bool = False, options: list[Any] | None = None) -> str:
    if multi:
        return "multi_select"
    if kind == "buttons" and _yes_no_options(options):
        return "boolean"
    if kind in {"select", "radio", "combobox", "buttons"}:
        return "single_select"
    if kind == "checkbox":
        return "boolean"
    if kind == "textarea":
        return "textarea"
    if input_type == "date" or kind == "date":
        return "date"
    if input_type in {"number", "range"} or kind == "number":
        return "number"
    return "text"


_COMMITTED_JS = r"""(selector) => {
  const el = document.querySelector(selector);
  if (!el) return false;
  const type = String(el.getAttribute("type") || el.type || "").toLowerCase();
  const tag = String(el.tagName || "");
  const role = String(el.getAttribute("role") || "").toLowerCase();
  function placeholder(text) {
    const folded = String(text || "").replace(/\s+/g, " ").trim().toLowerCase();
    if (!folded) return true;
    return /^(select\.{0,3}|choose\.{0,3}|please select|please choose|[-–—]+)$/.test(folded);
  }
  if (type === "radio") {
    const name = el.getAttribute("name") || "";
    if (!name) return !!el.checked;
    const nodes = document.querySelectorAll("input[type='radio']");
    for (const node of nodes) {
      if (node.getAttribute("name") === name && node.checked) return true;
    }
    return false;
  }
  if (type === "checkbox") {
    if (el.checked) return true;
    const name = el.getAttribute("name") || "";
    if (!name) return false;
    const nodes = document.querySelectorAll("input[type='checkbox']");
    for (const node of nodes) {
      if (node.getAttribute("name") === name && node.checked) return true;
    }
    return false;
  }
  if (tag === "SELECT") {
    const value = String(el.value || "").trim();
    if (!value) return false;
    const option = el.options[el.selectedIndex];
    const label = option ? String(option.label || option.text || "") : "";
    return !placeholder(label);
  }
  if (type === "file") return !!(el.files && el.files.length);
  if (tag === "BUTTON" || role === "button" || role === "listbox" || role === "combobox") {
    const popup = String(el.getAttribute("aria-haspopup") || "").toLowerCase();
    if (popup === "listbox" || role === "listbox") {
      const shown = String(el.innerText || el.textContent || "").replace(/\s+/g, " ").trim();
      return shown.length > 0 && !placeholder(shown);
    }
    if (role === "combobox" || tag === "INPUT" || tag === "TEXTAREA") {
      const typed = String(el.value || "").trim();
      if (typed && !placeholder(typed)) return true;
      const root = el.closest(".select__control") || el.parentElement;
      const choice = root ? root.querySelector(".select__single-value, .select__multi-value") : null;
      const shown = choice ? String(choice.textContent || "").replace(/\s+/g, " ").trim() : "";
      return shown.length > 0 && !placeholder(shown);
    }
    function selected(node) {
      if (!node) return false;
      if (node.getAttribute("aria-pressed") === "true" || node.getAttribute("aria-selected") === "true") {
        return true;
      }
      if (node.getAttribute("aria-checked") === "true") return true;
      const cls = String(node.className || "").toLowerCase();
      return /(^|\s)(selected|is-selected)(\s|$)/.test(cls);
    }
    let box = el.parentElement;
    for (let depth = 0; box && depth < 6; depth += 1, box = box.parentElement) {
      const kids = box.querySelectorAll("button, [role='button']");
      if (kids.length < 2 || kids.length > 8) continue;
      for (const kid of kids) {
        if (selected(kid)) return true;
      }
      return false;
    }
    return selected(el);
  }
  const value = "value" in el ? el.value : el.textContent;
  return String(value || "").trim().length > 0;
}"""


def control_committed(page: Any, selector: str) -> bool:
    """True when the live control holds a real value.

    A page with no ``evaluate`` (a unit-test double) is treated as committed so
    those tests do not invent a blocker. A real page that throws is not committed.
    """
    if not selector:
        return False
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return True
    try:
        return bool(evaluate(_COMMITTED_JS, selector))
    except Exception:
        return False


def sync_required_progress(committed_keys: set[str], open_keys: set[str]) -> None:
    """Count required fields from this scan only.

    ``requiredDone`` and ``requiredTotal`` both come from the fields visible now.
    A field that left the page is not kept in either number.
    """
    state = current_run()
    if state is None:
        return
    current = {key for key in committed_keys | open_keys if key}
    state.seen = set(current)
    state.done = {key for key in committed_keys if key}
    state.progress["requiredTotal"] = len(state.seen)
    state.progress["requiredDone"] = len(state.done)
    _refresh(state)


def counts_required(control: Any) -> bool:
    if not getattr(control, "required", False) or getattr(control, "hidden", False):
        return False
    if getattr(control, "kind", "") in {"password", "hidden", "file"}:
        return False
    if getattr(control, "input_type", "") == "password":
        return False
    return True


def _refresh(state: RunState) -> None:
    total = int(state.progress.get("requiredTotal") or 0)
    done = int(state.progress.get("requiredDone") or 0)
    if total:
        done = min(done, total)
    state.progress["requiredDone"] = done
    state.progress["percent"] = progress_percent(str(state.progress.get("stage") or "OPENING"), total, done)


def note_snapshot(snapshot: Any) -> None:
    state = current_run()
    if state is None or snapshot is None:
        return
    heading = str(getattr(snapshot, "heading", "") or "")
    step = page_step(state.ats, heading, state.url)
    stage = stage_for(step, heading, "")
    if stage != "OPENING" or state.progress.get("stage") == "OPENING":
        state.progress["stage"] = stage
    label = safe_label(heading) or STAGE_TITLES.get(stage, STAGE_TITLES["OPENING"])
    state.progress["currentLabel"] = label
    current: set[str] = set()
    for control in getattr(snapshot, "controls", []) or []:
        if not counts_required(control):
            continue
        key = str(getattr(control, "selector", "") or getattr(control, "name", "") or getattr(control, "label", ""))
        if not key:
            continue
        current.add(key)
        selector = str(getattr(control, "selector", "") or "")
        if selector:
            state.selector_to_key[selector] = key
        for option in getattr(control, "options", []) or []:
            option_selector = str(getattr(option, "selector", "") or "")
            if option_selector:
                state.selector_to_key[option_selector] = key
    state.seen = current
    state.done = {key for key in state.done if key in state.seen}
    state.progress["requiredTotal"] = len(state.seen)
    state.progress["requiredDone"] = len(state.done)
    _refresh(state)


def note_filled(fields: list) -> None:
    state = current_run()
    if state is None:
        return
    for field in fields:
        if getattr(field, "action", "") not in _FILLED_ACTIONS:
            continue
        selector = str(getattr(field, "selector", "") or "")
        key = state.selector_to_key.get(selector)
        if key and key not in state.done:
            state.done.add(key)
        named = str(getattr(field, "key", "") or "")
        if named and named not in state.filled_keys:
            state.filled_keys.append(named)
    state.progress["requiredDone"] = len(state.done)
    _refresh(state)


def remember_open_items(items: list[dict[str, Any]]) -> None:
    state = current_run()
    if state is None:
        return
    state.open_items.clear()
    state.open_items.extend(dict(item) for item in items)


def checkpoint() -> bool:
    """Wait for resume at a field boundary. False while login or a challenge blocks writes."""
    _pump_queue()
    state = current_run()
    if state is None:
        return True
    if state.pause_gate is not None:
        state.pause_gate.wait()
    return state.halted not in {"LOGIN", "CAPTCHA"}


def refill_cleared() -> bool:
    """Protocol 0.7.0 refills a completed field when the site has cleared it."""
    state = current_run()
    return bool(state is not None and state.refill_cleared)


def selector_held(selector: str) -> bool:
    state = current_run()
    if state is None or not selector:
        return False
    return selector in state.held_selectors


def choose_blocking(status: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Status stops win. Otherwise the first blocking item in page order."""
    if status in _STATUS_BLOCKERS:
        kind, text, field_key = _STATUS_BLOCKERS[status]
        return {
            "kind": kind,
            "questionId": None,
            "questionHash": None,
            "text": text,
            "fieldKey": field_key,
            "answerType": "text",
            "options": [],
            "required": True,
            "selector": "",
            "blocking": True,
        }
    for item in items:
        if item.get("blocking", True):
            return item
    return None


def canonical_blocker_kind(kind: object) -> str:
    """The public blocker kind. An acknowledgement alias is consent."""
    name = str(kind or "")
    if name in _CONSENT_KIND_ALIASES:
        return "CONSENT"
    return name


def public_blocking(item: dict[str, Any] | None) -> dict[str, Any] | None:
    if item is None:
        return None
    question_id = item.get("questionId") or None
    question_hash = item.get("questionHash") or question_id
    return {
        "kind": canonical_blocker_kind(item.get("kind") or "QUESTION"),
        "questionId": question_id,
        "questionHash": question_hash,
        "text": str(item.get("text") or ""),
        "fieldKey": item.get("fieldKey") or None,
        "answerType": str(item.get("answerType") or "text"),
        "options": [str(option) for option in item.get("options") or []],
        "required": bool(item.get("required")),
    }


def _apply_status_stage(state: RunState, status: str) -> None:
    if status in _READY:
        state.progress["stage"] = "READY"
        state.progress["currentLabel"] = STAGE_TITLES["READY"]
        return
    if status == "ACCOUNT_CREATION_REQUIRED":
        state.progress["stage"] = "ACCOUNT"
        state.progress["currentLabel"] = STAGE_TITLES["ACCOUNT"]
        return
    if status == "EMAIL_VERIFICATION_REQUIRED":
        state.progress["stage"] = "VERIFICATION"
        state.progress["currentLabel"] = STAGE_TITLES["VERIFICATION"]
        return
    if status == "LOGIN_REQUIRED":
        state.progress["stage"] = "LOGIN"
        if state.progress.get("currentLabel") in {"", "Opening"}:
            state.progress["currentLabel"] = STAGE_TITLES["LOGIN"]
        return
    if status == "RESUME_UPLOAD_REQUIRED" and state.progress.get("stage") == "OPENING":
        state.progress["stage"] = "RESUME"
        state.progress["currentLabel"] = STAGE_TITLES["RESUME"]


def finalize_run(status: str) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
    state = current_run()
    if state is None:
        stage = stage_for("", "", status)
        progress = empty_progress()
        progress["stage"] = stage
        progress["currentLabel"] = STAGE_TITLES.get(stage, STAGE_TITLES["OPENING"])
        progress["percent"] = progress_percent(stage, 0, 0)
        return progress, public_blocking(choose_blocking(status, [])), []
    if status in {"CAPTCHA_REQUIRED", "LOGIN_REQUIRED", "NO_FORM_FOUND"}:
        state.open_items.clear()
        if status == "CAPTCHA_REQUIRED":
            state.halted = "CAPTCHA"
        elif status == "LOGIN_REQUIRED":
            state.halted = "LOGIN"
    _apply_status_stage(state, status)
    _refresh(state)
    item = choose_blocking(status, state.open_items)
    present_blocking(state.page, item)
    return dict(state.progress), public_blocking(item), [dict(entry) for entry in state.open_items]


def present_blocking(page: Any, item: dict[str, Any] | None) -> None:
    """Center and outline a human-action field. Other kinds stay unmarked."""
    if page is None:
        return
    if item is None or str(item.get("kind") or "") not in HIGHLIGHT_KINDS:
        clear_highlight(page, None)
        return
    selector = str(item.get("selector") or "")
    if not selector:
        return
    focus_field(page, selector)
    highlight(page, selector)


def _wait_for_scroll(page: Any) -> None:
    if fast_pace():
        return
    wait = getattr(page, "wait_for_timeout", None)
    if wait is not None:
        wait(300)
        return
    import time

    time.sleep(0.3)


def focus_field(page: Any, selector: str) -> bool:
    if not selector:
        return False
    install(page)
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return False
    try:
        moved = bool(evaluate(_FOCUS_JS, selector))
    except Exception:
        return False
    if moved:
        _wait_for_scroll(page)
    return moved


def type_visible(page: Any, selector: str, text: str) -> bool:
    """Scroll the field to center, then type it. Returns false when the page cannot type."""
    if not selector:
        return False
    focus_field(page, selector)
    if _mid_type(text):
        prefix = text[:8]
        typed = _type_once(page, selector, prefix)
        if not typed:
            install(page)
            typed = _type_once(page, selector, prefix)
        if typed:
            _pump_queue()
            rest = _type_once(page, selector, text[8:], append=True)
            _pump_queue()
            return rest
    typed = _type_once(page, selector, text)
    if not typed:
        install(page)
        typed = _type_once(page, selector, text)
    _pump_queue()
    return typed


def _mid_type(text: str) -> bool:
    """One screenshot window in a long field. Off unless AUTOFILL_MID_TYPE=1."""
    return os.environ.get("AUTOFILL_MID_TYPE") == "1" and len(text) >= 40


def _type_once(page: Any, selector: str, text: str, *, append: bool = False) -> bool:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return False
    try:
        return bool(evaluate(_TYPE_JS, {"selector": selector, "text": text, "append": append}))
    except Exception:
        return False


def highlight(page: Any, selector: str) -> bool:
    if not selector:
        return False
    install(page)
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return False
    try:
        return bool(evaluate(_HIGHLIGHT_JS, selector))
    except Exception:
        return False


def clear_highlight(page: Any, selector: str | None) -> bool:
    install(page)
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return False
    try:
        return bool(evaluate(_CLEAR_JS, selector or ""))
    except Exception:
        return False


def _choice(answer_type: str) -> bool:
    return answer_type in {"single_select", "multi_select", "boolean"}


_BUTTON_CHOICE_JS = r"""({ selector, expected }) => {
  const root = document.querySelector(selector);
  if (!root) return "";
  const wanted = String(expected || "").replace(/\s+/g, " ").trim().toLowerCase();
  if (!wanted) return "";
  let box = root;
  for (let depth = 0; box && depth < 6; depth += 1, box = box.parentElement) {
    const kids = Array.from(box.querySelectorAll("button, [role='button']"));
    if (kids.length < 2 || kids.length > 8) continue;
    const names = kids.map((kid) =>
      (kid.innerText || kid.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim().toLowerCase()
    );
    if (names.indexOf("yes") === -1 || names.indexOf("no") === -1) continue;
    for (const kid of kids) {
      const text = (kid.innerText || kid.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim().toLowerCase();
      if (text !== wanted) continue;
      if (kid.id) return "#" + CSS.escape(kid.id);
      const field = kid.closest("[data-field-path]");
      const path = field ? field.getAttribute("data-field-path") || "" : "";
      const option = (kid.getAttribute("data-option") || "").trim().toLowerCase();
      if (path && option) {
        const safe = (value) => String(value).replace(/\\/g, "\\\\").replace(/'/g, "\\'");
        return "[data-field-path='" + safe(path) + "'] button[data-option='" + safe(option) + "']";
      }
      if (!kid.getAttribute("data-autofill-target")) {
        kid.setAttribute("data-autofill-target", Math.random().toString(36).slice(2));
      }
      return "[data-autofill-target='" + kid.getAttribute("data-autofill-target") + "']";
    }
    return "";
  }
  return "";
}"""


def _button_choice_selector(page: Any, selector: str, value: str) -> str:
    """The Yes or No button in this group, when the target is a button pair."""
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None or not selector or not value.strip():
        return ""
    try:
        found = evaluate(_BUTTON_CHOICE_JS, {"selector": selector, "expected": value})
    except Exception:
        return ""
    return str(found or "")


def _looks_like_combobox(page: Any, selector: str) -> bool:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return False
    try:
        return bool(evaluate(_COMBOBOX_JS, selector))
    except Exception:
        return False


def commit_visible(page: Any, target: dict[str, Any]) -> dict[str, Any]:
    """Fill one field visibly, verify the commit, and clear the outline."""
    selector = str(target.get("selector") or "")
    if not selector:
        return {"filled": False, "reason": "no_selector"}
    answer_type = str(target.get("answerType") or "text")
    values = [str(item) for item in target.get("values") or [] if str(item).strip()]
    single = str(target.get("value") or "")
    if not values and single.strip():
        values = [single]
    if not values:
        return {"filled": False, "reason": "empty"}
    if target.get("check"):
        return _check_box(page, selector)
    install(page)
    focus_field(page, selector)
    button = _button_choice_selector(page, selector, values[0])
    if button:
        from autofill.safeguards import click_choice

        try:
            click_choice(page, button, values[0])
        except Exception:
            return {"filled": False, "reason": "not_committed"}
        if not control_committed(page, selector):
            return {"filled": False, "reason": "not_committed"}
        clear_highlight(page, selector)
        return {"filled": True}
    if _looks_like_combobox(page, selector):
        from autofill.combobox import select_combobox

        try:
            picked = select_combobox(page, selector, values[0])
        except Exception:
            picked = None
        if picked is None:
            return {"filled": False, "reason": "not_committed"}
        if not control_committed(page, selector):
            return {"filled": False, "reason": "not_committed"}
        clear_highlight(page, selector)
        return {"filled": True}
    if _choice(answer_type):
        evaluate = getattr(page, "evaluate", None)
        if evaluate is None:
            return {"filled": False, "reason": "not_committed"}
        try:
            multi = answer_type == "multi_select"
            expected: str | list[str] = values if multi else values[0]
            chosen = bool(evaluate(_CHOOSE_JS, {"selector": selector, "expected": expected, "multi": multi}))
        except Exception:
            chosen = False
        if not chosen:
            return {"filled": False, "reason": "not_committed"}
    else:
        if not type_visible(page, selector, values[0]):
            return {"filled": False, "reason": "not_committed"}
    if not _committed(
        page,
        selector,
        values,
        multi=answer_type == "multi_select",
        field_key=str(target.get("fieldKey") or ""),
    ):
        return {"filled": False, "reason": "not_committed"}
    if not control_committed(page, selector):
        return {"filled": False, "reason": "not_committed"}
    clear_highlight(page, selector)
    return {"filled": True}


def _check_box(page: Any, selector: str) -> dict[str, Any]:
    """Check one checkbox through the locator. This does not click a button."""
    locator = getattr(page, "locator", None)
    if locator is None:
        return {"filled": False, "reason": "not_committed"}
    try:
        page.locator(selector).check()
    except Exception:
        return {"filled": False, "reason": "not_committed"}
    if not control_committed(page, selector):
        return {"filled": False, "reason": "not_committed"}
    clear_highlight(page, selector)
    return {"filled": True}


def _committed(page: Any, selector: str, values: list[str], *, multi: bool, field_key: str = "") -> bool:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return False
    try:
        return bool(
            evaluate(
                _VERIFY_JS,
                {
                    "selector": selector,
                    "expected": values if multi else values[0],
                    "multi": multi,
                    "fieldKey": field_key,
                },
            )
        )
    except Exception:
        return False


def values_fit(item: dict[str, Any], values: list[str]) -> bool:
    """True when every value is an option, or the control is free text."""
    reason, _canonical = fit_choice_values(item, values)
    return reason == "ok"


_CHOICE_ANSWER_TYPES = frozenset({"single_select", "multi_select", "boolean"})

_CHOICE_OPTIONS_JS = r"""(selector) => {
  const el = document.querySelector(selector);
  if (!el) return [];
  const labels = [];
  function add(text) {
    const label = String(text || "").replace(/\s+/g, " ").trim();
    if (!label || labels.indexOf(label) !== -1) return;
    const folded = label.toLowerCase().replace(/[.…]+$/g, "").trim();
    if (
      folded === "select" ||
      folded === "please select" ||
      folded === "choose" ||
      folded === "choose one" ||
      folded === "select one"
    ) {
      return;
    }
    if (
      folded.indexOf("click here") !== -1 &&
      (folded.indexOf("issue") !== -1 || folded.indexOf("browser") !== -1 || folded.indexOf("cache") !== -1)
    ) {
      return;
    }
    labels.push(label.slice(0, 200));
  }
  if (el.tagName === "SELECT") {
    Array.from(el.options).forEach((option) => add(option.label || option.text || option.value));
    return labels.slice(0, 200);
  }
  const type = String(el.getAttribute("type") || el.type || "").toLowerCase();
  const role = String(el.getAttribute("role") || "").toLowerCase();
  if (type === "radio" || type === "checkbox" || role === "radio") {
    const kind = type === "checkbox" ? "checkbox" : "radio";
    const name = el.getAttribute("name") || "";
    const nodes = name ? document.querySelectorAll("input[type='" + kind + "']") : [el];
    nodes.forEach((node) => {
      if (name && node.getAttribute("name") !== name) return;
      const wrap = node.closest("label");
      add(wrap ? wrap.innerText : (node.getAttribute("aria-label") || node.value || ""));
    });
    return labels.slice(0, 200);
  }
  if (role === "radiogroup" || el.tagName === "FIELDSET") {
    el.querySelectorAll("input[type='radio'], input[type='checkbox']").forEach((node) => {
      const wrap = node.closest("label");
      add(wrap ? wrap.innerText : (node.getAttribute("aria-label") || node.value || ""));
    });
    return labels.slice(0, 200);
  }
  return [];
}"""


def read_choice_options(page: Any, selector: str) -> list[str]:
    """Labels on a native select, radio group, or checkbox group. No menu is opened."""
    if not selector:
        return []
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return []
    try:
        raw = evaluate(_CHOICE_OPTIONS_JS, selector) or []
    except Exception:
        return []
    labels: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in labels:
            labels.append(text)
    return labels


def _place_suggestion_answer(item: dict[str, Any]) -> bool:
    """A location typeahead lists places. The answer can be a city and region, not one label."""
    key = str(item.get("fieldKey") or "").strip()
    text = str(item.get("text") or "").strip().casefold()
    location = key in {"location", "address.city", "city"} or "working from" in text or "payroll tax" in text
    if not location and text not in {"location", "your location", "current location", "your current location"}:
        return False
    return any("," in str(option) for option in item.get("options") or [])


def fit_choice_values(item: dict[str, Any], values: list[str]) -> tuple[str, list[str]]:
    """Rewrite a unique choice to its option label.

    ``ok`` keeps free text when the control has no options. ``needs_option``
    is no match, or more than one option at the best tier.
    A location typeahead keeps the caller's place text so ``Austin, TX`` can
    still choose among the live suggestions.
    """
    options = [str(option).strip() for option in item.get("options") or [] if str(option).strip()]
    answer_type = str(item.get("answerType") or "")
    if _place_suggestion_answer(item):
        return "ok", list(values)
    if answer_type not in _CHOICE_ANSWER_TYPES or not options:
        if options and not all(value.strip() in set(options) for value in values):
            return "needs_option", options
        return "ok", list(values)
    from autofill.intents import pick_option
    from autofill.models import Option

    catalog = [Option(value=label, label=label) for label in options]
    canonical: list[str] = []
    saw_ambiguous = False
    saw_none = False
    for value in values:
        status, option = pick_option(value, catalog)
        if status == "ambiguous":
            saw_ambiguous = True
        elif status != "match" or option is None:
            saw_none = True
        else:
            canonical.append(option.label or option.value)
    if saw_ambiguous or saw_none:
        return "needs_option", options
    return "ok", canonical


def match_open_item(
    items: list[dict[str, Any]],
    *,
    question_id: str = "",
    question_hash: str = "",
    field_key: str = "",
    prefer: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Match one open item. Every identifier that was sent has to agree.

    When several fields share a key, the current blocker wins over the first one.
    """
    wanted: list[tuple[str, str]] = []
    if question_id.strip():
        wanted.append(("id", question_id.strip()))
    if question_hash.strip():
        wanted.append(("hash", question_hash.strip()))
    if field_key.strip():
        wanted.append(("key", field_key.strip()))
    if not wanted:
        return None
    matches: list[dict[str, Any]] = []
    for item in items:
        matched = True
        for kind, value in wanted:
            if kind == "key":
                if str(item.get("fieldKey") or "") != value:
                    matched = False
            elif value not in {item.get("questionId"), item.get("questionHash")}:
                matched = False
        if matched:
            matches.append(item)
    if not matches:
        return None
    if prefer and len(matches) > 1:
        selector = str(prefer.get("selector") or "")
        digest = str(prefer.get("questionHash") or prefer.get("questionId") or "")
        for item in matches:
            if selector and str(item.get("selector") or "") == selector:
                return item
            if digest and digest in {item.get("questionId"), item.get("questionHash")}:
                return item
    return matches[0]
