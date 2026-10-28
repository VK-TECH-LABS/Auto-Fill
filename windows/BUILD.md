# Windows helper build (staging)

Employees install `TileArc-AutoFill-Helper-Setup-staging.exe`. They do not install Python. The frozen app is the existing loopback helper (`src/autofill/local_helper.py`) with `AUTOFILL_LOCAL_ATTACH=1` set in that process, port **9410**, and a dedicated Chrome profile (`%LOCALAPPDATA%\TileArc\AutoFill\apply-profile`). It does not attach to daily Chrome. Final Submit stays human.

GitHub Actions workflow: `.github/workflows/windows-helper.yml` (`windows-latest`).

## Artifact vinayaka should link

| Item | Value |
| --- | --- |
| File name | `TileArc-AutoFill-Helper-Setup-staging.exe` |
| Actions artifact name | `TileArc-AutoFill-Helper-Setup-staging` |
| Workflow | https://github.com/VK-TECH-LABS/Auto-Fill/actions/workflows/windows-helper.yml |
| Draft release tag | `tilearc-autofill-helper-staging` |
| Draft release | https://github.com/VK-TECH-LABS/Auto-Fill/releases/tag/tilearc-autofill-helper-staging |
| Public download URL (after the draft is published) | https://github.com/VK-TECH-LABS/Auto-Fill/releases/download/tilearc-autofill-helper-staging/TileArc-AutoFill-Helper-Setup-staging.exe |

The workflow uploads the Actions artifact on every pull request into `staging` and on every push to `staging`. A push to `staging` also creates or updates a **draft** GitHub release with that exact asset name. Draft assets are not an anonymous download. Publish that draft (keep the title marked STAGING) before the TileArc "Download Installer" button uses the public URL above.

Do not point the button at a placeholder, a source zip, or the extension package. The extension zip stays in `vinayaka/fillo-extension` and is loaded separately. See `docs/WINDOWS_EMPLOYEE.md`.

## What the smoke test checks

`windows/smoke.ps1` runs in CI after the installer is built:

- `--doctor` finds the bundled Playwright driver (CDP only; Chrome itself is the employee's Google Chrome).
- The profile path is not Chrome's User Data directory.
- `GET http://127.0.0.1:9410/health` returns `status=ok`, `service=auto-fill-local`, `mode=dedicated-profile`, `submit=human`.
- The listener is `127.0.0.1:9410`, not `0.0.0.0`.
- The same checks pass for the exe installed by a silent run of `TileArc-AutoFill-Helper-Setup-staging.exe`.

## Local rebuild

On a 64-bit Windows machine, from the repository root:

```powershell
powershell -ExecutionPolicy Bypass -File windows/build.ps1
powershell -ExecutionPolicy Bypass -File windows/smoke.ps1
```

Output: `dist/TileArc-AutoFill-Helper-Setup-staging.exe`.
