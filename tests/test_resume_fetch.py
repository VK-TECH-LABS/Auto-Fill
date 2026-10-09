"""The attached resume is the grant, under the grant's filename."""

import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from autofill.models import Control, Option, PageSnapshot
from autofill.resolver import _resume_descriptor
from autofill.resume_fetch import fetch_resume_payload, resume_filename, sanitized_resume_name
from autofill.stepfill import _clusters, _compose_location, _describe, _question_text, choose_location_label

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
