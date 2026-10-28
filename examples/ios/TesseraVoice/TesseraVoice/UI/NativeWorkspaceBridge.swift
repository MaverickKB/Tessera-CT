import Foundation

enum NativeWorkspaceBridge {
    static let script = #"""
    (() => {
      "use strict";
      const connection = window.tesseraConnection;
      if (location.origin !== new URL(connection.origin).origin) return;
      const post = body => window.webkit.messageHandlers.tesseraNative.postMessage(body);
      addEventListener("error", event => post({action: "failure", detail: String(event.message || "A workspace script could not load.").slice(0, 200)}));
      addEventListener("unhandledrejection", event => post({action: "failure", detail: String(event.reason?.message || "A workspace request failed.").slice(0, 200)}));
      const route = () => new URLSearchParams(location.hash.slice(1)).get("session") || "";
      let call = {running: false, label: "Ready", slug: ""};
      function selection() { post({action: "selection", slug: route()}); }
      for (const method of ["pushState", "replaceState"]) {
        const original = history[method].bind(history);
        history[method] = function(...args) {
          const result = original(...args);
          selection();
          return result;
        };
      }
      addEventListener("hashchange", selection);
      const originalFetch = window.fetch.bind(window);
      window.fetch = function(input, options) {
        const url = new URL(input instanceof Request ? input.url : input, location.href);
        if (!connection.token || url.origin !== location.origin) return originalFetch(input, options);
        const request = new Request(input, options);
        const headers = new Headers(request.headers);
        headers.set("Authorization", "Bearer " + connection.token);
        return originalFetch(new Request(request, {headers}));
      };
      // Capture explicit voice clicks before the browser client can acquire audio.
      document.addEventListener("click", event => {
        const button = event.target.closest?.("#voice-toggle, #voice-page-toggle, #voice-mute");
        if (!button) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        if (button.id === "voice-mute") post({action: "mute"});
        else post({action: "voice", slug: route(), voice: document.getElementById("voice-pick")?.value || ""});
      }, true);
      function render() {
        for (const id of ["voice-toggle", "voice-page-toggle"]) {
          const node = document.getElementById(id);
          if (!node) continue;
          const text = call.running ? "End native call" : "Start native call";
          if (node.textContent !== text) node.textContent = text;
          if (call.running && node.disabled) node.disabled = false;
          const title = call.running ? call.label + " · " + call.slug : "Native background voice";
          if (node.title !== title) node.title = title;
        }
        const mute = document.getElementById("voice-mute");
        if (mute) {
          const text = call.muted ? "Unmute" : "Mute";
          if (mute.textContent !== text) mute.textContent = text;
          if (mute.disabled === call.running) mute.disabled = !call.running;
        }
        const status = document.getElementById("voice-page-state");
        const label = call.running ? call.label + " · " + call.slug : "Native voice ready";
        if (status && status.textContent !== label) status.textContent = label;
        for (const id of ["voice-mode", "voice-mic", "voice-mic-refresh", "voice-ptt"]) {
          const node = document.getElementById(id);
          if (node && !node.disabled) node.disabled = true;
        }
      }
      window.tesseraNativeState = state => { call = state; render(); };
      document.addEventListener("DOMContentLoaded", () => {
        new MutationObserver(render).observe(document.body, {childList: true, subtree: true, attributes: true, attributeFilter: ["disabled"]});
        render();
        selection();
        post({action: "ready"});
      });
    })();
    """#
}
