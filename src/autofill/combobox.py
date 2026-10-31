"""Open one combobox and read only the listbox that belongs to it.

A phone-country list that is already on the page is not an option list for a
different question. A Workday ``selectedItemList`` is the value already chosen
on that prompt, not the prompt menu. Options that appear after the control is
opened are read from ``aria-controls`` / ``aria-owns``, from the list inside
that form field, or from the open ``promptOption`` menu.
"""

from __future__ import annotations

import re
import time

from autofill.dom_ui import focus_field, type_char_delay
from autofill.intents import match_option_exact
from autofill.models import Option
from autofill.safeguards import click_choice, dismiss_combobox, open_combobox

# A menu may show any of these for one country. Longer names are typed first.
# Calling codes let a phone-country row such as "United States +1" match too.
_COUNTRY_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"united states", "united states of america", "usa", "us", "+1"}),
    frozenset({"united kingdom", "great britain", "uk", "gb", "+44"}),
)
_CALLING_CODES: tuple[tuple[str, str], ...] = (
    ("+44", "United Kingdom"),
    ("+1", "United States"),
)
_DIAL_SUFFIX = re.compile(r"\s*\+\d{1,4}\s*$")
_DIAL_PREFIX = re.compile(r"^\+\d{1,4}\s+")


def _fold(value: str) -> str:
    text = str(value or "").casefold().replace(".", "")
    return re.sub(r"\s+", " ", text).strip()


def _country_name(value: str) -> str:
    """Drop a leading or trailing dial code so ``United States +1`` is the country."""
    text = _DIAL_SUFFIX.sub("", _fold(value))
    return _DIAL_PREFIX.sub("", text).strip()


def country_aliases(value: str) -> list[str]:
    """Names a country menu might show for ``value``, longest first."""
    token = _fold(value)
    name = _country_name(value)
    if not token and not name:
        return []
    for group in _COUNTRY_GROUPS:
        if token in group or name in group:
            return sorted(group, key=len, reverse=True)
    return [name or token]


def country_from_phone(phone: str) -> str:
    """Country name for a phone that starts with a known calling code, else empty."""
    compact = re.sub(r"[^\d+]", "", str(phone or ""))
    if compact.startswith("00"):
        compact = "+" + compact[2:]
    if not compact.startswith("+"):
        return ""
    for code, name in _CALLING_CODES:
        if compact.startswith(code):
            return name
    return ""


def _known_country(value: str) -> bool:
    token = _fold(value)
    name = _country_name(value)
    return any(token in group or name in group for group in _COUNTRY_GROUPS)


def match_country_option(desired: str, options: list[Option]) -> Option | None:
    """Case-insensitive match across United States / USA / US / ``United States +1``."""
    if not _known_country(desired):
        return None
    aliases = set(country_aliases(desired))
    for option in options:
        label = _country_name(option.label)
        value = _country_name(option.value)
        if label in aliases or value in aliases or _fold(option.label) in aliases or _fold(option.value) in aliases:
            return option
    return None


_SCOPED_OPTIONS_JS = r"""
(args) => {
  const selector = args && args.selector ? args.selector : args;
  const allowSingle = !!(args && args.allowSingle);
  const el = document.querySelector(selector);
  if (!el) return [];
  function textOf(node) {
    return (node.innerText || node.textContent || "").replace(/\s+/g, " ").trim();
  }
  function optionEnabled(option) {
    if (!option) return false;
    if (option.disabled) return false;
    return option.getAttribute("aria-disabled") !== "true";
  }
  function claimedByOther(list) {
    if (!list.id) return false;
    const query = "[aria-controls='" + CSS.escape(list.id) + "'], [aria-owns='" + CSS.escape(list.id) + "']";
    const owner = document.querySelector(query);
    return !!(owner && owner !== el);
  }
  function pageRoot(node) {
    if (!node || !node.tagName) return true;
    const tag = node.tagName.toLowerCase();
    return tag === "body" || tag === "html" || tag === "form";
  }
  function isSelectedItemNode(node) {
    // The Country Phone Code pill ("India (+91)") lives in selectedItemList.
    // That list is not How Did You Hear About Us's promptOption menu.
    if (!node || !node.closest) return false;
    const auto = ((node.getAttribute && node.getAttribute("data-automation-id")) || "").toLowerCase();
    if (
      auto === "selecteditem" ||
      auto === "selecteditemlist" ||
      auto === "selecteditemtext" ||
      auto === "promptselectionlabel"
    ) {
      return true;
    }
    return !!node.closest(
      "[data-automation-id='selectedItemList'], [data-automation-id='selectedItem'], " +
      "[data-automation-id='selectedItemText']"
    );
  }
  function owningField(node) {
    if (!node || !node.closest) return null;
    return node.closest("[data-automation-id^='formField']")
      || node.closest("[data-field-path], fieldset");
  }
  let list = null;
  const owned = (el.getAttribute("aria-controls") || el.getAttribute("aria-owns") || "").trim();
  if (owned) {
    for (const id of owned.split(/\s+/)) {
      const node = document.getElementById(id);
      if (node && (node.getAttribute("role") || "") === "listbox" && !claimedByOther(node)) {
        list = node;
        break;
      }
    }
  }
  // A list inside this control counts. A list elsewhere on the page does not,
  // even when it is the only visible listbox.
  const widget = [
    ".select",
    "[class*='select__']",
    "fieldset",
    "[data-automation-id='multiSelectContainer']",
    "[data-automation-id='multiselectInputContainer']",
  ].join(", ");
  const owner = owningField(el);
  let container = el.closest(widget) || el.parentElement;
  for (let depth = 0; container && depth < 4 && !list; depth += 1) {
    if (pageRoot(container)) break;
    // Do not climb out of this form field into a sibling prompt's selected list.
    if (owner && container !== owner && !owner.contains(container)) break;
    for (const candidate of container.querySelectorAll("[role='listbox']")) {
      if (isSelectedItemNode(candidate) || claimedByOther(candidate)) continue;
      const candidateOwner = owningField(candidate);
      if (owner && candidateOwner && candidateOwner !== owner) continue;
      list = candidate;
      break;
    }
    const parent = container.parentElement;
    if (!parent || pageRoot(parent)) break;
    if (owner && parent !== owner && !owner.contains(parent)) break;
    container = parent;
  }
  function shown(node) {
    if (!node || node.hidden) return false;
    const style = window.getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0;
  }
  function optionShown(option) {
    if (!option || option.hidden) return false;
    const style = window.getComputedStyle(option);
    return style.display !== "none" && style.visibility !== "hidden";
  }
  const optionNodes = [];
  const optionSelector = [
    "[role='option']",
    "[data-automation-id='promptOption']",
    "[data-automation-id='promptLeafNode']",
    "[data-automation-id='menuItem']",
  ].join(", ");
  if (list) {
    for (const option of list.querySelectorAll(optionSelector)) {
      if (optionEnabled(option) && optionShown(option) && !isSelectedItemNode(option)) optionNodes.push(option);
    }
  }
  const promptRoot = el.closest(
    "[data-automation-id='multiSelectContainer'], [data-automation-id='multiselectInputContainer']"
  );
  function promptOpen(node) {
    if (!node) return false;
    if (node.getAttribute("aria-expanded") === "true") return true;
    const root = node.closest(
      "[data-automation-id='multiSelectContainer'], [data-automation-id='multiselectInputContainer']"
    );
    return !!(root && root.querySelector("[aria-expanded='true']"));
  }
  if (!optionNodes.length && promptRoot) {
    // Workday draws the prompt menu on document.body. A selected pill on
    // another field is not that menu. Skip the portal when a different prompt
    // is the one that is open.
    const anotherOpen = Array.from(document.querySelectorAll(
      "[data-automation-id='multiSelectContainer'] [aria-expanded='true'], " +
      "[data-automation-id='multiselectInputContainer'] [aria-expanded='true']"
    )).some((node) => !promptRoot.contains(node));
    if (promptOpen(el) || !anotherOpen) {
      const portalSelector = "[data-automation-id='promptOption'], [data-automation-id='promptLeafNode']";
      for (const option of document.querySelectorAll(portalSelector)) {
        if (isSelectedItemNode(option)) continue;
        if (optionEnabled(option) && shown(option)) optionNodes.push(option);
      }
    }
  }
  if (!optionNodes.length && allowSingle) {
    // One unclaimed visible list. Two menus means we will not guess which one.
    // A selected-item list is a value, not a menu.
    const boxes = [];
    for (const candidate of document.querySelectorAll("[role='listbox']")) {
      if (claimedByOther(candidate) || !shown(candidate) || isSelectedItemNode(candidate)) continue;
      boxes.push(candidate);
    }
    if (boxes.length === 1) {
      for (const option of boxes[0].querySelectorAll(optionSelector)) {
        if (optionEnabled(option) && optionShown(option)) optionNodes.push(option);
      }
    }
  }
  if (!optionNodes.length) return [];
  const options = [];
  for (const option of optionNodes) {
    const label = textOf(option);
    if (!label) continue;
    const nested = option.querySelector(
      "[role='option'], [data-automation-id='promptOption'], [data-automation-id='promptLeafNode']"
    );
    if (nested && nested !== option && textOf(nested) === label) continue;
    if (!option.id) option.setAttribute("data-autofill-option", String(options.length));
    const itemSelector = option.id
      ? "#" + CSS.escape(option.id)
      : "[data-autofill-option='" + option.getAttribute("data-autofill-option") + "']";
    options.push({
      label: label,
      value: option.getAttribute("data-value") || label,
      selector: itemSelector,
    });
  }
  return options;
}
"""


_COMMIT_JS = r"""
({ selector, label }) => {
  const el = document.querySelector(selector);
  if (!el) return { error: true, tracksValue: true };
  function shown(node) {
    if (!node || node.hidden) return false;
    const style = window.getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden") return false;
    return true;
  }
  function fold(value) {
    return String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
  }
  const wanted = fold(label);
  function matches(value) {
    const text = fold(value);
    if (!text || !wanted) return false;
    return text === wanted || text.indexOf(wanted) !== -1 || wanted.indexOf(text) !== -1;
  }
  const field = el.closest(".field, .select, fieldset") || el.parentElement;
  const promptRoot = el.closest(
    "[data-automation-id='multiSelectContainer'], [data-automation-id='multiselectInputContainer']"
  );
  const fieldRoot = el.closest("[data-automation-id^='formField']");
  let error = el.getAttribute("aria-invalid") === "true";
  if (field) {
    const nodes = field.querySelectorAll("[role='alert'], .field-error, .error-message, .select-error, .error");
    for (const node of nodes) {
      if (!shown(node)) continue;
      const text = (node.innerText || node.textContent || "").trim();
      if (text) error = true;
    }
  }
  let hiddenOk = false;
  let tracksValue = false;
  const root = (promptRoot && fieldRoot) || promptRoot || field || el.parentElement || document.body;
  if (promptRoot) tracksValue = true;
  function hiddenControl(node) {
    if (!node || node === el) return false;
    const type = (node.getAttribute("type") || "").toLowerCase();
    if (type === "hidden") return true;
    if (node.tagName !== "SELECT") return false;
    if (node.hidden || node.getAttribute("aria-hidden") === "true") return true;
    const style = window.getComputedStyle(node);
    return style.display === "none" || style.visibility === "hidden";
  }
  for (const input of root.querySelectorAll("input, select")) {
    // A visible <select> elsewhere on the page is its own question.
    if (!hiddenControl(input)) continue;
    tracksValue = true;
    const selected = input.selectedOptions && input.selectedOptions[0];
    const selectedText = selected ? selected.textContent : "";
    if (matches(input.value) || matches(selectedText)) hiddenOk = true;
  }
  let shownOk = false;
  const single = root.querySelector(".select__single-value, [class*='singleValue']");
  if (single && matches(single.textContent)) shownOk = true;
  let ariaOk = false;
  const activeId = el.getAttribute("aria-activedescendant") || "";
  if (activeId) {
    tracksValue = true;
    const active = document.getElementById(activeId);
    if (active && matches(active.textContent)) ariaOk = true;
  }
  const pillSelector = [
    "[data-automation-id='selectedItem']",
    "[data-automation-id='selectedItemText']",
    "[data-automation-id*='selectedItem']",
  ].join(", ");
  const pills = root.querySelectorAll(pillSelector);
  if (pills.length) {
    tracksValue = true;
    for (const pill of pills) {
      if (matches(pill.textContent)) shownOk = true;
    }
  }
  const picked = root.querySelector("[role='option'][aria-selected='true']");
  if (picked) {
    tracksValue = true;
    if (matches(picked.textContent)) ariaOk = true;
  }
  let inputOk = false;
  const tag = el.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA") inputOk = fold(el.value) === wanted;
  let buttonOk = false;
  if (tag !== "INPUT" && tag !== "TEXTAREA" && tag !== "SELECT") {
    buttonOk = fold(el.innerText || el.textContent) === wanted;
  }
  return { error, hiddenOk, shownOk, ariaOk, tracksValue, inputOk, buttonOk };
}
"""


def _pause(page, millis: int) -> None:
    pause = getattr(page, "wait_for_timeout", None)
    if pause is None:
        return
    pause(millis)


def _combobox_committed(page, selector: str, label: str, *, accept_input: bool = False) -> bool:
    """True only when the widget shows the choice. A click that changes nothing is not a commit.

    The search input counts only when ``accept_input`` is set. Typing a filter
    leaves that same text in the box before a choice exists.
    """
    try:
        state = page.evaluate(_COMMIT_JS, {"selector": selector, "label": label}) or {}
    except Exception:
        return False
    if not isinstance(state, dict) or state.get("error"):
        return False
    if state.get("hiddenOk") or state.get("ariaOk") or state.get("shownOk") or state.get("buttonOk"):
        return True
    return bool(accept_input and state.get("inputOk"))


def _read_options(page, selector: str, *, allow_single: bool = False) -> list[Option]:
    try:
        raw = page.evaluate(
            _SCOPED_OPTIONS_JS,
            {"selector": selector, "allowSingle": allow_single},
        ) or []
    except Exception:
        return []
    found: list[Option] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        found.append(
            Option(
                value=str(item.get("value") or ""),
                label=str(item.get("label") or ""),
                selector=str(item.get("selector") or ""),
            )
        )
    return found


def _match_desired(desired: str, found: list[Option]) -> Option | None:
    country = match_country_option(desired, found)
    if country is not None:
        return country
    return match_option_exact(desired, found)


def _filter_terms(desired: str) -> list[str]:
    if _known_country(desired):
        # Type the country name. A bare calling code would surface every +1 row.
        return [term for term in country_aliases(desired) if not term.startswith("+")]
    return [desired]


def _await_menu(page, selector: str, timeout_ms: int) -> list[Option]:
    """Read the open menu until options appear or ``timeout_ms`` passes."""
    found = _read_options(page, selector)
    if found or timeout_ms <= 0 or getattr(page, "wait_for_timeout", None) is None:
        return found
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        _pause(page, 50)
        found = _read_options(page, selector)
        if found:
            return found
    return found


_OPENER_JS = r"""
(selector) => {
  const el = document.querySelector(selector);
  if (!el) return "";
  const widget = [
    "[data-automation-id='multiSelectContainer']",
    "[data-automation-id='multiselectInputContainer']",
    ".select",
    ".select__control",
  ].join(", ");
  const root = el.closest(widget) || el.parentElement;
  const icon = root && root.querySelector(
    "[data-automation-id='promptIcon'], [data-automation-id='promptSearchButton']"
  );
  function mark(node) {
    if (!node || node === el) return "";
    if (node.id) return "#" + CSS.escape(node.id);
    if (!node.getAttribute("data-autofill-open")) node.setAttribute("data-autofill-open", "prompt");
    return "[data-autofill-open='prompt']";
  }
  if (icon) return mark(icon);
  const control = el.closest(".select__control");
  if (control && control !== el) return mark(control);
  return "";
}
"""


def _click_fallback_opener(page, selector: str) -> bool:
    """Click the prompt icon or the closed control when the input click did not open a menu."""
    try:
        opener = str(page.evaluate(_OPENER_JS, selector) or "")
    except Exception:
        return False
    if not opener or opener == selector:
        return False
    try:
        open_combobox(page, opener)
    except Exception:
        return False
    return True


def _open_for_options(page, selector: str) -> list[Option]:
    """Open the menu and wait. An async or portaled list is not read before it exists."""
    open_combobox(page, selector)
    found = _await_menu(page, selector, 400)
    if found:
        return found
    if _click_fallback_opener(page, selector):
        return _await_menu(page, selector, 1500)
    return found


def _type_filter(page, selector: str, text: str) -> None:
    """Type into the combobox so an async menu can filter."""
    try:
        page.evaluate(_BLOCK_SUBMIT_JS)
    except Exception:
        return
    field = page.locator(selector)
    try:
        target = field
        inner = field.locator("input:not([type='hidden']), textarea")
        if inner.count():
            target = inner.first
        target.fill("")
        target.press_sequentially(text, delay=type_char_delay())
    except Exception:
        return
    _pause(page, 40)


def list_combobox_options(page, selector: str, *, limit: int = 200) -> list[str]:
    """Open the menu once, read option text, and close it.

    The prompt icon is clicked when the input click does not reveal options.
    Workday search prompts open from that icon, and the menu is ``promptOption``
    rows, not another field's selected pills.
    """
    if not selector:
        return []
    focus_field(page, selector)
    try:
        found = _open_for_options(page, selector)
    except Exception:
        found = []
    labels: list[str] = []
    for option in found:
        label = (option.label or option.value).strip()
        if not label or label in labels:
            continue
        labels.append(label[:200])
        if len(labels) >= limit:
            break
    try:
        dismiss_combobox(page)
    except Exception:
        return labels
    return labels


def _same_option(left: Option, right: Option) -> bool:
    wanted = _fold(right.label) or _fold(right.value)
    if not wanted:
        return False
    return wanted in {_fold(left.label), _fold(left.value)}


def _keyboard_commit(page, selector: str, desired: str, match: Option | None = None) -> None:
    """Type the option and confirm it.

    ArrowDown is pressed only when the first visible row is that option.
    Enter otherwise confirms a widget that commits the typed label, and does
    not highlight a different first row.
    """
    field = page.locator(selector)
    try:
        page.evaluate(_BLOCK_SUBMIT_JS)
        target = field
        inner = field.locator("input:not([type='hidden']), textarea")
        if inner.count():
            target = inner.first
        target.fill("")
        target.press_sequentially(desired, delay=type_char_delay())
        found = _await_menu(page, selector, 800)
        if match is not None and found and _same_option(found[0], match):
            target.press("ArrowDown")
        # Enter confirms the row. It must not activate the form's submit button.
        target.evaluate(
            """(el) => {
              if (el.dataset.autofillEnterHeld) return;
              el.dataset.autofillEnterHeld = "1";
              el.addEventListener("keydown", (event) => {
                if (event.key === "Enter") event.preventDefault();
              }, true);
            }"""
        )
        target.press("Enter")
    except Exception:
        return
    _pause(page, 100)


_BLOCK_SUBMIT_JS = """
() => {
  if (window.__autofillBlockSubmit) return;
  window.__autofillBlockSubmit = true;
  document.addEventListener("submit", (event) => {
    event.preventDefault();
    event.stopPropagation();
  }, true);
}
"""


def select_combobox(page, selector: str, desired: str) -> Option | None:
    """Open ``selector``, choose ``desired``, and keep it only when the widget commits.

    A click that paints the label but leaves the hidden input empty, or that
    shows a field error, is retried by typing the option and pressing Enter.
    """
    if not selector or not desired.strip():
        return None
    focus_field(page, selector)
    found = _open_for_options(page, selector)
    if not found:
        # A prompt list that is not wired with aria-controls, and is the only open menu.
        found = _read_options(page, selector, allow_single=True)
    typed = False
    match = _match_desired(desired, found)
    if match is None:
        for term in _filter_terms(desired):
            _type_filter(page, selector, term)
            typed = True
            found = _await_menu(page, selector, 1500)
            match = _match_desired(desired, found)
            if match is not None:
                break
    if match is None or not match.selector:
        dismiss_combobox(page)
        return None
    click_choice(page, match.selector, match.label)
    _pause(page, 50)
    if _combobox_committed(page, selector, match.label, accept_input=not typed):
        return match
    _keyboard_commit(page, selector, desired, match)
    if _combobox_committed(page, selector, match.label, accept_input=True):
        return match
    dismiss_combobox(page)
    return None


_LISTBOX_OPTIONS_JS = r"""() => {
  function textOf(node) {
    return (node.innerText || node.textContent || "").replace(/\s+/g, " ").trim();
  }
  function shown(node) {
    if (!node || node.hidden) return false;
    const style = window.getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 || rect.height > 0;
  }
  function junk(text) {
    const folded = String(text || "").toLowerCase().replace(/[.…]+$/g, "").trim();
    return (
      folded === "select" ||
      folded === "select one" ||
      folded === "please select" ||
      folded === "please select one" ||
      folded === "choose" ||
      folded === "choose one"
    );
  }
  function selectedPill(node) {
    if (!node || !node.closest) return false;
    return !!node.closest(
      "[data-automation-id='selectedItemList'], [data-automation-id='selectedItem'], " +
      "[data-automation-id='selectedItemText']"
    );
  }
  const found = [];
  const seen = new Set();
  for (const option of document.querySelectorAll("[role='option'], [data-automation-id='promptOption']")) {
    if (!shown(option) || selectedPill(option)) continue;
    if (option.getAttribute("aria-disabled") === "true" || option.disabled) continue;
    const label = textOf(option);
    if (!label || junk(label) || seen.has(label)) continue;
    seen.add(label);
    if (!option.id) option.setAttribute("data-autofill-option", String(found.length));
    const selector = option.id
      ? "#" + CSS.escape(option.id)
      : "[data-autofill-option='" + option.getAttribute("data-autofill-option") + "']";
    found.push({
      label: label,
      value: option.getAttribute("data-value") || label,
      selector: selector,
    });
  }
  return found;
}"""


_BUTTON_SHOWS_JS = r"""({ selector, label }) => {
  const el = document.querySelector(selector);
  if (!el) return false;
  const shown = String(el.innerText || el.textContent || "").replace(/\s+/g, " ").trim().toLowerCase();
  const wanted = String(label || "").replace(/\s+/g, " ").trim().toLowerCase();
  return !!wanted && shown === wanted;
}"""


def _options_from_raw(raw) -> list[Option]:
    found: list[Option] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        found.append(
            Option(
                value=str(item.get("value") or label),
                label=label,
                selector=str(item.get("selector") or ""),
            )
        )
    return found


def _read_open_listbox(page) -> list[Option]:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return []
    try:
        raw = evaluate(_LISTBOX_OPTIONS_JS) or []
    except Exception:
        return []
    return _options_from_raw(raw)


def _await_open_listbox(page, timeout_ms: int) -> list[Option]:
    found = _read_open_listbox(page)
    if found or timeout_ms <= 0 or getattr(page, "wait_for_timeout", None) is None:
        return found
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        _pause(page, 40)
        found = _read_open_listbox(page)
        if found:
            return found
    return found


def _close_listbox(page) -> None:
    try:
        dismiss_combobox(page)
    except Exception:
        return


def read_listbox_button_options(page, selector: str) -> list[Option]:
    """Open one Workday listbox button, read the rows that appear, and close it.

    The menu is not in the page until the button is clicked. ``Select One`` is
    not an option. A selected pill on another prompt is not this menu.
    """
    if not selector:
        return []
    try:
        open_combobox(page, selector)
    except Exception:
        return []
    found = _await_open_listbox(page, 400)
    _close_listbox(page)
    return found


def attach_listbox_options(page, control) -> None:
    """Fill ``control.options`` from the menu. A control that already has rows is left alone."""
    if control is None:
        return
    if getattr(control, "kind", "") != "buttons" or getattr(control, "role", "") != "listbox":
        return
    if getattr(control, "options", None) or not getattr(control, "selector", ""):
        return
    found = read_listbox_button_options(page, control.selector)
    if found:
        control.options = found


def _button_shows(page, selector: str, label: str) -> bool:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return False
    try:
        return bool(evaluate(_BUTTON_SHOWS_JS, {"selector": selector, "label": label}))
    except Exception:
        return False


def select_listbox_button(page, selector: str, desired: str) -> Option | None:
    """Open a Workday choice button and choose ``desired``. Does not type into the button.

    The button text must equal the option label afterwards. A click that leaves
    ``Select One`` in place is not a commit.
    """
    if not selector or not str(desired or "").strip():
        return None
    from autofill.intents import pick_option

    try:
        open_combobox(page, selector)
    except Exception:
        return None
    found = _await_open_listbox(page, 400)
    status, match = pick_option(desired, found)
    if status != "match" or match is None or not match.selector:
        _close_listbox(page)
        return None
    try:
        click_choice(page, match.selector, match.label or desired)
    except Exception:
        _close_listbox(page)
        return None
    _pause(page, 40)
    if _button_shows(page, selector, match.label or desired):
        return match
    _close_listbox(page)
    return None
