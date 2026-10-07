"""Turn one HTTP create-body into engine objects. No disk and no host database."""

from __future__ import annotations

from urllib.parse import urlparse

from autofill.ats import site_domain
from autofill.models import JobContext
from autofill.profile import ApplicationAnswer, CandidateProfile, ProfileError
from autofill.service.schemas import ApprovedAnswerIn, CreateSessionIn, CredentialsIn

_SECRET_KEYS = frozenset({"password", "databaseurl", "token", "apikey", "secret", "database_url"})


class ContextError(ValueError):
    """The caller sent a body this service will not run. ``status_code`` is 409 or 422."""

    def __init__(self, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def _reject_secret_keys(payload: dict) -> None:
    for key in payload:
        if str(key).casefold().replace("-", "_") in _SECRET_KEYS or str(key).casefold() in _SECRET_KEYS:
            raise ContextError("candidateContext must not include secrets.")
    for section_name in ("personal", "basics"):
        section = payload.get(section_name)
        if isinstance(section, dict) and any(str(key).casefold() == "password" for key in section):
            raise ContextError("candidateContext must not include secrets.")


def validate_application_url(url: str) -> str:
    """Accept an http(s) application URL with no userinfo and no embedded secret."""
    cleaned = url.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ContextError("applicationUrl must be an absolute http or https URL.")
    if parsed.username or parsed.password:
        raise ContextError("applicationUrl must not embed credentials.")
    return cleaned


def apply_approved_answers(profile: CandidateProfile, answers: list[ApprovedAnswerIn]) -> None:
    """Approved answers override the same question. Other profile answers stay."""
    merged: dict[str, ApplicationAnswer] = {}
    order: list[str] = []
    for existing in profile.application_answers:
        key = existing.question.casefold().strip()
        if not key:
            continue
        if key not in merged:
            order.append(key)
        merged[key] = ApplicationAnswer(question=existing.question, answer=existing.answer)
    for approved in answers:
        key = approved.question.casefold().strip()
        if key not in merged:
            order.append(key)
        merged[key] = ApplicationAnswer(question=approved.question.strip(), answer=approved.answer.strip())
    profile.application_answers = [merged[key] for key in order]


def build_profile(body: CreateSessionIn) -> tuple[CandidateProfile, JobContext, str | None]:
    """Validate ids and return the profile, job, and explicit cover-letter text."""
    if not isinstance(body.candidate_context, dict):
        raise ContextError("candidateContext must be an object.")
    _reject_secret_keys(body.candidate_context)
    try:
        profile = CandidateProfile.from_dict(body.candidate_context)
    except ProfileError as exc:
        raise ContextError(str(exc)) from exc
    apply_approved_answers(profile, body.approved_answers)
    candidate_id = profile.candidate_id.strip()
    if not candidate_id:
        raise ContextError("candidateContext.candidateId is required.")
    profile.candidate_id = candidate_id
    job_candidate = body.job_context.candidate_id.strip()
    if job_candidate and job_candidate != candidate_id:
        raise ContextError("Candidate does not match jobContext.", status_code=409)
    application_url = validate_application_url(body.job_context.application_url)
    job = JobContext(
        title=body.job_context.title.strip(),
        company=body.job_context.company.strip(),
        url=application_url,
        posted_salary_min=body.job_context.posted_salary_min,
        posted_salary_max=body.job_context.posted_salary_max,
    )
    cover = profile.documents.cover_letter_text.strip() or None
    return profile, job, cover


def credential_domain(application_url: str, site: str) -> str:
    """Domain for this session. A different site is rejected."""
    expected = site_domain(application_url)
    if not expected:
        raise ContextError("This session has no site domain to scope credentials to.")
    raw = site.strip()
    if not raw:
        return expected
    presented = site_domain(raw) or raw.lower()
    if presented != expected:
        raise ContextError("Credentials must be for this session's application site.", status_code=409)
    return expected


def require_login_name(body: CredentialsIn) -> None:
    """A password alone cannot identify the account."""
    if not body.email.strip() and not body.username.strip():
        raise ContextError("Credentials need an email or a username.")
