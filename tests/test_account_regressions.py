"""Shared-engine regressions from the Nov 2026 account screenshots.

Zekelman / HPE / Boeing: the required Create Account consent stays unchecked
after email and password are filled. SNC / Clark / GM: a plain Sign In is left
empty, or parked on email verification while the page is only Sign In.
Fermilab / Prysmian: a nested prompt menu is left open. SmartRecruiters: profile
basics are asked again on Save & Continue.

No employer site is contacted.
"""

from __future__ import annotations

from autofill.combobox import close_open_menu
from autofill.local_helper import _profile_basic_value
from autofill.login import score_account, sign_in_verification_holding
from autofill.mapping import (
    account_checkbox_required,
    bind_standard_terms,
    consume_auto_consents,
    terms_category,
)
from autofill.models import ButtonControl, Control, LinkControl, PageSnapshot
from autofill.profile import CandidateProfile

_ZEKELMAN = (
    "By submitting my information above or signing up for job alerts, I agree to receive "
    "phone or email communication from Zekelman and its subsidiaries about Zekelman "
    "employment and/or recruiting activities as they may arise. I understand that the "
    "Zekelman Privacy Policy will apply and that I may withdraw my consent to these "
    "communications at any time."
)
_HPE = (
    "I declare that I have read the HPE Recruitment Notice and agree to HPE processing "
    "my personal data for the purposes of my applying or employment with HPE."
)
_BOEING = "I have received the Boeing Applicant Privacy Notice."


def _box(label: str, **kwargs) -> Control:
    return Control(kind="checkbox", label=label, selector="#box", required=True, **kwargs)


def test_create_account_privacy_boxes_are_standard_and_compound_opt_ins_are_not():
    understand = Control(
        kind="checkbox",
        label="I understand",
        prompt=_ZEKELMAN,
        selector="#understand",
        required=True,
    )
    assert terms_category(understand) == "STANDARD_TERMS"
    assert account_checkbox_required(understand) is True
    assert terms_category(_box(_HPE)) == "STANDARD_TERMS"
    assert terms_category(_box(_BOEING)) == "STANDARD_TERMS"
    notice_only = _box("Boeing Applicant Privacy Notice")
    assert terms_category(notice_only) == "STANDARD_TERMS"
    assert terms_category(_box("I agree")) == "CONSENT"
    on_the_create_box = _box(
        "I agree",
        automation_id="createAccountCheckbox",
        prompt="By creating an external account you are agreeing to the Terms and Conditions.",
    )
    assert terms_category(on_the_create_box) == "CONSENT"
    assert terms_category(_box("I consent", automation_id="createAccountCheckbox")) == "STANDARD_TERMS"
    assert terms_category(_box("I understand")) == "CONSENT"
    assert terms_category(_box("I agree to the Privacy Policy and to receive job alerts")) == "CONSENT"
    assert terms_category(_box("I agree to binding arbitration")) == "CONSENT"
    assert terms_category(_box("I certify this is true under penalty of perjury")) == "CONSENT"
    assert terms_category(_box("I agree to receive promotional SMS")) == "CONSENT"
    mixed = _box("I understand", prompt="I agree to binding arbitration and the Privacy Policy")
    assert terms_category(mixed) == "CONSENT"


def test_i_understand_is_checked_before_create_account_submit():
    from autofill.account_flow import _accept_account_terms

    understand = Control(
        kind="checkbox",
        label="I understand",
        prompt=_ZEKELMAN,
        selector="#understand",
        required=True,
    )
    marketing = Control(
        kind="checkbox",
        label="I agree to receive promotional SMS",
        selector="#sms",
        required=True,
    )
    snapshot = PageSnapshot(
        controls=[understand, marketing],
        buttons=[],
        password_present=True,
        heading="Create Account",
    )
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

    class Page:
        def locator(self, selector: str) -> Locator:
            return Locator(selector)

    bind_standard_terms(True)
    try:
        _rows, pending = _accept_account_terms(Page(), snapshot, accept=True)
    finally:
        consume_auto_consents()
    assert checked == ["#understand"]
    assert any("promotional" in item.casefold() for item in pending)
    assert not any("understand" in item.casefold() and "promotional" not in item.casefold() for item in pending)


def test_plain_sign_in_is_login_with_create_account_alternate_not_a_verify_hold():
    """SNC and Clark: Email, Password, Sign In, and an unused Create Account link."""
    snapshot = PageSnapshot(
        heading="Sign In",
        password_present=True,
        page_text="Don't have an account yet? Create Account Forgot your password?",
        controls=[
            Control(kind="text", label="Email Address", selector="#email", input_type="text", automation_id="email"),
            Control(kind="password", label="Password", selector="#password", input_type="password"),
        ],
        buttons=[ButtonControl(name="Sign In", selector="#sign", automation_id="signInSubmitButton")],
        links=[LinkControl(name="Create Account", selector="#create", automation_id="createAccountLink")],
        automation_ids=["signInSubmitButton", "createAccountLink", "email"],
    )
    scored = score_account(snapshot, "https://snc.wd1.myworkdayjobs.com/en-US/SNC_External_Career_Site/login")
    assert scored.mode == "login"
    assert scored.alternate is True
    assert sign_in_verification_holding(snapshot) is False
    holding = PageSnapshot(
        heading="Sign In",
        password_present=True,
        page_text="An email has been sent to you. Please verify your account.",
        controls=[Control(kind="password", label="Password", selector="#password")],
        buttons=[ButtonControl(name="Sign In", selector="#sign")],
    )
    assert sign_in_verification_holding(holding) is True


def test_profile_basics_cover_smartrecruiters_labels():
    profile = CandidateProfile.from_dict(
        {
            "candidateId": "jahnavi",
            "personal": {
                "full_name": "Jahnavi Torra",
                "email": "jahnavitorra93@gmail.com",
                "phone": "+1 555 010 0199",
                "city": "Newyork",
            },
        }
    )
    assert _profile_basic_value(profile, {"prompt": "First name*"}) == "Jahnavi"
    assert _profile_basic_value(profile, {"prompt": "Last name*"}) == "Torra"
    assert _profile_basic_value(profile, {"prompt": "Given Name(s)"}) == "Jahnavi"
    assert _profile_basic_value(profile, {"prompt": "Confirm your email*"}) == "jahnavitorra93@gmail.com"
    assert _profile_basic_value(profile, {"prompt": "Email*"}) == "jahnavitorra93@gmail.com"
    assert _profile_basic_value(profile, {"prompt": "City*"}) == "Newyork"
    assert _profile_basic_value(profile, {"prompt": "Phone number*"}) == "+1 555 010 0199"
    assert _profile_basic_value(profile, {"prompt": "Confirm your email", "challenge": "confirm"}) == (
        "jahnavitorra93@gmail.com"
    )
    assert _profile_basic_value(profile, {"prompt": "Confirm your salary", "challenge": "confirm"}) == ""


def test_nested_prompt_is_closed_after_it_is_read():
    presses: list[str] = []
    state = {"open": 2}

    class Page:
        def evaluate(self, _script):
            return state["open"] > 0

        class keyboard:
            @staticmethod
            def press(key: str) -> None:
                presses.append(key)
                if key == "Escape" and state["open"]:
                    state["open"] -= 1

    close_open_menu(Page())
    assert presses == ["Escape", "Escape"]
    assert state["open"] == 0
