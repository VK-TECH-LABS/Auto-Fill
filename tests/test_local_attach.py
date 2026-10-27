"""Local same-tab attach. Final Submit stays refused, and the hosted runner is unchanged."""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Any

import pytest

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
