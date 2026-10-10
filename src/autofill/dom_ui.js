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

  async function typeVisible(el, text) {
    if (!(el instanceof HTMLInputElement) && !(el instanceof HTMLTextAreaElement)) {
      return false;
    }
    const value = text == null ? "" : String(text);
    focusField(el);
    setNative(el, "");
    const visibleCount = value.length > 200 ? 40 : value.length;
    let current = "";
    for (let index = 0; index < visibleCount; index += 1) {
      await sleep(nextDelay());
      current += value.charAt(index);
      setNative(el, current);
    }
    if (current !== value) {
      setNative(el, value);
    }
    finish(el);
    return true;
  }

  function highlight(el) {
    if (!el || !el.style) {
      return;
    }
    el.style.outline = "3px solid #FF3B30";
    el.style.outlineOffset = "2px";
    el.setAttribute("data-af-highlight", "1");
  }

  function clearHighlight(el) {
    const nodes = el ? [el] : Array.from(document.querySelectorAll("[data-af-highlight]"));
    for (const node of nodes) {
      if (!node || !node.style) {
        continue;
      }
      node.style.outline = "";
      node.style.outlineOffset = "";
      node.removeAttribute("data-af-highlight");
    }
  }

  window.__afUI = { focusField, typeVisible, highlight, clearHighlight };
})();
