# t79u item 4 — question mapping and answer reuse (gap map)

Staging tip this map was written against: `c6d7f9ff` (dropdown PR #95).
Checkbox/radio PR #96 (`cursor/checkbox-radio-slices-1cd8`, `9174244`) is open and not in this tip. Overlaps are called out per slice. Implement on top of whichever of those two is the staging tip at the time, and rebase if #96 merges first.

This document is a map only. It does not change fill behavior.

Direct-browser Auto-Fill (no resolver on the local helper) and the hosted resolver path must match the same candidate fact to the same question on Workday, Greenhouse, Lever, SmartRecruiters, and the other named hosts. Adapters already do not fork filling. The gap is that classification, reuse, and option commit still disagree with each other, and a few facts are reused across employers when they must not be.

## Non-goals

- Final Submit stays human. `HUMAN_SUBMIT_ONLY` stays true. No submit click, no review-page bypass.
- Companion stays paused.
- Staging only. No `main`, no production, no Jobs V2.
- Do not touch vinayaka. Do not add a credential or vault change.
- Do not add an ATS adapter, and do not put question semantics in `adapters.py`. Adapters keep naming steps only (`src/autofill/adapters.py`).
- Do not turn on the level-3 intent hook, and do not let MEDIUM confidence write a value (`may_fill` in `src/autofill/intents.py`).
- Do not widen the 80% word-containment fallback in `src/autofill/answers.py`.
- Do not click custom `role="checkbox"` or `role="switch"` widgets. #96 reports them and pauses. That deferral stays.
- Do not take multi-select pills or `aria-haspopup="listbox"` button menus. Those stay on the #95 deferral.
- Marketing, SMS, AI-tool, arbitration, e-sign, background-check, legal certifications, and demographic questions stay manual. Auto-Fill may auto-accept only a required standard ToS/Privacy checkbox and the fixed submission-consent sentence, and only when `standardTermsAuthorized` is set. Those accepts stay on `autoConsents`.
- Salary shape rules stay in `src/autofill/salary.py`. A reused salary still has to cover the question shape (`salary_fill_allowed`). This map does not reopen that work.
- `confirmed_values` DOM restore (id / name / automation id / label, and #96's choice restore) is page repair after a rewrite. It is not the cross-page answer catalogue. Do not merge the two stores.

## Current behavior

### 1. How a question is classified

`classify_question` (`src/autofill/intents.py`) is the only intent classifier.

| Step | What it does | Confidence |
| --- | --- | --- |
| Level 1 | Longest synonym phrase in `_SYNONYMS`, after compound sponsorship rules | HIGH |
| Jurisdiction gate | A work-auth intent whose text is not US-only becomes no intent | `jurisdiction` |
| Level 2 | Token overlap with `_PROTOTYPES`. Strict legal intents need a higher score to stay HIGH | HIGH / MEDIUM / LOW |
| Level 3 | Optional hook. Off unless a caller passes it. Never HIGH | MEDIUM |

`jurisdiction()` treats a question that names no country as the United States (`src/autofill/intents.py`). Sponsorship now, future, and now-or-future are separate intents. "Authorized without sponsorship" is its own intent.

Intents that are never written: `GENDER`, `RACE_ETHNICITY`, `VETERAN_STATUS`, `DISABILITY_STATUS`, `REFERRAL` (`UNFILLED_INTENTS`). EEO copy is a second manual gate (`_eeo` in `src/autofill/stepfill.py`).

Control kinds come from extract (`src/autofill/extract.py`): `text`, `textarea`, `select`, `radio`, `checkbox`, `combobox`, `file`, `password`, plus `buttons` for Yes/No pairs. Named radios are one control with options. A radio with an empty `name` is skipped on this tip (`if (!name || seenRadios.has(groupKey)) continue`). #96 groups those by radiogroup, fieldset, or `data-field-path`.

Blocker kinds the employee sees (`_gap_kind` in `src/autofill/stepfill.py`, `canonical_blocker_kind` in `src/autofill/dom_ui.py`): `QUESTION`, `MISSING_PROFILE_VALUE`, `OPTION_MISMATCH`, `DEMOGRAPHIC`, `CONSENT`, `CONSENT_CHOICE`. There is no kind for "two saved answers disagree" or "two options fit".

Answer types (`answer_type_name` in `src/autofill/dom_ui.py`): `text`, `textarea`, `number`, `date`, `boolean`, `single_select`, `multi_select`.

The question string is not one function:

- Resolver / gap text: `_question_text` (`src/autofill/stepfill.py`) prefers `group` and `prompt` for radio and checkbox, then label.
- Catalogue match: `_question_texts` (`src/autofill/mapping.py`) sends label, prompt, aria-label, group, placeholder, **and** `haystack(control)` into `match_answer`. The haystack mixes name, nearby text, and autocomplete.

ATS-specific DOM (Workday prompt rows, Greenhouse menus, Lever location search, Ashby field titles) lives in `extract.py`, `combobox.py`, and `stepfill.py`. That is widget discovery. It is not a per-ATS answer table. `page_step` only labels the step.

### 2. How an answer is chosen

Two engines. The local helper does not set a resolver (`fill_visible_tab` in `src/autofill/local_attach.py`), so direct-browser fill is engine A.

**Engine A — profile mapper.** `fill_one_page` (`src/autofill/fill.py`) calls `map_field` (`src/autofill/mapping.py`) once per control. `MapCursor` is created per page, so repeated employment rows restart at row 0 on the next page.

`map_field` order:

1. Skip password, disabled, read-only, hidden, honeypot.
2. Skip consent choices, marketing checkboxes, and legal attestations (`is_consent_choice`, `acknowledgement_kind`, `_LEGAL_RE`).
3. Match a profile key from autocomplete, then `_RULES` (`_match_rule`).
4. Demographic keys stay unanswered. Resume is `resume_required` (human upload). Salary goes through `_map_salary`. Work authorization goes through `_map_work_authorization` (US profile facts only; a non-US question pauses unless an approved answer matches that wording).
5. Screening keys (`age_18_or_older`, `willing_background_check`, `felony_conviction`, `previously_employed_here`, `how_heard`) use the profile first, then an approved answer. Disagreement pauses.
6. If no profile key matches, `_explicit_answer` calls `match_answer`. A skill question is Yes only when that skill is on the profile.

**Engine B — resolver.** `fill_step` / `_apply_question` (`src/autofill/stepfill.py`) asks the resolver, then wipes the response. `_take_answer` returns the first resolver row for that intent (`by_intent.setdefault`). It does not check jurisdiction and it does not refuse two disagreeing rows. Unknown questions pair by normalized text, question hash, or id. A keyless answer is used only when that step has a single unclassified question.

Option commit also differs:

| Path | Matcher | On two fits |
| --- | --- | --- |
| Engine A `match_option` | Exact label, yes/no, one leading "Yes"/"No", four state aliases, then `match_country_option` | Returns None only for two leading yes/no phrases. Otherwise the first exact hit |
| Engine B `match_option_exact` / `pick_option` | Exact equivalent, else one word-tier match (exact, prefix, contains) | `ambiguous` becomes None, then `OPTION_MISMATCH` |
| Combobox write | `select_combobox` (`src/autofill/combobox.py`, #95) | No commit unless the page shows the choice |

`may_fill` requires HIGH local confidence and HIGH resolver confidence. A saved answer may fill an unknown question only when the resolver marks `source=saved_answer` and confidence HIGH (`_saved_unknown`).

### 3. How an answer is reused

`match_answer` (`src/autofill/answers.py`) is the Req2 matcher (PR #90):

1. Same normalized question text. Two different values for that text return None.
2. Same HIGH intent. Work-auth intents also need the same country set. Two values for that intent return None. `UNFILLED_INTENTS` return None even when the text matches an intent. A carried `intent` that contradicts the saved text is ignored (`_saved_intent`).
3. If neither side has an intent: "how did you hear" synonym phrases, then a containment check (one string inside the other, at least 12 characters, 80% of the longer question's content words).

That catalogue is `CandidateProfile.application_answers`. Profile JSON may carry `intent` (`_answer_list` in `src/autofill/profile.py`). The HTTP field does not: `ApprovedAnswerIn` (`src/autofill/service/schemas.py`) is `question` + `answer` with `extra=forbid`. `apply_approved_answers` (`src/autofill/service/context.py`) merges by casefolded question text and builds `ApplicationAnswer` **without** `intent`, so a host-approved paraphrase loses the intent TileArc stored.

Within one direct-browser fill, `_remember_answer` (`src/autofill/local_helper.py`) appends a verified human answer onto that job's profile:

- Same candidate only. The profile is dropped on replace or cancel.
- Skips `CONSENT`, `CONSENT_CHOICE`, `DEMOGRAPHIC`, `MISSING_PROFILE_VALUE`.
- Stores one value. A multi-select is dropped.
- Does not replace an existing row with the same question text.
- Copies `intent` off the blocker. The blocker stores an intent only when classification is HIGH (or demographic). A later page can reuse it through `match_answer`.

The hosted session never appends to `application_answers`. `ServiceSession.confirmed_values` (`src/autofill/service/sessions.py`) remembers what was written so a later parse can put it back on the same control (`_confirmed_hint` / restore JS in `src/autofill/stepfill.py`). Match keys are element id, automation id, name, and label. A reworded question on the next page does not see it. Resolver answers are wiped at the end of the step.

There is no cross-application store in this repo. Reuse across ATS happens only when TileArc sends the same `applicationAnswers` / `approvedAnswers` on the next session, and only through engine A.

### 4. How a pause is chosen

| Situation | What the employee gets |
| --- | --- |
| Required control, no profile value, no catalogue hit | `MISSING_PROFILE_VALUE` if a profile key matched, else `QUESTION` |
| Two saved answers disagree | `match_answer` returns None, so the same missing-answer pause. The reason string is not a distinct blocker kind |
| Option list has no unique hit (engine B) | `OPTION_MISMATCH`, blocking |
| Option list has two leading yes/no phrases (engine A) | `unanswered` ("No option matched") |
| Salary shape not covered | `challenge=confirm` on the blocker (`salary_range_incomplete` / `salary_needs_confirm`) |
| Demographic or EEO | `DEMOGRAPHIC`, not filled |
| Consent / legal checkbox | `CONSENT` or `CONSENT_CHOICE`, or skipped when not required |
| Optional unknown question with no answer (engine B) | Does not block continue (`_question_blocks`) |
| Location typeahead, several cities | `ambiguous` vs `no_match` inside the place picker (`stepfill.py` around the city-hit check) |

`fit_choice_values` (`src/autofill/dom_ui.py`) already refuses an ambiguous option when a human answer is posted. The automatic path does not surface that same status.

### 5. Dropdown (#95) and checkbox/radio (#96)

#95 (merged, this tip) owns the widget write for selects and comboboxes: `choose_native_select`, `select_combobox`, country-then-state, async menus, disabled options. Question mapping should call those writes. It should not grow a second menu driver.

#96 (open) owns boolean discovery and the terms gate. It does not edit `answers.py` or `intents.py`. It does edit the files a reuse slice will also touch:

| #96 change | Why a reuse slice must not redo it |
| --- | --- |
| `extract.py` groups unnamed radios and custom `role="checkbox"`, `role="switch"`, `role="radio"` | Slice 2 should consume that grouping. Do not add a second radio grouper |
| `mapping.py` `attestation_manual`, `is_demographic_control`, terms kinds extended to radio, select, combobox, and buttons | Background-check checkbox becomes `skip` / `LEGAL_FIELD` (`tests/test_mapping.py`). A saved "Yes" must not check it. Demographic text stays `DEMOGRAPHIC` even on a Yes/No button |
| `stepfill.py` `_describe` classifies consent and attestations before buttons; required attestations pause; optional ones stay unchecked | Reuse runs only after that gate |
| `stepfill.py` choice restore from `confirmed_values`, skipping consent, attestation, and demographic | That is DOM restore. Leave it. Semantic reuse is slice 4 |
| `fill.py` `commit_native_choice` | The successful Yes/No write. Slice 5 calls it |
| `local_helper.py` `standardTermsAuthorized` | Consent flag only. `_remember_answer` is unchanged |
| Custom checkbox and switch are reported and not clicked | A reused Yes on those widgets pauses |

If #96 has not merged, slices 1, 3, 4, and 6 can land on `c6d7f9ff` without those files' consent edits. Slices 2 and 5 should wait for #96 or include only the part #96 does not already cover (see each slice).

## Cross-ATS reuse constraints

These stay true in every slice.

- One candidate, one fact. Work authorization, sponsorship timing, age, relocation, travel, shifts, clearance, licenses, start date, employment type, and education level are portable. The same HIGH answer may fill Workday and Greenhouse.
- Employer-scoped facts must not cross `credential_scope` or `job.company`: previously employed here, referral name, "worked for this company", and any question whose wording names the employer. `PREVIOUSLY_EMPLOYED` is a portable intent today, so a "No" at company A fills company B. That is the bug slice 3 closes.
- "How did you hear" may be reused for the same candidate as a source (LinkedIn). It must not be reused as a referral name. `REFERRAL` stays unfilled.
- A country named in the question is part of the key. Canada does not take the US work-authorization answer (already true in `match_answer`). A question that names no country must not silently become US (slice 6).
- Two saved values for one key never pick one. The field pauses.
- Disagreement between the Application Profile and an approved answer pauses (already true for screening and work auth).
- Consent, marketing, SMS, arbitration, e-sign, background check, certifications, and demographics are not catalogue entries.
- Salary reuse still goes through `salary_fill_allowed`. An annual number does not fill an hourly question, and a single number does not fill a range.
- Option labels differ by ATS ("Bachelor's" vs "BS", "Full-time" vs "Full Time"). Equivalents are a closed list (slice 5). Word-containment that matches two options pauses.
- The catalogue is memory on the session. Nothing in these slices writes answers to disk, logs the text, or sends them to another candidate.
- Widget CSS stays in extract and the #95/#96 writers. A new Greenhouse class name is not a new matcher.

## Ranked slices

Each slice is one implement pass: the files named, the tests named, and no extra behavior.

### 1. One catalogue lookup for both engines

**Gap.** Engine A uses `match_answer` (disagreement and jurisdiction). Engine B uses `_take_answer` (first intent row wins, no jurisdiction). `approvedAnswers` drops `intent`. `match_answer` also sees `haystack(control)`, so a placeholder or nearby word can hit a different saved question.

**Steps.**

1. Add optional `intent` on `ApprovedAnswerIn` (must be in `PROTOCOL_INTENTS` or empty). `apply_approved_answers` copies it onto `ApplicationAnswer`. Contradiction handling stays in `_saved_intent`.
2. Add `question_stem(control)` used by `classify_question`, `match_answer`, and blocker text. Stem is group, prompt, label, then aria-label. It does not include `haystack`.
3. Change `_take_answer` so an intent hit goes through the same rules as `match_answer`: one value, same jurisdiction for work-auth intents, no `UNFILLED_INTENTS`. Two resolver rows that disagree return no answer.
4. Leave containment and the how-heard synonym list as they are. Do not add a fuzzy tier.

**Files.** `src/autofill/answers.py`, `src/autofill/service/schemas.py`, `src/autofill/service/context.py`, `src/autofill/mapping.py` (`_question_texts`, `_explicit_answer`), `src/autofill/stepfill.py` (`_take_answer`, `_index_answers`, `_question_text` calls).

**#96.** No overlap on `answers.py`, schemas, or context. `mapping.py` and `stepfill.py` overlap the consent edits. Rebase onto #96 if it has merged; do not edit `attestation_manual` or `_describe`'s consent branch in this slice.

**Done when.** A Greenhouse wording and a Workday wording of `SPONSORSHIP_FUTURE` both return the same saved value from `match_answer` and from `_take_answer`. Two different values return None on both. An `approvedAnswers` row with `intent` survives `apply_approved_answers`. A placeholder that equals some other question does not match.

### 2. Cluster a question before mapping on the direct-browser path

**Gap.** `fill_one_page` maps each control alone. `stepfill._clusters` already turns a checkbox fieldset into one multi question. A "which languages" group therefore becomes one resolver question and many profile-path checkboxes, each labeled with the option. Unnamed radios are absent until #96.

**Steps.**

1. After #96, build clusters with the same `_clusters` / `_checkbox_key` rules `stepfill` uses, including unnamed radios and the form-field wrapper group.
2. `fill_one_page` calls `map_field` once per cluster, using `question_stem` from slice 1. A multi cluster uses `match_all_options`. Any miss pauses the group and checks nothing.
3. If this slice lands before #96, cluster only what this tip already clusters (named radios, fieldset or prompt checkbox groups). Leave unnamed radios and custom roles to #96. Do not copy #96's extract JS into this slice.

**Files.** `src/autofill/fill.py`, `src/autofill/stepfill.py` (export the cluster helper if it is still private), tests.

**#96.** This is the main overlap. Prefer to start this slice from the #96 branch. Do not reimplement radio grouping or `commit_native_choice`.

**Done when.** One HTML checkbox group is one question on both engines, and a partial option list checks nothing.

### 3. Portable facts vs employer-scoped facts

**Gap.** `PREVIOUSLY_EMPLOYED` reuses across employers. The how-heard synonym bucket is fine for a source and dangerous if it swallows a referral. Nothing on `ApplicationAnswer` records which employer it came from.

**Steps.**

1. Mark a closed set as employer-scoped: `PREVIOUSLY_EMPLOYED`, `REFERRAL`, and any stem that matches "this company", "our company", or the current `job.company` name. `REFERRAL` stays unfilled either way.
2. `match_answer` reuses an employer-scoped row only when the saved row's employer key equals this session's `credential_scope` or company. A missing employer key does not match another employer. Exact text on the same question still matches inside that employer.
3. Portable intents keep today's reuse. How-heard synonyms stay portable and still do not match `REFERRAL`.
4. Session write-back (slice 4) stores the employer key on employer-scoped rows.

**Files.** `src/autofill/answers.py`, `src/autofill/profile.py` (`ApplicationAnswer` gains an optional employer key, default empty), `src/autofill/mapping.py` (pass job company into the catalogue call), `src/autofill/local_helper.py` only if slice 4 is in the same pass. Otherwise accept the key on rows the host already sends.

**#96.** No overlap, except `local_helper.py` if write-back lands here. #96's helper edits are the terms flag. Keep them.

**Done when.** "Have you worked at Acme before?" answered No does not fill the same intent at Beta. "How did you hear about us?" answered LinkedIn still fills the synonym on Beta. "Who referred you?" still pauses.

### 4. Write a verified answer back for the next page, on both engines

**Gap.** Only the local helper appends to `application_answers`, and only for a single value, and only when that exact question text is new. The hosted path can restore the same DOM node and cannot answer a reworded question on page 2.

**Steps.**

1. After a verified commit (`remember_confirmed` already means the page showed the value), append one catalogue row when the stem has a HIGH portable intent, or when the stem is unknown and the employee typed a value for that exact stem.
2. Skip the kinds already skipped, plus #96 attestations (background check, e-sign, arbitration, SMS, certification) and anything `acknowledgement_kind` calls consent or marketing.
3. Do not store profile-field writes (name, email, phone, address) as questions. Do not store secrets.
4. A second answer for the same intent replaces the row only when the new value was verified and the old row was also created this session. A host-supplied row that disagrees still pauses (slice 1).
5. Call this from the local helper and from the hosted session's confirmed hook, writing the catalogue on the profile, not into `confirmed_values`.
6. Multi-select waits for slice 2. This slice stores a single value only.

**Files.** `src/autofill/local_helper.py` (`_remember_answer`), `src/autofill/service/sessions.py` or the runner hook in `src/autofill/service/app.py`, `src/autofill/answers.py`.

**#96.** `_remember_answer` is untouched by #96. The choice-restore block in `stepfill.py` must stay a DOM restore. Do not append consent rows from it.

**Done when.** Page 1 of a fixture answers "Are you authorized to work in the United States?" with Yes, page 2 asks "Legally authorized to work in the U.S.?", and the second page is filled from the catalogue with no second pause. A consent checkbox answered on page 1 is empty on page 2 and pauses again.

### 5. One option commit, and a pause the employee can tell apart

**Gap.** Engine A and engine B pick options differently. Ambiguous and missing both look like a blank question. Education and employment-type labels differ by ATS and have no equivalent table (`options_equivalent` covers yes/no, percent buckets, and plain numbers only).

**Steps.**

1. Route engine A selects, radios, and comboboxes through `pick_option`. `ambiguous` and `none` both write nothing.
2. Blocker kind `ANSWER_CONFLICT` when the catalogue has two values. Blocker kind `AMBIGUOUS_OPTION` when `pick_option` returns `ambiguous`, including the option labels. Keep `OPTION_MISMATCH` for a single value that is not in the list.
3. The write, once a single option is chosen, is the existing one: `select_combobox` or `choose_native_select` (#95), `commit_native_choice` (#96) for native checkbox and radio. No new click path.
4. Add a closed equivalent table, used only by `options_equivalent`, for education level (Bachelor's / BS / Bachelor of Science, and the same shape for Master and Doctorate) and employment type (Full-time / Full Time / Permanent, Part-time / Part Time). Two options that both match the table pause as `AMBIGUOUS_OPTION`.
5. A custom checkbox or switch (#96, not clicked) pauses even when the catalogue has Yes.

**Files.** `src/autofill/intents.py` (`options_equivalent`), `src/autofill/mapping.py` (`_from_text`), `src/autofill/stepfill.py` (`_apply_question`, `_gap_kind`), `src/autofill/dom_ui.py` (blocker kind names).

**#96.** Depends on `commit_native_choice` and on custom widgets staying unclicked. Land this slice after #96, or call the pre-#96 `locator.check` only for native boxes and leave custom widgets as a pause with a comment that #96 owns the press.

**Done when.** "Bachelor of Science" selects a single "Bachelor's" option and does not select when both "BS" and "Bachelor's" are present. A catalogue conflict does not type either value. Dropdown commit still requires the #95 read-back.

### 6. Do not treat a silent question as United States

**Gap.** `jurisdiction()` returns `{US}` when the text names no country. "Are you legally authorized to work?" on a Toronto Greenhouse form takes the US profile answer. `classify_question` then locks it to `US_WORK_AUTHORIZATION` at HIGH.

**Steps.**

1. When the text names no country, do not assign a work-auth intent unless this job's filled address country is the United States or the question's page already committed a US country control.
2. Otherwise the question stays unknown. An exact-text approved answer for that wording still applies. The US profile facts do not.
3. A question that names Canada, the UK, or another listed country keeps today's behavior: no US intent, exact-text answer only.

**Files.** `src/autofill/intents.py` (`jurisdiction` callers in `classify_question` — do not change the default inside `jurisdiction` if other callers rely on "none means US"; gate it at the classify and `match_answer` call), `src/autofill/mapping.py` (`_map_work_authorization`).

**#96.** No overlap.

**Done when.** The existing Canada tests still pass. A new case with no country token and no US address on the page does not select Yes from the US profile. The same wording with a committed US country still selects.

### 7. Cross-ATS fixtures for the slices above

**Gap.** `tests/fixtures/question_mapping_r2.html` and `tests/test_question_mapping_r2.py` cover wording and work-auth semantics on one control shape. They do not show the same stem extracted from four ATS shells, a page-2 reuse, or an employer-scoped refusal.

**Steps.** Add fixtures and tests only. No new production branch.

| Fixture | Asserts |
| --- | --- |
| `tests/fixtures/question_reuse/workday_questions.html` | Workday-style prompt + named radios |
| `tests/fixtures/question_reuse/greenhouse_questions.html` | Label + select, including education |
| `tests/fixtures/question_reuse/lever_questions.html` | Lever-style application field |
| `tests/fixtures/question_reuse/smartrecruiters_questions.html` | Same stems, different wrappers |
| Page-2 continuation (one HTML with two sections, or two files) | Slice 4 |
| Consent and demographic controls on the same page | Not filled from a saved Yes |
| Two companies in one unit test, no browser | Slice 3 |

Shared stems in every shell: US work authorization, Canada work authorization, sponsorship now, sponsorship in the future, previously employed, how did you hear, education level, a yes/no that is not legal, a required standard terms checkbox, a gender question, a salary range.

**Files.** `tests/fixtures/question_reuse/`, `tests/test_question_reuse.py`. Extend `tests/test_question_mapping_r2.py` only for the catalogue rules in slices 1 and 6.

**#96.** The terms checkbox assertion should expect the #96 gate: checked only when `standardTermsAuthorized` and the sentence is one of the two fixed ones, and listed on `autoConsents`. On this tip, before #96, assert it is not filled from the answer catalogue (it may still be a consent skip). Do not weaken #96's tests.

**Done when.** The four shells produce the same stem and the same catalogue decision for each shared question, and the consent and demographic controls stay empty.

## Suggested implement order

1. Slice 1 and slice 6 (catalogue correctness, no widget changes).
2. Slice 3 (employer scope) on that catalogue.
3. Slice 4 (session write-back) once the catalogue refuses the wrong rows.
4. Slice 7 unit tests alongside each of those.
5. Slice 2 and slice 5 after #96 merges, then the browser fixtures in slice 7.

## What this map is not asking a later pass to reopen

- Dropdown commit, country-then-state, and async menus (#95).
- Checkbox discovery, the shared native press, the terms flag, and `autoConsents` (#96).
- Sign-in, tenant-scoped secrets, and account creation (#94).
- Continue / reconnect / nav block (#93).
- Resume upload status (#92) and the Save & Continue popup (#91).
- Req2 rules that already hold: no guess past a disagreement, sponsorship timings are not interchangeable, demographic intents are not reused, writes are read back.
