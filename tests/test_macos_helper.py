"""macOS packaging stays on the dedicated profile and the staging health port.

These tests do not launch Chrome and do not build the disk image.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

from autofill.local_attach import (
    ENV_FLAG,
    DefaultProfileBlocked,
    chrome_search_paths,
    dedicated_launch_args,
    is_default_user_data_dir,
    listening_hosts,
    stable_chrome_user_data_dir,
)
from autofill.local_helper import HELPER_PORT, default_profile_dir
from autofill.win_launcher import (
    MACOS_APP_NAME,
    MACOS_ARTIFACT_NAME,
    MACOS_ARTIFACT_NAME_ARM64,
    MACOS_ARTIFACT_NAME_X86_64,
    MACOS_BUNDLE_ID,
    MACOS_INSTALLER_BASENAME,
    MACOS_INSTALLER_BASENAME_ARM64,
    MACOS_INSTALLER_BASENAME_X86_64,
    MACOS_LOGIN_LABEL,
    MACOS_RELEASE_TAG,
    MACOS_VOLUME_NAME,
    applications_copy,
    apply_packaged_environment,
    install_macos_login_item,
    main,
    playwright_driver_node,
    render_login_item_plist,
    write_login_item,
)

ROOT = Path(__file__).resolve().parents[1]


def test_linux_chrome_search_is_unchanged(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    paths = [path.as_posix() for path in chrome_search_paths()]
    assert "/usr/bin/google-chrome-stable" in paths
    assert not any(path.endswith("Google Chrome") for path in paths)


def test_macos_profile_is_dedicated_and_daily_chrome_is_refused(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("AUTOFILL_LOCAL_PROFILE_DIR", raising=False)
    monkeypatch.delenv("CHROME_USER_DATA_DIR", raising=False)
    daily = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    dedicated = Path.home() / "Library" / "Application Support" / "TileArc" / "AutoFill" / "apply-profile"
    assert stable_chrome_user_data_dir() == daily
    assert default_profile_dir() == dedicated
    assert is_default_user_data_dir(daily)
    assert is_default_user_data_dir(daily / "Default")
    assert is_default_user_data_dir(Path.home() / "Library" / "Application Support" / "Google" / "Chrome Canary")
    assert not is_default_user_data_dir(dedicated)
    with pytest.raises(DefaultProfileBlocked):
        dedicated_launch_args(
            executable="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            user_data_dir=daily,
            port=9222,
            start_url="about:blank",
        )
    args = dedicated_launch_args(
        executable="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        user_data_dir=dedicated,
        port=9222,
        start_url="about:blank",
    )
    assert "--remote-debugging-address=127.0.0.1" in args
    assert f"--user-data-dir={dedicated}" in args


def test_macos_chrome_search_lists_the_stable_app_first(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    paths = chrome_search_paths()
    assert paths[0] == Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    home_stable = Path.home() / "Applications" / "Google Chrome.app" / "Contents" / "MacOS" / "Google Chrome"
    assert home_stable in paths


def test_spawn_on_macos_starts_a_new_session(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    captured: dict[str, object] = {}

    class _Proc:
        pid = 11

        def poll(self) -> None:
            return None

    def fake_popen(args: list[str], **kwargs: object) -> _Proc:
        captured["args"] = args
        captured.update(kwargs)
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    from autofill.local_attach import _spawn

    _spawn(["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"], tmp_path / "chrome.log")
    assert captured["start_new_session"] is True
    assert "creationflags" not in captured


def test_packaged_environment_on_macos_forces_the_extension_port(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delenv("AUTOFILL_LOCAL_PROFILE_DIR", raising=False)
    monkeypatch.delenv("CHROME_USER_DATA_DIR", raising=False)
    monkeypatch.setenv(ENV_FLAG, "0")
    monkeypatch.setenv("AUTOFILL_LOCAL_HELPER_PORT", "9999")
    profile = apply_packaged_environment()
    assert profile == Path.home() / "Library" / "Application Support" / "TileArc" / "AutoFill" / "apply-profile"
    assert profile == default_profile_dir()
    assert os.environ["AUTOFILL_LOCAL_HELPER_PORT"] == str(HELPER_PORT)
    assert os.environ[ENV_FLAG] == "1"


def _macos_app_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    app = tmp_path / MACOS_APP_NAME
    resources = app / "Contents" / "Resources" / "playwright" / "driver" / "node"
    frameworks = app / "Contents" / "Frameworks" / "playwright" / "driver" / "node"
    resources.parent.mkdir(parents=True)
    frameworks.parent.mkdir(parents=True)
    resources.write_bytes(b"resource")
    frameworks.write_bytes(b"framework")
    executable = app / "Contents" / "MacOS" / "TileArcAutoFillHelper"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"exe")
    return executable, frameworks, resources


def test_frozen_macos_app_prefers_the_frameworks_driver(monkeypatch, tmp_path):
    executable, frameworks, _resources = _macos_app_tree(tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    assert playwright_driver_node() == frameworks


def test_runtime_hook_points_playwright_at_the_frameworks_node(monkeypatch, tmp_path):
    executable, frameworks, _resources = _macos_app_tree(tmp_path)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    # setenv records the previous value so teardown removes the path the hook writes.
    # delenv does not, and a leftover PLAYWRIGHT_NODEJS_PATH points later tests at this fake node.
    monkeypatch.setenv("PLAYWRIGHT_NODEJS_PATH", "restore-me")
    source = (ROOT / "windows" / "pyi_rth_playwright.py").read_text(encoding="utf-8")
    exec(compile(source, "pyi_rth_playwright.py", "exec"), {"__name__": "pyi_rth_playwright"})  # noqa: S102
    assert Path(os.environ["PLAYWRIGHT_NODEJS_PATH"]) == frameworks


def test_login_item_plist_points_at_the_app(tmp_path):
    executable = (
        tmp_path
        / "Applications"
        / MACOS_APP_NAME
        / "Contents"
        / "MacOS"
        / "TileArcAutoFillHelper"
    )
    payload = plistlib.loads(render_login_item_plist(executable))
    assert payload["Label"] == MACOS_LOGIN_LABEL == MACOS_BUNDLE_ID
    assert payload["ProgramArguments"] == [str(executable)]
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is False
    destination = tmp_path / "LaunchAgents" / f"{MACOS_LOGIN_LABEL}.plist"
    write_login_item(executable, destination)
    assert destination.is_file()
    assert plistlib.loads(destination.read_bytes())["ProgramArguments"] == [str(executable)]


def test_login_item_is_only_for_an_applications_copy(tmp_path, monkeypatch):
    installed = Path("/Applications") / MACOS_APP_NAME / "Contents" / "MacOS" / "TileArcAutoFillHelper"
    on_disk = Path("/Volumes") / MACOS_VOLUME_NAME / MACOS_APP_NAME / "Contents" / "MacOS" / "TileArcAutoFillHelper"
    in_dist = tmp_path / "dist" / MACOS_APP_NAME / "Contents" / "MacOS" / "TileArcAutoFillHelper"
    assert applications_copy(installed)
    assert not applications_copy(on_disk)
    assert not applications_copy(in_dist)

    home = tmp_path / "home"
    executable = home / "Applications" / MACOS_APP_NAME / "Contents" / "MacOS" / "TileArcAutoFillHelper"
    executable.parent.mkdir(parents=True)
    executable.write_text("helper", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Path.home() on Windows
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    install_macos_login_item()
    plist = home / "Library" / "LaunchAgents" / f"{MACOS_LOGIN_LABEL}.plist"
    assert plist.is_file()
    assert plistlib.loads(plist.read_bytes())["ProgramArguments"] == [str(executable.resolve())]

    monkeypatch.setattr(sys, "executable", str(in_dist))
    other_home = tmp_path / "other-home"
    monkeypatch.setenv("HOME", str(other_home))
    monkeypatch.setenv("USERPROFILE", str(other_home))
    install_macos_login_item()
    assert not (other_home / "Library" / "LaunchAgents" / f"{MACOS_LOGIN_LABEL}.plist").exists()


def test_doctor_does_not_register_the_login_item(monkeypatch, tmp_path):
    home = tmp_path / "home"
    executable = home / "Applications" / MACOS_APP_NAME / "Contents" / "MacOS" / "TileArcAutoFillHelper"
    executable.parent.mkdir(parents=True)
    executable.write_text("helper", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Path.home() on Windows
    monkeypatch.delenv("AUTOFILL_LOCAL_PROFILE_DIR", raising=False)
    monkeypatch.delenv("CHROME_USER_DATA_DIR", raising=False)
    monkeypatch.setenv(ENV_FLAG, "0")
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(executable))
    code = main(["--doctor"])
    assert code == 1
    assert not (home / "Library" / "LaunchAgents" / f"{MACOS_LOGIN_LABEL}.plist").exists()
    doctor = home / "Library" / "Application Support" / "TileArc" / "AutoFill" / "doctor.json"
    assert doctor.is_file()
    text = doctor.read_text(encoding="utf-8")
    assert '"port": 9410' in text
    assert '"bind": "127.0.0.1"' in text
    assert '"submit": "human"' in text


def test_staging_macos_artifact_is_the_installer_contract():
    workflow = (ROOT / ".github" / "workflows" / "macos-helper.yml").read_text(encoding="utf-8")
    windows = (ROOT / ".github" / "workflows" / "windows-helper.yml").read_text(encoding="utf-8")
    employee = (ROOT / "docs" / "MACOS_EMPLOYEE.md").read_text(encoding="utf-8")
    windows_employee = (ROOT / "docs" / "WINDOWS_EMPLOYEE.md").read_text(encoding="utf-8")
    build = (ROOT / "macos" / "BUILD.md").read_text(encoding="utf-8")
    windows_build = (ROOT / "windows" / "BUILD.md").read_text(encoding="utf-8")
    spec = (ROOT / "macos" / "autofill_helper.spec").read_text(encoding="utf-8")
    script = (ROOT / "macos" / "build.sh").read_text(encoding="utf-8")
    smoke = (ROOT / "macos" / "smoke.sh").read_text(encoding="utf-8")
    publish = (ROOT / "macos" / "publish-draft.sh").read_text(encoding="utf-8")
    readme = (ROOT / "macos" / "dmg-readme.txt").read_text(encoding="utf-8")

    assert MACOS_INSTALLER_BASENAME == MACOS_INSTALLER_BASENAME_ARM64
    assert MACOS_ARTIFACT_NAME == MACOS_ARTIFACT_NAME_ARM64
    assert MACOS_INSTALLER_BASENAME_ARM64 != MACOS_INSTALLER_BASENAME_X86_64
    for filename in (MACOS_INSTALLER_BASENAME_ARM64, MACOS_INSTALLER_BASENAME_X86_64):
        assert filename in workflow
        assert filename in build
        assert filename in employee
        assert filename in publish
        assert filename in windows
        assert filename in windows_employee
        assert filename in windows_build
        assert filename in readme
    assert MACOS_ARTIFACT_NAME_ARM64 in workflow
    assert MACOS_ARTIFACT_NAME_ARM64 in build
    assert MACOS_ARTIFACT_NAME_X86_64 in workflow
    assert MACOS_ARTIFACT_NAME_X86_64 in build
    assert MACOS_APP_NAME in spec
    assert MACOS_APP_NAME in employee
    assert MACOS_APP_NAME in readme
    assert MACOS_VOLUME_NAME in script
    assert MACOS_RELEASE_TAG in workflow
    assert MACOS_RELEASE_TAG in publish
    assert MACOS_RELEASE_TAG in build
    assert "runs-on: ${{ matrix.runner }}" in workflow
    assert "runner: macos-latest" in workflow
    assert "runner: macos-15-intel" in workflow
    assert "Build Setup.dmg (${{ matrix.arch }})" in workflow
    assert "Add macOS assets to draft release" in workflow
    assert "runs-on: windows-latest" in windows
    assert "path: dist/TileArc-AutoFill-Helper-Setup-staging.exe" in windows
    assert "TileArc-AutoFill-Helper-Setup-staging.exe" in windows
    assert "bash macos/build.sh" in workflow
    assert "bash macos/smoke.sh" in workflow
    assert "bash macos/publish-draft.sh" in workflow
    assert "if: always()" in workflow
    assert "actions/checkout@v4" in workflow.split("draft-release:", 1)[1]
    assert "http://127.0.0.1:9410/health" in employee
    assert "http://127.0.0.1:9410/health" in smoke
    assert "auto-fill-local" in employee
    assert "auto-fill-local" in smoke
    assert "dedicated-profile" in smoke
    assert "Open Anyway" in employee
    assert "xattr -dr com.apple.quarantine" in employee
    assert "Apple Silicon" in employee
    assert "Intel" in employee
    assert "Apple Silicon" in publish
    assert "Intel" in publish
    assert "sha256sum" in publish
    assert "gh release delete-asset" in publish
    assert "TileArc-AutoFill-Helper-Setup-staging.dmg" in publish
    assert "path: dist/TileArc-AutoFill-Helper-Setup-staging.dmg" not in workflow
    assert "hosted TileArc companion" in employee
    assert "hosted companion remains the fallback" in build
    assert "Intel Macs stay on the hosted companion" not in build
    assert "An Intel Mac uses the hosted TileArc companion" not in employee
    assert "macos-15-intel" in build
    assert "vinayaka/fillo-extension" in employee
    assert "Final Submit" in employee
    assert MACOS_LOGIN_LABEL in employee
    assert "win_launcher.py" in spec
    assert 'root / "windows" / "pyi_rth_playwright.py"' in spec
    assert "BUNDLE(" in spec
    assert 'excludes=["autofill.service"' in spec
    assert "argv_emulation=False" in spec
    assert "--doctor" in smoke
    assert "127.0.0.1:9410" in smoke
    assert '"hdiutil", "attach"' in smoke
    assert "--draft" in publish
    assert "--latest=false" in publish
    assert "--target" in publish
    assert 'repo="${TILEARC_RELEASE_REPO:-VK-TECH-LABS/Auto-Fill}"' in publish
    assert '-R "$repo"' in publish
    assert "application/x-apple-diskimage" in build
    assert '"arch": "arm64"' in build
    assert "TileArc-AutoFill-Helper-Setup-staging.exe" in build
    assert "MACOS_EMPLOYEE.md" in windows_employee
    assert MACOS_INSTALLER_BASENAME in windows_build
    assert "pyinstaller==6.16.0" in script
    assert "macos_require_dmg_name" in script
    assert "lipo" in script
    assert "macos_require_dmg_name" in smoke
    assert 'target_arch=None' in spec


def _disk_name(function: str, arg: str) -> subprocess.CompletedProcess[str]:
    script = ROOT / "macos" / "disk-name.sh"
    return subprocess.run(
        ["bash", "-c", f'source "$1"; {function} "$2"', "bash", str(script), arg],
        check=False,
        capture_output=True,
        text=True,
    )


def test_disk_names_match_the_installer_constants():
    arm = _disk_name("macos_dmg_basename_for", "arm64")
    intel = _disk_name("macos_dmg_basename_for", "x86_64")
    assert arm.returncode == 0
    assert intel.returncode == 0
    assert arm.stdout.strip() == MACOS_INSTALLER_BASENAME_ARM64
    assert intel.stdout.strip() == MACOS_INSTALLER_BASENAME_X86_64
    arm_label = _disk_name("macos_dmg_label_for", "arm64")
    intel_label = _disk_name("macos_dmg_label_for", "x86_64")
    assert arm_label.stdout.strip() == "Apple Silicon (arm64)"
    assert intel_label.stdout.strip() == "Intel (x86_64)"
    rejected = _disk_name("macos_dmg_basename_for", "aarch64")
    assert rejected.returncode != 0


def test_disk_name_rejects_a_mismatched_requested_dmg():
    script = ROOT / "macos" / "disk-name.sh"
    mismatched = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; TILEARC_MACOS_DMG="$2" macos_require_dmg_name',
            "bash",
            str(script),
            "TileArc-AutoFill-Helper-Setup-staging.dmg",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert mismatched.returncode != 0
    host = subprocess.run(["uname", "-m"], check=True, capture_output=True, text=True).stdout.strip()
    if host not in {"arm64", "x86_64"}:
        return
    expected = _disk_name("macos_dmg_basename_for", host).stdout.strip()
    matched = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; TILEARC_MACOS_DMG="$2" macos_require_dmg_name',
            "bash",
            str(script),
            expected,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert matched.returncode == 0
    assert matched.stdout.strip() == expected


def test_macos_debug_port_is_visible_without_proc(monkeypatch):
    """macOS has no /proc/net/tcp. A loopback CDP port must still count as listening."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr("autofill.local_attach._proc_tcp_present", lambda: False)

    def fake_run(command, **kwargs):
        assert command[:2] == ["lsof", "-nP"]
        text = "Google Chrome  4 user  12u  IPv4 0xabc  0t0  TCP 127.0.0.1:9333 (LISTEN)\n"
        return subprocess.CompletedProcess(command, 0, stdout=text, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert listening_hosts(9333) == ["127.0.0.1"]
