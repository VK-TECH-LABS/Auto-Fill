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
import re
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
    address_line2: str = ""
    city: str = ""
    province_state: str = ""
    country: str = ""
    postal_code: str = ""
    preferred_name: str = ""
    middle_name: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    portfolio_url: str = ""
    website_url: str = ""
    # Given and family names from the TileArc handoff. Empty means split full_name.
    first_name: str = ""
    family_name: str = ""

    @property
    def first_token(self) -> str:
        explicit = self.first_name.strip()
        if explicit:
            return explicit
        # A payload that only sent a family name must not be typed into First name.
        if self.family_name.strip() and self.full_name.strip() == self.family_name.strip():
            return ""
        return split_name(self.full_name)[0]

    @property
    def last_name(self) -> str:
        explicit = self.family_name.strip()
        if explicit:
            return explicit
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
    """Voluntary self-identification. Never filled; a person answers these."""

    gender: str = ""
    race_ethnicity: str = ""
    veteran_status: str = ""
    disability_status: str = ""


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
    # Only a location stored on this row. The home address is not a job site.
    location: str = ""
    # Role text stored on this row. Empty stays empty.
    description: str = ""


@dataclass
class EducationEntry:
    school: str = ""
    degree: str = ""
    field_of_study: str = ""
    start_date: str = ""
    end_date: str = ""
    current: bool = False
    location: str = ""
    description: str = ""


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
    # TileArc's canonical intent for this saved answer (e.g. SPONSORSHIP_FUTURE),
    # when known. It is checked against the question text before it is used.
    intent: str = ""
    # Employer this answer belongs to. Empty means the host did not name one.
    # Previously-employed answers are not reused for a different employer.
    employer: str = ""
    # True when this fill verified the value. A host-supplied row stays False.
    from_session: bool = False
    # Catalogue key. Recruitment SMS is ``recruitment_sms_consent``.
    key: str = ""


def _how_heard_question(text: str) -> bool:
    """Hear About wording. There is no HOW_HEARD intent on the LIVE payload."""
    folded = text.casefold()
    return any(
        phrase in folded
        for phrase in ("how did you hear", "how you heard", "how heard", "how did you find", "where did you hear")
    )


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
    # Top-level create-body ``sourceUrl`` only. The apply URL is not a source.
    source_url: str = ""
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
        given = _text(personal_raw, "first_name")
        family = _text(personal_raw, "last_name")
        full_name = _text(personal_raw, "full_name")
        if not full_name:
            full_name = " ".join(part for part in (given, family) if part).strip()
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

        built = cls(
            personal=Personal(
                full_name=full_name,
                email=email,
                first_name=given,
                family_name=family,
                phone=_text(personal_raw, "phone"),
                address=_text(personal_raw, "address"),
                address_line2=_text(personal_raw, "address_line2"),
                city=_text(personal_raw, "city"),
                province_state=_text(personal_raw, "province_state"),
                country=_text(personal_raw, "country"),
                postal_code=_text(personal_raw, "postal_code"),
                preferred_name=_text(personal_raw, "preferred_name"),
                middle_name=_text(personal_raw, "middle_name") or _text(personal_raw, "middleName"),
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
                gender=_text(eeo, "gender"),
                race_ethnicity=_text(eeo, "race_ethnicity"),
                veteran_status=_text(eeo, "veteran_status"),
                disability_status=_text(eeo, "disability_status"),
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
            application_answers=_answer_list(
                data.get("applicationAnswers") or data.get("application_answers"),
                _text(data, "candidateId") or _text(data, "candidate_id"),
            ),
        )
        if not built.screening.how_heard:
            for item in built.application_answers:
                heard = _how_heard_question(item.question) or item.key == "how_heard"
                if heard and item.answer.strip():
                    built.screening.how_heard = item.answer.strip()
                    break
        return built


def _alias_personal_keys(personal: dict) -> None:
    """Copy Fillo handoff names onto the keys the profile stores.

    ``firstName`` and ``lastName`` stay separate. They are not joined and
    then split again. ``state`` is the province field.
    """
    aliases = {
        "firstName": "first_name",
        "givenName": "first_name",
        "given_name": "first_name",
        "lastName": "last_name",
        "familyName": "last_name",
        "family_name": "last_name",
        "surname": "last_name",
        "fullName": "full_name",
        "legalName": "full_name",
        "legal_name": "full_name",
        "state": "province_state",
        "province": "province_state",
        "region": "province_state",
        "postalCode": "postal_code",
        "zip": "postal_code",
        "middleName": "middle_name",
        "linkedinUrl": "linkedin_url",
        "linkedInUrl": "linkedin_url",
        "linkedin": "linkedin_url",
        "githubUrl": "github_url",
        "github": "github_url",
        "portfolioUrl": "portfolio_url",
        "portfolio": "portfolio_url",
        "websiteUrl": "website_url",
        "website": "website_url",
    }
    for source, dest in aliases.items():
        if personal.get(source) not in (None, "") and not personal.get(dest):
            personal[dest] = personal[source]


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
            "line2": "address_line2",
            "line_2": "address_line2",
            "street2": "address_line2",
            "address2": "address_line2",
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
    links = merged.get("links")
    if isinstance(links, dict):
        link_alias = {
            "linkedin": "linkedin_url",
            "linkedinUrl": "linkedin_url",
            "github": "github_url",
            "githubUrl": "github_url",
            "portfolio": "portfolio_url",
            "portfolioUrl": "portfolio_url",
            "website": "website_url",
            "websiteUrl": "website_url",
        }
        for source, dest in link_alias.items():
            if links.get(source) not in (None, "") and not personal.get(dest):
                personal[dest] = links[source]
    _alias_personal_keys(personal)
    merged["personal"] = personal

    compensation = dict(_section(merged, "compensation"))
    # A saved minimum is the floor or the low end of a range. It is not a max.
    _compensation_aliases = {
        "salaryExpectation": "salary_expectation",
        "desiredSalary": "salary_expectation",
        "desired_salary": "salary_expectation",
        "minimumSalary": "salary_range_min",
        "minimum_salary": "salary_range_min",
        "salaryMin": "salary_range_min",
        "salary_min": "salary_range_min",
        "salaryRangeMin": "salary_range_min",
        "salaryRangeMax": "salary_range_max",
        "maximumSalary": "salary_range_max",
        "maximum_salary": "salary_range_max",
        "salaryMax": "salary_range_max",
        "salary_max": "salary_range_max",
    }
    for source, dest in _compensation_aliases.items():
        incoming = merged.get(source)
        if incoming not in (None, "") and not compensation.get(dest):
            compensation[dest] = incoming
        nested = compensation.get(source)
        if nested not in (None, "") and not compensation.get(dest):
            compensation[dest] = nested
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


def _pad_month(value: str) -> str:
    text = value.strip()
    if text.isdigit() and 1 <= int(text) <= 12:
        return f"{int(text):02d}"
    return ""


def _normalize_from_to(value: str) -> str:
    """``MM/YYYY``, or ``YYYY`` when the payload has no month.

    ``03-2019`` and ``Jan-2019`` are the same month and year. A year alone stays
    a year. A missing month is not invented.
    """
    text = value.strip()
    if re.fullmatch(r"(?:19|20)\d{2}", text):
        return text
    from autofill.dates import parse_month_year

    # A full day/month/year string is not a from/to month. Leave it unchanged.
    if re.fullmatch(r"\d{1,2}[./-]\d{1,2}[./-]\d{4}", text):
        return text
    parsed = parse_month_year(text)
    if parsed:
        month, year = parsed
        return f"{month:02d}/{year}"
    return text


def _first_text(entry: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = _text(entry, key)
        if value:
            return value
    return ""


def _date_text(entry: dict, which: str) -> str:
    """Month and year for one side of a work or education row.

    vinayaka #176 sends ``start_month`` / ``start_year`` (and the end pair) plus
    ``from`` / ``to``. Resolver rows use ``startMonth`` / ``startYear`` / ``from``
    / ``to``. Those win when they are present. A current LIVE row that only has
    ``start_date`` / ``end_date`` (or ``startDate`` / ``endDate``) keeps that
    string, and the filler reads the month and year from it.
    """
    if which == "start":
        month_keys = ("start_month", "startMonth", "from_month", "fromMonth")
        year_keys = ("start_year", "startYear", "from_year", "fromYear")
        explicit_keys = ("from", "fromDate", "from_date")
        legacy_keys = ("start_date", "startDate")
    else:
        month_keys = ("end_month", "endMonth", "to_month", "toMonth")
        year_keys = ("end_year", "endYear", "to_year", "toYear")
        explicit_keys = ("to", "toDate", "to_date")
        legacy_keys = ("end_date", "endDate")
    month = _pad_month(_first_text(entry, month_keys))
    year = _first_text(entry, year_keys)
    if month and re.fullmatch(r"(?:19|20)\d{2}", year):
        return f"{month}/{year}"
    if not month and re.fullmatch(r"(?:19|20)\d{2}", year) and not _first_text(entry, explicit_keys):
        return year
    explicit = _first_text(entry, explicit_keys)
    if explicit:
        return _normalize_from_to(explicit)
    return _first_text(entry, legacy_keys)


def _new_date_shape(entry: dict) -> bool:
    keys = (
        "from",
        "to",
        "start_month",
        "startMonth",
        "start_year",
        "startYear",
        "end_month",
        "endMonth",
        "end_year",
        "endYear",
    )
    return any(entry.get(key) not in (None, "") for key in keys)


def _has_end(entry: dict) -> bool:
    keys = (
        "to",
        "toDate",
        "to_date",
        "end_date",
        "endDate",
        "end_month",
        "endMonth",
        "end_year",
        "endYear",
    )
    return any(entry.get(key) not in (None, "") for key in keys)


def _current_job(entry: dict) -> bool:
    """True when the payload says the job is current.

    An explicit flag wins. On a #176 row, an omitted ``to`` is the same fact.
    A LIVE row that simply has no end date stays not-current.
    """
    for key in ("current", "currentlyWorking", "currently_working", "isCurrent", "present"):
        if entry.get(key) is True:
            return True
        if entry.get(key) is False:
            return False
    return _new_date_shape(entry) and not _has_end(entry)


_LOCATION_KEYS = (
    "location",
    "city",
    "job_location",
    "jobLocation",
    "work_location",
    "workLocation",
)
_DESCRIPTION_KEYS = (
    "role_description",
    "roleDescription",
    "role_summary",
    "roleSummary",
    "description",
    "summary",
    "responsibilities",
    "responsibility",
    "duties",
)


def _location_text(entry: dict) -> str:
    """Job or school location. A stored city is that place, not the home address."""
    return _first_text(entry, _LOCATION_KEYS)


def _description_text(entry: dict) -> str:
    return _first_text(entry, _DESCRIPTION_KEYS)


def _explicit_flag(entry: dict, keys: tuple[str, ...]) -> bool | None:
    for key in keys:
        if entry.get(key) is True:
            return True
        if entry.get(key) is False:
            return False
    return None


def _employment_list(raw: object) -> list[Employment]:
    return [
        Employment(
            company=_text(entry, "company") or _text(entry, "employer"),
            title=_text(entry, "title") or _text(entry, "jobTitle") or _text(entry, "job_title"),
            start_date=_date_text(entry, "start"),
            end_date=_date_text(entry, "end"),
            current=_current_job(entry),
            location=_location_text(entry),
            description=_description_text(entry),
        )
        for entry in _objects(raw, "employment")
    ]


def _education_current(entry: dict) -> bool:
    """Explicit only. An omitted graduation date is not 'currently attending'."""
    flag = _explicit_flag(
        entry,
        (
            "current",
            "currentlyAttending",
            "currently_attending",
            "currentlyEnrolled",
            "currently_enrolled",
            "isCurrent",
            "present",
        ),
    )
    return flag is True


def _education_list(raw: object) -> list[EducationEntry]:
    return [
        EducationEntry(
            school=_text(entry, "school") or _text(entry, "institution"),
            degree=_text(entry, "degree"),
            field_of_study=_text(entry, "field") or _text(entry, "fieldOfStudy") or _text(entry, "field_of_study"),
            start_date=_date_text(entry, "start"),
            end_date=_date_text(entry, "end"),
            current=_education_current(entry),
            location=_location_text(entry),
            description=_description_text(entry),
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


def _stored_intent(value: str) -> str:
    """Keep a host intent. ``SMS_CONSENT`` is the recruitment-SMS alias."""
    from autofill.sms_consent import canonical_sms_intent

    return canonical_sms_intent(value)[:64]


def _label_of(raw: object) -> str:
    if isinstance(raw, dict):
        return _text(raw, "label") or _text(raw, "value") or _text(raw, "name")
    if isinstance(raw, str):
        return raw.strip()
    return ""


def _label_list(raw: object) -> list[str]:
    if not isinstance(raw, list):
        return []
    labels: list[str] = []
    for item in raw:
        label = _label_of(item)
        if label and label not in labels:
            labels.append(label)
    return labels


def _selection_text(entry: dict) -> str:
    """The committed choice. A taxonomy path is ``Parent > Leaf``.

    ``leaves`` are in commit order. ``leaf`` is the one committed leaf.
    ``values`` are the selected labels. ``answer`` is the saved text, often
    those labels joined with `` | ``. A parent label alone is not preferred
    over the leaf.
    """
    leaves = _label_list(entry.get("leaves"))
    leaf = _label_of(entry.get("leaf"))
    values = _label_list(entry.get("values"))
    answer = _text(entry, "answer")
    if len(leaves) >= 2:
        return " > ".join(leaves)
    if leaf:
        if len(values) >= 2 and values[-1].casefold() == leaf.casefold():
            return " > ".join(values)
        if " > " in answer and leaf.casefold() in answer.casefold():
            return answer
        return leaf
    if leaves:
        return leaves[-1]
    if " > " in answer:
        return answer
    return answer


def _answer_row(entry: dict) -> ApplicationAnswer:
    """One application answer. Recruitment SMS is normalized to the host key.

    Hear About has no intent. The leaf path is the answer the dropdown commits.
    """
    from autofill.sms_consent import SMS_INTENT, SMS_KEY, canonical_sms_answer, canonical_sms_key

    question = _text(entry, "question")
    intent = _stored_intent(_text(entry, "intent"))
    host_key = _text(entry, "key").strip()
    key = canonical_sms_key(host_key) or canonical_sms_key(question)
    if not key and host_key and " " not in host_key:
        key = host_key[:64]
    if intent == SMS_INTENT:
        key = key or SMS_KEY
    if key == SMS_KEY and not intent:
        intent = SMS_INTENT
    if not question and key:
        question = key
    answer = _selection_text(entry)
    if key == SMS_KEY or intent == SMS_INTENT:
        answer = canonical_sms_answer(answer or _text(entry, "answer"))
    return ApplicationAnswer(
        question=question,
        answer=answer,
        intent=intent,
        employer=_text(entry, "employer")[:200],
        key=key,
    )


def _answer_list(raw: object, owner: str = "") -> list[ApplicationAnswer]:
    """Approved answers for this candidate.

    A row that names a different ``candidateId`` is dropped. A row with no
    candidate id stays, which is how older payloads are shaped.
    """
    rows: list[ApplicationAnswer] = []
    for entry in _objects(raw, "applicationAnswers"):
        tagged = _text(entry, "candidateId") or _text(entry, "candidate_id")
        if owner and tagged and tagged != owner:
            continue
        rows.append(_answer_row(entry))
    return rows
