"""Resolver HTTP bounds. The token stays on the Authorization header."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from autofill.intents import IntentMatch, question_hash
from autofill.resolver import ResolverAnswer, ResolverBinding, ResolverCallError, ResolverResponse, resolve_step
from autofill.stepfill import _index_answers, _take_answer

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
            if mode == "413":
                self.send_response(413)
                self.end_headers()
                return
            if mode == "echo":
                requested = server.calls[-1]["body"]
                encoded = json.dumps(
                    {
                        "fields": {key: "ok" for key in requested.get("fields", [])},
                        "answers": [],
                        "unresolved": [],
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)
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


def test_large_steps_are_chunked(resolver_url):
    server, url = resolver_url
    server.mode = "echo"
    fields = [f"field.{index}" for index in range(51)]
    result = resolve_step(_binding(url), session_id="sess", step="questions", fields=fields, questions=[])
    assert [len(call["body"]["fields"]) for call in server.calls] == [40, 11]
    assert len(result.fields) == 51
    assert result.result_code == "ok"


def test_413_marks_the_chunk_unresolved(resolver_url):
    server, url = resolver_url
    server.mode = "413"
    result = resolve_step(
        _binding(url, retries=2),
        session_id="sess",
        step="questions",
        fields=["email"],
        questions=[{"intent": "RELOCATE", "text": "Relocate?", "options": ["Yes"], "control": "radio"}],
    )
    assert result.result_code == "unprocessable"
    assert result.unresolved == ["email", "RELOCATE"]
    assert result.fields == {}
    assert len(server.calls) == 1


def test_question_text_and_options_are_bounded(resolver_url):
    server, url = resolver_url
    options = [f"option-{index:02d}-" + ("x" * 90) for index in range(31)]
    resolve_step(
        _binding(url),
        session_id="sess",
        step="questions",
        fields=[],
        questions=[{"intent": None, "text": "Q" * 500, "options": options, "control": "select"}],
    )
    question = server.calls[0]["body"]["questions"][0]
    assert len(question["text"]) == 300
    assert question["options"] == []
    assert question["id"] == question_hash(question["text"])


def test_unknown_answer_keyed_by_hash_is_kept(resolver_url):
    server, url = resolver_url
    text = "Are you a Singapore citizen?"
    digest = question_hash(text)
    server.payload = {
        "fields": {},
        "answers": [
            {
                "intent": digest,
                "value": "No",
                "confidence": "HIGH",
                "source": "saved_answer",
            },
            {"intent": "NOT_A_REQUEST", "value": "Yes", "confidence": "HIGH", "source": "saved_answer"},
        ],
        "unresolved": [],
    }
    result = resolve_step(
        _binding(url),
        session_id="sess",
        step="questions",
        fields=[],
        questions=[{"intent": None, "text": text, "options": ["Yes", "No"], "control": "radio"}],
    )
    assert len(result.answers) == 1
    answer = result.answers[0]
    assert answer.intent is None
    assert answer.question_id == digest
    assert answer.value == "No"
    by_intent, unnamed, by_key = _index_answers(result)
    taken = _take_answer(IntentMatch(None, "unknown", "none", text), by_intent, unnamed, by_key)
    assert taken is not None and taken.value == "No"
    normalized = ResolverAnswer(intent=None, value="No", confidence="HIGH", source="saved_answer", text=text.casefold())
    response = ResolverResponse(answers=[normalized])
    by_intent, unnamed, by_key = _index_answers(response)
    taken = _take_answer(IntentMatch(None, "unknown", "none", text), by_intent, unnamed, by_key)
    assert taken is normalized


def test_echoed_question_hash_is_kept(resolver_url):
    server, url = resolver_url
    text = "Are you a Singapore citizen?"
    digest = question_hash(text)
    server.payload = {
        "fields": {},
        "answers": [
            {
                "intent": None,
                "text": text,
                "questionId": digest,
                "questionHash": digest,
                "value": "No",
                "confidence": "HIGH",
                "source": "saved_answer",
            }
        ],
        "unresolved": [],
    }
    result = resolve_step(
        _binding(url),
        session_id="sess",
        step="questions",
        fields=[],
        questions=[{"intent": None, "text": text, "options": ["Yes", "No"], "control": "radio"}],
    )
    assert len(result.answers) == 1
    answer = result.answers[0]
    assert answer.question_id == digest
    assert answer.text == text
    by_intent, unnamed, by_key = _index_answers(result)
    taken = _take_answer(IntentMatch(None, "unknown", "none", text), by_intent, unnamed, by_key, unclassified=2)
    assert taken is not None and taken.value == "No"


def test_keyless_answer_is_not_assigned_across_unclassified_questions():
    text = "Are you a Singapore citizen?"
    other = "What is your favorite color?"
    loose = ResolverAnswer(intent=None, value="No", confidence="HIGH", source="saved_answer")
    response = ResolverResponse(answers=[loose])
    by_intent, unnamed, by_key = _index_answers(response)
    first = IntentMatch(None, "unknown", "none", text)
    second = IntentMatch(None, "unknown", "none", other)
    assert _take_answer(first, by_intent, unnamed, by_key, unclassified=2) is None
    assert _take_answer(second, by_intent, unnamed, by_key, unclassified=2) is None
    assert loose in unnamed
    by_intent, unnamed, by_key = _index_answers(response)
    taken = _take_answer(first, by_intent, unnamed, by_key, unclassified=1)
    assert taken is loose
