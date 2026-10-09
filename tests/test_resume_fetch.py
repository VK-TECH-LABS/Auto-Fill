"""The file a site receives is named for the candidate."""

from autofill.models import Control, Option, PageSnapshot
from autofill.resume_fetch import candidate_resume_name
from autofill.stepfill import _clusters, _compose_location, _describe, _question_text, choose_location_label


def test_resume_name_uses_first_and_last():
    assert candidate_resume_name("River", "Example") == "River_Example_Resume.pdf"
    assert not candidate_resume_name("River", "Example").startswith("autofill-resume")


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
