"""Salary answers from the job description, then the profile floor.

A minimum is not turned into a range. A range is used only when both ends exist.
"""

import pytest

from autofill.profile import Compensation
from autofill.salary import (
    assess_salary,
    expectation_from_jd_salary,
    jd_salary_figure,
    resolve_salary,
    salary_fill_allowed,
    salary_shape,
)


def compensation() -> Compensation:
    return Compensation(
        salary_expectation="85000",
        salary_range_min="80000",
        salary_range_max="100000",
    )


def test_floor_midpoint_and_hourly():
    comp = compensation()
    assert resolve_salary(comp) == "85000"
    # Band midpoint is 120000. That sits between 100000 and 130000, so minus 5000.
    assert resolve_salary(comp, posted_min=100000, posted_max=140000) == "115000"
    # Band midpoint is 50000, at or below 100000, so the posted figure itself.
    assert resolve_salary(comp, posted_min=40000, posted_max=60000) == "50000"
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


def test_jd_expected_salary_rules():
    empty = Compensation()
    # At or below $100,000, including the boundary, the answer is the JD amount.
    assert expectation_from_jd_salary(80000) == 80000
    assert expectation_from_jd_salary(100000) == 100000
    assert resolve_salary(empty, posted_min=80000) == "80000"
    assert resolve_salary(empty, posted_min=100000, posted_max=100000) == "100000"
    assert resolve_salary(empty, posted_max=75000) == "75000"
    # Above $100,000 and below $130,000, subtract $5,000. $130,000 is the next rule.
    assert expectation_from_jd_salary(100001) == 95001
    assert expectation_from_jd_salary(120000) == 115000
    assert expectation_from_jd_salary(129999) == 124999
    assert resolve_salary(empty, posted_min=100001) == "95001"
    assert resolve_salary(empty, posted_min=120000) == "115000"
    assert resolve_salary(empty, posted_min=129999) == "124999"
    # At or above $130,000, subtract $10,000.
    assert expectation_from_jd_salary(130000) == 120000
    assert expectation_from_jd_salary(150000) == 140000
    assert resolve_salary(empty, posted_min=130000) == "120000"
    assert resolve_salary(empty, posted_min=150000) == "140000"
    # A posted band uses the midpoint, then the same rules.
    assert jd_salary_figure(100000, 140000) == 120000
    assert resolve_salary(empty, posted_min=100000, posted_max=140000) == "115000"
    assert jd_salary_figure(140000, 180000) == 160000
    assert resolve_salary(empty, posted_min=140000, posted_max=180000) == "150000"
    assert jd_salary_figure(90000, 110000) == 100000
    assert resolve_salary(empty, posted_min=90000, posted_max=110000) == "100000"
    # Unknown posting and no saved floor defaults to $90,000.
    assert jd_salary_figure(None, None) is None
    assert jd_salary_figure(0, -1) is None
    assert jd_salary_figure(140000, 100000) is None
    unknown = assess_salary(empty, shape="single")
    assert unknown.status == "answer"
    assert unknown.value == "90000"
    assert unknown.confidence == "HIGH"
    assert assess_salary(empty, shape="minimum").value == "90000"
    assert resolve_salary(empty) == "90000"
    assert resolve_salary(empty, posted_min=140000, posted_max=100000) == "90000"
    # An inverted band is not a figure, so a saved floor still answers.
    assert resolve_salary(Compensation(salary_expectation="85000"), posted_min=140000, posted_max=100000) == "85000"
    # A saved floor still answers when the job description is silent.
    assert resolve_salary(Compensation(salary_expectation="85000")) == "85000"
    assert resolve_salary(Compensation(salary_expectation="120000")) == "120000"
    # A minimum question uses the JD figure too, not the saved floor.
    minimum = assess_salary(Compensation(salary_range_min="90000"), shape="minimum", posted_min=150000)
    assert minimum.status == "answer"
    assert minimum.value == "140000"
    assert resolve_salary(empty, posted_min=120000, hourly=True) == str(115000 // 2080)


def test_jd_figure_does_not_invent_a_range_max():
    comp = Compensation(salary_expectation="120000")
    single = resolve_salary(comp, posted_min=150000)
    assert single == "140000"
    decision = assess_salary(comp, shape="range", posted_min=150000)
    assert decision.status == "confirm"
    assert decision.confidence == "LOW"
    assert decision.value == ""
    assert decision.reason == "salary_range_incomplete"
    assert decision.related_fields == ("salary_expectation",)
    ranged = resolve_salary(comp, posted_min=150000, as_range=True)
    assert ranged == ""
    assert ranged != f"{single}-{single}"
    # The unknown default is one number. It does not become a range or min-min.
    empty = assess_salary(Compensation(), shape="range")
    assert empty.status == "missing"
    assert empty.value == ""
    assert resolve_salary(Compensation(), as_range=True) == ""
    assert resolve_salary(Compensation(), as_range=True) != "90000-90000"
    # Both posted ends still answer a range with the existing band. The
    # discounted expectation is not written twice. The profile floor here is
    # 85000, under the widened low end, so the band stays 108000-132000.
    posted = resolve_salary(compensation(), posted_min=100000, posted_max=140000, as_range=True)
    assert posted == "108000-132000"
    assert posted != "115000-115000"


def test_saved_maximum_alone_still_confirms_a_single_question():
    decision = assess_salary(Compensation(salary_range_max="140000"), shape="single")
    assert decision.status == "confirm"
    assert decision.value == ""
    assert decision.value != "90000"


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
