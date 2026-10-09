"""Download the resume grant and keep those bytes.

The descriptor is a URL returned for the field key ``resume.file``. The file
name is the sanitized name from that grant. This module does not invent a
document, a sample resume, or a replacement name, and it does not send the
resolver token or read a caller-chosen local path.
"""

from __future__ import annotations

import os
import re
import tempfile
import urllib.request
from dataclasses import dataclass
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


def sanitized_resume_name(filename: str) -> str:
    """The grant's basename, with characters a file input cannot safely carry removed.

    An empty result means Vinayaka did not provide a usable name. Callers must
    not replace that with a generated name.
    """
    base = Path(str(filename or "")).name
    cleaned = _NAME_RE.sub("-", base).strip(".-")
    if not cleaned:
        return ""
    if len(cleaned) <= 80:
        return cleaned
    suffix = Path(cleaned).suffix[:8]
    stem = cleaned[: 80 - len(suffix)].strip(".-")
    if not stem:
        return ""
    return f"{stem}{suffix}" if suffix else stem[:80]


@dataclass(frozen=True)
class ResumePayload:
    """Resume bytes held in memory. Nothing is written to disk."""

    name: str
    mime_type: str
    data: bytes


def fetch_resume_payload(descriptor: dict, *, timeout: float = 15.0) -> ResumePayload:
    """Download the grant and keep those bytes. The site reads this buffer, not a path."""
    data, filename, mime_type = _download(descriptor, timeout=timeout)
    return ResumePayload(name=filename, mime_type=mime_type, data=data)


def fetch_resume(descriptor: dict, *, timeout: float = 15.0) -> Path:
    """Save the grant under its sanitized filename. The caller must delete it."""
    data, filename, _mime = _download(descriptor, timeout=timeout)
    directory = "/dev/shm" if os.path.isdir("/dev/shm") else None
    folder = tempfile.mkdtemp(prefix="autofill-upload-", dir=directory)
    path = Path(folder) / filename
    try:
        path.write_bytes(data)
    except OSError as exc:
        path.unlink(missing_ok=True)
        try:
            os.rmdir(folder)
        except OSError:
            pass
        raise ResumeFetchError("unavailable") from exc
    return path


def _download(descriptor: dict, *, timeout: float) -> tuple[bytes, str, str]:
    url = descriptor.get("url") if isinstance(descriptor, dict) else None
    if not isinstance(url, str):
        raise ResumeFetchError("invalid")
    filename = sanitized_resume_name(str(descriptor.get("filename") or ""))
    if not filename:
        raise ResumeFetchError("invalid")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ResumeFetchError("invalid")
    if parsed.username or parsed.password:
        raise ResumeFetchError("invalid")
    if _expired(str(descriptor.get("expiresAt") or "")):
        raise ResumeFetchError("expired")
    content_type = str(descriptor.get("contentType") or "")
    header = content_type
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            header = str(response.headers.get("Content-Type", content_type) or content_type)
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
    mime = _mime_type(str(header), filename, content_type)
    return payload, filename, mime


def _mime_type(header: str, filename: str, declared: str) -> str:
    for candidate in (header, declared):
        token = candidate.split(";", 1)[0].strip().lower()
        if token and token != "application/octet-stream" and "/" in token:
            return token
    if filename.lower().endswith(".pdf"):
        return "application/pdf"
    return "application/octet-stream"


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
