"""Local same-tab attach. Final Submit stays refused, and the hosted runner is unchanged."""

from __future__ import annotations

import inspect
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from autofill.engine import ApplicationResult, Status
from autofill.local_attach import (
    ENV_FLAG,
    DefaultProfileBlocked,
    FeatureDisabled,
    LocalAttachError,
    LoopbackRequired,
    _probe_note,
    assert_loopback_listen,
    attempt_alternate_submits,
    dedicated_launch_args,
    demonstrate_dedicated_fill,
    drive_control,
    fill_visible_tab,
    find_chrome,
    is_default_user_data_dir,
    listening_hosts,
    parse_endpoint,
    probe_branded_load_extension,
    probe_debug_port,
    probe_extension_debugger,
    stable_chrome_user_data_dir,
    start_fixture_server,
)
from autofill.local_helper import LocalHelper, serve
from autofill.profile import CandidateProfile
from autofill.safeguards import HUMAN_SUBMIT_ONLY, HumanSubmissionRequired

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tools" / "local-cdp-poc" / "fixture" / "application.html"
EXTENSION = ROOT / "tools" / "local-cdp-poc" / "extension"
PROFILE = ROOT / "examples" / "profile.example.json"

SUBMIT_PAGE = """<!DOCTYPE html>
<html><body>
<form id="application" onsubmit="window.__submitted = true; return false;">
  <label for="email">Email address</label>
  <input id="email" type="email">
  <button type="submit" id="submit">Submit application</button>
  <button type="submit" id="apply">Apply now</button>
  <button type="submit" id="send">Send application</button>
  <button type="button" id="next">Save and continue</button>
</form>
<script>
  window.__submitted = false;
  window.__continued = false;
  window.__entered = false;
  document.getElementById("next").addEventListener("click", () => { window.__continued = true; });
  document.getElementById("email").addEventListener("keydown", (event) => {
    if (event.key === "Enter") window.__entered = true;
  });
</script>
</body></html>
"""

ALTERNATE_SUBMITS = [
    ("#submit", "Submit application", "navigation"),
    ("#submit", "Submit application", "login"),
    ("#submit", "Submit application", "account"),
    ("#submit", "Submit application", "account_switch"),
    ("#submit", "Next", "navigation"),
    ("#submit", "Save and continue", "navigation"),
    ("#apply", "Apply now", "navigation"),
    ("#apply", "Next", "login"),
    ("#send", "Send application", "account"),
    ("#submit", "Submit application", "eval"),
    ("#submit", "Submit application", "form_submit"),
    ("#submit", "Submit application", "keyboard_enter"),
    ("#submit", "Submit application", "request_submit"),
    ("#submit", "Submit application", "not-a-purpose"),
]


def _flags(page) -> dict[str, bool]:
    return {
        "submitted": bool(page.evaluate("() => window.__submitted")),
        "continued": bool(page.evaluate("() => window.__continued")),
        "entered": bool(page.evaluate("() => window.__entered")),
    }


def test_human_submit_only_stays_on():
    assert HUMAN_SUBMIT_ONLY is True


def test_hosted_service_does_not_import_the_local_driver():
    for relative in (
        "src/autofill/__init__.py",
        "src/autofill/service/__init__.py",
        "src/autofill/service/app.py",
        "src/autofill/service/runner.py",
        "src/autofill/service/server.py",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "local_attach" not in text
        assert "local_helper" not in text
        assert "connect_over_cdp" not in text
    runner = (ROOT / "src/autofill/service/runner.py").read_text(encoding="utf-8")
    assert "chromium.launch" in runner
    assert "new_context(" in runner


def test_extension_has_no_service_token():
    for path in EXTENSION.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        assert "AUTOFILL_SERVICE_TOKEN" not in text
        assert "service_token" not in text.lower()
        assert "0.0.0.0" not in text


def test_fill_path_does_not_open_a_new_context_or_click_directly():
    from autofill import local_attach

    source = inspect.getsource(local_attach.attach_visible_tab) + inspect.getsource(local_attach.fill_visible_tab)
    assert "new_context(" not in source
    module = Path(local_attach.__file__).read_text(encoding="utf-8")
    helper = (ROOT / "src" / "autofill" / "local_helper.py").read_text(encoding="utf-8")
    assert ".click(" not in module
    assert ".click(" not in helper
    assert "browser.close(" not in module
    assert "browser.close(" not in helper
    assert "_block_heavy" not in module
    assert "_block_heavy" not in helper
    assert "AUTOFILL_SERVICE_TOKEN" not in helper


def test_endpoint_rejects_anything_but_loopback():
    host, port = parse_endpoint("http://127.0.0.1:9222/json/version")
    assert (host, port) == ("127.0.0.1", 9222)
    parse_endpoint("ws://127.0.0.1:9333/devtools/browser/abc")
    for endpoint in (
        "http://0.0.0.0:9222",
        "http://localhost:9222",
        "http://8.8.8.8:9222",
        "http://[::1]:9222",
        "https://127.0.0.1:9222",
        "http://127.0.0.1",
        "http://user:pass@127.0.0.1:9222",
    ):
        with pytest.raises(LoopbackRequired):
            parse_endpoint(endpoint)


def test_dedicated_args_refuse_the_default_profile_and_public_bind(tmp_path):
    assert is_default_user_data_dir(stable_chrome_user_data_dir())
    with pytest.raises(DefaultProfileBlocked):
        dedicated_launch_args(
            executable="/usr/bin/google-chrome-stable",
            user_data_dir=stable_chrome_user_data_dir(),
            port=9222,
            start_url="about:blank",
        )
    args = dedicated_launch_args(
        executable="/usr/bin/google-chrome-stable",
        user_data_dir=tmp_path / "profile",
        port=9222,
        start_url="http://127.0.0.1:9/t172-local-tab.html",
    )
    assert "--remote-debugging-address=127.0.0.1" in args
    assert f"--user-data-dir={tmp_path / 'profile'}" in args
    with pytest.raises(LoopbackRequired):
        dedicated_launch_args(
            executable="/usr/bin/google-chrome-stable",
            user_data_dir=tmp_path / "profile",
            port=9222,
            start_url="about:blank",
            extra_args=["--remote-debugging-address=0.0.0.0"],
        )


def test_launch_requires_the_feature_flag(monkeypatch, tmp_path):
    monkeypatch.delenv(ENV_FLAG, raising=False)
    from autofill.local_attach import launch_dedicated_chrome

    with pytest.raises(FeatureDisabled):
        launch_dedicated_chrome(user_data_dir=tmp_path / "profile", port=9222, start_url="about:blank")


def _ipv4_listen_blob(rows: list[tuple[int, bytes, int]]) -> bytes:
    from autofill.local_attach import _IPV4_ROW_SIZE

    body = b"".join(
        state.to_bytes(4, "little")
        + ip
        + socket.htons(port).to_bytes(4, "little")
        + b"\x00" * (_IPV4_ROW_SIZE - 12)
        for state, ip, port in rows
    )
    return len(rows).to_bytes(4, "little") + body


def test_windows_tcp_table_sees_loopback_and_ignores_other_rows():
    from autofill.local_attach import _TCP_STATE_LISTEN, _hosts_from_ipv4_table, _hosts_from_ipv6_table

    blob = _ipv4_listen_blob(
        [
            (_TCP_STATE_LISTEN, b"\x7f\x00\x00\x01", 9222),
            (5, b"\x0a\x00\x00\x05", 9222),
            (_TCP_STATE_LISTEN, b"\x00\x00\x00\x00", 80),
        ]
    )
    assert _hosts_from_ipv4_table(blob, 9222) == ["127.0.0.1"]
    public = _ipv4_listen_blob([(_TCP_STATE_LISTEN, b"\x00\x00\x00\x00", 9222)])
    assert _hosts_from_ipv4_table(public, 9222) == ["0.0.0.0"]
    addr = b"\x00" * 15 + b"\x01"
    raw_port = socket.htons(9222).to_bytes(4, "little")
    row = addr + b"\x00" * 4 + raw_port + b"\x00" * 16 + b"\x00" * 4 + b"\x00" * 4
    row += (2).to_bytes(4, "little") + b"\x00" * 4
    assert len(row) == 56
    assert _hosts_from_ipv6_table((1).to_bytes(4, "little") + row, 9222) == ["::1"]


def test_listen_text_parses_lsof_and_macos_netstat():
    from autofill.local_attach import _endpoints_from_listen_text

    lsof = "\n".join(
        [
            "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME",
            "Google  9 user  20u  IPv4 0x1  0t0  TCP 127.0.0.1:9222 (LISTEN)",
            "Google  9 user  21u  IPv6 0x2  0t0  TCP [::1]:9222 (LISTEN)",
            "Google  9 user  22u  IPv4 0x3  0t0  TCP *:80 (LISTEN)",
        ]
    )
    assert _endpoints_from_listen_text(lsof) == [
        ("127.0.0.1", 9222),
        ("::1", 9222),
        ("0.0.0.0", 80),
    ]
    netstat = "\n".join(
        [
            "tcp4       0      0  127.0.0.1.9222         *.*                    LISTEN",
            "tcp6       0      0  ::1.9222               *.*                    LISTEN",
            "tcp4       0      0  *.9222                 *.*                    LISTEN",
        ]
    )
    assert _endpoints_from_listen_text(netstat) == [
        ("127.0.0.1", 9222),
        ("::1", 9222),
        ("0.0.0.0", 9222),
    ]


def test_darwin_without_proc_uses_lsof_and_refuses_a_wildcard(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("autofill.local_attach._proc_tcp_present", lambda: False)

    def fake_run(command, **kwargs):
        assert command[0] == "lsof"
        text = "Google  1 user  20u  IPv4 0x1  0t0  TCP 127.0.0.1:9222 (LISTEN)\n"
        return subprocess.CompletedProcess(command, 0, stdout=text, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert listening_hosts(9222) == ["127.0.0.1"]

    def wildcard(command, **kwargs):
        text = "Google  1 user  20u  IPv4 0x1  0t0  TCP *:9222 (LISTEN)\n"
        return subprocess.CompletedProcess(command, 0, stdout=text, stderr="")

    monkeypatch.setattr(subprocess, "run", wildcard)
    with pytest.raises(LoopbackRequired):
        assert_loopback_listen(9222)


def test_windows_listener_check_does_not_read_proc(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def fail_proc(port: int) -> list[str]:
        raise AssertionError(f"proc must not be read for port {port}")

    monkeypatch.setattr("autofill.local_attach._listening_hosts_proc", fail_proc)
    monkeypatch.setattr("autofill.local_attach._listening_hosts_windows", lambda port: ["127.0.0.1"])
    assert listening_hosts(9222) == ["127.0.0.1"]
    assert assert_loopback_listen(9222) == ["127.0.0.1"]
    monkeypatch.setattr(
        "autofill.local_attach._listening_hosts_windows",
        lambda port: ["0.0.0.0"],
    )
    with pytest.raises(LoopbackRequired):
        assert_loopback_listen(9222)


def _helper_request(port: int, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _drive_session(monkeypatch, tmp_path, *, listeners: list[str]) -> tuple[list[str], dict]:
    """POST /v1/sessions against a dedicated session whose Chrome launch is fake.

    ``listeners`` is what a Windows or macOS listener check reports. The
    employer window opening is recorded separately from the fill call.
    ``ChromeProcess.stop`` is stubbed so a refused port cannot signal a real pid.
    """
    from autofill.local_attach import ChromeProcess, DedicatedApplySession, VisibleTab

    monkeypatch.setenv(ENV_FLAG, "1")
    events: list[str] = []
    launched = {"value": False}

    class _Proc:
        pid = 4242

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            return None

    def fake_launch(**kwargs: object) -> ChromeProcess:
        events.append("browser_open")
        launched["value"] = True
        return ChromeProcess(
            process=_Proc(),  # type: ignore[arg-type]
            user_data_dir=kwargs["user_data_dir"],  # type: ignore[arg-type]
            port=int(kwargs["port"]),  # type: ignore[arg-type]
            endpoint=f"http://127.0.0.1:{kwargs['port']}",
            log_path=tmp_path / "chrome.log",
        )

    def fake_fetch(port: int, timeout: float = 1.0) -> dict | None:
        if not launched["value"]:
            return None
        return {
            "Browser": "Chrome/120.0.0.0",
            "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/devtools/browser/abc",
        }

    class _Page:
        url = "https://boards.greenhouse.io/example/jobs/1"

        def evaluate(self, script: str) -> bool:
            return False

    page = _Page()

    class _Driver:
        def start(self) -> _Driver:
            events.append("playwright")
            return self

        def stop(self) -> None:
            events.append("playwright_stop")

    def fake_attach(playwright: object, endpoint: str, *, url_prefix: str = "") -> VisibleTab:
        events.append("attach")
        return VisibleTab(
            browser=object(),
            context=object(),
            page=page,
            endpoint=endpoint,
            target_id="target-1",
            page_url=page.url,
            context_count=1,
            page_urls=[page.url],
        )

    def fake_fill(
        page_obj: object, application_url: str, profile: CandidateProfile, *, cancel: object = None
    ) -> ApplicationResult:
        events.append("fill")
        assert page_obj is page
        assert application_url.startswith("https://boards.greenhouse.io/")
        assert profile.personal.full_name == "Casey Example"
        return ApplicationResult(
            status=Status.READY_FOR_HUMAN_SUBMIT,
            ats=None,
            current_step="done",
            login_status="NOT_REQUIRED",
            fields_detected=3,
            fields_filled=3,
            stopped_before_submit=True,
            submit_controls=["Submit application"],
        )

    def fake_stop(self: ChromeProcess) -> None:
        events.append("chrome_stopped")

    monkeypatch.setattr("autofill.local_attach.launch_dedicated_chrome", fake_launch)
    monkeypatch.setattr("autofill.local_attach.fetch_version", fake_fetch)
    monkeypatch.setattr(ChromeProcess, "stop", fake_stop)
    monkeypatch.setattr(
        "autofill.local_attach.listening_hosts",
        lambda port: events.append("loopback_checked") or list(listeners),
    )
    monkeypatch.setattr("autofill.local_attach.attach_visible_tab", fake_attach)
    monkeypatch.setattr("autofill.local_attach.adopt_employer_tab", lambda context, url: page)
    monkeypatch.setattr("autofill.local_attach._target_id", lambda context, page_obj: "target-1")
    monkeypatch.setattr("autofill.local_attach.fill_visible_tab", fake_fill)
    monkeypatch.setattr("playwright.sync_api.sync_playwright", lambda: _Driver())

    session = DedicatedApplySession(user_data_dir=tmp_path / "apply-profile", debug_port=9222, headless=True)
    helper = LocalHelper(session=session, idle_detach_seconds=-1)  # type: ignore[arg-type]
    server = serve(port=0, helper=helper)
    bound = int(server.server_address[1])
    profile = json.loads(PROFILE.read_text(encoding="utf-8"))
    url = "https://boards.greenhouse.io/example/jobs/1"
    try:
        status, created = _helper_request(bound, "POST", "/v1/sessions", {"applicationUrl": url, "profile": profile})
        assert status == 202
        body: dict = {}
        for _ in range(50):
            _code, body = _helper_request(bound, "GET", f"/v1/sessions/{created['sessionId']}")
            if body.get("status") not in {"queued", "running"}:
                break
            time.sleep(0.02)
        return events, body
    finally:
        helper.close()
        server.shutdown()
        server.server_close()


def test_create_fills_after_the_window_opens_on_windows(monkeypatch, tmp_path):
    """CDP answering and the employer window opening are not a fill.

    Windows has no /proc/net/tcp. The old check treated that as a closed port,
    raised before Playwright started, and left the dedicated window idle.
    """
    events, body = _drive_session(monkeypatch, tmp_path, listeners=["127.0.0.1"])
    assert events.index("browser_open") < events.index("loopback_checked")
    assert events.index("loopback_checked") < events.index("playwright")
    assert events.index("playwright") < events.index("attach")
    assert events.index("attach") < events.index("fill")
    assert "chrome_stopped" not in events
    assert body["status"] == "READY_FOR_HUMAN_SUBMIT"
    assert body["fill"]["fields_filled"] == 3
    assert body["stopped_before_submit"] is True
    assert body["submitted"] is False
    assert body["targetId"] == "target-1"


def test_missing_windows_listeners_do_not_leave_an_idle_window(monkeypatch, tmp_path):
    events, body = _drive_session(monkeypatch, tmp_path, listeners=[])
    assert "browser_open" in events
    assert "loopback_checked" in events
    assert "playwright" not in events
    assert "fill" not in events
    assert "chrome_stopped" in events
    assert body["status"] == "FAILED"
    assert body["error"] == "LocalAttachError"
    assert "fill" not in body


def test_fill_visible_tab_uses_the_hosted_page_budget(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    captured: dict[str, Any] = {}

    def fake_autofill(url, profile, credential_provider=None, options=None):
        captured["max_pages"] = options.max_pages
        captured["page"] = options.page
        return ApplicationResult(
            status=Status.READY_FOR_HUMAN_SUBMIT,
            ats=None,
            current_step="done",
            login_status="NOT_REQUIRED",
            fields_filled=1,
            stopped_before_submit=True,
        )

    monkeypatch.setattr("autofill.local_attach.autofill_application", fake_autofill)
    page = object()
    result = fill_visible_tab(page, "https://boards.greenhouse.io/example/jobs/1", CandidateProfile.load(PROFILE))
    assert captured["page"] is page
    assert captured["max_pages"] == 8
    assert result.fields_filled == 1
    assert result.stopped_before_submit is True


def test_public_bind_is_refused_and_loopback_is_visible():
    import socket

    public = socket.socket()
    public.bind(("0.0.0.0", 0))
    public.listen(1)
    public_port = public.getsockname()[1]
    local = socket.socket()
    local.bind(("127.0.0.1", 0))
    local.listen(1)
    local_port = local.getsockname()[1]
    try:
        with pytest.raises(LoopbackRequired):
            assert_loopback_listen(public_port)
        assert "127.0.0.1" in listening_hosts(local_port)
    finally:
        public.close()
        local.close()


def test_probe_notes_do_not_treat_headless_as_daily_chrome():
    daily, daily_note = _probe_note("headed_daily", cdp_open=False, stderr_text="non-default data directory")
    assert daily is False
    assert "not available" in daily_note
    opened, opened_note = _probe_note("headed_daily", cdp_open=True, stderr_text="")
    assert opened is True
    assert "did not apply" in opened_note
    false_positive, note = _probe_note("headless_omitted", cdp_open=True, stderr_text="")
    assert false_positive is False
    assert "not the employee's daily profile" in note
    explicit, _explicit_note = _probe_note("explicit_default", cdp_open=False, stderr_text="")
    assert explicit is False


def test_application_url_allows_an_employer_site_and_rejects_non_http():
    from autofill.local_attach import validate_application_url

    assert validate_application_url("https://boards.greenhouse.io/example/jobs/1").startswith("https://")
    assert validate_application_url("http://127.0.0.1:9/t172-local-tab.html")
    for url in ("file:///tmp/form.html", "javascript:alert(1)", "chrome://inspect", "https://user:pass@example.com/"):
        with pytest.raises(LocalAttachError):
            validate_application_url(url)


def test_dedicated_launch_does_not_force_the_hosted_viewport(tmp_path):
    args = dedicated_launch_args(
        executable="/usr/bin/google-chrome-stable",
        user_data_dir=tmp_path / "profile",
        port=9222,
        start_url="https://example.com/apply",
        headless=False,
    )
    joined = " ".join(args)
    assert "--window-size" not in joined
    assert "1280" not in joined
    assert "--disable-gpu" not in joined
    assert "--remote-debugging-address=127.0.0.1" in args


def test_flag_off_and_on_still_launch_headless_chromium(monkeypatch):
    """Railway's driver does not switch to CDP when the local-attach flag changes."""
    import playwright.sync_api as sync_api

    from autofill.service.runner import DEFAULT_LAUNCH_ARGS, PlaywrightRunner, _SessionContext, _SharedDriver

    calls: list[tuple[Any, ...]] = []

    class _Browser:
        def close(self) -> None:
            calls.append(("browser.close",))

    class _Chromium:
        def launch(self, *, headless: bool, args: list[str]) -> _Browser:
            calls.append(("launch", headless, list(args)))
            return _Browser()

    class _Playwright:
        chromium = _Chromium()

        def stop(self) -> None:
            calls.append(("playwright.stop",))

    class _Manager:
        def start(self) -> _Playwright:
            return _Playwright()

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _Manager())
    for value in ("", "1"):
        monkeypatch.setenv(ENV_FLAG, value)
        calls.clear()
        driver = _SharedDriver(headless=True, launch_args=list(DEFAULT_LAUNCH_ARGS))
        driver.browser()
        kind, headless, args = calls[0]
        assert kind == "launch"
        assert headless is True
        assert not any("remote-debugging" in item or "user-data-dir" in item for item in args)
        assert "connect_over_cdp" not in inspect.getsource(_SharedDriver.browser)
        driver.stop()
    runner = PlaywrightRunner(workers=1)
    try:
        assert runner.headless is True
        opened = inspect.getsource(_SessionContext.open)
        assert "chromium.launch" in inspect.getsource(_SharedDriver.browser)
        assert "new_context(" in opened
        assert "1280" in opened
        assert "_block_heavy" in opened
    finally:
        runner.shutdown()


def test_detach_stops_the_pipe_and_does_not_close_the_browser():
    from autofill.local_attach import DedicatedApplySession, detach_cdp_client

    closed: list[str] = []
    stopped: list[str] = []

    class _Connection:
        def stop_async(self) -> None:
            stopped.append("pipe")

    class _Impl:
        _connection = _Connection()

    class _Browser:
        _impl_obj = _Impl()

        def _sync(self, awaitable: object) -> object:
            return awaitable

        def close(self) -> None:
            closed.append("browser")

    detach_cdp_client(_Browser())
    assert stopped == ["pipe"]
    assert closed == []

    class _Playwright:
        def stop(self) -> None:
            stopped.append("driver")

        def close(self) -> None:
            closed.append("playwright")

    session = DedicatedApplySession(user_data_dir=Path("/tmp/t172-not-created"), debug_port=9)
    session._playwright = _Playwright()
    session.owns_chrome = False
    session.detach()
    session.stop_launched_chrome()
    assert "driver" in stopped
    assert closed == []


def test_alternate_submit_paths_never_fire(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    monkeypatch.setenv(ENV_FLAG, "1")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.set_content(SUBMIT_PAGE)
            refusals = attempt_alternate_submits(page, ALTERNATE_SUBMITS)
            assert len(refusals) == len(ALTERNATE_SUBMITS)
            flags = _flags(page)
            assert flags == {"submitted": False, "continued": False, "entered": False}
            with pytest.raises(HumanSubmissionRequired):
                drive_control(page, "#submit", "Next", purpose="navigation")
            drive_control(page, "#next", "Save and continue", purpose="navigation")
            flags = _flags(page)
            assert flags["continued"] is True
            assert flags["submitted"] is False
            assert flags["entered"] is False
        finally:
            browser.close()


def test_fill_visible_tab_uses_the_engine_and_does_not_submit(monkeypatch):
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    monkeypatch.setenv(ENV_FLAG, "1")
    server = start_fixture_server(FIXTURE.read_bytes())
    port = int(server.server_address[1])
    url = f"http://127.0.0.1:{port}/t172-local-tab.html"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded")
                result = fill_visible_tab(page, url, CandidateProfile.load(PROFILE))
                assert result.stopped_before_submit is True
                assert result.fields_filled >= 1
                assert page.locator("#first").input_value() == "Casey"
                assert page.locator("#last").input_value() == "Example"
                assert page.locator("#email").input_value() == "casey.example@example.com"
                assert page.evaluate("() => window.__submitted") is False
                assert "t172_visible=same-tab" in page.evaluate("() => document.cookie")
            finally:
                browser.close()
    finally:
        server.shutdown()


class _StatusSession:
    """Stand-in for DedicatedApplySession. It never clicks Final Submit."""

    def __init__(self, outcome: str) -> None:
        self.outcome = outcome
        self.owns_chrome = False
        self.tab = None
        self.detached = 0

    def fill(self, url: str, profile: object) -> tuple[ApplicationResult, bool]:
        if self.outcome == "ready":
            result = ApplicationResult(
                status=Status.READY_FOR_HUMAN_SUBMIT,
                ats=None,
                current_step="done",
                login_status="NOT_REQUIRED",
                fields_detected=3,
                fields_filled=3,
                stopped_before_submit=True,
                submit_controls=["Submit application"],
            )
            return result, False
        if self.outcome == "claimed":
            raise LocalAttachError("The page reported a submit. The local driver does not submit applications.")
        raise LocalAttachError("Dedicated Chrome did not open CDP.")

    def detach(self) -> None:
        self.detached += 1

    def stop_launched_chrome(self) -> None:
        return None

    def browser_running(self) -> bool:
        return True


def _wait_settled(job: Any) -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        if job.state not in {"queued", "running"}:
            return
        time.sleep(0.02)
    raise AssertionError(f"job stayed {job.state}")


def test_status_flag_is_false_only_after_a_reported_submit(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    profile = CandidateProfile.load(PROFILE)
    url = "https://boards.greenhouse.io/example/jobs/1"

    ready = _StatusSession("ready")
    ready_helper = LocalHelper(session=ready, idle_detach_seconds=-1)  # type: ignore[arg-type]
    try:
        job = ready_helper.submit(url, profile)
        _wait_settled(job)
        body = job.public()
        assert body["status"] == "READY_FOR_HUMAN_SUBMIT"
        assert body["submitted"] is False
        assert body["stopped_before_submit"] is True
        assert body["fill"]["stopped_before_submit"] is True
        ready_helper.request_detach(job.session_id)
        detached = job.public()
        assert detached["detached"] is True
        assert detached["status"] == "READY_FOR_HUMAN_SUBMIT"
        assert detached["submitted"] is False
        assert detached["stopped_before_submit"] is True
        assert ready.detached == 1
    finally:
        ready_helper.close()

    other = _StatusSession("other")
    other_helper = LocalHelper(session=other, idle_detach_seconds=-1)  # type: ignore[arg-type]
    try:
        job = other_helper.submit(url, profile)
        _wait_settled(job)
        body = job.public()
        assert body["status"] == "FAILED"
        assert body["submitted"] is None
        assert body["stopped_before_submit"] is True
    finally:
        other_helper.close()

    claimed = _StatusSession("claimed")
    claimed_helper = LocalHelper(session=claimed, idle_detach_seconds=-1)  # type: ignore[arg-type]
    try:
        job = claimed_helper.submit(url, profile)
        _wait_settled(job)
        body = job.public()
        assert body["status"] == "FAILED"
        assert body["submitted"] is True
        assert body["stopped_before_submit"] is False
        assert body["status"] not in {"SUBMITTED", "FILLED"}
        assert "fill" not in body
        claimed_helper.request_detach(job.session_id)
        detached = job.public()
        assert detached["detached"] is True
        assert detached["status"] == "FAILED"
        assert detached["submitted"] is True
        assert detached["stopped_before_submit"] is False
        assert claimed.detached == 1
    finally:
        claimed_helper.close()


def _chrome_ready() -> bool:
    try:
        find_chrome()
    except LocalAttachError:
        return False
    return True


def _testing_chrome() -> str | None:
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        path = playwright.chromium.executable_path
    if path and Path(path).is_file():
        return path
    return None


@pytest.mark.skipif(not _chrome_ready(), reason="Google Chrome is not installed")
@pytest.mark.skipif(os.environ.get("T172_RUN_CHROME_POC") != "1", reason="Set T172_RUN_CHROME_POC=1 to touch Chrome")
def test_daily_profile_debugging_port_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    headed = probe_debug_port("headed_daily", timeout=6, work_dir=tmp_path / "headed")
    explicit = probe_debug_port("explicit_default", timeout=6, work_dir=tmp_path / "explicit")
    headless = probe_debug_port("headless_omitted", timeout=6, work_dir=tmp_path / "headless")
    dedicated = probe_debug_port("dedicated", timeout=8, work_dir=tmp_path / "dedicated")
    assert headed["cdp_open"] is False
    assert headed["daily_profile_attach"] is False
    assert "non-default data directory" in headed["stderr_excerpt"]
    assert explicit["cdp_open"] is False
    assert explicit["daily_profile_attach"] is False
    assert headless["daily_profile_attach"] is False
    assert dedicated["cdp_open"] is True
    assert dedicated["listen_hosts"] == ["127.0.0.1"]
    assert dedicated["default_profile"] is False
    assert str(dedicated["webSocketDebuggerUrl"]).startswith("ws://127.0.0.1:")


@pytest.mark.skipif(os.environ.get("T172_RUN_CHROME_POC") != "1", reason="Set T172_RUN_CHROME_POC=1 to launch Chrome")
@pytest.mark.skipif(not _chrome_ready(), reason="Google Chrome is not installed")
def test_dedicated_profile_fills_the_same_tab(tmp_path, monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    evidence = demonstrate_dedicated_fill(
        fixture_html=FIXTURE,
        profile_path=PROFILE,
        work_dir=tmp_path / "fill",
        screenshot_path=tmp_path / "dedicated-fill.png",
        headless=True,
    )
    assert evidence["created_new_context_for_fill"] is False
    assert evidence["user_data_dir_is_default"] is False
    assert evidence["listen_hosts"] == ["127.0.0.1"]
    assert evidence["same_target"] is True
    assert evidence["chrome_alive_after_disconnect"] is True
    assert evidence["cookie_preserved"] is True
    assert evidence["values"]["email"] == "casey.example@example.com"
    assert evidence["values"]["first"] == "Casey"
    assert evidence["submitted"] is False
    assert evidence["fill"]["stopped_before_submit"] is True
    assert evidence["fill"]["fields_filled"] >= 1
    assert Path(str(evidence["screenshot"])).is_file()


@pytest.mark.skipif(os.environ.get("T172_RUN_CHROME_POC") != "1", reason="Set T172_RUN_CHROME_POC=1 to launch Chrome")
@pytest.mark.skipif(not _chrome_ready(), reason="Google Chrome is not installed")
def test_branded_chrome_ignores_load_extension(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    result = probe_branded_load_extension(EXTENSION, timeout=3)
    assert result["load_extension_ignored"] is True


@pytest.mark.skipif(os.environ.get("T172_RUN_CHROME_POC") != "1", reason="Set T172_RUN_CHROME_POC=1 to launch Chrome")
@pytest.mark.skipif(_testing_chrome() is None, reason="Chrome for Testing is not installed")
def test_extension_debugger_reads_the_same_tab_cookie(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    executable = _testing_chrome()
    assert executable is not None
    result = probe_extension_debugger(EXTENSION, executable=executable, timeout=8)
    assert result["remote_debugging_port"] is False
    assert result["attached"] is True
    assert result["cookie_visible"] is True
