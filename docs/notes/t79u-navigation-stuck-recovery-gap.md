# t79u item 6 — navigation, stuck detection, and recovery (gap map)

Staging tip this map was written against: `0e21166` (checkbox/radio PR #96).

This document is a map only. It does not change fill behavior.

Item 6 is the navigation half of Req5. Local continue/reconnect already shipped in #93 (`18553f2`). The vinayaka Continue control already shipped in vinayaka #135. This map ranks what is still open for stuck detection, page-advance, recovery after a disconnect, and avoiding a second Next or Apply. It covers the hosted runner and the local helper. Both call `autofill_application`.

Question-mapping PR #97 (`cursor/question-mapping-reuse-f983`) and the one-popup map #98 (`cursor/one-save-continue-popup-8af2`) are open and may merge onto this tip. Neither one owns Next, stall, or reconnect. Overlaps are called out per slice. Do not block this map on them. Implement on whichever commit is the staging tip, and rebase if either lands first.

## Non-goals

- Final Submit stays human. `HUMAN_SUBMIT_ONLY` in `src/autofill/safeguards.py` stays true. No Submit, Send, or Complete click, and no review-page bypass.
- Companion stays paused. This repo does not draw the TileArc Apply panel and does not edit vinayaka. #135 already shows Continue. This map only says which Auto-Fill status that button should be allowed to call.
- Staging only. No `main`, no production, no Jobs V2.
- Do not rebuild #93. Keep one CDP reconnect per process, the continue cap of 2, `DevToolsActivePort` pinning, resume bytes across idle detach, and `adopt_employer_tab` refusing to rewind a later path of the same application. Keep passing binding ids into `AutofillOptions`.
- Do not persist profiles, passwords, resume bytes, or nav blocks to disk. A helper restart does not become a journal.
- Do not attach Chrome's default user-data-dir. The dedicated Apply window stays the only local target.
- Do not raise `_CONTINUE_CAP` above 2, and do not spend `browser_restarts` on a stall. A stall is not a crash.
- Account create, sign-in, and email-verification Continue stay on their routes (#94, R4). `verified: true` still means the person opened the verification email.
- Cookie-banner dismiss stays in `dismiss_cookie_banner`. It is not a page turn.
- #98 owns the one Save & Continue card and the extract skip for `#tilearc-answer-popup`. This map only says a fill must not treat that card as the employer's Next. Do not implement the card here.
- #97 owns the question catalogue, employer scope, and option equivalents. Rebase `engine.py` if that PR merges. Do not change matching here.
- The legacy loop in `src/autofill/fill.py` (`_fill_pages`) is not the direct-browser or hosted path. Leave it. The service and the helper call `autofill_application`.
- `max_pages` stays 8 on the direct path and 12 on the resolver path. Slice 8 says what the loop does on the last landing. It does not raise the cap.

## What already works

| Piece | Where | What it does |
| --- | --- | --- |
| One dead Next or Apply inside a run | `NavigationStalled`, `_activate_forward`, `_follow_entry` in `src/autofill/engine.py` | A click that does not move the page raises `navigation_stalled`. That call does not click the same control again. Entry clicks stop at 2. |
| Click counters | `_log_move`, `RunStatusOut` | Hosted status returns `entryClicks`, `nextClicks`, `pagesAdvanced`, and `lastNavigation` (`{action, result}`). Totals accumulate across later runs through `nav_base_*` in `src/autofill/service/app.py`. The payload has no labels, URLs, or candidate values. |
| Local continue after a crash or `FAILED_RETRYABLE` | `POST /v1/sessions/{id}/continue`, same-binding Apply again, `_can_continue` | Reattaches, `fresh` stays false, passes `nav_block`, stops after two continues. A different candidate still reloads. |
| One CDP reconnect | `DedicatedApplySession._reconnect_cdp` | A dead page drops the client and connects again on the same port, once. Startup reads `DevToolsActivePort` and does not launch a second Chrome. |
| Progress gate | `LocalProgress`, `_progress_allows` | Continue types nothing when the live target id, document URL, or binding does not match the recorded page. |
| Hosted crash budget | `_allow_browser_restart`, `PlaywrightRunner._recover_driver` | A crashed browser can be started again twice. `navigation_stalled` does not spend that budget (`test_navigation_stalled_is_retryable_without_a_crash_budget`). |
| New tab during a hosted run | `on_page=_bind_page` in `PlaywrightRunner._options` | An Apply that opens a tab becomes the session page for the rest of that run. |

## Rank

| Rank | Gap | Stuck | Advance | Recovery | Double-nav | Surface |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | Next block is stored against the page the click landed on |  | yes | yes | yes | both |
| 2 | A stall cannot continue locally, and hosted retry reloads the posting | yes |  | yes | yes | both |
| 3 | A slow step is a stall after 1.5s | yes | yes |  |  | both |
| 4 | Answering the last field does not share one page-turn rule |  | yes |  | yes | both |
| 5 | The helper keeps the posting tab when Apply opened another tab |  | yes | yes | yes | local |
| 6 | Continue rejects the path the click just opened |  |  | yes |  | local |
| 7 | A repeated field set or a stale banner ends as filled or manual | yes |  | yes |  | both |
| 8 | The page landed on by the last allowed Next is not filled |  | yes | yes |  | both |
| 9 | A second pipe drop and a helper restart lose the block; local status hides the counters |  |  | yes | yes | local |

## What a page, a stall, and a block mean

A **page** is one settled application step: `navigation_signature` in `src/autofill/pagewait.py` (route including the fragment, the visible-field set, and the heading). `navigation_changed` treats a route change, a heading change, a new field set with no validation alert, or a replacement of most of the old fields as an advance. A validation redraw that keeps the same fields is the same page.

`PageMark` in `src/autofill/follow.py` is coarser. It is the URL, `page_step`, and the selector set. `page_step` (`src/autofill/adapters.py`) returns `application` when the heading does not name a step. Workday often keeps one URL across My Information, Experience, and Questions. Those screens are different pages when the heading or the field set changes. The answer-follow path does not use that rule today (slice 4).

A **stall** is one forward click, Apply or Next, whose signature did not change before the wait ended, and which was not a validation alert or a captcha. The public status is `NAVIGATION_STALLED` with message `navigation_stalled`. The same control is not clicked again inside that call.

A **block** is `(selector, name, signature)`. `_next_button` skips a forward control with that selector and name while the current signature equals the stored one. Submit, Send, and Complete stay unclicked either way. The block is how a continue avoids pressing the Next that already ran. Slice 1 is about which signature is stored.

## Current behavior

### 1. The in-run block uses the destination signature

After a Next that counts as a move, both loops store the button that was clicked together with the signature of the page that is now showing:

- Direct path: `src/autofill/engine.py` around the assignment after `_activate_forward` (the `nav_block = (nxt.selector, nxt.name, navigation_signature(...))` that follows the validation check).
- Resolver path: the same assignment at the end of the attempt loop. That `break` only ends the three reveal attempts. The page loop then continues.

On the next iteration the current signature is that destination. The block matches. The iteration still fills the destination. `_next_button` then skips any forward control with the same selector and name. A step that still has required gaps returns manual before that check, which is a normal stop. A step the profile can finish has its own Next skipped, the loop breaks, and the run ends as filled or ready on that step. It does not raise `navigation_stalled`. Local continue is not offered (slice 2). `pagesAdvanced` stays 1.

Workday's step button is `[data-automation-id='bottom-navigation-next-button']` with the name Save and Continue on every step (`selectorFor` in `src/autofill/extract.py` prefers `data-automation-id` when the node has no id). That is the stable pair.

The fixtures hide this. `tests/fixtures/eight_page_application.html` gives every Next a different id. `tests/fixtures/multistep_pages.html` uses `#forward` for both steps but changes the name from Next to Save & Continue, so the `(selector, name)` pair does not match. `test_multistep_next_then_review_stops_before_submit` expects both clicks. `test_recorded_next_is_not_pressed_again` builds the block with the signature of the page the button is on, before any click, and expects zero clicks. The publisher writes the signature after the click. Those two conventions disagree. `test_dropped_second_page_continues_without_clicking_next_again` only asserts that the label Next was not pushed a second time. Page 2's button has a different name, so the test still passes if that button is skipped or pressed.

The resolver loop initializes `nav_block` to `None` and never reads `opts.nav_block`. `PlaywrightRunner._options` does not pass `nav_block` or `on_nav_block`. Hosted recovery cannot honor a block even after slice 1 fixes the key. Local continue passes `job.progress.nav_block` only when `recover` is true (`src/autofill/local_helper.py`, `_run_fill`).

### 2. Local continue ignores a stall, and the two hosted doors disagree

`_can_continue` is true for `state == "error"` or a done job whose result status is `FAILED_RETRYABLE`. A finished `NAVIGATION_STALLED` is `state == "done"` with that status in the result, so it is false. `POST /v1/sessions/{id}/continue` returns `not_continuable`. Apply again with the same binding returns the existing job and does not run (`LocalHelper.submit`).

Hosted `POST /start` allows `NAVIGATION_STALLED` (`_STARTABLE`) and does not discard the page. The runner reuses `slot.page` when it is still open, then runs the engine with no block, so the stalled Apply or Next is clicked again. Each start can add one more click. `navigation_stalled` is in the spared set inside `_allow_browser_restart`, so this does not hit the crash cap.

Hosted `POST /retry` also allows `NAVIGATION_STALLED`, then calls `active_runner.discard` before `_run`. `PlaywrightRunner.discard` closes the context. The next `slot.open` builds a fresh context and `page.goto(application_url)`. That is the posting URL, not the step the stall happened on. Entry can click Apply again. Cookies from the closed context are gone. `tests/test_service.py` locks only the fake runner: retry stays allowed and does not spend the crash budget. It does not open a browser.

`POST /continue` on the hosted service does not list `NAVIGATION_STALLED` in either the `humanResolved` set or the machine set. The vinayaka Continue button (#135) calls the local helper's continue. A stalled local job refuses that call.

### 3. The movement wait is 1.5 seconds

`wait_for_navigation` polls until `navigation_changed` or `timeout_s` (default 1.5). `_confirm_move` uses that default. `wait_for_render`, used before an extract, already waits up to 12 seconds for controls. A Workday Save and Continue that is still spinning at 1.5 seconds is `result=stalled`, `nextClicks` increments, and the call returns `NAVIGATION_STALLED`. The button is not tried again in that call, which is the right single-click rule. The page often moves a moment later. The next recovery then either refuses (local) or clicks again / reloads (hosted). That second click is the double-nav. The short wait is what created the stall.

A route or heading change that also matches `validation_message` and `invalid_field_messages` is logged as `invalid` and returned as `MANUAL_ANSWER_REQUIRED`, not as an advance (`_activate_forward`). A stale alert on a step that really changed can hold the run on the new step without filling it. Slice 7 covers that classification. Slice 3 only lengthens the wait until the signature settles.

### 4. The answer path and the engine do not share a page turn

`follow_answer` (`src/autofill/follow.py`) asks for a full fill only when `_page_changed` is true: the URL string changed, `page_step` changed, or the new page has no selectors. A new field set on the same URL with a heading `page_step` still calls `application` is not a page change. New controls are filled by `_fill_new`. The follow result stays on this step.

Hosted `advance_after_answer` (`src/autofill/service/runner.py`) runs the engine only when `follow.changed` is true. When the last required field is filled and the mark did not change, it returns the follow status. `FILLED` and `READY_FOR_HUMAN_SUBMIT` both come back with no Next click. The employer's Save and Continue stays unpressed. A later hosted continue is `not_at_human_stop` for `FILLED`.

Local `_follow_answer` (`src/autofill/local_attach.py`) does press Next in that case: status `FILLED` and no open items calls `_continue_fill`, which passes `nav_block`. `READY_FOR_HUMAN_SUBMIT` does not. That split is the right submit boundary. The hosted answer path does not have the `FILLED` branch.

`resume_action` on the same class calls `fill_visible_tab` after a verified upload and does not pass `nav_block` or `on_nav_block`. A resume retry can press the Next the fill already pressed.

`_continue_fill` and the first `fill()` do pass the block. Slice 1 decides whether that block still suppresses the destination button. Slice 4 only adds the missing hosted branch and the missing resume arguments, using the same `_activate_forward` the engine already uses. One signature gets one Next. A follow that already set `changed` and ran the engine must not also click Next for the signature the engine just left.

#98's page set is the list of questions. This slice runs only when that set is empty. If #98 lands first, "empty" means the page set from that map, not a second scan that invents a new card.

### 5. The helper does not follow the tab Apply opened

`_adopt_page` in the engine calls `opts.on_page`. The hosted runner sets that to `_bind_page`, so `slot.page` becomes the new tab. `fill_visible_tab` does not set `on_page`. `DedicatedApplySession.tab` stays on the tab it adopted at `ensure`. After the run, `_optional_submitted` and `_remember_mark` read `tab.page`, which is still the posting. The engine's result came from the other tab.

The next `ensure` calls `adopt_employer_tab`. That function returns the first tab whose path equals the Apply URL before it considers a later path on the same host. The posting matches. Continue and the next fill run on the posting and can click Apply again. `tests/test_navigation_harness.py` (`test_apply_opens_a_new_tab`) covers the engine with a page object that is already the only page the test holds. It does not cover the helper's tab pointer.

### 6. The progress check is stricter than the click

`_capture_progress` runs from `on_observed`, which the engine calls at the start of a page, before Next. The stored URL is the page about to be filled.

`_progress_allows` then requires `same_document`: same scheme, host, port, and path. The fragment and query are ignored. A crash after Next changed the path, and before the next observe, stores the old path. Continue returns `progress_mismatch` and types nothing. The step is the one the click opened. `adopt_employer_tab` would have kept that later path. The continue gate rejects it.

A target-id change from slice 5 fails the same gate. Binding mismatch still has to fail it.

### 7. Some stops look finished

Both page loops break when the visible selector tuple is already in `seen`, then fall through to `inspect_required` and `_terminal_status`. A second step that reuses the same ids, or a redraw that did not change the field set, becomes `FILLED` or `READY_FOR_HUMAN_SUBMIT` rather than `NAVIGATION_STALLED`. Local continue will not run. Hosted retry will not offer. No click counter says the loop gave up.

`_activate_forward` returns `invalid` when the signature changed, the route is the same, a validation phrase matched, and `invalid_field_messages` found a node. The phrases in `validation_message` include `invalid` and `is required`. A banner that survived a real step change is reported as `MANUAL_ANSWER_REQUIRED`. The new step is not filled. Nothing clicks Next a second time, which stays correct. The status is a field stop, so the person is asked to type instead of continuing the step.

### 8. The cap fills eight pages and then inspects a ninth

`fill_visible_tab` sets `max_pages=8`. The hosted direct path uses 8. The resolver path uses 12. The loop body fills the current page and then clicks Next. After the last iteration the code below the loop extracts whatever Next just opened and inspects it. That page is not passed through `fill_one_page`. Empty profile fields on it become manual gaps. The status is not a distinct budget stop, so continue does not run, and the counters look like a normal finish.

A Workday sequence of My Information, contact, address, experience, education, websites, questions, voluntary disclosures, and review is longer than eight fills. The early stop is this cap, not a stall.

### 9. The second disconnect and a process restart have no block to restore

`_reconnect_cdp` allows one `connect_over_cdp` per process. The second dead page raises `CDP reconnect limit reached`. Continue cannot run. Chrome is still listening. Detach does not increment the counter. A new process starts at zero, and `win_launcher.py` pins `AUTOFILL_LOCAL_CDP_PORT` from `DevToolsActivePort` (#93).

The job, `nav_block`, continue count, and in-memory credentials die with the process. Vinayaka Continue calls `POST /continue` and gets 404. A new Apply creates a new job, `fresh` is false because `_window_used` is also new, and adopt keeps a non-posting later path. There is no block, so the engine fills the open step and clicks its Next once. That single click is right when the open step has not been advanced yet. It is a second click when the dead process had already pressed Next and the page had not settled (slice 3), or when slice 1's destination block would have skipped a stable button that still needed to be pressed on the new step.

`_result_brief` (`src/autofill/local_attach.py`) copies status and field counts only. The helper status has no `entryClicks`, `nextClicks`, `pagesAdvanced`, or `lastNavigation`. Each engine call starts its own record at zero, and the job does not add them the way `nav_base_*` does. #135 cannot show the hosted counters for a local fill.

## Slices

Implement in this order. Each slice is one change set. Later slices assume the earlier key and the stall door exist.

### 1. Key the block to the page the click happened on

**Gap.** A stable Save and Continue is ineligible on the step it just opened, in the same run and on continue. The harness test that freezes a button uses the pre-click signature. The publisher stores the post-click signature.

**Steps.**

1. Store `(selector, name, signature_before_click)`. `_activate_forward` already computes `before`.
2. On the next decision, clear the block when `current_sig` differs from that before-signature. `_next_button` then sees the destination's button, including a Workday button with the same automation id and the same name.
3. A continue that is still on the before-signature skips that pair. One click per signature. Do not add a second skip list.
4. Pass `opts.nav_block` into the resolver loop the same way `_initial_nav_block` does for the direct loop. `PlaywrightRunner._options` passes `nav_block` and `on_nav_block` once the session has one to give (slice 2 stores it). Until slice 2, an absent block stays `None`.
5. Update `test_dropped_second_page_continues_without_clicking_next_again` so the recorded block is the before-signature. Assert the destination's own forward control still runs once, and Submit does not.

**Files.** `src/autofill/engine.py` (`_next_button`, both loops, `_initial_nav_block`), `src/autofill/service/runner.py` (`_options`), `tests/test_navigation_harness.py`.

**#97.** The catalogue edits in `engine.py` are above these lines. Rebase. Do not change lookup.

**#98.** No overlap. The card skip is extract, not the block key.

**Done when.** A two-step fixture whose only forward control is `[data-automation-id='bottom-navigation-next-button']` named Save and Continue on both steps records `nextClicks == 2` and `pagesAdvanced == 2` in one run. A continue that is still on step 1 with that block records no extra click. Submit is not in the click list.

### 2. Continue a stall in place

**Gap.** Local continue and same-binding Apply ignore `NAVIGATION_STALLED`. Hosted `/start` clicks the stalled control again on the live page. Hosted `/retry` closes the page and opens the posting URL. Hosted `/continue` refuses the status.

**Steps.**

1. `_can_continue` treats `NAVIGATION_STALLED` like `FAILED_RETRYABLE`: same job, `fresh` false, existing `_CONTINUE_CAP` of 2, pass `nav_block`. A reported submit still cannot continue.
2. Hosted `/retry` and `/start` from `NAVIGATION_STALLED` keep `slot.page` when it still answers. Do not `discard` a live page. `goto(application_url)` runs only when the page is gone, and then only through the existing `slot.open`.
3. Thread the block the engine publishes into the session the way `on_navigation` already threads counters. The next run passes it in. A live-page retry does not click the blocked control. A dead page that had to `goto` clears the block, because the posting is not the stalled step.
4. Hosted `/continue` with `humanResolved: true` accepts `NAVIGATION_STALLED` and uses that same in-place run. It does not accept `READY_FOR_HUMAN_SUBMIT`.
5. Close the TileArc popup host before that run if one is connected, the same `close_popup` `apply_answer` already uses. The extract skip for the host stays in #98.
6. Keep `test_navigation_stalled_is_retryable_without_a_crash_budget` green: retry stays allowed, and `navigation_stalled` still does not increment `browser_restarts`.

**Files.** `src/autofill/local_helper.py` (`_can_continue`, `_run_fill`), `src/autofill/service/app.py` (`retry_session`, `start_session`, `continue_session`, `_note_navigation`), `src/autofill/service/sessions.py` (hold the block next to `last_navigation`), `src/autofill/service/runner.py` (`_run_with_page`, `_options`), `src/autofill/local_attach.py` (`close_popup` call site only).

**#97.** `local_helper.py` overlap is `_remember_answer`. Leave it.

**#98.** Popup close is the shared line. Do not draw the card and do not change `answer_popup.py`.

**Done when.** A stalled Next on a live local page continues with `fresh` false, the blocked control's click count stays 1, and the third continue is `continue_limit`. A hosted retry on a live page does not call `goto`. A retry whose page was discarded opens the Apply URL once and does not spend the crash budget.

### 3. Wait for the step to settle, then decide once

**Gap.** `wait_for_navigation` gives up at 1.5 seconds. A step that moves at 2 seconds is a stall, and slice 2's continue is what would click again.

**Steps.**

1. After the click, poll `navigation_changed` until it is true or the page has been still. Reuse the stillness idea in `wait_for_render` (controls present, not a fresh extract on every tick). Cap the wait at the render budget (12 seconds), not at 1.5.
2. One click. The function still returns a single `(advanced, snapshot)`. It does not call `activate` again.
3. If the signature changes inside the budget, log `advanced` and store the before-signature block from slice 1.
4. If it never changes, and there is no validation alert and no captcha, raise `NavigationStalled` as today.
5. A timeout exception from the driver stays `ats_timeout` / crash, as `is_ats_timeout` and `is_browser_crash` already classify. Do not map those onto `navigation_stalled`.

**Files.** `src/autofill/pagewait.py` (`wait_for_navigation`), `src/autofill/engine.py` (`_confirm_move` only if it must pass the budget through).

**#97 / #98.** `pagewait.py` is not on either branch's behavior change. `navigation_signature` stays the comparator.

**Done when.** A fixture that swaps the heading 2 seconds after Next is `pagesAdvanced == 1`, `nextClicks == 1`, and not `NAVIGATION_STALLED`. A fixture that never changes is still one click and `navigation_stalled`. Submit is not clicked.

### 4. One Next after the last empty field

**Gap.** Hosted `advance_after_answer` does not press Next when the step is filled and the mark did not change. Local resume continues without the block. `_page_changed` ignores a field-set change that `navigation_changed` would count.

**Steps.**

1. Decide "this is a new page" with `navigation_changed` / `navigation_signature`, not with `_page_changed`. Keep `PageMark` as the progress record the helper already stores.
2. When the follow-up is a new signature, the full engine owns typing and the later Next. Do not also click Next for the signature that was just left.
3. When the signature is unchanged, the open-item list is empty, and the status would be `FILLED`, hosted `advance_after_answer` runs the same engine path local `_continue_fill` runs, with the block from slice 1. That path presses this page's Next once and fills what follows.
4. `READY_FOR_HUMAN_SUBMIT` and a review heading do not enter that path. Submit stays unclicked.
5. `resume_action` passes `on_nav_block` and `nav_block` into `fill_visible_tab`, the same arguments `_continue_fill` already passes.
6. A consent or salary-confirm row that is still empty is not "the set is empty." Those stay human stops. #98's single card, if it has merged, is the thing that must be empty before this Next.

**Files.** `src/autofill/follow.py` (`_page_changed`, `follow_answer`), `src/autofill/service/runner.py` (`advance_after_answer`), `src/autofill/local_attach.py` (`resume_action`, `_follow_answer`).

**#97.** `follow.py` is untouched there. If the catalogue fills a row, it never becomes a gap, and this Next waits for a genuinely empty set.

**#98.** This is the click after the page set is committed. Do not add a card, and do not press Next while rows remain.

**Done when.** Two required questions, both answered, one Next, on hosted and on the helper. A review page with nothing empty returns `READY_FOR_HUMAN_SUBMIT` and an empty click list. A resume confirm on a page whose Next is blocked does not increment that button's click count.

### 5. Bind the tab the entry click opened

**Gap.** The helper's `tab.page` stays on the posting. The next adopt prefers that posting because its path equals the Apply URL, and Apply can run again.

**Steps.**

1. `fill_visible_tab` sets `on_page` to a callback the `DedicatedApplySession` owns. The callback points `tab.page`, `tab.page_url`, and `tab.target_id` at the page `_adopt_page` just received. That is the hosted `_bind_page` behavior.
2. `adopt_employer_tab` returns that bound page when it is still in the context. The posting-URL match runs only when there is no bound page.
3. Progress capture reads the bound target id. Slice 6 then allows that id.
4. A continue on the posting, with no bound application tab, may still click Apply. A continue with a bound application tab clicks Apply zero times.

**Files.** `src/autofill/local_attach.py` (`fill_visible_tab`, `adopt_employer_tab`, `DedicatedApplySession`), `src/autofill/engine.py` (`_adopt_page`, hook only).

**#97 / #98.** No overlap beyond `local_attach.py` imports. Leave `apply_answer`.

**Done when.** The `apply_new_tab` fixture driven through the session records one Apply, fills the email on the opened tab, and a second `ensure` of the same Apply URL does not click Apply. `window.__submitted` stays false.

### 6. Accept the path the click already took

**Gap.** `_progress_allows` requires the same path. A crash between Next and the next observe stores the previous path. Continue returns `progress_mismatch`.

**Steps.**

1. After `ensure`, allow the live URL when `same_site` is true and the tab is not a posting (`_tab_is_posting`), even if the path differs from `progress.url`.
2. Allow the target id the slice 5 callback stored, or the id already on the progress record. Any other target id still returns `progress_mismatch` and types nothing.
3. A different host, a binding mismatch, and a tab that is still a job posting stay refused. Do not `goto` to repair them.
4. Selectors stay off the public progress view.

**Files.** `src/autofill/local_helper.py` (`_progress_allows`, `_capture_progress`), `src/autofill/local_attach.py` (`same_site`, `_tab_is_posting`).

**#97.** No overlap on this function.

**Done when.** Progress recorded on `/apply/start`, live URL `/apply/experience` on the same host, continue fills and does not `goto`. A different host and a mismatched binding still type nothing.

### 7. A loop that did not move is a stall

**Gap.** `visible in seen` ends as a normal finish. A real step change with a leftover alert ends as a manual field.

**Steps.**

1. Breaking because the selector tuple repeated, after a Next in this run, returns `NAVIGATION_STALLED` with `navigation_stalled`. The existing block is kept so slice 2 does not click that control again.
2. The first time a tuple is seen, fill it. The repeat is the stall.
3. In `_activate_forward`, a heading or route change stays `advanced` when `invalid_field_messages` is empty. A flagged field on the same route stays `invalid` and is not a second click.
4. Do not widen `validation_message` phrases.

**Files.** `src/autofill/engine.py` (both `seen` breaks, `_activate_forward`), `src/autofill/pagewait.py` (`navigation_changed`, read-only unless the invalid check needs a helper).

**Done when.** Two extracts with the same selector tuple after one Next produce `NAVIGATION_STALLED` and `nextClicks == 1`. A heading change with no `aria-invalid` node produces `pagesAdvanced == 1` and is not `MANUAL_ANSWER_REQUIRED`.

### 8. Fill the page the last Next landed on

**Gap.** Iteration `max_pages` clicks Next, then the code after the loop inspects the new page without filling it. Profile fields there become manual gaps. Continue is not offered.

**Steps.**

1. `max_pages` counts pages filled, not clicks. The loop fills the current page, then clicks Next only when another iteration remains (`index + 1 < max_pages`).
2. The last iteration fills its page and does not call `_activate_forward`. The inspect below the loop therefore sees that filled page, not a ninth page the last click opened and nobody filled.
3. A form whose eighth page is review still returns on the review branch, which already skips Next. `tests/test_eight_page.py` passes `max_pages=12` and stays on that path.
4. The stop on a full non-review page is that page's real gaps (`MANUAL_ANSWER_REQUIRED`) or the existing terminal status. Do not invent a budget status. Do not raise `max_pages`. Continue from slice 2 applies only when the status is already `NAVIGATION_STALLED` or `FAILED_RETRYABLE`.

**Files.** `src/autofill/engine.py` (both `for index in range(opts.max_pages)` loops).

**#97.** Rebase the direct loop if the catalogue context manager landed around it.

**Done when.** A nine-step fixture with `max_pages=8` fills step 8, never opens step 9, and does not click Submit. `nextClicks` is 7. The eight-page resolver fixture with `max_pages=12` still reaches review.

### 9. Say when the pipe is exhausted, and publish the counters

**Gap.** The second dead page in one process raises `CDP reconnect limit reached` with no status the Continue button can show. A helper restart has no block and the local status omits the four navigation fields.

**Steps.**

1. On the second dead page, leave Chrome up, do not launch another, and set the job `state` to `error` with reason `reconnect_limit`. `_can_continue` stays false for that reason so the cap is not spent on a dead pipe. A process restart already resets the counter; do not reset it in-process.
2. Do not write the profile, the secret, the resume, or the block to disk. The new process's first fill keeps today's adopt rule (no `goto` when the tab is not a posting) and `fresh` false.
3. Add `entryClicks`, `nextClicks`, `pagesAdvanced`, and `lastNavigation` to the helper status from the engine result. Add them to totals held on the job, the same way `nav_base_entry`, `nav_base_next`, and `nav_base_pages` work in `_run`. A continue must not report zeros for clicks the previous run already made.
4. `lastNavigation` stays `{action, result}`. No label, URL, or value.

**Files.** `src/autofill/local_attach.py` (`_reconnect_cdp`, `_result_brief`), `src/autofill/local_helper.py` (`LocalJob`, `_remember_result`, `public`).

**#97.** `_remember_answer` stays. The public body gains the four fields only.

**#135.** No vinayaka edit. The fields match the hosted status Continue already knows how to read.

**Done when.** Two dead pages in one process leave a single Chrome, status reason `reconnect_limit`, and zero further `connect_over_cdp` calls. A continue after one successful Next reports `nextClicks == 1` on the helper status. A restart test, with a fake port file, does not call `goto` when the open tab is already the form.

## Cross-ATS notes

Adapters do not branch the block or the stall. The button identity is whatever `selectorFor` returns. The page boundary is `navigation_signature`.

| Host | What usually goes wrong today | After the slices |
| --- | --- | --- |
| Workday | Save and Continue keeps one automation id. Slice 1's bug stops the wizard on the second step. The URL often stays put, so slice 4's `PageMark` does not see a new page. Spinners often outlast 1.5 seconds. | The same button is eligible on the new signature. The last answered field presses it once. The wait covers the spinner. |
| Oracle Candidate Experience, iCIMS, Taleo | Multi-step Next, and the path often changes. Slice 6 then refuses continue. | Continue allows that later path on the same host. |
| Greenhouse, Lever, Ashby | Often one long page, then one Next. The eight-page fixture's distinct ids already advance. | Unchanged for a single Next. A stall continues in place instead of reloading the job URL. |
| Apply that opens a tab | The engine fills the new tab. The helper's next action returns to the posting. | The bound tab is the one adopt returns. Apply is not clicked again. |
| Any host, review | The forward control is Submit. | Still unclicked. `READY_FOR_HUMAN_SUBMIT` is not a continue. |

## Tests

Fixture and unit tests only. No live employer, no Submit.

- Slice 1: extend `tests/test_navigation_harness.py` with a stable automation-id button across two steps. Keep `test_recorded_next_is_not_pressed_again` as the "still on this signature" case.
- Slice 2: extend `tests/test_local_helper.py` so a done `NAVIGATION_STALLED` job continues once and stops at `continue_limit`. Extend `tests/test_service.py` so a runner that records `goto` is not asked to `goto` when the page is alive. Keep the crash-budget test.
- Slice 3: a delayed-heading fixture in `tests/test_navigation_harness.py`. One click.
- Slice 4: a two-question page in `tests/test_save_continue.py` or `tests/test_nav_optional_blockers.py`. Hosted advance and local follow each press Next once. The review fixture presses it zero times.
- Slice 5: `tests/fixtures/apply_new_tab.html` through `DedicatedApplySession` with a fake context that has two pages.
- Slice 6: the progress_mismatch cases in `tests/test_local_helper.py`. Same host, new path, allowed. Other host, refused.
- Slice 7: a same-selector second step expects `NAVIGATION_STALLED`. A heading change without `aria-invalid` expects `advanced`.
- Slice 8: nine local steps with the default cap. Step 8's fields are filled. Step 9 is not opened.
- Slice 9: `tests/test_local_attach.py` already has the one-reconnect case (`test_dead_page_reconnects_on_the_same_port_without_goto`). Add the second drop and the counter fields on the helper status.
- `tests/test_crash.py` stays the hosted relaunch-from-URL case for a real crash. Do not point it at local continue.

`ruff`, `mypy`, and `pytest` belong to the implement pass. This PR is the markdown file only.
