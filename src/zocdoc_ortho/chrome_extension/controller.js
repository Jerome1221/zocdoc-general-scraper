(() => {
  if (location.pathname !== "/controller") return;

  const params = new URLSearchParams(location.search);
  const expectedPort = params.get("expected_port");
  const runnerId = params.get("runner_id") || "runner";
  if (expectedPort && location.port !== expectedPort) {
    const corrected = `http://127.0.0.1:${expectedPort}/controller?runner_id=${encodeURIComponent(runnerId)}&expected_port=${encodeURIComponent(expectedPort)}`;
    console.warn("[Zocdoc Controller] correcting controller port:", location.href, "->", corrected);
    location.replace(corrected);
    return;
  }

  const SERVER = location.origin;
  document.title = `Zocdoc Controller - ${runnerId} - ${SERVER}`;
  const FALLBACK_POLL_MS = 500;
  let busy = false;

  chrome.runtime.sendMessage({ type: "SET_SERVER", server: SERVER }, (result) => {
    if (chrome.runtime.lastError) {
      console.error("[Zocdoc Controller] could not bind local server:", chrome.runtime.lastError.message);
    } else {
      console.log("[Zocdoc Controller] bound to:", result?.server || SERVER);
    }
  });

  async function getPollMs() {
    try {
      const response = await fetch(SERVER + "/capture-config", { cache: "no-store" });
      const data = await response.json();
      return Math.max(50, Number(data?.config?.controller_poll_ms || FALLBACK_POLL_MS));
    } catch (_) {
      return FALLBACK_POLL_MS;
    }
  }

  async function poll() {
    if (busy) return;
    busy = true;
    try {
      const response = await fetch(SERVER + "/next-open", { cache: "no-store" });
      const data = await response.json();
      if (data && data.ok && data.url) {
        chrome.runtime.sendMessage({ type: "OPEN_BACKGROUND_TAB", url: data.url }, (result) => {
          if (chrome.runtime.lastError) {
            console.error("[Zocdoc Controller] runtime error:", chrome.runtime.lastError.message);
          } else {
            console.log("[Zocdoc Controller] opened:", data.url, result);
          }
        });
      }
    } catch (err) {
      console.error("[Zocdoc Controller] poll failed:", err);
    } finally {
      busy = false;
    }
  }

  async function loop() {
    await poll();
    const pollMs = await getPollMs();
    setTimeout(loop, pollMs);
  }

  console.log("[Zocdoc Controller] active:", SERVER);
  loop();
})();
