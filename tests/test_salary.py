"""Salary answers stay at or above the profile floor.

A minimum is not turned into a range. A range is used only when both ends exist.
"""

import pytest

from autofill.profile import Compensation
from autofill.salary import assess_salary, resolve_salary, salary_fill_allowed, salary_shape


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


def test_minimum_alone_is_not_a_range():
    comp = Compensation(salary_expectation="120000")
    assert resolve_salary(comp) == "120000"
    assert resolve_salary(comp, as_range=True) == ""
    assert resolve_salary(comp, as_range=True) != "120000-120000"
    decision = assess_salary(comp, shape="range")
    assert decision.status == "confirm"
    assert decision.confidence == "LOW"
    assert decision.value == ""
    assert decision.reason == "salary_range_incomplete"
    assert decision.related_fields == ("salary_expectation",)


def test_saved_minimum_covers_a_single_salary_question():
    comp = Compensation(salary_range_min="90000")
    decision = assess_salary(comp, shape="single")
    assert decision.status == "answer"
    assert decision.value == "90000"
    assert decision.confidence == "HIGH"
    minimum = assess_salary(comp, shape="minimum")
    assert minimum.status == "answer"
    assert minimum.value == "90000"


def test_profile_range_is_used_when_both_ends_exist():
    comp = Compensation(salary_range_min="90000", salary_range_max="110000")
    assert resolve_salary(comp, as_range=True) == "90000-110000"
    decision = assess_salary(comp, shape="range")
    assert decision.status == "answer"
    assert decision.value == "90000-110000"


def test_posted_band_can_answer_a_range_without_inventing_a_max():
    comp = Compensation(salary_expectation="85000")
    assert resolve_salary(comp, posted_min=100000, posted_max=140000, as_range=True) == "108000-132000"
    collapsed = assess_salary(comp, shape="range", posted_min=40000, posted_max=60000)
    assert collapsed.status == "confirm"
    assert collapsed.value == ""
    assert resolve_salary(comp, posted_min=40000, posted_max=60000, as_range=True) == ""


def test_maximum_is_not_copied_from_the_minimum():
    comp = Compensation(salary_expectation="120000")
    decision = assess_salary(comp, shape="maximum")
    assert decision.status == "confirm"
    assert decision.value == ""
    stored = assess_salary(Compensation(salary_range_max="140000"), shape="maximum")
    assert stored.status == "answer"
    assert stored.value == "140000"


def test_equal_ends_do_not_count_as_a_range():
    comp = Compensation(salary_range_min="100000", salary_range_max="100000")
    assert assess_salary(comp, shape="range").status == "confirm"
    assert resolve_salary(comp, as_range=True) == ""


def test_salary_shape_and_resolver_coverage():
    assert salary_shape("What is your desired salary range?") == "range"
    assert salary_shape("Minimum salary") == "minimum"
    assert salary_shape("Desired salary") == "single"
    assert salary_shape("Salary range minimum") == "minimum"
    assert salary_fill_allowed("What is your desired salary range?", "120000-150000", confidence="HIGH")
    assert not salary_fill_allowed("What is your desired salary range?", "120000", confidence="HIGH")
    assert not salary_fill_allowed("What is your desired salary range?", "120000-120000", confidence="HIGH")
    assert not salary_fill_allowed("What is your desired salary range?", "120000-150000", confidence="MEDIUM")
    assert salary_fill_allowed("Desired salary", "120000", confidence="HIGH")
    assert not salary_fill_allowed("Desired salary", "120000", confidence="LOW")
