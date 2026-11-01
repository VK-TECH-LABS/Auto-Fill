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
- TileArc-AutoFill-Helper-Setup-staging.dmg (macOS, Apple Silicon)

This is not a production release. The helper listens on 127.0.0.1:9410, sets AUTOFILL_LOCAL_ATTACH for that process, and uses a dedicated Chrome profile. It does not attach to daily Chrome. Final Submit stays human.

macOS employees install the disk image. Docs: docs/MACOS_EMPLOYEE.md and docs/WINDOWS_EMPLOYEE.md.
EOF
)"

if gh release view "$tag" -R "$repo" >/dev/null 2>&1; then
  gh release upload "$tag" "$file" --clobber -R "$repo"
  current="$(gh release view "$tag" -R "$repo" --json body --jq .body)"
  if ! grep -q "TileArc-AutoFill-Helper-Setup-staging.dmg" <<<"$current"; then
    gh release edit "$tag" --notes "$notes" -R "$repo"
  fi
else
  gh release create "$tag" "$file" \
    --draft \
    --latest=false \
    --target "$sha" \
    --title "TileArc Auto-Fill Helper (STAGING)" \
    --notes "$notes" \
    -R "$repo"
fi
