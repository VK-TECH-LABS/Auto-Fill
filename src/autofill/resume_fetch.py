"""Download one resume descriptor into a temp file, then the caller deletes it.

The descriptor is a URL returned for the field key ``resume.file``. This
module does not send the resolver token and does not read a caller-chosen
local path.
"""

from __future__ import annotations

import os
import re
import tempfile
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

MAX_RESUME_BYTES = 10 * 1024 * 1024
_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


class ResumeFetchError(RuntimeError):
    """The resume could not be attached. ``code`` is a category, not a body."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def fetch_resume(descriptor: dict, *, timeout: float = 15.0) -> Path:
    """Save the remote file under a temp name. The caller must delete it."""
    url = descriptor.get("url") if isinstance(descriptor, dict) else None
    if not isinstance(url, str):
        raise ResumeFetchError("invalid")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ResumeFetchError("invalid")
    if parsed.username or parsed.password:
        raise ResumeFetchError("invalid")
    if _expired(str(descriptor.get("expiresAt") or "")):
        raise ResumeFetchError("expired")
    content_type = str(descriptor.get("contentType") or "")
    filename = _safe_name(str(descriptor.get("filename") or "resume.bin"))
    suffix = Path(filename).suffix[:8] or ".bin"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            header = response.headers.get("Content-Type", content_type)
            if "text/html" in header.casefold():
                raise ResumeFetchError("invalid")
            chunks: list[bytes] = []
            total = 0
            while True:
                block = response.read(65536)
                if not block:
                    break
                total += len(block)
                if total > MAX_RESUME_BYTES:
                    raise ResumeFetchError("too_large")
                chunks.append(block)
    except ResumeFetchError:
        raise
    except Exception as exc:
        raise ResumeFetchError("unavailable") from exc
    payload = b"".join(chunks)
    if not payload or payload.lstrip().startswith(b"<"):
        raise ResumeFetchError("invalid")
    directory = "/dev/shm" if os.path.isdir("/dev/shm") else None
    handle, name = tempfile.mkstemp(prefix="autofill-resume-", suffix=suffix, dir=directory)
    try:
        os.write(handle, payload)
    finally:
        os.close(handle)
    return Path(name)


def _safe_name(value: str) -> str:
    cleaned = _NAME_RE.sub("-", value).strip(".-")
    return (cleaned or "resume.bin")[:80]


def _expired(value: str) -> bool:
    text = value.strip()
    if not text:
        return False
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed <= datetime.now(UTC)
