# Auto-Fill

Standalone library that fills a job-application form from a candidate profile and **stops**. A person clicks Submit or Apply.

It is meant to be embedded in another product (TileArc is the intended host). It does not search for jobs, score them, rewrite resumes, or submit anything.

## License and attribution

Auto-Fill is [AGPL-3.0-only](LICENSE). The form-filling behavior is adapted from [ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot) by Pickle-Pixel, which is also AGPL-3.0-only. That license was checked against ApplyPilot's GitHub license metadata, its `LICENSE` file, and `pyproject.toml` (`AGPL-3.0-only`) at revision `4a8d521f67` (package version 0.3.0).

[NOTICE](NOTICE) lists what was derived and what was left out. Short version:

| Kept (reimplemented) | Left out |
| --- | --- |
| Profile sections ApplyPilot fills from (identity, work authorization, availability, compensation, experience, voluntary EEO) | Job scraping, search, scoring, resume tailoring, cover letters, dashboards |
| Label-to-profile mapping, selects, radios, checkboxes, resume and cover-letter uploads, honeypots | CapSolver / CAPTCHA bypass |
| Multi-page Next/Continue for Workday, Taleo, and iCIMS; resume-first step for Workday and Lever | Automatic Submit. ApplyPilot submits. This package cannot. |
| Salary floor, posted midpoint, range, and hourly (/ 2080) | ApplyPilot's hardcoded $110k senior-title floor |
| Manual-ATS host `ibegin.tcsapps.com` and the SSO domains ApplyPilot refuses | Passwords, account creation, inbox codes, SSO login |

ApplyPilot's apply step is an agent prompt (`src/applypilot/apply/prompt.py`) driving a browser, not a deterministic filler. Auto-Fill is a Python library with an explicit mapper and a Playwright fill routine. It does not copy that prompt.

## Install

Python 3.11 or newer.

```bash
pip install -e ".[dev]"
python -m playwright install chromium
```

The Playwright browser is only needed to fill pages. Mapping and profile tests do not launch it. CI installs Chromium with system dependencies (see `.github/workflows/ci.yml`).

## Human submit safeguard

`fill_application` never clicks a final **Submit** or **Apply** control. It also never clicks **Log in**, **Sign up**, or **Create account**.

The only buttons it can click are **Next**, **Continue**, **Save and continue**, **Save & continue**, **Proceed**, and **Review**, and only when you pass `advance_pages=True`. Those open another page of the same form. They do not send the application. There is no flag, environment variable, or dry-run switch that turns submission on. `HUMAN_SUBMIT_ONLY` in `src/autofill/safeguards.py` is constant, and `FillResult.stopped_before_submit` is always true.

If the page shows a password field, nothing is typed and no auth button is clicked. If a CAPTCHA widget is present, filled fields stay as they are and the run stops. This package does not solve CAPTCHAs.

You should still review every value before submitting. Some sites submit on Enter or on a control this classifier does not recognize. Auto-Fill does not press Enter.

## Profile schema

Profiles are JSON. Load them from a path the caller provides or from `AUTOFILL_PROFILE`. Do not commit real profiles, resumes, or secrets. `profile.json`, `.env`, and `*.pdf` are gitignored. The file in `examples/profile.example.json` is fictional (`Casey Example`, `casey.example@example.com`).

`personal.password` is ignored if present. This package does not log in.

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
| `documents` | `cover_letter_text` | Pasted into a cover-letter text box. File uploads are separate arguments, not profile paths. |

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
from autofill import CandidateProfile, fill_application

profile = CandidateProfile.load("profile.json")  # gitignored, your file
result = fill_application(
    profile,
    url="https://boards.greenhouse.io/example/jobs/1",
    resume_path="resume.pdf",          # optional
    cover_letter_path="letter.pdf",    # optional
    advance_pages=True,                # Next/Continue only
)
assert result.stopped_before_submit
print(result.submit_controls)  # seen, not clicked
print(result.messages)
```

CLI (same guarantee):

```bash
export AUTOFILL_PROFILE="$PWD/profile.json"
export AUTOFILL_RESUME="$PWD/resume.pdf"
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

`map_field` is the mapping layer. Give it a `Control` (tag, label, autocomplete, options) and a `CandidateProfile`. It returns a `MappedField` (`fill`, `select`, `check`, `uncheck`, `upload`, `skip`, or `unanswered`). Unrecognized controls, honeypots, hidden text fields, and password fields are not given invented values.

`detect_ats(url)` labels Greenhouse, Lever, Workday, Taleo, iCIMS, Ashby, SmartRecruiters, Jobvite, BambooHR, and Workable. Workday, Taleo, and iCIMS are marked multi-page. Workday and Lever are marked resume-first. Filling those hosts still uses the generic mapper; ApplyPilot did not ship per-ATS DOM scripts, and neither does this package. Live ATS pages often use custom widgets, so a host label is not a promise that every field on that site will match.

`ibegin.tcsapps.com` returns `skipped_manual_ats` without opening the page. SSO hosts ApplyPilot blocked (`accounts.google.com`, `login.microsoftonline.com`, `okta.com`, `auth0.com`, `sso.cisco.com`) return `skipped_sso`.

## Embedding in TileArc

Keep TileArc's product code in its own repository. This package does not import it.

1. Depend on `auto-fill` (this repo) from TileArc, or vendor it as a subdirectory. Because the license is AGPL-3.0, modifications of **this** program that users interact with over a network need the corresponding source of this program offered to those users (AGPL section 13). That is a property of this library, not a claim about unrelated TileArc code. Read `LICENSE` before embedding it in a hosted product.
2. TileArc owns the candidate record. Serialize it into the JSON schema above and pass the path or the dict (`CandidateProfile.from_dict`) at fill time. Do not put TileArc database credentials, API keys, or session tokens in this package.
3. Prefer passing a Playwright `page` TileArc already opened, including a page that is already logged in. `fill_application(..., page=page)` does not close that page.

```python
result = fill_application(profile, page=tilearc_page, resume_path=resume, advance_pages=True)
# Show result.fields where action == "unanswered" in the TileArc UI.
# Leave the browser on the filled form. The user clicks Submit.
```

4. Treat `result.stopped_before_submit` as an invariant. Do not add a submit click in a wrapper "for convenience".
5. Surface `login_wall`, `captcha_present`, and `submit_controls` in the product UI so a person can finish those steps.
6. `FillResult.to_dict()` echoes which controls were filled. It is profile data. Do not ship it to third-party telemetry.

## Tests

```bash
pytest
```

Unit tests cover mapping, salary, ATS detection, the submit safeguard, and profile loading. `tests/test_fill.py` opens `tests/fixtures/sample_application.html` in Chromium, fills text inputs, a select, radios, a checkbox, a file input, and an open shadow-DOM field, follows "Save and continue", and asserts the submit handler never ran.

## Limits

- The mapper matches labels, names, ids, placeholders, aria-labels, and `autocomplete`. Custom widgets that are not `input`, `select`, or `textarea` (or that live in a closed shadow root) are not filled.
- It will not create an ATS account, solve a CAPTCHA, or type a password.
- It will not infer skills, salary, or work authorization that are absent from the profile.
- "Apply" on a job description is treated as a final action and is not clicked. Open the form URL, then call the filler.
