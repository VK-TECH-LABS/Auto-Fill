"""Cold contexts do not keep another session's values. Sentinels are synthetic."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from autofill import AutofillOptions, autofill_application
from autofill.profile import CandidateProfile
from autofill.resolver import ResolverBinding

FIXTURE = Path(__file__).parent / "fixtures" / "leak_form.html"
URL = "https://boards.greenhouse.io/example/jobs/1"
SENTINELS = ("AlphaSentinel42", "BravoSentinel42", "CharlieSentinel42")


class _Resolver(ThreadingHTTPServer):
    calls: list[dict]


def _future() -> str:
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat()


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {
            "candidateId": "ref-leak",
            "personal": {"full_name": "Canary Name", "email": "canary@example.com"},
        }
    )


@pytest.fixture(scope="module")
def browser():
    try:
        with sync_playwright() as playwright:
            chromium = playwright.chromium.launch(headless=True)
            try:
                yield chromium
            finally:
                chromium.close()
    except Exception as exc:
        pytest.skip(f"Chromium is not installed: {exc}")


@pytest.fixture
def resolver():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            server: _Resolver = self.server  # type: ignore[assignment]
            server.calls.append(body)
            ref = body["candidateRef"]
            index = {"ref-a": 0, "ref-b": 1, "ref-c": 2}[ref]
            fields = {}
            if "firstName" in body["fields"]:
                fields["firstName"] = SENTINELS[index]
            if "email" in body["fields"]:
                fields["email"] = f"{ref}@example.com"
            if "address.city" in body["fields"]:
                fields["address.city"] = f"City{ref}"
            encoded = json.dumps({"fields": fields, "answers": [], "unresolved": []}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, fmt: str, *args) -> None:
            return

    server = _Resolver(("127.0.0.1", 0), Handler)
    server.calls = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield server, f"http://{host}:{port}/resolve"
    finally:
        server.shutdown()


def _fill(page, url: str, ref: str):
    binding = ResolverBinding(
        url=url,
        token=f"synthetic-resolver-token-{ref}-0123456789",
        expires_at=_future(),
        candidate_ref=ref,
        job_ref="job-leak",
        timeout_seconds=5,
        retries=0,
    )
    return autofill_application(
        URL,
        _profile(),
        None,
        AutofillOptions(
            page=page,
            resolver=binding,
            candidate_id=ref,
            job_id="job-leak",
            session_id=f"sess-{ref}",
            timings={},
        ),
    )


def _blob(page) -> str:
    return page.evaluate(
        """() => {
            const bits = [document.documentElement.dataset.owner || ""];
            for (const el of document.querySelectorAll("input, select, textarea")) {
                bits.push(el.value || "");
            }
            bits.push(sessionStorage.getItem("owner") || "");
            return bits.join("\\n");
        }"""
    )


def test_destroying_a_context_leaves_nothing_for_the_next_session(browser, resolver):
    _server, url = resolver
    sequence = ["ref-a", "ref-b", "ref-c", "ref-a"]
    previous: list[str] = []
    for ref in sequence:
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(FIXTURE.as_uri() + f"?case={ref}")
            _fill(page, url, ref)
            blob = _blob(page)
            index = {"ref-a": 0, "ref-b": 1, "ref-c": 2}[ref]
            assert SENTINELS[index] in blob
            assert page.evaluate("() => document.documentElement.dataset.owner") == ref
            for older in previous:
                older_index = {"ref-a": 0, "ref-b": 1, "ref-c": 2}[older]
                if older == ref:
                    continue
                assert SENTINELS[older_index] not in blob
                assert f"{older}@example.com" not in blob
            previous.append(ref)
        finally:
            context.close()


def test_live_contexts_do_not_share_values(browser, resolver):
    _server, url = resolver
    contexts = []
    try:
        for ref in ("ref-a", "ref-b", "ref-c"):
            context = browser.new_context()
            contexts.append((ref, context))
            page = context.new_page()
            page.goto(FIXTURE.as_uri() + f"?case={ref}")
            _fill(page, url, ref)
        blobs = []
        for ref, context in contexts:
            page = context.pages[0]
            blob = _blob(page)
            blobs.append((ref, blob))
            assert page.evaluate("() => document.documentElement.dataset.owner") == ref
        for ref, blob in blobs:
            index = {"ref-a": 0, "ref-b": 1, "ref-c": 2}[ref]
            assert SENTINELS[index] in blob
            for other, sentinel in enumerate(SENTINELS):
                if other != index:
                    assert sentinel not in blob
    finally:
        for _ref, context in contexts:
            context.close()
