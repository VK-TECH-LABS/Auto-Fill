"""Field mapping uses the profile, and the job-description rules for expected pay.

It does not invent work authorization, a salary-range maximum, or an unrelated answer.
"""

from pathlib import Path

from autofill.dom_ui import public_blocking
from autofill.intents import classify_question
from autofill.mapping import map_field
from autofill.models import Control, JobContext, Option
from autofill.profile import ApplicationAnswer, CandidateProfile
from autofill.service.schemas import BlockingOut
from autofill.stepfill import _Ask, _gap_item

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


def test_referral_and_relative_questions_are_not_the_employer():
    filled = profile()
    labels = [
        "If an employee referred you, please list their name",
        "Who referred you?",
        "Employee referral",
        "Previous employer contact",
        "Do you have relatives employed here?",
    ]
    for label in labels:
        mapped = map_field(control(label), filled)
        assert mapped.key != "current_company", label
        assert mapped.text != "Example Labs", label
        assert "Example Labs" not in mapped.text


def test_current_employer_labels_still_map():
    filled = profile()
    for label in ("Current company", "Current employer", "Most recent employer", "Employer", "Company name"):
        mapped = map_field(control(label), filled)
        assert mapped.key == "current_company", label
        assert mapped.action == "fill"
        assert mapped.text == "Example Labs"


def test_preferred_first_name_stays_blank_without_a_preferred_name():
    named = CandidateProfile.from_dict(
        {
            "personal": {
                "full_name": "Alexandra Example",
                "preferred_name": "Alex",
                "email": "alex.example@example.com",
            }
        }
    )
    assert map_field(control("Preferred First Name"), named).text == "Alex"
    blank = CandidateProfile.from_dict(
        {"personal": {"full_name": "Alexandra Example", "email": "alex.example@example.com"}}
    )
    preferred = map_field(control("Preferred First Name"), blank)
    assert preferred.key == "preferred_name"
    assert preferred.action == "unanswered"
    assert preferred.text == ""
    assert map_field(control("First name"), blank).text == "Alexandra"
    assert map_field(control("Legal first name"), blank).text == "Alexandra"


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


def test_screening_uses_the_profile_first_and_asks_when_an_approved_answer_disagrees():
    """t60u Req2: Application Profile first, then approved answers. A disagreement is not guessed."""
    person = profile()
    assert person.screening.how_heard == "Online job board"
    assert person.screening.age_18_or_older == "Yes"
    assert person.screening.felony_conviction == "No"
    person.application_answers = [
        ApplicationAnswer(question="Where did you hear about this role?", answer="Employee referral"),
        ApplicationAnswer(question="Are you age 18 or older?", answer="No"),
        ApplicationAnswer(question="Have you been convicted of a felony?", answer="No"),
    ]
    heard = map_field(control("How did you hear about this role?"), person)
    assert heard.action == "unanswered"
    assert heard.field_class == "MANUAL_REVIEW_FIELD"
    assert "disagree" in heard.reason
    age = map_field(
        control(
            "Are you age 18 or older?",
            kind="select",
            options=[Option("Yes", "Yes"), Option("No", "No")],
        ),
        person,
    )
    assert age.action == "unanswered"
    assert age.field_class == "MANUAL_REVIEW_FIELD"
    felony = map_field(control("Have you been convicted of a felony?", kind="checkbox"), person)
    assert felony.action == "uncheck"
    assert felony.text == "No"
    assert felony.field_class == "PROFILE_FIELD"
    unknown = map_field(control("What is your favorite color?"), person)
    assert unknown.action == "unanswered"
    assert unknown.text == ""
    assert unknown.field_class == "UNKNOWN_FIELD"


def test_screening_uses_an_approved_answer_when_the_profile_is_blank():
    person = profile()
    person.screening.how_heard = ""
    person.application_answers = [
        ApplicationAnswer(question="Where did you hear about this role?", answer="Employee referral"),
    ]
    heard = map_field(control("How did you hear about this role?"), person)
    assert heard.action == "fill"
    assert heard.text == "Employee referral"
    assert heard.field_class == "APPROVED_QUESTION"


def test_eeo_decline_is_not_selected():
    filled = CandidateProfile.from_dict(
        {
            "personal": {"full_name": "Casey Example", "email": "casey.example@example.com"},
            "eeo_voluntary": {"gender": "Prefer not to say"},
        }
    )
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
        filled,
    )
    assert mapped.action == "unanswered"
    assert mapped.key == "gender"


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


def test_salary_uses_profile_floor_and_jd_expectation():
    filled = profile()
    base = map_field(control("Desired salary"), filled)
    assert base.text == "85000"
    posted = map_field(
        control("Desired salary"),
        filled,
        job=JobContext(posted_salary_min=100000, posted_salary_max=140000),
    )
    # Midpoint 120000 is between 100000 and 130000, so the fill is 115000.
    assert posted.text == "115000"
    posted_low = map_field(
        control("Desired salary"),
        filled,
        job=JobContext(posted_salary_min=40000, posted_salary_max=60000),
    )
    assert posted_low.text == "50000"
    hourly = map_field(control("Hourly rate"), filled)
    assert hourly.text == str(85000 // 2080)
    ranged = map_field(control("Salary range"), filled)
    assert ranged.text == "85000-100000"


def _minimum_only() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {
            "personal": {"full_name": "Casey Example", "email": "casey.example@example.com"},
            "compensation": {"salary_expectation": "120000"},
        }
    )


def test_jd_single_figure_fills_expected_salary_and_not_a_range():
    person = _minimum_only()
    job = JobContext(posted_salary_min=150000)
    single = map_field(control("Desired salary"), person, job=job)
    assert single.action == "fill"
    assert single.text == "140000"
    ranged = map_field(control("What is your desired salary range?"), person, job=job)
    assert ranged.action == "unanswered"
    assert ranged.challenge == "confirm"
    assert ranged.text == ""
    assert ranged.text != "140000-140000"


def test_related_salary_labels_use_a_saved_minimum():
    person = _minimum_only()
    assert classify_question("What is your desired salary range?").intent == "SALARY_EXPECTATION"
    assert classify_question("Expected pay").intent == "SALARY_EXPECTATION"
    single = map_field(control("Desired salary"), person)
    assert single.action == "fill"
    assert single.text == "120000"
    assert single.challenge == ""
    expected = map_field(control("Expected pay"), person)
    assert expected.key == "salary"
    assert expected.text == "120000"
    endpoint = map_field(control("Salary range minimum"), person)
    assert endpoint.action == "fill"
    assert endpoint.text == "120000"


def test_desired_salary_range_confirms_when_only_a_minimum_is_saved():
    person = _minimum_only()
    label = "What is your desired salary range?"
    mapped = map_field(control(label), person)
    assert mapped.key == "salary_range"
    assert mapped.action == "unanswered"
    assert mapped.text == ""
    assert mapped.challenge == "confirm"
    assert mapped.confidence == "low"
    assert mapped.reason == "salary_range_incomplete"
    assert mapped.related_fields == ("salary_expectation",)
    assert "120000" not in mapped.reason
    gap = _gap_item([control(label, required=True)], person)
    assert gap["intent"] == "SALARY_EXPECTATION"
    assert gap["challenge"] == "confirm"
    assert gap["confidence"] == "LOW"
    assert gap["salaryShape"] == "range"
    assert gap["relatedFields"] == ["salary_expectation"]
    assert gap["kind"] == "MISSING_PROFILE_VALUE"
    published = public_blocking(gap)
    assert published is not None
    assert published["challenge"] == "confirm"
    assert published["relatedFields"] == ["salary_expectation"]
    assert published["reason"] == "salary_range_incomplete"
    assert "120000" not in repr(published)
    body = BlockingOut.model_validate(published).model_dump(by_alias=True)
    assert body["challenge"] == "confirm"
    assert body["salaryShape"] == "range"
    assert "120000" not in repr(body)
    plain = BlockingOut(kind="QUESTION", text="Nickname").model_dump(by_alias=True)
    assert "challenge" not in plain
    assert "relatedFields" not in plain


def test_minimum_alias_does_not_invent_a_range_max():
    person = CandidateProfile.from_dict(
        {
            "personal": {"full_name": "Casey Example", "email": "casey.example@example.com"},
            "compensation": {"minimumSalary": "90000"},
        }
    )
    assert person.compensation.salary_range_min == "90000"
    assert person.compensation.salary_range_max == ""
    assert person.compensation.salary_expectation == ""
    single = map_field(control("Minimum salary"), person)
    assert single.action == "fill"
    assert single.text == "90000"
    ranged = map_field(control("What is your desired salary range?"), person)
    assert ranged.action == "unanswered"
    assert ranged.challenge == "confirm"
    assert ranged.text == ""
    assert ranged.related_fields == ("salary_range_min",)


def test_resolver_question_names_related_salary_fields_without_amounts():
    match = classify_question("What is your desired salary range?")
    body = _Ask(
        controls=[control("What is your desired salary range?")],
        match=match,
        include_question=True,
    ).question_body()
    assert body["intent"] == "SALARY_EXPECTATION"
    assert body["salaryShape"] == "range"
    assert body["relatedFields"] == [
        "salary_expectation",
        "salary_range_min",
        "salary_range_max",
    ]
    single = _Ask(
        controls=[control("Desired salary")],
        match=classify_question("Desired salary"),
        include_question=True,
    ).question_body()
    assert single["salaryShape"] == "single"
    assert "relatedFields" not in _Ask(controls=[control("City")], include_question=True).question_body()


def test_non_iso_start_date_is_not_forced_into_a_date_input():
    mapped = map_field(control("Earliest start date", input_type="date"), profile())
    assert mapped.action == "unanswered"


def test_phone_country_menu_uses_the_address_or_the_calling_code():
    options = [
        Option("1", "Canada +1", selector="#ca"),
        Option("1", "United States +1", selector="#us"),
        Option("44", "United Kingdom +44", selector="#uk"),
    ]
    united = CandidateProfile.from_dict(
        {
            "personal": {
                "full_name": "River Example",
                "email": "river.example@example.com",
                "phone": "+44 20 7946 0000",
            },
            "address": {"country": "United States"},
        }
    )
    chosen = map_field(control("Country", kind="select", options=options), united)
    assert chosen.key == "phone_country"
    assert chosen.action == "select"
    assert chosen.option_label == "United States +1"
    plain = map_field(
        control(
            "Country",
            kind="select",
            options=[Option("US", "United States"), Option("CA", "Canada")],
        ),
        united,
    )
    assert plain.key == "country"
    assert plain.option_label == "United States"
    code = map_field(control("Country code", kind="combobox", options=options), united)
    assert code.option_label == "United States +1"
    uk = CandidateProfile.from_dict(
        {
            "personal": {
                "full_name": "Casey Example",
                "email": "casey.example@example.com",
                "phone": "0044 20 7946 0000",
            }
        }
    )
    from_phone = map_field(control("Country", kind="select", options=options), uk)
    assert from_phone.option_label == "United Kingdom +44"
    blank = CandidateProfile.from_dict(
        {
            "personal": {
                "full_name": "Avery Example",
                "email": "avery.example@example.com",
                "phone": "5550100000",
            }
        }
    )
    unanswered = map_field(control("Country", kind="select", options=options), blank)
    assert unanswered.action == "unanswered"


def test_unrecognized_control_is_left_alone():
    mapped = map_field(control("Pronouns"), profile())
    assert mapped.action == "unanswered"
    assert mapped.key is None


def test_demographic_values_are_never_mapped():
    filled = CandidateProfile.from_dict(
        {
            "personal": {"full_name": "Casey Example", "email": "casey.example@example.com"},
            "eeo_voluntary": {
                "gender": "Decline to self-identify",
                "veteran_status": "I am not a protected veteran",
                "disability_status": "I do not wish to answer",
                "race_ethnicity": "Decline to self-identify",
            },
        }
    )
    assert filled.eeo_voluntary.veteran_status == "I am not a protected veteran"
    veteran = control(
        "Veteran status",
        kind="select",
        required=True,
        options=[Option("I am not a protected veteran", "I am not a protected veteran")],
    )
    mapped = map_field(veteran, filled)
    assert mapped.action == "unanswered"
    assert mapped.key == "veteran_status"
    assert map_field(control("Gender", kind="select", options=[Option("Decline", "Decline")]), filled).action == (
        "unanswered"
    )
