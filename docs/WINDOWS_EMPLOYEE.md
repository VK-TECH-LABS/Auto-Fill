# TileArc Auto-Fill Helper (staging)

This installs the staging helper on your Windows PC. You do not need Python or PowerShell.

On a Mac, install `TileArc-AutoFill-Helper-Setup-staging.dmg` instead. The steps are in [MACOS_EMPLOYEE.md](MACOS_EMPLOYEE.md).

Use this only for **TileArc staging**. It is not the production installer.

The helper runs on this computer at `http://127.0.0.1:9410`. It opens a **separate Chrome profile** for applications. It does not use your everyday Chrome, and it does not click Final Submit. You still submit the application yourself.

## Before you start

Install **Google Chrome** if it is not already installed. Microsoft Edge is not a substitute. Chrome has to be the normal Google Chrome app.

## Install

1. Download **TileArc-AutoFill-Helper-Setup-staging.exe** from TileArc (Download Installer) or from the staging release your team shared.
2. Run that Setup file.
3. If Windows says "Windows protected your PC":
   - Click **More info**
   - Click **Run anyway**

   Windows shows that because this staging build is not code-signed. Only do this for the file named `TileArc-AutoFill-Helper-Setup-staging.exe` from TileArc.
4. Finish the installer. It starts the helper and adds a Start menu item named **TileArc Auto-Fill Helper (STAGING)**. The helper also starts when you sign in to Windows.

You do not type any commands.

## Check that it is running

1. Open your browser.
2. Go to `http://127.0.0.1:9410/health`
3. The page should include `"status": "ok"` and `"service": "auto-fill-local"`.

If that page does not load, open the Start menu, run **TileArc Auto-Fill Helper (STAGING)**, and try the address again.

When the helper is running, TileArc staging can show the connection as **Connected**.

## Load the Chrome extension

The extension is **not** inside this installer.

1. In TileArc Resume Builder (staging), download the Chrome extension zip.
2. That zip is built from `vinayaka/fillo-extension`. Use the zip TileArc gives you. Do not copy a folder from this Auto-Fill project.
3. In Chrome, open `chrome://extensions`.
4. Turn on **Developer mode**.
5. Load the extension the way TileArc describes for that zip (Load unpacked after unzipping, if that is what the page says).

The extension talks to the helper at `http://127.0.0.1:9410`. It does not use a password for that local address.

## When you apply

- TileArc opens a Chrome window that belongs to the helper. That window has its own profile, stored under your user account in `TileArc\AutoFill\apply-profile`.
- Sign in to the employer in **that** window if the site asks you to. Your everyday Chrome stays signed in on its own and is not the window being filled.
- Read the form. The helper stops for questions it should not guess.
- **You click Final Submit.** The helper will not.

If the helper is not running, TileArc can still use the hosted companion. Installing this helper does not turn the companion off.

## Remove it

Windows Settings → Apps → **TileArc Auto-Fill Helper (STAGING)** → Uninstall.

This program is free software under the AGPL-3.0 license. Source code and the license text are in the [Auto-Fill repository](https://github.com/VK-TECH-LABS/Auto-Fill). The installer also copies `LICENSE` and `NOTICE` into the install folder.
