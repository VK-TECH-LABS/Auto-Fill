"""Workday month, day, and year inputs that together are one date.

``dateSectionMonth-input``, ``dateSectionDay-input``, and
``dateSectionYear-input`` under one form field are one date. Under a
``--startDate`` or ``--endDate`` id they are a work or education From/To and
come from that profile row. A current job leaves the end pair empty. A
signature date (Self Identify, Voluntary Disclosures) is always today.

Workday only commits a segment from real key presses, so every write focuses
the segment, types its digits, and leaves with Tab. Setting ``.value`` shows
the digits without committing them, so it is never used here. A date counts as
written only when Workday's own readout says so, or the typed digits held and
the field shows no error.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from autofill.dates import parse_month_year
from autofill.models import Control, PageSnapshot
from autofill.profile import CandidateProfile

_PART_RE = re.compile(r"datesection(month|day|year)", re.IGNORECASE)
_WHICH_RE = re.compile(r"(.+?)--(start|end)date", re.IGNORECASE)
_SINGLE_RE = re.compile(r"([^\s]*?)-?datesection(?:month|day|year)", re.IGNORECASE)
_SEGMENT_ORDER = ("month", "day", "year")
# A date the person signs today. Birth, start, availability, and document dates are not.
_SIGNATURE_IDENT_RE = re.compile(r"signed|signature|todaysdate|todaydate|currentdate", re.IGNORECASE)
_SIGNATURE_LABEL_RE = re.compile(
    r"^\s*(?:today'?s\s+date|date\s+signed|signature\s+date|date(?:\s+of\s+signature)?)\s*\*?\s*$",
    re.IGNORECASE,
)
_NOT_SIGNATURE_RE = re.compile(
    r"birth|\bdob\b|start|\bend\b|avail|graduat|expir|issue|hire|employ|leave|separat|discharge|\bfrom\b|\bto\b",
    re.IGNORECASE,
)


def _ident(control: Control) -> str:
    return " ".join(
        part
        for part in (control.element_id, control.name, control.automation_id, control.selector)
        if part
    )


def split_date_part(control: Control) -> tuple[str, str, str] | None:
    """``(row key, start|end|single, month|day|year)`` for one Workday date section input.

    ``single`` is a date that is not a work or education From/To, such as a
    signature date. Its row key is the form field id before ``dateSection``.
    """
    ident = _ident(control)
    part_match = _PART_RE.search(ident)
    if not part_match:
        return None
    part = part_match.group(1).casefold()
    which_match = _WHICH_RE.search(ident)
    if which_match:
        which = "start" if which_match.group(2).casefold() == "start" else "end"
        return which_match.group(1), which, part
    single = _SINGLE_RE.search(control.element_id or control.selector or "")
    key = (single.group(1) if single else "") or (control.form_automation_id or "") or "date"
    return key, "single", part


def is_signature_date(controls: list[Control]) -> bool:
    """A Month/Day/Year date the applicant signs today, not a From/To or birth date."""
    if not controls:
        return False
    idents = " ".join(_ident(item) for item in controls)
    if not re.search(r"datesectionday", idents, re.IGNORECASE):
        return False
    labels = " ".join(
        str(part)
        for item in controls
        for part in (item.label, item.prompt, item.group)
        if part
    )
    if _NOT_SIGNATURE_RE.search(labels):
        return False
    if _SIGNATURE_IDENT_RE.search(idents) or _SIGNATURE_IDENT_RE.search(labels.replace(" ", "")):
        return True
    return any(_SIGNATURE_LABEL_RE.match(str(item.label or item.prompt or "")) for item in controls)


def today_parts() -> dict[str, str]:
    """Today in this machine's local calendar. Never a saved answer."""
    today = date.today()
    return {"month": f"{today.month:02d}", "day": f"{today.day:02d}", "year": str(today.year)}


_SEGMENTS_COMMITTED_JS = r"""({ selector, wanted }) => {
  let el = null;
  try { el = document.querySelector(selector); } catch (error) { return false; }
  if (!el) return false;
  const field = el.closest("[data-automation-id^='formField']") || el.parentElement || el;
  const text = String(field.innerText || field.textContent || "").replace(/\s+/g, " ");
  const readout = text.match(/current value is\s+([0-9A-Za-z]{1,4}(?:\s*\/\s*[0-9A-Za-z]{1,4}){1,2})/i);
  const parts = Array.from(field.querySelectorAll("input")).filter((node) =>
    /dateSection(Month|Day|Year)/i.test((node.id || "") + " " + (node.getAttribute("data-automation-id") || ""))
  );
  if (readout) {
    const shown = readout[1].replace(/\s+/g, "");
    if (/[A-Za-z]/.test(shown)) return false;
    if (wanted) return shown === wanted;
    return true;
  }
  const invalid =
    !!field.querySelector("[data-automation-id='errorMessage'], [role='alert']") ||
    parts.some((node) => node.getAttribute("aria-invalid") === "true");
  if (invalid || !parts.length) return false;
  const values = parts.map((node) => String(node.value || "").trim());
  if (values.some((value) => !/^\d+$/.test(value))) return false;
  if (!wanted) return true;
  return values.join("/") === wanted;
}"""


def _segments_committed(page, selector: str, wanted: str = "") -> bool:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None or not selector:
        return False
    try:
        return bool(evaluate(_SEGMENTS_COMMITTED_JS, {"selector": selector, "wanted": wanted}))
    except Exception:
        return False


def type_date_segments(page, parts: dict[str, Control], values: dict[str, str]) -> bool:
    """Type each segment with real key presses in Month, Day, Year order, then Tab.

    True only when Workday shows that exact date afterwards.
    """
    locator = getattr(page, "locator", None)
    keyboard = getattr(page, "keyboard", None)
    ordered = [(name, parts[name]) for name in _SEGMENT_ORDER if name in parts and parts[name].selector]
    if locator is None or keyboard is None or not ordered:
        return False
    if any(not values.get(name) for name, _ in ordered):
        return False
    wanted = "/".join(values[name] for name, _ in ordered)

    def type_parts(names: list[tuple[str, Control]]) -> None:
        for name, control in names:
            target = locator(control.selector).first
            target.focus(timeout=3000)
            target.press("Control+A")
            target.press("Backspace")
            keyboard.type(values[name], delay=40)
        keyboard.press("Tab")

    try:
        type_parts(ordered)
        if _segments_committed(page, ordered[0][1].selector, wanted):
            return True
        # A year edit can clear the month or day. Retype them without another year edit.
        type_parts([item for item in ordered if item[0] != "year"])
    except Exception:
        return False
    return _segments_committed(page, ordered[0][1].selector, wanted)


def _bucket(row_key: str) -> str:
    token = row_key.casefold()
    if "educat" in token or "school" in token:
        return "education"
    return "work"


_DEEP_QUERY_JS = r"""
  function afQuery(selector) {
    if (!selector) return null;
    const stack = [document];
    const seen = new Set();
    while (stack.length) {
      const root = stack.pop();
      if (!root || seen.has(root)) continue;
      seen.add(root);
      try {
        const found = root.querySelector(selector);
        if (found) return found;
      } catch (error) {
        return null;
      }
      let nodes = [];
      try { nodes = Array.from(root.querySelectorAll("*")); } catch (error) { nodes = []; }
      for (const node of nodes) {
        if (node.shadowRoot) stack.push(node.shadowRoot);
      }
    }
    return null;
  }
  function writeValue(node, value) {
    if (!node) return;
    const tracker = node._valueTracker;
    if (tracker && tracker.setValue) tracker.setValue("");
    const proto = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
    if (proto && proto.set) proto.set.call(node, value);
    else node.value = value;
    node.dispatchEvent(new Event("input", { bubbles: true }));
    node.dispatchEvent(new Event("change", { bubbles: true }));
  }
"""

_MONTH_YEAR_RE = re.compile(r"(?<![a-z/])mm\s*/\s*yyyy\b", re.IGNORECASE)
_DAY_DATE_RE = re.compile(r"mm\s*/\s*dd|dd\s*/\s*mm|\b(?:dd|day)\b", re.IGNORECASE)
_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _month_year_control(control: Control) -> bool:
    """A single From/To box whose placeholder is MM/YYYY, not a full date or a split pair."""
    if control.hidden or control.kind not in {"text", "combobox"}:
        return False
    if split_date_part(control) is not None:
        # A Workday month/day/year segment is typed by fill_split_dates, never as one box.
        return False
    hint = " ".join(part for part in (control.placeholder, control.label, control.aria_label) if part)
    if not hint or _DAY_DATE_RE.search(hint):
        return False
    return _MONTH_YEAR_RE.search(hint) is not None


def _which_side(control: Control) -> str:
    blob = " ".join(
        part
        for part in (control.label, control.aria_label, control.form_automation_id, control.automation_id, control.name)
        if part
    ).casefold()
    if any(token in blob for token in ("end date", "enddate", " to", "todate")) or blob.strip() in {"to", "to*"}:
        return "end"
    return "start"


def _date_bucket(control: Control) -> str:
    blob = " ".join(
        part
        for part in (
            control.form_automation_id,
            control.automation_id,
            control.element_id,
            control.name,
            control.nearby,
            control.group,
            control.label,
        )
        if part
    ).casefold()
    if any(token in blob for token in ("education", "school")):
        return "education"
    return "work"


def month_year_slots(snapshot: PageSnapshot) -> list[tuple[str, int, str, Control]]:
    """Page-order From/To boxes. Each start begins the next work or education row."""
    slots: list[tuple[str, int, str, Control]] = []
    index = {"work": -1, "education": -1}
    expect = {"work": "start", "education": "start"}
    for control in snapshot.controls:
        if not _month_year_control(control) or not control.selector:
            continue
        bucket = _date_bucket(control)
        which = _which_side(control)
        if which == "start" or expect[bucket] == "start":
            index[bucket] += 1
            which = "start"
        slots.append((bucket, index[bucket], which, control))
        expect[bucket] = "end" if which == "start" else "start"
    return slots


def month_year_profile_selectors(snapshot: PageSnapshot, profile: CandidateProfile | None) -> set[str]:
    """Selectors whose profile row already has that From or To. They are not questions."""
    if profile is None:
        return set()
    covered: set[str] = set()
    for bucket, index, which, control in month_year_slots(snapshot):
        rows = profile.employment if bucket == "work" else profile.education_history
        if index < 0 or index >= len(rows):
            continue
        row = rows[index]
        if which == "end" and getattr(row, "current", False):
            covered.add(control.selector)
            continue
        raw = row.start_date if which == "start" else row.end_date
        if parse_month_year(raw):
            covered.add(control.selector)
    return covered


_BOX_HOLDS_JS = r"""({ selector, text }) => {
  let el = null;
  try { el = document.querySelector(selector); } catch (error) { return false; }
  if (!el) return false;
  const shown = String(el.value || "").replace(/\s+/g, "");
  if (shown !== String(text)) return false;
  const field = el.closest("[data-automation-id^='formField']") || el.parentElement || el;
  return !field.querySelector("[data-automation-id='errorMessage']") && el.getAttribute("aria-invalid") !== "true";
}"""


_WRITE_MONTH_YEAR_JS = r"""({ selector, text, monthName, year }) => {
""" + _DEEP_QUERY_JS + r"""
  const el = afQuery(selector);
  if (!el) return { ok: false };
  function clean(value) {
    return String(value || "").replace(/\s+/g, " ").trim();
  }
  function held() {
    return clean(el.value) === text;
  }
  writeValue(el, text);
  el.dispatchEvent(new Event("blur", { bubbles: true }));
  if (held()) return { ok: true, how: "value" };
  el.focus();
  function visible(node) {
    const style = window.getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden") return false;
    const box = node.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  }
  function buttons() {
    return Array.from(document.querySelectorAll("button, [role='button']")).filter(visible);
  }
  const field = el.closest("[data-automation-id^='formField']") || el.parentElement || el;
  const local = Array.from(field.querySelectorAll("button, [role='button']"));
  const opener = local.find((node) => {
    const label = (node.getAttribute("aria-label") || "") + " " + (node.getAttribute("data-automation-id") || "");
    return /calendar|dateicon|date-icon|select date/i.test(label);
  });
  if (opener) {
    const open = opener["click"];
    if (open) open.call(opener);
  } else {
    const press = el["click"];
    if (press) press.call(el);
  }
  let guard = 0;
  while (guard < 24) {
    guard += 1;
    const shown = buttons();
    const header = shown.map((node) => clean(node.innerText || node.textContent)).join(" ");
    if (header.indexOf(String(year)) !== -1) break;
    const next = shown.find((node) => /next/i.test(node.getAttribute("aria-label") || ""));
    const prev = shown.find((node) => /prev|previous/i.test(node.getAttribute("aria-label") || ""));
    const targetYear = Number(year);
    const headerYear = Number((header.match(/\b(19|20)\d{2}\b/) || [])[0] || 0);
    const move = headerYear && targetYear && headerYear > targetYear ? prev : next;
    if (!move) break;
    const step = move["click"];
    if (step) step.call(move);
  }
  const month = buttons().find((node) => clean(node.innerText || node.textContent) === monthName);
  if (month) {
    const choose = month["click"];
    if (choose) choose.call(month);
  }
  if (!held()) writeValue(el, text);
  return { ok: held(), how: month ? "calendar" : "value" };
}"""


def _type_month_year_box(page, control: Control, text: str) -> bool:
    """Type MM/YYYY into one masked box with key presses and leave with Tab.

    A box that ignores typing (its value only changes from its calendar) is
    set by picking the month there. The picker is then left with Tab so it
    does not stay open over the next field.
    """
    locator = getattr(page, "locator", None)
    keyboard = getattr(page, "keyboard", None)
    evaluate = getattr(page, "evaluate", None)
    if locator is None or keyboard is None or evaluate is None:
        return False
    try:
        for typed in (text, text.replace("/", "")):
            # A mask that inserts its own slash takes the digits alone.
            target = locator(control.selector).first
            target.focus(timeout=3000)
            target.press("Control+A")
            target.press("Backspace")
            keyboard.type(typed, delay=40)
            keyboard.press("Tab")
            if bool(evaluate(_BOX_HOLDS_JS, {"selector": control.selector, "text": text})):
                return True
        month, year = text.split("/")
        report = evaluate(
            _WRITE_MONTH_YEAR_JS,
            {"selector": control.selector, "text": text, "monthName": _MONTH_NAMES[int(month) - 1], "year": year},
        )
        keyboard.press("Tab")
        return bool(isinstance(report, dict) and report.get("ok"))
    except Exception:
        return False


def fill_month_year_inputs(page, profile: CandidateProfile, snapshot: PageSnapshot) -> set[str]:
    """Write each MM/YYYY From or To from that work or education row.

    The box is typed with key presses and must keep ``MM/YYYY`` with no field
    error. Only a box that held is returned, so a failed write is still asked.
    """
    handled: set[str] = set()
    for bucket, index, which, control in month_year_slots(snapshot):
        rows = profile.employment if bucket == "work" else profile.education_history
        if index < 0 or index >= len(rows):
            continue
        row = rows[index]
        if which == "end" and getattr(row, "current", False):
            handled.add(control.selector)
            continue
        parsed = parse_month_year(row.start_date if which == "start" else row.end_date)
        if parsed is None:
            continue
        month, year = parsed
        if _type_month_year_box(page, control, f"{month:02d}/{year}"):
            handled.add(control.selector)
    return handled


_CURRENT_WORK_RE = re.compile(
    r"\b(?:i currently work here|currently work here|i currently work|currently working here)\b",
    re.IGNORECASE,
)
_CURRENT_EDU_RE = re.compile(
    r"\b(?:i currently attend|currently attending|currently enrolled)\b",
    re.IGNORECASE,
)
_CHECK_BOX_JS = r"""(selector) => {
  let el = null;
  try { el = document.querySelector(selector); } catch (error) { return false; }
  if (!el) return false;
  if (el.checked) return true;
  const press = el["click"];
  if (press) press.call(el);
  if (el.checked) return true;
  const proto = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "checked");
  if (proto && proto.set) proto.set.call(el, true);
  else el.checked = true;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return !!el.checked;
}"""


def _checkbox_blob(control: Control) -> str:
    return " ".join(part for part in (control.label, control.aria_label, control.prompt, control.group) if part)


def fill_current_boxes(page, profile: CandidateProfile, snapshot: PageSnapshot) -> set[str]:
    """Check "I currently work here" when that row is current. Do not invent a To date.

    A box for a past row stays unchecked and is not asked. Education uses the
    same rule when the form asks about current enrollment.
    """
    handled: set[str] = set()
    work: list[Control] = []
    school: list[Control] = []
    for control in snapshot.controls:
        if control.hidden or control.kind != "checkbox" or not control.selector:
            continue
        blob = _checkbox_blob(control)
        if _CURRENT_WORK_RE.search(blob):
            work.append(control)
        elif _CURRENT_EDU_RE.search(blob):
            school.append(control)
    evaluate = getattr(page, "evaluate", None)

    def apply(boxes: list[Control], rows: list) -> None:
        for index, control in enumerate(boxes):
            if index >= len(rows):
                continue
            handled.add(control.selector)
            if not getattr(rows[index], "current", False) or evaluate is None:
                continue
            try:
                evaluate(_CHECK_BOX_JS, control.selector)
            except Exception:
                continue

    apply(work, list(profile.employment))
    apply(school, list(profile.education_history))
    return handled


def _groups(
    snapshot: PageSnapshot,
) -> tuple[dict[tuple[str, str, str], dict[str, Control]], list[tuple[str, str, str]], dict[str, list[str]]]:
    groups: dict[tuple[str, str, str], dict[str, Control]] = {}
    order: list[tuple[str, str, str]] = []
    rows: dict[str, list[str]] = {"work": [], "education": [], "single": []}
    for control in snapshot.controls:
        if control.hidden or not control.selector:
            continue
        parsed = split_date_part(control)
        if parsed is None:
            continue
        row_key, which, part = parsed
        bucket = "single" if which == "single" else _bucket(row_key)
        key = (bucket, row_key, which)
        groups.setdefault(key, {})[part] = control
        if key not in order:
            order.append(key)
        if row_key not in rows[bucket]:
            rows[bucket].append(row_key)
    return groups, order, rows


def fill_split_dates(page, profile: CandidateProfile, snapshot: PageSnapshot) -> set[str]:
    """Type each From/To from the matching profile row, and each signature date as today.

    Every split input is returned so the generic text fill does not type a
    whole date into one segment. A date that did not commit is asked later by
    the split-date gap scan, which reads Workday's own readout.
    """
    groups, order, rows_by_bucket = _groups(snapshot)
    handled: set[str] = set()
    for key in order:
        bucket, row_key, which = key
        parts = groups[key]
        handled.update(control.selector for control in parts.values() if control.selector)
        anchor = next((parts[name] for name in _SEGMENT_ORDER if name in parts), None)
        if anchor is None:
            continue
        if which == "single":
            if not is_signature_date(list(parts.values())) or not {"month", "day", "year"} <= set(parts):
                continue
            today = today_parts()
            if not _segments_committed(page, anchor.selector, "/".join(today[name] for name in _SEGMENT_ORDER)):
                type_date_segments(page, parts, today)
            continue
        if _segments_committed(page, anchor.selector):
            continue
        if "month" not in parts or "year" not in parts:
            continue
        index = rows_by_bucket[bucket].index(row_key)
        rows = profile.employment if bucket == "work" else profile.education_history
        if index < 0 or index >= len(rows):
            continue
        row = rows[index]
        if which == "end" and getattr(row, "current", False):
            continue
        parsed_date = parse_month_year(row.start_date if which == "start" else row.end_date)
        if parsed_date is None:
            continue
        month, year = parsed_date
        values = {"month": f"{month:02d}", "year": str(year)}
        if "day" in parts:
            values["day"] = "01"
        type_date_segments(page, parts, values)
    return handled


_READOUT_RE = re.compile(r"current value is\s+(\d{1,2})\s*/\s*(\d{4})", re.IGNORECASE)

def split_pair_committed(page, selector: str) -> bool:
    """True when this Workday date already holds a full date.

    Workday's readout (``current value is MM/YYYY`` or ``MM/DD/YYYY``) decides
    when it is there; a readout still showing ``DD`` or ``YYYY`` is not a date.
    Without a readout, every segment must be numeric and the field must show
    no error. Digits set without key presses leave the error, so they fail.
    """
    return _segments_committed(page, selector)


def date_groups(snapshot: PageSnapshot) -> list[dict[str, Any]]:
    """Split dates in page order. Each item is one From, To, or single date."""
    groups, order, rows = _groups(snapshot)
    found: list[dict[str, Any]] = []
    for bucket, row_key, which in order:
        parts = groups[(bucket, row_key, which)]
        selectors = [control.selector for control in parts.values() if control.selector]
        anchor = next((parts[name] for name in _SEGMENT_ORDER if name in parts), None)
        found.append(
            {
                "bucket": bucket,
                "index": rows[bucket].index(row_key),
                "which": which,
                "selectors": selectors,
                "anchor": anchor,
                "controls": list(parts.values()),
                "signature": which == "single" and is_signature_date(list(parts.values())),
            }
        )
    return found


def date_prompt(profile: CandidateProfile | None, bucket: str, index: int, which: str) -> str:
    """Label one date by its work or education entry. ``From`` alone is not a label."""
    side = "From" if which == "start" else "To"
    if bucket == "education":
        school = ""
        if profile is not None and 0 <= index < len(profile.education_history):
            school = profile.education_history[index].school.strip()
        label = f"Education {index + 1}"
        if school:
            label = f"{label} — {school}"
        return f"{label} — {side}"
    company = ""
    if profile is not None and 0 <= index < len(profile.employment):
        company = profile.employment[index].company.strip()
    label = f"Work Experience {index + 1}"
    if company:
        label = f"{label} — {company}"
    return f"{label} — {side}"


def date_field_key(bucket: str, index: int, which: str) -> str:
    section = "education" if bucket == "education" else "employment"
    member = "startDate" if which == "start" else "endDate"
    return f"{section}.{index}.{member}"


def _sibling_selector(selector: str, part: str) -> str:
    """The same date's ``dateSection<Part>`` input for this selector."""
    name = {"month": "Month", "day": "Day", "year": "Year"}[part]
    return re.sub(r"dateSection(?:Month|Day|Year)", "dateSection" + name, selector, count=1, flags=re.IGNORECASE)


def is_split_date_answer(target: dict) -> bool:
    """True when this answer belongs on one Workday month/day/year date."""
    key = str(target.get("fieldKey") or "")
    if re.fullmatch(r"(employment|education)\.\d+\.(startDate|endDate)", key):
        return True
    selector = str(target.get("selector") or "")
    if not selector:
        return False
    return split_date_part(Control(kind="text", selector=selector, element_id=selector)) is not None


_PART_PRESENT_JS = r"""(selector) => {
  try { return !!document.querySelector(selector); } catch (error) { return false; }
}"""


def _answer_values(value: str, has_day: bool) -> dict[str, str] | None:
    """Month/Day/Year digits from ``MM/DD/YYYY`` (US widget order) or ``MM/YYYY``."""
    text = value.strip()
    if has_day:
        full = re.fullmatch(r"(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{4})", text)
        if not full:
            return None
        month, day, year = int(full.group(1)), int(full.group(2)), int(full.group(3))
        try:
            date(year, month, day)
        except ValueError:
            return None
        return {"month": f"{month:02d}", "day": f"{day:02d}", "year": str(year)}
    parsed = parse_month_year(text)
    if parsed is None:
        return None
    month, year = parsed
    return {"month": f"{month:02d}", "year": str(year)}


def commit_split_date_answer(page, target: dict) -> dict:
    """Type this answer into the month/day/year inputs of this one date only."""
    value = str(target.get("value") or "").strip()
    if not value:
        values = [str(item).strip() for item in target.get("values") or [] if str(item).strip()]
        value = values[0] if values else ""
    selector = str(target.get("selector") or "")
    evaluate = getattr(page, "evaluate", None)
    if not selector or evaluate is None:
        return {"filled": False, "verified": False, "reason": "not_committed"}
    parts: dict[str, Control] = {}
    for name in _SEGMENT_ORDER:
        sibling = _sibling_selector(selector, name)
        try:
            present = bool(evaluate(_PART_PRESENT_JS, sibling))
        except Exception:
            present = False
        if present:
            parts[name] = Control(kind="text", selector=sibling, element_id=sibling)
    if "month" not in parts or "year" not in parts:
        return {"filled": False, "verified": False, "reason": "not_committed"}
    answer = _answer_values(value, "day" in parts)
    if answer is None or not type_date_segments(page, parts, answer):
        return {"filled": False, "verified": False, "reason": "not_committed"}
    return {"filled": True, "verified": True}
