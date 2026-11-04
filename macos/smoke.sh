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

can_run_arch() {
  arch "-$1" /usr/bin/true >/dev/null 2>&1
}

if [[ "$(uname -m)" == "arm64" ]] && ! can_run_arch x86_64; then
  echo "Installing Rosetta so the Intel slice can run"
  sudo softwareupdate --install-rosetta --agree-to-license
fi
if [[ "$(uname -m)" == "arm64" ]] && ! can_run_arch x86_64; then
  echo "The x86_64 slice cannot run on this Apple Silicon Mac" >&2
  exit 1
fi

expect_node_arch() {
  local node="$1"
  local arch_name="$2"
  local expect="$3"
  local err
  err="$(mktemp)"
  local out
  if ! out="$(arch "-$arch_name" "$node" -e 'process.stdout.write(process.arch + "\n")' 2>"$err")"; then
    echo "Playwright node failed under arch -$arch_name ($node)" >&2
    cat "$err" >&2 || true
    rm -f "$err"
    exit 1
  fi
  rm -f "$err"
  if [[ "$out" != "$expect" ]]; then
    echo "Playwright node under -$arch_name printed [$out], expected [$expect] ($node)" >&2
    exit 1
  fi
  echo "Playwright node $arch_name reports $out"
}

assert_node_slices() {
  local app="$1"
  local found=0
  while IFS= read -r -d '' node; do
    found=1
    if can_run_arch arm64; then
      expect_node_arch "$node" arm64 arm64
    fi
    if can_run_arch x86_64; then
      expect_node_arch "$node" x86_64 x64
    fi
  done < <(find "$app" -type f -path '*/playwright/driver/node' -print0)
  if [[ "$found" -eq 0 ]]; then
    echo "Playwright node is missing from $app" >&2
    exit 1
  fi
}

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
  shift
  echo "Doctor: $* $exe"
  rm -f "$DOCTOR"
  if ! python3 - "$exe" "$DOCTOR" "$@" <<'PY'
import json
import subprocess
import sys
import time
from pathlib import Path

exe = sys.argv[1]
doctor = Path(sys.argv[2])
prefix = sys.argv[3:]
try:
    completed = subprocess.run([*prefix, exe, "--doctor"], timeout=180, check=False)
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
  shift
  stop_helper
  echo "Launch: $* $exe"
  "$@" "$exe" >/dev/null 2>&1 &
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
bash "$ROOT/macos/check_universal.sh" "$APP"
assert_node_slices "$APP"
assert_doctor "$BIN"
assert_running "$BIN"
if can_run_arch x86_64 && [[ "$(uname -m)" != "x86_64" ]]; then
  assert_doctor "$BIN" arch -x86_64
  assert_running "$BIN" arch -x86_64
fi
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
MOUNTED_APP="$MOUNT/$APP_NAME"
if [[ ! -f "$INSTALLED" ]]; then
  echo "disk image is missing $INSTALLED" >&2
  exit 1
fi
bash "$ROOT/macos/check_universal.sh" "$MOUNTED_APP"
assert_node_slices "$MOUNTED_APP"
arch_file="$MOUNT/Build-arch.txt"
if [[ ! -f "$arch_file" ]]; then
  echo "disk image is missing Build-arch.txt" >&2
  exit 1
fi
arch_text="$(cat "$arch_file")"
if [[ "$arch_text" != *"universal2"* || "$arch_text" != *"x86_64"* || "$arch_text" != *"arm64"* ]]; then
  echo "Build-arch.txt does not list universal2, x86_64, and arm64:" >&2
  echo "$arch_text" >&2
  exit 1
fi
assert_doctor "$INSTALLED"
assert_running "$INSTALLED"
assert_plist_absent
echo "Smoke passed for TileArc-AutoFill-Helper-Setup-staging.dmg"
