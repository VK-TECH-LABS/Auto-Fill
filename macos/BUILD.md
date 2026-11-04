# macOS helper build (staging)

Employees on macOS install the disk image that matches their chip. They do not install Python. Each disk image contains **TileArc Auto-Fill Helper (STAGING).app**, the same loopback helper Windows freezes (`src/autofill/win_launcher.py` → `src/autofill/local_helper.py`). The app sets `AUTOFILL_LOCAL_ATTACH=1` in that process, listens on port **9410**, and uses a dedicated Chrome profile:

`~/Library/Application Support/TileArc/AutoFill/apply-profile`

It does not attach to daily Chrome (`~/Library/Application Support/Google/Chrome`). Final Submit stays human. The hosted companion remains the fallback when this helper is not running.

The app is ad-hoc signed so macOS can launch the bundled Playwright driver. It is **not** signed with an Apple Developer ID and it is **not** notarized. Gatekeeper steps are in `docs/MACOS_EMPLOYEE.md`.

GitHub Actions workflow: `.github/workflows/macos-helper.yml`. Two jobs build two thin disk images:

| Job | Runner | Architecture |
| --- | --- | --- |
| `Build Setup.dmg (arm64)` | `macos-latest` | Apple Silicon (`arm64`) |
| `Build Setup.dmg (x86_64)` | `macos-15-intel` | Intel (`x86_64`) |

`macos-13` was removed in December 2025, so the Intel job uses `macos-15-intel`. The Windows installer stays on `.github/workflows/windows-helper.yml`.

Playwright's driver `node` and native extensions such as greenlet are thin Mach-O binaries for the machine that runs the build. `lipo` cannot merge that tree into one universal app. The spec leaves `target_arch` unset. Each job freezes the helper on a runner of that architecture and refuses the disk image if any Mach-O file is fat or is the other architecture.

## Artifacts the Installer API should expose

| Item | Apple Silicon | Intel |
| --- | --- | --- |
| File name | `TileArc-AutoFill-Helper-Setup-staging-arm64.dmg` | `TileArc-AutoFill-Helper-Setup-staging-x86_64.dmg` |
| Who installs it | M1 or newer | Intel Macs, including a 2020 MacBook |
| App inside the disk image | `TileArc Auto-Fill Helper (STAGING).app` | `TileArc Auto-Fill Helper (STAGING).app` |
| Actions artifact name | `TileArc-AutoFill-Helper-Setup-staging-macos-arm64` | `TileArc-AutoFill-Helper-Setup-staging-macos-x86_64` |
| Architecture | `arm64` (`Build-arch.txt` on the disk is `arm64`) | `x86_64` (`Build-arch.txt` on the disk is `x86_64`) |
| Content type | `application/x-apple-diskimage` | `application/x-apple-diskimage` |
| Workflow | https://github.com/VK-TECH-LABS/Auto-Fill/actions/workflows/macos-helper.yml | same workflow, Intel job |
| Draft release tag | `tilearc-autofill-helper-staging` (same draft as the Windows exe) | same tag |
| Draft release | https://github.com/VK-TECH-LABS/Auto-Fill/releases/tag/tilearc-autofill-helper-staging | same draft |
| Public download URL (after the draft is published) | https://github.com/VK-TECH-LABS/Auto-Fill/releases/download/tilearc-autofill-helper-staging/TileArc-AutoFill-Helper-Setup-staging-arm64.dmg | https://github.com/VK-TECH-LABS/Auto-Fill/releases/download/tilearc-autofill-helper-staging/TileArc-AutoFill-Helper-Setup-staging-x86_64.dmg |

A push to `staging` runs `Add macOS assets to draft release`, which uploads both disk images onto the existing **draft** release tag `tilearc-autofill-helper-staging`. Release notes name the Apple Silicon asset and the Intel asset separately so a digests handoff can Priority-ping Setup with both hashes. The publish script prints `sha256sum` for each file and removes the retired unsuffixed asset `TileArc-AutoFill-Helper-Setup-staging.dmg` (that file was Apple Silicon only). It does not remove `TileArc-AutoFill-Helper-Setup-staging.exe`.

The workflow also uploads each Actions artifact on every pull request into `staging`. Draft assets are not an anonymous download. Publish that draft (keep the title marked STAGING) before the TileArc "Download Installer" button uses the public URLs above.

Choose the asset from the employee operating system and, on a Mac, from the chip. Suggested staging payload:

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
      "arm64": {
        "label": "Apple Silicon",
        "filename": "TileArc-AutoFill-Helper-Setup-staging-arm64.dmg",
        "arch": "arm64",
        "contentType": "application/x-apple-diskimage",
        "url": "https://github.com/VK-TECH-LABS/Auto-Fill/releases/download/tilearc-autofill-helper-staging/TileArc-AutoFill-Helper-Setup-staging-arm64.dmg"
      },
      "x86_64": {
        "label": "Intel",
        "filename": "TileArc-AutoFill-Helper-Setup-staging-x86_64.dmg",
        "arch": "x86_64",
        "contentType": "application/x-apple-diskimage",
        "url": "https://github.com/VK-TECH-LABS/Auto-Fill/releases/download/tilearc-autofill-helper-staging/TileArc-AutoFill-Helper-Setup-staging-x86_64.dmg"
      }
    }
  }
}
```

If the portal vendors the files instead of redirecting to GitHub, vendor both disk images beside the Windows exe and serve the one that matches the Mac. Do not point a Mac button at the Windows exe, a placeholder, a source zip, or the extension package. The extension zip stays in `vinayaka/fillo-extension`. See `docs/MACOS_EMPLOYEE.md`.

## What the smoke test checks

`macos/smoke.sh` runs in CI after the disk image is built, on that job's runner:

- `--doctor` finds the bundled Playwright driver (CDP only; Chrome itself is the employee's Google Chrome).
- The profile path is `~/Library/Application Support/TileArc/AutoFill/apply-profile`, not Chrome's daily directory.
- `GET http://127.0.0.1:9410/health` returns `status=ok`, `service=auto-fill-local`, `mode=dedicated-profile`, `submit=human`.
- The listener is `127.0.0.1:9410`, not `0.0.0.0`.
- The same checks pass for the app inside the architecture-specific disk image.
- `Build-arch.txt` matches `uname -m`, the disk readme names that architecture, and the helper binary is a thin Mach-O of that architecture.
- A copy that is not in an Applications folder does not write the login-item plist.

## Local rebuild

On the Mac you are packaging for, from the repository root:

```bash
bash macos/build.sh
bash macos/smoke.sh
```

Apple Silicon writes `dist/TileArc-AutoFill-Helper-Setup-staging-arm64.dmg`. Intel writes `dist/TileArc-AutoFill-Helper-Setup-staging-x86_64.dmg`.
