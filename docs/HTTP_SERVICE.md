# Auto-Fill HTTP service

This document is the network protocol for the public Auto-Fill program. Auto-Fill is the service described here. Callers are separate applications. This repository does not contain those applications, their private APIs, their database schemas, or their credentials.

The license is AGPL-3.0-only. See [LICENSE](../LICENSE) and [NOTICE](../NOTICE). Form-filling behavior is adapted from [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) (AGPL-3.0-only). Because this is a network service, AGPL section 13 applies to this program: users who interact with it remotely are offered the corresponding source of this program.

The in-process Python API still exists. This service calls `autofill_application` and the same human-submit guard.

## What the service will not do

- It will not click Submit, Submit Application, Finish, Complete, or Apply. There is no submit route. `stopped_before_submit` is always true. A refused submit raises `HumanSubmissionRequired` inside the process and the HTTP call returns 409.
- It will not upload a resume. A resume file input stops the run with `RESUME_UPLOAD_REQUIRED`. The caller confirms, after a person has uploaded, with `resumeUploaded: true`. The service does not accept resume bytes.
- It will not solve, bypass, or inject a token for CAPTCHA or any other challenge. The status is `CAPTCHA_REQUIRED` and continue is rejected.
- It will not open a caller database. `DATABASE_URL` is ignored. Do not send database URLs, API tokens, or resume files in the JSON body.
- It will not log raw profile values, passwords, or the service token. Logs contain the session id, status, ATS name, and step.

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
| `AUTOFILL_MAX_SESSIONS` | no | Cap on sessions kept in memory. Default `32`. |
| `AUTOFILL_BROWSER_NO_SANDBOX` | no | `1` adds Chromium `--no-sandbox`. The container image sets this. Leave it unset on a normal desktop. |

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

## Status values

The engine statuses are unchanged:

| Status | Meaning |
| --- | --- |
| `CREATED` | Session is stored. `start` has not run. |
| `LOGIN_REQUIRED` | The page needs a login and this session has no credentials. Nothing was typed. |
| `LOGIN_FAILED` | The site rejected the login. Filling did not start. |
| `CAPTCHA_REQUIRED` | A challenge widget is present. The run stopped. |
| `RESUME_UPLOAD_REQUIRED` | A person uploads the resume, then the caller continues. |
| `MANUAL_REVIEW_REQUIRED` | Account creation, or a host left for a person. |
| `READY_FOR_HUMAN_SUBMIT` | Review, or a final submit control was seen and not clicked. |
| `FILLED` | No further safe page turn. |
| `UNSUPPORTED` | Identity-provider SSO. Not an ATS password form. |
| `FAILED` | The run stopped on an unexpected error. The message names the error type only. |

`stopped_before_submit` is true on every success body. `loginStatus` is `NOT_REQUIRED`, `LOGIN_REQUIRED`, `AUTHENTICATED`, `LOGIN_FAILED`, or `CAPTCHA_REQUIRED` after a run.

## Container

The Dockerfile in the repository root builds an image that contains this package and Chromium. It does not contain a service token. Pass `AUTOFILL_SERVICE_TOKEN` at runtime. The image sets `AUTOFILL_RUN_BROWSER=1` and `AUTOFILL_BROWSER_NO_SANDBOX=1`.

```bash
docker build -t auto-fill .
docker run --rm -p 8080:8080 -e AUTOFILL_SERVICE_TOKEN -e PORT=8080 auto-fill
```

Use a health check on `GET /health`. Sessions are memory-only, so a new instance starts empty. The browser runs inside the container. This API does not expose that browser to a person and does not accept the resume file. `continue` is only the confirmation checkpoint.

## Errors

| Code | When |
| --- | --- |
| 401 | Missing or wrong bearer token. |
| 404 | Unknown session. |
| 409 | Candidate or site mismatch, illegal transition, submit refused, or browser disabled. |
| 422 | Body failed validation. Rejected values are not echoed. |
| 429 | Too many open sessions. |
