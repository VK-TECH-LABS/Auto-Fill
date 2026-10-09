"""Log redaction. Messages keep ids, keys, and counts, and lose values."""

from __future__ import annotations

import json
import logging
import logging.config
import re
import threading

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b")
_BEARER_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_TOKEN_QUERY_RE = re.compile(r"(?i)(token|access_token|authorization)=([^&\s]+)")

_LOGGERS = (
    "autofill",
    "autofill.service",
    "autofill.engine",
    "autofill.resolver",
    "autofill.login",
    "autofill.stepfill",
    "autofill.redact",
)


class RedactionFilter(logging.Filter):
    """Strip emails, phone numbers, bearer tokens, and registered secrets."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def add(self, secret: str) -> None:
        if secret and len(secret) >= 8:
            with self._lock:
                self._secrets.add(secret)

    def discard(self, secret: str) -> None:
        if not secret:
            return
        with self._lock:
            self._secrets.discard(secret)

    def scrub(self, text: str) -> str:
        cleaned = _EMAIL_RE.sub("[redacted-email]", text)
        cleaned = _PHONE_RE.sub("[redacted-phone]", cleaned)
        cleaned = _BEARER_RE.sub("Bearer [redacted]", cleaned)
        cleaned = _TOKEN_QUERY_RE.sub(r"\1=[redacted]", cleaned)
        with self._lock:
            secrets = sorted(self._secrets, key=len, reverse=True)
        for secret in secrets:
            if secret and secret in cleaned:
                cleaned = cleaned.replace(secret, "[redacted]")
        return cleaned

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            rendered = str(record.msg)
        record.msg = self.scrub(rendered)
        record.args = ()
        return True


_FILTER = RedactionFilter()


def redaction_filter() -> RedactionFilter:
    return _FILTER


def install_redaction() -> RedactionFilter:
    """Attach the filter to Auto-Fill loggers. Safe to call more than once."""
    for name in _LOGGERS:
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)
    return _FILTER


class StructuredFormatter(logging.Formatter):
    """One JSON object per line. The message is already redacted."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        return json.dumps(payload, ensure_ascii=True)


def uvicorn_log_config() -> dict:
    """Uvicorn config that also emits Auto-Fill INFO logs on stdout."""
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "()": "uvicorn.logging.DefaultFormatter",
                "fmt": "%(levelprefix)s %(message)s",
                "use_colors": None,
            },
            "access": {
                "()": "uvicorn.logging.AccessFormatter",
                "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
            },
            "autofill": {"()": "autofill.redact.StructuredFormatter"},
        },
        "filters": {"redact": {"()": "autofill.redact.redaction_filter"}},
        "handlers": {
            "default": {
                "formatter": "default",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "formatter": "access",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
            },
            "autofill": {
                "formatter": "autofill",
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "filters": ["redact"],
            },
        },
        "loggers": {
            "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "uvicorn.error": {"level": "INFO"},
            "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
            "autofill": {"handlers": ["autofill"], "level": "INFO", "propagate": False},
        },
    }


def configure_stdout_logging() -> None:
    """Send Auto-Fill INFO records to stdout as redacted JSON lines."""
    install_redaction()
    logging.config.dictConfig(uvicorn_log_config())
    logging.getLogger("autofill").setLevel(logging.INFO)
