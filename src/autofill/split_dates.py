"""Workday month and year inputs that together are one date.

``dateSectionMonth-input`` and ``dateSectionYear-input`` under a
``--startDate`` or ``--endDate`` id are filled as one pair. A current job
leaves the end pair empty. An answer for one part does not erase the other.
"""

from __future__ import annotations

import re

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
