"""Workday month and year inputs that together are one date.

``dateSectionMonth-input`` and ``dateSectionYear-input`` under a
``--startDate`` or ``--endDate`` id are filled as one pair. A current job
leaves the end pair empty. An answer for one part does not erase the other.
"""

from __future__ import annotations

import re
from typing import Any

from autofill.dates import parse_month_year
from autofill.models import Control, PageSnapshot
from autofill.profile import CandidateProfile

_PART_RE = re.compile(r"datesection(month|year)", re.IGNORECASE)
_WHICH_RE = re.compile(r"(.+?)--(start|end)date", re.IGNORECASE)


def split_date_part(control: Control) -> tuple[str, str, str] | None:
    """``(row key, start|end, month|year)`` for one Workday date section input."""
    ident = " ".join(
        part
        for part in (control.element_id, control.name, control.automation_id, control.selector)
        if part
    )
    part_match = _PART_RE.search(ident)
    which_match = _WHICH_RE.search(ident)
    if not part_match or not which_match:
        return None
    which = "start" if which_match.group(2).casefold() == "start" else "end"
    part = "month" if part_match.group(1).casefold() == "month" else "year"
    return which_match.group(1), which, part


def _bucket(row_key: str) -> str:
    token = row_key.casefold()
    if "educat" in token or "school" in token:
        return "education"
    return "work"


_WRITE_PAIR_JS = r"""({ monthSelector, yearSelector, month, year }) => {
  function setValue(selector, value) {
    let el = null;
    try { el = document.querySelector(selector); } catch (error) { return null; }
    if (!el) return null;
    const proto = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
    if (proto && proto.set) proto.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return el;
  }
  const monthEl = setValue(monthSelector, month);
  const yearEl = setValue(yearSelector, year);
  if (!monthEl || !yearEl) return false;
  // A year edit can clear the month. Put the month back without another year edit.
  if (String(monthEl.value || "") !== String(month)) {
    const proto = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
    if (proto && proto.set) proto.set.call(monthEl, month);
    else monthEl.value = month;
    monthEl.dispatchEvent(new Event("input", { bubbles: true }));
  }
  const field = monthEl.closest("[data-automation-id^='formField']") || monthEl.parentElement;
  const text = field ? String(field.innerText || field.textContent || "") : "";
  const wanted = "current value is " + month + "/" + year;
  return text.toLowerCase().indexOf(wanted.toLowerCase()) !== -1;
}"""


def fill_split_dates(page, profile: CandidateProfile, snapshot: PageSnapshot) -> set[str]:
    """Write each start/end pair from the matching profile row.

    Every split input is returned so the generic text fill does not type a
    whole date into the month box. A pair is written only when the profile
    has a month and year and the field then reads ``current value is MM/YYYY``.
    """
    groups: dict[tuple[str, str, str], dict[str, Control]] = {}
    order: dict[str, list[str]] = {"work": [], "education": []}
    for control in snapshot.controls:
        if control.hidden or not control.selector:
            continue
        parsed = split_date_part(control)
        if parsed is None:
            continue
        row_key, which, part = parsed
        bucket = _bucket(row_key)
        groups.setdefault((bucket, row_key, which), {})[part] = control
        if row_key not in order[bucket]:
            order[bucket].append(row_key)

    handled: set[str] = set()
    evaluate = getattr(page, "evaluate", None)
    for (bucket, row_key, which), parts in groups.items():
        selectors = [control.selector for control in parts.values() if control.selector]
        handled.update(selectors)
        month_control = parts.get("month")
        year_control = parts.get("year")
        if month_control is None or year_control is None or evaluate is None:
            continue
        index = order[bucket].index(row_key)
        rows = profile.employment if bucket == "work" else profile.education_history
        if index < 0 or index >= len(rows):
            continue
        row = rows[index]
        if bucket == "work" and which == "end" and getattr(row, "current", False):
            continue
        raw = row.start_date if which == "start" else row.end_date
        parsed_date = parse_month_year(raw)
        if parsed_date is None:
            continue
        month, year = parsed_date
        shown_month = f"{month:02d}"
        shown_year = str(year)
        try:
            wrote = bool(
                evaluate(
                    _WRITE_PAIR_JS,
                    {
                        "monthSelector": month_control.selector,
                        "yearSelector": year_control.selector,
                        "month": shown_month,
                        "year": shown_year,
                    },
                )
            )
        except Exception:
            wrote = False
        if not wrote:
            continue
    return handled


_READOUT_RE = re.compile(r"current value is\s+(\d{1,2})\s*/\s*(\d{4})", re.IGNORECASE)

_PAIR_COMMITTED_JS = r"""(selector) => {
  let el = null;
  try { el = document.querySelector(selector); } catch (error) { return false; }
  if (!el) return false;
  const field = el.closest("[data-automation-id^='formField']") || el.parentElement || el;
  const text = String(field.innerText || field.textContent || "");
  if (/current value is\s+\d{1,2}\s*\/\s*\d{4}/i.test(text)) return true;
  const months = field.querySelectorAll(
    "input[id*='dateSectionMonth'], input[data-automation-id='dateSectionMonth-input']"
  );
  const years = field.querySelectorAll(
    "input[id*='dateSectionYear'], input[data-automation-id='dateSectionYear-input']"
  );
  function monthOk(node) {
    return /^\d{1,2}$/.test(String(node.value || "").trim());
  }
  function yearOk(node) {
    return /^\d{4}$/.test(String(node.value || "").trim());
  }
  return Array.from(months).some(monthOk) && Array.from(years).some(yearOk);
}"""


def split_pair_committed(page, selector: str) -> bool:
    """True when this Workday month/year pair already shows a real date.

    A readout of ``current value is MM/YYYY`` counts, and so do numeric month
    and year inputs. An empty ``MM/YYYY`` placeholder does not.
    """
    if not selector:
        return False
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return False
    try:
        return bool(evaluate(_PAIR_COMMITTED_JS, selector))
    except Exception:
        return False


def date_groups(snapshot: PageSnapshot) -> list[dict[str, Any]]:
    """Split date pairs in page order. Each item is one From or To for one row."""
    groups: dict[tuple[str, str, str], dict[str, Control]] = {}
    order: list[tuple[str, str, str]] = []
    seen_rows: dict[str, list[str]] = {"work": [], "education": []}
    for control in snapshot.controls:
        if control.hidden or not control.selector:
            continue
        parsed = split_date_part(control)
        if parsed is None:
            continue
        row_key, which, part = parsed
        bucket = _bucket(row_key)
        key = (bucket, row_key, which)
        groups.setdefault(key, {})[part] = control
        if key not in order:
            order.append(key)
        if row_key not in seen_rows[bucket]:
            seen_rows[bucket].append(row_key)
    found: list[dict[str, object]] = []
    for bucket, row_key, which in order:
        parts = groups[(bucket, row_key, which)]
        index = seen_rows[bucket].index(row_key)
        selectors = [control.selector for control in parts.values() if control.selector]
        anchor = parts.get("month") or parts.get("year")
        found.append(
            {
                "bucket": bucket,
                "index": index,
                "which": which,
                "selectors": selectors,
                "anchor": anchor,
                "controls": list(parts.values()),
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


_WRITE_ANSWER_JS = r"""({ monthSelector, yearSelector, month, year }) => {
  function setValue(selector, value) {
    let el = null;
    try { el = document.querySelector(selector); } catch (error) { return null; }
    if (!el) return null;
    const proto = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
    if (proto && proto.set) proto.set.call(el, value);
    else el.value = value;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return el;
  }
  const monthEl = setValue(monthSelector, month);
  const yearEl = setValue(yearSelector, year);
  if (!monthEl || !yearEl) return false;
  if (String(monthEl.value || "") !== String(month)) {
    const proto = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
    if (proto && proto.set) proto.set.call(monthEl, month);
    else monthEl.value = month;
    monthEl.dispatchEvent(new Event("input", { bubbles: true }));
  }
  const field = monthEl.closest("[data-automation-id^='formField']") || monthEl.parentElement;
  const text = field ? String(field.innerText || field.textContent || "") : "";
  const wanted = "current value is " + month + "/" + year;
  if (text.toLowerCase().indexOf(wanted.toLowerCase()) !== -1) return true;
  return String(monthEl.value || "") === String(month) && String(yearEl.value || "") === String(year);
}"""


def _sibling_selector(selector: str, part: str) -> str:
    """The other half of a ``dateSectionMonth`` / ``dateSectionYear`` pair."""
    if part == "month":
        return re.sub(r"dateSectionYear", "dateSectionMonth", selector, count=1, flags=re.IGNORECASE)
    return re.sub(r"dateSectionMonth", "dateSectionYear", selector, count=1, flags=re.IGNORECASE)


def is_split_date_answer(target: dict) -> bool:
    """True when this answer belongs on one work or education date pair."""
    key = str(target.get("fieldKey") or "")
    if re.fullmatch(r"(employment|education)\.\d+\.(startDate|endDate)", key):
        return True
    selector = str(target.get("selector") or "")
    if not selector:
        return False
    return split_date_part(Control(kind="text", selector=selector, element_id=selector)) is not None


def commit_split_date_answer(page, target: dict) -> dict:
    """Write MM/YYYY into the month and year inputs of this entry only."""
    value = str(target.get("value") or "").strip()
    if not value:
        values = [str(item).strip() for item in target.get("values") or [] if str(item).strip()]
        value = values[0] if values else ""
    selector = str(target.get("selector") or "")
    parsed = parse_month_year(value)
    if not selector or parsed is None:
        return {"filled": False, "verified": False, "reason": "not_committed"}
    month, year = parsed
    shown_month = f"{month:02d}"
    shown_year = str(year)
    if re.search(r"dateSectionYear", selector, re.IGNORECASE):
        month_selector = _sibling_selector(selector, "month")
        year_selector = selector
    else:
        month_selector = selector
        year_selector = _sibling_selector(selector, "year")
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return {"filled": False, "verified": False, "reason": "not_committed"}
    try:
        wrote = bool(
            evaluate(
                _WRITE_ANSWER_JS,
                {
                    "monthSelector": month_selector,
                    "yearSelector": year_selector,
                    "month": shown_month,
                    "year": shown_year,
                },
            )
        )
    except Exception:
        wrote = False
    if not wrote:
        return {"filled": False, "verified": False, "reason": "not_committed"}
    return {"filled": True, "verified": True}
