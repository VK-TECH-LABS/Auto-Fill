/* Visible automation for a remote Playwright page.
   Installed as window.__afUI. Known fields use focus and typing only.
   The red outline is for a human-action blocker, and only Python asks for it. */
(() => {
  function sleep(ms) {
    if (window.__afClock && typeof window.__afClock.sleep === "function") {
      return Promise.resolve(window.__afClock.sleep(ms));
    }
    if (window.__afFast) {
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      setTimeout(resolve, ms);
    });
  }

  function nextDelay() {
    if (window.__afClock && typeof window.__afClock.nextDelay === "function") {
      return window.__afClock.nextDelay();
    }
    return 30 + Math.floor(Math.random() * 41);
  }

  function setNative(el, value) {
    const proto = el instanceof HTMLTextAreaElement
      ? window.HTMLTextAreaElement.prototype
      : window.HTMLInputElement.prototype;
    const desc = Object.getOwnPropertyDescriptor(proto, "value");
    if (desc && desc.set) {
      desc.set.call(el, value);
    } else {
      el.value = value;
    }
    el.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function finish(el) {
    el.dispatchEvent(new Event("change", { bubbles: true }));
    el.dispatchEvent(new Event("blur", { bubbles: true }));
  }

  function focusField(el) {
    if (!el || typeof el.scrollIntoView !== "function") {
      return;
    }
    el.scrollIntoView({ behavior: "smooth", block: "center" });
    try {
      el.focus({ preventScroll: true });
    } catch (error) {
      try {
        el.focus();
      } catch (ignored) {
        /* The scroll already happened. */
      }
    }
  }

  function typingTarget(el) {
    if (!el) {
      return null;
    }
    const blocked = { hidden: 1, radio: 1, checkbox: 1, file: 1, button: 1, submit: 1, reset: 1, image: 1 };
    function usable(node) {
      if (!(node instanceof HTMLInputElement) && !(node instanceof HTMLTextAreaElement)) {
        return false;
      }
      const type = String(node.getAttribute("type") || node.type || "").toLowerCase();
      return !blocked[type];
    }
    if (usable(el)) {
      return el;
    }
    if (!el.querySelector) {
      return null;
    }
    const inner = el.querySelector(
      "textarea, input:not([type='hidden']):not([type='radio']):not([type='checkbox']):not([type='file']):not([type='button']):not([type='submit']):not([type='reset']):not([type='image'])"
    );
    return usable(inner) ? inner : null;
  }

  async function typeVisible(el, text) {
    const target = typingTarget(el);
    if (!target) {
      return false;
    }
    const value = text == null ? "" : String(text);
    focusField(el);
    if (target !== el) {
      focusField(target);
    }
    setNative(target, "");
    const visibleCount = value.length > 200 ? 40 : value.length;
    let current = "";
    for (let index = 0; index < visibleCount; index += 1) {
      await sleep(nextDelay());
      current += value.charAt(index);
      setNative(target, current);
    }
    if (current !== value) {
      setNative(target, value);
    }
    finish(target);
    return true;
  }

  function controlBox(el) {
    if (!el || !el.closest) {
      return el;
    }
    const react = el.closest("[class*='select__control']");
    if (react) {
      return react;
    }
    const role = String(el.getAttribute("role") || "").toLowerCase();
    if (role === "combobox") {
      if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
        return el.parentElement || el;
      }
      return el;
    }
    const combo = el.closest("[role='combobox']");
    if (combo) {
      return combo;
    }
    const type = String(el.getAttribute("type") || el.type || "").toLowerCase();
    if (type === "radio" || role === "radio") {
      return el.closest("fieldset") || el.closest("[class*='question']") || el.closest(".field") || el;
    }
    return el;
  }

  function paint(node) {
    if (!node || !node.style) {
      return;
    }
    node.style.outline = "3px solid #FF3B30";
    node.style.outlineOffset = "2px";
    node.style.boxShadow = "inset 0 0 0 3px #FF3B30, 0 0 0 3px #FF3B30";
    node.setAttribute("data-af-highlight", "1");
  }

  function unpaint(node) {
    if (!node || !node.style) {
      return;
    }
    node.style.outline = "";
    node.style.outlineOffset = "";
    node.style.boxShadow = "";
    node.removeAttribute("data-af-highlight");
  }

  function highlight(el) {
    if (!el) {
      return;
    }
    clearHighlight(null);
    paint(controlBox(el));
  }

  function clearHighlight(el) {
    const nodes = new Set();
    if (!el) {
      document.querySelectorAll("[data-af-highlight]").forEach((node) => nodes.add(node));
    } else {
      nodes.add(el);
      const box = controlBox(el);
      if (box) {
        nodes.add(box);
      }
      let parent = el.parentElement;
      while (parent) {
        if (parent.getAttribute && parent.getAttribute("data-af-highlight") === "1") {
          nodes.add(parent);
        }
        parent = parent.parentElement;
      }
    }
    nodes.forEach((node) => unpaint(node));
  }

  window.__afUI = { focusField, typeVisible, highlight, clearHighlight };
})();
