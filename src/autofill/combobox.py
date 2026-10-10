"""Open one combobox and read only the listbox that belongs to it.

A phone-country list that is already on the page is not an option list for a
different question. Options that appear after the control is opened are read
from ``aria-controls`` / ``aria-owns``, or from the list inside that control.
"""

from __future__ import annotations

import re

from autofill.dom_ui import focus_field
from autofill.intents import match_option_exact
from autofill.models import Option
from autofill.safeguards import click_choice, dismiss_combobox, open_combobox

# A menu may show any of these for one country. Longer names are typed first.
_COUNTRY_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"united states", "united states of america", "usa", "us"}),
    frozenset({"united kingdom", "great britain", "uk", "gb"}),
)


def _fold(value: str) -> str:
    text = str(value or "").casefold().replace(".", "")
    return re.sub(r"\s+", " ", text).strip()


def country_aliases(value: str) -> list[str]:
    """Names a country menu might show for ``value``, longest first."""
    token = _fold(value)
    if not token:
        return []
    for group in _COUNTRY_GROUPS:
        if token in group:
            return sorted(group, key=len, reverse=True)
    return [token]


def _known_country(value: str) -> bool:
    token = _fold(value)
    return any(token in group for group in _COUNTRY_GROUPS)


def match_country_option(desired: str, options: list[Option]) -> Option | None:
    """Case-insensitive match across United States / USA / US and the other groups."""
    if not _known_country(desired):
        return None
    aliases = set(country_aliases(desired))
    for option in options:
        if _fold(option.label) in aliases or _fold(option.value) in aliases:
            return option
    return None


_SCOPED_OPTIONS_JS = r"""
(selector) => {
  const el = document.querySelector(selector);
  if (!el) return [];
  function textOf(node) {
    return (node.innerText || node.textContent || "").replace(/\s+/g, " ").trim();
  }
  function claimedByOther(list) {
    if (!list.id) return false;
    const query = "[aria-controls='" + CSS.escape(list.id) + "'], [aria-owns='" + CSS.escape(list.id) + "']";
    const owner = document.querySelector(query);
    return !!(owner && owner !== el);
  }
  let list = null;
  const owned = el.getAttribute("aria-controls") || el.getAttribute("aria-owns") || "";
  if (owned) {
    const node = document.getElementById(owned);
    if (node && (node.getAttribute("role") || "") === "listbox" && !claimedByOther(node)) list = node;
  }
  let container = el.closest(".select, [class*='select__'], fieldset") || el.parentElement;
  for (let depth = 0; container && depth < 4 && !list; depth += 1) {
    for (const candidate of container.querySelectorAll("[role='listbox']")) {
      if (!claimedByOther(candidate)) {
        list = candidate;
        break;
      }
    }
    container = container.parentElement;
  }
  if (!list) return [];
  const options = [];
  for (const option of list.querySelectorAll("[role='option']")) {
    const label = textOf(option);
    if (!label) continue;
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
  const root = field || el.parentElement || document.body;
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
  const picked = root.querySelector("[role='option'][aria-selected='true']");
  if (picked) {
    tracksValue = true;
    if (matches(picked.textContent)) ariaOk = true;
  }
  return { error, hiddenOk, shownOk, ariaOk, tracksValue };
}
"""


def _pause(page, millis: int) -> None:
    pause = getattr(page, "wait_for_timeout", None)
    if pause is None:
        return
    pause(millis)


def _combobox_committed(page, selector: str, label: str) -> bool:
    try:
        state = page.evaluate(_COMMIT_JS, {"selector": selector, "label": label}) or {}
    except Exception:
        return False
    if not isinstance(state, dict) or state.get("error"):
        return False
    if state.get("hiddenOk") or state.get("ariaOk") or state.get("shownOk"):
        return True
    return not bool(state.get("tracksValue"))


def _read_options(page, selector: str) -> list[Option]:
    try:
        raw = page.evaluate(_SCOPED_OPTIONS_JS, selector) or []
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
        return country_aliases(desired)
    return [desired]


def _poll_options(page, selector: str, attempts: int, delay_ms: int) -> list[Option]:
    found: list[Option] = []
    for _ in range(attempts):
        found = _read_options(page, selector)
        if found:
            return found
        if getattr(page, "wait_for_timeout", None) is None:
            break
        _pause(page, delay_ms)
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
        target.press_sequentially(text, delay=20)
    except Exception:
        return
    _pause(page, 80)


def list_combobox_options(page, selector: str, *, limit: int = 200) -> list[str]:
    """Open the menu once, read option text, and close it."""
    if not selector:
        return []
    focus_field(page, selector)
    try:
        open_combobox(page, selector)
        found = _poll_options(page, selector, 6, 80)
    except Exception:
        found = []
    labels: list[str] = []
    for option in found:
        label = (option.label or option.value).strip()
        if not label:
            continue
        labels.append(label[:200])
        if len(labels) >= limit:
            break
    try:
        dismiss_combobox(page)
    except Exception:
        return labels
    return labels


def _keyboard_commit(page, selector: str, desired: str) -> None:
    """Type the option into the combobox input and confirm with Enter."""
    field = page.locator(selector)
    try:
        page.evaluate(_BLOCK_SUBMIT_JS)
        target = field
        inner = field.locator("input:not([type='hidden']), textarea")
        if inner.count():
            target = inner.first
        target.fill("")
        target.press_sequentially(desired, delay=30)
        target.press("Enter")
    except Exception:
        return
    _pause(page, 100)


_BLOCK_SUBMIT_JS = """
() => {
  if (window.__autofillBlockSubmit) return;
  window.__autofillBlockSubmit = true;
  document.addEventListener("submit", (event) => event.preventDefault(), true);
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
    open_combobox(page, selector)
    found = _poll_options(page, selector, 4, 50)
    match = _match_desired(desired, found)
    if match is None:
        for term in _filter_terms(desired):
            _type_filter(page, selector, term)
            found = _poll_options(page, selector, 10, 80)
            match = _match_desired(desired, found)
            if match is not None:
                break
    if match is None or not match.selector:
        dismiss_combobox(page)
        return None
    click_choice(page, match.selector, match.label)
    _pause(page, 50)
    if _combobox_committed(page, selector, match.label):
        return match
    _keyboard_commit(page, selector, desired)
    if _combobox_committed(page, selector, match.label):
        return match
    dismiss_combobox(page)
    return None
