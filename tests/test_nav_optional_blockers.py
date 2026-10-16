"""Optional or already-answered questions must not stop Next. A revealed step is filled.

Regression for the Jobvite multi-page flow (consent gate, revealed form, NEXT,
an empty interstitial step, and a final step with Submit Application).
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from playwright.sync_api import sync_playwright

from autofill import AutofillOptions, Status, autofill_application
from autofill.profile import CandidateProfile
from autofill.resolver import ResolverBinding

URL = "https://jobs.jobvite.com/example/job/oExample/apply"
TOKEN = "synthetic-resolver-token-0123456789abcdef"
FIELDS = {
    "firstName": "Test",
    "lastName": "Candidate",
    "fullName": "Test Candidate",
    "email": "test.candidate@example.com",
    "address.country": "United States",
}

_HEAD = """<!doctype html><html><body><main>
<h1>Example Engineer</h1>
<form id="apply" novalidate onsubmit="return false">
"""

_STEP_SCRIPT = """
<script>
window.__submitClicks = 0;
function reveal() { document.getElementById('form1').hidden = false; }
function showStep(id) {
  for (const s of document.querySelectorAll('.step')) s.hidden = true;
  document.getElementById(id).hidden = false;
}
function goStep2() {
  for (const el of document.querySelectorAll('#step1 [required]')) {
    if (!el.value) { document.getElementById('err').hidden = false; return; }
  }
  showStep("step2");
}
</script>
"""

_FINAL = """
<section class="step" id="step2" hidden>
  <h2>Additional Information</h2>
  <label for="note">Anything else we should know?</label><textarea id="note" name="note"></textarea>
  <button type="button" id="next2" onclick="showStep('step3')">NEXT</button>
</section>
<section class="step" id="step3" hidden>
  <h2>Voluntary Self Identification</h2>
  <label for="sig">Your Name</label><input id="sig" name="sig" type="text">
  <button type="submit" id="send" onclick="window.__submitClicks++">Submit Application</button>
</section>
"""


def _page_html(extra: str, *, gate: bool = False) -> str:
    reveal = "hidden" if gate else ""
    gate_html = ""
    if gate:
        gate_html = """
<section id="consent">
  <h2>Data Consent</h2>
  <label for="jv-country-select">Location of Residence and Language:</label>
  <select id="jv-country-select" required onchange="setTimeout(reveal, 300)">
    <option value="">Select your location of residence and language</option>
    <option value="us">United States &amp; Other (not in list below)</option><option value="uk">United Kingdom</option>
  </select>
</section>"""
    return (
        _HEAD
        + gate_html
        + f"""
<section class="step" id="step1">
 <div id="form1" {reveal}>
  <h2>Personal Information</h2>
  <label for="fn">First Name*</label><input id="fn" name="fn" type="text" required>
  <label for="ln">Last Name*</label><input id="ln" name="ln" type="text" required>
  <label for="em">Email*</label><input id="em" name="em" type="email" required>
  {extra}
  <div id="err" role="alert" hidden>The above information is required.</div>
  <button type="button" id="next1" onclick="goStep2()">NEXT &rarr;</button>
 </div>
</section>
"""
        + _FINAL
        + "</form></main>"
        + _STEP_SCRIPT
        + "</body></html>"
    )


_REFERRAL = """
  <label for="ref">If an Employee Referral please provide name</label><input id="ref" name="ref" type="text">"""
_OPTIONAL_CONSENT = """
  <label for="hear">How did you hear about us?*</label>
  <div><input type="checkbox" id="li" name="li"> I agree to allow my application statuses to be visible inside
  LinkedIn Recruiter (optional).</div>"""
_WORK_STATUS = """
  <label for="ws">Work Status*</label>
  <select id="ws" name="ws" required><option value="">Select an option...</option>
  <option value="a">Citizen Example</option><option value="b">Visa Example</option></select>"""


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


@pytest.fixture
def resolver():
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8"))
            fields = {key: FIELDS[key] for key in body.get("fields", []) if key in FIELDS}
            encoded = json.dumps({"fields": fields, "answers": [], "unresolved": []}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, fmt: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}/resolve"
    finally:
        server.shutdown()


def _binding(url: str) -> ResolverBinding:
    return ResolverBinding(
        url=url,
        token=TOKEN,
        expires_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        candidate_ref="ref-a",
        job_ref="job-a",
        timeout_seconds=5,
        retries=1,
    )


def _profile() -> CandidateProfile:
    return CandidateProfile.from_dict(
        {"candidateId": "ref-a", "personal": {"full_name": "Test Candidate", "email": "test.candidate@example.com"}}
    )


def _run(page, url: str, *, updated: bool):
    return autofill_application(
        URL,
        _profile(),
        None,
        AutofillOptions(page=page, resolver=_binding(url), session_id="sess-nav", answers_updated=updated),
    )


def _continue_until(page, url: str, target: str, calls: int = 3):
    result = None
    for index in range(calls):
        result = _run(page, url, updated=index > 0)
        if page.locator(target).is_visible():
            break
    return result


@pytest.mark.parametrize("extra", [_REFERRAL, _OPTIONAL_CONSENT], ids=["optional_referral", "optional_consent"])
def test_optional_question_does_not_block_next(browser, resolver, extra):
    page = browser.new_page()
    try:
        page.set_content(_page_html(extra))
        result = _continue_until(page, resolver, "#step3")
        assert page.locator("#step3").is_visible(), result.messages
        assert result.next_clicks >= 1
        assert page.evaluate("window.__submitClicks") == 0
        assert page.locator("#ref").count() == 0 or page.locator("#ref").input_value() == ""
        assert page.locator("#li").count() == 0 or page.locator("#li").is_checked() is False
    finally:
        page.close()


def test_select_the_person_already_set_does_not_block_next(browser, resolver):
    page = browser.new_page()
    try:
        page.set_content(_page_html(_WORK_STATUS))
        first = _run(page, resolver, updated=False)
        assert first.status == Status.MANUAL_ANSWER_REQUIRED
        assert not page.locator("#step2").is_visible()
        page.select_option("#ws", "b")
        result = _continue_until(page, resolver, "#step3", calls=2)
        assert page.locator("#step3").is_visible(), result.messages
        assert page.evaluate("window.__submitClicks") == 0
    finally:
        page.close()


def test_jobvite_flow_reveal_then_next_twice_stops_before_submit(browser, resolver):
    page = browser.new_page()
    try:
        page.set_content(_page_html(_REFERRAL + _OPTIONAL_CONSENT, gate=True))
        first = _run(page, resolver, updated=False)
        # The location write reveals the form. The engine fills it instead of reporting Ready.
        assert first.status != Status.FILLED or page.locator("#step3").is_visible()
        assert page.locator("#fn").input_value() == "Test"
        assert page.locator("#em").input_value() == "test.candidate@example.com"
        result = _continue_until(page, resolver, "#step3", calls=2)
        assert page.locator("#step3").is_visible(), result.messages
        assert result.next_clicks >= 2
        assert page.evaluate("window.__submitClicks") == 0
        assert "Submit Application" in result.submit_controls
    finally:
        page.close()


def _snap(selectors: list[str], heading: str = "Example", alerts: tuple = ()):
    from types import SimpleNamespace

    controls = [SimpleNamespace(selector=item, hidden=False, frame_url="") for item in selectors]
    return SimpleNamespace(controls=controls, heading=heading, alerts=list(alerts))


def test_validation_redraw_is_not_an_advance_but_a_new_step_is():
    from types import SimpleNamespace

    from autofill.pagewait import navigation_changed, navigation_signature

    page = SimpleNamespace(url=URL)
    before = navigation_signature(page, _snap(["#a", "#b", "#c"]))
    assert navigation_changed(before, page, _snap(["#a", "#b", "#c"])) is False
    # The employer re-renders the same step with an error and one extra control.
    redraw = _snap(["#a", "#b", "#c", "#d"], alerts=("This field is required",))
    assert navigation_changed(before, page, redraw) is False
    # The click opened the next section below the first one, with no error.
    assert navigation_changed(before, page, _snap(["#a", "#b", "#c", "#d"])) is True
    # A persistent gate stays and the step's own field is replaced.
    assert navigation_changed(before, page, _snap(["#a", "#b", "#x"])) is True
    assert navigation_changed(before, page, _snap(["#y"])) is True
    assert navigation_changed(before, page, _snap(["#a", "#b", "#c"], heading="Review")) is True
