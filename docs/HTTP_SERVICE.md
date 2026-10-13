# Auto-Fill HTTP service

This document is the network protocol for the public Auto-Fill program. Auto-Fill is the service described here. Callers are separate applications. This repository does not contain those applications, their private APIs, their database schemas, or their credentials.

The license is AGPL-3.0-only. See [LICENSE](../LICENSE) and [NOTICE](../NOTICE). Form-filling behavior is adapted from [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) (AGPL-3.0-only). Because this is a network service, AGPL section 13 applies to this program: users who interact with it remotely are offered the corresponding source of this program.

The in-process Python API still exists. This service calls `autofill_application` and the same human-submit guard.

## What the service will not do

- It will not click Submit or Submit Application on a form. There is no submit route. `stopped_before_submit` is always true. A refused automation click raises `HumanSubmissionRequired` inside the process and the HTTP call returns 409. `Apply`, `Apply now`, `Apply for this job`, `I'm interested`, and `Apply Manually` may be clicked only when the page has no application form yet.
- It will not accept resume bytes in the JSON body, and it will not generate or substitute a resume. A resolver `resume.file` grant (`url`, `filename`, `contentType`, `expiresAt`) is downloaded into memory with a size cap and attached with `set_input_files` as that filename, its type, and those exact bytes, so the site can read the file after this process returns. The filename is the grant's basename, sanitized, and it must end in `.pdf`, `.doc`, or `.docx`. A grant with no usable filename is attached as `Resume.pdf`. The name is never taken from the placeholder profile. A missing grant or a failed download leaves the field empty. The attach counts only when, a few seconds later, that filename is shown on the field or the page shows a Success! upload indicator, and no upload-error toast mentions that filename or "failed to upload". The filename comparison ignores case and treats spaces and underscores as the same. Any other resume input stops at `RESUME_UPLOAD_REQUIRED` until `resumeUploaded: true`.
- It will not solve, bypass, or inject a token for CAPTCHA, DataDome, Cloudflare, or PerimeterX. A visible interactive challenge, including one below the fold, or a full-page bot wall, sets `CAPTCHA_REQUIRED`. Invisible reCAPTCHA (including Enterprise outside the badge) and invisible hCaptcha do not stop the run, so a visible form is filled. A challenge that becomes visible later, including after Next, stops the run. `continue` with `humanResolved: true` resumes after a person has cleared a challenge.
- Resolver calls stay within 40 fields and 20 questions. Question text is at most 300 characters and option labels at most 80. A question with more than 30 options is asked without its list, then matched locally. A `413` is treated like a `422`: those keys stay unresolved and the session is not `FAILED_FINAL`.
- It will not open a caller database. `DATABASE_URL` is ignored. Do not send database URLs, API tokens, or resume files in the JSON body.
- It will not fill demographic or voluntary self-identification questions, including Greenhouse-style react-select EEO controls.
- It will not log raw profile values, passwords, typed interaction text, screenshot bytes, or the service token. Logs contain the session id, status, ATS name, and step. Screenshots stay in memory for the response only.

## Run

Python 3.11 or newer.

```bash
pip install .
python -m playwright install chromium
export AUTOFILL_SERVICE_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export AUTOFILL_RUN_BROWSER=1
export PORT=8080
python -m autofill.service
```

`autofill-serve` is the same entrypoint. The process refuses to start when `AUTOFILL_SERVICE_TOKEN` is missing or shorter than 16 characters.

| Variable | Required | Meaning |
| --- | --- | --- |
| `AUTOFILL_SERVICE_TOKEN` | yes | Bearer secret. Set it in the process environment. Do not commit it, put it in a URL, or embed it in client-side JavaScript. |
| `PORT` | no | Listen port. Default `8080`. |
| `HOST` | no | Listen address. Default `0.0.0.0`. |
| `AUTOFILL_RUN_BROWSER` | no | `1` (default) launches Chromium for fill runs. `0` still serves the API and still applies manual-ATS and SSO refusals, and it will not launch a browser. |
| `AUTOFILL_MAX_SESSIONS` | no | Cap on sessions kept in memory. Default `32`. Expired sessions do not count. |
| `AUTOFILL_BROWSER_NO_SANDBOX` | no | `1` adds Chromium `--no-sandbox`. The container image sets this. Leave it unset on a normal desktop. |
| `AUTOFILL_SESSION_TTL_SECONDS` | no | Session lifetime. Default `1800`. A background reaper then marks the session `EXPIRED` and wipes secrets. |
| `AUTOFILL_REAPER_INTERVAL_SECONDS` | no | How often the reaper runs. Default `30`. |
| `AUTOFILL_RESOLVER_TIMEOUT_SECONDS` | no | Resolver HTTP timeout. Default `5`. |
| `AUTOFILL_RESOLVER_RETRIES` | no | Extra attempts after a retryable resolver failure. Default `2`. |
| `AUTOFILL_RESOLVER_MAX_BYTES` | no | Maximum resolver response body. Default `65536`. |
| `AUTOFILL_BROWSER_WORKERS` | no | Browser worker threads. Each has its own Chromium. Default `2`. |
| `AUTOFILL_MEMORY_LIMIT_MB` | no | Container memory limit for opening a new session. Unset uses 85% of the cgroup memory maximum when that maximum is finite, otherwise `1536`. |

Interactive API docs are served at `/docs`. The schema is `/openapi.json`. Neither document contains a real token.

## Auth

Every `/v1/` route requires:

```http
Authorization: Bearer <AUTOFILL_SERVICE_TOKEN>
```

Missing and wrong tokens both receive `401` and `{"detail":"Unauthorized"}`. `GET /health` and `GET /v1/health` do not require auth. Both return `{"status":"ok","service":"auto-fill","supportedProtocolVersions":["0.4.0","0.5.0","0.6.0","0.7.0"],"features":["standardTerms"]}` and nothing about sessions. `supportedProtocolVersions` is the field a host reads to see that `0.7.0` is supported. `features` lists `standardTerms` when this build will honor `standardTermsAuthorized`. Creating a session still defaults `protocolVersion` to `0.6.0` unless the caller sends `0.7.0`. The `201` create body repeats `features`.

Call the API from a backend you control. A browser page that holds the token is the wrong place for it.

## Sessions

Sessions live in process memory, keyed by `sessionId`. Two sessions cannot read each other's profile or site password, even when the candidate id and site domain are the same. Passwords are scoped to that session and that application host. They are dropped when the session is deleted. A restart drops every session. Nothing is written to disk.

`candidateContext.candidateId` is required. If `jobContext.candidateId` is also sent, it must be the same id or the create call fails with 409 and no session is stored. Later calls that send a different `candidateId` fail with 409 and do not run the filler.

## Endpoints

### `POST /v1/sessions`

Create a session. Does not open the browser.

```json
{
  "candidateContext": {
    "candidateId": "candidate-example",
    "personal": {
      "full_name": "Casey Example",
      "email": "casey.example@example.com"
    }
  },
  "jobContext": {
    "applicationUrl": "https://boards.greenhouse.io/example/jobs/1",
    "jobId": "job-example",
    "candidateId": "candidate-example",
    "title": "Example Engineer",
    "company": "Example Labs",
    "postedSalaryMin": 80000,
    "postedSalaryMax": 100000
  },
  "approvedAnswers": [
    {"question": "Are you legally authorized to work?", "answer": "Yes"}
  ]
}
```

`candidateContext` uses the profile object documented in the README. `approvedAnswers` are explicit question/answer pairs and override the same question on the profile. Unknown questions stay blank. `applicationUrl` must be `http` or `https`, with no username or password in the URL.

`201` response:

```json
{
  "sessionId": "<id>",
  "status": "CREATED",
  "stopped_before_submit": true,
  "candidateId": "candidate-example",
  "jobId": "job-example"
}
```

The response does not echo the profile, the email, or any secret.

### `POST /v1/sessions/{sessionId}/credentials`

Store a site login for this session only. Send it before `start` when the application page is a login form. Replacing credentials overwrites the previous secret for this session. The response is `{"sessionId":"<id>","stored":true}`.

```json
{
  "email": "casey.example@example.com",
  "username": "",
  "password": "<runtime secret for this site>",
  "site": "boards.greenhouse.io",
  "candidateId": "candidate-example"
}
```

`site` may be omitted. When it is set, its host must be the application host. Another candidate id is 409. An email or username is required along with the password.

### `POST /v1/sessions/{sessionId}/start`

Open the application URL and fill until the engine stops. Optional body: `{"candidateId":"candidate-example","standardTermsAuthorized":true}`. `standardTermsAuthorized` is stored only when it is true, so a false value does not clear a grant from create, and the deny-list is unchanged. Allowed from `CREATED`, `LOGIN_REQUIRED`, `LOGIN_FAILED`, and `NAVIGATION_STALLED`. The response returns after navigation begins, within a few seconds, and the fill continues in the background. Status and screenshots show that progress. A start that does not run leaves the session `FAILED_RETRYABLE` with message `start_failed`, and `POST /retry` runs it again. A click on Apply or Next that does not change the page stops as `NAVIGATION_STALLED` with message `navigation_stalled`. Status includes `entryClicks`, `nextClicks`, `pagesAdvanced`, and `lastNavigation` (`{action, result}` or null). Those fields count clicks and carry no labels, URLs, or candidate values. That status is retryable and does not spend the browser-crash budget. A session that is still `CREATED` more than 120 seconds after create, with no successful start, is expired and no longer counts toward the session cap.

### `GET /v1/sessions/{sessionId}/status`

Return the latest public state. Counts and control names only. Not the values written into the form.

### `POST /v1/sessions/{sessionId}/continue`

```json
{"resumeUploaded": true, "candidateId": "candidate-example"}
```

Allowed only when the status is `RESUME_UPLOAD_REQUIRED` and `resumeUploaded` is true. This confirms that a person uploaded the resume. It does not attach a file and it does not submit the application.

`READY_FOR_HUMAN_SUBMIT` returns 409. The person clicks Submit on the application. `CAPTCHA_REQUIRED` returns 409. The service does not continue past a challenge.

### `DELETE /v1/sessions/{sessionId}`

Drop the session, close its browser, and forget its site password. `204` on success. `404` when the id is unknown.

## Protocol 0.4.0 resolver mode

`POST /v1/sessions` may create a resolver session instead of sending a profile. The body carries opaque refs, the application URL, and a per-session resolver. It does not carry candidate profile values or approved answers.

```json
{
  "protocolVersion": "0.4.0",
  "candidateRef": "candidate-ref-example",
  "jobRef": "job-ref-example",
  "jobContext": {
    "applicationUrl": "https://boards.greenhouse.io/example/jobs/1",
    "title": "Example Engineer",
    "company": "Example Labs"
  },
  "resolver": {
    "url": "https://resolver.example/v1/resolve",
    "token": "<32 or more random bytes, memory only>",
    "expiresAt": "2026-10-09T12:00:00+00:00"
  }
}
```

`standardTermsAuthorized` is an optional boolean on create (default false). Send it only when `features` includes `standardTerms` on `GET /v1/health` or on the create response. It is the candidate's preference for plain site terms. When it is true, a required checkbox is checked only after a deny-list and then a short allow-list: marketing, SMS, promotional mail, newsletters, job alerts, email updates, talent community or network, calls, sharing with third parties or partners, AI or recording consent, arbitration, e-sign or signature, background check, certify/attest/declare under penalty, demographic questions, sensitive, biometric, health, medical, or criminal information, social security or SSN, contacted or contact me, a drug test, waive or waiver, jury, transfer, interview or video analysis, and salary stay `CONSENT`, and so does a compound statement that mixes plain privacy text with any of those, and anything that does not match the allow-list. Each acceptance is reported as `autoConsents`: `{questionHash, textExcerpt, category}`. `category` is `STANDARD_TERMS` only when the whole statement is "I agree to", "I accept", or "I have read and agree to" the Terms of Service, Terms and Conditions, Terms of Use, Privacy Policy, or Privacy Notice, optionally joined with "and" to another of those documents, with an optional final period. "I agree to the Terms of Service and I agree to anything at all" stays `CONSENT`. `STANDARD_SUBMISSION_CONSENT` applies only when the whole statement is fixed wording with no free-word gap: "I consent" or "I agree" to "submit", "the submission of", or "the processing of" "my" optional "personal" "information" or "data", optionally "for this application", "for this job application", or "for the purposes of this application" or "recruitment process". "I consent to submit my information" and "I agree to the processing of my personal data for this application" match. A share-with-employer sentence, a longer sentence, or any unusual wording stays `CONSENT`. The log records the category and `checked` or `failed`, not the statement and not candidate data. When the flag is false or omitted, those boxes stay unchecked.

`201` returns `{sessionId, status: CREATED, stopped_before_submit: true, ...}`. The resolver token is not echoed. A resolver URL that contains the token, a userinfo password, or a `token` / `access_token` / `authorization` query key is rejected. `candidateContext` sent together with `resolver` is ignored. `approvedAnswers` are rejected when `resolver` is set. The 0.3.0 profile body above still works when `resolver` is omitted.

On each form step the engine inspects the page, derives normalized field keys and question intents, and `POST`s the resolver URL with `Authorization: Bearer <token>`. The token is never placed in the URL. The body is `{sessionId, candidateRef, jobRef, step, fields, questions}`. Returned values are held only for that step and then dropped. Calls use a timeout, a bounded retry count, and a response size limit. Redirects are not followed. `401`, `409`, and `410` are not retried and end the run (`FAILED_FINAL` or `EXPIRED`). A `422` for one step does not fail the session: the keys requested on that step are treated as unresolved, so fields stay blank and questions stay manual, and the log records the category `unprocessable` only.

Status includes `protocolVersion` (the negotiated `0.4.0`, `0.5.0`, `0.6.0`, or `0.7.0`, or `0.6.0` when the client omitted it), `manualQuestions` (`[{intent, text}]` only, and only while `MANUAL_ANSWER_REQUIRED`), and `timings`:

| Field | Meaning |
| --- | --- |
| `sessionCreatedMs` | Time spent handling session create. At least 1 when any time passed. |
| `contextReadyMs` | Time to open this session's browser context. |
| `browserReadyMs` | Same per-session context time. It is not the process startup measurement. |
| `browserPrewarmMs` | Wall time to launch the worker browsers at process start. |
| `firstFormInspectedMs` | Time from the start of the run until the first page inspection. |
| `firstFillMs` | Time from the start of the run until the first field is written. |

`POST /v1/sessions/{sessionId}/continue` accepts `{candidateRef, resumeUploaded, answersUpdated, humanResolved}`. `answersUpdated: true` while `MANUAL_ANSWER_REQUIRED` resumes the same browser context and asks the resolver again for the pending intents. `resumeUploaded: true` while `RESUME_UPLOAD_REQUIRED` continues past the file input. `humanResolved: true` while `LOGIN_REQUIRED`, `CAPTCHA_REQUIRED`, `NO_FORM_FOUND`, or a `CONSENT` / `CONSENT_CHOICE` blocker resumes automation in the same context after a person has solved that stop. `continue` on `CONSENT` or `CONSENT_CHOICE` also re-reads the live control when `humanResolved` is omitted. A selected option, or a checked attestation, is committed and the run moves on. It also resumes after a field was left manual because Auto-Fill could not set it. Both confirmations stay on the session, so a later `/answer`, pause, resume, or `continue` does not ask for the resume or the login again. Protocol `0.5.0` uses the same resolver body as `0.4.0`. Protocol `0.6.0` uses that same resolver body.

Protocol `0.7.0` is opt-in. Send `"protocolVersion": "0.7.0"` with the same resolver body. The default remains `0.6.0`. Sessions created at `0.6.0` or earlier keep every existing field, route, and status. `ACCOUNT_CREATION_REQUIRED` is reported as `LOGIN_REQUIRED` for those sessions, `EMAIL_VERIFICATION_REQUIRED` is reported as `MANUAL_REVIEW_REQUIRED`, and `JOB_DESCRIPTION` is reported as `NO_FORM_FOUND`.

On `0.7.0` the status adds `stateVersion` (it increases when status, blocking, pause, or `progress.stage` changes), `account`, `resume`, `completed` (field keys and question hashes only), and `checkpoint`. `checkpoint.since` is an ISO-8601 UTC timestamp. `progress.stage` can be `ACCOUNT`, `VERIFICATION`, or `CAPTCHA`, taken from the current page. Create Account and Sign In both use stage `ACCOUNT` and current label `Employer account`. On `0.7.0`, `progress.percent` is the workflow stage, kept apart from the field ratio in `progress.fieldPercent`. A job description is 5. Sign-in is 10 and account creation is 15. A form scales from 20 to 80. A resume checkpoint is 70 to 85. Ready to submit is 100. `NO_FORM_FOUND` stays at the last application percent, or `0` when no application form was seen. `percent` does not decrease on the same required-field set. Blocking kinds add `ACCOUNT` (`Create employer account`) and `VERIFICATION` (`Email verification`). A Sign In page that also offers Create Account stays `LOGIN_REQUIRED` with `account.alternateModeAvailable: true`. The engine does not click that switch unless `POST /credentials` sends `mode: "register"`.

`POST /v1/sessions/{sessionId}/credentials` still defaults to `mode: "store"`, which only remembers the secret. `mode` `login` or `register` is allowed while the status is `LOGIN_REQUIRED`, `ACCOUNT_CREATION_REQUIRED`, or `LOGIN_FAILED`. The optional `confirmPassword` defaults to the password. For `mode: "register"` the service types the email, the password, and the verify-password field, reads both password fields back, and clicks Create Account only when those values match each other and the provided password. The values are not logged or returned. The register response and a `422` body may include `fieldProof` with `emailFilled`, `emailLength`, `emailMatches`, `passwordLength`, `verifyLength`, and `passwordsMatch`. Those are lengths and booleans only. When the session was created with `standardTermsAuthorized: true`, a required checkbox classified `STANDARD_TERMS` or `STANDARD_SUBMISSION_CONSENT` is checked and listed on `autoConsents` with that category. Any other checkbox stays unchecked and is listed on `consentRows` with category `CONSENT`. The deny term `ai` matches a whole word, so `email` and `maintain` do not trip it. A mismatch is `409` `password_mismatch` and the button is not clicked. The page is classified again after the click. An OTP or email-verification step is `EMAIL_VERIFICATION_REQUIRED` (or `LOGIN_REQUIRED` with a reason) and the run pauses. The call fills the account form and clicks that form's Create Account, Sign In, Continue, or Next button. Submit Application is still refused. A site validation error is `422` with `reason: "site_rejected"` and `siteErrors`. `account_exists` is the reason when the site says the account is already there. Passwords stay in memory, are redacted, and are wiped on delete, human-done, expiry, and shutdown.

`POST /v1/sessions/{sessionId}/resume-file` is multipart (`file`, `candidateId` or `candidateRef`, optional `fieldHint`, optional `prepare`). It is not the pause/resume route. The default cap is 10 MiB (`AUTOFILL_MAX_RESUME_BYTES`). PDF bytes must start with `%PDF-`. A file over the cap is `413` `too_large`. A file that is not a PDF, DOC, or DOCX is `415` `unsupported_type`. A candidate id that does not match the session is `409` `candidate_mismatch`. An ATS rejection is `422` and can be retried. Acceptance requires an ATS indicator, no upload error, and a file on the control or a hidden file id. The filename alone is not enough. Accepted bytes are kept only until the session ends, and a later empty resume control on that session is filled from those bytes. `prepare=true` is accepted at any non-terminal status, including before a resume control exists. The bytes are held in memory and in a `0700` temp directory under the same size limit, and they are deleted on delete, human-done, expiry, and shutdown. The status then reports `resume.prepared: true` and `resume.verified: false`. A prepared file can be replaced until it is verified. After verification the same route returns `409` `already_verified`. When a later run reaches a resume control, the prepared file is attached and checked with the same verifier. Verification sets `resume.verified`. `RESUME_UPLOAD_REQUIRED` is raised only when that attach or check fails (`resume.siteErrors`) or when nothing was prepared.

`continue` with `humanResolved: true` on `CAPTCHA_REQUIRED` or `EMAIL_VERIFICATION_REQUIRED` checks the page again first. A challenge that is still there is `409` `challenge_present` or `verification_pending`, and the status does not change. Auto-Fill does not read mail and does not solve a challenge. `GET /screenshot` during navigation is `503` with `Retry-After: 1`. `409` means no page is open. Screenshot and interact failures do not by themselves change a live status. Responses include `X-Autofill-State-Version` and `X-Autofill-Viewport: 1280x800`. `/interact` accepts `clickCount`, scroll `dx`, and the keys Delete, Home, End, PageUp, PageDown, and Shift+Tab, including while the two new statuses are current. The limit is 60 actions per minute on `0.7.0` and stays 30 before that.

Protocol `0.6.0` adds `progress`, `paused`, and `blocking` to every status payload. `progress.percent` is `requiredDone / requiredTotal` when required fields were detected, and it is 100 only when every one of them is done. Otherwise it is a fixed value for the stage. It is never random. `currentLabel` is a short section label with emails and phone numbers removed. `blocking` is the single current item. Field kinds are `QUESTION`, `MISSING_PROFILE_VALUE`, `OPTION_MISMATCH`, `DEMOGRAPHIC`, `CONSENT`, and `CONSENT_CHOICE`. Status kinds are `LOGIN`, `CAPTCHA`, `RESUME`, and `NO_FORM`, and protocol `0.7.0` adds `ACCOUNT` and `VERIFICATION`. An acknowledgement blocker (`ACKNOWLEDGMENT` or `ACKNOWLEDGEMENT`) is reported and answered as `CONSENT`. `manualQuestions` is still the full `{intent, text}` list. `POST /v1/sessions/{id}/pause` and `/resume` stop between fields and keep the browser context; both are idempotent and do not start a fill. `POST /v1/sessions/{id}/answer` fills that one open field visibly, checks that the value committed, and clears the highlight. It then reads the page for the next empty required field and outlines that field. Committed fields are not typed again. A full fill runs only when the page or the step changed. A control that was not visible on the previous scan, such as a conditional question, is filled on its own. The response includes `ms`, the time spent filling that field and scanning for the next one. That scan sets `progress.stage` from the page step and the current blocker, the same sections a full fill uses (`PERSONAL`, `ADDRESS`, `EMPLOYMENT`, `EDUCATION`, `QUESTIONS`, `RESUME`, `REVIEW`). Once a form is found, the stage does not return to `OPENING`. `AUTOFILL_MID_TYPE=1` pauses once in the middle of a long text field so a screenshot can catch the typing; the default types the field in one pass. A choice answer matches one option when the value is a unique exact, prefix, or contains match after word folding, so `Bachelor's` selects `Bachelor's Degree` and `United States` selects `United States +1`. A value that is not one option is `422` with reason `needs_option` and those options. `blocking.options` is the full junk-filtered list for every native select, radio, and multi-select blocker, including one found by the next-field scan, and that same list is in the `422` body. The initial fill types each text field through `focusField` and `typeVisible`. A screenshot requested during that fill runs between fields. A combobox answer uses the menu path, including a type-ahead country list. A phone value matches on its digits, ignoring punctuation and a leading calling code. A phone-country menu (`United States +1`), including a Country select next to the phone input, is `phone.country`, filled from the profile country or the phone calling code, and is not an `address.country` blocker. `/answer` writes into the current blocker's element. `/answer` reports `filled: true` only when the scan's committed check agrees. A `200` from `/answer` includes `verified` as `true` or `false` for that check. `filled` is `true` only when `verified` is `true`. Each field is answered at most three times in a session, counted by its question hash rather than by field key and not reset when another question is answered in between. After the third attempt that leaves the same blocker, that field becomes manual (`blocking.text` asks the person to set it in the live view and Continue), `/answer` returns `409` with reason `needs_manual`, and `/interact` stays available. A current-location answer uses the suggestion list and commits only when that suggestion is recorded. The red outline is the visible control (the react-select box or the question wrapper), drawn with an outline and a box-shadow so `overflow: hidden` cannot clip it. An unknown target is `409` with reason `stale_blocker`. A value that matches no option, or more than one option, is `422` with reason `needs_option` and the option list. The item stays blocked. `POST /v1/sessions/{id}/suggestions` accepts `{questionId or fieldKey, query}` for the current blocker. `POST /v1/sessions/{id}/suggest` is the same route. It types the query into that combobox, waits for the list, and returns `{options: [{label, value?}], source: "live" or "static"}` without choosing a row. A native select, radio, or multi-select returns `source: "static"` and is not typed into. The typed query is cleared unless that blocker was already answered. `/answer` with an exact option label commits that option and checks that it stuck. A location answer is a city, a state, or a full place. On a location field that needs a city, a state alone (`florida`, `FL`, `Florida, United States`, or `Florida, USA`) is `422` `needs_option` with the places the list showed, and the typed state is cleared. `/suggestions` and that option list omit a state-only label, so every label they offer can be committed. Required checkboxes stay in the required set, so READY does not occur while one is unchecked. A factual yes/no checkbox is filled when an approved answer matches. A required checkbox whose whole statement is a fixed terms or privacy sentence is checked when `standardTermsAuthorized` is true, and that acceptance is listed on `autoConsents`. Marketing, SMS, promotional mail, AI or recording consent, arbitration, e-sign, a background check, a certification or attestation, and any other unusual statement stay `CONSENT` blockers and are not checked. When the flag is false or omitted, terms boxes stay unchecked too. The blocker kind is `CONSENT` and its text is the statement, at most 600 characters. `/answer` checks it only when the value is `confirm` and the body sends `confirm: true`. A radio or select that offers both a consent option and a decline option (`Yes, I consent` / `No, I do not consent`, `Opt out`) is `CONSENT_CHOICE`. `blocking.options` is those labels. A decline option is allowed without `confirm`. An agree option needs `confirm: true`. An agree option without `confirm: true` returns `422` `needs_confirm`. Neither side is selected from the resolver or from a saved answer. The group blocks only when the page marks it required (asterisk, a required class, or `aria-required`). An optional opt-in stays blank and does not block READY. `CONSENT` and `CONSENT_CHOICE` are human stops: `/interact` is allowed, and `continue` re-reads the live control. An optional marketing opt-in is never checked. Demographic questions stay manual. Non-2xx responses from `/answer`, `/interact`, `/continue`, `/suggestions`, and `/suggest` use a machine `reason`: `needs_option`, `needs_confirm`, `needs_manual`, `rate_limited`, `not_at_human_stop`, `session_expired`, `element_not_found`, `stale_blocker`, or `busy`. Two `/answer` calls on one session at the same time return `409` `busy`. `Idempotency-Key` on `/answer` replays the stored status and body. A candidate id that does not match the session is `needs_manual` on those four routes. An `/interact` fault while the page is navigating or has crashed is `element_not_found`. `/continue` still returns `challenge_present` or `verification_pending` when a person says that stop is done and the page still shows it. The value is held in memory and is not logged. Nothing is filled or polled while `LOGIN` or `CAPTCHA` is the blocker. Text is typed through `window.__afUI`; a human-action field is outlined in red and scrolled to the center. Known fields are not outlined.

`GET /v1/sessions/{sessionId}/screenshot` returns `image/jpeg` (quality 60, 1280x800 viewport) for the current page. Unknown sessions are `404`. A session with no open page is `409`. The bytes are not logged or written to disk. Response headers are `X-Autofill-Status` and `X-Autofill-Url-Host` (hostname only). The capture runs on the session's browser worker.

`POST /v1/sessions/{sessionId}/interact` performs one action while the session is in `LOGIN_REQUIRED`, `CAPTCHA_REQUIRED`, `MANUAL_ANSWER_REQUIRED`, `RESUME_UPLOAD_REQUIRED`, `NO_FORM_FOUND`, or `READY_FOR_HUMAN_SUBMIT`: `{action: click, x, y}`, `{action: type, text}` (at most 500 characters), `{action: key, key}` for Enter, Tab, Backspace, Escape, the arrow keys, or Space, or `{action: scroll, dy}`. The response is `{status, screenshotVersion}`. A candidate mismatch or any other status is `409`. Typed text is not logged. A session is limited to 30 actions per minute. A human click is a coordinate and may land on Submit. Automation still never clicks Submit.

`POST /v1/sessions/{sessionId}/human-done` accepts `{candidateRef, outcome: submitted|abandoned}`, records `humanOutcome`, closes the browser context, and returns `200`.

Before each inspection the engine waits for the page to render. A page that already shows a form returns immediately. Otherwise it waits for network idle, then polls for up to 12 seconds for form controls or known markers. Every page is classified as `JOB_DESCRIPTION`, `APPLY_ENTRY`, `LOGIN`, `ACCOUNT`, `APPLICATION_FORM`, `REVIEW`, `CONFIRMATION`, `SITE_ERROR`, or `UNKNOWN`, and that value is `pageType` on the status. A page that says the site failed (`Problem with the service`, `please try again` with no application form, `something went wrong`, `temporarily unavailable`, or 5xx text) is `SITE_ERROR` even when the header has a Sign in link. The run stops as `FAILED_RETRYABLE` with message `site_error`, which `/retry` accepts without spending the browser-crash budget. On a job description or apply entry the engine clicks the employer's own entry control (`Apply for this job`, `Apply now`, `I'm interested`, Greenhouse `#apply_button`, a Lever `.postings-btn` apply link, or Workday `Apply` then `Apply Manually`) at most twice, then looks again. A control whose label is submit or send application is never clicked. An account pane with a password, a verify-password field, and Create Account is `ACCOUNT_CREATION_REQUIRED`, `pageType` `ACCOUNT`, and `account.mode` `register`. A sign-in pane is `LOGIN_REQUIRED` and `pageType` `LOGIN`. That score happens before any zero-field or `NO_FORM_FOUND` decision. `NO_FORM_FOUND` is only a page that is not a job, login, account, captcha, application form, or review, after at most two safe Apply hops and a settle wait. `READY_FOR_HUMAN_SUBMIT` requires `pageType` `APPLICATION_FORM` or `REVIEW`, fields detected greater than zero, every required field committed, no unresolved blocker, and a submit or review control. It is never an account, login, captcha, or resume checkpoint, and it is never a screenshot, a navigation-only page, or an empty adapter result. Zero fields never become `READY_FOR_HUMAN_SUBMIT` and never report 100 percent. A job description is `JOB_DESCRIPTION` at 5 percent, not `NO_FORM_FOUND`. On protocol `0.7.0` the checkpoint kind is `NO_FORM` and `checkpoint.reason` is `job_description`, `apply_entry`, `confirmation`, or `no_application_form`. The percent stays at the last real application percent, or `0` when no form was seen.

Status also includes `phase` (`launching`, `navigating`, or `loaded`) and `startedAt` (ISO-8601 UTC, null before start). Browser launch and the first navigation share a 45 second budget and one retry. After that the status is `FAILED_RETRYABLE` with message `navigation_timeout`, `browser_crashed`, `blocked`, or `capacity`, and the context is closed. The first JPEG is taken when the document commits (`domcontentloaded`) and later frames replace it, including while a fill is running. A crashed page reports `browser_crashed` for that startup failure (an in-run crash still uses `browser_crash`) and does not wait forever. `POST /v1/sessions` returns `503` with `Retry-After` and `reason: capacity` when memory admission refuses a new session, and no session is stored. `POST /v1/sessions/{sessionId}/retry` navigates again in the same session with the same bindings.

The process prewarms one Chromium per worker thread (`AUTOFILL_BROWSER_WORKERS`, default 2). Chromium is launched with a shared-memory workaround, GPU disabled, a renderer cap, and a JavaScript heap cap. Video, fonts, and analytics requests are not loaded. A session is pinned to one worker. Each session gets a new browser context that is never reused. Before that context is opened, memory is read from the container cgroup (`memory.current` minus `inactive_file`, then the cgroup v1 equivalent, then PSS or USS from `smaps_rollup`). Shared pages are not added once per process. The limit is `AUTOFILL_MEMORY_LIMIT_MB`, or 85% of a finite cgroup maximum, or 1536 MB. The check is repeated briefly. A session that is still over the limit is `FAILED_RETRYABLE` with category `capacity_busy`, which is not a browser crash. A browser or tab crash closes that context, records `FAILED_RETRYABLE` with category `browser_crash`, and `continue` or `humanResolved` can start again from the application URL up to two more times. A screenshot or interaction on a crashed page returns 410 and the status becomes `FAILED_RETRYABLE`. A navigation timeout records `FAILED_RETRYABLE` with category `ats_timeout`. There is no persistent profile directory.

A background reaper closes an expired session's context and wipes the resolver token, site credentials, and held values, then sets `EXPIRED`. A resolver `410` does the same close and wipe when the run ends. After that, `status`, `screenshot`, `interact`, `pause`, `resume`, `answer`, `continue`, and `credentials` return `410` and do not pause, fill, capture, or store anything. Shutdown does the same wipe. Logs for resolver steps record the session id, step, adapter, field keys, intents, counts, result codes, and latency. They do not record values, emails, phones, or tokens.

Question classification is local. Level 1 maps normalized text and synonyms to a canonical intent. Level 2 is an in-process token matcher and does not call a network or a language model. Level 3 is an in-process hook, off by default. A hook would receive sanitized question text, option labels, and the intent catalogue, and would return an intent only. A value is written only when both the local match and the resolver confidence are `HIGH`. An unknown question (`intent` null) is filled only when the resolver answer for that question has source `saved_answer` and confidence `HIGH`. The answer may be keyed by the question `id` (sha256 of the normalized text, plus the field id, name, or selector when the control has one), echoed `text`, `questionId`, or `questionHash`. Two fields with the same label do not share an id. An answer that has none of those is applied only when the step asked a single unclassified question. When more than one unclassified question was asked, that answer is not assigned. A referral question (`REFERRAL`: referral, referrer, who referred you, employee referral) is never filled from an employer or work history. Demographic questions, including Hispanic/Latino, race, veteran, and disability, are never filled and are listed for a person. A preferred-name field is filled only from an explicit `preferredName`; it is left blank when that value is absent. A choice control is filled when the saved value is a unique exact match, an allowed equivalent, or a unique prefix or contains match after word folding (`Bachelor's` selects `Bachelor's Degree`). An ambiguous match is not written. A text control receives the saved text. Demographic intents are never filled. Legal and authorization intents are never filled below `HIGH`. Option labels match when they are exact, an allowed equivalent (`Yes`/`No`, `True`/`False`, `Authorized`/`Not Authorized`, and the exact percent buckets `0`/`25`/`50`/`75`/`100`), or the only option whose words start with or contain the saved words. `Yes, I think so` is not a match for `Yes`. A multiselect is checked only when every saved value maps. A label that repeats its accessible name is sent once, and a trailing `*` is not part of the question.

### Clarifications

- Address components are requested as `address.line1`, `address.region`, `address.postalCode`, and `address.country`, not as a single `address.*` wildcard. A current-location field also requests `address.state` and uses it as the region when `address.region` is absent. The state and country may be a full name or an abbreviation (`TX` or `Texas`, `US` or `USA`). The city comparison ignores case. The city is typed slowly, and the step waits for the suggestion list, including a list rendered outside the field. If a click does not commit the suggestion, ArrowDown and Enter are tried. The answer is committed only when a suggestion was clicked and the hidden selected-location value is JSON with an id from that suggestion. An empty object, a typed city, or a plain string is not a commit. An explicit answer that names only the city, when several suggestions share that city, keeps the first suggestion the list ranked. A profile fill with a city and no region still leaves the field manual. The suggestion list is waited on for up to 5 seconds, until it is visible or the search response has finished. A request that is still running is not an empty list. If the Lever suggestion request returns 4xx, or an empty list after that wait, that answer stops after one try and `blocking.text` is `Lever didn't offer location suggestions; pick Current location in the live view, then Continue`. A required typeahead, including an Ashby location box whose label carries a required mark or `aria-required`, stays open until a suggestion is selected. Ashby commits when the box shows a full place — the input value, the text on a selected-value element, or a hidden input in the field — and the suggestion list is closed. The labels produced by that typing are sent on `blocking.options`. `requiredDone` and `requiredTotal` are both counted from the required fields of the current scan, and `requiredDone` is never greater than `requiredTotal`. Before READY, every visible required field is read again. When a location field stays manual, the log records a reason (`no_city`, `type_error`, `no_suggestions`, `ambiguous`, `no_match`, or `not_committed`) and, for a list that was shown, the suggestion labels as city and region text only. The field stays manual. Each step also logs the requested field key names and the returned field key names, never the values. A react-select choice is kept only when a hidden input or aria state records it and no field error is showing; otherwise it is typed and confirmed with Enter, or left manual. A longer question that mentions "current location", including a sponsorship question, is sent as a question. Select questions, including a closed react-select whose options are not on the page yet, are extracted and sent.
- `DELETE` wipes secrets and removes the session (`204`, then `404`). `CANCELLED` is set on the detached object and is not kept as a tombstone.
- The first unresolved question stops at `MANUAL_ANSWER_REQUIRED`. `answersUpdated` asks the resolver again. A question that is still unresolved, or whose saved value does not match an option, stops again at `MANUAL_ANSWER_REQUIRED`. An optional question with no saved answer may stay blank when the page advances, and it is still listed on `manualQuestions`. A missing resume grant does not skip the rest of that step: every field and question is still requested, HIGH answers are written, and the stop is `RESUME_UPLOAD_REQUIRED` with the unanswered questions still on `manualQuestions`.
- A Level 3 hook is capped at `MEDIUM`, so it cannot cause a fill. There is no network client for Level 3.
- "Now or in the future" sponsorship, including "now or will you in the future", maps to `SPONSORSHIP_NOW_OR_FUTURE`. That includes "sponsorship for a visa", "visa sponsorship", "employment visa status", and "remain in your country" or "the US" when the question is about a visa or sponsorship. A future-only phrase maps to `SPONSORSHIP_FUTURE`. A now or currently phrase maps to `SPONSORSHIP_NOW`. If the resolver leaves `SPONSORSHIP_NOW_OR_FUTURE` unresolved, the step asks for `SPONSORSHIP_NOW` and `SPONSORSHIP_FUTURE` and combines them: Yes when either saved answer is Yes, No only when both are No, and manual otherwise. "Authorized to work ... without sponsorship" maps to `AUTHORIZED_WITHOUT_SPONSORSHIP` and is not filled from work authorization alone. Sponsorship and authorization answers are written only at HIGH confidence.
- The 0.3.0 statuses `LOGIN_FAILED`, `FILLED`, `UNSUPPORTED`, `MANUAL_REVIEW_REQUIRED`, `APPLICATION_READY`, and `FAILED` remain for the profile mode.
- `browserPrewarmMs` is the process startup prewarm. `contextReadyMs` and `browserReadyMs` are the per-session context open. `sessionCreatedMs` is create handling.

## Status values

Profile mode keeps the earlier statuses. Resolver mode also uses the protocol 0.4.0 statuses below.

| Status | Meaning |
| --- | --- |
| `CREATED` | Session is stored. `start` has not run. |
| `LOGIN_REQUIRED` | The page needs a sign-in and this session has no credentials. Nothing was typed. |
| `ACCOUNT_CREATION_REQUIRED` | Create Account: email, password, and verify password. Protocol `0.7.0`. Older sessions report `LOGIN_REQUIRED`. |
| `EMAIL_VERIFICATION_REQUIRED` | The site asked for a code or an inbox check. The run pauses. Older sessions report `MANUAL_REVIEW_REQUIRED`. |
| `JOB_DESCRIPTION` | A posting, after at most two Apply hops, with no application form. Older sessions report `NO_FORM_FOUND`. |
| `LOGIN_FAILED` | The site rejected the login. Filling did not start. |
| `CAPTCHA_REQUIRED` | A visible challenge or bot wall is present. The run stopped. A bot wall's message is `site blocked automated access`. |
| `NO_FORM_FOUND` | The page is not an application form. `checkpoint.reason` names why. |
| `RESUME_UPLOAD_REQUIRED` | A person uploads the resume, or a `resume.file` download failed. |
| `MANUAL_REVIEW_REQUIRED` | Account creation, or a host left for a person. |
| `READY_FOR_HUMAN_SUBMIT` | An application or review page, with fields detected, every required field committed, no blocker, and a submit or review control. |
| `FILLED` | No further safe page turn. |
| `UNSUPPORTED` | Identity-provider SSO. Not an ATS password form. |
| `FAILED` | The run stopped on an unexpected error. The message names the error type only. |
| `STARTED` | A run has begun for this session. |
| `FORM_IN_PROGRESS` | Resolver mode is filling the current step. |
| `MANUAL_ANSWER_REQUIRED` | A question was not `HIGH` confidence. `manualQuestions` lists `{intent, text}` only. |
| `FAILED_RETRYABLE` | A bounded failure (validation, or a browser fault after one relaunch). The same session can be started again. |
| `FAILED_FINAL` | The resolver rejected the call (`401` or `409`) or the response was invalid. |
| `CANCELLED` | Recorded when a session is deleted. The store does not keep a tombstone. |
| `EXPIRED` | The TTL elapsed, or the resolver returned `410`. Secrets are wiped and the browser context is closed. Later calls return `410`. |

`stopped_before_submit` is true on every success body. `loginStatus` is `NOT_REQUIRED`, `LOGIN_REQUIRED`, `AUTHENTICATED`, `LOGIN_FAILED`, or `CAPTCHA_REQUIRED` after a run.

## Container

The Dockerfile in the repository root builds an image that contains this package and Chromium. It does not contain a service token. Pass `AUTOFILL_SERVICE_TOKEN` at runtime. The image sets `AUTOFILL_RUN_BROWSER=1` and `AUTOFILL_BROWSER_NO_SANDBOX=1`.

```bash
docker build -t auto-fill .
docker run --rm -p 8080:8080 -e AUTOFILL_SERVICE_TOKEN -e PORT=8080 auto-fill
```

Use a health check on `GET /health`. Sessions are memory-only, so a new instance starts empty. The browser runs inside the container. A person can view the current page and send one bounded action through the screenshot and interact routes. The JSON API still does not accept resume bytes.

## Errors

| Code | When |
| --- | --- |
| 401 | Missing or wrong bearer token. |
| 404 | Unknown session. |
| 409 | Candidate or site mismatch, illegal transition, submit refused, or browser disabled. |
| 410 | The session expired or its TTL was reaped. The call has no side effects. |
| 422 | Body failed validation. Rejected values are not echoed. |
| 429 | Too many open sessions. |
