# t79u item 5 — one Save & Continue popup per page (gap map)

Staging tip this map was written against: `0e21166` (checkbox/radio PR #96).

Question-mapping PR #97 (`cursor/question-mapping-reuse-f983`, head `ad8abf30`) is open and may merge onto this tip. It does not draw the popup. It does edit `src/autofill/local_helper.py` (`_remember_answer` → `note_session_answer`), `src/autofill/stepfill.py` (classification, clusters, `pick_option`), `src/autofill/service/schemas.py` (`ApprovedAnswerIn.intent` / `employer` only), `src/autofill/engine.py` (catalogue bind), and `src/autofill/fill.py`. Overlaps are called out per slice. Do not block this map on #97. Implement on whichever of `0e21166` or the #97 merge is the staging tip, and rebase if #97 lands first.

This document is a map only. It does not change fill behavior.

Direct-browser Auto-Fill (local helper, no resolver) and the hosted runner must stop on a page once, show one Save & Continue card (or one hosted human stop) for every question already on that page, write those answers, and only then press the employer's Next / Save and Continue. A second card is for the next page, or for required fields that appear only after those writes. Final Submit stays human.

## Non-goals

- Final Submit stays human. `HUMAN_SUBMIT_ONLY` in `src/autofill/safeguards.py` stays true. No submit click, no review-page bypass.
- Companion stays paused. This repo does not draw the TileArc Apply panel and does not edit vinayaka.
- Staging only. No `main`, no production, no Jobs V2.
- No credential, vault, or sign-in change (#94). Login, Create Account, and email-verification Continue stay on their own routes.
- Resume upload retry and `confirm_manual` stay a resume stop (#92). They are not rows on the answer card.
- Cookie and consent-banner dismiss stays automatic (`dismiss_cookie_banner` in `src/autofill/safeguards.py`). It is not a Save & Continue confirmation.
- Do not add a toast framework. Employer toasts are only read, for resume failure (`src/autofill/stepfill.py`, `src/autofill/resume_manual.py`).
- Do not auto-answer consent, marketing, SMS, arbitration, e-sign, background check, legal certifications, demographics, or a salary confirm. A required standard ToS/Privacy checkbox still follows the #96 terms flag. Those accepts stay on `autoConsents`.
- Do not put ATS-specific question text in `src/autofill/adapters.py`. Adapters keep naming steps.
- Do not merge `confirmed_values` DOM restore with the answer card. Restore is page repair. The card is the human stop.
- #97's catalogue, employer scope, and option equivalents stay in that PR. This map only says which pauses remain after a catalogue hit. It does not reopen mapping.

## What "one per page" means

A **page** is one settled application step: the `navigation_signature` in `src/autofill/pagewait.py` (route, visible-field set, heading). A heading change or a field set that `navigation_changed` treats as an advance is the next page. A validation redraw that keeps the same fields is the same page.

`PageMark` in `src/autofill/follow.py` is coarser. It uses `page_step` (`src/autofill/adapters.py`), which often returns `application` when the heading does not name a step. Workday and Oracle keep one URL across My Information, Experience, and Questions. Those screens are different pages when the heading or the field set changes. They are the same page when only a value was typed.

The **page set** is the required gaps already collected for that signature: `required_gaps` / `inspect_required` (`src/autofill/stepfill.py`) on the direct path, and the blocking `manual_questions` for the current step on the resolver path. Optional blanks are not in the set. Kinds `CAPTCHA`, `LOGIN`, `ACCOUNT`, `VERIFICATION`, `NO_FORM`, and `RESUME` are not rows (`_NON_QUESTION_KINDS` in `src/autofill/local_helper.py`).

**One confirmation** means:

- Local helper: one `#tilearc-answer-popup` host, one nonce, one **Save & Continue** button, listing every row in the page set.
- Hosted runner: one `MANUAL_ANSWER_REQUIRED` stop whose public status describes that same set. Answering a sibling does not count as a new page confirmation.
- The employer's own Next, Continue, or Save and Continue is pressed once, after the set is committed, and it is the employer's control.

A question that was not in the set and appears only after a write (a conditional section) gets one more card for that new set. It does not get one card per new field. The resolver path already retries a revealed step at most three times (`_run_resolver_pages` in `src/autofill/engine.py`). Keep that cap.

Re-showing the same card after a lost DOM node, a bad choice, or the 45s save timeout is the same confirmation. It uses the same host id. It does not add a second dialog.

## Current behavior

### 1. The engine already stops once with every gap

Direct-browser fill (`autofill_application` with no resolver, which is what `DedicatedApplySession.fill` runs) calls `fill_one_page` and then `required_gaps`. Any gap returns `_manual_for_gaps`, which stores the full list on `open_items` and stops. It does not ask one field, write it, and scan again inside that call (`src/autofill/engine.py`).

The resolver path (`_run_resolver_pages`) does the same for the current step: every blocking manual question is returned together. Optional items are carried forward and do not by themselves stop Next.

`inspect_required` builds that list from `_gap_clusters`: required, visible, non-honeypot controls in page order. A checkbox group is one cluster. Each gap has `questionId`, `questionHash`, `text`, `answerType`, `options`, `selector`, `kind`, and sometimes a salary `challenge` of `confirm` (`_gap_item`).

So the missing piece is not discovery. The list exists, and the UI publishes one row of it.

### 2. The local card is one question, then another card

`pending_question` (`src/autofill/local_helper.py`) publishes a single `question` from `blocking`, which `choose_blocking` (`src/autofill/dom_ui.py`) sets to the first blocking item. `LocalJob.public` adds `question.popup` and does not add `open_items`.

`_present_popup` draws that one question through `show_question_popup` → `show_popup` (`src/autofill/answer_popup.py`). The card is one shadow host, `#tilearc-answer-popup`, fixed at the bottom right, with one button labeled **Save & Continue**. A new show removes the previous host, so two cards are not on screen together. The spam is sequential: the employee dismisses one, the fill writes one field, and another card appears for the next sibling on the same page.

That sequence is the contract `tests/test_save_continue.py` (`test_popup_payload_answer_and_continue`) locks in. The fake session returns `q-visa` after `q-color` with no page change, and the test expects a new nonce and `popup.state == "shown"`.

After a verified `POST /v1/sessions/{id}/answer`, `_answer_job` closes the card, writes one target (`apply_answer` → `apply_visible_answer`), then `_follow_answer` → `follow_answer`. On the same `PageMark`, `follow_answer` returns the remaining gaps and highlights `gaps[0]`. `_present_popup` then draws the next question. The card's own docstring says it never fills the field. The helper fills only after the panel posts `/answer`. Keep that.

The real Chrome fixture `tests/fixtures/save_continue_two_pages.html` is one unknown question per page. Two cards there are correct. The bug is two or more required questions on one page.

`_present_popup` always passes `force=True`, which rebuilds the host. `_sync_popup` avoids that while the card is connected and still in `ask`. A status poll does not wipe an in-progress answer. A rebuild happens when the node is gone, the question id changes, or the save times out (`_POPUP_SAVE_TIMEOUT_SECONDS` = 45). Timeout and a bad choice reopen with an error message. That reopen should stay one card.

While a question is pending, idle detach waits (`_finish_active` skips the idle timer). `POST /v1/sessions/{id}/continue` is only for `error` or `FAILED_RETRYABLE` (`_can_continue`). It does not run on top of `MANUAL_ANSWER_REQUIRED`. Continue does not call `close_popup` before `fill_visible_tab`. The answer and resume paths do (`local_attach.apply_answer`, `resume_action`).

There is no employer-tab card when `show_question_popup` is missing. The comment in `_present_popup` leaves that to the panel's Save & Fill. The hosted runner never imports `answer_popup`.

### 3. The card button looks like the employer's Save and Continue

`semantic_action` classifies "Save and Continue" / "Save & Continue" as `save_and_continue`, and `classify_control` plus `_CONTINUE_RE` treat it as `continue` (`src/autofill/safeguards.py`). `_next_button` (`src/autofill/engine.py`) clicks the first in-main forward control that is a `NAVIGATION_CONTROL`.

Extract walks open shadow roots (`eachRoot` in `src/autofill/extract.py`). `placementOf` uses `closest("main")`, which does not cross a shadow boundary. The card is attached to `documentElement`, so its button is not inside `<main>`, and `inMain` becomes true. Nothing skips `#tilearc-answer-popup` or `[data-tilearc-popup]`.

Today the card is removed before a write or a Next on the answer path, so the engine usually does not see it. A later pass that leaves the card up, or a continue that fills while a crashed page still has the host, can treat TileArc's button as the page's Next. On Workday the employer's button often has the same words. Light-DOM buttons are collected before shadow buttons, so the employer's control usually wins when both exist. A step whose only forward control is inside a shadow root that is walked after the card can lose.

`_FINGERPRINT_JS` also counts shadow buttons. Showing the card changes the fingerprint.

### 4. The hosted stop is one blocker, then another stop

`RunStatusOut` (`src/autofill/service/schemas.py`) publishes `blocking` (one item) and `manualQuestions` (intent and text only). `session.open_items` holds the full list and is not in the status body.

`POST /sessions/{id}/answer` (`_answer_once` in `src/autofill/service/app.py`) writes one `AnswerIn`, then `advance_after_answer` (`src/autofill/service/runner.py`). That calls `follow_answer`. Same page: the result is a new `MANUAL_ANSWER_REQUIRED` whose `blocking` is the next gap. Different page: a full fill, which may stop again.

`PauseGate` pauses the worker only when `POST /sessions/{id}/pause` is used. It is not a dialog and it is not per question. `present_blocking` scrolls and draws one red outline (`HIGHLIGHT_KINDS` in `src/autofill/dom_ui.py`). That outline is not a second popup.

Companion is paused, so nothing in this repo paints a hosted modal. The equivalent of the spam is the status contract: each sibling becomes a new blocking publication. A panel that opens a dialog on each `blocking` change would open one dialog per question.

Salary confirm is already a gap row (`challenge: "confirm"` from `src/autofill/salary.py`). It is not `window.confirm`. Consent still requires `confirm: true` on the answer body (`_prepared_target`, and the same rule in `_answer_once`). The current card does not send that flag. The panel does, when it posts `/answer`.

### 5. What a second card is allowed to be

| Event | Today | Wanted |
| --- | --- | --- |
| Two required questions already in `open_items` | Card for the first, then a new card and a new nonce after `/answer` | One card, one nonce, one click |
| Next page after the employer's Next (`save_continue_two_pages.html`) | One card per page | Same |
| Bad choice, or TileArc did not confirm within 45s | Same card reopens with an error | Same |
| Page re-render drops the host | `_sync_popup` shows it again | Same nonce, same rows, still one host |
| Choice reveals a new required field | `follow_answer` fills brand-new controls, then the next gap is a new card | One extra card for the new set, then stop |
| Review, with every required field filled | Card closes, `READY_FOR_HUMAN_SUBMIT` | Same, and Submit stays unclicked |
| Catalogue fills one of two questions (#97) | The filled one never becomes a gap | The card lists only what is still empty |

## Ranked slices

Each slice is one implement pass: the files named, the tests named, and no extra behavior.

### 1. One local card for the page set already in memory

**Gap.** `job.open_items` has every gap. `pending_question` and `show_popup` take the first. The next sibling gets a new card after one `/answer`.

**Steps.**

1. Build the page set with `question_view` over `open_items`, skipping `_NON_QUESTION_KINDS`. Order stays page order. Remember the set's ids on the job when the card is shown.
2. Extend the shadow card to one dialog (`role="dialog"`) with one section per row: prompt, instructions, choices or a textarea, matching today's single-question markup. One **Save & Continue** button. The button stays disabled until every row has a value. Multi-select stays checkboxes.
3. One nonce for the set. The answered state stores `{questionId, values}[]`, still capped (`_MAX_VALUES`, `_MAX_VALUE_CHARS`). A click whose nonce or set does not match is ignored.
4. Status keeps `question` as the first unanswered row so a panel that still posts one `/answer` keeps working. Add the set beside it (`questions`, same shape as `question`, plus one `popup` on the stop). `popup.values` is present only after the click, as today, and lists every row.
5. The card still does not type into the employer form. The helper still writes only from `/answer`.

**Files.** `src/autofill/answer_popup.py`, `src/autofill/local_helper.py` (`pending_question`, `_present_popup`, `_sync_popup`, `_popup_view`, `LocalJob.public`), `src/autofill/local_attach.py` (`show_question_popup` argument shape).

**#97.** No overlap on `answer_popup.py`. `local_helper.py` overlap is `_remember_answer` only. Leave that function's body to #97. Call it once per verified row after this slice's writes.

**Done when.** A fixture with two required questions and one Next shows a single `#tilearc-answer-popup`. One click records both values under one nonce. `question` is still the first row. The second row is in `questions`. Nothing is typed until `/answer`.

### 2. Do not open a second card while that set is being saved

**Gap.** `_answer_job` sets `job.popup = None` and calls `_present_popup` after every verified write. `follow_answer` then supplies the next sibling, and slice 1's card would appear again.

**Steps.**

1. After a verified write, drop that id from the open set. If the remaining ids were already on the card, do not call `show_popup`. Leave the card on "Saving to TileArc…" until the set is empty, or close it after the click and apply later `/answer` posts with the host gone.
2. A failed verify reopens that same host with the error on the failed row (`popup_message` / the existing error copy). Other rows keep their values. Same nonce.
3. A value that fails `fit_choice_values` still reopens once, with "Pick one of the choices shown…". It does not mint a new card.
4. The 45s timeout still reopens the same card when the panel has not posted. Copy stays the existing "TileArc did not confirm the save…" line.
5. When the set is empty and `follow_answer` says the page is filled, the existing `_continue_fill` path presses Next. That is the page turn, not a second confirmation.
6. Update `test_popup_payload_answer_and_continue`: a follow-up whose id was already in the first `open_items` must not add a host or a nonce. A follow-up id that was not in the set still gets one new card (slice 5).

**Files.** `src/autofill/local_helper.py` (`_answer_job`, `_adopt_follow`, `_sync_popup`), `tests/test_save_continue.py`, `tests/test_local_helper.py` (`_AnswerSession` if the fake must return both items at the first stop).

**#97.** After each verified row, keep calling `_remember_answer` / `note_session_answer`. Do not batch that into a new store.

**Done when.** Both answers from one click can be posted as two `/answer` calls, the host count stays one, and the second call does not show a card. A replaced session still returns 410 and the next candidate gets their own card (`test_popup_answer_for_a_replaced_session_is_refused`).

### 3. The employer's Save and Continue is the only forward click

**Gap.** The card's button is extracted as an in-main continue control with the same name Workday uses. The answer path hides this by closing the card first. Any fill that runs while the host is connected can press it.

**Steps.**

1. Skip `#tilearc-answer-popup` and `[data-tilearc-popup]` in the extract walk (controls and buttons), and skip that subtree in `_FINGERPRINT_JS`.
2. `close_popup` before `fill_visible_tab` on continue and on the initial fill, same as `apply_answer` already does. Then the engine cannot click the card.
3. Press the page's forward control only after the page set is committed, through the existing `_next_button` / `activate` path. Do not add a second click helper.
4. A Workday-style fixture whose real button is "Save and Continue", with the card still in the DOM, must select the employer button. The card button is not in `snapshot.buttons`.

**Files.** `src/autofill/extract.py`, `src/autofill/local_attach.py` (`fill`, next to the `close_popup` already in `apply_answer`), `src/autofill/safeguards.py` only if a test needs the classifier unchanged. Do not rename the card button to "OK" or "Confirm": cookie dismiss already clicks those words.

**#97.** No overlap on extract. `engine.py` on #97 is the catalogue context manager. Leave it.

**Done when.** With the card attached, `_next_button` returns the employer control. `tests/test_navigation_harness.py` still sees the employer's "Save & Continue" when that is the page button and the card is absent. Final Submit is still refused.

### 4. One hosted stop for the same page set

**Gap.** The hosted runner stores every gap and publishes one `blocking`. Each `/answer` runs `advance_after_answer`, and the next sibling is a new `MANUAL_ANSWER_REQUIRED`. There is no employer-tab card on this path.

**Steps.**

1. Add the page set to the status: the same `questions` shape as slice 1, without a `popup` (the hosted runner has no employer tab). Keep `blocking` as the first unanswered row.
2. Stamp the stop with the `navigation_signature` taken when the gaps were collected. `advance_after_answer` compares the follow-up signature to that stamp.
3. Same signature and the remaining gaps were in the original set: update `blocking` to the next unanswered row and do not count a new confirmation. `nextClicks` stays unchanged.
4. `AnswerIn` stays one question so current callers keep working. A batch body is optional and only if it is a list of the same fields on the same route. One write path: the existing `apply_answer` per row, then Next once when the set is empty.
5. Consent and consent-choice still require `confirm: true`. A salary `challenge: "confirm"` stays a row in the set. The outline stays on the current blocking field (`present_blocking`). No second overlay.

**Files.** `src/autofill/service/schemas.py` (`RunStatusOut`, not `ApprovedAnswerIn`), `src/autofill/service/app.py` (`_status_out`, `_answer_once`), `src/autofill/service/runner.py` (`advance_after_answer`), `src/autofill/pagewait.py` (`navigation_signature` as the stamp; no new detector).

**#97.** `ApprovedAnswerIn` is the overlap in `schemas.py`. Add `questions` on the status model only. Rebase onto #97's intent/employer fields if that PR has merged. Do not fold catalogue rows into this status.

**Done when.** Two gaps, one status, both listed. Answering the first leaves `pagesAdvanced` and `nextClicks` unchanged and the signature the same. Answering the last runs the existing Next once. A review page with nothing left returns `READY_FOR_HUMAN_SUBMIT` and does not click Submit.

### 5. One extra card when the page reveals new required fields

**Gap.** `follow_answer` types into controls that were not on the previous `PageMark`, then inspects again. A Yes that opens two more required questions would be two more cards if each inspection returns one gap. Slice 2 correctly suppresses siblings of the original set. New ids still need a bound.

**Steps.**

1. If the follow-up gaps contain an id that was not in the card's set, and `navigation_changed` is false, show one card for that new set (slice 1). That is the one allowed extra confirmation on this page.
2. If `navigation_changed` is true, this is the next page. The full fill's next stop owns the card. Do not also show the follow-up card.
3. Cap reveals at the three attempts `_run_resolver_pages` already uses. The direct path should use the same cap inside `_follow_answer` / `follow_answer`. A fourth reveal stops with the fields still empty and one card, and does not loop.
4. Do not fill a revealed consent, demographic, or salary-confirm row from the profile inside `_fill_new` if that row would have paused on a normal scan. `_fill_new` may keep filling profile fields it already fills.

**Files.** `src/autofill/follow.py` (`follow_answer`, `_fill_new`), `src/autofill/local_attach.py` (`_follow_answer`), `src/autofill/engine.py` only to share the existing attempt cap, not to add a click.

**#97.** `follow.py` is untouched by #97. If #97's catalogue fills a revealed question, it never enters the new set. Assert that, and do not reimplement the catalogue here.

**Done when.** A radio that reveals two required text fields produces one additional card with both fields. A third nested reveal does not produce a third card. The two-page fixture still produces exactly two cards, one per page, and `window.__submitted` stays false.

### 6. Continue, reconnect, and a lost card stay on that one host

**Gap.** Continue fills without closing the card. A lost host is re-shown with `force=True`. Slice 1's multi-row state has to survive that re-show. Detach already waits while a question is pending, and continue is refused on `MANUAL_ANSWER_REQUIRED`. Those rules stay.

**Steps.**

1. Re-show uses the stored nonce and the stored rows. `force` rebuilds the DOM and restores the values the employee already picked. It does not allocate a new nonce and it does not drop a row.
2. `close_popup` on continue (slice 3) covers a retryable failure whose page still has a host from an earlier stop.
3. Replace and cancel already clear `job.popup` and `job.question`. Also remove the host, which `end` does not do by itself. `_present_popup` on the new job is the new candidate's one card.
4. A validation stop that returns `MANUAL_ANSWER_REQUIRED` with messages and no gaps (`engine.py`, the alert returns) still has no card. Put the alert text on the existing card when a page set is open. When no set is open, one card whose only body is that message, with Save & Continue disabled, is enough. Do not add a second dialog for the banner.

**Files.** `src/autofill/local_helper.py` (`_sync_popup`, `end`, `request_continue`), `src/autofill/answer_popup.py` (restore on force), `src/autofill/local_attach.py`.

**#97.** No overlap beyond `_remember_answer`.

**Done when.** `test_stale_popup_save_reopens_with_an_error` still sees one host and `applied == []`. Pulling the host out of the DOM and polling status puts the same rows back under the same nonce. Continue on `FAILED_RETRYABLE` leaves zero TileArc hosts before the engine looks for Next.

## Cross-ATS notes

Adapters do not branch the card. The page boundary is `navigation_signature`, which already treats a heading change and a replaced field set as an advance. `page_step` is only a label.

| Host | What one page usually is | Card |
| --- | --- | --- |
| Workday | One My Information / Experience / Questions screen. The employer button is often **Save and Continue**. The URL often does not change. | One TileArc card per screen. The employer button is pressed once after the set is committed. The card button must not be that click (slice 3). |
| Oracle Candidate Experience, iCIMS, Taleo, SuccessFactors, Dayforce | Multi-step Next or Continue. Review is the last screen. | One card per step. Review with nothing empty is `READY_FOR_HUMAN_SUBMIT` and no card. |
| Greenhouse, Lever, Ashby, SmartRecruiters | Often one long page of questions, then Next or a final Submit. | One card listing every remaining required question. One Next. Submit stays unclicked. A long page scrolls inside the existing `max-height: 70vh` card. |
| Any host, conditional question | A committed choice reveals more required fields on the same signature. | One extra card for the new set (slice 5). |
| Any host, standard ToS | #96 checks it when `standardTermsAuthorized` matches the fixed sentence. | It is not a card row when it was auto-accepted. Any other consent row stays manual inside the one card, and `/answer` still requires `confirm: true`. |

Workday and Greenhouse widgets live in open shadow roots. Extract already walks those. The skip in slice 3 has to be the TileArc host only, so a Workday prompt inside a different shadow root is still a row.

## Test ideas

Unit tests first. The Chrome test stays behind `T172_RUN_CHROME_POC=1`.

- Two required questions, one Next, no navigation between them: one host, one nonce, both values on `popup.values` after one click, zero `applied` until `/answer`.
- Two `/answer` posts for that set: host count stays one, the second post does not call `show_question_popup`.
- `tests/fixtures/save_continue_two_pages.html`: one card on page 1, the employer Next once, one card on page 2, `READY_FOR_HUMAN_SUBMIT`, `__submitted` false. This is the existing Chrome test. Keep it.
- Bad option and the 45s timeout: same nonce, `applied` empty, one host.
- Extract of a page that contains the host: the card's button is absent from `snapshot.buttons`. A second button labeled "Save and Continue" inside `<main>` is the one `_next_button` returns.
- Revealed pair: one extra card, two rows, no third card.
- Hosted status lists both questions. After the first answer, `nextClicks` is unchanged. After the second, Next has been pressed once.
- Consent row: `/answer` without `confirm: true` is still 422. The card does not check the employer box by itself.
- Demographic and salary-confirm rows appear in the set and are not auto-filled.
- Replace candidate: old host removed, new job has one unanswered card.
- `HUMAN_SUBMIT_ONLY` is still true (`tests/test_local_attach.py`).

## Suggested implement order

1. Slice 3's extract skip can land with slice 1, because a multi-row card is on screen longer.
2. Slice 1, then slice 2. Those two are the employee-visible fix on the helper.
3. Slice 4 for the hosted contract, same page-set rules.
4. Slice 5 once a same-page set no longer opens a card per sibling.
5. Slice 6 alongside 1 and 2, so the timeout and re-show tests keep passing.

## What this map is not asking a later pass to reopen

- Question catalogue, employer scope, and option equivalents (#97).
- Checkbox discovery, the terms flag, and `autoConsents` (#96).
- Dropdown commit and country-then-state (#95).
- Sign-in and tenant-scoped secrets (#94).
- Continue / reconnect eligibility and the nav block (#93). Continue stays limited to a crash or `FAILED_RETRYABLE`.
- Resume failure status and `confirm_manual` (#92).
- The rule that the card never writes the employer field, and that Final Submit is not a control on it (#91).
