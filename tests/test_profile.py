"""Profile schema: required identity fields, no passwords, fictional example."""

import warnings
from pathlib import Path

import pytest

from autofill.profile import CandidateProfile, ProfileError

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "profile.example.json"


def test_example_profile_is_fictional_and_has_no_password():
    raw = EXAMPLE.read_text(encoding="utf-8")
    assert "password" not in raw.lower()
    assert "example.com" in raw
    profile = CandidateProfile.load(EXAMPLE)
    assert profile.personal.full_name == "Casey Example"
    assert profile.personal.email == "casey.example@example.com"
    assert profile.phone_digits() == "5550100199"
    assert profile.personal.public_name == "Casey Example"
    assert "Python" in profile.skills


def test_password_key_is_discarded():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        profile = CandidateProfile.from_dict(
            {
                "personal": {
                    "full_name": "Casey Example",
                    "email": "casey.example@example.com",
                    "password": "not-a-real-secret",
                }
            }
        )
    assert not hasattr(profile.personal, "password")
    assert any("password" in str(item.message).lower() for item in caught)


def test_required_fields():
    with pytest.raises(ProfileError):
        CandidateProfile.from_dict({"personal": {"email": "casey.example@example.com"}})
    with pytest.raises(ProfileError):
        CandidateProfile.from_dict({"personal": {"full_name": "Casey Example", "email": "not-an-email"}})


def test_skills_must_be_strings():
    with pytest.raises(ProfileError):
        CandidateProfile.from_dict(
            {
                "personal": {"full_name": "Casey Example", "email": "casey.example@example.com"},
                "skills": ["Python", 3],
            }
        )


def test_preferred_name_is_used_for_the_public_name():
    profile = CandidateProfile.from_dict(
        {
            "personal": {
                "full_name": "Alexandra Example",
                "preferred_name": "Alex",
                "email": "alex.example@example.com",
            }
        }
    )
    assert profile.personal.public_name == "Alex Example"
    assert profile.personal.first_token == "Alexandra"
    assert profile.personal.preferred_or_first == "Alex"
    assert profile.personal.last_name == "Example"


def test_screening_defaults_are_blank():
    profile = CandidateProfile.from_dict(
        {"personal": {"full_name": "Casey Example", "email": "casey.example@example.com"}}
    )
    assert profile.screening.felony_conviction == ""
    assert profile.eeo_voluntary.gender == "Decline to self-identify"
