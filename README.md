# Auto-Fill

Standalone library that opens a job application, signs in when the host supplies credentials, fills the form, and **stops**. A person uploads the resume and clicks the final Submit or Apply.

It is meant to be embedded in another product (TileArc is the intended host). It does not search for jobs, score them, rewrite resumes, generate cover letters, or send the application. CAPTCHA and anti-bot challenges are detected and the run stops. They are not solved.

## License and attribution

Auto-Fill is [AGPL-3.0-only](LICENSE). The form-filling behavior is adapted from [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) by Pickle-Pixel, which is also AGPL-3.0-only. That license was checked against ApplyPilot's GitHub license metadata, its `LICENSE` file, and `pyproject.toml` (`AGPL-3.0-only`) at revision `4a8d521f67` (package version 0.3.0).

[NOTICE](NOTICE) lists what was derived and what was left out. Short version:

| Kept (reimplemented) | Left out |
| --- | --- |
| Profile sections ApplyPilot fills from (identity, work authorization, availability, compensation, experience, voluntary EEO), plus employment, education, internships, projects, and explicit answers | Job scraping, search, scoring, resume tailoring, cover-letter generation, dashboards |
| Label-to-profile mapping, selects, radios, checkboxes, dates, comboboxes, honeypots | CapSolver and every other CAPTCHA or challenge solver. A widget stops the run. |
| Multi-page Next/Continue/Save and Continue for Workday, Oracle Candidate Experience, Greenhouse, Lever, Ashby, SmartRecruiters, iCIMS, Taleo, SuccessFactors, and Dayforce | Automatic final Submit. ApplyPilot submits. This package cannot. |
| Salary floor, posted midpoint, range, and hourly (/ 2080), only from an explicit profile value | ApplyPilot's hardcoded $110k senior-title floor |
| Manual-ATS host `ibegin.tcsapps.com` and the SSO domains ApplyPilot refuses | Stored passwords, account creation, inbox codes, SSO login. A runtime `CredentialProvider` may sign in to an existing ATS account. |

ApplyPilot's apply step is an agent prompt (`src/applypilot/apply/prompt.py`) driving a browser, not a deterministic filler. Auto-Fill is a Python library with an explicit mapper and a Playwright fill routine. It does not copy that prompt.

## Install

Python 3.11 or newer.

```bash
pip install -e ".[dev]"
python -m playwright install chromium
```

The Playwright browser is only needed to fill pages. Mapping and profile tests do not launch it. CI installs Chromium with system dependencies (see `.github/workflows/ci.yml`).

## Two different buttons

**Login submit is allowed. Final application submit is not.**

| Action | Examples | What Auto-Fill does |
| --- | --- | --- |
| `LoginSubmitAllowed` | Log in, Sign in | Clicks it after typing credentials from a `CredentialProvider`. |
| Navigation | Next, Continue, Save and Continue, Proceed, Review, Add another | Clicks it to open the next step of the same application. |
| `FinalApplicationSubmitForbidden` | Submit, Submit Application, Finish, Complete, Apply, Apply now | Never clicks it. `perform_click` raises `HumanSubmissionRequired` first, including when the caller asked for a login click. |

Sign up, Register, and Create account are not clicked. `HUMAN_SUBMIT_ONLY` in `src/autofill/safeguards.py` is constant. `stopped_before_submit` is always true. There is no flag that turns final submission on.

`fill_application` is the lower-level helper. It does not type passwords. Use `autofill_application` when the page may be a login.

### CAPTCHA

CAPTCHA is **not** automated. If the page contains hCaptcha, reCAPTCHA, Cloudflare Turnstile, or a known challenge iframe, the status is `CAPTCHA_REQUIRED` and nothing is solved, bypassed, or injected. ApplyPilot's CapSolver path is not in this package.

### Resume checkpoint

A resume file input returns `RESUME_UPLOAD_REQUIRED`. Auto-Fill does not select a local resume. The person uploads the file the host already downloaded, then the host calls again with `resume_uploaded=True`. A cover letter is written only when the caller passes `cover_letter_text` or `cover_letter_path`. Nothing is generated.

You should still review every value before submitting. Some sites submit on Enter. Auto-Fill does not press Enter.

## Profile schema

Profiles are JSON. Load them from a path the caller provides or from `AUTOFILL_PROFILE`. Do not commit real profiles, resumes, or secrets. `profile.json`, `.env`, and `*.pdf` are gitignored. The file in `examples/profile.example.json` is fictional (`Casey Example`, `casey.example@example.com`).

`personal.password` and `basics.password` are ignored if present. Passwords are not part of the profile. Login uses a `CredentialProvider` at call time, scoped to the candidate and the site domain.

| Section | Fields | Notes |
| --- | --- | --- |
| `personal` | `full_name` (required), `email` (required), `preferred_name`, `phone`, `address`, `city`, `province_state`, `country`, `postal_code`, `linkedin_url`, `github_url`, `portfolio_url`, `website_url` | A "Name" field gets the preferred first name plus the legal last name when they differ. "Legal name" gets `full_name`. Phone fields that ask for digits or a national number get digits only. |
| `work_authorization` | `legally_authorized_to_work`, `require_sponsorship`, `work_permit_type` | Empty means the control is left unanswered. The mapper does not assume "Yes". |
| `availability` | `earliest_start_date`, `available_for_full_time`, `available_for_contract` | A value like `Immediately` is not written into `<input type="date">`. |
| `compensation` | `salary_expectation`, `salary_currency`, `salary_range_min`, `salary_range_max`, `currency_conversion_note` | The expectation is the floor. See salary below. |
| `experience` | `years_of_experience_total`, `education_level`, `current_job_title`, `current_company`, `target_role` | "Years of experience with Kubernetes" is not treated as total years. |
| `skills` | list of strings | "Do you have experience with Python?" is Yes only when Python is in this list. Otherwise it is left for a person. |
| `screening` | `age_18_or_older`, `willing_background_check`, `felony_conviction`, `previously_employed_here`, `how_heard` | Empty stays empty. ApplyPilot hardcoded several of these; Auto-Fill will not. |
| `eeo_voluntary` | `gender`, `race_ethnicity`, `veteran_status`, `disability_status` | Default is to decline. Set them when the candidate wants to self-identify. |
| `documents` | `cover_letter_text` | Used only when the caller also passes that text into the API. File uploads are separate arguments. |
| Extended | `candidateId`, `basics`, `address`, `employment[]`, `education[]`, `internships[]`, `projects[]`, `applicationAnswers[]`, `workAuthorization`, `sponsorship`, `salaryExpectation` | Folded into the sections above. `personal` wins when both `basics` and `personal` set the same key. Unknown questions stay blank. Salary and work authorization are never invented. |

`currency_conversion_note` and `target_role` are stored for a host app. The filler does not convert currencies or generate prose from the target role.

### Salary

`resolve_salary` implements this order, and never returns a number below `salary_expectation`:

1. A posted range (only if the caller passes `JobContext`) uses the midpoint.
2. No posted range uses the floor.
3. A range question uses the posted midpoint ±10%, or `salary_range_min` / `salary_range_max` raised to the floor.
4. An hourly field divides the annual figure by 2080.

Auto-Fill does not look up the posting. Pass `posted_salary_min` and `posted_salary_max` if the host already knows them.

## Usage

```python
from autofill import (
    AutofillOptions,
    CandidateProfile,
    Credentials,
    MemoryCredentialProvider,
    autofill_application,
)

profile = CandidateProfile.load("profile.json")  # gitignored, your file
credentials = MemoryCredentialProvider()
# runtime_secret is supplied by the host for this call. It is not part of the profile.
credentials.put(
    profile.candidate_id,
    "boards.greenhouse.io",
    Credentials(site="boards.greenhouse.io", email=profile.personal.email, password=runtime_secret),
)
result = autofill_application(
    "https://boards.greenhouse.io/example/jobs/1",
    profile,
    credentials,
    AutofillOptions(job_id="job-1", candidate_id=profile.candidate_id),
)
assert result.stopped_before_submit
print(result.status, result.login_status, result.current_step)
print(result.submit_controls)  # seen, not clicked
```

Statuses include `FILLED`, `LOGIN_REQUIRED`, `LOGIN_FAILED`, `RESUME_UPLOAD_REQUIRED`, `MANUAL_REVIEW_REQUIRED`, `READY_FOR_HUMAN_SUBMIT`, `UNSUPPORTED`, `FAILED`, and `CAPTCHA_REQUIRED`.

After the person uploads the resume, call again with `AutofillOptions(resume_uploaded=True, page=same_page, ...)`.

The CLI uses the lower-level filler. It does not log in and it does not upload a resume. `--resume` is accepted and ignored.

```bash
export AUTOFILL_PROFILE="$PWD/profile.json"
autofill --url "https://jobs.lever.co/example/11111111-2222-3333-4444-555555555555" --advance-pages
```

`--headed` shows the browser. The process exits after filling. Submit yourself.

Run the local fixture the tests use:

```bash
python -c "
from pathlib import Path
from playwright.sync_api import sync_playwright
from autofill import CandidateProfile, fill_application
profile = CandidateProfile.load('examples/profile.example.json')
page_url = Path('tests/fixtures/sample_application.html').resolve().as_uri()
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page()
    page.goto(page_url)
    result = fill_application(profile, page=page, advance_pages=True)
    print(result.status, result.stopped_before_submit, result.submit_controls)
    browser.close()
"
```

### Mapping without a browser

`map_field` is the mapping layer. Give it a `Control` (tag, label, autocomplete, options, nearby heading) and a `CandidateProfile`. It returns a `MappedField` (`fill`, `select`, `check`, `uncheck`, `upload`, `skip`, `unanswered`, or `resume_required`). A match that exists only in nearby heading text is low confidence and is left untouched. Unrecognized controls, honeypots, hidden fields, legal attestations, and password fields are not given invented values. Password fields are filled only by the login flow.

Field classes are `PROFILE_FIELD`, `APPROVED_QUESTION`, `UNKNOWN_FIELD`, `MANUAL_REVIEW_FIELD`, `RESUME_FIELD`, `LEGAL_FIELD`, `FINAL_SUBMIT`, `LOGIN_FIELD`, and `NAVIGATION_CONTROL`.

`detect_ats(url)` labels Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Oracle Candidate Experience (`*.oraclecloud.com` with `hcmUI` or `CandidateExperience` in the path), iCIMS, Taleo, SuccessFactors, Dayforce, and the earlier Jobvite, BambooHR, and Workable hosts. Each named ATS has an adapter that lists its usual steps. Filling uses one shared matcher. A host label is not a promise that every custom widget on a live site will match.

Dates accept `MM/DD/YYYY`, `YYYY-MM-DD`, and `Month YYYY`. Country and state options accept US/USA/United States and CA/California aliases. Employment rows advance through `employment[]` without repeating a row into the next empty field. Projects and internships fill only when the label itself names them. Controlled inputs are set through the native value setter plus `input`, `change`, and `blur`, and an existing different value is left in place. Native selects and confident ARIA comboboxes are chosen from their options. Radios and checkboxes need an explicit yes or no.

`ibegin.tcsapps.com` returns `skipped_manual_ats` without opening the page. SSO hosts ApplyPilot blocked (`accounts.google.com`, `login.microsoftonline.com`, `okta.com`, `auth0.com`, `sso.cisco.com`) return `skipped_sso`.

## Embedding in TileArc

The host flow is: candidate select, jobs, download the tailored resume, Apply opens the ATS, then Auto-Fill signs in if needed, fills, stops for the human resume upload, continues, and stops on review for a human submit.

Keep TileArc's product code in its own repository. This package does not import it and does not open its database. The contract is [docs/TILEARC_INTEGRATION.md](docs/TILEARC_INTEGRATION.md).

1. Depend on `auto-fill` (this repo) from TileArc, or vendor it as a subdirectory. Because the license is AGPL-3.0, modifications of **this** program that users interact with over a network need the corresponding source of this program offered to those users (AGPL section 13). That is a property of this library, not a claim about unrelated TileArc code. Read `LICENSE` before embedding it in a hosted product.
2. TileArc owns the candidate record. Build a `CandidateProfile` at fill time. Do not put TileArc database credentials, API keys, or session tokens in this package.
3. Pass the Playwright `page` TileArc already opened. `autofill_application` does not close that page and does not navigate it away.

```python
result = autofill_application(
    application_url,
    profile,
    credential_provider,
    AutofillOptions(job_id=job_id, candidate_id=candidate_id, session_id=session_id, page=tilearc_page),
)
# RESUME_UPLOAD_REQUIRED: the person uploads the resume TileArc downloaded.
# READY_FOR_HUMAN_SUBMIT: the person clicks Submit.
```

4. Treat `result.stopped_before_submit` as an invariant. Do not add a submit click in a wrapper.
5. Surface `login_status`, `CAPTCHA_REQUIRED`, `manual_actions`, and `submit_controls` in the product UI.
6. Result payloads name controls. Do not ship them, or any field value, to third-party telemetry.

## Tests

```bash
pytest
```

Unit tests cover mapping, salary, ATS detection, the submit safeguard, profile loading, login, sessions, and the state machine. `tests/test_fill.py` opens `tests/fixtures/sample_application.html` in Chromium and asserts the submit handler never ran. `tests/test_flow.py` runs a synthetic login-to-review application with Fake Candidate A and one local page per ATS under `tests/fixtures/ats/`. CI runs Ruff, mypy, and pytest with Playwright Chromium.

## Limits

- The mapper uses labels, names, ids, placeholders, aria-labels, autocomplete, type, and nearby headings. Custom widgets that are not `input`, `select`, `textarea`, or an ARIA combobox (or that live in a closed shadow root) are not filled.
- It will not create an ATS account, solve a CAPTCHA, or store a password. It types a password only into a detected login form, from a `CredentialProvider`, and it never writes that value to a log.
- It will not infer skills, salary, or work authorization that are absent from the profile.
- It will not choose a resume file.
- "Apply" on a job description is a final action and is not clicked. Open the form URL, then call the filler.
