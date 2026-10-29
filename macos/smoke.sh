#!/usr/bin/env bash
# Prove the frozen macOS helper serves the staging extension health contract.
# Run after macos/build.sh. Stops the helper before it returns.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="TileArc Auto-Fill Helper (STAGING).app"
BIN_REL="Contents/MacOS/TileArcAutoFillHelper"
APP="$ROOT/dist/$APP_NAME"
BIN="$APP/$BIN_REL"
DMG="$ROOT/dist/TileArc-AutoFill-Helper-Setup-staging.dmg"
SUPPORT="$HOME/Library/Application Support/TileArc/AutoFill"
DOCTOR="$SUPPORT/doctor.json"
PLIST="$HOME/Library/LaunchAgents/com.vktechlabs.tilearc.autofill.helper.staging.plist"

if [[ ! -f "$BIN" ]]; then
  echo "Missing $BIN" >&2
  exit 1
fi
if [[ ! -f "$DMG" ]]; then
  echo "Missing $DMG" >&2
  exit 1
fi

stop_helper() {
  pkill -f "Contents/MacOS/TileArcAutoFillHelper" >/dev/null 2>&1 || true
  sleep 1
}

show_logs() {
  if [[ -f "$DOCTOR" ]]; then
    echo "----- $DOCTOR -----"
    tail -n 80 "$DOCTOR" || true
  fi
  if [[ -f "$SUPPORT/helper.log" ]]; then
    echo "----- $SUPPORT/helper.log -----"
    tail -n 80 "$SUPPORT/helper.log" || true
  fi
}

assert_doctor() {
  local exe="$1"
  echo "Doctor: $exe"
  rm -f "$DOCTOR"
  if ! python3 - "$exe" "$DOCTOR" <<'PY'
import json
import subprocess
import sys
import time
from pathlib import Path

exe = sys.argv[1]
doctor = Path(sys.argv[2])
try:
    completed = subprocess.run([exe, "--doctor"], timeout=60, check=False)
except subprocess.TimeoutExpired:
    sys.exit("doctor timed out")
deadline = time.time() + 5
while not doctor.is_file() and time.time() < deadline:
    time.sleep(0.25)
if completed.returncode != 0:
    sys.exit(f"doctor exited {completed.returncode}")
if not doctor.is_file():
    sys.exit("doctor.json was not written")
report = json.loads(doctor.read_text(encoding="utf-8"))
failures = []
if report.get("driverOk") is not True:
    failures.append(f"Playwright driver missing: {report.get('driver')}")
if report.get("domUiOk") is not True:
    failures.append(f"dom_ui.js missing: {report.get('domUi')}")
if report.get("attach") != "1":
    failures.append("AUTOFILL_LOCAL_ATTACH was not set")
if report.get("bind") != "127.0.0.1":
    failures.append("helper is not loopback-only")
if report.get("port") != 9410:
    failures.append(f"helper port is {report.get('port')}, expected 9410")
if report.get("profileIsDailyChrome") is True:
    failures.append("profile is daily Chrome")
if report.get("submit") != "human":
    failures.append("submit is not human")
if report.get("service") != "auto-fill-local":
    failures.append("service is not auto-fill-local")
profile = str(report.get("profileDir") or "").replace("\\", "/")
if "/Google/Chrome" in profile or profile.rstrip("/").endswith("google-chrome"):
    failures.append(f"profile looks like daily Chrome: {profile}")
if not profile.endswith("TileArc/AutoFill/apply-profile"):
    failures.append(f"profile is not the dedicated apply profile: {profile}")
if failures:
    print("\n".join(failures), file=sys.stderr)
    sys.exit(1)
print("Doctor ok. Profile:", profile)
PY
  then
    show_logs
    exit 1
  fi
}

wait_health() {
  python3 - <<'PY'
import json
import sys
import time
import urllib.error
import urllib.request

url = "http://127.0.0.1:9410/health"
deadline = time.time() + 90
last = ""
while time.time() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=3) as response:
            body = json.loads(response.read().decode("utf-8"))
        if (
            body.get("status") == "ok"
            and body.get("service") == "auto-fill-local"
            and body.get("mode") == "dedicated-profile"
            and body.get("submit") == "human"
        ):
            print(json.dumps(body))
            raise SystemExit(0)
        last = json.dumps(body)
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        last = str(exc)
    time.sleep(0.5)
print(f"health did not become ready on 127.0.0.1:9410 ({last})", file=sys.stderr)
raise SystemExit(1)
PY
}

assert_loopback() {
  local text
  text="$(lsof -nP -iTCP:9410 -sTCP:LISTEN || true)"
  echo "$text"
  if [[ -z "$text" ]]; then
    echo "nothing is listening on 9410" >&2
    exit 1
  fi
  if ! grep -q "127.0.0.1:9410" <<<"$text"; then
    echo "9410 is not bound to 127.0.0.1" >&2
    exit 1
  fi
  if grep -E '\*:9410|0\.0\.0\.0:9410|\[::\]:9410' <<<"$text"; then
    echo "9410 is exposed beyond loopback" >&2
    exit 1
  fi
}

assert_running() {
  local exe="$1"
  stop_helper
  "$exe" >/dev/null 2>&1 &
  local pid=$!
  if ! wait_health; then
    show_logs
    kill "$pid" >/dev/null 2>&1 || true
    stop_helper
    exit 1
  fi
  assert_loopback
  kill "$pid" >/dev/null 2>&1 || true
  stop_helper
}

assert_plist_absent() {
  if [[ -f "$PLIST" ]]; then
    echo "login item was written outside an Applications folder: $PLIST" >&2
    exit 1
  fi
}

rm -f "$PLIST"
stop_helper
assert_doctor "$BIN"
assert_running "$BIN"
assert_plist_absent

MOUNT="$(python3 - "$DMG" <<'PY'
import plistlib
import subprocess
import sys

raw = subprocess.check_output(
    ["hdiutil", "attach", "-nobrowse", "-readonly", "-plist", sys.argv[1]]
)
data = plistlib.loads(raw)
for entity in data.get("system-entities", []):
    mount = entity.get("mount-point")
    if mount:
        print(mount)
        raise SystemExit(0)
sys.exit("disk image did not mount")
PY
)"
cleanup_mount() {
  if [[ -n "${MOUNT:-}" ]]; then
    hdiutil detach "$MOUNT" >/dev/null 2>&1 || hdiutil detach -force "$MOUNT" >/dev/null 2>&1 || true
  fi
  stop_helper
}
trap cleanup_mount EXIT

INSTALLED="$MOUNT/$APP_NAME/$BIN_REL"
if [[ ! -f "$INSTALLED" ]]; then
  echo "disk image is missing $INSTALLED" >&2
  exit 1
fi
assert_doctor "$INSTALLED"
assert_running "$INSTALLED"
assert_plist_absent
echo "Smoke passed for TileArc-AutoFill-Helper-Setup-staging.dmg"
