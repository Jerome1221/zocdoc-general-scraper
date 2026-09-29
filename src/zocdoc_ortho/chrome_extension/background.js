const FALLBACK_SERVER = "http://127.0.0.1:8765";
const CLOSE_AFTER_SUCCESS = true;

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
    chrome.tabs.create({ url: message.url, active: false }, (tab) => {
      if (chrome.runtime.lastError) {
        sendResponse({ ok: false, error: chrome.runtime.lastError.message });
        return;
      }
      sendResponse({ ok: true, tabId: tab.id });
    });
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

  getServer()
    .then((server) => fetch(server + endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(message.payload || {})
    }))
    .then(async (response) => {
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      if (message.type === "SAVE_HTML" && CLOSE_AFTER_SUCCESS && sender.tab?.id) {
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
