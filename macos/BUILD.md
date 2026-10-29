# macOS helper build (staging)

Employees on macOS install `TileArc-AutoFill-Helper-Setup-staging.dmg`. They do not install Python. The disk image contains **TileArc Auto-Fill Helper (STAGING).app**, the same loopback helper Windows freezes (`src/autofill/win_launcher.py` → `src/autofill/local_helper.py`). The app sets `AUTOFILL_LOCAL_ATTACH=1` in that process, listens on port **9410**, and uses a dedicated Chrome profile:

`~/Library/Application Support/TileArc/AutoFill/apply-profile`

It does not attach to daily Chrome (`~/Library/Application Support/Google/Chrome`). Final Submit stays human. The hosted companion remains the fallback when this helper is not running.

The app is ad-hoc signed so macOS can launch the bundled Playwright driver. It is **not** signed with an Apple Developer ID and it is **not** notarized. Gatekeeper steps are in `docs/MACOS_EMPLOYEE.md`.

GitHub Actions workflow: `.github/workflows/macos-helper.yml` (`macos-latest`, Apple Silicon). The Windows installer stays on `.github/workflows/windows-helper.yml`.

## Artifact the Installer API should expose

| Item | Value |
| --- | --- |
| File name | `TileArc-AutoFill-Helper-Setup-staging.dmg` |
| App inside the disk image | `TileArc Auto-Fill Helper (STAGING).app` |
| Actions artifact name | `TileArc-AutoFill-Helper-Setup-staging-macos` |
| Architecture | `arm64` (the `macos-latest` runner; `Build-arch.txt` on the disk records `uname -m`). Intel Macs stay on the hosted companion. |
| Content type | `application/x-apple-diskimage` |
| Workflow | https://github.com/VK-TECH-LABS/Auto-Fill/actions/workflows/macos-helper.yml |
| Draft release tag | `tilearc-autofill-helper-staging` (same draft as the Windows exe) |
| Draft release | https://github.com/VK-TECH-LABS/Auto-Fill/releases/tag/tilearc-autofill-helper-staging |
| Public download URL (after the draft is published) | https://github.com/VK-TECH-LABS/Auto-Fill/releases/download/tilearc-autofill-helper-staging/TileArc-AutoFill-Helper-Setup-staging.dmg |

The workflow uploads the Actions artifact on every pull request into `staging` and on every push to `staging`. A push to `staging` adds the disk image to the existing **draft** release tag `tilearc-autofill-helper-staging`. It does not remove `TileArc-AutoFill-Helper-Setup-staging.exe`. Draft assets are not an anonymous download. Publish that draft (keep the title marked STAGING) before the TileArc "Download Installer" button uses the public URL above.

Choose the asset from the employee operating system. A Mac download returns the disk image. A Windows download keeps returning the Setup.exe. Suggested staging payload:

```json
{
  "staging": true,
  "assets": {
    "windows": {
      "filename": "TileArc-AutoFill-Helper-Setup-staging.exe",
      "contentType": "application/vnd.microsoft.portable-executable",
      "url": "https://github.com/VK-TECH-LABS/Auto-Fill/releases/download/tilearc-autofill-helper-staging/TileArc-AutoFill-Helper-Setup-staging.exe"
    },
    "macos": {
      "filename": "TileArc-AutoFill-Helper-Setup-staging.dmg",
      "arch": "arm64",
      "contentType": "application/x-apple-diskimage",
      "url": "https://github.com/VK-TECH-LABS/Auto-Fill/releases/download/tilearc-autofill-helper-staging/TileArc-AutoFill-Helper-Setup-staging.dmg"
    }
  }
}
```

If the portal vendors the file instead of redirecting to GitHub, vendor `TileArc-AutoFill-Helper-Setup-staging.dmg` beside the Windows exe and serve that file for macOS clients. Do not point the Mac button at the Windows exe, a placeholder, a source zip, or the extension package. The extension zip stays in `vinayaka/fillo-extension`. See `docs/MACOS_EMPLOYEE.md`.

## What the smoke test checks

`macos/smoke.sh` runs in CI after the disk image is built:

- `--doctor` finds the bundled Playwright driver (CDP only; Chrome itself is the employee's Google Chrome).
- The profile path is `~/Library/Application Support/TileArc/AutoFill/apply-profile`, not Chrome's daily directory.
- `GET http://127.0.0.1:9410/health` returns `status=ok`, `service=auto-fill-local`, `mode=dedicated-profile`, `submit=human`.
- The listener is `127.0.0.1:9410`, not `0.0.0.0`.
- The same checks pass for the app inside `TileArc-AutoFill-Helper-Setup-staging.dmg`.
- A copy that is not in an Applications folder does not write the login-item plist.

## Local rebuild

On an Apple Silicon Mac, from the repository root:

```bash
bash macos/build.sh
bash macos/smoke.sh
```

Output: `dist/TileArc-AutoFill-Helper-Setup-staging.dmg`.
