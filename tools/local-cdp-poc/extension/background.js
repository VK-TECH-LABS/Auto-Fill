async function report(pageUrl, payload) {
  let destination;
  try {
    destination = new URL("/extension-evidence", pageUrl);
  } catch (_err) {
    return;
  }
  if (destination.protocol !== "http:" || destination.hostname !== "127.0.0.1") {
    return;
  }
  await fetch(destination.toString(), {
    method: "POST",
    headers: {"content-type": "application/json"},
    body: JSON.stringify(payload),
  });
}

async function inspect(tabId, href) {
  const target = {tabId};
  const attachError = await new Promise((resolve) => {
    chrome.debugger.attach(target, "1.3", () => {
      const err = chrome.runtime.lastError && chrome.runtime.lastError.message;
      resolve(err || "");
    });
  });
  if (attachError) {
    await report(href, {attached: false, error: attachError, href, via: "chrome.debugger"});
    return;
  }
  const evaluated = await new Promise((resolve) => {
    chrome.debugger.sendCommand(
      target,
      "Runtime.evaluate",
      {expression: "document.cookie", returnByValue: true},
      (result) => {
        const err = chrome.runtime.lastError && chrome.runtime.lastError.message;
        const value = result && result.result && result.result.value;
        resolve({err: err || "", value: typeof value === "string" ? value : ""});
      },
    );
  });
  chrome.debugger.detach(target);
  await report(href, {
    attached: true,
    error: evaluated.err,
    cookie: evaluated.value,
    href,
    via: "chrome.debugger",
  });
}

async function scan() {
  const tabs = await chrome.tabs.query({});
  for (const tab of tabs) {
    if (tab.id != null && tab.url && tab.url.includes("t172-local-tab")) {
      await inspect(tab.id, tab.url);
    }
  }
}

chrome.tabs.onUpdated.addListener((tabId, info, tab) => {
  if (info.status === "complete" && tab.url && tab.url.includes("t172-local-tab")) {
    inspect(tabId, tab.url);
  }
});
chrome.runtime.onInstalled.addListener(() => {
  scan();
});
setTimeout(scan, 1000);
setTimeout(scan, 2500);
