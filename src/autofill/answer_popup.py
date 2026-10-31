"""Save & Continue card on the employer tab (local helper path).

When a local fill stops on ``MANUAL_ANSWER_REQUIRED`` the helper shows one card
on the employer's application tab for every question already in that page set.
The card lists each prompt, any instructions printed next to the control, the
choices, and one **Save & Continue** button.

The card never talks to TileArc and never fills the employer field itself.
Clicking the button records every row on the page. The helper reads that on
the next status poll and reports it in ``question.popup``. The TileArc Apply
panel saves the answers and posts them to ``/v1/sessions/{id}/answer``. Later
posts for the same page set do not open another card. A question that appears
only after those writes gets one more card. Final Submit is not a control here.

The card is one shadow host (``#tilearc-answer-popup``) and one nonce for the
set. An answer recorded under another nonce is ignored. Extract skips the host
so its button is not the employer's Next or Save and Continue.
"""

from __future__ import annotations

import secrets
from typing import Any

POPUP_KEY = "__tilearcAnswerPopup"
HOST_ID = "tilearc-answer-popup"

_MAX_VALUES = 20
_MAX_VALUE_CHARS = 2000
_MAX_ROWS = 20

_SHOW_JS = r"""(args) => {
  const KEY = "__tilearcAnswerPopup";
  const HOST = "tilearc-answer-popup";
  const prior = window[KEY];
  let host = document.getElementById(HOST);
  const same = prior && prior.nonce === args.nonce && host && host.isConnected;
  if (same && prior.state === "answered" && !args.force) {
    return {shown: true, state: prior.state};
  }
  if (host) host.remove();
  const questions = Array.isArray(args.questions) ? args.questions.slice(0, 20) : [];
  const state = {
    nonce: args.nonce,
    questionId: questions.length ? questions[0].id : "",
    questionIds: questions.map((row) => row.id),
    state: "ask",
    values: null,
    at: 0,
  };
  Object.defineProperty(window, KEY, {value: state, configurable: true, writable: true, enumerable: false});
  host = document.createElement("div");
  host.id = HOST;
  host.setAttribute("data-tilearc-popup", "save-continue");
  host.style.cssText = "all:initial;position:fixed;right:16px;bottom:16px;z-index:2147483647;";
  const root = host.attachShadow({mode: "open"});
  const css = `
    .card{font:14px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#0f172a;background:#fff;
      border:1px solid #0d9488;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.25);width:340px;max-width:90vw;
      max-height:70vh;overflow:auto;padding:14px}
    .brand{font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#0f766e;font-weight:700}
    .who{font-size:12px;color:#475569;margin-top:2px}
    .row{margin-top:10px;padding-top:8px;border-top:1px solid #e2e8f0}
    .row:first-of-type{border-top:0;padding-top:0}
    .q{font-weight:600;margin:8px 0 4px}
    .help{font-size:12px;color:#475569;margin:0 0 8px;white-space:pre-wrap}
    .opt{display:flex;gap:8px;align-items:flex-start;margin:4px 0;cursor:pointer}
    textarea{width:100%;box-sizing:border-box;min-height:64px;border:1px solid #cbd5e1;
      border-radius:8px;padding:6px;font:inherit}
    button{margin-top:10px;width:100%;background:#0f766e;color:#fff;border:0;border-radius:8px;
      padding:8px 10px;font:inherit;font-weight:600;cursor:pointer}
    button[disabled]{opacity:.5;cursor:default}
    .msg{font-size:12px;margin-top:8px;color:#334155}
    .msg.err{color:#b91c1c}
    .note{font-size:11px;color:#64748b;margin-top:8px}`;
  const style = document.createElement("style");
  style.textContent = css;
  root.appendChild(style);
  const card = document.createElement("div");
  card.className = "card";
  card.setAttribute("role", "dialog");
  card.setAttribute("aria-label", "TileArc Auto-Fill needs an answer");
  const brand = document.createElement("div");
  brand.className = "brand";
  brand.textContent = "TileArc Auto-Fill needs your answer";
  card.appendChild(brand);
  if (args.candidate) {
    const who = document.createElement("div");
    who.className = "who";
    who.setAttribute("data-role", "candidate");
    who.textContent = "Answering for " + args.candidate;
    card.appendChild(who);
  }
  function helpFor(question) {
    if (question.help) return question.help;
    try {
      const el = question.selector ? document.querySelector(question.selector) : null;
      if (!el || el.closest("#" + HOST)) return "";
      const parts = [];
      const ids = (el.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
      for (const id of ids) {
        const node = document.getElementById(id);
        if (node && node.textContent) parts.push(node.textContent);
      }
      if (!parts.length) {
        const box = el.closest("fieldset, [role=group], [role=radiogroup], .field, .form-group, .question, li");
        if (box) {
          const hints = "small, .help, .hint, .description, [class*=help], [class*=hint], [class*=description], p";
          for (const node of box.querySelectorAll(hints)) {
            if (node.closest("label") || node.querySelector("input,select,textarea")) continue;
            if (node.textContent) parts.push(node.textContent);
            if (parts.length >= 3) break;
          }
        }
      }
      const text = parts.map((part) => part.replace(/\s+/g, " ").trim()).filter(Boolean)
        .filter((part) => part !== question.prompt).join("\n");
      return text.slice(0, 400);
    } catch (e) { return ""; }
  }
  const sections = [];
  for (const question of questions) {
    const section = document.createElement("div");
    section.className = "row";
    section.setAttribute("data-question-id", question.id);
    const q = document.createElement("div");
    q.className = "q";
    q.setAttribute("data-role", "question");
    q.textContent = question.prompt || "";
    section.appendChild(q);
    const helpText = helpFor(question);
    if (helpText) {
      const help = document.createElement("p");
      help.className = "help";
      help.setAttribute("data-role", "instructions");
      help.textContent = helpText;
      section.appendChild(help);
    }
    const restored = Array.isArray(question.restored) ? question.restored : [];
    if (!args.locked) {
      const multi = question.type === "multi_select";
      if (question.options && question.options.length) {
        const group = document.createElement("div");
        group.setAttribute("role", multi ? "group" : "radiogroup");
        for (const option of question.options) {
          const line = document.createElement("label");
          line.className = "opt";
          const input = document.createElement("input");
          input.type = multi ? "checkbox" : "radio";
          input.name = "tilearc-" + question.id;
          input.value = option;
          if (restored.indexOf(option) !== -1) input.checked = true;
          const span = document.createElement("span");
          span.textContent = option;
          line.appendChild(input);
          line.appendChild(span);
          group.appendChild(line);
        }
        section.appendChild(group);
      } else {
        const area = document.createElement("textarea");
        area.setAttribute("aria-label", question.prompt || "Answer");
        area.maxLength = 2000;
        if (restored.length) area.value = restored[0];
        section.appendChild(area);
      }
    }
    card.appendChild(section);
    sections.push(section);
  }
  const button = document.createElement("button");
  button.type = "button";
  button.setAttribute("data-role", "save-continue");
  button.textContent = "Save & Continue";
  card.appendChild(button);
  const msg = document.createElement("div");
  msg.className = "msg" + (args.error ? " err" : "");
  msg.setAttribute("data-role", "message");
  msg.setAttribute("aria-live", "polite");
  msg.textContent = args.message || "";
  card.appendChild(msg);
  const note = document.createElement("div");
  note.className = "note";
  note.textContent = "Saved to this candidate's Application Answers in TileArc, then filled here. "
    + "You still click the final Submit yourself.";
  card.appendChild(note);
  root.appendChild(card);
  function rowValues(section) {
    const area = section.querySelector("textarea");
    if (area) return area.value.trim() ? [area.value.trim()] : [];
    return Array.from(section.querySelectorAll("input:checked")).map((input) => input.value);
  }
  function allPicked() {
    if (args.locked || !sections.length) return false;
    return sections.every((section) => rowValues(section).length > 0);
  }
  function refresh() { button.disabled = args.locked || state.state !== "ask" || !allPicked(); }
  for (const section of sections) {
    for (const input of section.querySelectorAll("input")) input.addEventListener("change", refresh);
    const area = section.querySelector("textarea");
    if (area) area.addEventListener("input", refresh);
  }
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (args.locked || state.state !== "ask" || !allPicked()) return;
    state.values = sections.map((section) => ({
      questionId: section.getAttribute("data-question-id"),
      values: rowValues(section),
    }));
    state.state = "answered";
    state.at = Date.now();
    for (const input of root.querySelectorAll("input, textarea")) input.disabled = true;
    msg.className = "msg";
    msg.textContent = "Saving to TileArc…";
    refresh();
  });
  refresh();
  (document.documentElement || document.body).appendChild(host);
  return {shown: true, state: state.state};
}"""

_READ_JS = r"""(nonce) => {
  const state = window["__tilearcAnswerPopup"];
  const host = document.getElementById("tilearc-answer-popup");
  if (!state || state.nonce !== nonce) return {present: false};
  return {
    present: Boolean(host && host.isConnected),
    state: state.state,
    questionId: state.questionId,
    questionIds: Array.isArray(state.questionIds) ? state.questionIds.slice(0, 20) : [],
    values: state.state === "answered" && Array.isArray(state.values) ? state.values.slice(0, 20) : null,
  };
}"""

_MESSAGE_JS = r"""(args) => {
  const state = window["__tilearcAnswerPopup"];
  const host = document.getElementById("tilearc-answer-popup");
  if (!state || state.nonce !== args.nonce || !host || !host.shadowRoot) return false;
  const root = host.shadowRoot;
  const msg = root.querySelector("[data-role=message]");
  if (msg) {
    msg.className = "msg" + (args.error ? " err" : "");
    msg.textContent = args.message || "";
  }
  if (args.reopen) {
    state.state = "ask";
    state.values = null;
    for (const input of root.querySelectorAll("input, textarea")) input.disabled = false;
    const button = root.querySelector("[data-role=save-continue]");
    if (button) button.disabled = false;
  }
  return true;
}"""

_CLOSE_JS = r"""() => {
  const host = document.getElementById("tilearc-answer-popup");
  if (host) host.remove();
  try { delete window["__tilearcAnswerPopup"]; } catch (e) { window["__tilearcAnswerPopup"] = undefined; }
  return true;
}"""


def new_nonce() -> str:
    return secrets.token_hex(12)


def _rows_for_show(
    question: dict[str, Any],
    questions: list[dict[str, Any]] | None,
    selector: str,
    restored: dict[str, list[str]] | None,
) -> list[dict[str, Any]]:
    source = questions if questions else [question]
    rows: list[dict[str, Any]] = []
    for item in source[:_MAX_ROWS]:
        qid = str(item.get("id") or "")
        if not qid:
            continue
        picked = list((restored or {}).get(qid) or [])
        row_selector = str(item.get("selector") or "")
        if not row_selector and item is question:
            row_selector = selector
        rows.append(
            {
                "id": qid,
                "prompt": str(item.get("prompt") or ""),
                "type": str(item.get("type") or "text"),
                "options": [str(option) for option in item.get("options") or []][:40],
                "selector": row_selector,
                "help": str(item.get("help") or ""),
                "restored": [str(value) for value in picked][:_MAX_VALUES],
            }
        )
    return rows


def show_popup(
    page: Any,
    question: dict[str, Any],
    *,
    nonce: str,
    questions: list[dict[str, Any]] | None = None,
    selector: str = "",
    candidate: str = "",
    help_text: str = "",
    message: str = "",
    error: bool = False,
    force: bool = False,
    locked: bool = False,
    restored: dict[str, list[str]] | None = None,
) -> bool:
    """Show one card for ``questions`` (or the single ``question``). False when the page refused the script."""
    rows = _rows_for_show(question, questions, selector, restored)
    if help_text and rows and not rows[0].get("help"):
        rows[0]["help"] = help_text
    if not rows and question.get("prompt"):
        rows = _rows_for_show(question, [question], selector, restored)
    try:
        result = page.evaluate(
            _SHOW_JS,
            {
                "nonce": nonce,
                "questions": rows,
                "candidate": candidate,
                "message": message,
                "error": bool(error),
                "force": bool(force),
                "locked": bool(locked),
            },
        )
    except Exception:
        return False
    return bool(isinstance(result, dict) and result.get("shown"))


def _clean_text_list(values: list[Any]) -> list[str]:
    clean: list[str] = []
    for value in values[:_MAX_VALUES]:
        text = str(value).strip()
        if text:
            clean.append(text[:_MAX_VALUE_CHARS])
    return clean


def read_popup(page: Any, nonce: str) -> dict[str, Any]:
    """``{"present", "state", "questionId", "questionIds", "values"}`` for this nonce.

    ``values`` is present only after Save & Continue. A one-row card may be a
    list of strings. A page set is ``{"questionId", "values"}`` rows.
    """
    try:
        result = page.evaluate(_READ_JS, nonce)
    except Exception:
        return {"present": False}
    if not isinstance(result, dict):
        return {"present": False}
    values = result.get("values")
    parsed: list[Any] | None = None
    if isinstance(values, list):
        parsed = []
        for item in values[:_MAX_ROWS]:
            if isinstance(item, dict):
                qid = str(item.get("questionId") or "").strip()
                picked = _clean_text_list(list(item.get("values") or []))
                if qid and picked:
                    parsed.append({"questionId": qid, "values": picked})
            else:
                text = str(item).strip()
                if text:
                    parsed.append(text[:_MAX_VALUE_CHARS])
    ids = [str(item) for item in result.get("questionIds") or [] if str(item).strip()]
    return {
        "present": bool(result.get("present")),
        "state": str(result.get("state") or ""),
        "questionId": str(result.get("questionId") or ""),
        "questionIds": ids[:_MAX_ROWS],
        "values": parsed,
    }


def popup_message(page: Any, nonce: str, message: str, *, error: bool = False, reopen: bool = False) -> bool:
    try:
        return bool(page.evaluate(_MESSAGE_JS, {"nonce": nonce, "message": message, "error": error, "reopen": reopen}))
    except Exception:
        return False


def close_popup(page: Any) -> None:
    try:
        page.evaluate(_CLOSE_JS)
    except Exception:
        return
