"""t222: fresh profile, nested leaf, one card, and create-account consent.

No employer site is contacted. Account creation in these tests is a fake page.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from autofill.combobox import _menu_replaced
from autofill.engine import AutofillOptions, Status, autofill_application
from autofill.fill import _apply_mapped, _profile_owned, bind_replace_owned, reset_replace_owned
from autofill.models import Control, MappedField, Option
from autofill.profile import CandidateProfile

_HPE = (
    "I declare that I have read the HPE Recruitment Notice and agree to HPE processing "
    "my personal data for the purposes of my applying or employment with HPE."
)


def test_profile_owned_email_is_replaced_and_a_free_answer_is_not():
    assert _profile_owned(MappedField(key="email", action="fill", text="new@example.com"))
    assert not _profile_owned(MappedField(key="how_heard", action="select", text="Job Board"))


def test_a_click_that_opens_a_new_menu_is_a_folder():
    parents = [
        Option("Job Board", "Job Board", "#job", leaf=True, folder=False),
        Option("Advertisement", "Advertisement", "#ad", leaf=True, folder=False),
    ]
    children = [
        Option("Indeed", "Indeed", "#indeed", leaf=True),
        Option("LinkedIn", "LinkedIn", "#li", leaf=True),
    ]
    assert _menu_replaced(parents, children) is True
    assert _menu_replaced(parents, []) is False
    assert _menu_replaced(parents, parents) is False


class _Value:
    def __init__(self, value: str) -> None:
        self.value = value

    def input_value(self) -> str:
        return self.value

    def fill(self, text: str) -> None:
        self.value = text

    def evaluate(self, script, arg=None):
        if arg is not None:
            self.value = str(arg)
        return self.value


class _Page:
    def __init__(self, value: str) -> None:
        self.box = _Value(value)

    def locator(self, _selector: str) -> _Value:
        return self.box


def test_a_profile_email_replaces_the_value_already_in_the_box():
    page = _Page("old.person@example.com")
    control = Control(kind="text", label="Email", selector="#email")
    mapped = MappedField(
        key="email",
        action="fill",
        text="new.person@example.com",
        field_class="PROFILE_FIELD",
        confidence="high",
    )
    token = bind_replace_owned(True)
    try:
        outcome = _apply_mapped(page, control, mapped)
    finally:
        reset_replace_owned(token)
    assert outcome.action == "fill"
    assert page.box.value == "new.person@example.com"


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


def _person(**personal) -> CandidateProfile:
    fields = {
        "full_name": "Jahnavi Torra",
        "email": "jahnavi.updated@example.com",
        "phone": "9155550100",
        "city": "Austin",
    }
    fields.update(personal)
    return CandidateProfile.from_dict({"candidateId": "cand-t222", "personal": fields})


def test_ashby_titles_above_the_inputs_open_one_question_card(browser):
    page = browser.new_page()
    try:
        page.set_content(
            """
            <h1>Application</h1>
            <div class="ashby-application-form-field-entry">
              <div>LinkedIn</div>
              <input id="li" placeholder="Type here...">
            </div>
            <div class="ashby-application-form-field-entry">
              <div>Twitter</div>
              <input id="tw" placeholder="Type here...">
            </div>
            <div class="ashby-application-form-field-entry">
              <div>GitHub</div>
              <input id="gh" placeholder="Type here...">
            </div>
            <div class="ashby-application-form-field-entry">
              <div>Portfolio</div>
              <input id="po" placeholder="Type here...">
            </div>
            <div class="ashby-application-form-field-entry">
              <div>Are you able to work onsite, 5 days a week, in the location listed on this job?*</div>
              <textarea id="onsite"></textarea>
            </div>
            <button type="button">Submit application</button>
            """
        )
        result = autofill_application(
            "https://jobs.ashbyhq.com/rainmaker/1/application",
            _person(),
            None,
            AutofillOptions(page=page, candidate_id="cand-t222", session_id="ashby-1"),
        )
        assert result.status == Status.MANUAL_ANSWER_REQUIRED
        assert result.stopped_before_submit is True
        texts = " ".join(str(item.get("text") or "") for item in result.open_items).casefold()
        assert "linkedin" not in texts
        assert "twitter" not in texts
        assert "github" not in texts
        assert "portfolio" not in texts
        assert "onsite" in texts
        assert page.locator("#li").input_value() == ""
    finally:
        page.close()


def test_profile_email_overwrites_a_stale_value_on_the_form(browser):
    page = browser.new_page()
    try:
        page.set_content(
            """
            <h1>My Information</h1>
            <label for="email">Email</label>
            <input id="email" type="email" value="old.person@example.com">
            <label for="city">City*</label>
            <input id="city" required>
            <button type="button">Save and Continue</button>
            """
        )
        result = autofill_application(
            "https://example.wd5.myworkdayjobs.com/en-US/Careers/job/Example/apply",
            _person(),
            None,
            AutofillOptions(
                page=page,
                candidate_id="cand-t222",
                session_id="email-1",
                replace_profile_fields=True,
            ),
        )
        assert page.locator("#email").input_value() == "jahnavi.updated@example.com"
        assert result.stopped_before_submit is True
    finally:
        page.close()


def test_hpe_notice_is_checked_on_the_role_box_before_create_account(browser):
    from autofill.account_flow import run_account_action

    page = browser.new_page()
    try:
        page.set_content(
            f"""
            <div data-automation-id="authPage">
              <h3>Create Account</h3>
              <label for="email">Email Address</label>
              <input id="email" data-automation-id="email">
              <label for="password">Password</label>
              <input id="password" type="password" data-automation-id="password">
              <label for="verify">Verify New Password</label>
              <input id="verify" type="password" data-automation-id="verifyPassword">
              <div data-automation-id="formField-createAccountCheckbox">
                <div id="visual" role="checkbox" aria-checked="false"></div>
                <input id="box" type="checkbox" data-automation-id="createAccountCheckbox">
                <div>{_HPE.replace("HPE Recruitment Notice", '<a href="#n">HPE Recruitment Notice</a>')}</div>
              </div>
              <button type="button" data-automation-id="createAccountSubmitButton">Create Account</button>
            </div>
            <script>
              window.__accepted = false;
              window.__created = 0;
              document.getElementById("visual").addEventListener("click", () => {{
                document.getElementById("visual").setAttribute("aria-checked", "true");
                window.__accepted = true;
              }});
              document.getElementById("box").addEventListener("click", () => {{
                document.getElementById("box").checked = true;
              }});
              const create = document.querySelector("[data-automation-id='createAccountSubmitButton']");
              create.addEventListener("click", () => {{
                if (!window.__accepted) return;
                window.__created += 1;
              }});
            </script>
            """
        )
        result = run_account_action(
            page,
            mode="register",
            email="jahnavi.updated@example.com",
            username="",
            password="Fixture-Pass-9!",
            confirm="Fixture-Pass-9!",
            ats="workday",
            standard_terms=True,
        )
        assert page.evaluate("window.__accepted") is True
        assert page.evaluate("document.getElementById('visual').getAttribute('aria-checked')") == "true"
        assert result["ok"] is True
        assert result["consentRows"][0]["category"] == "STANDARD_TERMS"
        assert result["consentRows"][0]["checked"] is True
    finally:
        page.close()


def test_marketing_sign_in_page_is_typed_when_verify_is_stale(browser):
    from autofill.account_flow import plain_sign_in_page, run_account_action
    from autofill.extract import extract_page

    page = browser.new_page()
    try:
        page.set_content(
            """
            <h1>Proud to build what matters, together.</h1>
            <form>
              <h2>Sign In</h2>
              <label for="email">Email Address</label>
              <input id="email" data-automation-id="email">
              <label for="password">Password</label>
              <input id="password" type="password" data-automation-id="password">
              <button type="button" data-automation-id="signInSubmitButton">Sign In</button>
              <a href="#create">Create Account</a>
            </form>
            <script>
              window.__signed = 0;
              document.querySelector("[data-automation-id='signInSubmitButton']").addEventListener("click", () => {
                const email = document.getElementById("email").value;
                const password = document.getElementById("password").value;
                if (email && password) window.__signed += 1;
              });
            </script>
            """
        )
        url = "https://clark.wd5.myworkdayjobs.com/en-US/ClarkExternal/login"
        snapshot = extract_page(page)
        assert plain_sign_in_page(snapshot, url) is True
        result = run_account_action(
            page,
            mode="login",
            email="jahnavi.updated@example.com",
            username="",
            password="Fixture-Pass-9!",
            confirm="",
            ats="workday",
        )
        assert page.locator("#email").input_value() == "jahnavi.updated@example.com"
        assert page.locator("#password").input_value() == "Fixture-Pass-9!"
        assert result["ok"] is True
    finally:
        page.close()


def test_nested_prompt_without_a_chevron_does_not_commit_the_parent(browser):
    from autofill.combobox import select_combobox, take_opened_children

    page = browser.new_page()
    try:
        page.set_content(
            """
            <label for="hear">How Did You Hear About Us?</label>
            <input id="hear" role="combobox" aria-expanded="true" aria-controls="menu">
            <div id="menu" role="listbox"></div>
            <script>
              const parents = ["Job Board", "Advertisement"];
              const children = ["Indeed", "LinkedIn"];
              const menu = document.getElementById("menu");
              const field = document.getElementById("hear");
              function paint(rows, nested) {
                menu.innerHTML = "";
                rows.forEach((label) => {
                  const row = document.createElement("div");
                  row.setAttribute("role", "option");
                  row.id = "opt-" + label.replace(/\\s+/g, "-").toLowerCase();
                  row.textContent = label;
                  row.addEventListener("click", () => {
                    if (!nested && label === "Job Board") paint(children, true);
                    else {
                      field.value = label;
                      field.setAttribute("data-selected", label);
                      menu.innerHTML = "";
                    }
                  });
                  menu.appendChild(row);
                });
              }
              field.addEventListener("focus", () => {
                if (!field.getAttribute("data-selected")) paint(parents, false);
              });
              paint(parents, false);
            </script>
            """
        )
        missed = select_combobox(page, "#hear", "Job Board")
        assert missed is None
        opened = " ".join(take_opened_children())
        assert "Indeed" in opened
        assert page.locator("#hear").input_value() != "Job Board"
        picked = select_combobox(page, "#hear", "Job Board > Indeed")
        assert picked is not None
        assert picked.label == "Indeed"
        assert page.locator("#hear").get_attribute("data-selected") == "Indeed"
    finally:
        page.close()
