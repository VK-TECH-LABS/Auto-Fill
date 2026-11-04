#!/usr/bin/env python3
"""Make the frozen helper's native dependencies universal2 before PyInstaller runs.

Playwright publishes three macOS wheels. The arm64 wheel and the x86_64 wheel
each contain a thin ``playwright/driver/node``. The wheel tagged
``macosx_11_0_universal2`` is the x86_64 node again (same bytes, different
filename). PyInstaller ``target_arch="universal2"`` keeps a fat Mach-O and
rejects a thin extension, so this step:

- reinstalls the greenlet ``macosx_11_0_universal2`` wheel (that one is fat)
- downloads the arm64 and x86_64 Playwright wheels, never the universal2 tag
- ``lipo -create`` those two ``node`` binaries over the installed driver

Chrome, Chromium, and ChromeDriver are not part of this package. The helper
talks to the employee's Google Chrome over CDP.
"""

from __future__ import annotations

import importlib.metadata
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PLAYWRIGHT_ARM64_PLATFORM = "macosx_11_0_arm64"
PLAYWRIGHT_X86_64_PLATFORM = "macosx_10_13_x86_64"
PLAYWRIGHT_SLICES = (PLAYWRIGHT_ARM64_PLATFORM, PLAYWRIGHT_X86_64_PLATFORM)
GREENLET_PLATFORM = "macosx_11_0_universal2"


def slice_for_playwright_wheel(filename: str) -> str | None:
    """Return ``arm64`` or ``x86_64`` for a thin Playwright macOS wheel.

    The PyPI ``macosx_11_0_universal2`` Playwright wheel is rejected. Its node
    binary is x86_64-only.
    """
    name = Path(filename).name
    if not name.startswith("playwright-") or not name.endswith(".whl"):
        return None
    if "universal2" in name:
        return None
    if PLAYWRIGHT_ARM64_PLATFORM in name:
        return "arm64"
    if PLAYWRIGHT_X86_64_PLATFORM in name:
        return "x86_64"
    return None


def playwright_download_args(version: str, platform: str, python_version: tuple[int, int]) -> list[str]:
    if platform not in PLAYWRIGHT_SLICES:
        raise ValueError(
            f"refusing Playwright platform {platform}; use {PLAYWRIGHT_ARM64_PLATFORM} and {PLAYWRIGHT_X86_64_PLATFORM}"
        )
    major, minor = python_version
    return [
        f"playwright=={version}",
        "--only-binary=:all:",
        "--platform",
        platform,
        "--python-version",
        f"{major}.{minor}",
        "--implementation",
        "py",
        "--abi",
        "none",
        "--no-deps",
    ]


def greenlet_download_args(version: str, python_version: tuple[int, int]) -> list[str]:
    major, minor = python_version
    abi = f"cp{major}{minor}"
    return [
        f"greenlet=={version}",
        "--only-binary=:all:",
        "--platform",
        GREENLET_PLATFORM,
        "--python-version",
        f"{major}.{minor}",
        "--implementation",
        "cp",
        "--abi",
        abi,
        "--no-deps",
    ]


def _pip_download(args: list[str], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.check_call([sys.executable, "-m", "pip", "download", *args, "-d", str(dest)])


def _one_wheel(dest: Path, prefix: str) -> Path:
    wheels = sorted(dest.glob(f"{prefix}-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected one {prefix} wheel in {dest}, found {[path.name for path in wheels]}")
    return wheels[0]


def _extract_node(wheel: Path, dest: Path) -> Path:
    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist() if name.endswith("playwright/driver/node")]
        if len(names) != 1:
            raise SystemExit(f"{wheel.name} does not contain one playwright/driver/node ({names})")
        dest.write_bytes(archive.read(names[0]))
    dest.chmod(dest.stat().st_mode | stat.S_IEXEC)
    return dest


def _lipo_archs(path: Path) -> set[str]:
    completed = subprocess.run(
        ["lipo", "-archs", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise SystemExit(f"lipo -archs failed for {path}: {completed.stderr.strip() or completed.stdout.strip()}")
    return set(completed.stdout.split())


def _require_archs(path: Path, expected: set[str], label: str) -> None:
    archs = _lipo_archs(path)
    if archs != expected:
        raise SystemExit(f"{label} architectures are {sorted(archs)}, expected {sorted(expected)}")


def _strip_signature(path: Path) -> None:
    subprocess.run(["codesign", "--remove-signature", str(path)], check=False, capture_output=True)


def ensure_greenlet_universal(work: Path) -> None:
    version = importlib.metadata.version("greenlet")
    dest = work / "greenlet"
    _pip_download(greenlet_download_args(version, sys.version_info[:2]), dest)
    wheel = _one_wheel(dest, "greenlet")
    if "universal2" not in wheel.name:
        raise SystemExit(f"greenlet wheel {wheel.name} is not universal2")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "--force-reinstall", "--no-deps", str(wheel)])
    import greenlet

    root = Path(greenlet.__file__).resolve().parent
    matches = sorted(root.glob("_greenlet*.so"))
    if len(matches) != 1:
        raise SystemExit(f"expected one greenlet extension in {root}, found {matches}")
    _require_archs(matches[0], {"x86_64", "arm64"}, "greenlet")
    print(f"greenlet {version} is universal2 ({matches[0]})")


def install_universal_playwright_node(work: Path) -> None:
    version = importlib.metadata.version("playwright")
    nodes: dict[str, Path] = {}
    for platform, arch in (
        (PLAYWRIGHT_ARM64_PLATFORM, "arm64"),
        (PLAYWRIGHT_X86_64_PLATFORM, "x86_64"),
    ):
        dest = work / arch
        _pip_download(playwright_download_args(version, platform, sys.version_info[:2]), dest)
        wheel = _one_wheel(dest, "playwright")
        found = slice_for_playwright_wheel(wheel.name)
        if found != arch:
            raise SystemExit(f"refusing {wheel.name}; wanted the {arch} Playwright wheel")
        node = _extract_node(wheel, work / f"node-{arch}")
        _strip_signature(node)
        _require_archs(node, {arch}, f"Playwright node {arch}")
        nodes[arch] = node

    import playwright

    installed = Path(playwright.__file__).resolve().parent / "driver" / "node"
    if not installed.is_file():
        raise SystemExit(f"installed Playwright node is missing: {installed}")
    merged = work / "node-universal"
    subprocess.check_call(
        ["lipo", "-create", str(nodes["arm64"]), str(nodes["x86_64"]), "-output", str(merged)]
    )
    _require_archs(merged, {"x86_64", "arm64"}, "merged Playwright node")
    installed.chmod(installed.stat().st_mode | stat.S_IWRITE | stat.S_IREAD)
    shutil.copyfile(merged, installed)
    installed.chmod(0o755)
    _require_archs(installed, {"x86_64", "arm64"}, "installed Playwright node")
    print(f"Playwright {version} node is universal2 ({installed})")


def main() -> int:
    if sys.platform != "darwin":
        print("universal2 node merge runs on macOS", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory(prefix="tilearc-universal-") as tmp:
        work = Path(tmp)
        ensure_greenlet_universal(work)
        install_universal_playwright_node(work)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
