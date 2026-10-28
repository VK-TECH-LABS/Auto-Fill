"""Windows entry for the packaged TileArc Auto-Fill helper (staging).

Railway does not run this module. The frozen Setup.exe calls ``main``, which
sets ``AUTOFILL_LOCAL_ATTACH=1`` in this process only, binds
``127.0.0.1:9410``, and uses a dedicated Chrome profile. Final Submit stays
human. The hosted runner is a different program and does not import this file.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from autofill.local_attach import LOOPBACK, DefaultProfileBlocked, is_default_user_data_dir
from autofill.local_helper import HELPER_PORT, default_profile_dir

HEALTH_SERVICE = "auto-fill-local"
INSTALLER_BASENAME = "TileArc-AutoFill-Helper-Setup-staging.exe"


def packaged_profile_dir() -> Path:
    """The dedicated profile this package will pass to the helper.

    An explicit ``AUTOFILL_LOCAL_PROFILE_DIR`` is kept when it is not Chrome's
    daily directory. The daily directory is refused.
    """
    path = default_profile_dir()
    if is_default_user_data_dir(path):
        raise DefaultProfileBlocked(
            "Refusing Chrome's daily profile. TileArc Auto-Fill uses its own Chrome profile."
        )
    return path


def apply_packaged_environment() -> Path:
    """Set the attach flag, loopback port, and dedicated profile for this process."""
    os.environ["AUTOFILL_LOCAL_ATTACH"] = "1"
    # The staging extension probes this port. The packaged process does not honor another port.
    os.environ["AUTOFILL_LOCAL_HELPER_PORT"] = str(HELPER_PORT)
    profile = packaged_profile_dir()
    os.environ["AUTOFILL_LOCAL_PROFILE_DIR"] = str(profile)
    return profile


def helper_port() -> int:
    raw = os.environ.get("AUTOFILL_LOCAL_HELPER_PORT", str(HELPER_PORT)).strip()
    return int(raw)


def helper_is_healthy(port: int | None = None) -> bool:
    """True when this helper is already answering on loopback."""
    bound = HELPER_PORT if port is None else port
    url = f"http://{LOOPBACK}:{bound}/health"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as response:
            if getattr(response, "status", 200) != 200:
                return False
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("status") == "ok" and payload.get("service") == HEALTH_SERVICE


def playwright_driver_node() -> Path | None:
    """Node binary shipped with Playwright. The helper uses it for CDP, not for a browser."""
    name = "node.exe" if sys.platform == "win32" else "node"
    if getattr(sys, "frozen", False):
        roots: list[Path] = []
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            roots.append(Path(meipass))
        roots.append(Path(sys.executable).resolve().parent)
        roots.append(Path(sys.executable).resolve().parent / "_internal")
        for root in roots:
            direct = root / "playwright" / "driver" / name
            if direct.is_file():
                return direct
            try:
                matches = [item for item in root.rglob(name) if item.is_file() and "driver" in item.parts]
            except OSError:
                matches = []
            if matches:
                return matches[0]
        return None
    try:
        import playwright
    except ImportError:
        return None
    candidate = Path(playwright.__file__).resolve().parent / "driver" / name
    if candidate.is_file():
        return candidate
    return None


def prepare_frozen_playwright() -> None:
    """Point Playwright at the bundled driver when running inside the installer."""
    if not getattr(sys, "frozen", False):
        return
    node = playwright_driver_node()
    if node is not None:
        os.environ["PLAYWRIGHT_NODEJS_PATH"] = str(node)


def dom_ui_script() -> Path:
    """The page script the fill engine loads. PyInstaller must place it beside the module."""
    from autofill.dom_ui import _JS_PATH

    return _JS_PATH


def doctor_report() -> dict[str, Any]:
    """Packaging checks. Chrome may be absent; the driver and the profile must be present."""
    profile = packaged_profile_dir()
    node = playwright_driver_node()
    script = dom_ui_script()
    port = helper_port()
    return {
        "driver": "" if node is None else str(node),
        "driverOk": node is not None,
        "domUi": str(script),
        "domUiOk": script.is_file(),
        "profileDir": str(profile),
        "profileIsDailyChrome": is_default_user_data_dir(profile),
        "port": port,
        "bind": LOOPBACK,
        "attach": os.environ.get("AUTOFILL_LOCAL_ATTACH", ""),
        "service": HEALTH_SERVICE,
        "submit": "human",
    }


def doctor_ok(report: dict[str, Any]) -> bool:
    return bool(
        report.get("driverOk")
        and report.get("domUiOk")
        and report.get("attach") == "1"
        and report.get("bind") == LOOPBACK
        and report.get("port") == HELPER_PORT
        and report.get("profileIsDailyChrome") is False
        and report.get("submit") == "human"
    )


def log_path() -> Path:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(local) if local else Path.home() / "AppData" / "Local"
        directory = root / "TileArc" / "AutoFill"
    else:
        state = os.environ.get("XDG_STATE_HOME", "").strip()
        base = Path(state) if state else Path.home() / ".local" / "state"
        directory = base / "tilearc-autofill"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "helper.log"


def _capture_log() -> None:
    if not getattr(sys, "frozen", False):
        return
    handle = log_path().open("a", encoding="utf-8")
    sys.stdout = handle
    sys.stderr = handle


def _write_doctor(report: dict[str, Any]) -> None:
    text = json.dumps(report)
    print(text, flush=True)
    try:
        destination = log_path().with_name("doctor.json")
        destination.write_text(text + "\n", encoding="utf-8")
    except OSError:
        return


def main(argv: list[str] | None = None) -> int:
    """Start the loopback helper, or exit 0 when it is already healthy."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        apply_packaged_environment()
    except DefaultProfileBlocked as exc:
        print(str(exc), flush=True)
        return 2
    if "--doctor" in args:
        report = doctor_report()
        _write_doctor(report)
        return 0 if doctor_ok(report) else 1
    port = helper_port()
    if helper_is_healthy(port):
        print(f"already listening http://{LOOPBACK}:{port}", flush=True)
        return 0
    prepare_frozen_playwright()
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError as exc:
        print(f"Playwright is not bundled: {exc}", flush=True)
        return 2
    _capture_log()
    from autofill.local_helper import main as helper_main

    return helper_main()


if __name__ == "__main__":
    raise SystemExit(main())
