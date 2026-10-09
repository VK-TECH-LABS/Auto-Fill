"""HTTP bodies for the versioned Auto-Fill session API.

Examples use fictional people. Do not put a real service token, resume, or
candidate record in this schema.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_URL_LENGTH = 2048
MAX_REF_LENGTH = 128
MAX_TEXT = 200
MAX_TOKEN = 512
MIN_RESOLVER_TOKEN = 32
MAX_CONTEXT_BYTES = 100_000
MAX_ANSWERS = 32


class JobContextIn(BaseModel):
    """Opaque job identifiers plus the application URL the engine should open."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    application_url: str = Field(alias="applicationUrl", min_length=1, max_length=MAX_URL_LENGTH)
    job_id: str = Field(default="", alias="jobId", max_length=MAX_REF_LENGTH)
    candidate_id: str = Field(default="", alias="candidateId", max_length=MAX_REF_LENGTH)
    title: str = Field(default="", max_length=MAX_TEXT)
    company: str = Field(default="", max_length=MAX_TEXT)
    posted_salary_min: int | None = Field(default=None, alias="postedSalaryMin")
    posted_salary_max: int | None = Field(default=None, alias="postedSalaryMax")


class ApprovedAnswerIn(BaseModel):
    """One question the caller has already approved. Unknown questions stay blank."""

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1, max_length=500)
    answer: str = Field(min_length=1, max_length=2000)

    @field_validator("question", "answer")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned


class ResolverIn(BaseModel):
    """Per-session resolver. The token is memory-only and is not returned."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    url: str = Field(min_length=1, max_length=MAX_URL_LENGTH)
    token: str = Field(min_length=MIN_RESOLVER_TOKEN, max_length=MAX_TOKEN)
    expires_at: str = Field(alias="expiresAt", min_length=1, max_length=40)


class CreateSessionIn(BaseModel):
    """Create one isolated fill session. Credentials are a different request."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    protocol_version: str = Field(default="", alias="protocolVersion", max_length=16)
    candidate_ref: str = Field(default="", alias="candidateRef", max_length=MAX_REF_LENGTH)
    job_ref: str = Field(default="", alias="jobRef", max_length=MAX_REF_LENGTH)
    candidate_context: dict[str, Any] | None = Field(default=None, alias="candidateContext")
    job_context: JobContextIn = Field(alias="jobContext")
    approved_answers: list[ApprovedAnswerIn] = Field(
        default_factory=list,
        alias="approvedAnswers",
        max_length=MAX_ANSWERS,
    )
    resolver: ResolverIn | None = None

    @field_validator("candidate_context")
    @classmethod
    def _bound_context(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        if len(value) > 80:
            raise ValueError("candidateContext has too many keys")
        try:
            encoded = json.dumps(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("candidateContext is not valid JSON") from exc
        if len(encoded) > MAX_CONTEXT_BYTES:
            raise ValueError("candidateContext is too large")
        return value

    @model_validator(mode="after")
    def _mode(self) -> CreateSessionIn:
        version = self.protocol_version.strip()
        if self.resolver is not None or version == "0.4.0":
            if version != "0.4.0":
                raise ValueError("protocolVersion must be 0.4.0 when resolver is set")
            if self.resolver is None:
                raise ValueError("resolver is required for protocol 0.4.0")
            if not self.candidate_ref.strip() or not self.job_ref.strip():
                raise ValueError("candidateRef and jobRef are required")
            if "@" in self.candidate_ref or "@" in self.job_ref:
                raise ValueError("refs must be opaque ids")
            if self.candidate_context is not None or self.approved_answers:
                raise ValueError("resolver mode does not accept candidate profiles or answers")
        elif self.candidate_context is None:
            raise ValueError("candidateContext is required")
        return self


class CandidateCheckIn(BaseModel):
    """Optional candidate id. When present it must match the session or the call stops."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    candidate_id: str = Field(default="", alias="candidateId", max_length=MAX_REF_LENGTH)
    candidate_ref: str = Field(default="", alias="candidateRef", max_length=MAX_REF_LENGTH)


class ContinueIn(BaseModel):
    """Resume upload or a fresh resolver query for the same session. This is not a submit."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    resume_uploaded: bool = Field(default=False, alias="resumeUploaded")
    answers_updated: bool = Field(default=False, alias="answersUpdated")
    candidate_id: str = Field(default="", alias="candidateId", max_length=MAX_REF_LENGTH)
    candidate_ref: str = Field(default="", alias="candidateRef", max_length=MAX_REF_LENGTH)


class CredentialsIn(BaseModel):
    """Site login for this session only. The response never echoes the secret."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    password: str = Field(min_length=1, max_length=256)
    email: str = Field(default="", max_length=320)
    username: str = Field(default="", max_length=320)
    site: str = Field(default="", max_length=MAX_URL_LENGTH)
    candidate_id: str = Field(default="", alias="candidateId", max_length=MAX_REF_LENGTH)


class ManualQuestionOut(BaseModel):
    """A question left blank. The answer value is not included."""

    model_config = ConfigDict(populate_by_name=True)

    intent: str | None = None
    text: str = ""


class TimingsOut(BaseModel):
    """Millisecond durations. These are not timestamps and contain no field values."""

    model_config = ConfigDict(populate_by_name=True)

    session_created_ms: int = Field(default=0, alias="sessionCreatedMs")
    browser_ready_ms: int = Field(default=0, alias="browserReadyMs")
    first_form_inspected_ms: int = Field(default=0, alias="firstFormInspectedMs")


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
    protocol_version: str = Field(default="", alias="protocolVersion")
    manual_questions: list[ManualQuestionOut] = Field(default_factory=list, alias="manualQuestions")
    timings: TimingsOut | None = None


class CredentialsOut(BaseModel):
    """Acknowledgement only. The password is not returned."""

    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(alias="sessionId")
    stored: Literal[True] = True


class HealthOut(BaseModel):
    """Unauthenticated process check. It does not report sessions or config."""

    status: Literal["ok"] = "ok"
    service: Literal["auto-fill"] = "auto-fill"
