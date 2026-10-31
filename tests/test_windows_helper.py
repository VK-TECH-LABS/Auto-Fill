"""Windows packaging stays on the dedicated profile and the staging health port.

These tests do not launch Chrome and do not build the installer.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from autofill.local_attach import (
    ENV_FLAG,
    ChromeProcess,
    DefaultProfileBlocked,
    LocalAttachError,
    chrome_search_paths,
    dedicated_launch_args,
    find_chrome,
    is_default_user_data_dir,
    stable_chrome_user_data_dir,
)
from autofill.local_helper import HELPER_PORT, LocalHelper, default_profile_dir, serve
from autofill.win_launcher import (
    INSTALLER_BASENAME,
    apply_packaged_environment,
    doctor_ok,
    doctor_report,
    helper_is_healthy,
    packaged_profile_dir,
    playwright_driver_node,
)

ROOT = Path(__file__).resolve().parents[1]


class _IdleSession:
    owns_chrome = False

    def detach(self) -> None:
        return None

    def browser_running(self) -> bool:
        return False

    def stop_launched_chrome(self) -> None:
        return None


def test_linux_profile_default_is_unchanged(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("AUTOFILL_LOCAL_PROFILE_DIR", raising=False)
    assert default_profile_dir() == Path.home() / ".local" / "share" / "auto-fill" / "apply-profile"
    assert is_default_user_data_dir(stable_chrome_user_data_dir())
    assert is_default_user_data_dir(stable_chrome_user_data_dir() / "Default")
    assert not is_default_user_data_dir(default_profile_dir())


def test_windows_daily_chrome_profile_is_refused(monkeypatch, tmp_path):
    local = tmp_path / "Local"
    daily = local / "Google" / "Chrome" / "User Data"
    (daily / "Default").mkdir(parents=True)
    dedicated = local / "TileArc" / "AutoFill" / "apply-profile"
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("CHROME_USER_DATA_DIR", raising=False)
    monkeypatch.delenv("AUTOFILL_LOCAL_PROFILE_DIR", raising=False)
    assert is_default_user_data_dir(daily)
    assert is_default_user_data_dir(daily / "Default")
    assert not is_default_user_data_dir(dedicated)
    assert default_profile_dir() == dedicated
    with pytest.raises(DefaultProfileBlocked):
        dedicated_launch_args(
            executable="chrome.exe",
            user_data_dir=daily,
            port=9222,
            start_url="about:blank",
        )
    args = dedicated_launch_args(
        executable="chrome.exe",
        user_data_dir=dedicated,
        port=9222,
        start_url="about:blank",
    )
    assert "--remote-debugging-address=127.0.0.1" in args
    assert f"--user-data-dir={dedicated}" in args


def test_windows_chrome_search_includes_program_files(monkeypatch, tmp_path):
    program = tmp_path / "Program Files"
    exe = program / "Google" / "Chrome" / "Application" / "chrome.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("PROGRAMFILES", str(program))
    monkeypatch.setenv("PROGRAMFILES(X86)", str(tmp_path / "x86"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.delenv("CHROME_BIN", raising=False)
    paths = [str(path) for path in chrome_search_paths()]
    assert str(exe) in paths
    assert find_chrome() == str(exe)


def test_missing_chrome_bin_does_not_hide_a_real_binary(monkeypatch, tmp_path):
    exe = tmp_path / "chrome"
    exe.write_text("chrome", encoding="utf-8")
    monkeypatch.setenv("CHROME_BIN", str(tmp_path / "missing"))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(
        "autofill.local_attach.chrome_search_paths",
        lambda: [exe],
    )
    assert find_chrome() == str(exe)
    monkeypatch.setattr("autofill.local_attach.chrome_search_paths", lambda: [])
    with pytest.raises(LocalAttachError):
        find_chrome()


def test_spawn_on_linux_still_starts_a_new_session(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    captured: dict[str, object] = {}

    class _Proc:
        pid = 7

        def poll(self) -> None:
            return None

    def fake_popen(args: list[str], **kwargs: object) -> _Proc:
        captured["args"] = args
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    from autofill.local_attach import _spawn

    _spawn(["/usr/bin/google-chrome-stable"], tmp_path / "chrome.log")
    assert captured["start_new_session"] is True
    assert "creationflags" not in captured


def test_spawn_uses_a_process_group_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    captured: dict[str, object] = {}

    class _Proc:
        pid = 4321

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            return None

    def fake_popen(args: list[str], **kwargs: object) -> _Proc:
        captured["args"] = args
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    from autofill.local_attach import _spawn

    _spawn(["chrome.exe"], tmp_path / "chrome.log")
    assert captured["creationflags"] == 0x200
    assert "start_new_session" not in captured


def test_windows_stop_uses_taskkill_not_killpg(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0)

    def fail_killpg(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("killpg must not run on Windows")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(os, "killpg", fail_killpg, raising=False)

    class _Proc:
        pid = 99

        def poll(self) -> None:
            return None

        def wait(self, timeout: float | None = None) -> int:
            return 0

        def kill(self) -> None:
            return None

    chrome = ChromeProcess(
        process=_Proc(),  # type: ignore[arg-type]
        user_data_dir=tmp_path,
        port=9222,
        endpoint="http://127.0.0.1:9222",
        log_path=tmp_path / "log",
    )
    chrome.stop()
    assert calls[0][:5] == ["taskkill", "/PID", "99", "/T", "/F"]


def test_packaged_environment_forces_the_extension_port(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv(ENV_FLAG, "0")
    monkeypatch.setenv("AUTOFILL_LOCAL_HELPER_PORT", "9999")
    monkeypatch.delenv("AUTOFILL_LOCAL_PROFILE_DIR", raising=False)
    profile = apply_packaged_environment()
    assert os.environ[ENV_FLAG] == "1"
    assert os.environ["AUTOFILL_LOCAL_HELPER_PORT"] == str(HELPER_PORT)
    assert profile == tmp_path / "TileArc" / "AutoFill" / "apply-profile"
    assert packaged_profile_dir() == profile
    report = doctor_report()
    assert report["bind"] == "127.0.0.1"
    assert report["port"] == 9410
    assert report["profileIsDailyChrome"] is False
    assert report["submit"] == "human"
    assert report["domUiOk"] is True
    if report["driverOk"]:
        assert doctor_ok(report)


def test_packaged_environment_pins_a_live_devtools_port(monkeypatch, tmp_path):
    profile = tmp_path / "TileArc" / "AutoFill" / "apply-profile"
    profile.mkdir(parents=True)
    (profile / "DevToolsActivePort").write_text("9555\n/devtools/browser/abc\n", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv(ENV_FLAG, "0")
    monkeypatch.delenv("AUTOFILL_LOCAL_PROFILE_DIR", raising=False)
    monkeypatch.delenv("AUTOFILL_LOCAL_CDP_PORT", raising=False)
    monkeypatch.setattr(
        "autofill.local_attach.fetch_version",
        lambda port, timeout=1.0: {"Browser": "Chrome"} if port == 9555 else None,
    )
    try:
        apply_packaged_environment()
        assert os.environ["AUTOFILL_LOCAL_CDP_PORT"] == "9555"
    finally:
        monkeypatch.delenv("AUTOFILL_LOCAL_CDP_PORT", raising=False)


def test_packaged_environment_refuses_daily_chrome(monkeypatch, tmp_path):
    daily = tmp_path / "Google" / "Chrome" / "User Data"
    daily.mkdir(parents=True)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("AUTOFILL_LOCAL_PROFILE_DIR", str(daily))
    monkeypatch.setenv(ENV_FLAG, "0")
    monkeypatch.setenv("AUTOFILL_LOCAL_HELPER_PORT", "9410")
    with pytest.raises(DefaultProfileBlocked):
        apply_packaged_environment()


def test_helper_is_healthy_reads_the_extension_contract(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "1")
    helper = LocalHelper(session=_IdleSession(), idle_detach_seconds=60)  # type: ignore[arg-type]
    server = serve(port=0, helper=helper)
    port = int(server.server_address[1])
    try:
        assert helper_is_healthy(port)
        assert not helper_is_healthy(port + 1)
    finally:
        server.shutdown()
        server.server_close()
        helper.close()


def test_frozen_windows_driver_stays_beside_the_exe(monkeypatch, tmp_path):
    exe = tmp_path / "TileArcAutoFillHelper.exe"
    node = tmp_path / "playwright" / "driver" / "node.exe"
    node.parent.mkdir(parents=True)
    node.write_bytes(b"node")
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert playwright_driver_node() == node


def test_staging_installer_name_is_the_vinayaka_contract():
    iss = (ROOT / "windows" / "TileArcAutoFillHelper.iss").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "windows-helper.yml").read_text(encoding="utf-8")
    smoke = (ROOT / "windows" / "smoke.ps1").read_text(encoding="utf-8")
    employee = (ROOT / "docs" / "WINDOWS_EMPLOYEE.md").read_text(encoding="utf-8")
    build = (ROOT / "windows" / "BUILD.md").read_text(encoding="utf-8")
    assert INSTALLER_BASENAME in iss
    assert INSTALLER_BASENAME in workflow
    assert INSTALLER_BASENAME in smoke
    assert INSTALLER_BASENAME in employee
    assert INSTALLER_BASENAME in build
    assert "tilearc-autofill-helper-staging" in workflow
    assert "http://127.0.0.1:9410/health" in employee
    assert "auto-fill-local" in employee
    assert "vinayaka/fillo-extension" in employee
    assert "127.0.0.1:9410" in smoke
    assert "auto-fill-local" in smoke
    assert "dedicated-profile" in smoke
    assert 'Start-Process -FilePath $ExePath -ArgumentList "--doctor"' in smoke
    assert "& $ExePath" not in smoke
    assert "if: always()" in workflow
    draft = workflow.split("draft-release:", 1)[1]
    assert "actions/checkout@v4" in draft
    assert '-R "$repo"' in draft
    assert 'repo=VK-TECH-LABS/Auto-Fill' in draft
    assert "--target" in draft
    assert "github.sha" in draft
