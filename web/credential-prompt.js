/* Out-of-transcript credential entry. Never use the composer or browser storage. */
(() => {
  "use strict";
  function mount({api, root, element}) {
    let epoch = 0;
    let slug = null;
    let reads = 0;
    let requestId = null;
    const el = element || ((tag, className, text) => {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text != null) node.textContent = String(text);
      return node;
    });
    function clear() {
      for (const input of root.querySelectorAll("input")) input.value = "";
      root.replaceChildren();
      root.hidden = true;
      requestId = null;
    }
    async function refresh(selected) {
      const read = ++reads;
      if (slug !== selected) { epoch++; clear(); }
      slug = selected;
      if (!selected) { clear(); return; }
      let data;
      try { data = await api(`/api/sessions/${encodeURIComponent(selected)}/credential-prompts`); }
      catch (_) { return; } // An older core has no protected prompt route.
      if (read !== reads || slug !== selected) return;
      const request = data.requests?.[0];
      if (!request || request.state !== "pending") { epoch++; clear(); return; }
      if (requestId === request.id) return; // Never replace a key the person is typing.
      epoch++;
      clear();
      const generation = epoch;
      requestId = request.id;
      const form = el("form", "protected-credential-form");
      form.append(el("h3", "", "Protected credential entry"));
      form.append(el("p", "", (request.replacing ? "Replace key for " : "Save key for ") + request.name));
      form.append(el("p", "set-hint", request.purpose));
      form.append(el("p", "set-hint", "This value goes directly to the core’s credential store. It is not sent to the model or saved in the conversation. Use browser sign-in for OAuth accounts."));
      const label = el("label", "", "API key or token");
      const input = el("input");
      input.type = "password";
      input.autocomplete = "new-password";
      input.maxLength = 4096;
      input.required = true;
      label.append(input);
      const note = el("p", "form-error");
      note.setAttribute("role", "status");
      const save = el("button", "primary", "Save securely and continue");
      save.type = "submit";
      const cancel = el("button", "", "Cancel");
      cancel.type = "button";
      let busy = false;
      async function resolve(cancelled) {
        if (busy || generation !== epoch || slug !== selected) return;
        busy = true;
        save.disabled = cancel.disabled = true;
        input.disabled = true;
        note.textContent = cancelled ? "Cancelling…" : "Saving securely…";
        try {
          // Keep the value out of tool calls, messages, hooks and draft storage.
          const result = await api(`/api/sessions/${encodeURIComponent(selected)}/credential-prompts`, "POST",
            cancelled ? {request_id: request.id, cancel: true} : {request_id: request.id, value: input.value});
          input.value = "";
          if (generation !== epoch || slug !== selected) return;
          clear();
          root.hidden = false;
          root.append(el("p", "set-hint", result.state === "saved"
            ? (result.note || "Credential saved securely. Setup continues in this conversation.") : "Credential entry cancelled."));
        } catch (_) {
          if (generation === epoch && slug === selected) {
            note.textContent = "Could not save securely. Check the credential store or request a new form if this one expired. Your key has not been added to chat.";
          }
        } finally {
          busy = false;
          input.disabled = save.disabled = cancel.disabled = false;
        }
      }
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        if (input.value) return resolve(false);
      });
      cancel.addEventListener("click", () => resolve(true));
      form.append(label, note, save, cancel);
      root.hidden = false;
      root.append(form);
    }
    return {refresh, clear: () => { epoch++; reads++; slug = null; clear(); }};
  }
  window.TesseraCredentialPrompt = {mount};
})();
