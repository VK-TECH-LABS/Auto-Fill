"""Ephemeral sessions. Nothing here is written to disk or to another application's database."""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from autofill.credentials import MemoryCredentialProvider
from autofill.dom_ui import PauseGate, empty_progress
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
    resume_uploaded: bool = False
    human_resolved: bool = False
    timings: dict[str, int] = field(default_factory=dict)
    manual_questions: list[dict[str, str | None]] = field(default_factory=list)
    screenshot_version: int = 0
    human_outcome: str = ""
    interact_marks: list[float] = field(default_factory=list)
    browser_restarts: int = 0
    paused: bool = False
    pause_gate: PauseGate = field(default_factory=PauseGate)
    progress: dict = field(default_factory=empty_progress)
    open_items: list[dict] = field(default_factory=list)
    held_selectors: set[str] = field(default_factory=set)
    held_answers: list[str] = field(default_factory=list)
    state_version: int = 0
    account: dict | None = None
    resume_required: bool = False
    resume_verified: bool = False
    resume_filename: str = ""
    resume_verified_at: str = ""
    resume_source: str = ""
    resume_bytes: bytes | None = None
    resume_mime: str = ""
    resume_temp: str = ""
    resume_prepared: bool = False
    resume_site_errors: list[str] = field(default_factory=list)
    answer_busy: bool = False
    idempotency: dict[str, tuple[int, dict]] = field(default_factory=dict)
    completed_keys: list[str] = field(default_factory=list)
    completed_hashes: list[str] = field(default_factory=list)
    answer_attempts: dict[str, int] = field(default_factory=dict)
    stuck_manual: bool = False
    checkpoint: dict | None = None
    percent_high: int = 0
    required_done_high: int = 0
    required_total_mark: int = -1
    required_page_key: str = ""
    form_seen: bool = False
    pending_resume: bool = False
    page_type: str = ""
    standard_terms_authorized: bool = False
    created_mono: float = 0.0
    auto_consents: list[dict] = field(default_factory=list)
    phase: str = "launching"
    started_at: str | None = None
    preview_jpeg: bytes | None = None
    preview_host: str = ""
    secret_values: list[str] = field(default_factory=list)
    _state_marker: tuple | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return f"ServiceSession(session_id={self.session_id!r}, status={self.status!r}, mode={self.mode!r})"

    def remember_secret(self, value: str) -> None:
        """Register a secret for redaction and for deletion when the session ends."""
        if not value:
            return
        from autofill.redact import redaction_filter

        redaction_filter().add(value)
        if value not in self.secret_values:
            self.secret_values.append(value)

    def observe(self) -> int:
        """Increase the public version when status, blocking, pause, or stage changes."""
        blocking = None
        if self.result is not None and self.result.blocking:
            item = self.result.blocking
            blocking = (item.get("kind"), item.get("fieldKey"), item.get("questionHash"))
        stage = str((self.progress or {}).get("stage") or "")
        marker = (self.status, bool(self.paused), stage, blocking)
        if marker != self._state_marker:
            self.state_version += 1
            self._state_marker = marker
        return self.state_version

    def clear_resume_bytes(self) -> None:
        """Drop resume bytes and delete the per-session temp file."""
        self.resume_bytes = None
        self.resume_prepared = False
        self.resume_site_errors = []
        path = self.resume_temp
        self.resume_temp = ""
        if not path:
            return
        try:
            os.remove(path)
        except OSError:
            pass
        parent = str(Path(path).parent)
        try:
            os.rmdir(parent)
        except OSError:
            pass

    def wipe_secrets(self) -> None:
        """Forget site passwords, the resolver token, resume bytes, and any held values."""
        from autofill.redact import redaction_filter

        redaction_filter().discard(self.resolver_token)
        for value in self.secret_values:
            redaction_filter().discard(value)
        self.secret_values.clear()
        self.credentials.clear()
        self.resolver_token = ""
        self.resolver_url = ""
        self.cover_letter_text = None
        for value in self.held_answers:
            redaction_filter().discard(value)
        self.held_answers.clear()
        self.held_selectors.clear()
        self.clear_resume_bytes()


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

    def claim_answer(
        self, session_id: str, key: str
    ) -> tuple[Literal["replay"], tuple[int, dict]] | tuple[Literal["run"], ServiceSession]:
        """Reserve one answer. A second call on the same session is busy.

        A finished Idempotency-Key replays its status and body. The busy flag
        used by a fill is separate, so this claim does not block ``begin_run``.
        """
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            token = key.strip()[:200]
            if token and token in session.idempotency:
                return ("replay", session.idempotency[token])
            if session.answer_busy or session.busy:
                raise RuntimeError("busy")
            session.answer_busy = True
            return ("run", session)

    def finish_answer(self, session_id: str, key: str, status: int, body: dict) -> None:
        """Store one answer result and release the session for the next answer."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return
            session.answer_busy = False
            token = key.strip()[:200]
            if not token:
                return
            if len(session.idempotency) >= 32:
                session.idempotency.pop(next(iter(session.idempotency)))
            session.idempotency[token] = (status, body)

    def release_answer(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.answer_busy = False

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
        moment = monotonic() if now is None else now
        expired: list[str] = []
        with self._lock:
            for session in self._sessions.values():
                if session.busy or session.status in {Status.EXPIRED, Status.CANCELLED}:
                    continue
                stale_start = created_start_is_stale(session, moment)
                if stale_start or (session.expires_at and moment >= session.expires_at):
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


CREATED_WITHOUT_START_SECONDS = 120.0


def created_start_is_stale(session: ServiceSession, moment: float) -> bool:
    """A session that never left CREATED after 120 seconds has no successful start."""
    if session.status not in {Status.CREATED, "CREATED"}:
        return False
    if session.created_mono <= 0:
        return False
    return moment - session.created_mono > CREATED_WITHOUT_START_SECONDS


def monotonic() -> float:
    """TTL clock. Tests replace this to expire a session without waiting."""
    return time.monotonic()


def new_session_id() -> str:
    return uuid.uuid4().hex
