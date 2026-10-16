"""iCIMS paints the posting inside icims_content_iframe after DOMContentLoaded.

Apply must be found in the iframe, clicked once, and the email-first /login
gate behind it is a login wall: nothing is typed and Next is not clicked.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from autofill.engine import AutofillOptions, Status, autofill_application
from autofill.models import ButtonControl, Control, PageSnapshot
from autofill.pagewait import icims_login_gate
from autofill.profile import CandidateProfile

BASE = "https://careers-test.icims.com/jobs/7/engineer"
CHROME = """<header><nav><button>Who We Are</button><button>News</button>
<label>Search this site <input type="text" name="q"></label></nav></header>"""


def _outer(inner: str) -> str:
    return f"""<html><body>{CHROME}<main><div id="slot"></div></main>
<script>setTimeout(() => {{ const f = document.createElement('iframe');
f.id = 'icims_content_iframe'; f.name = 'icims_content_iframe'; f.src = '{inner}';
f.style.width = '900px'; f.style.height = '600px';
document.getElementById('slot').appendChild(f); }}, 1200);</script></body></html>"""


PAGES = {
    "/job": _outer(f"{BASE}/job?in_iframe=1"),
    "/job?in_iframe=1": f"""<html><body><h1>Electrical Engineer</h1><p>Job description. Responsibilities.</p>
<a href="{BASE}/login" target="_top" class="iCIMS_ApplyOnlineButton">Apply for this job online</a>
</body></html>""",
    "/login": _outer(f"{BASE}/login?in_iframe=1"),
    "/login?in_iframe=1": """<html><body><h2>Enter Your Information</h2>
<form onsubmit="return false"><label for="em">Email</label><input id="em" type="email" name="email">
<button id="enterEmailSubmitButton" type="submit">Next</button></form></body></html>""",
}


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


def _serve(route):
    key = route.request.url[len(BASE):]
    body = PAGES.get(key)
    if body is None:
        route.fulfill(status=404, body="")
        return
    route.fulfill(status=200, content_type="text/html", body=body)


def test_icims_iframe_apply_leads_to_login_required(browser):
    page = browser.new_page()
    try:
        page.route("https://careers-test.icims.com/**", _serve)
        page.goto(f"{BASE}/job")
        profile = CandidateProfile.from_dict(
            {"candidateId": "ref-i", "personal": {"full_name": "River Example", "email": "river@example.com"}}
        )
        result = autofill_application(
            f"{BASE}/job", profile, None, AutofillOptions(page=page, session_id="sess-icims", timings={})
        )
        assert result.status == Status.LOGIN_REQUIRED
        assert page.url.endswith("/login")
        frame = next(f for f in page.frames if "in_iframe=1" in f.url)
        assert frame.locator("#em").input_value() == ""
    finally:
        page.close()


def test_icims_login_gate_needs_icims_login_url_and_lone_email():
    email = Control(selector="#em", kind="email", label="Email", frame_url=f"{BASE}/login?in_iframe=1")
    gate = PageSnapshot(controls=[email], buttons=[ButtonControl(name="Next", selector="#n")])
    assert icims_login_gate(gate, f"{BASE}/login")
    assert not icims_login_gate(gate, f"{BASE}/candidate")
    assert not icims_login_gate(gate, "https://jobs.example.com/login")
    name = Control(selector="#fn", kind="text", label="First Name", frame_url=email.frame_url)
    assert not icims_login_gate(PageSnapshot(controls=[email, name], buttons=[]), f"{BASE}/login")
