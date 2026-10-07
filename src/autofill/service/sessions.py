"""Ephemeral sessions. Nothing here is written to disk or to another application's database."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field

from autofill.credentials import MemoryCredentialProvider
from autofill.engine import ApplicationResult
from autofill.models import JobContext
from autofill.profile import CandidateProfile


@dataclass
class ServiceSession:
    """One candidate, one job, one browser run. Credentials live only on this object."""

    session_id: str
    candidate_id: str
    job_id: str
    application_url: str
    profile: CandidateProfile
    job: JobContext
    cover_letter_text: str | None
    credentials: MemoryCredentialProvider = field(default_factory=MemoryCredentialProvider)
    status: str = "CREATED"
    result: ApplicationResult | None = None
    busy: bool = False

    def wipe_secrets(self) -> None:
        """Forget site passwords as soon as the session is no longer reachable."""
        self.credentials.clear()


class ServiceSessionStore:
    """In-memory sessions. Looking up one id never returns another session."""

    def __init__(self, *, max_sessions: int = 32) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be at least 1.")
        self._max_sessions = max_sessions
        self._sessions: dict[str, ServiceSession] = {}
        self._lock = threading.Lock()

    def create(self, session: ServiceSession) -> ServiceSession:
        with self._lock:
            if len(self._sessions) >= self._max_sessions:
                raise RuntimeError("Too many open sessions.")
            if session.session_id in self._sessions:
                raise ValueError(f"Session {session.session_id} is already open.")
            self._sessions[session.session_id] = session
            return session

    def get(self, session_id: str) -> ServiceSession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def begin_run(self, session_id: str) -> ServiceSession:
        """Mark a session busy. A second run on the same id fails closed."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            if session.busy:
                raise RuntimeError("Session is already running.")
            session.busy = True
            return session

    def finish_run(self, session: ServiceSession) -> None:
        """Clear the busy flag, or drop secrets when a delete already removed the session."""
        with self._lock:
            if self._sessions.get(session.session_id) is None:
                session.wipe_secrets()
            else:
                session.busy = False

    def delete(self, session_id: str) -> ServiceSession | None:
        with self._lock:
            session = self._sessions.pop(session_id, None)
            if session is not None and not session.busy:
                session.wipe_secrets()
            return session

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


def new_session_id() -> str:
    return uuid.uuid4().hex
