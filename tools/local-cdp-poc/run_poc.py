"""Write T172 evidence for the local same-tab attach PoC.

The hosted Auto-Fill service is not started. Nothing is deployed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("AUTOFILL_LOCAL_ATTACH", "1")

from autofill.local_attach import (  # noqa: E402
    chrome_version,
    demonstrate_dedicated_fill,
    find_chrome,
    probe_branded_load_extension,
    probe_debug_port,
    probe_extension_debugger,
    probe_inspect_consent_cold,
)

EVIDENCE = Path(__file__).resolve().parent / "evidence"
FIXTURE = Path(__file__).resolve().parent / "fixture" / "application.html"
EXTENSION = Path(__file__).resolve().parent / "extension"
PROFILE = ROOT / "examples" / "profile.example.json"


def _chrome_for_testing() -> str | None:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        path = playwright.chromium.executable_path
    if path and Path(path).is_file():
        return path
    return None


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="t172-poc-"))
    report: dict[str, object] = {
        "chrome_version": chrome_version(find_chrome()),
        "probes": [],
    }
    probes = []
    for mode in ("headed_daily", "explicit_default", "headless_omitted", "dedicated"):
        probes.append(probe_debug_port(mode, timeout=6, work_dir=work / mode))
    report["probes"] = probes
    report["branded_extension"] = probe_branded_load_extension(EXTENSION)
    testing = _chrome_for_testing()
    if testing:
        report["extension_debugger"] = probe_extension_debugger(EXTENSION, executable=testing)
    report["inspect_consent"] = probe_inspect_consent_cold()
    report["dedicated_fill"] = demonstrate_dedicated_fill(
        fixture_html=FIXTURE,
        profile_path=PROFILE,
        work_dir=work / "fill",
        screenshot_path=EVIDENCE / "dedicated-fill.png",
        headless=False,
    )
    destination = EVIDENCE / "report.json"
    destination.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
