"""Candidate profile schema and loader.

Section names follow ApplyPilot's ``profile.example.json``: personal,
work authorization, availability, compensation, experience, and voluntary
EEO. Passwords are not part of the schema. ApplyPilot stored a site
password so its agent could log in and create accounts; this package does
not. Screening answers are explicit fields with empty defaults so the
mapper does not invent "No" for a felony question the candidate never
answered.

Real profiles belong in a gitignored file or a path passed at runtime.
The example shipped with this repo is fictional.
"""

from __future__ import annotations

import json
import warnings
from dataclasses import dataclass, field
from pathlib import Path


class ProfileError(ValueError):
    """The profile file is missing a required field or is not an object."""


def _section(data: dict, name: str) -> dict:
    raw = data.get(name, {})
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ProfileError(f"Profile section {name!r} must be an object.")
    return raw


def _text(section: dict, key: str, default: str = "") -> str:
    value = section.get(key, default)
    if value is None:
        return default
    return str(value).strip()


def split_name(full_name: str) -> tuple[str, str]:
    """Split a legal name into a first token and the remainder."""
    parts = full_name.split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


@dataclass
class Personal:
    full_name: str
    email: str
    phone: str = ""
    address: str = ""
    city: str = ""
    province_state: str = ""
    country: str = ""
    postal_code: str = ""
    preferred_name: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    website_url: str = ""

    @property
    def first_token(self) -> str:
        return split_name(self.full_name)[0]

    @property
    def last_name(self) -> str:
        return split_name(self.full_name)[1]

    @property
    def preferred_or_first(self) -> str:
        return self.preferred_name.strip() or self.first_token

    @property
    def public_name(self) -> str:
        """Preferred first name plus legal last name, when they differ.

        ApplyPilot's hard rule: use the preferred name unless the field
        asks for the legal name.
        """
        preferred = self.preferred_name.strip()
        if preferred and preferred.casefold() != self.first_token.casefold():
            return f"{preferred} {self.last_name}".strip()
        return self.full_name


@dataclass
class WorkAuthorization:
    legally_authorized_to_work: str = ""
    require_sponsorship: str = ""
    work_permit_type: str = ""


@dataclass
class Availability:
    earliest_start_date: str = ""
    available_for_full_time: str = ""
    available_for_contract: str = ""


@dataclass
class Compensation:
    salary_expectation: str = ""
    salary_currency: str = "USD"
    salary_range_min: str = ""
    salary_range_max: str = ""
    currency_conversion_note: str = ""


@dataclass
class Experience:
    years_of_experience_total: str = ""
    education_level: str = ""
    current_job_title: str = ""
    current_company: str = ""
    target_role: str = ""


@dataclass
class EeoVoluntary:
    """Voluntary self-identification. Defaults decline rather than guess."""

    gender: str = "Decline to self-identify"
    race_ethnicity: str = "Decline to self-identify"
    veteran_status: str = "I am not a protected veteran"
    disability_status: str = "I do not wish to answer"


@dataclass
class Screening:
    """Yes/no and short answers. Empty means leave the control for a person."""

    age_18_or_older: str = ""
    willing_background_check: str = ""
    felony_conviction: str = ""
    previously_employed_here: str = ""
    how_heard: str = ""


@dataclass
class Documents:
    """Text that can be pasted into a cover-letter box.

    File paths are passed to :func:`autofill.fill_application` so a shared
    profile does not have to embed machine-specific paths.
    """

    cover_letter_text: str = ""


@dataclass
class CandidateProfile:
    personal: Personal
    work_authorization: WorkAuthorization = field(default_factory=WorkAuthorization)
    availability: Availability = field(default_factory=Availability)
    compensation: Compensation = field(default_factory=Compensation)
    experience: Experience = field(default_factory=Experience)
    eeo_voluntary: EeoVoluntary = field(default_factory=EeoVoluntary)
    screening: Screening = field(default_factory=Screening)
    documents: Documents = field(default_factory=Documents)
    skills: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path) -> CandidateProfile:
        """Load a profile JSON file. A ``password`` key is discarded."""
        file_path = Path(path)
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProfileError(f"Profile is not valid JSON: {file_path}") from exc
        if not isinstance(raw, dict):
            raise ProfileError("Profile must be a JSON object.")
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, data: dict) -> CandidateProfile:
        personal_raw = dict(_section(data, "personal"))
        if "password" in personal_raw:
            warnings.warn(
                "Ignoring profile personal.password. Auto-Fill does not log in or store passwords.",
                stacklevel=2,
            )
            personal_raw.pop("password", None)
        full_name = _text(personal_raw, "full_name")
        email = _text(personal_raw, "email")
        if not full_name:
            raise ProfileError("personal.full_name is required.")
        if "@" not in email or email.startswith("@") or email.endswith("@"):
            raise ProfileError("personal.email must be an email address.")

        work = _section(data, "work_authorization")
        availability = _section(data, "availability")
        compensation = _section(data, "compensation")
        experience = _section(data, "experience")
        eeo = _section(data, "eeo_voluntary")
        screening = _section(data, "screening")
        documents = _section(data, "documents")
        skills_raw = data.get("skills", [])
        if skills_raw is None:
            skills_raw = []
        if not isinstance(skills_raw, list) or not all(isinstance(item, str) for item in skills_raw):
            raise ProfileError("skills must be a list of strings.")

        eeo_defaults = EeoVoluntary()
        return cls(
            personal=Personal(
                full_name=full_name,
                email=email,
                phone=_text(personal_raw, "phone"),
                address=_text(personal_raw, "address"),
                city=_text(personal_raw, "city"),
                province_state=_text(personal_raw, "province_state"),
                country=_text(personal_raw, "country"),
                postal_code=_text(personal_raw, "postal_code"),
                preferred_name=_text(personal_raw, "preferred_name"),
                linkedin_url=_text(personal_raw, "linkedin_url"),
                github_url=_text(personal_raw, "github_url"),
                portfolio_url=_text(personal_raw, "portfolio_url"),
                website_url=_text(personal_raw, "website_url"),
            ),
            work_authorization=WorkAuthorization(
                legally_authorized_to_work=_text(work, "legally_authorized_to_work"),
                require_sponsorship=_text(work, "require_sponsorship"),
                work_permit_type=_text(work, "work_permit_type"),
            ),
            availability=Availability(
                earliest_start_date=_text(availability, "earliest_start_date"),
                available_for_full_time=_text(availability, "available_for_full_time"),
                available_for_contract=_text(availability, "available_for_contract"),
            ),
            compensation=Compensation(
                salary_expectation=_text(compensation, "salary_expectation"),
                salary_currency=_text(compensation, "salary_currency", "USD") or "USD",
                salary_range_min=_text(compensation, "salary_range_min"),
                salary_range_max=_text(compensation, "salary_range_max"),
                currency_conversion_note=_text(compensation, "currency_conversion_note"),
            ),
            experience=Experience(
                years_of_experience_total=_text(experience, "years_of_experience_total"),
                education_level=_text(experience, "education_level"),
                current_job_title=_text(experience, "current_job_title"),
                current_company=_text(experience, "current_company"),
                target_role=_text(experience, "target_role"),
            ),
            eeo_voluntary=EeoVoluntary(
                gender=_text(eeo, "gender", eeo_defaults.gender) or eeo_defaults.gender,
                race_ethnicity=_text(eeo, "race_ethnicity", eeo_defaults.race_ethnicity)
                or eeo_defaults.race_ethnicity,
                veteran_status=_text(eeo, "veteran_status", eeo_defaults.veteran_status)
                or eeo_defaults.veteran_status,
                disability_status=_text(eeo, "disability_status", eeo_defaults.disability_status)
                or eeo_defaults.disability_status,
            ),
            screening=Screening(
                age_18_or_older=_text(screening, "age_18_or_older"),
                willing_background_check=_text(screening, "willing_background_check"),
                felony_conviction=_text(screening, "felony_conviction"),
                previously_employed_here=_text(screening, "previously_employed_here"),
                how_heard=_text(screening, "how_heard"),
            ),
            documents=Documents(cover_letter_text=_text(documents, "cover_letter_text")),
            skills=list(skills_raw),
        )

    def phone_digits(self) -> str:
        return "".join(character for character in self.personal.phone if character.isdigit())
