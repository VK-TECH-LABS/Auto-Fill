"""Recruitment SMS consent, separate from standard terms.

A form question about receiving text messages for an application is filled
with Yes only when this candidate's Application Answers already authorize it.
The authorization is ``RECRUITMENT_SMS_CONSENT`` (hosts may send the alias
``SMS_CONSENT``). It is not ``standardTermsAuthorized`` and it is not a
Terms of Service checkbox.

Marketing email, promotional SMS, AI recording, arbitration, e-sign,
background checks, demographic attestations, and other certifications stay
manual. A decline, or a missing authorization, is not auto-checked.
"""

from __future__ import annotations

import re
from contextvars import ContextVar

from autofill.models import Control, MappedField, Option
from autofill.profile import ApplicationAnswer, CandidateProfile

SMS_INTENT = "RECRUITMENT_SMS_CONSENT"
SMS_INTENT_ALIAS = "SMS_CONSENT"
SMS_CATEGORY = "RECRUITMENT_SMS_CONSENT"
# Canonical key on the candidate's applicationAnswers (vinayaka / Fillo).
SMS_KEY = "recruitment_sms_consent"
_KEY_ALIASES = frozenset({"recruitment_sms_consent", "sms_consent", "recruitment_sms"})

_SMS_WORD = re.compile(r"\b(?:sms|text messages?|text messaging)\b")
_PURPOSE = re.compile(
    r"\b(?:receive|receiving|consent|opt in|would you like|application|recruitment|recruiting|job)\b"
)
# A statement that also promises one of these is not a recruitment-SMS question.
# ``normalize`` has already turned punctuation into spaces (``e-sign`` → ``e sign``).
_UNRELATED = re.compile(
    r"\b(?:marketing|promotional|newsletter|job alerts?|product updates|special offers|"
    r"arbitration|electronic signature|e sign|esign|background check|"
    r"recording|notetaker|note taker|artificial intelligence|\bai\b|email|"
    r"certify|attest|attestation|demographic|gender|race|ethnicity|veteran|disability|"
    r"privacy policy|terms of service|terms of use|terms and conditions)\b"
)
_SELF_DECLINE = re.compile(
    r"\b(?:do not consent|dont consent|do not agree|do not wish|do not want|opt out)\b"
)


class _Scope:
    __slots__ = ("candidate_id", "refused")

    def __init__(self, candidate_id: str, refused: bool) -> None:
        self.candidate_id = candidate_id
        self.refused = refused


_SCOPE: ContextVar[_Scope | None] = ContextVar("autofill_sms_scope", default=None)
_PASS_BLOCKED: ContextVar[bool] = ContextVar("autofill_sms_pass_blocked", default=False)
# Set only for one account-page call, after the caller checked candidate identity.
_ACCOUNT_OK: ContextVar[bool] = ContextVar("autofill_sms_account_ok", default=False)


def canonical_sms_intent(value: str) -> str:
    """``SMS_CONSENT`` and ``RECRUITMENT_SMS_CONSENT`` are one intent.

    Any other string is returned unchanged, aside from surrounding space.
    """
    cleaned = (value or "").strip()
    if cleaned.upper() == SMS_INTENT_ALIAS or cleaned.upper() == SMS_INTENT:
        return SMS_INTENT
    return cleaned


def canonical_sms_key(value: str) -> str:
    """Return ``recruitment_sms_consent`` when ``value`` is that catalogue key.

    ``SMS_CONSENT`` and ``RECRUITMENT_SMS_CONSENT`` are accepted as the same key.
    Any other string returns empty, so a question sentence is not treated as the key.
    """
    token = (value or "").strip()
    folded = token.casefold().replace("-", "_").replace(" ", "_")
    if folded in _KEY_ALIASES or token.upper() in {SMS_INTENT, SMS_INTENT_ALIAS}:
        return SMS_KEY
    return ""


def canonical_sms_answer(value: str) -> str:
    """Store ``Yes`` or ``No``. Other wording is left unchanged."""
    if answer_authorizes_sms(value):
        return "Yes"
    if answer_declines_sms(value):
        return "No"
    return (value or "").strip()


def bind_sms_scope(profile: CandidateProfile | None, candidate_id: str = "") -> None:
    """Remember which candidate this fill may read SMS answers for.

    A profile whose ``candidateId`` differs from the active candidate is
    refused. An empty id on either side is refused, so an unbound profile
    cannot borrow another candidate's consent.
    """
    owner = (getattr(profile, "candidate_id", "") or "").strip()
    active = (candidate_id or "").strip() or owner
    refused = not owner or not active or owner != active
    _SCOPE.set(_Scope(active, refused))
    _PASS_BLOCKED.set(False)


def bind_account_sms(authorized: bool) -> None:
    """Account-page flag. The caller has already matched the candidate."""
    _ACCOUNT_OK.set(bool(authorized))


def clear_account_sms() -> None:
    _ACCOUNT_OK.set(False)


def begin_sms_pass() -> None:
    """One human stop per page pass. A later pass may stop again if still unresolved."""
    _PASS_BLOCKED.set(False)


def sms_blocked_this_pass() -> bool:
    return bool(_PASS_BLOCKED.get())


def note_sms_block() -> None:
    _PASS_BLOCKED.set(True)


def _normalize(text: str) -> str:
    from autofill.mapping import normalize

    return normalize(text or "")


def _question_text(control: Control) -> str:
    parts = (control.label, control.prompt, control.group, control.aria_label)
    return _normalize(" ".join(part for part in parts if part))


def _option_text(control: Control) -> str:
    parts = [option.label or "" for option in control.options]
    parts.extend(option.value or "" for option in control.options)
    return _normalize(" ".join(parts))


def is_recruitment_sms_text(text: str) -> bool:
    """True for a recruitment text-message consent question, not a mixed legal statement."""
    folded = _normalize(text)
    if not folded or not _SMS_WORD.search(folded):
        return False
    if not _PURPOSE.search(folded):
        return False
    if _UNRELATED.search(folded) or _SELF_DECLINE.search(folded):
        return False
    return True


def is_recruitment_sms_control(control: Control) -> bool:
    """The question is recruitment SMS. A decline option does not disqualify it."""
    question = _question_text(control)
    blob = _normalize(f"{question} {_option_text(control)}")
    if not _SMS_WORD.search(blob) or not _PURPOSE.search(blob):
        return False
    # Unrelated promises on the question, or on the combined statement, stay manual.
    if _UNRELATED.search(question) or _UNRELATED.search(blob):
        return False
    if question and _SELF_DECLINE.search(question):
        return False
    # The SMS words must be the question, not a stray option on another prompt.
    if question and not _SMS_WORD.search(question) and not _SMS_WORD.search(blob):
        return False
    if question and not _SMS_WORD.search(question):
        return False
    if not question and not _SMS_WORD.search(blob):
        return False
    return True


def _yes_no(value: str) -> str | None:
    from autofill.mapping import yes_no

    return yes_no(value)


def _stance(value: str) -> str:
    from autofill.mapping import option_stance

    return option_stance(value)


def answer_authorizes_sms(answer: str) -> bool:
    """Yes, I agree, I consent, or opt-in. A decline is not authorization."""
    text = (answer or "").strip()
    if not text:
        return False
    folded = _normalize(text)
    if _SELF_DECLINE.search(folded) or _stance(text) == "decline" or _yes_no(text) == "no":
        return False
    if _yes_no(text) == "yes" or _stance(text) == "agree":
        return True
    if folded in {"i agree", "agree", "opt in", "i opt in", "authorized", "i authorize"}:
        return True
    return folded.startswith("yes ")


def answer_declines_sms(answer: str) -> bool:
    text = (answer or "").strip()
    if not text:
        return False
    folded = _normalize(text)
    if _stance(text) == "decline" or _yes_no(text) == "no":
        return True
    return bool(_SELF_DECLINE.search(folded))


def is_sms_answer_row(item: ApplicationAnswer) -> bool:
    """True when this row is the candidate's recruitment-SMS answer."""
    key = canonical_sms_key(getattr(item, "key", "") or "")
    intent = canonical_sms_intent(getattr(item, "intent", "") or "")
    question = item.question or ""
    if key == SMS_KEY or canonical_sms_key(question) == SMS_KEY:
        return True
    if intent == SMS_INTENT:
        if (
            question.strip()
            and not is_recruitment_sms_text(question)
            and canonical_sms_key(question) != SMS_KEY
            and _UNRELATED.search(_normalize(question))
        ):
            return False
        return True
    return bool(is_recruitment_sms_text(question) and not intent)


def _row_vote(item: ApplicationAnswer) -> str | None:
    """``yes``, ``no``, or None when this row is not a recruitment-SMS answer."""
    if not is_sms_answer_row(item):
        return None
    if answer_declines_sms(item.answer):
        return "no"
    if answer_authorizes_sms(item.answer):
        return "yes"
    return None


def _state(profile: CandidateProfile) -> dict:
    raw = getattr(profile, "_sms_consent", None)
    if not isinstance(raw, dict):
        raw = {}
        setattr(profile, "_sms_consent", raw)
    return raw


def _ids_match(profile: CandidateProfile, candidate_id: str) -> bool:
    owner = (profile.candidate_id or "").strip()
    active = (candidate_id or "").strip()
    return bool(owner) and owner == active


def sms_resolution(profile: CandidateProfile | None, candidate_id: str = "") -> str:
    """``yes``, ``no``, or empty for this candidate's in-session choice."""
    if profile is None:
        return ""
    active = (candidate_id or "").strip() or _active_candidate()
    if not _ids_match(profile, active):
        return ""
    state = _state(profile)
    if (state.get("candidate_id") or "") != profile.candidate_id.strip():
        return ""
    resolved = str(state.get("resolved") or "")
    return resolved if resolved in {"yes", "no"} else ""


def _active_candidate() -> str:
    scope = _SCOPE.get()
    if scope is None:
        return ""
    return scope.candidate_id


def _scope_refused(candidate_id: str) -> bool:
    scope = _SCOPE.get()
    if scope is None or candidate_id:
        return False
    return bool(scope.refused)


def _candidate_ok(profile: CandidateProfile, candidate_id: str) -> bool:
    scope = _SCOPE.get()
    if scope is not None and scope.refused and not (candidate_id or "").strip():
        return False
    active = (candidate_id or "").strip() or (scope.candidate_id if scope is not None else "")
    if not active:
        active = (profile.candidate_id or "").strip()
    return _ids_match(profile, active)


def sms_stored_choice(profile: CandidateProfile | None, candidate_id: str = "") -> str:
    """``yes``, ``no``, or empty when this candidate has not answered.

    A stored ``No`` is resolved. It does not authorize auto-Yes and it is not
    asked again. Disagreeing rows are unresolved.
    """
    if profile is None or not _candidate_ok(profile, candidate_id):
        return ""
    scope = _SCOPE.get()
    active = (candidate_id or "").strip() or (scope.candidate_id if scope is not None else "")
    if not active:
        active = profile.candidate_id.strip()
    resolved = sms_resolution(profile, active)
    if resolved:
        return resolved
    votes = [vote for vote in (_row_vote(item) for item in profile.application_answers) if vote]
    if votes and all(vote == "yes" for vote in votes):
        return "yes"
    if votes and all(vote == "no" for vote in votes):
        return "no"
    return ""


def sms_authorized(profile: CandidateProfile | None, candidate_id: str = "") -> bool:
    """True only for this candidate's own ``Yes`` on ``recruitment_sms_consent``."""
    if _ACCOUNT_OK.get() and profile is None:
        return True
    return sms_stored_choice(profile, candidate_id) == "yes"


def _agree_option(control: Control) -> Option | None:
    agrees: list[Option] = []
    yeses: list[Option] = []
    for option in control.options:
        label = option.label or ""
        value = option.value or ""
        if _stance(label) == "agree" or _stance(value) == "agree":
            agrees.append(option)
            continue
        if _yes_no(label) == "yes" or _yes_no(value) == "yes":
            yeses.append(option)
    if len(agrees) == 1:
        return agrees[0]
    if len(yeses) == 1:
        return yeses[0]
    return None


def recruitment_sms_field(control: Control, profile: CandidateProfile | None) -> MappedField | None:
    """The Yes / opt-in action when this candidate authorized recruitment SMS.

    None leaves the existing manual path in place. This does not check the page.
    """
    if not is_recruitment_sms_control(control):
        return None
    if not sms_authorized(profile):
        return None
    if control.kind == "checkbox" and not control.options:
        if control.input_type in {"aria-checkbox", "switch"} or control.role == "switch":
            return None
        return MappedField(
            key=SMS_INTENT,
            action="check",
            text="Yes",
            field_class="APPROVED_QUESTION",
            confidence="high",
            reason="Application Answers authorize recruitment SMS.",
        )
    option = _agree_option(control)
    if option is None:
        # The menu is not on the page until the button is opened. Yes is still
        # the approved answer; the click verifies the button text afterwards.
        if control.kind == "buttons" and control.role == "listbox" and not control.options:
            return MappedField(
                key=SMS_INTENT,
                action="select",
                text="Yes",
                option_label="Yes",
                field_class="APPROVED_QUESTION",
                confidence="high",
                reason="Application Answers authorize recruitment SMS.",
            )
        return None
    return MappedField(
        key=SMS_INTENT,
        action="select",
        text="Yes",
        option_label=option.label or option.value,
        option_value=option.value,
        option_selector=option.selector,
        field_class="APPROVED_QUESTION",
        confidence="high",
        reason="Application Answers authorize recruitment SMS.",
    )


def accept_recruitment_sms(page, control: Control, profile: CandidateProfile | None) -> bool:
    """Select Yes when authorized. Records an audit row only after the page keeps it."""
    mapped = recruitment_sms_field(control, profile)
    if mapped is None or page is None:
        return False
    from autofill.fill import apply_mapped
    from autofill.mapping import record_auto_consent

    outcome = apply_mapped(page, control, mapped)
    if outcome.action not in {"check", "select"}:
        return False
    record_auto_consent(control, SMS_CATEGORY)
    return True


def skip_resolved_sms(control: Control, profile: CandidateProfile | None) -> bool:
    """True when this candidate's stored answer is No, so the field is not asked."""
    if not is_recruitment_sms_control(control):
        return False
    return sms_stored_choice(profile) == "no"


def _decline_option(control: Control) -> Option | None:
    declines: list[Option] = []
    nos: list[Option] = []
    for option in control.options:
        label = option.label or ""
        value = option.value or ""
        if _stance(label) == "decline" or _stance(value) == "decline":
            declines.append(option)
            continue
        if _yes_no(label) == "no" or _yes_no(value) == "no":
            nos.append(option)
    if len(declines) == 1:
        return declines[0]
    if len(nos) == 1:
        return nos[0]
    return None


def apply_stored_sms_no(page, control: Control, profile: CandidateProfile | None) -> bool:
    """Select No when this candidate already declined. A No is not an auto-accept.

    A checkbox stays unchecked. A Yes/No choice selects the decline option.
    Nothing is written to ``autoConsents``.
    """
    if page is None or sms_stored_choice(profile) != "no":
        return False
    if not is_recruitment_sms_control(control):
        return False
    option = _decline_option(control)
    if option is None:
        if control.kind == "checkbox":
            return True
        if not (control.kind == "buttons" and control.role == "listbox" and not control.options):
            return False
        option = Option(value="No", label="No", selector="")
    from autofill.fill import apply_mapped

    mapped = MappedField(
        key=SMS_INTENT,
        action="select",
        text="No",
        option_label=option.label or option.value,
        option_value=option.value,
        option_selector=option.selector,
        field_class="APPROVED_QUESTION",
        confidence="high",
        reason="Application Answers decline recruitment SMS.",
    )
    outcome = apply_mapped(page, control, mapped)
    return outcome.action == "select"


def collapse_sms_gaps(gaps: list[dict], *, skip_all: bool = False) -> list[dict]:
    """One recruitment-SMS stop per pass. Other consents are left as they are.

    ``skip_all`` is set when this candidate already declined in the session,
    so a later field is not asked again.
    """
    if skip_all:
        return [gap for gap in gaps if not _gap_is_sms(gap)]
    kept: list[dict] = []
    seen = sms_blocked_this_pass()
    for gap in gaps:
        if not _gap_is_sms(gap):
            kept.append(gap)
            continue
        if seen:
            continue
        seen = True
        note_sms_block()
        kept.append(gap)
    return kept


def _gap_is_sms(gap: dict) -> bool:
    text = str(gap.get("text") or "")
    intent = canonical_sms_intent(str(gap.get("intent") or ""))
    if text and is_recruitment_sms_text(text):
        return True
    if intent == SMS_INTENT and text and _UNRELATED.search(_normalize(text)):
        return False
    return intent == SMS_INTENT


def gap_intent(control: Control, classified: str | None) -> str | None:
    """Publish the SMS intent only for a pure recruitment-SMS question."""
    if is_recruitment_sms_control(control):
        return SMS_INTENT
    if classified == SMS_INTENT:
        return None
    return classified


def human_sms_choice(question: str, value: str, *, intent: str = "", kind: str = "") -> str:
    """``yes``, ``no``, or empty when this human answer is not recruitment SMS."""
    carried = canonical_sms_intent(intent)
    if carried != SMS_INTENT and not is_recruitment_sms_text(question):
        return ""
    if carried == SMS_INTENT and question.strip() and not is_recruitment_sms_text(question):
        if _UNRELATED.search(_normalize(question)):
            return ""
    folded = (value or "").strip().casefold()
    if kind == "CONSENT" and folded == "confirm":
        return "yes"
    if answer_authorizes_sms(value):
        return "yes"
    if answer_declines_sms(value):
        return "no"
    return ""


def remember_human_sms(
    profile: CandidateProfile | None,
    *,
    question: str,
    value: str,
    intent: str = "",
    kind: str = "",
    candidate_id: str = "",
) -> str:
    """Upsert ``recruitment_sms_consent`` as ``Yes`` or ``No`` for this candidate.

    Returns ``yes``, ``no``, or empty. A mismatched candidate id stores nothing.
    ``SMS_CONSENT`` is stored as ``RECRUITMENT_SMS_CONSENT``.
    """
    choice = human_sms_choice(question, value, intent=intent, kind=kind)
    if profile is None or not choice:
        return ""
    active = (candidate_id or "").strip() or (profile.candidate_id or "").strip()
    if not _ids_match(profile, active):
        return ""
    state = _state(profile)
    state["candidate_id"] = profile.candidate_id.strip()
    state["resolved"] = choice
    stem = " ".join((question or SMS_KEY).split())
    fresh = ApplicationAnswer(
        question=stem,
        answer="Yes" if choice == "yes" else "No",
        intent=SMS_INTENT,
        key=SMS_KEY,
        from_session=True,
    )
    profile.application_answers = [item for item in profile.application_answers if not is_sms_answer_row(item)]
    profile.application_answers.append(fresh)
    return choice


def learned_application_answers(profile: CandidateProfile | None, candidate_id: str = "") -> list[dict]:
    """Rows this session wrote, for the host to upsert on this candidate.

    ``key`` is ``recruitment_sms_consent``. ``intent`` is ``RECRUITMENT_SMS_CONSENT``.
    ``answer`` is ``Yes`` or ``No``.
    """
    if profile is None:
        return []
    active = (candidate_id or "").strip() or (profile.candidate_id or "").strip()
    if not _ids_match(profile, active):
        return []
    rows: list[dict] = []
    for item in profile.application_answers:
        if not item.from_session or not is_sms_answer_row(item):
            continue
        answer = canonical_sms_answer(item.answer)
        if answer not in {"Yes", "No"}:
            continue
        rows.append(
            {
                "key": SMS_KEY,
                "intent": SMS_INTENT,
                "question": item.question,
                "answer": answer,
                "candidateId": profile.candidate_id.strip(),
            }
        )
    return rows
