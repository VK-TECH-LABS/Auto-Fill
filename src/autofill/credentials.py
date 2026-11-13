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

    def clear(self) -> None:
        """Drop every record. Callers use this when a session ends."""
        self._records.clear()

    def drop(self, candidate_id: str, scope: str) -> None:
        """Drop one candidate's secret for one employer. Other scopes stay."""
        if candidate_id and scope:
            self._records.pop((candidate_id, scope.lower()), None)

    def drop_candidate(self, candidate_id: str) -> None:
        """Drop every secret for one candidate. Other candidates stay."""
        if not candidate_id:
            return
        self._records = {
            key: value for key, value in self._records.items() if key[0] != candidate_id
        }

    def get_credentials(
        self,
        site_context: SiteContext,
        candidate_id: str | None = None,
    ) -> Credentials | None:
        cid = candidate_id if candidate_id is not None else site_context.candidate_id
        if not cid or not site_context.domain:
            return None
        return self._records.get((cid, site_context.domain.lower()))


def credential_scope(url: str, ats: str | None = None) -> str:
    """Employer key for a stored password. Not the bare hostname when the host is shared.

    Workday's ``tenantKey`` is already ``host/site``. Greenhouse, Lever, Ashby,
    and SmartRecruiters contribute a company slug, which is stored as
    ``host/slug`` so two employers on one host do not share a secret. A host
    with no tenant stays the hostname. The password is not part of this string.
    """
    from autofill.ats import detect_ats, site_domain
    from autofill.login import tenant_identity

    host = site_domain(url)
    if ats is None:
        info = detect_ats(url)
        ats = info.name if info else None
    tenant = str(tenant_identity(url, ats).get("tenantKey") or "")
    if not tenant:
        return host
    if tenant == host or tenant.startswith(f"{host}/"):
        return tenant
    return f"{host}/{tenant}" if host else tenant
