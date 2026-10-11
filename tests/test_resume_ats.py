"""Resume confirmation against local Greenhouse, Lever, and Ashby DOM fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from autofill.resume_manual import _CONFIRM_JS, attach_and_confirm, confirm_current

FIXTURES = Path(__file__).parent / "fixtures"
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def _browser():
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
    except Exception as exc:
        pytest.skip(f"Chromium is not installed: {exc}")
    return sync_playwright


def test_greenhouse_lever_and_ashby_attachments_confirm():
    sync_playwright = _browser()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            for name in ("greenhouse_resume.html", "lever_resume.html", "ashby_resume.html"):
                page.goto((FIXTURES / name).as_uri())
                report = attach_and_confirm(page, name="notes.pdf", data=PDF)
                assert report["verified"] is True, (name, report)
                assert report["reason"] == ""
        finally:
            browser.close()


def test_replaced_greenhouse_input_is_found_after_scroll():
    sync_playwright = _browser()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto((FIXTURES / "greenhouse_swap.html").as_uri())
            for offset in (0, 240, 480):
                page.evaluate("(y) => window.scrollTo(0, y)", offset)
                report = attach_and_confirm(page, name="notes.pdf", data=PDF)
                assert report["reason"] != "no_upload_control", report
                assert report["verified"] is True, report
            page.goto((FIXTURES / "greenhouse_replaced.html").as_uri())
            held = confirm_current(page, filename="notes.pdf")
            assert held["verified"] is True, held
            assert held["reason"] != "no_upload_control"
            again = attach_and_confirm(page, name="notes.pdf", data=PDF)
            assert again["verified"] is True, again
        finally:
            browser.close()


def test_a_filename_without_an_attachment_indicator_is_not_confirmed():
    sync_playwright = _browser()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto((FIXTURES / "resume_filename_only.html").as_uri())
            once = page.evaluate(_CONFIRM_JS, {"selector": "#resume", "filename": "notes.pdf"})
            assert once["state"] == "wait"
            page.locator("#resume").set_input_files(
                [{"name": "notes.pdf", "mimeType": "application/pdf", "buffer": PDF}]
            )
            filled = page.evaluate(_CONFIRM_JS, {"selector": "#resume", "filename": "notes.pdf"})
            assert filled["state"] == "wait"
        finally:
            browser.close()
