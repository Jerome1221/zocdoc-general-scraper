(() => {
  const FALLBACK = { check_ms: 500, stable_checks: 3, max_wait_ms: 12000 };
  const path = location.pathname.replace(/\/+$/, "");

  const NON_PROFILE_ROUTES = new Set([
    "about", "business", "patient-help", "createuser", "insurance",
    "specialty", "location", "condition", "procedure", "treatment",
    "hospital", "practice", "resources", "blog", "techblog"
  ]);

  function isProfilePath(value) {
    const segments = String(value || "").split("/").filter(Boolean);
    if (segments.length !== 2) return false;
    const [route, slug] = segments;
    if (!route || !slug) return false;
    if (NON_PROFILE_ROUTES.has(route.toLowerCase())) return false;
    if (/-\d+pm$/i.test(slug)) return false;
    return true;
  }

  const isSpecialtyIndex = path === "/specialty";
  const isProfile = isProfilePath(path);
  const isSpecialtyLanding =
    !isSpecialtyIndex &&
    !isProfile &&
    path.split("/").filter(Boolean).length === 1;
  const isListing =
    !isSpecialtyIndex &&
    !isProfile &&
    !isSpecialtyLanding &&
    (/\/[^/]+\/[^/]+-\d+pm(?:\/\d+)?$/.test(path) ||
      !!document.querySelector('article[data-test="search-result-item"]') ||
      !!document.querySelector('[data-test="selection-criteria-header"]'));

  const pageType =
    isSpecialtyIndex ? "specialty_index" :
    isSpecialtyLanding ? "specialty_landing" :
    isListing ? "listing" :
    isProfile ? "profile" :
    null;

  if (!pageType) return;

  const blockTerms = [
    "access is temporarily restricted",
    "unusual activity",
    "verify you are human",
    "are you a human",
    "captcha",
    "automated (bot) activity"
  ];

  const scriptStarted = performance.now();
  const navigationTiming = performance.getEntriesByType("navigation")[0];
  const navigationToContentScriptMs = Math.max(0, scriptStarted);

  function send(type, payload) {
    chrome.runtime.sendMessage({ type, payload }, (response) => {
      if (chrome.runtime.lastError) {
        console.error("[Zocdoc Saver] message error:", chrome.runtime.lastError.message);
        return;
      }
      console.log("[Zocdoc Saver] response:", response);
    });
  }

  function bodyTextLower() {
    return (document.body?.innerText || "").toLowerCase();
  }

  function blocked() {
    const text = bodyTextLower();
    return blockTerms.some((term) => text.includes(term));
  }

  function claimProfileVisible() {
    return pageType === "profile" && bodyTextLower().includes("claim your profile");
  }

  function readinessSignature() {
    if (pageType === "specialty_index") {
      const headings = Array.from(document.querySelectorAll("h1,h2,h3,h4"));
      const found = headings.some((node) =>
        (node.textContent || "").trim().toLowerCase() === "browse all specialties"
      );
      return found ? document.querySelectorAll("a[href]").length : 0;
    }

    if (pageType === "specialty_landing") {
      const panel = document.querySelector('[data-test="tab-panel-location"]');
      if (panel) return panel.querySelectorAll('a[href*="pm"]').length || 1;
      const canonical = document.querySelector('link[rel="canonical"]');
      return canonical ? document.querySelectorAll("a[href]").length || 1 : 0;
    }

    if (pageType === "listing") {
      const doctors = document.querySelectorAll('a[data-test="doctor-card-info-name"][href]').length;
      const pagination = document.querySelectorAll(
        'nav[data-test="search-results-pagination"] a[href]'
      ).length;
      return `${doctors}:${pagination}`;
    }

    const name = document.querySelector('[data-test="provider-name"]') ? 1 : 0;
    const locations = document.querySelectorAll('[data-test^="location-card-"]').length;
    const claim = claimProfileVisible() ? 1 : 0;
    return `${name}:${locations}:${claim}`;
  }

  function signatureIsMeaningful(signature) {
    if (pageType === "listing") return signature !== "0:0";
    if (pageType === "profile") return signature !== "0:0:0";
    return signature !== 0 && signature !== "0";
  }

  function capture({ started, stableCount, signature, reason, checks }) {
    if (blocked()) {
      send("PAGE_BLOCKED", {
        url: location.href,
        title: document.title,
        text: (document.body?.innerText || "").slice(0, 3000)
      });
      return;
    }

    const serializeStarted = performance.now();
    const html = "<!doctype html>\n" + document.documentElement.outerHTML;
    const serializeMs = performance.now() - serializeStarted;
    const captureAt = performance.now();
    const domInteractive = Number(navigationTiming?.domInteractive || 0);
    const domContentLoaded = Number(navigationTiming?.domContentLoadedEventEnd || 0);
    const loadEventEnd = Number(navigationTiming?.loadEventEnd || 0);

    send("SAVE_HTML", {
      url: location.href,
      title: document.title,
      html,
      capture_metrics: {
        navigation_to_content_script_ms: navigationToContentScriptMs,
        dom_interactive_ms: domInteractive,
        dom_content_loaded_ms: domContentLoaded,
        load_event_end_ms: loadEventEnd,
        stabilization_wait_ms: Math.max(0, captureAt - started),
        html_serialize_ms: Math.max(0, serializeMs),
        readiness_checks: checks,
        stable_count: stableCount,
        readiness_signature: String(signature ?? ""),
        capture_reason: reason,
        claim_profile_visible: claimProfileVisible()
      }
    });
  }

  function start(config) {
    const checkMs = Math.max(50, Number(config?.check_ms || FALLBACK.check_ms));
    const stableRequired = Math.max(1, Number(config?.stable_checks || FALLBACK.stable_checks));
    const maxWaitMs = Math.max(1000, Number(config?.max_wait_ms || FALLBACK.max_wait_ms));
    const started = performance.now();
    let lastSignature = null;
    let stableCount = 0;
    let checks = 0;

    const timer = setInterval(() => {
      checks += 1;
      if (blocked()) {
        clearInterval(timer);
        capture({ started, stableCount, signature: lastSignature, reason: "blocked", checks });
        return;
      }

      const signature = readinessSignature();
      const meaningful = signatureIsMeaningful(signature);
      if (signature === lastSignature && meaningful) {
        stableCount += 1;
      } else {
        stableCount = 0;
        lastSignature = signature;
      }

      const elapsed = performance.now() - started;
      if (stableCount >= stableRequired) {
        clearInterval(timer);
        capture({ started, stableCount, signature, reason: "stable", checks });
      } else if (elapsed >= maxWaitMs) {
        clearInterval(timer);
        capture({ started, stableCount, signature, reason: "max_wait", checks });
      }
    }, checkMs);
  }

  chrome.runtime.sendMessage({ type: "GET_CAPTURE_CONFIG", pageType }, (response) => {
    if (chrome.runtime.lastError || !response?.ok) {
      start(FALLBACK);
      return;
    }
    start(response.config || FALLBACK);
  });
})();
