"""Bearer-token check. The secret stays in server memory and is never logged."""

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
