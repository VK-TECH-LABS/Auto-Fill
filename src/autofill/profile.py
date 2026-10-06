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
    profile does not have to embed machine-specific paths. Cover-letter text
    is used only when the profile or the API call supplies it. Nothing is
    generated.
    """

    cover_letter_text: str = ""


@dataclass
class Employment:
    company: str = ""
    title: str = ""
    start_date: str = ""
    end_date: str = ""
    current: bool = False


@dataclass
class EducationEntry:
    school: str = ""
    degree: str = ""
    field_of_study: str = ""
    start_date: str = ""
    end_date: str = ""


@dataclass
class Internship:
    company: str = ""
    title: str = ""


@dataclass
class Project:
    name: str = ""
    description: str = ""


@dataclass
class ApplicationAnswer:
    """An explicit answer. Unknown questions are never guessed from this list."""

    question: str
    answer: str


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
    candidate_id: str = ""
    employment: list[Employment] = field(default_factory=list)
    education_history: list[EducationEntry] = field(default_factory=list)
    internships: list[Internship] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)
    application_answers: list[ApplicationAnswer] = field(default_factory=list)

    def phone_digits(self) -> str:
        return "".join(character for character in self.personal.phone if character.isdigit())

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
        data = _accept_extended_shape(data)
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
            candidate_id=_text(data, "candidateId") or _text(data, "candidate_id"),
            employment=_employment_list(data.get("employment")),
            education_history=_education_list(data.get("education")),
            internships=_internship_list(data.get("internships")),
            projects=_project_list(data.get("projects")),
            application_answers=_answer_list(data.get("applicationAnswers") or data.get("application_answers")),
        )


def _accept_extended_shape(data: dict) -> dict:
    """Fold ``basics``, ``address``, and top-level aliases into the original sections.

    The original ApplyPilot-shaped sections still work. ``personal`` wins when
    both ``basics`` and ``personal`` set the same key. The caller's dict is
    not mutated.
    """
    merged = dict(data)
    personal = dict(_section(merged, "personal"))
    basics = merged.get("basics")
    if isinstance(basics, dict):
        if "password" in basics:
            warnings.warn(
                "Ignoring profile basics.password. Auto-Fill does not store passwords in the profile.",
                stacklevel=2,
            )
        for key, value in basics.items():
            if key == "password":
                continue
            personal.setdefault(key, value)
    address = merged.get("address")
    if isinstance(address, dict):
        alias = {
            "line1": "address",
            "street": "address",
            "city": "city",
            "state": "province_state",
            "province": "province_state",
            "province_state": "province_state",
            "postalCode": "postal_code",
            "postal_code": "postal_code",
            "zip": "postal_code",
            "country": "country",
        }
        for source, dest in alias.items():
            if address.get(source) and not personal.get(dest):
                personal[dest] = address[source]
    merged["personal"] = personal

    compensation = dict(_section(merged, "compensation"))
    if merged.get("salaryExpectation") not in (None, "") and not compensation.get("salary_expectation"):
        compensation["salary_expectation"] = merged["salaryExpectation"]
        merged["compensation"] = compensation

    work = dict(_section(merged, "work_authorization"))
    raw_auth = merged.get("workAuthorization")
    if raw_auth not in (None, "") and not work.get("legally_authorized_to_work"):
        if isinstance(raw_auth, str):
            work["legally_authorized_to_work"] = raw_auth
        elif isinstance(raw_auth, dict):
            work["legally_authorized_to_work"] = raw_auth.get("authorized") or raw_auth.get(
                "legally_authorized_to_work", ""
            )
    if merged.get("sponsorship") not in (None, "") and not work.get("require_sponsorship"):
        work["require_sponsorship"] = merged["sponsorship"]
    merged["work_authorization"] = work
    return merged


def _objects(raw: object, label: str) -> list[dict]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ProfileError(f"{label} must be a list.")
    items: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise ProfileError(f"{label} entries must be objects.")
        items.append(entry)
    return items


def _employment_list(raw: object) -> list[Employment]:
    return [
        Employment(
            company=_text(entry, "company") or _text(entry, "employer"),
            title=_text(entry, "title") or _text(entry, "jobTitle") or _text(entry, "job_title"),
            start_date=_text(entry, "startDate") or _text(entry, "start_date"),
            end_date=_text(entry, "endDate") or _text(entry, "end_date"),
            current=bool(entry.get("current", False)),
        )
        for entry in _objects(raw, "employment")
    ]


def _education_list(raw: object) -> list[EducationEntry]:
    return [
        EducationEntry(
            school=_text(entry, "school") or _text(entry, "institution"),
            degree=_text(entry, "degree"),
            field_of_study=_text(entry, "field") or _text(entry, "fieldOfStudy") or _text(entry, "field_of_study"),
            start_date=_text(entry, "startDate") or _text(entry, "start_date"),
            end_date=_text(entry, "endDate") or _text(entry, "end_date"),
        )
        for entry in _objects(raw, "education")
    ]


def _internship_list(raw: object) -> list[Internship]:
    return [
        Internship(
            company=_text(entry, "company") or _text(entry, "organization"),
            title=_text(entry, "title"),
        )
        for entry in _objects(raw, "internships")
    ]


def _project_list(raw: object) -> list[Project]:
    return [
        Project(name=_text(entry, "name"), description=_text(entry, "description"))
        for entry in _objects(raw, "projects")
    ]


def _answer_list(raw: object) -> list[ApplicationAnswer]:
    return [
        ApplicationAnswer(question=_text(entry, "question"), answer=_text(entry, "answer"))
        for entry in _objects(raw, "applicationAnswers")
    ]
