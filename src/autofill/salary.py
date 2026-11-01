"""Compensation answers derived from the candidate profile and the job posting.

A single or minimum expected-salary question uses the job-description figure
when one is known. One listed number is that number. A posted band (the high
end above the low end) uses its midpoint, the same figure a single question
already took from a posted range. That figure is then adjusted:

- at or below $100,000, use it
- above $100,000 and below $130,000, subtract $5,000
- at or above $130,000, subtract $10,000

When the posting does not mention a salary, the floor is the profile's
``salary_expectation``, or ``salary_range_min`` when that is the only saved
minimum. When neither the posting nor the profile has a salary, a single or
minimum question defaults to $90,000.

ApplyPilot hardcoded a senior-title floor of $110,000. That number is a
personal constant, not a general rule, so it is not reproduced here.

A range is answered only when both ends are already known. The posted-band
range stays the midpoint widened by 10 percent and clamped up to the profile
floor. A minimum by itself is not a range: nothing here invents a maximum or
a ``min-min`` band. A job-description figure is one number and is not used as
the missing end of a range. Hourly answers divide a sufficient annual figure
by 2080.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from autofill.profile import Compensation

HOURS_PER_YEAR = 2080

# Expected salary taken from the job description. A listed figure at or below
# the first threshold is used as-is. Above it and below the high threshold,
# subtract the near discount. At or above the high threshold, subtract the
# high discount. No posting and no saved floor uses the unknown default.
JD_SALARY_SAME_THROUGH = 100_000
JD_SALARY_HIGH_FROM = 130_000
JD_SALARY_NEAR_DISCOUNT = 5_000
JD_SALARY_HIGH_DISCOUNT = 10_000
JD_SALARY_UNKNOWN = 90_000

# Profile keys a salary question can draw on. Values are never copied into
# the Companion payload; only these names are.
SALARY_RELATED_FIELDS: tuple[str, ...] = (
    "salary_expectation",
    "salary_range_min",
    "salary_range_max",
)

_AMOUNT = r"\$?\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.0+)?"
_RANGE_TEXT = re.compile(rf"^{_AMOUNT}\s*(?:-|–|—|\bto\b)\s*{_AMOUNT}$", re.IGNORECASE)


@dataclass(frozen=True)
class SalaryAssessment:
    """Whether one compensation question can be answered from saved data.

    ``status`` is ``answer`` (safe to write), ``confirm`` (related data exists
    or the question is a range the saved data does not cover), or ``missing``.
    ``confidence`` is ``HIGH`` only for an answer. A confirm challenge stays
    ``LOW`` so a person completes it.
    """

    status: str
    value: str = ""
    confidence: str = "LOW"
    related_fields: tuple[str, ...] = ()
    reason: str = ""
    shape: str = "single"


def _fold(text: str) -> str:
    lowered = text.casefold().replace("_", " ").replace("-", " ").replace("/", " ")
    cleaned = re.sub(r"[^a-z0-9+\s]", " ", lowered)
    return re.sub(r"\s+", " ", cleaned).strip()


def salary_shape(text: str) -> str:
    """``range``, ``minimum``, ``maximum``, or ``single``.

    "Desired salary range" is a range. "Minimum salary" is one number. A label
    that only names one end of a range, such as "Salary range minimum", is that
    end and not the whole band.
    """
    folded = _fold(text)
    has_range = bool(re.search(r"\brange\b|\bbetween\b", folded))
    has_min = bool(re.search(r"\b(?:min|minimum|lower|floor|at least)\b", folded))
    has_max = bool(re.search(r"\b(?:max|maximum|upper|ceiling|at most)\b", folded))
    if has_min and has_max:
        return "range"
    if re.search(r"\b(?:min|minimum|lower)\b$", folded):
        return "minimum"
    if re.search(r"\b(?:max|maximum|upper)\b$", folded):
        return "maximum"
    if has_range:
        return "range"
    if has_max:
        return "maximum"
    if has_min:
        return "minimum"
    return "single"


def _parse(value: str | int | None, *, strict: bool) -> int | None:
    """A positive whole-dollar amount, or None when the field is empty.

    A non-empty value that is not a whole number raises in strict mode. That
    is the profile path. Resolver text that is not a number is simply absent.
    """
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    cleaned = raw.replace(",", "").replace("$", "").strip()
    if re.fullmatch(r"\d+\.0+", cleaned):
        cleaned = cleaned.split(".", 1)[0]
    try:
        number = int(cleaned)
    except ValueError as exc:
        if strict:
            raise ValueError(f"Salary value {value!r} is not a whole number.") from exc
        return None
    if number <= 0:
        return None
    return number


def _range_ends(value: str) -> tuple[int, int] | None:
    found = _RANGE_TEXT.fullmatch(value.strip())
    if found is None:
        return None
    low = _parse(found.group(1), strict=False)
    high = _parse(found.group(2), strict=False)
    if low is None or high is None:
        return None
    return low, high


def salary_value_covers(value: str, shape: str) -> bool:
    """True when ``value`` is enough for this question shape.

    A single number does not cover a range. A band whose ends are equal does
    not cover a range either. No maximum is inferred.
    """
    text = str(value or "").strip()
    if not text:
        return False
    if (shape or "single") == "range":
        ends = _range_ends(text)
        return ends is not None and ends[1] > ends[0]
    return _parse(text, strict=False) is not None


def salary_fill_allowed(question: str, value: str, *, confidence: str) -> bool:
    """True only for a HIGH value that covers the question's salary shape."""
    if confidence != "HIGH":
        return False
    return salary_value_covers(value, salary_shape(question))


def _positive_amount(value: int | None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def jd_salary_figure(posted_min: int | None, posted_max: int | None) -> int | None:
    """The job-description amount the expectation rules apply to.

    One positive end, or two equal ends, is that listed figure. A band whose
    high end is above the low end uses the midpoint. An inverted band is not
    a figure. Missing ends are unknown.
    """
    low = _positive_amount(posted_min)
    high = _positive_amount(posted_max)
    if low is not None and high is not None:
        if high < low:
            return None
        return (low + high) // 2
    if low is not None:
        return low
    return high


def expectation_from_jd_salary(amount: int) -> int:
    """Expected salary from one job-description figure.

    ``amount`` at or below $100,000 stays. Above that and below $130,000
    subtracts $5,000. At or above $130,000 subtracts $10,000.
    """
    if amount <= JD_SALARY_SAME_THROUGH:
        return amount
    if amount < JD_SALARY_HIGH_FROM:
        return amount - JD_SALARY_NEAR_DISCOUNT
    return amount - JD_SALARY_HIGH_DISCOUNT


def _format_annual(annual: int, *, hourly: bool) -> str:
    if hourly:
        annual //= HOURS_PER_YEAR
    if annual <= 0:
        return ""
    return str(annual)


def _format_range(low: int, high: int, *, hourly: bool) -> str:
    if hourly:
        low //= HOURS_PER_YEAR
        high //= HOURS_PER_YEAR
    if high <= low or low <= 0:
        return ""
    return f"{low}-{high}"


def assess_salary(
    compensation: Compensation,
    *,
    shape: str = "single",
    posted_min: int | None = None,
    posted_max: int | None = None,
    hourly: bool = False,
) -> SalaryAssessment:
    """Decide whether saved compensation covers this question.

    A posted band answers a range only when the caller supplied both ends and,
    after the floor is applied, the high end is still above the low end. A
    single or minimum question uses :func:`expectation_from_jd_salary` on
    :func:`jd_salary_figure` when the posting names a figure. The profile
    maximum is used only when it is saved and is above the floor. A minimum
    alone confirms a range question instead of becoming ``min-min``.
    """
    expectation = _parse(compensation.salary_expectation, strict=True)
    range_min = _parse(compensation.salary_range_min, strict=True)
    range_max = _parse(compensation.salary_range_max, strict=True)
    related = tuple(
        name
        for name, present in (
            ("salary_expectation", expectation is not None),
            ("salary_range_min", range_min is not None),
            ("salary_range_max", range_max is not None),
        )
        if present
    )
    floors = [number for number in (expectation, range_min) if number is not None]
    floor = max(floors) if floors else None
    posting: tuple[int, int] | None = None
    if (
        posted_min is not None
        and posted_max is not None
        and posted_min > 0
        and posted_max >= posted_min
    ):
        posting = (posted_min, posted_max)
    asked = shape or "single"

    def confirm(reason: str) -> SalaryAssessment:
        return SalaryAssessment("confirm", "", "LOW", related, reason, asked)

    def missing() -> SalaryAssessment:
        return SalaryAssessment("missing", "", "LOW", (), "salary_missing", asked)

    def answer(value: str, used: tuple[str, ...]) -> SalaryAssessment:
        return SalaryAssessment("answer", value, "HIGH", used, "", asked)

    if asked == "range":
        if posting is not None:
            low_post, high_post = posting
            midpoint = (low_post + high_post) // 2
            low = int(midpoint * 0.9)
            high = int(midpoint * 1.1)
            if floor is not None:
                low = max(floor, low)
            rendered = _format_range(low, high, hourly=hourly)
            if rendered:
                return answer(rendered, related)
            if related or floor is not None:
                return confirm("salary_range_incomplete")
            return missing()
        if range_max is not None and floor is not None:
            rendered = _format_range(floor, range_max, hourly=hourly)
            if rendered:
                return answer(rendered, related)
        if related:
            return confirm("salary_range_incomplete")
        return missing()

    if asked == "maximum":
        if range_max is None:
            return confirm("salary_needs_confirm") if related else missing()
        if floor is not None and range_max < floor:
            return confirm("salary_needs_confirm")
        rendered = _format_annual(range_max, hourly=hourly)
        if not rendered:
            return confirm("salary_needs_confirm") if related else missing()
        return answer(rendered, ("salary_range_max",))

    # single or minimum. A known JD figure is the answer, including when it
    # sits under the saved floor. The range branch above does not use it as
    # a second end. No JD figure falls through to the saved floor, then to
    # the unknown default. A saved maximum alone still confirms: it is not
    # an expectation, and the default must not replace it.
    figure = jd_salary_figure(posted_min, posted_max)
    if figure is not None:
        rendered = _format_annual(expectation_from_jd_salary(figure), hourly=hourly)
        if not rendered:
            return confirm("salary_needs_confirm") if related else missing()
        return answer(rendered, ())
    if floor is None:
        if related:
            return confirm("salary_needs_confirm")
        rendered = _format_annual(JD_SALARY_UNKNOWN, hourly=hourly)
        if not rendered:
            return missing()
        return answer(rendered, ())
    annual = floor
    used = tuple(name for name in ("salary_expectation", "salary_range_min") if name in related)
    rendered = _format_annual(annual, hourly=hourly)
    if not rendered:
        return confirm("salary_needs_confirm")
    return answer(rendered, used)


def resolve_salary(
    compensation: Compensation,
    *,
    posted_min: int | None = None,
    posted_max: int | None = None,
    hourly: bool = False,
    as_range: bool = False,
) -> str:
    """Return a salary string for this question, or empty when it cannot be filled.

    A single figure comes from the job description when the posting names one,
    otherwise from the profile floor, otherwise from the $90,000 default.
    An empty string means a range (or a maximum) the saved data does not cover.
    Callers leave that control for a person. A range is never fabricated from
    a minimum or from the default.

    Args:
        compensation: Profile compensation section.
        posted_min: Lower end of a range printed on the job posting, if known.
            Alone, this is the listed job-description salary.
        posted_max: Upper end of that range, if known.
        hourly: Divide a sufficient annual answer by 2080.
        as_range: The question asks for ``low-high`` instead of one figure.
    """
    decision = assess_salary(
        compensation,
        shape="range" if as_range else "single",
        posted_min=posted_min,
        posted_max=posted_max,
        hourly=hourly,
    )
    if decision.status != "answer":
        return ""
    return decision.value
