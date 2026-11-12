"""Open one combobox and read only the listbox that belongs to it.

A phone-country list that is already on the page is not an option list for a
different question. A Workday ``selectedItemList`` is the value already chosen
on that prompt, not the prompt menu. Options that appear after the control is
opened are read from ``aria-controls`` / ``aria-owns``, from the list inside
that form field, or from the open ``promptOption`` menu.
"""

from __future__ import annotations

import logging
import re
import time
from contextvars import ContextVar

from autofill.dom_ui import focus_field, latest_listbox_choices, note_listbox_choice, prompt_pill_text, type_char_delay
from autofill.intents import match_option_exact
from autofill.models import Option
from autofill.safeguards import (
    HumanSubmissionRequired,
    clear_listbox_value,
    click_choice,
    click_element,
    dismiss_combobox,
    open_combobox,
)

logger = logging.getLogger("autofill.combobox")

# Child labels seen when a click opened a nested menu instead of committing.
# The answer card reads these once and asks for the leaf. A later success clears them.
_OPENED_CHILDREN: ContextVar[tuple[str, ...]] = ContextVar("autofill_opened_children", default=())

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
# Workday writes the code in parentheses: "United States of America (+1)".
_DIAL_SUFFIX = re.compile(r"\s*(?:\(\s*)?\+\d{1,4}\s*\)?\s*$")
_DIAL_PREFIX = re.compile(r"^(?:\(\s*)?\+\d{1,4}\s*\)?\s+")


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
  // An open prompt whose next level is a plain listbox (role=option rows,
  // not promptLeafNode) is still this control's menu. Two menus are not guessed.
  if (!optionNodes.length && (allowSingle || promptOpen(el))) {
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
    const foldedLabel = label.toLowerCase().replace(/[.…]+$/g, "").trim();
    if (foldedLabel === "no items" || foldedLabel === "no matches" || foldedLabel === "no results") continue;
    const nested = option.querySelector(
      "[role='option'], [data-automation-id='promptOption'], [data-automation-id='promptLeafNode']"
    );
    if (nested && nested !== option && textOf(nested) === label) continue;
    if (!option.id) option.setAttribute("data-autofill-option", String(options.length));
    const itemSelector = option.id
      ? "#" + CSS.escape(option.id)
      : "[data-autofill-option='" + option.getAttribute("data-autofill-option") + "']";
    const auto = String(option.getAttribute("data-automation-id") || "").toLowerCase();
    // A category can be marked promptLeafNode and still only open the next
    // list. The folder cues are the chevron, side charm, and multiselect type 2.
    // A flat promptOption with none of those commits on click.
    function folderCue(node) {
      if (!node || !node.getAttribute) return false;
      if (node.getAttribute("data-autofill-folder") === "true") return true;
      if ((node.getAttribute("data-uxi-multiselectlistitem-type") || "") === "2") return true;
      const attrs = node.attributes ? Array.from(node.attributes) : [];
      for (const attr of attrs) {
        const name = String(attr.name || "").toLowerCase();
        const value = String(attr.value || "").toLowerCase();
        if (name.indexOf("sidecharm") !== -1 && (value === "true" || value === "1")) return true;
      }
      return false;
    }
    let folder = folderCue(option);
    if (!folder) {
      const marks = option.querySelectorAll(
        "[data-automation-id], [class], svg, [data-uxi-multiselectlistitem-type], [data-uxi-hassidecharm]"
      );
      for (const mark of marks) {
        if (folderCue(mark)) {
          folder = true;
          break;
        }
        const markAuto = String(mark.getAttribute("data-automation-id") || "").toLowerCase();
        const cls = String(mark.getAttribute("class") || "").toLowerCase();
        if (markAuto.indexOf("chevron") !== -1 || cls.indexOf("chevron") !== -1) {
          folder = true;
          break;
        }
      }
    }
    // A calling-code pill from Country Phone Code is not a source option.
    // The phone field's own menu still lists those rows.
    function phoneCodeField(node) {
      const field = node && node.closest ? node.closest("[data-automation-id^='formField']") : null;
      const ident = (
        (field ? field.getAttribute("data-automation-id") || "" : "") + " " +
        (node.id || "") + " " +
        (node.getAttribute("name") || "")
      ).toLowerCase();
      return ident.indexOf("phonecode") !== -1 || ident.indexOf("countrycode") !== -1
        || (ident.indexOf("phone") !== -1 && ident.indexOf("code") !== -1);
    }
    function besideNationalPhone(node) {
      // The only menu in a small group with one phone input is the calling code.
      // A second prompt on the same form is not, so its menu still drops (+1) pills.
      let cursor = node;
      for (let depth = 0; cursor && depth < 5; depth += 1, cursor = cursor.parentElement) {
        const tag = cursor.tagName ? cursor.tagName.toLowerCase() : "";
        if (tag === "form" || tag === "body" || tag === "html") break;
        const tels = cursor.querySelectorAll(
          "input[type='tel'], input[autocomplete='tel'], input[autocomplete='tel-national']"
        );
        if (!tels.length) continue;
        const menus = cursor.querySelectorAll("select, [role='combobox']");
        if (tels.length === 1 && menus.length === 1) return true;
        if (menus.length > 1) break;
      }
      return false;
    }
    if (!phoneCodeField(el) && !besideNationalPhone(el) && /\(\+\d+\)/.test(label)) continue;
    const leaf = !folder && (
      auto === "promptleafnode"
      || !!option.querySelector("[data-automation-id='promptLeafNode']")
      || auto !== "promptoption"
    );
    options.push({
      label: label,
      value: option.getAttribute("data-value") || label,
      selector: itemSelector,
      leaf: leaf,
      folder: folder,
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
  function promptValueRoot(node) {
    // The search box lives in multiselectInputContainer. The selected pill is
    // a sibling of that box, inside the outer multiSelectContainer.
    if (!node || !node.closest) return null;
    const outer = node.closest("[data-automation-id='multiSelectContainer']");
    if (outer) return outer;
    return node.closest("[data-automation-id='multiselectInputContainer']");
  }
  const promptRoot = promptValueRoot(el);
  const fieldRoot = el.closest("[data-automation-id^='formField']");
  // The widget root holds the hidden value and this menu's error. A shared
  // fieldset is not that root, so a sibling question's error is not this one.
  const field = promptRoot || fieldRoot || el.closest(".field, .select") || el.parentElement;
  let error = el.getAttribute("aria-invalid") === "true";
  // A leftover "required" flag is stale once the value is visible. A format
  // error, or any other message, still blocks the commit.
  let blocking = false;
  if (field) {
    const nodes = field.querySelectorAll("[role='alert'], .field-error, .error-message, .select-error, .error");
    for (const node of nodes) {
      if (!shown(node)) continue;
      const text = (node.innerText || node.textContent || "").replace(/\s+/g, " ").trim();
      if (!text) continue;
      error = true;
      const folded = text.toLowerCase();
      const stale = folded.indexOf("required") !== -1 || folded.indexOf("must have a value") !== -1;
      const format = folded.indexOf("invalid phone") !== -1
        || folded.indexOf("valid format") !== -1
        || folded.indexOf("valid date") !== -1;
      if (format || !stale) blocking = true;
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
  let pillOk = false;
  const single = root.querySelector(".select__single-value, [class*='singleValue']");
  if (single && matches(single.textContent)) shownOk = true;
  let ariaOk = false;
  // An open menu's highlighted row is not the value on the employer field.
  // aria-activedescendant and aria-selected paint that highlight. The commit
  // is a pill, a hidden input, or the closed control.
  // A menu is open only while it paints a row. A stale aria-expanded or an
  // emptied list after the commit is a closed menu.
  function rowsShown(scope) {
    const rows = scope.querySelectorAll("[role='option'], [data-automation-id='promptOption']");
    for (const row of rows) {
      if (!shown(row)) continue;
      if (row.closest("[data-automation-id='selectedItem'], [data-automation-id='selectedItemList']")) continue;
      const box = row.getBoundingClientRect();
      if (box.width > 0 && box.height > 0) return true;
    }
    return false;
  }
  function menuOpen(node) {
    if (!node || !node.getAttribute) return false;
    if (node.getAttribute("aria-expanded") === "true" && rowsShown(document)) return true;
    const listId = node.getAttribute("aria-controls") || node.getAttribute("aria-owns") || "";
    if (listId) {
      const list = document.getElementById(listId);
      if (list && shown(list) && rowsShown(list)) return true;
    }
    const activeList = document.querySelector("[data-automation-id='activeListContainer']");
    if (activeList && shown(activeList) && rowsShown(activeList)) return true;
    // A highlighted promptOption is still the open menu. It is not a pill.
    const rows = document.querySelectorAll("[data-automation-id='promptOption']");
    for (const row of rows) {
      if (!shown(row)) continue;
      if (row.closest("[data-automation-id='selectedItem'], [data-automation-id='selectedItemList']")) continue;
      return true;
    }
    return false;
  }
  const openMenu = menuOpen(el);
  const activeId = el.getAttribute("aria-activedescendant") || "";
  if (!openMenu && activeId) {
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
      if (matches(pill.textContent)) {
        shownOk = true;
        pillOk = true;
      }
    }
  }
  const picked = root.querySelector("[role='option'][aria-selected='true']");
  if (!openMenu && picked) {
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
  return { error, blocking, hiddenOk, shownOk, pillOk, ariaOk, tracksValue, inputOk, buttonOk, openMenu };
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
    if not isinstance(state, dict):
        return False
    # While the menu is open, a highlight or a typed search is not a committed
    # leaf. A written hidden value, a pill, or a single-value chip still is:
    # some widgets leave the list painted after a real commit. The input text
    # counts only when nothing was typed into it (``accept_input``).
    if state.get("openMenu"):
        written = (
            state.get("pillOk")
            or state.get("hiddenOk")
            or state.get("shownOk")
            or (accept_input and state.get("inputOk"))
        )
        return bool(written and not state.get("blocking"))
    # A visible choice is the commit. The search box can stay aria-invalid,
    # and a stale "required" message can stay, after that choice is painted.
    # A format error or any other field message is not a commit.
    visible = (
        state.get("pillOk")
        or state.get("hiddenOk")
        or state.get("ariaOk")
        or state.get("shownOk")
        or state.get("buttonOk")
    )
    if visible and not state.get("blocking"):
        return True
    if state.get("blocking") or state.get("error"):
        return False
    return bool(accept_input and state.get("inputOk"))


_CLEAR_SATISFIED_JS = r"""(selector) => {
  let el = null;
  try { el = document.querySelector(selector); } catch (error) { return false; }
  if (!el) return false;
  function clean(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }
  function placeholder(text) {
    const folded = clean(text).toLowerCase().replace(/[.…]+$/g, "").trim();
    if (!folded) return true;
    return /^(select one|select|choose|choose one|please select|please select one|search)$/.test(folded);
  }
  const tag = String(el.tagName || "");
  let shown = "";
  if (tag === "SELECT") {
    const option = el.options && el.selectedIndex >= 0 ? el.options[el.selectedIndex] : null;
    shown = clean(option ? (option.text || option.label || el.value) : el.value);
  } else if (tag === "INPUT" || tag === "TEXTAREA") {
    shown = clean(el.value);
  } else {
    shown = clean(el.innerText || el.textContent);
  }
  if (!shown || placeholder(shown)) return false;
  const field = (el.closest && (
    el.closest("[data-automation-id^='formField']") ||
    el.closest(".field, .form-group, .jv-form-field")
  )) || el.parentElement;
  let formatError = false;
  if (field && field.querySelectorAll) {
    const nodes = field.querySelectorAll(
      "[data-automation-id='errorMessage'], [data-automation-id='errorWidget'], " +
      "[role='alert'], .field-error, .error-message"
    );
    for (const node of nodes) {
      if (node === el || (node.contains && node.contains(el))) continue;
      const text = clean(node.innerText || node.textContent).toLowerCase();
      if (!text) continue;
      if (
        text.indexOf("invalid phone") !== -1 ||
        text.indexOf("valid format") !== -1 ||
        text.indexOf("valid date") !== -1
      ) {
        formatError = true;
        continue;
      }
      if (text.indexOf("required") === -1 && text.indexOf("must have a value") === -1) continue;
      node.remove();
    }
  }
  if (!formatError) el.removeAttribute("aria-invalid");
  return true;
}"""


def clear_satisfied_invalid(page, selector: str) -> None:
    """Drop a stale required flag once this control shows a real value.

    A format error stays, so a phone or date retry can still see it. An empty
    control and a placeholder such as Select One are left invalid.
    """
    if not selector:
        return
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return
    try:
        evaluate(_CLEAR_SATISFIED_JS, selector)
    except Exception:
        return


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
                leaf=bool(item.get("leaf", True)),
                folder=bool(item.get("folder", False)),
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


def _await_replaced(page, selector: str, before: list[Option], timeout_ms: int) -> list[Option]:
    """Read until the menu is a different list, or ``timeout_ms`` passes.

    A parent click leaves the old rows on screen until the children load.
    Returning that first list marks the parent as a leaf and never commits
    the child.
    """
    found = _read_options(page, selector)
    if _menu_replaced(before, found):
        return found
    if timeout_ms <= 0 or getattr(page, "wait_for_timeout", None) is None:
        return found
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        _pause(page, 50)
        found = _read_options(page, selector)
        if _menu_replaced(before, found):
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
  const root = el.closest("[data-automation-id='multiSelectContainer']")
    || el.closest(widget)
    || el.parentElement;
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


def _type_filter(page, selector: str, text: str, *, school: bool = False) -> None:
    """Type into the combobox so an async menu can filter.

    A school search ignores a value assignment. The query is real key events,
    and the menu is given time to replace ``No Items.``
    """
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
        if school:
            target.focus()
            target.press("Control+A")
            target.press("Backspace")
        else:
            target.fill("")
        target.press_sequentially(text, delay=type_char_delay())
    except Exception:
        return
    _pause(page, 200 if school else 40)


def _menu_junk(label: str) -> bool:
    folded = re.sub(r"[.…]+$", "", (label or "").strip().casefold()).strip()
    return folded in {"no items", "no matches", "no results", "select one", "select", "choose"}


def _remember_label(labels: list[str], label: str, limit: int) -> None:
    text = (label or "").strip()
    if not text or text in labels or _menu_junk(text) or len(labels) >= limit:
        return
    labels.append(text[:200])


def _path_parts(desired: str) -> list[str]:
    """``Career Websites > Corporate Website`` and the same path with / or →."""
    parts = [part.strip() for part in re.split(r"\s*(?:>|→|/)\s*", desired or "") if part.strip()]
    if len(parts) < 2:
        return []
    return parts


_MENU_OPEN_JS = r"""() => {
  function shown(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden" || node.hidden) return false;
    const box = node.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  }
  // A selected-items pill list (phone code, a chosen leaf) is role=listbox too. It is not a menu.
  function pillList(node) {
    return !!(node.closest && node.closest(
      "[data-automation-id='selectedItemList'], [data-automation-id*='selectedItem']"
    ));
  }
  const options = document.querySelectorAll("[data-automation-id='promptOption'], [role='listbox']");
  for (const node of options) {
    if (!pillList(node) && shown(node)) return true;
  }
  // A stale aria-expanded with no painted row is a closed menu.
  const expanded = document.querySelector("[aria-expanded='true']");
  if (!expanded || !shown(expanded)) return false;
  for (const row of document.querySelectorAll("[role='option']")) {
    if (shown(row)) return true;
  }
  return false;
}"""


def _release_menu(page, selector: str) -> None:
    """Close a menu that was not committed and leave the field so the next open starts at the top."""
    close_open_menu(page)
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None or not selector:
        return
    try:
        evaluate(
            """(selector) => {
              let el = null;
              try { el = document.querySelector(selector); } catch (error) { return; }
              if (el && el.blur) el.blur();
            }""",
            selector,
        )
    except Exception:
        return


def close_open_menu(page) -> None:
    """Close a prompt a category click left open.

    One Escape only steps back from a nested leaf to the parent menu. Repeat
    until the menu is gone. Once it is closed, Escape is not pressed again, so
    a committed value is not cleared.
    """
    evaluate = getattr(page, "evaluate", None)
    for _ in range(3):
        open_menu = False
        if evaluate is not None:
            try:
                open_menu = bool(evaluate(_MENU_OPEN_JS))
            except Exception:
                open_menu = False
        if not open_menu:
            return
        try:
            dismiss_combobox(page)
        except Exception:
            return
        _pause(page, 40)


def take_opened_children() -> list[str]:
    """Leaf labels from a click that opened a nested menu, then clear them."""
    found = [label for label in _OPENED_CHILDREN.get() if label]
    _OPENED_CHILDREN.set(())
    return found


def _note_children(parent: str, children: list[Option]) -> None:
    """Remember ``Parent > Child`` labels so the card can ask for the leaf.

    A replaced menu's rows are leaves even when Workday marks them
    ``promptOption`` with no leaf flag. A folder row is not a choice.
    """
    labels: list[str] = []
    parent_name = (parent or "").strip()
    for child in _real_leaves(children):
        name = (child.label or child.value).strip()
        if not name or _menu_junk(name):
            continue
        if parent_name and not _labels_match(name, parent_name):
            path = f"{parent_name} > {name}"
            if path not in labels:
                labels.append(path)
        if name not in labels:
            labels.append(name)
    if labels:
        _OPENED_CHILDREN.set(tuple(labels))


def child_for_answer(desired: str, parent: str, children: list[Option]) -> Option | None:
    """The leaf ``desired`` names under this parent.

    A parent name with several children is not a commit. A different parent,
    including whichever row is first, is not used.
    """
    parts = _path_parts(desired)
    if parts and not _labels_match(parts[0], parent):
        return None
    leaf_name = parts[-1] if parts else desired
    leaves = _real_leaves(children)
    if not parts and _labels_match(leaf_name, parent):
        if len(leaves) == 1:
            return leaves[0]
        return None
    return _prefer_leaf(leaf_name, leaves)


def terminal_menu_labels(
    found: list[Option],
    children_by_parent: list[tuple[str, list[Option]]],
    *,
    limit: int = 200,
) -> list[str]:
    """Choices a person can save. A folder that opened leaves is not one of them."""
    labels: list[str] = []
    opened = {
        parent
        for parent, kids in children_by_parent
        if any(child.leaf and not child.folder and (child.label or child.value).strip() for child in kids)
    }
    for option in found:
        label = (option.label or option.value).strip()
        if option.folder and label in opened:
            continue
        _remember_label(labels, label, limit)
    for parent, kids in children_by_parent:
        for child in kids:
            if not child.leaf or child.folder:
                continue
            name = (child.label or child.value).strip()
            if not name:
                continue
            _remember_label(labels, name, limit)
            if parent:
                _remember_label(labels, f"{parent} > {name}", limit)
    return labels


def _mark_terminal(option: Option) -> Option:
    return Option(
        value=option.value,
        label=option.label,
        selector=option.selector,
        leaf=True,
        folder=False,
    )


def _terminals_from(children: list[Option]) -> list[Option]:
    return [
        _mark_terminal(child)
        for child in children
        if child.selector and not child.folder and not _menu_junk(child.label or child.value)
    ]


def _undo_probe(page, selector: str, pill_before: str) -> None:
    """Drop a highlight or pill a probe click left behind. The field stays unchosen."""
    try:
        dismiss_combobox(page)
    except Exception:
        pass
    _pause(page, 40)
    shown = _selected_text(page, selector)
    if not shown or _labels_match(shown, pill_before):
        return
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return
    try:
        evaluate(
            """(selector) => {
              let el = null;
              try { el = document.querySelector(selector); } catch (error) { return false; }
              if (!el) return false;
              const root = el.closest(
                "[data-automation-id='multiSelectContainer'], " +
                "[data-automation-id='multiselectInputContainer'], " +
                "[data-automation-id^='formField']"
              ) || el.parentElement;
              if (!root) return false;
              const pill = root.querySelector(
                "[data-automation-id='selectedItem'], [data-automation-id='selectedItemText']"
              );
              if (!pill) return false;
              const del = pill.querySelector(
                "[data-automation-id='DELETE_charm'], button, [role='button']"
              );
              if (del) {
                const press = del["click"];
                if (press) press.call(del);
                return true;
              }
              pill.textContent = "";
              pill.hidden = true;
              return true;
            }""",
            selector,
        )
    except Exception:
        return


def _probe_row(page, selector: str, option: Option, before: list[Option]) -> tuple[str, list[Option]]:
    """Click one row. ``folder`` when the menu is replaced. A leaf click is undone."""
    if not option.selector:
        return "leaf", []
    pill_before = _selected_text(page, selector)
    try:
        click_choice(page, option.selector, option.label)
    except Exception:
        return "leaf", []
    _pause(page, 50)
    after = _await_replaced(page, selector, before, 800)
    if not after:
        after = _read_options(page, selector, allow_single=True)
    if _menu_replaced(before, after):
        return "folder", after
    _undo_probe(page, selector, pill_before)
    return "leaf", []


def _collapse_child_level(page, selector: str, kids: list[Option], budget: list[int]) -> list[Option]:
    """Leaves under this menu. One sample click decides whether the level opens again."""
    deeper = [child for child in kids if child.selector and (child.folder or not child.leaf)]
    if not deeper or budget[0] <= 0:
        return _terminals_from(kids)
    kind, grand = _probe_row(page, selector, deeper[0], kids)
    budget[0] -= 1
    if kind != "folder":
        return _terminals_from(kids)
    collected: list[Option] = []

    def adopt(mid: str, grands: list[Option]) -> None:
        mid_name = (mid or "").strip()
        for leaf in _terminals_from(grands):
            name = (leaf.label or leaf.value).strip()
            label = f"{mid_name} > {name}" if mid_name and name and not _labels_match(name, mid_name) else name
            if not label:
                continue
            collected.append(Option(value=label, label=label, selector=leaf.selector, leaf=True, folder=False))

    adopt(deeper[0].label, grand)
    try:
        dismiss_combobox(page)
    except Exception:
        pass
    _pause(page, 40)
    for other in deeper[1:]:
        if budget[0] <= 0:
            break
        opened = _await_menu(page, selector, 400) or kids
        current = _match_desired(other.label, opened) or other
        kind, grand = _probe_row(page, selector, current, opened)
        budget[0] -= 1
        if kind == "folder":
            adopt(current.label or other.label, grand)
            try:
                dismiss_combobox(page)
            except Exception:
                pass
            _pause(page, 40)
        else:
            collected.append(_mark_terminal(other))
    return collected


def list_combobox_options(page, selector: str, *, limit: int = 200) -> list[str]:
    """Open the menu, read option text, and close it.

    The prompt icon is clicked when the input click does not reveal options.
    Workday search prompts open from that icon, and the menu is ``promptOption``
    rows, not another field's selected pills. A category, including one with no
    chevron, is opened so the card lists ``Category > Leaf``. A probe click
    that only highlights a row is undone. The first row is not left selected.
    """
    if not selector:
        return []
    focus_field(page, selector)
    pill_before = _selected_text(page, selector)
    try:
        found = _open_for_options(page, selector)
    except Exception:
        found = []
    if not found:
        try:
            found = _read_options(page, selector, allow_single=True)
        except Exception:
            found = []
    if not _needs_drill(found):
        try:
            close_open_menu(page)
        except Exception:
            pass
        return terminal_menu_labels(found, [], limit=limit)
    rows = [option for option in found if option.selector and (option.folder or not option.leaf)]
    if not rows:
        rows = [option for option in found if option.selector]
    nested: list[tuple[str, list[Option]]] = []
    budget = [24]
    for index, row in enumerate(rows):
        if budget[0] <= 0 or sum(len(kids) for _parent, kids in nested) >= limit:
            break
        try:
            opened = _open_for_options(page, selector)
        except Exception:
            opened = []
        current = _match_desired(row.label, opened) or row
        if not current.selector:
            continue
        before = opened or found
        kind, kids = _probe_row(page, selector, current, before)
        budget[0] -= 1
        parent = (current.label or row.label).strip()
        if kind != "folder":
            if index == 0:
                for option in found:
                    if not option.folder:
                        option.leaf = True
                try:
                    close_open_menu(page)
                except Exception:
                    pass
                _undo_probe(page, selector, pill_before)
                return terminal_menu_labels(found, [], limit=limit)
            row.leaf = True
            row.folder = False
            continue
        leaves = _collapse_child_level(page, selector, kids, budget)
        if leaves:
            nested.append((parent, leaves))
            row.folder = True
        try:
            dismiss_combobox(page)
        except Exception:
            pass
        _pause(page, 40)
    for option in found:
        if any(_labels_match(option.label, parent) for parent, _kids in nested if parent):
            option.folder = True
    try:
        close_open_menu(page)
    except Exception:
        labels = terminal_menu_labels(found, nested, limit=limit)
        _undo_probe(page, selector, pill_before)
        return labels
    _undo_probe(page, selector, pill_before)
    return terminal_menu_labels(found, nested, limit=limit)


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


def _labels_match(desired: str, shown: str) -> bool:
    """True when the pill is the requested choice, including a country alias."""
    left, right = _fold(desired), _fold(shown)
    if not left or not right:
        return False
    if left == right:
        return True
    if match_country_option(desired, [Option(value=shown, label=shown)]) is not None:
        return True
    return match_country_option(shown, [Option(value=desired, label=desired)]) is not None


def selected_pill_text(page, selector: str) -> str:
    """The prompt pill Workday is showing. Empty when the prompt is still blank."""
    return prompt_pill_text(page, selector)


def _selected_text(page, selector: str) -> str:
    return selected_pill_text(page, selector)


def _prefer_leaf(desired: str, found: list[Option]) -> Option | None:
    """A nested leaf wins over the category that has the same name."""
    usable = [option for option in found if not _menu_junk(option.label or option.value)]
    leaves = [option for option in usable if option.leaf]
    leaf = _match_desired(desired, leaves) if leaves else None
    if leaf is not None:
        return leaf
    return _match_desired(desired, usable)


def _other_leaf(desired: str, current: Option, found: list[Option]) -> Option | None:
    leaves = [
        option
        for option in found
        if option.leaf and option.selector and option.selector != current.selector
    ]
    if not leaves:
        return None
    return _prefer_leaf(desired, leaves) or _prefer_leaf(current.label, leaves)


def _has_pill_slot(page, selector: str) -> bool:
    """True when the widget commits with a selected-item pill, not the search text."""
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None or not selector:
        return False
    script = r"""(selector) => {
      let el = null;
      try { el = document.querySelector(selector); } catch (error) { return false; }
      if (!el) return false;
      const root = el.closest(
        "[data-automation-id='multiSelectContainer'], [data-automation-id^='formField']"
      ) || el.parentElement;
      if (!root) return false;
      return !!root.querySelector(
        "[data-automation-id='selectedItem'], [data-automation-id='selectedItemText'], " +
        "[data-automation-id='selectedItemList']"
      );
    }"""
    try:
        return bool(evaluate(script, selector))
    except Exception:
        return False


def _menu_is_open(page) -> bool:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return False
    try:
        return bool(evaluate(_MENU_OPEN_JS))
    except Exception:
        return False


def _committed_choice(page, selector: str, match: Option, desired: str, *, accept_input: bool) -> bool:
    if _combobox_committed(page, selector, match.label, accept_input=accept_input):
        return True
    if _combobox_committed(page, selector, desired, accept_input=accept_input):
        return True
    # The shown text of an open menu can be a highlight. Only the written
    # evidence above counts until the menu closes.
    if _menu_is_open(page):
        return False
    shown = _selected_text(page, selector)
    leaf = _path_parts(desired)[-1] if _path_parts(desired) else desired
    # The parent category on a closed control is not the leaf.
    if match.folder and _labels_match(match.label, shown) and not _labels_match(leaf, shown):
        return False
    return _labels_match(leaf, shown) or _labels_match(match.label, shown)


def _kept_after_close(page, selector: str, match: Option, desired: str, *, accept_input: bool) -> bool:
    """The choice is on the field after the menu closes.

    Escape drops a highlight that was never written. A pill or closed value
    that is still there is the commit. An open menu is not.
    """
    try:
        close_open_menu(page)
    except Exception:
        pass
    _pause(page, 40)
    if _commit_held(page, selector, match, desired, accept_input=accept_input):
        clear_satisfied_invalid(page, selector)
        return True
    return False


def _commit_held(page, selector: str, match: Option, desired: str, *, accept_input: bool) -> bool:
    """True once the widget still shows the choice. A late pill is a commit.

    The first read is immediate. A menu that paints the choice on the next
    frame is waited on. A search string left in the box is not a commit
    unless ``accept_input`` is set.
    """
    for _attempt in range(8):
        if _committed_choice(page, selector, match, desired, accept_input=accept_input):
            return True
        _pause(page, 40)
    return False


_SCHOOL_FIELD_JS = r"""(selector) => {
  const el = document.querySelector(selector);
  if (!el) return false;
  const field = el.closest("[data-automation-id^='formField']");
  const ident = [
    el.id || "",
    el.getAttribute("name") || "",
    field ? (field.getAttribute("data-automation-id") || "") : "",
  ].join(" ").toLowerCase();
  if (ident.indexOf("school") !== -1 || ident.indexOf("university") !== -1) return true;
  let label = "";
  if (el.id) {
    const lab = document.querySelector("label[for='" + CSS.escape(el.id) + "']");
    if (lab) label = lab.innerText || lab.textContent || "";
  }
  if (!label && field) {
    const lab = field.querySelector("label, legend");
    if (lab && !lab.contains(el)) label = lab.innerText || lab.textContent || "";
  }
  const folded = String(label || "").toLowerCase();
  return folded.indexOf("school") !== -1 || folded.indexOf("university") !== -1 || folded.indexOf("college") !== -1;
}"""


_SCHOOL_HINT_JS = r"""() => {
  const text = ((document.body && document.body.innerText) || "").slice(0, 8000).toLowerCase();
  return text.indexOf("school not found") !== -1;
}"""


def _is_school_field(page, selector: str) -> bool:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return False
    try:
        return bool(evaluate(_SCHOOL_FIELD_JS, selector))
    except Exception:
        return False


def _real_leaves(found: list[Option]) -> list[Option]:
    """Rows that can be committed.

    A Workday ``promptOption`` often has no ``promptLeafNode`` and no chevron,
    so it is neither flagged leaf nor folder. After a parent click those rows
    are the next level. A row that is flagged as a folder is not a choice.
    """
    usable = [
        option
        for option in found
        if option.selector and not option.folder and not _menu_junk(option.label or option.value)
    ]
    proven = [option for option in usable if option.leaf]
    return proven or usable


def _needs_drill(options: list[Option]) -> bool:
    """True when the open menu is categories, or every row is an unproven prompt."""
    usable = [
        option
        for option in options
        if (option.label or option.value).strip() and not _menu_junk(option.label or option.value)
    ]
    if not usable:
        return False
    if any(option.folder for option in usable):
        return True
    return not any(option.leaf and not option.folder for option in usable)


def _follow_path(page, selector: str, parts: list[str], found: list[Option]) -> Option | None:
    """Click each segment of ``Parent > Child > Leaf``. The last click is the commit."""
    menu = list(found)
    clicked: Option | None = None
    for index, name in enumerate(parts):
        match = _match_desired(name, menu)
        if match is None or not match.selector:
            return None
        last = index == len(parts) - 1
        before = list(menu)
        try:
            if last:
                _choose_listed(page, match.selector, match.label)
            else:
                click_choice(page, match.selector, match.label)
        except Exception:
            return None
        _pause(page, 50)
        clicked = match
        if last:
            return clicked
        after = _await_replaced(page, selector, before, 800)
        if not _menu_replaced(before, after):
            return None
        menu = after
    return clicked


def _drill_for_leaf(page, selector: str, desired: str, found: list[Option]) -> Option | None:
    """Open a category and choose the leaf, or the only leaf it contains.

    ``Career Websites > Corporate Website`` follows that path. A leaf name
    searches each category. A category with one leaf selects that leaf.
    A prompt row with no chevron is still drilled when the menu has no leaves.
    """
    parts = _path_parts(desired)
    if len(parts) > 2:
        return _follow_path(page, selector, parts, found)
    folders = [option for option in found if option.folder and option.selector]
    if not folders:
        folders = [option for option in found if option.selector and not option.leaf]
    if not folders:
        return None
    category_name = parts[0] if parts else ""
    leaf_name = parts[-1] if parts else desired
    ordered = folders
    if category_name:
        named = [option for option in folders if _labels_match(category_name, option.label)]
        if named:
            ordered = named
    for folder in ordered:
        try:
            opened = _open_for_options(page, selector)
        except Exception:
            opened = []
        current = _match_desired(folder.label, opened) or folder
        if not current.selector:
            continue
        try:
            click_choice(page, current.selector, current.label)
        except Exception:
            continue
        _pause(page, 50)
        children = _await_replaced(page, selector, opened or found, 800)
        parent_label = (current.label or folder.label).strip()
        leaf = child_for_answer(desired, parent_label, children)
        if leaf is None and leaf_name and not _labels_match(leaf_name, parent_label):
            # The next level is a search box. The leaf is not in the DOM
            # until the query is typed.
            _type_filter(page, selector, leaf_name)
            searched = _await_menu(page, selector, 1000)
            leaf = child_for_answer(desired, parent_label, searched)
            if leaf is None:
                children = searched
        if leaf is None or not leaf.selector:
            _note_children(parent_label, children)
            try:
                dismiss_combobox(page)
            except Exception:
                return None
            continue
        try:
            _choose_listed(page, leaf.selector, leaf.label)
        except Exception:
            continue
        _pause(page, 50)
        shown = _selected_text(page, selector)
        if shown and _labels_match(leaf.label, shown):
            return leaf
        # Workday paints the pill a few frames after the leaf click. Wait for
        # it before treating the leaf as lost. A pill widget ignores the search
        # text; a plain input that kept the leaf after the menu closed is committed.
        if _commit_held(
            page,
            selector,
            leaf,
            leaf.label,
            accept_input=not _has_pill_slot(page, selector),
        ):
            return leaf
        continue
    return None


def _school_not_found(page, selector: str) -> Option | None:
    """Choose School Not Found when the page tells the candidate to."""
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return None
    try:
        instructed = bool(evaluate(_SCHOOL_HINT_JS))
    except Exception:
        return None
    if not instructed:
        return None
    found = _read_options(page, selector)
    if not any(_labels_match("School Not Found", option.label) for option in found):
        try:
            found = _open_for_options(page, selector)
        except Exception:
            found = []
    match = _match_desired("School Not Found", found)
    if match is None or not match.selector:
        return None
    try:
        click_choice(page, match.selector, match.label)
    except Exception:
        return None
    _pause(page, 50)
    return match


def _best_school_match(query: str, found: list[Option]) -> Option | None:
    """The one school row ``query`` names. ``harvard`` matches Harvard University."""
    usable = [
        option
        for option in found
        if option.selector
        and not _menu_junk(option.label or option.value)
        and not _school_fallback_label(option.label or option.value)
    ]
    wanted = _fold(query)
    if not wanted or not usable:
        return None
    exact = [option for option in usable if _fold(option.label) == wanted or _fold(option.value) == wanted]
    if len(exact) == 1:
        return exact[0]
    first = [option for option in usable if (_fold(option.label).split(" ")[:1] == [wanted])]
    if len(first) == 1:
        return first[0]
    prefix = [option for option in usable if _fold(option.label).startswith(wanted)]
    if len(prefix) == 1:
        return prefix[0]
    contains = [option for option in usable if wanted in _fold(option.label)]
    if len(contains) == 1:
        return contains[0]
    return None


def _school_fallback_label(label: str) -> bool:
    text = _fold(label)
    return text in {"school not found", "other", "school not listed", "not listed"} or "school not found" in text


def _school_fallback(found: list[Option]) -> Option | None:
    """School Not Found, then Other, when the search has no school."""
    ranked = ("school not found", "other", "school not listed", "not listed")
    usable = [option for option in found if option.selector and not _menu_junk(option.label or option.value)]
    for name in ranked:
        for option in usable:
            if name in _fold(option.label) or name in _fold(option.value):
                return option
    return None


def _press_enter(page, selector: str) -> None:
    """Confirm a school query. Enter must not submit the application."""
    try:
        page.evaluate(_BLOCK_SUBMIT_JS)
    except Exception:
        pass
    field = page.locator(selector)
    try:
        target = field
        inner = field.locator("input:not([type='hidden']), textarea")
        if inner.count():
            target = inner.first
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
    _pause(page, 50)


def is_school_target(page, selector: str, target: dict | None = None) -> bool:
    """True for a school prompt, including one reported as an empty single select."""
    info = target or {}
    key = str(info.get("fieldKey") or "").casefold()
    if key in {"school", "education.school"} or key.endswith(".school"):
        return True
    if _is_school_field(page, selector):
        return True
    answer_type = str(info.get("answerType") or "").casefold()
    text = str(info.get("text") or info.get("label") or "").casefold()
    if answer_type == "single_select" and any(word in text for word in ("school", "university", "college")):
        if "high school" not in text and "diploma" not in text and "degree" not in text:
            return True
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None or not selector:
        return False
    script = r"""(selector) => {
      let el = null;
      try { el = document.querySelector(selector); } catch (error) { return false; }
      if (!el) return false;
      const field = el.closest("[data-automation-id^='formField']");
      const ident = [
        el.id || "",
        el.getAttribute("name") || "",
        el.getAttribute("aria-label") || "",
        field ? (field.getAttribute("data-automation-id") || "") : "",
      ].join(" ").toLowerCase();
      return ident.indexOf("school") !== -1 || ident.indexOf("university") !== -1;
    }"""
    try:
        return bool(evaluate(script, selector))
    except Exception:
        return False


def search_school(page, selector: str, desired: str) -> Option | None:
    """Type the school, press Enter, and choose the prompt row.

    ``harvard`` selects Harvard University. No match selects School Not Found
    or Other when that row is offered.
    """
    query = (desired or "").strip()
    if not selector or not query:
        return None
    focus_field(page, selector)
    shown = _selected_text(page, selector)
    if _labels_match(query, shown):
        return Option(value=shown, label=shown, selector=selector)
    _type_filter(page, selector, query, school=True)
    _press_enter(page, selector)
    found = _await_menu(page, selector, 2500)
    match = _best_school_match(query, found)
    if match is None:
        match = _school_fallback(found)
        if match is None:
            opened = _open_for_options(page, selector)
            match = _school_fallback(opened)
    if match is None or not match.selector:
        try:
            dismiss_combobox(page)
        except Exception:
            return None
        return None
    try:
        _choose_listed(page, match.selector, match.label)
    except Exception:
        return None
    _pause(page, 50)
    if _committed_choice(page, selector, match, query, accept_input=False) or _labels_match(
        match.label, _selected_text(page, selector)
    ):
        return match
    if _committed_choice(page, selector, match, match.label, accept_input=False):
        return match
    try:
        dismiss_combobox(page)
    except Exception:
        return None
    return None


_STUDY_FIELD_JS = r"""(selector) => {
  const el = document.querySelector(selector);
  if (!el) return false;
  const field = el.closest("[data-automation-id^='formField']");
  const ident = [
    el.id || "",
    el.getAttribute("name") || "",
    field ? (field.getAttribute("data-automation-id") || "") : "",
  ].join(" ").toLowerCase();
  if (ident.indexOf("fieldofstudy") !== -1 || ident.indexOf("field-of-study") !== -1) return true;
  let label = "";
  if (el.id) {
    const lab = document.querySelector("label[for='" + CSS.escape(el.id) + "']");
    if (lab) label = lab.innerText || lab.textContent || "";
  }
  if (!label && field) {
    const lab = field.querySelector("label, legend");
    if (lab && !lab.contains(el)) label = lab.innerText || lab.textContent || "";
  }
  return String(label || "").toLowerCase().indexOf("field of study") !== -1;
}"""


def _is_study_field(page, selector: str) -> bool:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return False
    try:
        return bool(evaluate(_STUDY_FIELD_JS, selector))
    except Exception:
        return False


def _best_study_match(query: str, found: list[Option]) -> Option | None:
    """Prefer the field-of-study row that names the profile value.

    ``engineer`` matches ``Engineering``. A menu with no such row matches nothing.
    """
    folded = (query or "").strip().casefold()
    if not folded:
        return None
    pool = [
        option
        for option in found
        if option.selector and not option.folder and not _menu_junk(option.label or option.value)
    ]
    if not pool:
        pool = [option for option in found if option.selector and not _menu_junk(option.label or option.value)]

    def label_of(option: Option) -> str:
        return (option.label or option.value or "").strip().casefold()

    exact = [option for option in pool if label_of(option) == folded]
    if exact:
        return exact[0]
    starts = [option for option in pool if label_of(option).startswith(folded)]
    if starts:
        return starts[0]
    contains = [option for option in pool if folded in label_of(option)]
    if len(contains) == 1:
        return contains[0]
    return None


def search_field_of_study(page, selector: str, desired: str) -> Option | None:
    """Type a field of study the way a school search types, and pick that row.

    There is no School Not Found fallback. No matching prompt row stays blank.
    """
    query = (desired or "").strip()
    if not selector or not query:
        return None
    focus_field(page, selector)
    shown = _selected_text(page, selector)
    already = Option(value=shown, label=shown, selector=selector)
    if shown and (_labels_match(query, shown) or query.casefold() in shown.casefold()):
        if _committed_choice(page, selector, already, query, accept_input=False):
            return already
    _type_filter(page, selector, query, school=True)
    _press_enter(page, selector)
    found = _await_menu(page, selector, 2500)
    match = _best_study_match(query, found)
    if match is None or not match.selector:
        try:
            dismiss_combobox(page)
        except Exception:
            return None
        return None
    try:
        _choose_listed(page, match.selector, match.label)
    except Exception:
        return None
    _pause(page, 50)
    if _committed_choice(page, selector, match, query, accept_input=False) or _labels_match(
        match.label, _selected_text(page, selector)
    ):
        return match
    pill = _selected_text(page, selector)
    if pill and query.casefold() in pill.casefold() and _committed_choice(
        page, selector, match, query, accept_input=False
    ):
        return match
    try:
        dismiss_combobox(page)
    except Exception:
        return None
    return None


def _menu_replaced(before: list[Option], after: list[Option]) -> bool:
    """True when a click opened a different menu.

    A nested prompt often has no chevron. The next list is the proof the row
    was a folder. An empty menu means the row committed or the menu closed.
    """

    def labels(options: list[Option]) -> set[str]:
        found: set[str] = set()
        for option in options:
            text = _fold(option.label or option.value)
            if text and not _menu_junk(option.label or option.value):
                found.add(text)
        return found

    after_labels = labels(after)
    if not after_labels:
        return False
    return bool(after_labels - labels(before))


def match_source_host(url: str, labels: list[str]) -> str:
    """One employer option whose leaf names this listing host.

    ``sourceUrl`` is the match listing URL. Several matches, or none, stay
    blank. The apply URL is not passed here, and the first option is not used.
    """
    from urllib.parse import urlparse

    host = (urlparse(str(url or "").strip()).hostname or "").lower().removeprefix("www.")
    stem = host.split(".")[0] if host else ""
    if len(stem) < 4:
        return ""
    generic = {
        "jobs",
        "careers",
        "apply",
        "workday",
        "myworkdayjobs",
        "smartrecruiters",
        "ashbyhq",
        "greenhouse",
        "lever",
        "icims",
        "taleo",
        "oraclecloud",
    }
    if stem in generic:
        return ""
    hits: list[str] = []
    for label in labels:
        text = str(label or "").strip()
        if not text:
            continue
        leaf = text.split(">")[-1].strip().casefold()
        if leaf == stem or leaf.replace(" ", "") == stem:
            hits.append(text)
    leaves = {item.split(">")[-1].strip().casefold() for item in hits}
    if len(leaves) != 1:
        return ""
    paths = [item for item in hits if ">" in item]
    return paths[0] if paths else hits[0]


def select_combobox(page, selector: str, desired: str) -> Option | None:
    """Open ``selector``, choose ``desired``, and keep it only when the widget commits.

    A category row drills into the leaf of the same name. A chevron category
    drills into a differently named leaf, and a ``Category > Leaf`` path is
    followed. A pill that already shows the choice is committed; the search
    box is not opened again. A click that paints the label but leaves the
    hidden input empty, or that shows a field error, is retried by typing
    the option and pressing Enter. A school search types with real keys and
    may fall back to School Not Found when the page says to.
    """
    if not selector or not desired.strip():
        return None
    _OPENED_CHILDREN.set(())
    focus_field(page, selector)
    shown = _selected_text(page, selector)
    leaf_name = _path_parts(desired)[-1] if _path_parts(desired) else desired
    if shown and (_labels_match(leaf_name, shown) or _labels_match(desired, shown)):
        # A pill already on the closed control is the commit. An open menu's
        # highlight uses the same words and is not.
        if _combobox_committed(page, selector, shown, accept_input=False):
            clear_satisfied_invalid(page, selector)
            return Option(value=shown, label=shown, selector=selector)
    if _is_school_field(page, selector):
        return search_school(page, selector, desired)
    if _is_study_field(page, selector):
        return search_field_of_study(page, selector, desired)
    found = _open_for_options(page, selector)
    if not found:
        # A prompt list that is not wired with aria-controls, and is the only open menu.
        found = _read_options(page, selector, allow_single=True)
    typed = False
    clicked_leaf = False
    school = _is_school_field(page, selector)
    top_level = list(found)
    match = _prefer_leaf(desired, found)
    folder_hit = match if match is not None and match.folder else None
    if match is not None and match.folder:
        match = None
    # A category menu is drilled first. Typing the leaf here hides the folder.
    # An all-promptOption menu has no chevron and no leaf flag. It is still drilled.
    needs_drill = _needs_drill(top_level)
    if match is None and folder_hit is None and not _path_parts(desired) and not needs_drill:
        for term in _filter_terms(desired):
            _type_filter(page, selector, term, school=school)
            typed = True
            wait_ms = 2500 if school else 1500
            found = _await_menu(page, selector, wait_ms)
            match = _prefer_leaf(desired, found)
            if match is not None and match.folder:
                folder_hit = match
                match = None
            if match is not None:
                break
    hierarchical = bool(_path_parts(desired) or needs_drill or folder_hit is not None)
    if hierarchical and match is None:
        drilled = _drill_for_leaf(page, selector, desired, top_level)
        if drilled is not None:
            match = drilled
            clicked_leaf = True
    if school and match is None:
        fallback = _school_not_found(page, selector)
        if fallback is not None:
            match = fallback
            clicked_leaf = True
    parts = _path_parts(desired)
    if match is None and len(parts) > 1:
        # "Job Board > Indeed" when the parent row has no chevron. Click that
        # row and take the leaf. Do not treat the path text as a missing option.
        parent = _match_desired(parts[0], top_level) or _match_desired(parts[0], found)
        if parent is not None and parent.selector:
            parent.folder = True
            parent.leaf = False
            match = parent
    if match is None and len(parts) > 1:
        # The menu is already the child list. The leaf name is the commit.
        leaf_only = _prefer_leaf(parts[-1], top_level) or _prefer_leaf(parts[-1], found)
        if leaf_only is not None and leaf_only.selector and not leaf_only.folder:
            match = leaf_only
    if match is None or not match.selector:
        _release_menu(page, selector)
        return None
    opened_submenu = False
    if not clicked_leaf:
        before = list(found)
        try:
            _choose_listed(page, match.selector, match.label)
        except Exception:
            _release_menu(page, selector)
            return None
        _pause(page, 50)
        # A promptOption is not flagged as a leaf. A click that replaces the
        # menu is still a folder, whether or not the row looked like a leaf.
        # Wait for the child list. The parent rows are still showing until then.
        after = _await_replaced(page, selector, before, 800)
        if _menu_replaced(before, after):
            match.folder = True
            match.leaf = False
            opened_submenu = True
            _note_children(match.label, after)
    if not match.leaf:
        # The category click opens the nested menu. The leaf is the commit.
        children = _read_options(page, selector)
        leaf = _other_leaf(desired, match, children)
        if leaf is None and not _committed_choice(page, selector, match, desired, accept_input=not typed):
            children = _await_menu(page, selector, 500)
            leaf = _other_leaf(desired, match, children)
        if leaf is None and match.folder and not _committed_choice(
            page, selector, match, desired, accept_input=not typed
        ):
            leaf = child_for_answer(desired, match.label, children)
        if leaf is None and match.folder:
            _note_children(match.label, children)
            opened_submenu = True
        if leaf is not None and leaf.selector:
            try:
                _choose_listed(page, leaf.selector, leaf.label)
            except Exception:
                _release_menu(page, selector)
                return None
            _pause(page, 50)
            match = leaf
    if opened_submenu and (match.folder or not match.leaf):
        # The parent name in the search box is not a committed leaf.
        _release_menu(page, selector)
        return None
    pill_widget = _has_pill_slot(page, selector)
    if _kept_after_close(page, selector, match, desired, accept_input=not typed and not pill_widget):
        _OPENED_CHILDREN.set(())
        return match
    _keyboard_commit(page, selector, desired, match)
    if _kept_after_close(page, selector, match, desired, accept_input=not pill_widget):
        _OPENED_CHILDREN.set(())
        return match
    _release_menu(page, selector)
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


# Workday paints the clicked row first. The closed button can then commit the
# opposite word, or flip again after a short settle. These waits are the
# settle; a match during the first paint is not a commit.
_LISTBOX_SETTLE_MS = 160
_LISTBOX_SETTLE_AGAIN_MS = 100
_LISTBOX_RECHECK_MS = 120

# The committed value is the closed button itself. A promptSelectionLabel
# elsewhere in the field is the clicked row, not this button. A decoy span
# inside the button is not the value either. ``promptSelectionLabel`` on the
# button is that value. The accessible name is returned separately.
_COMMITTED_BUTTON_JS = r"""(selector) => {
  let el = null;
  try { el = document.querySelector(selector); } catch (error) { return { missing: true }; }
  if (!el) return { missing: true };
  function clean(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }
  const expanded = el.getAttribute("aria-expanded") === "true";
  const menu = el.querySelector("[role='listbox'], [data-autofill-menu]");
  let menuOpen = false;
  if (menu) {
    const style = window.getComputedStyle(menu);
    menuOpen = style.display !== "none" && style.visibility !== "hidden" && !menu.hidden;
  }
  let labelled = "";
  const nodes = el.querySelectorAll(
    "[data-automation-id='promptSelectionLabel'], [data-automation-id='selectedItemText']"
  );
  for (const node of nodes) {
    if (node.closest("[role='option'], [role='listbox'], [data-autofill-menu], [data-autofill-decoy]")) continue;
    const text = clean(node.innerText || node.textContent);
    if (text) labelled = text;
  }
  const clone = el.cloneNode(true);
  clone.querySelectorAll(
    "[role='option'], [role='listbox'], [data-autofill-menu], [data-autofill-decoy]"
  ).forEach((node) => node.remove());
  return {
    missing: false,
    open: expanded || menuOpen,
    labelled: labelled,
    visible: clean(clone.textContent),
    aria: clean(el.getAttribute("aria-label") || ""),
  };
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


def _open_listbox(page, selector: str) -> bool:
    """Open one choice button. A pointer click is not used.

    The menu is not in the page until the button is clicked. A trusted pointer
    click can release on the first option (Yes) when that row covers the button.
    """
    return click_element(page, selector)


def _choose_listed(page, selector: str, name: str) -> bool:
    """Choose one menu row. A final Submit label is refused."""
    if click_element(page, selector, name):
        return True
    try:
        click_choice(page, selector, name)
    except HumanSubmissionRequired:
        raise
    except Exception:
        return False
    return True


def read_listbox_button_options(page, selector: str) -> list[Option]:
    """Open one Workday listbox button, read the rows that appear, and close it.

    The menu is not in the page until the button is clicked. ``Select One`` is
    not an option. A selected pill on another prompt is not this menu.
    """
    if not selector:
        return []
    try:
        opened = _open_listbox(page, selector)
    except HumanSubmissionRequired:
        raise
    except Exception:
        return []
    if not opened:
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


def _listbox_token(shown: str) -> str | None:
    """The committed Yes or No, or None when the text is a menu of both.

    ``Yes Required`` is Yes. A string that contains both Yes and No is an open
    menu or a decoy row, not the value Workday committed.
    """
    from autofill.mapping import normalize, yes_no

    text = re.sub(r"\brequired\b", "", normalize(shown)).strip()
    if not text:
        return None
    if re.search(r"\byes\b", text) and re.search(r"\bno\b", text):
        return None
    exact = yes_no(text)
    if exact:
        return exact
    first = text.split(" ")[:1]
    if first == ["yes"] or first == ["no"]:
        return first[0]
    return None


def _read_closed_button(page, selector: str) -> dict:
    """Parts of one listbox button. ``missing`` when the button is gone."""
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return {"missing": True}
    try:
        raw = evaluate(_COMMITTED_BUTTON_JS, selector)
    except Exception:
        return {"missing": True}
    if isinstance(raw, str):
        return {"missing": False, "open": False, "labelled": raw, "visible": raw, "aria": ""}
    if not isinstance(raw, dict):
        return {"missing": True}
    return raw


def _agree_label(labelled: str, visible: str, aria: str) -> str:
    """The closed button's Yes or No when its own reads agree.

    A clicked-row label and the button text are not a commit when they
    disagree. The word comes from those reads. It is not copied from the
    answer we intended.
    """
    tokens: list[str] = []
    for part in (labelled, visible, aria):
        token = _listbox_token(part)
        if token:
            tokens.append(token)
    if not tokens:
        return (labelled or visible or aria or "").strip()
    if len(set(tokens)) != 1:
        return ""
    return "Yes" if tokens[0] == "yes" else "No"


def _button_text(page, selector: str) -> str:
    """Committed label of one closed listbox button.

    An open menu, a clicked row outside the button, and a decoy inside it are
    not this value. Disagreeing reads are not a commit.
    """
    raw = _read_closed_button(page, selector)
    if raw.get("missing") or raw.get("open"):
        return ""
    return _agree_label(
        str(raw.get("labelled") or ""),
        str(raw.get("visible") or ""),
        str(raw.get("aria") or ""),
    )


def _answer_matches(shown: str, intended: str) -> bool:
    """True when the committed button text is the approved answer. Yes is not No."""
    from autofill.mapping import normalize, yes_no

    wanted = yes_no(intended)
    if wanted:
        return _listbox_token(shown) == wanted
    folded = normalize(shown)
    target = normalize(intended)
    return bool(target) and folded == target


def _settled_button_label(page, selector: str) -> str:
    """Closed-button label after the menu is gone and a short settle.

    The first paint is often the clicked row. This wait does not return when
    that paint matches the intended answer. The string is the later read.
    """
    _close_listbox(page)
    _pause(page, _LISTBOX_SETTLE_MS)
    _close_listbox(page)
    _pause(page, _LISTBOX_SETTLE_AGAIN_MS)
    _close_listbox(page)
    return _button_text(page, selector)


def _revert_listbox(page, selector: str) -> None:
    """Clear a committed choice that is not the approved answer."""
    try:
        clear_listbox_value(page, selector)
    except HumanSubmissionRequired:
        raise
    except Exception:
        return


def _note_listbox(selector: str, question: str, intended: str, clicked: str, shown: str) -> None:
    label = " ".join((question or selector or "").split())[:160]
    logger.info(
        "listbox label=%s intended=%s clicked=%s shown=%s",
        label,
        intended,
        clicked,
        shown,
    )
    note_listbox_choice(
        {
            "selector": selector,
            "label": label,
            "intended": intended,
            "clicked": clicked,
            "shown": shown,
        }
    )


def select_listbox_button(page, selector: str, desired: str, question: str = "") -> Option | None:
    """Open a Workday choice button and choose ``desired``. Does not type into the button.

    The button text afterwards must be the approved answer, not merely the row
    that was clicked and not the first visible row. The check reads the closed
    button's committed label, not an open menu or a decoy Yes/No inside it.
    A mismatch is clicked again. If it still disagrees, the wrong value is
    cleared and this returns None.
    A button that already shows the approved answer is left as it is.
    """
    intended = str(desired or "").strip()
    if not selector or not intended:
        return None
    from autofill.intents import pick_option

    clicked = ""
    for _attempt in range(2):
        shown = _button_text(page, selector)
        if _answer_matches(shown, intended):
            clear_satisfied_invalid(page, selector)
            _note_listbox(selector, question, intended, clicked or shown, shown)
            return Option(value=intended, label=shown, selector=selector)
        try:
            opened = _open_listbox(page, selector)
        except HumanSubmissionRequired:
            raise
        except Exception:
            opened = False
        if not opened:
            break
        found = _await_open_listbox(page, 400)
        if not found:
            _close_listbox(page)
            shown = _button_text(page, selector)
            if _answer_matches(shown, intended):
                clear_satisfied_invalid(page, selector)
                _note_listbox(selector, question, intended, clicked or shown, shown)
                return Option(value=intended, label=shown, selector=selector)
            continue
        status, match = pick_option(intended, found)
        if status != "match" or match is None or not match.selector:
            _close_listbox(page)
            break
        clicked = match.label or intended
        try:
            chose = _choose_listed(page, match.selector, clicked)
        except HumanSubmissionRequired:
            raise
        except Exception:
            chose = False
        if not chose:
            _close_listbox(page)
            continue
        # The menu's own Yes/No is not the commit. Close it, wait out the
        # paint of the clicked row, then read the closed button. ``shown`` is
        # that read. It is not the intended word and not the row we clicked.
        _close_listbox(page)
        shown = _settled_button_label(page, selector)
        if _answer_matches(shown, intended):
            clear_satisfied_invalid(page, selector)
            _note_listbox(selector, question, intended, clicked, shown)
            return Option(value=intended, label=shown, selector=match.selector)
        _close_listbox(page)
    shown = _button_text(page, selector)
    if shown and not _answer_matches(shown, intended):
        _revert_listbox(page, selector)
        _pause(page, 40)
        shown = _button_text(page, selector)
    _note_listbox(selector, question, intended, clicked, shown)
    if _answer_matches(shown, intended):
        clear_satisfied_invalid(page, selector)
        return Option(value=intended, label=shown, selector=selector)
    return None


def resettle_listboxes(page) -> None:
    """Re-read listboxes after the page has had a moment to flip a value.

    A button that no longer shows the approved answer is cleared and chosen
    again. A button that left the page is left alone. ``shown`` stays the
    read label.
    """
    choices = latest_listbox_choices()
    if not choices:
        return
    _pause(page, _LISTBOX_RECHECK_MS)
    for choice in choices:
        selector = str(choice.get("selector") or "")
        intended = str(choice.get("intended") or "")
        if not selector or not intended:
            continue
        raw = _read_closed_button(page, selector)
        if raw.get("missing"):
            continue
        shown = _button_text(page, selector)
        if _answer_matches(shown, intended):
            if shown and shown != str(choice.get("shown") or ""):
                _note_listbox(
                    selector,
                    str(choice.get("label") or ""),
                    intended,
                    str(choice.get("clicked") or ""),
                    shown,
                )
            continue
        select_listbox_button(page, selector, intended, question=str(choice.get("label") or ""))
