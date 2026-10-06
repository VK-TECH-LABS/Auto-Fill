"""Compensation answers derived from the candidate profile.

The decision order follows ApplyPilot's salary section in
``src/applypilot/apply/prompt.py`` (floor, posted midpoint, range around
the midpoint, hourly = annual / 2080). ApplyPilot also hardcoded a senior-title
floor of $110,000. That number is a personal constant, not a general rule, so
it is not reproduced here. The floor is always the profile's
``salary_expectation``.
"""

from __future__ import annotations

from autofill.profile import Compensation

HOURS_PER_YEAR = 2080


def _as_int(value: str | int | None, fallback: int) -> int:
    if value is None or value == "":
        return fallback
    cleaned = str(value).replace(",", "").replace("$", "").strip()
    try:
        return int(cleaned)
    except ValueError as exc:
        raise ValueError(f"Salary value {value!r} is not a whole number.") from exc


def resolve_salary(
    compensation: Compensation,
    *,
    posted_min: int | None = None,
    posted_max: int | None = None,
    hourly: bool = False,
    as_range: bool = False,
) -> str:
    """Return a salary string that is never below the profile floor.

    Args:
        compensation: Profile compensation section.
        posted_min: Lower end of a range printed on the job posting, if known.
        posted_max: Upper end of that range, if known.
        hourly: Divide the annual answer by 2080.
        as_range: Return ``low-high`` instead of a single figure.
    """
    floor = _as_int(compensation.salary_expectation, 0)
    range_min = _as_int(compensation.salary_range_min, floor)
    range_max = _as_int(compensation.salary_range_max, floor)
    has_posting = posted_min is not None and posted_max is not None

    if as_range:
        if has_posting:
            midpoint = (int(posted_min) + int(posted_max)) // 2
            low = max(floor, int(midpoint * 0.9))
            high = max(low, int(midpoint * 1.1))
        else:
            low = max(floor, range_min)
            high = max(low, range_max)
        if hourly:
            return f"{low // HOURS_PER_YEAR}-{high // HOURS_PER_YEAR}"
        return f"{low}-{high}"

    if has_posting:
        annual = max(floor, (int(posted_min) + int(posted_max)) // 2)
    else:
        annual = floor
    if hourly:
        return str(annual // HOURS_PER_YEAR)
    return str(annual)
