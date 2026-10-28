/* Vanilla UI for Tessera's HTTP contract. No dependencies or remote assets. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const desktop = window.TesseraDesktop || null;
  const state = {
    selecting: false,
    selected: new Set(),
    sessions: [], slug: null, epoch: 0, messages: new Map(), nodes: new Map(),
    stream: null, streamAbort: null, retryTimer: null,
    status: {}, busy: false, sending: false, stopping: null, dialogMode: "create", lastEvent: "",
    historyFlight: null, draft: new Map(), attachments: [], memory: new Map(), memoryVersion: new Map(), drawer: null,
    pendingMessages: [], messageTimer: null, detailsTimer: null, queue: [],
    page: "chat", pageShown: false, pageAnimations: [], sendShortcut: "enter", stickToBottom: true, programmaticScroll: false,
    readerScrolling: false, readerScrollTimer: null,
    statusEpoch: 0, approvalEpoch: 0, feedback: {}, replyTo: null, callNodes: new Map(), memoryEpoch: 0, taskFilter: "all", boardRevision: null, voiceNotice: null,
    boardView: null, boardViewEpoch: 0, infraSamples: {}, targetsReport: null,
    // The oversight lane's last reading. On demand only: it reads the newest bytes
    // of every session's events log, so it must not ride the 15-second poll.
    qualityReport: null, qualityReading: false,
    qualityMissing: false,
    boardControlsMissing: false,
    boardProjectFilter: "", boardShowDone: false, boardFocus: "all", boardSearch: "", boardCategory: "", openProject: "", pressure: null,
    // Which cards the reader has opened to read their whole note. Keyed by card
    // identity so a poll that rebuilds the row hands the expansion back.
    noteExpanded: {},
    // Which pressure groups the reader opened, so a refresh keeps them open.
    pressureExpanded: {},
    // Which card rows the reader opened, by identity, so a row rebuilt after it left
    // the current filter comes back open rather than collapsed under them.
    detailExpanded: {},
    // A reader who closes the session-activity panel means it; new activity must not
    // reopen it. Set from the details element's own toggle.
    activityCollapsed: false,
    // Sessions whose view panel the person asked to see whole.
    viewFull: new Set(),
    pendingBuild: null, typingTimer: null,
    pendingFieldText: {},
    pinPending: false, modelPickerKey: "",
    // The session the Voice page talks through. Empty means "use the dedicated
    // voice session", which is the default target rather than whichever chat the
    // reader happens to have open.
    voiceTarget: "",
  };
  let voice;
  let localVoice;
  let configView = null;
  let autonomy = null;
  let shelfView = null;
  const eventTypes = ["delta", "message", "turn_start", "turn_end", "route", "capture", "error", "steer", "interrupt", "queue", "resume", "checkpoint"];
  const pages = ["chat", "wip", "infrastructure", "voice", "integrations", "settings", "shelf"];
  const taskLabels = {queued: "Queued", waiting: "Waiting", running: "Running", succeeded: "Succeeded", failed: "Failed", cancelled: "Cancelled"};
  const boardLabels = {queued: "Queued", running: "Running", blocked: "Blocked", succeeded: "Succeeded", failed: "Failed", cancelled: "Cancelled"};
  const hostKinds = {local: "Local", ssh: "SSH", agent: "Agent", prometheus: "Prometheus", unknown: "Unknown kind"};
  const hostHealth = {unknown: "Not checked", healthy: "Healthy", degraded: "Degraded", unreachable: "Unreachable", stale: "Stale", disabled: "Off", unconfigured: "Not configured"};
  const infraErrors = {timeout: "Timed out", process: "Probe failed", unavailable: "Unavailable", invalid_response: "Invalid response", unsupported: "Unsupported"};

  function readLocation() {
    const route = { page: "chat", slug: null, project:null, pane:null };
    for (const entry of location.hash.slice(1).split("&")) {
      const separator = entry.indexOf("=");
      if (separator < 0) continue;
      const name = entry.slice(0, separator);
      let value;
      try { value = decodeURIComponent(entry.slice(separator + 1)); }
      catch (_) { continue; }
      if (name === "view" && pages.includes(value)) route.page = value;
      if (name === "session") route.slug = value;
      if (name === "project") route.project = value;
      if (name === "pane" && ["canvas", "plan", "tasks"].includes(value)) route.pane = value;
    }
    return route;
  }

  function writeLocation(replace = true) {
    let hash = "#view=" + encodeURIComponent(state.page);
    if (state.slug) hash += "&session=" + encodeURIComponent(state.slug);
    if (state.page === "wip" && state.openProject) hash += "&project=" + encodeURIComponent(state.openProject) + "&pane=" + (state.arenaMode || "canvas");
    if (location.hash === hash) return;
    if (replace) history.replaceState(null, "", hash);
    else history.pushState(null, "", hash);
  }

  function closeNavigation(focus = false) {
    const wasOpen = document.body.classList.contains("show-navigation");
    document.body.classList.remove("show-navigation");
    $("navigation-toggle").setAttribute("aria-expanded", "false");
    if (wasOpen && !state.drawer) $("drawer-backdrop").hidden = true;
    if (focus && wasOpen) $("navigation-toggle").focus();
  }

  function setPage(name, focus = true, write = true) {
    if (!pages.includes(name)) name = "chat";
    const changed = state.pageShown && state.page !== name;
    state.pageShown = true;
    if (name !== "chat" && name !== "voice") stopVoice("Stopped");
    // Reading focus is a chat-page posture. Leaving the chat page restores the bars, so
    // the setting cannot hide the navigation on a page that needs it.
    if (name !== "chat" && document.body.classList.contains("reading-focus")) applyReadingFocus(false, {persist: false});
    closeDrawers();
    closeNavigation();
    state.page = name;
    for (const page of pages) {
      const selected = page === name;
      $("page-" + page).hidden = !selected;
      const link = $("nav-" + page);
      link.setAttribute("aria-current", selected ? "page" : "false");
    }
    $("skip-link").href = "#page-" + name;
    if (name === "integrations") refreshIntegrations();
    if (name === "settings" && configView && !configView.hasEdits()) configView.load();
    if (name === "shelf" && shelfView) shelfView.load();
    syncProjectArena(name === "wip" ? projectById(state.openProject) : null);
    if (name === "infrastructure") {
      refreshTargets();
      // On demand, once, when the page is opened. The 15-second status poll must not
      // carry it: this route reads the newest bytes of every session's events log.
      if (!state.qualityReport) refreshQuality();
    }
    if (name === "wip") {
      state.boardViewPromise = refreshBoardView();
      refreshPressure().catch(() => {});
    }
    if (name === "voice") refreshMics();
    document.title = $("nav-" + name).textContent + " | Tessera";
    if (write) writeLocation(false);
    if (focus) $("page-" + name).focus({ preventScroll: true });
    if (changed) revealPage(name);
  }

  function revealPage(name) {
    for (const animation of state.pageAnimations) animation.cancel();
    state.pageAnimations = [];
    const reduced = typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const page = $("page-" + name);
    if (reduced || typeof page.animate !== "function") return;
    const reveal = page.animate([{opacity: 0.76}, {opacity: 1}], {duration: 360, easing: "cubic-bezier(0.2, 0.7, 0.2, 1)"});
    reveal.id = "cairn-page-reveal";
    state.pageAnimations.push(reveal);
    const seam = $("page-seam");
    const glow = seam.animate([{opacity: 0}, {opacity: 0.25, offset: 0.3}, {opacity: 0}], {duration: 460, easing: "ease-out"});
    glow.id = "cairn-page-seam";
    state.pageAnimations.push(glow);
    let delay = 0;
    for (const branch of seam.querySelectorAll("path")) {
      const trace = branch.animate([{strokeDashoffset: 100}, {strokeDashoffset: 0}], {duration: 360, delay, easing: "cubic-bezier(0.2, 0.7, 0.2, 1)"});
      trace.id = "cairn-page-branch";
      state.pageAnimations.push(trace);
      delay += 25;
    }
  }

  function agentName() {
    const name = state.status?.agent_name;
    return typeof name === "string" && name.trim() ? name.trim().slice(0, 40) : "Agent";
  }

  function durationText(seconds) {
    const total = Math.round(seconds);
    if (total < 60) return total + "s";
    const units = [[86400, "d"], [3600, "h"], [60, "m"]];
    const parts = [];
    let rest = total;
    for (const [size, label] of units) {
      if (rest >= size) { parts.push(Math.floor(rest / size) + label); rest %= size; }
      if (parts.length === 2) break;
    }
    return parts.join(" ");
  }

  function fillInlines(node, parts) {
    for (const part of parts) {
      if (part.type === "strong") node.append(element("strong", "", part.text));
      else if (part.type === "code") node.append(element("code", "", part.text));
      else node.append(document.createTextNode(part.text));
    }
  }

  function fillFormatted(container, text) {
    const format = window.TesseraFormat;
    if (!format || typeof text !== "string" || !text) { container.replaceChildren(); return; }
    const blocks = format.blocks(text);
    const previous = Array.from(container.children);
    const next = [];
    let position = 0;
    for (const block of blocks) {
      const key = JSON.stringify(block);
      const existing = previous[position];
      position += 1;
      if (existing && existing.formatKey === key) {
        next.push(existing);
        continue;
      }
      const slot = document.createDocumentFragment();
      if (block.type === "code") {
        slot.append(element("pre", "formatted-code", block.text));
      } else if (block.type === "heading") {
        const heading = element("h" + (block.level + 2), "formatted-heading");
        fillInlines(heading, block.inlines);
        slot.append(heading);
      } else if (block.type === "list") {
        const list = element(block.ordered ? "ol" : "ul", "formatted-list");
        for (const item of block.items) {
          const row = element("li");
          fillInlines(row, item);
          list.append(row);
        }
        slot.append(list);
      } else if (block.type === "table") {
        const table = element("table", "formatted-table");
        const head = element("thead");
        const headRow = element("tr");
        for (const cell of block.header) headRow.append(element("th", "", cell));
        head.append(headRow);
        const body = element("tbody");
        for (const row of block.rows) {
          const line = element("tr");
          for (const cell of row) line.append(element("td", "", cell));
          body.append(line);
        }
        table.append(head, body);
        slot.append(table);
      } else {
        const paragraph = element("p");
        fillInlines(paragraph, block.inlines);
        slot.append(paragraph);
      }
      const node = slot.firstChild;
      node.formatKey = key;
      next.push(node);
    }
    replaceChangedChildren(container, next);
  }

  function replaceChangedChildren(container, next) {
    const previous = Array.from(container.children);
    const wanted = new Set(next);
    let cursor = container.firstChild;
    for (const node of next) {
      while (cursor && !wanted.has(cursor)) {
        const obsolete = cursor;
        cursor = cursor.nextSibling;
        obsolete.remove();
      }
      if (node !== cursor) container.insertBefore(node, cursor);
      cursor = node.nextSibling;
    }
    for (const node of previous) if (!wanted.has(node)) node.remove();
  }

  function interimReply(message) {
    const calls = message.meta && message.meta.tool_calls;
    return message.kind === "agent" && Array.isArray(calls) && calls.length > 0;
  }

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function stringify(value) {
    if (typeof value === "string") return value;
    return JSON.stringify(value, null, 2) || "";
  }

  function bannerText(message) {
    if (window.TesseraShell && typeof window.TesseraShell.bannerText === "function") {
      return window.TesseraShell.bannerText(message);
    }
    return String(message || "").trim();
  }

  function showError(error) {
    if (!error || error.name === "AbortError") return;
    // A not-found for the open session is a presence question, settled by the session list.
    if (state.slug && error.message === "Session or resource not found") {
      refreshSessions().catch(() => {});
      return;
    }
    const text = bannerText(error.message);
    if (!text) return;
    // The only bar that does work is the one with Retry while the core is down.
    // It clears itself when the core answers. Every other failure is already in the
    // transcript or in the composer status, and a dismiss button is not an action.
    if (!text.startsWith("Server unreachable")) return;
    $("error-text").textContent = text;
    $("error-banner").hidden = false;
    watchReconnect();
  }

  function clearError() { $("error-banner").hidden = true; }

  // Offer new UI assets in Settings. Apply them when the reader chooses, keeping drafts.
  const draftKey = "tessera.drafts";
  // A field with an id survives a reload: the same id names the same field on the other
  // side, so its words come back. A field created at runtime with no id does not survive,
  // and reloading it would silently drop what a person typed. Only that case holds the
  // reload. Holding on the composer too would leave the page stale for as long as a draft
  // sat in it, which is most of the time.
  const DRAFT_PREFIX = "tessera-wip-draft:";

  // A reload must never cost a person's words. Every id'd field on the WIP page
  // (the Add work box, the note editors) drops its text to localStorage as it is
  // typed and recovers it if the page comes back with the field empty. Cleared on
  // successful saves. A live-build reload discards a WIP note and jumps pages; the
  // hold above stops the reload, this keeps the words safe even if something else
  // bounces the page.
  function forgetWipDraft(id) {
    try { localStorage.removeItem(DRAFT_PREFIX + id); } catch (_) {}
  }
  function captureWipDrafts() {
    document.addEventListener("input", (event) => {
      const target = event.target;
      if (!target || !target.id || typeof target.value !== "string") return;
      const page = document.getElementById("page-wip");
      if (!page || (typeof page.contains === "function" && !page.contains(target))) return;
      try {
        if (target.value) localStorage.setItem(DRAFT_PREFIX + target.id, target.value);
        else localStorage.removeItem(DRAFT_PREFIX + target.id);
      } catch (_) {}
    });
  }
  function restoreWipDrafts() {
    try {
      for (let index = 0; index < localStorage.length; index += 1) {
        const key = localStorage.key(index);
        if (!key || !key.startsWith(DRAFT_PREFIX)) continue;
        const id = key.slice(DRAFT_PREFIX.length);
        const field = document.getElementById(id);
        const page = document.getElementById("page-wip");
        if (!field || !page || !page.contains(field)) continue;
        if ((typeof field.value === "string") && field.value === "") {
          field.value = localStorage.getItem(key) || "";
          field.dispatchEvent(new Event("input", {bubbles: true}));
        }
      }
    } catch (_) {}
  }

  function holdsUnrecoverableText() {
    // The caret is the strongest signal that words are in flight: a focused
    // field with text blocks the live-build reload even though it carries an
    // id (the WIP note boxes all do; the earlier id skip is exactly what let
    // the reload eat the caret). A text box that loses focus and then sees the page
    // jump is seeing the reload.
    const active = document.activeElement;
    if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA")) {
      const type = active.type || "";
      const ignorable = ["checkbox", "radio", "button", "submit", "file", "range", "color"];
      if (!ignorable.includes(type) && typeof active.value === "string" && active.value) return true;
    }
    // Legacy: any unnamed field with leftover text also holds the build.
    for (const field of document.querySelectorAll("input, textarea")) {
      if (field.id) continue;
      if (["checkbox", "radio", "button", "submit", "file", "range", "color"].includes(field.type)) continue;
      if (typeof field.value === "string" && field.value) return true;
    }
    return false;
  }
  function holdReloadWhileTyping(build) {
    state.pendingBuild = build;
    clearTimeout(state.typingTimer);
    state.typingTimer = setTimeout(function retry() {
      state.typingTimer = null;
      if (!state.pendingBuild) return;
      if (holdsUnrecoverableText()) { state.typingTimer = setTimeout(retry, 1500); return; }
      reloadForNewBuild(state.pendingBuild);
    }, 1500);
  }
  function setInterfaceUpdateReady(ready) {
    const link = $("nav-settings");
    link.classList.toggle("update-ready", ready);
    if (ready) {
      const purpose = "Settings: a newer interface is available. Choose Update interface in Settings to reload and update.";
      link.setAttribute("title", purpose);
      link.setAttribute("aria-label", purpose);
    } else {
      link.removeAttribute("title");
      link.removeAttribute("aria-label");
    }
  }
  function reloadForNewBuild(build) {
    if (typeof build !== "string" || !build) return false;
    if (!state.uiBuild) { state.uiBuild = build; return false; }
    if (build === state.uiBuild) return false;
    state.pendingBuild = build;
    if (!state.applyBuild) {
      $("interface-update").hidden = false;
      setInterfaceUpdateReady(true);
      return false;
    }
    // Defer while a runtime-created field holds words that would not survive; the poll
    // calls again, and the timer retries once the person is done.
    if (holdsUnrecoverableText()) { holdReloadWhileTyping(build); return false; }
    // A rebuild is not the person's fault and must not cost them what they were typing.
    // The composer draft was the only thing kept, so an edit to a stylesheet or script
    // threw away a half-written board card. Keep every form field a person can be in.
    // Save every field that has an id: an id survives the reload and names the same field
    // on the other side. A field created at runtime with no id cannot be restored this
    // way (after a reload that element does not exist), which is why a reload never
    // happens while someone is typing in one. See holdReloadWhileTyping.
    const typing = {};
    document.querySelectorAll("input[id], textarea[id], select[id]").forEach((field) => {
      if (typeof field.value === "string" && field.value) typing[field.id] = field.value;
    });
    if (state.slug) state.draft.set(state.slug, $("message-input").value);
    const drafts = { current: state.slug ? null : $("message-input").value, sessions: Object.fromEntries(state.draft) };
    try { sessionStorage.setItem(draftKey, JSON.stringify(drafts)); sessionStorage.setItem(draftFieldsKey, JSON.stringify(typing)); } catch (_) { /* storage off: reload anyway */ }
    state.uiBuild = build;
    setInterfaceUpdateReady(false);
    location.reload();
    return true;
  }
  const draftFieldsKey = "tessera.fields";
  function restoreDrafts() {
    let saved;
    try { saved = JSON.parse(sessionStorage.getItem(draftKey) || "null"); sessionStorage.removeItem(draftKey); }
    catch (_) { saved = null; }
    if (saved && typeof saved === "object") {
      for (const [slug, text] of Object.entries(saved.sessions || {})) if (typeof text === "string" && text) state.draft.set(slug, text);
      if (typeof saved.current === "string" && saved.current) $("message-input").value = saved.current;
    }
    let fields;
    try { fields = JSON.parse(sessionStorage.getItem(draftFieldsKey) || "null"); sessionStorage.removeItem(draftFieldsKey); }
    catch (_) { fields = null; }
    if (!fields || typeof fields !== "object") return;
    for (const [id, value] of Object.entries(fields)) {
      if (typeof value !== "string") continue;
      const field = $(id);
      if (field && typeof field.value === "string") { field.value = value; continue; }
      // A card's note field is created when its card renders, which is after this runs.
      // Hold the words and hand them over when the field exists, rather than dropping them.
      state.pendingFieldText[id] = value;
    }
  }

  // Give back text that arrived before its field existed.
  function adoptPendingFieldText() {
    for (const id of Object.keys(state.pendingFieldText)) {
      const field = document.getElementById(id);
      if (!field || typeof field.value !== "string") continue;
      if (!field.value) field.value = state.pendingFieldText[id];
      delete state.pendingFieldText[id];
    }
  }

  // A rebuild replaces every card node, and a half-typed note lives inside one of them.
  // reloadForNewBuild already saves field text for a reload; this is the same save for a
  // rebuild that never leaves the page. A refresh must not drop the text someone is
  // typing into a note. Capture before the nodes go, hand the words back through
  // adoptPendingFieldText once the fields exist again.
  function captureFieldText(root) {
    if (!root || typeof root.querySelectorAll !== "function") return;
    for (const field of root.querySelectorAll("input[id], textarea[id], select[id]")) {
      if (typeof field.value === "string" && field.value) state.pendingFieldText[field.id] = field.value;
    }
  }

  // While the core is down, probe it; when it answers, resync everything the outage made stale.
  function watchReconnect() {
    if (state.reconnectTimer) return;
    const delay = voiceSetting("reconnectMs", 2000);
    const probe = async () => {
      try { await api("/api/status?shape=connection"); }
      catch (_) { state.reconnectTimer = setTimeout(probe, delay); return; }
      state.reconnectTimer = null;
      try { await resyncAfterReconnect(); }
      catch (error) { showError(error); }
    };
    state.reconnectTimer = setTimeout(probe, delay);
  }

  async function resyncAfterReconnect() {
    await Promise.all([refreshSessions(), refreshStatus(true)]);
    if (String($("error-text").textContent).startsWith("Server unreachable")) clearError();
    if (!state.slug) return;
    // Refresh in place: re-selecting would blank the open conversation and reset a running turn.
    if (state.sessions.some((session) => session.slug === state.slug)) await loadMessages(state.slug, state.epoch);
  }

  // A session leaves the view only after several successful lists in a row omit it.
  // Errors, slow responses and reconnects never count as absence.
  const absenceConfirmations = 3;

  function leaveSession() {
    stopVoice("Stopped");
    stopStream();
    state.epoch += 1;
    state.slug = null;
    state.messages = new Map();
    state.nodes = new Map();
    state.callNodes = new Map();
    state.oldest = 0;
    $("load-earlier").hidden = true;
    state.feedback = {};
    clearReply();
    state.live = null;
    state.busy = false;
    $("messages").replaceChildren();
    $("message-input").value = "";
    $("empty-state").hidden = false;
    $("empty-create").hidden = false;
    $("connection-state").textContent = "Connected";
    renderActivity();
    renderSessions();
    updateHeading();
    writeLocation();
  }

  // Shared key for a proxied core. The logic lives in authkey.js so it
  // can be tested without a DOM; here we only wire it in. Degrade rather than throw
  // if it is absent: a missing script must not brick a page that otherwise works,
  // and a loopback core needs no key anyway.
  const authKey = window.TesseraAuthKey || {
    takeKeyFromLocation() { return ""; },
    headers() { return { "Accept": "application/json" }; },
    withKey(path) { return path; },
  };
  authKey.takeKeyFromLocation();

  function headers() {
    return authKey.headers();
  }

  // EventSource cannot set request headers, so the SSE stream carries the key in
  // the query string. Without this, turning auth on would break live updates and
  // look like a dead page -- the exact way a shared key strands the operator.
  function withKey(path) {
    return authKey.withKey(path);
  }

  async function checkResponse(response) {
    if (response.status === 401) {
      // Name the fix instead of reporting a bare status: a locked core that says
      // only "Request failed (401)" reads as broken, and leaves a person stranded
      // with no way forward.
      const error = new Error("This core requires a shared key. Open it once as /?key=<key> to store it.");
      error.status = 401;
      throw error;
    }
    if (response.status === 403) {
      const error = new Error("Rejected by core");
      error.status = 403;
      throw error;
    }
    if (!response.ok) {
      let message = "Request failed (" + response.status + ").";
      try {
        const body = await response.json();
        if (typeof body.error === "string") message = body.error;
      } catch (_) { /* A non-JSON error remains a readable HTTP status. */ }
      const error = new Error(message);
      error.status = response.status;
      throw error;
    }
    return response;
  }

  async function desktopReply(result) {
    if (result.ok) return result.data;
    const error = new Error("Core request failed");
    error.status = result.status;
    throw error;
  }

  async function api(path, method = "GET", body) {
    if (desktop) return desktopReply(await desktop.request(path, method, body));
    const options = { method, headers: headers(), cache: "no-store", redirect: "error" };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    let response;
    try { response = await fetch(path, options); }
    catch (_) { throw new Error("Server unreachable; message kept"); }
    await checkResponse(response);
    if (response.status === 204) return null;
    return response.json();
  }

  // Display tuning comes from [ui] in config.toml via /api/status.
  function uiSetting(name) {
    const value = state.status?.ui?.[name];
    if (value === undefined) throw new Error(`Server did not send ui.${name}`);
    return value;
  }

  // Optional tuning: a newer core sends it, an older one does not. Absent means
  // "let the server decide", not "fail the panel".
  function optionalUiSetting(name) {
    return state.status?.ui?.[name];
  }

  function sessionPath(slug, suffix = "") {
    return `/api/sessions/${encodeURIComponent(slug)}${suffix}`;
  }

  function activeSession() { return state.sessions.find((session) => session.slug === state.slug); }

  function renderSessions() {
    const query = $("session-search").value.toLocaleLowerCase();
    const list = $("session-list");
    const rows = [];
    for (const session of state.sessions) rows.push([session.slug, session.title, session.messages, session.archived, Boolean(session.infinity), Boolean(session.voice)]);
    // A record the server could not render is not a session that does not exist.
    // It gets its own line, because the case that matters is one broken record
    // among healthy ones: hiding the reason with the empty state would mean the
    // reader never sees it exactly when the list is not empty.
    const broken = state.status && Array.isArray(state.status.sessions_unreadable)
      ? state.status.sessions_unreadable : [];
    const identityErrors = state.status && Array.isArray(state.status.identity_record_errors)
      ? state.status.identity_record_errors : [];
    const renderKey = JSON.stringify([rows, state.slug, query, $("show-archived").checked, state.selecting, Array.from(state.selected), broken, identityErrors]);
    if (list.renderKey === renderKey) return;
    list.renderKey = renderKey;
    const fragment = document.createDocumentFragment();
    let shown = 0;
    // Autonomy sessions come first, under their own label. The flag is the server's
    // (meta.infinity on the session row), so a renamed autonomy session keeps its
    // treatment and an ordinary chat can never be mistaken for one. Partition first
    // because the server does not promise a stable order.
    const visible = state.sessions.filter((session) =>
      (!session.archived || $("show-archived").checked) && session.title.toLocaleLowerCase().includes(query));
    // The dedicated voice session is its own lane too: it is a place to talk, not
    // a chat that happens to be selected when you press Talk. Same server flag
    // pattern as autonomy, so renaming it keeps the treatment.
    const ordered = [...visible.filter((session) => session.infinity),
                     ...visible.filter((session) => !session.infinity && session.voice),
                     ...visible.filter((session) => !session.infinity && !session.voice)];
    for (const session of ordered) {
      const item = element("li", "session-item");
      if (session.infinity) item.classList.add("autonomy");
      if (session.voice) item.classList.add("voice-thread");
      item.dataset.slug = session.slug;
      const button = element("button", "session-button");
      button.type = "button";
      if (session.slug === state.slug) button.setAttribute("aria-current", "page");
      if (state.selecting) {
        const picked = state.selected.has(session.slug);
        button.setAttribute("aria-pressed", String(picked));
        if (picked) item.classList.add("picked");
      }
      const title = element("span", "", session.title);
      if (session.infinity) title.setAttribute("aria-label", session.title + " (autonomy session)");
      if (session.voice) title.setAttribute("aria-label", session.title + " (voice session)");
      button.append(title);
      const details = `${session.messages || 0} ${session.messages === 1 ? "message" : "messages"}${session.archived ? " / archived" : ""}`;
      button.append(element("small", "", details));
      button.addEventListener("click", () => {
        if (item.dataset.pressed) { delete item.dataset.pressed; return; }
        if (state.selecting) { toggleSelected(session.slug); return; }
        selectSession(session.slug).catch(showError);
      });
      item.addEventListener("contextmenu", (event) => { event.preventDefault(); openSessionMenu(session.slug, event.clientX, event.clientY); });
      watchLongPress(item, session.slug);
      const more = element("button", "session-more", "\u22ef");
      more.type = "button";
      more.setAttribute("aria-label", `Actions for ${session.title}`);
      more.setAttribute("aria-haspopup", "menu");
      more.addEventListener("click", (event) => {
        event.stopPropagation();
        const box = more.getBoundingClientRect();
        openSessionMenu(session.slug, box.right, box.bottom);
      });
      item.append(button, more);
      fragment.append(item);
      shown += 1;
    }
    // Labels go in front of the first row of each group, so a group is named once.
    if (ordered.some((session) => session.infinity)) {
      fragment.insertBefore(element("li", "session-group-label", "Autonomy"), fragment.firstChild);
    }
    const firstVoice = Array.from(fragment.children).find((row) =>
      row.classList.contains("session-item") && row.classList.contains("voice-thread"));
    if (firstVoice) fragment.insertBefore(element("li", "session-group-label", "Voice"), firstVoice);
    const firstOrdinary = Array.from(fragment.children).find((row) =>
      row.classList.contains("session-item") && !row.classList.contains("autonomy") && !row.classList.contains("voice-thread"));
    if (firstOrdinary) fragment.insertBefore(element("li", "session-group-label", "Bots"), firstOrdinary);
    $("session-list").replaceChildren(fragment);
    renderSelectBar();
    $("session-list-empty").hidden = shown > 0;
    $("session-list-empty").textContent = query ? "No matching sessions." : "No sessions here yet.";
    const notice = $("session-list-unreadable");
    const parts = [];
    if (broken.length) {
      parts.push(`Unreadable session${broken.length === 1 ? "" : "s"}: ` +
        broken.map((row) => `${row.slug} (${row.field} ${row.reason})`).join("; "));
    }
    if (identityErrors.length) {
      parts.push(identityErrors.map((row) => `${row.what || "identity"}: ${row.error || "unreported"}`).join("; "));
    }
    notice.hidden = parts.length === 0;
    notice.textContent = parts.join(" ");
  }

  // Session actions: right-click, long-press, or the row's ⋯ button open one menu.
  function watchLongPress(item, slug) {
    let timer = null;
    const cancel = () => { clearTimeout(timer); timer = null; };
    item.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "mouse") return;
      const x = event.clientX, y = event.clientY;
      timer = setTimeout(() => {
        timer = null;
        item.dataset.pressed = "1";
        openSessionMenu(slug, x, y);
      }, Number(uiSetting("long_press_ms")));
    });
    for (const name of ["pointerup", "pointercancel", "pointerleave", "pointermove"]) item.addEventListener(name, cancel);
  }

  function openSessionMenu(slug, x, y) {
    const session = state.sessions.find((entry) => entry.slug === slug);
    if (!session) return;
    const menu = $("session-menu");
    menu.dataset.slug = slug;
    $("menu-archive").textContent = session.archived ? "Restore" : "Archive";
    $("menu-workspace").value = session.workspace || "";
    menu.hidden = false;
    const width = menu.offsetWidth, height = menu.offsetHeight;
    menu.style.left = `${Math.max(4, Math.min(x, window.innerWidth - width - 4))}px`;
    menu.style.top = `${Math.max(4, Math.min(y, window.innerHeight - height - 4))}px`;
    $("menu-rename").focus();
  }

  function closeContextMenu() { $("session-menu").hidden = true; }

  // In-app confirmation; WKWebView silently refuses window.confirm.
  function ask(text, action) {
    return new Promise((resolve) => {
      const dialog = $("confirm-dialog");
      $("confirm-text").textContent = text;
      $("confirm-ok").textContent = action;
      $("confirm-ok").classList.toggle("danger", action === "Delete");
      const done = (answer) => () => { dialog.close(); resolve(answer); };
      $("confirm-ok").onclick = done(true);
      $("confirm-cancel").onclick = done(false);
      dialog.oncancel = done(false);
      dialog.showModal();
      $("confirm-cancel").focus();
    });
  }

  function titled(slugs) {
    if (slugs.length !== 1) return `${slugs.length} sessions`;
    const session = state.sessions.find((entry) => entry.slug === slugs[0]);
    return `\u201c${session ? session.title : slugs[0]}\u201d`;
  }

  async function archiveSessions(slugs, archived) {
    if (!slugs.length) return;
    if (slugs.includes(state.slug)) stopVoice("Stopped");
    for (const slug of slugs) await api(sessionPath(slug), "PATCH", { archived });
    $("show-archived").checked = true;
    endSelecting();
    await refreshSessions();
    clearError();
  }

  async function deleteSessions(slugs) {
    if (!slugs.length) return;
    if (!(await ask(`Delete ${titled(slugs)} permanently?`, "Delete"))) return;
    if (slugs.includes(state.slug)) { stopVoice("Stopped"); await moveOffSession(slugs); }
    for (const slug of slugs) await api(sessionPath(slug), "DELETE");
    state.sessions = state.sessions.filter((session) => !slugs.includes(session.slug));
    endSelecting();
    await refreshSessions();
  }

  async function moveOffSession(gone) {
    leaveSession();
    const next = state.sessions.find((session) => !session.archived && !gone.includes(session.slug));
    if (next) await selectSession(next.slug);
    else updateHeading();
  }

  function toggleSelected(slug) {
    if (state.selected.has(slug)) state.selected.delete(slug); else state.selected.add(slug);
    renderSessions();
  }

  function startSelecting(slug) {
    state.selecting = true;
    state.selected = new Set(slug ? [slug] : []);
    renderSessions();
  }

  function endSelecting() {
    state.selecting = false;
    state.selected = new Set();
    renderSessions();
  }

  function renderSelectBar() {
    const bar = $("session-select-bar");
    bar.hidden = !state.selecting;
    document.body.classList.toggle("selecting-sessions", Boolean(state.selecting));
    if (!state.selecting) return;
    const count = state.selected.size;
    $("select-count").textContent = `${count} selected`;
    $("select-archive").disabled = count === 0;
    $("select-delete").disabled = count === 0;
  }

  function desktopWithoutBackend() {
    return desktop && Array.isArray(state.status.backends) && state.status.backends.length === 0;
  }

  const activity = {phase: "", tools: new Map(), recent: [], approval: null};

  function observeActivity(type, data) {
    const previousPhase = activity.phase;
    let detail = "";
    if (type === "turn_start" || type === "resume") {
      activity.tools.clear();
      activity.approval = null;
      activity.phase = "Waiting for model";
      if (type === "resume" && data.live) {
        if (data.live.thought) activity.phase = "Thinking";
        if (data.live.text) activity.phase = "Writing";
        if (data.live.tool) activity.phase = "Tool requested: " + data.live.tool;
        if (data.live.status) activity.phase = data.live.status;
      }
      detail = type === "resume" ? (data.busy ? "Rejoined the current turn" : "Session connected") : "Turn started";
    } else if (type === "delta") {
      if (data.kind === "status") activity.phase = data.text;
      if (data.kind === "thought") activity.phase = "Thinking";
      if (data.kind === "text") activity.phase = "Writing";
      if (data.kind === "tool") activity.phase = "Preparing tool: " + data.text;
      if (data.kind === "reset") activity.phase = "Waiting for model";
    } else if (type === "message") {
      const message = data.message || data;
      const id = message.meta?.call_id;
      if (message.meta?.approval?.state === "pending") activity.approval = message.meta.approval.id;
      if (message.meta?.approval_result?.id === activity.approval) activity.approval = null;
      if (message.kind === "tool") {
        const item = window.TesseraItems.describe(message);
        detail = item.label + (item.summary ? ": " + item.summary : "");
        if (id) activity.tools.set(id, detail);
        activity.phase = "Tool requested: " + item.label;
      } else if (message.kind === "echo") {
        const title = activity.tools.get(id) || "Tool";
        activity.tools.delete(id);
        detail = title + (message.meta?.blocked ? " blocked" : message.meta?.success === false ? " failed" : " returned");
        activity.phase = activity.tools.size ? "Waiting for tool results" : "Waiting for model";
      }
    } else if (type === "turn_end") {
      activity.tools.clear();
      activity.approval = null;
      activity.phase = data.interrupted ? "Stopped" : data.error ? "Turn ended with an error" : "Turn complete";
      detail = activity.phase;
    } else if (type === "interrupt") {
      activity.phase = "Cancelling now";
      detail = "Stop requested";
    } else if (type === "steer") detail = "Your guidance reached the session";
    if (type === "delta" && activity.phase === previousPhase) return;
    if (detail) {
      activity.recent.unshift({time: new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}), detail});
      activity.recent = activity.recent.slice(0, 6);
    }
    renderActivity();
  }

  function renderActivity() {
    const box = $("session-activity");
    const status = sessionStatus();
    const connected = $("connection-state").textContent === "Connected";
    const busy = Boolean(state.busy);
    // A closed activity panel stays closed. Only an explicit open reopens it; the
    // summary line keeps reporting the phase either way.
    if (box && state.activityCollapsed === true && box.open === true) box.open = false;
    box.dataset.busy = String(busy && connected);
    document.body.classList.toggle("cairn-working", busy && connected);
    paintTrail();
    let phase = state.slug ? "Ready" : "Choose a session";
    if (state.slug && status.operator_stopped && !busy) phase = "Stopped";
    if (state.slug && busy) phase = activity.phase || "Working";
    if (state.slug && !connected) phase = "Reconnecting to session";
    if (busy && activity.approval) phase = "Waiting for your approval";
    $("activity-phase").textContent = phase;
    const counts = [];
    if (status.active_agents > 0) counts.push(status.active_agents + (status.active_agents === 1 ? " agent working" : " agents working"));
    if (status.queued > 0) counts.push(status.queued + " queued");
    $("activity-counts").textContent = counts.join(" · ");
    const pending = [];
    for (const title of activity.tools.values()) pending.push(title);
    for (const agent of status.agents || []) pending.push("Agent " + agent.name + (agent.backend ? " on " + backendLabel(agent.backend) : " starting"));
    $("activity-detail").textContent = pending.length ? "Awaiting results: " + pending.join("; ")
      : busy ? "Send guidance with Steer. Queue keeps your next prompt for the next turn."
      : "Activity appears here as this session works.";
    const events = $("activity-events");
    events.replaceChildren();
    for (const event of activity.recent) {
      const row = element("li");
      row.append(element("time", "", event.time), element("span", "", event.detail));
      events.append(row);
    }
  }

  function updateHeading() {
    // Until a session is selected the chat page shows only the empty-state call to action.
    document.body.classList.toggle("no-session", !state.slug);
    const session = activeSession();
    const title = session ? session.title : "Your next thread";
    $("session-title").textContent = title;
    $("session-title").title = title;
    const readingSwitch = $("reading-session-switch");
    if (readingSwitch) {
      readingSwitch.textContent = title + " - Sessions";
      readingSwitch.title = "Switch session: " + title;
    }
    // An autonomy session is not a chat someone opened, and the heading has to say so
    // without relying on the reader noticing a sidebar group. Flag is the server's.
    const autonomy = Boolean(session && session.infinity);
    $("session-badge").hidden = !autonomy;
    $("session-badge").textContent = autonomy ? "Autonomy" : "";
    document.body.classList.toggle("autonomy-session", autonomy);
    // A voice session says what it is for. The restriction that makes it usable is
    // that the spoken side answers briefly and hands real work to this same
    // session, so the heading names that instead of leaving a reader to guess.
    const spoken = Boolean(session && session.voice);
    if (spoken) {
      $("session-badge").hidden = false;
      $("session-badge").textContent = "Voice";
    }
    document.body.classList.toggle("voice-thread", spoken);
    const workspace = session ? (session.workspace || "default workspace") : "";
    const detail = session
      ? `${backendLabel(session.backend)} / ${workspace}${session.archived ? " / archived" : ""}${autonomy ? " / wakes on its own schedule" : ""}${spoken ? " / spoken answers, hands work to this session" : ""}`
      : "Choose a session or start a new one.";
    $("session-detail").textContent = detail;
    $("session-detail").title = detail;
    $("rename-session").disabled = !session;
    $("archive-session").disabled = !session;
    $("archive-session").textContent = session && session.archived ? "Restore" : "Archive";
    // The composer is always typeable; with no open session, sending starts one.
    $("message-input").disabled = Boolean(session && session.archived) || desktopWithoutBackend();
    $("message-input").placeholder = session ? "Continue this session..." : "Message to start a new session...";
    $("send-message").disabled = Boolean(session && session.archived) || state.sending || desktopWithoutBackend() || (!$("message-input").value.trim() && !state.attachments.length && !state.busy);
    $("attach-file").disabled = Boolean(session && session.archived) || state.sending || desktopWithoutBackend();
    $("send-message").textContent = state.sending ? "Sending..." : state.busy ? "Steer" : "Send";
    $("stop-turn").hidden = !session || !state.busy;
    $("stop-turn").disabled = !session || !state.busy || state.stopping === slugOrNull();
    const canQueue = Boolean(session && state.busy && !session.archived && ($("message-input").value.trim() || state.attachments.length));
    $("queue-next").hidden = !session || !state.busy;
    $("queue-next").disabled = !canQueue || state.sending;
    $("queue-next").textContent = "Queue";
    renderVoiceStatus();
    renderActivity();
    renderModelPickerOnce();
  }

  // Keystrokes only enable or disable Send. Rebuild the model picker when the
  // session, the available models, or their credential changes.
  function renderModelPickerOnce() {
    const session = activeSession();
    const main = ((state.status || {}).roles || {}).main || "";
    const names = state.chatNames || [];
    const key = [session ? session.slug : "", session ? session.backend || "" : "", main,
                 session?.reasoning || "low", session?.thinking_budget || 0,
                 names.join("|"), session ? session.archived ? "1" : "0" : "-",
                 names.map((name) => { const row = credentialOf(name); return row && row.label ? row.label : ""; }).join("")].join("\u0000");
    if (state.modelPickerKey === key) return;
    state.modelPickerKey = key;
    renderModelPicker();
  }

  function slugOrNull() { return state.slug || null; }

  function backendLabel(name) {
    if (!name) return "Default backend";
    return name;
  }

  // Credential label from the core. A CLI is named as that CLI. An API key names its variable.
  function credentialOf(name) {
    const all = ((state.status || {}).backend_credentials) || {};
    return all[name] || null;
  }

  function credentialTag(name) {
    const row = credentialOf(name);
    if (!row || typeof row.label !== "string") return "";
    return row.label;
  }

  function modelLabel(name) {
    const tag = credentialTag(name);
    return tag ? name + " · " + tag : name;
  }

  // Compact per-session model switch on the chat screen. Applies from the next turn.
  function renderModelPicker() {
    const picker = $("session-model");
    const session = activeSession();
    const main = ((state.status || {}).roles || {}).main || "";
    const names = state.chatNames || [];
    picker.replaceChildren();
    for (const name of names) {
      const option = element("option", "", modelLabel(name));
      option.value = name;
      picker.append(option);
    }
    const chosen = session && session.backend ? session.backend : main;
    picker.value = names.includes(chosen) ? chosen : (names[0] || "");
    picker.title = picker.selectedOptions?.[0]?.textContent || "";
    picker.disabled = !session || Boolean(session.archived) || names.length === 0;
    const reasoning = $("session-reasoning");
    if (reasoning) {
      reasoning.value = session && session.reasoning ? session.reasoning : "low";
      reasoning.disabled = !session || Boolean(session.archived);
    }
    renderThinkingBudget();
  }

  function renderThinkingBudget() {
    const session = activeSession();
    const budget = session?.thinking_budget || 0;
    const enabled = $("thinking-budget-enabled");
    const slider = $("thinking-budget");
    enabled.checked = budget > 0;
    enabled.disabled = !session || Boolean(session.archived) || session.reasoning === "off";
    slider.disabled = enabled.disabled || !enabled.checked;
    slider.value = budget || 4096;
    renderThinkingEnergy();
    if (session?.reasoning === "off") {
      $("thinking-budget-summary").textContent = "Thinking budget: off";
    } else {
      $("thinking-budget-summary").textContent = budget > 0
        ? "Thinking budget: " + budget.toLocaleString() + " tokens"
        : "Thinking budget: engine default";
    }
  }

  function renderThinkingEnergy() {
    const slider = $("thinking-budget");
    const low = Number(slider.min || 256);
    const high = Number(slider.max || 32768);
    const intensity = Math.max(0, Math.min(1, (Number(slider.value) - low) / (high - low)));
    const control = $("thinking-budget-control");
    control.style.setProperty("--thinking-fill", (intensity * 100).toFixed(2) + "%");
    control.style.setProperty("--thinking-period", (2.8 - intensity * 2.15).toFixed(2) + "s");
    control.style.setProperty("--thinking-halo", (3 + intensity * 14).toFixed(1) + "px");
    control.dataset.energized = String(!slider.disabled);
    $("thinking-budget-value").textContent = Number(slider.value).toLocaleString() + " tokens";
  }

  async function saveThinkingBudget() {
    const slug = state.slug;
    if (!slug) return;
    const budget = $("thinking-budget-enabled").checked ? Number($("thinking-budget").value) : 0;
    try {
      await api(sessionPath(slug), "PATCH", { thinking_budget: budget });
      await refreshSessions();
      $("send-state").textContent = budget > 0
        ? "Thinking budget saved for the next model response."
        : "Thinking budget uses the engine default.";
    } catch (error) { showError(error); renderThinkingBudget(); }
  }

  function newestOpen() {
    let chosen = null;
    for (const session of state.sessions) {
      if (session.archived) continue;
      const stamp = String(session.updated || session.created || "");
      const chosenStamp = chosen ? String(chosen.updated || chosen.created || "") : "";
      if (!chosen || stamp > chosenStamp) chosen = session;
    }
    return chosen;
  }

  async function refreshSessions() {
    const sessions = await api("/api/sessions");
    if (!Array.isArray(sessions)) throw new Error("The server returned an invalid session list.");
    // Keep the open session listed until its absence is confirmed, so the list never flickers.
    const open = state.slug && state.sessions.find((session) => session.slug === state.slug);
    const missing = Boolean(open) && !sessions.some((session) => session.slug === state.slug);
    state.missingLists = missing ? (state.missingLists || 0) + 1 : 0;
    const confirmed = state.missingLists >= absenceConfirmations;
    state.sessions = missing && !confirmed ? [...sessions, open] : sessions;
    renderSessions();
    updateHeading();
    if (confirmed) {
      state.missingLists = 0;
      leaveSession();
      const next = newestOpen();
      if (next) await selectSession(next.slug);
    } else if (missing) {
      clearTimeout(state.absenceTimer);
      state.absenceTimer = setTimeout(() => refreshSessions().catch(() => {}), voiceSetting("absenceCheckMs", 2000));
    }
  }

  function stopStream() {
    if (state.stream) state.stream.close();
    if (state.streamAbort) state.streamAbort.abort();
    clearTimeout(state.retryTimer);
    clearTimeout(state.messageTimer);
    clearTimeout(state.detailsTimer);
    state.pendingMessages = [];
    state.messageTimer = null;
    state.detailsTimer = null;
    state.stream = null;
    state.streamAbort = null;
    state.retryTimer = null;
  }

  async function selectSession(slug) {
    stopVoice("Stopped");
    state.voiceNotice = null;
    $("board-evidence").hidden = true;
    $("board-evidence").textContent = "";
    if (state.slug) state.draft.set(state.slug, $("message-input").value);
    stopStream();
    state.epoch += 1;
    const epoch = state.epoch;
    state.slug = slug;
    activity.phase = "";
    activity.approval = null;
    activity.tools.clear();
    activity.recent = [];
    $("memory-record-search").value = "";
    $("memory-record-search").disabled = true;
    $("memory-live-content").replaceChildren();
    $("memory-read-state").textContent = "Choose a memory scope to read it.";
    state.oldest = 0;
    state.stickToBottom = true;
    $("jump-latest").hidden = true;
    $("load-earlier").hidden = true;
    state.messages = new Map();
    state.nodes = new Map();
    state.live = null;
    state.historyFlight = null;
    state.lastEvent = "";
    state.busy = false;
    $("messages").replaceChildren();
    $("queue-list").replaceChildren();
    $("queue-list").hidden = true;
    $("message-input").value = state.draft.get(slug) || "";
    $("send-state").textContent = "";
    $("route-state").textContent = "";
    $("view-lines").replaceChildren();
    $("view-budget").textContent = "Loading view...";
    $("inheritance-digest").textContent = "Loading memory...";
    $("write-debt").textContent = "Loading write-debt...";
    $("empty-state").hidden = true;
    renderSessions();
    updateHeading();
    closeDrawers();
    writeLocation();
    if (desktopWithoutBackend()) {
      $("connection-state").textContent = "No backend configured";
      $("session-detail").textContent = "No backend configured";
      $("view-budget").textContent = "Session view is unavailable until a backend is configured.";
      $("inheritance-digest").textContent = "Session memory is unavailable until a backend is configured.";
      $("write-debt").textContent = "Capture counts are not reported for this session.";
      return;
    }
    startStream(slug, epoch);
    await loadMessages(slug, epoch);
    if (epoch !== state.epoch) return;
    // A session opens at its newest line, on a phone as well as a desktop. The
    // history lands above the fold otherwise, and the reader sees the start of a
    // long conversation instead of the work in progress.
    scrollToLatest();
    $("message-input").focus({ preventScroll: true });
    Promise.all([loadFeedback(slug, epoch), refreshView(slug, epoch), refreshStatus(true), loadQueue(slug, epoch)]).catch(showError);
  }

  function toolResultNotice(text) {
    const outputs = [text];
    try {
      const result = JSON.parse(text);
      if (result && typeof result === "object" && !Array.isArray(result)) {
        for (const field of ["output", "text"]) {
          if (typeof result[field] === "string") outputs.push(result[field]);
        }
      }
    } catch (_) { /* Plain tool text is also a valid result. */ }
    for (const output of outputs) {
      if (/(?:^|\r?\n)\[clipped\](?:\r?\n|$)/.test(output)) {
        return "Output clipped";
      }
      if (/(?:^|\r?\n)\[Message \d+: bytes \d+\.\.\d+ of \d+\. Continue with zoom\(\d+, 1, offset=\d+(?:, source='parent')?\)\.\]$/.test(output)) {
        return "More source remains. Open the result for its continuation.";
      }
    }
    return "";
  }

  function messageNode(message) {
    const kind = message.kind || "agent";
    if (message.meta?.landing_receipt === true) {
      const node = element("article", "message work landing-receipt-message");
      node.dataset.index = String(message.i);
      const heading = element("div", "message-heading");
      heading.append(element("strong", "", "Git landing receipt"));
      const date = new Date(message.date);
      const time = element("time", "", Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
      if (!Number.isNaN(date.getTime())) time.dateTime = date.toISOString();
      heading.append(time);
      const lines = Array.isArray(message.meta.landing_lines)
        ? message.meta.landing_lines.filter((line) => typeof line === "string")
        : String(message.text || "").split("\n");
      node.append(heading, element("pre", "landing-receipt", lines.join("\n")));
      return node;
    }
    const node = element("article", "message " + kind);
    node.dataset.index = String(message.i);
    const companion = kind === "note" ? message.meta?.companion : null;
    if (companion === "ember" || companion === "atlas") {
      node.classList.add("companion-chip");
      const details = element("details");
      const label = message.meta?.label === "proposal" ? "Proposal" : companion === "ember" ? "Brief" : "Orientation";
      details.append(element("summary", "", label), element("pre", "", message.text));
      node.append(details);
      return node;
    }
    const item = window.TesseraItems.describe(message, Number(uiSetting("item_summary_chars")));
    if (item) {
      node.classList.add("item", "item-" + item.kind);
      const details = element("details");
      const summary = element("summary");
      summary.append(element("span", "item-kind", item.label), element("span", "item-summary", item.summary));
      if (kind === "tool") summary.append(element("span", "tool-outcome", "Requested"));
      const notice = kind === "echo" ? toolResultNotice(message.text) : "";
      if (notice) {
        node.classList.add("incomplete-result");
        summary.append(element("span", "tool-result-badge", "Incomplete"));
      }
      details.append(summary, itemBody(item.body));
      node.append(details);
      if (message.meta?.clipped) {
        details.addEventListener("toggle", () => { if (details.open && message.meta.clipped) loadFullBody(message, details).catch(showError); }, { once: true });
      }
      if (notice) node.append(element("p", "tool-result-notice", notice));
      const callId = message.meta?.call_id;
      if (kind === "tool" && callId) {
        details.open = false;
        state.callNodes.set(callId, details);
      }
      if (kind === "echo" && callId && state.callNodes.has(callId)) {
        const call = state.callNodes.get(callId);
        const outcome = call.querySelector(".tool-outcome");
        outcome.textContent = message.meta?.blocked ? "Blocked" : message.meta?.success === false ? "Failed" : "Returned";
        outcome.dataset.failed = String(Boolean(message.meta?.blocked || message.meta?.success === false));
      }
    } else {
      const heading = element("div", "message-heading");
      const labels = { user: "You", agent: agentName(), work: "Session report" };
      heading.append(element("strong", "", labels[kind] || kind));
      if (message.meta?.voice_transcript === true) heading.append(element("span", "", "Voice transcript"));
      const date = new Date(message.date);
      const time = element("time", "", Number.isNaN(date.getTime()) ? "" : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
      if (!Number.isNaN(date.getTime())) time.dateTime = date.toISOString();
      heading.append(time);
      node.append(heading);
      if (kind === "agent" && typeof message.meta?.thought === "string" && message.meta.thought) node.append(thoughtBlock(message.meta.thought, false));
      const attached = message.meta?.attachments;
      if (kind === "user" && Array.isArray(attached) && attached.length) node.append(attachmentRow(message, attached));
      const body = element("div", "message-text");
      fillFormatted(body, message.text);
      node.append(body);
      if (interimReply(message)) node.classList.add("interim");
      if (kind === "agent") {
        node.append(messageActions(message));
        paintFeedback(node);
      }
    }
    return node;
  }

  function attachmentRow(message, refs) {
    const row = element("div", "message-attachments");
    for (const ref of refs) {
      if (!ref || typeof ref.id !== "string") continue;
      const href = sessionPath(state.slug, "/attachments?id=" + encodeURIComponent(ref.id));
      if (typeof ref.media_type === "string" && ref.media_type.startsWith("image/")) {
        const image = element("img");
        image.src = href;
        image.alt = ref.name || "Attached image";
        row.append(image);
      } else {
        const link = element("a", "", ref.name || "Attached file");
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener";
        row.append(link);
      }
    }
    return row;
  }

  function itemBody(body) {
    const container = element("div", "item-body");
    if (body.kind === "layers") {
      for (const layer of body.layers) {
        const block = element("section", "item-layer");
        block.append(element("div", "item-layer-head", layer.head), element("div", "item-text", layer.text));
        container.append(block);
      }
    } else if (body.kind === "fields") {
      const list = element("dl", "item-fields");
      for (const [key, value] of body.fields) list.append(element("dt", "", key), element("dd", "item-text", value));
      container.append(list);
    } else {
      container.append(element("div", "item-text", body.text));
    }
    return container;
  }

  function actionButton(className, label, glyph) {
    const button = element("button", "message-action " + className, glyph);
    button.type = "button";
    button.setAttribute("aria-label", label);
    button.title = label;
    return button;
  }

  function messageActions(message) {
    const row = element("div", "message-actions");
    const reply = actionButton("action-reply", "Reply", "Reply");
    const copy = actionButton("action-copy", "Copy", "Copy");
    const good = actionButton("action-good", "Good", "👍");
    const flag = actionButton("action-flag", "Flag an issue", "👎");
    const resolve = actionButton("action-resolve", "Resolve flag", "Resolve");
    for (const button of [good, flag, resolve]) button.setAttribute("aria-pressed", "false");
    reply.addEventListener("click", () => startReply(message));
    copy.addEventListener("click", () => copyText(message.text).then(() => flash(copy, "Copied"), () => flash(copy, "Copy failed")));
    good.addEventListener("click", () => setMark(message.i, { rating: state.feedback[message.i]?.rating === "good" ? null : "good" }).catch(showError));
    flag.addEventListener("click", () => setMark(message.i, { rating: state.feedback[message.i]?.rating === "flag" ? null : "flag" }).catch(showError));
    resolve.addEventListener("click", () => setMark(message.i, { resolved: !state.feedback[message.i]?.resolved }).catch(showError));
    row.append(reply, copy, good, flag, resolve);
    return row;
  }

  function flash(button, text) {
    const original = button.textContent;
    button.textContent = text;
    setTimeout(() => { button.textContent = original; }, 1500);
  }

  async function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      try { await navigator.clipboard.writeText(text); return; } catch (_) { /* WebKit may refuse; use the selection path. */ }
    }
    const area = element("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    area.setSelectionRange(0, text.length);
    const copied = document.execCommand("copy");
    area.remove();
    if (!copied) throw new Error("Copy failed");
  }

  function paintFeedback(node) {
    const mark = state.feedback[node.dataset.index] || {};
    node.dataset.rating = mark.rating || "";
    node.dataset.resolved = String(Boolean(mark.resolved));
    node.querySelector(".action-good")?.setAttribute("aria-pressed", String(mark.rating === "good"));
    node.querySelector(".action-flag")?.setAttribute("aria-pressed", String(mark.rating === "flag"));
    const resolve = node.querySelector(".action-resolve");
    if (resolve) {
      resolve.hidden = mark.rating !== "flag";
      resolve.setAttribute("aria-pressed", String(Boolean(mark.resolved)));
      resolve.textContent = mark.resolved ? "Resolved" : "Resolve";
    }
  }

  function renderFeedback() {
    const marks = Object.values(state.feedback);
    const open = marks.filter((mark) => mark.rating === "flag" && !mark.resolved).length;
    const good = marks.filter((mark) => mark.rating === "good").length;
    $("filter-flag").textContent = open ? `Flagged (${open})` : "Flagged";
    $("filter-good").textContent = good ? `Good (${good})` : "Good";
    for (const [index, node] of state.nodes) if (state.messages.get(index)?.kind === "agent") paintFeedback(node);
  }

  function applyFeedbackFilter() {
    const filter = $("feedback-filter").value;
    $("messages").dataset.filter = filter;
    $("show-resolved").hidden = filter !== "flag";
    $("messages").dataset.showResolved = String($("show-resolved").getAttribute("aria-pressed") === "true");
  }

  async function loadFeedback(slug, epoch) {
    const marks = await api(sessionPath(slug, "/feedback"));
    if (epoch !== state.epoch) return;
    state.feedback = marks && typeof marks === "object" ? marks : {};
    renderFeedback();
  }

  async function setMark(index, change) {
    const slug = state.slug;
    const epoch = state.epoch;
    const marks = await api(sessionPath(slug, "/feedback"), "POST", { i: index, ...change });
    if (epoch !== state.epoch) return;
    state.feedback = marks;
    renderFeedback();
  }

  function startReply(message) {
    state.replyTo = message.i;
    $("reply-text").textContent = (message.text || "").replace(/\s+/g, " ").slice(0, 140);
    $("reply-chip").hidden = false;
    $("message-input").focus();
  }

  function clearReply() {
    state.replyTo = null;
    $("reply-chip").hidden = true;
    $("reply-text").textContent = "";
  }

  // The model's reasoning, shown live while a turn runs and kept collapsed on the reply. Never spoken.
  function thoughtBlock(text, open) {
    const details = element("details", "thought");
    details.open = open;
    const pre = element("pre", "thought-text", text);
    // The reasoning block scrolls on its own. Reading upward pauses just this box.
    pre.addEventListener("scroll", () => {
      if (pre._pinning) return;
      pre.dataset.stick = nearBottom(pre) ? "1" : "0";
    });
    details.append(element("summary", "", open ? "Thinking" : "Thought"), pre);
    return details;
  }

  function liveTurn() {
    if (!state.live) {
      const node = element("article", "message agent live-turn");
      const heading = element("div", "message-heading");
      heading.append(element("strong", "", agentName()), element("span", "live-tool", "Waiting for model output"));
      const thought = thoughtBlock("", true);
      thought.hidden = true;
      node.append(heading, thought, element("div", "message-text live-text", ""));
      state.live = { node, thought, tool: heading.lastChild, text: node.lastChild };
    }
    if (state.live.node.parentNode !== $("messages") || state.live.node.nextSibling) $("messages").append(state.live.node);
    return state.live;
  }

  function nearBottom(scroller) {
    return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 48;
  }

  function showJumpLatest() {
    const scroller = $("conversation-scroll");
    const overflow = scroller.scrollHeight > scroller.clientHeight + 8;
    $("jump-latest").hidden = state.stickToBottom || !overflow;
  }

  function scrollToLatest() {
    const scroller = $("conversation-scroll");
    const pin = () => {
      state.programmaticScroll = true;
      scroller.scrollTop = scroller.scrollHeight;
      state.programmaticScroll = false;
    };
    // One assignment is not enough on a phone. WebKit settles the flex column a
    // frame or two after the messages are inserted, and late layout (images,
    // fonts, the resumed live turn) grows the transcript after the first pin.
    // A single scrollTop then leaves the newest line partly below the fold, and
    // the follow never corrects it because nothing streams afterwards. Re-pin
    // across the frames where that growth lands, but stop the moment the reader
    // moves the transcript themselves.
    pin();
    state.stickToBottom = true;
    showJumpLatest();
    if (typeof requestAnimationFrame !== "function") return;
    requestAnimationFrame(() => {
      if (!state.stickToBottom || state.readerScrolling) return;
      pin();
      requestAnimationFrame(() => {
        if (!state.stickToBottom || state.readerScrolling) return;
        pin();
        showJumpLatest();
      });
    });
  }

  // While a turn streams, the transcript grows many times a second. Reading
  // scrollHeight forces layout of the whole conversation, and the MutationObserver
  // fires for the same growth the delta already handled. Doing that per delta cost
  // twelve layout operations per chunk and made typing in the composer drop
  // characters. Coalesce the follow into one pin per frame instead: the newest
  // line still lands, but the browser lays the transcript out once, not three times.
  function scheduleFollow() {
    if (state.pinPending) return;
    state.pinPending = true;
    const pinNow = () => {
      state.pinPending = false;
      if (!state.stickToBottom || state.readerScrolling) { showJumpLatest(); return; }
      const scroller = $("conversation-scroll");
      state.programmaticScroll = true;
      scroller.scrollTop = scroller.scrollHeight;
      state.programmaticScroll = false;
    };
    if (typeof requestAnimationFrame === "function") requestAnimationFrame(pinNow);
    else pinNow();
  }

  // The thinking box has its own max-height. Pin it while it streams, unless the reader moved inside it.
  function followThought() {
    const thought = state.live && state.live.thought;
    if (!thought || thought.hidden) return;
    const pre = thought.lastChild;
    if (!pre || pre.dataset.stick === "0") return;
    pre._pinning = true;
    pre.scrollTop = pre.scrollHeight;
    pre._pinning = false;
  }

  // Streaming text, thinking and tool lines share this. A user scroll up pauses the transcript.
  function followLatest() {
    followThought();
    // While a finger or wheel is moving the transcript, do not pull it back to the
    // bottom: the two corrections fought each other and the screen flickered. The
    // same holds while the caret sits in the composer: incoming batches would yank
    // the page out from under the words being typed (phone chat showed exactly this).
    // Typing shows the jump-latest affordance instead.
    const composer = $("message-input");
    const composing = Boolean(composer && document.activeElement === composer);
    if (state.stickToBottom && !state.readerScrolling && !composing) scheduleFollow();
    else showJumpLatest();
  }

  function bindTranscriptFollow() {
    const scroller = $("conversation-scroll");
    const readerMoved = () => {
      state.readerScrolling = true;
      clearTimeout(state.readerScrollTimer);
      state.readerScrollTimer = setTimeout(() => { state.readerScrolling = false; }, 260);
    };
    // Any deliberate input from the reader wins over the follow for a moment.
    for (const type of ["wheel", "touchmove", "pointerdown", "keydown"]) {
      scroller.addEventListener(type, readerMoved, { passive: true });
    }
    scroller.addEventListener("scroll", () => {
      if (state.programmaticScroll) return;
      state.stickToBottom = nearBottom(scroller);
      showJumpLatest();
    });
    const messages = $("messages");
    const pin = () => followLatest();
    if (typeof ResizeObserver === "function") new ResizeObserver(pin).observe(messages);
    if (typeof MutationObserver === "function") new MutationObserver(pin).observe(messages, {childList: true, subtree: true, characterData: true});
  }

  function onDelta(data) {
    if (!data || typeof data.text !== "string") return;
    const live = liveTurn();
    if (data.kind === "heartbeat") {
      live.tool.dataset.connectedAt = data.at || new Date().toISOString();
      return;
    }
    if (data.kind !== "status" || live.tool.dataset.phase !== data.text) {
      live.tool.dataset.updatedAt = data.at || new Date().toISOString();
    }
    if (data.kind === "status") live.tool.textContent = data.text;
    if (data.kind === "thought") {
      live.tool.textContent = "Thinking";
      live.thought.hidden = false;
      live.thought.lastChild.textContent += data.text;
    } else if (data.kind === "text") {
      live.tool.textContent = "Writing";
      live.raw = (live.raw || "") + data.text;
      fillFormatted(live.text, live.raw);
    }
    else if (data.kind === "tool") live.tool.textContent = "Calling " + data.text + "...";
    else if (data.kind === "reset") { live.raw = ""; fillFormatted(live.text, ""); live.tool.textContent = "Waiting for model output"; }
    live.tool.dataset.phase = live.tool.textContent;
    followLatest();
  }

  function clearLive() {
    if (state.live) state.live.node.remove();
    state.live = null;
  }

  // A session opened mid-turn: history is replayed, the live deltas are not.
  // Rebuild the working turn from the snapshot so the reader sees the work
  // instead of a stalled-looking transcript.
  function onResume(data) {
    const snapshot = data && data.live;
    if (!data || !data.busy) {
      state.busy = false;
      clearLive();
      $("send-state").textContent = data?.operator_stopped ? "Stopped. Send a new message when you want to resume." : "";
      if (data?.operator_stopped) activity.phase = "Stopped";
      updateHeading();
      return;
    }
    state.busy = true;
    const live = liveTurn();
    live.raw = snapshot && typeof snapshot.text === "string" ? snapshot.text : "";
    if (live.raw) fillFormatted(live.text, live.raw);
    if (snapshot && snapshot.thought) {
      live.thought.hidden = false;
      live.thought.lastChild.textContent = snapshot.thought;
    }
    live.tool.textContent = snapshot?.status || (snapshot?.tool ? "Calling " + snapshot.tool + "..." : snapshot?.text ? "Writing" : snapshot?.thought ? "Thinking" : "Waiting for model output");
    live.tool.dataset.phase = live.tool.textContent;
    live.tool.dataset.updatedAt = snapshot?.at || data.started_at || new Date().toISOString();
    $("send-state").textContent = agentName() + " is working. Send steers. Queue holds the next prompt. ⌘-Enter queues.";
    updateHeading();
    followLatest();
  }

  function addMessages(messages) {
    let voiceAdded = false;
    for (const message of messages) {
      if (Number.isInteger(message.i)) {
        if (!state.messages.has(message.i) && message.meta?.voice_transcript === true) voiceAdded = true;
        if (!state.messages.has(message.i) && autonomy) autonomy.notify(message);
        state.messages.set(message.i, message);
      }
    }
    const sorted = Array.from(state.messages.values());
    sorted.sort((a, b) => a.i - b.i);
    let cursor = $("messages").firstChild;
    const hidden = autonomy ? autonomy.hiddenIndices(sorted) : new Set();
    const resolved = autonomy ? autonomy.resolvedApprovals(sorted) : new Map();
    for (const message of sorted) {
      let node = state.nodes.get(message.i);
      if ((!message.text && message.kind === "agent") || hidden.has(message.i)) {
        // A removed node cannot be an insertion point. If it was the cursor, step
        // past it before it leaves the tree, or the next insertBefore throws and
        // aborts the whole rebuild -- and a voice transcript arrives through here.
        if (node && node.parentNode) {
          if (cursor === node) cursor = node.nextSibling;
          node.remove();
        }
        continue;
      }
      const approval = autonomy && message.kind === "note" && message.meta?.approval;
      if (approval && node && node.dataset.approval !== (resolved.get(approval.id) || "pending")) {
        const replacement = autonomy.approvalNode(message, resolved);
        if (cursor === node) cursor = replacement;
        node.replaceWith(replacement);
        node = replacement;
        node.dataset.approval = resolved.get(approval.id) || "pending";
        state.nodes.set(message.i, node);
      }
      if (!node) {
        node = approval ? autonomy.approvalNode(message, resolved) : messageNode(message);
        if (approval) node.dataset.approval = resolved.get(approval.id) || "pending";
        state.nodes.set(message.i, node);
      }
      // Keep existing nodes in place, preserving keyboard focus and disclosures.
      if (node !== cursor) $("messages").insertBefore(node, cursor);
      cursor = node.nextSibling;
    }
    if (state.commitInterim && state.live) {
      state.live.raw = "";
      fillFormatted(state.live.text, "");
      state.commitInterim = false;
    }
    if (state.replaceLive) {
      clearLive();
      state.replaceLive = false;
    }
    $("empty-state").hidden = state.messages.size > 0;
    if (!state.messages.size) {
      $("empty-create").hidden = true;
    }
    followLatest();
    renderActivity();
    if (voiceAdded) queueSessionDetails(state.slug, state.epoch);
  }

  function queueSessionDetails(slug, epoch) {
    if (!slug || state.detailsTimer) return;
    // While a turn is running, the SSE stream already carries every message; the
    // five-panel refresh here adds five REST round trips per batch on top of it,
    // which is what made phone chat crawl during a busy turn. turn_end and capture
    // events refresh the panels themselves, so skipping the busy case loses nothing.
    if (state.busy) return;
    state.detailsTimer = setTimeout(() => {
      state.detailsTimer = null;
      if (epoch !== state.epoch || slug !== state.slug) return;
      Promise.all([loadMessages(slug, epoch), refreshView(slug, epoch), refreshSessions(), refreshStatus(), loadQueue(slug, epoch)]).catch(showError);
    }, 120);
  }

  // Opening a session fetches only the newest page with long tool bodies clipped; later calls fetch what is new.
  async function loadMessages(slug, epoch) {
    if (state.historyFlight) return state.historyFlight;
    if (!state.status?.ui) await refreshStatus();
    const page = Number(uiSetting("history_page"));
    const clip = Number(uiSetting("item_body_chars"));
    const flight = (async () => {
      if (!state.messages.size) {
        const rows = await api(sessionPath(slug, `/messages?tail=${page}&clip=${clip}`));
        if (epoch !== state.epoch) return;
        if (!Array.isArray(rows)) throw new Error("The server returned invalid message history.");
        state.oldest = rows.length ? rows[0].i : 0;
        addMessages(rows);
        $("load-earlier").hidden = !(state.oldest > 0);
        if (rows.length < page) return;
      }
      let after = Math.max(-1, ...state.messages.keys());
      while (epoch === state.epoch) {
        const rows = await api(sessionPath(slug, `/messages?after=${after}&limit=200&clip=${clip}`));
        if (epoch !== state.epoch) return;
        if (!Array.isArray(rows)) throw new Error("The server returned invalid message history.");
        addMessages(rows);
        if (!rows.length) break;
        const last = rows[rows.length - 1].i;
        if (!Number.isInteger(last) || last <= after) throw new Error("Message history did not advance.");
        after = last;
        if (rows.length < 200) break;
      }
    })();
    state.historyFlight = flight;
    try { await flight; }
    finally { if (state.historyFlight === flight) state.historyFlight = null; }
  }

  async function loadEarlier() {
    const slug = state.slug;
    const epoch = state.epoch;
    const page = Number(uiSetting("history_page"));
    const scroller = $("conversation-scroll");
    const shown = $("messages").children.length;
    // Wake turns and approvals are hidden, so a page can add nothing visible; keep going until it does.
    while (state.oldest > 0 && $("messages").children.length === shown) {
      const count = Math.min(page, state.oldest);
      const rows = await api(sessionPath(slug, `/messages?after=${state.oldest - count - 1}&limit=${count}&clip=${Number(uiSetting("item_body_chars"))}`));
      if (epoch !== state.epoch || !Array.isArray(rows) || !rows.length) return;
      const fromBottom = scroller.scrollHeight - scroller.scrollTop;
      state.oldest = rows[0].i;
      state.stickToBottom = false;
      addMessages(rows);
      state.programmaticScroll = true;
      scroller.scrollTop = scroller.scrollHeight - fromBottom;
      state.programmaticScroll = false;
      showJumpLatest();
    }
    $("load-earlier").hidden = !(state.oldest > 0);
  }

  async function loadFullBody(message, details) {
    const rows = await api(sessionPath(state.slug, `/messages?after=${message.i - 1}&limit=1`));
    const full = Array.isArray(rows) && rows[0];
    if (!full || full.i !== message.i) return;
    state.messages.set(full.i, full);
    const item = window.TesseraItems.describe(full, Number(uiSetting("item_summary_chars")));
    details.querySelector(".item-body").replaceWith(itemBody(item.body));
  }

  function handleEvent(type, data, id, slug, epoch) {
    if (epoch !== state.epoch || slug !== state.slug) return;
    if (id) state.lastEvent = id;
    if (type === "delta" && state.stopping === slug) return;
    observeActivity(type, data);
    // A fresh open skips replayed message bodies (the transcript fetched them) and
    // reports the newest journal id so a reconnect resumes after it instead of
    // replaying the window a second time.
    if (type === "checkpoint") return;
    if (type === "resume") { onResume(data); return; }
    if (type === "delta") {
      if (state.stopping !== slug) onDelta(data);
      return;
    }
    if (type === "message") {
      const message = data.message || data;
      const kind = message.kind;
      if (kind === "agent" && message.text) {
        if (interimReply(message) && state.live) {
          state.commitInterim = true;
        } else state.replaceLive = true;
      } else if (kind === "tool" && state.live) {
        const tool = window.TesseraItems.describe(message);
        state.live.tool.textContent = "Running " + tool.label;
        state.live.tool.dataset.phase = state.live.tool.textContent;
        state.live.tool.dataset.updatedAt = new Date().toISOString();
      }
      state.pendingMessages.push(data.message || data);
      if (!state.messageTimer) {
        state.messageTimer = setTimeout(() => {
          state.messageTimer = null;
          if (epoch !== state.epoch) return;
          const messages = state.pendingMessages;
          state.pendingMessages = [];
          addMessages(messages);
        }, 16);
      }
    }
    if (type === "turn_start") {
      state.busy = true;
      $("send-state").textContent = agentName() + " is working. Send steers. Queue holds the next prompt. ⌘-Enter queues.";
      updateHeading();
      loadQueue(slug, epoch).catch(() => {});
    }
    if (type === "steer") $("send-state").textContent = "Steer delivered. " + agentName() + " applies it at the next step.";
    // The server announces every queue change it accepts. Without this the list only
    // refreshes when this tab made the change, so a revise or remove from anywhere
    // else left a prompt on screen that was already gone from the queue.
    if (type === "queue") loadQueue(slug, epoch).catch(() => {});
    if (type === "interrupt") {
      state.stopping = slug;
      $("send-state").textContent = "Cancelling now...";
      if (state.live) {
        state.live.tool.textContent = "Cancelling now";
        state.live.tool.dataset.phase = "Cancelling now";
        state.live.tool.dataset.updatedAt = new Date().toISOString();
      }
    }
    if (type === "turn_end") {
      if (state.pendingMessages.length) {
        const messages = state.pendingMessages;
        state.pendingMessages = [];
        addMessages(messages);
      }
      clearLive();
      state.busy = false;
      state.stopping = null;
      loadQueue(slug, epoch).catch(() => {});
      updateHeading();
      $("send-state").textContent = data.interrupted ? "Stopped. Send a new message when you want to resume." : data.error ? "The turn ended with an error. See the conversation." : "Turn complete.";
      // Replayed historical turns can arrive in a burst. Refresh the panels once
      // per batch instead of issuing several requests for every historical turn.
      queueSessionDetails(slug, epoch);
    }
    if (type === "route") {
      const route = data.routing || data;
      $("route-state").textContent = "Route: " + stringify(route.route || route);
    }
    if (type === "capture") {
      state.memoryVersion.set(slug, (state.memoryVersion.get(slug) || 0) + 1);
      renderMemory({ ...sessionStatus(), ...data });
    }
    updateHeading();
  }

  function streamConnected(slug, epoch) {
    if (epoch !== state.epoch) return;
    $("connection-state").textContent = "Connected";
    renderActivity();
    // If the first REST snapshot is in flight, refresh after it completes too.
    Promise.resolve(state.historyFlight).then(() => {
      if (epoch === state.epoch) return loadMessages(slug, epoch);
    }).catch(showError);
  }

  function startStream(slug, epoch) {
    $("connection-state").textContent = "Connecting...";
    paintTrail();
    if (desktop) {
      startDesktopStream(slug, epoch);
      return;
    }
    // The transcript is fetched on the same open, so ask the stream to skip the
    // message bodies it would otherwise replay a second time.
    const source = new EventSource(withKey(sessionPath(slug, "/events?messages=skip")));
    state.stream = source;
    source.onopen = () => streamConnected(slug, epoch);
    source.onerror = (event) => {
      if (typeof event.data === "string") return;
      if (epoch !== state.epoch) return;
      $("connection-state").textContent = "Reconnecting...";
      renderActivity();
      stopVoice("Stopped: connection lost");
      watchReconnect();
      refreshStatus(true).catch(showError);
    };
    for (const type of eventTypes) {
      source.addEventListener(type, (event) => {
        if (typeof event.data !== "string") return;
        try { handleEvent(type, JSON.parse(event.data), event.lastEventId, slug, epoch); }
        catch (_) { showError(new Error("Unreadable event; refresh")); }
      });
    }
  }

  function startDesktopStream(slug, epoch) {
    let closed = false;
    const cancel = desktop.subscribe(slug, state.lastEvent, event => {
      if (closed || epoch !== state.epoch) return;
      if (event.kind === "open") streamConnected(slug, epoch);
      else if (event.kind === "event") handleEvent(event.type, event.data, event.id, slug, epoch);
      else if (event.kind === "error" || event.kind === "end") {
        closed = true;
        cancel();
        $("connection-state").textContent = "Local core unavailable";
        paintTrail();
        stopVoice("Stopped: core connection lost");
        state.retryTimer = setTimeout(() => {
          if (epoch === state.epoch) startDesktopStream(slug, epoch);
        }, 1800);
      }
    });
    state.stream = {close() { closed = true; cancel(); }};
  }

  async function refreshView(slug = state.slug, epoch = state.epoch) {
    if (!slug) return;
    const memoryEpoch = state.memoryEpoch;
    const memoryVersion = state.memoryVersion.get(slug) || 0;
    // The panel opens on the newest lines. The full view is one click away when the
    // person wants the whole prompt budget; it is not sent on every message. An older
    // core that does not report the setting still answers, and its own default applies.
    const page = optionalUiSetting("view_page_lines");
    const tail = state.viewFull.has(slug) ? 0 : (page === undefined ? null : Number(page));
    const suffix = tail === null ? "/view" : `/view?tail=${encodeURIComponent(tail)}`;
    const view = await api(sessionPath(slug, suffix));
    if (epoch !== state.epoch || memoryEpoch !== state.memoryEpoch) return;
    $("view-budget").textContent = measurementText(view.bytes, "bytes") + " in the conversation view";
    $("context-explanation").textContent = "Conversation summaries, with the original messages behind each span. Merge at "
      + measurementText(view.high, "bytes") + "; settle toward " + measurementText(view.low, "bytes") + ". This is the conversation view, not the entire model prompt.";
    const memory = { ...(state.memory.get(slug) || {}) };
    for (const field of ["inheritance", "debt", "write_debt"]) {
      if (field !== "inheritance" && memoryVersion !== (state.memoryVersion.get(slug) || 0)) continue;
      if (view[field] !== undefined) memory[field] = view[field];
    }
    state.memory.set(slug, memory);
    renderMemory();
    const viewLines = $("view-lines");
    const renderKey = JSON.stringify([slug, view.lines, view.omitted]);
    if (viewLines.renderKey === renderKey) return;
    viewLines.renderKey = renderKey;
    const nodes = [];
    const previous = new Map();
    for (const node of viewLines.children) if (node.dataset.span) previous.set(node.dataset.span, node);
    const lines = view.lines || [];
    if (view.omitted > 0) {
      const omitted = view.omitted;
      const more = element("button", "view-line view-more",
        `${omitted} older line${omitted === 1 ? "" : "s"} omitted, show the whole view`);
      more.type = "button";
      more.addEventListener("click", () => {
        state.viewFull.add(slug);
        refreshView(slug, epoch).catch(showError);
      });
      nodes.push(more);
    }
    for (let position = 0; position < lines.length; position += 1) {
      const line = lines[position];
      const spanKey = JSON.stringify([slug, line.start, line.count, line.text]);
      const old = previous.get(spanKey);
      if (old) {
        const age = line.age_rank === undefined ? lines.length - position - 1 : line.age_rank;
        old.dataset.age = age > 0 && age >= Math.ceil(lines.length / 2) ? "old" : "recent";
        nodes.push(old);
        continue;
      }
      const wrapper = element("div", "view-line");
      wrapper.dataset.span = spanKey;
      const age = line.age_rank === undefined ? lines.length - position - 1 : line.age_rank;
      wrapper.dataset.age = age > 0 && age >= Math.ceil(lines.length / 2) ? "old" : "recent";
      const button = element("button", "view-open");
      button.type = "button";
      button.setAttribute("aria-expanded", "false");
      const span = line.count === 1 ? "Original message " + line.start
        : "Summary of messages " + line.start + " to " + (line.start + line.count - 1);
      button.append(element("small", "", span + " · " + measurementText(line.bytes, "bytes")), element("span", "", line.text));
      const zoom = element("div", "zoom-content");
      zoom.hidden = true;
      button.addEventListener("click", () => {
        const open = button.getAttribute("aria-expanded") !== "true";
        button.setAttribute("aria-expanded", String(open));
        zoom.hidden = !open;
        if (open && zoom.dataset.loaded !== "true") loadZoom(slug, line.start, line.count, zoom, epoch).catch(showError);
      });
      wrapper.append(button, zoom);
      nodes.push(wrapper);
    }
    if (!lines.length) nodes.push(element("p", "muted", "The view will grow with this conversation."));
    replaceChangedChildren(viewLines, nodes);
  }

  async function loadZoom(slug, start, count, container, epoch) {
    container.textContent = "Loading...";
    const result = await api(sessionPath(slug, `/zoom?start=${encodeURIComponent(start)}&count=${encodeURIComponent(count)}`));
    if (epoch !== state.epoch) return;
    container.replaceChildren();
    container.dataset.loaded = "true";
    if (result.lines && Array.isArray(result.lines)) {
      for (const line of result.lines) {
        const button = element("button", "", `${line.start}+${line.count} | ${line.text}`);
        button.type = "button";
        const nested = element("div");
        button.addEventListener("click", () => loadZoom(slug, line.start, line.count, nested, epoch).catch(showError));
        container.append(button, nested);
      }
    } else container.append(element("pre", "", result.text === undefined ? stringify(result) : result.text));
  }

  function sessionStatus() {
    const sessions = state.status.sessions || {};
    const cached = state.memory.get(state.slug) || {};
    if (Array.isArray(sessions)) return { ...cached, ...(sessions.find((session) => session.slug === state.slug) || {}) };
    return { ...cached, ...(sessions[state.slug] || {}) };
  }

  function renderMemory(status = sessionStatus()) {
    if (state.slug) state.memory.set(state.slug, status);
    const inheritance = status.inheritance || status.inheritance_digest || (status.memory && status.memory.inheritance);
    $("inheritance-digest").textContent = state.slug ? (inheritance ? stringify(inheritance) : "No inheritance digest is available for this session.") : "Select a session to inspect its memory.";
    const debt = status.debt === undefined ? status.write_debt : status.debt;
    const count = Array.isArray(debt) ? debt.length : debt;
    const queue = captureQueue(count);
    setIntegrationBadge("memory-capture-state", queue.label, queue.tone);
    $("write-debt").textContent = state.slug ? queue.detail : "Select a session to inspect its capture queue.";
  }

  async function browseMemory(scope) {
    const slug = state.slug;
    const epoch = state.epoch;
    if (!slug) return;
    const notice = $("memory-read-state");
    const content = $("memory-live-content");
    notice.textContent = "Reading AetherMind…";
    content.replaceChildren();
    const buttons = [$("browse-session-memory"), $("browse-personal-memory")];
    for (const button of buttons) button.disabled = true;
    try {
      const response = await api(sessionPath(slug, "/memory?scope=" + encodeURIComponent(scope)));
      if (epoch !== state.epoch) return;
      const tree = response.tree;
      if (tree?.error) throw new Error(tree.error);
      notice.textContent = tree?.state === "no_tree"
        ? tree.message : "Live " + (scope === "personal" ? "personal continuity" : "session memory") + " read.";
      const provenance = element("p", "memory-provenance", (scope === "personal" ? "Personal continuity" : "Session memory")
        + " · read " + new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}));
      content.append(provenance);
      const layers = tree?.recall?.layers || tree?.layers;
      if (Array.isArray(layers)) {
        for (const layer of layers) {
          const record = element("details", "memory-record");
          record.append(element("summary", "", [layer.type, layer.ctx, layer.id].filter(Boolean).join(" · ")),
            element("pre", "digest", layer.body || stringify(layer)));
          const provenance = [];
          for (const key of ["author", "timestamp", "confidence", "conf", "corrects", "supersedes", "rollback_of"]) {
            const value = layer[key];
            if (value !== undefined && value !== null && value !== "" && (!Array.isArray(value) || value.length)) provenance.push(key + ": " + stringify(value));
          }
          if (provenance.length) record.append(element("p", "memory-provenance", provenance.join(" · ")));
          const source = element("details", "memory-source");
          source.append(element("summary", "", "Original record"), element("pre", "digest", stringify(layer)));
          record.append(source);
          record.dataset.search = stringify(layer).toLowerCase();
          content.append(record);
        }
        if (!layers.length) content.append(element("p", "inspector-note", "The store returned no layers."));
      } else {
        const view = typeof tree === "string" ? tree : tree?.view;
        if (typeof view === "string") {
          for (const row of window.TesseraItems.memoryRows(view)) content.append(memoryBranch(row, scope, slug, epoch));
        }
        const source = element("details", "memory-source");
        source.append(element("summary", "", "Original response"), element("pre", "digest", stringify(tree)));
        content.append(source);
      }
      $("memory-record-search").disabled = false;
      filterMemoryRecords();
      renderMemoryTreeStatus();
    } catch (error) {
      if (epoch === state.epoch) notice.textContent = "Memory read: " + (error.message || "unavailable");
    } finally {
      for (const button of buttons) button.disabled = false;
    }
  }

  function memoryBranch(row, scope, slug, epoch) {
    const record = element("details", "memory-record");
    record.dataset.search = row.text.toLowerCase();
    const summary = element("summary", "", row.label || "Memory node");
    record.append(summary, element("p", "memory-excerpt", row.text));
    if (Number.isInteger(row.level) && Number.isInteger(row.index)) {
      const button = element("button", "", row.level === 0 ? "Read original records" : "Expand memory branch");
      button.type = "button";
      const children = element("div", "memory-children");
      record.append(button, children);
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          const result = await api(sessionPath(slug, "/memory?scope=" + scope + "&level=" + row.level + "&index=" + row.index));
          if (epoch !== state.epoch) return;
          const tree = result.tree;
          if (tree?.error) throw new Error(tree.error);
          children.replaceChildren();
          for (const child of tree.children || []) children.append(memoryBranch({...child, label: "Level " + child.level + ", node " + child.index}, scope, slug, epoch));
          for (const layer of tree.layers || []) {
            const original = element("details", "memory-record");
            original.append(element("summary", "", "Original record " + (layer.id || "")), element("pre", "digest", stringify(layer)));
            children.append(original);
          }
          if (!children.children.length && tree.level === 0 && typeof tree.text === "string") {
            children.append(element("h4", "", tree.cut ? "Leaf record excerpt" : "Leaf record"), element("pre", "digest", tree.text));
            const source = element("details", "memory-source");
            source.append(element("summary", "", "Original response"), element("pre", "digest", stringify(tree)));
            children.append(source);
            if (tree.cut) children.append(element("p", "inspector-note", "AetherMind marked this record as cut. This excerpt is not the complete source."));
          }
          if (!children.children.length) children.append(element("p", "inspector-note", "The store returned no children or source records for this node."));
          button.hidden = true;
        } catch (error) {
          children.textContent = error.message || "Memory branch unavailable";
          button.disabled = false;
        }
      });
    }
    return record;
  }

  function filterMemoryRecords() {
    const query = $("memory-record-search").value.trim().toLowerCase();
    for (const record of $("memory-live-content").querySelectorAll(".memory-record")) record.hidden = Boolean(query && !String(record.dataset.search || record.textContent).toLowerCase().includes(query));
  }

  function captureQueue(count) {
    if (!nonnegativeInteger(count)) return {label: "Not reported", detail: "Capture counts are not reported.", tone: ""};
    if (count === 0) return {label: "Empty", detail: "0 unsent captures reported. An empty queue does not prove delivery or recall.", tone: ""};
    return {label: "Queued", detail: `${count} unsent captures reported. Queued does not mean delivered. Writes wait for their configured destination; this page does not send or retry them.`, tone: "attention"};
  }

  // Approval mode: two explicit segments, painted only from the mode the core reports it enforces.
  function renderApproval(approval) {
    const group = $("approval-mode");
    const valid = approval && typeof approval === "object" && typeof approval.mode === "string";
    const yolo = Boolean(valid && approval.yolo);
    group.hidden = !valid || !approval.switchable;
    group.dataset.mode = valid ? approval.mode : "";
    $("approval-safe").textContent = valid && approval.safe_mode === "ask" ? "Ask" : "Safe";
    $("approval-safe").setAttribute("aria-checked", String(valid && !yolo));
    $("approval-yolo").setAttribute("aria-checked", String(yolo));
    paintApprovalLabel(state.status?.ui);
    group.title = valid ? "Core enforces: " + approval.mode : "";
    const banner = yolo && state.status?.ui?.yolo_banner !== false;
    $("yolo-banner").hidden = !banner;
    document.body.classList.toggle("yolo-on", banner);
    if (valid) {
      state.approvalShown = yolo;
      const mode = document.getElementById("setting-permissions_mode");
      const next = yolo ? "yolo" : "safe";
      if (mode && document.activeElement !== mode && mode.value !== next) mode.value = next;
      if (configView) configView.noteApproval(approval);
      // The shelf says whether an off switch stops a call. That sentence is
      // false the moment Safe and Unleashed trade places, so read the list again.
      const enforced = approval.mode;
      if (shelfView && state.approvalMode && state.approvalMode !== enforced) shelfView.load().catch(() => {});
      state.approvalMode = enforced;
    }
  }

  // Another device or the Settings page may switch the mode; a tiny poll keeps the pill honest.
  async function pollApproval() {
    const epoch = state.approvalEpoch;
    try {
      const approval = await api("/api/approval");
      if (epoch === state.approvalEpoch && approval?.yolo !== state.approvalShown) renderApproval(approval);
    } catch (_) { /* The status poll reports an unreachable core. */ }
  }

  function scheduleApprovalPoll() {
    clearTimeout(state.approvalTimer);
    const every = Number(state.status?.ui?.approval_poll_ms);
    if (!(every > 0)) return;
    state.approvalTimer = setTimeout(async () => {
      if (!document.hidden) await pollApproval();
      scheduleApprovalPoll();
    }, every);
  }

  async function setYolo(enable) {
    if ($((enable ? "approval-yolo" : "approval-safe")).getAttribute("aria-checked") === "true") return;
    // No native dialog: Tauri's WKWebView has no handler for one, so it answers no and the click did nothing.
    // A status poll that started before this switch must not repaint the old mode afterwards.
    state.approvalEpoch += 1;
    const buttons = [$("approval-safe"), $("approval-yolo")];
    for (const button of buttons) button.disabled = true;
    try {
      renderApproval(await api("/api/approval", "POST", { yolo: enable }));
    } finally {
      state.approvalEpoch += 1;
      for (const button of buttons) button.disabled = false;
    }
  }

  async function refreshStatus(connectionOnly = false) {
    state.statusEpoch += 1;
    const epoch = state.statusEpoch;
    const approvalEpoch = state.approvalEpoch;
    let status;
    try {
      status = await api(connectionOnly ? "/api/status?shape=connection" : "/api/status");
      if (!status || typeof status !== "object" || Array.isArray(status)) throw new Error("Invalid status");
    } catch (error) {
      if (epoch !== state.statusEpoch) return;
      statusUnavailable();
      // Status errors can contain private provider details. Do not echo them.
      throw new Error("Server unreachable");
    }
    if (epoch !== state.statusEpoch) return;
    if (reloadForNewBuild(status.ui_build)) return;
    state.status = connectionOnly ? {...state.status, ...status} : status;
    if (approvalEpoch === state.approvalEpoch) renderApproval(status.approval);
    applyReportedAppearance(status.ui);
    if (!state.approvalTimer) scheduleApprovalPoll();
    if (state.voiceNotice?.label === "Status unavailable") state.voiceNotice = null;
    const list = $("status-list");
    list.replaceChildren();
    const reportedNames = statusBackendNames(status.backends);
    const names = reportedNames || [];
    const memory = memoryStatus(status.aethermind).connection;
    const jev = typeof status.jev === "object" && status.jev !== null ? status.jev.enabled : status.jev;
    let totalDebt = status.debt_total;
    if (totalDebt === undefined) totalDebt = status.write_debt;
    if (!nonnegativeInteger(totalDebt)) totalDebt = "Not reported";
    const entries = [
      ["Selected backend", backendLabel(activeSession()?.backend)],
      ["Session", sessionStatus().busy ? "Working" : "Ready"],
      ["Active agents", sessionStatus().active_agents ?? "Not reported"],
      ["Queued turns", sessionStatus().queued ?? "Not reported"],
      ["Jev routing", jev === true ? "On" : jev === false ? "Off" : "Not reported"],
      ["Reasoning effort", activeSession()?.reasoning || "low"],
      ["Thinking budget", activeSession()?.thinking_budget ? activeSession().thinking_budget.toLocaleString() + " tokens per response" : "Engine default"],
      ["AetherMind", memory.label],
      ["Pending captures, all sessions", totalDebt],
    ];
    for (const [label, value] of entries) list.append(element("dt", "", label), element("dd", "", value));
    const backendSelect = $("session-backend");
    const selected = backendSelect.value;
    backendSelect.replaceChildren(element("option", "", "Use default"));
    backendSelect.firstChild.value = "";
    // Utility/structured-output backends (chat = false) are never offered as chat brains.
    const chatNames = Array.isArray(status.chat_backends) ? names.filter((name) => status.chat_backends.includes(name)) : names;
    for (const name of chatNames) {
      const option = element("option", "", modelLabel(name));
      option.value = name;
      backendSelect.append(option);
    }
    backendSelect.value = selected;
    state.chatNames = chatNames;
    renderModelPickerOnce();
    updateHeading();
    renderMemory();
    renderServerSettings(names, status);
    renderVoiceSessions();
    renderMemoryTreeStatus();
    if (!connectionOnly) renderDetailedStatus();
    paintTrail();
    renderVoiceStatus();
    // The sidebar's unreadable-session notice comes from this payload, and this
    // poll is the only place it arrives. Without the call the notice would only
    // appear after an unrelated session-list refresh.
    renderSessions();
  }

  function renderDetailedStatus() {
    renderTaskStatus();
    renderJobStatus();
    renderBoardStatus();
    renderInfrastructureStatus();
  }

  function statusBackendNames(value) {
    if (!value || typeof value !== "object") return null;
    if (!Array.isArray(value)) return Object.keys(value);
    const names = [];
    for (const backend of value) {
      if (typeof backend === "string") names.push(backend);
      else if (backend && typeof backend.name === "string") names.push(backend.name);
      else return null;
    }
    return names;
  }

  function providerRecords(value) {
    if (!Array.isArray(value)) return null;
    for (const record of value) {
      if (!record || typeof record !== "object" || Array.isArray(record)) return null;
    }
    return value;
  }

  function statusUnavailable() {
    delete $("board-cards").renderKey;
    delete $("infrastructure-hosts").renderKey;
    watchReconnect();
    stopVoice("Stopped: server unreachable");
    state.status = {};
    state.memoryEpoch += 1;
    state.memory.clear();
    renderMemory({});
    const unavailableLabel = "Status unavailable";
    setIntegrationBadge("memory-capture-state", unavailableLabel, "attention");
    $("write-debt").textContent = "Capture counts are unavailable.";
    const message = "Server unreachable";
    $("settings-connection").textContent = message;
    $("settings-session-count").textContent = "? sessions";
    $("status-list").replaceChildren(element("dt", "", "Server"), element("dd", "", message));
    setIntegrationBadge("tasks-state", "Status unavailable", "attention");
    $("tasks-summary").textContent = "Unavailable";
    $("tasks-records").replaceChildren(element("li", "quiet-note", "No tasks"));
    clearTaskFilters();
    setIntegrationBadge("jobs-state", unavailableLabel, "attention");
    $("jobs-summary").textContent = "Unavailable";
    $("jobs-records").replaceChildren(element("li", "quiet-note", "No jobs"));
    clearJobOverview();
    setIntegrationBadge("memory-tree-state", "Status unavailable", "attention");
    $("memory-tree-summary").textContent = "Tree status unavailable.";
    $("memory-tree-metadata").replaceChildren();
    boardUnavailable();
    infrastructureUnavailable();
    state.voiceNotice = {label: "Status unavailable", detail: "Unavailable"};
    renderVoiceStatus();
    paintTrail();
  }

  function stopVoice(detail) {
    if (voice) voice.stop(detail);
    if (localVoice) localVoice.cancel();
  }

  // The session the Voice page talks through: the reader's pick if they made one,
  // otherwise the dedicated voice session, otherwise the open chat as a fallback
  // on a home that has no voice session yet. This is why Talk does not depend on
  // which chat happens to be open.
  function voiceSession() {
    const live = state.sessions.filter((session) => !session.archived);
    const wanted = state.voiceTarget;
    if (wanted) {
      const picked = live.find((session) => session.slug === wanted);
      if (picked) return picked;
    }
    return live.find((session) => session.voice) || activeSession();
  }

  function renderVoiceStatus() {
    const report = state.status.voice;
    const local = report?.pipe === "local";
    const localReady = report && report.enabled === true && local && report.pipes?.local === true;
    const available = report && report.enabled === true && report.configured === true;
    const ready = local ? localReady : available;
    const active = voice?.running() === true || localVoice?.running() === true;
    const session = voiceSession();
    if ((!ready || !session || session.archived) && active) stopVoice("Stopped");
    let label = "Not reported";
    let detail = "";
    if (report?.enabled === false) label = "Off";
    else if (local && !localReady) { label = "Not configured"; detail = "Add speech services in Settings"; }
    else if (!local && report?.configured === false) { label = "Not configured"; detail = "No voice credential on server"; }
    else if (ready) {
      label = "Ready";
      if (!session) detail = "Pick a session";
      else if (session.archived) detail = "Session archived";
      else if (local) detail = "Hold to talk";
    }
    if (state.voiceNotice && (ready || state.voiceNotice.label === "Status unavailable")) {
      label = state.voiceNotice.label;
      detail = state.voiceNotice.detail;
    }
    for (const id of ["voice-state", "voice-page-state"]) {
      $(id).textContent = label;
      $(id).dataset.state = active ? "active" : "";
    }
    for (const id of ["voice-detail", "voice-page-detail"]) $(id).textContent = detail;
    document.querySelector(".voice-console")?.classList.toggle("voice-live", active);
    document.querySelector(".voice-console").dataset.phase = label.toLowerCase();
    renderVoicePicker();
    applyVoiceMode();
    if (active && !state.voiceDrawing && typeof requestAnimationFrame === "function") {
      state.voiceDrawing = true;
      requestAnimationFrame(() => { state.voiceDrawing = false; drawVoice(); });
    }
    if (!active) $("voice-level").value = 0;
    $("voice-page-toggle").setAttribute("aria-pressed", voice?.running() ? "true" : "false");
    if ($("voice-session")) $("voice-session").disabled = active;
    for (const id of ["voice-toggle", "voice-page-toggle"]) {
      $(id).hidden = local;
      $(id).textContent = voice?.running() ? "Stop" : id === "voice-page-toggle" ? "Talk" : "Voice";
      $(id).disabled = !voice?.running() && (!available || !session || session.archived);
    }
  }

  function infrastructureUnavailable() {
    state.enginePressureEpoch = (state.enginePressureEpoch || 0) + 1;
    renderEnginePressure(null);
    for (const id of ["infrastructure-state"]) setIntegrationBadge(id, "Status unavailable", "attention");
    $("infrastructure-summary").textContent = "Unavailable";
    $("infrastructure-hosts").replaceChildren(element("li", "quiet-note", "No hosts"));
    clearInfraChrome();
  }

  function clearInfraChrome() {
    state.targetsReport = null;
    for (const id of ["infrastructure-kpis", "infra-models", "infra-grafana", "infra-activity"]) $(id).replaceChildren();
  }

  function clearBoardChrome() {
    $("wip-activity").replaceChildren();
  }

  function knownLabel(labels, value, fallback = "Not reported") {
    return typeof value === "string" && Object.hasOwn(labels, value) ? labels[value] : fallback;
  }

  function renderJobStatus() {
    const report = state.status.job_status;
    const valid = report && typeof report === "object" && !Array.isArray(report);
    const list = $("jobs-records");
    list.replaceChildren();
    clearJobOverview();
    let label = "Not reported";
    let detail = "Not reported";
    if ((valid && report.enabled === false) || (!valid && state.status.task_status?.enabled === false)) {
      label = "Off";
      detail = "Disabled in config.toml";
      list.append(element("li", "quiet-note", "Set swarm.enabled in config.toml; Settings does not expose this lane."));
    } else if (valid && report.configured === false) {
      // Settings exposes no swarm section, so a Configure button here is a dead
      // end: it navigates to a page that cannot change anything this panel reads.
      // The payload says only that the lane is unconfigured, not why, so the
      // note names where to set it and does not guess at the cause.
      label = "Not configured";
      detail = "Configured in config.toml";
      list.append(element("li", "quiet-note", "Settings does not expose the jobs lane; set swarm in config.toml."));
    } else if (valid && report.enabled === true && report.configured === true) {
      const jobs = providerRecords(report.jobs);
      if (jobs) {
        label = jobs.length ? "Snapshot" : "Empty";
        detail = jobs.length ? `${jobs.length} ${jobs.length === 1 ? "job" : "jobs"}` : "No jobs";
        // job_limit is a bound, not a filter. Same silence as the task rows.
        if (Number.isInteger(report.jobs_omitted) && report.jobs_omitted > 0) {
          detail += ` · ${report.jobs_omitted} older jobs not shown (${report.job_limit}-job limit)`;
        }
        renderJobOverview(jobs);
        for (const record of orderedJobs(jobs)) list.append(jobNode(record.job, record.index));
      } else detail = "No snapshot yet";
    }
    setIntegrationBadge("jobs-state", label);
    $("jobs-summary").textContent = detail;
    if (!list.children.length) list.append(element("li", "quiet-note", "No jobs"));
  }

  function settingsButton(label) {
    const button = element("button", "ds-text-button", label);
    button.type = "button";
    button.addEventListener("click", () => setPage("settings"));
    return button;
  }

  function clearJobOverview() {
    $("jobs-overview").replaceChildren();
    $("jobs-overview").hidden = true;
  }

  function orderedJobs(jobs) {
    const order = ["running", "blocked", "queued", "failed", "succeeded", "cancelled"];
    const records = [];
    for (let index = 0; index < jobs.length; index += 1) {
      const rank = order.indexOf(jobs[index].status);
      records.push({job: jobs[index], index, rank: rank < 0 ? order.length : rank});
    }
    records.sort((left, right) => left.rank - right.rank || left.index - right.index);
    return records;
  }

  function renderJobOverview(jobs) {
    if (!jobs.length) return;
    const counts = new Map();
    for (const job of jobs) {
      const label = knownLabel(boardLabels, job.status);
      counts.set(label, (counts.get(label) || 0) + 1);
    }
    const overview = $("jobs-overview");
    for (const label of [...Object.values(boardLabels), "Not reported"]) {
      if (counts.has(label)) overview.append(metricChip(label, counts.get(label)));
    }
    overview.hidden = false;
  }

  function jobNode(job, index) {
    const row = element("li", "board-card");
    const ordinal = Number.isSafeInteger(job.ordinal) && job.ordinal > 0 ? job.ordinal : index + 1;
    const heading = element("div", "board-card-heading");
    const badge = element("span", "status-badge", knownLabel(boardLabels, job.status));
    badge.dataset.state = job.status === "succeeded" ? "ready" : ["blocked", "failed"].includes(job.status) ? "attention" : "";
    heading.append(statusMark(badge.dataset.state), element("h3", "", "Job " + ordinal), badge);
    row.append(heading);
    if (typeof job.limit_reason === "string" && job.limit_reason && !job.limit_reason.includes("/")) {
      row.append(element("p", "quiet-note", job.limit_reason));
    }
    const counts = element("dl", "host-metrics");
    const overCount = nonnegativeInteger(job.worker_count) && nonnegativeInteger(job.completed_count) && nonnegativeInteger(job.failed_count) && job.completed_count + job.failed_count > job.worker_count;
    for (const [key, label] of [["worker_count", "Fan-out"], ["completed_count", "Completed"], ["failed_count", "Failed"], ["artifact_count", "Artifacts"]]) {
      let count = job[key];
      if (["completed_count", "failed_count"].includes(key) && (overCount || (nonnegativeInteger(job.worker_count) && count > job.worker_count))) count = null;
      counts.append(metricChip(label, nonnegativeInteger(count) ? String(count) : "Not reported"));
    }
    counts.append(metricChip("Merge", knownLabel({pending: "Pending", succeeded: "Succeeded", blocked: "Blocked", failed: "Failed"}, job.merge_status)));
    counts.append(metricChip("Gate", knownLabel({not_checked: "Not checked", allowed: "Allowed", blocked: "Blocked"}, job.gate_status)));
    const providers = jobProviderCount(job);
    counts.append(metricChip("Providers", providers === null ? "Not reported" : String(providers)));
    row.append(counts);
    if (!overCount && nonnegativeInteger(job.worker_count) && job.worker_count > 0 && nonnegativeInteger(job.completed_count) && nonnegativeInteger(job.failed_count)) {
      const finished = job.completed_count + job.failed_count;
      row.append(element("p", "quiet-note", `${finished}/${job.worker_count} workers done`));
      const progress = element("progress", "job-progress");
      progress.setAttribute("max", job.worker_count);
      progress.setAttribute("value", finished);
      progress.setAttribute("aria-label", "Workers with saved outcomes");
      row.append(progress);
    }
    renderJobWorkers(row, job);
    return row;
  }

  function renderJobWorkers(row, job) {
    row.append(element("h4", "", "Workers"));
    const workers = providerRecords(job.workers);
    const ordinals = new Set();
    let valid = workers !== null;
    for (const worker of workers || []) {
      if (!Number.isSafeInteger(worker.ordinal) || worker.ordinal <= 0 || ordinals.has(worker.ordinal)) valid = false;
      ordinals.add(worker.ordinal);
    }
    if (workers && nonnegativeInteger(job.worker_count) && workers.length > job.worker_count) valid = false;
    if (!valid) {
      row.append(element("p", "quiet-note", "No worker details"));
      return;
    }
    if (!workers.length) {
      row.append(element("p", "quiet-note", "No worker records are reported in this snapshot."));
      return;
    }
    const list = element("ul", "job-workers");
    list.setAttribute("aria-label", "Reported worker states");
    for (const worker of workers) {
      const item = element("li", "job-worker");
      const waiting = typeof worker.status === "string" && (worker.status === "waiting" || worker.status.startsWith("waiting for "));
      const badge = element("span", "status-badge", waiting && worker.status.startsWith("waiting for ") ? worker.status : knownLabel(taskLabels, worker.status));
      badge.dataset.state = worker.status === "succeeded" ? "ready" : worker.status === "failed" ? "attention" : waiting ? "waiting" : "";
      item.append(statusMark(badge.dataset.state), element("span", "job-worker-label", "Worker " + worker.ordinal), badge);
      const attempt = nonnegativeInteger(worker.attempt) ? String(worker.attempt) : "Not reported";
      item.append(element("span", "job-worker-detail", "Attempt " + attempt));
      if (Object.hasOwn(worker, "gate_status")) {
        item.append(element("span", "job-worker-detail", "Gate: " + knownLabel({not_checked: "Not checked", allowed: "Allowed", blocked: "Blocked"}, worker.gate_status)));
      }
      const provider = Number.isSafeInteger(worker.provider_ordinal) && worker.provider_ordinal > 0 ? "Provider " + worker.provider_ordinal : "Provider not reported";
      item.append(element("span", "job-worker-detail", provider));
      if (Number.isSafeInteger(worker.account_ordinal) && worker.account_ordinal > 0) {
        item.append(element("span", "job-worker-detail", "Account " + worker.account_ordinal));
      }
      list.append(item);
    }
    row.append(list);
    if (nonnegativeInteger(job.worker_count) && workers.length < job.worker_count) {
      row.append(element("p", "quiet-note", "Worker details cover only " + workers.length + " of " + job.worker_count + " workers in this snapshot."));
    }
  }

  function jobProviderCount(job) {
    const count = job.provider_count;
    if (!Number.isSafeInteger(count) || count <= 0) return null;
    if (nonnegativeInteger(job.worker_count) && count > job.worker_count) return null;
    const workers = providerRecords(job.workers);
    const providers = new Set();
    let complete = workers !== null && workers.length === job.worker_count;
    for (const worker of workers || []) {
      if (!Number.isSafeInteger(worker.provider_ordinal) || worker.provider_ordinal <= 0) complete = false;
      else providers.add(worker.provider_ordinal);
    }
    if (providers.size > count || (complete && providers.size !== count)) return null;
    return count;
  }

  function memoryStatus(report) {
    const connection = {label: "Not reported", detail: "Not reported", tone: ""};
    const tree = {
      label: "Not reported", detail: "Not reported",
      presence: "Not reported", view: "Not reported", zoom: "Not reported",
    };
    const snapshot = {connection, tree};
    if (!report || typeof report !== "object" || Array.isArray(report)) return snapshot;
    const processUrl = typeof report.process === "string" && report.process ? report.process : "";
    // 'present' without a returned process is a checkout, not a running engine.
    const embeddedStarted = report.active === "embedded" && (processUrl || report.engine !== "present");
    if (embeddedStarted || report.active === "external") {
      const embedded = report.active === "embedded";
      connection.label = embedded ? "Embedded" : "External";
      connection.detail = typeof report.reason === "string" && report.reason ? report.reason : connection.label;
      if (embedded && processUrl && !connection.detail.includes(processUrl)) {
        connection.detail = connection.detail + " (" + processUrl + ")";
      }
      if (showMemoryError(connection, report)) {
        connection.tone = "attention";
      } else if (report.active === "external" && report.reachable === false) {
        connection.tone = "attention";
        connection.detail = "Unreachable";
      } else if (report.reachable === true) {
        const observed = report.tree_status;
        if (observed && typeof observed === "object" && !Array.isArray(observed)) {
          tree.view = memoryAvailability(observed.view_available);
          tree.zoom = memoryAvailability(observed.zoom_available);
          if (observed.present === false) {
            tree.presence = "Not present";
          } else if (observed.present === true) {
            tree.presence = "Present";
          }
        }
      }
      if (!embedded && typeof report.embedded_stop === "string" && report.embedded_stop
          && !connection.detail.includes(report.embedded_stop)) {
        connection.detail = connection.detail + " (" + report.embedded_stop + ")";
      }
      tree.label = connection.label;
      tree.detail = connection.detail;
      return snapshot;
    }
    if (report.engine === "present" && !processUrl) {
      connection.label = "bytes present, not started";
      connection.detail = typeof report.reason === "string" && report.reason
        ? report.reason
        : "This checkout has no build receipt, so the embedded engine is not started";
      tree.label = connection.label;
      tree.detail = connection.detail;
      tree.view = "Unavailable · " + connection.label;
      tree.zoom = tree.view;
      if (showMemoryError(connection, report)) tree.detail = connection.detail;
      return snapshot;
    }
    let unavailable = false;
    if (report.configured === false) {
      if (report.lock_backend === "unavailable") {
        connection.label = "Cannot write";
        connection.detail = typeof report.lock_detail === "string" && report.lock_detail
          ? report.lock_detail
          : "Cannot write";
        connection.tone = "attention";
      } else if (report.engine === "missing") {
        connection.label = "Engine missing";
        connection.detail = "No embedded AetherMind engine in this install";
      } else if (report.engine === "bundled") {
        connection.label = "Embedded engine";
        connection.detail = "Embedded engine is the default; no external service is configured";
      } else {
        connection.label = "Not configured";
        connection.detail = "Not configured";
      }
      unavailable = true;
    } else if (report.configured === true) {
      if (["authentication", "permission"].includes(report.error_category)) {
        connection.label = "Auth required";
        connection.detail = "Access denied";
        connection.tone = "attention";
        unavailable = true;
      } else if (report.reachable === false) {
        connection.label = "Unreachable";
        connection.detail = "Unreachable";
        connection.tone = "attention";
        unavailable = true;
      } else if (report.reachable === true) {
        connection.label = "Connected";
        connection.detail = "Connected";
        const observed = report.tree_status;
        if (observed && typeof observed === "object" && !Array.isArray(observed)) {
          tree.view = memoryAvailability(observed.view_available);
          tree.zoom = memoryAvailability(observed.zoom_available);
          if (observed.present === false) {
            tree.label = "No tree yet";
            tree.presence = "Not present";
            tree.detail = "No tree yet";
            connection.detail = tree.detail;
            if (observed.view_available === true) tree.view = "Not reported";
            if (observed.zoom_available === true) tree.zoom = "Not reported";
          } else if (observed.present === true) {
            tree.label = "Present";
            tree.presence = "Present";
            tree.detail = "Tree present";
            connection.detail = tree.detail;
          }
        }
      } else {
        connection.label = "Configured";
        connection.detail = "Configured; not checked yet";
      }
    }
    if (unavailable) {
      tree.label = connection.label;
      tree.detail = connection.detail;
      tree.view = "Unavailable · " + connection.label;
      tree.zoom = tree.view;
    }
    if (showMemoryError(connection, report)) tree.detail = connection.detail;
    return snapshot;
  }

  function showMemoryError(connection, report) {
    const text = typeof report.external_error === "string" ? report.external_error : "";
    if (!text || connection.detail.includes(text)) return false;
    connection.detail = connection.detail + " (" + text + ")";
    connection.tone = "attention";
    return true;
  }

  function memoryAvailability(value) {
    if (value === true) return "Available";
    if (value === false) return "Unavailable";
    return "Not reported";
  }

  function memoryErrorLabel(report) {
    if (!report || typeof report.last_error !== "string" || report.last_error === "") return "";
    return report.last_error;
  }

  function renderMemoryTreeStatus() {
    const report = state.status.aethermind;
    const {connection, tree} = memoryStatus(report);
    setIntegrationBadge("memory-tree-state", tree.label, connection.tone);
    $("memory-tree-summary").textContent = tree.detail;
    const metadata = $("memory-tree-metadata");
    metadata.replaceChildren();
    const rows = [["Connection", connection.label], ["Tree", tree.presence], ["View", tree.view], ["Zoom", tree.zoom]];
    for (const [name, value] of rows) {
      metadata.append(element("dt", "", name), element("dd", "", value));
    }
    const errorLabel = memoryErrorLabel(report);
    if (errorLabel) metadata.append(element("dt", "", "Last error"), memoryErrorValue(report, errorLabel));
    const button = element("button", "", "Open engine log");
    button.type = "button";
    button.addEventListener("click", () => openEngineLog());
    const cell = element("dd", "");
    cell.append(button);
    metadata.append(element("dt", "", "Engine log"), cell);
  }

  function memoryErrorValue(report, errorLabel) {
    const category = report && typeof report.error_category === "string" ? report.error_category.trim() : "";
    const text = category && !errorLabel.includes(category) ? category + ": " + errorLabel : errorLabel;
    return element("dd", "", text);
  }

  function engineLogMessage(report) {
    if (report && typeof report.error === "string" && report.error) return report.error;
    if (report && typeof report.reason === "string" && report.reason) return report.reason;
    if (!report || report.available === false) {
      const path = report && typeof report.path === "string" ? report.path : "";
      return path ? "The engine log is missing: " + path + "." : "The engine log is missing.";
    }
    if (typeof report.text === "string" && report.text) return report.text;
    return "The engine log is empty.";
  }

  function engineLogCap(report) {
    return report && typeof report.cap === "number" && Number.isFinite(report.cap)
      ? "Engine log cap: " + report.cap + " bytes.\n" : "";
  }

  async function openEngineLog() {
    const metadata = $("memory-tree-metadata");
    try {
      const report = await api("/api/aethermind/engine-log");
      let slot = document.getElementById("engine-log");
      if (!slot) {
        slot = element("pre", "engine-log");
        slot.id = "engine-log";
        metadata.parentElement.append(slot);
      }
      slot.textContent = engineLogCap(report) + engineLogMessage(report);
    } catch (error) {
      let slot = document.getElementById("engine-log");
      if (!slot) {
        slot = element("pre", "engine-log");
        slot.id = "engine-log";
        metadata.parentElement.append(slot);
      }
      slot.textContent = error && error.message ? error.message : "Engine log could not be opened";
    }
  }

  function finiteMeasurement(value, maximum = Number.MAX_SAFE_INTEGER) {
    return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= maximum;
  }

  function measurementText(value, unit) {
    const maximum = unit === "%" ? 100 : Number.MAX_SAFE_INTEGER;
    if (!finiteMeasurement(value, maximum)) return "Not reported";
    if (["count", "bytes"].includes(unit) && !Number.isSafeInteger(value)) return "Not reported";
    if (unit === "bytes") {
      for (const [scale, label] of [[2 ** 40, "TiB"], [2 ** 30, "GiB"], [2 ** 20, "MiB"], [2 ** 10, "KiB"]]) {
        if (value >= scale) return value / scale >= 10 ? Math.round(value / scale) + " " + label : (value / scale).toFixed(1) + " " + label;
      }
      return Math.round(value) + " B";
    }
    if (unit === "s") return durationText(value);
    if (unit === "load") return value.toFixed(1);
    if (unit === "%") return Math.round(value) + "%";
    const number = Number.isInteger(value) ? String(value) : value.toFixed(1);
    return number + (["count", "load"].includes(unit) ? "" : unit === "%" ? "%" : " " + unit);
  }

  function metricChip(label, value, ratio, tone) {
    const chip = element("div", "host-metric");
    chip.append(element("dt", "", label), element("dd", "", value));
    if (window.TesseraDesign && typeof ratio === "number" && Number.isFinite(ratio)) chip.append(window.TesseraDesign.meter(ratio, tone || ""));
    return chip;
  }

  function statusMark(tone) {
    if (window.TesseraDesign) return window.TesseraDesign.statusDot(tone);
    const dot = element("span", "ds-dot");
    if (tone) dot.dataset.tone = tone;
    dot.setAttribute("aria-hidden", "true");
    return dot;
  }

  function healthTone(health) {
    if (health === "healthy") return "ready";
    if (health === "unreachable") return "danger";
    if (health === "degraded" || health === "stale") return "attention";
    return "";
  }

  function hostMetrics(host) {
    return host.metrics && typeof host.metrics === "object" && !Array.isArray(host.metrics) ? host.metrics : {};
  }

  function percentRatio(value) {
    return finiteMeasurement(value, 100) ? value / 100 : null;
  }

  function loadRatioFor(metrics) {
    const load = metrics.cpu_load1;
    const cores = metrics.cpu_count;
    if (!finiteMeasurement(load) || !finiteMeasurement(cores) || cores <= 0) return null;
    return Math.min(1, load / cores);
  }

  function sparkSample(metrics) {
    if (finiteMeasurement(metrics.cpu_percent, 100)) return metrics.cpu_percent;
    if (finiteMeasurement(metrics.cpu_load1)) return metrics.cpu_load1;
    return null;
  }

  function sparkSampleKey(ordinal, metrics) {
    if (finiteMeasurement(metrics.cpu_percent, 100)) return ordinal + ":cpu";
    return ordinal + ":load";
  }

  function shareRatio(part, whole) {
    if (!finiteMeasurement(part) || !finiteMeasurement(whole) || whole <= 0) return null;
    return Math.min(1, part / whole);
  }

  function usageTone(ratio) {
    if (ratio > 0.9) return "danger";
    if (ratio >= 0.7) return "warn";
    return "ok";
  }

  function byteField(metrics, key) {
    if (!Object.hasOwn(metrics, key)) return {present: false, value: null};
    const text = measurementText(metrics[key], "bytes");
    return {present: true, value: text === "Not reported" ? null : metrics[key]};
  }

  function storageChip(metrics, label, usedKey, totalKey, freeKey) {
    const used = byteField(metrics, usedKey);
    const total = byteField(metrics, totalKey);
    const free = byteField(metrics, freeKey);
    if (!used.present && !total.present && !free.present) return null;
    let usedBytes = used.value;
    let freeBytes = free.value;
    const totalBytes = total.value;
    if (usedBytes === null && totalBytes !== null && freeBytes !== null && freeBytes <= totalBytes) usedBytes = totalBytes - freeBytes;
    if (freeBytes === null && totalBytes !== null && usedBytes !== null && usedBytes <= totalBytes) freeBytes = totalBytes - usedBytes;
    const ratio = shareRatio(usedBytes, totalBytes);
    let value = "Not reported";
    if (usedBytes !== null && totalBytes !== null) {
      value = measurementText(usedBytes, "bytes") + " of " + measurementText(totalBytes, "bytes");
      if (freeBytes !== null) value += " · " + measurementText(freeBytes, "bytes") + " free";
    } else if (freeBytes !== null) value = measurementText(freeBytes, "bytes") + " free";
    else if (usedBytes !== null) value = measurementText(usedBytes, "bytes");
    return metricChip(label, value, ratio, ratio === null ? "" : usageTone(ratio));
  }

  function activeHosts(hosts) {
    return hosts.filter((host) => host && typeof host === "object" && host.enabled !== false && host.configured !== false);
  }

  // Aggregate the hosts that reported this metric, and say how many of how many did.
  // One host that reports only uptime must not blank Memory, CPU and Disk.
  function metricSeries(hosts, key, unit) {
    const maximum = unit === "%" ? 100 : Number.MAX_SAFE_INTEGER;
    const values = [];
    for (const host of hosts) {
      const value = hostMetrics(host)[key];
      if (finiteMeasurement(value, maximum)) values.push(value);
    }
    return values.length ? values : null;
  }

  function fleetValues(hosts, read) {
    const values = [];
    for (const host of hosts) {
      const value = read(host);
      if (value !== null && value !== undefined) values.push(value);
    }
    return values.length ? values : null;
  }

  function coverage(reported, total) {
    return total > reported ? reported + " of " + total + " hosts" : "";
  }

  function infraSeries(key, value) {
    if (typeof value !== "number" || !Number.isFinite(value)) return null;
    const series = state.infraSamples[key] || [];
    if (series[series.length - 1] !== value) {
      series.push(value);
      if (series.length > 24) series.splice(0, series.length - 24);
      state.infraSamples[key] = series;
    }
    if (!window.TesseraDesign || series.length < 2) return null;
    return window.TesseraDesign.sparkline(series);
  }

  function infrastructureHostNode(host, index, revision) {
    const row = element("li", "board-card");
    const ordinal = Number.isSafeInteger(host.ordinal) && host.ordinal > 0 ? host.ordinal : index + 1;
    const heading = element("div", "board-card-heading");
    const identity = element("div");
    const hostName = typeof host.label === "string" && host.label.trim() ? host.label.trim().slice(0, 80) : "Host " + ordinal;
    let health = host.health;
    if (host.enabled === false) health = "disabled";
    else if (host.configured === false) health = "unconfigured";
    const kind = element("span", "host-kind ds-pill", knownLabel(hostKinds, host.kind, "Kind not reported"));
    const nameRow = element("div", "host-name-row");
    nameRow.append(element("h3", "", hostName), kind);
    identity.append(nameRow);
    const badge = element("span", "status-badge", knownLabel(hostHealth, health, "Health not reported"));
    badge.dataset.state = health === "healthy" ? "ready" : ["degraded", "unreachable", "stale"].includes(health) ? "attention" : "";
    heading.append(statusMark(healthTone(health)), identity, badge);
    row.append(heading);
    if (host.enabled === false || host.configured === false) {
      row.classList.add("host-compact");
      row.append(settingsButton(host.enabled === false ? "Enable" : "Configure"));
      return row;
    }
    const age = measurementText(host.age_seconds, "s");
    if (age !== "Not reported") row.append(element("p", "quiet-note", "Updated " + age + " ago"));
    const category = knownLabel(infraErrors, host.error_category, "");
    if (category) row.append(element("p", "quiet-note", category));
    const metrics = hostMetrics(host);
    const chips = element("dl", "host-metrics");
    if (Object.hasOwn(metrics, "uptime_seconds")) chips.append(metricChip("Uptime", measurementText(metrics.uptime_seconds, "s")));
    if (Object.hasOwn(metrics, "cpu_percent")) {
      const cpuRatio = percentRatio(metrics.cpu_percent);
      const cpuTone = cpuRatio === null ? "" : usageTone(metrics.cpu_percent / 100);
      chips.append(metricChip("CPU", measurementText(metrics.cpu_percent, "%"), cpuRatio, cpuTone));
    }
    if (Object.hasOwn(metrics, "cpu_count")) chips.append(metricChip("CPU cores", measurementText(metrics.cpu_count, "count")));
    if (Object.hasOwn(metrics, "cpu_load1")) {
      const loadRatio = loadRatioFor(metrics);
      const loadTone = loadRatio === null ? "" : usageTone(loadRatio);
      chips.append(metricChip("Load (1 min)", measurementText(metrics.cpu_load1, "load"), loadRatio, loadTone));
    }
    const memory = storageChip(metrics, "Memory", "memory_used_bytes", "memory_total_bytes", "memory_available_bytes");
    const disk = storageChip(metrics, "Disk", "disk_used_bytes", "disk_total_bytes", "disk_free_bytes");
    if (memory) chips.append(memory);
    if (disk) chips.append(disk);
    row.append(chips);
    const sample = sparkSample(metrics);
    const sparkKey = sparkSampleKey(ordinal, metrics);
    const spark = sample === null ? null : infraSeries(sparkKey, sample);
    if (spark) row.append(spark);
    const gpus = providerRecords(host.gpu);
    if (gpus && gpus.length) {
      for (let gpuIndex = 0; gpuIndex < gpus.length; gpuIndex += 1) {
        const gpu = gpus[gpuIndex];
        const gpuOrdinal = Number.isSafeInteger(gpu.ordinal) && gpu.ordinal > 0 ? gpu.ordinal : gpuIndex + 1;
        const label = "GPU " + gpuOrdinal;
        chips.append(metricChip(label + " use", measurementText(gpu.utilization_percent, "%"), percentRatio(gpu.utilization_percent), ""));
        const gpuMemory = shareRatio(gpu.memory_used_bytes, gpu.memory_total_bytes);
        for (const [key, suffix, tone] of [["memory_used_bytes", "memory used", "mem"], ["memory_total_bytes", "capacity", ""]]) {
          if (Object.hasOwn(gpu, key)) chips.append(metricChip(label + " " + suffix, measurementText(gpu[key], "bytes"), key === "memory_used_bytes" ? gpuMemory : null, tone));
        }
        if (Object.hasOwn(gpu, "temperature_celsius")) {
          const temperature = typeof gpu.temperature_celsius === "number" && Number.isFinite(gpu.temperature_celsius) && Math.abs(gpu.temperature_celsius) <= Number.MAX_SAFE_INTEGER ? gpu.temperature_celsius + " °C" : "Not reported";
          chips.append(metricChip(label + " temperature", temperature));
        }
      }
    }
    const services = providerRecords(host.services);
    const serviceList = element("ul", "board-artifacts");
    serviceList.setAttribute("aria-label", "Reported services");
    if (services && services.length) {
      for (let serviceIndex = 0; serviceIndex < services.length; serviceIndex += 1) {
        const service = services[serviceIndex];
        const serviceOrdinal = Number.isSafeInteger(service.ordinal) && service.ordinal > 0 ? service.ordinal : serviceIndex + 1;
        serviceList.append(element("li", "", "Service " + serviceOrdinal + ": " + knownLabel({running: "Running", stopped: "Stopped", unknown: "Unknown"}, service.state)));
      }
      row.append(serviceList);
    }
    if (typeof revision === "string" && /^[a-f0-9]{64}$/.test(revision) && host.enabled !== false && host.configured !== false && Number.isSafeInteger(host.ordinal) && host.ordinal > 0) {
      const button = element("button", "", "Collect");
      button.type = "button";
      button.addEventListener("click", () => collectHost(host.ordinal, revision, button));
      row.append(button);
    }
    return row;
  }

  async function collectHost(ordinal, revision, button) {
    button.disabled = true;
    try {
      const report = await api("/api/infrastructure/collect", "POST", {ordinal, revision});
      state.status.infrastructure_status = report;
      renderInfrastructureStatus();
    } catch (error) {
      if (error.status === 401 || error.status === 403) {
        state.statusEpoch += 1;
        statusUnavailable();
      } else if (error.status === 409) $("infrastructure-summary").textContent = "Snapshot changed. Refresh before collecting.";
      else $("infrastructure-summary").textContent = "Collect failed";
    } finally {
      button.disabled = false;
    }
  }

  // Writing a new card from the page. Distinct from addBoardNote(ordinal, note), which
  // appends a note to a card that already exists.
  let workIntake = null;
  async function submitBoardNote() {
    const title = $("board-note-what").value.trim();
    const note = $("board-note-body").value.trim();
    const project = $("board-note-project").value;
    const button = $("board-note-submit");
    const status = $("board-note-status");
    if (!title) {
      status.textContent = "Say what needs doing.";
      return;
    }
    button.disabled = true;
    status.textContent = "Adding…";
    try {
      const report = workIntake ? await workIntake.prepare() : {note, evidence:[]};
      const reply = await api("/api/board/cards", "POST", {title, note:report.note, project, ...(report.evidence.length ? {evidence:report.evidence} : {})});
      if ($("board-note-what").value.trim() === title) $("board-note-what").value = "";
      if (workIntake) workIntake.saved(report);
      else if ($("board-note-body").value.trim() === note) $("board-note-body").value = "";
      status.textContent = project ? "Card added to the selected space." : "Card added. Awaiting filing.";
      if (reply && reply.view) applyBoardView(reply.view);
    } catch (error) {
      status.textContent = "Could not add it: " + error.message;
    } finally {
      button.disabled = false;
    }
  }

  async function refreshEnginePressure() {
    const epoch = (state.enginePressureEpoch || 0) + 1;
    state.enginePressureEpoch = epoch;
    let report = null;
    try {
      report = await api("/api/pressure/engines");
    } catch (error) {
      // Older cores have no pressure route. Keep the rest of the page usable.
    }
    if (epoch !== state.enginePressureEpoch) return;
    renderEnginePressure(report);
  }

  function enginePressureNode(engine) {
    const row = element("li", "board-card engine-pressure-card");
    const heading = element("div", "board-card-heading");
    const identity = element("div");
    identity.append(element("h3", "", engine.host + ":" + engine.port));
    identity.append(element("p", "quiet-note", engine.model));
    const reachable = engine.reachable === true;
    const badge = element("span", "status-badge", reachable ? "Reachable" : "UNREACHABLE");
    row.dataset.reachable = String(reachable);
    heading.append(statusMark(reachable ? "ready" : ""), identity, badge);
    row.append(heading);
    if (!reachable) {
      const error = typeof engine.error === "string" && engine.error ? engine.error : "No reading available";
      row.append(element("p", "quiet-note", error));
      return row;
    }
    const chips = element("dl", "host-metrics");
    const used = measurementText(engine.running, "count") + "/" + measurementText(engine.slots_total, "count") + " used";
    chips.append(metricChip("Slots", used));
    chips.append(metricChip("Waiting", measurementText(engine.waiting, "count")));
    chips.append(metricChip("Free", measurementText(engine.free, "count")));
    row.append(chips);
    return row;
  }

  function renderEnginePressure(report) {
    const list = $("engine-pressure-list");
    const engines = providerRecords(report?.engines);
    const renderKey = JSON.stringify(engines);
    if (list.renderKey === renderKey) return;
    list.renderKey = renderKey;
    list.replaceChildren();
    if (!engines) {
      list.append(element("li", "quiet-note", "Engine pressure not available"));
      return;
    }
    for (const engine of engines) list.append(enginePressureNode(engine));
    if (!engines.length) list.append(element("li", "quiet-note", "No engines configured"));
  }

  function renderInfrastructureStatus() {
    let report = state.status.infrastructure_status;
    if (!report || typeof report !== "object" || Array.isArray(report)) report = {};
    const list = $("infrastructure-hosts");
    const renderKey = JSON.stringify(report);
    if (list.renderKey === renderKey) return;
    list.renderKey = renderKey;
    list.replaceChildren();
    let label = "Not reported";
    let summary = "Not reported";
    let hosts = null;
    if (report.enabled === false) {
      label = "Off";
      summary = "Off";
    } else if (report.configured === false) {
      label = "Not configured";
      summary = "No hosts configured";
    } else if (report.enabled === true && report.configured === true) {
      hosts = providerRecords(report.hosts);
      if (hosts) {
        label = hosts.length ? "Live" : "Empty";
        summary = hosts.length ? fleetSummary(hosts) : "No hosts";
        for (let index = 0; index < hosts.length; index += 1) list.append(infrastructureHostNode(hosts[index], index, report.revision));
      } else summary = "No snapshot yet";
    }
    const collectionErrors = Array.isArray(report.collection_errors) ? report.collection_errors : [];
    if (collectionErrors.length || (typeof report.collector_error === "string" && report.collector_error)) {
      label = "Collection failed";
      const notes = collectionErrors.map((row) => (row.host || "host") + ": " + (row.error || "unreported"));
      if (typeof report.collector_error === "string" && report.collector_error) notes.push(report.collector_error);
      summary = notes.join("; ");
      for (const note of notes) list.append(element("li", "quiet-note", note));
    }
    setIntegrationBadge("infrastructure-state", label);
    $("infrastructure-summary").textContent = summary;
    if (!list.children.length) list.append(element("li", "quiet-note", "No hosts"));
    paintInfrastructure(hosts);
  }

  function fleetSummary(hosts) {
    const parts = [`${hosts.length} ${hosts.length === 1 ? "host" : "hosts"}`];
    const active = activeHosts(hosts);
    if (active.length) {
      const healthy = active.filter((host) => host.health === "healthy").length;
      parts.push(healthy + "/" + active.length + " healthy");
      const free = metricSeries(active, "memory_available_bytes", "bytes");
      if (free) {
        const note = coverage(free.length, active.length);
        parts.push(measurementText(free.reduce((sum, value) => sum + value, 0), "bytes") + " free" + (note ? " (" + note + ")" : ""));
      }
      const cpus = metricSeries(active, "cpu_percent", "%");
      if (cpus) {
        const note = coverage(cpus.length, active.length);
        parts.push("CPU " + Math.round(cpus.reduce((sum, value) => sum + value, 0) / cpus.length) + "%" + (note ? " (" + note + ")" : ""));
      }
      else {
        const loads = metricSeries(active, "cpu_load1", "load");
        if (loads) parts.push("load " + Math.max(...loads).toFixed(1));
      }
    }
    return parts.join(" · ");
  }

  function paintInfrastructure(hosts) {
    paintInfraKpis(hosts);
    paintModels();
    paintGrafana();
    paintInfraActivity(hosts);
  }

  function bytesParts(bytes) {
    const text = measurementText(bytes, "bytes");
    const split = text.lastIndexOf(" ");
    if (split <= 0) return {value: text, unit: ""};
    return {value: text.slice(0, split), unit: text.slice(split + 1)};
  }


  function memoryAccount(host) {
    const metrics = hostMetrics(host);
    const total = byteField(metrics, "memory_total_bytes").value;
    let used = byteField(metrics, "memory_used_bytes").value;
    let free = byteField(metrics, "memory_available_bytes").value;
    if (used === null && total !== null && free !== null && free <= total) used = total - free;
    if (free === null && total !== null && used !== null && used <= total) free = total - used;
    if (used === null || free === null || total === null) return null;
    return {used, free, total};
  }

  function diskAccount(host) {
    const metrics = hostMetrics(host);
    const total = byteField(metrics, "disk_total_bytes").value;
    let used = byteField(metrics, "disk_used_bytes").value;
    let free = byteField(metrics, "disk_free_bytes").value;
    if (used === null && total !== null && free !== null && free <= total) used = total - free;
    if (free === null && total !== null && used !== null && used <= total) free = total - used;
    if (used === null || total === null) return null;
    return {used, total};
  }

  // Fleet health is higher-is-better, so usageTone is the wrong helper for it:
  // that would paint 5/5 healthy danger red and 0/5 ok green. Full health is ok,
  // any unhealthy host is at least a warning, and less than half healthy is bad.
  // Named for the ratio, not "healthTone": that name already maps a host's
  // health string to a status tone (app.js:2606) and must keep doing so.
  function fleetHealthTone(ratio) {
    if (ratio >= 1) return "ok";
    if (ratio >= 0.5) return "warn";
    return "danger";
  }

  // tone is optional and defaults to the consumption reading: a ratio passed
  // without a tone is treated as a fill level, which is right for memory, CPU
  // and disk but wrong for health.
  function kpiTile(label, value, unit, ratio, seriesKey, seriesValue, note, tone) {
    const tile = window.TesseraDesign.kpi(label, value, unit || "");
    if (typeof ratio === "number" && Number.isFinite(ratio)) {
      const bounded = Math.max(0, Math.min(1, ratio));
      tile.append(window.TesseraDesign.meter(bounded, tone || usageTone(bounded)));
    }
    const spark = typeof seriesValue === "number" ? infraSeries(seriesKey, seriesValue) : null;
    if (spark) tile.append(spark);
    if (note) tile.append(element("div", "ds-meta", note));
    return tile;
  }

  // "Unknown" says nothing useful. Say what is missing and who did not report it.
  function unreportedTile(label, subject, activeCount) {
    const tile = window.TesseraDesign.kpi(label, "Not reported");
    tile.append(element("div", "ds-meta", activeCount ? "No active host reports " + subject : "No active hosts"));
    return tile;
  }

  function paintInfraKpis(hosts) {
    const slot = $("infrastructure-kpis");
    slot.replaceChildren();
    if (!window.TesseraDesign) return;
    if (!hosts) {
      for (const label of ["Hosts", "Memory", "CPU", "Disk"]) slot.append(unreportedTile(label, "any measurement", 0));
      return;
    }
    const active = activeHosts(hosts);
    const healthy = active.filter((host) => host.health === "healthy").length;
    const hostRatio = active.length ? healthy / active.length : null;
    slot.append(kpiTile("Hosts", active.length ? healthy + "/" + active.length : String(hosts.length), active.length ? "healthy" : "hosts", hostRatio, null, null, null, hostRatio == null ? "" : fleetHealthTone(hostRatio)));

    const memory = active.length ? fleetValues(active, memoryAccount) : null;
    if (memory) {
      const free = memory.reduce((sum, row) => sum + row.free, 0);
      const used = memory.reduce((sum, row) => sum + row.used, 0);
      const total = memory.reduce((sum, row) => sum + row.total, 0);
      const parts = bytesParts(free);
      slot.append(kpiTile("Memory", parts.value, parts.unit ? parts.unit + " free" : "free",
                          shareRatio(used, total), "kpi:memory", free, coverage(memory.length, active.length)));
    } else slot.append(unreportedTile("Memory", "memory", active.length));

    const cpus = active.length ? metricSeries(active, "cpu_percent", "%") : null;
    if (cpus) {
      const average = cpus.reduce((sum, value) => sum + value, 0) / cpus.length;
      slot.append(kpiTile("CPU", measurementText(average, "%"), "", average / 100, "kpi:cpu", average,
                          coverage(cpus.length, active.length)));
    } else {
      const loads = active.length ? metricSeries(active, "cpu_load1", "load") : null;
      if (loads) {
        const load = Math.max(...loads);
        const ratios = fleetValues(active, (host) => {
          const metrics = hostMetrics(host);
          if (!finiteMeasurement(metrics.cpu_load1) || !finiteMeasurement(metrics.cpu_count) || metrics.cpu_count <= 0) return null;
          return Math.min(1, metrics.cpu_load1 / metrics.cpu_count);
        });
        const ratio = ratios ? ratios.reduce((sum, value) => sum + value, 0) / ratios.length : null;
        slot.append(kpiTile("Load", load.toFixed(1), "", ratio, "kpi:load", load, coverage(loads.length, active.length)));
      } else slot.append(unreportedTile("CPU", "CPU load", active.length));
    }

    const disks = active.length ? fleetValues(active, diskAccount) : null;
    if (disks) {
      const used = disks.reduce((sum, row) => sum + row.used, 0);
      const total = disks.reduce((sum, row) => sum + row.total, 0);
      const parts = bytesParts(used);
      slot.append(kpiTile("Disk", parts.value, parts.unit, shareRatio(used, total), "kpi:disk", used,
                          coverage(disks.length, active.length)));
    } else {
      const usedOnly = active.length ? metricSeries(active, "disk_used_bytes", "bytes") : null;
      if (usedOnly) {
        const used = usedOnly.reduce((sum, value) => sum + value, 0);
        const parts = bytesParts(used);
        slot.append(kpiTile("Disk", parts.value, parts.unit, null, "kpi:disk", used, coverage(usedOnly.length, active.length)));
      } else slot.append(unreportedTile("Disk", "disk", active.length));
    }
  }

  function authLine(record) {
    const id = typeof record.id === "string" ? record.id : "";
    const row = id ? credentialOf(id) : null;
    if (row && typeof row.label === "string" && row.label) return row.label;
    if (record.credential === "not_required") return "Not required";
    if (record.credential === "available") return "Credential available";
    return "";
  }

  function paintModels() {
    const list = $("infra-models");
    list.replaceChildren();
    const report = state.status.provider_status;
    const backends = report && typeof report === "object" && !Array.isArray(report) ? providerRecords(report.backends) : null;
    if (!backends) {
      list.append(element("li", "ds-empty", "Not reported"));
      return;
    }
    if (!backends.length) {
      list.append(element("li", "ds-empty", "No models"));
      return;
    }
    backends.forEach((record, index) => {
      const row = element("li", "compact-row");
      const name = typeof record.id === "string" && record.id.trim() && record.id.trim().length <= 80 && !/[\u0000-\u001f]/.test(record.id) ? record.id.trim() : "Model " + (index + 1);
      const health = record.health === "healthy" ? "Healthy" : record.health === "error" ? "Error" : record.health === "not_checked" ? "Not checked" : "Not reported";
      const badge = element("span", "status-badge", health);
      badge.dataset.state = record.health === "healthy" ? "ready" : record.health === "error" ? "attention" : "";
      row.append(statusMark(badge.dataset.state), element("h3", "", name), badge);
      const auth = authLine(record);
      if (auth) row.append(element("span", "ds-meta", auth));
      if (record.configured === false || record.credential === "missing") row.append(settingsButton("Configure"));
      list.append(row);
    });
  }

  function paintGrafana() {
    const slot = $("infra-grafana");
    slot.replaceChildren();
    const report = state.status.grafana;
    if (!report || typeof report !== "object" || Array.isArray(report)) {
      slot.append(element("p", "ds-empty", "Not reported"));
      return;
    }
    const row = element("div", "compact-row");
    let label = "Not reported";
    let tone = "";
    if (report.enabled === false) label = "Off";
    else if (report.configured === false) label = "Not configured";
    else if (report.enabled === true && report.configured === true) {
      label = knownLabel({healthy: "Healthy", unavailable: "Unavailable", not_checked: "Not checked"}, report.health);
      tone = report.health === "healthy" ? "ready" : report.health === "unavailable" ? "attention" : "";
    }
    const badge = element("span", "status-badge", label);
    if (tone) badge.dataset.state = tone;
    row.append(statusMark(tone), element("h3", "", "Grafana"), badge);
    const counts = [];
    if (Number.isSafeInteger(report.dashboard_count) && report.dashboard_count >= 0) counts.push(report.dashboard_count + (report.dashboard_count === 1 ? " dashboard" : " dashboards"));
    if (Number.isSafeInteger(report.datasource_count) && report.datasource_count >= 0) {
      // list_limit is a bound, not a filter. Without this the visible count reads as
      // the whole set.
      const omitted = Number.isSafeInteger(report.datasource_omitted) && report.datasource_omitted > 0 ? report.datasource_omitted : 0;
      counts.push(report.datasource_count + (report.datasource_count === 1 ? " datasource" : " datasources")
        + (omitted ? ` (${omitted} more not shown)` : ""));
    }
    if (counts.length) row.append(element("span", "ds-meta", counts.join(" · ")));
    if (report.enabled === false || report.configured === false) row.append(settingsButton(report.enabled === false ? "Enable" : "Configure"));
    slot.append(row);
  }

  function paintInfraActivity(hosts) {
    const slot = $("infra-activity");
    slot.replaceChildren();
    if (!window.TesseraDesign) return;
    const items = [];
    for (const host of hosts || []) {
      if (!host || typeof host !== "object") continue;
      const ordinal = Number.isSafeInteger(host.ordinal) && host.ordinal > 0 ? host.ordinal : items.length + 1;
      const name = typeof host.label === "string" && host.label.trim() ? host.label.trim().slice(0, 80) : "Host " + ordinal;
      let health = host.health;
      if (host.enabled === false) health = "disabled";
      else if (host.configured === false) health = "unconfigured";
      items.push({
        tone: healthTone(health),
        time: finiteMeasurement(host.age_seconds) ? durationText(host.age_seconds) : "",
        text: name + " · " + knownLabel(hostHealth, health, "Health not reported"),
        age: finiteMeasurement(host.age_seconds) ? host.age_seconds : Number.MAX_SAFE_INTEGER,
      });
    }
    items.sort((left, right) => left.age - right.age);
    const report = state.status.provider_status;
    const backends = report && typeof report === "object" && !Array.isArray(report) ? providerRecords(report.backends) : null;
    for (const record of backends || []) {
      if (record.health !== "error") continue;
      const name = typeof record.id === "string" && record.id.trim() && record.id.trim().length <= 80 ? record.id.trim() : "Model";
      items.push({tone: "attention", time: "", text: name + " · Error", age: Number.MAX_SAFE_INTEGER});
    }
    const targets = state.targetsReport;
    if (targets && typeof targets === "object" && !Array.isArray(targets) && Array.isArray(targets.targets)) {
      for (const target of targets.targets) {
        if (!target || target.health === "up") continue;
        const job = typeof target.job === "string" && target.job.trim() ? target.job.trim().slice(0, 80) : "Target";
        items.push({tone: "danger", time: "", text: job + " · Down", age: Number.MAX_SAFE_INTEGER});
      }
    }
    const visible = items.slice(0, 8).map(({tone, time, text}) => ({tone, time, text}));
    slot.append(window.TesseraDesign.activityFeed(visible, visible.some((item) => item.time)));
  }

  function boardUnavailable() {
    state.boardRevision = null;
    state.boardView = null;
    setIntegrationBadge("board-state", "Status unavailable", "attention");
    $("board-summary").textContent = "Unavailable";
    $("board-cards").replaceChildren(element("li", "quiet-note", "Unavailable"));
    clearBoardChrome();
  }

  // Session quality is the oversight lane's reading of how sessions actually behaved.
  // It is deliberately NOT ranked by the judge's completion score: measured against
  // live config, the heuristic can only return 0.0/0.1/0.65/0.9 and its threshold sits
  // above every passing value, so a completion ranking would sort noise. Structure is
  // what a reader can act on: did the tools work, did a judge actually answer, and did
  // a reply claim more than the evidence shows.
  const QUALITY_POSTURE = {
    steady: {tone: "ready", text: "Steady"},
    "claims-unreliable": {tone: "danger", text: "Claims outran evidence"},
    "turn-failures": {tone: "danger", text: "Turns failed"},
    "judge-degraded": {tone: "attention", text: "Judge fell back"},
    unjudged: {tone: "", text: "Nothing graded"},
  };

  function qualityPosture(posture) {
    return QUALITY_POSTURE[posture] || {tone: "", text: typeof posture === "string" && posture.trim() ? posture : "Not graded"};
  }

  function qualityCount(value) {
    return nonnegativeInteger(value) ? value : 0;
  }

  function renderQuality() {
    const findingsSlot = $("quality-findings");
    const sessionsSlot = $("quality-sessions");
    const kpiSlot = $("quality-kpis");
    const limitsSlot = $("quality-limits");
    const limitsFold = $("quality-limits-fold");
    const report = state.qualityReport;
    findingsSlot.replaceChildren();
    sessionsSlot.replaceChildren();
    if (kpiSlot) kpiSlot.replaceChildren();
    if (limitsSlot) limitsSlot.replaceChildren();
    if (limitsFold) limitsFold.hidden = true;
    if (!report || typeof report !== "object" || Array.isArray(report)) {
      setIntegrationBadge("quality-state", state.qualityReading ? "Reading" : "Not read");
      $("quality-summary").textContent = state.qualityReading
        ? "Reading the newest turns of every session…"
        : "Read the newest turns of every session to see how they behaved.";
      return;
    }
    if (report.enabled === false) {
      setIntegrationBadge("quality-state", "Off");
      $("quality-summary").textContent = "The oversight lane is switched off in settings.";
      return;
    }
    const totals = report.totals && typeof report.totals === "object" && !Array.isArray(report.totals) ? report.totals : {};
    const sessions = Array.isArray(report.sessions) ? report.sessions.filter((row) => row && typeof row === "object") : [];
    const graded = qualityCount(totals.graded);
    const judged = qualityCount(totals.judge_answered);
    const refused = qualityCount(totals.refused_deterministic);
    const unsupported = qualityCount(totals.claims_unsupported);
    const calls = qualityCount(totals.tool_calls);
    const nonzero = qualityCount(totals.tool_nonzero);
    setIntegrationBadge("quality-state", sessions.length ? sessions.length + (sessions.length === 1 ? " session" : " sessions") : "No sessions",
      unsupported || refused ? "attention" : "");
    // The counts were a run-on sentence and every finding was a paragraph set as a
    // heading, so eleven rows read as one block of prose before a reader reached the
    // HUD-like view. The numbers belong in tiles a reader can take in at a glance.
    const postures = report.posture_counts && typeof report.posture_counts === "object" && !Array.isArray(report.posture_counts)
      ? report.posture_counts : {};
    const attention = qualityCount(postures["claims-unreliable"]) + qualityCount(postures["turn-failures"])
      + qualityCount(postures["judge-degraded"]) + qualityCount(postures["tool-failures"]);
    const steady = qualityCount(postures.steady);
    const unjudged = qualityCount(postures.unjudged) + qualityCount(postures.unreadable);
    // The design system is optional: a page without it still renders the section,
    // and the headless harness that drives this function has no window at all.
    const design = typeof window === "undefined" ? null : window.TesseraDesign;
    if (design && kpiSlot) {
      kpiSlot.append(design.kpi("Sessions", String(sessions.length)));
      kpiSlot.append(design.kpi("Need attention", String(attention)));
      kpiSlot.append(design.kpi("Steady", String(steady)));
      kpiSlot.append(design.kpi("Nothing graded", String(unjudged)));
    }
    // The summary states the split between a judge that answered and a check that
    // refused before the reply was read. Those are different facts and a single
    // "pass rate" would blend them.
    const parts = [];
    if (graded) parts.push(graded + " graded turn" + (graded === 1 ? "" : "s"));
    if (judged) parts.push(judged + " judged");
    if (refused) parts.push(refused + " refused before judging");
    if (calls) parts.push(nonzero + "/" + calls + " tool calls returned nonzero");
    $("quality-summary").textContent = parts.length ? parts.join(" · ") : "No graded turns in any session yet.";

    // A finding leads with its headline; the paragraph is available, not compulsory.
    // `findings_detail` is the structured form. A core that predates it still sends
    // `findings` as strings, so that path keeps working rather than showing nothing.
    const details = Array.isArray(report.findings_detail) ? report.findings_detail.filter((item) => item && typeof item === "object") : [];
    const findings = details.length ? details : (Array.isArray(report.findings)
      ? report.findings.filter((text) => typeof text === "string" && text.trim())
        .map((text) => ({kind: "", headline: text, detail: "", tone: "attention"})) : []);
    for (const finding of findings) {
      const headline = typeof finding.headline === "string" && finding.headline.trim() ? finding.headline.trim() : "";
      if (!headline) continue;
      const row = element("li", "quality-finding");
      row.append(statusMark(typeof finding.tone === "string" ? finding.tone : "attention"),
        element("h4", "quality-finding-headline", headline));
      const detail = typeof finding.detail === "string" ? finding.detail.trim() : "";
      if (detail && detail !== headline) {
        const fold = element("details", "quality-finding-detail");
        fold.append(element("summary", "", "Detail"));
        fold.append(element("p", "", detail));
        row.append(fold);
      }
      findingsSlot.append(row);
    }

    // The measurements 'auto' waits for. A trial states its own window, phase and
    // reading, so it is shown as recorded rather than summarised into a verdict.
    const trials = Array.isArray(report.trials) ? report.trials.filter((trial) => trial && typeof trial === "object") : [];
    for (const trial of trials) {
      const line = typeof trial.line === "string" && trial.line.trim() ? trial.line : null;
      if (!line) continue;
      const row = element("li", "compact-row");
      row.append(statusMark(trial.phase === "decided" ? "" : "attention"),
        element("h3", "", "Trial: " + line),
        element("span", "status-badge", typeof trial.phase === "string" ? trial.phase : "open"));
      const effective = typeof trial.margin_effective === "number" && Number.isFinite(trial.margin_effective)
        ? trial.margin_effective : null;
      const storedValid = trial.margin_invalid !== true && typeof trial.margin === "number"
        && Number.isFinite(trial.margin) && trial.margin > 0 && trial.margin <= 1;
      const margin = effective !== null ? effective : (storedValid ? trial.margin : null);
      if (margin !== null || trial.margin_invalid === true) {
        const parts = [];
        if (margin !== null) {
          parts.push("Win margin: a rate or duration win keeps the change only when the candidate is at most "
            + Math.round(margin * 100) + "% of the baseline.");
        }
        if (trial.margin_invalid === true) parts.push("The stored margin is not usable, so this is the default.");
        row.append(element("span", "ds-meta", parts.join(" ")));
      }
      findingsSlot.append(row);
    }

    // Ranked by structure, worst first: a failed turn outranks a fallback judge, which
    // outranks a session with nothing to read. Within a posture the busiest session
    // comes first, because more turns means more weight behind the reading.
    const rank = {"claims-unreliable": 0, "turn-failures": 1, "judge-degraded": 2, unjudged: 3, steady: 4};
    const sorted = sessions.slice().sort((left, right) => {
      const a = rank[left.posture] === undefined ? 5 : rank[left.posture];
      const b = rank[right.posture] === undefined ? 5 : rank[right.posture];
      if (a !== b) return a - b;
      return qualityCount(right.graded) - qualityCount(left.graded);
    });
    for (const session of sorted) {
      const slug = typeof session.slug === "string" && session.slug.trim() ? session.slug.trim() : "Session";
      const posture = qualityPosture(session.posture);
      const row = element("li", "compact-row");
      row.append(statusMark(posture.tone), element("h3", "", slug), element("span", "status-badge", posture.text));
      const facts = [];
      if (qualityCount(session.graded)) facts.push(qualityCount(session.graded) + " graded");
      if (qualityCount(session.judge_answered)) facts.push(qualityCount(session.judge_answered) + " judged");
      if (qualityCount(session.refused_deterministic)) facts.push(qualityCount(session.refused_deterministic) + " refused");
      if (qualityCount(session.claims_unsupported)) facts.push(qualityCount(session.claims_unsupported) + " claims unsupported");
      if (qualityCount(session.turns_failed)) facts.push(qualityCount(session.turns_failed) + " turns failed");
      if (qualityCount(session.errors)) facts.push(qualityCount(session.errors) + " errors");
      if (qualityCount(session.tool_calls)) facts.push(qualityCount(session.tool_nonzero) + "/" + qualityCount(session.tool_calls) + " tool calls nonzero");
      // Speed is measured from recorded message dates, so it is elapsed turn time,
      // not model latency. Said as "turn" so nobody reads it as a decode rate.
      if (qualityCount(session.turns_timed)) facts.push("median turn " + qualityCount(session.turn_seconds_median) + "s");
      if (qualityCount(session.repeat_failures)) facts.push(qualityCount(session.repeat_failures) + " retry loop" + (qualityCount(session.repeat_failures) === 1 ? "" : "s"));
      if (!facts.length) facts.push(session.readable === false ? "Log not readable" : "No turns recorded");
      row.append(element("span", "ds-meta", facts.join(" · ")));
      sessionsSlot.append(row);
    }
    if (!sessionsSlot.children.length) sessionsSlot.append(element("li", "quiet-note", "No sessions reported."));

    // What the lane cannot see, said on the page rather than left in a doc, but
    // folded and labelled, because a caveat is not a fault and must not be dressed
    // as one. A caveat has to explain itself and say what to fix.
    const limitItems = Array.isArray(report.limits_detail) ? report.limits_detail.filter((item) => item && typeof item === "object") : [];
    const limits = limitItems.length ? limitItems : (Array.isArray(report.limits)
      ? report.limits.filter((text) => typeof text === "string" && text.trim()).map((text) => ({label: "", text})) : []);
    if (limitsSlot && limits.length) {
      for (const limit of limits) {
        const row = element("li", "quality-limit");
        const label = typeof limit.label === "string" && limit.label.trim() ? limit.label.trim() : "";
        if (label) row.append(element("span", "quality-limit-label", label));
        row.append(element("span", "quality-limit-text", typeof limit.text === "string" ? limit.text : ""));
        limitsSlot.append(row);
      }
      if (limitsFold) limitsFold.hidden = false;
      const countSlot = $("quality-limits-count");
      if (countSlot) countSlot.textContent = limits.length + (limits.length === 1 ? " note" : " notes");
    }
  }

  async function refreshQuality() {
    if (state.qualityReading) return;
    state.qualityReading = true;
    renderQuality();
    try {
      const reply = await api("/api/oversight");
      state.qualityReport = reply && typeof reply === "object" && !Array.isArray(reply) ? reply : null;
      // A core older than the lane answers 404. That is "not served yet", not a fault,
      // and it must say so instead of showing an empty panel a reader has to interpret.
      state.qualityMissing = false;
    } catch (error) {
      state.qualityReport = null;
      state.qualityMissing = error && error.status === 404;
    } finally {
      state.qualityReading = false;
    }
    if (state.qualityMissing) {
      setIntegrationBadge("quality-state", "Needs a reload", "attention");
      $("quality-summary").textContent = "This core does not serve the oversight lane yet. Reload the core to read session quality.";
      $("quality-findings").replaceChildren();
      $("quality-sessions").replaceChildren();
      return;
    }
    renderQuality();
  }

  function nonnegativeInteger(value) {
    return Number.isSafeInteger(value) && value >= 0;
  }

  async function openBoardLink(card, revision, button, notice) {
    if (state.page !== "wip") return;
    button.disabled = true;
    notice.hidden = false;
    notice.textContent = "Opening the linked session...";
    try {
      const reply = await api("/api/board/cards/" + card.ordinal + "/link?revision=" + encodeURIComponent(revision));
      if (state.boardRevision !== revision || state.page !== "wip") return;
      if (!reply || typeof reply.session_slug !== "string" || !/^[a-z0-9][a-z0-9-]*$/.test(reply.session_slug)
          || reply.log_start !== card.log_start || reply.log_end !== card.log_end) throw new Error("Invalid board link");
      if (!state.sessions.some(item => item.slug === reply.session_slug)) await refreshSessions();
      if (state.boardRevision !== revision || state.page !== "wip") return;
      if (!state.sessions.some(item => item.slug === reply.session_slug)) throw new Error("Session not listed");
      await selectSession(reply.session_slug);
      if (state.slug !== reply.session_slug || state.page !== "wip") return;
      setPage("chat");
      $("board-evidence").textContent = `Linked source: messages ${reply.log_start}..${reply.log_end}. Only currently loaded messages are highlighted.`;
      $("board-evidence").hidden = false;
      for (const [index, node] of state.nodes) {
        if (Number(index) >= reply.log_start && Number(index) <= reply.log_end) {
          node.classList.add("board-source");
          const disclosure = node.querySelector("details");
          if (disclosure) disclosure.open = true;
        }
      }
      const node = state.nodes.get(reply.log_start);
      if (node && typeof node.scrollIntoView === "function") node.scrollIntoView({ block: "center" });
      notice.textContent = "";
    } catch (error) {
      if (state.boardRevision !== revision) return;
      if (error.status === 401 || error.status === 403) {
        state.statusEpoch += 1;
        statusUnavailable();
      } else {
        const message = error.status === 409 ? "This card snapshot changed. Refresh the board and try again." : "Link unavailable";
        notice.textContent = message;
        $("board-summary").textContent = message;
      }
    } finally {
      button.disabled = false;
      if (notice.textContent === "Opening the linked session...") notice.textContent = "";
      notice.hidden = !notice.textContent;
    }
  }

  function safeBoardTitle(value, fallback) {
    if (typeof value !== "string") return fallback;
    const title = value.trim();
    if (!title || title.length > 160 || /[\u0000-\u001f]/.test(title)) return fallback;
    return title;
  }

  function viewCard(ordinal) {
    const view = state.boardView;
    if (!view || view.revision !== state.boardRevision || !Array.isArray(view.cards)) return null;
    return view.cards.find((row) => row && row.ordinal === ordinal) || null;
  }

  // A project card says where the work stands in one sentence a person can read.
  function projectStanding(project) {
    const total = nonnegativeInteger(project.steps_total) ? project.steps_total : 0;
    const done = nonnegativeInteger(project.steps_done) ? project.steps_done : 0;
    const open = nonnegativeInteger(project.cards_open) ? project.cards_open : 0;
    const unclaimed = nonnegativeInteger(project.unclaimed) ? project.unclaimed : 0;
    const parts = [];
    if (total) parts.push(`${done} of ${total} ${total === 1 ? "step" : "steps"} done`);
    else parts.push(project.kind === "subject" ? "Related fixes & observations" : "No plan written yet");
    if (open) parts.push(`${open} ${open === 1 ? "card" : "cards"} open`);
    else if (project.cards_total) parts.push("no open cards");
    if (unclaimed) parts.push(`${unclaimed} waiting for an owner`);
    return parts.join(" · ");
  }

  function projectStepRatio(project) {
    const total = nonnegativeInteger(project.steps_total) ? project.steps_total : 0;
    const done = nonnegativeInteger(project.steps_done) ? project.steps_done : 0;
    if (!total) return null;
    return Math.max(0, Math.min(1, done / total));
  }

  function projectById(id) {
    const view = state.boardView;
    if (!view || !Array.isArray(view.projects)) return null;
    return view.projects.find((row) => row && row.id === id) || null;
  }

  // A card note can be thousands of characters. Show a readable preview and
  // let a person open the rest.
  const BOARD_NOTE_PREVIEW = 240;
  function boardNoteNode(text, identity, expanded) {
    const body = String(text).trim();
    const wrap = element("div", "board-note");
    const preview = element("p", "board-note-text", body.slice(0, BOARD_NOTE_PREVIEW).trimEnd() + (body.length > BOARD_NOTE_PREVIEW ? "\u2026" : ""));
    wrap.append(preview);
    if (body.length > BOARD_NOTE_PREVIEW) {
      const rest = element("p", "board-note-text");
      rest.textContent = body.slice(BOARD_NOTE_PREVIEW);
      const toggle = element("button", "board-note-more", "");
      toggle.type = "button";
      // The toggle carries the card's identity so an expansion survives a rebuild and
      // focus can be returned to it.
      if (identity) toggle.id = "board-note-more-" + identity;
      const paint = (open) => {
        rest.hidden = !open;
        toggle.setAttribute("aria-expanded", String(open));
        toggle.textContent = open ? "Show less" : "Show the rest (" + (body.length - BOARD_NOTE_PREVIEW) + " more characters)";
      };
      paint(expanded === true);
      toggle.addEventListener("click", () => {
        const open = rest.hidden;
        if (identity) state.noteExpanded[identity] = open;
        paint(open);
      });
      wrap.append(rest, toggle);
    }
    return wrap;
  }

  function boardCardNode(card, index) {
    if (!card || typeof card !== "object") card = {};
    const row = element("li", "board-card");
    const heading = element("div", "board-card-heading");
    const ordinal = nonnegativeInteger(card.ordinal) && card.ordinal > 0 ? card.ordinal : index + 1;
    // The card's identity, carried by every control on this row. The position is what
    // the route path still uses, but the identity is what the core checks the position
    // against, so a row drawn before a board change cannot write to a different card.
    const cardId = typeof card.card_id === "string" && card.card_id ? card.card_id : "";
    const overlay = viewCard(card.ordinal);
    const title = safeBoardTitle(overlay && overlay.title, "Card " + ordinal);
    const status = typeof card.status === "string" && Object.hasOwn(boardLabels, card.status) ? boardLabels[card.status] : "Status not reported";
    const badge = element("span", "status-badge", status);
    if (card.status === "blocked" || card.status === "failed") badge.dataset.state = "attention";
    else if (card.status === "succeeded" || card.status === "running") badge.dataset.state = "ready";
    const tone = card.status === "failed" ? "danger" : badge.dataset.state || "";
    heading.append(statusMark(tone), element("h3", "", title), badge);
    row.append(heading);
    if (overlay) {
      const owner = typeof overlay.owner === "string" && overlay.owner ? overlay.owner : "unclaimed";
      const ownerLine = element("p", "board-owner");
      ownerLine.append(element("span", "board-owner-label", "Owner"), element("span", "", owner));
      if (typeof overlay.note === "string" && overlay.note.trim()) {
        const identity = overlay.card_id || String(card.ordinal);
        row.append(ownerLine, boardNoteNode(overlay.note, identity, state.noteExpanded[identity] === true));
      } else row.append(ownerLine);
      row.append(element("p", "board-owner", Number.isInteger(overlay.priority)
        ? "Priority " + overlay.priority
        : "Priority unset"));
      // The card's project is the reason it exists; a card that cannot say which
      // project it belongs to is the gap that makes one board confusing.
      const project = projectById(overlay.project);
      const projectLine = element("p", "board-project");
      projectLine.append(element("span", "board-owner-label", "Project"));
      if (project) projectLine.append(element("span", "", project.title));
      else if (typeof overlay.project === "string" && overlay.project) projectLine.append(element("span", "", "Filed project is missing"));
      else {
        // Unfiled work is the gap that makes one board confusing. Every unfiled card
        // carries the way to file it, so nothing has to fall through to a stray list.
        projectLine.append(element("span", "board-project-none", "Unfiled"));
        const picker = fileCardControl(card.ordinal, cardId);
        if (picker) projectLine.append(picker);
      }
      // Live execution state: which session is actually running this card, what it
      // said it was doing, and how long ago the execution record was written.
      if (typeof overlay.execution === "object" && overlay.execution !== null) {
        const run = overlay.execution;
        const runLine = element("p", "board-execution");
        let detail = typeof run.session === "string" ? run.session : "";
        if (typeof run.receipt === "string" && run.receipt.trim()) {
          detail = detail ? detail + " - " + run.receipt : run.receipt;
        }
        if (typeof run.since === "number" && run.since > 0) {
          const minutes = Math.max(0, Math.floor(Date.now() / 1000 - run.since) / 60);
          detail = detail ? detail + " (" + Math.floor(minutes) + "m ago)" : Math.floor(minutes) + "m ago";
        }
        runLine.append(element("span", "board-owner-label", "Running"), element("span", "", detail));
        row.append(runLine);
      }
      row.append(projectLine);
      if (Array.isArray(overlay.evidence) && overlay.evidence.length) {
        const evidence = element("div", "work-card-evidence");
        for (const item of overlay.evidence) {
          const link = element("a", "", item.name);
          link.href = sessionPath(item.session_slug, "/attachments?id=" + encodeURIComponent(item.id));
          link.target = "_blank"; link.rel = "noopener";
          if (item.media_type.startsWith("image/")) {
            const preview = element("img"); preview.src = link.href; preview.alt = item.name; preview.loading = "lazy";
            link.prepend(preview);
          }
          evidence.append(link);
        }
        row.append(evidence);
      }
      if (typeof overlay.updated_by === "string" && overlay.updated_by) {
        row.append(element("p", "quiet-note", "Last touched by " + overlay.updated_by));
      }
    }
    if (nonnegativeInteger(card.attempt) && card.attempt > 1) row.append(element("p", "data", "Attempt " + card.attempt));
    const linked = nonnegativeInteger(card.ordinal) && card.ordinal > 0 && nonnegativeInteger(card.session_ordinal) && card.session_ordinal > 0
      && nonnegativeInteger(card.log_start) && nonnegativeInteger(card.log_end) && card.log_end >= card.log_start;
    if (linked && state.boardRevision) {
      const link = element("button", "board-session-link", "Open session");
      link.type = "button";
      const notice = element("p", "quiet-note");
      notice.hidden = true;
      notice.setAttribute("role", "status");
      link.addEventListener("click", () => openBoardLink(card, state.boardRevision, link, notice));
      const paragraph = element("p");
      paragraph.append(link);
      row.append(paragraph, notice);
    } else row.append(element("p", "quiet-note", "No session"));
    if (state.boardRevision && nonnegativeInteger(card.ordinal) && card.ordinal > 0) {
      row.draggable = true;
      // A drop carries the card's identity, not only its position. The board changes
      // under a drag too: any session writing to it renumbers the list.
      row.addEventListener("dragstart", (event) => {
        if (event.dataTransfer) event.dataTransfer.setData("text/plain", JSON.stringify({ordinal: card.ordinal, card_id: cardId}));
      });
      row.append(moveMenu(card.ordinal, cardId));
      row.append(claimControls(card.ordinal, overlay, cardId));
    }
    if (card.question_pending === true) {
      const block = element("section", "board-question");
      block.append(element("h4", "", "Your input is requested"));
      const asked = Array.isArray(overlay && overlay.questions) ? overlay.questions : [];
      for (const question of asked) {
        if (!question || question.pending !== true) continue;
        block.append(element("p", "board-question-text", question.text));
        const form = element("form", "board-answer-form");
        const label = element("label", "visually-hidden", "Answer");
        const field = element("textarea", "board-answer-input");
        field.rows = 2;
        field.placeholder = "Answer this";
        field.setAttribute("aria-label", "Answer: " + question.text);
        label.setAttribute("for", "answer-" + card.ordinal + "-" + question.id);
        field.id = "answer-" + card.ordinal + "-" + question.id;
        const send = element("button", "primary", "Answer");
        send.type = "submit";
        const note = element("p", "quiet-note");
        note.hidden = true;
        note.setAttribute("role", "status");
        form.append(label, field, send);
        form.addEventListener("submit", (event) => {
          event.preventDefault();
          const text = field.value.trim();
          if (!text) return;
          answerCard(card.ordinal, question.id, text, send, note, cardId).catch(showError);
        });
        block.append(form, note);
      }
      row.append(block);
    }
    if (nonnegativeInteger(card.artifact_count)) {
      if (card.artifact_count > 0) {
        row.append(element("h4", "", "Artifacts"));
        const artifacts = element("ul", "board-artifacts");
        const configured = Number(document.body.dataset.boardArtifactLabels);
        const limit = nonnegativeInteger(configured) && configured > 0 ? configured : 6;
        const visible = Math.min(card.artifact_count, limit);
        for (let item = 0; item < visible; item += 1) artifacts.append(element("li", "", "Artifact " + (item + 1)));
        if (card.artifact_count > visible) artifacts.append(element("li", "", "+" + (card.artifact_count - visible) + " more"));
        row.append(artifacts);
      }
    } else row.append(element("p", "quiet-note", "Artifacts not reported."));
    const details = element("details", "work-detail");
    details.open = card.question_pending === true;
    const summary = element("summary", "work-summary");
    const identity = element("span", "work-identity", "#" + ordinal);
    const meta = element("span", "work-meta", [overlay?.owner || "No owner", projectById(overlay?.project)?.title || "Unfiled"].join(" · "));
    summary.append(identity, heading, meta);
    if (overlay?.note?.trim()) {
      const paragraphs = overlay.note.trim().split(/\n\s*\n/);
      const latest = paragraphs[paragraphs.length - 1];
      summary.append(element("span", "work-preview", latest.length > 200 ? latest.slice(0,200) + "…" : latest));
    }
    const body = element("div", "work-body");
    for (const child of [...row.children]) if (child !== heading) body.append(child);
    details.append(summary, body);
    row.className = "board-card work-item";
    row.replaceChildren(details);
    return row;
  }

  function moveMenu(ordinal, cardId) {
    const menu = element("div", "ds-menu");
    const button = element("button", "ds-kebab", "Change status");
    button.type = "button";
    button.setAttribute("aria-label", "Move");
    button.setAttribute("aria-expanded", "false");
    const list = element("div", "ds-menu-list");
    list.hidden = true;
    button.addEventListener("click", () => {
      const open = list.hidden;
      list.replaceChildren();
      if (open) {
        for (const [key, label] of Object.entries(boardLabels)) {
          const item = element("button", "ds-menu-item", label);
          item.type = "button";
          item.addEventListener("click", () => moveCard(ordinal, key, cardId));
          list.append(item);
        }
      }
      list.hidden = !open;
      button.setAttribute("aria-expanded", String(open));
    });
    menu.append(button, list);
    return menu;
  }

  // The board is shared with every session. A person claims with their own name, or
  // releases a claim so an automated session can pick the card up.
  function claimControls(ordinal, overlay, cardId) {
    const wrap = element("div", "board-claim");
    const owner = overlay && typeof overlay.owner === "string" ? overlay.owner : "";
    // A core that started before these routes existed serves the page but 404s the calls.
    // Hide the controls instead of offering buttons that always fail.
    if (state.boardControlsMissing) {
      wrap.append(element("p", "quiet-note", "Claim and note need a core reload."));
      return wrap;
    }
    const operator = state.status && typeof state.status.operator === "string" ? state.status.operator : "";
    const button = element("button", "", owner ? "Release owner" : "I’ll take this");
    button.type = "button";
    if (!owner && !operator) {
      // A claim records a person's handle. With no operator name configured there is
      // no handle to record, and an empty owner would release the card instead.
      button.disabled = true;
      button.title = "Set an operator name in Settings to claim a card.";
    } else {
      button.addEventListener("click", () => setClaim(ordinal, owner ? "" : operator, cardId).catch(showError));
    }
    if (!["succeeded", "cancelled"].includes(overlay?.status)) wrap.append(button);
    const noteButton = element("button", "", "Add update");
    noteButton.type = "button";
    const editor = element("div", "board-note-editor");
    editor.hidden = true;
    const field = element("textarea", "board-note-field");
    field.rows = 2;
    // An id survives a rebuild reload. Without one, a card's half-written note was lost
    // whenever a session committed code.
    field.id = "card-note-" + encodeURIComponent(overlay?.card_id || ordinal);
    // If the reload saved words for this card, show the editor open rather than hiding
    // the restored text behind a button the person has to find again.
    if (state.pendingFieldText[field.id]) editor.hidden = false;
    field.setAttribute("aria-label", "New note for this card");
    field.placeholder = "What changed, what is left";
    const save = element("button", "primary", "Send update");
    save.type = "button";
    // A save has to answer: it disables while in flight, says what happened, and takes
    // the words away once they are stored. Without all three the button looks dead,
    // invites repeats, and leaves a reader unsure whether anything was saved.
    const saveStatus = element("p", "board-note-status");
    saveStatus.setAttribute("role", "status");
    saveStatus.hidden = true;
    save.addEventListener("click", () => {
      const text = field.value.trim();
      if (!text) {
        saveStatus.textContent = "Write something first.";
        saveStatus.hidden = false;
        return;
      }
      addBoardNote(ordinal, text, {field, save, status: saveStatus}, cardId).catch(showError);
    });
    editor.append(field, save, saveStatus);
    noteButton.addEventListener("click", () => {
      editor.hidden = !editor.hidden;
      if (!editor.hidden) field.focus();
    });
    wrap.append(noteButton);
    wrap.append(editor);
    // A card the model wrote badly has to be correctable by a person, and a card written
    // and then decided against has to come off the board. Both live here, beside the
    // claim button.
    const editButton = element("button", "", "Edit");
    editButton.type = "button";
    const editBox = element("div", "board-note-editor");
    editBox.hidden = true;
    const titleField = element("input", "board-edit-title");
    titleField.type = "text";
    titleField.id = "card-title-" + encodeURIComponent(overlay?.card_id || ordinal);
    titleField.setAttribute("aria-label", "Card title");
    titleField.maxLength = 200;
    titleField.value = overlay && typeof overlay.title === "string" ? overlay.title : "";
    const priorityField = element("input", "board-edit-title");
    priorityField.type = "number";
    priorityField.id = "card-priority-" + encodeURIComponent(overlay?.card_id || ordinal);
    priorityField.min = "0";
    priorityField.max = "100000";
    priorityField.step = "1";
    priorityField.setAttribute("aria-label", "Card priority");
    priorityField.placeholder = "Priority";
    if (overlay && Number.isInteger(overlay.priority)) priorityField.value = String(overlay.priority);
    // Same rule as the note field: if a rebuild saved half-typed words for this box, show
    // the box open rather than hiding the restored text behind a button.
    if (state.pendingFieldText[titleField.id] || state.pendingFieldText[priorityField.id]) editBox.hidden = false;
    const editSave = element("button", "", "Save card");
    editSave.type = "button";
    editSave.addEventListener("click", () => {
      const title = titleField.value.trim();
      if (!title) return;
      const raw = priorityField.value.trim();
      let priority = null;
      if (raw !== "") {
        const parsed = Number(raw);
        if (!Number.isInteger(parsed) || parsed < 0 || parsed > 100000) {
          $("board-summary").textContent = "Priority must be a whole number from 0 to 100000.";
          return;
        }
        priority = parsed;
      }
      editCard(ordinal, title, cardId, priority).catch(showError);
    });
    editBox.append(titleField, priorityField, editSave);
    editButton.addEventListener("click", () => {
      editBox.hidden = !editBox.hidden;
      if (!editBox.hidden) titleField.focus();
    });
    const removeButton = element("button", "danger", "Remove");
    removeButton.type = "button";
    removeButton.addEventListener("click", async () => {
      // The page has its own dialog; a native browser dialog blocks the whole tab on a phone.
      if (!(await ask("Take this card off the board? The record is kept.", "Remove"))) return;
      removeCard(ordinal, cardId).catch(showError);
    });
    wrap.append(editButton, editBox, removeButton);
    return wrap;
  }

  async function setClaim(ordinal, owner, cardId) {
    const revision = state.boardRevision;
    if (!revision || state.page !== "wip") return;
    try {
      const view = await boardWrite("/api/board/cards/" + ordinal + "/claim", {revision, owner}, cardId);
      if (state.boardRevision !== revision || state.page !== "wip") return;
      applyBoardView(view);
    } catch (error) {
      if (error.status === 404) {
        state.boardControlsMissing = true;
        $("board-summary").textContent = "Claim and note need a core reload.";
        renderBoardStatus();
      } else if (error.status === 409) $("board-summary").textContent = boardConflictText(error);
      else $("board-summary").textContent = "Claim failed";
    }
  }

  // Every write names the card it means, not only the position it sat in when the row
  // was drawn. An update can land on the card that moved into a position after the
  // list changed, although it was meant for another card. The page re-reads the
  // revision at click time, so a stale position passes the revision fence and the core
  // resolves it to whichever card now sits there. With the identity in the body, the core
  // refuses the mismatch instead of retargeting the write.
  function boardIdentity(body, cardId) {
    return cardId ? Object.assign({}, body, {card_id: cardId}) : body;
  }

  // The page's static files are served from disk, so a fix can be live on a page while
  // the core behind it is still the older process. A core that predates the identity
  // field rejects the whole body with 400 "Invalid request fields" (measured: live pid
  // 64736 answers 400 to {"card_id": ...}). Retrying once without it keeps every board
  // write working on an old core; the write is then position-only, which is exactly
  // what it was before, and the guard applies again as soon as the core reloads.
  async function boardWrite(path, body, cardId) {
    const payload = boardIdentity(body, cardId);
    if (!cardId) return api(path, "POST", payload);
    try {
      return await api(path, "POST", payload);
    } catch (error) {
      if (error.status !== 400) throw error;
      return api(path, "POST", body);
    }
  }

  // A refusal because the position moved is a different message from a refusal
  // because the board changed: the first one means the draft is still valid for the
  // card the person meant, the second means refresh and retry.
  function boardConflictText(error) {
    const message = typeof error?.message === "string" ? error.message : "";
    if (/no longer on the board/i.test(message)) return "That card is no longer on the board. Your words are kept.";
    return "This card changed. Refresh the board and try again.";
  }

  // control is the editor's own {field, save, status} when a person clicked Save, and
  // absent when a caller posts a note without a form (board_note from a session).
  async function addBoardNote(ordinal, note, control, cardId) {
    const revision = state.boardRevision;
    // A card named by identity does not need a revision to be written: the server
    // resolves it in the live snapshot. The submit button has to work when pressed:
    // append the action even if the page is stale, capture it, and accept it.
    // Returning silently here was the button doing nothing at all.
    if (state.page !== "wip") return;
    if (!revision && !cardId) {
      if (control) {
        control.status.textContent = "The board has not loaded yet. Try again in a moment.";
        control.status.hidden = false;
      }
      return;
    }
    if (control) {
      control.save.disabled = true;
      control.status.textContent = "Saving…";
      control.status.hidden = false;
    }
    try {
      const view = await boardWrite("/api/board/cards/" + ordinal + "/note", {revision, note}, cardId);
      if (control) {
        // The words are stored, so they leave the field. The rebuild that follows
        // captures field text for half-typed notes, which would hand this text back
        // and make a successful save look like nothing happened. Drop it first.
        control.field.value = "";
        delete state.pendingFieldText[control.field.id];
        control.status.textContent = "Note saved.";
      }
      // The view the write returned is the board as it stood when the write landed, so
      // it is adopted whether or not this page's revision moved. The old guard dropped
      // it whenever the revision had changed, which is exactly when a person needed to
      // see their words land.
      if (state.page !== "wip") return;
      applyBoardView(view);
    } catch (error) {
      if (control) {
        // The words did not reach the board, so they stay where the person can retry.
        control.status.textContent = error.status === 409 ? boardConflictText(error) : "Note not saved.";
      }
      if (error.status === 404) {
        state.boardControlsMissing = true;
        $("board-summary").textContent = "Claim and note need a core reload.";
        renderBoardStatus();
      } else if (error.status === 413) {
        // The note did not fit. Say what the core said rather than blaming a stale card.
        $("board-summary").textContent = error.message;
      } else if (error.status === 409) $("board-summary").textContent = boardConflictText(error);
      else $("board-summary").textContent = "Note failed";
    } finally {
      if (control) control.save.disabled = false;
    }
  }

  async function editCard(ordinal, title, cardId, priority) {
    const revision = state.boardRevision;
    if (!revision || state.page !== "wip") return;
    const body = {revision, title};
    if (priority !== undefined) body.priority = priority;
    try {
      const view = await boardWrite("/api/board/cards/" + ordinal + "/note", body, cardId);
      if (state.boardRevision !== revision || state.page !== "wip") return;
      applyBoardView(view);
    } catch (error) {
      if (error.status === 404) {
        state.boardControlsMissing = true;
        $("board-summary").textContent = "Edit needs a core reload.";
        renderBoardStatus();
      } else if (error.status === 413) {
        $("board-summary").textContent = error.message;
      } else if (error.status === 409) $("board-summary").textContent = boardConflictText(error);
      else $("board-summary").textContent = "Edit failed";
    }
  }

  async function removeCard(ordinal, cardId) {
    const revision = state.boardRevision;
    if (!revision || state.page !== "wip") return;
    try {
      const view = await boardWrite("/api/board/cards/" + ordinal + "/remove", {revision}, cardId);
      if (state.boardRevision !== revision || state.page !== "wip") return;
      applyBoardView(view);
    } catch (error) {
      if (error.status === 404) {
        state.boardControlsMissing = true;
        $("board-summary").textContent = "Remove needs a core reload.";
        renderBoardStatus();
      } else if (error.status === 409) $("board-summary").textContent = boardConflictText(error);
      else $("board-summary").textContent = "Remove failed";
    }
  }

  function boardLane(card, overlay) {
    if (card.status === "succeeded") return "done";
    if (card.status === "cancelled") return "cancelled";
    if (card.question_pending === true || card.status === "blocked" || card.status === "failed") return "attention";
    if (card.status === "running") return "running";
    if (card.status === "queued" && overlay?.claimable === true) return "available";
    if (card.status !== "queued") return "other";
    return "waiting";
  }

  function matchesBoardWork(card, overlay, filters) {
    const finished = card.status === "succeeded" || card.status === "cancelled";
    if (finished && !filters.history) return false;
    const project = overlay?.project || "";
    if (filters.project === "__unfiled" && project) return false;
    if (filters.project && filters.project !== "__unfiled" && project !== filters.project) return false;
    if (!filters.history && filters.focus && filters.focus !== "all" && boardLane(card, overlay) !== filters.focus) return false;
    if (filters.category && boardCategory(overlay?.title) !== filters.category) return false;
    const query = (filters.query || "").trim().toLowerCase();
    if (query) {
      const text = [overlay?.title, overlay?.note, overlay?.owner, overlay?.project, card.ordinal].join(" ").toLowerCase();
      if (!text.includes(query)) return false;
    }
    return true;
  }

  // A work order's category is the prefix its title already carries: "UI: ...",
  // "Backend: ...", "Bugs: ...". Cards are filed under ONE planning project with
  // those prefixes, not one subject project per category, so the category is read
  // from the title rather than invented as a second filing dimension. Returns ""
  // for a title with no prefix, which the filter treats as uncategorized rather
  // than guessing.
  function boardCategory(title) {
    if (typeof title !== "string") return "";
    const match = title.match(/^\s*([^:]{1,40}):\s+\S/);
    return match ? match[1].trim() : "";
  }

  function renderWorkOverview(cards) {
    const box = $("wip-overview");
    if (!box) return;
    const counts = {attention:0, running:0, available:0, waiting:0, done:0, cancelled:0, other:0};
    for (const card of cards) counts[boardLane(card, viewCard(card.ordinal))] += 1;
    const nodes = [];
    for (const [key,label] of [["attention","Needs attention"],["running","In flight"],["available","Available"],["waiting","Waiting"],["history","Finished"]]) {
      const button = element("button", "wip-stat");
      button.type = "button";
      const count = key === "history" ? counts.done + counts.cancelled : counts[key];
      button.append(element("span", "wip-stat-value", String(count)), element("span", "", label));
      button.dataset.lane = key;
      button.setAttribute("aria-pressed", String(key === "history" ? state.boardShowDone : !state.boardShowDone && state.boardFocus === key));
      button.addEventListener("click", () => {
        state.boardShowDone = key === "history";
        state.boardFocus = key === "history" ? "all" : key;
        $("board-focus").value = state.boardFocus;
        renderBoardStatus();
        $("board-title").scrollIntoView({block:"start",behavior:"instant"});
      });
      nodes.push(button);
    }
    box.replaceChildren(...nodes);
  }

  function renderBoardColumns(cards) {
    const list = $("board-cards");
    const previous = new Map();
    for (const row of list.querySelectorAll(".work-item")) previous.set(row.dataset.identity, row);
    const oldColumns = new Map();
    for (const column of list.children) if (column.dataset.lane) oldColumns.set(column.dataset.lane, column);
    const labels = {attention:"Needs attention", running:"In flight", available:"Ready for an owner", waiting:"Waiting", done:"Completed", cancelled:"Set aside", other:"Status not reported"};
    const groups = new Map();
    for (const key of Object.keys(labels)) groups.set(key, []);
    let doneCount = 0;
    for (let index = 0; index < cards.length; index += 1) {
      const card = cards[index];
      const overlay = viewCard(card.ordinal);
      if (["succeeded","cancelled"].includes(card.status)) doneCount += 1;
      if (!matchesBoardWork(card, overlay, {history:state.boardShowDone === true, project:state.boardProjectFilter, focus:state.boardFocus, query:state.boardSearch, category:state.boardCategory})) continue;
      const identity = overlay?.card_id || String(card.ordinal);
      const key = JSON.stringify([card,overlay]);
      let row = previous.get(identity);
      // A row that dropped out of the view (a filter change, or a status that moved it
      // to another lane) is removed and rebuilt fresh, which reset its expansion. Record
      // the reader's open state by identity so the rebuilt row comes back as they left it.
      // Otherwise expanded card content collapses before it can be read.
      if (row && typeof row.querySelector === "function") {
        const wasOpen = row.querySelector(".work-detail");
        if (wasOpen && typeof wasOpen.open === "boolean") state.detailExpanded[identity] = wasOpen.open;
      }
      if (!row || row.renderKey !== key) {
        const before = row;
        row = boardCardNode(card, index);
        row.dataset.identity = identity;
        row.renderKey = key;
        const oldDetails = before?.querySelector(".work-detail");
        const detail = row.querySelector(".work-detail");
        if (oldDetails && detail) detail.open = oldDetails.open;
        else if (detail && state.detailExpanded[identity] === true) detail.open = true;
        if (detail && typeof detail.addEventListener === "function") {
          detail.addEventListener("toggle", () => { state.detailExpanded[identity] = detail.open === true; });
        }
        if (before) retainWorkEditors(before, row);
      }
      row._boardCard = card;
      groups.get(boardLane(card, overlay)).push(row);
    }
    for (const rows of groups.values()) sortBoardLane(rows);
    $("board-show-open").setAttribute("aria-pressed", String(!state.boardShowDone));
    $("board-show-done").setAttribute("aria-pressed", String(state.boardShowDone));
    $("board-show-done").textContent = state.boardShowDone
      ? "All work (" + cards.length + ")"
      : doneCount + " completed card" + (doneCount === 1 ? "" : "s") + " hidden, show all";
    const nodes = [];
    for (const [key, rows] of groups) {
      if (!rows.length) continue;
      let column = oldColumns.get(key);
      if (!column) {
        column = element("li", "work-lane");
        column.dataset.lane = key;
        const head = element("h3", "work-lane-title");
        const stack = element("ul", "work-lane-cards");
        column.append(head, stack);
      }
      column.firstChild.textContent = labels[key] + " · " + rows.length;
      replaceChangedChildren(column.lastChild, rows);
      nodes.push(column);
    }
    if (!nodes.length) nodes.push(element("li", "work-empty", state.boardSearch ? "No matching work. Try another search or filter." : state.boardShowDone ? "No finished work in this view." : state.boardCategory ? `No open work in the ${state.boardCategory} category. Choose All categories to see the rest.` : "No work in this view. Add a task or choose another filter."));
    replaceChangedChildren(list, nodes);
    adoptPendingFieldText();
  }

  // Lanes render in payload order, which is creation order: an old untouched card
  // outranks fresh work inside its lane. Sort by recency instead (recently touched,
  // not recently created), so a note on an old card still bumps it toward the top
  // where the action is. Client-side on purpose: the payload array order is read by
  // other consumers (log ranges, session links) and is not ours to change here.
  function sortBoardLane(rows) {
    rows.sort((a, b) => {
      const stamp = (card) => card && typeof card.updated_at === "number" && Number.isFinite(card.updated_at) ? card.updated_at : null;
      const ta = stamp(a._boardCard), tb = stamp(b._boardCard);
      // A card with no usable updated_at sorts last, never first, and never crashes.
      if (ta === null && tb !== null) return 1;
      if (ta !== null && tb === null) return -1;
      if (ta !== null && tb !== null && ta !== tb) return tb - ta;
      // Equal or missing stamps: stable tiebreak on ordinal so consecutive 15s
      // renders never shuffle rows the renderKey guard just decided to keep.
      const oa = a._boardCard && typeof a._boardCard.ordinal === "number" ? a._boardCard.ordinal : Infinity;
      const ob = b._boardCard && typeof b._boardCard.ordinal === "number" ? b._boardCard.ordinal : Infinity;
      return oa - ob;
    });
    return rows;
  }

  function retainWorkEditors(previous, next) {
    for (const editor of previous.querySelectorAll(".board-note-editor")) {
      if (editor.hidden) continue;
      const field = editor.querySelector("input, textarea");
      if (!field?.id) continue;
      for (const replacement of next.querySelectorAll(".board-note-editor")) {
        if (replacement.querySelector("input, textarea")?.id === field.id) { replacement.replaceWith(editor); break; }
      }
    }
  }

  // A poll rebuild replaces rows, and whatever the reader had focused went with them.
  // Losing focus while typing is a pain. Remember the focused control's id and hand
  // focus back to the control with the same id after the rebuild. Selection is restored too, so a half-written note
  // does not jump its caret back to the start mid-typing.
  function captureFocus() {
    const active = document.activeElement;
    if (!active || typeof active.id !== "string" || !active.id) return null;
    if (["INPUT", "TEXTAREA", "SELECT"].indexOf(String(active.tagName || "").toUpperCase()) < 0) return null;
    let start = null, end = null;
    try {
      if (typeof active.selectionStart === "number") {
        start = active.selectionStart;
        end = active.selectionEnd;
      }
    } catch (_) { /* selection is not readable on every input type */ }
    return {id: active.id, start, end};
  }

  function restoreFocus(saved) {
    if (!saved) return;
    const field = document.getElementById(saved.id);
    if (!field || typeof field.focus !== "function") return;
    if (document.activeElement === field) return;
    field.focus({preventScroll: true});
    if (saved.start !== null && typeof field.setSelectionRange === "function") {
      try { field.setSelectionRange(saved.start, saved.end === null ? saved.start : saved.end); }
      catch (_) { /* not a text field */ }
    }
  }

  // Reading focus hides the top bars and the conversation heading so the transcript is
  // what is left on a phone. The top bars have to be hideable: otherwise a reply is
  // covered up and pinched by everything else demanding attention. The control sits
  // outside both bands it hides, so there is always a way back. The choice is remembered, but only for the chat page.
  function applyReadingFocus(on, options) {
    const wanted = on === true;
    document.body.classList.toggle("reading-focus", wanted);
    const button = $("reading-focus");
    if (button) {
      button.setAttribute("aria-pressed", String(wanted));
      button.textContent = wanted ? "Bars" : "Read";
      button.title = wanted ? "Show the top bars again (⌘/Ctrl Shift R)" : "Hide the top bars and read the conversation (⌘/Ctrl Shift R)";
    }
    if (!options || options.persist !== false) {
      try { localStorage.setItem("tessera-reading-focus", wanted ? "on" : "off"); } catch (_) { /* private mode */ }
    }
    return wanted;
  }

  // True while the reader is typing into a field on the work page. The poll reads this
  // to skip the rebuild that would replace the field they are typing into. Scoped to
  // #page-wip: a field elsewhere on the app is not a reason to stop refreshing the board.
  //
  // The two board view controls are the exception. Search and the lane filter sit in
  // #wip-tools, outside every container renderBoardStatus rebuilds (#board-cards,
  // #project-page), so holding the render for them costs nothing and breaks them
  // completely: their own input handler sets the query and calls renderBoardStatus,
  // which then returns early at the guard because the control it just came from has
  // focus. Typing produced no filtering at all -- the box was a text box, not a search.
  // A control whose purpose is to change the view must not be a reason to suspend it.
  function boardFieldFocused() {
    const active = document.activeElement;
    if (!active || typeof active.tagName !== "string") return false;
    const tag = active.tagName.toUpperCase();
    if (["INPUT", "TEXTAREA", "SELECT"].indexOf(tag) < 0) return false;
    // Kept inline rather than hoisted to a constant: the browser-free test rigs slice
    // this one function out of the file and run it alone, so a reference to a
    // module-level binding would be undefined there.
    if (["board-search", "board-focus"].indexOf(String(active.id || "")) >= 0) return false;
    const page = document.getElementById("page-wip");
    if (!page || typeof page.contains !== "function") return false;
    return page.contains(active);
  }

  function bindColumnDrop(node, key) {
    node.addEventListener("dragover", (event) => event.preventDefault());
    node.addEventListener("drop", (event) => {
      event.preventDefault();
      const raw = event.dataTransfer ? event.dataTransfer.getData("text/plain") : "";
      // The drag carries {ordinal, card_id}. A page that was already open when this
      // shipped still carries the bare ordinal, so both shapes are accepted: the
      // identity is what makes the move safe, and its absence is simply the old way.
      let ordinal = null;
      let cardId = "";
      try {
        const parsed = JSON.parse(raw);
        if (parsed && typeof parsed === "object") {
          ordinal = Number(parsed.ordinal);
          cardId = typeof parsed.card_id === "string" ? parsed.card_id : "";
        }
      } catch (_) {
        ordinal = Number(raw);
      }
      if (Number.isSafeInteger(ordinal) && ordinal > 0) moveCard(ordinal, key, cardId);
    });
  }

  function renderBoardStatus() {
    // The operator view is the better source when it is present: it carries titles, owners,
    // notes and projects. The passive status projection does not, and the 15-second status
    // poll replaces it in place, so reading it here is what blanked the cards.
    let report = state.boardView && Array.isArray(state.boardView.cards)
      ? state.boardView
      : state.status.board_status;
    if (!report || typeof report !== "object" || Array.isArray(report)) report = {};
    const list = $("board-cards");
    if (state.boardView && Array.isArray(report.projects) && state.boardProjectFilter && state.boardProjectFilter !== "__unfiled" && !projectById(state.boardProjectFilter)) {
      state.boardProjectFilter = "";
      state.openProject = "";
      state.boardCategory = "";
    }
    // renderBoardStatus is reachable from refreshStatus on the 15-second tick, so the
    // tick guard alone is not enough: any caller can rebuild the rows under a typing
    // caret. Defer while a field on this page is focused, same one-tick recovery.
    if (state.page === "wip" && boardFieldFocused()) return;
    const renderKey = JSON.stringify([report, state.boardProjectFilter, state.boardShowDone, state.boardFocus, state.boardSearch, state.boardCategory, state.openProject, state.boardControlsMissing]);
    if (list.renderKey === renderKey) return;
    list.renderKey = renderKey;
    // A rebuild is about to replace rows. Anything focused inside them is saved first
    // and handed back afterwards, so a poll never eats the caret mid-typing.
    const focus = captureFocus();
    // The nodes below are about to be replaced. Anything a person has typed into one of
    // them is saved first, so the rebuild hands it back instead of eating it.
    captureFieldText(list);
    state.boardRevision = null;
    let label = "Not reported";
    let summary = "Not reported";
    let cards = null;
    if (report.enabled === false) {
      label = "Off";
      summary = "Off";
    } else if (report.configured === false) {
      label = "Not configured";
      summary = "Not configured";
    } else if (typeof report.unreadable === "string" && report.unreadable) {
      // A blank board is not an empty board. Without this branch the page said "No cards
      // reported" for a journal that could not be read at all, which is the same silence
      // a journal that outgrew the reader cap produced. Say what is wrong and where.
      label = "Unreadable";
      summary = "Board unreadable: " + report.unreadable;
    } else if (report.enabled === true && report.configured === true) {
      cards = providerRecords(report.cards);
      if (cards) {
        if (typeof report.revision === "string" && /^[a-f0-9]{64}$/.test(report.revision)) state.boardRevision = report.revision;
        // A "Live" label meant "the board answered with at least one card", which is a
        // fact about the fetch, not about the work, and read as a tag on a card. The badge now counts the work instead of describing the transport.
        const open = cards.filter((card) => card && card.status !== "succeeded" && card.status !== "cancelled").length;
        label = !cards.length ? "Empty" : open ? open + " open" : "All done";
        summary = cards.length ? boardSummary(cards) : "No cards";
        renderBoardColumns(cards);
      } else summary = "No cards reported";
    }
    setIntegrationBadge("board-state", label,
      typeof report.unreadable === "string" && report.unreadable ? "attention" : "");
    $("board-summary").textContent = summary;
    if (!cards) list.replaceChildren();
    if (!list.children.length) {
      list.append(element("li", "quiet-note",
        typeof report.unreadable === "string" && report.unreadable ? "Board unreadable: " + report.unreadable : "No cards"));
    }
    renderBoardProjects();
    renderBoardFilters();
    renderProjectPage();
    paintBoardChrome(cards);
    renderWorkOverview(cards || []);
    // The rows exist again; give the caret back to where the reader left it.
    restoreFocus(focus);
  }

  // Projects are the spine. Each card says where the plan stands and opens its own page.
  function renderBoardProjects() {
    const box = $("board-projects");
    box.replaceChildren();
    const view = state.boardView;
    // An older core sends cards without a projects key. That is an empty project
    // list, not an unknown one: the board is still readable.
    const projects = view ? (Array.isArray(view.projects) ? view.projects : []) : null;
    // An empty project list is not always an empty set. The projects journal is a
    // separate file from the cards, so a corrupt or oversized one leaves every card
    // readable and the project list blank. The encouragement line below would then
    // describe that as a board nobody has grouped yet. Say which wall it hit.
    const report = view || state.status.board_status || {};
    const refused = typeof report.projects_unreadable === "string" && report.projects_unreadable
      ? report.projects_unreadable : "";
    if (!projects) {
      box.append(element("p", "quiet-note", "Projects load with the board."));
      $("board-projects-count").textContent = "";
      return;
    }
    const active = projects.filter((project) => project.status === "active");
    $("board-projects-count").textContent = projects.length
      ? `${active.length} active · ${projects.length} total` : "";
    renderBoardNoteProjects(projects);
    if (!projects.length) {
      box.append(element("p", "quiet-note", refused
        ? "Projects unreadable: " + refused
        : "Tessera can group related fixes into subjects, and open planning projects when needed."));
      return;
    }
    // 15 active tiles plus 6 closed ones made the grid 1863px tall at 1440x900, which
    // put the work list far below the fold. Active work is what a person comes here
    // for, so it renders inline; finished and dropped spaces fold into one line rather
    // than occupying the same space as live ones. Nothing is hidden: the fold names
    // its count and opens on a click.
    const live = projects.filter((project) => project.status === "active");
    const closed = projects.filter((project) => project.status !== "active");
    for (const project of live) box.append(projectCard(project));
    if (closed.length) {
      const fold = element("details", "project-closed-fold");
      fold.append(element("summary", "quiet-note",
        closed.length + (closed.length === 1 ? " finished or dropped space" : " finished or dropped spaces")));
      for (const project of closed) fold.append(projectCard(project));
      box.append(fold);
    }
  }

  // The project picker on the note form lists the same projects the board shows. A note
  // can be filed, or left unfiled and sorted later.
  function renderBoardNoteProjects(projects) {
    const select = $("board-note-project");
    if (!select) return;
    const keep = select.value;
    select.replaceChildren();
    const unfiled = element("option", "", "Let Tessera file it");
    unfiled.value = "";
    select.append(unfiled);
    for (const project of projects || []) {
      const option = element("option", "", (project.kind === "subject" ? "Subject · " : "Project · ") + safeBoardTitle(project.title, "Untitled space"));
      option.value = project.id;
      select.append(option);
    }
    if (keep && [...select.options].some((option) => option.value === keep)) select.value = keep;
  }

  function projectCard(project) {
    const card = element("article", "project-card");
    card.dataset.project = project.id;
    if (project.status !== "active") card.dataset.state = "closed";
    const head = element("div", "project-card-head");
    const title = element("button", "project-open", safeBoardTitle(project.title, "Untitled project"));
    title.type = "button";
    title.addEventListener("click", () => openProject(project.id));
    head.append(title);
    head.append(element("span", "workspace-kind", project.kind === "subject" ? "Subject" : "Planning project"));
    // A status that repeats on every card states nothing. Closed tiles already
    // dim and read Done or Dropped. Only the non-default states earn a badge;
    // active is the default and gets none.
    if (project.status !== "active") {
      head.append(element("span", "status-badge", project.status === "done" ? "Done" : "Dropped"));
    }
    card.append(head);
    if (typeof project.description === "string" && project.description.trim()) {
      card.append(element("p", "project-desc", project.description));
    }
    const manager = typeof project.manager === "string" && project.manager ? project.manager : "no manager";
    card.append(element("p", "project-meta", "Manager " + manager + " · " + projectStanding(project)));
    const ratio = projectStepRatio(project);
    if (ratio !== null) {
      const meter = element("div", "project-meter");
      const bar = element("div", "project-bar");
      bar.setAttribute("role", "progressbar");
      bar.setAttribute("aria-valuemin", "0");
      bar.setAttribute("aria-valuemax", "100");
      bar.setAttribute("aria-valuenow", String(Math.round(ratio * 100)));
      bar.setAttribute("aria-label", "Plan progress");
      const fill = element("div", "project-bar-fill");
      fill.style.width = Math.round(ratio * 100) + "%";
      bar.append(fill);
      meter.append(bar, element("span", "project-meter-text", `${Math.round(ratio * 100)}% of the plan`));
      card.append(meter);
    }
    // A percentage tells a person how far along the plan is, not what is left to do.
    // A board of cards that do not communicate is the failure, and a reader had to open
    // every project to learn its next step. Name it on the card.
    const nextStep = (Array.isArray(project.steps) ? project.steps : [])
      .find((step) => step && typeof step.text === "string" && step.text.trim() && !step.done);
    if (nextStep) card.append(element("p", "project-next", "Next: " + nextStep.text.trim()));
    const open = nonnegativeInteger(project.cards_open) ? project.cards_open : 0;
    const button = element("button", "project-filter", open ? `Show ${open} open ${open === 1 ? "card" : "cards"}` : "Show cards");
    button.type = "button";
    // This one names a destination, so it sets rather than toggles: pressing it
    // always leaves you looking at that project's cards. Toggling it here was my
    // own scope creep, and it let the button close a project page it had just
    // opened. The filter chips above are the toggles.
    button.addEventListener("click", () => {
      setBoardProjectFilter(project.id);
      renderBoardStatus();
    });
    card.append(button);
    // On a 390x844 phone viewport the only live target on a project card was the title
    // button, 251x34px. A tap on the
    // description, the manager/standing line, the progress meter or the card body hit
    // an ARTICLE with no handler and nothing happened. The card reads as one object,
    // so the whole object opens it; the two real buttons keep their own handlers.
    card.addEventListener("click", (event) => {
      const target = event.target;
      if (target && typeof target.closest === "function"
          && target.closest(".project-open, .project-filter")) return;
      openProject(project.id);
    });
    return card;
  }

  // The project page and the project filter are the same fact, so they move
  // together. A filter that does not name the open project closes the project page.
  function setBoardProjectFilter(value) {
    state.boardProjectFilter = value;
    if (state.openProject && state.openProject !== value) {
      state.openProject = "";
      state.boardCategory = "";
      // The URL carries the open project. Clearing it here without rewriting the
      // location left a reload reopening the page that was just closed.
      writeLocation();
    }
  }

  // One board, filtered by project. The filter is the bridge between the project page
  // and the work, not a second board per project.
  function renderBoardFilters() {
    const box = $("board-filters");
    box.replaceChildren();
    const view = state.boardView;
    const projects = view && Array.isArray(view.projects) ? view.projects : [];
    if (!projects.length && !state.boardProjectFilter) {
      box.hidden = true;
      return;
    }
    box.hidden = false;
    const all = element("button", "board-filter", "All cards");
    all.type = "button";
    all.setAttribute("aria-pressed", String(!state.boardProjectFilter));
    all.addEventListener("click", () => { setBoardProjectFilter(""); renderBoardStatus(); });
    box.append(all);
    // Project chips repeat the filter each tile already offers, and a long row of
    // them pushes the first card down the page. All and Unfiled stay in reach; the
    // project chips fold into one line, and the fold opens by itself when a project
    // filter is already on so the active chip is never hidden.
    const fold = element("details", "board-filter-fold");
    fold.open = Boolean(state.boardProjectFilter && state.boardProjectFilter !== "__unfiled");
    fold.append(element("summary", "board-filter", "Projects (" + projects.length + ")"));
    for (const project of projects) {
      const button = element("button", "board-filter", safeBoardTitle(project.title, "Untitled project"));
      button.type = "button";
      button.setAttribute("aria-pressed", String(state.boardProjectFilter === project.id));
      // The filter that is already on turns off when pressed again. Assigning the
      // same value unconditionally made a second click a no-op that looked like a
      // reload. Clicking it again removes the filter instead of reloading it.
      button.addEventListener("click", () => {
        setBoardProjectFilter(state.boardProjectFilter === project.id ? "" : project.id);
        renderBoardStatus();
      });
      fold.append(button);
    }
    box.append(fold);
    const unfiled = element("button", "board-filter", "Unfiled");
    unfiled.type = "button";
    // Say how many cards are unfiled, so the gap is visible rather than inferred.
    const cards = state.boardView && Array.isArray(state.boardView.cards) ? state.boardView.cards : [];
    const loose = cards.filter((card) => card && !card.project && card.status !== "succeeded" && card.status !== "cancelled").length;
    if (loose) unfiled.textContent = `Unfiled (${loose})`;
    unfiled.setAttribute("aria-pressed", String(state.boardProjectFilter === "__unfiled"));
    unfiled.addEventListener("click", () => {
      setBoardProjectFilter(state.boardProjectFilter === "__unfiled" ? "" : "__unfiled");
      renderBoardStatus();
    });
    box.append(unfiled);
  }

  // Pressure is what the body measured, in its own words, ranked. An empty
  // reading says so instead of showing an empty panel a person has to interpret.
  // items omits the host-steady fallback, so its name is read from report.rules.
  // The rule line is a wake instruction, not this sentence.
  function steadyHeadline(report) {
    const rules = Array.isArray(report.rules) ? report.rules : [];
    const rule = rules.find((item) => item && item.id === "host-steady");
    const name = rule && typeof rule.name === "string" ? rule.name.trim() : "";
    return name || "Hosts are steady";
  }

  function renderBoardPressure() {
    const box = $("board-pressure");
    box.replaceChildren();
    const report = state.pressure;
    if (!report || typeof report !== "object" || Array.isArray(report)) {
      setIntegrationBadge("board-pressure-state", "Not reported");
      box.append(element("p", "quiet-note", "The floor has not reported pressure yet."));
      return;
    }
    const items = Array.isArray(report.items) ? report.items.filter((item) => item && typeof item.text === "string") : [];
    if (typeof report.definition_error === "string" && report.definition_error) {
      box.append(element("p", "pressure-errors", report.definition_error));
    }
    if (report.approval_mode === "unknown") {
      box.append(element("p", "pressure-errors", "Approval status could not be read."));
    }
    const errors = Array.isArray(report.errors) ? report.errors.filter((item) => item && typeof item === "object") : [];
    if (errors.length) {
      const list = element("ul", "pressure-errors");
      for (const item of errors) {
        const source = typeof item.source === "string" ? item.source : "reading";
        const kind = typeof item.error_type === "string" ? item.error_type : "Error";
        const message = typeof item.message === "string" ? item.message : "";
        list.append(element("li", "", source + " failed: " + kind + (message ? ": " + message : "")));
      }
      box.append(list);
    }
    if (!items.length) {
      // Steady means the body measured no change. It does not mean nothing is open:
      // lessons, parked questions and committed intentions are still unfinished work,
      // and the meta line right below counts them. Saying "steady" alone reads as clear.
      const open = (nonnegativeInteger(report.learning && report.learning.open_lessons) ? report.learning.open_lessons : 0)
        + (nonnegativeInteger(report.learning && report.learning.open_questions) ? report.learning.open_questions : 0)
        + (nonnegativeInteger(report.learning && report.learning.open_intentions) ? report.learning.open_intentions : 0);
      setIntegrationBadge("board-pressure-state", open ? "Steady, work open" : "Steady", open ? "attention" : "");
      box.append(element("p", "pressure-steady", open
        ? `No measured change is asking for attention. ${open} unfinished ${open === 1 ? "item" : "items"} still open below.`
        : steadyHeadline(report) + ". No measured change is asking for attention."));
    } else {
      setIntegrationBadge("board-pressure-state", items.length === 1 ? "1 item" : items.length + " items", "attention");
      const list = element("ol", "pressure-list");
      for (const item of items) {
        const row = element("li", "pressure-item");
        const score = typeof item.score === "number" && Number.isFinite(item.score) ? item.score : null;
        if (score !== null && score >= 1.5) row.dataset.state = "high";
        else if (score !== null && score >= 1.2) row.dataset.state = "warm";
        const text = element("span", "pressure-text", item.text.replace(/^Pressure:\s*/i, ""));
        row.append(text);
        // The floor holds a card it already offered so it does not repeat itself every
        // wake. That is a nudge-suppression memo, not a claim the work stopped mattering,
        // so the row stays visible and says which of the two it is.
        if (item.held_back === true) {
          row.dataset.held = "true";
          row.append(element("span", "pressure-held",
            "Unclaimed. The floor offered it and is holding back for now."));
        }
        if (score !== null) row.append(element("span", "pressure-score", "salience " + score.toFixed(1)));
        if (typeof item.rule === "string" && item.rule) {
          row.append(element("span", "pressure-score", item.rule + (item.wake === true ? " · wakes" : " · report only")));
        }
        list.append(row);
      }
      box.append(list);
    }
    // Board work the floor reports but does not wake on. A session wakes for its own
    // cards. The floor wakes on measured pressure. Filing is the model's job, so an
    // unfiled card has to be visible here rather than waiting for a person to notice it.
    const boardWork = report.board && typeof report.board === "object" && Array.isArray(report.board.items)
      ? report.board.items.filter((item) => item && typeof item.text === "string") : [];
    if (boardWork.length) {
      const heading = element("p", "pressure-steady", "Work on the board nothing is acting on.");
      const list = element("ol", "pressure-list");
      for (const item of boardWork) {
        const row = element("li", "pressure-item");
        row.append(element("span", "pressure-text", item.text.replace(/^Pressure:\s*/i, "")));
        if (typeof item.rule === "string" && item.rule) {
          row.append(element("span", "pressure-score", item.rule + " · report only"));
        }
        list.append(row);
      }
      box.append(heading, list);
    }
    const learning = report.learning && typeof report.learning === "object" ? report.learning : null;
    const facts = [];
    if (learning) {
      if (nonnegativeInteger(learning.open_lessons) && learning.open_lessons) facts.push(`${learning.open_lessons} open ${learning.open_lessons === 1 ? "lesson" : "lessons"}`);
      if (nonnegativeInteger(learning.open_questions) && learning.open_questions) facts.push(`${learning.open_questions} parked ${learning.open_questions === 1 ? "question" : "questions"}`);
      if (nonnegativeInteger(learning.open_intentions) && learning.open_intentions) facts.push(`${learning.open_intentions} committed ${learning.open_intentions === 1 ? "intention" : "intentions"}`);
    }
    if (nonnegativeInteger(report.wakes_last_hour)) facts.push(`${report.wakes_last_hour} wakes in the last hour`);
    if (facts.length) box.append(element("p", "pressure-meta", facts.join(" · ")));
    if (typeof report.world_view === "string" && report.world_view.trim()) {
      const view = element("details", "pressure-group");
      view.append(element("summary", "pressure-group-head", "World view the floor would send"));
      view.append(element("pre", "floor-preview", report.world_view));
      box.append(view);
    }
    const last = report.last_wake && typeof report.last_wake === "object" ? report.last_wake : null;
    const sent = last && typeof last.sent === "string" && last.sent
      ? last.sent
      : (last && typeof last.posted === "string" ? last.posted : "");
    if (sent) {
      const wake = element("details", "pressure-group");
      wake.append(element("summary", "pressure-group-head", "Last wake sent"));
      const rules = Array.isArray(last.rules) ? last.rules.filter((rule) => typeof rule === "string" && rule) : [];
      if (rules.length) wake.append(element("p", "pressure-meta", "Rules: " + rules.join(", ")));
      wake.append(element("pre", "floor-preview", sent));
      box.append(wake);
    }
    const ruleRows = Array.isArray(report.rules) ? report.rules.filter((rule) => rule && typeof rule === "object") : [];
    if (ruleRows.length) {
      const details = element("details", "pressure-group");
      details.dataset.pressureGroup = "rules";
      details.open = state.pressureExpanded.rules === true;
      details.addEventListener("toggle", () => {
        state.pressureExpanded.rules = details.open === true;
      });
      details.append(element("summary", "pressure-group-head", "Floor rules (" + ruleRows.length + ")"));
      const list = element("ul", "pressure-group-list");
      for (const rule of ruleRows) {
        const lines = Array.isArray(rule.lines) ? rule.lines.filter((line) => typeof line === "string") : [];
        const stateWord = rule.enabled === false ? "off" : (rule.acting ? "acting" : (lines.length ? "report only" : "quiet"));
        const item = element("li", "pressure-group-item");
        const score = typeof rule.score === "number" && Number.isFinite(rule.score) ? " · " + rule.score : "";
        item.append(element("span", "pressure-group-text", (rule.id || "rule") + " · " + stateWord + score));
        if (lines.length) item.append(element("span", "pressure-group-text", lines.join(" ")));
        list.append(item);
      }
      details.append(list);
      box.append(details);
    }
    renderPressureItems(report.open_items);
  }

  // A count is not a report. The lessons, questions and intentions behind those numbers
  // are what a person needs to judge whether the system is worried about the right things.
  function renderPressureItems(items) {
    const box = $("board-pressure-items");
    if (!box) return;
    box.replaceChildren();
    if (!items || typeof items !== "object") return;
    const groups = [
      ["lessons", "Open lessons", (lesson) => `${lesson.trigger}. Failure: ${lesson.failure}. Check: ${lesson.check}`],
      ["questions", "Parked questions", (question) => question.text],
      ["intentions", "Committed intentions", (intention) => intention.title],
    ];
    for (const [key, label, describe] of groups) {
      const rows = Array.isArray(items[key]) ? items[key].filter((row) => row && typeof row === "object") : [];
      if (!rows.length) continue;
      const details = element("details", "pressure-group");
      // Collapsed by default. A rebuild keeps a group the reader opened.
      details.dataset.pressureGroup = key;
      details.open = state.pressureExpanded[key] === true;
      details.addEventListener("toggle", () => {
        state.pressureExpanded[key] = details.open === true;
      });
      details.append(element("summary", "pressure-group-head", `${label} (${rows.length})`));
      const list = element("ul", "pressure-group-list");
      for (const row of rows) {
        const item = element("li", "pressure-group-item");
        item.append(element("span", "pressure-group-text", describe(row)));
        if (typeof row.slug === "string" && row.slug) item.append(element("span", "pressure-group-slug", row.slug));
        if (typeof row.skill === "string" && row.skill) item.append(element("span", "pressure-group-skill", "skill " + row.skill));
        list.append(item);
      }
      details.append(list);
      box.append(details);
    }
  }

  async function refreshPressure() {
    try {
      const report = await api("/api/board/pressure");
      state.pressure = report;
      renderBoardPressure();
    } catch (_) {
      state.pressure = null;
      renderBoardPressure();
    }
  }

  let arena = null;
  let arenaHome = null;

  function restoreArenaNodes() {
    if (!arenaHome) return;
    for (const entry of arenaHome) {
      if (entry.node.id === "board-note-block" && $("quick-work-dialog")?.open) continue;
      entry.parent.insertBefore(entry.node, entry.next?.parentNode === entry.parent ? entry.next : null);
    }
    document.querySelector(".conversation").removeAttribute("role");
  }

  function setArenaMode(mode) {
    state.arenaMode = mode;
    for (const name of ["canvas", "plan", "tasks"]) {
      $("arena-" + name + "-tab").setAttribute("aria-pressed", String(name === mode));
      const id = name === "canvas" ? "project-canvas-root" : "arena-" + name;
      $(id).hidden = name !== mode;
    }
    if (mode === "canvas" && arena) arena.canvas.refresh();
    writeLocation();
  }

  function closeProjectArena() {
    state.openProject = "";
    state.boardProjectFilter = "";
    renderBoardStatus();
    writeLocation();
  }

  // Refresh means the default view, with current data. The button clears the filter
  // back to that view.
  function resetBoardView() {
    state.openProject = "";
    state.boardProjectFilter = "";
    state.boardCategory = "";
    state.boardFocus = "all";
    state.boardSearch = "";
    state.boardShowDone = false;
    const search = $("board-search");
    if (search) search.value = "";
    const focus = $("board-focus");
    if (focus) focus.value = "all";
    renderBoardStatus();
    writeLocation();
  }

  function syncProjectArena(project) {
    if (!window.TesseraProjectCanvas) return;
    const root = $("project-arena");
    if (!project) {
      arena?.canvas.destroy();
      arena = null;
      restoreArenaNodes();
      root.hidden = true;
      $("wip-global-work").hidden = false;
      return;
    }
    root.hidden = false;
    $("wip-global-work").hidden = true;
    $("arena-title").textContent = project.title;
    $("arena-plan-tab").textContent = project.kind === "subject" ? "Notes & approach" : "Plan & notes";
    if (arena?.projectId === project.id) return;
    arena?.canvas.destroy();
    if (!arenaHome) {
      arenaHome = [];
      for (const node of [document.querySelector(".conversation"), $("inspector"), $("wip-work-section"), $("board-note-block"), $("project-page-block")]) arenaHome.push({node, parent:node.parentNode, next:node.nextSibling});
    }
    $("arena-chat").append(document.querySelector(".conversation"));
    document.querySelector(".conversation").setAttribute("role", "region");
    $("arena-lens").append($("inspector"));
    $("arena-tasks").append($("board-note-block"), $("wip-work-section"));
    $("arena-plan").append($("project-page-block"));
    $("board-note-block").hidden = false;
    $("board-note-project").value = project.id;
    $("project-canvas-root").replaceChildren();
    const canvas = new window.TesseraProjectCanvas.ProjectCanvas($("project-canvas-root"), {
      projectId:project.id,
      api,
      format:fillFormatted,
      ask,
      session:() => state.slug,
      addTask:() => { setArenaMode("tasks"); $("board-note-what").focus(); },
      checkStep:async (id,done) => { await toggleStep(project.id,id,done); canvas.refresh(); },
      openTask:(id) => {
        setArenaMode("tasks");
        state.boardShowDone = false;
        const card = state.boardView?.cards.find(item=>item.card_id===id);
        if (card && ["succeeded","cancelled"].includes(card.status)) state.boardShowDone = true;
        state.boardFocus = "all"; state.boardSearch = "";
        $("board-search").value = ""; $("board-focus").value = "all";
        renderBoardStatus();
        for (const row of $("board-cards").querySelectorAll(".work-item")) if (row.dataset.identity === id) {
          row.querySelector("details").open = true;
          row.scrollIntoView({block:"nearest",behavior:"instant"});
        }
      },
      onSelection:(ids) => { $("arena-selection").textContent = ids.length ? ids.length + " selected pieces accompany your message" : "Conversation can create and edit the canvas"; },
    });
    arena = {projectId:project.id,canvas};
    const chooser = $("arena-session");
    chooser.replaceChildren();
    for (const session of state.sessions) if (!session.archived) {
      const option = element("option", "", session.title || session.slug);
      option.value = session.slug; chooser.append(option);
    }
    let preferred = state.slug;
    for (const slug of [project.manager,project.session_slug]) if (state.sessions.some(item=>item.slug===slug&&!item.archived)) { preferred=slug;break; }
    chooser.value = preferred || "";
    if (preferred && preferred !== state.slug) selectSession(preferred).catch(showError);
    setArenaMode(state.arenaMode || "canvas");
  }


  // A project opens as its own page: intent, plan, and the cards filed under it.
  async function openProject(projectId) {
    if (state.page !== "wip") return;
    state.openProject = projectId;
    state.boardProjectFilter = projectId;
    state.boardShowDone = false;
    state.boardFocus = "all";
    // A category only means something inside the project that carries it. Keeping it
    // across a project change would filter the new project by a label it may not have,
    // which reads as "no work here" when there is work.
    state.boardCategory = "";
    state.projectCompose = false;
    renderBoardStatus();
    // A project card opens to its shared planning space. The panel was always
    // rendered, but it sits below the project grid on the same
    // page, so a click appeared to do nothing. Bring it into view and mark the card
    // that is open, so the click visibly lands somewhere.
    const block = window.TesseraProjectCanvas ? $("project-arena") : $("project-page-block");
    if (block && !block.hidden && typeof block.scrollIntoView === "function") {
      block.scrollIntoView({block: "start", behavior: "smooth"});
    }
  }

  function renderProjectPage() {
    const panel = $("project-page");
    const project = projectById(state.openProject);
    $("page-wip").dataset.projectOpen = project ? "true" : "false";
    $("board-note-block").hidden = false;
    syncProjectArena(project);
    $("board-title").textContent = project ? (project.kind === "subject" ? "Related work" : "Project tasks") : "Work";
    // The category row shows which category is active, so its pressed state is part of
    // what this panel renders. Without it in the key, clicking a category changed the
    // card list but left the buttons looking unpressed.
    const key = JSON.stringify([project || null, state.boardCategory]);
    if (panel.renderKey === key) return;
    panel.renderKey = key;
    captureFieldText(panel);
    const oldNoteForm = panel.querySelector(".project-note-form");
    panel.replaceChildren();
    if (!project) {
      $("project-page-block").hidden = true;
      return;
    }
    $("project-page-block").hidden = false;
    // Mark the open project's card so the grid shows which one the panel belongs to.
    // Guarded like every other DOM scan here: the static rig has no querySelectorAll.
    if (typeof document.querySelectorAll === "function") {
      for (const node of document.querySelectorAll(".project-card")) {
        if (node.dataset.project === project.id) node.dataset.open = "true";
        else delete node.dataset.open;
      }
    }
    const head = element("div", "project-page-head");
    const back = element("button", "project-back", "← All work");
    back.type = "button";
    back.addEventListener("click", () => { state.openProject = ""; state.boardProjectFilter = ""; state.boardCategory = ""; renderBoardStatus(); });
    head.append(back, element("h2", "project-page-title", safeBoardTitle(project.title, "Untitled project")));
    panel.append(head);
    if (typeof project.description === "string" && project.description.trim()) panel.append(element("p", "project-desc", project.description));
    // The manager line said "none" and offered no way to change it, though the
    // route already accepts manager. Session assignment belongs to one project surface,
    // so the choice lives here, not only in a tool.
    const meta = element("p", "project-meta");
    meta.append(document.createTextNode("Manager "));
    const managerSelect = element("select", "project-manager");
    managerSelect.id = "project-manager-" + encodeURIComponent(project.id);
    managerSelect.setAttribute("aria-label", "Project manager");
    const none = element("option", "", "none");
    none.value = "";
    managerSelect.append(none);
    for (const row of state.sessions || []) {
      if (!row || row.archived) continue;
      const option = element("option", "", row.title || row.slug);
      option.value = row.slug;
      managerSelect.append(option);
    }
    managerSelect.value = project.manager || "";
    managerSelect.addEventListener("change", async () => {
      managerSelect.disabled = true;
      const notice = element("span", "quiet-note", "Saving…");
      meta.append(notice);
      try {
        const view = await api("/api/board/projects/" + encodeURIComponent(project.id) + "/update", "POST",
                               {manager: managerSelect.value});
        if (state.page !== "wip") return;
        applyBoardView(view);
      } catch (error) {
        notice.textContent = "Not saved: " + error.message;
        managerSelect.disabled = false;
      }
    });
    meta.append(managerSelect, document.createTextNode(" · " + projectStanding(project)));
    panel.append(meta);
    const ratio = projectStepRatio(project);
    if (ratio !== null) {
      const meter = element("div", "project-meter");
      const bar = element("div", "project-bar");
      bar.setAttribute("role", "progressbar");
      bar.setAttribute("aria-valuemin", "0");
      bar.setAttribute("aria-valuemax", "100");
      bar.setAttribute("aria-valuenow", String(Math.round(ratio * 100)));
      bar.setAttribute("aria-label", "Plan progress");
      const fill = element("div", "project-bar-fill");
      fill.style.width = Math.round(ratio * 100) + "%";
      bar.append(fill);
      meter.append(bar, element("span", "project-meter-text", `${project.steps_done} of ${project.steps_total} steps`));
      panel.append(meter);
    }

    const steps = Array.isArray(project.steps) ? project.steps : [];
    const planHead = element("div", "ds-section-head");
    planHead.append(element("h3", "ds-kicker", project.kind === "subject" ? "Approach (optional)" : "Plan"), element("span", "ds-count", `${project.steps_done} of ${project.steps_total}`));
    panel.append(planHead);
    if (!steps.length) {
      panel.append(element("p", "quiet-note", project.kind === "subject" ? "A shared home for related symptoms, fixes and lessons. Add a plan when it helps." : "Add steps when you are ready to shape the plan."));
    } else {
      const list = element("ol", "project-steps");
      for (const step of steps) {
        const row = element("li", "project-step");
        if (step.done === true) row.dataset.done = "true";
        const box = element("input", "project-step-box");
        box.type = "checkbox";
        box.checked = step.done === true;
        box.setAttribute("aria-label", "Mark step done: " + step.text);
        box.addEventListener("change", () => toggleStep(project.id, step.id, box.checked).catch(showError));
        row.append(box, element("span", "project-step-text", step.text));
        list.append(row);
      }
      panel.append(list);
    }

    const filed = [];
    const cards = state.boardView && Array.isArray(state.boardView.cards) ? state.boardView.cards : [];
    for (const card of cards) if (card.project === project.id) filed.push(card);

    // Categories are the title prefixes this project's own cards carry ("UI:",
    // "Backend:", "Bugs:", ...). They are a way to sort work inside ONE project, not
    // a reason to create a subject project per category. Cards can sit on one planning
    // project and carry category prefixes, so category-specific sorting has to work
    // here. Counting the live cards means the row disappears when there is nothing to
    // sort.
    const categoryCounts = new Map();
    for (const card of filed) {
      const label = boardCategory(viewCard(card.ordinal)?.title || card.title);
      if (!label) continue;
      categoryCounts.set(label, (categoryCounts.get(label) || 0) + 1);
    }
    // One category is not a choice, it is the whole project. Show the row only when
    // sorting can actually change what the reader sees.
    if (categoryCounts.size > 1) {
      const row = element("div", "project-categories");
      row.setAttribute("role", "group");
      row.setAttribute("aria-label", "Sort work in this project by category");
      const every = element("button", "board-filter", `All categories (${filed.length})`);
      every.type = "button";
      every.setAttribute("aria-pressed", String(!state.boardCategory));
      every.addEventListener("click", () => { state.boardCategory = ""; renderBoardStatus(); });
      row.append(every);
      const labels = [...categoryCounts.keys()].sort((a, b) => a.localeCompare(b));
      for (const label of labels) {
        const count = categoryCounts.get(label);
        const button = element("button", "board-filter", `${label} (${count})`);
        button.type = "button";
        button.setAttribute("aria-pressed", String(state.boardCategory === label));
        button.addEventListener("click", () => { state.boardCategory = label; renderBoardStatus(); });
        row.append(button);
      }
      panel.append(row);
    }

    const addTask = element("button", "primary", "Add task to this project");
    addTask.type = "button";
    addTask.addEventListener("click", () => {
      state.projectCompose = true;
      $("board-note-block").hidden = false;
      $("board-note-project").value = project.id;
      $("board-note-what").focus();
      $("board-note-block").scrollIntoView({block:"start",behavior:"instant"});
    });
    panel.append(addTask);
    const stepForm = element("form", "project-step-form");
    const stepField = element("input", "");
    stepField.id = "project-step-" + encodeURIComponent(project.id);
    stepField.placeholder = "Add the next step in the plan";
    stepField.setAttribute("aria-label", "New plan step");
    stepField.required = true;
    const stepSave = element("button", "", "Add step");
    stepSave.type = "submit";
    const stepNotice = element("p", "project-save-state");
    stepNotice.id = "project-save-add-step-" + encodeURIComponent(project.id);
    stepNotice.setAttribute("role", "status");
    stepForm.append(stepField, stepSave, stepNotice);
    stepForm.addEventListener("submit", event => { event.preventDefault(); appendProjectText(project.id, "add-step", stepField, stepSave, stepNotice); });
    panel.append(stepForm);
    // Artifacts are what a project produced. A reference can be a private path, so
    // the board shows how many a project holds and which card carries them, never
    // the reference itself. Opening the card opens the evidence.
    const artifacts = [];
    for (const card of filed) {
      const count = nonnegativeInteger(card.artifact_count) ? card.artifact_count : 0;
      if (count) artifacts.push({card, count});
    }
    const totalArtifacts = artifacts.reduce((sum, row) => sum + row.count, 0);
    const artifactHead = element("div", "ds-section-head");
    artifactHead.append(element("h3", "ds-kicker", "Artifacts"), element("span", "ds-count", String(totalArtifacts)));
    panel.append(artifactHead);
    if (!totalArtifacts) panel.append(element("p", "quiet-note", "No artifacts attached yet."));
    else {
      const list = element("ul", "project-artifacts");
      for (const {card, count} of artifacts) {
        const item = element("li", "project-artifact");
        item.append(element("span", "project-artifact-ref", `${count} ${count === 1 ? "artifact" : "artifacts"}`),
                    element("span", "project-artifact-card", safeBoardTitle(card.title, "Card " + card.ordinal)));
        list.append(item);
      }
      panel.append(list);
    }

    // Sessions that touched this project, from what the board itself records: the
    // project's creator and manager, each filed card's creator and last writer. It is
    // a floor rather than a full log: compaction keeps one record per card, so a
    // superseded writer and a session that only read are not evidenced here. The
    // section says "touched" and names the count, so it does not claim a complete
    // history it cannot support.
    const participants = Array.isArray(project.sessions) ? project.sessions : [];
    const historyHead = element("div", "ds-section-head");
    historyHead.append(element("h3", "ds-kicker", "Sessions that touched this project"),
                       element("span", "ds-count", String(participants.length)));
    panel.append(historyHead);
    if (!participants.length) {
      panel.append(element("p", "quiet-note", "No session is recorded against this project yet."));
    } else {
      const history = element("ul", "project-sessions");
      for (const row of participants) {
        const slug = typeof row.slug === "string" ? row.slug : "";
        if (!slug) continue;
        const item = element("li", "project-session");
        const cards = nonnegativeInteger(row.cards) ? row.cards : 0;
        item.append(element("span", "project-session-slug", slug),
                    element("span", "project-session-cards",
                            cards ? `${cards} ${cards === 1 ? "card" : "cards"}` : "no cards filed"));
        history.append(item);
      }
      panel.append(history);
    }

    // Notes are the manager's own record: thinking that is not yet a card, and
    // must never become actionable without an explicit request.
    const notesHead = element("div", "ds-section-head");
    notesHead.append(element("h3", "ds-kicker", "Shared planning notes"), element("span", "ds-count", "human + agent"));
    panel.append(notesHead);
    if (typeof project.note === "string" && project.note.trim()) panel.append(element("p", "project-note", project.note));
    else panel.append(element("p", "quiet-note", "No notes yet. Notes record thinking; they do not create work."));
    const form = element("form", "project-note-form");
    const label = element("label", "visually-hidden", "Project note");
    const field = element("textarea", "project-note-input");
    field.rows = 3;
    field.id = "project-note-" + encodeURIComponent(project.id);
    field.placeholder = "Add a plan, decision, update, document or pull-request link.";
    field.setAttribute("aria-label", "Project note");
    label.setAttribute("for", field.id);
    const save = element("button", "primary", "Save note");
    save.type = "submit";
    const status = element("p", "quiet-note");
    status.id = "project-save-note-" + encodeURIComponent(project.id);
    status.hidden = true;
    status.setAttribute("role", "status");
    form.append(label, field, save, status);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      appendProjectText(project.id, "note", field, save, status);
    });
    panel.append(oldNoteForm?.querySelector("textarea")?.value ? oldNoteForm : form);
    const refs = element("section", "project-references");
    refs.append(element("h3", "", "Documents & links"));
    const urls = new Set();
    for (const match of (project.note + " " + project.description).matchAll(/https?:\/\/[^\s<>]+/g)) urls.add(match[0].replace(/[).,;]+$/, ""));
    for (const url of urls) {
      const link = element("a", "project-reference", url);
      link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer";
      refs.append(link);
    }
    if (!urls.size) refs.append(element("p", "", "Add document, repository, issue or pull-request links in a shared note."));
    panel.append(refs);
    adoptPendingFieldText();
  }

  async function appendProjectText(projectId, action, field, button, notice) {
    const text = field.value.trim();
    if (!text) return;
    button.disabled = true;
    notice.hidden = false;
    notice.id = "project-save-" + action + "-" + encodeURIComponent(projectId);
    notice.textContent = "Saving…";
    try {
      const view = await api("/api/board/projects/" + encodeURIComponent(projectId) + "/" + action, "POST", {text});
      field.value = "";
      delete state.pendingFieldText[field.id];
      applyBoardView(view);
      const current = $(notice.id) || notice;
      current.hidden = false;
      current.textContent = "Saved.";
    } catch (error) { notice.textContent = "Not saved: " + error.message; }
    finally { button.disabled = false; }
  }

  async function toggleStep(projectId, stepId, done) {
    const view = await api("/api/board/projects/" + encodeURIComponent(projectId) + "/step", "POST", {step_id: stepId, done});
    if (state.page !== "wip") return;
    applyBoardView(view);
  }

  // The headline is a sentence about the work, not a total. "20 cards" says nothing a
  // reader can act on; "1 open · 19 completed" says whether the board is clear.
  function boardSummary(cards) {
    const live = cards.filter((card) => card && card.status !== "succeeded" && card.status !== "cancelled");
    const done = cards.length - live.length;
    const parts = [];
    if (!live.length) {
      parts.push(done ? `${done} completed, nothing open` : "No cards");
    } else {
      const running = live.filter((card) => card.status === "running").length;
      const blocked = live.filter((card) => card.status === "blocked").length;
      const waiting = live.length - running - blocked;
      parts.push(`${live.length} open`);
      if (running) parts.push(`${running} running`);
      if (blocked) parts.push(`${blocked} blocked`);
      if (waiting) parts.push(`${waiting} waiting`);
      const loose = live.filter((card) => !card.project).length;
      if (loose) parts.push(`${loose} unfiled`);
    }
    if (done && live.length) parts.push(`${done} completed`);
    return parts.join(" · ");
  }

  function paintBoardChrome(cards) {
    const activity = $("wip-activity");
    activity.replaceChildren();
    if (!window.TesseraDesign) return;
    const items = [];
    for (const card of cards || []) {
      const item = boardActivityItem(card, items.length + 1);
      if (item) items.push(item);
    }
    items.sort((left, right) => right.stamp - left.stamp);
    const visible = items.slice(0, 8).map(({tone, time, text}) => ({tone, time, text}));
    activity.append(window.TesseraDesign.activityFeed(visible, visible.some((item) => item.time)));
  }

  function boardActivityItem(card, fallbackOrdinal) {
    if (!card || typeof card !== "object") return null;
    const ordinal = nonnegativeInteger(card.ordinal) && card.ordinal > 0 ? card.ordinal : fallbackOrdinal;
    const overlay = viewCard(card.ordinal);
    const title = safeBoardTitle(overlay && overlay.title, "Card " + ordinal);
    const status = boardStatusLabel(card.status);
    const stamp = overlayStamp(overlay);
    const project = projectById(overlay && overlay.project);
    const prefix = project ? project.title + ": " : "";
    return {
      tone: boardStatusTone(card.status),
      time: stamp === null ? "" : relativeAge(stamp),
      text: prefix + title + " · " + status,
      stamp: stamp === null ? 0 : stamp,
    };
  }

  function boardStatusLabel(status) {
    if (typeof status === "string" && Object.hasOwn(boardLabels, status)) return boardLabels[status];
    return "Status not reported";
  }

  function boardStatusTone(status) {
    if (status === "failed") return "danger";
    if (status === "blocked") return "attention";
    if (status === "succeeded" || status === "running") return "ready";
    return "";
  }

  function overlayStamp(overlay) {
    if (!overlay || typeof overlay.updated_at !== "number" || !Number.isFinite(overlay.updated_at)) return null;
    return overlay.updated_at;
  }

  function relativeAge(stamp) {
    const age = Date.now() / 1000 - stamp;
    if (!Number.isFinite(age)) return "";
    if (age < 0 && age > -5) return "now";
    if (age < 0) return "";
    return durationText(age);
  }

  function passiveBoardCard(card) {
    if (!card || typeof card !== "object" || Array.isArray(card)) return card;
    const copy = {};
    for (const key of ["ordinal", "status", "attempt", "artifact_count", "question_pending", "session_ordinal", "log_start", "log_end", "version", "updated_at"]) {
      if (Object.hasOwn(card, key)) copy[key] = card[key];
    }
    return copy;
  }

  function applyBoardView(view) {
    if (!view || typeof view !== "object" || Array.isArray(view) || !Array.isArray(view.cards)) return;
    state.boardView = view;
    state.status.board_status = {...view, cards: view.cards.map(passiveBoardCard)};
    renderBoardStatus();
  }

  async function refreshBoardView() {
    const epoch = state.boardViewEpoch + 1;
    state.boardViewEpoch = epoch;
    try {
      const view = await api("/api/board/view");
      if (epoch !== state.boardViewEpoch || state.page !== "wip") return;
      if (!view || typeof view !== "object" || Array.isArray(view) || !Array.isArray(view.cards)) {
        state.boardView = null;
        return;
      }
      // The view carries its own revision and titles. Adopt it.
      applyBoardView(view);
    } catch (error) {
      if (error.status === 401 || error.status === 403) {
        state.statusEpoch += 1;
        statusUnavailable();
      }
    }
  }

  // An unfiled card can be filed from the card itself. The picker lists the open
  // projects; choosing one files the card and the board redraws on the new revision.
  function fileCardControl(ordinal, cardId) {
    const view = state.boardView;
    const projects = view && Array.isArray(view.projects) ? view.projects.filter((row) => row && row.status === "active") : [];
    if (!state.boardRevision || !projects.length) return null;
    const select = element("select", "board-file-select");
    select.setAttribute("aria-label", "File this card under a project");
    const placeholder = element("option", "", "File under…");
    placeholder.value = "";
    select.append(placeholder);
    for (const project of projects) {
      const option = element("option", "", safeBoardTitle(project.title, "Untitled project"));
      option.value = project.id;
      select.append(option);
    }
    select.addEventListener("change", () => {
      if (select.value) fileCard(ordinal, select.value, select, cardId).catch(showError);
    });
    return select;
  }

  async function fileCard(ordinal, project, select, cardId) {
    const revision = state.boardRevision;
    if (!revision || state.page !== "wip") return;
    select.disabled = true;
    try {
      const view = await boardWrite("/api/board/cards/" + ordinal + "/file", {revision, project}, cardId);
      if (state.boardRevision !== revision || state.page !== "wip") return;
      applyBoardView(view);
    } catch (error) {
      select.disabled = false;
      throw error;
    }
  }

  async function moveCard(ordinal, status, cardId) {
    const revision = state.boardRevision;
    if (!revision || state.page !== "wip") return;
    try {
      const view = await boardWrite("/api/board/cards/" + ordinal + "/status", {revision, status}, cardId);
      if (state.boardRevision !== revision || state.page !== "wip") return;
      applyBoardView(view);
    } catch (error) {
      if (error.status === 401 || error.status === 403) {
        state.statusEpoch += 1;
        statusUnavailable();
      } else if (error.status === 409) $("board-summary").textContent = boardConflictText(error);
      else $("board-summary").textContent = "Move failed";
    }
  }

  // Answering a card question is what unblocks the work. The card returns to the open
  // pool by itself; the person does not have to also move it.
  async function answerCard(ordinal, questionId, answer, button, notice, cardId) {
    const revision = state.boardRevision;
    // Same rule as a note: naming the card is enough to land the answer, and a button
    // that quietly does nothing is the bug this card reports.
    if (state.page !== "wip") return;
    if (!revision && !cardId) {
      notice.hidden = false;
      notice.textContent = "The board has not loaded yet. Try again in a moment.";
      return;
    }
    button.disabled = true;
    notice.hidden = false;
    notice.textContent = "Sending…";
    try {
      const view = await boardWrite("/api/board/cards/" + ordinal + "/answer", {revision, question_id: questionId, answer}, cardId);
      applyBoardView(view);
    } catch (error) {
      notice.textContent = error.status === 409 ? boardConflictText(error) : "Answer failed";
      button.disabled = false;
    }
  }

  function clearTaskFilters() {
    $("tasks-filters").replaceChildren();
    $("tasks-filters").hidden = true;
    $("tasks-filter-result").textContent = "";
    $("tasks-filter-result").hidden = true;
  }

  function taskStatusKey(task) {
    if (typeof task.status === "string" && Object.hasOwn(taskLabels, task.status)) return task.status;
    return "unreported";
  }

  function renderTaskRows(tasks) {
    const list = $("tasks-records");
    list.replaceChildren();
    let visible = 0;
    for (let index = 0; index < tasks.length; index += 1) {
      const task = tasks[index];
      const key = taskStatusKey(task);
      if (state.taskFilter !== "all" && state.taskFilter !== key) continue;
      visible += 1;
      const row = element("li", "provider-row");
      const heading = element("div");
      heading.append(statusMark(key === "failed" ? "danger" : key === "succeeded" || key === "running" ? "ready" : ""), element("h3", "", "Task " + (index + 1)));
      const attempt = Number.isSafeInteger(task.attempt) && task.attempt >= 0 ? "Attempt " + task.attempt : "Attempt not reported";
      const account = Number.isSafeInteger(task.account_ordinal) && task.account_ordinal > 0 ? "Account " + task.account_ordinal : "Account not reported";
      heading.append(element("p", "data", attempt + " · " + account));
      const badge = element("span", "status-badge", key === "unreported" ? "Status not reported" : taskLabels[key]);
      badge.dataset.taskStatus = key;
      row.append(heading, badge);
      list.append(row);
    }
    const label = state.taskFilter === "all" ? "All statuses" : state.taskFilter === "unreported" ? "Unreported status" : taskLabels[state.taskFilter] + " status";
    $("tasks-filter-result").textContent = `Showing ${visible} of ${tasks.length} reported records. ${label}.`;
    $("tasks-filter-result").hidden = false;
    if (!visible) list.append(element("li", "quiet-note", tasks.length ? "No records match this status in the reported snapshot." : "No task records to display."));
  }

  function renderTaskFilters(tasks, keepFocus) {
    const filters = $("tasks-filters");
    filters.replaceChildren();
    filters.hidden = false;
    const counts = {all: tasks.length, queued: 0, running: 0, succeeded: 0, failed: 0, cancelled: 0, unreported: 0};
    for (const task of tasks) counts[taskStatusKey(task)] += 1;
    const labels = {all: "All", ...taskLabels, unreported: "Unreported"};
    for (const [key, label] of Object.entries(labels)) {
      if (key === "unreported" && counts[key] === 0 && state.taskFilter !== key) continue;
      const button = element("button", "task-filter", label + " (" + counts[key] + ")");
      button.type = "button";
      button.dataset.filter = key;
      button.setAttribute("aria-pressed", String(state.taskFilter === key));
      button.setAttribute("aria-controls", "tasks-records");
      button.addEventListener("click", () => {
        state.taskFilter = key;
        for (const item of filters.children) item.setAttribute("aria-pressed", String(item === button));
        renderTaskRows(tasks);
      });
      filters.append(button);
      if (keepFocus && state.taskFilter === key) button.focus({ preventScroll: true });
    }
    renderTaskRows(tasks);
  }

  function renderTaskStatus() {
    let report = state.status.task_status;
    if (!report || typeof report !== "object" || Array.isArray(report)) report = {};
    const list = $("tasks-records");
    const keepFocus = Array.from($("tasks-filters").children).includes(document.activeElement);
    list.replaceChildren();
    clearTaskFilters();
    let label = "Not reported";
    let summary = "Not reported";
    if (report.enabled === false) {
      label = "Off";
      summary = "Disabled in config.toml";
      list.append(element("li", "quiet-note", "Set swarm.enabled in config.toml; Settings does not expose this lane."));
    } else if (report.configured === false) {
      // Same dead end as the jobs panel: Settings cannot set swarm.tasks_file.
      label = "Not configured";
      summary = "Configured in config.toml";
      list.append(element("li", "quiet-note", "Settings does not expose the tasks lane; set swarm in config.toml."));
    } else if (report.enabled === true && report.configured === true) {
      const tasks = providerRecords(report.tasks);
      if (tasks) {
        label = tasks.length ? "Snapshot" : "Empty";
        summary = tasks.length ? `${tasks.length} ${tasks.length === 1 ? "task" : "tasks"}` : "No tasks";
        // task_limit is a bound, not a filter. Without this the visible rows read
        // as the whole task store.
        if (Number.isInteger(report.tasks_omitted) && report.tasks_omitted > 0) {
          summary += ` · ${report.tasks_omitted} older tasks not shown (${report.task_limit}-task limit)`;
        }
        renderTaskFilters(tasks, keepFocus);
      } else summary = "No snapshot yet";
    }
    setIntegrationBadge("tasks-state", label);
    $("tasks-summary").textContent = summary;
    if (!list.children.length) list.append(element("li", "quiet-note", "No task records to display."));
  }

  function providerAvailability(record) {
    if (record.configured === false) return { label: "Not configured", detail: "Not configured", tone: "" };
    if (record.health === "error") {
      const categories = {
        authentication: "Authentication error", permission: "Permission denied", rate_limit: "Rate limited",
        quota: "Quota exhausted", timeout: "Request timed out", connection: "Connection error",
        provider: "Provider error", configuration: "Configuration error",
      };
      let category = "Unclassified error";
      if (typeof record.error_category === "string" && Object.hasOwn(categories, record.error_category)) {
        category = categories[record.error_category];
      }
      return { label: "Error", detail: category + ". Review the server configuration or private diagnostics.", tone: "attention" };
    }
    if (record.credential === "missing") return { label: "Needs credentials", detail: "Credential missing", tone: "attention" };
    if (record.configured === true && record.health === "healthy") return { label: "Healthy", detail: "Healthy", tone: "ready" };
    if (record.configured === true) return { label: "Configured", detail: "Health not reported", tone: "" };
    return { label: "Not reported", detail: "Not reported", tone: "" };
  }

  function providerCredential(record) {
    if (record.credential === "available") return "Available to the server";
    if (record.credential === "missing") return "Missing";
    if (record.credential === "not_required") return "Not required";
    return "Not reported";
  }

  function appendProviderRow(list, title, record) {
    const availability = providerAvailability(record);
    const row = element("li", "provider-row");
    const heading = element("div");
    heading.append(element("h4", "", title), element("p", "", "Credentials: " + providerCredential(record)));
    const badge = element("span", "status-badge", availability.label);
    badge.dataset.state = availability.tone;
    row.append(heading, badge, element("p", "provider-detail", availability.detail));
    list.append(row);
    return availability.label;
  }

  function setIntegrationBadge(id, text, tone = "") {
    $(id).textContent = text;
    $(id).dataset.state = tone;
  }

  function renderServerSettings(names, status) {
    $("settings-connection").textContent = "Core connected";
    const count = status.session_count;
    // session_count counts records on disk; the sidebar renders only the readable
    // ones. Both numbers are true, and saying only the first leaves a person
    // comparing "14 sessions" against a list of 13 with no explanation.
    const broken = Array.isArray(status.sessions_unreadable) ? status.sessions_unreadable.length : 0;
    const sessions = Number.isInteger(count) ? count + (count === 1 ? " session" : " sessions") : "? sessions";
    $("settings-session-count").textContent = broken
      ? `${sessions}, ${broken} unreadable` : sessions;
    const platform = $("settings-platform");
    if (platform) {
      const limits = status && status.host && Array.isArray(status.host.limits) ? status.host.limits : [];
      platform.textContent = limits.filter((line) => typeof line === "string" && line).join(" ");
    }
  }

  function renderVoiceSessions() {
    const select = $("voice-session");
    const current = state.slug || "";
    select.replaceChildren();
    const live = state.sessions.filter((session) => !session.archived);
    if (!live.length) select.append(element("option", "", "No session"));
    // Neither the autonomy session nor the dedicated voice session is one more chat
    // to talk through, so they do not sit in the same flat list as ten ordinary
    // threads. Same server flags as the sidebar, so renaming either keeps its group.
    const groups = [["Autonomy", live.filter((session) => session.infinity)],
                    ["Voice", live.filter((session) => !session.infinity && session.voice)],
                    ["Bots", live.filter((session) => !session.infinity && !session.voice)]];
    for (const [label, members] of groups) {
      if (!members.length) continue;
      const group = element("optgroup");
      group.label = label;
      for (const session of members) {
        const option = element("option", "", session.title);
        option.value = session.slug;
        group.append(option);
      }
      select.append(group);
    }
    // The dedicated voice session is the default target, because that is what the
    // Voice page is for. A reader who picked another target keeps it; the open
    // chat is only the fallback on a home with no voice session yet.
    const dedicated = live.find((session) => session.voice);
    const wanted = state.voiceTarget || (dedicated ? dedicated.slug : current);
    select.value = wanted;
    select.disabled = voice?.running() || !live.length;
  }

  const integrations = {rows: [], busy: new Set(), epoch: 0, testedAt: 0};

  async function refreshIntegrations() {
    const epoch = ++integrations.epoch;
    try {
      const reply = await api("/api/integrations");
      if (epoch !== integrations.epoch) return;
      const previous = new Map(integrations.rows.map((row) => [row.id, row.result]));
      integrations.rows = (Array.isArray(reply.integrations) ? reply.integrations : []).map((row) => row.result || !previous.get(row.id) ? row : {...row, result: previous.get(row.id)});
      $("integrations-state").textContent = "";
    } catch (error) {
      if (epoch !== integrations.epoch) return;
      integrations.rows = [];
      $("integrations-state").textContent = "Unavailable: " + (error.message || "request failed");
    }
    renderIntegrations();
    if (epoch === integrations.epoch && Date.now() - integrations.testedAt > 60000) testAllIntegrations();
  }

  function testAllIntegrations() {
    integrations.testedAt = Date.now();
    return Promise.all(integrations.rows.filter((row) => row.checkable && !integrations.busy.has(row.id)).map((row) => checkIntegration(row.id)));
  }

  function integrationResult(row) {
    if (integrations.busy.has(row.id)) return ["Testing…", "attention"];
    if (!row.result) return [row.checkable ? "Not tested" : "Configured", ""];
    return [row.result.ok ? "OK" : "Failed", row.result.ok ? "ready" : "danger"];
  }

  function integrationCard(row, previous) {
    const item = element("li", "integ-row");
    item.dataset.id = row.id;
    item.dataset.enabled = String(row.enabled === true);
    const head = element("div", "integ-head");
    const [label, tone] = integrationResult(row);
    item.dataset.tone = tone;
    const dot = element("span", "status-dot");
    dot.dataset.tone = tone;
    dot.setAttribute("aria-hidden", "true");
    const title = element("div", "integ-title");
    title.append(element("h3", "", row.name), element("span", "integ-type", row.type));
    head.append(dot, title);
    const badge = element("span", "status-badge", label);
    badge.dataset.tone = tone;
    head.append(badge);
    item.append(head);
    if (row.target) item.append(element("p", "integ-target", row.target));
    if (row.detail) item.append(element("p", "integ-facts", row.detail));
    if (row.result) {
      item.append(element("p", row.result.ok ? "integ-result" : "integ-result form-error", row.result.detail));
      const facts = [];
      if (Number.isInteger(row.result.ms)) facts.push("Check duration " + row.result.ms.toLocaleString() + " ms");
      const checked = new Date(row.result.checked_at);
      if (row.result.checked_at && !Number.isNaN(checked.getTime())) facts.push("Checked " + checked.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}));
      if (facts.length) item.append(element("p", "integ-check-time", facts.join(" · ")));
      if (Array.isArray(row.result.tools) && row.result.tools.length) {
        const tools = element("details", "integ-tools");
        tools.open = previous?.querySelector(".integ-tools")?.open || false;
        const kind = row.group === "Backends" ? "models" : "tools";
        const names = element("ul", "capability-list");
        for (const name of row.result.tools) names.append(element("li", "capability-chip", name));
        tools.append(element("summary", "", row.result.tools.length + " " + kind + " available"), names);
        item.append(tools);
      }
    }
    const actions = element("div", "integ-actions");
    const toggle = element("label", "integ-switch");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = row.enabled === true;
    box.disabled = integrations.busy.has(row.id) || (row.main === true && row.enabled === true);
    box.addEventListener("change", () => setIntegrationEnabled(row, box.checked));
    toggle.append(box, document.createTextNode(" " + row.switch));
    actions.append(toggle);
    if (row.checkable) {
      const test = element("button", "", "Test");
      test.type = "button";
      test.disabled = integrations.busy.has(row.id);
      test.addEventListener("click", () => checkIntegration(row.id));
      actions.append(test);
    }
    // A failed row with a refreshable sign-in gets a repair, not just another Test.
    if (row.repair && row.result && !row.result.ok) {
      const repair = element("button", "", "Refresh sign-in");
      repair.type = "button";
      repair.disabled = integrations.busy.has(row.id);
      repair.addEventListener("click", () => repairIntegration(row));
      actions.append(repair);
    }
    if (row.pending_restart) actions.append(element("span", "data", "Restart core to apply"));
    item.append(actions);
    return item;
  }

  function renderIntegrations() {
    const list = $("integrations-list");
    const previous = new Map();
    for (const node of list.children) if (node.dataset.id) previous.set(node.dataset.id, node);
    const query = $("integration-search").value.trim().toLowerCase();
    const filter = $("integration-filter").value;
    const groups = new Map();
    let passing = 0;
    let failed = 0;
    let enabled = 0;
    let shown = 0;
    for (const row of integrations.rows) {
      if (row.enabled) enabled += 1;
      if (row.result?.ok === true) passing += 1;
      if (row.result?.ok === false) failed += 1;
      if (!groups.has(row.group)) groups.set(row.group, []);
      groups.get(row.group).push(row);
    }
    $("integrations-overview").textContent = integrations.rows.length + " connections · " + enabled + " enabled · " + passing + " passing checks · " + failed + " failed checks";
    const nodes = [];
    for (const [group, rows] of groups) {
      const heading = element("li", "integ-group", group);
      let groupShown = 0;
      nodes.push(heading);
      for (const row of rows) {
        const key = JSON.stringify([row, integrations.busy.has(row.id)]);
        let item = previous.get(row.id);
        if (!item || item.renderKey !== key) item = integrationCard(row, item);
        item.renderKey = key;
        const searchable = [row.name, row.type, row.target, row.detail, ...(row.result?.tools || [])].join(" ").toLowerCase();
        const matches = (!query || searchable.includes(query)) && (filter !== "attention" || row.result?.ok === false) && (filter !== "enabled" || row.enabled === true);
        item.hidden = !matches;
        if (matches) { shown += 1; groupShown += 1; }
        nodes.push(item);
      }
      heading.hidden = groupShown === 0;
    }
    replaceChangedChildren(list, nodes);
    $("integration-empty").hidden = shown > 0 || !integrations.rows.length;
    if (!integrations.rows.length && !$("integrations-state").textContent) $("integrations-state").textContent = "None configured";
  }

  async function checkIntegration(id) {
    integrations.busy.add(id);
    renderIntegrations();
    try {
      const row = await api("/api/integrations/" + encodeURIComponent(id) + "/check", "POST", {});
      integrations.rows = integrations.rows.map((item) => item.id === id ? row : item);
    } catch (error) {
      integrations.rows = integrations.rows.map((item) => item.id === id ? {...item, result: {ok: false, detail: error.message || "request failed"}} : item);
    } finally {
      integrations.busy.delete(id);
      renderIntegrations();
    }
  }

  async function repairIntegration(row) {
    // A Failed row offered only Test, which re-tests the same broken credential, so
    // the row said Failed and offered no repair options.
    // When the backend's credential is an OAuth grant this harness can refresh,
    // the repair is a refresh; if that fails the grant is gone and the fix is a
    // sign-in on the settings page. Either way the row says what happened.
    if (!row.repair) return;
    integrations.busy.add(row.id);
    renderIntegrations();
    let message = "";
    try {
      const result = await api("/api/oauth/refresh", "POST", {provider: row.repair});
      message = result.message || "Refreshed.";
    } catch (error) {
      message = row.name + ": refresh failed - " + (error.message || "not refreshed") + " Sign in again on Settings.";
    }
    integrations.busy.delete(row.id);
    renderIntegrations();
    $("integrations-state").textContent = row.name + ": " + message;
    checkIntegration(row.id);
  }

  async function setIntegrationEnabled(row, enabled) {
    integrations.busy.add(row.id);
    renderIntegrations();
    try {
      await api("/api/integrations/" + encodeURIComponent(row.id) + "/enabled", "POST", {enabled});
      $("integrations-state").textContent = "";
    } catch (error) {
      $("integrations-state").textContent = row.name + ": " + (error.message || "not saved");
    } finally {
      integrations.busy.delete(row.id);
      await refreshIntegrations();
    }
  }

  // These telemetry nodes have native controls and no per-node event handlers.
  // Update their contents in place so periodic reads retain focus and disclosures.
  function updateSparkElement(current, next) {
    for (const attribute of [...current.attributes]) {
      if (attribute.name !== "open" && !next.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
    }
    for (const attribute of next.attributes) {
      if (attribute.name !== "open" && current.getAttribute(attribute.name) !== attribute.value) current.setAttribute(attribute.name, attribute.value);
    }
    const wanted = [...next.childNodes];
    for (let index = 0; index < wanted.length; index += 1) {
      const incoming = wanted[index];
      const existing = current.childNodes[index];
      if (!existing) { current.append(incoming); continue; }
      if (existing.nodeType === 3 && incoming.nodeType === 3) {
        if (existing.data !== incoming.data) existing.data = incoming.data;
      } else if (existing.nodeType === 1 && incoming.nodeType === 1 && existing.tagName === incoming.tagName) {
        updateSparkElement(existing, incoming);
      } else existing.replaceWith(incoming);
    }
    while (current.childNodes.length > wanted.length) current.lastChild.remove();
  }

  function updateSparkChildren(container, nodes) {
    const next = container.cloneNode(false);
    next.append(...nodes);
    updateSparkElement(container, next);
  }

  function sparkValue(value, unit = "") {
    if (typeof value !== "number" || !Number.isFinite(value)) return "Not reported";
    return new Intl.NumberFormat(undefined, {maximumFractionDigits: 1}).format(value) + unit;
  }

  // Each row is [label, value, unit, ratio?, series?, tone?]. A ratio adds a
  // meter only when the payload reports the ceiling it is measured against; a
  // series adds the recent shape beside the number. Neither is invented when
  // the payload is silent: a reading with no known ceiling gets no meter.
  function sparkMetrics(rows) {
    const dl = element("dl", "spark-readings");
    const design = typeof window === "undefined" ? null : window.TesseraDesign;
    for (const [label, value, unit, ratio, series, tone] of rows) {
      const group = element("div", "");
      group.append(element("dt", "", label), element("dd", "", sparkValue(value, unit)));
      if (design && typeof ratio === "number" && Number.isFinite(ratio)) {
        const bounded = Math.max(0, Math.min(1, ratio));
        group.append(design.meter(bounded, tone || usageTone(bounded)));
      }
      if (design && Array.isArray(series)) {
        const spark = design.sparkline(series);
        if (spark) group.append(spark);
      }
      dl.append(group);
    }
    return dl;
  }

  // Low headroom is the bad direction, so the tone inverts: a nearly empty
  // meter is a nearly full card and must not read as safe green.
  function headroomTone(ratio) {
    if (ratio < 0.1) return "danger";
    if (ratio < 0.2) return "warn";
    return "ok";
  }

  // Recent shape for one reading: the range-aware history when SparkDash has it,
  // else the live one-hour buffer. Downsampled because a sparkline is 120 units
  // wide and 1800 samples buy nothing there.
  function sparkSeries(spark, historyKey, bufferKey) {
    const points = spark.history?.points || [];
    if (spark.history?.enabled === true && points.length > 1) {
      const values = [];
      for (const point of points) {
        const cell = point[historyKey];
        values.push(cell && Number.isFinite(cell.avg) ? cell.avg : null);
      }
      if (values.some((value) => typeof value === "number")) return values;
    }
    const buffer = spark.gpu_history?.[bufferKey];
    if (!Array.isArray(buffer) || buffer.length < 2) return null;
    const step = Math.max(1, Math.floor(buffer.length / 60));
    const values = [];
    for (let index = 0; index < buffer.length; index += step) values.push(buffer[index]);
    return values;
  }

  function sparkChart(times, values, label, unit) {
    const figure = element("figure", "spark-chart");
    figure.append(element("figcaption", "", label));
    const valid = [];
    for (let i = 0; i < times.length; i += 1) {
      if (Number.isFinite(times[i]) && Number.isFinite(values[i])) valid.push([times[i], values[i]]);
    }
    if (!valid.length) {
      figure.append(element("p", "", "No recorded samples"));
      return figure;
    }
    const ns = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(ns, "svg");
    svg.setAttribute("viewBox", "0 0 600 100");
    svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", label);
    let max = 1;
    for (const point of valid) max = Math.max(max, point[1]);
    const first = valid[0][0];
    const duration = Math.max(1, valid[valid.length - 1][0] - first);
    let points = "";
    for (const [time, value] of valid) points += ((time - first) / duration * 600).toFixed(2) + "," + (94 - value / max * 88).toFixed(2) + " ";
    const line = document.createElementNS(ns, "polyline");
    line.setAttribute("points", points);
    line.setAttribute("fill", "none");
    line.setAttribute("stroke", "currentColor");
    line.setAttribute("stroke-width", "1.5");
    line.setAttribute("vector-effect", "non-scaling-stroke");
    svg.append(line);
    if (valid.length === 1) {
      const dot = document.createElementNS(ns, "circle");
      dot.setAttribute("cx", "0");
      dot.setAttribute("cy", String(94 - valid[0][1] / max * 88));
      dot.setAttribute("r", "3");
      dot.setAttribute("fill", "currentColor");
      svg.append(dot);
    }
    figure.append(svg);
    const axis = element("div", "spark-axis");
    const stamp = time => new Date(time).toLocaleString(undefined, {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"});
    axis.append(element("span", "", stamp(first)), element("span", "", "Peak " + sparkValue(max, unit)), element("span", "", stamp(valid[valid.length - 1][0])));
    figure.append(axis);
    return figure;
  }

  function renderSparkHistory(report) {
    const box = $("sparks-history");
    const tokens = report.tokens || {};
    const range = $("sparks-range").value;
    const hours = {"1h":1, "6h":6, "24h":24, "7d":168, "30d":720}[range];
    const source = hours <= 24 ? tokens.hour : tokens.day;
    const selected = [];
    const now = tokens.generatedAt || report.sampled_at * 1000;
    for (const row of source || []) {
      const time = Date.parse(row.t.length === 13 ? row.t + ":00:00Z" : row.t + "T00:00:00Z");
      // Include the bucket containing the beginning of the selected interval.
      const width = hours <= 24 ? 3600000 : 86400000;
      if (time + width > now - hours * 3600000) selected.push({...row, time});
    }
    let prompt = 0, output = 0, cached = 0;
    const buckets = new Map();
    for (const row of selected) {
      prompt += row.promptTokens || 0;
      output += row.completionTokens || 0;
      cached += row.cachedTokens || 0;
      buckets.set(row.time, (buckets.get(row.time) || 0) + (row.completionTokens || 0));
    }
    const heading = element("header", "spark-history-heading");
    heading.append(element("h3", "", "Recorded token traffic"), element("span", "", tokens.firstDay ? "Source records since " + tokens.firstDay : ""));
    const totals = sparkMetrics([["Prompt tokens",prompt,""],["Cached prompt tokens",cached,""],["Generated tokens",output,""],["Prompt cache share",prompt ? cached / prompt * 100 : null,"%"]]);
    const times = [...buckets.keys()].sort((a,b) => a-b);
    const values = [];
    for (const time of times) values.push(buckets.get(time));
    const chart = sparkChart(times, values, hours <= 24 ? "Generated tokens per hour" : "Generated tokens per day", " tokens");
    const disclosure = element("details", "spark-records");
    disclosure.open = box.querySelector("details")?.open === true;
    disclosure.append(element("summary", "", "Recorded models & token counts · " + selected.length + " buckets"));
    const scroll = element("div", "spark-table-scroll");
    const table = element("table", "spark-table");
    const head = element("tr", "");
    for (const title of ["Bucket (UTC)","Model / endpoint","Prompt","Cached","Generated"]) head.append(element("th", "", title));
    const thead = element("thead", ""); thead.append(head); table.append(thead);
    const body = element("tbody", "");
    for (const row of selected) {
      const tr = element("tr", "");
      for (const text of [row.t, row.modelId + " · " + row.sparkId + ":" + row.port, sparkValue(row.promptTokens), sparkValue(row.cachedTokens), sparkValue(row.completionTokens)]) tr.append(element("td", "", text));
      body.append(tr);
    }
    table.append(body); scroll.append(table); disclosure.append(scroll);
    if (tokens.error) updateSparkChildren(box, [element("p", "form-error", tokens.error)]);
    else if (!selected.length) updateSparkChildren(box, [heading, element("p", "", "No token records in this interval.")]);
    else updateSparkChildren(box, [heading, totals, chart, disclosure]);
  }

  let sparkRequest = 0;

  async function refreshSparks() {
    const request = ++sparkRequest;
    const status = $("sparks-status");
    try {
      const report = await api("/api/infrastructure/sparks?range=" + $("sparks-range").value);
      if (request !== sparkRequest) return;
      if (!report.configured) { status.textContent = "SparkDash source is not configured."; return; }
      if (report.error) { status.textContent = report.error; return; }
      const box = $("sparks-telemetry");
      const nodes = [];
      for (const spark of report.sparks) {
        const card = element("article", "spark-unit");
        card.dataset.id = spark.id;
        const header = element("header", "spark-unit-heading");
        header.append(element("h3", "", spark.name), element("span", "status-badge", spark.error ? "Unavailable" : spark.online ? "Online · " + spark.role : "Offline"));
        card.append(header);
        if (spark.error) { card.append(element("p", "form-error", spark.error)); nodes.push(card); continue; }
        const metrics = spark.metrics || {};
        const gpu = metrics.gpu || {};
        const vram = gpu.vram || {};
        const ram = metrics.ram || {};
        // Bounded readings get a meter. GPU utilization and CPU utilization are
        // percentages by definition; VRAM and system memory are bounded by the
        // totals the payload reports, so the ratio is measured, not assumed.
        const vramRatio = Number.isFinite(vram.used) && Number.isFinite(vram.total) && vram.total > 0 ? vram.used / vram.total : null;
        const ramRatio = Number.isFinite(ram.used) && Number.isFinite(ram.total) && ram.total > 0 ? ram.used / ram.total : null;
        const headroomRatio = Number.isFinite(vram.available) && Number.isFinite(vram.total) && vram.total > 0 ? vram.available / vram.total : null;
        card.append(sparkMetrics([
          ["GPU utilization", gpu.usage, "%", Number.isFinite(gpu.usage) ? gpu.usage / 100 : null, sparkSeries(spark, "gpuUtil", "u")],
          ["GPU temperature", gpu.temperature, "°C", null, sparkSeries(spark, "gpuTemp", "c")],
          ["GPU power", gpu.power?.draw, " W", Number.isFinite(gpu.power?.draw) && Number.isFinite(gpu.power?.limit) && gpu.power.limit > 0 ? gpu.power.draw / gpu.power.limit : null, sparkSeries(spark, "gpuPower", "p")],
          // Headroom is the one reading where a small meter is the bad case, so
          // it carries its own inverted tone. Do not pass a neutral tone here:
          // that would paint a nearly-full card purple instead of red.
          ["Memory headroom", vram.available == null ? null : vram.available / 1024, " GiB", headroomRatio, sparkSeries(spark, "memFreeMB", null), headroomRatio == null ? "" : headroomTone(headroomRatio)],
          ["CPU utilization", metrics.cpu?.usage, "%", Number.isFinite(metrics.cpu?.usage) ? metrics.cpu.usage / 100 : null, sparkSeries(spark, "cpuUtil", null)],
          ["System memory", ram.percentage, "%", ramRatio],
        ]));
        for (const engine of metrics.llm || []) {
          const section = element("section", "spark-engine");
          section.append(element("h4", "", engine.modelId || "Port " + engine.port), element("p", "spark-engine-meta", [engine.backend, ":" + engine.port, engine.available ? "Reporting" : engine.error || "Unavailable"].filter(Boolean).join(" · ")));
          if (engine.available) {
            // KV usage and slot occupancy are the two engine readings with a
            // ceiling the payload names, so they are the two that get meters.
            const slotsRatio = Number.isFinite(engine.slotsActive) && Number.isFinite(engine.slotsTotal) && engine.slotsTotal > 0 ? engine.slotsActive / engine.slotsTotal : null;
            const kvRatio = Number.isFinite(engine.kvCacheUsage) ? engine.kvCacheUsage : null;
            const engineSeries = (key, multiplier) => {
              const points = spark.history?.llm || [];
              const match = points.find((entry) => entry.port === engine.port);
              if (!match) return null;
              const values = [];
              for (const point of match.points || []) {
                const cell = point[key];
                values.push(cell && Number.isFinite(cell.avg) ? cell.avg * (multiplier || 1) : null);
              }
              return values.some((value) => typeof value === "number") ? values : null;
            };
            section.append(sparkMetrics([
              ["Generation", engine.generationTps, " tok/s", null, engineSeries("genTps", 1)],
              ["Prefill", engine.prefillTps, " tok/s", null, engineSeries("prefillTps", 1)],
              ["Active / total slots", engine.slotsActive, " / " + sparkValue(engine.slotsTotal), slotsRatio],
              ["Queued requests", engine.requestsWaiting, ""],
              ["TTFT p95", engine.ttftP95Seconds, " s"],
              ["KV usage", engine.kvCacheUsage == null ? null : engine.kvCacheUsage * 100, "%", kvRatio, engineSeries("kvUsage", 100)],
              ["KV capacity", engine.kvCacheTokens, " tokens"],
              // MTP acceptance is a quality rate, not a fill level. A meter would
              // say "this fraction of a ceiling" about a number that has none,
              // and I have no measured threshold to call a value bad.
              ["MTP acceptance", engine.mtpAcceptanceRate == null ? null : engine.mtpAcceptanceRate * 100, "%"],
            ]));
          }
          card.append(section);
        }
        const history = spark.gpu_history || {};
        const rangeLabel = $("sparks-range").selectedOptions[0].textContent;
        // The two readings anyone watches are promoted out of the fold, because
        // a HUD that hides its charts behind a summary line is not a HUD.
        const featured = element("div", "spark-featured");
        if (spark.history?.enabled === true) {
          const points = spark.history.points || [];
          const times = [];
          for (const point of points) times.push(point.t);
          for (const [key,label,unit] of [["gpuUtil","GPU utilization","%"],["gpuTemp","GPU temperature","°C"]]) {
            const values = [];
            for (const point of points) values.push(point[key]?.avg ?? null);
            featured.append(sparkChart(times, values, label + " · " + rangeLabel + " averages", unit));
          }
        } else {
          for (const [key,label,unit] of [["u","GPU utilization · last hour","%"],["c","GPU temperature · last hour","°C"]]) featured.append(sparkChart(history.t || [], history[key] || [], label, unit));
        }
        card.append(featured);
        const details = element("details", "spark-more");
        const previous = box.querySelector('[data-id="' + CSS.escape(spark.id) + '"] details');
        details.open = previous?.open === true;
        details.append(element("summary", "", "More history, network & source metrics"));
        if (spark.history?.enabled === true) {
          const points = spark.history.points || [];
          const times = [];
          for (const point of points) times.push(point.t);
          // The promoted two are excluded here so the same chart is not drawn twice.
          for (const [key,label,unit] of [["gpuPower","GPU power"," W"],["memFreeMB","Memory headroom"," MiB"],["cpuUtil","CPU utilization","%"],["netRx","Network receive"," B/s"],["netTx","Network transmit"," B/s"]]) {
            const values = [];
            for (const point of points) values.push(point[key]?.avg ?? null);
            details.append(sparkChart(times, values, label + " · " + rangeLabel + " averages", unit));
          }
        }
        for (const engine of spark.history?.llm || []) {
          for (const [key,label,unit,multiplier] of [["genTps","Generation"," tok/s",1],["prefillTps","Prefill"," tok/s",1],["kvUsage","KV usage","%",100]]) {
            const times = [], values = [];
            for (const point of engine.points || []) {
              times.push(point.t);
              values.push(point[key]?.avg == null ? null : point[key].avg * multiplier);
            }
            details.append(sparkChart(times, values, label + " · port " + engine.port + " · recorded averages", unit));
          }
        }
        if (spark.history?.enabled === false) details.append(element("p", "spark-history-note", "Long-term hardware history is disabled in SparkDash. The charts above use its live one-hour buffer."));
        for (const link of metrics.roce?.devices || []) {
          if (!link.active && !link.everActive) continue;
          details.append(element("h4", "", "RoCE · " + link.name + " · " + link.state));
          details.append(sparkMetrics([["Link rate",link.rateGbps," Gb/s"],["Receive",link.rxBps," B/s"],["Transmit",link.txBps," B/s"],["RX dropped (counter)",link.rxDropped,""],["TX dropped (counter)",link.txDropped,""],["Retransmissions (sample delta)",link.deltas?.roce_adp_retrans,""]]));
        }
        const raw = element("details", "spark-raw"); raw.append(element("summary", "", "All reported source metrics"), element("pre", "", JSON.stringify(metrics,null,2))); details.append(raw); card.append(details); nodes.push(card);
      }
      updateSparkChildren(box, nodes);
      renderSparkHistory(report);
      status.textContent = "SparkDash · read " + new Date(report.sampled_at * 1000).toLocaleTimeString() + " · refreshes every 30 seconds";
      const source = $("sparks-source");
      if (/^https?:\/\//.test(report.source_url || "")) { source.href = report.source_url; source.hidden = false; }
    } catch (error) { if (request === sparkRequest) status.textContent = "SparkDash unavailable · " + error.message; }
  }

  async function refreshTargets() {
    refreshSparks();
    refreshEnginePressure();
    const list = $("targets-list");
    try {
      const reply = await api("/api/infrastructure/targets");
      state.targetsReport = reply && typeof reply === "object" ? reply : null;
      list.replaceChildren();
      if (!reply || typeof reply !== "object" || reply.configured === false) {
        const row = element("li", "compact-row");
        row.append(statusMark(""), element("h3", "", "Targets"), settingsButton("Configure"));
        list.append(row);
        setIntegrationBadge("targets-state", "Not configured");
      } else if (reply.error) {
        list.append(element("li", "quiet-note", "Unavailable"));
        setIntegrationBadge("targets-state", "Unavailable", "attention");
      } else {
        const targets = Array.isArray(reply.targets) ? reply.targets : [];
        for (const target of targets) {
          const item = element("li", "integ-row target-row");
          const up = target.health === "up";
          const dot = element("span", "status-dot", "");
          dot.dataset.tone = up ? "ready" : "danger";
          dot.setAttribute("aria-hidden", "true");
          item.append(dot, element("strong", "", target.job || "?"), element("span", "data", target.instance || ""));
          const badge = element("span", "status-badge", up ? "Up" : target.health === "down" ? "Down" : "Unknown");
          badge.dataset.tone = up ? "ready" : "danger";
          item.append(badge);
          if (!up && typeof target.error === "string" && target.error) item.append(element("span", "data form-error", target.error.slice(0, 160)));
          list.append(item);
        }
        if (!targets.length) list.append(element("li", "quiet-note", "No targets"));
        const up = Number.isInteger(reply.up) ? reply.up : targets.filter((target) => target.health === "up").length;
        const total = Number.isInteger(reply.total) ? reply.total : targets.length;
        setIntegrationBadge("targets-state", targets.length ? up + " / " + total + " up" : "Empty", up === total ? "ready" : "attention");
      }
    } catch (error) {
      state.targetsReport = null;
      list.replaceChildren(element("li", "quiet-note", error.message || "Unavailable"));
      setIntegrationBadge("targets-state", "Unavailable", "attention");
    }
    const report = state.status.infrastructure_status;
    const hosts = report && typeof report === "object" && !Array.isArray(report) ? providerRecords(report.hosts) : null;
    paintInfrastructure(hosts);
  }

  $("sparks-range").addEventListener("change", refreshSparks);
  setInterval(() => { if (state.page === "infrastructure") refreshSparks(); }, 30000);

  const voicePrefs = {
    get(name, fallback) { try { return localStorage.getItem("tessera.voice." + name) ?? fallback; } catch (_) { return fallback; } },
    set(name, value) { try { localStorage.setItem("tessera.voice." + name, value); } catch (_) { /* private mode */ } },
  };

  // Voice overrides are explicit and tab-local. Old saved picker choices must
  // never silently replace Settings on page load or after a Settings change.
  const voiceChoice = {settings: undefined, override: null};

  function chooseVoiceOverride() {
    const chosen = $("voice-pick").value;
    voiceChoice.override = chosen === voiceChoice.settings ? null : chosen;
    renderVoicePicker();
  }

  function voiceStartOptions() {
    renderVoicePicker();
    return voiceChoice.override ? {voice: voiceChoice.override} : {};
  }

  function renderVoicePicker() {
    const select = $("voice-pick");
    const report = state.status.voice || {};
    const voices = Array.isArray(report.voices) ? report.voices.filter((name) => typeof name === "string") : [];
    const fallback = typeof report.voice === "string" ? report.voice : "";
    const names = [...voices];
    if (fallback && !names.includes(fallback)) names.push(fallback);
    if (voiceChoice.settings !== fallback) {
      voiceChoice.settings = fallback;
      voiceChoice.override = null;
    }
    if (!names.includes(voiceChoice.override)) voiceChoice.override = null;
    const current = voiceChoice.override || fallback;
    const pickerKey = JSON.stringify([names, fallback, voiceChoice.override]);
    if (select.dataset.names !== pickerKey) {
      select.replaceChildren();
      for (const name of names) {
        let label = name;
        if (name === fallback) label += " (Settings)";
        else if (name === voiceChoice.override) label += " (override for this tab)";
        const option = element("option", "", label);
        option.value = name;
        select.append(option);
      }
      select.dataset.names = pickerKey;
    }
    if (names.includes(current)) select.value = current;
    select.disabled = Boolean(desktop) || voice?.running() === true || names.length < 2;
    select.title = desktop ? "Uses Settings on the next voice start" : "Choose an override for this tab; selecting Settings restores the configured voice";
  }

  async function refreshMics() {
    const select = $("voice-mic");
    if (typeof navigator === "undefined" || !navigator.mediaDevices?.enumerateDevices) return;
    let devices = [];
    try { devices = (await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === "audioinput"); }
    catch (_) { return; }
    const chosen = voicePrefs.get("mic", "");
    select.replaceChildren(Object.assign(element("option", "", "Default"), {value: ""}));
    let index = 0;
    for (const device of devices) {
      if (!device.deviceId || device.deviceId === "default") continue;
      index += 1;
      const option = element("option", "", device.label || "Microphone " + index);
      option.value = device.deviceId;
      select.append(option);
    }
    select.value = [...select.options].some((option) => option.value === chosen) ? chosen : "";
    if (voice) voice.deviceId = select.value;
  }

  function drawVoice() {
    const wave = $("voice-wave");
    const count = 48;
    if (wave.childElementCount !== count) wave.replaceChildren(...Array.from({length: count}, () => element("span", "")));
    const active = localVoice?.running() ? localVoice : voice;
    const points = active?.waveform(count) || [];
    const phase = active?.phase();
    if (phase && !["off", "connecting"].includes(phase)) {
      document.querySelector(".voice-console").dataset.phase = phase;
      const labels = {speaking:"Speaking", responding:"Responding", thinking:"Thinking", muted:"Microphone muted", waiting:"Hold to talk", listening:"Listening"};
      $("voice-page-state").textContent = labels[phase];
    }
    $("voice-level").value = active ? active.level() : 0;
    [...wave.children].forEach((bar, index) => {
      const value = Math.min(1, Math.abs(points[index] || 0) * 4);
      bar.style.height = Math.max(4, Math.round(value * 100)) + "%";
    });
    if (localVoice?.running() || voice?.running()) requestAnimationFrame(drawVoice);
  }

  function applyVoiceMode() {
    const local = state.status?.voice?.pipe === "local";
    const push = $("voice-mode").value === "push";
    if (voice) voice.setHeld(!push);
    $("voice-ptt").hidden = !(local || (push && voice?.running()));
    $("voice-ptt").setAttribute("aria-pressed", "false");
  }

  function appendTranscript(role, text, final) {
    const list = $("voice-transcript");
    const follow = list.scrollHeight - list.clientHeight - list.scrollTop < 70;
    let last = list.lastElementChild;
    if (!last || last.dataset.role !== role || last.dataset.final === "true") {
      last = element("li", "voice-line");
      last.dataset.role = role;
      last.append(element("span", "data", role === "user" ? "You" : (state.status?.agent_name || "Tessera")), element("span", "voice-text", ""));
      list.append(last);
    }
    const body = last.querySelector(".voice-text");
    body.textContent = final && text ? text : body.textContent + text;
    last.dataset.final = final ? "true" : "false";
    while (list.children.length > 200) list.firstElementChild.remove();
    if (follow) list.scrollTop = list.scrollHeight;
  }

  function openSessionDialog(mode, slug = state.slug) {
    state.dialogMode = mode;
    state.dialogSlug = slug;
    $("dialog-title").textContent = mode === "create" ? "New session" : "Rename session";
    const current = state.sessions.find((session) => session.slug === slug) || {};
    $("session-name").value = mode === "create" ? "" : current.title || "";
    $("session-workspace").value = mode === "create" ? "" : current.workspace || "";
    $("backend-field").hidden = mode !== "create";
    $("save-session").textContent = mode === "create" ? "Create session" : "Save title";
    $("session-form-error").textContent = "";
    $("session-dialog").showModal();
    $("session-name").focus();
  }

  function closeDrawers() {
    const previous = state.drawer;
    state.drawer = null;
    document.body.classList.remove("show-sessions", "show-inspector");
    $("drawer-backdrop").hidden = true;
    $("sessions-toggle").setAttribute("aria-expanded", "false");
    $("inspector-toggle").setAttribute("aria-expanded", "false");
    document.querySelector(".conversation").inert = false;
    document.querySelector(".topbar").inert = false;
    $("sessions-panel").inert = false;
    $("inspector").inert = false;
    $("app-navigation").inert = false;
    if (previous) $(previous === "sessions" ? "sessions-toggle" : "inspector-toggle").focus();
  }

  function toggleDrawer(name) {
    const open = !document.body.classList.contains("show-" + name);
    closeDrawers();
    if (open) {
      state.drawer = name;
      document.body.classList.add("show-" + name);
      $("drawer-backdrop").hidden = false;
      $(name === "sessions" ? "sessions-toggle" : "inspector-toggle").setAttribute("aria-expanded", "true");
      document.querySelector(".conversation").inert = true;
      document.querySelector(".topbar").inert = true;
      $("app-navigation").inert = true;
      $(name === "sessions" ? "inspector" : "sessions-panel").inert = true;
      // Focus the close button, not the search field: on phones a focused input opens the keyboard.
      $(name === "sessions" ? "close-sessions" : "view-tab").focus();
    }
  }

  function setTab(name, focus = false) {
    for (const tabName of ["view", "memory", "status"]) {
      const selected = tabName === name;
      const button = $(tabName + "-tab");
      button.setAttribute("aria-selected", String(selected));
      button.tabIndex = selected ? 0 : -1;
      $(tabName + "-panel").hidden = !selected;
      if (selected && focus) button.focus();
    }
  }

  bindTranscriptFollow();
  $("jump-latest").addEventListener("click", scrollToLatest);

  function renderAttachmentTray() {
    const tray = $("attachment-tray");
    tray.replaceChildren();
    tray.hidden = state.attachments.length === 0;
    state.attachments.forEach((item, index) => {
      const chip = element("div", "attachment-chip");
      if (item.preview) {
        const image = element("img");
        image.src = item.preview;
        image.alt = "";
        chip.append(image);
      }
      chip.append(element("span", "", item.file.name));
      const remove = element("button", "", "Remove");
      remove.type = "button";
      remove.addEventListener("click", () => {
        if (item.preview) URL.revokeObjectURL(item.preview);
        state.attachments.splice(index, 1);
        renderAttachmentTray();
      });
      chip.append(remove);
      tray.append(chip);
    });
    updateHeading();
  }

  function addAttachmentFiles(files) {
    const accepted = [];
    for (const file of files || []) {
      if (!file || (!file.path && !file.size)) continue;
      if (!file.path && file.size > 8 * 1024 * 1024) {
        $("send-state").textContent = file.name + " is over 8 MB.";
        continue;
      }
      if (state.attachments.length + accepted.length >= 6) {
        $("send-state").textContent = "Six files per message.";
        break;
      }
      accepted.push({ file, preview: file.type.startsWith("image/") ? URL.createObjectURL(file) : "" });
    }
    if (!accepted.length) return;
    state.attachments.push(...accepted);
    renderAttachmentTray();
  }

  async function uploadAttachment(slug, file) {
    if (file.path) {
      return api(sessionPath(slug, "/attachment-references"), "POST", { path: file.path });
    }
    const response = await fetch(sessionPath(slug, "/attachments"), {
      method: "POST",
      headers: { ...headers(), "Content-Type": file.type || "application/octet-stream", "X-File-Name": encodeURIComponent(file.name || "file") },
      body: file,
      cache: "no-store",
      redirect: "error",
    });
    await checkResponse(response);
    return response.json();
  }

  window.addEventListener("cairn-local-file-drop", event => {
    const detail = event.detail;
    const target = document.elementFromPoint(detail.x, detail.y);
    const files = [];
    for (const path of detail.paths) {
      files.push({ path, name: path.split(/[\\/]/).pop(), type: "" });
    }
    if (target?.closest("#board-note-form")) {
      window.dispatchEvent(new CustomEvent("cairn-local-evidence", { detail: files }));
    } else if (target?.closest("#project-canvas-root")) {
      window.dispatchEvent(new CustomEvent("cairn-local-artifacts", { detail: files }));
    } else if (target?.closest("#composer")) {
      addAttachmentFiles(files);
    }
  });

  $("attach-file").addEventListener("click", () => $("attachment-input").click());
  $("attachment-input").addEventListener("change", () => {
    addAttachmentFiles($("attachment-input").files);
    $("attachment-input").value = "";
  });
  $("composer").addEventListener("dragover", (event) => { event.preventDefault(); });
  $("composer").addEventListener("drop", (event) => {
    event.preventDefault();
    addAttachmentFiles(event.dataTransfer?.files);
  });
  $("message-input").addEventListener("paste", (event) => {
    const files = [];
    for (const item of event.clipboardData?.items || []) {
      if (item.kind === "file") {
        const file = item.getAsFile();
        if (file) files.push(file);
      }
    }
    if (files.length) {
      event.preventDefault();
      addAttachmentFiles(files);
    }
  });

  async function deliverComposer(hold) {
    const text = $("message-input").value.trim() ? $("message-input").value : (state.attachments.length === 1 ? "See the attached file." : "See the attached files.");
    if ((!text.trim() && !state.attachments.length) || state.sending || activeSession()?.archived) return;
    state.stickToBottom = true;
    state.sending = true;
    updateHeading();
    try {
      if (!activeSession()) {
        const title = text.trim().split(/\s+/).slice(0, 6).join(" ").slice(0, 60);
        const created = await api("/api/sessions", "POST", { title });
        await refreshSessions();
        await selectSession(created.slug);
        $("message-input").value = text;
      }
    } catch (error) { state.sending = false; updateHeading(); showError(error); return; }
    const slug = state.slug;
    const epoch = state.epoch;
    try {
      const replyTo = state.replyTo;
      const pending = state.attachments.splice(0);
      renderAttachmentTray();
      const refs = [];
      for (const item of pending) {
        refs.push(await uploadAttachment(slug, item.file));
        if (item.preview) URL.revokeObjectURL(item.preview);
      }
      const steering = !hold && state.busy && replyTo === null && !refs.length;
      const payload = { text };
      if (state.page === "wip" && state.openProject) { payload.project_id = state.openProject; payload.selected = arena?.canvas.selectedIds() || []; }
      if (replyTo !== null) payload.reply_to = replyTo;
      if (refs.length) payload.attachments = refs;
      const result = steering ? await api(sessionPath(slug, "/steer"), "POST", payload)
        : await api(sessionPath(slug, "/messages"), "POST", payload);
      if (state.replyTo === replyTo) clearReply();
      if (state.draft.get(slug) === text) state.draft.delete(slug);
      if (epoch !== state.epoch) return;
      if ($("message-input").value === text) $("message-input").value = "";
      $("send-state").textContent = steering && result.mode === "steer"
        ? "Steering the running turn."
        : "Queued. It stays above the composer until the running turn finishes. Edit or remove it there.";
      clearError();
      loadMessages(slug, epoch).catch(showError);
      if (!steering) await loadQueue(slug, epoch);
    } catch (error) {
      if (hold && $("message-input").value.trim() === "" && text.trim()) $("message-input").value = text;
      $("send-state").textContent = error.message || "The message was not sent.";
    }
    finally { state.sending = false; updateHeading(); }
  }

  function renderQueue(items) {
    const list = $("queue-list");
    const drafts = new Map();
    for (const field of list.querySelectorAll("textarea[data-turn]")) drafts.set(field.dataset.turn, field.value);
    list.replaceChildren();
    if (!items.length) { list.hidden = true; return; }
    list.hidden = false;
    for (const item of items) {
      const row = element("div", "queue-item");
      row.append(element("span", "queue-label", "Waiting"));
      // A waiting prompt reads as a line, not as an open box. A box with a Save button
      // beside it reads as unsaved, so the person cannot tell whether it is queued yet.
      const text = element("span", "queue-text", item.text || "");
      text.dataset.turn = item.id;
      const actions = element("div", "queue-actions");
      const edit = element("button", "", "Edit");
      edit.type = "button";
      edit.addEventListener("click", () => {
        const field = document.createElement("textarea");
        field.rows = 2;
        // Same reason as the card note field: an id is what lets a rebuild reload put the
        // words back. The turn id is stable across a reload, so the id can be too.
        field.id = "queue-edit-" + item.id;
        field.dataset.turn = item.id;
        field.value = drafts.has(item.id) ? drafts.get(item.id) : (item.text || "");
        field.setAttribute("aria-label", "Waiting prompt");
        const save = element("button", "", "Save");
        save.type = "button";
        save.addEventListener("click", () => reviseQueued(item.id, field.value).catch(showError));
        const cancel = element("button", "", "Cancel");
        cancel.type = "button";
        cancel.addEventListener("click", () => renderQueue(items));
        const editing = element("div", "queue-actions");
        editing.append(save, cancel);
        row.replaceChildren(element("span", "queue-label", "Editing"), field, editing);
        field.focus();
      });
      // Send now is the person changing their mind: drop it from the queue and steer the
      // running turn with it instead of waiting for that turn to end.
      const now = element("button", "", "Send now");
      now.type = "button";
      now.addEventListener("click", () => sendQueuedNow(item.id).catch(showError));
      const remove = element("button", "", "Delete");
      remove.type = "button";
      remove.addEventListener("click", () => removeQueued(item.id).catch(showError));
      actions.append(edit, now, remove);
      row.append(text, actions);
      list.append(row);
    }
  }

  async function sendQueuedNow(turnId) {
    const slug = state.slug;
    const epoch = state.epoch;
    if (!slug || !turnId) return;
    const item = (state.queue || []).find(entry => entry.id === turnId);
    const text = item && item.text;
    if (!text) return;
    await api(sessionPath(slug, "/queue"), "DELETE", { turn_id: turnId });
    if (epoch !== state.epoch) return;
    await api(sessionPath(slug, "/steer"), "POST", { text });
    if (epoch !== state.epoch) return;
    $("send-state").textContent = "Sent now. It steers the running turn.";
    await loadQueue(slug, epoch);
  }

  async function loadQueue(slug, epoch) {
    if (!slug) return;
    const result = await api(sessionPath(slug, "/queue"));
    if (epoch !== state.epoch || slug !== state.slug) return;
    state.queue = Array.isArray(result.queued) ? result.queued : [];
    renderQueue(state.queue);
  }

  async function reviseQueued(turnId, text) {
    const slug = state.slug;
    const epoch = state.epoch;
    const next = String(text || "").trim();
    if (!slug || !turnId || !next) return;
    await api(sessionPath(slug, "/queue"), "PATCH", { turn_id: turnId, text: next });
    if (epoch !== state.epoch) return;
    $("send-state").textContent = "Waiting prompt updated.";
    await loadQueue(slug, epoch);
  }

  async function removeQueued(turnId) {
    const slug = state.slug;
    const epoch = state.epoch;
    if (!slug || !turnId) return;
    await api(sessionPath(slug, "/queue"), "DELETE", { turn_id: turnId });
    if (epoch !== state.epoch) return;
    $("send-state").textContent = "Removed from the queue.";
    await loadQueue(slug, epoch);
  }

  $("composer").addEventListener("submit", (event) => {
    event.preventDefault();
    if (state.busy && !$("message-input").value.trim() && !state.attachments.length) {
      $("message-input").focus({ preventScroll: true });
      $("send-state").textContent = "Type guidance, then press Steer.";
      return;
    }
    deliverComposer(false).catch(showError);
  });

  // The operator's own words become a card. This is the lane that was missing: a note
  // that is not worth interrupting a session for, kept on the board for idle time.
  if (window.TesseraWorkIntake) workIntake = new window.TesseraWorkIntake.WorkIntake({
    upload: uploadAttachment,
    loadGroups: async () => {
      const view = await api("/api/board/view");
      if (Array.isArray(view.projects)) renderBoardNoteProjects(view.projects);
    },
    session: () => {
      const selected = state.sessions.find(item => item.slug === state.slug && !item.archived);
      return selected?.slug || state.sessions.find(item => !item.archived)?.slug || "";
    },
    context: () => ({page: state.page, session: activeSession()?.title || "", project: state.page === "wip" ? state.openProject : ""})
  });
  $("board-note-form").addEventListener("submit", (event) => {
    event.preventDefault();
    submitBoardNote().catch((error) => {
      const status = $("board-note-status");
      if (status) status.textContent = "Could not add it: " + (error && error.message ? error.message : "unknown");
      $("board-note-submit").disabled = false;
    });
  });
  // After a successful add, drop the saved words so a later reload cannot
  // resurrect a note the board already took.
  $("board-note-form").addEventListener("submit", () => {
    setTimeout(() => {
      for (const field of document.querySelectorAll("#board-note-form input, #board-note-form textarea")) {
        if (field.id) forgetWipDraft(field.id);
      }
    }, 300);
  });
  $("project-create-form").addEventListener("submit", async event => {
    event.preventDefault();
    const title = $("project-create-title").value.trim();
    if (!title) return;
    const submit = event.currentTarget.querySelector("button");
    const notice = $("project-create-status");
    submit.disabled = true;
    notice.textContent = "Creating project…";
    try {
      const reply = await api("/api/board/projects", "POST", {
        title, description: $("project-create-description").value.trim(), kind: $("project-create-kind").value
      });
      applyBoardView(reply.view);
      $("project-create-title").value = "";
      $("project-create-description").value = "";
      notice.textContent = "Project created";
      openProject(reply.project_id);
    } catch (error) {
      notice.textContent = "Could not create project: " + error.message;
    } finally {
      submit.disabled = false;
    }
  });
  $("queue-next").addEventListener("click", () => {
    deliverComposer(true).catch(showError);
  });
  $("stop-turn").addEventListener("click", stopTurn);
  async function stopTurn() {
    const slug = state.slug;
    if (!slug || !state.busy) return;
    state.stopping = slug;
    activity.phase = "Cancelling now";
    $("send-state").textContent = "Cancelling now...";
    if (state.live) {
      state.live.tool.textContent = "Cancelling now";
      state.live.tool.dataset.phase = "Cancelling now";
      state.live.tool.dataset.updatedAt = new Date().toISOString();
    }
    updateHeading();
    renderActivity();
    try {
      const result = await api(sessionPath(slug, "/interrupt"), "POST", {});
      if (state.slug !== slug) return;
      // The terminal SSE event may beat the HTTP response. Never overwrite
      // its Stopped state with an older cancellation acknowledgement.
      if (state.busy && state.stopping === slug) {
        if (result.interrupted) $("send-state").textContent = "Cancelling now...";
        else {
          state.busy = false;
          state.stopping = null;
          clearLive();
          activity.phase = "Stopped";
          $("send-state").textContent = "Stopped. Send a new message when you want to resume.";
          renderActivity();
        }
      }
      clearError();
    } catch (error) { state.stopping = null; showError(error); }
    finally { updateHeading(); }
  }
  $("message-input").addEventListener("input", updateHeading);
  $("message-input").addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.isComposing) return;
    const modifier = (event.ctrlKey || event.metaKey) && !event.shiftKey && !event.altKey;
    // While a turn is running, ⌘-Enter or Ctrl-Enter queues the draft instead of steering it.
    if (modifier && state.busy) {
      event.preventDefault();
      deliverComposer(true).catch(showError);
      return;
    }
    const sendWithEnter = state.sendShortcut === "enter" && !event.shiftKey && !modifier;
    const sendWithModifier = state.sendShortcut === "modifier" && modifier;
    if (sendWithEnter || sendWithModifier) {
      event.preventDefault();
      $("composer").requestSubmit();
    }
  });
  $("session-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const title = $("session-name").value.trim();
    if (!title) return;
    const slug = state.dialogSlug;
    $("save-session").disabled = true;
    try {
      let session;
      const workspace = $("session-workspace").value.trim();
      if (state.dialogMode === "create") {
        const body = { title };
        if ($("session-backend").value) body.backend = $("session-backend").value;
        if (workspace) body.workspace = workspace;
        session = await api("/api/sessions", "POST", body);
      } else session = await api(sessionPath(slug), "PATCH", { title, workspace });
      $("session-dialog").close();
      await refreshSessions();
      if (state.dialogMode === "create") await selectSession(session.slug);
      clearError();
    } catch (error) { $("session-form-error").textContent = error.message; }
    finally { $("save-session").disabled = false; }
  });
  $("archive-session").addEventListener("click", async () => {
    const session = activeSession();
    if (!session) return;
    try {
      await archiveSessions([session.slug], !session.archived);
    } catch (error) {
      showError(error);
    }
  });
  $("session-model").addEventListener("change", async () => {
    const slug = state.slug;
    if (!slug) return;
    try {
      await api(sessionPath(slug), "PATCH", { backend: $("session-model").value });
      await refreshSessions();
      $("send-state").textContent = "Model switched; applies from the next turn.";
    } catch (error) { showError(error); renderModelPicker(); }
  });
  $("session-reasoning").addEventListener("change", async () => {
    const slug = state.slug;
    if (!slug) return;
    try {
      await api(sessionPath(slug), "PATCH", { reasoning: $("session-reasoning").value });
      await refreshSessions();
      $("send-state").textContent = "Reasoning set to " + $("session-reasoning").value + ".";
    } catch (error) { showError(error); renderModelPicker(); }
  });
  $("thinking-budget-enabled").addEventListener("change", () => {
    $("thinking-budget").disabled = !$("thinking-budget-enabled").checked;
    renderThinkingEnergy();
    return saveThinkingBudget();
  });
  $("thinking-budget").addEventListener("input", renderThinkingEnergy);
  $("thinking-budget").addEventListener("change", saveThinkingBudget);
  $("memory-record-search").addEventListener("input", filterMemoryRecords);
  $("browse-session-memory").addEventListener("click", () => browseMemory("session"));
  $("browse-personal-memory").addEventListener("click", () => browseMemory("personal"));
  $("new-session").addEventListener("click", () => openSessionDialog("create"));
  $("empty-create").addEventListener("click", () => openSessionDialog("create"));
  function closeHeaderMenu() {
    $("session-secondary").classList.remove("open");
    $("session-more").setAttribute("aria-expanded", "false");
  }
  $("session-more").addEventListener("click", (event) => {
    event.stopPropagation();
    const open = !$("session-secondary").classList.contains("open");
    $("session-secondary").classList.toggle("open", open);
    $("session-more").setAttribute("aria-expanded", open ? "true" : "false");
  });
  $("session-secondary").addEventListener("click", (event) => {
    if (event.target.closest("button")) closeHeaderMenu();
  });
  document.addEventListener("click", (event) => {
    if (!$("brand-story").contains(event.target)) $("brand-story").open = false;
    if (event.target === $("session-more") || $("session-secondary").contains(event.target)) return;
    closeHeaderMenu();
  });
  $("rename-session").addEventListener("click", () => openSessionDialog("rename"));
  $("cancel-session").addEventListener("click", () => $("session-dialog").close());
  $("session-search").addEventListener("input", renderSessions);
  $("show-archived").addEventListener("change", renderSessions);
  $("menu-workspace-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const slug = $("session-menu").dataset.slug;
    if (!slug) return;
    const workspace = $("menu-workspace").value.trim();
    try {
      await api(sessionPath(slug), "PATCH", { workspace });
      closeContextMenu();
      await refreshSessions();
    } catch (error) { showError(error); }
  });
  $("menu-rename").addEventListener("click", () => { const slug = $("session-menu").dataset.slug; closeContextMenu(); openSessionDialog("rename", slug); });
  $("menu-archive").addEventListener("click", async () => {
    const slug = $("session-menu").dataset.slug;
    const session = state.sessions.find((entry) => entry.slug === slug);
    closeContextMenu();
    if (!session) return;
    try {
      await archiveSessions([slug], !session.archived);
    } catch (error) {
      showError(error);
    }
  });
  $("menu-delete").addEventListener("click", () => { const slug = $("session-menu").dataset.slug; closeContextMenu(); deleteSessions([slug]).catch(showError); });
  $("menu-select").addEventListener("click", () => { const slug = $("session-menu").dataset.slug; closeContextMenu(); startSelecting(slug); });
  $("select-sessions").addEventListener("click", () => (state.selecting ? endSelecting() : startSelecting()));
  $("select-cancel").addEventListener("click", endSelecting);
  $("select-archive").addEventListener("click", () => archiveSessions([...state.selected], true).catch(showError));
  $("select-delete").addEventListener("click", () => deleteSessions([...state.selected]).catch(showError));
  document.addEventListener("pointerdown", (event) => { if (!$("session-menu").hidden && !$("session-menu").contains(event.target)) closeContextMenu(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") { closeContextMenu(); closeHeaderMenu(); } });
  $("retry-button").addEventListener("click", () => bootstrap().catch(showError));
  window.addEventListener("tessera:status-stale", () => refreshStatus().catch(showError));
  window.addEventListener("tessera:approval-set", (event) => { state.approvalEpoch += 1; renderApproval(event.detail); })
  $("approval-safe").addEventListener("click", () => setYolo(false).catch(showError));
  $("approval-yolo").addEventListener("click", () => setYolo(true).catch(showError));
  $("feedback-filter").addEventListener("change", applyFeedbackFilter);
  $("show-resolved").addEventListener("click", () => {
    const pressed = $("show-resolved").getAttribute("aria-pressed") !== "true";
    $("show-resolved").setAttribute("aria-pressed", String(pressed));
    applyFeedbackFilter();
  });
  $("reply-cancel").addEventListener("click", clearReply);
  $("load-earlier").addEventListener("click", () => loadEarlier().catch(showError));
  $("interface-update").addEventListener("click", () => {
    state.applyBuild = true;
    reloadForNewBuild(state.pendingBuild);
  });
  $("integration-search").addEventListener("input", renderIntegrations);
  $("integration-filter").addEventListener("change", renderIntegrations);
  $("refresh-details").addEventListener("click", () => Promise.all([refreshStatus(), refreshView()]).catch(showError));
  $("reading-session-switch").addEventListener("click", () => {
    if (window.matchMedia("(max-width: 760px)").matches) toggleDrawer("sessions");
    else if (document.body.classList.contains("sessions-collapsed")) $("sessions-expand").click();
    else $("session-search").focus();
  });
  $("sessions-toggle").addEventListener("click", () => toggleDrawer("sessions"));
  $("inspector-toggle").addEventListener("click", () => toggleDrawer("inspector"));
  $("drawer-backdrop").addEventListener("click", () => { closeDrawers(); closeNavigation(); });
  $("close-sessions").addEventListener("click", closeDrawers);
  $("close-inspector").addEventListener("click", closeDrawers);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      if ($("brand-story").open) {
        $("brand-story").open = false;
        $("brand-story-toggle").focus();
      }
      closeDrawers(); closeNavigation(true); closeContextMenu(); closeHeaderMenu();
    }
    if (event.key === "Tab" && state.drawer) {
      const panel = $(state.drawer === "sessions" ? "sessions-panel" : "inspector");
      const candidates = panel.querySelectorAll("button:not([disabled]), input, select, summary, [tabindex='0']");
      const targets = Array.from(candidates).filter((node) => node.getClientRects().length > 0);
      const first = targets[0];
      const last = targets[targets.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
  });
  const tabs = ["view", "memory", "status"];
  for (let index = 0; index < tabs.length; index += 1) {
    const name = tabs[index];
    $(name + "-tab").addEventListener("click", () => setTab(name));
    $(name + "-tab").addEventListener("keydown", (event) => {
      let next;
      if (event.key === "ArrowRight") next = (index + 1) % tabs.length;
      if (event.key === "ArrowLeft") next = (index + tabs.length - 1) % tabs.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = tabs.length - 1;
      if (next !== undefined) { event.preventDefault(); setTab(tabs[next], true); }
    });
  }
  function applyTheme(value) {
    const choice = value === "light" || value === "dark" ? value : "system";
    if (choice === "system") delete document.documentElement.dataset.theme;
    else document.documentElement.dataset.theme = choice;
    if ($("theme-choice").value !== choice) $("theme-choice").value = choice;
    try { localStorage.setItem("tessera-theme", choice); } catch (_) { /* First paint can use the last theme when storage works. */ }
  }

  function applyShortcut(value) {
    state.sendShortcut = value === "modifier" ? "modifier" : "enter";
    $("composer-hint").textContent = state.sendShortcut === "enter"
      ? "Enter to send. Shift+Enter for a new line. While a turn runs, ⌘-Enter queues it."
      : "Ctrl / ⌘ + Enter to send. Enter for a new line. While a turn runs, ⌘-Enter queues it.";
    try { localStorage.setItem("tessera-send-shortcut", state.sendShortcut); } catch (_) { /* The core value is applied again from status. */ }
  }

  function applyThoughts(on) {
    document.body.classList.toggle("hide-thoughts", !on);
    try { localStorage.setItem("tessera-show-thoughts", on ? "on" : "off"); } catch (_) { /* The core value is applied again from status. */ }
  }

  function paintApprovalLabel(ui) {
    if (window.TesseraUnleashed) window.TesseraUnleashed.paint($("approval-yolo"), ui);
  }

  function applyReportedAppearance(ui) {
    if (!ui || typeof ui !== "object") return;
    if (typeof ui.theme === "string") applyTheme(ui.theme);
    if (typeof ui.skin === "string") applySkin(ui.skin);
    if (typeof ui.mark === "string" || typeof ui.easter_egg_f_it === "boolean") {
      applyMark(typeof ui.mark === "string" ? ui.mark : (document.documentElement.dataset.markChoice || "cairn"), ui.easter_egg_f_it === true);
    }
    if (typeof ui.accent_color === "string") applyAccent(ui.accent_color);
    if (typeof ui.show_thoughts === "boolean") applyThoughts(ui.show_thoughts);
    if (typeof ui.send_shortcut === "string") applyShortcut(ui.send_shortcut);
    paintApprovalLabel(ui);
  }

  function rememberChoice(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch (error) {
      // Storage can be blocked (private mode, cleared cookies). The choice still
      // holds for this visit because the core value is applied again from status.
      if (window.console && typeof window.console.warn === "function") {
        window.console.warn("Tessera could not remember " + key + " for the next visit.", error);
      }
    }
  }

  function applyMark(mark, egg) {
    const stored = mark === "gremlin" ? "gremlin" : "cairn";
    const api = window.TesseraUnleashed;
    const eggOn = egg === true;
    const pack = api ? api.packOn({easter_egg_f_it: eggOn}) : eggOn;
    const active = api ? api.activeMark({mark: stored, easter_egg_f_it: eggOn}) : stored;
    document.documentElement.dataset.mark = active;
    document.documentElement.dataset.markChoice = stored;
    document.documentElement.dataset.pack = pack ? "gremlin" : "off";
    const slot = $("design-asset-slot");
    if (slot) slot.setAttribute("aria-label", active === "gremlin" && api && api.LOGO_ALT ? api.LOGO_ALT : "Tessera");
    const choice = api ? api.iconFor(pack ? "gremlin" : "cairn") : null;
    const links = typeof document.querySelectorAll === "function"
      ? document.querySelectorAll('link[rel="icon"], link[rel="apple-touch-icon"]')
      : [];
    for (const link of links) {
      if (!link.dataset.cairnHref) {
        link.dataset.cairnHref = link.getAttribute("href") || "";
        link.dataset.cairnType = link.getAttribute("type") || "";
        link.dataset.cairnSizes = link.getAttribute("sizes") || "";
      }
      if (choice) {
        link.href = choice.href;
        link.type = choice.type;
        link.removeAttribute("sizes");
      } else {
        link.href = link.dataset.cairnHref;
        if (link.dataset.cairnType) link.type = link.dataset.cairnType;
        else link.removeAttribute("type");
        if (link.dataset.cairnSizes) link.setAttribute("sizes", link.dataset.cairnSizes);
        else link.removeAttribute("sizes");
      }
    }
    rememberChoice("tessera-mark", stored);
    paintTrail();
  }

  function paintTrail() {
    const trail = $("brand-trail");
    const api = window.TesseraGremlinTrail;
    if (!trail || !api || typeof api.apply !== "function") return;
    const connection = $("connection-state");
    const session = sessionStatus();
    const jobs = state.status && state.status.job_status && Array.isArray(state.status.job_status.jobs)
      ? state.status.job_status.jobs : [];
    const cards = state.status && state.status.board_status && Array.isArray(state.status.board_status.cards)
      ? state.status.board_status.cards : [];
    api.apply(trail, $("trail-caption"), {
      pack: document.documentElement.dataset.pack === "gremlin",
      connected: Boolean(connection && connection.textContent === "Connected"),
      busy: state.busy === true,
      activeAgents: session.active_agents,
      queued: session.queued,
      sessionError: session.error,
      turnError: activity.phase === api.TURN_ERROR,
      jobs: jobs,
      cards: cards,
    });
  }

  function applySkin(value) {
    const skin = ["strata", "basalt", "tide"].includes(value) ? value : "strata";
    document.documentElement.dataset.skin = skin;
    rememberChoice("tessera-skin", skin);
  }

  function applyAccent(value) {
    const root = document.documentElement;
    const color = /^#[0-9a-f]{6}$/i.test(value) ? value : "";
    if (root.dataset.appliedAccent === color) return;
    root.dataset.appliedAccent = color;
    for (const name of ["--accent", "--accent-hover", "--accent-soft", "--focus", "--on-accent"]) root.style.removeProperty(name);
    if (color) {
      root.style.setProperty("--accent", color);
      root.style.setProperty("--focus", color);
      root.style.setProperty("--accent-hover", "color-mix(in srgb, " + color + " 80%, var(--ink))");
      root.style.setProperty("--accent-soft", "color-mix(in srgb, " + color + " 14%, var(--surface))");
      let luminance = 0;
      for (const [index, weight] of [[1, 0.2126], [3, 0.7152], [5, 0.0722]]) {
        const channel = parseInt(color.slice(index, index + 2), 16) / 255;
        luminance += weight * (channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4);
      }
      root.style.setProperty("--on-accent", luminance > 0.179 ? "var(--accent-ink)" : "var(--accent-paper)");
    }
    rememberChoice("tessera-accent", color);
  }

  try {
    const saved = localStorage.getItem("tessera-theme") || "system";
    if (["light", "dark", "system"].includes(saved)) $("theme-choice").value = saved;
    applySkin(localStorage.getItem("tessera-skin") || "strata");
    applyMark(localStorage.getItem("tessera-mark") || "cairn", false);
    applyAccent(localStorage.getItem("tessera-accent") || "");
  } catch (_) { /* Theme preferences are optional when storage is unavailable. */ }
  applyTheme($("theme-choice").value);
  $("theme-choice").addEventListener("change", () => {
    const value = $("theme-choice").value;
    applyTheme(value);
    api("/api/settings", "POST", {id: "ui.theme", value}).then((result) => {
      if (configView) configView.show(result);
    }).catch(showError);
  });
  try { applyShortcut(localStorage.getItem("tessera-send-shortcut") || "enter"); }
  catch (_) { applyShortcut("enter"); }
  try { applyThoughts(localStorage.getItem("tessera-show-thoughts") !== "off"); }
  catch (_) { applyThoughts(true); }
  // Reading focus is restored only on the chat page, so a saved preference cannot hide
  // the navigation on a page that needs it.
  try {
    if (state.page === "chat") applyReadingFocus(localStorage.getItem("tessera-reading-focus") === "on", {persist: false});
  } catch (_) { /* storage unavailable: the bars stay as they are */ }
  for (const page of pages) {
    $("nav-" + page).addEventListener("click", async (event) => {
      event.preventDefault();
      setPage(page);
      if (page === "wip" && state.boardViewPromise) await state.boardViewPromise;
    });
  }
  $("skip-link").addEventListener("click", (event) => {
    event.preventDefault();
    $("page-" + state.page).focus({ preventScroll: true });
  });
  $("navigation-toggle").addEventListener("click", () => {
    const open = !document.body.classList.contains("show-navigation");
    closeDrawers();
    if (open) {
      document.body.classList.add("show-navigation");
      $("navigation-toggle").setAttribute("aria-expanded", "true");
      $("drawer-backdrop").hidden = false;
    } else closeNavigation();
  });
  $("arena-back").addEventListener("click", closeProjectArena);
  $("arena-full-chat").addEventListener("click", () => setPage("chat"));
  const readingButton = $("reading-focus");
  if (readingButton) readingButton.addEventListener("click", () => {
    applyReadingFocus(!document.body.classList.contains("reading-focus"));
  });
  // The activity panel remembers being closed. renderActivity only reopens it when the
  // reader opens it themselves.
  const activityBox = $("session-activity");
  if (activityBox) activityBox.addEventListener("toggle", () => {
    state.activityCollapsed = activityBox.open !== true;
  });
  document.addEventListener("keydown", (event) => {
    // ⌘/Ctrl Shift R toggles reading focus. It must not fire while a field has focus,
    // or it would eat a keystroke in the middle of typing.
    const tag = String(event.target?.tagName || "").toUpperCase();
    if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
    if ((event.metaKey || event.ctrlKey) && event.shiftKey && String(event.key).toLowerCase() === "r") {
      event.preventDefault();
      applyReadingFocus(!document.body.classList.contains("reading-focus"));
    }
  });
  for (const mode of ["canvas", "plan", "tasks"]) $("arena-" + mode + "-tab").addEventListener("click", () => setArenaMode(mode));
  $("arena-session").addEventListener("change", () => selectSession($("arena-session").value).catch(showError));
  $("refresh-quality").addEventListener("click", () => { state.qualityReport = null; refreshQuality(); });
  $("board-search").addEventListener("input", () => { state.boardSearch = $("board-search").value; renderBoardStatus(); });
  $("board-focus").addEventListener("change", () => { state.boardFocus = $("board-focus").value; state.boardShowDone = false; renderBoardStatus(); });
  // Completed work is one click away, not in the way.
  for (const [id, showDone] of [["board-show-open", false], ["board-show-done", true]]) {
    const button = $(id);
    if (button) button.addEventListener("click", () => {
      state.boardShowDone = showDone;
      state.boardFocus = "all";
      $("board-focus").value = "all";
      renderBoardStatus();
    });
  }

  for (const id of ["settings-refresh", "refresh-tasks", "refresh-infrastructure"]) {
    $(id).addEventListener("click", async () => {
      $(id).disabled = true;
      try {
        await Promise.all([refreshSessions(), refreshStatus(), id === "settings-refresh" && configView ? configView.load() : null]);
        clearError();
        // Pressure is cheap and lives only on the board page; refreshing it off-page
        // costs one small GET and keeps the panel current when the page is opened.
        if (id === "refresh-tasks") {
          if (state.page === "wip") resetBoardView();
          await Promise.all([state.page === "wip" ? refreshBoardView() : null, refreshPressure()]);
        }
      }
      catch (error) { showError(error); }
      finally { $(id).disabled = false; }
    });
  }
  $("refresh-integrations").addEventListener("click", refreshIntegrations);
  $("integrations-check-all").addEventListener("click", async () => {
    $("integrations-check-all").disabled = true;
    try { await testAllIntegrations(); }
    finally { $("integrations-check-all").disabled = false; }
  });
  $("voice-session").addEventListener("change", () => {
    const slug = $("voice-session").value;
    // Choosing a target here says where the Voice page talks; it must not move the
    // reader off the chat they are reading.
    state.voiceTarget = slug || "";
    renderVoiceStatus();
  });
  async function restoreLocation() {
    const route = readLocation();
    state.openProject = route.project || "";
    state.arenaMode = route.pane || "canvas";
    if (state.openProject) state.boardProjectFilter = state.openProject;
    setPage(route.page, false, false);
    if (route.slug && route.slug !== state.slug && state.sessions.some((session) => session.slug === route.slug)) {
      await selectSession(route.slug);
    }
  }
  window.addEventListener("popstate", () => restoreLocation().catch(showError));
  window.addEventListener("hashchange", () => restoreLocation().catch(showError));
  async function bootstrap() {
    restoreDrafts();
    restoreWipDrafts();
    captureWipDrafts();
    await Promise.all([refreshSessions(), refreshStatus()]);
    let desired = state.slug;
    if (!desired) desired = readLocation().slug;
    if (!state.sessions.some((session) => session.slug === desired)) {
      const newest = newestOpen();
      desired = newest ? newest.slug : "";
    }
    if (desired) await selectSession(desired);
    else {
      $("connection-state").textContent = "Connected";
      paintTrail();
    }
    clearError();
  }
  function voiceSetting(key, fallback) {
    const value = Number(document.body.dataset[key]);
    return Number.isSafeInteger(value) && value > 0 ? value : fallback;
  }

  voice = new window.TesseraVoice({
    workletUrl: document.body.dataset.voiceWorklet || "/static/voice-worklet.js",
    chunkFrames: voiceSetting("voiceChunkFrames", 2048),
    maxBufferedBytes: voiceSetting("voiceBufferBytes", 262144),
    maxPendingEvents: voiceSetting("voicePendingEvents", 32),
    connectSeconds: voiceSetting("voiceConnectSeconds", 20),
    maxPlaybackSeconds: voiceSetting("voicePlaybackSeconds", 15),
    // Web-only lease liveness. The desktop/iOS surface passes no heartbeat, so
    // it never opts into reaping and keeps the deadline backstop.
    beatSeconds: voiceSetting("voiceBeatSeconds", 20),
    createSocket: desktop ? settings => new window.TesseraDesktopVoiceSocket(settings.voice_id) : null,
    // The page's static files are served from disk, so they can be newer than the
    // running core. An older core rejects the unknown keepalive field with 400
    // (measured on the live core: pid 37900 answers 400 to {"keepalive":true}).
    // Retry without it so a page reload cannot break voice on an old core; the
    // beat then simply 404s and is swallowed, leaving the deadline as backstop.
    start: async (slug, extra) => {
      if (desktop) return desktopReply(await desktop.voiceStart(slug));
      const body = voiceStartOptions();
      try { return await api(sessionPath(slug, "/voice/start"), "POST", Object.assign({}, body, extra || {})); }
      catch (error) {
        if (error.status !== 400 || !extra || !extra.keepalive) throw error;
        return api(sessionPath(slug, "/voice/start"), "POST", body);
      }
    },
    events: async (slug, voiceId, events) => desktop ? desktopReply(await desktop.voiceEvents(slug, voiceId, events)) : api(sessionPath(slug, "/voice/events"), "POST", {voice_id: voiceId, events}),
    stop: async (slug, voiceId) => desktop ? desktopReply(await desktop.voiceStop(slug, voiceId)) : api(sessionPath(slug, "/voice/stop"), "POST", {voice_id: voiceId}),
    heartbeat: desktop ? null : async (slug, voiceId) => api(sessionPath(slug, "/voice/heartbeat"), "POST", {voice_id: voiceId}),
    onTranscript: (role, text, final) => appendTranscript(role, text, final),
    onState: (label, detail, slug) => {
      // The lease may belong to the dedicated voice session while another chat is
      // open, so compare against the voice target rather than the open chat.
      const target = voiceSession();
      if (slug && slug !== (target ? target.slug : "")) return;
      state.voiceNotice = {label, detail};
      if (label === "Listening") refreshMics();
      renderVoiceStatus();
    },
    onMessages: (slug, messages) => {
      if (!Array.isArray(messages)) return;
      // A turn the spoken side handed to the voice session belongs to that
      // session's transcript, not to whichever chat is on screen.
      if (slug === state.slug) addMessages(messages);
    },
    onBargeIn: (slug) => {
      const target = voiceSession();
      if (!slug || slug !== (target ? target.slug : "")) return;
      api(sessionPath(slug, "/interrupt"), "POST", {}).catch(() => {});
    },
  });
  // The local pipe records one utterance in the page and lets the server do the
  // speech-to-text, the turn, and the text-to-speech. 24000 Hz mono is what the
  // worklet resamples to; the server wraps it as a WAV for the STT service.
  localVoice = new window.TesseraLocalVoice({
    workletUrl: document.body.dataset.voiceWorklet || "/static/voice-worklet.js",
    targetRate: 24000,
    chunkFrames: voiceSetting("voiceChunkFrames", 2048),
    request: (path, method, body) => api(path, method, body),
    onState: (label, detail, slug) => {
      const target = voiceSession();
      if (slug && slug !== (target ? target.slug : "")) return;
      state.voiceNotice = {label, detail};
      renderVoiceStatus();
    },
  });
  for (const id of ["voice-toggle", "voice-page-toggle"]) $(id).addEventListener("click", async () => {
    if (voice.running()) { stopVoice(); return; }
    const report = state.status.voice;
    const session = voiceSession();
    if (report?.enabled !== true || report?.configured !== true || !session || session.archived) return;
    state.voiceNotice = null;
    await voice.start(session.slug);
  });
  $("voice-pick").addEventListener("change", chooseVoiceOverride);
  $("voice-mic").addEventListener("change", () => {
    voicePrefs.set("mic", $("voice-mic").value);
    voice.deviceId = $("voice-mic").value;
    if (localVoice) localVoice.deviceId = $("voice-mic").value;
  });
  $("voice-mode").value = voicePrefs.get("mode", "hands-free") === "push" ? "push" : "hands-free";
  $("voice-mode").addEventListener("change", () => { voicePrefs.set("mode", $("voice-mode").value); applyVoiceMode(); });
  $("voice-mute").addEventListener("click", () => {
    const muted = $("voice-mute").getAttribute("aria-pressed") !== "true";
    voice.setMuted(muted);
    $("voice-mute").setAttribute("aria-pressed", muted ? "true" : "false");
    $("voice-mute").textContent = muted ? "Unmute" : "Mute";
  });
  const hold = (down) => (event) => {
    event.preventDefault();
    $("voice-ptt").setAttribute("aria-pressed", down ? "true" : "false");
    // The local pipe is always push-to-talk: hold records, release sends.
    if (state.status?.voice?.pipe === "local") {
      const session = voiceSession();
      if (down) {
        if (!session || session.archived) return;
        state.voiceNotice = null;
        localVoice.start(session.slug);
        return;
      }
      localVoice.stop().then((body) => {
        if (!body) return;
        appendTranscript("user", body.transcript, true);
        appendTranscript("assistant", body.reply, true);
      });
      return;
    }
    voice.setHeld(down);
  };
  $("voice-ptt").addEventListener("pointerdown", hold(true));
  for (const name of ["pointerup", "pointercancel", "pointerleave"]) $("voice-ptt").addEventListener(name, hold(false));
  $("voice-new-session").addEventListener("click", () => openSessionDialog("create"));
  if (typeof navigator !== "undefined" && navigator.mediaDevices?.addEventListener) navigator.mediaDevices.addEventListener("devicechange", refreshMics);
  $("refresh-infrastructure").addEventListener("click", refreshTargets);
  // A phone power button fires pagehide and freezes the page. That is not an exit.
  // Stopping voice there cuts a hands-free call the moment the screen sleeps.
  // A real navigation sets persisted false; the back-forward cache sets it true.
  window.addEventListener("pagehide", (event) => {
    if (event.persisted) return;
    stopVoice();
    stopStream();
  });
  window.addEventListener("pageshow", (event) => { if (event.persisted) bootstrap().catch(showError); });
  // Close drawers only when the layout crosses the drawer breakpoint. Phone keyboards and
  // collapsing browser bars fire resize events that must not slam an open drawer shut.
  const topbar = document.querySelector(".topbar");
  const syncTopbar = () => {
    if (!topbar || typeof topbar.getBoundingClientRect !== "function") return;
    const height = topbar.getBoundingClientRect().height;
    if (height > 0) document.documentElement.style.setProperty("--topbar-offset", height + "px");
  };
  syncTopbar();
  if (topbar && typeof ResizeObserver === "function") new ResizeObserver(syncTopbar).observe(topbar);
  const drawerQuery = typeof window.matchMedia === "function"
    ? window.matchMedia("(max-width: " + (Number(document.body.dataset.drawerBelow) || 760) + "px)") : null;
  const crossed = () => { closeDrawers(); closeNavigation(); };
  if (drawerQuery && drawerQuery.addEventListener) drawerQuery.addEventListener("change", crossed);
  else if (drawerQuery && drawerQuery.addListener) drawerQuery.addListener(crossed);
  // Update only the existing status text, preserving transcript geometry and scroll.
  setInterval(() => {
    const label = state.live?.tool;
    if (!state.busy || !label || !label.dataset.updatedAt || document.hidden) return;
    const seconds = Math.max(0, Math.floor((Date.now() - Date.parse(label.dataset.updatedAt)) / 1000));
    const elapsed = seconds < 60 ? seconds + "s" : Math.floor(seconds / 60) + "m " + (seconds % 60) + "s";
    label.textContent = (label.dataset.phase || "Waiting for model output") + " · " + elapsed + " since update";
  }, 1000);
  const refreshSeconds = Number(document.body.dataset.refreshSeconds);
  const refreshInterval = Number.isFinite(refreshSeconds) && refreshSeconds > 0 ? refreshSeconds * 1000 : 15000;
  setInterval(() => {
    if (document.hidden) return;
    // A rebuild replaces card rows, and those rows hold the fields a person types
    // into; a rebuilt node eats the caret and moves the page under the reader. The
    // board view is not the only rebuilder on this page: refreshStatus also renders
    // the board (renderBoardStatus > board-cards), which is why guarding only
    // refreshBoardView never stopped it. Hold the whole tick while a WIP field has
    // focus; the next tick after the field is left refreshes, so nothing stays stale.
    // An automatic refresh otherwise loses focus, makes typing a pain, and moves the
    // screen under the reader.
    if (state.page === "wip" && boardFieldFocused()) return;
    Promise.all([refreshSessions(), refreshStatus()]).catch(showError);
    if (state.page === "integrations") refreshIntegrations();
    if (state.page === "wip") refreshBoardView();
  }, refreshInterval);
  autonomy = window.TesseraAutonomy ? window.TesseraAutonomy.mount({ api, element, getSlug: () => state.slug, agentName }) : null;
  configView = window.TesseraSettings ? window.TesseraSettings.mount({
    api, element, root: $("settings-root"), status: $("config-status"), restartButton: $("core-restart"),
    onRestarted: () => Promise.all([refreshSessions(), refreshStatus()]).catch(showError),
    onAppearance: (patch) => {
      if (patch.theme) applyTheme(patch.theme);
      if (patch.skin) applySkin(patch.skin);
      if (typeof patch.accent_color === "string") applyAccent(patch.accent_color);
      if (typeof patch.show_thoughts === "boolean") applyThoughts(patch.show_thoughts);
      if (patch.send_shortcut) applyShortcut(patch.send_shortcut);
      if (patch.mark || typeof patch.easter_egg_f_it === "boolean") {
        state.status = state.status || {};
        state.status.ui = Object.assign({}, state.status.ui, patch);
        const ui = state.status.ui;
        applyMark(typeof ui.mark === "string" ? ui.mark : (document.documentElement.dataset.markChoice || "cairn"), ui.easter_egg_f_it === true);
        if (typeof patch.easter_egg_f_it === "boolean") paintApprovalLabel(ui);
      }
    },
    onApproval: (approval) => { state.approvalEpoch += 1; renderApproval(approval); },
    onChanged: () => refreshStatus().catch(showError),
  }) : null;
  if (configView) configView.load().catch((error) => { $("config-status").textContent = error.message || "Settings unavailable"; });
  shelfView = window.TesseraShelf ? window.TesseraShelf.mount({
    api, root: $("shelf-root"),
  }) : null;
  const shelfRefresh = $("shelf-refresh");
  if (shelfRefresh && shelfView) {
    shelfRefresh.addEventListener("click", () => {
      $("shelf-status").textContent = "Reading…";
      shelfView.load().then(() => { $("shelf-status").textContent = ""; })
        .catch((error) => { $("shelf-status").textContent = error.message || "Skills and tools unavailable"; });
    });
  }
  // At 1440x900 the section sat at y=645 behind a CLOSED 27px summary, after a 380px
  // Add-work form, with all 21 project tiles inside it. A phone-only open default left
  // the projects invisible on a desktop-width window. It now opens by default at
  // every width. An explicit choice by the reader is remembered and wins over it.
  const projectsBlock = $("board-projects-block");
  if (projectsBlock) {
    let remembered = null;
    try { remembered = localStorage.getItem("tessera.wip.projects.open"); } catch (_) { /* private mode */ }
    const initial = remembered === "off" ? false : true;
    projectsBlock.open = initial;
    // Setting .open also queues a toggle event, so a listener that simply stored
    // .open would record the default as if the reader had chosen it. Writing nothing
    // while the section is still in its default state keeps the phone default live
    // instead of freezing the first paint as a preference.
    projectsBlock.addEventListener("toggle", () => {
      try {
        if (projectsBlock.open === initial) localStorage.removeItem("tessera.wip.projects.open");
        else localStorage.setItem("tessera.wip.projects.open", projectsBlock.open ? "on" : "off");
      } catch (_) { /* private mode */ }
    });
    // The first thing inside the section was the whole "Start a shared space" create
    // form, so the project cards a person came to open began hundreds of pixels down.
    // A flex `order` rule cannot fix this: a <details> renders its children through a
    // shadow slot, so they are not flex items of the box and order is ignored. The
    // grid goes first and the create form follows it, at every width, not only phones.
    const grid = $("board-projects");
    const create = $("project-create-form");
    if (grid && create && grid.parentNode === projectsBlock && create.parentNode === projectsBlock) {
      projectsBlock.append(create);
    }
  }
  state.openProject = readLocation().project || "";
  state.arenaMode = readLocation().pane || "canvas";
  if (state.openProject) state.boardProjectFilter = state.openProject;
  setPage(readLocation().page, false, false);
  bootstrap().catch(showError);
})();
