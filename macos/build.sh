#!/usr/bin/env bash
# Build one thin staging disk image for the Mac this script is running on.
# Apple Silicon (arm64) writes TileArc-AutoFill-Helper-Setup-staging-arm64.dmg.
# Intel (x86_64) writes TileArc-AutoFill-Helper-Setup-staging-x86_64.dmg.
# GitHub Actions runs this on macos-latest and on macos-15-intel.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=disk-name.sh
source "$ROOT/macos/disk-name.sh"

APP_NAME="TileArc Auto-Fill Helper (STAGING).app"
VOLUME="TileArc Auto-Fill Helper (STAGING)"
APP="$ROOT/dist/$APP_NAME"
BIN="$APP/Contents/MacOS/TileArcAutoFillHelper"
arch="$(macos_disk_arch)"
dmg_name="$(macos_require_dmg_name)"
label="$(macos_dmg_label_for "$arch")"
DMG="$ROOT/dist/$dmg_name"

echo "Building ${label}"
echo "Installer path: ${DMG}"

echo "Installing Auto-Fill and PyInstaller"
python3 -m pip install --upgrade pip
python3 -m pip install -e . "pyinstaller==6.16.0"

echo "Checking Python and the Playwright driver are thin ${arch}"
python3 - "$arch" <<'PY'
import platform
import subprocess
import sys
from pathlib import Path

expected = sys.argv[1]
machine = platform.machine()
if machine != expected:
    sys.exit(f"Python is {machine}; this runner is {expected}.")
import playwright

node = Path(playwright.__file__).resolve().parent / "driver" / "node"
if not node.is_file():
    sys.exit(f"Playwright driver node is missing: {node}")
completed = subprocess.run(
    ["lipo", "-archs", str(node)],
    check=False,
    capture_output=True,
    text=True,
)
archs = completed.stdout.split()
if completed.returncode != 0 or archs != [expected]:
    detail = " ".join(archs) or completed.stderr.strip() or "unknown"
    sys.exit(f"Playwright driver node is {detail}; expected thin {expected}.")
print(f"Playwright driver node is thin {expected}: {node}")
PY

echo "Freezing the helper"
python3 -m PyInstaller --noconfirm --clean macos/autofill_helper.spec

if [[ ! -f "$BIN" ]]; then
  echo "Missing $BIN" >&2
  find "$ROOT/dist" -maxdepth 4 -print >&2 || true
  exit 1
fi

python3 - "$APP" "$arch" <<'PY'
import subprocess
import sys
from pathlib import Path

app = Path(sys.argv[1])
expected = sys.argv[2]
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


def mach_archs(path: Path) -> list[str]:
    completed = subprocess.run(
        ["lipo", "-archs", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return []
    return completed.stdout.split()


checked: list[Path] = []
seen: set[Path] = set()
for path in [main, node, *machos]:
    if path in seen:
        continue
    seen.add(path)
    checked.append(path)
mismatches: list[str] = []
for path in checked:
    found = mach_archs(path)
    # The helper executable and Playwright's node decide which Mac can open the app.
    # Those two stay thin. Other Mach-O files (a universal2 wheel) may list more than
    # one arch as long as this runner's arch is one of them.
    if path in (main, node):
        if found != [expected]:
            mismatches.append(f"{path}: {' '.join(found) if found else 'not a thin Mach-O'}")
    elif expected not in found:
        mismatches.append(f"{path}: {' '.join(found) if found else 'not a Mach-O'}")
if mismatches:
    sys.exit(
        "Refusing to package a disk image that will not run on "
        f"{expected}:\n" + "\n".join(mismatches)
    )
print(f"Mach-O arch {expected} confirmed for {len(checked)} files")
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
readme_template="$(cat "$ROOT/macos/dmg-readme.txt")"
printf '%s\n' "${readme_template//\{\{ARCH_LABEL\}\}/$label}" > "$stage/How to install (STAGING).txt"
if grep -q '{{ARCH_LABEL}}' "$stage/How to install (STAGING).txt"; then
  echo "arch label was not written into the disk readme" >&2
  exit 1
fi
ln -s /Applications "$stage/Applications"
printf '%s\n' "$arch" > "$stage/Build-arch.txt"

echo "Creating $DMG"
hdiutil create \
  -volname "$VOLUME" \
  -srcfolder "$stage" \
  -ov \
  -format UDZO \
  "$DMG"

echo "Installer: $DMG"
