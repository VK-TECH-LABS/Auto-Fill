# TileArc integration contract

Auto-Fill is a standalone program. A host can call the authenticated HTTP API in [HTTP_SERVICE.md](HTTP_SERVICE.md). That service is this public Auto-Fill implementation. Callers are separate applications. This repository does not import a host application, does not open a host database, and does not read candidate rows, resumes, or credentials from anywhere except the arguments of one call or one HTTP request.

The sections below describe the in-process library call. They are not a private API.

## What TileArc already does

1. The person selects a candidate.
2. TileArc shows jobs and downloads a tailored resume for that candidate.
3. Apply opens the external ATS in a browser.

Auto-Fill starts at that open page.

## What Auto-Fill does

`autofill_application(application_url, candidate_profile, credential_provider, options)`:

1. Records the site and detects the ATS from `application_url`.
2. If the page is a password login, asks `credential_provider` for that candidate and that employer (`credential_scope`, not the bare hostname when several employers share a host), then signs in. Email-first sites (email, Continue, password, Sign in) are handled. A page that is already signed in skips login. Create Account is typed only when the host posts `mode: "register"`.
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
| `ACCOUNT_CREATION_REQUIRED` | Protocol 0.7.0 only. A Create Account form is on the page. Nothing was typed. Older sessions see `LOGIN_REQUIRED`. |
| `EMAIL_VERIFICATION_REQUIRED` | Protocol 0.7.0 only. The site is asking for a code or an inbox check. Auto-Fill does not read mail. Older sessions see `MANUAL_REVIEW_REQUIRED`. |
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

A required "desired salary range" question is not filled from a minimum alone. The blocking item then includes `challenge: "confirm"`, `confidence: "LOW"`, `salaryShape`, `relatedFields`, and `reason`. Those fields are absent on other blockers, and they never contain the salary amount. The Companion should ask the person to confirm or complete the range. Resolver questions of intent `SALARY_EXPECTATION` also send `salaryShape` and `relatedFields`. The contract is in [HTTP_SERVICE.md](HTTP_SERVICE.md) under "Related salary questions".

A single or minimum expected-salary field is filled from the posting when `postedSalaryMin` / `postedSalaryMax` name one. One listed figure is that figure. A posted band uses its midpoint. At or below $100,000 that figure is the answer; above $100,000 and below $130,000 it drops by $5,000; at or above $130,000 it drops by $10,000. No posting and no saved salary floor uses $90,000. That default does not fill a range, and a single figure is never written as both ends.

Protocol 0.7.0 keeps that contract and adds account creation, email verification, and `POST /v1/sessions/{sessionId}/resume-file`. The default `protocolVersion` is still `0.6.0`. `GET /health` lists support on `supportedProtocolVersions` (it includes `0.7.0`). A host opts in by sending `0.7.0`. Details are in [HTTP_SERVICE.md](HTTP_SERVICE.md).

Protocol 0.4.0 adds an HTTP resolver mode, documented in [HTTP_SERVICE.md](HTTP_SERVICE.md). In that mode the create body carries opaque `candidateRef` and `jobRef` values, `jobContext.applicationUrl`, and a per-session `resolver` (`url`, `token`, `expiresAt`). Auto-Fill asks that resolver only for the field keys and question intents on the current step, holds the returned values in memory for that step, and then drops them. The resolver token is sent as a bearer header and is never written to a URL, a log line, or disk. This repository does not store a hash of that token and does not define the caller's schema.

## Sessions and credentials

Each call builds its own session from `candidate_id + job_id + application_url + session_id`. There is no process-wide "last profile". Two calls in parallel cannot read each other's profile or credentials.

`MemoryCredentialProvider` keys records by `(candidate_id, credential_scope)`. `credential_scope` is the Workday `tenantKey` (`host/site`) or `host/company` for Greenhouse, Lever, Ashby, and SmartRecruiters. A lookup does not fall back to another candidate, another employer, or the bare hostname. Hosts should apply the same scope in their own provider. Credential objects redact their values in `repr`. Logs record ids, ATS name, domain, state, and counts. A secret that misses the on-page password rules is refused with `password_policy` before Create Account is clicked.

## Employer password vault (vinayaka)

Auto-Fill does not generate or store the five-password pool. Vinayaka owns that pool: up to five generated passwords per candidate, one assigned to each employer, rotated when a site locks the account. The pool key is `candidateId` plus the `tenantKey` on the account view (`account.tenant.tenantKey`). The same string is `credential_scope(applicationUrl)`.

Vinayaka posts one secret for the current session. It does not put the password in `chrome.storage`, status JSON, or the helper profile directory.

Direct-browser helper, loopback only:

| Call | Body | What Auto-Fill does |
| --- | --- | --- |
| `POST /v1/sessions/{sessionId}/credentials` | `candidateId`, `email` or `username`, `password`, optional `confirmPassword`, `mode` (`store`, `login`, `register`), optional `standardTerms`, optional `verified` | `store` keeps the secret in memory under `(candidateId, tenantKey)`. `register` refuses with `422` `password_policy` when the secret misses the rules on the open page, and does not type it. Otherwise it types and clicks Create Account or Sign In. The response has `stored`, `outcome`, `tenantKey`, and lengths in `fieldProof`. It never has the password. |
| `POST /v1/sessions/{sessionId}/sign-out` | none | Clears that employer's cookies. Does not delete the secret and does not click a button. |
| `POST /v1/sessions/{sessionId}/continue` | existing continue body, optional `verified: true` | The same continue #93 added. It reattaches and fills the open page. The in-memory secret is still passed in, so sign-in works after reconnect. `verified: true` is the person's confirmation that they opened the verification email. Continue does not create an account by itself. |

There is no retrieve route that returns the password. The next fill or continue reads the memory store and signs in. OTP, CAPTCHA, MFA, and identity-provider SSO stay pauses. Final Submit stays human.

The hosted service uses the same scope on `POST /v1/sessions/{sessionId}/credentials`. `site` may still be the hostname. The stored key is the tenant scope. `422` `password_policy` means the host should pick another pool password and post again.

## Recruitment SMS consent

Recruitment text-message consent is an Application Answer, not a Terms of Service checkbox. `standardTermsAuthorized` does not check it. Marketing email, promotional SMS, AI or recording consent, arbitration, e-sign, background-check authorization, demographic attestations, and other certifications stay manual.

Hosts store one row on that candidate's `applicationAnswers`. The catalogue key is `recruitment_sms_consent`. The intent is `RECRUITMENT_SMS_CONSENT`. The answer is `Yes` or `No`.

```json
{
  "key": "recruitment_sms_consent",
  "intent": "RECRUITMENT_SMS_CONSENT",
  "answer": "Yes"
}
```

`SMS_CONSENT` is accepted as an alias for the intent or the key and is stored as `RECRUITMENT_SMS_CONSENT` / `recruitment_sms_consent`. A `Yes` authorizes auto-fill. A `No` does not, and it is not asked again.

When a form asks the question (any ATS; Workday's "Would you like to receive text messages…" is one wording), Auto-Fill selects **Yes / I agree / opt-in** only if this candidate's row is `Yes`. A different `candidateId` is refused. The acceptance is listed on `autoConsents` with `category: "RECRUITMENT_SMS_CONSENT"`. A stored `No` selects the decline option when the control is a choice, and leaves a checkbox unchecked. It is not an auto-accept, so it is not added to `autoConsents`.

If the row is missing, Auto-Fill does not check the box. It stops once with `MANUAL_ANSWER_REQUIRED` and kind `CONSENT` or `CONSENT_CHOICE`. The blocker `fieldKey` is `recruitment_sms_consent`. The same SMS consent is not asked again for every field in that session. A human answer is written onto the profile as `Yes` or `No` and returned as `learnedApplicationAnswers`:

```json
{
  "key": "recruitment_sms_consent",
  "intent": "RECRUITMENT_SMS_CONSENT",
  "question": "Would you like to receive text messages about your application?",
  "answer": "Yes",
  "candidateId": "candidate-id"
}
```

Vinayaka / Fillo should upsert that row on the candidate's Local Apply profile under `recruitment_sms_consent`. Final Submit stays human.

## What stays human

- Uploading the resume TileArc downloaded.
- Clicking the final Submit, Submit Application, Finish, Complete, or Apply control.
- Any CAPTCHA, puzzle, or anti-bot challenge. Detection only.
- Creating an account unless the host posted `mode: "register"` with a secret that meets the on-page rules.
- SSO through Google, Microsoft, Okta, Auth0, or Cisco, inbox codes, MFA, and the manual host `ibegin.tcsapps.com`.
- Legal attestations ("I agree", "I certify") and questions that are not on the profile.

Login submission is allowed and is a different action from final application submit. The central guard raises `HumanSubmissionRequired` if any code path tries to activate a final submit control, including a login-purpose click.
