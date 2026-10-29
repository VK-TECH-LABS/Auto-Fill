# TileArc Auto-Fill Helper for Mac (staging)

This installs the staging helper on your Mac. You do not need Python, and you do not install the Windows Setup file.

Use this only for **TileArc staging**. It is not the production installer. The disk image is built for **Apple Silicon** Macs (M1 or newer). An Intel Mac uses the hosted TileArc companion for this staging build.

The helper runs on this computer at `http://127.0.0.1:9410`. It opens a **separate Chrome profile** for applications. It does not use your everyday Chrome, and it does not click Final Submit. You still submit the application yourself.

## Before you start

Install **Google Chrome** if it is not already installed. Chrome has to be the normal Google Chrome app from the Applications folder.

## Install

1. Download **TileArc-AutoFill-Helper-Setup-staging.dmg** from TileArc (Download Installer) or from the staging release your team shared.
2. Open the disk image.
3. Drag **TileArc Auto-Fill Helper (STAGING)** onto the Applications folder.
4. Eject the disk image.
5. Open your Applications folder. **Right-click** the app and choose **Open**.
6. Click **Open** when macOS asks you to confirm.

   macOS shows that warning because this staging build is not signed with an Apple Developer ID and is not notarized. Only do this for the app named **TileArc Auto-Fill Helper (STAGING)** from TileArc.

7. If macOS says the developer cannot be verified, or that the app is damaged and should be moved to the Trash:
   - Open the Apple menu → **System Settings** → **Privacy & Security**.
   - Click **Open Anyway** next to TileArc Auto-Fill Helper (STAGING), then **Open**.
   - When the message says the app is damaged, the download was blocked by Gatekeeper. In Terminal, run:

     `xattr -dr com.apple.quarantine "/Applications/TileArc Auto-Fill Helper (STAGING).app"`

     Then right-click the app and choose **Open** again.

You do not type any other commands. The app has no setup window. While it is running it appears in the Dock.

The first time you open the app from Applications, it also registers itself to start when you sign in to the Mac.

## Check that it is running

1. Open your browser.
2. Go to `http://127.0.0.1:9410/health`
3. The page should include `"status": "ok"` and `"service": "auto-fill-local"`.

If that page does not load, open the app from Applications again and try the address again.

When the helper is running, TileArc staging can show the connection as **Connected**.

## Load the Chrome extension

The extension is **not** inside this disk image.

1. In TileArc Resume Builder (staging), download the Chrome extension zip.
2. That zip is built from `vinayaka/fillo-extension`. Use the zip TileArc gives you. Do not copy a folder from this Auto-Fill project.
3. In Chrome, open `chrome://extensions`.
4. Turn on **Developer mode**.
5. Load the extension the way TileArc describes for that zip (Load unpacked after unzipping, if that is what the page says).

The extension talks to the helper at `http://127.0.0.1:9410`. It does not use a password for that local address.

## When you apply

- TileArc opens a Chrome window that belongs to the helper. That window has its own profile, stored in your home folder under `Library/Application Support/TileArc/AutoFill/apply-profile`.
- Sign in to the employer in **that** window if the site asks you to. Your everyday Chrome stays signed in on its own and is not the window being filled.
- Read the form. The helper stops for questions it should not guess.
- **You click Final Submit.** The helper will not.

If the helper is not running, TileArc can still use the hosted companion. Installing this helper does not turn the companion off.

## Remove it

1. Quit **TileArc Auto-Fill Helper (STAGING)** from the Dock.
2. Drag the app from Applications to the Trash.
3. Move this file to the Trash if it is there:

   `Library/LaunchAgents/com.vktechlabs.tilearc.autofill.helper.staging.plist`

   That path is inside your home folder.

This program is free software under the AGPL-3.0 license. Source code and the license text are in the [Auto-Fill repository](https://github.com/VK-TECH-LABS/Auto-Fill). The app also includes `LICENSE` and `NOTICE`.
