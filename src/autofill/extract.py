"""Read form controls out of a Playwright page, including open shadow roots.

Workday and several other ATS sites place inputs inside open shadow trees.
``querySelector`` on the document does not see those nodes, so the extractor
walks every open shadow root. Playwright locators still pierce open shadow
roots, which is why an ``#id`` selector remains usable after extraction.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from autofill.mapping import mark_required_acknowledgements
from autofill.models import ButtonControl, Control, LinkControl, Option, PageSnapshot

_ATS_FRAME = re.compile(
    r"(greenhouse|lever\.co|ashbyhq|myworkday|smartrecruiters|adp\.com|icims|jobvite|workday)",
    re.IGNORECASE,
)

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

  function cleanPrompt(text) {
    return (text || "").replace(/[✱*＊]+$/, "").replace(/\s+/g, " ").trim();
  }

  function lettersOf(text) {
    return (text || "").replace(/[^A-Za-z]/g, "");
  }

  function isPlaceholderPrompt(text) {
    const folded = cleanPrompt(text).toLowerCase().replace(/[.…]+$/g, "").trim();
    return folded === "select" || folded === "please select" || folded === "choose" || folded === "choose one";
  }

  function isSectionHeading(node, text) {
    // "ADDITIONAL INFORMATION" is a section title, not a question.
    const cleaned = cleanPrompt(text);
    if (!cleaned || cleaned.indexOf("?") !== -1) return false;
    const tag = node && node.tagName ? node.tagName.toLowerCase() : "";
    if (/^h[1-6]$/.test(tag)) return true;
    const cls = ((node && node.getAttribute && node.getAttribute("class")) || "").toLowerCase();
    if (
      /(section|header|heading)/.test(cls) &&
      cls.indexOf("application-label") === -1 &&
      cls.indexOf("question") === -1
    ) {
      return true;
    }
    const letters = lettersOf(cleaned);
    return letters.length >= 3 && letters === letters.toUpperCase();
  }

  function optionText(el) {
    // The option is the wrapping label, or a label/span beside the input.
    // The group title lives on an ancestor, so it is not part of the option.
    const wrapping = el.closest("label");
    if (wrapping) return cleanPrompt(textOf(wrapping));
    const parent = el.parentElement;
    if (!parent) return "";
    const labels = [];
    for (const child of Array.from(parent.children)) {
      if (child === el || (child.contains && child.contains(el))) continue;
      const tag = child.tagName ? child.tagName.toLowerCase() : "";
      if (tag !== "label" && tag !== "span") continue;
      if (child.querySelector && child.querySelector("input, select, textarea, button")) continue;
      const text = cleanPrompt(textOf(child));
      if (text && !isSectionHeading(child, text)) labels.push(text);
    }
    return labels.join(" ").trim();
  }

  function groupQuestion(el) {
    // The question is the fieldset, the application-question text, or
    // aria-labelledby. An option label such as "Yes" is not the question.
    // A section heading is not the question either.
    const legendNode = el.closest("fieldset") ? el.closest("fieldset").querySelector("legend") : null;
    const legend = legendText(el);
    if (legend && !isSectionHeading(legendNode, legend)) return cleanPrompt(legend);
    const option = optionText(el);
    const stop = el.closest("form") || document.body;
    let node = el;
    for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
      const labelled = node.getAttribute && node.getAttribute("aria-labelledby");
      if (labelled) {
        const bits = [];
        for (const id of labelled.split(/\s+/)) {
          const target = document.getElementById(id);
          if (!target || target.contains(el) || isSectionHeading(target, textOf(target))) continue;
          const text = textOf(target);
          if (text) bits.push(text);
        }
        const joined = cleanPrompt(bits.join(" "));
        if (joined && joined !== option) return joined;
      }
      if (node === stop) break;
    }
    node = el.parentElement;
    for (let depth = 0; node && node !== stop && depth < 5; depth += 1, node = node.parentElement) {
      for (const child of Array.from(node.children)) {
        if (child.contains(el)) break;
        if (child.querySelector && child.querySelector("input, select, textarea, button")) continue;
        const text = cleanPrompt(textOf(child));
        if (!text || text.length > 600 || text === option || isSectionHeading(child, text)) continue;
        return text;
      }
    }
    return "";
  }

  function stripStar(text) {
    return cleanPrompt(text).replace(/[\s*✱＊]+$/g, "").trim();
  }

  function dedupeBits(bits) {
    const kept = [];
    for (const bit of bits) {
      const text = cleanPrompt(bit);
      const folded = stripStar(text).toLowerCase();
      if (!folded) continue;
      let skip = false;
      for (let index = 0; index < kept.length; index += 1) {
        const other = stripStar(kept[index]).toLowerCase();
        if (!other) continue;
        if (other === folded || (folded.length >= 3 && other.indexOf(folded) !== -1)) {
          skip = true;
          break;
        }
        if (other.length >= 3 && folded.indexOf(other) !== -1) {
          kept[index] = stripStar(text);
          skip = true;
          break;
        }
      }
      if (!skip) kept.push(stripStar(text));
    }
    return collapseRepeated(kept.join(" "));
  }

  function collapseRepeated(text) {
    const cleaned = (text || "").replace(/\s+/g, " ").trim();
    const words = cleaned ? cleaned.split(" ") : [];
    for (let count = 1; count < words.length; count += 1) {
      const left = stripStar(words.slice(0, count).join(" "));
      const right = stripStar(words.slice(count).join(" "));
      const leftFolded = left.toLowerCase();
      const rightFolded = right.toLowerCase();
      if (!leftFolded || !rightFolded) continue;
      if (leftFolded === rightFolded) return left;
      if (rightFolded.length >= 3 && leftFolded.indexOf(rightFolded) !== -1) return left;
      if (leftFolded.length >= 3 && rightFolded.indexOf(leftFolded) !== -1) return right;
    }
    return stripStar(cleaned);
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
    if (!bits.length) {
      const option = optionText(el);
      if (option) bits.push(option);
    }
    if (!bits.length) {
      const fieldset = el.closest("fieldset");
      if (fieldset) {
        const legend = fieldset.querySelector("legend");
        const text = legend ? textOf(legend) : "";
        if (text && !isSectionHeading(legend, text)) bits.push(text);
      }
    }
    if (!bits.length) {
      let node = el;
      const stop = el.closest ? el.closest("form") : null;
      let found = "";
      for (let depth = 0; node && depth < 8 && !found; depth += 1, node = node.parentElement) {
        if (stop && node === stop) break;
        let prev = node.previousElementSibling;
        while (prev && !found) {
          if (isHidden(prev)) {
            prev = prev.previousElementSibling;
            continue;
          }
          const tag = prev.tagName ? prev.tagName.toLowerCase() : "";
          if (tag === "label" || tag === "legend") {
            const text = textOf(prev);
            if (text && !isSectionHeading(prev, text) && !isPlaceholderPrompt(text)) found = text;
          }
          const blocks =
            tag === "input" ||
            tag === "textarea" ||
            tag === "button" ||
            tag === "select" ||
            (prev.querySelector &&
              prev.querySelector("input:not([type='hidden']), textarea, button, [role='combobox']"));
          if (blocks && !found) break;
          if (!found) {
            const inner = prev.querySelector && prev.querySelector("label, legend");
            if (inner && !isHidden(inner)) {
              const text = textOf(inner);
              if (text && !isSectionHeading(inner, text) && !isPlaceholderPrompt(text)) found = text;
            }
          }
          if (!found) {
            const cls = (prev.getAttribute("class") || "").toLowerCase();
            const text = cleanPrompt(textOf(prev));
            if (
              text &&
              text.length <= 200 &&
              !isSectionHeading(prev, text) &&
              !isPlaceholderPrompt(text) &&
              (
                cls.indexOf("question") !== -1 ||
                cls.indexOf("application-label") !== -1 ||
                cls.indexOf("label") !== -1 ||
                text.indexOf("?") !== -1
              )
            ) {
              found = text;
            }
          }
          if (found) break;
          prev = prev.previousElementSibling;
        }
      }
      if (found) bits.push(found);
    }
    return dedupeBits(bits);
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
    const fieldPath = el.closest && el.closest("[data-field-path]");
    const roleName = (el.getAttribute("role") || "").toLowerCase();
    if (
      fieldPath &&
      (roleName === "combobox" || (el.getAttribute("aria-autocomplete") || "").toLowerCase() === "list")
    ) {
      const path = fieldPath.getAttribute("data-field-path") || "";
      if (path) return "[data-field-path='" + cssAttr(path) + "'] [role='combobox']";
    }
    const automation = el.getAttribute("data-automation-id");
    if (automation) return "[data-automation-id='" + CSS.escape(automation) + "']";
    if (el.classList && el.classList.contains("postings-btn")) return tag + ".postings-btn";
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

  function placementOf(el) {
    const main = el.closest("main, [role='main']");
    const header = el.closest("header, [role='banner']");
    const nav = el.closest("nav, [role='navigation']");
    const footer = el.closest("footer, [role='contentinfo']");
    const ad = el.closest("aside, [role='complementary'], [data-ad], [data-advertisement], .ad, .advertisement");
    const inHeader = !!(header && (!main || !main.contains(header)));
    const inAd = !!ad;
    const outside = (node) => !!(node && (!main || !main.contains(node)));
    const inChrome = outside(nav) || outside(footer);
    const inMain = main ? !inAd : !(inHeader || inAd || inChrome);
    return { inHeader, inAd, inMain };
  }

  function isDisabled(el) {
    return !!(el.disabled || el.getAttribute("aria-disabled") === "true");
  }

  function controlRole(el) {
    const role = (el.getAttribute("role") || "").toLowerCase();
    if (role) return role;
    const tag = el.tagName ? el.tagName.toLowerCase() : "";
    return tag === "a" ? "link" : "button";
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
        const question = groupQuestion(el);
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
          label: question,
          placeholder: "",
          ariaLabel: el.getAttribute("aria-label") || "",
          autocomplete: "",
          required: group.some((radio) => (
            radio.required ||
            radio.getAttribute("aria-required") === "true" ||
            controlMarkedRequired(radio)
          )),
          hidden: group.every((radio) => isHidden(radio)),
          disabled: group.every((radio) => radio.disabled),
          readOnly: false,
          options,
          selector: options[0].selector,
          inputMode: "",
          inputType: "radio",
          role: "radio",
          nearby: "",
          group: textOf(legend) || question,
          prompt: question,
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
      if (kind === "checkbox") {
        const yesno = el.closest("[class*='yesno'], .ashby-application-form-input-yesno");
        if (yesno && yesno.querySelector("button, [role='button']")) continue;
      }
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
        })).filter((option) => (option.label || option.value) && !isJunkOption(option.label || option.value));
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
          required:
            !!el.required ||
            el.getAttribute("aria-required") === "true" ||
            (kind === "checkbox" && controlMarkedRequired(el)) ||
            (
              (role === "combobox" ||
                (el.getAttribute("aria-autocomplete") || "").toLowerCase() === "list" ||
                classToken.indexOf("autocomplete") !== -1) &&
              controlMarkedRequired(el)
            ),
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
        prompt: inputType === "checkbox" ? groupQuestion(el) : "",
        automationId: el.getAttribute("data-automation-id") || "",
        maxLength: el.maxLength > 0 && el.maxLength < 100000 ? el.maxLength : null,
        phoneAdjacent: kind === "combobox" || kind === "select" ? phoneAdjacent(el) : false,
      });
    }

    for (const el of root.querySelectorAll("button, input[type='submit'], input[type='button'], [role='button']")) {
      if (isHidden(el)) continue;
      const href = el.getAttribute("href") || "";
      let name = (el.innerText || el.value || el.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
      if (!name && /apply/i.test(href)) name = "Apply";
      const selector = selectorFor(el);
      if (!name || !selector || seenButtons.has(selector)) continue;
      seenButtons.add(selector);
      const place = placementOf(el);
      buttons.push({
        name,
        selector,
        controlType: (el.getAttribute("type") || "").toLowerCase(),
        automationId: el.getAttribute("data-automation-id") || "",
        ariaLabel: el.getAttribute("aria-label") || "",
        role: controlRole(el),
        href,
        inHeader: place.inHeader,
        inAd: place.inAd,
        inMain: place.inMain,
        disabled: isDisabled(el),
      });
    }
  });

  // A closed react-select keeps the real <input> or <select> aria-hidden and
  // shows .select__control. The question is the label beside that control.
  eachRoot(document, (root) => {
    for (const el of root.querySelectorAll(".select__control, [role='combobox']")) {
      if (isHidden(el)) continue;
      const inner = el.matches("input, select, textarea")
        ? el
        : (el.querySelector ? el.querySelector("input, select, textarea") : null);
      if (inner && !isHidden(inner)) continue;
      const question = labelFor(root, el);
      if (!question || isPlaceholderPrompt(question)) continue;
      const selector = selectorFor(el);
      if (!selector || controls.some((control) => control.selector === selector)) continue;
      controls.push({
        kind: "combobox",
        name: el.getAttribute("name") || "",
        elementId: el.id || "",
        label: question,
        placeholder: "",
        ariaLabel: el.getAttribute("aria-label") || "",
        autocomplete: "",
        required: controlMarkedRequired(el) || controlMarkedRequired(inner),
        hidden: false,
        disabled: !!(el.disabled || (inner && inner.disabled)),
        readOnly: false,
        options: [],
        selector,
        inputMode: "",
        inputType: "text",
        role: "combobox",
        nearby: "",
        group: "",
        prompt: "",
        phoneAdjacent: phoneAdjacent(inner || el),
      });
    }
  });

  function buttonText(node) {
    return (node.innerText || node.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
  }

  function visibleButtons(container) {
    return Array.from(container.querySelectorAll("button, [role='button']")).filter((node) => !isHidden(node));
  }

  function questionFor(container) {
    const labelled = container.getAttribute("aria-labelledby") || "";
    if (labelled) {
      const node = document.getElementById(labelled.split(/\s+/)[0]);
      if (node && !container.contains(node)) {
        const text = textOf(node);
        if (text && !isSectionHeading(node, text)) return text;
      }
    }
    const aria = (container.getAttribute("aria-label") || "").trim();
    if (aria) return aria;
    const inside = container.querySelector("label, legend, .ashby-application-form-question-title");
    if (inside && !inside.querySelector("button, [role='button']")) {
      const text = textOf(inside);
      if (text && !isSectionHeading(inside, text)) return text;
    }
    let node = container;
    for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
      const prev = node.previousElementSibling;
      if (!prev || !prev.tagName) continue;
      if (prev.querySelector && prev.querySelector("input, select, textarea, button")) continue;
      const text = textOf(prev);
      if (text && text.length <= 200 && !isSectionHeading(prev, text)) return text;
    }
    return "";
  }

  function requirementLabel(el) {
    const entry = el.closest && el.closest("[data-field-path], .ashby-application-form-field-entry");
    if (entry) {
      const labels = entry.querySelectorAll("label, .ashby-application-form-question-title, legend");
      for (const label of labels) {
        if (!label || label.contains(el)) continue;
        // A title whose `for` points at another control, such as Phone, does not
        // make a nested opt-in radio required.
        const targetId = label.getAttribute("for") || "";
        if (targetId) {
          const target = document.getElementById(targetId);
          if (target && target !== el) continue;
        }
        return label;
      }
    }
    if (el.id) {
      const explicit = document.querySelector("label[for='" + CSS.escape(el.id) + "']");
      if (explicit) return explicit;
    }
    const labelled = (el.getAttribute && el.getAttribute("aria-labelledby")) || "";
    if (labelled) {
      const node = document.getElementById(labelled.split(/\s+/)[0]);
      if (node) return node;
    }
    let node = el;
    for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
      const prev = node.previousElementSibling;
      if (prev && prev.tagName && prev.tagName.toLowerCase() === "label") return prev;
    }
    return null;
  }

  function controlMarkedRequired(el) {
    if (!el) return false;
    if (el.required || el.getAttribute("aria-required") === "true") return true;
    const label = requirementLabel(el);
    return !!(label && hasRequirementMark(label));
  }

  function classMarksRequired(node) {
    const cls = ((node.getAttribute && node.getAttribute("class")) || "").toLowerCase();
    const tokens = cls.split(/\s+/);
    for (const token of tokens) {
      if (!token || token.indexOf("not-required") !== -1 || token.indexOf("optional") !== -1) continue;
      if (
        token === "required" ||
        token.indexOf("_required") === 0 ||
        token.indexOf("question-required") !== -1 ||
        token.indexOf("required-mark") !== -1
      ) {
        return true;
      }
    }
    return false;
  }

  function hasRequirementMark(node) {
    if (!node) return false;
    if (/[*✱＊]/.test(node.textContent || "")) return true;
    return classMarksRequired(node);
  }

  function controlLabel(el) {
    const aria = ((el.getAttribute && el.getAttribute("aria-label")) || "").trim();
    if (aria) return cleanPrompt(aria).toLowerCase();
    const labelled = (el.getAttribute && el.getAttribute("aria-labelledby")) || "";
    if (labelled) {
      const node = document.getElementById(labelled.split(/\s+/)[0]);
      if (node) return cleanPrompt(textOf(node)).toLowerCase();
    }
    if (el.id) {
      const explicit = document.querySelector("label[for='" + CSS.escape(el.id) + "']");
      if (explicit) return cleanPrompt(textOf(explicit)).toLowerCase();
    }
    return "";
  }

  function phoneAdjacent(el) {
    // A Country menu that shares a small wrapper with the phone input.
    // Stop at the form so a later address Country is not pulled in.
    const label = controlLabel(el);
    if (label && label !== "country" && label.indexOf("country ") !== 0) return false;
    let node = el;
    for (let depth = 0; node && depth < 6; depth += 1, node = node.parentElement) {
      const tag = node.tagName ? node.tagName.toLowerCase() : "";
      if (tag === "form" || tag === "body" || tag === "html") break;
      const tels = node.querySelectorAll("input[type='tel'], input[autocomplete='tel'], input[id='phone']");
      if (!tels.length) continue;
      let menus = 0;
      for (const input of node.querySelectorAll("input[role='combobox'], select")) {
        const aria = (input.getAttribute("aria-label") || "").toLowerCase();
        const type = (input.getAttribute("type") || "").toLowerCase();
        if (aria === "search" || type === "search") continue;
        menus += 1;
      }
      if (menus <= 1) return true;
      break;
    }
    return false;
  }

  function stableGroupId(container) {
    if (container.id) return container.id;
    const field = container.closest && container.closest("[data-field-path]");
    if (field && field.getAttribute("data-field-path")) return field.getAttribute("data-field-path");
    const named = container.querySelector && container.querySelector("input[name]");
    if (named && named.getAttribute("name")) return named.getAttribute("name");
    const label = container.previousElementSibling;
    if (label && label.getAttribute && label.getAttribute("for")) return label.getAttribute("for");
    return "";
  }

  function cssAttr(value) {
    return String(value || "").replace(/\\/g, "\\\\").replace(/'/g, "\\'");
  }

  function stableChoiceSelector(container, kid) {
    if (kid.id) return "#" + CSS.escape(kid.id);
    const field = container.closest && container.closest("[data-field-path]");
    const path = field ? field.getAttribute("data-field-path") || "" : "";
    const option = (kid.getAttribute("data-option") || "").trim().toLowerCase();
    if (!path || !option) return selectorFor(kid);
    const root = "[data-field-path='" + cssAttr(path) + "']";
    const choice = "[data-option='" + cssAttr(option) + "']";
    return root + " button" + choice;
  }

  function isJunkOption(text) {
    const folded = (text || "").toLowerCase().replace(/[.…]+$/g, "").trim();
    if (
      folded === "select" ||
      folded === "please select" ||
      folded === "choose" ||
      folded === "choose one" ||
      folded === "select one" ||
      folded === "start typing"
    ) return true;
    return folded.indexOf("click here") !== -1 && (
      folded.indexOf("issue") !== -1 || folded.indexOf("browser") !== -1 || folded.indexOf("cache") !== -1
    );
  }

  function groupRequired(container, raw) {
    if (/[*✱＊]/.test(raw || "")) return true;
    if (hasRequirementMark(container)) return true;
    const labelled = (container.getAttribute && container.getAttribute("aria-labelledby")) || "";
    if (labelled) {
      const title = document.getElementById(labelled.split(/\s+/)[0]);
      if (title && !container.contains(title) && hasRequirementMark(title)) return true;
    }
    let node = container;
    for (let depth = 0; node && depth < 8; depth += 1, node = node.parentElement) {
      if (node.getAttribute && node.getAttribute("aria-required") === "true") return true;
      const flag = ((node.getAttribute && node.getAttribute("data-required")) || "").toLowerCase();
      if (flag === "true" || flag === "required") return true;
      const prev = node.previousElementSibling;
      if (!prev || (prev.querySelector && prev.querySelector("button, [role='button']"))) continue;
      if (hasRequirementMark(prev)) return true;
    }
    return false;
  }

  const consumed = new Set();
  eachRoot(document, (root) => {
    const seenGroups = new Set();
    for (const el of root.querySelectorAll("button, [role='button']")) {
      if (isHidden(el)) continue;
      let container = el.parentElement;
      for (let depth = 0; container && depth < 6; depth += 1, container = container.parentElement) {
        if (seenGroups.has(container)) break;
        const kids = visibleButtons(container);
        if (kids.length !== 2) continue;
        const names = kids.map((node) => buttonText(node).toLowerCase());
        if (names.indexOf("yes") === -1 || names.indexOf("no") === -1) continue;
        seenGroups.add(container);
        const rawQuestion = questionFor(container);
        const question = cleanPrompt(rawQuestion);
        if (!question) break;
        const options = [];
        for (const kid of kids) {
          const optionSelector = stableChoiceSelector(container, kid);
          if (!optionSelector) continue;
          const optionLabel = buttonText(kid);
          options.push({ value: optionLabel, label: optionLabel, selector: optionSelector });
        }
        if (options.length !== 2) break;
        for (const option of options) consumed.add(option.selector);
        controls.push({
          kind: "buttons",
          name: "",
          elementId: stableGroupId(container),
          label: question,
          placeholder: "",
          ariaLabel: container.getAttribute("aria-label") || "",
          autocomplete: "",
          required: groupRequired(container, rawQuestion),
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
        break;
      }
    }
  });
  function pickerQuestion(el) {
    const labelled = el.getAttribute("aria-labelledby") || "";
    if (labelled) {
      const bits = [];
      for (const id of labelled.split(/\s+/)) {
        const node = document.getElementById(id);
        if (!node || node === el || el.contains(node) || isSectionHeading(node, textOf(node))) continue;
        const text = cleanPrompt(textOf(node));
        if (text) bits.push(text);
      }
      if (bits.length) return bits.join(" ");
    }
    if (el.id) {
      const explicit = document.querySelector("label[for='" + CSS.escape(el.id) + "']");
      if (explicit && !el.contains(explicit)) {
        const text = cleanPrompt(textOf(explicit));
        if (text) return text;
      }
    }
    let node = el;
    for (let depth = 0; node && depth < 5; depth += 1, node = node.parentElement) {
      const prev = node.previousElementSibling;
      if (!prev || !prev.tagName) continue;
      if (prev.querySelector && prev.querySelector("input, select, textarea, button")) continue;
      const tag = prev.tagName.toLowerCase();
      const cls = (prev.getAttribute("class") || "").toLowerCase();
      const text = cleanPrompt(textOf(prev));
      if (!text || text.length > 200 || isSectionHeading(prev, text)) continue;
      if (
        tag === "label" ||
        tag === "legend" ||
        cls.indexOf("question") !== -1 ||
        cls.indexOf("application-label") !== -1 ||
        text.indexOf("?") !== -1
      ) {
        return text;
      }
    }
    const fieldset = el.closest("fieldset");
    if (fieldset) {
      const legend = fieldset.querySelector("legend");
      if (legend && !legend.contains(el)) {
        const text = cleanPrompt(textOf(legend));
        if (text && !isSectionHeading(legend, text)) return text;
      }
    }
    return "";
  }

  const navNames = {
    yes: true,
    no: true,
    submit: true,
    "submit application": true,
    apply: true,
    "apply now": true,
    next: true,
    continue: true,
    back: true,
    save: true,
    "save and continue": true,
    cancel: true,
    review: true,
    proceed: true,
  };
  eachRoot(document, (root) => {
    for (const el of root.querySelectorAll("button, [role='button']")) {
      if (isHidden(el)) continue;
      const selector = selectorFor(el);
      if (!selector || consumed.has(selector)) continue;
      const popup = (el.getAttribute("aria-haspopup") || "").toLowerCase();
      const ariaRequired = el.getAttribute("aria-required") === "true";
      if (popup !== "listbox" && !ariaRequired) continue;
      if ((el.getAttribute("type") || "").toLowerCase() === "submit") continue;
      const own = (el.innerText || el.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
      if (!own || navNames[own.toLowerCase()]) continue;
      const question = pickerQuestion(el);
      if (!question || question.toLowerCase() === own.toLowerCase()) continue;
      consumed.add(selector);
      controls.push({
        kind: "buttons",
        name: "",
        elementId: el.id || "",
        label: question,
        placeholder: "",
        ariaLabel: el.getAttribute("aria-label") || "",
        autocomplete: "",
        required: ariaRequired,
        hidden: false,
        disabled: !!el.disabled,
        readOnly: false,
        options: [],
        selector,
        inputMode: "",
        inputType: "button",
        role: "listbox",
        nearby: "",
        group: question,
        prompt: question,
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
  let botWall = false;
  const wallText = (document.body ? document.body.innerText : "").slice(0, 5000).toLowerCase();
  const wallPhrases = [
    "access is temporarily restricted",
    "datadome",
    "captcha-delivery",
    "checking your browser",
    "just a moment",
    "perimeterx",
    "px-captcha",
  ];
  for (const phrase of wallPhrases) {
    if (wallText.indexOf(phrase) !== -1) botWall = true;
  }
  if (botWall) captcha = true;
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
  const links = [];
  eachRoot(document, (root) => {
    for (const el of root.querySelectorAll("a[href], [role='link']")) {
      if (links.length >= 40) break;
      if (isHidden(el)) continue;
      const href = el.getAttribute("href") || "";
      let name = (el.innerText || el.getAttribute("aria-label") || "").replace(/\s+/g, " ").trim();
      if (!name && /apply/i.test(href)) name = "Apply";
      const selector = selectorFor(el);
      if (!name || !selector) continue;
      const place = placementOf(el);
      links.push({
        name: name.slice(0, 200),
        selector,
        automationId: el.getAttribute("data-automation-id") || "",
        ariaLabel: el.getAttribute("aria-label") || "",
        role: controlRole(el),
        href,
        inHeader: place.inHeader,
        inAd: place.inAd,
        inMain: place.inMain,
        disabled: isDisabled(el),
      });
    }
  });
  const automationIds = [];
  eachRoot(document, (root) => {
    for (const el of root.querySelectorAll("[data-automation-id]")) {
      if (isHidden(el)) continue;
      const id = el.getAttribute("data-automation-id");
      if (id && automationIds.indexOf(id) === -1) automationIds.push(id);
    }
  });
  function passwordHintText() {
    const fields = [];
    eachRoot(document, (root) => {
      for (const el of root.querySelectorAll("input[type='password']")) {
        if (!isHidden(el)) fields.push(el);
      }
    });
    const bits = [];
    function push(node) {
      if (!node || node.nodeType !== 1 || isHidden(node)) return;
      const value = textOf(node);
      if (!value || bits.indexOf(value) !== -1) return;
      bits.push(value);
    }
    function policyId(node) {
      const id = ((node.getAttribute && node.getAttribute("data-automation-id")) || "").toLowerCase();
      return id.indexOf("password") !== -1 && /requirement|rule|hint|policy|criteria|instruction/.test(id);
    }
    function looksLikeRules(node) {
      const text = textOf(node).toLowerCase();
      return /character|uppercase|lowercase|numeric|special|symbol|at least \d+/.test(text);
    }
    function pushLists(node) {
      if (!node || node.nodeType !== 1 || isHidden(node)) return false;
      const lists = [];
      const tag = node.tagName ? node.tagName.toLowerCase() : "";
      if (tag === "ul" || tag === "ol") lists.push(node);
      else if (node.querySelectorAll) {
        for (const list of node.querySelectorAll("ul, ol")) {
          if (list.querySelector && list.querySelector("input[type='password']")) continue;
          if (!isHidden(list)) lists.push(list);
        }
      }
      if (!lists.length) return false;
      for (const list of lists) push(list);
      return true;
    }
    eachRoot(document, (root) => {
      for (const el of root.querySelectorAll("[data-automation-id]")) {
        if (!policyId(el)) continue;
        if (!pushLists(el)) push(el);
      }
    });
    for (const el of fields) {
      const described = el.getAttribute("aria-describedby") || "";
      for (const id of described.split(/\s+/)) {
        if (!id) continue;
        const node = document.getElementById(id);
        if (!node) continue;
        if (!pushLists(node)) push(node);
      }
      let wrap = el.parentElement;
      for (let depth = 0; wrap && wrap !== document.body && depth < 8; depth += 1) {
        for (const child of Array.from(wrap.children || [])) {
          if (child.contains && child.contains(el)) continue;
          const tag = child.tagName ? child.tagName.toLowerCase() : "";
          if (tag === "ul" || tag === "ol") push(child);
          else if (policyId(child) && !pushLists(child)) push(child);
        }
        let sib = wrap.nextElementSibling;
        for (let index = 0; sib && index < 4; index += 1, sib = sib.nextElementSibling) {
          if (sib.querySelector && sib.querySelector("input[type='password']")) continue;
          if (policyId(sib)) {
            if (!pushLists(sib)) push(sib);
            continue;
          }
          const tag = sib.tagName ? sib.tagName.toLowerCase() : "";
          if (tag === "ul" || tag === "ol") {
            if (looksLikeRules(sib)) push(sib);
            continue;
          }
          if ((tag === "div" || tag === "p" || tag === "small") && looksLikeRules(sib)) {
            if (!pushLists(sib)) push(sib);
          }
        }
        const rootNode = wrap.getRootNode && wrap.getRootNode();
        if (wrap.parentElement) wrap = wrap.parentElement;
        else if (rootNode && rootNode.host) wrap = rootNode.host;
        else break;
      }
    }
    return bits.join(" ").replace(/\s+/g, " ").trim().slice(0, 300);
  }
  const pageText = (document.body ? document.body.innerText : "").replace(/\s+/g, " ").trim().slice(0, 2000);
  const passwordHint = passwordHintText();
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
  return {
    controls,
    buttons,
    captcha,
    botWall,
    password,
    heading,
    banner,
    alerts,
    pageText,
    passwordHint,
    links,
    automationIds,
  };
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
                automation_id=str(raw.get("automationId", "") or ""),
                max_length=_max_length(raw.get("maxLength")),
                phone_adjacent=bool(raw.get("phoneAdjacent", False)),
            )
        )
    buttons = [
        ButtonControl(
            name=str(raw.get("name", "")),
            selector=str(raw.get("selector", "")),
            control_type=str(raw.get("controlType", "")),
            automation_id=str(raw.get("automationId", "") or ""),
            aria_label=str(raw.get("ariaLabel", "") or ""),
            role=str(raw.get("role", "") or ""),
            href=str(raw.get("href", "") or ""),
            in_header=bool(raw.get("inHeader", False)),
            in_ad=bool(raw.get("inAd", False)),
            in_main=bool(raw.get("inMain", True)),
            disabled=bool(raw.get("disabled", False)),
        )
        for raw in data.get("buttons", [])
    ]
    links = [
        LinkControl(
            name=str(raw.get("name", "")),
            selector=str(raw.get("selector", "")),
            automation_id=str(raw.get("automationId", "") or ""),
            aria_label=str(raw.get("ariaLabel", "") or ""),
            role=str(raw.get("role", "") or ""),
            href=str(raw.get("href", "") or ""),
            in_header=bool(raw.get("inHeader", False)),
            in_ad=bool(raw.get("inAd", False)),
            in_main=bool(raw.get("inMain", True)),
            disabled=bool(raw.get("disabled", False)),
        )
        for raw in data.get("links", [])
        if str(raw.get("name", "")).strip()
    ]
    snapshot = PageSnapshot(
        controls=controls,
        buttons=buttons,
        captcha_present=bool(data.get("captcha", False)),
        bot_wall=bool(data.get("botWall", False)),
        password_present=bool(data.get("password", False)),
        heading=str(data.get("heading", "")),
        banner=str(data.get("banner", "")),
        alerts=[str(item) for item in data.get("alerts", []) if str(item).strip()],
        page_text=str(data.get("pageText", "") or "")[:2000],
        password_hint=str(data.get("passwordHint", "") or "")[:300],
        links=links,
        automation_ids=[str(item) for item in data.get("automationIds", []) if str(item).strip()],
    )
    mark_required_acknowledgements(snapshot.controls)
    return snapshot


def _max_length(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int) and 0 < value < 100000:
        return value
    if isinstance(value, str) and value.isdigit():
        parsed = int(value)
        if 0 < parsed < 100000:
            return parsed
    return None


def challenge_message(snapshot: PageSnapshot) -> str:
    """A bot wall names the block. A visible widget keeps the CAPTCHA message."""
    if snapshot.bot_wall:
        return "site blocked automated access"
    return "CAPTCHA or challenge widget detected. Auto-Fill does not solve CAPTCHAs."


def _frame_url(frame) -> str:
    try:
        return str(getattr(frame, "url", "") or "")
    except Exception:
        return ""


def _usable_frame(page, frame) -> bool:
    """Same-origin frames and known ATS embeds. The main frame is already read."""
    main = getattr(page, "main_frame", None)
    if main is not None and frame is main:
        return False
    url = _frame_url(frame)
    if not url or url.startswith("about:"):
        return True
    try:
        page_url = str(getattr(page, "url", "") or "")
    except Exception:
        page_url = ""
    host = (urlparse(url).hostname or "").lower()
    parent = (urlparse(page_url).hostname or "").lower()
    if not host or host == parent:
        return True
    return bool(_ATS_FRAME.search(host))


def _tag_frame(snapshot: PageSnapshot, frame_url: str) -> None:
    for control in snapshot.controls:
        control.frame_url = frame_url
    for button in snapshot.buttons:
        button.frame_url = frame_url
    for link in snapshot.links:
        link.frame_url = frame_url


def _merge_snapshot(base: PageSnapshot, extra: PageSnapshot) -> None:
    base.controls.extend(extra.controls)
    base.buttons.extend(extra.buttons)
    base.links.extend(extra.links)
    for alert in extra.alerts:
        if alert not in base.alerts:
            base.alerts.append(alert)
    for item in extra.automation_ids:
        if item not in base.automation_ids:
            base.automation_ids.append(item)
    base.captcha_present = base.captcha_present or extra.captcha_present
    base.bot_wall = base.bot_wall or extra.bot_wall
    base.password_present = base.password_present or extra.password_present
    if not base.heading and extra.heading:
        base.heading = extra.heading
    if extra.page_text and extra.page_text not in base.page_text:
        base.page_text = f"{base.page_text} {extra.page_text}".strip()[:2000]
    if not base.banner and extra.banner:
        base.banner = extra.banner
    if not base.password_hint and extra.password_hint:
        base.password_hint = extra.password_hint


def extract_page(page) -> PageSnapshot:
    """Evaluate the extractor in ``page`` and in same-origin or ATS iframes."""
    # Call the arrow function as an expression. Playwright also accepts a bare
    # function string, but invoking it keeps the contract obvious.
    data = page.evaluate("(" + EXTRACT_JS + ")()")
    snapshot = parse_snapshot(data)
    frames = getattr(page, "frames", None)
    if not frames:
        return snapshot
    for frame in list(frames):
        if not _usable_frame(page, frame):
            continue
        try:
            extra_data = frame.evaluate("(" + EXTRACT_JS + ")()")
        except Exception:
            continue
        extra = parse_snapshot(extra_data)
        _tag_frame(extra, _frame_url(frame))
        _merge_snapshot(snapshot, extra)
    return snapshot
