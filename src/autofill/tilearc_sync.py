"""Vinayaka Apply session contract (PR #173).

Auto-Fill refetches the latest committed profile and trusts a Save & Continue
only when the confirm response is the #173 shape and the read-back matches
this candidate and this answer. An older body is not a confirmation. A 409
``IDENTITY_MISMATCH`` drops the snapshot that was just refused.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

# Live staging host after vinayaka #173. The packaged helper uses this when
# the extension forwards authorization. An older response shape is not a save.
STAGING_ORIGIN = "https://vinayaka-app-staging-staging.up.railway.app"

_SESSION = "/api/autofill/sessions/{session_id}"
_STATUS = "/api/autofill/sessions/{session_id}/local-status"
_ANSWER = "/api/autofill/sessions/{session_id}/local-answer"


class IdentityMismatch(Exception):
    """local-status 409. The caller's fingerprint is not the Apply snapshot or the latest."""


class TilearcTransportError(Exception):
    """The TileArc call did not return a body. The save is not confirmed."""


@dataclass
class SessionSnapshot:
    profile: dict[str, Any]
    candidate_profile: dict[str, Any]
    fingerprint: str
    version: str
    candidate_id: str = ""


@dataclass
class AnswerReadBack:
    candidate_id: str
    key: str
    question: str
    answer: str
    version: str
    updated_at: str


@dataclass
class AnswerAck:
    saved: bool
    verified: bool
    candidate_id: str = ""
    keys: list[str] = field(default_factory=list)
    version: str = ""
    updated_at: str = ""
    canonical_key: str = ""
    read_back: AnswerReadBack | None = None
    standard_terms_authorized: bool | None = None
    recruitment_sms_consent: str = ""


@dataclass
class PullResult:
    """A refetch. ``blocked`` means do not keep the previous snapshot."""

    profile_body: dict[str, Any] | None
    fingerprint: str
    version: str
    stale: bool
    blocked: bool


def _text(value: Any) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _same(left: str, right: str) -> bool:
    return _text(left).casefold() == _text(right).casefold()


def parse_session_body(body: Any, *, candidate_id: str) -> SessionSnapshot | None:
    """#173 session GET. ``None`` when the body is an older shape."""
    if not isinstance(body, dict):
        return None
    profile = body.get("profile")
    fingerprint = _text(body.get("profileFingerprint"))
    version = _text(body.get("profileVersion"))
    candidate_profile = body.get("candidateProfile")
    if not isinstance(profile, dict) or not fingerprint or not version:
        return None
    if not isinstance(candidate_profile, dict):
        candidate_profile = {}
    owner = _text(body.get("candidateId")) or candidate_id
    if owner and candidate_id and owner != candidate_id:
        return None
    return SessionSnapshot(
        profile=profile,
        candidate_profile=candidate_profile,
        fingerprint=fingerprint,
        version=version,
        candidate_id=owner or candidate_id,
    )


def _read_back(raw: Any) -> AnswerReadBack | None:
    if not isinstance(raw, dict):
        return None
    candidate_id = _text(raw.get("candidateId"))
    answer = _text(raw.get("answer"))
    if not candidate_id or not answer:
        return None
    return AnswerReadBack(
        candidate_id=candidate_id,
        key=_text(raw.get("key")),
        question=_text(raw.get("question")),
        answer=answer,
        version=_text(raw.get("version")),
        updated_at=_text(raw.get("updatedAt")),
    )


def parse_confirm_body(body: Any) -> AnswerAck | None:
    """Confirm POST. ``None`` for an older shape (no verified flag, no read-back)."""
    if not isinstance(body, dict) or "verified" not in body:
        return None
    verified = body.get("verified") is True
    read_back = _read_back(body.get("readBack"))
    if verified and read_back is None:
        return None
    keys = [str(item) for item in body.get("keys") or [] if str(item).strip()]
    terms = body.get("standardTermsAuthorized")
    sms = _text(body.get("recruitment_sms_consent"))
    return AnswerAck(
        saved=body.get("saved") is True,
        verified=verified,
        candidate_id=_text(body.get("candidateId")),
        keys=keys,
        version=_text(body.get("version")),
        updated_at=_text(body.get("updatedAt")),
        canonical_key=_text(body.get("canonicalKey")),
        read_back=read_back,
        standard_terms_authorized=terms if isinstance(terms, bool) else None,
        recruitment_sms_consent=sms,
    )


def parse_read_body(body: Any) -> AnswerAck | None:
    """GET local-answer. Accepts the confirm wrapper or a bare read-back row."""
    wrapped = parse_confirm_body(body)
    if wrapped is not None:
        return wrapped
    row = _read_back(body)
    if row is None:
        return None
    return AnswerAck(
        saved=True,
        verified=True,
        candidate_id=row.candidate_id,
        keys=[row.key] if row.key else [],
        version=row.version,
        updated_at=row.updated_at,
        canonical_key=row.key,
        read_back=row,
    )


def ack_is_trusted(ack: AnswerAck | None, *, candidate_id: str, submitted: str) -> bool:
    """True only when verified and the read-back is this candidate's submitted answer."""
    if ack is None or not ack.verified or ack.read_back is None:
        return False
    if not candidate_id or ack.read_back.candidate_id != candidate_id:
        return False
    return _same(ack.read_back.answer, submitted)


def merged_profile_body(posted: dict[str, Any] | None, snapshot: SessionSnapshot) -> dict[str, Any]:
    """Latest profile. ``candidateProfile`` names replace a blank or stale legal name."""
    body = dict(snapshot.profile)
    personal = body.get("personal")
    personal = dict(personal) if isinstance(personal, dict) else {}
    cand = snapshot.candidate_profile.get("personal")
    cand = cand if isinstance(cand, dict) else {}
    for source, target in (
        ("first_name", "first_name"),
        ("firstName", "first_name"),
        ("last_name", "last_name"),
        ("lastName", "last_name"),
        ("email", "email"),
    ):
        value = _text(cand.get(source))
        if value:
            personal[target] = value
    if personal:
        if not _text(personal.get("full_name")) and not _text(personal.get("fullName")):
            full = " ".join(
                part
                for part in (_text(personal.get("first_name")), _text(personal.get("last_name")))
                if part
            )
            if full:
                personal["full_name"] = full
        body["personal"] = personal
    owner = snapshot.candidate_id
    if owner:
        body["candidateId"] = owner
    if posted and not body.get("personal") and isinstance(posted.get("personal"), dict):
        body["personal"] = dict(posted["personal"])
    return body


def identity_mismatch(status: int, body: Any) -> bool:
    if status != 409 or not isinstance(body, dict):
        return False
    blob = " ".join(_text(body.get(key)) for key in ("code", "error", "reason", "detail", "message"))
    return "IDENTITY_MISMATCH" in blob.upper()


class TilearcClient:
    """Calls one origin. ``transport`` is ``(method, path, query, body) -> (status, json)``."""

    def __init__(
        self,
        origin: str,
        transport: Any | None = None,
        *,
        authorization: str = "",
        cookie: str = "",
    ) -> None:
        parsed = urlparse(origin.strip())
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("TileArc origin must be https and must not carry a secret.")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self._transport = transport
        self._authorization = authorization.strip()
        self._cookie = cookie.strip()

    def fetch_session(self, session_id: str, candidate_id: str) -> SessionSnapshot | None:
        status, body = self._call(
            "GET",
            _SESSION.format(session_id=session_id),
            query={"candidateId": candidate_id},
            body=None,
        )
        if identity_mismatch(status, body):
            raise IdentityMismatch()
        if status != 200:
            return None
        return parse_session_body(body, candidate_id=candidate_id)

    def local_status(self, session_id: str, candidate_id: str) -> tuple[str, str] | None:
        """``(fingerprint, version)``. ``None`` when the body is an older shape."""
        status, body = self._call(
            "GET",
            _STATUS.format(session_id=session_id),
            query={"candidateId": candidate_id},
            body=None,
        )
        if identity_mismatch(status, body):
            raise IdentityMismatch()
        if status != 200 or not isinstance(body, dict):
            return None
        fingerprint = _text(body.get("profileFingerprint"))
        version = _text(body.get("profileVersion"))
        if not fingerprint or not version:
            return None
        return fingerprint, version

    def confirm_answer(
        self,
        session_id: str,
        *,
        candidate_id: str,
        job_id: str,
        question: str,
        answer: str,
        key: str = "",
        standard_terms_authorized: bool | None = None,
    ) -> AnswerAck | None:
        payload: dict[str, Any] = {
            "phase": "confirm",
            "candidateId": candidate_id,
            "jobId": job_id,
            "question": question,
            "answer": answer,
        }
        if key:
            payload["key"] = key
        if standard_terms_authorized is not None:
            payload["standardTermsAuthorized"] = standard_terms_authorized
        status, body = self._call(
            "POST",
            _ANSWER.format(session_id=session_id),
            query=None,
            body=payload,
        )
        if status != 200:
            return None
        return parse_confirm_body(body)

    def read_answer(self, session_id: str, *, candidate_id: str, job_id: str, question: str) -> AnswerAck | None:
        status, body = self._call(
            "GET",
            _ANSWER.format(session_id=session_id),
            query={"candidateId": candidate_id, "jobId": job_id, "question": question},
            body=None,
        )
        if status != 200:
            return None
        return parse_read_body(body)

    def _call(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None,
        body: dict[str, Any] | None,
    ) -> tuple[int, Any]:
        if self._transport is not None:
            try:
                status, payload = self._transport(method, path, query or {}, body)
            except Exception as exc:
                raise TilearcTransportError(type(exc).__name__) from exc
            return int(status), payload
        return _urllib_call(
            self.origin,
            method,
            path,
            query=query,
            body=body,
            authorization=self._authorization,
            cookie=self._cookie,
        )


def pull_latest(
    client: TilearcClient,
    *,
    session_id: str,
    candidate_id: str,
    fingerprint: str,
    version: str,
    posted: dict[str, Any] | None,
) -> PullResult:
    """Refetch. A 409 loads the session again and does not return ``posted``."""
    try:
        status = client.local_status(session_id, candidate_id)
    except IdentityMismatch:
        status = None
        try:
            snapshot = client.fetch_session(session_id, candidate_id)
        except IdentityMismatch:
            snapshot = None
        if snapshot is None:
            return PullResult(None, "", "", True, True)
        return _from_snapshot(snapshot, fingerprint, version, posted)
    except TilearcTransportError:
        return PullResult(posted, fingerprint, version, False, False)
    try:
        snapshot = client.fetch_session(session_id, candidate_id)
    except (IdentityMismatch, TilearcTransportError):
        snapshot = None
    if snapshot is None:
        # Older session route. Keep the posted profile. Do not invent a version.
        fresh_fp = status[0] if status else fingerprint
        fresh_ver = status[1] if status else version
        stale = bool(status) and (fresh_fp != fingerprint or fresh_ver != version)
        return PullResult(posted, fresh_fp or fingerprint, fresh_ver or version, stale, False)
    return _from_snapshot(snapshot, fingerprint, version, posted)


def _from_snapshot(
    snapshot: SessionSnapshot,
    fingerprint: str,
    version: str,
    posted: dict[str, Any] | None,
) -> PullResult:
    stale = snapshot.fingerprint != fingerprint or snapshot.version != version
    return PullResult(
        merged_profile_body(posted, snapshot),
        snapshot.fingerprint,
        snapshot.version,
        stale,
        False,
    )


def _urllib_call(
    origin: str,
    method: str,
    path: str,
    *,
    query: dict[str, str] | None,
    body: dict[str, Any] | None,
    authorization: str,
    cookie: str = "",
) -> tuple[int, Any]:
    url = origin + path
    if query:
        url = url + "?" + urlencode({key: value for key, value in query.items() if value})
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if authorization:
        headers["Authorization"] = authorization
    if cookie:
        headers["Cookie"] = cookie
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            raw = response.read(1_048_576)
            status = int(getattr(response, "status", 200) or 200)
    except HTTPError as exc:
        raw = exc.read(1_048_576)
        status = int(exc.code)
    except (URLError, TimeoutError, OSError) as exc:
        raise TilearcTransportError(type(exc).__name__) from exc
    if not raw:
        return status, {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise TilearcTransportError("invalid_json") from exc
    return status, parsed
