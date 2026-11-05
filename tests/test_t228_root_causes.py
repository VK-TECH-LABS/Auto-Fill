"""t228: one save status, known profile fields, and nested prompt leaves.

No employer site is contacted. The pages are fixtures.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import sync_playwright

from autofill.answer_popup import show_popup
from autofill.engine import AutofillOptions, Status, autofill_application
from autofill.local_helper import (
    _STATUS_REJECTED,
    _STATUS_SAVED,
    LocalJob,
    _data_version,
    _profile_basic_value,
    _read_back_receipt,
    _remember_answer,
    _stamp_receipt,
)
from autofill.mapping import map_field
from autofill.models import Control
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


def _person(**personal) -> CandidateProfile:
    fields = {
        "full_name": "Jahnavi Torra",
        "first_name": "Jahnavi",
        "last_name": "Torra",
        "email": "jahnavi.torra@example.com",
        "phone": "9155550100",
        "city": "Austin",
        "linkedin_url": "https://www.linkedin.com/in/jahnavi-torra",
        "portfolio_url": "https://github.com/jahnavi-torra",
    }
    fields.update(personal)
    return CandidateProfile.from_dict({"candidateId": "cand-t228", "personal": fields})


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


def test_failure_status_is_one_line(browser):
    page = browser.new_page()
    try:
        page.set_content("<h1>My Information</h1>")
        show_popup(
            page,
            {"id": "q-hear", "prompt": "How Did You Hear About Us?", "type": "single_select", "options": ["Indeed"]},
            nonce="nonce-fail",
            message=_STATUS_REJECTED,
            error=True,
        )
        text = _card_text(page)
        assert "did not accept" in text.casefold()
        assert "saved to this candidate" not in text.casefold()
        assert "saved & verified" not in text.casefold()
        assert "did not confirm" not in text.casefold()
    finally:
        page.close()


def test_saved_status_does_not_also_say_the_save_failed(browser):
    page = browser.new_page()
    try:
        page.set_content("<h1>Application</h1>")
        show_popup(
            page,
            {"id": "q1", "prompt": "Work authorization?", "type": "text", "options": []},
            nonce="nonce-ok",
            message=_STATUS_SAVED,
            error=False,
        )
        text = _card_text(page)
        assert "Saved & Verified" in text
        assert "did not confirm" not in text.casefold()
        assert "did not accept" not in text.casefold()
        assert "saved to this candidate" not in text.casefold()
    finally:
        page.close()


def test_receipt_read_back_is_bound_to_candidate_job_session_and_version():
    profile = _person()
    job = LocalJob(
        "sess-a",
        "https://amat.wd1.myworkdayjobs.com/en-US/External/job/x/apply",
        profile,
        {"candidateId": "cand-t228", "jobId": "job-amat"},
    )
    item = {"questionId": "q-hear", "text": "How Did You Hear About Us?", "fieldKey": "how_heard"}
    _stamp_receipt(job, item, ["Job Board or Social Media > Indeed"])
    assert _read_back_receipt(job, item, ["Job Board or Social Media > Indeed"]) is True
    assert _read_back_receipt(job, item, ["Indeed"]) is False
    other = LocalJob(
        "sess-b",
        job.application_url,
        _person(),
        {"candidateId": "cand-other", "jobId": "job-amat"},
    )
    other.save_receipts = dict(job.save_receipts)
    assert _read_back_receipt(other, item, ["Job Board or Social Media > Indeed"]) is False
    stale = _data_version(job)
    job.profile.personal.email = "someone.else@example.com"
    assert _data_version(job) != stale
    assert _read_back_receipt(job, item, ["Job Board or Social Media > Indeed"]) is False


def test_the_same_answer_is_stored_once():
    profile = _person()
    job = LocalJob("sess-a", "https://jobs.example.com/apply", profile, {"candidateId": "cand-t228", "jobId": "job-1"})
    item = {
        "questionId": "q-hear",
        "text": "How Did You Hear About Us?",
        "fieldKey": "how_heard",
        "kind": "QUESTION",
    }
    target = {"values": ["Job Board or Social Media > Indeed"], "value": "Job Board or Social Media > Indeed"}
    _remember_answer(job, item, target)
    _remember_answer(job, item, target)
    heard = [row for row in profile.application_answers if "Indeed" in row.answer]
    assert len(heard) == 1
    assert profile.screening.how_heard == "Job Board or Social Media > Indeed"


def test_legal_name_ids_map_to_the_name_part_not_the_section():
    profile = _person()
    first = map_field(
        Control(
            kind="text",
            label="Legal Name",
            automation_id="legalNameSection_firstName",
            form_automation_id="formField-legalNameSection_firstName",
            selector="#first",
        ),
        profile,
    )
    last = map_field(
        Control(
            kind="text",
            label="Legal Name",
            automation_id="legalNameSection_lastName",
            form_automation_id="formField-legalNameSection_lastName",
            selector="#last",
        ),
        profile,
    )
    assert first.key == "first_name"
    assert first.action == "fill"
    assert first.text == "Jahnavi"
    assert last.key == "last_name"
    assert last.text == "Torra"


def test_known_name_and_links_are_filled_and_not_asked(browser):
    page = browser.new_page()
    try:
        page.set_content(
            """
            <h1>My Information</h1>
            <div data-automation-id="legalNameSection">
              <h2>Legal Name</h2>
              <div data-automation-id="formField-legalNameSection_firstName">
                <div>First Name<abbr>*</abbr></div>
                <input id="first" data-automation-id="legalNameSection_firstName" aria-required="true">
              </div>
              <div data-automation-id="formField-legalNameSection_lastName">
                <div>Last Name<abbr>*</abbr></div>
                <input id="last" data-automation-id="legalNameSection_lastName" aria-required="true">
              </div>
            </div>
            <div class="ashby-application-form-field-entry">
              <div>LinkedIn Profile URL<abbr>*</abbr></div>
              <input id="li" aria-required="true">
            </div>
            <div class="ashby-application-form-field-entry">
              <div>Website / Portfolio URL<abbr>*</abbr></div>
              <input id="web" aria-required="true">
            </div>
            <div class="ashby-application-form-field-entry">
              <div>Are you able to work onsite, 5 days a week?*</div>
              <textarea id="onsite" required></textarea>
            </div>
            <button type="button">Save and Continue</button>
            """
        )
        result = autofill_application(
            "https://jobs.ashbyhq.com/gem/1/application",
            _person(),
            None,
            AutofillOptions(page=page, candidate_id="cand-t228", session_id="names-1"),
        )
        assert page.locator("#first").input_value() == "Jahnavi"
        assert page.locator("#last").input_value() == "Torra"
        assert "linkedin.com/in/jahnavi-torra" in page.locator("#li").input_value()
        assert "github.com/jahnavi-torra" in page.locator("#web").input_value()
        texts = " ".join(str(item.get("text") or "") for item in result.open_items).casefold()
        assert "first name" not in texts
        assert "last name" not in texts
        assert "linkedin" not in texts
        assert "portfolio" not in texts
        assert "onsite" in texts
        assert result.stopped_before_submit is True
    finally:
        page.close()


_HEAR = """
<h1>My Information</h1>
<div data-automation-id="formField-source">
  <label>How Did You Hear About Us?<abbr>*</abbr></label>
  <div data-automation-id="multiSelectContainer">
    <input id="source" data-automation-id="searchBox" role="combobox" aria-label="Search"
      placeholder="Search" aria-required="true">
    <button type="button" id="source-prompt" data-automation-id="promptIcon" aria-label="Search"></button>
    <span id="picked" data-automation-id="selectedItem" hidden></span>
  </div>
</div>
<button type="button">Save and Continue</button>
<script>
  const parents = ["Job Board or Social Media", "Staffing Agency"];
  const children = {
    "Job Board or Social Media": ["Indeed", "LinkedIn", "Dice.com"],
    "Staffing Agency": ["Aerotek"]
  };
  const source = document.getElementById("source");
  const picked = document.getElementById("picked");
  function paint(rows, nested) {
    const prior = document.getElementById("source-menu");
    if (prior) prior.remove();
    const menu = document.createElement("div");
    menu.id = "source-menu";
    menu.setAttribute("role", "listbox");
    rows.forEach((label) => {
      const row = document.createElement("div");
      row.id = "opt-" + label.toLowerCase().replace(/[^a-z0-9]+/g, "-");
      row.setAttribute("data-automation-id", "promptOption");
      row.style.cssText = "display:block;height:24px";
      row.textContent = label;
      row.addEventListener("click", () => {
        if (!nested && children[label]) paint(children[label], true);
        else {
          picked.hidden = false;
          picked.textContent = label;
          source.value = "";
          source.removeAttribute("aria-expanded");
          menu.remove();
        }
      });
      menu.appendChild(row);
    });
    document.body.appendChild(menu);
    source.setAttribute("aria-expanded", "true");
  }
  function openParents() {
    if (!picked.hidden && picked.textContent.trim()) return;
    paint(parents, false);
  }
  source.addEventListener("click", openParents);
  document.getElementById("source-prompt").addEventListener("click", openParents);
</script>
"""


def test_workday_prompt_without_a_chevron_lists_leaves_and_does_not_pick_one(browser):
    from autofill.combobox import list_combobox_options, select_combobox

    page = browser.new_page()
    try:
        page.set_content(_HEAR)
        labels = list_combobox_options(page, "#source")
        assert "Job Board or Social Media > Indeed" in labels
        assert "Job Board or Social Media > LinkedIn" in labels
        assert "Staffing Agency > Aerotek" in labels
        assert "Job Board or Social Media" not in labels
        assert "Staffing Agency" not in labels
        assert page.locator("#picked").inner_text().strip() == ""
        missed = select_combobox(page, "#source", "Job Board or Social Media")
        assert missed is None
        assert page.locator("#picked").inner_text().strip() == ""
        picked = select_combobox(page, "#source", "Job Board or Social Media > Indeed")
        assert picked is not None
        assert picked.label == "Indeed"
        assert page.locator("#picked").inner_text().strip() == "Indeed"
    finally:
        page.close()


def test_unknown_hear_about_card_gets_leaves_not_parents(browser):
    page = browser.new_page()
    try:
        page.set_content(_HEAR)
        result = autofill_application(
            "https://amat.wd1.myworkdayjobs.com/en-US/External/job/Austin/apply",
            _person(linkedin_url="", portfolio_url=""),
            None,
            AutofillOptions(page=page, candidate_id="cand-t228", session_id="hear-1"),
        )
        hear = [
            item
            for item in result.open_items
            if "how did you hear" in str(item.get("text") or "").casefold()
        ]
        assert hear
        options = [str(option) for option in hear[0].get("options") or []]
        assert "Job Board or Social Media > Indeed" in options
        assert "Job Board or Social Media" not in options
        assert page.locator("#picked").inner_text().strip() == ""
        assert result.status == Status.MANUAL_ANSWER_REQUIRED
        assert result.stopped_before_submit is True
        texts = " ".join(str(item.get("text") or "") for item in result.open_items).casefold()
        assert "first name" not in texts
    finally:
        page.close()


def _read_back(candidate: str, answer: str, *, version: str = "ans-1") -> dict:
    return {
        "candidateId": candidate,
        "key": "how_heard",
        "question": "How Did You Hear About Us?",
        "answer": answer,
        "version": version,
        "updatedAt": "2026-11-05T18:12:00Z",
    }


def _confirm(candidate: str, answer: str, *, version: str = "ans-1", **extra) -> dict:
    body = {
        "saved": True,
        "verified": True,
        "candidateId": candidate,
        "keys": ["how_heard"],
        "version": version,
        "updatedAt": "2026-11-05T18:12:00Z",
        "canonicalKey": "how_heard",
        "readBack": _read_back(candidate, answer, version=version),
    }
    body.update(extra)
    return body


class _Tilearc:
    """Scripted #173 origin. ``confirm`` is the POST body, or an exception."""

    def __init__(self, *, confirm=None, read=None, status=None, session=None) -> None:
        self.confirm = confirm
        self.read = read
        self.status = status or (200, {"profileFingerprint": "fp-old", "profileVersion": "2026-11-05T18:00:00Z"})
        self.session = session or (200, {})
        self.calls: list[tuple] = []

    def __call__(self, method, path, query, body):
        self.calls.append((method, path, dict(query), body))
        if path.endswith("/local-status"):
            return self.status
        if path.endswith("/local-answer") and method == "GET":
            return self.read if self.read is not None else (200, {})
        if path.endswith("/local-answer"):
            if isinstance(self.confirm, Exception):
                raise self.confirm
            return self.confirm if self.confirm is not None else (200, {})
        return self.session


def _client(script: _Tilearc):
    from autofill.tilearc_sync import TilearcClient

    return TilearcClient("https://vinayaka.example", script)


def test_confirm_is_trusted_only_when_read_back_matches():
    from autofill.tilearc_sync import ack_is_trusted, parse_confirm_body

    good = parse_confirm_body(_confirm("cand-t228", "Job Board or Social Media > Indeed"))
    assert ack_is_trusted(good, candidate_id="cand-t228", submitted="Job Board or Social Media > Indeed")
    assert not ack_is_trusted(good, candidate_id="cand-other", submitted="Job Board or Social Media > Indeed")
    assert not ack_is_trusted(good, candidate_id="cand-t228", submitted="Indeed")
    wrong = parse_confirm_body(_confirm("cand-other", "Job Board or Social Media > Indeed"))
    assert not ack_is_trusted(wrong, candidate_id="cand-t228", submitted="Job Board or Social Media > Indeed")
    assert parse_confirm_body({"saved": True, "candidateId": "cand-t228"}) is None
    unverified = parse_confirm_body({"saved": False, "verified": False})
    assert not ack_is_trusted(unverified, candidate_id="cand-t228", submitted="Indeed")


def test_timeout_then_get_read_back_confirms_and_a_mismatch_does_not():
    from autofill.tilearc_sync import TilearcTransportError

    job = LocalJob(
        "sess-local",
        "https://amat.wd1.myworkdayjobs.com/en-US/External/job/x/apply",
        _person(),
        {"tilearcSessionId": "sess-tile", "candidateId": "cand-t228", "jobId": "job-amat", "profileFingerprint": "fp"},
    )
    job.open_items = [
        {
            "questionId": "q-hear",
            "text": "How Did You Hear About Us?",
            "fieldKey": "how_heard",
            "answerType": "single_select",
            "options": ["Job Board or Social Media > Indeed"],
        }
    ]
    stored = [{"questionId": "q-hear", "values": ["Job Board or Social Media > Indeed"]}]
    timed_out = _Tilearc(
        confirm=TilearcTransportError("timeout"),
        read=(200, _read_back("cand-t228", "Job Board or Social Media > Indeed")),
    )
    job.tilearc = _client(timed_out)
    from autofill.local_helper import _tilearc_ack

    assert _tilearc_ack(job, stored) is True
    assert any(method == "GET" and path.endswith("/local-answer") for method, path, _q, _b in timed_out.calls)
    mismatch = _Tilearc(
        confirm=(200, {"saved": True}),
        read=(200, _read_back("cand-other", "Job Board or Social Media > Indeed")),
    )
    job.tilearc = _client(mismatch)
    job.answer_versions = {}
    assert _tilearc_ack(job, stored) is False


def test_repost_keeps_the_same_answer_version():
    from autofill.local_helper import _tilearc_ack

    job = LocalJob(
        "sess-local",
        "https://jobs.example.com/apply",
        _person(),
        {"tilearcSessionId": "sess-tile", "candidateId": "cand-t228", "jobId": "job-1", "profileFingerprint": "fp"},
    )
    job.open_items = [{"questionId": "q-hear", "text": "How Did You Hear About Us?", "fieldKey": "how_heard"}]
    stored = [{"questionId": "q-hear", "values": ["Job Board or Social Media > Indeed"]}]
    script = _Tilearc(confirm=(200, _confirm("cand-t228", "Job Board or Social Media > Indeed", version="ans-9")))
    job.tilearc = _client(script)
    assert _tilearc_ack(job, stored) is True
    assert _tilearc_ack(job, stored) is True
    posts = [body for method, path, _query, body in script.calls if method == "POST"]
    assert len(posts) == 2
    assert posts[0]["answer"] == posts[1]["answer"] == "Job Board or Social Media > Indeed"
    assert job.answer_versions["q-hear"] == "ans-9"


def test_changed_fingerprint_adopts_roster_names_and_409_drops_the_old_snapshot():
    from autofill.local_helper import _refresh_posted_profile
    from autofill.tilearc_sync import pull_latest

    posted = {
        "candidateId": "cand-t228",
        "personal": {
            "full_name": "Old Name",
            "first_name": "Old",
            "last_name": "Name",
            "email": "old@example.com",
        },
    }
    session = {
        "candidateId": "cand-t228",
        "profileFingerprint": "fp-new",
        "profileVersion": "2026-11-05T18:12:00Z",
        "profile": {
            "candidateId": "cand-t228",
            "personal": {"full_name": "Jahnavi Torra", "email": "jahnavi.torra@example.com"},
        },
        "candidateProfile": {
            "personal": {"first_name": "Jahnavi", "last_name": "Torra", "email": "jahnavi.torra@example.com"}
        },
    }
    script = _Tilearc(
        status=(200, {"profileFingerprint": "fp-new", "profileVersion": "2026-11-05T18:12:00Z"}),
        session=(200, session),
    )
    result = pull_latest(
        _client(script),
        session_id="sess-tile",
        candidate_id="cand-t228",
        fingerprint="fp-old",
        version="2026-11-05T17:00:00Z",
        posted=posted,
    )
    assert result.blocked is False
    assert result.stale is True
    assert result.profile_body["personal"]["first_name"] == "Jahnavi"
    assert result.profile_body["personal"]["last_name"] == "Torra"
    assert result.profile_body["personal"]["email"] == "jahnavi.torra@example.com"
    binding = {
        "tilearcSessionId": "sess-tile",
        "candidateId": "cand-t228",
        "jobId": "job-amat",
        "profileFingerprint": "fp-old",
    }
    fresh, updated, version, stale = _refresh_posted_profile(_person(), binding, _client(script))
    assert stale is True
    assert fresh.personal.first_name == "Jahnavi"
    assert fresh.personal.email == "jahnavi.torra@example.com"
    assert updated["profileFingerprint"] == "fp-new"
    assert version == "2026-11-05T18:12:00Z"
    blocked = _Tilearc(
        status=(409, {"code": "IDENTITY_MISMATCH"}),
        session=(200, {"ok": True}),
    )
    refused = pull_latest(
        _client(blocked),
        session_id="sess-tile",
        candidate_id="cand-t228",
        fingerprint="fp-old",
        version="2026-11-05T17:00:00Z",
        posted=posted,
    )
    assert refused.blocked is True
    assert refused.profile_body is None


def test_live_staging_origin_is_used_only_when_credentials_are_forwarded(monkeypatch):
    from autofill.local_helper import _tilearc_client
    from autofill.tilearc_sync import STAGING_ORIGIN

    monkeypatch.setenv("TILEARC_API_ORIGIN", STAGING_ORIGIN)
    assert _tilearc_client(None) is None
    client = _tilearc_client({"authorization": "Bearer staged-token"})
    assert client is not None
    assert client.origin == STAGING_ORIGIN


def test_profile_links_match_the_short_prompt():
    profile = _person()
    assert _profile_basic_value(profile, {"text": "LinkedIn Profile URL"}) == profile.personal.linkedin_url
    assert "github.com" in _profile_basic_value(profile, {"text": "Website / Portfolio URL"})
    assert _profile_basic_value(profile, {"text": "First Name"}) == "Jahnavi"
    assert _profile_basic_value(profile, {"prompt": "Favorite color?"}) == ""
