#!/usr/bin/env bash
# Fail unless every Mach-O file in the .app contains Intel (x86_64) and Apple Silicon (arm64).
set -euo pipefail

app="${1:?path to the .app}"
if [[ ! -d "$app" ]]; then
  echo "Not an app directory: $app" >&2
  exit 1
fi

fail=0
count=0
while IFS= read -r -d '' path; do
  kind="$(file -b "$path" || true)"
  if [[ "$kind" != *Mach-O* ]]; then
    continue
  fi
  count=$((count + 1))
  archs="$(lipo -archs "$path" 2>/dev/null || true)"
  if [[ "$archs" != *x86_64* || "$archs" != *arm64* ]]; then
    echo "Mach-O is not universal2 (archs: ${archs:-unknown}): $path" >&2
    fail=1
  fi
done < <(find "$app" -type f -print0)

if [[ "$count" -lt 1 ]]; then
  echo "No Mach-O files in $app" >&2
  exit 1
fi
if [[ "$fail" -ne 0 ]]; then
  exit 1
fi
echo "Universal2 check ok: $count Mach-O files contain x86_64 and arm64 ($app)"
