"""Per-application sessions. There is no process-wide last profile."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from autofill.ats import site_domain
from autofill.profile import CandidateProfile


@dataclass
class Session:
    """One candidate, one job, one application URL."""

    session_id: str
    candidate_id: str
    job_id: str
    application_url: str
    profile: CandidateProfile
    current_step: str = "OPEN_URL"
    steps: list[str] = field(default_factory=list)
    timings: dict[str, int] = field(default_factory=dict)

    def mark(self, step: str) -> None:
        self.current_step = step
        self.steps.append(step)

    def safe_log(self) -> dict[str, str]:
        """Ids, domain, and step only. No profile values and no credentials."""
        return {
            "sessionId": self.session_id,
            "candidateId": self.candidate_id,
            "jobId": self.job_id,
            "domain": site_domain(self.application_url),
            "step": self.current_step,
        }


class SessionStore:
    """Isolated sessions. Opening one does not read or replace another."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def open(
        self,
        *,
        profile: CandidateProfile,
        application_url: str,
        job_id: str = "",
        candidate_id: str | None = None,
        session_id: str = "",
    ) -> Session:
        sid = session_id or uuid.uuid4().hex
        if sid in self._sessions:
            raise ValueError(f"Session {sid} is already open.")
        cid = candidate_id if candidate_id is not None else profile.candidate_id
        session = Session(
            session_id=sid,
            candidate_id=cid,
            job_id=job_id,
            application_url=application_url,
            profile=profile,
        )
        self._sessions[sid] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)
