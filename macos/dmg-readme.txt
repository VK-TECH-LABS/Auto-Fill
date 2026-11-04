TileArc Auto-Fill Helper (STAGING)

This disk image is for TileArc staging on an {{ARCH_LABEL}} Mac.
It is not the production helper.

Apple Silicon Macs use TileArc-AutoFill-Helper-Setup-staging-arm64.dmg.
Intel Macs, including a 2020 MacBook, use TileArc-AutoFill-Helper-Setup-staging-x86_64.dmg.
Build-arch.txt on this disk records the architecture of this image.

1. Drag "TileArc Auto-Fill Helper (STAGING).app" onto Applications.
2. Eject this disk.
3. Open Applications. Right-click the app and choose Open, then Open.
4. If macOS says the developer cannot be verified, open System Settings,
   Privacy & Security, and click Open Anyway.
5. Visit http://127.0.0.1:9410/health
   The page should include "status": "ok".

This build is not signed with an Apple Developer ID and is not notarized.
The longer steps are in MACOS_EMPLOYEE.md on this disk.

Use this Mac app on macOS. The Windows Setup.exe is for Windows PCs.
