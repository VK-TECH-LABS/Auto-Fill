"""Read form controls out of a Playwright page, including open shadow roots.

Workday and several other ATS sites place inputs inside open shadow trees.
``querySelector`` on the document does not see those nodes, so the extractor
walks every open shadow root. Playwright locators still pierce open shadow
roots, which is why an ``#id`` selector remains usable after extraction.
"""

from __future__ import annotations

from autofill.models import ButtonControl, Control, Option, PageSnapshot

EXTRACT_JS = r"""
() => {
  function isHidden(el) {
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (type === "hidden") return true;
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      if (node.hidden) return true;
      const style = window.getComputedStyle(node);
      if (style.display === "none" || style.visibility === "hidden") return true;
      if (node.getAttribute("aria-hidden") === "true") return true;
    }
    return false;
  }

  function textOf(node) {
    if (!node) return "";
    return (node.innerText || node.textContent || "").replace(/\s+/g, " ").trim();
  }

  function byId(root, id) {
    if (!id) return null;
    if (typeof root.getElementById === "function") return root.getElementById(id);
    return root.querySelector("#" + CSS.escape(id));
  }

  function legendText(el) {
    const fieldset = el.closest("fieldset");
    if (!fieldset) return "";
    const legend = fieldset.querySelector("legend");
    return legend ? textOf(legend) : "";
  }

  function checkboxPrompt(el) {
    const legend = legendText(el);
    if (legend) return legend.replace(/[✱*＊]+$/, "").trim();
    const stop = el.closest("form") || document.body;
    let node = el.parentElement;
    for (let depth = 0; node && node !== stop && depth < 5; depth += 1, node = node.parentElement) {
      for (const child of Array.from(node.children)) {
        if (child.contains(el)) break;
        if (child.querySelector && child.querySelector("input, select, textarea, button")) continue;
        const text = textOf(child).replace(/[✱*＊]+$/, "").trim();
        if (text && text.length <= 200) return text;
      }
    }
    return "";
  }

  function labelFor(root, el) {
    const bits = [];
    if (el.id) {
      const explicit = root.querySelector("label[for='" + CSS.escape(el.id) + "']");
      if (explicit) bits.push(textOf(explicit));
    }
    const wrapping = el.closest("label");
    if (wrapping) bits.push(textOf(wrapping));
    const labelledby = el.getAttribute("aria-labelledby");
    if (labelledby) {
      for (const id of labelledby.split(/\s+/)) {
        const node = byId(root, id);
        if (node) bits.push(textOf(node));
      }
    }
    const aria = el.getAttribute("aria-label");
    if (aria) bits.push(aria.trim());
    const fieldset = el.closest("fieldset");
    if (fieldset) {
      const legend = fieldset.querySelector("legend");
      if (legend) bits.push(textOf(legend));
    }
    if (!bits.length) {
      let node = el;
      for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
        const prev = node.previousElementSibling;
        if (!prev || !prev.tagName) continue;
        const tag = prev.tagName.toLowerCase();
        if (tag === "label" || tag === "legend" || /^h[1-6]$/.test(tag)) {
          const text = textOf(prev);
          if (text) {
            bits.push(text);
            break;
          }
        }
      }
    }
    return bits.join(" ").replace(/\s+/g, " ").trim();
  }

  function selectorFor(el) {
    if (el.id) return "#" + CSS.escape(el.id);
    const name = el.getAttribute("name");
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute("type") || "").toLowerCase();
    if (name && (type === "radio" || type === "checkbox")) {
      return tag + "[type='" + type + "'][name='" + CSS.escape(name) + "'][value='" + CSS.escape(el.value || "") + "']";
    }
    if (name) return tag + "[name='" + CSS.escape(name) + "']";
    const automation = el.getAttribute("data-automation-id");
    if (automation) return "[data-automation-id='" + CSS.escape(automation) + "']";
    // Unnamed submit buttons still need a selector so they can be reported
    // and so the fill routine can refuse them. The attribute is not a secret.
    if (!el.getAttribute("data-autofill-target")) {
      el.setAttribute("data-autofill-target", Math.random().toString(36).slice(2));
    }
    return "[data-autofill-target='" + el.getAttribute("data-autofill-target") + "']";
  }

  function eachRoot(root, visit) {
    visit(root);
    for (const el of root.querySelectorAll("*")) {
      if (el.shadowRoot) eachRoot(el.shadowRoot, visit);
    }
  }

  const controls = [];
  const seenRadios = new Set();
  const buttons = [];
  const seenButtons = new Set();

  eachRoot(document, (root) => {
    const rootKey = root.host ? (root.host.id || "shadow") : "document";
    for (const el of root.querySelectorAll("input, select, textarea")) {
      const tag = el.tagName.toLowerCase();
      const inputType = tag === "textarea"
        ? "textarea"
        : tag === "select"
          ? "select"
          : (el.getAttribute("type") || "text").toLowerCase();
      if (["submit", "button", "reset", "image"].includes(inputType)) continue;

      if (inputType === "radio") {
        const name = el.getAttribute("name") || "";
        const groupKey = rootKey + "::" + name;
        if (!name || seenRadios.has(groupKey)) continue;
        seenRadios.add(groupKey);
        const group = Array.from(root.querySelectorAll("input[type='radio'][name='" + CSS.escape(name) + "']"));
        const fieldset = el.closest("fieldset");
        const legend = fieldset ? fieldset.querySelector("legend") : null;
        const options = group.map((radio) => ({
          value: radio.value || "",
          label: textOf(radio.closest("label")) || radio.value || "",
          selector: selectorFor(radio),
        })).filter((option) => option.selector);
        if (!options.length) continue;
        controls.push({
          kind: "radio",
          name,
          elementId: el.id || "",
          label: textOf(legend) || labelFor(root, el),
          placeholder: "",
          ariaLabel: el.getAttribute("aria-label") || "",
          autocomplete: "",
          required: group.some((radio) => radio.required),
          hidden: group.every((radio) => isHidden(radio)),
          disabled: group.every((radio) => radio.disabled),
          readOnly: false,
          options,
          selector: options[0].selector,
          inputMode: "",
          inputType: "radio",
          role: "radio",
          nearby: "",
          group: textOf(legend),
          prompt: "",
        });
        continue;
      }

      const selector = selectorFor(el);
      if (!selector) continue;
      const role = (el.getAttribute("role") || "").toLowerCase();
      let kind = "text";
      if (inputType === "checkbox") kind = "checkbox";
      else if (inputType === "file") kind = "file";
      else if (inputType === "password") kind = "password";
      else if (tag === "select") kind = "select";
      else if (tag === "textarea") kind = "textarea";
      else if (role === "combobox") kind = "combobox";
      const classToken = el.getAttribute("class") || "";
      if (classToken.indexOf("select__input") !== -1 || (el.closest && el.closest(".select__control"))) {
        kind = "combobox";
      }

      let options = [];
      if (tag === "select") {
        options = Array.from(el.options).map((option) => ({
          value: option.value,
          label: (option.textContent || "").replace(/\s+/g, " ").trim(),
          selector: "",
        })).filter((option) => option.label || option.value);
      } else if (kind === "combobox") {
        const listId = el.getAttribute("aria-controls") || el.getAttribute("aria-owns") || "";
        const list = listId ? byId(root, listId) : null;
        if (list && (list.getAttribute("role") || "") === "listbox") {
          options = Array.from(list.querySelectorAll("[role='option']")).map((option) => ({
            value: option.getAttribute("data-value") || textOf(option),
            label: textOf(option),
            selector: selectorFor(option),
          })).filter((option) => option.label && option.selector);
        }
      }
      const block = el.closest("section, fieldset") || el.parentElement;
      const headingNode = block ? block.querySelector("h1, h2, h3, legend") : null;

      controls.push({
        kind,
        name: el.getAttribute("name") || "",
        elementId: el.id || "",
        label: labelFor(root, el),
        placeholder: el.getAttribute("placeholder") || "",
        ariaLabel: el.getAttribute("aria-label") || "",
        autocomplete: el.getAttribute("autocomplete") || "",
        required: !!el.required,
        hidden: isHidden(el),
        disabled: !!el.disabled,
        readOnly: !!el.readOnly,
        options,
        selector,
        inputMode: el.getAttribute("inputmode") || "",
        inputType,
        role,
        nearby: textOf(headingNode).slice(0, 160),
        group: legendText(el),
        prompt: inputType === "checkbox" ? checkboxPrompt(el) : "",
      });
    }

    for (const el of root.querySelectorAll("button, input[type='submit'], input[type='button'], [role='button']")) {
      if (isHidden(el)) continue;
      const name = (el.innerText || el.value || el.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
      const selector = selectorFor(el);
      if (!name || !selector || seenButtons.has(selector)) continue;
      seenButtons.add(selector);
      buttons.push({
        name,
        selector,
        controlType: (el.getAttribute("type") || "").toLowerCase(),
      });
    }
  });

  const consumed = new Set();
  eachRoot(document, (root) => {
    const seenParents = new Set();
    for (const el of root.querySelectorAll("button, [role='button']")) {
      if (isHidden(el) || !el.parentElement || seenParents.has(el.parentElement)) continue;
      const parent = el.parentElement;
      const kids = Array.from(parent.children).filter((node) => {
        const tag = node.tagName ? node.tagName.toLowerCase() : "";
        return (tag === "button" || node.getAttribute("role") === "button") && !isHidden(node);
      });
      const names = kids.map((node) => (node.innerText || "").replace(/\s+/g, " ").trim().toLowerCase());
      if (names.length !== 2 || names.indexOf("yes") === -1 || names.indexOf("no") === -1) continue;
      seenParents.add(parent);
      let question = "";
      const labelled = parent.getAttribute("aria-labelledby") || "";
      if (labelled) {
        const node = document.getElementById(labelled.split(/\s+/)[0]);
        if (node) question = textOf(node);
      }
      if (!question) question = (parent.getAttribute("aria-label") || "").trim();
      if (!question && parent.querySelector("legend")) question = textOf(parent.querySelector("legend"));
      if (!question && parent.previousElementSibling) question = textOf(parent.previousElementSibling);
      if (!question) continue;
      const options = [];
      for (const kid of kids) {
        const optionSelector = selectorFor(kid);
        if (!optionSelector) continue;
        const optionLabel = (kid.innerText || "").replace(/\s+/g, " ").trim();
        options.push({ value: optionLabel, label: optionLabel, selector: optionSelector });
      }
      if (options.length !== 2) continue;
      for (const option of options) consumed.add(option.selector);
      controls.push({
        kind: "buttons",
        name: "",
        elementId: parent.id || "",
        label: question,
        placeholder: "",
        ariaLabel: parent.getAttribute("aria-label") || "",
        autocomplete: "",
        required: parent.getAttribute("aria-required") === "true",
        hidden: false,
        disabled: false,
        readOnly: false,
        options,
        selector: options[0].selector,
        inputMode: "",
        inputType: "button",
        role: "group",
        nearby: "",
        group: question,
        prompt: "",
      });
    }
  });
  for (let index = buttons.length - 1; index >= 0; index -= 1) {
    if (consumed.has(buttons[index].selector)) buttons.splice(index, 1);
  }

  function invisibleChallenge(el) {
    if (el.closest(".grecaptcha-badge")) return true;
    if (el.closest("[data-size='invisible']")) return true;
    const src = (el.getAttribute("src") || "").toLowerCase();
    return src.indexOf("size=invisible") !== -1;
  }

  function challengeShown(el) {
    // A widget counts only when a person could interact with it. Invisible
    // reCAPTCHA and hCaptcha, and nodes with no real box, do not.
    if (invisibleChallenge(el)) return false;
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      if (node.hidden) return false;
      const style = window.getComputedStyle(node);
      if (style.display === "none" || style.visibility === "hidden") return false;
      if (parseFloat(style.opacity) === 0) return false;
      if (style.display !== "contents") {
        const box = node.getBoundingClientRect();
        if (box.width < 1 || box.height < 1) return false;
      }
    }
    const rect = el.getBoundingClientRect();
    if (rect.width < 30 || rect.height < 30) return false;
    // A box parked above or left of the page is hidden. A widget below the
    // fold still has a real box, so it still stops the run.
    if (rect.bottom <= 0 || rect.right <= 0) return false;
    return true;
  }

  let captcha = false;
  const wallText = (document.body ? document.body.innerText : "").slice(0, 5000).toLowerCase();
  if (
    wallText.indexOf("datadome") !== -1 ||
    wallText.indexOf("captcha-delivery") !== -1 ||
    wallText.indexOf("checking your browser") !== -1 ||
    wallText.indexOf("just a moment") !== -1 ||
    wallText.indexOf("perimeterx") !== -1 ||
    wallText.indexOf("px-captcha") !== -1
  ) {
    captcha = true;
  }
  const challengeSelector = [
    ".h-captcha",
    ".g-recaptcha",
    ".cf-turnstile",
    "#px-captcha",
    ".px-captcha",
    "iframe[src*='hcaptcha.com']",
    "iframe[src*='challenges.cloudflare.com']",
    "iframe[src*='arkoselabs']",
    "iframe[src*='datadome']",
    "iframe[src*='captcha-delivery']",
    "iframe[src*='recaptcha']",
  ].join(", ");
  eachRoot(document, (root) => {
    for (const el of root.querySelectorAll(challengeSelector)) {
      if (challengeShown(el)) captcha = true;
    }
  });
  const password = controls.some((control) => control.kind === "password" && !control.hidden);
  let heading = "";
  for (const node of document.querySelectorAll("h1, h2")) {
    if (!isHidden(node)) {
      heading = (node.innerText || node.textContent || "").replace(/\s+/g, " ").trim();
      break;
    }
  }
  let banner = "";
  const alertNode = document.querySelector("[role='alert'], #login-error, .login-error");
  if (alertNode && !isHidden(alertNode)) {
    banner = (alertNode.innerText || alertNode.textContent || "").replace(/\s+/g, " ").trim();
  }
  const alerts = [];
  eachRoot(document, (root) => {
    for (const node of root.querySelectorAll("[role='alert'], .field-error")) {
      if (!isHidden(node)) {
        const text = textOf(node);
        if (text) alerts.push(text.slice(0, 300));
      }
    }
  });
  return { controls, buttons, captcha, password, heading, banner, alerts };
}
"""


def _option(raw: dict) -> Option:
    return Option(
        value=str(raw.get("value", "")),
        label=str(raw.get("label", "")),
        selector=str(raw.get("selector", "")),
    )


def parse_snapshot(data: dict) -> PageSnapshot:
    """Turn the JSON returned by ``EXTRACT_JS`` into dataclasses."""
    controls: list[Control] = []
    for raw in data.get("controls", []):
        controls.append(
            Control(
                kind=str(raw.get("kind", "text")),
                name=str(raw.get("name", "")),
                element_id=str(raw.get("elementId", "")),
                label=str(raw.get("label", "")),
                placeholder=str(raw.get("placeholder", "")),
                aria_label=str(raw.get("ariaLabel", "")),
                autocomplete=str(raw.get("autocomplete", "")),
                required=bool(raw.get("required", False)),
                hidden=bool(raw.get("hidden", False)),
                disabled=bool(raw.get("disabled", False)),
                read_only=bool(raw.get("readOnly", False)),
                options=[_option(option) for option in raw.get("options", [])],
                selector=str(raw.get("selector", "")),
                input_mode=str(raw.get("inputMode", "")),
                input_type=str(raw.get("inputType", "")),
                role=str(raw.get("role", "")),
                nearby=str(raw.get("nearby", "")),
                group=str(raw.get("group", "")),
                prompt=str(raw.get("prompt", "")),
            )
        )
    buttons = [
        ButtonControl(
            name=str(raw.get("name", "")),
            selector=str(raw.get("selector", "")),
            control_type=str(raw.get("controlType", "")),
        )
        for raw in data.get("buttons", [])
    ]
    return PageSnapshot(
        controls=controls,
        buttons=buttons,
        captcha_present=bool(data.get("captcha", False)),
        password_present=bool(data.get("password", False)),
        heading=str(data.get("heading", "")),
        banner=str(data.get("banner", "")),
        alerts=[str(item) for item in data.get("alerts", []) if str(item).strip()],
    )


def extract_page(page) -> PageSnapshot:
    """Evaluate the extractor in ``page`` and return a snapshot."""
    # Call the arrow function as an expression. Playwright also accepts a bare
    # function string, but invoking it keeps the contract obvious.
    data = page.evaluate("(" + EXTRACT_JS + ")()")
    return parse_snapshot(data)
