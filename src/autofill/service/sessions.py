"""Ephemeral sessions. Nothing here is written to disk or to another application's database."""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from autofill.credentials import MemoryCredentialProvider
from autofill.engine import ApplicationResult, Status
from autofill.models import JobContext
from autofill.profile import CandidateProfile


@dataclass
class ServiceSession:
    """One candidate, one job, one browser run. Secrets live only on this object."""

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
    mode: str = "profile"
    protocol_version: str = ""
    candidate_ref: str = ""
    job_ref: str = ""
    resolver_url: str = ""
    resolver_token: str = ""
    resolver_expires_at: str = ""
    expires_at: float = 0.0
    timings: dict[str, int] = field(default_factory=dict)
    manual_questions: list[dict[str, str | None]] = field(default_factory=list)
    screenshot_version: int = 0
    human_outcome: str = ""
    interact_marks: list[float] = field(default_factory=list)

    def __repr__(self) -> str:
        return f"ServiceSession(session_id={self.session_id!r}, status={self.status!r}, mode={self.mode!r})"

    def wipe_secrets(self) -> None:
        """Forget site passwords, the resolver token, and any held values."""
        from autofill.redact import redaction_filter

        redaction_filter().discard(self.resolver_token)
        self.credentials.clear()
        self.resolver_token = ""
        self.resolver_url = ""
        self.cover_letter_text = None


class ServiceSessionStore:
    """In-memory sessions. Looking up one id never returns another session."""

    def __init__(self, *, max_sessions: int = 32) -> None:
        if max_sessions < 1:
            raise ValueError("max_sessions must be at least 1.")
        self._max_sessions = max_sessions
        self._sessions: dict[str, ServiceSession] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _active_count(self) -> int:
        return sum(1 for session in self._sessions.values() if session.status != Status.EXPIRED)

    def create(self, session: ServiceSession) -> ServiceSession:
        with self._lock:
            if self._active_count() >= self._max_sessions:
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

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._sessions)

    def reap(self, now: float | None = None) -> list[str]:
        """Mark due sessions EXPIRED and wipe secrets. Returns the ids."""
        moment = time.monotonic() if now is None else now
        expired: list[str] = []
        with self._lock:
            for session in self._sessions.values():
                if session.busy or session.status in {Status.EXPIRED, Status.CANCELLED}:
                    continue
                if session.expires_at and moment >= session.expires_at:
                    session.status = Status.EXPIRED
                    session.wipe_secrets()
                    expired.append(session.session_id)
        return expired

    def start_reaper(self, on_expire: Callable[[str], None], *, interval_seconds: float) -> None:
        """Background sweep. ``on_expire`` closes that session's browser context."""
        if self._thread is not None:
            return

        def loop() -> None:
            while not self._stop.wait(interval_seconds):
                for session_id in self.reap():
                    try:
                        on_expire(session_id)
                    except Exception:
                        continue

        self._thread = threading.Thread(target=loop, name="autofill-reaper", daemon=True)
        self._thread.start()

    def stop_reaper(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2)
            self._thread = None


def new_session_id() -> str:
    return uuid.uuid4().hex
