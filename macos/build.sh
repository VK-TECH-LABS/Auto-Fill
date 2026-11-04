#!/usr/bin/env bash
# Build the Universal TileArc-AutoFill-Helper-Setup-staging.dmg on macOS.
# Employees do not run this. GitHub Actions runs it on macos-latest (Apple Silicon).
# The frozen app contains Intel x86_64 and Apple Silicon arm64 slices.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

APP_NAME="TileArc Auto-Fill Helper (STAGING).app"
VOLUME="TileArc Auto-Fill Helper (STAGING)"
APP="$ROOT/dist/$APP_NAME"
BIN="$APP/Contents/MacOS/TileArcAutoFillHelper"
DMG="$ROOT/dist/TileArc-AutoFill-Helper-Setup-staging.dmg"
FRAMEWORK_PY="/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
# Last Python 3.12 release that ships a macOS universal2 installer.
PY_PKG_URL="https://www.python.org/ftp/python/3.12.10/python-3.12.10-macos11.pkg"
PY_PKG_SHA256="8373e58da4ea146b3eb1c1f9834f19a319440b6b679b06050b1f9ee3237aa8e4"
VENV="$ROOT/.venv-macos-universal"

python_is_universal() {
  local bin="$1"
  [[ -x "$bin" ]] || return 1
  local archs
  archs="$(lipo -archs "$bin" 2>/dev/null || true)"
  [[ "$archs" == *x86_64* && "$archs" == *arm64* ]]
}

if [[ "$(uname -m)" == "arm64" ]]; then
  if ! arch -x86_64 /usr/bin/true >/dev/null 2>&1; then
    echo "Installing Rosetta so smoke.sh can execute the x86_64 slice"
    sudo softwareupdate --install-rosetta --agree-to-license
  fi
  arch -x86_64 /usr/bin/true
fi

if ! python_is_universal "$FRAMEWORK_PY"; then
  echo "Installing Python 3.12.10 universal2 from python.org"
  curl -fL --retry 3 -o /tmp/python-3.12.10-macos11.pkg "$PY_PKG_URL"
  echo "${PY_PKG_SHA256}  /tmp/python-3.12.10-macos11.pkg" | shasum -a 256 -c -
  xattr -c /tmp/python-3.12.10-macos11.pkg 2>/dev/null || true
  sudo installer -pkg /tmp/python-3.12.10-macos11.pkg -target /
fi
if ! python_is_universal "$FRAMEWORK_PY"; then
  echo "Python at $FRAMEWORK_PY is not a universal2 binary" >&2
  exit 1
fi
"$FRAMEWORK_PY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else "need Python 3.12")'

echo "Installing Auto-Fill and PyInstaller into a universal2 virtualenv"
rm -rf "$VENV"
"$FRAMEWORK_PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -e . "pyinstaller==6.16.0"
"$VENV/bin/python" "$ROOT/macos/universal_node.py"

echo "Freezing the universal2 helper"
"$VENV/bin/python" -m PyInstaller --noconfirm --clean macos/autofill_helper.spec

if [[ ! -f "$BIN" ]]; then
  echo "Missing $BIN" >&2
  find "$ROOT/dist" -maxdepth 4 -print >&2 || true
  exit 1
fi

bash "$ROOT/macos/check_universal.sh" "$APP"

"$VENV/bin/python" - "$APP" <<'PY'
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

bash "$ROOT/macos/check_universal.sh" "$APP"

stage="$(mktemp -d)"
cleanup() {
  rm -rf "$stage"
}
trap cleanup EXIT

ditto "$APP" "$stage/$APP_NAME"
cp "$ROOT/docs/MACOS_EMPLOYEE.md" "$stage/MACOS_EMPLOYEE.md"
cp "$ROOT/macos/dmg-readme.txt" "$stage/How to install (STAGING).txt"
ln -s /Applications "$stage/Applications"
# Record the Mach-O slices inside the app. uname -m is only the runner.
printf '%s\n' universal2 x86_64 arm64 > "$stage/Build-arch.txt"

echo "Creating $DMG"
hdiutil create \
  -volname "$VOLUME" \
  -srcfolder "$stage" \
  -ov \
  -format UDZO \
  "$DMG"

echo "Installer: $DMG"
