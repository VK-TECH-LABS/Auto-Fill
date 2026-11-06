"""Dropdown commit, dependent state, async menus, and disabled or stray options.

Synthetic pages only. Nothing here clicks Submit or calls a live ATS.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from autofill.combobox import select_combobox
from autofill.dom_ui import commit_visible
from autofill.extract import extract_page
from autofill.models import Option
from autofill.resolver import ResolverBinding, ResolverResponse
from autofill.stepfill import fill_resolved_page, match_region_option

FIXTURES = Path(__file__).parent / "fixtures"


def test_region_option_uses_the_full_state_table():
    options = [
        Option(value="IL", label="Illinois", selector="#il"),
        Option(value="CA", label="California", selector="#ca"),
        Option(value="ON", label="Ontario", selector="#on"),
    ]
    illinois = match_region_option("IL", options)
    assert illinois is not None and illinois.label == "Illinois"
    california = match_region_option("California", options)
    assert california is not None and california.value == "CA"
    coded = match_region_option("CA", options)
    assert coded is not None and coded.label == "California"
    texas = match_region_option("tx", [Option(value="TX", label="Texas")])
    assert texas is not None and texas.label == "Texas"
    ontario = match_region_option("Ontario", options)
    assert ontario is not None and ontario.label == "Ontario"
    assert match_region_option("", options) is None


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        chromium = playwright.chromium.launch(headless=True)
        try:
            yield chromium
        finally:
            chromium.close()


def _binding() -> ResolverBinding:
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    return ResolverBinding(
        url="https://resolver.example.test/v1/resolve",
        token="token",
        expires_at=expires,
        candidate_ref="ref-a",
        job_ref="job-a",
    )


def _resolve(monkeypatch, answer: dict[str, str]):
    saved = dict(answer)

    def resolve(binding, *, session_id, step, fields, questions):
        del binding, session_id, step, questions
        assert "address.country" in fields
        return ResolverResponse(fields=dict(saved))

    monkeypatch.setattr("autofill.stepfill.resolve_step", resolve)


def test_noop_combobox_click_is_not_a_commit(browser):
    page = browser.new_page()
    try:
        page.set_content(
            "<label for='box'>Employment type</label>"
            "<input id='box' role='combobox' aria-controls='list'>"
            "<ul id='list' role='listbox'>"
            "<li id='full' role='option'>Full-time</li>"
            "</ul>"
            "<script>"
            "window.__clicked = 0;"
            "document.getElementById('full').addEventListener('click', () => { window.__clicked += 1; });"
            "document.getElementById('box').addEventListener('keydown', (event) => {"
            "  if (event.key !== 'Enter') return;"
            "  event.preventDefault();"
            "  event.target.value = '';"
            "});"
            "</script>"
        )
        assert select_combobox(page, "#box", "Full-time") is None
        assert page.locator("#box").input_value() == ""
        assert page.evaluate("() => window.__clicked") >= 1
    finally:
        page.close()


def test_answer_path_sets_a_controlled_select(browser):
    page = browser.new_page()
    try:
        page.set_content(
            "<select id='color'>"
            "<option value=''>Select...</option>"
            "<option value='red'>Red</option>"
            "<option value='blue'>Blue</option>"
            "</select>"
            "<script>"
            "const select = document.getElementById('color');"
            "const desc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');"
            "window.__direct = 0;"
            "window.__changes = 0;"
            "Object.defineProperty(select, 'value', {"
            "  configurable: true,"
            "  get() { return desc.get.call(select); },"
            "  set() { window.__direct += 1; }"
            "});"
            "select.addEventListener('change', () => { window.__changes += 1; });"
            "</script>"
        )
        result = commit_visible(
            page,
            {"selector": "#color", "value": "Blue", "answerType": "single_select"},
        )
        assert result["filled"] is True
        assert page.locator("#color").input_value() == "blue"
        assert page.evaluate("() => window.__changes") >= 1
    finally:
        page.close()


def test_disabled_option_is_skipped_and_a_stray_list_is_not_used(browser):
    page = browser.new_page()
    try:
        page.set_content(
            "<label for='country'>Country</label>"
            "<select id='country'>"
            "<option value=''>Select...</option>"
            "<option value='US' disabled>United States</option>"
            "<option value='USA'>United States of America</option>"
            "</select>"
            "<input id='combo' role='combobox' aria-controls='combo-list' aria-label='Country'>"
            "<ul id='combo-list' role='listbox'>"
            "<li id='disabled-us' role='option' aria-disabled='true'>United States</li>"
            "<li id='enabled-us' role='option'>United States of America</li>"
            "</ul>"
            "<input type='hidden' id='combo-hidden'>"
            "<ul id='stray' role='listbox' style='display:block;min-height:24px'>"
            "<li id='stray-us' role='option' style='height:24px'>United States</li>"
            "</ul>"
            "<script>"
            "window.__picked = [];"
            "document.getElementById('disabled-us').addEventListener('click', () => {"
            "  window.__picked.push('disabled');"
            "  document.getElementById('combo-hidden').value = 'DISABLED';"
            "});"
            "document.getElementById('enabled-us').addEventListener('click', () => {"
            "  window.__picked.push('enabled');"
            "  document.getElementById('combo-hidden').value = 'United States of America';"
            "});"
            "document.getElementById('stray-us').addEventListener('click', () => window.__picked.push('stray'));"
            "</script>"
        )
        labels = [
            option.label
            for control in extract_page(page).controls
            if control.element_id == "country"
            for option in control.options
        ]
        assert "United States" not in labels
        assert "United States of America" in labels
        missed = commit_visible(
            page,
            {"selector": "#country", "value": "United States", "answerType": "single_select"},
        )
        assert missed["filled"] is False
        assert page.locator("#country").input_value() == ""
        chosen = commit_visible(
            page,
            {"selector": "#country", "value": "United States of America", "answerType": "single_select"},
        )
        assert chosen["filled"] is True
        assert page.locator("#country").input_value() == "USA"
        picked = select_combobox(page, "#combo", "United States")
        assert picked is not None
        assert picked.label == "United States of America"
        assert page.evaluate("() => window.__picked") == ["enabled"]
        assert page.locator("#combo-hidden").input_value() == "United States of America"
    finally:
        page.close()


def test_state_enables_after_country(browser, monkeypatch):
    _resolve(monkeypatch, {"address.country": "United States", "address.region": "CA"})
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "dependent_state.html").as_uri())
        outcome = fill_resolved_page(page, _binding(), session_id="session-a", ats_name="workday", step="address")
        assert page.locator("#country").input_value() == "United States"
        assert page.locator("#state").is_disabled() is False
        assert page.locator("#state").input_value() == "CA"
        texts = [str(item.get("text") or "") for item in outcome.manual_questions]
        assert not any(text.casefold() == "state" for text in texts)
    finally:
        page.close()


def test_state_options_arrive_after_country(browser, monkeypatch):
    _resolve(monkeypatch, {"address.country": "United States", "address.region": "Illinois"})
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "dependent_state_options.html").as_uri())
        outcome = fill_resolved_page(page, _binding(), session_id="session-a", ats_name="greenhouse", step="address")
        assert page.locator("#state").input_value() == "IL"
        texts = [str(item.get("text") or "") for item in outcome.manual_questions]
        assert not any(text.casefold() == "state" for text in texts)
    finally:
        page.close()


def test_greenhouse_portal_waits_and_confirms_with_the_keyboard(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "greenhouse_portal_menu.html").as_uri())
        picked = select_combobox(page, "#color", "Blue")
        assert picked is not None
        assert picked.label == "Blue"
        assert page.locator("#color-hidden").input_value() == "Blue"
        assert page.evaluate("() => window.__picked") == ["color-blue"]
    finally:
        page.close()


def test_workday_prompt_opens_from_the_icon_and_ignores_a_stray_list(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "workday_prompt_async.html").as_uri())
        picked = select_combobox(page, "#source", "Job Board")
        assert picked is not None
        assert picked.label == "Job Board"
        assert "Job Board" in page.locator("#picked").inner_text()
        assert page.evaluate("() => window.__opened") == "prompt"
    finally:
        page.close()
