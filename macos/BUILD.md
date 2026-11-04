# macOS helper build (staging)

Employees on macOS install `TileArc-AutoFill-Helper-Setup-staging.dmg`. They do not install Python. The disk image contains **TileArc Auto-Fill Helper (STAGING).app**, the same loopback helper Windows freezes (`src/autofill/win_launcher.py` → `src/autofill/local_helper.py`). The app sets `AUTOFILL_LOCAL_ATTACH=1` in that process, listens on port **9410**, and uses a dedicated Chrome profile:

`~/Library/Application Support/TileArc/AutoFill/apply-profile`

It does not attach to daily Chrome (`~/Library/Application Support/Google/Chrome`). Final Submit stays human. The hosted companion remains the fallback when this helper is not running.

The app is ad-hoc signed so macOS can launch the bundled Playwright driver. It is **not** signed with an Apple Developer ID and it is **not** notarized. Gatekeeper steps are in `docs/MACOS_EMPLOYEE.md`.

GitHub Actions workflow: `.github/workflows/macos-helper.yml` (`macos-latest`). The runner is Apple Silicon. The disk image is still Universal: PyInstaller is set to `target_arch="universal2"`, and `macos/universal_node.py` joins the arm64 and x86_64 Playwright `node` binaries with `lipo`. Leaving `target_arch` unset thins PyInstaller's fat bootloader to the runner, which is why the previous image was arm64-only and an Intel Mac showed the prohibitory icon ("not supported on this Mac"). The Windows installer stays on `.github/workflows/windows-helper.yml`.

## Artifact the Installer API should expose

| Item | Value |
| --- | --- |
| File name | `TileArc-AutoFill-Helper-Setup-staging.dmg` |
| App inside the disk image | `TileArc Auto-Fill Helper (STAGING).app` |
| Actions artifact name | `TileArc-AutoFill-Helper-Setup-staging-macos` |
| Architecture | `universal2` (Intel `x86_64` and Apple Silicon `arm64` in one Mach-O). `Build-arch.txt` on the disk lists `universal2`, `x86_64`, and `arm64`. |
| Content type | `application/x-apple-diskimage` |
| Workflow | https://github.com/VK-TECH-LABS/Auto-Fill/actions/workflows/macos-helper.yml |
| Draft release tag | `tilearc-autofill-helper-staging` (same draft as the Windows exe) |
| Draft release | https://github.com/VK-TECH-LABS/Auto-Fill/releases/tag/tilearc-autofill-helper-staging |
| Public download URL (after the draft is published) | https://github.com/VK-TECH-LABS/Auto-Fill/releases/download/tilearc-autofill-helper-staging/TileArc-AutoFill-Helper-Setup-staging.dmg |
| Release asset digest | GitHub Releases API `assets[].digest` for that filename, shaped `sha256:<64 hex chars>`. `macos/publish-draft.sh` prints it after upload. `--clobber` replaces the bytes of the same name, so the digest changes and the previous arm64-only digest no longer matches. The Windows exe keeps the name `TileArc-AutoFill-Helper-Setup-staging.exe` and its own digest. |

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
      "arch": "universal2",
      "architectures": ["x86_64", "arm64"],
      "labels": ["Intel", "Apple Silicon"],
      "minOs": "13.5",
      "contentType": "application/x-apple-diskimage",
      "url": "https://github.com/VK-TECH-LABS/Auto-Fill/releases/download/tilearc-autofill-helper-staging/TileArc-AutoFill-Helper-Setup-staging.dmg"
    }
  }
}
```

If the portal vendors the file instead of redirecting to GitHub, vendor `TileArc-AutoFill-Helper-Setup-staging.dmg` beside the Windows exe and serve that one file for every Mac client (Intel and Apple Silicon). Record the new `sha256` digest from the draft release asset. Do not point the Mac button at the Windows exe, a placeholder, a source zip, or the extension package. The extension zip stays in `vinayaka/fillo-extension`. See `docs/MACOS_EMPLOYEE.md`.

The helper does not bundle Chrome, Chromium, or ChromeDriver. Google Chrome stays the employee's app. The native pieces inside the disk image are the PyInstaller bootloader, the universal2 Python extensions (including greenlet's `macosx_11_0_universal2` wheel), and Playwright's `node` driver. `macos/universal_node.py` downloads `macosx_11_0_arm64` and `macosx_10_13_x86_64` Playwright wheels and joins those `node` binaries with `lipo`. The PyPI wheel tagged `macosx_11_0_universal2` is an x86_64 node with a universal filename, so the build refuses it. Both node slices require macOS 13.5.

## What the smoke test checks

`macos/smoke.sh` runs in CI after the disk image is built:

- Every Mach-O file in the app contains `x86_64` and `arm64`. On Apple Silicon, `arch -x86_64` runs `--doctor`, the health server, and Playwright's `node` (`process.arch` is `x64`).
- `--doctor` finds the bundled Playwright driver (CDP only; Chrome itself is the employee's Google Chrome).
- The profile path is `~/Library/Application Support/TileArc/AutoFill/apply-profile`, not Chrome's daily directory.
- `GET http://127.0.0.1:9410/health` returns `status=ok`, `service=auto-fill-local`, `mode=dedicated-profile`, `submit=human`.
- The listener is `127.0.0.1:9410`, not `0.0.0.0`.
- The same checks pass for the app inside `TileArc-AutoFill-Helper-Setup-staging.dmg`.
- A copy that is not in an Applications folder does not write the login-item plist.

## Local rebuild

On a Mac, from the repository root. `macos/build.sh` installs Python 3.12.10 universal2 from python.org when `/Library/Frameworks/Python.framework/Versions/3.12/bin/python3` is not already a universal2 binary. That install uses `sudo`. On Apple Silicon it also installs Rosetta so the Intel slice can be executed.

```bash
bash macos/build.sh
bash macos/smoke.sh
```

Output: `dist/TileArc-AutoFill-Helper-Setup-staging.dmg`.

`macos/smoke.sh` checks every Mach-O file for `x86_64` and `arm64`, runs the bundled Node binary on each slice this Mac can execute, and calls `http://127.0.0.1:9410/health`. On Apple Silicon that includes an `arch -x86_64` launch of the helper.

## Human check

On an Intel Mac (macOS 13.5 or newer, such as a 2020 MacBook Pro) and on an Apple Silicon Mac:

1. Download `TileArc-AutoFill-Helper-Setup-staging.dmg` from draft tag `tilearc-autofill-helper-staging`.
2. Open the disk image. The app icon opens with the disk. macOS does not show the prohibitory slash or "not supported on this Mac".
3. `Build-arch.txt` lists `universal2`, `x86_64`, and `arm64`.
4. Drag the app to Applications, right-click Open, then visit `http://127.0.0.1:9410/health`. The page includes `"status": "ok"` and `"service": "auto-fill-local"`.
5. The Windows download is still `TileArc-AutoFill-Helper-Setup-staging.exe`. This workflow does not upload that file.
