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


class TilearcAuthExpired(Exception):
    """401 or 403. The short-lived token is no longer valid. The save is not verified."""


class TilearcScopeError(Exception):
    """This token was issued for a different TileArc session or candidate."""


def authorization_header(value: str) -> str:
    """``Bearer <token>``. A value that already has the prefix is not doubled.

    Another scheme, such as ``Basic``, is left unchanged. An empty value stays empty.
    """
    text = (value or "").strip()
    if not text:
        return ""
    scheme, _, rest = text.partition(" ")
    if rest and scheme.lower() == "bearer":
        token = rest.strip()
        return f"Bearer {token}" if token else ""
    if rest:
        return text
    return f"Bearer {text}"


@dataclass
class SessionSnapshot:
    profile: dict[str, Any]
    candidate_profile: dict[str, Any]
    fingerprint: str
    version: str
    candidate_id: str = ""
    local_profile: dict[str, Any] = field(default_factory=dict)


@dataclass
class AnswerReadBack:
    candidate_id: str
    key: str
    question: str
    answer: str
    version: str
    updated_at: str
    values: list[str] = field(default_factory=list)
    selected_options: list[dict[str, str]] = field(default_factory=list)
    leaf: dict[str, str] | None = None
    leaves: list[dict[str, str]] = field(default_factory=list)
    # True when the body carries #176 selection keys, even if they are empty.
    selection_shape: bool = False


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


def _answer_text(value: Any) -> str:
    """The stored answer, including a multiselect list or a nested leaf object.

    A string stays a string. A list is the same `` | `` join the helper submits.
    ``{"parent", "leaf"}`` and ``{"path"}`` are the committed leaf path.
    """
    if isinstance(value, list):
        parts = [_answer_text(item) for item in value]
        return " | ".join(part for part in parts if part)
    if isinstance(value, dict):
        path = _text(value.get("path"))
        if path:
            return path
        parent = _text(value.get("parent"))
        leaf = _text(value.get("leaf") or value.get("child") or value.get("label") or value.get("value"))
        if parent and leaf:
            return f"{parent} > {leaf}"
        if leaf:
            return leaf
        return _text(value.get("answer") or value.get("text"))
    return _text(value)


def _same(left: str, right: str) -> bool:
    return _text(left).casefold() == _text(right).casefold()


def _same_answer(submitted: str, read: str) -> bool:
    """Exact match after a list or nested leaf is flattened to the submitted text."""
    return _same(submitted, read)


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
    local_profile = body.get("localProfile")
    if not isinstance(local_profile, dict):
        local_profile = {}
    owner = _text(body.get("candidateId")) or candidate_id
    if owner and candidate_id and owner != candidate_id:
        return None
    return SessionSnapshot(
        profile=profile,
        candidate_profile=candidate_profile,
        fingerprint=fingerprint,
        version=version,
        candidate_id=owner or candidate_id,
        local_profile=local_profile,
    )


def _option_row(raw: Any) -> dict[str, str] | None:
    """One #176 option or leaf: ``id``, ``label``, and ``path``."""
    if not isinstance(raw, dict):
        return None
    row = {
        "id": _text(raw.get("id")),
        "label": _text(raw.get("label")),
        "path": _text(raw.get("path")),
    }
    if not any(row.values()):
        return None
    return row


def _option_rows(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    return [row for item in raw if (row := _option_row(item)) is not None]


def _value_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [_answer_text(item) for item in raw if _answer_text(item)]
    text = _answer_text(raw)
    return [text] if text else []


def _read_back(raw: Any) -> AnswerReadBack | None:
    if not isinstance(raw, dict):
        return None
    candidate_id = _text(raw.get("candidateId"))
    answer = _answer_text(raw.get("answer"))
    values = _value_list(raw.get("values")) if "values" in raw else []
    selected = _option_rows(raw.get("selectedOptions")) if "selectedOptions" in raw else []
    leaf = _option_row(raw.get("leaf")) if "leaf" in raw else None
    leaves = _option_rows(raw.get("leaves")) if "leaves" in raw else []
    selection_shape = any(key in raw for key in ("values", "selectedOptions", "leaf", "leaves"))
    if not candidate_id or not (answer or values or leaf or leaves or selected):
        return None
    return AnswerReadBack(
        candidate_id=candidate_id,
        key=_text(raw.get("key")),
        question=_text(raw.get("question")),
        answer=answer,
        version=_text(raw.get("version")),
        updated_at=_text(raw.get("updatedAt")),
        values=values,
        selected_options=selected,
        leaf=leaf,
        leaves=leaves,
        selection_shape=selection_shape,
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


def _joined(values: list[str]) -> str:
    cleaned = [value.strip() for value in values if value.strip()]
    if len(cleaned) == 1:
        return cleaned[0]
    return " | ".join(cleaned)


def _committed_ids(read_back: AnswerReadBack) -> set[str]:
    found: set[str] = set()
    rows = list(read_back.selected_options) + list(read_back.leaves)
    if read_back.leaf is not None:
        rows.append(read_back.leaf)
    for row in rows:
        ident = _text(row.get("id"))
        if ident:
            found.add(ident.casefold())
    return found


def ack_is_trusted(
    ack: AnswerAck | None,
    *,
    candidate_id: str,
    submitted: str,
    choice: bool = False,
    values: list[str] | None = None,
    leaf_id: str = "",
) -> bool:
    """True only when verified and the read-back is this candidate's submitted answer.

    A #176 selection is committed by ``values`` or ``leaf.id``. The typed
    ``answer`` string does not verify that selection. A LIVE read-back that has
    no selection keys still matches the submitted answer text.
    """
    if ack is None or not ack.verified or ack.read_back is None:
        return False
    read_back = ack.read_back
    if not candidate_id or read_back.candidate_id != candidate_id:
        return False
    if choice and read_back.selection_shape:
        submitted_values = list(values or [])
        if submitted_values and _same(_joined(read_back.values), _joined(submitted_values)):
            return True
        if read_back.values and _same(_joined(read_back.values), submitted):
            return True
        wanted = {_text(leaf_id).casefold()} if _text(leaf_id) else set()
        for row in values or []:
            text = _text(row)
            if text:
                wanted.add(text.casefold())
        if wanted and wanted & _committed_ids(read_back):
            return True
        return False
    return _same_answer(submitted, read_back.answer)


_LINK_KEYS = (
    "linkedin_url",
    "portfolio_url",
    "website_url",
    "github_url",
    "linkedinUrl",
    "portfolioUrl",
    "websiteUrl",
    "githubUrl",
)
_IDENTITY_KEYS = (
    "first_name",
    "firstName",
    "last_name",
    "lastName",
    "full_name",
    "fullName",
    "email",
    "phone",
    "city",
)
_ROW_FILL_KEYS = (
    "location",
    "city",
    "description",
    "summary",
    "responsibilities",
    "role_description",
    "roleDescription",
    "from",
    "to",
    "start_date",
    "end_date",
    "start_month",
    "start_year",
    "end_month",
    "end_year",
)


def _overlay_records(body: dict[str, Any], source: dict[str, Any]) -> None:
    """Copy work, education, and stored links. Blank keys are not written."""
    for key in ("employment", "education"):
        rows = source.get(key)
        if isinstance(rows, list) and rows:
            body[key] = rows
    links = source.get("links")
    if isinstance(links, dict):
        current = body.get("links")
        merged = dict(current) if isinstance(current, dict) else {}
        for name, value in links.items():
            if value not in (None, ""):
                merged[name] = value
        if merged:
            body["links"] = merged
    personal_src = source.get("personal")
    if not isinstance(personal_src, dict):
        return
    personal = body.get("personal")
    personal = dict(personal) if isinstance(personal, dict) else {}
    for key in _LINK_KEYS:
        if personal_src.get(key) not in (None, ""):
            personal[key] = personal_src[key]
    for key in _IDENTITY_KEYS:
        if personal_src.get(key) not in (None, "") and personal.get(key) in (None, ""):
            personal[key] = personal_src[key]
    if personal:
        body["personal"] = personal


def merged_profile_body(posted: dict[str, Any] | None, snapshot: SessionSnapshot) -> dict[str, Any]:
    """Latest profile. ``candidateProfile`` names replace a blank or stale legal name.

    ``localProfile`` and ``candidateProfile`` work and education rows carry the
    #176 dates when the session sent them. A link is copied only when stored.
    """
    body = dict(snapshot.profile)
    _overlay_records(body, snapshot.local_profile)
    _overlay_records(body, snapshot.candidate_profile)
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
    _fill_posted_blanks(body, posted)
    return body


def _fill_posted_blanks(body: dict[str, Any], posted: dict[str, Any] | None) -> None:
    """Keep a value the Apply already sent when the session snapshot left it blank."""
    if not isinstance(posted, dict):
        return
    source = posted.get("personal")
    if isinstance(source, dict):
        personal = body.get("personal")
        personal = dict(personal) if isinstance(personal, dict) else {}
        for key, value in source.items():
            if value in (None, "", False) or isinstance(value, (dict, list)):
                continue
            if personal.get(key) in (None, ""):
                personal[key] = value
        if personal:
            body["personal"] = personal
    for key in ("employment", "education"):
        incoming = posted.get(key)
        rows = body.get(key)
        if not isinstance(incoming, list) or not isinstance(rows, list):
            continue
        for index, row in enumerate(rows):
            if index >= len(incoming) or not isinstance(row, dict):
                continue
            extra = incoming[index]
            if not isinstance(extra, dict):
                continue
            for name in _ROW_FILL_KEYS:
                if row.get(name) in (None, "") and extra.get(name) not in (None, ""):
                    row[name] = extra[name]


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
        session_id: str = "",
        candidate_id: str = "",
    ) -> None:
        parsed = urlparse(origin.strip())
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("TileArc origin must be https and must not carry a secret.")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.session_id = (session_id or "").strip()
        self.candidate_id = (candidate_id or "").strip()
        self._transport = transport
        self._authorization = authorization_header(authorization)
        self._cookie = (cookie or "").strip()
        self._remember_secrets()

    def __repr__(self) -> str:
        return f"TilearcClient(origin={self.origin!r}, session_id={self.session_id!r})"

    def drop(self) -> None:
        """Forget the token. It is not written to disk."""
        self._forget_secrets()
        self._authorization = ""
        self._cookie = ""

    def _secret_values(self) -> list[str]:
        values: list[str] = []
        header = self._authorization
        if header.lower().startswith("bearer "):
            token = header.split(None, 1)[1].strip()
            if token:
                values.append(token)
        elif header:
            values.append(header)
        if self._cookie:
            values.append(self._cookie)
        return values

    def _remember_secrets(self) -> None:
        from autofill.redact import redaction_filter

        scrub = redaction_filter()
        for secret in self._secret_values():
            scrub.add(secret)

    def _forget_secrets(self) -> None:
        from autofill.redact import redaction_filter

        scrub = redaction_filter()
        for secret in self._secret_values():
            scrub.discard(secret)

    def _check_scope(self, session_id: str, candidate_id: str) -> None:
        if self.session_id and session_id != self.session_id:
            raise TilearcScopeError()
        if self.candidate_id and candidate_id != self.candidate_id:
            raise TilearcScopeError()

    def fetch_session(self, session_id: str, candidate_id: str) -> SessionSnapshot | None:
        self._check_scope(session_id, candidate_id)
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
        self._check_scope(session_id, candidate_id)
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
        values: list[str] | None = None,
        selected_options: list[dict[str, str]] | None = None,
        leaf: dict[str, str] | None = None,
        leaves: list[dict[str, str]] | None = None,
    ) -> AnswerAck | None:
        payload: dict[str, Any] = {
            "phase": "confirm",
            "candidateId": candidate_id,
            "jobId": job_id,
            "question": question,
            "answer": answer,
        }
        if values:
            payload["values"] = list(values)
        if selected_options:
            payload["selectedOptions"] = list(selected_options)
        if leaf:
            payload["leaf"] = dict(leaf)
        if leaves:
            payload["leaves"] = list(leaves)
        if key:
            payload["key"] = key
        if standard_terms_authorized is not None:
            payload["standardTermsAuthorized"] = standard_terms_authorized
        self._check_scope(session_id, candidate_id)
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
        self._check_scope(session_id, candidate_id)
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
        headers = self._outbound_headers(json_body=body is not None)
        if self._transport is not None:
            try:
                status, payload = self._transport(method, path, query or {}, body, headers)
            except TilearcAuthExpired:
                raise
            except Exception as exc:
                raise TilearcTransportError(type(exc).__name__) from exc
            if int(status) in {401, 403}:
                raise TilearcAuthExpired()
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

    def _outbound_headers(self, *, json_body: bool) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if json_body:
            headers["Content-Type"] = "application/json"
        if self._authorization:
            headers["Authorization"] = self._authorization
        if self._cookie:
            headers["Cookie"] = self._cookie
        return headers


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
    if status in {401, 403}:
        raise TilearcAuthExpired()
    if not raw:
        return status, {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise TilearcTransportError("invalid_json") from exc
    return status, parsed
