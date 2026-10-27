# T172 local same-tab PoC

Phase 1 answered which architecture can drive the same visible Chrome tab an employee is looking at for an employer Apply flow, using this repository's Auto-Fill engine, without a Companion JPEG stream, without a second employer login, without exposing Chrome DevTools to Railway or the public internet, and without clicking Final Submit. Phase 2 keeps only the path that worked.

The hosted Railway runner is unchanged. Companion is unchanged. Nothing here is deployed.

## Result

On Google Chrome **156.0.8078.47**, silent attach to the default profile fails. The path that works with the existing engine is a **dedicated Chrome profile**: non-default `--user-data-dir`, `--remote-debugging-port` bound to **127.0.0.1**, Playwright `connect_over_cdp`, and the browser's existing `contexts[0]` tab. That tab was filled with `autofill_application` / `AutofillOptions.page`. The cookie set in the tab was still there after a disconnect and reconnect, the target id did not change, and **Submit application was not clicked**.

That dedicated window is the tab the employee sees only if they are looking at that window. It is not their already-open daily Chrome, and it does not have daily Chrome's cookies.

An extension using `chrome.debugger` can read the same tab's cookie without a remote-debugging port. Branded Chrome 156 refuses `--load-extension`, so that install is a manual step. Chrome's `chrome://inspect/#remote-debugging` page offers "Allow remote debugging for this browser instance", and a cold start does not write `DevToolsActivePort` until someone turns that on.

## What this run did not change

- `src/autofill/service/runner.py` still launches headless Chromium with `browser.new_context()`. It does not import `autofill.local_attach` and it does not call `connect_over_cdp`.
- `HUMAN_SUBMIT_ONLY` is still true. There is still no switch that enables Final Submit.
- No Companion code was removed. No Railway config, Dockerfile, or service route was edited.
- The local driver runs only when `AUTOFILL_LOCAL_ATTACH=1`. The hosted process does not set that variable. Setting it on Railway does not switch the hosted runner to CDP. A test calls `_SharedDriver.browser()` with the variable unset and set to `1`; both calls are `chromium.launch(headless=True)` with the existing launch args and no debugging port.

## Phase 2

The working path is now a local helper, still behind the flag.

| Piece | Behavior |
| --- | --- |
| Driver | `DedicatedApplySession` in `src/autofill/local_attach.py`. Dedicated non-default profile, `--remote-debugging-address=127.0.0.1`, `connect_over_cdp`, existing `contexts[0]`. |
| Fill | `autofill_application` with `AutofillOptions.page` set to that tab. Clicks still go through `perform_click`. |
| Employer URL | http or https with a host. `file:`, `javascript:`, `chrome:`, and URLs with credentials are refused. The debugging port stays on `127.0.0.1`. |
| Helper | `python -m autofill.local_helper` or `autofill-local`. `ThreadingHTTPServer` bound to `127.0.0.1` only (`AUTOFILL_LOCAL_HELPER_PORT`, default 9410). |
| Status | `GET /health`, `POST /v1/sessions` (`applicationUrl`, `profile`), `GET /v1/sessions/{id}`, `POST /v1/sessions/{id}/answer`, `POST /v1/sessions/{id}/detach`. `READY_FOR_HUMAN_SUBMIT`, detach, and other refusals keep `stopped_before_submit: true`. A page that sets `window.__submitted` fails the job (`FAILED`, `submitted: true`) and that body sets `stopped_before_submit: false`. The flag is a report only: the helper still does not click Final Submit and does not mark the job applied. |
| Flag off | The helper exits 2 and does not bind. Railway's runner is a different process and still launches headless Chromium. |
| Idle detach | Stops the Playwright driver. It does not close the browser object and it does not kill Chrome, including a Chrome this process did not launch (`ownsBrowser` is false when the port was already open). |
| Visible window | The dedicated launch does not set a window size and does not call `_block_heavy`. The hosted runner still uses a 1280×800 context and request blocking. Headless is only for the CLI env `AUTOFILL_LOCAL_HEADLESS=1` and for tests. |
| Profile | `AUTOFILL_LOCAL_PROFILE_DIR`, or `~/.local/share/auto-fill/apply-profile`. The default Chrome directory is refused. |
| Token | The helper does not read `AUTOFILL_SERVICE_TOKEN`. The extension directory still has none. |

`POST /v1/sessions` returns 202 and runs the fill on one worker thread. Playwright's sync client is not shared across threads. A second fill while one is running returns 409. After the fill, an idle timer (`AUTOFILL_LOCAL_IDLE_DETACH_SECONDS`, default 30) detaches, unless the fill stopped on a question. The Chrome window stays so the employee can click Submit. Tests call `stop_launched_chrome` only in teardown, and only for a process this object started.

## Unknown questions

A profile fill that stops on `MANUAL_ANSWER_REQUIRED` puts the current question on `GET /v1/sessions/{id}`:

```json
{
  "question": {
    "id": "questionId, otherwise questionHash, otherwise the field key",
    "prompt": "What is your favorite color?",
    "type": "single_select",
    "options": ["Red", "Blue"]
  }
}
```

`id`, `prompt`, `type`, and `options` are the stable shape. `type` and `options` are the engine's `answerType` and option labels when the page has them. `options` is `[]` for free text. The selector, the profile, and any typed answer are not on this object. Login, captcha, account, verification, resume, and no-form stops are not questions. While `question` is present the idle timer waits, so the dedicated tab stays attached.

`POST /v1/sessions/{id}/answer` accepts that `id` (also `questionId`, `questionHash`, or `fieldKey`) and the employee's `value` or `answer`. It writes the value in the same Chrome tab, through the same visible commit the hosted runner uses, then reads the page again. The response is `{verified, filled, stopped_before_submit, status, ...}`. `filled` is true only when `verified` is true. `verified` is true only when the control holds the value after the write. The value is not echoed and is not logged.

Callers persist that answer as `confirmedByEmployee` only when `verified` is true. `verified: false` means the control did not keep it (`not_committed`, a wrong option, a stale id, or a consent box without `confirm`). Do not persist those. This helper does not store `confirmedByEmployee` and does not read `AUTOFILL_SERVICE_TOKEN`.

A verified answer that leaves another required field empty returns the next `question` and stays on `MANUAL_ANSWER_REQUIRED`. When nothing required is left, `question` is absent and the idle detach runs. The route never clicks Final Submit. A page that sets `window.__submitted` fails the answer (`FAILED`, `submitted: true`, `stopped_before_submit: false`). `READY_FOR_HUMAN_SUBMIT` and detach stay `stopped_before_submit: true`. The hosted Railway runner is a different process and is unchanged.

Phase 2 does not install the extension, does not click Allow on `chrome://inspect`, and does not attach to daily Chrome. Those phase-1 results are unchanged.

## Chrome under test

| Binary | Version | Role |
| --- | --- | --- |
| `/usr/bin/google-chrome-stable` | Google Chrome 156.0.8078.47 | Employee-shaped branded Chrome. Default-profile block, dedicated-profile fill, `--load-extension` refusal, inspect page. |
| Playwright Chrome for Testing | Google Chrome for Testing 156.0.8078.4 | Extension `chrome.debugger` proof only. Not daily Chrome. |

Chrome's own note for this class of change: from Chrome 136, `--remote-debugging-port` and `--remote-debugging-pipe` are ignored for the default data directory unless `--user-data-dir` points somewhere else ([Chrome developer blog](https://developer.chrome.com/blog/remote-debugging-port)). Chrome 144+ adds a separate, user-consent debugger at `chrome://inspect/#remote-debugging` ([Chrome DevTools agents](https://developer.chrome.com/blog/chrome-devtools-mcp-debug-your-browser-session)).

Raw probe output is `tools/local-cdp-poc/evidence/report.json`. The filled form is `tools/local-cdp-poc/evidence/dedicated-fill.png`. The Chrome profile directories from the run were deleted and are not in the commit.

## Attach modes

| Mode | What was launched | CDP opened | Same daily profile | Verdict |
| --- | --- | --- | --- | --- |
| Headed, no `--user-data-dir`, `--remote-debugging-port` | Branded Chrome, real default profile | No | Yes, the process was daily Chrome | **Fail.** Stderr: `DevTools remote debugging requires a non-default data directory. Specify this using --user-data-dir.` No `DevToolsActivePort`. |
| `--user-data-dir=$HOME/.config/google-chrome` plus the debugging port | Branded Chrome, explicit default directory | No | Yes | **Fail.** Same stderr. Passing the default path does not count as a non-default directory. |
| Headless, no `--user-data-dir` | Branded Chrome headless | Yes, `127.0.0.1` only | **No** | **False positive.** Headless uses a temporary data directory. This is not the employee's open daily Chrome. |
| Dedicated `--user-data-dir` + port `127.0.0.1` | Branded Chrome, fresh directory, headed for the fill | Yes, `127.0.0.1` only | No | **Works** for that window. See below. |
| `--load-extension` on branded Chrome | Branded Chrome 156, `--enable-logging=stderr` | n/a | A non-default dir was used so the port block was not the question | **Install refused.** `extension_service.cc`: `--load-extension is not allowed in Google Chrome, ignoring.` `--disable-features=DisableLoadExtensionCommandLineSwitch` did not change that. |
| Extension `chrome.debugger`, no debugging port | Chrome for Testing, headless, unpacked extension | No port was requested | No | **Mechanism works.** The extension posted `attached: true` and `cookie: t172_visible=same-tab` for `http://127.0.0.1/.../t172-local-tab.html`. |
| `chrome://inspect/#remote-debugging` with no debugging flag and no click | Branded Chrome, fresh directory | No | A fresh profile, same UI as daily Chrome | **Not attached.** `DevToolsActivePort` was absent. The page text, read from a dedicated session, is quoted below. |

`ss` showed the successful debugging ports as `127.0.0.1` only. The driver refuses any endpoint whose host is not `127.0.0.1`, and it refuses a listener on `0.0.0.0`.

## Dedicated profile (the path that fills)

Code: `src/autofill/local_attach.py`. The command is `python tools/local-cdp-poc/run_poc.py` with `AUTOFILL_LOCAL_ATTACH=1`.

What the headed fill did:

1. Served `tools/local-cdp-poc/fixture/application.html` on `127.0.0.1` only.
2. Launched branded Chrome with a fresh `--user-data-dir`, `--remote-debugging-address=127.0.0.1`, and `--remote-debugging-port` on a free loopback port. The page URL was the fixture.
3. `chromium.connect_over_cdp("http://127.0.0.1:<port>")`.
4. Used `browser.contexts[0]` and the existing fixture tab. `new_context()` is not on this path. If Chrome exposes no context, the driver raises and stops.
5. Read target id `4F5DC6455095CA0E0DAC83355EE418CC` and cookie `t172_visible=same-tab`.
6. Disconnected the Playwright client. The Chrome process stayed up.
7. Connected again. Same target id. Same cookie.
8. Called `autofill_application` with `AutofillOptions(page=that tab, max_pages=1)` and `examples/profile.example.json`.

Observed result:

| Check | Value |
| --- | --- |
| Status | `READY_FOR_HUMAN_SUBMIT` |
| Fields | 3 detected, 3 filled, 0 skipped |
| First / last / email | Casey / Example / casey.example@example.com |
| `window.__submitted` | false |
| `stopped_before_submit` | true |
| Submit control seen and not clicked | `Submit application` |
| Contexts | 1 |
| `created_new_context_for_fill` | false |
| Cookie after fill | `t172_visible=same-tab` |
| Listeners | `127.0.0.1` only |

The screenshot shows those three values in the form and the Submit application button still sitting there.

This reuses the engine. It does not reimplement field mapping.

## Extension inside Chrome

`tools/local-cdp-poc/extension/` is a Manifest V3 probe. It has `debugger` and `tabs`, and `host_permissions` for `http://127.0.0.1/*` only. The service worker attaches `chrome.debugger` to a tab whose URL contains `t172-local-tab`, evaluates `document.cookie`, and POSTs that to the fixture origin. It does not fill the form and it does not click. A test scans the extension directory for `AUTOFILL_SERVICE_TOKEN`. The string is not there.

Branded Chrome 156 will not load that directory from `--load-extension`. A person loads it from `chrome://extensions` (developer mode, Load unpacked). That is a consent step. Attaching `chrome.debugger` shows Chrome's debugging banner for the life of the attach. This run did not capture a pixel of that banner: it is browser chrome, outside the page screenshot. The extension does not give Playwright a `Page`. `autofill_application` was not driven through the extension. A product bridge would have to stay on loopback and still end in `perform_click`. That bridge is not in this PoC.

Chrome for Testing still honors `--load-extension`. On 156.0.8078.4, with no `--remote-debugging-port`, the probe reported the fixture cookie from the tab it attached to. That proves the extension can see the tab it runs in. It does not prove branded daily Chrome can be loaded from a flag.

## Chrome 144+ inspect consent

From the dedicated session, a second tab (same context, not a new context) opened `chrome://inspect/#remote-debugging`. The page text on Chrome 156 was:

> Allow remote debugging for this browser instance
>
> Turning on this setting allows external apps to request full control of this browser. This includes read access to your saved data, cookies and site data, and the ability to navigate to any URL.
>
> Only web developers should turn on this feature, and only use it with trusted apps.

A separate branded Chrome was started with no debugging flag, pointed at that URL, and left alone. `DevToolsActivePort` was not created. Nothing attached. This VM did not click Allow. Chrome's published 144+ flow still asks for that approval on each connection and shows the automation banner while a session is active. This PoC does not claim that flow was completed.

## Final Submit

`drive_control` is the only click entry in the local driver. It reads the control's own text and `type` from the DOM. A caller who labels a Submit button "Next" or "Save and continue" is still refused, because the DOM name is classified with `action_class`. These purposes are refused before any key or script is sent: `eval`, `form_submit`, `keyboard_enter`, `request_submit`. Every other purpose goes through `perform_click`, which raises `HumanSubmissionRequired` for a final Submit or Apply.

`tests/test_local_attach.py` loads a page whose Submit, Apply now, and Send application buttons set `window.__submitted`, and whose email field records Enter. It runs those labels under navigation, login, account, account_switch, the script purposes, and an unknown purpose. The flags stay false. A real "Save and continue" button still clicks, and that click does not submit. The dedicated-profile fill asserts `submitted` is false and `stopped_before_submit` is true. A reported submit is `FAILED` with `submitted` true and `stopped_before_submit` false. `READY_FOR_HUMAN_SUBMIT` and detach stay true.

There is no flag in this driver that turns Final Submit on.

## Cookies and a second employer session

The dedicated profile starts empty. The fixture cookie was created in that tab and was still there after reconnect, which shows the driver did not replace the tab or its cookie jar. It does not show daily-Chrome cookies. Those live under the default user-data-dir, and Chrome refused debugging there. This run did not copy the default profile into the debug directory. Chrome's 136 change is explicit that a non-default directory is a different store (a different encryption key), so copying the default folder is not a supported way to inherit the employee's logins.

If the employee is already signed into an employer in daily Chrome, a dedicated window that also signs in is a second session. The two windows can kick each other, or the person can look at the daily tab while the filler is typing in the other one. `browser.new_context()` would be a further split inside the dedicated Chrome: a jar that does not have the visible tab's cookies. The fill path does not call it.

The extension and the inspect-consent path run inside the Chrome that already has the cookies. They are the same session. They are also visible to the employee (install, Allow dialog, debugging banner).

## Recommendation for TileArc

Use a hybrid, and do not sell the half that Chrome blocks.

**Dedicated Apply window, when the employee can work in the window TileArc opened.** Keep using `autofill_application` on `contexts[0]` of a loopback CDP Chrome with a non-default `--user-data-dir`. Gate it with `AUTOFILL_LOCAL_ATTACH`. Leave the Railway headless runner as the server-side path it already is: that browser is a different machine and a different cookie jar, and it must not be given a debugging port. The employer login, if any, has to happen in the dedicated window. Do not also hold that login open in daily Chrome.

**Daily Chrome, when the employer session is already in the employee's normal profile.** Do not launch flags at that profile. Chrome 156 ignores them. The honest options are an extension the person installs, or the inspect-page Allow control the person turns on. Either one is consent, and either one shows a debugging banner. The extension must not contain the service token. The Python engine stays on the laptop. This PoC did not wire `chrome.debugger` into `AutofillOptions.page`; that is the next product step, still loopback-only, still through `perform_click`.

**Do not run both against one employer account.** Dedicated profile plus daily Chrome is two sessions.

## What cannot be promised

- Silent control of an already-open daily Chrome, with no extension, no Allow click, and no debugging banner.
- That a dedicated debug profile is the employee's daily profile, or that it contains that profile's cookies.
- That headless Chrome opening a port proves the daily profile is debuggable. It does not.
- That `--load-extension` installs anything on current branded Chrome.
- That Railway, or any non-loopback host, can attach to the employee's tab.
- That Auto-Fill will click Final Submit. It will not.

## Licensing

This change is new source in this repository. The repository is AGPL-3.0-only (`LICENSE`, `NOTICE`). No ApplyPilot file was copied. The fill used `examples/profile.example.json`. That file's section names were already adapted from ApplyPilot and are already described in `NOTICE` as a clean-room reimplementation of behavior, not a copy of ApplyPilot's agent prompt. Nothing in `tools/local-cdp-poc/` or `src/autofill/local_attach.py` imports ApplyPilot. Shipping this driver as part of the Auto-Fill program keeps it under the same AGPL-3.0-only terms. This note is not a new license and it is not advice to relicense TileArc.

## Re-run

```bash
pip install -e ".[dev]"
python -m playwright install chromium
export AUTOFILL_LOCAL_ATTACH=1
export T172_RUN_CHROME_POC=1   # also probes the real default profile
pytest tests/test_local_attach.py tests/test_local_helper.py
python tools/local-cdp-poc/run_poc.py
python -m autofill.local_helper
```

`autofill-local` is the same entry. It listens on `http://127.0.0.1:9410` unless `AUTOFILL_LOCAL_HELPER_PORT` is set. Without `AUTOFILL_LOCAL_ATTACH=1` it exits 2. Railway does not run this command.

Default `pytest` does not launch Chrome. `T172_RUN_CHROME_POC=1` enables the probes that do: the daily-profile port refusal, the dedicated-profile fill, the branded `--load-extension` refusal, the Chrome-for-Testing extension cookie check, and the helper integration fill. Do not set it on a machine whose daily Chrome you cannot interrupt. The daily-profile probe is the only one that touches the real default user-data-dir. CI does not set the variable. The flag-off headless-launch test fakes Playwright and always runs.
