"""Runtime credentials. Nothing in this module is a stored secret.

Providers return a username, email, and password for one site at call time.
:class:`Credentials` redacts those values in ``repr`` so logs cannot pick them
up by accident. The mapping is scoped to a candidate id plus a domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SiteContext:
    """Where a login is being attempted. No password belongs here."""

    url: str
    domain: str
    ats: str | None = None
    candidate_id: str | None = None
    session_id: str | None = None


@dataclass(frozen=True)
class Credentials:
    """Login material for one site. ``repr`` never includes the secret values."""

    site: str
    username: str = ""
    email: str = ""
    password: str = ""

    def __repr__(self) -> str:
        # Attribute names here must not contain the secret's name, so a log
        # line that prints the object cannot be grepped for that word.
        return (
            f"Credentials(site={self.site!r}, username_present={bool(self.username)}, "
            f"email_present={bool(self.email)}, secret_present={bool(self.password)})"
        )

    __str__ = __repr__


class CredentialProvider(Protocol):
    """Host-supplied credentials. Auto-Fill does not talk to TileArc's database."""

    def get_credentials(
        self,
        site_context: SiteContext,
        candidate_id: str | None = None,
    ) -> Credentials | None:
        """Return credentials for this candidate and site, or None when missing."""


class MemoryCredentialProvider:
    """In-memory provider for tests and for a host that already resolved credentials.

    Keys are ``(candidate_id, domain)``. A lookup never falls back to another
    candidate or another domain.
    """

    def __init__(self, records: dict[tuple[str, str], Credentials] | None = None) -> None:
        self._records = dict(records or {})

    def put(self, candidate_id: str, domain: str, credentials: Credentials) -> None:
        self._records[(candidate_id, domain.lower())] = credentials

    def get_credentials(
        self,
        site_context: SiteContext,
        candidate_id: str | None = None,
    ) -> Credentials | None:
        cid = candidate_id if candidate_id is not None else site_context.candidate_id
        if not cid or not site_context.domain:
            return None
        return self._records.get((cid, site_context.domain.lower()))
