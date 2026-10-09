"""The attached resume is the grant, under the grant's filename."""

import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from autofill.intents import IntentMatch
from autofill.models import Control, Option, PageSnapshot
from autofill.resolver import _resume_descriptor
from autofill.resume_fetch import fetch_resume_payload, resume_filename, sanitized_resume_name
from autofill.stepfill import (
    ResolvedPage,
    _clusters,
    _compose_location,
    _describe,
    _manual,
    _note_unfilled_required,
    _question_text,
    _skip,
    choose_location_label,
)

_PDF = b"%PDF-1.1\ngrant-bytes\n%%EOF\n"


def test_grant_filename_is_sanitized_and_not_taken_from_a_profile():
    assert sanitized_resume_name("First_Last_Resume.pdf") == "First_Last_Resume.pdf"
    assert sanitized_resume_name("../../First_Last_Resume.pdf") == "First_Last_Resume.pdf"
    assert sanitized_resume_name("My Resume.docx") == "My-Resume.docx"
    assert sanitized_resume_name("letter.DOC") == "letter.DOC"
    assert sanitized_resume_name("") == ""
    assert sanitized_resume_name("Synthetic Candidate") == ""
    assert sanitized_resume_name("notes.txt") == ""
    assert resume_filename("") == "Resume.pdf"
    assert resume_filename("Synthetic Candidate") == "Resume.pdf"
    assert resume_filename("Synthetic_Candidate_Resume.pdf") == "Synthetic_Candidate_Resume.pdf"
    unnamed = _resume_descriptor({"url": "https://files.example/resume", "filename": ""})
    assert unnamed is not None and unnamed["filename"] == "Resume.pdf"
    missing = _resume_descriptor({"url": "https://files.example/resume"})
    assert missing is not None and missing["filename"] == "Resume.pdf"
    kept = _resume_descriptor(
        {
            "url": "https://files.example/resume",
            "filename": "First_Last_Resume.pdf",
            "contentType": "application/pdf",
        }
    )
    assert kept is not None
    assert kept["filename"] == "First_Last_Resume.pdf"


def test_payload_is_the_grant_bytes_under_the_grant_name():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(_PDF)))
            self.end_headers()
            self.wfile.write(_PDF)

        def log_message(self, fmt: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    try:
        payload = fetch_resume_payload(
            {
                "url": f"http://{host}:{port}/granted",
                "filename": "Granted_Resume.pdf",
                "contentType": "application/pdf",
                "expiresAt": expires,
            }
        )
        neutral = fetch_resume_payload(
            {
                "url": f"http://{host}:{port}/granted",
                "filename": "",
                "contentType": "application/pdf",
                "expiresAt": expires,
            }
        )
    finally:
        server.shutdown()
    assert payload.data == _PDF
    assert payload.name == "Granted_Resume.pdf"
    assert neutral.data == _PDF
    assert neutral.name == "Resume.pdf"
    assert "Synthetic" not in neutral.name


def test_fieldset_and_shared_name_checkboxes_are_one_question():
    english = Control(kind="checkbox", name="en", label="Languages English", group="Languages", selector="#en")
    spanish = Control(kind="checkbox", name="es", label="Languages Spanish", group="Languages", selector="#es")
    kind, grouped = _clusters(PageSnapshot(controls=[english, spanish], buttons=[]))[0]
    ask = _describe(kind, grouped, "", None)
    assert ask is not None and ask.include_question
    assert ask.question_body()["options"] == ["English", "Spanish"]
    named = [
        Control(
            kind="checkbox",
            name="languages",
            label=label,
            selector=f"#{label}",
            prompt="Which languages do you speak?",
        )
        for label in ("English", "Spanish", "French")
    ]
    kind, grouped = _clusters(PageSnapshot(controls=named, buttons=[]))[0]
    ask = _describe(kind, grouped, "", None)
    assert ask is not None
    body = ask.question_body()
    assert body["text"] == "Which languages do you speak?"
    assert body["options"] == ["English", "Spanish", "French"]


def test_grouped_checkbox_options_and_section_headings_are_not_questions():
    boxes = [
        Control(
            kind="checkbox",
            name=name,
            label=label,
            prompt="Language Skill(s)",
            required=True,
            selector=f"#{name}",
        )
        for name, label in (
            ("lang-en", "English (ENG)"),
            ("lang-es", "Spanish (SPA)"),
            ("lang-fr", "French (FRA)"),
        )
    ]
    heading = Control(
        kind="textarea",
        name="notes",
        label="ADDITIONAL INFORMATION",
        required=True,
        selector="#notes",
    )
    snapshot = PageSnapshot(controls=[heading, *boxes], buttons=[])
    clusters = _clusters(snapshot)
    asks = [ask for kind, grouped in clusters if (ask := _describe(kind, grouped, "", None)) is not None]
    questions = [ask.question_body() for ask in asks if ask.include_question]
    assert [item["text"] for item in questions] == ["Language Skill(s)"]
    assert questions[0]["options"] == ["English (ENG)", "Spanish (SPA)", "French (FRA)"]
    outcome = ResolvedPage(snapshot=snapshot)
    _manual(outcome, IntentMatch(None, "unknown", "none", "Language Skill(s)"), blocking=True)
    for label in ("English (ENG)", "Spanish (SPA)", "French (FRA)", "ADDITIONAL INFORMATION"):
        _manual(outcome, IntentMatch(None, "unknown", "none", label), blocking=True)
    _note_unfilled_required(outcome)
    texts = [item["text"] for item in outcome.manual_questions]
    assert texts.count("Language Skill(s)") == 1
    assert "English (ENG)" not in texts
    assert "Spanish (SPA)" not in texts
    assert "French (FRA)" not in texts
    assert "ADDITIONAL INFORMATION" not in texts
    assert _question_text([heading]) == ""


def test_question_text_is_not_an_option_label():
    radio = Control(
        kind="radio",
        label="Yes, I consent",
        prompt="Do you consent to a background check?",
        options=[Option("yes", "Yes, I consent"), Option("no", "No")],
    )
    assert _question_text([radio]) == "Do you consent to a background check?"
    unnamed = Control(
        kind="radio",
        label="Yes",
        options=[Option("Yes", "Yes"), Option("No", "No")],
    )
    assert _question_text([unnamed]) == ""


def test_location_state_alias_picks_austin_tx():
    labels = [
        "Austin, TX, USA",
        "Austin, MN, USA",
        "Austin, IN, USA",
        "Austin, AR, USA",
    ]
    assert choose_location_label(labels, city="Austin", region="TX", country="US") == "Austin, TX, USA"
    assert choose_location_label(labels, city="Austin", region="Texas", country="USA") == "Austin, TX, USA"
    assert choose_location_label(labels, city="Austin", region="", country="US") is None
    assert _compose_location({"address.city": "Austin", "address.state": "TX"}) == "Austin, TX"
    assert (
        _compose_location({"address.city": "Austin", "address.region": "TX", "address.state": "MN"}) == "Austin, TX"
    )


def test_required_unfilled_control_becomes_a_manual_question():
    years = Control(
        kind="text",
        label="How many years of experience do you have?",
        required=True,
        selector="#yoe",
    )
    field = _describe("one", [years], "", None)
    assert field is not None and field.field_key == "yearsExperience"
    outcome = ResolvedPage()
    _skip(years, outcome, "yearsExperience", "Resolver did not return this key.")
    assert outcome.manual_questions == [
        {
            "intent": None,
            "text": "How many years of experience do you have?",
            "blocking": True,
        }
    ]
    optional = Control(kind="text", label="Internal badge number", selector="#badge")
    assert _describe("one", [optional], "", None) is None
    skipped = ResolvedPage()
    _skip(optional, skipped, "badge", "Resolver did not return this key.")
    assert skipped.manual_questions == []
    blank = ResolvedPage()
    _manual(blank, IntentMatch(None, "unknown", "none", ""), blocking=True)
    _manual(blank, IntentMatch(None, "unknown", "none", "Where do you plan on working from?"), blocking=True)
    assert [item["text"] for item in blank.manual_questions] == ["Where do you plan on working from?"]
    required_text = Control(kind="text", label="Years of experience using Python", required=True, selector="#py")
    asked = _describe("one", [required_text], "", None)
    assert asked is not None and asked.include_question is True and asked.field_key is None


def test_location_pick_normalizes_region_and_country():
    labels = [
        "Example City, California, United States",
        "Example City, Texas, United States",
        "Other Town, California, United States",
    ]
    assert (
        choose_location_label(labels, city="Example City", region="CA", country="US")
        == "Example City, California, United States"
    )
    missed = choose_location_label(
        ["Far Town, Texas, United States"],
        city="Example City",
        region="CA",
        country="US",
    )
    assert missed is None
    tied = ["Example City, California, United States", "Example City, California, United States"]
    assert choose_location_label(tied, city="Example City", region="CA", country="US") is None


def test_location_prefers_a_location_key_then_address_parts():
    assert _compose_location({"location": "Example City, EX"}) == "Example City, EX"
    assert (
        _compose_location(
            {
                "address.city": "Example City",
                "address.region": "EX",
                "address.country": "Exampleland",
            }
        )
        == "Example City, EX, Exampleland"
    )
    assert _compose_location({"address.city": "Example City", "address.country": "Exampleland"}) == (
        "Example City, Exampleland"
    )
