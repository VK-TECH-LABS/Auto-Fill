#!/usr/bin/env bash
# Names for the two thin staging macOS disk images.
# Sourced by build.sh, smoke.sh, and publish-draft.sh.
# arm64 is Apple Silicon. x86_64 is Intel. This script does not build a universal binary.

macos_disk_arch() {
  uname -m
}

macos_dmg_basename_for() {
  case "$1" in
    arm64) printf '%s\n' "TileArc-AutoFill-Helper-Setup-staging-arm64.dmg" ;;
    x86_64) printf '%s\n' "TileArc-AutoFill-Helper-Setup-staging-x86_64.dmg" ;;
    *)
      echo "Unsupported Mac architecture: $1" >&2
      return 1
      ;;
  esac
}

macos_dmg_label_for() {
  case "$1" in
    arm64) printf '%s\n' "Apple Silicon (arm64)" ;;
    x86_64) printf '%s\n' "Intel (x86_64)" ;;
    *)
      echo "Unsupported Mac architecture: $1" >&2
      return 1
      ;;
  esac
}

# Basename for this machine. TILEARC_MACOS_DMG, when set, must match that name.
macos_require_dmg_name() {
  local arch name requested
  arch="$(macos_disk_arch)"
  name="$(macos_dmg_basename_for "$arch")"
  requested="${TILEARC_MACOS_DMG:-}"
  if [[ -n "$requested" && "$requested" != "$name" ]]; then
    echo "TILEARC_MACOS_DMG=${requested} does not match runner arch ${arch} (${name})" >&2
    return 1
  fi
  printf '%s\n' "$name"
}
