"""A resume parse must not leave approved fields empty or a stale gap."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from autofill.credentials import MemoryCredentialProvider
from autofill.extract import extract_page
from autofill.fill import (
    apply_mapped,
    bind_confirmed,
    bind_session_id,
    confirmed_records,
    reset_confirmed,
    reset_session_id,
)
from autofill.models import Control, JobContext, MappedField
from autofill.profile import CandidateProfile
from autofill.resolver import ResolverBinding, ResolverResponse
from autofill.resume_fetch import ResumePayload
from autofill.resume_manual import reattach_verified
from autofill.service.sessions import ServiceSession
from autofill.stepfill import (
    _resume_accepted,
    fill_resolved_page,
    live_field_value,
    post_upload_refill,
    required_gaps,
)

FIXTURES = Path(__file__).parent / "fixtures"
PDF = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


def _binding() -> ResolverBinding:
    return ResolverBinding(
        url="https://resolver.example/v1",
        token="synthetic-token",
        expires_at="2099-01-01T00:00:00Z",
        candidate_ref="candidate-a",
        job_ref="job-a",
    )


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


def test_ashby_parse_refills_cleared_values_and_keeps_a_typed_one(browser, monkeypatch):
    monkeypatch.setattr(
        "autofill.stepfill.resolve_step",
        lambda *args, **kwargs: ResolverResponse(
            fields={
                "fullName": "River Example",
                "email": "river.example@example.com",
                "phone": "5550100199",
                "resume.file": {"url": "https://example.com/resume.pdf", "filename": "notes.pdf"},
            }
        ),
    )
    monkeypatch.setattr(
        "autofill.stepfill.fetch_resume_payload",
        lambda descriptor: ResumePayload(name="notes.pdf", mime_type="application/pdf", data=PDF),
    )
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "ashby_resume_rewrite.html").as_uri())
        outcome = fill_resolved_page(
            page,
            _binding(),
            session_id="sess-ashby",
            ats_name="ashby",
            step="application",
        )
        assert outcome.resume_blocked is False
        assert page.locator("#name").input_value() == "River Example"
        assert page.locator("#phone").input_value() == "5550100199"
        assert page.locator("#email").input_value() == "parsed@example.com"
        assert page.locator("#referral").input_value() == "FRIEND"
    finally:
        page.close()


def test_lever_gap_rereads_a_filled_name_after_upload(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "lever_stale_name.html").as_uri())
        stale = extract_page(page)
        page.locator("#resume").set_input_files(
            [{"name": "notes.pdf", "mimeType": "application/pdf", "buffer": PDF}]
        )
        page.locator("#chip").wait_for(state="visible")
        gaps = required_gaps(page, stale, None)
        texts = " ".join(str(item.get("text") or "") for item in gaps).casefold()
        assert "full name" not in texts
        assert page.locator("#name").input_value() == "River Example"
    finally:
        page.close()


def _confirmed() -> list[dict]:
    """Values the engine already wrote, keyed the way the session stores them."""
    return [
        {
            "value": "River Example",
            "label": "Full name",
            "name": "name",
            "elementId": "name",
            "automationId": "",
            "fieldKey": "fullName",
        },
        {
            "value": "river.example@example.com",
            "label": "Email",
            "name": "email",
            "elementId": "email",
            "automationId": "",
            "fieldKey": "email",
        },
        {
            "value": "5550100199",
            "label": "Phone",
            "name": "phone",
            "elementId": "phone",
            "automationId": "",
            "fieldKey": "phone",
        },
    ]


def test_resume_file_refill_restores_cleared_fields_and_keeps_a_parsed_email(browser, caplog):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "ashby_resume_rewrite.html").as_uri())
        page.locator("#name").fill("River Example")
        page.locator("#email").fill("river.example@example.com")
        page.locator("#phone").fill("5550100199")
        page.locator("#resume").set_input_files(
            [{"name": "notes.pdf", "mimeType": "application/pdf", "buffer": PDF}]
        )
        caplog.set_level(logging.INFO, logger="autofill.stepfill")
        restored = post_upload_refill(page, _confirmed(), session_id="sess-ashby")
        assert restored == 2
        assert "session=sess-ashby action=post_upload_refill restored=2" in caplog.text
        assert page.locator("#name").input_value() == "River Example"
        assert page.locator("#phone").input_value() == "5550100199"
        assert page.locator("#email").input_value() == "parsed@example.com"
        assert page.locator("#referral").input_value() == "FRIEND"
    finally:
        page.close()


def test_ashby_rerender_restores_from_confirmed_values_not_the_old_nodes(browser, caplog):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "ashby_resume_rerender.html").as_uri())
        page.locator("#name").fill("River Example")
        page.locator("#email").fill("river.example@example.com")
        page.locator("#phone").fill("5550100199")
        page.locator("#resume").set_input_files(
            [{"name": "notes.pdf", "mimeType": "application/pdf", "buffer": PDF}]
        )
        caplog.set_level(logging.INFO, logger="autofill.stepfill")
        restored = post_upload_refill(page, _confirmed(), session_id="sess-ashby")
        assert restored == 2
        assert "session=sess-ashby action=post_upload_refill restored=2" in caplog.text
        assert page.locator("#name-replaced").input_value() == "River Example"
        assert page.locator("#phone-replaced").input_value() == "5550100199"
        assert page.locator("#email").input_value() == "parsed@example.com"
        assert page.locator("#referral").input_value() == "FRIEND"
        assert page.locator("#name").count() == 0
    finally:
        page.close()


def test_refill_log_uses_the_bound_session_when_the_caller_omits_it(browser, caplog):
    page = browser.new_page()
    token = bind_session_id("sess-bound")
    try:
        page.goto((FIXTURES / "ashby_resume_rewrite.html").as_uri())
        caplog.set_level(logging.INFO, logger="autofill.stepfill")
        restored = post_upload_refill(page, [])
        assert restored == 0
        assert "session=sess-bound action=post_upload_refill restored=0" in caplog.text
        assert "session=- action=post_upload_refill" not in caplog.text
    finally:
        reset_session_id(token)
        page.close()


def test_lever_follow_up_does_not_report_a_filled_name(browser, monkeypatch):
    monkeypatch.setattr(
        "autofill.stepfill.resolve_step",
        lambda *args, **kwargs: ResolverResponse(fields={"email": "river.example@example.com"}),
    )
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "lever_stale_name.html").as_uri())
        stale = extract_page(page)
        page.locator("#resume").set_input_files(
            [{"name": "notes.pdf", "mimeType": "application/pdf", "buffer": PDF}]
        )
        page.locator("#chip").wait_for(state="visible")
        outcome = fill_resolved_page(
            page,
            _binding(),
            session_id="sess-lever",
            ats_name="lever",
            step="application",
            resume_uploaded=True,
        )
        texts = " ".join(str(item.get("text") or "") for item in outcome.manual_questions).casefold()
        assert "full name" not in texts
        gaps = required_gaps(page, stale, None)
        gap_text = " ".join(str(item.get("text") or "") for item in gaps).casefold()
        assert "full name" not in gap_text
        assert page.locator("#name").input_value() == "River Example"
        assert page.locator("#ghost").input_value() == ""
    finally:
        page.close()


def test_reattach_does_not_upload_again_when_the_chip_is_already_accepted(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "greenhouse_replaced.html").as_uri())
        reattach_verified(page, "notes.pdf", "application/pdf", PDF, session_id="sess-greenhouse")
        assert page.locator("#resume-next").evaluate("el => el.files.length") == 0
    finally:
        page.close()


def test_reattach_skips_when_the_accepted_chip_is_outside_the_new_input(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "greenhouse_chip_outside.html").as_uri())
        reattach_verified(page, "notes.pdf", "application/pdf", PDF, session_id="sess-greenhouse")
        assert page.locator("#resume-next").evaluate("el => el.files.length") == 0
    finally:
        page.close()


def test_ashby_live_replace_matches_field_path_and_question_title(browser, caplog):
    """The live form drops the old id and name and leaves label-for pointing at nothing."""
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "ashby_live_replace.html").as_uri())
        page.locator("#name").fill("River Example")
        page.locator("#email").fill("river.example@example.com")
        page.locator("#phone").fill("5550100199")
        page.locator("#resume").set_input_files(
            [{"name": "notes.pdf", "mimeType": "application/pdf", "buffer": PDF}]
        )
        records = [
            {
                "value": "River Example",
                "label": "Full name",
                "name": "_systemfield_name",
                "elementId": "name",
                "fieldPath": "_systemfield_name",
                "fieldKey": "fullName",
            },
            {
                "value": "5550100199",
                "label": "Phone",
                "name": "_systemfield_phone",
                "elementId": "phone",
                "fieldKey": "phone",
            },
            {
                "value": "river.example@example.com",
                "label": "Email",
                "name": "_systemfield_email",
                "elementId": "email",
                "fieldKey": "email",
            },
        ]
        caplog.set_level(logging.INFO, logger="autofill.stepfill")
        restored = post_upload_refill(page, records, session_id="sess-ashby")
        assert restored == 2
        assert "session=sess-ashby action=post_upload_refill records=3" in caplog.text
        assert "action=post_upload_refill_match field=fullName via=path result=restored" in caplog.text
        assert "action=post_upload_refill_match field=phone via=label result=restored" in caplog.text
        assert "action=post_upload_refill_match field=email via=id result=kept" in caplog.text
        assert "River Example" not in caplog.text
        assert "5550100199" not in caplog.text
        assert "river.example@example.com" not in caplog.text
        assert page.locator("#name-live").input_value() == "River Example"
        assert page.locator("#phone-live").input_value() == "5550100199"
        assert page.locator("#email").input_value() == "parsed@example.com"
        assert page.locator("#referral").input_value() == "FRIEND"
        assert page.locator("#name").count() == 0
    finally:
        page.close()


def test_confirmed_snapshot_is_the_list_the_upload_reads(browser):
    """A Playwright call must not drop the values recorded for the later refill."""
    profile = CandidateProfile.from_dict(
        {"candidateId": "ref-a", "personal": {"full_name": "River Example", "email": "river.example@example.com"}}
    )
    session = ServiceSession(
        session_id="sess-ashby",
        candidate_id="ref-a",
        job_id="job-a",
        application_url="https://jobs.ashbyhq.com/example",
        profile=profile,
        job=JobContext(title="Example", company="Example", url="https://jobs.ashbyhq.com/example"),
        cover_letter_text=None,
        credentials=MemoryCredentialProvider(),
    )
    held = session.confirmed_values
    token = bind_confirmed(session.remember_confirmed_value, session.confirmed_values)
    page = browser.new_page()
    try:
        page.set_content("<label for='name'>Name</label><input id='name' name='_systemfield_name'>")
        page.locator("#name").evaluate("el => el.value")
        apply_mapped(
            page,
            Control(
                kind="text",
                selector="#name",
                label="Name",
                name="_systemfield_name",
                element_id="name",
                field_path="_systemfield_name",
            ),
            MappedField(key="fullName", action="fill", text="River Example", field_class="PROFILE_FIELD"),
        )
        assert session.confirmed_values is held
        assert len(session.confirmed_values) == 1
        assert session.confirmed_values[0]["fieldKey"] == "fullName"
        assert session.confirmed_values[0]["fieldPath"] == "_systemfield_name"
        assert confirmed_records() == session.confirmed_values
    finally:
        reset_confirmed(token)
        page.close()


def test_lever_hidden_proxy_is_not_the_full_name_gap(browser):
    """The gap scan reads the visible name, not the 1px empty input that shares its name."""
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "lever_hidden_name.html").as_uri())
        stale = extract_page(page)
        proxy = next(item for item in stale.controls if item.element_id == "name-proxy")
        assert proxy.hidden is True
        page.locator("#resume").set_input_files(
            [{"name": "notes.pdf", "mimeType": "application/pdf", "buffer": PDF}]
        )
        page.locator("#chip").wait_for(state="visible")
        shown = live_field_value(
            page,
            Control(
                kind="text",
                selector="#name-proxy",
                label="Full name",
                name="name",
                element_id="name-proxy",
                required=True,
            ),
        )
        assert shown == "River Example"
        gaps = required_gaps(page, stale, None)
        texts = " ".join(str(item.get("text") or "") for item in gaps).casefold()
        assert "full name" not in texts
        assert page.locator("#name").input_value() == "River Example"
        assert page.locator("#name-proxy").input_value() == ""
    finally:
        page.close()


def _name_session() -> ServiceSession:
    profile = CandidateProfile.from_dict(
        {
            "candidateId": "ref-a",
            "personal": {"full_name": "River Example", "email": "river.example@example.com"},
        }
    )
    return ServiceSession(
        session_id="sess-name",
        candidate_id="ref-a",
        job_id="job-a",
        application_url="https://jobs.lever.co/example/apply",
        profile=profile,
        job=JobContext(title="Example", company="Example", url="https://jobs.lever.co/example/apply"),
        cover_letter_text=None,
        credentials=MemoryCredentialProvider(),
    )


def _keys(session: ServiceSession) -> list[str]:
    return [str(item.get("fieldKey") or "") for item in session.confirmed_values]


def test_lever_live_dom_records_a_prefilled_name_and_does_not_clear_it(browser, monkeypatch, caplog):
    """The live Lever apply row is one visible Full name input. A parser value is a record.

    The follow-up pass must not replace that value, and it must not report the
    filled row as a gap.
    """
    monkeypatch.setattr(
        "autofill.stepfill.resolve_step",
        lambda *args, **kwargs: ResolverResponse(fields={"email": "river.example@example.com"}),
    )
    page = browser.new_page()
    session = _name_session()
    token = bind_confirmed(session.remember_confirmed_value, session.confirmed_values)
    try:
        page.goto((FIXTURES / "lever_apply_live.html").as_uri())
        box = page.locator("input[name='name']").bounding_box()
        assert box is not None and box["width"] > 400 and box["height"] > 10
        assert page.locator("input[name='name']").count() == 1
        page.locator("input[name='name']").evaluate("el => { el.value = 'River Example'; }")
        outcome = fill_resolved_page(
            page,
            _binding(),
            session_id="sess-lever",
            ats_name="lever",
            step="application",
        )
        assert "fullName" in outcome.requested_fields
        assert "fullName" in _keys(session)
        name = next(item for item in session.confirmed_values if item.get("fieldKey") == "fullName")
        assert name.get("name") == "name"
        texts = " ".join(str(item.get("text") or "") for item in outcome.manual_questions).casefold()
        assert "full name" not in texts
        gaps = required_gaps(page, extract_page(page), None)
        gap_text = " ".join(str(item.get("text") or "") for item in gaps).casefold()
        assert "full name" not in gap_text
        assert page.locator("input[name='name']").input_value() == "River Example"

        monkeypatch.setattr(
            "autofill.stepfill.resolve_step",
            lambda *args, **kwargs: ResolverResponse(
                fields={"fullName": "Other Person", "email": "other@example.com"}
            ),
        )
        follow = fill_resolved_page(
            page,
            _binding(),
            session_id="sess-lever",
            ats_name="lever",
            step="application",
            resume_uploaded=True,
        )
        assert page.locator("input[name='name']").input_value() == "River Example"
        follow_text = " ".join(str(item.get("text") or "") for item in follow.manual_questions).casefold()
        assert "full name" not in follow_text
        caplog.clear()
        caplog.set_level(logging.INFO, logger="autofill.stepfill")
        records = [{"value": "river.example@example.com", "label": "Email", "name": "email", "fieldKey": "email"}]
        post_upload_refill(page, records, session_id="sess-lever")
        assert any(item.get("fieldKey") == "fullName" for item in records)
        assert "River Example" not in caplog.text
        assert "action=post_upload_refill_match field=fullName" in caplog.text
    finally:
        reset_confirmed(token)
        page.close()


def test_greenhouse_full_name_splits_and_a_prefilled_name_is_recorded(browser, monkeypatch):
    monkeypatch.setattr(
        "autofill.stepfill.resolve_step",
        lambda *args, **kwargs: ResolverResponse(fields={"fullName": "River Example"}),
    )
    page = browser.new_page()
    session = _name_session()
    token = bind_confirmed(session.remember_confirmed_value, session.confirmed_values)
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='first_name'>First Name</label>"
            "<input id='first_name' autocomplete='given-name' aria-label='First Name' required>"
            "<label for='last_name'>Last Name</label>"
            "<input id='last_name' autocomplete='family-name' aria-label='Last Name' required>"
            "<label for='referral'>If a DLR Group employee referred you, please list their name.</label>"
            "<textarea id='referral'></textarea>"
            "</form>"
        )
        outcome = fill_resolved_page(
            page,
            _binding(),
            session_id="sess-greenhouse",
            ats_name="greenhouse",
            step="application",
        )
        assert "fullName" in outcome.requested_fields
        assert page.locator("#first_name").input_value() == "River"
        assert page.locator("#last_name").input_value() == "Example"
        assert page.locator("#referral").input_value() == ""
        assert "firstName" in _keys(session)
        assert "lastName" in _keys(session)

        page.locator("#first_name").evaluate("el => { el.value = 'River'; }")
        page.locator("#last_name").evaluate("el => { el.value = 'Example'; }")
        session.confirmed_values.clear()
        monkeypatch.setattr(
            "autofill.stepfill.resolve_step",
            lambda *args, **kwargs: ResolverResponse(fields={"email": "river.example@example.com"}),
        )
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='first_name'>First Name</label>"
            "<input id='first_name' autocomplete='given-name' value='River' required>"
            "<label for='last_name'>Last Name</label>"
            "<input id='last_name' autocomplete='family-name' value='Example' required>"
            "<label for='email'>Email</label>"
            "<input id='email' type='email' required>"
            "</form>"
        )
        fill_resolved_page(
            page,
            _binding(),
            session_id="sess-greenhouse",
            ats_name="greenhouse",
            step="application",
        )
        assert "firstName" in _keys(session)
        assert "lastName" in _keys(session)
        recorded = {item["fieldKey"]: item["value"] for item in session.confirmed_values}
        assert recorded["firstName"] == "River"
        assert recorded["lastName"] == "Example"
    finally:
        reset_confirmed(token)
        page.close()


def test_follow_up_waits_out_a_field_drop_and_does_not_clear_filled_values(browser, monkeypatch):
    monkeypatch.setattr(
        "autofill.stepfill.resolve_step",
        lambda *args, **kwargs: ResolverResponse(
            fields={
                "email": "other@example.com",
                "phone": "0000000000",
                "fullName": "Other Person",
                "firstName": "Other",
                "lastName": "Person",
            }
        ),
    )
    page = browser.new_page()
    try:
        page.set_content(
            "<h1>Application</h1><form>"
            "<label for='email'>Email</label>"
            "<input id='email' name='email' type='email' required value='river.example@example.com'>"
            "<div id='slot'>"
            "<label for='phone'>Phone</label>"
            "<input id='phone' name='phone' required value='5550100199'>"
            "<label for='first_name'>First Name</label>"
            "<input id='first_name' autocomplete='given-name' required value='River'>"
            "<label for='last_name'>Last Name</label>"
            "<input id='last_name' autocomplete='family-name' required value='Example'>"
            "</div></form>"
            "<script>"
            "const slot = document.getElementById('slot');"
            "const html = slot.innerHTML;"
            "setTimeout(() => { slot.innerHTML = ''; }, 40);"
            "setTimeout(() => { slot.innerHTML = html; }, 700);"
            "</script>"
        )
        started = time.monotonic()
        outcome = fill_resolved_page(
            page,
            _binding(),
            session_id="sess-greenhouse",
            ats_name="greenhouse",
            step="application",
            resume_uploaded=True,
        )
        elapsed = time.monotonic() - started
        assert elapsed < 5.0
        assert "phone" in outcome.requested_fields
        assert "firstName" in outcome.requested_fields
        assert page.locator("#email").input_value() == "river.example@example.com"
        assert page.locator("#phone").input_value() == "5550100199"
        assert page.locator("#first_name").input_value() == "River"
        assert page.locator("#last_name").input_value() == "Example"
    finally:
        page.close()


def test_iframe_name_fill_is_recorded(browser):
    page = browser.new_page()
    session = _name_session()
    token = bind_confirmed(session.remember_confirmed_value, session.confirmed_values)
    try:
        page.set_content(
            "<h1>Application</h1>"
            "<iframe id='app' srcdoc=\"<label for='name'>Full name</label>"
            "<input id='name' name='name' style='width:240px;height:32px'>\"></iframe>"
        )
        page.frame_locator("#app").locator("#name").wait_for()
        snapshot = extract_page(page)
        control = next(item for item in snapshot.controls if item.element_id == "name")
        assert control.frame_url
        apply_mapped(
            page,
            control,
            MappedField(key="fullName", action="fill", text="River Example", field_class="PROFILE_FIELD"),
        )
        frame = next(item for item in page.frames if item != page.main_frame)
        assert frame.locator("#name").input_value() == "River Example"
        assert _keys(session) == ["fullName"]
    finally:
        reset_confirmed(token)
        page.close()


def test_greenhouse_resume_confirm_returns_when_the_file_is_shown(browser):
    page = browser.new_page()
    try:
        page.goto((FIXTURES / "greenhouse_resume.html").as_uri())
        page.locator("#resume").set_input_files(
            [{"name": "notes.pdf", "mimeType": "application/pdf", "buffer": PDF}]
        )
        started = time.monotonic()
        assert _resume_accepted(page, "#resume", "notes.pdf") is True
        assert time.monotonic() - started < 1.0
    finally:
        page.close()
