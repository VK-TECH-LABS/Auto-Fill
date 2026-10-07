"""HTTP bodies for the versioned Auto-Fill session API.

Examples use fictional people. Do not put a real service token, resume, or
candidate record in this schema.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class JobContextIn(BaseModel):
    """Opaque job identifiers plus the application URL the engine should open."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    application_url: str = Field(alias="applicationUrl", min_length=1)
    job_id: str = Field(default="", alias="jobId")
    candidate_id: str = Field(default="", alias="candidateId")
    title: str = ""
    company: str = ""
    posted_salary_min: int | None = Field(default=None, alias="postedSalaryMin")
    posted_salary_max: int | None = Field(default=None, alias="postedSalaryMax")


class ApprovedAnswerIn(BaseModel):
    """One question the caller has already approved. Unknown questions stay blank."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    answer: str = Field(min_length=1)

    @field_validator("question", "answer")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned


class CreateSessionIn(BaseModel):
    """Create one isolated fill session. Credentials are a different request."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    candidate_context: dict[str, Any] = Field(alias="candidateContext")
    job_context: JobContextIn = Field(alias="jobContext")
    approved_answers: list[ApprovedAnswerIn] = Field(default_factory=list, alias="approvedAnswers")


class CandidateCheckIn(BaseModel):
    """Optional candidate id. When present it must match the session or the call stops."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    candidate_id: str = Field(default="", alias="candidateId")


class ContinueIn(BaseModel):
    """Confirmation that a person uploaded the resume. This is not a submit."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    resume_uploaded: bool = Field(alias="resumeUploaded")
    candidate_id: str = Field(default="", alias="candidateId")


class CredentialsIn(BaseModel):
    """Site login for this session only. The response never echoes the secret."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    password: str = Field(min_length=1)
    email: str = ""
    username: str = ""
    site: str = ""
    candidate_id: str = Field(default="", alias="candidateId")


class RunStatusOut(BaseModel):
    """Public run state. Field values, resumes, and secrets are not included."""

    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    status: str
    stopped_before_submit: Literal[True] = True
    candidate_id: str = Field(alias="candidateId")
    job_id: str = Field(alias="jobId")
    ats: str | None = None
    current_step: str = Field(default="", alias="currentStep")
    login_status: str = Field(default="", alias="loginStatus")
    fields_detected: int = Field(default=0, alias="fieldsDetected")
    fields_filled: int = Field(default=0, alias="fieldsFilled")
    fields_skipped: int = Field(default=0, alias="fieldsSkipped")
    manual_actions: list[str] = Field(default_factory=list, alias="manualActions")
    steps: list[str] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    submit_controls: list[str] = Field(default_factory=list, alias="submitControls")


class CredentialsOut(BaseModel):
    """Acknowledgement only. The password is not returned."""

    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    stored: Literal[True] = True


class HealthOut(BaseModel):
    """Unauthenticated process check. It does not report sessions or config."""

    status: Literal["ok"] = "ok"
    service: Literal["auto-fill"] = "auto-fill"
