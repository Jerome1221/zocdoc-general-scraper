const FALLBACK_SERVER = "http://127.0.0.1:8765";
const CLOSE_AFTER_SUCCESS = true;
const REQUEST_KEY_PREFIX = "zocdocRequestedUrl:";
const CAPTURE_WATCHDOG_PREFIX = "zocdocCaptureWatchdog:";
const CAPTURE_WATCHDOG_MS = 25000;

function requestKey(tabId) {
  return REQUEST_KEY_PREFIX + String(tabId);
}

function captureWatchdogName(tabId) {
  return CAPTURE_WATCHDOG_PREFIX + String(tabId);
}

async function rememberRequestedUrl(tabId, url) {
  if (!tabId) return;
  await chrome.storage.session.set({ [requestKey(tabId)]: String(url || "") });
}

async function requestedUrlForTab(tabId) {
  if (!tabId) return "";
  const key = requestKey(tabId);
  const stored = await chrome.storage.session.get([key]);
  return String(stored[key] || "");
}

async function forgetRequestedUrl(tabId) {
  if (!tabId) return;
  await Promise.all([
    chrome.storage.session.remove([requestKey(tabId)]),
    chrome.alarms.clear(captureWatchdogName(tabId))
  ]);
}

function isZocdocUrl(url) {
  return /^https:\/\/www\.zocdoc\.com\//i.test(String(url || ""));
}

async function ensureCaptureScript(tabId, tabUrl = "") {
  const requestedUrl = await requestedUrlForTab(tabId);
  if (!requestedUrl || !isZocdocUrl(tabUrl || requestedUrl)) return false;

  try {
    const response = await chrome.tabs.sendMessage(tabId, { type: "PING_CAPTURE_SCRIPT" });
    if (response?.ok) return true;
  } catch (_) {
    // A missing receiver means the declarative content script was not attached.
  }

  await chrome.scripting.executeScript({
    target: { tabId },
    files: ["content.js"]
  });
  return true;
}

async function scheduleCaptureWatchdog(tabId) {
  await chrome.alarms.create(captureWatchdogName(tabId), {
    when: Date.now() + CAPTURE_WATCHDOG_MS
  });
}

async function closeRequestedTabs(url) {
  const target = String(url || "");
  if (!target) return 0;
  const stored = await chrome.storage.session.get(null);
  const tabIds = Object.entries(stored)
    .filter(([key, value]) => key.startsWith(REQUEST_KEY_PREFIX) && String(value || "") === target)
    .map(([key]) => Number(key.slice(REQUEST_KEY_PREFIX.length)))
    .filter((tabId) => Number.isInteger(tabId) && tabId > 0);

  let closed = 0;
  for (const tabId of tabIds) {
    try {
      await chrome.tabs.remove(tabId);
      closed += 1;
    } catch (error) {
      console.warn("[Zocdoc Saver] stale tab was already unavailable:", tabId, error);
    } finally {
      await forgetRequestedUrl(tabId).catch(() => {});
    }
  }
  return closed;
}

function normalizeServer(value) {
  const raw = String(value || "").replace(/\/+$/, "");
  if (/^http:\/\/(127\.0\.0\.1|localhost):\d+$/.test(raw)) return raw;
  return FALLBACK_SERVER;
}

async function getServer() {
  try {
    const stored = await chrome.storage.local.get(["zocdocServer"]);
    return normalizeServer(stored.zocdocServer || FALLBACK_SERVER);
  } catch (_) {
    return FALLBACK_SERVER;
  }
}

async function setServer(value) {
  const server = normalizeServer(value);
  await chrome.storage.local.set({ zocdocServer: server });
  return server;
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || !message.type) return;

  if (message.type === "SET_SERVER") {
    setServer(message.server)
      .then((server) => sendResponse({ ok: true, server }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  if (message.type === "OPEN_BACKGROUND_TAB") {
    chrome.tabs.create({ url: "about:blank", active: false }, async (tab) => {
      if (chrome.runtime.lastError) {
        sendResponse({ ok: false, error: chrome.runtime.lastError.message });
        return;
      }
      try {
        await rememberRequestedUrl(tab.id, message.url);
        await chrome.tabs.update(tab.id, { url: message.url });
        sendResponse({ ok: true, tabId: tab.id });
      } catch (error) {
        await forgetRequestedUrl(tab.id).catch(() => {});
        await chrome.tabs.remove(tab.id).catch(() => {});
        sendResponse({ ok: false, error: String(error) });
      }
    });
    return true;
  }

  if (message.type === "CLOSE_REQUESTED_TABS") {
    closeRequestedTabs(message.url)
      .then((closed) => sendResponse({ ok: true, closed, url: message.url }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  if (message.type === "GET_CAPTURE_CONFIG") {
    getServer()
      .then((server) => fetch(server + "/capture-config", { cache: "no-store" }))
      .then(async (response) => {
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
        const config = data?.config?.[message.pageType] || null;
        sendResponse({ ok: true, config });
      })
      .catch((error) => {
        console.warn("[Zocdoc Saver] capture config unavailable:", error);
        sendResponse({ ok: false, error: String(error) });
      });
    return true;
  }

  const endpoint =
    message.type === "PAGE_BLOCKED" ? "/blocked" :
    message.type === "SAVE_HTML" ? "/save" :
    null;

  if (!endpoint) return;

  Promise.all([getServer(), requestedUrlForTab(sender.tab?.id)])
    .then(([server, requestedUrl]) => fetch(server + endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ...(message.payload || {}),
        requested_url: requestedUrl || message.payload?.url || ""
      })
    }))
    .then(async (response) => {
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      if (message.type === "SAVE_HTML" && CLOSE_AFTER_SUCCESS && sender.tab?.id) {
        await forgetRequestedUrl(sender.tab.id).catch(() => {});
        try { await chrome.tabs.remove(sender.tab.id); }
        catch (err) { console.warn("[Zocdoc Saver] saved but could not close tab:", err); }
      }
      sendResponse({ ok: true, data });
    })
    .catch((error) => {
      console.error("[Zocdoc Saver] local server error:", error);
      sendResponse({ ok: false, error: String(error) });
    });

  return true;
});

chrome.tabs.onRemoved.addListener((tabId) => {
  forgetRequestedUrl(tabId).catch(() => {});
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.status !== "complete") return;
  ensureCaptureScript(tabId, tab.url)
    .then((active) => active ? scheduleCaptureWatchdog(tabId) : null)
    .catch((error) => {
      console.warn("[Zocdoc Saver] capture script watchdog setup failed:", error);
    });
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (!alarm.name.startsWith(CAPTURE_WATCHDOG_PREFIX)) return;
  const tabId = Number(alarm.name.slice(CAPTURE_WATCHDOG_PREFIX.length));
  if (!Number.isInteger(tabId) || tabId <= 0) return;

  ensureCaptureScript(tabId)
    .then((active) => {
      if (!active) return null;
      return chrome.tabs.sendMessage(tabId, { type: "FORCE_CAPTURE" });
    })
    .catch((error) => {
      console.warn("[Zocdoc Saver] forced capture failed:", tabId, error);
    });
});
