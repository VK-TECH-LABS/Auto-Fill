"""Whole-workflow regressions for the required-only engine, run on every ATS host shape.

One fixture application is served under each ATS's URL so host-specific
adapters run, and the complete flow is checked rather than single fields:
mandatory fields filled, optional fields left blank and never asked, a field
the page only reveals as mandatory through a Save & Continue error is filled on
the next pass, the run reaches the final page, and Submit is never clicked.
Fixtures only; fictional candidate; no live employer site.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import sync_playwright

from autofill.engine import AutofillOptions, autofill_application
from autofill.local_attach import claim_employer_host
from autofill.profile import CandidateProfile

_HOSTS = [
    "https://boards.greenhouse.io/example/jobs/1",
    "https://jobs.lever.co/example/1/apply",
    "https://jobs.ashbyhq.com/example/1/application",
    "https://example.wd1.myworkdayjobs.com/en-US/External/job/X/apply",
    "https://jobs.smartrecruiters.com/oneclick-ui/company/Example/publication/1",
    "https://careers-example.icims.com/jobs/1/job",
    "https://careers.unseen-employer.example/apply/1",
]

_FLOW = """
<h1 id="title">Application</h1>
<form onsubmit="return false">
  <section id="step1">
    <label for="fn">First Name<abbr>*</abbr></label><input id="fn" required>
    <label for="ln">Last Name<abbr>*</abbr></label><input id="ln" required>
    <label for="em">Email<abbr>*</abbr></label><input id="em" type="email" required>
    <label for="li">LinkedIn Profile</label><input id="li">
    <label for="pf">Portfolio</label><input id="pf">
    <label for="hear">How did you hear about us?</label>
    <select id="hear"><option value="">Select One</option><option>LinkedIn</option><option>Indeed</option></select>
    <button type="button" id="next1">Save and Continue</button>
  </section>
  <section id="step2" hidden>
    <h2>My Information</h2>
    <div class="field-group">
      <label for="city">City</label>
      <input id="city" aria-describedby="city-err">
      <div id="city-err" class="error" hidden>This field is required.</div>
    </div>
    <label for="pron">Pronouns</label><input id="pron">
    <label for="web">Personal Website</label><input id="web">
    <button type="button" id="next2">Save and Continue</button>
  </section>
  <section id="step3" hidden>
    <h2>Review</h2>
    <button type="submit" id="submit">Submit Application</button>
  </section>
</form>
<script>
  window.__submitClicks = 0;
  window.__nextClicks = 0;
  document.getElementById("submit").addEventListener("click", () => { window.__submitClicks += 1; });
  function show(id) {
    for (const s of ["step1", "step2", "step3"]) document.getElementById(s).hidden = s !== id;
  }
  document.getElementById("next1").addEventListener("click", () => {
    window.__nextClicks += 1;
    if (["fn", "ln", "em"].every((id) => document.getElementById(id).value.trim())) show("step2");
  });
  document.getElementById("next2").addEventListener("click", () => {
    window.__nextClicks += 1;
    const city = document.getElementById("city");
    const err = document.getElementById("city-err");
    if (!city.value.trim()) {
      // Mandatory only by validation: no asterisk until the page rejects it.
      city.setAttribute("aria-invalid", "true");
      err.hidden = false;
      return;
    }
    city.removeAttribute("aria-invalid");
    err.hidden = true;
    show("step3");
  });
</script>
"""


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {
            "candidateId": "cand-cross-ats",
            "personal": {
                "first_name": "Avery",
                "last_name": "Alpha",
                "email": "avery.alpha@example.com",
                "phone": "6125550148",
                "city": "Austin",
                "state": "TX",
                "country": "United States",
                "linkedinUrl": "https://www.linkedin.com/in/avery-alpha",
            },
            "links": {"portfolio": "https://avery.example/portfolio"},
        }
    )


@pytest.fixture(scope="module")
def browser():
    try:
        with sync_playwright() as playwright:
            chromium = playwright.chromium.launch(headless=True)
            try:
                yield chromium
            finally:
                chromium.close()
    except Exception as exc:
        pytest.skip(f"Chromium is not installed: {exc}")


def _run(page, url: str, index: int):
    return autofill_application(
        url,
        _profile(),
        None,
        AutofillOptions(page=page, candidate_id="cand-cross-ats", session_id=f"cross-{index}"),
    )


@pytest.mark.parametrize("url", _HOSTS, ids=lambda u: u.split("/")[2])
def test_required_only_flow_reaches_review_and_never_submits(browser, url):
    page = browser.new_page()
    try:
        page.set_content(_FLOW)
        results = []
        for index in range(4):
            results.append(_run(page, url, index))
            if page.locator("#step3").is_visible():
                break
        assert page.locator("#step3").is_visible(), [r.messages for r in results]
        # Mandatory fields hold the candidate's data, including the one the page
        # only revealed as mandatory after Save and Continue.
        assert page.locator("#fn").input_value() == "Avery"
        assert page.locator("#ln").input_value() == "Alpha"
        assert page.locator("#em").input_value() == "avery.alpha@example.com"
        assert page.locator("#city").input_value().startswith("Austin")
        # Optional fields stay blank even though the profile has the data.
        assert page.locator("#li").input_value() == ""
        assert page.locator("#pf").input_value() == ""
        assert page.locator("#hear").input_value() == ""
        assert page.locator("#pron").input_value() == ""
        assert page.locator("#web").input_value() == ""  # the old fill-everything fallback typed the portfolio here
        asked = " ".join(str(item.get("text") or "") for r in results for item in r.open_items).casefold()
        for optional in ("linkedin", "portfolio", "how did you hear", "pronouns"):
            assert optional not in asked
        # Exactly the two Save and Continue pages; Submit is human-only.
        assert page.evaluate("window.__nextClicks") >= 2
        assert page.evaluate("window.__submitClicks") == 0
        assert all(r.stopped_before_submit for r in results)
    finally:
        page.close()


def test_an_unmarked_page_fills_identity_only_and_never_submits(browser):
    page = browser.new_page()
    try:
        page.set_content(
            """
            <h1>Apply</h1>
            <form onsubmit="return false">
              <label for="fn">First Name</label><input id="fn">
              <label for="em">Email</label><input id="em" type="email" autocomplete="email">
              <label for="li">LinkedIn</label><input id="li">
              <label for="city">City</label><input id="city">
              <button type="submit" id="submit">Submit</button>
            </form>
            <script>window.__submitClicks = 0;
              document.getElementById("submit").addEventListener("click", () => { window.__submitClicks += 1; });
            </script>
            """
        )
        _run(page, "https://careers.unseen-employer.example/apply/2", 9)
        assert page.locator("#fn").input_value() == "Avery"
        assert page.locator("#em").input_value() == "avery.alpha@example.com"
        assert page.locator("#li").input_value() == ""
        assert page.locator("#city").input_value() == ""
        assert page.evaluate("window.__submitClicks") == 0
    finally:
        page.close()


def test_an_employer_session_never_carries_to_another_candidate(tmp_path):
    url = "https://duke.wd1.myworkdayjobs.com/en-US/search/job/X/apply"
    assert claim_employer_host(tmp_path, url, "cand-a") is True  # unknown owner: clear first
    assert claim_employer_host(tmp_path, url, "cand-a") is False  # same candidate keeps the session
    assert claim_employer_host(tmp_path, url, "cand-b") is True  # another candidate: clear
    # The record survives a helper restart and holds no raw candidate id.
    stored = (tmp_path / "autofill-employer-owners.json").read_text(encoding="utf-8")
    assert "cand-b" not in stored and len(json.loads(stored)) == 1
    assert claim_employer_host(tmp_path, url, "cand-a") is True
