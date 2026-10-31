"""Checkbox and radio slices 1–4. Discovery, verified commit, the terms flag, groups, restore."""

from __future__ import annotations

from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from autofill.engine import AutofillOptions, autofill_application
from autofill.extract import extract_page
from autofill.fill import apply_mapped
from autofill.mapping import map_field, terms_category
from autofill.models import Control, MappedField, Option
from autofill.profile import CandidateProfile
from autofill.safeguards import commit_native_choice, press_aria_radio
from autofill.stepfill import _clusters, _describe, post_upload_refill

FIXTURES = Path(__file__).parent / "fixtures"


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {
            "candidateId": "ref-a",
            "personal": {
                "full_name": "Casey Example",
                "email": "casey.example@example.com",
                "phone": "555-010-0199",
            },
            "work_authorization": {"legally_authorized_to_work": "Yes", "require_sponsorship": "No"},
            "screening": {"willing_background_check": "Yes"},
        }
    )


@pytest.fixture
def page():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        tab = browser.new_page()
        try:
            yield tab
        finally:
            browser.close()


def test_slice1_reports_aria_and_unnamed_radios_without_clicking(page):
    page.set_content((FIXTURES / "aria_boolean_widgets.html").read_text())
    snapshot = extract_page(page)
    radios = [control for control in snapshot.controls if control.kind == "radio"]
    checks = [control for control in snapshot.controls if control.kind == "checkbox"]
    auth = next(control for control in radios if "authorized" in control.label.casefold())
    contact = next(control for control in radios if "contact" in (control.group or control.label).casefold())
    terms = next(control for control in checks if control.input_type == "aria-checkbox")
    switch = next(control for control in checks if control.input_type == "switch")
    assert auth.input_type == "aria-radio"
    assert auth.required is True
    assert {option.label for option in auth.options} == {"Yes", "No"}
    assert contact.input_type == "radio"
    assert {option.label for option in contact.options} == {"Email", "Phone"}
    assert terms.required is True
    assert switch.role == "switch"
    assert page.locator("#aria-yes").get_attribute("aria-checked") == "false"
    assert page.locator("#custom-box").get_attribute("aria-checked") == "false"
    gaps = [
        ask
        for kind, grouped in _clusters(snapshot)
        if (ask := _describe(kind, grouped, snapshot.heading, None)) is not None and ask.consent
    ]
    assert any("authorized" in (ask.controls[0].label or "").casefold() for ask in gaps) is False
    required = [control for control in (auth, terms) if control.required]
    assert len(required) == 2


def test_slice1_required_aria_radio_pauses_with_the_question(page):
    page.set_content((FIXTURES / "aria_boolean_widgets.html").read_text())
    snapshot = extract_page(page)
    asks = [
        ask
        for kind, grouped in _clusters(snapshot)
        if (ask := _describe(kind, grouped, "", None)) is not None and ask.include_question
    ]
    texts = [ask.question_body()["text"] for ask in asks]
    assert "Are you authorized to work in the United States?" in texts


def test_slice2_label_without_id_commits_and_refuses_a_button(page):
    page.set_content(
        "<div>"
        "<input type='checkbox' name='terms'>"
        "<label>I agree to the Terms of Service</label>"
        "</div>"
        "<button id='go' type='submit'>Submit application</button>"
    )
    selector = "input[type='checkbox'][name='terms']"
    assert commit_native_choice(page, selector, on=True) is True
    assert page.locator(selector).is_checked() is True
    assert commit_native_choice(page, "#go", on=True) is False
    assert press_aria_radio(page, "#go") is False


def test_slice2_covered_box_uses_the_label(page):
    page.set_content(
        "<label for='box' id='cover' style='display:inline-block;width:20px;height:20px'>"
        "I agree to the Terms of Service</label>"
        "<input id='box' type='checkbox' style='opacity:0;position:absolute;width:1px;height:1px'"
        " onclick='window.__hits=(window.__hits||0)+1'>"
    )
    assert commit_native_choice(page, "#box", on=True) is True
    assert page.locator("#box").is_checked() is True
    assert page.evaluate("() => window.__hits") >= 1


def test_slice3_flag_off_checks_nothing_and_flag_on_audits_standard_terms(page):
    page.set_content((FIXTURES / "checkbox_field_group.html").read_text())
    off = autofill_application(
        "https://jobs.example.test/apply",
        _profile(),
        None,
        AutofillOptions(page=page, session_id="sess-off", candidate_id="ref-a", standard_terms_authorized=False),
    )
    assert page.locator("input[name='terms']").is_checked() is False
    assert page.locator("#bg").is_checked() is False
    assert page.locator("#auth-yes").is_checked() is True
    assert off.auto_consents == []

    page.set_content((FIXTURES / "checkbox_field_group.html").read_text())
    on = autofill_application(
        "https://jobs.example.test/apply",
        _profile(),
        None,
        AutofillOptions(page=page, session_id="sess-on", candidate_id="ref-a", standard_terms_authorized=True),
    )
    assert page.locator("input[name='terms']").is_checked() is True
    assert page.locator("#bg").is_checked() is False
    assert page.locator("#auth-yes").is_checked() is True
    assert on.auto_consents
    assert on.auto_consents[0]["category"] == "STANDARD_TERMS"


def test_slice3_classifier_covers_radio_select_and_buttons():
    radio = Control(
        kind="radio",
        label="I agree to the Terms of Service",
        options=[Option("Yes", "Yes"), Option("No", "No")],
        required=True,
    )
    buttons = Control(
        kind="buttons",
        label="I agree to binding arbitration",
        options=[Option("Yes", "Yes"), Option("No", "No")],
        required=True,
    )
    work = Control(
        kind="radio",
        label="Are you authorized to work in the United States?",
        options=[Option("Yes", "Yes"), Option("No", "No")],
        required=True,
    )
    assert terms_category(radio) == "STANDARD_TERMS"
    assert terms_category(buttons) == "CONSENT"
    assert map_field(work, _profile()).action == "select"
    assert map_field(radio, _profile()).action == "skip"
    assert map_field(buttons, _profile()).action == "skip"


def test_slice4_form_field_checkboxes_are_one_question(page):
    page.set_content((FIXTURES / "checkbox_field_group.html").read_text())
    snapshot = extract_page(page)
    clusters = _clusters(snapshot)
    multi = [grouped for kind, grouped in clusters if kind == "multi"]
    assert any(len(group) == 2 and "shift" in (group[0].group or "").casefold() for group in multi)
    asks = [ask for kind, grouped in clusters if (ask := _describe(kind, grouped, "", None)) is not None]
    bodies = [ask.question_body() for ask in asks if ask.include_question]
    shifts = next(body for body in bodies if "shift" in body["text"].casefold())
    assert shifts["options"] == ["Day", "Night"]
    assert shifts["control"] == "multiselect"


def test_slice4_fills_factual_aria_yes_no_and_restores_factual_choices(page):
    page.set_content((FIXTURES / "aria_boolean_widgets.html").read_text())
    snapshot = extract_page(page)
    auth = next(control for control in snapshot.controls if control.input_type == "aria-radio")
    yes = next(option for option in auth.options if option.label == "Yes")
    outcome = apply_mapped(
        page,
        auth,
        MappedField(
            key="legally_authorized_to_work",
            action="select",
            option_label="Yes",
            option_selector=yes.selector,
            field_class="PROFILE_FIELD",
        ),
    )
    assert outcome.action == "select"
    assert page.locator("#aria-yes").get_attribute("aria-checked") == "true"

    page.set_content(
        "<label><input id='night' type='checkbox' name='night'> Can you work nights?</label>"
        "<fieldset><legend>Are you authorized to work in the United States?</legend>"
        "<label><input id='yes' type='radio' name='auth' value='Yes'> Yes</label>"
        "<label><input id='no' type='radio' name='auth' value='No'> No</label></fieldset>"
        "<label><input id='terms' type='checkbox'> I agree to the Terms of Service</label>"
    )
    records = [
        {
            "choice": "checkbox",
            "value": "yes",
            "label": "Can you work nights?",
            "name": "night",
            "elementId": "night",
            "selector": "#night",
            "fieldKey": "nights",
        },
        {
            "choice": "radio",
            "value": "Yes",
            "label": "Are you authorized to work in the United States?",
            "name": "auth",
            "elementId": "yes",
            "selector": "#yes",
            "fieldKey": "legally_authorized_to_work",
        },
        {
            "choice": "checkbox",
            "value": "yes",
            "label": "I agree to the Terms of Service",
            "name": "",
            "elementId": "terms",
            "selector": "#terms",
            "fieldKey": "STANDARD_TERMS",
        },
    ]
    page.locator("#night").uncheck()
    page.locator("#yes").uncheck()
    restored = post_upload_refill(page, confirmed=records, log=False)
    assert restored == 2
    assert page.locator("#night").is_checked() is True
    assert page.locator("#yes").is_checked() is True
    assert page.locator("#terms").is_checked() is False
