"""Local same-tab Chrome attach. The hosted Railway service does not import this.

Launching a browser requires ``AUTOFILL_LOCAL_ATTACH=1``. Debugging endpoints
must be ``http://127.0.0.1:<port>``. The fill path reuses
:func:`autofill.engine.autofill_application` on the browser's existing context
and never opens a second context. Clicks go through
:func:`autofill.safeguards.perform_click`. Final Submit stays human.

This module is original to this AGPL-3.0-only repository. It does not copy
ApplyPilot source.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from autofill.dom_ui import PauseGate
from autofill.engine import ApplicationResult, AutofillOptions, autofill_application
from autofill.profile import CandidateProfile
from autofill.safeguards import (
    HUMAN_SUBMIT_ONLY,
    ActionClass,
    HumanSubmissionRequired,
    SubmitBlockedError,
    action_class,
    perform_click,
)

logger = logging.getLogger("autofill.local_attach")

ENV_FLAG = "AUTOFILL_LOCAL_ATTACH"
LOOPBACK = "127.0.0.1"
_DAILY_BLOCK_TEXT = "non-default data directory"
_SCRIPT_PURPOSES = frozenset({"eval", "form_submit", "keyboard_enter", "request_submit"})
_CONTROL_JS = """(selector) => {
  const el = document.querySelector(selector);
  if (!el) return null;
  const name = (el.innerText || el.value || el.getAttribute("aria-label") || "")
    .replace(/\\s+/g, " ")
    .trim();
  return {name, controlType: (el.getAttribute("type") || "").toLowerCase()};
}"""


class LocalAttachError(RuntimeError):
    """The local attach driver refused or could not complete a step."""


class LoopbackRequired(LocalAttachError):
    """A debugging endpoint was not bound to 127.0.0.1."""


class DefaultProfileBlocked(LocalAttachError):
    """The dedicated launcher was pointed at Chrome's default user-data-dir."""


class FeatureDisabled(LocalAttachError):
    """``AUTOFILL_LOCAL_ATTACH`` is not set. The hosted service leaves it unset."""


class DualSessionRefused(LocalAttachError):
    """Refusing to create a browser context. That would drop the visible tab's cookies."""


def feature_enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip() == "1"


def require_feature() -> None:
    if not feature_enabled():
        raise FeatureDisabled(
            f"Set {ENV_FLAG}=1 to use the local Chrome attach. The hosted service never sets this."
        )
    if not HUMAN_SUBMIT_ONLY:
        raise HumanSubmissionRequired("HUMAN_SUBMIT_ONLY was disabled. That is not a supported configuration.")


def config_home() -> Path:
    if os.environ.get("CHROME_CONFIG_HOME"):
        return Path(os.environ["CHROME_CONFIG_HOME"])
    if os.environ.get("XDG_CONFIG_HOME"):
        return Path(os.environ["XDG_CONFIG_HOME"])
    return Path.home() / ".config"


def stable_chrome_user_data_dir() -> Path:
    """Google Chrome's daily profile directory on this operating system."""
    override = os.environ.get("CHROME_USER_DATA_DIR", "").strip()
    if override:
        return Path(override)
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    return config_home() / "google-chrome"


def _windows_user_data_dirs() -> list[Path]:
    """Daily Chrome data directories on Windows. Not used as the Apply profile."""
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if not local:
        local = str(Path.home() / "AppData" / "Local")
    base = Path(local)
    names = (
        Path("Google") / "Chrome" / "User Data",
        Path("Google") / "Chrome Beta" / "User Data",
        Path("Google") / "Chrome Dev" / "User Data",
        Path("Google") / "Chrome SxS" / "User Data",
        Path("Chromium") / "User Data",
    )
    return [base / name for name in names]


def _darwin_user_data_dirs() -> list[Path]:
    """Daily Chrome data directories on macOS. Not used as the Apply profile."""
    root = Path.home() / "Library" / "Application Support"
    names = (
        Path("Google") / "Chrome",
        Path("Google") / "Chrome Beta",
        Path("Google") / "Chrome Canary",
        Path("Chromium"),
    )
    return [root / name for name in names]


def default_user_data_dirs() -> list[Path]:
    """Directories Chrome treats as the daily profile. The dedicated launcher refuses these."""
    if sys.platform == "win32":
        dirs = _windows_user_data_dirs()
    elif sys.platform == "darwin":
        dirs = _darwin_user_data_dirs()
    else:
        root = config_home()
        names = (
            "google-chrome",
            "google-chrome-beta",
            "google-chrome-unstable",
            "google-chrome-canary",
            "google-chrome-for-testing",
            "chromium",
        )
        dirs = [root / name for name in names]
    override = os.environ.get("CHROME_USER_DATA_DIR", "").strip()
    if override:
        dirs.append(Path(override))
    return dirs


def _resolved(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser()


def is_default_user_data_dir(path: Path) -> bool:
    """True for a daily Chrome data directory or anything inside it."""
    resolved = _resolved(path)
    for candidate in default_user_data_dirs():
        other = _resolved(candidate)
        if resolved == other:
            return True
        try:
            resolved.relative_to(other)
        except ValueError:
            continue
        return True
    return False


def parse_endpoint(endpoint: str) -> tuple[str, int]:
    """Accept only ``http`` or ``ws`` on ``127.0.0.1`` with an explicit port."""
    parsed = urlparse((endpoint or "").strip())
    if parsed.scheme not in {"http", "ws"}:
        raise LoopbackRequired("The debugging endpoint must be http://127.0.0.1:<port> or ws://127.0.0.1:<port>.")
    if parsed.username or parsed.password:
        raise LoopbackRequired("The debugging endpoint must not carry credentials.")
    host = parsed.hostname or ""
    if host != LOOPBACK:
        raise LoopbackRequired(f"Refusing debugging endpoint host {host!r}. Only 127.0.0.1 is allowed.")
    if parsed.port is None:
        raise LoopbackRequired("The debugging endpoint needs a port.")
    return host, parsed.port


def endpoint_for_port(port: int) -> str:
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise LoopbackRequired("Debugging port must be an integer from 1 to 65535.")
    return f"http://{LOOPBACK}:{port}"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return int(sock.getsockname()[1])


def _ipv4_from_proc(hex_ip: str) -> str:
    raw = bytes.fromhex(hex_ip)
    return ".".join(str(part) for part in raw[::-1])


def _ipv6_from_proc(hex_ip: str) -> str:
    raw = bytes.fromhex(hex_ip)
    words = [raw[index : index + 4][::-1] for index in range(0, 16, 4)]
    addr = b"".join(words)
    if addr == b"\x00" * 15 + b"\x01":
        return "::1"
    if addr == b"\x00" * 16:
        return "::"
    return ":".join(f"{addr[index]:02x}{addr[index + 1]:02x}" for index in range(0, 16, 2))


def _listening_hosts_proc(port: int) -> list[str]:
    """Hosts with a TCP listener on ``port``, from ``/proc/net/tcp``."""
    hosts: list[str] = []
    for path, width, decode in (
        (Path("/proc/net/tcp"), 8, _ipv4_from_proc),
        (Path("/proc/net/tcp6"), 32, _ipv6_from_proc),
    ):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4 or parts[3] != "0A":
                continue
            local = parts[1]
            ip_hex, port_hex = local.split(":")
            if len(ip_hex) != width or int(port_hex, 16) != port:
                continue
            hosts.append(decode(ip_hex))
    return hosts


def _proc_tcp_present() -> bool:
    return Path("/proc/net/tcp").is_file() or Path("/proc/net/tcp6").is_file()


# MIB_TCP_STATE_LISTEN. Same value for the IPv4 and IPv6 tables.
_TCP_STATE_LISTEN = 2
_AF_INET = 2
_AF_INET6 = 23
_TCP_TABLE_OWNER_PID_LISTENER = 3
_ERROR_INSUFFICIENT_BUFFER = 122
# IPv4 MIB_TCPROW_OWNER_PID is 6 DWORDs. IPv6 MIB_TCP6ROW_OWNER_PID is 56 bytes
# and stores dwState after the addresses, not before them.
_IPV4_ROW_SIZE = 24
_IPV6_ROW_SIZE = 56
_LISTEN_ENDPOINT = re.compile(
    r"(?P<host>\[::1\]|\[::\]|::1|::|\*|0\.0\.0\.0|127\.0\.0\.1|(?:\d{1,3}\.){3}\d{1,3})"
    r"[.:]"
    r"(?P<port>\d+)"
)


def _normalize_listen_host(host: str) -> str:
    """Map a tool's bind address to the form ``assert_loopback_listen`` checks."""
    if host == "*":
        return "0.0.0.0"
    if host in {"[::]", "::"}:
        return "::"
    if host == "[::1]":
        return "::1"
    return host


def _endpoints_from_listen_text(text: str) -> list[tuple[str, int]] | None:
    """Parse ``lsof``, ``ss``, or ``netstat`` listen lines.

    ``None`` means a LISTEN line was not understood. An empty list means the
    tool reported no listeners.
    """
    found: list[tuple[str, int]] = []
    for line in text.splitlines():
        if "LISTEN" not in line.upper():
            continue
        matches = list(_LISTEN_ENDPOINT.finditer(line))
        if not matches:
            return None
        for match in matches:
            found.append((_normalize_listen_host(match.group("host")), int(match.group("port"))))
    return found


def _listening_hosts_tool(port: int) -> list[str]:
    """Listeners from ``lsof`` or ``netstat`` when ``/proc/net/tcp`` is absent.

    macOS has no proc tcp table. Treating that as an empty listener list made
    ``assert_loopback_listen`` raise after Chrome had opened the employer URL.
    """
    commands = (
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
        ["ss", "-H", "-ltn", f"sport = :{port}"],
        ["netstat", "-an"],
    )
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        parsed = _endpoints_from_listen_text(f"{completed.stdout or ''}\n{completed.stderr or ''}")
        if parsed is None:
            continue
        if parsed or completed.returncode == 0:
            return [host for host, bound in parsed if bound == port]
    raise LocalAttachError(
        "Could not read TCP listeners to confirm the debugging port is on 127.0.0.1."
    )


def _windows_tcp_table(family: int) -> bytes:
    """Raw ``GetExtendedTcpTable`` listener blob. Windows only."""
    import ctypes
    from ctypes import wintypes

    # WinDLL exists on Windows. typeshed omits it when typechecking on other platforms.
    load_library = getattr(ctypes, "WinDLL", None)
    if load_library is None:
        raise OSError("The Windows TCP table is only available on Windows.")
    iphlpapi = load_library("iphlpapi")
    get_table = iphlpapi.GetExtendedTcpTable
    get_table.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.BOOL,
        wintypes.ULONG,
        ctypes.c_int,
        wintypes.ULONG,
    ]
    get_table.restype = wintypes.DWORD
    size = wintypes.DWORD(0)
    status = int(get_table(None, ctypes.byref(size), False, family, _TCP_TABLE_OWNER_PID_LISTENER, 0))
    if status == 0 and size.value == 0:
        return b""
    if status not in (0, _ERROR_INSUFFICIENT_BUFFER):
        raise OSError(status)
    for _attempt in range(3):
        if size.value <= 0:
            return b""
        buffer = ctypes.create_string_buffer(size.value)
        status = int(
            get_table(
                ctypes.addressof(buffer),
                ctypes.byref(size),
                False,
                family,
                _TCP_TABLE_OWNER_PID_LISTENER,
                0,
            )
        )
        if status == 0:
            return buffer.raw[: size.value]
        if status != _ERROR_INSUFFICIENT_BUFFER:
            raise OSError(status)
    raise OSError(_ERROR_INSUFFICIENT_BUFFER)


def _hosts_from_ipv4_table(blob: bytes, port: int) -> list[str]:
    """Decode an IPv4 ``TCP_TABLE_OWNER_PID_LISTENER`` blob for ``port``."""
    if len(blob) < 4:
        return []
    count = int.from_bytes(blob[:4], "little")
    hosts: list[str] = []
    for index in range(count):
        start = 4 + index * _IPV4_ROW_SIZE
        row = blob[start : start + _IPV4_ROW_SIZE]
        if len(row) < _IPV4_ROW_SIZE:
            break
        state = int.from_bytes(row[0:4], "little")
        if state != _TCP_STATE_LISTEN:
            continue
        raw_port = int.from_bytes(row[8:12], "little")
        if socket.ntohs(raw_port & 0xFFFF) != port:
            continue
        addr = int.from_bytes(row[4:8], "little")
        hosts.append(".".join(str(part) for part in addr.to_bytes(4, "little")))
    return hosts


def _hosts_from_ipv6_table(blob: bytes, port: int) -> list[str]:
    """Decode an IPv6 ``TCP_TABLE_OWNER_PID_LISTENER`` blob for ``port``."""
    if len(blob) < 4:
        return []
    count = int.from_bytes(blob[:4], "little")
    hosts: list[str] = []
    for index in range(count):
        start = 4 + index * _IPV6_ROW_SIZE
        row = blob[start : start + _IPV6_ROW_SIZE]
        if len(row) < _IPV6_ROW_SIZE:
            break
        state = int.from_bytes(row[48:52], "little")
        if state != _TCP_STATE_LISTEN:
            continue
        raw_port = int.from_bytes(row[20:24], "little")
        if socket.ntohs(raw_port & 0xFFFF) != port:
            continue
        hosts.append(_format_ipv6(row[0:16]))
    return hosts


def _format_ipv6(addr: bytes) -> str:
    """Render a network-order IPv6 address. ``::`` and ``::1`` stay short."""
    if addr == b"\x00" * 16:
        return "::"
    if addr == b"\x00" * 15 + b"\x01":
        return "::1"
    return ":".join(f"{addr[index]:02x}{addr[index + 1]:02x}" for index in range(0, 16, 2))


def _listening_hosts_windows(port: int) -> list[str]:
    """Listeners from the Windows TCP table. ``/proc/net/tcp`` does not exist there."""
    try:
        ipv4 = _windows_tcp_table(_AF_INET)
        ipv6 = _windows_tcp_table(_AF_INET6)
    except OSError as exc:
        raise LocalAttachError(
            "Could not read Windows TCP listeners to confirm the debugging port is on 127.0.0.1."
        ) from exc
    return _hosts_from_ipv4_table(ipv4, port) + _hosts_from_ipv6_table(ipv6, port)


def listening_hosts(port: int) -> list[str]:
    """Hosts with a TCP listener on ``port``.

    Linux reads ``/proc/net/tcp``. Windows and macOS do not have that file.
    Missing proc used to look like "nothing is listening", so a dedicated
    Chrome that had already opened the employer URL never received a
    Playwright session.
    """
    if sys.platform == "win32":
        return _listening_hosts_windows(port)
    if _proc_tcp_present():
        return _listening_hosts_proc(port)
    return _listening_hosts_tool(port)


def assert_loopback_listen(port: int) -> list[str]:
    hosts = listening_hosts(port)
    if not hosts:
        raise LocalAttachError(f"Nothing is listening on port {port}.")
    bad = [host for host in hosts if host not in {LOOPBACK, "::1"}]
    if bad:
        raise LoopbackRequired(f"Debugging port {port} is listening on {bad}. Refusing to attach.")
    return hosts


def _registry_chrome_paths() -> list[str]:
    """App Paths entries for chrome.exe. Empty off Windows."""
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []

    keys = (
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe",
        ),
    )
    found: list[str] = []
    for hive, subkey in keys:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                value, _kind = winreg.QueryValueEx(key, "")
        except OSError:
            continue
        text = str(value or "").strip().strip('"')
        if text:
            found.append(text)
    return found


def _windows_chrome_paths() -> list[Path]:
    paths = [Path(raw) for raw in _registry_chrome_paths()]
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    paths.append(Path(program_files) / "Google" / "Chrome" / "Application" / "chrome.exe")
    paths.append(Path(program_files_x86) / "Google" / "Chrome" / "Application" / "chrome.exe")
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        root = Path(local)
        paths.append(root / "Google" / "Chrome" / "Application" / "chrome.exe")
        paths.append(root / "Google" / "Chrome SxS" / "Application" / "chrome.exe")
    return paths


def _darwin_chrome_paths() -> list[Path]:
    """Well-known Google Chrome.app binaries. The stable app is first."""
    bundles = (
        ("Google Chrome.app", "Google Chrome"),
        ("Google Chrome Beta.app", "Google Chrome Beta"),
        ("Google Chrome Canary.app", "Google Chrome Canary"),
    )
    roots = (Path("/Applications"), Path.home() / "Applications")
    paths: list[Path] = []
    for root in roots:
        for bundle, binary in bundles:
            paths.append(root / bundle / "Contents" / "MacOS" / binary)
    return paths


def chrome_search_paths() -> list[Path]:
    """Well-known Google Chrome binaries. This does not search PATH."""
    if sys.platform == "win32":
        return _windows_chrome_paths()
    if sys.platform == "darwin":
        return _darwin_chrome_paths()
    return [
        Path("/usr/bin/google-chrome-stable"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/local/bin/google-chrome"),
        Path("/opt/google/chrome/chrome"),
    ]


def find_chrome(explicit: str | None = None) -> str:
    if explicit:
        if not Path(explicit).is_file():
            raise LocalAttachError(f"Chrome executable not found: {explicit}")
        return explicit
    candidates: list[str] = []
    env = os.environ.get("CHROME_BIN", "").strip()
    if env:
        candidates.append(env)
    candidates.extend(str(path) for path in chrome_search_paths())
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise LocalAttachError("Google Chrome is not installed.")


def chrome_version(executable: str) -> str:
    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LocalAttachError("Could not read the Chrome version.") from exc
    line = (completed.stdout or completed.stderr or "").strip().splitlines()
    if not line or not line[0].strip():
        raise LocalAttachError("Chrome did not report a version.")
    return line[0].strip()


def fetch_version(port: int, timeout: float = 1.0) -> dict[str, Any] | None:
    url = f"http://{LOOPBACK}:{port}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            body = response.read(65536)
    except (urllib.error.URLError, TimeoutError, OSError):
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def dedicated_launch_args(
    *,
    executable: str,
    user_data_dir: Path,
    port: int,
    start_url: str,
    headless: bool = False,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Argv for a dedicated profile. The default user-data-dir is refused."""
    if is_default_user_data_dir(user_data_dir):
        raise DefaultProfileBlocked(
            "Chrome 136+ ignores remote debugging on the default user-data-dir. "
            "The dedicated launcher will not point --user-data-dir at that directory."
        )
    endpoint_for_port(port)
    args = [
        executable,
        f"--remote-debugging-port={port}",
        f"--remote-debugging-address={LOOPBACK}",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-popup-blocking",
    ]
    # No viewport and no window size. The Railway runner's 1280x800 context is a
    # different browser; forcing it here changes the page the employee sees.
    if headless:
        args.extend(["--headless=new", "--disable-gpu"])
    for item in extra_args or []:
        if "remote-debugging-address" in item and LOOPBACK not in item:
            raise LoopbackRequired(f"Refusing Chrome argument {item!r}.")
        lowered = item.replace(" ", "")
        if "0.0.0.0" in lowered or lowered.endswith("=*"):
            raise LoopbackRequired(f"Refusing Chrome argument {item!r}.")
        args.append(item)
    args.append(start_url)
    return args


@dataclass
class ChromeProcess:
    """A Chrome this driver started. ``stop`` ends that process, not an unrelated one."""

    process: subprocess.Popen[str]
    user_data_dir: Path | None
    port: int | None
    endpoint: str
    log_path: Path

    def poll(self) -> int | None:
        return self.process.poll()

    def stderr_text(self, limit: int = 8000) -> str:
        if not self.log_path.exists():
            return ""
        text = self.log_path.read_text(encoding="utf-8", errors="replace")
        if len(text) <= limit:
            return text
        return text[-limit:]

    def stop(self) -> None:
        proc = self.process
        if sys.platform == "win32":
            _stop_windows_process(proc)
            return
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                proc.terminate()
        if self.user_data_dir is not None:
            subprocess.run(
                ["pkill", "-f", f"--user-data-dir={self.user_data_dir}"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                proc.kill()
            proc.wait(timeout=5)


def _stop_windows_process(proc: subprocess.Popen[str]) -> None:
    """End the Chrome process tree this helper started. No pkill and no killpg."""
    if proc.poll() is None:
        flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
        )
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            return


def _spawn(args: list[str], log_path: Path) -> subprocess.Popen[str]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8")
    kwargs: dict[str, Any] = {
        "stdout": subprocess.DEVNULL,
        "stderr": handle,
        "text": True,
    }
    # Windows rejects start_new_session. A new process group lets taskkill /T
    # stop the dedicated Chrome without touching the employee's other windows.
    if sys.platform == "win32":
        kwargs["creationflags"] = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(args, **kwargs)
    finally:
        handle.close()


def _wait_cdp(port: int, proc: subprocess.Popen[str], timeout: float) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = fetch_version(port, timeout=0.4)
        if found is not None:
            return found
        if proc.poll() is not None:
            return None
        time.sleep(0.2)
    return fetch_version(port, timeout=0.4)


def launch_dedicated_chrome(
    *,
    user_data_dir: Path,
    port: int,
    start_url: str,
    executable: str | None = None,
    headless: bool = False,
    extra_args: list[str] | None = None,
) -> ChromeProcess:
    """Launch Chrome with a non-default profile and a loopback debugging port."""
    require_feature()
    exe = find_chrome(executable)
    user_data_dir.mkdir(parents=True, exist_ok=True)
    args = dedicated_launch_args(
        executable=exe,
        user_data_dir=user_data_dir,
        port=port,
        start_url=start_url,
        headless=headless,
        extra_args=extra_args,
    )
    log_path = user_data_dir / "chrome-stderr.log"
    proc = _spawn(args, log_path)
    return ChromeProcess(
        process=proc,
        user_data_dir=user_data_dir,
        port=port,
        endpoint=endpoint_for_port(port),
        log_path=log_path,
    )


def _pick_page(pages: list[Any], url_prefix: str) -> Any | None:
    usable: list[Any] = []
    for page in pages:
        try:
            url = page.url or ""
        except Exception:
            continue
        if url.startswith(("devtools://", "chrome-extension://")):
            continue
        usable.append(page)
    if url_prefix:
        for page in usable:
            if (page.url or "").startswith(url_prefix):
                return page
    for page in reversed(usable):
        url = page.url or ""
        if url.startswith(("http://", "https://")):
            return page
    if not usable:
        return None
    return usable[-1]


def _target_id(context: Any, page: Any) -> str:
    session = context.new_cdp_session(page)
    try:
        info = session.send("Target.getTargetInfo")
    finally:
        detach = getattr(session, "detach", None)
        if detach is not None:
            detach()
    target = info.get("targetInfo") if isinstance(info, dict) else None
    if not isinstance(target, dict):
        return ""
    return str(target.get("targetId") or "")


@dataclass
class VisibleTab:
    """The existing tab in ``contexts[0]``. No new context was created."""

    browser: Any
    context: Any
    page: Any
    endpoint: str
    target_id: str
    page_url: str
    context_count: int
    page_urls: list[str]

    def disconnect(self) -> None:
        """Drop this CDP client. Leaves the Chrome process running."""
        detach_cdp_client(self.browser)


def attach_visible_tab(playwright: Any, endpoint: str, *, url_prefix: str = "") -> VisibleTab:
    """Connect over CDP and drive the existing context. ``new_context`` is not called."""
    require_feature()
    host, port = parse_endpoint(endpoint)
    assert_loopback_listen(port)
    browser = playwright.chromium.connect_over_cdp(f"http://{host}:{port}")
    contexts = list(browser.contexts)
    if not contexts:
        raise DualSessionRefused(
            "Chrome exposed no existing context. Refusing to create one; a new context is not the visible tab."
        )
    context = contexts[0]
    pages = list(context.pages)
    chosen = _pick_page(pages, url_prefix)
    if chosen is None:
        raise LocalAttachError("The existing context has no page to drive.")
    urls: list[str] = []
    for page in pages:
        try:
            urls.append(str(page.url or ""))
        except Exception:
            urls.append("")
    return VisibleTab(
        browser=browser,
        context=context,
        page=chosen,
        endpoint=f"http://{host}:{port}",
        target_id=_target_id(context, chosen),
        page_url=str(chosen.url or ""),
        context_count=len(contexts),
        page_urls=urls,
    )


def detach_cdp_client(browser: Any) -> None:
    """Stop the Playwright CDP client without closing Chrome.

    ``connect_over_cdp`` leaves ``_should_close_connection_on_close`` false, and
    closing the browser object then asks the driver to tear that browser down.
    Idle detach only stops this client's pipe. The Chrome window stays so a
    person can review it and click Submit.
    """
    impl = getattr(browser, "_impl_obj", None)
    connection = getattr(impl, "_connection", None)
    sync = getattr(browser, "_sync", None)
    if connection is None or sync is None:
        return
    sync(connection.stop_async())


def validate_application_url(application_url: str) -> str:
    """Accept an http(s) employer or fixture URL. The CDP port is still loopback-only."""
    parsed = urlparse((application_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise LocalAttachError("The application URL must be http or https with a host.")
    if parsed.username or parsed.password:
        raise LocalAttachError("The application URL must not carry credentials.")
    return application_url.strip()


def _default_port(scheme: str) -> int | None:
    if scheme == "http":
        return 80
    if scheme == "https":
        return 443
    return None


def same_document(current: str, wanted: str) -> bool:
    """True when both URLs are the same document, ignoring the fragment."""
    left, right = urlparse(current), urlparse(wanted)
    if left.scheme != right.scheme:
        return False
    if (left.hostname or "").casefold() != (right.hostname or "").casefold():
        return False
    if (left.port or _default_port(left.scheme)) != (right.port or _default_port(right.scheme)):
        return False
    return left.path.rstrip("/") == right.path.rstrip("/")


def adopt_employer_tab(context: Any, application_url: str) -> Any:
    """Use the tab already on this URL, or navigate an existing tab in ``contexts[0]``.

    This does not create a browser context, does not set a viewport, and does
    not install request blocking.
    """
    validate_application_url(application_url)
    pages = list(getattr(context, "pages", []) or [])
    for page in pages:
        try:
            current = str(page.url or "")
        except Exception:
            continue
        if same_document(current, application_url):
            return page
    chosen = _pick_page(pages, "")
    if chosen is None and pages:
        chosen = pages[0]
    if chosen is None:
        raise DualSessionRefused("The existing context has no tab. Refusing to create a context.")
    chosen.goto(application_url, wait_until="domcontentloaded", timeout=45000)
    return chosen


def apply_visible_answer(page: Any, target: dict[str, Any]) -> dict[str, Any]:
    """Commit one value on the open page and report whether it stuck.

    A location field uses the suggestion commit. Every other field uses the
    same visible commit as the hosted runner. Neither path clicks Final Submit.
    ``verified`` is true only when that commit check passed. The value is not
    copied into the result.
    """
    require_feature()
    from autofill.dom_ui import commit_visible, control_committed
    from autofill.stepfill import commit_location_answer, is_location_answer

    if is_location_answer(page, target):
        report = commit_location_answer(page, target)
    else:
        report = commit_visible(page, target)
        selector = str(target.get("selector") or "")
        if report.get("filled") and selector and not control_committed(page, selector):
            report = {"filled": False, "reason": "not_committed"}
    if _optional_submitted(page) is True:
        raise LocalAttachError("The page reported a submit. The local driver does not submit applications.")
    if report.get("reason") == "needs_option":
        return {
            "filled": False,
            "verified": False,
            "reason": "needs_option",
            "options": [str(option) for option in report.get("options") or []],
        }
    verified = bool(report.get("filled"))
    if "verified" in report:
        verified = bool(report.get("verified")) and bool(report.get("filled"))
    body: dict[str, Any] = {"filled": verified, "verified": verified}
    if not verified:
        body["reason"] = str(report.get("reason") or "not_committed")
        options = [str(option) for option in report.get("options") or [] if str(option).strip()]
        if options:
            body["options"] = options
    return body


def _optional_submitted(page: Any) -> bool | None:
    try:
        value = page.evaluate("() => window.__submitted")
    except Exception:
        return None
    if isinstance(value, bool):
        return value
    return None


class FillCancelled(BaseException):
    """A newer Apply replaced this fill, or the employee cancelled it.

    ``BaseException`` on purpose: the engine catches ``Exception`` around
    field writes, and a cancel must not be swallowed there and keep typing
    the previous candidate. ``finally`` blocks still run.
    """


class CancelGate(PauseGate):
    """Field-boundary pause gate that can also cancel one fill.

    The engine calls ``wait`` before every field (``dom_ui.checkpoint``) and
    ``check`` between pages and before Next/entry clicks. After ``cancel``
    both raise ``FillCancelled``; a paused wait wakes up and raises too.
    """

    def __init__(self) -> None:
        super().__init__()
        self._cancelled = threading.Event()
        self.reason = ""

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def cancel(self, reason: str = "cancelled") -> None:
        if not self._cancelled.is_set():
            self.reason = reason
            self._cancelled.set()
        self.resume()

    def check(self) -> None:
        if self._cancelled.is_set():
            raise FillCancelled(self.reason or "cancelled")

    def wait(self) -> None:
        self.check()
        super().wait()
        self.check()


def fill_visible_tab(
    page: Any,
    application_url: str,
    profile: CandidateProfile,
    *,
    cancel: CancelGate | None = None,
) -> ApplicationResult:
    """Fill the already-open tab with the existing engine. The page is not replaced.

    The page budget matches the hosted runner. A cap of one page filled the
    PoC fixture and then left the next employer page untouched. Final Submit
    stays refused inside the engine. ``cancel`` stops the fill at the next
    field or page boundary by raising ``FillCancelled``.
    """
    require_feature()
    validate_application_url(application_url)
    if cancel is not None:
        cancel.check()
    return autofill_application(
        application_url,
        profile,
        options=AutofillOptions(
            page=page,
            headless=False,
            max_pages=8,
            pause_gate=cancel,
            cancel_check=cancel.check if cancel is not None else None,
        ),
    )


class DedicatedApplySession:
    """One dedicated profile. Call it from a single thread.

    The separate window is intentional. Chrome 136+ ignores remote debugging
    on the default user-data-dir, so the employee's daily browser is not a
    connected session. This object launches ``--user-data-dir`` at the TileArc
    apply profile and drives that window over loopback CDP.

    ``detach`` drops the CDP client and leaves Chrome running. ``stop_launched_chrome``
    is the only way this object kills a process, and only a process it launched.
    """

    def __init__(
        self,
        *,
        user_data_dir: Path,
        debug_port: int | None = None,
        headless: bool = False,
        executable: str | None = None,
    ) -> None:
        self.user_data_dir = user_data_dir
        self.debug_port = free_port() if debug_port is None else debug_port
        self.headless = headless
        self.executable = executable
        self.endpoint = endpoint_for_port(self.debug_port)
        self.chrome: ChromeProcess | None = None
        self.owns_chrome = False
        self.tab: VisibleTab | None = None
        self._playwright: Any = None
        self._page_mark: Any = None
        self._held: set[str] = set()

    def ensure(self, application_url: str) -> VisibleTab:
        """Launch the dedicated profile if needed, then adopt the employer tab."""
        require_feature()
        validate_application_url(application_url)
        if is_default_user_data_dir(self.user_data_dir):
            raise DefaultProfileBlocked(
                "The dedicated Apply profile cannot be Chrome's default user-data-dir."
            )
        if self.tab is not None:
            page = adopt_employer_tab(self.tab.context, application_url)
            self.tab.page = page
            self.tab.page_url = str(page.url or "")
            self.tab.target_id = _target_id(self.tab.context, page)
            return self.tab
        live = fetch_version(self.debug_port, timeout=0.4)
        if live is None:
            self.user_data_dir.mkdir(parents=True, exist_ok=True)
            self.chrome = launch_dedicated_chrome(
                user_data_dir=self.user_data_dir,
                port=self.debug_port,
                start_url=application_url,
                executable=self.executable,
                headless=self.headless,
            )
            self.owns_chrome = True
            opened = _wait_cdp(self.debug_port, self.chrome.process, 20)
            if opened is None:
                detail = self.chrome.stderr_text()[:500]
                self.stop_launched_chrome()
                raise LocalAttachError(f"Dedicated Chrome did not open CDP. {detail}")
        elif self.chrome is None:
            self.owns_chrome = False
        # This check used to run outside the cleanup below. On Windows and
        # macOS it raised "Nothing is listening" (no /proc/net/tcp) after
        # Chrome had opened the employer URL, and the window was left idle.
        try:
            assert_loopback_listen(self.debug_port)
            from playwright.sync_api import sync_playwright

            self._playwright = sync_playwright().start()
            tab = attach_visible_tab(self._playwright, self.endpoint, url_prefix=application_url)
            page = adopt_employer_tab(tab.context, application_url)
            tab.page = page
            tab.page_url = str(page.url or "")
            tab.target_id = _target_id(tab.context, page)
        except BaseException:
            self.detach()
            if self.owns_chrome:
                self.stop_launched_chrome()
            raise
        self.tab = tab
        return tab

    def fill(
        self,
        application_url: str,
        profile: CandidateProfile,
        *,
        cancel: CancelGate | None = None,
        fresh: bool = False,
    ) -> tuple[ApplicationResult, bool | None]:
        """Fill the adopted tab. A page that reports ``__submitted`` is a failure.

        ``fresh`` reloads the employer URL first and forgets held selectors.
        The helper sets it when an earlier Apply already wrote into this
        window, so the previous candidate's values are not kept: the engine
        does not overwrite a field that already holds a value.
        """
        if cancel is not None:
            cancel.check()
        tab = self.ensure(application_url)
        if fresh:
            self._held.clear()
            self._page_mark = None
            tab.page.goto(application_url, wait_until="domcontentloaded", timeout=45000)
            tab.page_url = str(tab.page.url or "")
            if cancel is not None:
                cancel.check()
        result = fill_visible_tab(tab.page, application_url, profile, cancel=cancel)
        submitted = _optional_submitted(tab.page)
        if submitted is True:
            raise LocalAttachError("The page reported a submit. The local driver does not submit applications.")
        self._remember_mark(tab.page)
        return result, submitted

    def apply_answer(
        self,
        application_url: str,
        profile: CandidateProfile,
        target: dict[str, Any],
        *,
        cancel: CancelGate | None = None,
    ) -> dict[str, Any]:
        """Write one employee answer on the open tab and read what is still empty.

        ``verified`` is true only when the control holds the value afterwards.
        Callers persist confirmedByEmployee only in that case. Final Submit is
        not clicked. The answer text is not returned.
        """
        require_feature()
        if cancel is not None:
            cancel.check()
        page = self._page_for_answer(application_url)
        report = apply_visible_answer(page, target)
        if not report.get("verified"):
            return report
        selector = str(target.get("selector") or "")
        if selector:
            self._held.add(selector)
        followed = self._follow_answer(page, application_url, profile, cancel=cancel)
        followed["verified"] = True
        followed["filled"] = True
        return followed

    def _page_for_answer(self, application_url: str) -> Any:
        """Use the tab fill already adopted. Reconnect only after a detach."""
        if self.tab is not None and self._playwright is not None:
            return self.tab.page
        return self.ensure(application_url).page

    def _remember_mark(self, page: Any) -> None:
        try:
            from autofill.follow import read_page_mark

            self._page_mark, _snapshot = read_page_mark(page)
        except Exception:
            self._page_mark = None

    def _follow_answer(
        self,
        page: Any,
        application_url: str,
        profile: CandidateProfile,
        *,
        cancel: CancelGate | None = None,
    ) -> dict[str, Any]:
        """The next empty required field, or a full fill when the page changed."""
        from autofill.follow import follow_answer

        follow = follow_answer(
            page,
            profile=profile,
            job=None,
            held=set(self._held),
            prior=[],
            previous=self._page_mark,
        )
        if _optional_submitted(page) is True:
            raise LocalAttachError("The page reported a submit. The local driver does not submit applications.")
        if follow.changed:
            if cancel is not None:
                cancel.check()
            result = fill_visible_tab(page, application_url, profile, cancel=cancel)
            if _optional_submitted(page) is True:
                raise LocalAttachError("The page reported a submit. The local driver does not submit applications.")
            self._remember_mark(page)
            return {
                "status": result.status,
                "open_items": list(result.open_items),
                "blocking": result.blocking,
                "result": result,
            }
        if follow.mark is not None:
            self._page_mark = follow.mark
        return {
            "status": follow.status,
            "open_items": list(follow.open_items),
            "blocking": follow.blocking,
        }

    def detach(self) -> None:
        """Idle detach. Chrome keeps running whether or not this process launched it.

        Stops the Playwright driver. Does not close the browser object, because
        that close is how a CDP client asks Chrome to exit.
        """
        self.tab = None
        playwright = self._playwright
        self._playwright = None
        if playwright is None:
            return
        try:
            playwright.stop()
        except Exception:
            return

    def stop_launched_chrome(self) -> None:
        """Kill Chrome only when this object launched it. Detach does not call this."""
        self.detach()
        chrome = self.chrome
        self.chrome = None
        if self.owns_chrome and chrome is not None:
            chrome.stop()
        self.owns_chrome = False

    def browser_running(self) -> bool:
        if self.owns_chrome and self.chrome is not None:
            return self.chrome.poll() is None
        return fetch_version(self.debug_port, timeout=0.4) is not None


def describe_control(page: Any, selector: str) -> tuple[str, str]:
    raw = page.evaluate(_CONTROL_JS, selector)
    if not isinstance(raw, dict):
        raise LocalAttachError(f"No control matches {selector!r}.")
    return str(raw.get("name") or ""), str(raw.get("controlType") or "")


def drive_control(page: Any, selector: str, claimed_name: str, *, purpose: str) -> None:
    """Only click entry. A final Submit is refused from the DOM label, not the caller's label.

    Script, form, and keyboard submit purposes are refused before any input is sent.
    """
    if not HUMAN_SUBMIT_ONLY:
        raise HumanSubmissionRequired("HUMAN_SUBMIT_ONLY was disabled. That is not a supported configuration.")
    if purpose in _SCRIPT_PURPOSES:
        raise HumanSubmissionRequired(
            f"Refusing {purpose} on {selector!r}. The local driver has no script, form, or keyboard submit path."
        )
    actual_name, actual_type = describe_control(page, selector)
    for label in (actual_name, claimed_name):
        if action_class(label, control_type=actual_type) == ActionClass.FINAL_APPLICATION_SUBMIT_FORBIDDEN:
            shown = actual_name or claimed_name
            raise HumanSubmissionRequired(
                f"Refusing to activate {shown!r}. A person must click the final Submit or Apply control."
            )
    perform_click(
        page,
        selector,
        actual_name or claimed_name,
        control_type=actual_type,
        purpose=purpose,
    )


def attempt_alternate_submits(page: Any, attempts: list[tuple[str, str, str]]) -> list[str]:
    """Try each ``(selector, claimed_name, purpose)``. Every one must be refused."""
    refusals: list[str] = []
    for selector, claimed_name, purpose in attempts:
        try:
            drive_control(page, selector, claimed_name, purpose=purpose)
        except (HumanSubmissionRequired, SubmitBlockedError) as exc:
            refusals.append(f"{purpose} {selector}: {exc}")
            continue
        raise LocalAttachError(
            f"Submit path was not refused: selector={selector!r} name={claimed_name!r} purpose={purpose!r}."
        )
    return refusals


def _devtools_active_port(user_data_dir: Path | None) -> str:
    if user_data_dir is None:
        return ""
    path = user_data_dir / "DevToolsActivePort"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8", errors="replace").strip()


def _probe_note(mode: str, *, cdp_open: bool, stderr_text: str) -> tuple[bool, str]:
    blocked = _DAILY_BLOCK_TEXT in stderr_text
    if mode in {"headed_daily", "explicit_default"}:
        if cdp_open:
            return True, "CDP opened on the default profile. The Chrome 136+ block did not apply to this binary."
        detail = "Chrome logged that a non-default data directory is required." if blocked else "The port did not open."
        return False, f"Daily-profile debugging was not available. {detail}"
    if mode == "headless_omitted":
        if cdp_open:
            return False, (
                "Headless Chrome without --user-data-dir opened CDP. Headless uses a temporary data directory, "
                "not the employee's daily profile. This is not silent attach to an already-open daily Chrome."
            )
        return False, "Headless Chrome without --user-data-dir did not open CDP in this run."
    if mode == "dedicated":
        if cdp_open:
            return False, "A non-default --user-data-dir accepted the debugging port."
        return False, "The dedicated profile did not open CDP."
    raise LocalAttachError(f"Unknown probe mode {mode!r}.")


def probe_debug_port(
    mode: str,
    *,
    executable: str | None = None,
    timeout: float = 6.0,
    work_dir: Path | None = None,
) -> dict[str, Any]:
    """Launch Chrome and report whether ``/json/version`` answers.

    ``headed_daily`` omits ``--user-data-dir`` so Chrome uses the real default profile.
    ``explicit_default`` passes that same directory. ``headless_omitted`` is the false
    positive: headless without a data dir is not the daily profile. ``dedicated`` uses
    a fresh directory.
    """
    require_feature()
    if mode not in {"headed_daily", "explicit_default", "headless_omitted", "dedicated"}:
        raise LocalAttachError(f"Unknown probe mode {mode!r}.")
    exe = find_chrome(executable)
    version = chrome_version(exe)
    port = free_port()
    headless = mode != "headed_daily"
    user_data: Path | None
    if mode == "dedicated":
        root = work_dir or Path(tempfile.mkdtemp(prefix="t172-probe-"))
        user_data = root / "dedicated-profile"
        user_data.mkdir(parents=True, exist_ok=True)
    elif mode == "explicit_default":
        user_data = stable_chrome_user_data_dir()
    else:
        user_data = None
    args = [exe, f"--remote-debugging-port={port}", f"--remote-debugging-address={LOOPBACK}"]
    if user_data is not None:
        args.append(f"--user-data-dir={user_data}")
    args.extend(
        [
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--disable-gpu",
            "--window-size=900,700",
        ]
    )
    if headless:
        args.append("--headless=new")
    args.append("about:blank")
    if user_data is not None and mode != "explicit_default":
        log_parent = user_data
    else:
        log_parent = Path(tempfile.mkdtemp(prefix="t172-log-"))
    log_path = log_parent / f"{mode}.log"
    proc = _spawn(args, log_path)
    chrome = ChromeProcess(
        process=proc,
        user_data_dir=user_data if mode != "headed_daily" else stable_chrome_user_data_dir(),
        port=port,
        endpoint=endpoint_for_port(port),
        log_path=log_path,
    )
    # headed_daily's stop() must be allowed to signal the default-profile process.
    if mode == "headed_daily":
        chrome.user_data_dir = None
    version_payload: dict[str, Any] | None = None
    listen: list[str] = []
    try:
        version_payload = _wait_cdp(port, proc, timeout)
        if version_payload is not None:
            listen = assert_loopback_listen(port)
            socket_url = str(version_payload.get("webSocketDebuggerUrl") or "")
            if socket_url and not socket_url.startswith(f"ws://{LOOPBACK}:"):
                raise LoopbackRequired(f"Chrome advertised a non-loopback websocket: {socket_url}")
    finally:
        stderr_text = chrome.stderr_text()
        active = _devtools_active_port(user_data if mode != "headed_daily" else stable_chrome_user_data_dir())
        chrome.stop()
    cdp_open = version_payload is not None
    daily_attach, note = _probe_note(mode, cdp_open=cdp_open, stderr_text=stderr_text)
    browser_field = ""
    websocket_url = ""
    if version_payload is not None:
        browser_field = str(version_payload.get("Browser") or "")
        websocket_url = str(version_payload.get("webSocketDebuggerUrl") or "")
    logger.info("probe mode=%s cdp_open=%s daily_attach=%s", mode, cdp_open, daily_attach)
    return {
        "mode": mode,
        "chrome_version": version,
        "executable": exe,
        "headless": headless,
        "user_data_dir": "" if user_data is None else str(user_data),
        "default_profile": mode != "dedicated",
        "port": port,
        "cdp_open": cdp_open,
        "listen_hosts": listen,
        "browser": browser_field,
        "webSocketDebuggerUrl": websocket_url,
        "devtools_active_port": active,
        "stderr_excerpt": _excerpt(stderr_text),
        "daily_profile_attach": daily_attach,
        "note": note,
    }


def _excerpt(text: str, limit: int = 500) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    interesting = [line for line in lines if "DevTools" in line or "load-extension" in line or "user-data" in line]
    chosen = interesting or lines[-4:]
    excerpt = " | ".join(chosen)
    return excerpt[:limit]


class _FixtureServer(ThreadingHTTPServer):
    html: bytes
    evidence: list[dict[str, Any]]


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = urlparse(self.path).path
        server = self.server
        if path != "/t172-local-tab.html" or not isinstance(server, _FixtureServer):
            self.send_error(404)
            return
        body = server.html
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        server = self.server
        if path != "/extension-evidence" or not isinstance(server, _FixtureServer):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or "0")
        if length < 0 or length > 65536:
            self.send_error(413)
            return
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            parsed = {"unparsed": True}
        if isinstance(parsed, dict) and len(server.evidence) < 20:
            server.evidence.append(parsed)
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: Any) -> None:
        return


def start_fixture_server(html: bytes) -> _FixtureServer:
    server = _FixtureServer((LOOPBACK, 0), _FixtureHandler)
    server.html = html
    server.evidence = []
    threading.Thread(target=server.serve_forever, name="t172-fixture", daemon=True).start()
    return server


def _cookie_value(page: Any) -> str:
    value = page.evaluate("() => document.cookie")
    return value if isinstance(value, str) else ""


def _field_values(page: Any) -> dict[str, str]:
    values: dict[str, str] = {}
    for field_id in ("first", "last", "email"):
        try:
            values[field_id] = str(page.locator(f"#{field_id}").input_value())
        except Exception:
            values[field_id] = ""
    return values


def _result_brief(result: ApplicationResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "fields_detected": result.fields_detected,
        "fields_filled": result.fields_filled,
        "fields_skipped": result.fields_skipped,
        "stopped_before_submit": result.stopped_before_submit,
        "submit_controls": list(result.submit_controls),
    }


def read_inspect_surface(page: Any) -> dict[str, Any]:
    """Open ``chrome://inspect/#remote-debugging`` in a tab this driver already controls."""
    try:
        page.goto("chrome://inspect/#remote-debugging", wait_until="domcontentloaded", timeout=10000)
    except Exception as exc:
        return {"opened": False, "error": type(exc).__name__}
    try:
        text = page.evaluate("() => document.body ? document.body.innerText.slice(0, 2000) : ''")
    except Exception as exc:
        return {"opened": True, "url": str(page.url or ""), "error": type(exc).__name__}
    return {"opened": True, "url": str(page.url or ""), "text": text if isinstance(text, str) else ""}


def demonstrate_dedicated_fill(
    *,
    fixture_html: Path,
    profile_path: Path,
    work_dir: Path,
    screenshot_path: Path,
    headless: bool = True,
    executable: str | None = None,
) -> dict[str, Any]:
    """Open a local form in a dedicated Chrome profile and fill that same tab.

    The hosted service is not involved. A second browser context is not created
    for the fill. Reconnecting must observe the same target id and cookie.
    """
    require_feature()
    html = fixture_html.read_bytes()
    server = start_fixture_server(html)
    port = int(server.server_address[1])
    form_url = f"http://{LOOPBACK}:{port}/t172-local-tab.html"
    user_data = work_dir / "chrome-profile"
    debug_port = free_port()
    chrome = launch_dedicated_chrome(
        user_data_dir=user_data,
        port=debug_port,
        start_url=form_url,
        executable=executable,
        headless=headless,
    )
    from playwright.sync_api import sync_playwright

    try:
        version_payload = _wait_cdp(debug_port, chrome.process, 20)
        if version_payload is None:
            raise LocalAttachError(f"Dedicated Chrome did not open CDP. {chrome.stderr_text()[:500]}")
        listen = assert_loopback_listen(debug_port)
        socket_url = str(version_payload.get("webSocketDebuggerUrl") or "")
        if not socket_url.startswith(f"ws://{LOOPBACK}:"):
            raise LoopbackRequired(f"Chrome advertised a non-loopback websocket: {socket_url}")
        playwright = sync_playwright().start()
        try:
            first = attach_visible_tab(playwright, chrome.endpoint, url_prefix=form_url)
            first_target = first.target_id
            first_cookie = _cookie_value(first.page)
            first_context_count = first.context_count
        finally:
            playwright.stop()
        alive = chrome.poll() is None
        playwright = sync_playwright().start()
        try:
            second = attach_visible_tab(playwright, chrome.endpoint, url_prefix=form_url)
            profile = CandidateProfile.load(profile_path)
            result = fill_visible_tab(second.page, form_url, profile)
            values = _field_values(second.page)
            submitted = bool(second.page.evaluate("() => window.__submitted"))
            cookie_after = _cookie_value(second.page)
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            second.page.screenshot(path=str(screenshot_path), full_page=True)
            inspect_page = second.context.new_page()
            inspect = read_inspect_surface(inspect_page)
        finally:
            playwright.stop()
        return {
            "mode": "dedicated_fill",
            "chrome_version": chrome_version(find_chrome(executable)),
            "headless": headless,
            "endpoint": chrome.endpoint,
            "listen_hosts": listen,
            "browser": str(version_payload.get("Browser") or ""),
            "webSocketDebuggerUrl_host_ok": socket_url.startswith(f"ws://{LOOPBACK}:"),
            "user_data_dir_is_default": is_default_user_data_dir(user_data),
            "created_new_context_for_fill": False,
            "context_count": first_context_count,
            "target_id": first_target,
            "target_id_after_reconnect": second.target_id,
            "same_target": bool(first_target) and first_target == second.target_id,
            "chrome_alive_after_disconnect": alive,
            "cookie_before": first_cookie,
            "cookie_after_fill": cookie_after,
            "cookie_preserved": "t172_visible=same-tab" in first_cookie and "t172_visible=same-tab" in cookie_after,
            "values": values,
            "submitted": submitted,
            "fill": _result_brief(result),
            "screenshot": str(screenshot_path),
            "inspect_surface": inspect,
        }
    finally:
        chrome.stop()
        server.shutdown()


def probe_branded_load_extension(
    extension_dir: Path,
    *,
    executable: str | None = None,
    timeout: float = 4.0,
) -> dict[str, Any]:
    """Record whether branded Chrome honors ``--load-extension``. It should not."""
    require_feature()
    exe = find_chrome(executable)
    version = chrome_version(exe)
    work = Path(tempfile.mkdtemp(prefix="t172-brand-ext-"))
    user_data = work / "profile"
    log_path = work / "chrome.log"
    args = [
        exe,
        f"--user-data-dir={user_data}",
        f"--load-extension={extension_dir}",
        "--disable-features=DisableLoadExtensionCommandLineSwitch",
        "--enable-logging=stderr",
        "--no-first-run",
        "--no-default-browser-check",
        "--headless=new",
        "--disable-gpu",
        "about:blank",
    ]
    proc = _spawn(args, log_path)
    chrome = ChromeProcess(process=proc, user_data_dir=user_data, port=None, endpoint="", log_path=log_path)
    try:
        time.sleep(timeout)
        stderr_text = chrome.stderr_text()
    finally:
        chrome.stop()
    ignored = "--load-extension is not allowed in Google Chrome" in stderr_text
    return {
        "mode": "branded_load_extension",
        "chrome_version": version,
        "executable": exe,
        "load_extension_ignored": ignored,
        "stderr_excerpt": _excerpt(stderr_text),
        "note": (
            "Branded Google Chrome ignored --load-extension. An employee must install an unpacked "
            "extension from chrome://extensions. That is a consent step, not a silent attach."
            if ignored
            else "Branded Chrome did not log the load-extension refusal. See the excerpt."
        ),
    }


def probe_extension_debugger(
    extension_dir: Path,
    *,
    executable: str,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Load the probe extension without a remote-debugging port and read the tab cookie.

    ``executable`` is Chrome for Testing when branded Chrome refuses ``--load-extension``.
    The extension reports to the fixture origin on 127.0.0.1 and does not receive a service token.
    """
    require_feature()
    version = chrome_version(executable)
    html_path = extension_dir.parent / "fixture" / "application.html"
    if not html_path.is_file():
        raise LocalAttachError(f"Missing fixture next to the extension: {html_path}")
    server = start_fixture_server(html_path.read_bytes())
    port = int(server.server_address[1])
    form_url = f"http://{LOOPBACK}:{port}/t172-local-tab.html"
    work = Path(tempfile.mkdtemp(prefix="t172-ext-"))
    user_data = work / "profile"
    log_path = work / "chrome.log"
    args = [
        executable,
        f"--user-data-dir={user_data}",
        f"--load-extension={extension_dir}",
        f"--disable-extensions-except={extension_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--headless=new",
        form_url,
    ]
    if "--remote-debugging-port" in " ".join(args):
        raise LoopbackRequired("The extension probe must not open a remote debugging port.")
    proc = _spawn(args, log_path)
    chrome = ChromeProcess(process=proc, user_data_dir=user_data, port=None, endpoint="", log_path=log_path)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not server.evidence:
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        evidence = list(server.evidence)
        stderr_text = chrome.stderr_text()
    finally:
        chrome.stop()
        server.shutdown()
    attached = next((item for item in evidence if item.get("attached") is True), None)
    return {
        "mode": "extension_debugger",
        "chrome_version": version,
        "executable": executable,
        "remote_debugging_port": False,
        "reports": evidence,
        "attached": attached is not None,
        "cookie": "" if attached is None else str(attached.get("cookie") or ""),
        "cookie_visible": attached is not None and "t172_visible=same-tab" in str(attached.get("cookie") or ""),
        "stderr_excerpt": _excerpt(stderr_text),
        "note": (
            "chrome.debugger attached inside the browser and read the visible tab's cookie. "
            "No remote-debugging port was passed. Branded Chrome still requires a manual install."
            if attached is not None
            else "The extension did not report an attach. See reports and the Chrome log excerpt."
        ),
    }


def probe_inspect_consent_cold(
    *,
    executable: str | None = None,
    timeout: float = 4.0,
) -> dict[str, Any]:
    """Open the inspect page without a debugging flag. Consent is not granted from here.

    A person has to enable remote debugging and allow the connection. This probe
    only records that the port file stays absent when nobody does that.
    """
    require_feature()
    exe = find_chrome(executable)
    version = chrome_version(exe)
    work = Path(tempfile.mkdtemp(prefix="t172-inspect-"))
    user_data = work / "profile"
    log_path = work / "chrome.log"
    args = [
        exe,
        f"--user-data-dir={user_data}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-gpu",
        "--headless=new",
        "chrome://inspect/#remote-debugging",
    ]
    proc = _spawn(args, log_path)
    chrome = ChromeProcess(process=proc, user_data_dir=user_data, port=None, endpoint="", log_path=log_path)
    try:
        time.sleep(timeout)
        active = _devtools_active_port(user_data)
        stderr_text = chrome.stderr_text()
    finally:
        chrome.stop()
    return {
        "mode": "inspect_consent_cold",
        "chrome_version": version,
        "devtools_active_port_present": bool(active),
        "devtools_active_port": active,
        "stderr_excerpt": _excerpt(stderr_text),
        "note": (
            "Chrome was opened on chrome://inspect/#remote-debugging with no debugging flag and no Allow click. "
            "DevToolsActivePort was not written, so nothing could attach. Chrome 144+ still requires the person "
            "to enable remote debugging and approve each connection."
            if not active
            else "DevToolsActivePort appeared without an Allow click. Inspect the file before treating that as consent."
        ),
    }


# Kept so a caller can see every probe the PoC knows about.
PROBE_MODES = ("headed_daily", "explicit_default", "headless_omitted", "dedicated")


@dataclass
class EvidenceBundle:
    """JSON-serializable results from one PoC run."""

    probes: list[dict[str, Any]] = field(default_factory=list)
    dedicated_fill: dict[str, Any] | None = None
    branded_extension: dict[str, Any] | None = None
    extension_debugger: dict[str, Any] | None = None
    inspect_consent: dict[str, Any] | None = None
