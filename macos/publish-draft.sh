#!/usr/bin/env bash
# Add the macOS disk image to the existing staging draft release.
# The Windows workflow owns TileArc-AutoFill-Helper-Setup-staging.exe on the same tag.
set -euo pipefail

repo="${TILEARC_RELEASE_REPO:-VK-TECH-LABS/Auto-Fill}"
tag=tilearc-autofill-helper-staging
file=dist/TileArc-AutoFill-Helper-Setup-staging.dmg
sha="${GITHUB_SHA:?}"

test -f "$file"

notes="$(cat <<'EOF'
Staging-only installers for the TileArc Auto-Fill local helper.

Assets on this draft tag:
- TileArc-AutoFill-Helper-Setup-staging.exe (Windows)
- TileArc-AutoFill-Helper-Setup-staging.dmg (macOS Universal: Intel x86_64 and Apple Silicon arm64)

This is not a production release. The helper listens on 127.0.0.1:9410, sets AUTOFILL_LOCAL_ATTACH for that process, and uses a dedicated Chrome profile. It does not attach to daily Chrome. Final Submit stays human.

The disk image is one Universal app. Build-arch.txt inside the image lists universal2, x86_64, and arm64. Uploading replaces that asset in place, so the release asset digest (sha256) changes. The Windows exe keeps its own filename and digest.

macOS employees install the disk image. Docs: docs/MACOS_EMPLOYEE.md and docs/WINDOWS_EMPLOYEE.md.
EOF
)"

if gh release view "$tag" -R "$repo" >/dev/null 2>&1; then
  gh release upload "$tag" "$file" --clobber -R "$repo"
  gh release edit "$tag" --notes "$notes" -R "$repo"
else
  gh release create "$tag" "$file" \
    --draft \
    --latest=false \
    --target "$sha" \
    --title "TileArc Auto-Fill Helper (STAGING)" \
    --notes "$notes" \
    -R "$repo"
fi

echo "Draft release asset digests (assets[].digest is sha256 of the uploaded bytes):"
gh release view "$tag" -R "$repo" --json tagName,isDraft,assets \
  --jq '{tagName,isDraft,assets:[.assets[]|{name,digest,size}]}'
