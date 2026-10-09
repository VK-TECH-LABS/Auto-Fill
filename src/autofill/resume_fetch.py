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


def candidate_resume_name(first: str, last: str, suffix: str = ".pdf") -> str:
    """A person-shaped file name such as ``River_Example_Resume.pdf``."""
    first_token = _token(first)
    last_token = _token(last)
    if first_token and last_token:
        stem = f"{first_token}_{last_token}"
    else:
        stem = first_token or last_token or "Candidate"
    ext = suffix if suffix.startswith(".") else f".{suffix}"
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,7}", ext):
        ext = ".pdf"
    return f"{stem}_Resume{ext}"


@dataclass(frozen=True)
class ResumePayload:
    """Resume bytes held in memory. Nothing is written to disk."""

    name: str
    mime_type: str
    data: bytes


def fetch_resume_payload(descriptor: dict, *, timeout: float = 15.0, display_name: str | None = None) -> ResumePayload:
    """Download the resume and keep the bytes. The site reads this buffer, not a path."""
    data, filename, mime_type = _download(descriptor, timeout=timeout, display_name=display_name)
    return ResumePayload(name=filename, mime_type=mime_type, data=data)


def fetch_resume(descriptor: dict, *, timeout: float = 15.0, display_name: str | None = None) -> Path:
    """Save the remote file under ``display_name``. The caller must delete it."""
    data, filename, _mime = _download(descriptor, timeout=timeout, display_name=display_name)
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


def _download(descriptor: dict, *, timeout: float, display_name: str | None) -> tuple[bytes, str, str]:
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
    chosen = display_name or _safe_name(str(descriptor.get("filename") or "")) or "Candidate_Resume.pdf"
    filename = _visible_filename(chosen)
    if not Path(filename).suffix:
        filename = f"{filename}{suffix}"
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


def _token(value: str) -> str:
    return _NAME_RE.sub("_", value).strip("._")[:40]


def _visible_filename(value: str) -> str:
    """The basename the site sees. Never the internal ``autofill-resume-`` prefix."""
    cleaned = _safe_name(Path(value).name)
    if not cleaned or cleaned.lower().startswith("autofill-resume"):
        cleaned = "Candidate_Resume.pdf"
    if not Path(cleaned).suffix:
        cleaned = f"{cleaned}.pdf"
    return cleaned[:80]


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
