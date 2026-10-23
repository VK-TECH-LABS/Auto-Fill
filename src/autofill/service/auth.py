"""Bearer-token check. The secret stays in server memory and is never logged.

Rotation: the service accepts ``AUTOFILL_SERVICE_TOKEN`` and, while a rotation
is in progress, the optional ``AUTOFILL_SERVICE_TOKEN_PREVIOUS``. Clients keep
sending the primary token. Drop the previous token once every client has moved.
"""

from __future__ import annotations

import hashlib
import secrets

MIN_TOKEN_LENGTH = 16


class AuthConfigurationError(RuntimeError):
    """``AUTOFILL_SERVICE_TOKEN`` is missing or too short to start the service."""


def require_configured_token(token: str) -> str:
    """Return a stripped token or raise when the process would otherwise be open."""
    cleaned = token.strip()
    if len(cleaned) < MIN_TOKEN_LENGTH:
        raise AuthConfigurationError(
            "AUTOFILL_SERVICE_TOKEN must be set to a random secret of at least "
            f"{MIN_TOKEN_LENGTH} characters. The service does not start without it."
        )
    return cleaned


def tokens_match(presented: str, expected: str) -> bool:
    """Compare digests so the check does not depend on the raw token length."""
    if not presented or not expected:
        return False
    left = hashlib.sha256(presented.encode("utf-8")).digest()
    right = hashlib.sha256(expected.encode("utf-8")).digest()
    return secrets.compare_digest(left, right)


def optional_previous_token(token: str | None) -> str | None:
    """Return the stripped previous token, or ``None`` when unset or too short.

    A too-short previous token is never accepted (fail closed) but does not stop
    the service, so a mistyped rotation variable cannot take the engine down.
    """
    cleaned = (token or "").strip()
    if len(cleaned) < MIN_TOKEN_LENGTH:
        return None
    return cleaned


def accepted_tokens(primary: str, previous: str | None = None) -> tuple[str, ...]:
    """Primary first, then a distinct previous token."""
    expected = require_configured_token(primary)
    prior = optional_previous_token(previous)
    if prior and prior != expected:
        return (expected, prior)
    return (expected,)


def tokens_match_any(presented: str, accepted: tuple[str, ...] | list[str]) -> bool:
    """Constant-time match against every accepted token (no early exit)."""
    if not presented or not accepted:
        return False
    left = hashlib.sha256(presented.encode("utf-8")).digest()
    matched = False
    for candidate in accepted:
        if not candidate:
            continue
        right = hashlib.sha256(candidate.encode("utf-8")).digest()
        matched = secrets.compare_digest(left, right) or matched
    return matched
