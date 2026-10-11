"""Manual resume bytes for one session.

The bytes stay in memory or a per-session temp file. This module checks the
type and whether the ATS accepted the file. A filename on the page is not
enough.
"""

from __future__ import annotations

import os
import time

from autofill.extract import extract_page
from autofill.models import Control, PageSnapshot
from autofill.resume_fetch import sanitized_resume_name
from autofill.safeguards import attach_resume_control

_OLE = b"\xd0\xcf\x11\xe0"
_ZIP = b"PK\x03\x04"
_CONFIRM_SECONDS = 8.0

_CONFIRM_JS = r"""(selector) => {
  function shown(node) {
    if (!node || node.hidden) return false;
    const style = window.getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden") return false;
    return true;
  }
  function text(node) {
    return String(node.innerText || node.textContent || "").replace(/\s+/g, " ").trim();
  }
  const errors = [];
  const alerts = document.querySelectorAll("[role='alert'], .field-error, .error, [class*='toast'], [class*='Toast']");
  for (const alert of alerts) {
    if (!shown(alert)) continue;
    const value = text(alert);
    const folded = value.toLowerCase();
    if (!folded) continue;
    if (folded.indexOf("fail") !== -1 || folded.indexOf("error") !== -1 || folded.indexOf("rejected") !== -1) {
      errors.push(value.slice(0, 200));
    }
  }
  if (errors.length) return { state: "error", errors: errors.slice(0, 5) };
  const el = selector ? document.querySelector(selector) : null;
  let files = false;
  if (el && el.files && el.files.length > 0) files = true;
  let hidden = false;
  for (const input of document.querySelectorAll("input[type='hidden']")) {
    const name = ((input.name || "") + " " + (input.id || "")).toLowerCase();
    if (!/resume|cv|file|upload/.test(name)) continue;
    if (String(input.value || "").trim()) hidden = true;
  }
  let indicator = false;
  const marks = document.querySelectorAll(
    "[role='status'], [class*='chip'], [class*='Chip'], [data-automation-id='file-upload-item']"
  );
  for (const mark of marks) {
    if (!shown(mark)) continue;
    const id = mark.getAttribute("data-automation-id") || "";
    const cls = String(mark.className || "").toLowerCase();
    if (id === "file-upload-item" && cls.indexOf("error") !== -1) continue;
    const value = text(mark).toLowerCase();
    if (
      id === "file-upload-item" ||
      value.indexOf("uploaded") !== -1 ||
      value.indexOf("success") !== -1 ||
      value.indexOf("parsed") !== -1
    ) {
      indicator = true;
    }
  }
  if ((files || hidden) && indicator) return { state: "ok", errors: [] };
  return { state: "wait", errors: [] };
}"""


class ResumeRejected(Exception):
    """The upload cannot be stored. ``status_code`` is 413 or 415."""

    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code = status_code
        self.reason = reason


def max_resume_bytes() -> int:
    raw = os.environ.get("AUTOFILL_MAX_RESUME_BYTES", "").strip()
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return 10 * 1024 * 1024


def validate_resume(filename: str, data: bytes) -> str:
    """Return a sanitized basename, or reject the bytes."""
    if len(data) > max_resume_bytes():
        raise ResumeRejected(413, "too_large")
    name = sanitized_resume_name(filename)
    if not name:
        raise ResumeRejected(415, "unsupported_type")
    lowered = name.lower()
    if lowered.endswith(".pdf"):
        if not data.startswith(b"%PDF-"):
            raise ResumeRejected(415, "unsupported_type")
        return name
    if lowered.endswith(".doc"):
        if not data.startswith(_OLE):
            raise ResumeRejected(415, "unsupported_type")
        return name
    if lowered.endswith(".docx"):
        if not data.startswith(_ZIP):
            raise ResumeRejected(415, "unsupported_type")
        return name
    raise ResumeRejected(415, "unsupported_type")


def mime_for(name: str) -> str:
    lowered = name.lower()
    if lowered.endswith(".pdf"):
        return "application/pdf"
    if lowered.endswith(".docx"):
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if lowered.endswith(".doc"):
        return "application/msword"
    return "application/octet-stream"


def resume_control(snapshot: PageSnapshot, hint: str = "") -> Control | None:
    files = [control for control in snapshot.controls if control.kind == "file" and not control.hidden]
    folded_hint = hint.casefold().strip()
    if folded_hint:
        for control in files:
            blob = f"{control.label} {control.name} {control.element_id}".casefold()
            if folded_hint in blob:
                return control
    for control in files:
        blob = f"{control.label} {control.name} {control.element_id}".casefold()
        if "resume" in blob or blob.strip() == "cv" or " cv" in f" {blob}":
            return control
    if len(files) == 1:
        return files[0]
    return None


def _has_file(page, selector: str) -> bool:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None or not selector:
        return False
    try:
        script = (
            "(selector) => { const el = document.querySelector(selector);"
            " return !!(el && el.files && el.files.length); }"
        )
        return bool(evaluate(script, selector))
    except Exception:
        return False


def attach_and_confirm(page, *, name: str, data: bytes, hint: str = "") -> dict:
    """Attach bytes, then require an ATS indicator and no upload error."""
    snapshot = extract_page(page)
    control = resume_control(snapshot, hint)
    if control is None or not control.selector:
        return {"verified": False, "reason": "no_upload_control", "siteErrors": []}
    try:
        attach_resume_control(
            page,
            control.selector,
            [{"name": name, "mimeType": mime_for(name), "buffer": data}],
        )
    except Exception:
        return {"verified": False, "reason": "no_upload_control", "siteErrors": []}
    return _confirm(page, control.selector)


def confirm_current(page, hint: str = "") -> dict:
    """Re-check a resume already on the page. A name alone does not pass."""
    snapshot = extract_page(page)
    control = resume_control(snapshot, hint)
    if control is None or not control.selector:
        return {"verified": False, "reason": "no_upload_control", "siteErrors": []}
    return _confirm(page, control.selector)


def _confirm(page, selector: str) -> dict:
    deadline = time.monotonic() + _CONFIRM_SECONDS
    last = {"state": "wait", "errors": []}
    while True:
        try:
            last = page.evaluate(_CONFIRM_JS, selector) or last
        except Exception:
            return {"verified": False, "reason": "not_confirmed", "siteErrors": []}
        state = str(last.get("state") or "wait")
        errors = [str(item)[:200] for item in last.get("errors") or [] if str(item).strip()][:5]
        if state == "error":
            return {"verified": False, "reason": "ats_rejected", "siteErrors": errors}
        if state == "ok":
            return {"verified": True, "reason": "", "siteErrors": []}
        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    return {"verified": False, "reason": "not_confirmed", "siteErrors": []}


def reattach_verified(page, name: str, mime: str, data: bytes) -> None:
    """Put the same bytes on an empty resume control. A file already there stays."""
    if not name or not data:
        return
    try:
        snapshot = extract_page(page)
    except Exception:
        return
    control = resume_control(snapshot)
    if control is None or not control.selector or _has_file(page, control.selector):
        return
    try:
        attach_resume_control(
            page,
            control.selector,
            [{"name": name, "mimeType": mime or mime_for(name), "buffer": data}],
        )
    except Exception:
        return
