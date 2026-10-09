"""Resolver HTTP bounds. The token stays on the Authorization header."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from autofill.resolver import ResolverBinding, ResolverCallError, resolve_step

TOKEN = "synthetic-resolver-token-0123456789abcdef"


class _Server(ThreadingHTTPServer):
    calls: list[dict]
    mode: str
    payload: dict


def _binding(base: str, **kwargs) -> ResolverBinding:
    return ResolverBinding(
        url=base,
        token=TOKEN,
        expires_at="2099-01-01T00:00:00Z",
        candidate_ref="ref-a",
        job_ref="job-a",
        timeout_seconds=2,
        retries=kwargs.get("retries", 0),
        max_bytes=kwargs.get("max_bytes", 65536),
    )


@pytest.fixture
def resolver_url():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            server: _Server = self.server  # type: ignore[assignment]
            server.calls.append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization", ""),
                    "body": json.loads(raw.decode("utf-8")),
                }
            )
            mode = server.mode
            if mode == "503-once":
                server.mode = "ok"
                self.send_response(503)
                self.end_headers()
                return
            if mode in {"401", "409", "410", "422"}:
                self.send_response(int(mode))
                if mode == "422":
                    encoded = b'{"fields":{"email":"sentinel-must-not-fill@example.com"}}'
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)
                    return
                self.end_headers()
                return
            if mode == "huge":
                blob = b'{"fields":{"email":"' + (b"x" * 80000) + b'"}}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)
                return
            encoded = json.dumps(server.payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, fmt: str, *args) -> None:
            return

    server = _Server(("127.0.0.1", 0), Handler)
    server.calls = []
    server.mode = "ok"
    server.payload = {
        "fields": {"email": "river.example@example.com", "secretExtra": "nope"},
        "answers": [
            {"intent": "RELOCATE", "value": "Yes", "confidence": "HIGH", "source": "saved_answer"},
            {"intent": "NOT_A_REQUEST", "value": "Yes", "confidence": "HIGH", "source": "saved_answer"},
        ],
        "unresolved": [],
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield server, f"http://{host}:{port}/resolve"
    finally:
        server.shutdown()


def test_token_is_never_placed_in_the_url(resolver_url):
    server, url = resolver_url
    with pytest.raises(ResolverCallError) as raised:
        resolve_step(
            _binding(url + "?token=" + TOKEN),
            session_id="sess",
            step="contact",
            fields=["email"],
            questions=[],
        )
    assert raised.value.code == "invalid"
    assert server.calls == []


def test_success_uses_bearer_header_and_drops_extra_keys(resolver_url):
    server, url = resolver_url
    result = resolve_step(
        _binding(url),
        session_id="sess",
        step="contact",
        fields=["email"],
        questions=[{"intent": "RELOCATE", "text": "Relocate?", "options": ["Yes", "No"], "control": "radio"}],
    )
    assert result.fields == {"email": "river.example@example.com"}
    assert [item.intent for item in result.answers] == ["RELOCATE"]
    call = server.calls[0]
    assert call["auth"] == f"Bearer {TOKEN}"
    assert TOKEN not in call["path"]
    assert "token=" not in call["path"]
    assert call["body"]["fields"] == ["email"]
    assert call["body"]["questions"][0]["intent"] == "RELOCATE"


@pytest.mark.parametrize(("mode", "code"), [("401", "unauthorized"), ("409", "mismatch"), ("410", "expired")])
def test_auth_mismatch_and_expiry(resolver_url, mode: str, code: str):
    server, url = resolver_url
    server.mode = mode
    with pytest.raises(ResolverCallError) as raised:
        resolve_step(_binding(url), session_id="sess", step="contact", fields=["email"], questions=[])
    assert raised.value.code == code
    assert TOKEN not in str(raised.value)


def test_422_leaves_requested_keys_unresolved(resolver_url, caplog):
    server, url = resolver_url
    server.mode = "422"
    caplog.set_level("INFO")
    result = resolve_step(
        _binding(url, retries=2),
        session_id="sess",
        step="contact",
        fields=["email", "phone"],
        questions=[{"intent": "RELOCATE", "text": "Relocate?", "options": ["Yes", "No"], "control": "radio"}],
    )
    assert result.result_code == "unprocessable"
    assert result.fields == {}
    assert result.answers == []
    assert result.unresolved == ["email", "phone", "RELOCATE"]
    assert len(server.calls) == 1
    assert "unprocessable" in caplog.text
    assert "sentinel-must-not-fill" not in caplog.text
    assert TOKEN not in caplog.text


def test_retry_is_bounded(resolver_url):
    server, url = resolver_url
    server.mode = "503-once"
    result = resolve_step(
        _binding(url, retries=1),
        session_id="sess",
        step="contact",
        fields=["email"],
        questions=[],
    )
    assert result.fields["email"] == "river.example@example.com"
    assert len(server.calls) == 2


def test_response_size_is_limited(resolver_url):
    server, url = resolver_url
    server.mode = "huge"
    with pytest.raises(ResolverCallError) as raised:
        resolve_step(
            _binding(url, max_bytes=1024),
            session_id="sess",
            step="contact",
            fields=["email"],
            questions=[],
        )
    assert raised.value.code == "too_large"
