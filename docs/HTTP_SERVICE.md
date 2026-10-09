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

Missing and wrong tokens both receive `401` and `{"detail":"Unauthorized"}`. `GET /health` does not require auth. It returns `{"status":"ok","service":"auto-fill"}` and nothing about sessions.

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

Open the application URL and fill until the engine stops. Optional body: `{"candidateId":"candidate-example"}`. Allowed from `CREATED`, `LOGIN_REQUIRED`, and `LOGIN_FAILED`.

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

`201` returns `{sessionId, status: CREATED, stopped_before_submit: true, ...}`. The resolver token is not echoed. A resolver URL that contains the token, a userinfo password, or a `token` / `access_token` / `authorization` query key is rejected. `candidateContext` and `approvedAnswers` are rejected when `resolver` is set. The 0.3.0 profile body above still works when `resolver` is omitted.

On each form step the engine inspects the page, derives normalized field keys and question intents, and `POST`s the resolver URL with `Authorization: Bearer <token>`. The token is never placed in the URL. The body is `{sessionId, candidateRef, jobRef, step, fields, questions}`. Returned values are held only for that step and then dropped. Calls use a timeout, a bounded retry count, and a response size limit. Redirects are not followed. `401`, `409`, and `410` are not retried and end the run (`FAILED_FINAL` or `EXPIRED`). A `422` for one step does not fail the session: the keys requested on that step are treated as unresolved, so fields stay blank and questions stay manual, and the log records the category `unprocessable` only.

Status includes `protocolVersion`, `manualQuestions` (`[{intent, text}]` only, and only while `MANUAL_ANSWER_REQUIRED`), and `timings`:

| Field | Meaning |
| --- | --- |
| `sessionCreatedMs` | Time spent handling session create. At least 1 when any time passed. |
| `contextReadyMs` | Time to open this session's browser context. |
| `browserReadyMs` | Same per-session context time. It is not the process startup measurement. |
| `browserPrewarmMs` | Wall time to launch the worker browsers at process start. |
| `firstFormInspectedMs` | Time from the start of the run until the first page inspection. |
| `firstFillMs` | Time from the start of the run until the first field is written. |

`POST /v1/sessions/{sessionId}/continue` accepts `{candidateRef, resumeUploaded, answersUpdated, humanResolved}`. `answersUpdated: true` while `MANUAL_ANSWER_REQUIRED` resumes the same browser context and asks the resolver again for the pending intents. `resumeUploaded: true` while `RESUME_UPLOAD_REQUIRED` continues past the file input. `humanResolved: true` while `LOGIN_REQUIRED`, `CAPTCHA_REQUIRED`, or `NO_FORM_FOUND` resumes automation in the same context after a person has solved that stop. Protocol `0.5.0` uses the same resolver body as `0.4.0`.

`GET /v1/sessions/{sessionId}/screenshot` returns `image/jpeg` (quality 60, 1280x800 viewport) for the current page. Unknown sessions are `404`. A session with no open page is `409`. The bytes are not logged or written to disk. Response headers are `X-Autofill-Status` and `X-Autofill-Url-Host` (hostname only). The capture runs on the session's browser worker.

`POST /v1/sessions/{sessionId}/interact` performs one action while the session is in `LOGIN_REQUIRED`, `CAPTCHA_REQUIRED`, `MANUAL_ANSWER_REQUIRED`, `RESUME_UPLOAD_REQUIRED`, `NO_FORM_FOUND`, or `READY_FOR_HUMAN_SUBMIT`: `{action: click, x, y}`, `{action: type, text}` (at most 500 characters), `{action: key, key}` for Enter, Tab, Backspace, Escape, the arrow keys, or Space, or `{action: scroll, dy}`. The response is `{status, screenshotVersion}`. A candidate mismatch or any other status is `409`. Typed text is not logged. A session is limited to 30 actions per minute. A human click is a coordinate and may land on Submit. Automation still never clicks Submit.

`POST /v1/sessions/{sessionId}/human-done` accepts `{candidateRef, outcome: submitted|abandoned}`, records `humanOutcome`, closes the browser context, and returns `200`.

Before each inspection the engine waits for the page to render (network idle, then a poll of up to 12 seconds for form controls or known markers). A non-review step with no fields ends as `NO_FORM_FOUND`, not `READY_FOR_HUMAN_SUBMIT`.

The process prewarms one Chromium per worker thread (`AUTOFILL_BROWSER_WORKERS`, default 2). Chromium is launched with a shared-memory workaround, GPU disabled, a renderer cap, and a JavaScript heap cap. Video, fonts, and analytics requests are not loaded. A session is pinned to one worker. Each session gets a new browser context that is never reused. Before that context is opened, memory is read from the container cgroup (`memory.current` minus `inactive_file`, then the cgroup v1 equivalent, then PSS or USS from `smaps_rollup`). Shared pages are not added once per process. The limit is `AUTOFILL_MEMORY_LIMIT_MB`, or 85% of a finite cgroup maximum, or 1536 MB. The check is repeated briefly. A session that is still over the limit is `FAILED_RETRYABLE` with category `capacity_busy`, which is not a browser crash. A browser or tab crash closes that context, records `FAILED_RETRYABLE` with category `browser_crash`, and `continue` or `humanResolved` can start again from the application URL up to two more times. A screenshot or interaction on a crashed page returns 410 and the status becomes `FAILED_RETRYABLE`. A navigation timeout records `FAILED_RETRYABLE` with category `ats_timeout`. There is no persistent profile directory.

A background reaper closes an expired session's context and wipes the resolver token, site credentials, and held values, then sets `EXPIRED`. Shutdown does the same wipe. Logs for resolver steps record the session id, step, adapter, field keys, intents, counts, result codes, and latency. They do not record values, emails, phones, or tokens.

Question classification is local. Level 1 maps normalized text and synonyms to a canonical intent. Level 2 is an in-process token matcher and does not call a network or a language model. Level 3 is an in-process hook, off by default. A hook would receive sanitized question text, option labels, and the intent catalogue, and would return an intent only. A value is written only when both the local match and the resolver confidence are `HIGH`. An unknown question (`intent` null) is filled only when the resolver answer for that exact question has source `saved_answer` and confidence `HIGH`, and a choice control still needs an exact option. Demographic intents are never filled. Legal and authorization intents are never filled below `HIGH`. Option labels match only when they are exact or an allowed equivalent (`Yes`/`No`, `True`/`False`, `Authorized`/`Not Authorized`, and the exact percent buckets `0`/`25`/`50`/`75`/`100`). A multiselect is checked only when every saved value maps.

### Clarifications

- Address components are requested as `address.line1`, `address.region`, `address.postalCode`, and `address.country`, not as a single `address.*` wildcard.
- `DELETE` wipes secrets and removes the session (`204`, then `404`). `CANCELLED` is set on the detached object and is not kept as a tombstone.
- The first unresolved question stops at `MANUAL_ANSWER_REQUIRED`. `answersUpdated` asks the resolver again. A question that is still unresolved, or whose saved value does not match an option, stops again at `MANUAL_ANSWER_REQUIRED`. An optional question with no saved answer may stay blank when the page advances, and it is still listed on `manualQuestions`.
- A Level 3 hook is capped at `MEDIUM`, so it cannot cause a fill. There is no network client for Level 3.
- "Now or in the future" sponsorship, including "now or will you in the future", maps to `SPONSORSHIP_NOW_OR_FUTURE`. A future-only phrase maps to `SPONSORSHIP_FUTURE`. A now or currently phrase maps to `SPONSORSHIP_NOW`. If the resolver leaves `SPONSORSHIP_NOW_OR_FUTURE` unresolved, the step asks for `SPONSORSHIP_NOW` and `SPONSORSHIP_FUTURE` and combines them: Yes when either saved answer is Yes, No only when both are No, and manual otherwise. "Authorized to work ... without sponsorship" maps to `AUTHORIZED_WITHOUT_SPONSORSHIP` and is not filled from work authorization alone. Sponsorship and authorization answers are written only at HIGH confidence.
- The 0.3.0 statuses `LOGIN_FAILED`, `FILLED`, `UNSUPPORTED`, `MANUAL_REVIEW_REQUIRED`, `APPLICATION_READY`, and `FAILED` remain for the profile mode.
- `browserPrewarmMs` is the process startup prewarm. `contextReadyMs` and `browserReadyMs` are the per-session context open. `sessionCreatedMs` is create handling.

## Status values

Profile mode keeps the earlier statuses. Resolver mode also uses the protocol 0.4.0 statuses below.

| Status | Meaning |
| --- | --- |
| `CREATED` | Session is stored. `start` has not run. |
| `LOGIN_REQUIRED` | The page needs a login and this session has no credentials. Nothing was typed. |
| `LOGIN_FAILED` | The site rejected the login. Filling did not start. |
| `CAPTCHA_REQUIRED` | A visible challenge or bot wall is present. The run stopped. |
| `NO_FORM_FOUND` | A non-review step rendered with no application fields. |
| `RESUME_UPLOAD_REQUIRED` | A person uploads the resume, or a `resume.file` download failed. |
| `MANUAL_REVIEW_REQUIRED` | Account creation, or a host left for a person. |
| `READY_FOR_HUMAN_SUBMIT` | Review, or a final submit control was seen and not clicked. |
| `FILLED` | No further safe page turn. |
| `UNSUPPORTED` | Identity-provider SSO. Not an ATS password form. |
| `FAILED` | The run stopped on an unexpected error. The message names the error type only. |
| `STARTED` | A run has begun for this session. |
| `FORM_IN_PROGRESS` | Resolver mode is filling the current step. |
| `MANUAL_ANSWER_REQUIRED` | A question was not `HIGH` confidence. `manualQuestions` lists `{intent, text}` only. |
| `FAILED_RETRYABLE` | A bounded failure (validation, or a browser fault after one relaunch). The same session can be started again. |
| `FAILED_FINAL` | The resolver rejected the call (`401` or `409`) or the response was invalid. |
| `CANCELLED` | Recorded when a session is deleted. The store does not keep a tombstone. |
| `EXPIRED` | The TTL elapsed, or the resolver returned `410`. Secrets are wiped. `start` is rejected. |

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
| 422 | Body failed validation. Rejected values are not echoed. |
| 429 | Too many open sessions. |
