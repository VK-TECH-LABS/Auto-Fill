"""Field mapping never invents an answer the profile did not give."""

from pathlib import Path

from autofill.mapping import map_field
from autofill.models import Control, JobContext, Option
from autofill.profile import ApplicationAnswer, CandidateProfile

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "profile.example.json"


def profile() -> CandidateProfile:
    return CandidateProfile.load(EXAMPLE)


def control(label: str, **kwargs) -> Control:
    data = {"kind": "text", "label": label, "selector": "#x"}
    data.update(kwargs)
    return Control(**data)


def test_identity_contact_and_address_labels():
    filled = profile()
    assert map_field(control("First name"), filled).text == "Casey"
    assert map_field(control("Last name"), filled).text == "Example"
    assert map_field(control("Email address"), filled).key == "email"
    assert map_field(control("Email address"), filled).text == "casey.example@example.com"
    assert map_field(control("Street address"), filled).key == "address"
    assert map_field(control("City"), filled).text == "Example City"
    phone = map_field(control("Phone", autocomplete="tel"), filled)
    assert phone.text == "555-010-0199"
    digits = map_field(control("Phone", autocomplete="tel-national", input_mode="numeric"), filled)
    assert digits.text == "5550100199"


def test_current_location_uses_city_state_and_country():
    filled = profile()
    mapped = map_field(control("Current location"), filled)
    assert mapped.key == "location"
    assert mapped.text == "Example City, EX, Exampleland"
    assert map_field(control("Where are you located?"), filled).key != "location"
    assert map_field(control("Are you willing to relocate?"), filled).key != "location"


def test_preferred_name_versus_legal_name():
    person = CandidateProfile.from_dict(
        {
            "personal": {
                "full_name": "Alexandra Example",
                "preferred_name": "Alex",
                "email": "alex.example@example.com",
            }
        }
    )
    assert map_field(control("Name"), person).text == "Alex Example"
    assert map_field(control("Legal name"), person).text == "Alexandra Example"
    assert map_field(control("First name"), person).text == "Alex"
    assert map_field(control("Legal first name"), person).text == "Alexandra"


def test_autocomplete_beats_a_vague_label():
    mapped = map_field(control("Applicant", autocomplete="given-name"), profile())
    assert mapped.key == "first_name"
    assert mapped.text == "Casey"


def test_work_auth_select_and_sponsorship_radio():
    filled = profile()
    authorized = map_field(
        control(
            "Are you legally authorized to work?",
            kind="select",
            options=[Option("Yes", "Yes"), Option("No", "No")],
        ),
        filled,
    )
    assert authorized.action == "select"
    assert authorized.option_label == "Yes"
    sponsorship = map_field(
        control(
            "Will you require sponsorship?",
            kind="radio",
            options=[
                Option("Yes", "Yes", "#sponsor-yes"),
                Option("No", "No", "#sponsor-no"),
            ],
        ),
        filled,
    )
    assert sponsorship.action == "select"
    assert sponsorship.option_selector == "#sponsor-no"


def test_checkbox_yes_and_no():
    filled = profile()
    background = map_field(control("Willing to undergo a background check", kind="checkbox"), filled)
    assert background.action == "check"
    felony = map_field(control("Have you been convicted of a felony?", kind="checkbox"), filled)
    assert felony.action == "uncheck"


def test_blank_screening_answer_is_not_invented():
    person = CandidateProfile.from_dict(
        {"personal": {"full_name": "Casey Example", "email": "casey.example@example.com"}}
    )
    mapped = map_field(control("Have you been convicted of a felony?"), person)
    assert mapped.action == "unanswered"
    assert mapped.text == ""


def test_approved_answer_overrides_screening_for_the_same_question():
    """Session approved Q&A wins over screening.* when both match. Other questions stay put."""
    person = profile()
    assert person.screening.how_heard == "Online job board"
    assert person.screening.age_18_or_older == "Yes"
    assert person.screening.felony_conviction == "No"
    person.application_answers = [
        ApplicationAnswer(question="Where did you hear about this role?", answer="Employee referral"),
        ApplicationAnswer(question="Are you age 18 or older?", answer="No"),
    ]
    heard = map_field(control("How did you hear about this role?"), person)
    assert heard.action == "fill"
    assert heard.text == "Employee referral"
    assert heard.field_class == "APPROVED_QUESTION"
    age = map_field(
        control(
            "Are you age 18 or older?",
            kind="select",
            options=[Option("Yes", "Yes"), Option("No", "No")],
        ),
        person,
    )
    assert age.action == "select"
    assert age.option_label == "No"
    assert age.field_class == "APPROVED_QUESTION"
    felony = map_field(control("Have you been convicted of a felony?", kind="checkbox"), person)
    assert felony.action == "uncheck"
    assert felony.text == "No"
    unknown = map_field(control("What is your favorite color?"), person)
    assert unknown.action == "unanswered"
    assert unknown.text == ""
    assert unknown.field_class == "UNKNOWN_FIELD"


def test_eeo_decline_matches_a_similar_option():
    mapped = map_field(
        control(
            "Gender",
            kind="select",
            options=[
                Option("", "Select..."),
                Option("female", "Female"),
                Option("decline", "Prefer not to say"),
            ],
        ),
        profile(),
    )
    assert mapped.action == "select"
    assert mapped.option_label == "Prefer not to say"


def test_hidden_and_honeypot_fields_are_skipped():
    filled = profile()
    hidden = map_field(control("Website", hidden=True), filled)
    assert hidden.action == "skip"
    trap = map_field(control("Company website"), filled)
    assert trap.action == "skip"
    assert map_field(control("Password", kind="password"), filled).action == "skip"


def test_resume_and_cover_letter_files():
    filled = profile()
    missing = map_field(control("Resume", kind="file"), filled)
    assert missing.action == "resume_required"
    assert missing.field_class == "RESUME_FIELD"
    still_human = map_field(control("Resume", kind="file"), filled, resume_path="/tmp/resume.pdf")
    assert still_human.action == "resume_required"
    assert still_human.text == ""
    letter = map_field(control("Cover letter", kind="textarea"), filled)
    assert letter.action == "fill"
    assert "fictional" in letter.text


def test_skill_question_uses_only_listed_skills():
    filled = profile()
    python = map_field(control("Do you have experience with Python?"), filled)
    assert python.action == "fill"
    assert python.text == "Yes"
    rust = map_field(control("Do you have experience with Rust?"), filled)
    assert rust.action == "unanswered"
    years = map_field(control("Years of experience with Kubernetes"), filled)
    assert years.action == "unanswered"
    assert years.key != "years_of_experience_total"


def test_salary_uses_profile_floor_and_posted_midpoint():
    filled = profile()
    base = map_field(control("Desired salary"), filled)
    assert base.text == "85000"
    posted = map_field(
        control("Desired salary"),
        filled,
        job=JobContext(posted_salary_min=100000, posted_salary_max=140000),
    )
    assert posted.text == "120000"
    below_floor = map_field(
        control("Desired salary"),
        filled,
        job=JobContext(posted_salary_min=40000, posted_salary_max=60000),
    )
    assert below_floor.text == "85000"
    hourly = map_field(control("Hourly rate"), filled)
    assert hourly.text == str(85000 // 2080)
    ranged = map_field(control("Salary range"), filled)
    assert ranged.text == "85000-100000"


def test_non_iso_start_date_is_not_forced_into_a_date_input():
    mapped = map_field(control("Earliest start date", input_type="date"), profile())
    assert mapped.action == "unanswered"


def test_unrecognized_control_is_left_alone():
    mapped = map_field(control("Pronouns"), profile())
    assert mapped.action == "unanswered"
    assert mapped.key is None
