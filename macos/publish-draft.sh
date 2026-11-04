#!/usr/bin/env bash
# Add both macOS disk images to the existing staging draft release.
# The Windows workflow owns TileArc-AutoFill-Helper-Setup-staging.exe on the same tag.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=disk-name.sh
source "$ROOT/macos/disk-name.sh"

repo="${TILEARC_RELEASE_REPO:-VK-TECH-LABS/Auto-Fill}"
tag=tilearc-autofill-helper-staging
sha="${GITHUB_SHA:?}"
arm="dist/$(macos_dmg_basename_for arm64)"
intel="dist/$(macos_dmg_basename_for x86_64)"
# Retired single image. It was Apple Silicon only and had no architecture in the name.
legacy="TileArc-AutoFill-Helper-Setup-staging.dmg"

test -f "$arm"
test -f "$intel"

echo "Apple Silicon (arm64) $(basename "$arm")"
sha256sum "$arm"
echo "Intel (x86_64) $(basename "$intel")"
sha256sum "$intel"

notes="$(cat <<'EOF'
Staging-only installers for the TileArc Auto-Fill local helper.

Assets on this draft tag:
- TileArc-AutoFill-Helper-Setup-staging.exe (Windows)
- TileArc-AutoFill-Helper-Setup-staging-arm64.dmg (macOS, Apple Silicon, arm64)
- TileArc-AutoFill-Helper-Setup-staging-x86_64.dmg (macOS, Intel, x86_64)

Apple Silicon Macs (M1 or newer) install the arm64 disk image.
Intel Macs, including a 2020 MacBook, install the x86_64 disk image.
The arm64 image does not open on an Intel Mac.

Digests: use the sha256 of each macOS asset. Ping Setup with both hashes. The arm64 file is Apple Silicon. The x86_64 file is Intel.

This is not a production release. The helper listens on 127.0.0.1:9410, sets AUTOFILL_LOCAL_ATTACH for that process, and uses a dedicated Chrome profile. It does not attach to daily Chrome. Final Submit stays human.

macOS employees install the disk image that matches their chip. Docs: docs/MACOS_EMPLOYEE.md and docs/WINDOWS_EMPLOYEE.md.
EOF
)"

upload_notes() {
  local current="$1"
  if ! grep -q "TileArc-AutoFill-Helper-Setup-staging-arm64.dmg" <<<"$current" \
    || ! grep -q "TileArc-AutoFill-Helper-Setup-staging-x86_64.dmg" <<<"$current" \
    || ! grep -q "Apple Silicon" <<<"$current" \
    || ! grep -q "Intel" <<<"$current"; then
    gh release edit "$tag" --notes "$notes" -R "$repo"
  fi
}

drop_legacy_asset() {
  if gh release view "$tag" -R "$repo" --json assets --jq '.assets[].name' | grep -qx "$legacy"; then
    gh release delete-asset "$tag" "$legacy" --yes -R "$repo"
  fi
}

if gh release view "$tag" -R "$repo" >/dev/null 2>&1; then
  gh release upload "$tag" "$arm" "$intel" --clobber -R "$repo"
  drop_legacy_asset
  current="$(gh release view "$tag" -R "$repo" --json body --jq .body)"
  upload_notes "$current"
else
  gh release create "$tag" "$arm" "$intel" \
    --draft \
    --latest=false \
    --target "$sha" \
    --title "TileArc Auto-Fill Helper (STAGING)" \
    --notes "$notes" \
    -R "$repo"
fi
