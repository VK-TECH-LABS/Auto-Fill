"""Salary answers stay at or above the profile floor."""

import pytest

from autofill.profile import Compensation
from autofill.salary import resolve_salary


def compensation() -> Compensation:
    return Compensation(
        salary_expectation="85000",
        salary_range_min="80000",
        salary_range_max="100000",
    )


def test_floor_midpoint_and_hourly():
    comp = compensation()
    assert resolve_salary(comp) == "85000"
    assert resolve_salary(comp, posted_min=100000, posted_max=140000) == "120000"
    assert resolve_salary(comp, posted_min=40000, posted_max=60000) == "85000"
    assert resolve_salary(comp, hourly=True) == "40"
    assert resolve_salary(comp, as_range=True) == "85000-100000"
    posted_range = resolve_salary(comp, posted_min=100000, posted_max=140000, as_range=True)
    assert posted_range == "108000-132000"


def test_non_numeric_salary_is_rejected():
    with pytest.raises(ValueError):
        resolve_salary(Compensation(salary_expectation="eighty thousand"))
