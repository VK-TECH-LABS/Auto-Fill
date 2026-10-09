"""Open one combobox and read only the listbox that belongs to it.

A phone-country list that is already on the page is not an option list for a
different question. Options that appear after the control is opened are read
from ``aria-controls`` / ``aria-owns``, or from the list inside that control.
"""

from __future__ import annotations

from autofill.intents import match_option_exact
from autofill.models import Option
from autofill.safeguards import click_choice, dismiss_combobox, open_combobox

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


def select_combobox(page, selector: str, desired: str) -> Option | None:
    """Open ``selector``, choose ``desired`` from its own list, and close on a miss."""
    if not selector or not desired.strip():
        return None
    open_combobox(page, selector)
    found: list[Option] = []
    for _ in range(10):
        raw = page.evaluate(_SCOPED_OPTIONS_JS, selector) or []
        found = [
            Option(
                value=str(item.get("value") or ""),
                label=str(item.get("label") or ""),
                selector=str(item.get("selector") or ""),
            )
            for item in raw
            if isinstance(item, dict)
        ]
        if found:
            break
        pause = getattr(page, "wait_for_timeout", None)
        if pause is None:
            break
        pause(100)
    match = match_option_exact(desired, found)
    if match is None or not match.selector:
        dismiss_combobox(page)
        return None
    click_choice(page, match.selector, match.label)
    return match
