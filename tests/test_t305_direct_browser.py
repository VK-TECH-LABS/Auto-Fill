"""t305: one question card, Proceed, nested leaves, and create-account consent.

No employer site is contacted. Account creation in these tests is a fake page.
"""

from __future__ import annotations

from autofill.account_flow import _accept_account_terms, plain_sign_in_page
from autofill.combobox import child_for_answer, terminal_menu_labels
from autofill.engine import Status, _with_study_gaps
from autofill.local_helper import LocalJob, _continue_allowed, _fill_identity
from autofill.mapping import bind_standard_terms, consume_auto_consents, terms_category
from autofill.models import ButtonControl, Control, Option, PageSnapshot
from autofill.profile import CandidateProfile, Personal
from autofill.stepfill import required_gaps

_HPE = (
    "I declare that I have read the HPE Recruitment Notice and agree to HPE processing "
    "my personal data for the purposes of my applying or employment with HPE."
)


def _box(label: str, **kwargs) -> Control:
    return Control(kind="checkbox", label=label, selector="#box", required=True, **kwargs)


def _profile(**personal) -> CandidateProfile:
    fields = {"full_name": "Jahnavi Torra", "email": "jahnavi@example.com"}
    fields.update(personal)
    return CandidateProfile(personal=Personal(**fields), candidate_id="cand-hpe")


class _Page:
    """Enough of a page for gap scans. ``extract_page`` fails and the snapshot is kept."""

    def __init__(self, *, invalid: str = "") -> None:
        self.invalid = invalid

    def evaluate(self, script, selector=None):
        if not isinstance(script, str) or selector is None:
            raise RuntimeError("not a browser")
        if "aria-invalid" in script:
            return selector == self.invalid
        return False


def test_hpe_recruitment_notice_is_standard_and_penalty_declare_is_not():
    assert terms_category(_box(_HPE)) == "STANDARD_TERMS"
    penalty = _box("I declare under penalty of perjury that I have read the privacy notice")
    assert terms_category(penalty) == "CONSENT"
    alerts = _box("I declare that I have read the HPE Recruitment Notice and agree to receive job alerts")
    assert terms_category(alerts) == "CONSENT"
    assert terms_category(_box("I agree")) == "CONSENT"


def test_hpe_notice_is_checked_before_create_account():
    notice = _box(_HPE)
    marketing = Control(kind="checkbox", label="I agree to receive promotional SMS", selector="#sms", required=True)
    snapshot = PageSnapshot(controls=[notice, marketing], buttons=[], password_present=True, heading="Create Account")
    checked: list[str] = []

    class Locator:
        def __init__(self, selector: str) -> None:
            self.selector = selector

        def check(self, **_kwargs) -> None:
            checked.append(self.selector)

        def uncheck(self, **_kwargs) -> None:
            return None

        def is_checked(self) -> bool:
            return self.selector in checked

        def click(self, **_kwargs) -> None:
            checked.append(self.selector)

    class Page:
        def locator(self, selector: str) -> Locator:
            return Locator(selector)

    bind_standard_terms(True)
    try:
        _rows, pending = _accept_account_terms(Page(), snapshot, accept=True)
    finally:
        consume_auto_consents()
    assert "#box" in checked
    assert "#sms" not in checked
    assert pending == ["I agree to receive promotional SMS"]


def test_plain_sign_in_is_not_a_verify_hold():
    plain = PageSnapshot(
        controls=[
            Control(kind="text", label="Email", selector="#email"),
            Control(kind="password", label="Password", selector="#password"),
        ],
        buttons=[ButtonControl(name="Sign In", selector="#sign")],
        password_present=True,
        heading="Sign In",
    )
    url = "https://hpe.wd5.myworkdayjobs.com/en-US/Extra/job/1"
    assert plain_sign_in_page(plain, url) is True
    mailed = PageSnapshot(
        controls=list(plain.controls),
        buttons=list(plain.buttons),
        password_present=True,
        heading="Sign In",
        banner="Please verify your account. An email has been sent.",
    )
    assert plain_sign_in_page(mailed, url) is False
    assert plain_sign_in_page(None, url) is False


def test_nested_menu_offers_leaves_and_does_not_commit_the_parent():
    found = [
        Option("Job Board", "Job Board", "#job", leaf=False, folder=True),
        Option("Advertisement", "Advertisement", "#ad", leaf=True, folder=False),
    ]
    children = [
        Option("Indeed", "Indeed", "#indeed", leaf=True),
        Option("LinkedIn", "LinkedIn", "#li", leaf=True),
    ]
    labels = terminal_menu_labels(found, [("Job Board", children)])
    assert "Job Board" not in labels
    assert "Advertisement" in labels
    assert "Job Board > Indeed" in labels
    assert "Job Board > LinkedIn" in labels
    assert child_for_answer("Job Board", "Job Board", children) is None
    assert child_for_answer("Job Board > Indeed", "Job Board", children).label == "Indeed"
    assert child_for_answer("Indeed", "Advertisement", [Option("Campus", "Campus", "#c", leaf=True)]) is None
    only = [Option("Corporate Website", "Corporate Website", "#corp", leaf=True)]
    assert child_for_answer("Career Websites", "Career Websites", only).label == "Corporate Website"


def test_ashby_unknowns_and_invalid_phone_share_one_gap_pass():
    profile = _profile()
    controls = [
        Control(kind="text", label="LinkedIn", selector="#li"),
        Control(kind="text", label="Twitter", selector="#tw"),
        Control(kind="text", label="GitHub", selector="#gh"),
        Control(kind="text", label="Portfolio", selector="#po"),
        Control(
            kind="textarea",
            label="Are you able to work onsite, 5 days a week, in the location listed on this job?",
            selector="#onsite",
            required=True,
        ),
        Control(kind="text", label="Phone", selector="#phone", required=True),
    ]
    snapshot = PageSnapshot(controls=controls, buttons=[])
    page = _Page(invalid="#phone")
    gaps = _with_study_gaps(page, snapshot, profile, required_gaps(page, snapshot, profile))
    texts = " ".join(str(item.get("text") or "") for item in gaps)
    selectors = {str(item.get("selector") or "") for item in gaps}
    assert {"#li", "#tw", "#gh", "#po", "#onsite", "#phone"} <= selectors
    assert "LinkedIn" in texts
    assert "onsite" in texts.casefold()
    assert len([item for item in gaps if item.get("selector") == "#phone"]) == 1
    filled = _profile(linkedin_url="https://linkedin.example/in/jahnavi")
    again = _with_study_gaps(page, snapshot, filled, required_gaps(page, snapshot, filled))
    assert "#li" not in {str(item.get("selector") or "") for item in again}


def test_resume_and_question_pauses_can_proceed_on_the_same_job():
    profile = _profile()
    job = LocalJob("sess-1", "https://jobs.ashbyhq.com/rainmaker/1/application", profile)
    job.state = "done"
    job.result = {"status": Status.RESUME_UPLOAD_REQUIRED}
    assert _continue_allowed(job) is True
    job.result = {"status": Status.MANUAL_ANSWER_REQUIRED}
    assert _continue_allowed(job) is True
    job._credential_candidate = "cand-hpe"
    job.profile = None
    job.binding = None
    assert _fill_identity(job)["candidate_id"] == "cand-hpe"


def test_popup_script_has_proceed_and_can_hide_save():
    from autofill.answer_popup import _SHOW_JS

    assert 'setAttribute("data-role", "proceed")' in _SHOW_JS
    assert 'setAttribute("data-role", "save-continue")' in _SHOW_JS
    assert "args.proceedOnly" in _SHOW_JS
    assert 'state.state = "proceed"' in _SHOW_JS
