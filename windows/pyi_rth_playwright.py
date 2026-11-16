"""PyInstaller runtime hook. Runs before the helper so CDP can find Node."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _roots() -> list[Path]:
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    executable = Path(sys.executable).resolve()
    roots.append(executable.parent)
    roots.append(executable.parent / "_internal")
    # macOS .app: binaries live in Contents/Frameworks and data copies in Contents/Resources.
    # The executable itself is Contents/MacOS/<name>, so those directories are siblings.
    if executable.parent.name == "MacOS":
        contents = executable.parent.parent
        if contents.name == "Contents":
            roots.append(contents / "Frameworks")
            roots.append(contents / "Resources")
    return roots


def _rank(path: Path) -> tuple[int, int]:
    parts = path.parts
    if "Frameworks" in parts or "MacOS" in parts:
        return (0, len(parts))
    if "Resources" in parts:
        return (2, len(parts))
    return (1, len(parts))


def _node() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    name = "node.exe" if sys.platform == "win32" else "node"
    found: list[Path] = []
    for root in _roots():
        direct = root / "playwright" / "driver" / name
        if direct.is_file():
            return direct
        try:
            found.extend(item for item in root.rglob(name) if item.is_file() and "driver" in item.parts)
        except OSError:
            continue
    if not found:
        return None
    found.sort(key=_rank)
    return found[0]


_found = _node()
if _found is not None:
    os.environ["PLAYWRIGHT_NODEJS_PATH"] = str(_found)
