"""Format an explicit profile date for the control that asked for it.

Values that are not dates, such as "Immediately", are returned unchanged for
text fields and refused for ``<input type="date">``. Nothing is invented.
"""

from __future__ import annotations

import re

_ISO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def parse_month_year(value: str) -> tuple[int, int] | None:
    """Month and year from ``DD/MM/YYYY``, ``MM/YYYY``, or an ISO date.

    A three-part numeric date is day/month/year. ``01/10/2023`` is October
    2023. A two-part date is month/year. Nothing is guessed from the day.
    """
    text = (value or "").strip()
    if not text:
        return None
    iso = re.fullmatch(r"(\d{4})-(\d{2})(?:-(\d{2}))?", text)
    if iso:
        year, month = int(iso.group(1)), int(iso.group(2))
        if 1 <= month <= 12:
            return month, year
        return None
    three = re.fullmatch(r"(\d{1,2})[./-](\d{1,2})[./-](\d{4})", text)
    if three:
        day, month, year = int(three.group(1)), int(three.group(2)), int(three.group(3))
        if 1 <= day <= 31 and 1 <= month <= 12:
            return month, year
        return None
    two = re.fullmatch(r"(\d{1,2})[./-](\d{4})", text)
    if two:
        month, year = int(two.group(1)), int(two.group(2))
        if 1 <= month <= 12:
            return month, year
    return None


def format_for_control(value: str, *, input_type: str = "", placeholder: str = "", label: str = "") -> str | None:
    """Return ``value`` in the control's format, or None when it does not fit."""
    text = value.strip()
    match = _ISO.fullmatch(text)
    kind = input_type.casefold()
    hint = f"{placeholder} {label}".casefold()
    if kind == "date":
        return text if match else None
    if kind in {"month"} and match:
        return f"{match.group(1)}-{match.group(2)}"
    if not match:
        return text
    year, month, day = match.group(1), match.group(2), match.group(3)
    if "mm/dd/yyyy" in hint:
        return f"{month}/{day}/{year}"
    if "dd/mm/yyyy" in hint:
        return f"{day}/{month}/{year}"
    if "yyyy-mm-dd" in hint:
        return f"{year}-{month}-{day}"
    if "month" in hint and "day" not in hint and "dd" not in hint:
        return f"{_MONTHS[int(month) - 1]} {year}"
    return text
