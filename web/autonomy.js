"use strict";
// Presence reporting, wake-aware message filtering, approval cards, notifications and the Desk window.
(function () {
  const HEARTBEAT_MS = 120000;
  const TYPING_MS = 15000;
  // A heartbeat is not a person. An open window that nobody has typed in or
  // focused recently must not keep the session marked present, or a due watch
  // waits forever behind an engaged operator.
  const HEARTBEAT_IDLE_MS = 300000;

  function hiddenIndices(messages) {
    const wakeTurns = new Set();
    for (const message of messages) {
      if (message.kind === "user" && message.meta && message.meta.wake) wakeTurns.add(message.meta.turn);
    }
    const hidden = new Set();
    for (const message of messages) {
      if (message.meta && Number.isInteger(message.meta.supersedes)) hidden.add(message.meta.supersedes);
    }
    for (const message of messages) {
      const meta = message.meta || {};
      if (message.kind === "user" && meta.wake) hidden.add(message.i);
      else if (meta.approval_result) hidden.add(message.i);
      else if (wakeTurns.has(meta.turn) && (message.kind === "tool" || message.kind === "echo")) hidden.add(message.i);
      // A wake turn's empty scaffolding is not speech. A row the model actually
      // wrote is: it streams into view under the thinking box, so hiding the
      // committed row takes the reply away the moment the turn moves on. Those
      // replies are important. Keep the text, hide the husks.
      else if (message.kind === "agent" && (meta.silent
        || (wakeTurns.has(meta.turn) && !meta.proactive && !String(message.text || "").trim()))) hidden.add(message.i);
    }
    return hidden;
  }

  function mount(options) {
    const { api, element, getSlug, agentName } = options;
    const approvals = new Map();
    let lastArrive = 0;
    let lastHuman = Date.now();
    let present = false;

    function report(stateName) {
      const slug = getSlug();
      if (!slug) return;
      api(`/api/sessions/${encodeURIComponent(slug)}/presence`, "POST", { state: stateName, source: "window" }).catch(() => {});
    }
    function arrive() {
      lastHuman = Date.now();
      if (present || Date.now() - lastArrive < 2000) return;
      present = true;
      lastArrive = Date.now();
      report("arrive");
    }
    function leave() {
      if (!present) return;
      present = false;
      report("leave");
    }
    window.addEventListener("focus", arrive);
    window.addEventListener("blur", leave);
    document.addEventListener("visibilitychange", () => (document.hidden ? leave() : document.hasFocus() && arrive()));
    setInterval(() => {
      if (!present || document.hidden) return;
      if (Date.now() - lastHuman >= HEARTBEAT_IDLE_MS) {
        leave();
        return;
      }
      report("here");
    }, HEARTBEAT_MS);
    let lastTyping = 0;
    const input = document.getElementById("message-input");
    if (input) input.addEventListener("input", () => {
      lastHuman = Date.now();
      if (Date.now() - lastTyping < TYPING_MS) return;
      lastTyping = Date.now();
      report("typing");
    });
    if (!document.hidden && document.hasFocus()) setTimeout(arrive, 1500);

    function askNotifications() {
      if (window.Notification && Notification.permission === "default") Notification.requestPermission().catch(() => {});
    }
    document.addEventListener("click", askNotifications, { once: true });

    function notify(message) {
      const meta = message.meta || {};
      const proactive = message.kind === "agent" && meta.proactive;
      const asking = message.kind === "note" && meta.approval;
      if (!(proactive || asking) || !document.hidden) return;
      if (!window.Notification || Notification.permission !== "granted") return;
      const title = asking ? agentName() + " asks for approval" : agentName();
      try { new Notification(title, { body: message.text.slice(0, 180), tag: "tessera-" + message.i }); } catch (_) { /* Unsupported here. */ }
    }

    function approvalNode(message, resolved) {
      const info = message.meta.approval;
      const node = element("article", "message note approval-card");
      node.dataset.index = String(message.i);
      node.dataset.approvalId = info.id;
      node.append(element("div", "message-text", message.text));
      const outcome = resolved.get(info.id);
      if (outcome) {
        node.append(element("p", "approval-state", outcome[0].toUpperCase() + outcome.slice(1)));
        return node;
      }
      const actions = element("div", "dialog-actions approval-actions");
      for (const [label, approve] of [["Deny", false], ["Approve", true]]) {
        const button = element("button", approve ? "primary" : "", label);
        button.type = "button";
        button.addEventListener("click", async () => {
          for (const item of actions.querySelectorAll("button")) item.disabled = true;
          try {
            await api(`/api/approvals/${encodeURIComponent(info.id)}`, "POST", { approve, source: "card" });
            actions.replaceWith(element("p", "approval-state", approve ? "Approved" : "Denied"));
          } catch (error) {
            actions.append(element("span", "form-error", error.message));
            for (const item of actions.querySelectorAll("button")) item.disabled = false;
          }
        });
        actions.append(button);
      }
      node.append(actions);
      return node;
    }

    function resolvedApprovals(messages) {
      const resolved = new Map();
      for (const message of messages) {
        const result = message.meta && message.meta.approval_result;
        if (result) resolved.set(result.id, result.state);
      }
      return resolved;
    }

    const dialog = document.getElementById("desk-dialog");
    const tabs = document.getElementById("desk-tabs");
    const image = document.createElement("img");
    image.className = "desk-image";
    image.hidden = true;
    document.getElementById("desk-view").append(image);
    const line = document.getElementById("desk-status");
    let current = null;

    async function show(name) {
      current = name;
      for (const button of tabs.querySelectorAll("button")) button.setAttribute("aria-pressed", String(button.dataset.desk === name));
      line.textContent = "Looking...";
      try {
        const [look, glance] = await Promise.all([
          api(`/api/desk/${encodeURIComponent(name)}/look`),
          api(`/api/desk/${encodeURIComponent(name)}/status`).catch(() => null),
        ]);
        if (current !== name) return;
        image.src = look.data_url;
        image.alt = name === "mac" ? "This Mac's screen" : `${agentName()}'s desk`;
        image.hidden = false;
        const parts = [];
        if (glance && "up" in glance) {
          parts.push(glance.up ? "Up" : "Down");
          if (glance.idle_seconds != null) parts.push(`idle ${glance.idle_seconds}s`);
          if (glance.open && glance.open.length) parts.push(glance.open.join(", "));
        }
        parts.push(new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }));
        line.textContent = parts.join(" · ");
      } catch (error) {
        if (current !== name) return;
        image.hidden = true;
        line.textContent = error.message;
      }
    }

    async function open() {
      const listing = await api("/api/desk");
      tabs.replaceChildren();
      const names = listing.desks.concat(listing.mac ? ["mac"] : []);
      for (const name of names) {
        const button = element("button", "", name === "mac" ? "This Mac" : name);
        button.type = "button";
        button.dataset.desk = name;
        button.addEventListener("click", () => show(name));
        tabs.append(button);
      }
      dialog.showModal();
      if (names.length) show(current && names.includes(current) ? current : names[0]);
      else line.textContent = "No desk configured.";
    }

    document.getElementById("desk-toggle").addEventListener("click", () => open().catch((error) => { line.textContent = error.message; dialog.showModal(); }));
    document.getElementById("desk-refresh").addEventListener("click", () => current && show(current));
    document.getElementById("desk-close").addEventListener("click", () => dialog.close());

    const timersDialog = document.getElementById("timers-dialog");
    const timersList = document.getElementById("timers-list");
    const timersLine = document.getElementById("timers-status");

    async function showTimers() {
      const state = await api("/api/autonomy");
      timersList.replaceChildren();
      timersLine.textContent = state.timers.length ? "" : "No timers.";
      for (const timer of state.timers) {
        const row = element("li", "timer-row");
        row.dataset.timerId = timer.id;
        const repeat = timer.repeat ? `, every ${timer.repeat}` : "";
        row.append(element("span", "", `${timer.note} · ${timer.slug} · in ${timer.in}${repeat}`));
        const cancel = element("button", "", "Cancel");
        cancel.type = "button";
        cancel.addEventListener("click", async () => {
          cancel.disabled = true;
          try {
            await api(`/api/timers/${encodeURIComponent(timer.id)}/cancel`, "POST", {});
            await showTimers();
          } catch (error) {
            timersLine.textContent = error.message;
            cancel.disabled = false;
          }
        });
        row.append(cancel);
        timersList.append(row);
      }
    }

    document.getElementById("timers-toggle").addEventListener("click", () => {
      timersDialog.showModal();
      showTimers().catch((error) => { timersLine.textContent = error.message; });
    });
    document.getElementById("timers-close").addEventListener("click", () => timersDialog.close());

    return { hiddenIndices, notify, approvalNode, resolvedApprovals };
  }

  window.TesseraAutonomy = { mount, hiddenIndices };
})();
