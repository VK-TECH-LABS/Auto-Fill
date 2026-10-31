"""Multi-page fixture harness. Real Greenhouse, Lever, and Ashby forms are one page."""

from __future__ import annotations

import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from autofill.engine import AutofillOptions, Status, autofill_application
from autofill.profile import CandidateProfile

FIXTURES = Path(__file__).parent / "fixtures"
EMAIL = "river.example@example.com"


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {"candidateId": "ref-a", "personal": {"full_name": "River Example", "email": EMAIL}}
    )


def _run(page, url: str):
    return autofill_application(
        url,
        _profile(),
        None,
        AutofillOptions(page=page, session_id="sess-harness", candidate_id="ref-a", timings={}),
    )


@pytest.fixture(scope="module")
def fixture_origin():
    handler = partial(SimpleHTTPRequestHandler, directory=str(FIXTURES))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def browser():
    try:
        with sync_playwright() as playwright:
            chromium = playwright.chromium.launch(headless=True)
            try:
                yield chromium
            finally:
                chromium.close()
    except Exception as exc:
        pytest.skip(f"Chromium is not installed: {exc}")


def test_multistep_next_then_review_stops_before_submit(browser, fixture_origin):
    page = browser.new_page()
    try:
        page.goto(f"{fixture_origin}/multistep_pages.html")
        result = _run(page, f"{fixture_origin}/multistep_pages.html")
        clicked = page.evaluate("() => window.__clicked")
        assert clicked == ["Next", "Save & Continue"]
        assert "Submit application" not in clicked
        assert "Send" not in clicked
        assert result.status != Status.NAVIGATION_STALLED
        assert result.next_clicks >= 2
        assert result.pages_advanced >= 2
        assert result.last_navigation["action"] == "next"
        assert result.last_navigation["result"] == "advanced"
        assert result.stopped_before_submit is True
    finally:
        page.close()


def test_stalled_apply_is_one_click(browser, fixture_origin):
    page = browser.new_page()
    try:
        page.goto(f"{fixture_origin}/stalled_apply.html")
        result = _run(page, f"{fixture_origin}/stalled_apply.html")
        assert result.status == Status.NAVIGATION_STALLED
        assert result.messages == ["navigation_stalled"]
        assert result.entry_clicks == 1
        assert result.pages_advanced == 0
        assert result.last_navigation == {"action": "entry", "result": "stalled"}
        assert page.evaluate("() => window.__clicked") == ["Apply"]
    finally:
        page.close()


def test_validation_error_does_not_advance(browser, fixture_origin):
    page = browser.new_page()
    try:
        page.goto(f"{fixture_origin}/validation_error.html")
        result = _run(page, f"{fixture_origin}/validation_error.html")
        assert result.status == Status.MANUAL_ANSWER_REQUIRED
        assert result.messages == ["This field is required"]
        assert result.next_clicks == 0
        assert result.pages_advanced == 0
        assert page.evaluate("() => window.__clicked") == []
    finally:
        page.close()


def test_apply_opens_a_new_tab(browser, fixture_origin):
    page = browser.new_page()
    try:
        page.goto(f"{fixture_origin}/apply_new_tab.html")
        result = _run(page, f"{fixture_origin}/apply_new_tab.html")
        assert result.entry_clicks == 1
        assert result.pages_advanced >= 1
        assert result.last_navigation["result"] in {"new_page", "advanced"}
        popup = next(item for item in page.context.pages if item is not page)
        assert popup.locator("#email").input_value() == EMAIL
        assert result.stopped_before_submit is True
    finally:
        page.context.close()


def test_apply_inside_an_iframe(browser, fixture_origin):
    page = browser.new_page()
    try:
        page.goto(f"{fixture_origin}/apply_iframe_job.html")
        page.frame_locator("#apply-frame").locator("#apply").wait_for()
        result = _run(page, f"{fixture_origin}/apply_iframe_job.html")
        assert result.status != Status.LOGIN_REQUIRED
        assert page.evaluate("() => window.__clicked") == ["Apply"]
        assert page.locator("#email").input_value() == EMAIL
        assert result.entry_clicks == 1
        assert result.stopped_before_submit is True
    finally:
        page.close()


def test_recorded_next_is_not_pressed_again(browser, fixture_origin):
    """A Next already recorded for this page is not clicked on the continue."""
    from autofill.extract import extract_page
    from autofill.pagewait import navigation_signature

    page = browser.new_page()
    url = f"{fixture_origin}/multistep_pages.html"
    try:
        page.goto(url)
        snapshot = extract_page(page)
        button = next(item for item in snapshot.buttons if (item.name or "").strip() == "Next")
        block = (button.selector, button.name, navigation_signature(page, snapshot))
        result = autofill_application(
            url,
            _profile(),
            None,
            AutofillOptions(
                page=page,
                nav_block=block,
                session_id="sess-harness",
                candidate_id="ref-a",
                timings={},
            ),
        )
        clicked = page.evaluate("() => window.__clicked")
        assert clicked == []
        assert "Submit application" not in clicked
        assert "Send" not in clicked
        assert result.stopped_before_submit is True
    finally:
        page.close()


def test_dropped_second_page_continues_without_clicking_next_again(browser, fixture_origin):
    """A crash on page 2 resumes there. The recorded block is the page Next left."""
    from autofill.extract import extract_page
    from autofill.pagewait import navigation_signature

    page = browser.new_page()
    url = f"{fixture_origin}/multistep_pages.html"
    seen = {"n": 0}
    blocks: list[tuple] = []
    first_signature: list[tuple] = []

    def observe(current) -> None:
        seen["n"] += 1
        if seen["n"] == 1:
            first_signature.append(navigation_signature(current, extract_page(current)))
        if seen["n"] == 2:
            raise RuntimeError("Target closed")

    def remember(block: tuple) -> None:
        blocks.append(block)

    try:
        page.goto(url)
        with pytest.raises(RuntimeError, match="Target closed"):
            autofill_application(
                url,
                _profile(),
                None,
                AutofillOptions(
                    page=page,
                    on_observed=observe,
                    on_nav_block=remember,
                    session_id="sess-harness",
                    candidate_id="ref-a",
                    timings={},
                ),
            )
        assert page.evaluate("() => window.__clicked") == ["Next"]
        assert blocks
        assert blocks[-1][2] == first_signature[0]
        result = autofill_application(
            url,
            _profile(),
            None,
            AutofillOptions(
                page=page,
                nav_block=blocks[-1],
                session_id="sess-harness",
                candidate_id="ref-a",
                timings={},
            ),
        )
        clicked = page.evaluate("() => window.__clicked")
        assert clicked.count("Next") == 1
        assert clicked.count("Save & Continue") == 1
        assert "Submit application" not in clicked
        assert "Send" not in clicked
        assert result.stopped_before_submit is True
    finally:
        page.close()


def test_stable_save_and_continue_advances_both_steps(browser, fixture_origin):
    page = browser.new_page()
    try:
        page.goto(f"{fixture_origin}/stable_next_button.html")
        result = _run(page, f"{fixture_origin}/stable_next_button.html")
        clicked = page.evaluate("() => window.__clicked")
        assert clicked == ["Save and Continue", "Save and Continue"]
        assert "Submit application" not in clicked
        assert result.next_clicks == 2
        assert result.pages_advanced == 2
        assert result.status != Status.NAVIGATION_STALLED
        assert result.stopped_before_submit is True
    finally:
        page.close()


def test_delayed_heading_is_one_advance(browser, fixture_origin):
    page = browser.new_page()
    try:
        page.goto(f"{fixture_origin}/delayed_heading.html")
        result = _run(page, f"{fixture_origin}/delayed_heading.html")
        assert page.evaluate("() => window.__clicked") == ["Next"]
        assert result.next_clicks == 1
        assert result.pages_advanced == 1
        assert result.status != Status.NAVIGATION_STALLED
        assert "Submit application" not in page.evaluate("() => window.__clicked")
    finally:
        page.close()


def test_repeated_selectors_after_next_are_a_stall(browser, fixture_origin):
    page = browser.new_page()
    try:
        page.goto(f"{fixture_origin}/same_selector_steps.html")
        result = _run(page, f"{fixture_origin}/same_selector_steps.html")
        assert page.evaluate("() => window.__clicked") == ["Next"]
        assert result.status == Status.NAVIGATION_STALLED
        assert result.messages == ["navigation_stalled"]
        assert result.next_clicks == 1
    finally:
        page.close()


def test_heading_change_without_a_flagged_field_is_not_manual(browser, fixture_origin):
    page = browser.new_page()
    try:
        page.goto(f"{fixture_origin}/heading_without_invalid.html")
        result = _run(page, f"{fixture_origin}/heading_without_invalid.html")
        assert page.evaluate("() => window.__clicked") == ["Next"]
        assert result.pages_advanced == 1
        assert result.status != Status.MANUAL_ANSWER_REQUIRED
        assert result.stopped_before_submit is True
    finally:
        page.close()


def test_page_cap_fills_eight_steps_and_does_not_open_the_ninth(browser, fixture_origin):
    page = browser.new_page()
    try:
        page.goto(f"{fixture_origin}/nine_step_pages.html")
        result = _run(page, f"{fixture_origin}/nine_step_pages.html")
        assert page.evaluate("() => window.__step") == 8
        assert page.evaluate("() => window.__clicked") == ["Next"] * 7
        assert result.next_clicks == 7
        assert "Submit application" not in page.evaluate("() => window.__clicked")
        assert result.stopped_before_submit is True
    finally:
        page.close()


def test_filled_step_presses_next_once(browser, fixture_origin):
    from autofill.follow import follow_answer, read_page_mark

    page = browser.new_page()
    url = f"{fixture_origin}/two_questions_next.html"
    try:
        page.goto(url)
        previous, _snapshot = read_page_mark(page)
        page.fill("#color", "Blue")
        page.fill("#visa", "No")
        follow = follow_answer(
            page,
            profile=_profile(),
            job=None,
            held=set(),
            prior=[],
            previous=previous,
        )
        assert follow.changed is False
        assert follow.status == Status.FILLED
        assert follow.open_items == []
        result = autofill_application(
            url,
            _profile(),
            None,
            AutofillOptions(page=page, session_id="sess-harness", candidate_id="ref-a", timings={}),
        )
        clicked = page.evaluate("() => window.__clicked")
        assert clicked == ["Next"]
        assert result.stopped_before_submit is True
        assert result.status != Status.NAVIGATION_STALLED
    finally:
        page.close()
