#!/usr/bin/env bash
# Build TileArc-AutoFill-Helper-Setup-staging.dmg on macOS.
# Employees do not run this. GitHub Actions runs it on macos-latest.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP_NAME="TileArc Auto-Fill Helper (STAGING).app"
VOLUME="TileArc Auto-Fill Helper (STAGING)"
APP="$ROOT/dist/$APP_NAME"
BIN="$APP/Contents/MacOS/TileArcAutoFillHelper"
DMG="$ROOT/dist/TileArc-AutoFill-Helper-Setup-staging.dmg"

echo "Installing Auto-Fill and PyInstaller"
python3 -m pip install --upgrade pip
python3 -m pip install -e . "pyinstaller==6.16.0"

echo "Freezing the helper"
python3 -m PyInstaller --noconfirm --clean macos/autofill_helper.spec

if [[ ! -f "$BIN" ]]; then
  echo "Missing $BIN" >&2
  find "$ROOT/dist" -maxdepth 4 -print >&2 || true
  exit 1
fi

python3 - "$APP" <<'PY'
import subprocess
import sys
from pathlib import Path

app = Path(sys.argv[1])
node = None
cli = None
for path in app.rglob("*"):
    if not path.is_file():
        continue
    parts = path.parts
    if path.name == "node" and "playwright" in parts and "driver" in parts:
        node = path
    if path.name == "cli.js" and "playwright" in parts:
        cli = path
if node is None:
    for path in list(app.rglob("*"))[:400]:
        print(path, file=sys.stderr)
    sys.exit("Playwright driver node was not frozen into the helper.")
if cli is None:
    sys.exit("Playwright driver cli.js was not frozen into the helper.")
print(f"Playwright driver: {node}")
print(f"Playwright cli: {cli}")

print("Ad-hoc signing the app (no Apple Developer ID)")
subprocess.run(["xattr", "-cr", str(app)], check=False)
main = app / "Contents" / "MacOS" / "TileArcAutoFillHelper"
machos: list[Path] = []
for path in app.rglob("*"):
    if not path.is_file() or path.is_symlink() or path == main:
        continue
    kind = subprocess.run(
        ["file", "-b", str(path)],
        check=False,
        capture_output=True,
        text=True,
    ).stdout
    if "Mach-O" in kind:
        machos.append(path)
# Inside-out. codesign --deep does not sign Mach-O files that PyInstaller stored
# as data under Contents/Resources (the Playwright driver can land there).
machos.sort(key=lambda path: len(path.parts), reverse=True)
for path in machos:
    subprocess.run(["codesign", "--force", "--sign", "-", str(path)], check=True)
subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
verify = subprocess.run(
    ["codesign", "--verify", "--deep", "--strict", str(app)],
    check=False,
    capture_output=True,
    text=True,
)
if verify.returncode != 0:
    sys.stderr.write(verify.stdout)
    sys.stderr.write(verify.stderr)
    sys.exit(f"codesign --verify failed with {verify.returncode}")
PY

stage="$(mktemp -d)"
cleanup() {
  rm -rf "$stage"
}
trap cleanup EXIT

ditto "$APP" "$stage/$APP_NAME"
cp "$ROOT/docs/MACOS_EMPLOYEE.md" "$stage/MACOS_EMPLOYEE.md"
cp "$ROOT/macos/dmg-readme.txt" "$stage/How to install (STAGING).txt"
ln -s /Applications "$stage/Applications"
uname -m > "$stage/Build-arch.txt"

echo "Creating $DMG"
hdiutil create \
  -volname "$VOLUME" \
  -srcfolder "$stage" \
  -ov \
  -format UDZO \
  "$DMG"

echo "Installer: $DMG"
