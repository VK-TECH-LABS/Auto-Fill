"""ATS host detection from ApplyPilot's form notes and sites.yaml lists."""

from autofill.ats import detect_ats, is_blocked_sso, is_manual_ats


def test_known_application_hosts():
    workday = detect_ats("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/1/apply")
    assert workday is not None
    assert workday.name == "workday"
    assert workday.multipage and workday.resume_first

    taleo = detect_ats("https://acme.taleo.net/careersection/application")
    assert taleo is not None and taleo.name == "taleo" and taleo.multipage

    icims = detect_ats("https://careers-acme.icims.com/jobs/1/login")
    assert icims is not None and icims.name == "icims" and icims.multipage

    lever = detect_ats("https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555")
    assert lever is not None and lever.name == "lever" and lever.resume_first

    greenhouse = detect_ats("https://boards.greenhouse.io/acme/jobs/1")
    assert greenhouse is not None and greenhouse.name == "greenhouse"
    assert detect_ats("https://jobs.ashbyhq.com/acme/1111").name == "ashby"
    assert detect_ats("https://jobs.smartrecruiters.com/Acme/1").name == "smartrecruiters"


def test_unrelated_urls_are_not_an_ats():
    assert detect_ats("https://example.com/workday-article") is None
    assert detect_ats("file:///tmp/form.html") is None
    assert detect_ats("") is None


def test_manual_ats_and_sso_are_refused():
    assert is_manual_ats("https://ibegin.tcsapps.com/candidate/apply?id=1")
    assert not is_manual_ats("https://boards.greenhouse.io/acme/jobs/1")
    assert is_blocked_sso("https://accounts.google.com/o/oauth2/v2/auth")
    assert is_blocked_sso("https://login.microsoftonline.com/common/oauth2/authorize")
    assert is_blocked_sso("https://acme.okta.com/login/login.htm")
    assert is_blocked_sso("https://acme.auth0.com/authorize")
    assert not is_blocked_sso("https://example.com/okta-mentioned-in-the-path")
