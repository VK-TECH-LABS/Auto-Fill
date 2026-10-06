# TileArc integration contract

Auto-Fill is a standalone library. TileArc can call it later. This repository does not import TileArc, does not open TileArc's database, and does not read candidate rows, resumes, or credentials from anywhere except the arguments of one call.

## What TileArc already does

1. The person selects a candidate.
2. TileArc shows jobs and downloads a tailored resume for that candidate.
3. Apply opens the external ATS in a browser.

Auto-Fill starts at that open page.

## What Auto-Fill does

`autofill_application(application_url, candidate_profile, credential_provider, options)`:

1. Records the site and detects the ATS from `application_url`.
2. If the page is a password login, asks `credential_provider` for that candidate and that domain, then signs in. Email-first sites (email, Continue, password, Sign in) are handled. A page that is already signed in skips login.
3. Fills profile fields and explicit application answers.
4. Clicks Next, Continue, Save and Continue, or Proceed when that control is not a final submission.
5. Stops at a resume file input with status `RESUME_UPLOAD_REQUIRED`. It does not choose a file. The person uploads the resume TileArc already downloaded.
6. A later call with `AutofillOptions.resume_uploaded=True` continues past that file.
7. Stops on the review step with status `READY_FOR_HUMAN_SUBMIT`. The person clicks Submit or Apply.

## Call shape

```python
from autofill import AutofillOptions, CandidateProfile, autofill_application

result = autofill_application(
    application_url,          # the ATS URL TileArc opened
    candidate_profile,        # CandidateProfile built by TileArc for this call
    credential_provider,      # TileArc resolves this at runtime; may be None
    AutofillOptions(
        job_id=job_id,
        candidate_id=candidate_id,
        session_id=session_id,
        page=playwright_page,  # the page TileArc already opened
        resume_uploaded=False,
        cover_letter_text=None,  # only when a person explicitly provided one
    ),
)
```

Future inputs, all supplied by the host at call time:

| Input | Owner | Notes |
| --- | --- | --- |
| `application_url` | TileArc | Used for ATS detection and credential scope. |
| `candidate_profile` | TileArc | Serialized from TileArc's own record into `CandidateProfile`. |
| `credential_provider` | TileArc | `get_credentials(site_context, candidate_id)` returns site, username, email, and password for that call only. |
| `options.page` | TileArc | An existing Playwright page. Auto-Fill does not close it. |
| `options.job_id`, `candidate_id`, `session_id` | TileArc | Scope the session. Parallel applications do not share state. |
| `options.cover_letter_text` or `cover_letter_path` | TileArc | Used only when the product already has that text or file. Auto-Fill does not write a cover letter. |
| `options.resume_uploaded` | The person | True only after they attached the downloaded resume. |

Auto-Fill does not accept a database URL, a TileArc API token, or a resume filesystem path for the application resume. `resume_path` on the older `fill_application` helper is ignored.

## Status values

| Status | Meaning for the product UI |
| --- | --- |
| `LOGIN_REQUIRED` | No credentials for this candidate and domain. Nothing was typed. |
| `LOGIN_FAILED` | The site rejected the login. Filling did not start. |
| `CAPTCHA_REQUIRED` | A challenge widget is on the page. Auto-Fill stopped. It does not solve CAPTCHAs. |
| `APPLICATION_READY` | Recorded as a step after auth, before filling. |
| `RESUME_UPLOAD_REQUIRED` | The person uploads the resume, then the host calls again with `resume_uploaded=True`. |
| `MANUAL_REVIEW_REQUIRED` | Account creation, a manual-only host, or a field left for a person. |
| `READY_FOR_HUMAN_SUBMIT` | Review (or the last fillable page). The person clicks Submit. |
| `FILLED` | No further safe page turn, and no final submit control was required to stop. |
| `UNSUPPORTED` | An identity-provider SSO host, not an ATS password form. |
| `FAILED` | The run stopped on an unexpected error. The message names the error type only. |

`result.stopped_before_submit` is always true. `result.login_status` is `NOT_REQUIRED`, `LOGIN_REQUIRED`, `AUTHENTICATED`, `LOGIN_FAILED`, or `CAPTCHA_REQUIRED`.

## Sessions and credentials

Each call builds its own session from `candidate_id + job_id + application_url + session_id`. There is no process-wide "last profile". Two calls in parallel cannot read each other's profile or credentials.

`MemoryCredentialProvider` keys records by `(candidate_id, domain)`. A lookup does not fall back to another candidate or another site. Hosts should apply the same scope in their own provider. Credential objects redact their values in `repr`. Logs record ids, ATS name, domain, state, and counts.

## What stays human

- Uploading the resume TileArc downloaded.
- Clicking the final Submit, Submit Application, Finish, Complete, or Apply control.
- Any CAPTCHA, puzzle, or anti-bot challenge. Detection only.
- Creating an account, SSO through Google, Microsoft, Okta, Auth0, or Cisco, and the manual host `ibegin.tcsapps.com`.
- Legal attestations ("I agree", "I certify") and questions that are not on the profile.

Login submission is allowed and is a different action from final application submit. The central guard raises `HumanSubmissionRequired` if any code path tries to activate a final submit control, including a login-purpose click.
