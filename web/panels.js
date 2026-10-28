"use strict";
// Collapsible side panels: the navigation rail (icon-only when collapsed) and
// the Sessions list (fully hidden). Preferences persist in localStorage; narrow
// windows collapse automatically without overwriting the saved preference.
(function () {
  const STORAGE_KEY = "tessera-panels";
  const NAMES = ["navigation", "sessions"];

  function px(value, fallback) {
    const number = Number(value);
    return Number.isSafeInteger(number) && number > 0 ? number : fallback;
  }

  function create(options) {
    const doc = options.document;
    const storage = options.storage;
    const media = options.matchMedia;
    const body = doc.body;
    const $ = (id) => doc.getElementById(id);
    const limits = {
      navigation: px(body.dataset.navigationCollapseBelow, 1100),
      sessions: px(body.dataset.sessionsCollapseBelow, 900),
    };
    const mobile = media("(max-width: " + px(body.dataset.drawerBelow, 760) + "px)");
    const narrow = {
      navigation: media("(max-width: " + limits.navigation + "px)"),
      sessions: media("(max-width: " + limits.sessions + "px)"),
    };
    const saved = {navigation: false, sessions: false};
    const override = {navigation: null, sessions: null};
    try {
      const value = JSON.parse(storage.getItem(STORAGE_KEY) || "{}");
      for (const name of NAMES) if (typeof value[name] === "boolean") saved[name] = value[name];
    } catch (_) { /* Unreadable or blocked storage keeps the expanded defaults. */ }

    function collapsed(name) {
      if (override[name] !== null) return override[name];
      return narrow[name].matches || saved[name];
    }

    function persist() {
      try { storage.setItem(STORAGE_KEY, JSON.stringify(saved)); } catch (_) { /* Session-only state. */ }
    }

    function render() {
      const nav = collapsed("navigation");
      const sessions = collapsed("sessions");
      body.classList.toggle("navigation-collapsed", nav);
      body.classList.toggle("sessions-collapsed", sessions);
      const navButton = $("navigation-collapse");
      navButton.setAttribute("aria-expanded", String(!nav));
      navButton.setAttribute("aria-label", nav ? "Expand menu" : "Collapse menu");
      navButton.title = (nav ? "Expand menu" : "Collapse menu") + " (" + options.modifierLabel + "B)";
      $("navigation-collapse-label").textContent = nav ? "Expand menu" : "Collapse menu";
      for (const link of doc.querySelectorAll(".navigation-links a")) {
        // Icon-only links keep their accessible name from the visible text span; add a tooltip.
        if (nav) link.title = link.textContent.trim(); else link.removeAttribute("title");
      }
      for (const id of ["sessions-collapse", "sessions-expand"]) {
        const button = $(id);
        button.setAttribute("aria-expanded", String(!sessions));
        button.title = (sessions ? "Show sessions" : "Hide sessions") + " (" + options.modifierLabel + "Shift+B)";
      }
      $("sessions-collapse").setAttribute("aria-label", "Hide sessions");
      $("sessions-expand").setAttribute("aria-label", "Show sessions");
    }

    function toggle(name) {
      if (mobile.matches) return false;
      const next = !collapsed(name);
      const focusInside = name === "sessions" && next && $("sessions-panel").contains(doc.activeElement);
      if (narrow[name].matches) override[name] = next;   // Temporary while the window is narrow.
      else { saved[name] = next; override[name] = null; persist(); }
      render();
      if (focusInside) $("sessions-expand").focus();
      else if (name === "sessions" && !next && doc.activeElement === $("sessions-expand")) $("sessions-collapse").focus();
      return true;
    }

    function onKeydown(event) {
      if (!(event.metaKey || event.ctrlKey) || event.altKey || event.isComposing) return;
      if (String(event.key).toLowerCase() !== "b") return;
      if (toggle(event.shiftKey ? "sessions" : "navigation")) event.preventDefault();
    }

    for (const name of NAMES) {
      const query = narrow[name];
      const reset = () => { override[name] = null; render(); };
      if (query.addEventListener) query.addEventListener("change", reset);
      else if (query.addListener) query.addListener(reset);
    }
    $("navigation-collapse").addEventListener("click", () => toggle("navigation"));
    $("sessions-collapse").addEventListener("click", () => toggle("sessions"));
    $("sessions-expand").addEventListener("click", () => toggle("sessions"));
    doc.addEventListener("keydown", onKeydown);
    render();
    // Enable transitions only after the first paint so restored state does not animate.
    (options.afterPaint || ((fn) => fn()))(() => body.classList.add("panels-ready"));
    return {toggle, collapsed, render, onKeydown, storageKey: STORAGE_KEY};
  }

  window.TesseraPanels = {create};
  if (typeof document !== "undefined" && document.getElementById && document.getElementById("navigation-collapse") && window.matchMedia) {
    const mac = /Mac|iPhone|iPad/.test((window.navigator && window.navigator.platform) || "");
    let storage;
    try { storage = window.localStorage; } catch (_) { storage = {getItem: () => null, setItem: () => {}}; }
    window.TesseraPanels.instance = create({
      document, storage, matchMedia: (q) => window.matchMedia(q),
      modifierLabel: mac ? "⌘" : "Ctrl+",
      afterPaint: (fn) => window.requestAnimationFrame(() => window.requestAnimationFrame(fn)),
    });
  }
})();
