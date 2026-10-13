"""Manual resume bytes for one session.

The bytes stay in memory or a per-session temp file. This module checks the
type and whether the ATS accepted the file. A filename on the page is not
enough.
"""

from __future__ import annotations

import logging
import os
import time

from autofill.extract import extract_page
from autofill.models import Control, PageSnapshot
from autofill.resume_fetch import sanitized_resume_name
from autofill.safeguards import attach_resume_control

logger = logging.getLogger("autofill.resume_manual")

_OLE = b"\xd0\xcf\x11\xe0"
_ZIP = b"PK\x03\x04"
_CONFIRM_SECONDS = 8.0

_CONFIRM_JS = r"""({ selector, filename }) => {
  function shown(node) {
    if (!node || node.nodeType !== 1 || node.hidden) return false;
    const style = window.getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden") return false;
    return true;
  }
  function text(node) {
    return String((node && (node.innerText || node.textContent)) || "").replace(/\s+/g, " ").trim();
  }
  function cls(node) {
    return String((node && node.className) || "").toLowerCase();
  }
  const errors = [];
  const alerts = document.querySelectorAll(
    "[role='alert'], .field-error, .error, [class*='toast'], [class*='Toast']"
  );
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
  let liveName = "";
  if (el && el.files && el.files.length > 0) {
    files = true;
    liveName = String(el.files[0].name || "");
  }
  let hidden = false;
  for (const input of document.querySelectorAll("input[type='hidden']")) {
    const name = ((input.name || "") + " " + (input.id || "")).toLowerCase();
    if (!/resume|cv|file|upload/.test(name)) continue;
    if (String(input.value || "").trim()) hidden = true;
  }
  const wanted = String(filename || liveName || "").trim().toLowerCase();
  const regions = [];
  function addRegion(node) {
    if (!node || node === document.body || node === document.documentElement) return;
    if (regions.indexOf(node) !== -1) return;
    regions.push(node);
  }
  let node = el;
  for (let depth = 0; node && node !== document.body && depth < 4; depth += 1, node = node.parentElement) {
    addRegion(node);
  }
  if (el && el.id) {
    const lab = document.querySelector("label[for='" + CSS.escape(el.id) + "']");
    addRegion(lab);
    if (lab) addRegion(lab.parentElement);
  }
  for (const lab of document.querySelectorAll("label, legend, h2, h3")) {
    const blob = text(lab).toLowerCase();
    if (!/resume|\bcv\b|curriculum vitae/.test(blob)) continue;
    addRegion(lab);
    addRegion(lab.parentElement);
    if (lab.parentElement) addRegion(lab.parentElement.parentElement);
  }
  function inRegion(target) {
    if (!target) return false;
    if (!regions.length) return false;
    for (const region of regions) {
      if (region === target || (region.contains && region.contains(target))) return true;
    }
    return false;
  }
  const regionBlob = regions.map((region) => text(region)).join(" ").toLowerCase();
  const filenameShown = !!(wanted && regionBlob.indexOf(wanted) !== -1);
  function buttonText(button) {
    return (
      text(button) + " " + (button.getAttribute("aria-label") || "") + " " + (button.getAttribute("title") || "")
    ).toLowerCase();
  }
  function hasControl(root, words) {
    if (!root || !root.querySelectorAll) return false;
    for (const button of root.querySelectorAll("button, a, [role='button']")) {
      const value = buttonText(button).trim();
      for (const word of words) {
        if (word.length === 1 ? value === word : value.indexOf(word) !== -1) return true;
      }
    }
    return false;
  }
  let ats = false;
  for (const mark of document.querySelectorAll(".resume-upload-success")) {
    if (shown(mark) && text(mark).toLowerCase().indexOf("success") !== -1) ats = true;
  }
  for (const mark of document.querySelectorAll("[data-automation-id='file-upload-item']")) {
    if (!shown(mark) || cls(mark).indexOf("error") !== -1) continue;
    ats = true;
  }
  const chips = document.querySelectorAll(
    "[class*='chip'], [class*='Chip'], [class*='filename'], [class*='file-name'], [class*='file-upload']"
  );
  for (const chip of chips) {
    if (!shown(chip) || cls(chip).indexOf("error") !== -1) continue;
    if (regions.length && !inRegion(chip)) continue;
    const host = chip.parentElement || chip;
    if (hasControl(chip, ["remove", "delete", "×", "x"]) || hasControl(host, ["remove", "delete"])) ats = true;
  }
  for (const region of regions) {
    const low = text(region).toLowerCase();
    const fileRow = low.indexOf("replace") !== -1 || (filenameShown && low.indexOf("remove") !== -1);
    if (fileRow && hasControl(region, ["replace", "remove"])) ats = true;
  }
  let generic = false;
  for (const mark of document.querySelectorAll("[role='status']")) {
    if (!shown(mark)) continue;
    const value = text(mark).toLowerCase();
    if (value.indexOf("uploaded") !== -1 || value.indexOf("success") !== -1 || value.indexOf("parsed") !== -1) {
      generic = true;
    }
  }
  const replaced = !files && ats;
  const partA = filenameShown || ats || generic;
  const partC = files || hidden || replaced || ats;
  if (partA && partC) return { state: "ok", errors: [] };
  if (!document.querySelector("input[type='file']") && !ats && !generic) {
    return { state: "absent", errors: [] };
  }
  return { state: "wait", errors: [] };
}"""

_FIND_FILE_JS = r"""(hint) => {
  function selectorFor(el) {
    if (el.id) return "#" + CSS.escape(el.id);
    if (el.getAttribute("name")) {
      return "input[type='file'][name='" + CSS.escape(el.getAttribute("name")) + "']";
    }
    return "";
  }
  function blob(el) {
    let label = el.getAttribute("aria-label") || "";
    if (el.id) {
      const lab = document.querySelector("label[for='" + CSS.escape(el.id) + "']");
      if (lab) label += " " + (lab.innerText || "");
    }
    const parent = el.closest ? el.closest("label") : null;
    if (parent) label += " " + (parent.innerText || "");
    return (label + " " + (el.name || "") + " " + (el.id || "")).toLowerCase();
  }
  const inputs = Array.from(document.querySelectorAll("input[type='file']"));
  const folded = String(hint || "").trim().toLowerCase();
  if (folded) {
    for (const el of inputs) {
      if (blob(el).indexOf(folded) !== -1 && selectorFor(el)) return selectorFor(el);
    }
  }
  const resume = inputs.filter((el) => /resume|\bcv\b|curriculum/.test(blob(el)));
  if (resume.length && selectorFor(resume[0])) return selectorFor(resume[0]);
  if (inputs.length === 1) return selectorFor(inputs[0]);
  return "";
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


def _resume_blob(control: Control) -> str:
    return f"{control.label} {control.name} {control.element_id}".casefold()


def _match_resume(controls: list[Control], hint: str) -> Control | None:
    folded_hint = hint.casefold().strip()
    if folded_hint:
        for control in controls:
            if folded_hint in _resume_blob(control):
                return control
    for control in controls:
        blob = _resume_blob(control)
        if "resume" in blob or blob.strip() == "cv" or " cv" in f" {blob}":
            return control
    return None


def resume_control(snapshot: PageSnapshot, hint: str = "") -> Control | None:
    """The resume file input, including one the ATS hid after the first attach."""
    files = [control for control in snapshot.controls if control.kind == "file"]
    visible = [control for control in files if not control.hidden]
    hidden = [control for control in files if control.hidden]
    found = _match_resume(visible, hint) or _match_resume(hidden, hint)
    if found is not None:
        return found
    if len(visible) == 1:
        return visible[0]
    if not visible and len(hidden) == 1:
        return hidden[0]
    return None


def _selector_from_page(page, hint: str) -> str:
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None:
        return ""
    try:
        found = evaluate(_FIND_FILE_JS, hint)
    except Exception:
        return ""
    return str(found or "")


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


def _resume_selector(page, hint: str) -> str:
    try:
        snapshot = extract_page(page)
    except Exception:
        snapshot = None
    if snapshot is not None:
        control = resume_control(snapshot, hint)
        if control is not None and control.selector:
            return control.selector
    return _selector_from_page(page, hint)


def attach_and_confirm(page, *, name: str, data: bytes, hint: str = "") -> dict:
    """Attach bytes, then require an attached indicator and no upload error."""
    started = time.perf_counter()
    selector = _resume_selector(page, hint)
    if not selector:
        return {"verified": False, "reason": "no_upload_control", "siteErrors": []}
    attach_started = time.perf_counter()
    try:
        attach_resume_control(
            page,
            selector,
            [{"name": name, "mimeType": mime_for(name), "buffer": data}],
        )
    except Exception:
        return {"verified": False, "reason": "no_upload_control", "siteErrors": []}
    attach_ms = int((time.perf_counter() - attach_started) * 1000)
    report = _confirm(page, selector, name)
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    state = "ok" if report.get("verified") else str(report.get("reason") or "wait")
    logger.info(
        "action=resume_confirm elapsed_ms=%s attach_ms=%s state=%s",
        elapsed_ms,
        attach_ms,
        state,
    )
    return report


def confirm_current(page, hint: str = "", filename: str = "") -> dict:
    """Re-check a resume already on the page. A name alone does not pass."""
    return _confirm(page, _resume_selector(page, hint), filename)


def _confirm_once(page, selector: str, filename: str) -> dict | None:
    try:
        return page.evaluate(_CONFIRM_JS, {"selector": selector, "filename": filename}) or {}
    except Exception:
        return None


def _confirm(page, selector: str, filename: str = "") -> dict:
    deadline = time.monotonic() + _CONFIRM_SECONDS
    last = {"state": "wait", "errors": []}
    while True:
        found = _confirm_once(page, selector, filename)
        if found is None:
            return {"verified": False, "reason": "not_confirmed", "siteErrors": []}
        last = found or last
        state = str(last.get("state") or "wait")
        errors = [str(item)[:200] for item in last.get("errors") or [] if str(item).strip()][:5]
        if state == "error":
            return {"verified": False, "reason": "ats_rejected", "siteErrors": errors}
        if state == "ok":
            return {"verified": True, "reason": "", "siteErrors": []}
        if state == "absent":
            return {"verified": False, "reason": "no_upload_control", "siteErrors": []}
        if time.monotonic() >= deadline:
            break
        time.sleep(0.1)
    return {"verified": False, "reason": "not_confirmed", "siteErrors": []}


_ACCEPTED_JS = r"""(filename) => {
  const wanted = String(filename || "").trim().toLowerCase();
  if (!wanted) return false;
  const body = String((document.body && (document.body.innerText || document.body.textContent)) || "").toLowerCase();
  if (body.indexOf(wanted) === -1) return false;
  for (const input of document.querySelectorAll("input[type='file']")) {
    const files = input.files ? Array.from(input.files) : [];
    if (files.some((file) => String(file.name || "").toLowerCase() === wanted)) return true;
  }
  function shown(node) {
    if (!node || node.nodeType !== 1 || node.hidden) return false;
    const style = window.getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden") return false;
    return true;
  }
  const marks = document.querySelectorAll(
    "[class*='chip'], [class*='Chip'], [class*='filename'], [class*='file-name'], [class*='file-upload'], " +
      ".resume-upload-success, [data-automation-id='file-upload-item']"
  );
  for (const mark of marks) {
    if (!shown(mark)) continue;
    const text = String(mark.innerText || mark.textContent || "").toLowerCase();
    if (text.indexOf(wanted) !== -1) return true;
    let host = mark.parentElement;
    for (let depth = 0; host && depth < 3; depth += 1, host = host.parentElement) {
      const around = String(host.innerText || host.textContent || "").toLowerCase();
      if (around.indexOf(wanted) !== -1) return true;
    }
  }
  return false;
}"""


def _filename_accepted(page, filename: str) -> bool:
    """True when the resume name is already on a chip, even on a replacement input."""
    evaluate = getattr(page, "evaluate", None)
    if evaluate is None or not filename:
        return False
    try:
        return bool(evaluate(_ACCEPTED_JS, filename))
    except Exception:
        return False


def reattach_verified(page, name: str, mime: str, data: bytes, session_id: str = "") -> None:
    """Put the same bytes on an empty resume control. A file already there stays.

    Greenhouse drops the original file input and leaves the accepted name on a
    chip beside a new empty input. Uploading again starts a second parse.
    """
    if not name or not data:
        return
    try:
        snapshot = extract_page(page)
    except Exception:
        return
    control = resume_control(snapshot)
    if control is None or not control.selector or _has_file(page, control.selector):
        return
    once = _confirm_once(page, control.selector, name)
    state = str(once.get("state") or "") if isinstance(once, dict) else ""
    if state == "ok" or _filename_accepted(page, name):
        logger.info("action=resume_reattach result=skipped")
        return
    if state == "error":
        logger.info("action=resume_reattach result=rejected")
        return
    from autofill.stepfill import post_upload_refill

    try:
        attach_resume_control(
            page,
            control.selector,
            [{"name": name, "mimeType": mime or mime_for(name), "buffer": data}],
        )
    except Exception:
        return
    post_upload_refill(page, session_id=session_id)
    logger.info("action=resume_reattach result=uploaded")
