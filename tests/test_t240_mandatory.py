"""t240: mandatory-only questions, MM/YYYY dates, and a committed leaf.

No employer site is contacted. The pages are fixtures.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from autofill.engine import AutofillOptions, autofill_application
from autofill.mapping import map_field
from autofill.models import Control, Option
from autofill.profile import CandidateProfile


def _card_text(page) -> str:
    return str(
        page.evaluate(
            """() => {
              const host = document.getElementById("tilearc-answer-popup");
              return host && host.shadowRoot ? host.shadowRoot.textContent : "";
            }"""
        )
        or ""
    )


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {
            "candidateId": "cand-t240",
            "personal": {
                "first_name": "Jahnavi",
                "last_name": "Torra",
                "email": "jahnavi.torra@example.com",
                "phone": "9155550100",
                "linkedinUrl": "https://www.linkedin.com/in/jahnavi-torra",
            },
            "links": {"portfolio": "https://jahnavi.example/portfolio"},
            "employment": [
                {
                    "company": "Standard electric company, inc.",
                    "title": "engineer",
                    "from": "2/2025",
                    "to": "12/2025",
                },
                {
                    "employer": "VEM",
                    "jobTitle": "engineer",
                    "startMonth": "7",
                    "startYear": "2020",
                    "endMonth": "11",
                    "endYear": "2022",
                },
            ],
            "education": [
                {
                    "school": "State University",
                    "degree": "Master of Science",
                    "fieldOfStudy": "Computer Science",
                    "fromDate": "08/2018",
                    "toDate": "05/2020",
                }
            ],
        }
    )


def test_current_and_added_date_and_link_shapes_both_load():
    added = _profile()
    assert added.personal.linkedin_url == "https://www.linkedin.com/in/jahnavi-torra"
    assert added.personal.portfolio_url == "https://jahnavi.example/portfolio"
    assert added.employment[0].start_date == "02/2025"
    assert added.employment[0].end_date == "12/2025"
    assert added.employment[1].start_date == "07/2020"
    assert added.employment[1].end_date == "11/2022"
    assert added.education_history[0].degree == "Master of Science"
    assert added.education_history[0].field_of_study == "Computer Science"
    assert added.education_history[0].start_date == "08/2018"
    current = CandidateProfile.from_dict(
        {
            "candidateId": "cand-t240",
            "personal": {
                "first_name": "Jahnavi",
                "last_name": "Torra",
                "email": "jahnavi.torra@example.com",
                "linkedin_url": "https://www.linkedin.com/in/jahnavi-torra",
                "portfolio_url": "https://jahnavi.example/portfolio",
            },
            "employment": [
                {
                    "company": "Standard electric company, inc.",
                    "title": "engineer",
                    "startDate": "2025-02",
                    "endDate": "2025-12",
                    "current": False,
                }
            ],
        }
    )
    assert current.employment[0].start_date == "2025-02"
    assert current.personal.linkedin_url.endswith("/jahnavi-torra")
    newer = CandidateProfile.from_dict(
        {
            "candidateId": "cand-t240",
            "personal": {"first_name": "Jahnavi", "last_name": "Torra", "email": "jahnavi.torra@example.com"},
            "employment": [
                {
                    "company": "Standard",
                    "title": "engineer",
                    "start_date": "2025-06-01",
                    "start_month": "02",
                    "start_year": "2025",
                    "end_month": "12",
                    "end_year": "2025",
                }
            ],
            "education": [
                {
                    "school": "State University",
                    "degree": "Master of Science",
                    "startMonth": "08",
                    "startYear": "2018",
                    "to": "2020",
                }
            ],
        }
    )
    assert newer.employment[0].start_date == "02/2025"
    assert newer.employment[0].end_date == "12/2025"
    assert newer.education_history[0].start_date == "08/2018"
    assert newer.education_history[0].end_date == "2020"
    open_ended = CandidateProfile.from_dict(
        {
            "candidateId": "cand-t240",
            "personal": {"first_name": "Jahnavi", "last_name": "Torra", "email": "jahnavi.torra@example.com"},
            "employment": [{"company": "Standard", "title": "engineer", "from": "02/2025"}],
        }
    )
    assert open_ended.employment[0].current is True
    assert open_ended.employment[0].end_date == ""
    working = CandidateProfile.from_dict(
        {
            "candidateId": "cand-t240",
            "personal": {
                "first_name": "Jahnavi",
                "last_name": "Torra",
                "email": "jahnavi.torra@example.com",
            },
            "employment": [{"company": "Standard", "title": "engineer", "from": "2/2025", "currentlyWorking": True}],
        }
    )
    assert working.employment[0].current is True


def test_multiselect_and_nested_leaf_read_back_match_the_submitted_text():
    from autofill.tilearc_sync import SessionSnapshot, ack_is_trusted, merged_profile_body, parse_confirm_body

    submitted = "Campus Campaign > College | Career Websites > Corporate Website"
    values = ["Campus Campaign > College", "Career Websites > Corporate Website"]
    listed = parse_confirm_body(
        {
            "saved": True,
            "verified": True,
            "candidateId": "cand-t240",
            "readBack": {
                "candidateId": "cand-t240",
                "key": "how_heard",
                "question": "How Did You Hear About Us?",
                "answer": "typed text the server ignores",
                "values": values,
                "selectedOptions": [
                    {"id": "college", "label": "College", "path": values[0]},
                    {"id": "corp", "label": "Corporate Website", "path": values[1]},
                ],
                "version": "1",
                "updatedAt": "2026-11-06T03:37:00Z",
            },
        }
    )
    assert ack_is_trusted(
        listed,
        candidate_id="cand-t240",
        submitted=submitted,
        choice=True,
        values=values,
    )
    nested = parse_confirm_body(
        {
            "saved": True,
            "verified": True,
            "candidateId": "cand-t240",
            "readBack": {
                "candidateId": "cand-t240",
                "answer": "Corporate Website",
                "values": [],
                "leaf": {
                    "id": "corp",
                    "label": "Corporate Website",
                    "path": "Career Websites > Corporate Website",
                },
                "leaves": [
                    {"id": "corp", "label": "Corporate Website", "path": "Career Websites > Corporate Website"}
                ],
                "version": "1",
            },
        }
    )
    assert ack_is_trusted(
        nested,
        candidate_id="cand-t240",
        submitted="Career Websites > Corporate Website",
        choice=True,
        values=["Career Websites > Corporate Website"],
        leaf_id="corp",
    )
    typed = parse_confirm_body(
        {
            "saved": True,
            "verified": True,
            "candidateId": "cand-t240",
            "readBack": {
                "candidateId": "cand-t240",
                "answer": "Corporate Website",
                "values": [],
                "selectedOptions": [],
                "leaf": {},
                "version": "1",
            },
        }
    )
    assert not ack_is_trusted(
        typed,
        candidate_id="cand-t240",
        submitted="Corporate Website",
        choice=True,
        values=["Corporate Website"],
        leaf_id="corp",
    )
    live = parse_confirm_body(
        {
            "saved": True,
            "verified": True,
            "candidateId": "cand-t240",
            "readBack": {"candidateId": "cand-t240", "answer": "Indeed", "version": "1"},
        }
    )
    assert ack_is_trusted(live, candidate_id="cand-t240", submitted="Indeed", choice=True, values=["Indeed"])
    assert not ack_is_trusted(live, candidate_id="cand-t240", submitted="LinkedIn")
    snapshot = SessionSnapshot(
        profile={"candidateId": "cand-t240", "personal": {"full_name": "Stale Name"}},
        candidate_profile={
            "personal": {"first_name": "Jahnavi", "last_name": "Torra", "email": "jahnavi.torra@example.com"}
        },
        fingerprint="fp",
        version="1",
        candidate_id="cand-t240",
        local_profile={
            "personal": {"linkedin_url": "https://www.linkedin.com/in/jahnavi-torra"},
            "links": {"portfolio": "https://jahnavi.example/portfolio"},
            "employment": [
                {
                    "company": "Standard",
                    "title": "engineer",
                    "start_date": "2025-06-01",
                    "start_month": "02",
                    "start_year": "2025",
                    "to": "12/2025",
                }
            ],
        },
    )
    loaded = CandidateProfile.from_dict(merged_profile_body(None, snapshot))
    assert loaded.personal.linkedin_url.endswith("/jahnavi-torra")
    assert loaded.personal.portfolio_url == "https://jahnavi.example/portfolio"
    assert loaded.employment[0].start_date == "02/2025"
    assert loaded.employment[0].end_date == "12/2025"
    assert loaded.personal.github_url == ""


def test_linkedin_url_is_not_typed_into_a_yes_no_question():
    profile = _profile()
    mapped = map_field(
        Control(
            kind="textarea",
            label="Would you be willing to share your LinkedIn profile with us?",
            selector="#share",
            required=True,
        ),
        profile,
    )
    assert mapped.key != "linkedin_url"
    assert mapped.text != profile.personal.linkedin_url
    degree = map_field(
        Control(
            kind="radio",
            label="Degree",
            selector="#masters",
            required=True,
            options=[
                Option(label="Bachelor's Degree", value="bachelors", selector="#bachelors"),
                Option(label="Master's / Graduate Degree", value="masters", selector="#masters"),
                Option(label="Doctorate", value="doctorate", selector="#doctorate"),
            ],
        ),
        profile,
    )
    assert degree.action == "select"
    assert degree.option_label == "Master's / Graduate Degree"


def test_highlight_without_a_pill_is_not_a_committed_leaf(browser):
    from autofill.combobox import select_combobox

    page = browser.new_page()
    try:
        page.set_content(
            """
            <div data-automation-id="formField-source">
              <label>How Did You Hear About Us?</label>
              <input id="source" data-automation-id="searchBox" role="combobox" placeholder="Search">
              <span id="picked" data-automation-id="selectedItem" hidden></span>
            </div>
            <div id="menu" role="listbox">
              <div id="site" data-automation-id="promptOption" style="height:24px">Corporate Website</div>
            </div>
            <script>
              document.getElementById("site").addEventListener("click", () => {
                document.getElementById("site").setAttribute("aria-selected", "true");
              });
              document.getElementById("source").addEventListener("click", () => {
                document.getElementById("source").setAttribute("aria-expanded", "true");
              });
            </script>
            """
        )
        picked = select_combobox(page, "#source", "Corporate Website")
        assert picked is None
        assert page.locator("#picked").inner_text().strip() == ""
    finally:
        page.close()


def test_leaf_mousedown_commits_the_pill(browser):
    from autofill.combobox import select_combobox

    page = browser.new_page()
    try:
        page.set_content(
            """
            <div data-automation-id="formField-source">
              <label>How Did You Hear About Us?</label>
              <input id="source" data-automation-id="searchBox" role="combobox"
                placeholder="Search" aria-required="true">
              <span id="picked" data-automation-id="selectedItem" hidden></span>
            </div>
            <div id="menu" role="listbox">
              <div id="site" data-automation-id="promptOption" style="height:24px">Corporate Website</div>
            </div>
            <script>
              document.getElementById("site").addEventListener("mousedown", () => {
                const picked = document.getElementById("picked");
                picked.hidden = false;
                picked.textContent = "Corporate Website";
                document.getElementById("menu").remove();
                document.getElementById("source").value = "";
              });
            </script>
            """
        )
        picked = select_combobox(page, "#source", "Corporate Website")
        assert picked is not None
        assert page.locator("#picked").inner_text().strip() == "Corporate Website"
    finally:
        page.close()


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


def test_optional_links_are_not_asked_and_known_links_are_filled(browser):
    page = browser.new_page()
    try:
        page.set_content(
            """
            <h1>Application</h1>
            <label>LinkedIn<input id="li"></label>
            <label>Twitter<input id="tw"></label>
            <label>GitHub<input id="gh"></label>
            <label>Portfolio<input id="pf"></label>
            <label>Why this role?<abbr>*</abbr><textarea id="why" required></textarea></label>
            """
        )
        result = autofill_application(
            "https://jobs.ashbyhq.com/rainmaker/1/application",
            _profile(),
            None,
            AutofillOptions(page=page, candidate_id="cand-t240", session_id="t240-links"),
        )
        assert "jahnavi-torra" in page.locator("#li").input_value()
        assert "jahnavi.example/portfolio" in page.locator("#pf").input_value()
        assert page.locator("#tw").input_value() == ""
        assert page.locator("#gh").input_value() == ""
        texts = " ".join(str(item.get("text") or "") for item in result.open_items).casefold()
        assert "twitter" not in texts
        assert "github" not in texts
        assert "linkedin" not in texts
        assert "portfolio" not in texts
        assert "why this role" in texts
    finally:
        page.close()


def test_month_year_dates_are_written_and_not_asked(browser):
    page = browser.new_page()
    try:
        page.set_content(
            """
            <h1>My Experience</h1>
            <div data-automation-id="formField-startDate">
              <label>From<abbr>*</abbr></label>
              <input id="we1-from" placeholder="MM/YYYY" aria-required="true">
            </div>
            <div data-automation-id="formField-endDate">
              <label>To<abbr>*</abbr></label>
              <input id="we1-to" placeholder="MM/YYYY" aria-required="true">
            </div>
            <div data-automation-id="formField-startDate-2">
              <label>From<abbr>*</abbr></label>
              <input id="we2-from" placeholder="MM/YYYY" aria-required="true">
            </div>
            <div data-automation-id="formField-endDate-2">
              <label>To<abbr>*</abbr></label>
              <input id="we2-to" placeholder="MM/YYYY" aria-required="true">
            </div>
            <label>Job title<input id="title" value="engineer"></label>
            """
        )
        result = autofill_application(
            "https://madixinc.wd12.myworkdayjobs.com/en-US/Madix/job/apply",
            _profile(),
            None,
            AutofillOptions(page=page, candidate_id="cand-t240", session_id="t240-dates"),
        )
        assert page.locator("#we1-from").input_value() == "02/2025"
        assert page.locator("#we1-to").input_value() == "12/2025"
        assert page.locator("#we2-from").input_value() == "07/2020"
        assert page.locator("#we2-to").input_value() == "11/2022"
        texts = " ".join(str(item.get("text") or "") for item in result.open_items).casefold()
        assert "from" not in texts
        assert "to" not in texts
    finally:
        page.close()


def test_calendar_month_click_keeps_the_date_when_the_value_does_not_stick(browser):
    page = browser.new_page()
    try:
        page.set_content(
            """
            <h1>My Experience</h1>
            <div data-automation-id="formField-startDate">
              <label>From<abbr>*</abbr></label>
              <input id="we1-from" placeholder="MM/YYYY" aria-required="true">
            </div>
            <div id="cal">
              <button type="button" id="feb">Feb</button>
            </div>
            <script>
              const input = document.getElementById("we1-from");
              Object.defineProperty(input, "value", {
                configurable: true,
                get() { return this.dataset.kept || ""; },
                set() {}
              });
              document.getElementById("feb").addEventListener("click", () => {
                input.dataset.kept = "02/2025";
              });
            </script>
            """
        )
        profile = CandidateProfile.from_dict(
            {
                "candidateId": "cand-t240",
                "personal": {
                    "first_name": "Jahnavi",
                    "last_name": "Torra",
                    "email": "jahnavi.torra@example.com",
                },
                "employment": [{"company": "Standard", "title": "engineer", "from": "2/2025", "to": ""}],
            }
        )
        autofill_application(
            "https://madixinc.wd12.myworkdayjobs.com/en-US/Madix/job/apply",
            profile,
            None,
            AutofillOptions(page=page, candidate_id="cand-t240", session_id="t240-cal"),
        )
        assert page.locator("#we1-from").input_value() == "02/2025"
    finally:
        page.close()


def test_degree_is_selected_and_study_text_without_a_pill_is_not_saved(browser):
    page = browser.new_page()
    try:
        page.set_content(
            """
            <h1>My Experience</h1>
            <fieldset>
              <legend>Degree<abbr>*</abbr></legend>
              <label><input type="radio" name="degree" id="bachelors" value="bachelors" required>
                Bachelor's Degree</label>
              <label><input type="radio" name="degree" id="masters" value="masters" required>
                Master's / Graduate Degree</label>
              <label><input type="radio" name="degree" id="doctorate" value="doctorate" required>
                Doctorate</label>
            </fieldset>
            <div data-automation-id="formField-fieldOfStudy">
              <label>Field of Study<abbr>*</abbr></label>
              <div data-automation-id="multiSelectContainer">
                <input id="study" role="combobox" placeholder="Search" aria-required="true">
                <div id="count">0 items selected</div>
                <span id="pill" data-automation-id="selectedItem" hidden></span>
              </div>
            </div>
            <script>
              const input = document.getElementById("study");
              input.addEventListener("input", () => {
                input.setAttribute("aria-expanded", "true");
                let menu = document.getElementById("study-menu");
                if (!menu) {
                  menu = document.createElement("div");
                  menu.id = "study-menu";
                  menu.setAttribute("role", "listbox");
                  const row = document.createElement("div");
                  row.id = "study-cs";
                  row.setAttribute("data-automation-id", "promptOption");
                  row.style.height = "24px";
                  row.textContent = "Computer Science";
                  row.addEventListener("mousedown", () => {
                    const pill = document.getElementById("pill");
                    pill.hidden = false;
                    pill.textContent = "Computer Science";
                    document.getElementById("count").textContent = "1 item selected";
                    input.value = "";
                    menu.remove();
                  });
                  menu.appendChild(row);
                  document.body.appendChild(menu);
                }
              });
            </script>
            """
        )
        result = autofill_application(
            "https://amgen.wd1.myworkdayjobs.com/en-US/Careers/job/apply",
            _profile(),
            None,
            AutofillOptions(page=page, candidate_id="cand-t240", session_id="t240-study"),
        )
        assert page.locator("#masters").is_checked()
        assert page.locator("#pill").inner_text().strip() == "Computer Science"
        assert page.locator("#share").count() == 0
        texts = " ".join(str(item.get("text") or "") for item in result.open_items).casefold()
        assert "degree" not in texts
        assert "field of study" not in texts
        assert "linkedin.com" not in _card_text(page)
    finally:
        page.close()


def test_typed_study_text_with_zero_selected_is_not_a_commit(browser):
    from autofill.combobox import search_field_of_study

    page = browser.new_page()
    try:
        page.set_content(
            """
            <div data-automation-id="formField-fieldOfStudy">
              <label>Field of Study</label>
              <div data-automation-id="multiSelectContainer">
                <input id="study" role="combobox" placeholder="Search">
                <div id="count">0 items selected</div>
                <span id="pill" data-automation-id="selectedItem" hidden></span>
              </div>
            </div>
            <script>
              const input = document.getElementById("study");
              input.addEventListener("input", () => {
                input.setAttribute("aria-expanded", "true");
                const menu = document.createElement("div");
                menu.id = "study-menu";
                menu.setAttribute("role", "listbox");
                const row = document.createElement("div");
                row.setAttribute("data-automation-id", "promptOption");
                row.textContent = "Computer Science";
                row.addEventListener("click", () => {
                  row.setAttribute("aria-selected", "true");
                });
                menu.appendChild(row);
                document.body.appendChild(menu);
              });
            </script>
            """
        )
        picked = search_field_of_study(page, "#study", "Computer Science")
        assert picked is None
        assert page.locator("#count").inner_text() == "0 items selected"
    finally:
        page.close()
