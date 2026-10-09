"""Process entrypoint. The service token comes from the environment, never from the image."""

from __future__ import annotations

import os
import sys

import uvicorn

from autofill.service.app import create_app
from autofill.service.auth import AuthConfigurationError


def env_flag(name: str, default: bool) -> bool:
    """Read a boolean environment flag. Unset uses ``default``."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def main() -> None:
    """Start the HTTP service. Exits 2 when the auth token is not configured."""
    if os.environ.get("DATABASE_URL"):
        print("DATABASE_URL is set and ignored. Auto-Fill does not use a database.", file=sys.stderr)
    token = os.environ.get("AUTOFILL_SERVICE_TOKEN", "")
    try:
        app = create_app(
            token=token,
            run_browser=env_flag("AUTOFILL_RUN_BROWSER", True),
            max_sessions=int(os.environ.get("AUTOFILL_MAX_SESSIONS", "32")),
            browser_no_sandbox=env_flag("AUTOFILL_BROWSER_NO_SANDBOX", False),
            session_ttl_seconds=float(os.environ.get("AUTOFILL_SESSION_TTL_SECONDS", "1800")),
            reaper_interval_seconds=float(os.environ.get("AUTOFILL_REAPER_INTERVAL_SECONDS", "30")),
            resolver_timeout_seconds=float(os.environ.get("AUTOFILL_RESOLVER_TIMEOUT_SECONDS", "5")),
            resolver_retries=int(os.environ.get("AUTOFILL_RESOLVER_RETRIES", "2")),
            resolver_max_bytes=int(os.environ.get("AUTOFILL_RESOLVER_MAX_BYTES", "65536")),
        )
    except (AuthConfigurationError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2) from exc
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host=host, port=port, log_level="info")
