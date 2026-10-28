"""PyInstaller runtime hook. Runs before the helper so CDP can find Node."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _node() -> Path | None:
    if not getattr(sys, "frozen", False):
        return None
    name = "node.exe" if sys.platform == "win32" else "node"
    meipass = getattr(sys, "_MEIPASS", None)
    roots = []
    if meipass:
        roots.append(Path(meipass))
    roots.append(Path(sys.executable).resolve().parent)
    roots.append(Path(sys.executable).resolve().parent / "_internal")
    for root in roots:
        direct = root / "playwright" / "driver" / name
        if direct.is_file():
            return direct
        try:
            for item in root.rglob(name):
                if item.is_file() and "driver" in item.parts:
                    return item
        except OSError:
            continue
    return None


_found = _node()
if _found is not None:
    os.environ["PLAYWRIGHT_NODEJS_PATH"] = str(_found)
