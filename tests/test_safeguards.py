"""The submit safeguard has no off switch."""

from pathlib import Path

import pytest

from autofill.models import FillResult
from autofill.safeguards import HUMAN_SUBMIT_ONLY, SubmitBlockedError, assert_safe_to_activate, classify_control


@pytest.mark.parametrize(
    ("name", "control_type", "expected"),
    [
        ("Submit application", "submit", "submit"),
        ("Apply", "button", "submit"),
        ("Apply now", "button", "submit"),
        ("Send application", "submit", "submit"),
        ("Submit and continue", "submit", "submit"),
        ("Done", "submit", "submit"),
        ("Save and continue", "button", "continue"),
        ("Next", "submit", "continue"),
        ("Review", "button", "continue"),
        ("Log in", "submit", "auth"),
        ("Sign up", "submit", "auth"),
        ("Create account", "button", "auth"),
        ("Back", "button", "other"),
    ],
)
def test_classify_control(name, control_type, expected):
    assert classify_control(name, control_type=control_type) == expected


def test_only_continue_controls_pass_the_guard():
    assert HUMAN_SUBMIT_ONLY is True
    assert assert_safe_to_activate("Save and continue") == "continue"
    with pytest.raises(SubmitBlockedError):
        assert_safe_to_activate("Submit application", control_type="submit")
    with pytest.raises(SubmitBlockedError):
        assert_safe_to_activate("Apply now")
    with pytest.raises(SubmitBlockedError):
        assert_safe_to_activate("Log in", control_type="submit")
    with pytest.raises(SubmitBlockedError):
        assert_safe_to_activate("Learn more")


def test_result_cannot_claim_it_submitted():
    with pytest.raises(ValueError):
        FillResult(status="filled", stopped_before_submit=False)


def test_package_does_not_include_captcha_solving_or_unguarded_clicks():
    root = Path(__file__).resolve().parents[1] / "src" / "autofill"
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        assert "capsolver" not in lowered
        assert "result:applied" not in lowered
        if path.name != "safeguards.py":
            assert ".click(" not in text
