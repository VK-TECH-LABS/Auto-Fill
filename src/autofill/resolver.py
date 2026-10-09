"""Per-step resolver client.

The per-session token is sent only on the Authorization header. It is never
placed in the URL, logged, or written to disk. Calls are bounded by timeout,
retry count, and response size.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

from autofill.resume_fetch import resume_filename

logger = logging.getLogger("autofill.resolver")

_RETRYABLE_HTTP = frozenset({408, 429, 500, 502, 503, 504})
MAX_FIELDS_PER_CALL = 40
MAX_QUESTIONS_PER_CALL = 20
MAX_QUESTION_TEXT = 300
MAX_OPTION_LABEL = 80
MAX_OPTIONS = 30
_TOKEN_QUERY_KEYS = frozenset({"token", "access_token", "authorization", "auth"})


class ResolverCallError(RuntimeError):
    """A resolver call failed. ``code`` is a result code, never a payload."""

    def __init__(self, code: str, http_status: int = 0) -> None:
        super().__init__(code)
        self.code = code
        self.http_status = http_status


@dataclass
class ResolverAnswer:
    """One saved answer. Held in memory for the current step only."""

    intent: str | None
    value: str = ""
    values: list[str] = field(default_factory=list)
    confidence: str = "LOW"
    source: str = ""
    text: str = ""


@dataclass
class ResolverResponse:
    """Field values and answers for the keys this step asked for."""

    fields: dict[str, Any] = field(default_factory=dict)
    answers: list[ResolverAnswer] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    latency_ms: int = 0
    result_code: str = "ok"

    def wipe(self) -> None:
        """Drop values as soon as the step has applied or abandoned them."""
        self.fields.clear()
        self.answers.clear()
        self.unresolved.clear()


@dataclass
class ResolverBinding:
    """Memory-only coordinates for one session's resolver. ``repr`` hides the token."""

    url: str
    token: str
    expires_at: str
    candidate_ref: str
    job_ref: str
    timeout_seconds: float = 5.0
    retries: int = 2
    max_bytes: int = 65536

    def __repr__(self) -> str:
        host = urlparse(self.url).hostname or ""
        return (
            f"ResolverBinding(host={host!r}, candidate_ref={self.candidate_ref!r}, "
            f"job_ref={self.job_ref!r}, token_present={bool(self.token)})"
        )

    def expired(self, *, now: datetime | None = None) -> bool:
        moment = _parse_time(self.expires_at)
        if moment is None:
            return True
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        return current >= moment


def assert_token_not_in_url(url: str, token: str) -> None:
    """Refuse a resolver URL that would carry the bearer token."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ResolverCallError("invalid")
    if parsed.username or parsed.password:
        raise ResolverCallError("invalid")
    if token and token in url:
        raise ResolverCallError("invalid")
    for key in parse_qs(parsed.query, keep_blank_values=True):
        if key.casefold() in _TOKEN_QUERY_KEYS:
            raise ResolverCallError("invalid")


def limit_question(question: dict[str, Any]) -> dict[str, Any]:
    """Fit one question to the resolver size limits.

    More than 30 options are omitted. The caller still has the full list and
    can match a returned value locally.
    """
    text = question.get("text")
    text = text[:MAX_QUESTION_TEXT] if isinstance(text, str) else ""
    options = question.get("options")
    limited: list[str] = []
    if isinstance(options, list) and len(options) <= MAX_OPTIONS:
        for option in options:
            if isinstance(option, str) and option:
                limited.append(option[:MAX_OPTION_LABEL])
    bounded = dict(question)
    bounded["text"] = text
    bounded["options"] = limited
    return bounded


def _batches(items: list[Any], size: int) -> list[list[Any]]:
    if not items:
        return []
    return [items[index : index + size] for index in range(0, len(items), size)]


def resolve_step(
    binding: ResolverBinding,
    *,
    session_id: str,
    step: str,
    fields: list[str],
    questions: list[dict[str, Any]],
) -> ResolverResponse:
    """POST one step in bounded chunks. A 413 marks that chunk unresolved."""
    assert_token_not_in_url(binding.url, binding.token)
    if binding.expired():
        raise ResolverCallError("expired", 410)
    if not binding.token:
        raise ResolverCallError("unauthorized", 401)
    limited = [limit_question(item) for item in questions if isinstance(item, dict)]
    field_batches = _batches(list(fields), MAX_FIELDS_PER_CALL)
    question_batches = _batches(limited, MAX_QUESTIONS_PER_CALL)
    count = max(len(field_batches), len(question_batches), 1)
    merged = ResolverResponse()
    for index in range(count):
        field_batch = field_batches[index] if index < len(field_batches) else []
        question_batch = question_batches[index] if index < len(question_batches) else []
        part = _resolve_once(
            binding,
            session_id=session_id,
            step=step,
            fields=field_batch,
            questions=question_batch,
        )
        merged.fields.update(part.fields)
        merged.answers.extend(part.answers)
        merged.unresolved.extend(part.unresolved)
        merged.latency_ms += part.latency_ms
        if part.result_code != "ok":
            merged.result_code = part.result_code
    return merged


def _resolve_once(
    binding: ResolverBinding,
    *,
    session_id: str,
    step: str,
    fields: list[str],
    questions: list[dict[str, Any]],
) -> ResolverResponse:
    """POST one chunk. Retries are bounded and never include 401, 409, or 410."""
    payload = {
        "sessionId": session_id,
        "candidateRef": binding.candidate_ref,
        "jobRef": binding.job_ref,
        "step": step,
        "fields": list(fields),
        "questions": questions,
    }
    raw = json.dumps(payload).encode("utf-8")
    if len(raw) > binding.max_bytes:
        return ResolverResponse(
            unresolved=_requested_names(fields, questions),
            result_code="unprocessable",
        )
    requested_fields = set(fields)
    requested_intents = {item.get("intent") for item in questions}
    started = time.perf_counter()
    attempts = max(0, binding.retries) + 1
    last: ResolverCallError | None = None
    for attempt in range(attempts):
        try:
            body, status = _post(binding, raw)
            parsed = _parse_body(
                body,
                requested_fields=requested_fields,
                requested_intents=requested_intents,
            )
            parsed.latency_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "session=%s step=%s result=%s requested=%s returned=%s latency_ms=%s",
                session_id,
                step,
                status,
                len(fields) + len(questions),
                len(parsed.fields) + len(parsed.answers),
                parsed.latency_ms,
            )
            return parsed
        except ResolverCallError as exc:
            last = exc
            if exc.code == "unprocessable":
                latency_ms = int((time.perf_counter() - started) * 1000)
                logger.info(
                    "session=%s step=%s result=%s requested=%s returned=%s latency_ms=%s",
                    session_id,
                    step,
                    exc.code,
                    len(fields) + len(questions),
                    0,
                    latency_ms,
                )
                return ResolverResponse(
                    unresolved=_requested_names(fields, questions),
                    latency_ms=latency_ms,
                    result_code=exc.code,
                )
            if exc.code != "retryable" or attempt + 1 >= attempts:
                logger.info(
                    "session=%s step=%s result=%s requested=%s returned=%s latency_ms=%s",
                    session_id,
                    step,
                    exc.code,
                    len(fields) + len(questions),
                    0,
                    int((time.perf_counter() - started) * 1000),
                )
                raise
    if last is not None:
        raise last
    raise ResolverCallError("retryable")


def _post(binding: ResolverBinding, raw: bytes) -> tuple[bytes, int]:
    request = urllib.request.Request(
        binding.url,
        data=raw,
        method="POST",
        headers={
            "Authorization": f"Bearer {binding.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=binding.timeout_seconds) as response:
            status = int(getattr(response, "status", 200))
            body = _read_limited(response, binding.max_bytes)
            return body, status
    except urllib.error.HTTPError as exc:
        code = int(exc.code)
        if code == 401:
            raise ResolverCallError("unauthorized", code) from None
        if code == 409:
            raise ResolverCallError("mismatch", code) from None
        if code == 410:
            raise ResolverCallError("expired", code) from None
        if code in {413, 422}:
            # Too many fields, or a step the resolver will not answer. The body is not logged.
            raise ResolverCallError("unprocessable", code) from None
        if code in _RETRYABLE_HTTP:
            raise ResolverCallError("retryable", code) from None
        raise ResolverCallError("invalid", code) from None
    except ResolverCallError:
        raise
    except (TimeoutError, urllib.error.URLError, OSError):
        raise ResolverCallError("retryable") from None


def _requested_names(fields: list[str], questions: list[dict[str, Any]]) -> list[str]:
    """Keys this step asked for. Used when the resolver refuses the step."""
    names = list(fields)
    for item in questions:
        intent = item.get("intent")
        names.append(intent if isinstance(intent, str) and intent else "unknown")
    return names


def _read_limited(response: Any, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        block = response.read(8192)
        if not block:
            break
        total += len(block)
        if total > max_bytes:
            raise ResolverCallError("too_large")
        chunks.append(block)
    return b"".join(chunks)


def _parse_body(
    raw: bytes,
    *,
    requested_fields: set[str],
    requested_intents: set[Any],
) -> ResolverResponse:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise ResolverCallError("invalid") from None
    if not isinstance(data, dict):
        raise ResolverCallError("invalid")
    fields_raw = data.get("fields", {})
    answers_raw = data.get("answers", [])
    unresolved_raw = data.get("unresolved", [])
    if not isinstance(fields_raw, dict) or not isinstance(answers_raw, list):
        raise ResolverCallError("invalid")
    fields: dict[str, Any] = {}
    for key, value in fields_raw.items():
        if key not in requested_fields:
            continue
        if str(key) == "resume.file":
            descriptor = _resume_descriptor(value)
            if descriptor is not None:
                fields["resume.file"] = descriptor
            continue
        parsed = _bounded_value(value)
        if parsed is not _INVALID:
            fields[str(key)] = parsed
    answers: list[ResolverAnswer] = []
    for item in answers_raw[:64]:
        if not isinstance(item, dict):
            continue
        intent = item.get("intent")
        if intent is not None and not isinstance(intent, str):
            continue
        if intent not in requested_intents:
            continue
        confidence = item.get("confidence", "LOW")
        if confidence not in {"HIGH", "MEDIUM", "LOW"}:
            confidence = "LOW"
        value = item.get("value")
        text = value if isinstance(value, str) else ""
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            text = str(value)
        values_raw = item.get("values", [])
        values = [entry for entry in values_raw if isinstance(entry, str)] if isinstance(values_raw, list) else []
        source_raw = item.get("source")
        source = source_raw if isinstance(source_raw, str) else ""
        question_text = item.get("text")
        question_text = question_text if isinstance(question_text, str) else ""
        if len(text) > 4000 or any(len(entry) > 4000 for entry in values):
            continue
        answers.append(
            ResolverAnswer(
                intent=intent,
                value=text[:4000],
                values=values[:32],
                confidence=str(confidence),
                source=source[:64],
                text=question_text[:300],
            )
        )
    if isinstance(unresolved_raw, list):
        unresolved = [item for item in unresolved_raw if isinstance(item, str)][:64]
    else:
        unresolved = []
    return ResolverResponse(fields=fields, answers=answers, unresolved=unresolved)


_INVALID = object()


def _resume_descriptor(value: Any) -> dict[str, str] | None:
    """Keep a remote resume pointer. Local paths and token URLs are dropped."""
    if not isinstance(value, dict):
        return None
    url = value.get("url")
    if not isinstance(url, str) or len(url) > 2048 or not url.startswith(("http://", "https://")):
        return None
    raw_name = value.get("filename")
    safe_name = resume_filename(raw_name if isinstance(raw_name, str) else "")
    content_type = value.get("contentType")
    expires = value.get("expiresAt")
    return {
        "url": url,
        "filename": safe_name,
        "contentType": content_type[:120] if isinstance(content_type, str) else "",
        "expiresAt": expires[:40] if isinstance(expires, str) else "",
    }


def _bounded_value(value: Any) -> Any:
    if isinstance(value, str):
        return value[:4000]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, list):
        if len(value) > 32:
            return _INVALID
        rows: list[Any] = []
        for entry in value:
            if isinstance(entry, str):
                rows.append(entry[:4000])
            elif isinstance(entry, dict) and len(entry) <= 24:
                rows.append({str(key)[:80]: _scalar(item) for key, item in entry.items()})
            else:
                return _INVALID
        return rows
    if isinstance(value, dict) and len(value) <= 24:
        return {str(key)[:80]: _scalar(item) for key, item in value.items()}
    return _INVALID


def _scalar(value: Any) -> str:
    if isinstance(value, str):
        return value[:4000]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return ""


def _parse_time(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not follow redirects. A redirect could move the bearer token."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        raise ResolverCallError("invalid", code)
