"""Save & Continue popup on the employer tab (local helper path, t60u Req3).

When a local fill stops on ``MANUAL_ANSWER_REQUIRED`` the helper shows a small
popup on the employer's application tab: the exact employer question, any
instructions printed next to the control, the available choices, and one
**Save & Continue** button.

The popup never talks to TileArc and never fills the employer field itself.
Clicking the button only records the employee's choice on the page. The
helper reads it on the next status poll and reports it in ``question.popup``.
The TileArc Apply panel (signed-in employee, frozen session) then saves it to
the candidate's Application Answers, verifies the save, and posts it back to
``/v1/sessions/{id}/answer``. Only then does the helper close the popup, fill
the field, verify it, and continue the same application.

The popup is bound to one helper session and one question by a random nonce.
An answer recorded under another nonce or question id is ignored. The popup
is removed before the engine writes or navigates, so the engine never sees
its button. Final Submit is not a control here.
"""

from __future__ import annotations

import secrets
from typing import Any

POPUP_KEY = "__tilearcAnswerPopup"
HOST_ID = "tilearc-answer-popup"

_MAX_VALUES = 20
_MAX_VALUE_CHARS = 2000

_SHOW_JS = r"""(args) => {
  const KEY = "__tilearcAnswerPopup";
  const HOST = "tilearc-answer-popup";
  const prior = window[KEY];
  let host = document.getElementById(HOST);
  const sameQuestion = prior && prior.nonce === args.nonce && host && host.isConnected;
  if (sameQuestion && prior.state === "answered" && !args.force) {
    return {shown: true, state: prior.state};
  }
  if (host) host.remove();
  const state = {nonce: args.nonce, questionId: args.questionId, state: "ask", values: null, at: 0};
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
    .q{font-weight:600;margin:8px 0 4px}
    .help{font-size:12px;color:#475569;margin:0 0 8px;white-space:pre-wrap}
    .opt{display:flex;gap:8px;align-items:flex-start;margin:4px 0;cursor:pointer}
    textarea{width:100%;box-sizing:border-box;min-height:64px;border:1px solid #cbd5e1;border-radius:8px;padding:6px;font:inherit}
    button{margin-top:10px;width:100%;background:#0f766e;color:#fff;border:0;border-radius:8px;padding:8px 10px;font:inherit;font-weight:600;cursor:pointer}
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
  const q = document.createElement("div");
  q.className = "q";
  q.setAttribute("data-role", "question");
  q.textContent = args.prompt;
  card.appendChild(q);
  const helpText = (() => {
    if (args.help) return args.help;
    try {
      const el = args.selector ? document.querySelector(args.selector) : null;
      if (!el) return "";
      const parts = [];
      const ids = (el.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
      for (const id of ids) {
        const node = document.getElementById(id);
        if (node && node.textContent) parts.push(node.textContent);
      }
      if (!parts.length) {
        const box = el.closest("fieldset, [role=group], [role=radiogroup], .field, .form-group, .question, li");
        if (box) {
          for (const node of box.querySelectorAll("small, .help, .hint, .description, [class*=help], [class*=hint], [class*=description], p")) {
            if (node.closest("label") || node.querySelector("input,select,textarea")) continue;
            if (node.textContent) parts.push(node.textContent);
            if (parts.length >= 3) break;
          }
        }
      }
      const text = parts.map((part) => part.replace(/\s+/g, " ").trim()).filter(Boolean)
        .filter((part) => part !== args.prompt).join("\n");
      return text.slice(0, 400);
    } catch (e) { return ""; }
  })();
  if (helpText) {
    const help = document.createElement("p");
    help.className = "help";
    help.setAttribute("data-role", "instructions");
    help.textContent = helpText;
    card.appendChild(help);
  }
  const multi = args.type === "multi_select";
  const inputs = [];
  let area = null;
  if (args.options && args.options.length) {
    const group = document.createElement("div");
    group.setAttribute("role", multi ? "group" : "radiogroup");
    for (const option of args.options) {
      const row = document.createElement("label");
      row.className = "opt";
      const input = document.createElement("input");
      input.type = multi ? "checkbox" : "radio";
      input.name = "tilearc-choice";
      input.value = option;
      inputs.push(input);
      const span = document.createElement("span");
      span.textContent = option;
      row.appendChild(input);
      row.appendChild(span);
      group.appendChild(row);
    }
    card.appendChild(group);
  } else {
    area = document.createElement("textarea");
    area.setAttribute("aria-label", args.prompt);
    area.maxLength = 2000;
    card.appendChild(area);
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
  note.textContent = "Saved to this candidate's Application Answers in TileArc, then filled here. You still click the final Submit yourself.";
  card.appendChild(note);
  root.appendChild(card);
  function picked() {
    if (area) return area.value.trim() ? [area.value.trim()] : [];
    return inputs.filter((input) => input.checked).map((input) => input.value);
  }
  function refresh() { button.disabled = state.state !== "ask" || picked().length === 0; }
  for (const input of inputs) input.addEventListener("change", refresh);
  if (area) area.addEventListener("input", refresh);
  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    const values = picked();
    if (!values.length || state.state !== "ask") return;
    state.values = values;
    state.state = "answered";
    state.at = Date.now();
    for (const input of inputs) input.disabled = true;
    if (area) area.disabled = true;
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


def show_popup(
    page: Any,
    question: dict[str, Any],
    *,
    nonce: str,
    selector: str = "",
    candidate: str = "",
    help_text: str = "",
    message: str = "",
    error: bool = False,
    force: bool = False,
) -> bool:
    """Show (or keep) the popup for ``question``. False when the page refused the script."""
    try:
        result = page.evaluate(
            _SHOW_JS,
            {
                "nonce": nonce,
                "questionId": str(question.get("id") or ""),
                "prompt": str(question.get("prompt") or ""),
                "type": str(question.get("type") or "text"),
                "options": [str(option) for option in question.get("options") or []][:40],
                "selector": selector,
                "candidate": candidate,
                "help": help_text,
                "message": message,
                "error": bool(error),
                "force": bool(force),
            },
        )
    except Exception:
        return False
    return bool(isinstance(result, dict) and result.get("shown"))


def read_popup(page: Any, nonce: str) -> dict[str, Any]:
    """``{"present", "state", "questionId", "values"}`` for this nonce. Values only once answered."""
    try:
        result = page.evaluate(_READ_JS, nonce)
    except Exception:
        return {"present": False}
    if not isinstance(result, dict):
        return {"present": False}
    values = result.get("values")
    clean: list[str] | None = None
    if isinstance(values, list):
        clean = []
        for value in values[:_MAX_VALUES]:
            text = str(value).strip()
            if text:
                clean.append(text[:_MAX_VALUE_CHARS])
    return {
        "present": bool(result.get("present")),
        "state": str(result.get("state") or ""),
        "questionId": str(result.get("questionId") or ""),
        "values": clean,
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
