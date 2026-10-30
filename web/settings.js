/* Settings renders the core registry. Every control writes; none of the rows are invented here. */
(() => {
  "use strict";

  const LABELS = {
    edit: "Edit",
    remove: "Remove",
    "add-credential": "Add credential",
    "sign-in": "Sign in",
    refresh: "Refresh",
    check: "Check",
    "add-model": "Add model",
  };
  // "Add account" on a pool was the wrong word: the sign-in buttons above are the
  // accounts. A pool row adds a pool, so its button says so.
  const ADD = {providers: "Add model", credentials: "Add credential", mcp_servers: "Add plugin", named_accounts: "Add service account"};
  ADD.identities = "Add person";
  ADD.hosts = "Add host";
  const CLI_KEYS = new Set(["vendor", "command", "cwd"]);
  const HTTP_KEYS = new Set(["base_url", "credential", "kind"]);
  const OAUTH_ACCOUNTS = new Set(["xai", "openai-codex"]);
  const FORM_ADVANCED = new Set(["kind", "api_key_env", "chat", "max_concurrent_requests", "throttle_requests_per_minute", "throttle_tokens_per_minute"]);
  const NAME_OK = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

  function mount({api, element, root, status, restartButton, onRestarted, onAppearance, onApproval, onChanged}) {
    const view = {data: null, form: null, confirm: "", login: null, section: "all", search: "", detail: "", restoreName: "", accounts: null};
    let sectionChosen = false;

    const advancedOpen = new Map();
    const drafts = new Map();
    const scalarNotices = new Map();

    function scalarNotice(field, message) {
      scalarNotices.set(field.id, message);
      for (const node of root.querySelectorAll("p")) {
        if (node.id === domId(field) + "-notice") node.textContent = message;
      }
    }

    function navigate(section) {
      if (view.form) drafts.set(view.section, {form: view.form, detail: view.detail, discovery: view.discovery});
      const saved = drafts.get(section);
      drafts.delete(section);
      view.section = section;
      view.search = "";
      view.form = saved?.form || null;
      view.detail = saved?.detail || "";
      view.discovery = saved?.discovery || null;
      view.confirm = "";
      paint();
    }

    function addClass(node, name) {
      if (node && node.classList && typeof node.classList.add === "function") node.classList.add(name);
    }

    function domId(field) {
      if (field.id === "ui.show_thoughts") return "show-thoughts";
      return "setting-" + String(field.id).replace(/[^A-Za-z0-9_-]/g, "_");
    }

    function appearanceOf(field, value) {
      if (field.id === "ui.theme") return {theme: value};
      if (field.id === "ui.mark") return {mark: value};
      if (field.id === "ui.skin") return {skin: value};
      if (field.id === "ui.accent_color") return {accent_color: value};
      if (field.id === "ui.show_thoughts") return {show_thoughts: value};
      if (field.id === "ui.send_shortcut") return {send_shortcut: value};
      if (field.id === "ui.easter_egg_f_it") return {easter_egg_f_it: value};
      return null;
    }

    function readAppearance(data) {
      const patch = {};
      for (const section of data?.sections || []) {
        for (const field of section.fields || []) {
          const part = appearanceOf(field, field.value);
          if (part) Object.assign(patch, part);
        }
      }
      return patch;
    }

    function notice(result) {
      if (!result) return;
      if (result.detail) status.textContent = result.detail;
      else if (result.restart_required) status.textContent = "Saved. Restart the core to apply.";
      else if (result.changed === false) status.textContent = "";
      else status.textContent = "Saved";
    }

    function applyResult(result) {
      // Any settings change can change an account's grant or a model's state,
      // so the roster is re-read rather than kept for the life of the page.
      view.accounts = null;
      if (result?.settings) {
        if (result.settings.provider_pages) view.data = result.settings;
        else view.data = Object.assign({}, view.data, result.settings);
      }
      if (result?.approval && onApproval) onApproval(result.approval);
      const patch = readAppearance(view.data);
      if (onAppearance && Object.keys(patch).length) onAppearance(patch);
      paint();
      notice(result);
      if (onChanged) onChanged(result);
    }

    async function commitScalar(field, value) {
      const patch = appearanceOf(field, value);
      if (patch && onAppearance) onAppearance(patch);
      status.textContent = "Saving…";
      scalarNotice(field, "Saving…");
      try {
        const result = await api("/api/settings", "POST", {id: field.id, value});
        scalarNotice(field, result.restart_required ? "Saved. Restart the core to apply." : "Saved.");
        applyResult(result);
      } catch (error) {
        const message = error.message || "Not saved";
        try { await load(); } catch (_) { /* Keep the write error when the reload also fails. */ }
        scalarNotice(field, message);
        status.textContent = message;
      }
    }

    async function commitRecord(id, action, name, record) {
      const submittedSection = view.section;
      const submittedForm = view.form;
      const body = {id, action};
      if (name) body.name = name;
      if (record) body.record = record;
      status.textContent = action === "check" ? "Checking…" : "Saving…";
      try {
        const result = await api("/api/settings", "POST", body);
        if (view.form === submittedForm) {
          view.form = null;
          view.discovery = null;
          view.confirm = "";
        }
        if (drafts.get(submittedSection)?.form === submittedForm) drafts.delete(submittedSection);
        applyResult(result);
      } catch (error) {
        const message = error.message || "Not saved";
        // A switch that the core refused must not stay looking on. An open form
        // keeps what was typed so the person can correct it, with the error on
        // the form rather than in the page header.
        if (submittedForm) {
          submittedForm.error = message;
          paint();
          status.textContent = message;
          return;
        }
        try { await load(); } catch (_) { /* The error below is the one to show. */ }
        status.textContent = message;
      }
    }

    function inputFor(field, id) {
      let input;
      if (field.type === "bool") {
        input = document.createElement("input");
        input.type = "checkbox";
        input.checked = field.value === true;
      } else if (field.type === "enum") {
        input = document.createElement("select");
        const options = Array.isArray(field.options) ? field.options.slice() : [];
        if (!options.some((option) => option.value === (field.value ?? ""))) {
          const empty = field.id === "roles.main" ? "Pick a chat model" : "Current";
          options.unshift({value: field.value ?? "", label: field.value || empty});
        }
        for (const option of options) {
          const node = document.createElement("option");
          node.value = option.value;
          node.textContent = option.label;
          input.append(node);
        }
        input.value = field.value ?? "";
      } else if (field.type === "color") {
        input = document.createElement("input");
        input.type = "color";
        const sample = document.createElement("span");
        sample.style.color = "var(--accent)";
        document.body.append(sample);
        const channels = getComputedStyle(sample).color.match(/\d+/g) || [128, 128, 128];
        sample.remove();
        let current = "#";
        for (const channel of channels.slice(0, 3)) current += Number(channel).toString(16).padStart(2, "0");
        input.value = field.value || current;
      } else if (field.type === "secret") {
        input = document.createElement("input");
        input.type = "password";
        input.autocomplete = "new-password";
        input.placeholder = "Key";
        input.value = "";
      } else if (field.constraints?.lines) {
        input = document.createElement("textarea");
        input.rows = 2;
        input.value = field.value ?? "";
      } else {
        input = document.createElement("input");
        input.type = field.type === "int" || field.type === "float" ? "number" : "text";
        if (field.type === "float") input.step = "any";
        if (field.constraints?.min != null) input.min = String(field.constraints.min);
        if (field.constraints?.max != null) input.max = String(field.constraints.max);
        input.value = field.value ?? "";
        input.autocomplete = "off";
      }
      input.id = id;
      input.addEventListener("input", () => { input.dataset.dirty = "true"; });
      input.addEventListener("change", () => { delete input.dataset.dirty; });
      input.dataset.key = field.key || "";
      // A field the core will refuse is disabled. The disabled state comes from
      // the structured `inert` flag, not from the prose in `status`.
      // permissions.mode carries a status while remaining the live switch.
      if (field.inert) {
        input.disabled = true;
        input.setAttribute("aria-disabled", "true");
      }
      return input;
    }

    function scalarValue(field, input) {
      if (field.type === "bool") return input.checked;
      if (field.type === "int") return Number(input.value);
      if (field.type === "float") return Number(input.value);
      return input.value;
    }

    // Short help sits under its control. A longer hint is a disclosure on the
    // label line; the paragraph stays in the page and starts hidden. A
    // checkbox always shows its hint: "Details" beside a checkbox reads as a
    // link to somewhere else, and the sentence is the only thing that explains
    // what the box does.
    const INLINE_HINT_MAX = 120;

    function hintNode(text) {
      if (!text) return null;
      if (text.length <= INLINE_HINT_MAX) {
        const node = element("p", "set-hint", text);
        addClass(node, "settings-field-help");
        return node;
      }
      const fold = element("details", "set-hint-fold");
      fold.append(element("summary", "set-hint-summary", "About this list"));
      fold.append(element("p", "set-hint", text));
      return fold;
    }

    function fieldHelp(text, expand) {
      if (!text) return null;
      if (expand || text.length <= INLINE_HINT_MAX) {
        const hint = element("p", "set-hint", text);
        addClass(hint, "settings-field-help");
        return {fold: null, hint};
      }
      const hint = element("p", "set-hint", text);
      addClass(hint, "settings-field-help");
      hint.hidden = true;
      const fold = element("details", "set-hint-fold");
      const summary = element("summary", "set-hint-summary", "Details");
      summary.addEventListener("click", (event) => {
        event.preventDefault();
        hint.hidden = !hint.hidden;
      });
      fold.append(summary);
      return {fold, hint};
    }

    function controlFor(field, input) {
      const control = element("div", "set-control");
      addClass(control, "settings-field-control");
      control.append(input);
      return control;
    }

    function placeField(row, field, input, control, tier, expand) {
      addClass(row, "settings-field");
      if (tier) addClass(row, "settings-model-field");
      const help = fieldHelp(field.hint, expand || field.type === "bool");
      if (field.type === "bool") {
        addClass(row, "settings-check");
        const line = element("div", "settings-check-line");
        const label = element("label", "set-label", field.label);
        addClass(label, "settings-field-label");
        label.htmlFor = input.id;
        line.append(input, label);
        if (help && help.fold) line.append(help.fold);
        row.append(line);
        if (help && help.hint) row.append(help.hint);
        return;
      }
      const line = element("div", "settings-label-line");
      const label = element("label", "set-label", field.label);
      addClass(label, "settings-field-label");
      label.htmlFor = input.id;
      line.append(label);
      if (help && help.fold) line.append(help.fold);
      if (tier) line.append(element("span", "settings-tier", tier));
      if (control) addClass(control, "settings-field-control");
      row.append(line);
      if (control) row.append(control);
      if (help && help.hint) row.append(help.hint);
    }

    function attachStatus(row, field) {
      if (!field.status) return;
      const wide = field.status.length > 60;
      const fact = element("span", wide ? "set-fact set-fact-wide" : "set-fact", field.status);
      const children = row.children || [];
      let cursor = null;
      for (let index = 0; index < children.length; index += 1) {
        const child = children[index];
        const name = String(child.className || "");
        if (name.split(/\s+/).includes("set-hint") && child.hidden) {
          cursor = child;
          break;
        }
      }
      if (cursor) {
        row.insertBefore(fact, cursor);
        return;
      }
      // No hidden paragraph to sit in front of: the fact goes directly under the
      // control, and the help follows it.
      let controlAt = -1;
      for (let index = 0; index < children.length; index += 1) {
        const name = String(children[index].className || "");
        if (name.split(/\s+/).includes("set-control")) {
          controlAt = index;
          break;
        }
      }
      if (controlAt >= 0) row.insertBefore(fact, children[controlAt + 1] || null);
      else row.append(fact);
    }

    // A control that does not apply to the current choice is hidden, not left
    // inert. Today that is the duplex voice controls under the local pipe.
    function hiddenByState(field) {
      const rule = field.hidden_when;
      if (!rule) return false;
      for (const id of Object.keys(rule)) {
        const other = fieldById(id);
        if (!other) return false;
        if (other.value !== rule[id]) return false;
      }
      return true;
    }

    function renderScalar(field, tier) {
      const row = element("div", "set-row");
      const id = domId(field);
      const input = inputFor(field, id);
      input.addEventListener("change", () => commitScalar(field, scalarValue(field, input)));
      // A field whose choices are drawn as previews uses the previews as its
      // control. A select above them repeats the same list.
      const previewKind = field.id === "ui.mark" ? "mark"
        : field.id === "ui.skin" ? "skin" : "";
      if (previewKind) placeField(row, field, input, null, tier);
      else placeField(row, field, input, field.type === "bool" ? null : controlFor(field, input), tier);
      if (previewKind) {
        const previews = element("div", previewKind === "mark" ? "mark-previews" : "skin-previews");
        previews.setAttribute("role", "group");
        previews.setAttribute("aria-label", field.label);
        for (const option of field.options || []) {
          const button = element("button", previewKind === "mark" ? "mark-preview" : "skin-preview",
            option.label.split(" · ")[0]);
          button.type = "button";
          button.dataset[previewKind] = option.value;
          button.setAttribute("aria-pressed", String(field.value === option.value));
          button.title = option.label;
          if (previewKind === "skin") {
            const palette = element("span", "skin-palette");
            palette.setAttribute("aria-hidden", "true");
            for (let index = 0; index < 3; index++) palette.append(element("i"));
            button.prepend(palette);
          }
          button.addEventListener("click", () => commitScalar(field, option.value));
          previews.append(button);
        }
        row.append(previews);
      }
      if (field.type === "color") {
        const reset = element("button", "", "Use skin color");
        reset.type = "button";
        reset.disabled = !field.value;
        reset.addEventListener("click", () => commitScalar(field, ""));
        row.append(reset);
      }
      if (field.id === "autonomy.world_view") {
        // This is a prompt, not a note. It gets an editor, a live word count and
        // an example of the shape, because "what matters" is not a format a
        // person can guess.
        input.rows = 8;
        input.placeholder = "What matters right now, in the order it matters. One line per item.";
        const count = element("p", "set-count");
        const updateCount = () => {
          const text = String(input.value || "");
          const words = text.trim() ? text.trim().split(/\s+/).filter(Boolean).length : 0;
          count.textContent = words + (words === 1 ? " word" : " words") + " · " + text.length + " characters";
        };
        input.addEventListener("input", updateCount);
        updateCount();
        const sample = element("details", "set-hint-fold");
        sample.append(element("summary", "set-hint-summary", "Show an example"));
        const example = [
          "Wake only when one of these changed since the last wake.",
          "1. The board: a card blocked for more than a day.",
          "2. Hosts: disk below 10%, or a service that stopped answering.",
          "3. The build: a test that was green yesterday and fails now.",
          "Say what changed, what you checked, and what you did about it.",
        ].join("\n");
        sample.append(element("pre", "settings-example", example));
        const use = element("button", "", "Use this example");
        use.type = "button";
        use.addEventListener("click", () => {
          input.value = example;
          updateCount();
          // Save it like any other change, so the example the person just read
          // is the one the floor wakes with.
          input.dispatchEvent(new Event("change", {bubbles: true}));
        });
        sample.append(use);
        row.append(count, sample);
      }
      if (field.inert) addClass(row, "set-row-inert");
      if (hiddenByState(field)) row.hidden = true;
      attachStatus(row, field);
      const saved = element("p", "set-hint settings-save-status", scalarNotices.get(field.id) || "");
      saved.id = id + "-notice";
      saved.setAttribute("role", "status");
      input.setAttribute("aria-describedby", saved.id);
      row.append(saved);
      return row;
    }

    function providerKind(fields) {
      const draft = view.form?.draft || {};
      if (typeof draft.kind === "string" && draft.kind) return draft.kind;
      // A named account names its connection type under provider_kind. Reading only
      // `kind` here meant the account form could not tell what it was building, so it
      // offered a Credential on a CLI pool -- a combination config.validate() rejects
      // with "CLI accounts use the official CLI login, not API credentials."
      if (typeof draft.provider_kind === "string" && draft.provider_kind) return draft.provider_kind;
      const found = (fields || []).find((item) => item.key === "kind");
      if (found) return found.value || "openai_chat";
      const accountKind = (fields || []).find((item) => item.key === "provider_kind");
      return accountKind?.value || "openai_chat";
    }

    function providerAccount(fields) {
      const draft = view.form?.draft || {};
      if (typeof draft.account === "string" && draft.account) return draft.account;
      const found = (fields || []).find((item) => item.key === "account");
      return found?.value || "xai";
    }

    function mcpTransport(fields) {
      const url = (fields || []).find((item) => item.key === "url");
      if (url?.value?.trim()) return "http";
      const command = (fields || []).find((item) => item.key === "command");
      if (command?.value?.trim()) return "stdio";
      return "unknown";
    }

    function showKey(listId, key, kind, account, fields) {
      if (listId === "mcp_servers" && key === "env") {
        if (mcpTransport(fields) === "http") return false;
        return true;
      }
      // A CLI login holds its own grant; a credential beside it is refused on save, so
      // do not ask for one. The page must not offer a choice that cannot be stored.
      if (listId === "named_accounts" && key === "credential_ref") return kind !== "cli_worker";
      if (listId !== "providers" || view.form?.mode !== "add") return true;
      if (key === "credential") return false;
      // The account picker is the first question on Add model. A row that adds a
      // model to a known account already answered it, so the picker stays hidden
      // there and the endpoint and credential fields stay hidden with it.
      if (key === "account") return view.form?.flavor !== "signin";
      const flavor = view.form?.flavor || "";
      if (flavor === "local" && key === "api_key_env") return false;
      if (view.form?.presetLocked && (key === "base_url" || key === "kind")) return false;
      if (OAUTH_ACCOUNTS.has(account)) {
        if (HTTP_KEYS.has(key) || CLI_KEYS.has(key) || key === "api_key_env") return false;
        return true;
      }
      if (CLI_KEYS.has(key)) return kind === "cli_worker";
      if (key === "base_url" || key === "model") return kind !== "cli_worker";
      return true;
    }

    function readForm(form, fields, listId) {
      const kind = providerKind(fields);
      const account = providerAccount(fields);
      const record = {};
      for (const field of fields) {
        if (field.inert) continue;
        if (!showKey(listId, field.key, kind, account, fields)) continue;
        const input = form.querySelector(`[data-key="${field.key}"]`);
        if (!input) continue;
        // A control the form is not showing (another kind, an inert row) is
        // disabled, and a disabled control carries no value into the save.
        if (input.disabled) continue;
        if (field.type === "secret" && !input.value) continue;
        record[field.key] = scalarValue(field, input);
      }
      const hidden = view.form?.draft || {};
      for (const key of ["account", "credential", "base_url", "kind", "api_key_env"]) {
        if (!Object.prototype.hasOwnProperty.call(record, key) && hidden[key] != null && hidden[key] !== "") {
          record[key] = hidden[key];
        }
      }
      return record;
    }

    // Ask the endpoint what it serves. Two shapes, because the form knows two
    // things: a sign-in provider (fixed wiring, chosen by account) or an HTTP
    // endpoint being typed (base URL and protocol as currently drafted).
    function discoverControl(list, fields, form, input) {
      const wrap = element("div", "set-discover");
      const button = element("button", "", "Find models");
      button.type = "button";
      const note = element("p", "set-hint", "");
      addClass(note, "settings-field-help");
      note.hidden = true;
      const choices = element("div", "set-model-choices");
      const fillName = (model) => {
        const name = form.querySelector('[data-key="name"]');
        if (!name || name.value) return;
        const cleaned = String(model).replace(/[^A-Za-z0-9_-]/g, "").slice(0, 64);
        if (!cleaned || !/^[A-Za-z0-9]/.test(cleaned)) return;
        name.value = cleaned;
        if (view.form) view.form.draft.name = cleaned;
      };
      function paintDiscovery() {
        const saved = view.discovery;
        choices.replaceChildren();
        if (!saved || !saved.text) {
          note.hidden = true;
          note.textContent = "";
          return;
        }
        note.hidden = false;
        note.textContent = saved.text;
        for (const model of saved.models || []) {
          const pick = element("button", "set-model-choice", model);
          pick.type = "button";
          pick.addEventListener("click", () => {
            input.value = model;
            if (view.form) view.form.draft[input.dataset.key] = model;
            fillName(model);
            view.discovery = {text: "Model set to " + model + ". Save to keep it.", models: saved.models};
            paintDiscovery();
          });
          choices.append(pick);
        }
      }
      button.addEventListener("click", async () => {
        const draft = readForm(form, fields, list.id);
        // Only an account field that the form actually carries can mean a sign-in
        // provider. providerAccount() defaults to "xai", which would send a plain
        // HTTP backend down the OAuth path and hide the URL it needs. A sign-in
        // card stores the account on the draft because the dropdown is gone.
        const accountField = (fields || []).find((item) => item.key === "account");
        const account = accountField
          ? (draft.account || accountField.value || "")
          : (view.form?.draft?.account || "");
        const kind = draft.kind || providerKind(fields);
        const request = OAUTH_ACCOUNTS.has(account)
          ? {account}
          : {base_url: draft.base_url || view.form?.draft?.base_url || "", kind,
             credential: draft.credential || "",
             api_key_env: draft.api_key_env || view.form?.draft?.api_key_env || ""};
        if (!OAUTH_ACCOUNTS.has(account) && !request.base_url.trim()) {
          view.discovery = {text: "Type the base URL first, then ask what it serves.", models: []};
          paintDiscovery();
          return;
        }
        button.disabled = true;
        view.discovery = {text: "Asking the endpoint…", models: []};
        paintDiscovery();
        try {
          const answer = await api("/api/models/discover", "POST", request);
          const models = answer.models || [];
          const text = models.length === 1 ? "One model available." : models.length + " models available.";
          if (models.length === 1 && view.form) {
            input.value = models[0];
            view.form.draft[input.dataset.key] = models[0];
            fillName(models[0]);
          }
          view.discovery = {text, models: models.length > 1 ? models : []};
        } catch (error) {
          view.discovery = {text: error.message || "Could not read the model list", models: []};
        } finally {
          button.disabled = false;
          paintDiscovery();
        }
      });
      wrap.append(button, note, choices);
      paintDiscovery();
      return wrap;
    }

    function renderForm(list, fields, name) {
      const form = element("form", "record-form");
      addClass(form, "settings-form");
      form.dataset.form = list.id;
      const kind = providerKind(fields);
      const account = providerAccount(fields);
      const baseline = {};
      const primary = [];
      const advancedRows = [];
      const foldAdvanced = list.id === "providers" && view.form?.mode === "add" && !OAUTH_ACCOUNTS.has(account);
      const formError = view.form?.error || "";
      for (const field of fields) {
        if (!showKey(list.id, field.key, kind, account, fields)) continue;
        const drafted = view.form?.draft && Object.prototype.hasOwnProperty.call(view.form.draft, field.key);
        const shown = drafted ? {...field, value: view.form.draft[field.key]} : field;
        if (name && field.inline) continue;
        // The value the server sent, before anything was typed. hasEdits compares
        // against this so navigating away only reloads a form nobody touched. A
        // secret never round-trips, so its baseline is empty and the first typed
        // character is already an edit.
        baseline[field.key] = field.type === "secret" ? "" : field.value;
        const row = element("div", "set-row");
        const input = inputFor(shown, domId(field) + (name ? "-" + name : "-new"));
        if (field.key === "kind" || field.key === "account") {
          input.addEventListener("change", () => {
            view.form.draft = readForm(form, fields, list.id);
            view.form.draft[field.key] = input.value;
            paint();
          });
        } else {
          // Record the draft as it is typed. Without this the only captured draft
          // was the one a kind/account change wrote, so hasEdits had nothing true
          // to report and the navigation reload discarded the form.
          input.addEventListener("input", () => { view.form.draft[field.key] = scalarValue(field, input); });
          input.addEventListener("change", () => { view.form.draft[field.key] = scalarValue(field, input); });
        }
        const control = field.type === "bool" ? null : controlFor(shown, input);
        // A form is short. Folding its help into "Details" hides the sentence
        // that says what to type, so every hint in a form stays visible.
        placeField(row, shown, input, control, undefined, true);
        // A field is only offered for the kinds that read it. Records carry the
        // list on the field; a form that shows a Prometheus instance on an SSH
        // connection is asking for a value nothing reads.
        if (field.kinds && field.kinds.length) {
          addClass(row, "set-kind-row");
          row.dataset.kinds = field.kinds.join(",");
        }
        row.dataset.inert = field.inert ? "1" : "";
        // Model names are the one field a person usually does not know. Ask the
        // endpoint it is being wired to and offer what it actually serves, so a
        // typo cannot become a failed turn later. The answer is a suggestion: the
        // field stays typeable. Help stays directly under the input; the button
        // and its results follow that help.
        if (list.id === "providers" && field.key === "model") {
          row.append(discoverControl(list, fields, form, input));
        }
        if (list.id === "providers" && field.key === "name" && view.form?.mode === "add") {
          const note = element("p", "set-hint settings-field-help", "");
          const showId = () => {
            const raw = String(input.value || "").trim();
            const slug = raw.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);
            if (!raw) note.textContent = "A short name. Spaces are saved as dashes.";
            else if (!/^[a-z0-9]/.test(slug)) note.textContent = "Name needs a letter or number.";
            else note.textContent = "Saved as " + slug + ".";
          };
          input.addEventListener("input", showId);
          showId();
          const existing = row.querySelector(".set-hint");
          if (existing) existing.replaceWith(note);
          else row.append(note);
        }
        if (field.inert) addClass(row, "set-row-inert");
        attachStatus(row, field);
        if (foldAdvanced && FORM_ADVANCED.has(field.key)) advancedRows.push(row);
        else primary.push(row);
      }
      const errorOnEnv = formError && /environment variable/i.test(formError);
      let envRow = null;
      if (errorOnEnv) {
        envRow = primary.concat(advancedRows).find((row) => row.querySelector && row.querySelector('[data-key="api_key_env"]'));
      }
      if (formError && envRow) envRow.append(element("p", "settings-field-error", formError));
      else if (formError) form.append(element("p", "settings-field-error", formError));
      for (const row of primary) form.append(row);
      if (advancedRows.length) {
        const fold = element("details", "settings-advanced");
        fold.open = view.form?.advancedOpen === true || Boolean(envRow);
        fold.addEventListener("toggle", () => { if (view.form) view.form.advancedOpen = fold.open; });
        fold.append(element("summary", "", "Advanced"));
        for (const row of advancedRows) fold.append(row);
        form.append(fold);
      }
      view.form.baseline = baseline;
      // Hide the rows the chosen kind does not read, and disable their inputs so
      // a save cannot carry a value the form is not showing. Before a kind is
      // chosen every connection row stays hidden: the first decision is the kind.
      const kindSelect = form.querySelector('[data-key="kind"]');
      const chosenKind = kindSelect ? kindSelect.value : "";
      for (const row of form.querySelectorAll(".set-kind-row")) {
        const allowed = String(row.dataset.kinds || "").split(",");
        const applies = Boolean(chosenKind) && allowed.includes(chosenKind);
        row.hidden = !applies;
        for (const tag of ["input", "select", "textarea"]) {
          for (const field of row.querySelectorAll(tag)) {
            field.disabled = !applies || row.dataset.inert === "1";
          }
        }
      }
      const actions = element("div", "record-actions");
      addClass(actions, "settings-form-actions");
      const cancel = element("button", "", "Cancel");
      cancel.type = "button";
      cancel.addEventListener("click", () => { view.form = null; drafts.delete(view.section); view.discovery = null; paint(); });
      const saveLabel = view.form.mode !== "add" ? "Save"
        : (view.form.flavor === "endpoint" ? "Add endpoint" : (ADD[list.id] || "Add"));
      const save = element("button", "primary", saveLabel);
      save.type = "submit";
      actions.append(cancel, save);
      form.append(actions);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const record = readForm(form, fields, list.id);
        if (list.id === "providers" && view.form.mode === "add" && Object.prototype.hasOwnProperty.call(record, "name")) {
          const raw = String(record.name || "").trim();
          if (!raw) {
            view.form.error = "Name is required.";
            paint();
            return;
          }
          if (!NAME_OK.test(raw)) {
            const slug = raw.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);
            if (!/^[a-z0-9]/.test(slug)) {
              view.form.error = "Name needs a letter or number.";
              paint();
              return;
            }
            record.name = slug;
            view.form.draft.name = slug;
          }
        }
        view.form.error = "";
        if (view.form.mode === "add") commitRecord(list.id, "add", "", record);
        else commitRecord(list.id, "edit", name, record);
      });
      return form;
    }

    function renderLogin(login) {
      const panel = element("div", "record-form settings-login");
      addClass(panel, "settings-form");
      panel.addEventListener("contextmenu", (event) => event.preventDefault());
      const waiting = !(login.user_code || login.verification_url);
      const message = waiting ? (login.message || "Requesting a sign-in code.") : (login.message && login.message !== "Requesting a sign-in code." ? login.message : "");
      if (message) panel.append(element("p", "set-hint settings-field-help", message));
      if (login.status === "error" && login.message) {
        panel.append(element("p", "settings-field-error", login.message));
      }
      if (login.verification_url) {
        const box = document.createElement("input");
        box.readOnly = true;
        box.value = login.verification_url;
        box.setAttribute("aria-label", "Sign-in address");
        const open = element("button", "primary", "Open in browser");
        open.type = "button";
        open.addEventListener("click", () => {
          api("/api/oauth/open", "POST", {url: login.verification_url}).catch((error) => {
            status.textContent = error.message || "Could not open the browser";
          });
        });
        const copy = element("button", "", "Copy address");
        copy.type = "button";
        copy.addEventListener("click", () => copyValue(box, "Address copied"));
        panel.append(loginField("Sign-in address", box, open, copy));
      }
      if (login.user_code) {
        const box = document.createElement("input");
        box.readOnly = true;
        box.value = login.user_code;
        box.setAttribute("aria-label", "Sign-in code");
        const copy = element("button", "", "Copy code");
        copy.type = "button";
        copy.addEventListener("click", () => copyValue(box, "Code copied"));
        panel.append(loginField("Code", box, copy));
      }
      const actions = element("div", "record-actions");
      addClass(actions, "settings-form-actions");
      const cancel = element("button", "", "Cancel");
      cancel.type = "button";
      cancel.addEventListener("click", () => { view.login = null; paint(); });
      actions.append(cancel);
      panel.append(actions);
      return panel;
    }

    function loginField(labelText, box, ...buttons) {
      const row = element("div", "set-row");
      addClass(row, "settings-field");
      const label = element("label", "set-label", labelText);
      addClass(label, "settings-field-label");
      const control = element("div", "set-control");
      addClass(control, "settings-field-control");
      control.append(box, ...buttons);
      row.append(label, control);
      return row;
    }

    function copyValue(box, note) {
      box.focus();
      box.select();
      let copied = false;
      try { copied = document.execCommand("copy"); } catch (_) { copied = false; }
      if (!copied && navigator.clipboard) {
        navigator.clipboard.writeText(box.value).then(() => { status.textContent = note; }).catch(() => {
          status.textContent = "Select the text and use Edit, Copy";
        });
        return;
      }
      status.textContent = copied ? note : "Select the text and use Edit, Copy";
    }

    async function pollLogin(provider) {
      const ticket = view.login;
      if (!ticket || ticket.provider !== provider) return;
      try {
        const result = await api("/api/oauth/login?provider=" + encodeURIComponent(provider));
        if (!view.login || view.login.provider !== provider) return;
        view.login.verification_url = result.verification_url || "";
        view.login.user_code = result.user_code || "";
        view.login.message = result.message || "";
        view.login.status = result.status || "";
        if (result.status === "signed_in") {
          view.login = null;
          status.textContent = "Signed in.";
          paint();
          await load();
          return;
        }
        paint();
        if (result.status === "error") {
          status.textContent = result.message || "Sign-in failed";
          return;
        }
      } catch (error) {
        status.textContent = error.message || "Sign-in failed";
        return;
      }
      window.setTimeout(() => pollLogin(provider), 1500);
    }

    async function refreshLogin(provider) {
      status.textContent = "Refreshing the saved sign-in.";
      try {
        const result = await api("/api/oauth/refresh", "POST", {provider});
        status.textContent = result.message || "Refreshed.";
        await load();
      } catch (error) {
        status.textContent = error.message || "Refresh failed";
        try { await load(); } catch (_) { /* Keep the refresh error. */ }
      }
    }

    async function startLogin(provider) {
      view.section = "provider-accounts";
      view.login = {provider, verification_url: "", user_code: "", message: "Requesting a sign-in code.", status: "starting"};
      view.form = null;
      view.discovery = null;
      view.confirm = "";
      paint();
      const panel = root.querySelector(".settings-login");
      if (panel && panel.scrollIntoView) panel.scrollIntoView({block: "nearest"});
      status.textContent = "";
      try {
        const result = await api("/api/oauth/login", "POST", {provider});
        if (!view.login || view.login.provider !== provider) return;
        view.login.message = result.message || view.login.message;
        view.login.status = result.status || "starting";
        paint();
        pollLogin(provider);
      } catch (error) {
        if (view.login && view.login.provider === provider) {
          view.login.status = "error";
          view.login.message = error.message || "Sign-in failed";
          paint();
        }
      }
    }

    function yoloMode() {
      for (const section of view.data?.sections || []) {
        for (const field of section.fields || []) {
          if (field.id === "permissions.mode") return field.value === "yolo";
        }
      }
      return false;
    }

    function onAction(list, record, action) {
      if (action === "edit") {
        view.form = {id: list.id, mode: "edit", name: record.name, draft: {}};
        view.confirm = "";
        paint();
        return;
      }
      if (action === "remove") {
        // Unleashed drops the confirm: a plugin remove is not an approval prompt.
        // Safe mode keeps the existing confirm every other list already uses.
        if (list.id === "mcp_servers" && yoloMode()) {
          commitRecord(list.id, "remove", record.name);
          return;
        }
        view.confirm = list.id + ":" + record.name;
        view.form = null;
        paint();
        return;
      }
      if (action === "add-model") {
        const preset = Object.assign({}, record.preset || {});
        if (!preset.account) preset.account = record.name || "other";
        view.form = {
          id: "providers", mode: "add", draft: preset,
          flavor: record.kind === "local" ? "local" : (OAUTH_ACCOUNTS.has(preset.account) ? "signin" : "custom"),
          presetLocked: Boolean(preset.base_url),
          anchor: record.id || record.name,
        };
        view.detail = record.id || record.name;
        view.confirm = "";
        paint();
        return;
      }
      if (action === "sign-in") {
        startLogin(record.name);
        return;
      }
      if (action === "refresh") {
        refreshLogin(record.name);
        return;
      }
      if (action === "add-credential") {
        view.form = {id: "credentials", mode: "add", draft: {}};
        view.confirm = "";
        paint();
        root.querySelector("[data-form='credentials'] input")?.focus();
        return;
      }
      if (action === "check") {
        commitRecord(list.id, "check", record.name);
      }
    }

    function renderModelRecord(list, record) {
      const host = element("div", "record-block");
      const card = element("div", "settings-list-card");
      const line = element("div", "settings-list-row");
      const mainCol = element("div", "settings-list-main");
      const identity = element("div", "settings-check-line");
      identity.append(element("strong", "settings-card-name", record.title || record.name));
      if (record.status) identity.append(element("span", "settings-tag", record.status));
      for (const field of record.fields || []) {
        if (!field.inline) continue;
        const label = element("label", "set-inline");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.checked = field.value === true;
        if (field.inert) {
          box.disabled = true;
          box.setAttribute("aria-disabled", "true");
          if (field.hint) box.title = field.hint;
        } else {
          box.addEventListener("change", () => commitRecord(list.id, "edit", record.name, {[field.key]: box.checked}));
        }
        label.append(box, document.createTextNode(field.label));
        identity.append(label);
      }
      mainCol.append(identity);
      if (record.source) {
        const value = record.source === "No credential" || !record.source ? "none" : record.source;
        mainCol.append(element("span", "settings-card-line", "Credential: " + value));
      }
      const aside = element("div", "settings-list-aside");
      const confirmKey = list.id + ":" + record.name;
      if (view.confirm === confirmKey) {
        const now = element("button", "danger", "Remove now");
        now.type = "button";
        now.addEventListener("click", () => commitRecord(list.id, "remove", record.name));
        const cancel = element("button", "", "Cancel");
        cancel.type = "button";
        cancel.addEventListener("click", () => { view.confirm = ""; paint(); });
        aside.append(now, cancel);
      } else {
        for (const action of record.actions || []) {
          const button = element("button", action === "remove" ? "danger" : "", LABELS[action] || action);
          button.type = "button";
          button.addEventListener("click", () => onAction(list, record, action));
          aside.append(button);
        }
      }
      line.append(mainCol, aside);
      card.append(line);
      host.append(card);
      if (view.form?.id === list.id && view.form.mode === "edit" && view.form.name === record.name) {
        host.append(renderForm(list, record.fields || [], record.name));
      }
      return host;
    }

    function renderRecord(list, record) {
      if (list.id === "providers") return renderModelRecord(list, record);
      const host = element("div", "record-block");
      const row = element("div", list.id === "oauth_accounts" ? "record-row oauth-row" : "record-row");
      row.append(element("strong", "record-title", record.title || record.name));
      const control = element("div", "set-control");
      if (record.status) control.append(element("span", "set-fact", record.status));
      for (const field of record.fields || []) {
        if (!field.inline) continue;
        const label = element("label", "set-inline");
        const box = document.createElement("input");
        box.type = "checkbox";
        box.checked = field.value === true;
        // A plugin that serves AetherMind memory locks Enabled. The box stays
        // visible so the saved value is reviewable, and it does not post.
        if (field.inert) {
          box.disabled = true;
          box.setAttribute("aria-disabled", "true");
          if (field.hint) box.title = field.hint;
        } else {
          box.addEventListener("change", () => commitRecord(list.id, "edit", record.name, {[field.key]: box.checked}));
        }
        label.append(box, document.createTextNode(field.label));
        control.append(label);
      }
      row.append(control);
      const actions = element("div", "record-actions");
      const confirmKey = list.id + ":" + record.name;
      if (view.confirm === confirmKey) {
        const now = element("button", "danger", "Remove now");
        now.type = "button";
        now.addEventListener("click", () => commitRecord(list.id, "remove", record.name));
        const cancel = element("button", "", "Cancel");
        cancel.type = "button";
        cancel.addEventListener("click", () => { view.confirm = ""; paint(); });
        actions.append(now, cancel);
      } else {
        for (const action of record.actions || []) {
          const button = element("button", action === "remove" ? "danger" : action === "sign-in" ? "primary" : "", LABELS[action] || action);
          button.type = "button";
          button.addEventListener("click", () => onAction(list, record, action));
          actions.append(button);
        }
      }
      row.append(actions);
      if (record.summary) row.append(element("p", "plugin-detail", record.summary));
      if (record.source) row.append(element("p", "plugin-detail", (record.source_label || "Source") + ": " + record.source));
      if (record.connection && record.connection.detail) {
        const failed = record.connection.state === "error";
        row.append(element("p", failed ? "plugin-detail plugin-error" : "plugin-detail", record.connection.detail));
        if (record.connection.last_error && record.connection.state !== "error") {
          row.append(element("p", "plugin-detail plugin-error", "Last error: " + record.connection.last_error));
        }
      }
      if (record.note) row.append(element("p", "plugin-detail", record.note));
      host.append(row);
      if (view.login && view.login.provider === record.name && view.login.status !== "signed_in") host.append(renderLogin(view.login));
      if (view.form?.id === list.id && view.form.mode === "edit" && view.form.name === record.name) {
        host.append(renderForm(list, record.fields || [], record.name));
      }
      return host;
    }

    function renderRecords(field, options) {
      const wrap = element("div", "record-list");
      // A list nested inside another section names itself. A list whose label
      // repeats the page heading does not.
      if (field.show_label) wrap.append(element("h3", "settings-list-title", field.label));
      if (!options || options.hint !== false) {
        const hint = hintNode(field.hint);
        if (hint) wrap.append(hint);
      }
      // An empty list states what the list is for. One Add button and a folded
      // paragraph is a page that looks broken, not a page with nothing in it.
      const records = field.records || [];
      if (!records.length && field.empty) {
        wrap.append(element("p", "settings-empty-list set-hint", field.empty));
      }
      let previousGroup = "";
      for (const record of records) {
        if (field.id === "providers" && record.group && record.group !== previousGroup) {
          wrap.append(element("h3", "settings-group", record.group));
          previousGroup = record.group;
        }
        wrap.append(renderRecord(field, record));
      }
      if (view.form?.id === field.id && view.form.mode === "add") wrap.append(renderForm(field, field.blank || [], ""));
      else if ((field.actions || []).includes("add")) {
        const add = element("button", "settings-add", ADD[field.id] || "Add");
        add.type = "button";
        add.addEventListener("click", () => {
          view.form = {id: field.id, mode: "add", draft: {}};
          view.confirm = "";
          paint();
          root.querySelector(`[data-form="${field.id}"] input, [data-form="${field.id}"] select`)?.focus();
        });
        wrap.append(add);
      }
      // Owner strings that resolve to nobody are listed after the records, not as
      // records. They are evidence that a card wakes no one, and editing them into
      // the list would tidy away the defect this layer exists to show.
      const unresolved = field.unresolved || [];
      if (unresolved.length) {
        const report = element("div", "identity-unresolved");
        report.append(element("h3", "", "Owned by nobody (" + unresolved.length + ")"));
        for (const item of unresolved) {
          const row = element("div", "record-row");
          row.append(element("strong", "record-title", item.handle));
          const control = element("div", "set-control");
          const where = item.kind === "project" ? "Project" : "Card " + (item.ordinal ?? "");
          control.append(element("span", "set-fact", where + " · " + (item.title || "") + (item.status ? " · " + item.status : "")));
          row.append(control);
          report.append(row);
        }
        wrap.append(report);
      }
      const recordErrors = Array.isArray(field.record_errors) ? field.record_errors : [];
      if (recordErrors.length) {
        const report = element("div", "identity-record-errors");
        report.append(element("h3", "", "Identity record errors"));
        for (const item of recordErrors) {
          if (!item || typeof item !== "object") continue;
          report.append(element("p", "set-fact", (item.what || "record") + ": " + (item.error || "unreported")));
        }
        wrap.append(report);
      }
      return wrap;
    }

    function filterSections() {
      const query = view.search.trim().toLowerCase();
      let visible = 0;
      for (const block of root.querySelectorAll(".settings-block")) {
        const selected = view.section === "all" || block.dataset.section === view.section;
        const matches = !query || block.textContent.toLowerCase().includes(query);
        // A folded advanced field still counts as a match, so a search that lands on
        // one must open the fold — otherwise the page says it found something and
        // shows a shut disclosure.
        for (const fold of block.querySelectorAll(".settings-advanced")) {
          // querySelectorAll already filters by class in a browser, but the
          // browser-free harness returns every child and may expose only
          // className, so test both without dereferencing a missing classList.
          const isFold = (fold.classList && fold.classList.contains("settings-advanced"))
            || String(fold.className || "").split(/\s+/).includes("settings-advanced");
          if (!isFold) continue;
          const inside = query && fold.textContent.toLowerCase().includes(query);
          if (inside) fold.open = true;
        }
        block.hidden = !selected || !matches;
        if (!block.hidden) visible += 1;
      }
      for (const button of root.querySelectorAll(".settings-nav button")) button.setAttribute("aria-pressed", String(button.dataset.section === view.section));
      const empty = root.querySelector(".settings-empty");
      if (empty) empty.hidden = visible > 0;
    }

    function fieldById(id) {
      for (const section of view.data?.sections || []) {
        for (const field of section.fields || []) {
          if (field.id === id) return field;
        }
      }
      return null;
    }

    function navButton(id, label, extra) {
      const button = element("button", extra || "", label);
      button.type = "button";
      button.dataset.section = id;
      button.id = "settings-nav-" + id;
      button.addEventListener("click", () => {
        navigate(id);
      });
      return button;
    }

    function crumbBar(parts) {
      const line = element("p", "settings-crumbs");
      parts.forEach((part, index) => {
        if (index) line.append(document.createTextNode(" / "));
        const target = index === 0 ? "provider-accounts" : (index === 1 ? "provider-accounts" : view.section);
        const button = element("button", "settings-crumb", part);
        button.type = "button";
        button.addEventListener("click", () => navigate(target));
        line.append(button);
      });
      return line;
    }

    function openAdd() {
      // Adding a model starts here now. The old target was an Endpoints page that
      // listed the same backends this page lists, and the form that opened there
      // never asked which account it belonged to.
      view.section = "models";
      view.discovery = null;
      view.form = {
        id: "providers", mode: "add", flavor: "custom",
        draft: {}, presetLocked: false, anchor: "", error: "",
      };
      view.detail = "";
      paint();
      root.querySelector('[data-form="providers"] select[data-key="account"]')?.focus();
    }

    function connectRow(row) {
      const wrap = element("div", "settings-card settings-list-card");
      const line = element("div", "settings-connect-row settings-list-row");
      const mainCol = element("div", "settings-list-main");
      mainCol.append(element("strong", "settings-card-name", row.name));
      const when = row.added && row.added !== "Not recorded" ? "Added " + row.added : "";
      const detail = [row.line || row.source || "", when].filter(Boolean).join(" · ");
      if (detail) mainCol.append(element("span", "settings-card-line", detail));
      const aside = element("div", "settings-list-aside");
      const on = row.pill === "Connected" || row.pill === "Saved" || row.pill === "Configured" || row.pill === "Keychain item found";
      if (row.pill) aside.append(element("span", on ? "settings-pill settings-pill-on" : "settings-pill", row.pill));
      if ((row.actions || []).includes("sign-in") && row.pill !== "Connected") {
        const sign = element("button", "primary", "Sign in");
        sign.type = "button";
        sign.addEventListener("click", (event) => {
          event.stopPropagation();
          const record = Object.assign({}, row, {name: row.id});
          onAction({id: "oauth_accounts"}, record, "sign-in");
        });
        aside.append(sign);
      }
      const key = row.id || row.name;
      const open = element("button", "settings-chevron", "›");
      open.type = "button";
      open.setAttribute("aria-label", "Details for " + row.name);
      open.addEventListener("click", () => {
        if (row.kind === "local") {
          openAdd();
          return;
        }
        view.detail = view.detail === key ? "" : key;
        paint();
      });
      aside.append(open);
      if (row.removable) {
        const remove = element("button", "settings-remove", "×");
        remove.type = "button";
        remove.setAttribute("aria-label", "Remove " + row.name);
        remove.addEventListener("click", () => onAction({id: row.kind === "key" ? "credentials" : "providers"}, row, "remove"));
        aside.append(remove);
      }
      const listId = row.kind === "key" ? "credentials" : "providers";
      if (view.confirm === listId + ":" + row.name) {
        const confirm = element("div", "record-actions");
        confirm.append(element("span", "", "Remove " + row.name + "?"));
        const now = element("button", "danger", "Remove now");
        now.type = "button";
        now.addEventListener("click", () => commitRecord(listId, "remove", row.name));
        const cancel = element("button", "", "Cancel");
        cancel.type = "button";
        cancel.addEventListener("click", () => { view.confirm = ""; paint(); });
        confirm.append(now, cancel);
        wrap.append(confirm);
      }
      line.append(mainCol, aside);
      wrap.append(line);
      if (view.login && (view.login.provider === row.id || view.login.provider === row.name) && view.login.status !== "signed_in") {
        wrap.append(renderLogin(view.login));
      }
      if (view.detail === key) {
        const detail = element("div", "settings-detail");
        if (when) detail.append(element("p", "set-fact", when));
        if (row.source) {
          const value = row.source === "No credential" ? "none" : row.source;
          detail.append(element("p", "set-fact", "Credential: " + value));
        }
        for (const action of row.actions || []) {
          const button = element("button", action === "sign-in" ? "primary" : "", LABELS[action] || action);
          button.type = "button";
          button.addEventListener("click", () => {
            const record = Object.assign({}, row, {name: row.kind === "signin" ? row.id : (row.name || row.id)});
            const listId = row.kind === "signin" ? "oauth_accounts" : (row.kind === "key" ? "credentials" : "providers");
            onAction({id: listId}, record, action);
          });
          detail.append(button);
        }
        for (const model of row.models || []) {
          const modelLine = element("div", "settings-connect-row");
          modelLine.append(element("span", "", model.title || model.name));
          const value = model.source === "No credential" ? "none" : (model.source || "");
          modelLine.append(element("span", "settings-card-line", value ? "Credential: " + value : ""));
          if (model.removable) {
            const remove = element("button", "settings-remove", "×");
            remove.type = "button";
            remove.setAttribute("aria-label", "Remove " + model.name);
            remove.addEventListener("click", () => onAction({id: "providers"}, model, "remove"));
            modelLine.append(remove);
          }
          detail.append(modelLine);
        }
        if (view.form?.mode === "add" && view.form.anchor === key) {
          const list = fieldById("providers");
          if (list) detail.append(renderForm(list, list.blank || [], ""));
        }
        wrap.append(detail);
      }
      return wrap;
    }

    function paintUpdate(main) {
      const inBlock = !String(main.className || "").includes("settings-main");
      if (!inBlock) {
        main.append(crumbBar(["Settings", "Update"]));
        main.append(element("h2", "", "Update"));
      }
      main.append(element("p", "set-hint", fieldById("update.status")?.hint || ""));
      const report = element("div", "update-report");
      const checkButton = element("button", "", "Check upstream");
      const applyButton = element("button", "", "Update now");
      checkButton.type = "button";
      applyButton.type = "button";
      applyButton.disabled = true;
      const line = (text) => element("p", "set-fact", text);
      const rows = element("div", "update-commits");
      report.append(checkButton, applyButton, line("Not checked yet."));
      main.append(report, rows);
      let latest = null;

      async function check() {
        checkButton.disabled = true;
        applyButton.disabled = true;
        report.replaceChildren(checkButton, applyButton, line("Checking upstream…"));
        try { latest = await api("/api/update/check", "GET"); }
        catch (error) {
          report.replaceChildren(checkButton, applyButton, line(error.message || "The check failed."));
          checkButton.disabled = false;
          return;
        }
        checkButton.disabled = false;
        if (!latest.available) {
          report.replaceChildren(checkButton, applyButton, line(latest.reason || "Updates are not available for this install."));
          return;
        }
        applyButton.disabled = latest.behind < 1;
        const origin = latest.origin || {};
        const head = line((latest.behind < 1 ? "Up to date with " : latest.behind + " commit" + (latest.behind === 1 ? "" : "s") + " behind ")
          + (origin.url || "upstream") + " (" + (latest.branch || "main") + "), at " + String(latest.current || "").slice(0, 10));
        report.replaceChildren(checkButton, applyButton, head);
        rows.replaceChildren();
        for (const commit of latest.commits || []) {
          rows.append(element("p", "set-fact", (commit.sha || "") + "  " + (commit.message || "")));
        }
      }

      async function apply() {
        applyButton.disabled = true;
        checkButton.disabled = true;
        report.replaceChildren(checkButton, applyButton, line("Updating. The core restarts when the reinstall finishes…"));
        try {
          const result = await api("/api/update/apply", "POST", {});
          if (result?.updated) {
            report.replaceChildren(checkButton, applyButton, line(result.reason || "Updated. Waiting for the core to come back…"));
          } else {
            report.replaceChildren(checkButton, applyButton, line(result?.reason || "The update did not complete."));
            checkButton.disabled = false;
          }
        } catch (error) {
          report.replaceChildren(checkButton, applyButton, line(error.message || "The update failed."));
          checkButton.disabled = false;
        }
      }

      checkButton.addEventListener("click", () => check());
      applyButton.addEventListener("click", () => apply());
    }

    function paintProvider(main) {
      const page = view.data.provider_pages.accounts;
      main.append(crumbBar(page.crumb));
      main.append(element("h2", "", page.title));
      main.append(element("p", "set-hint", page.help));
      if (page.api_key_link) {
        const link = element("button", "settings-text-link", page.api_key_link);
        link.type = "button";
        // One place holds keys. This link goes there instead of opening a second
        // page that lists the same credentials under another name.
        link.addEventListener("click", () => navigate("credentials"));
        main.append(link);
      }
      if (page.local_row) {
        const spec = typeof page.local_row === "string" ? {name: page.local_row, line: ""} : page.local_row;
        main.append(connectRow({
          id: "run-locally", name: spec.name, line: spec.line, kind: "local", actions: [],
        }));
      }
      const connected = page.connected || [];
      if (connected.length) {
        main.append(element("h3", "", "Connected"));
        for (const row of connected) main.append(connectRow(row));
      }
      const available = page.available || page.records || [];
      if (page.available) main.append(element("h3", "", "Connect another provider"));
      for (const row of available) main.append(connectRow(row));
    }

    function modelToggle(account, model, checked, kind) {
      const box = document.createElement("input");
      box.type = kind === "default" ? "radio" : "checkbox";
      if (kind === "default") box.name = "settings-default-model";
      box.checked = checked;
      box.setAttribute("aria-label", (kind === "default"
        ? "Make " + model.id + " the default chat model"
        : "List " + model.id + " in the chat model picker"));
      box.addEventListener("change", () => setModelState(account.id, model, kind));
      return box;
    }

    // Every model an account offers is a row with its own two switches. The
    // name is not a button: a click on a name must never change what answers
    // chat. Only the Default column does that, and it says so.
    function modelTable(account) {
      const table = element("div", "settings-model-table");
      const head = element("div", "settings-model-head");
      head.append(element("span", "settings-model-id", "Model"),
                  element("span", "settings-model-col", "In chat"),
                  element("span", "settings-model-col", "Default"));
      table.append(head);
      for (const model of account.models) {
        const row = element("div", "settings-model-row");
        const name = element("code", "settings-model-id", model.id);
        if (model.role === "main") name.append(element("span", "settings-model-current", "default"));
        row.append(name,
                   modelToggle(account, model, model.chat === true, "chat"),
                   modelToggle(account, model, model.role === "main", "default"));
        table.append(row);
      }
      return table;
    }

    function accountModelBlock(account) {
      const block = element("div", "settings-account");
      const head = element("div", "settings-account-head");
      head.append(element("strong", "settings-card-name", account.label));
      if (account.status) head.append(element("span", "settings-tag", account.status));
      block.append(head);
      if (account.error) {
        block.append(element("p", "settings-field-error", account.error));
        return block;
      }
      if (!account.models.length) {
        block.append(element("p", "set-hint", "This account reported no models."));
        return block;
      }
      block.append(modelTable(account));
      return block;
    }

    function renderAccountRows(host) {
      const accounts = (view.accounts && view.accounts.accounts) || [];
      const title = element("h3", "", "Connected accounts");
      if (!accounts.length) {
        host.replaceChildren(title, element("p", "set-hint", "No sign-in account is connected. Connect one on Accounts."));
        return;
      }
      const nodes = [title];
      for (const account of accounts) nodes.push(accountModelBlock(account));
      host.replaceChildren(...nodes);
    }

    // The page cannot know an account's models without asking the service. It
    // renders the accounts it already knows, then fills the model rows, and a
    // failed read stays on that account's block.
    function paintAccountModels(main) {
      const host = element("div", "settings-accounts");
      main.append(host);
      if (view.accounts) {
        renderAccountRows(host);
        return;
      }
      host.append(element("p", "set-hint", "Reading models from connected accounts…"));
      api("/api/models/accounts", "GET").then((data) => {
        view.accounts = data;
        renderAccountRows(host);
      }).catch((error) => {
        host.replaceChildren(element("p", "settings-field-error", error.message || "Could not read connected accounts."));
      });
    }

    async function setModelState(accountId, model, kind) {
      const body = {account: accountId, model: model.id};
      if (kind === "chat") body.chat = model.chat !== true;
      if (kind === "default") body.default = true;
      status.textContent = "Saving " + model.id + "…";
      try {
        const result = await api("/api/models/state", "POST", body);
        applyResult(result);
        status.textContent = model.id + " saved.";
      } catch (error) {
        // The switch did not land, so the page must show the saved state again.
        view.accounts = null;
        paint();
        status.textContent = error.message || "Not saved";
      }
    }

    function paintAccountModels(main) {
      const host = element("div", "settings-accounts");
      main.append(host);
      if (view.accounts) {
        renderAccountRows(host);
        return;
      }
      host.append(element("p", "set-hint", "Reading models from connected accounts…"));
      api("/api/models/accounts", "GET").then((data) => {
        view.accounts = data;
        renderAccountRows(host);
      }).catch((error) => {
        host.replaceChildren(element("p", "settings-field-error", error.message || "Could not read connected accounts."));
      });
    }

    function paintModels(main) {
      main.append(crumbBar(["Settings", "Models"]));
      main.append(element("h2", "", "Models"));
      const section = (view.data.sections || []).find((item) => item.id === "models");
      if (section?.start_hint) main.append(element("p", "set-hint", section.start_hint));
      const mainRole = fieldById("roles.main");
      const failover = fieldById("roles.failover");
      if (mainRole) main.append(renderScalar(mainRole, "Required"));
      if (failover) main.append(renderScalar(failover, "Optional"));
      paintAccountModels(main);
      const models = fieldById("providers");
      if (models) main.append(renderRecords(models, {hint: false}));
      const advanced = (section?.fields || []).filter((field) => field.tier === "advanced");
      if (advanced.length) {
        const fold = element("details", "settings-advanced");
        fold.append(element("summary", "", "Advanced"));
        for (const field of advanced) fold.append(field.type === "list-of-records" ? renderRecords(field) : renderScalar(field));
        main.append(fold);
      }
    }

    function paintShell() {
      root.replaceChildren();
      const shell = element("div", "settings-shell");
      const navigation = element("nav", "settings-nav settings-nav-side");
      navigation.setAttribute("aria-label", "Settings sections");
      navigation.append(element("div", "settings-nav-label", "Required"));
      navigation.append(element("div", "settings-nav-label settings-nav-sub", "Providers"));
      navigation.append(navButton("provider-accounts", "Accounts", "settings-nav-sub"));
      navigation.append(navButton("models", "Models", "settings-nav-sub"));
      navigation.append(navButton("credentials", "Saved credentials", "settings-nav-sub"));
      const optional = new Set(["identity", "voice", "appearance", "goblin", "integrations", "hosts", "plugins"]);
      const advanced = new Set(["accounts", "systems", "computer", "permissions", "autonomy", "judge", "update"]);
      const labels = {credentials: "Saved credentials", accounts: "Service accounts"};
      navigation.append(element("div", "settings-nav-label", "Optional"));
      for (const section of view.data.sections || []) {
        if (!optional.has(section.id)) continue;
        navigation.append(navButton(section.id, labels[section.id] || section.label));
      }
      navigation.append(element("div", "settings-nav-label", "Advanced"));
      for (const section of view.data.sections || []) {
        if (!advanced.has(section.id)) continue;
        navigation.append(navButton(section.id, labels[section.id] || section.label));
      }
      navigation.append(navButton("file-only", "Set in config.toml"));
      const floorButton = element("button", "", "Floor definition");
      floorButton.type = "button";
      floorButton.dataset.section = "floor";
      floorButton.addEventListener("click", () => navigate("floor"));
      navigation.append(floorButton);
      const sectionSelect = element("select", "settings-section-select");
      sectionSelect.id = "settings-section-select";
      sectionSelect.setAttribute("aria-label", "Settings section");
      const addOption = (parent, value, label) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = label;
        parent.append(option);
      };
      addOption(sectionSelect, "all", "All settings");
      const groups = [
        ["Required", [["provider-accounts", "Accounts"], ["models", "Models"],
                      ["credentials", "Saved credentials"]]],
        ["Optional", [["identity", "Identity"], ["voice", "Voice"],
                      ["appearance", "Appearance"], ["goblin", "Goblin Mode"],
                      ["integrations", "Integrations"], ["hosts", "Hosts"],
                      ["plugins", "Plugins"]]],
        ["Advanced", [["accounts", "Service accounts"], ["systems", "Systems"], ["computer", "Computer use"],
                      ["permissions", "Permissions"], ["autonomy", "Autonomy"],
                      ["judge", "Session judge (Jev)"], ["update", "Update"],
                      ["file-only", "Set in config.toml"], ["floor", "Floor definition"]]],
      ];
      const known = new Set(["all"]);
      for (const [label, items] of groups) {
        const group = document.createElement("optgroup");
        group.label = label;
        for (const [value, text] of items) {
          addOption(group, value, text);
          known.add(value);
        }
        sectionSelect.append(group);
      }
      sectionSelect.value = known.has(view.section) ? view.section : "all";
      sectionSelect.addEventListener("change", () => {
        navigate(sectionSelect.value);
      });
      const sectionLabel = element("label", "settings-section-label", "Settings section");
      sectionLabel.setAttribute("for", "settings-section-select");
      navigation.insertBefore(sectionLabel, navigation.firstChild);
      navigation.insertBefore(sectionSelect, sectionLabel.nextSibling);
      const search = element("input", "settings-search");
      search.type = "search";
      search.id = "settings-search";
      search.placeholder = "Find a setting";
      search.setAttribute("aria-label", "Find a setting");
      search.value = view.search;
      search.addEventListener("input", () => {
        const wasSearching = Boolean(view.search.trim());
        view.search = search.value;
        if (!wasSearching && view.search.trim()) {
          // Build the searchable sections once; subsequent keystrokes keep this input.
          if (view.form) drafts.set(view.section, {form: view.form, detail: view.detail, discovery: view.discovery});
          view.section = "all";
          view.form = null;
          view.detail = "";
          paint();
        } else filterSections();
      });
      const main = element("div", "settings-main");
      main.append(search);
      if (view.section !== "all" && !view.search.trim()) {
        const back = element("button", "settings-back", "All settings");
        back.type = "button";
        back.addEventListener("click", () => {
          navigate("all");
        });
        main.append(back);
      }
      if (Array.isArray(view.data.setup_steps) && view.data.setup_steps.length) {
        const steps = element("ol", "settings-setup");
        for (const step of view.data.setup_steps) {
          const item = element("li", step.done ? "settings-setup-done" : "");
          const next = element("button", "settings-text-link", (step.done ? "✓ " : "") + step.text);
          next.type = "button";
          next.addEventListener("click", () => navigate(step.n === 1 ? "provider-accounts" : "models"));
          item.append(next);
          steps.append(item);
        }
        steps.append(element("li", "", "Everything else is optional."));
        main.append(steps);
        if (view.data.setup_steps.every((step) => step.done)) {
          const chat = element("a", "settings-back", "Start chatting");
          chat.href = "#view=chat";
          main.append(chat);
        }
      } else if (view.data.setup) main.append(element("p", "settings-setup", view.data.setup));
      const page = view.section;
      if (page === "provider-accounts") {
        paintProvider(main);
      } else if (page === "models") {
        paintModels(main);
      } else if (page === "credentials") {
        main.append(crumbBar(["Settings", "Saved credentials"]));
        main.append(element("h2", "", "Saved credentials"));
        main.append(element("p", "set-hint", "Save an API key here, then open Models to add your provider’s endpoint and model. The key value is never shown."));
        const next = element("button", "", "Next: configure a model");
        next.type = "button";
        next.addEventListener("click", () => navigate("models"));
        main.append(next);
        const saved = fieldById("credentials");
        for (const record of saved?.records || []) {
          main.append(connectRow({
            id: record.name, name: record.name, pill: record.pill || "Not connected",
            line: record.line || record.stored || record.status || "",
            added: record.added, removable: (record.actions || []).includes("remove"),
            kind: "key", actions: (record.actions || []).filter((action) => action !== "remove"),
          }));
        }
        if (!saved?.records?.length) main.append(element("p", "set-hint", "No saved credentials yet."));
        const add = element("button", "settings-add", "+ Add");
        add.type = "button";
        add.addEventListener("click", () => {
          view.form = {id: "credentials", mode: "add", draft: {}};
          paint();
        });
        main.append(add);
        if (saved && view.form?.id === "credentials" && view.form.mode === "add") {
          main.append(renderForm(saved, saved.blank || [], ""));
        }
        if (saved && view.form?.id === "credentials" && view.form.mode === "edit") {
          const record = (saved.records || []).find((item) => item.name === view.form.name);
          if (record) main.append(renderForm(saved, record.fields || [], record.name));
        }
      } else if (page === "update") {
        paintUpdate(main);
      } else if (page === "file-only") {
        const listed = fileOnlyBlock();
        if (listed) main.append(listed);
      } else if (page === "all" || view.search.trim()) {
        for (const section of view.data.sections || []) main.append(sectionBlock(section));
        const listed = fileOnlyBlock();
        if (listed) main.append(listed);
      } else {
        const section = (view.data.sections || []).find((item) => item.id === page);
        if (section) main.append(sectionBlock(section));
        if (page === "systems") {
          const platform = document.getElementById("settings-platform");
          const text = platform && platform.textContent ? platform.textContent.trim() : "";
          if (text) main.append(element("p", "settings-platform-copy set-hint", text));
        }
      }
      const hidden = element("div", "settings-offscreen");
      hidden.hidden = true;
      if (page !== "all") {
        for (const section of view.data.sections || []) {
          if (section.id === page || section.id === "providers") continue;
          if (page === "models" && section.id === "models") continue;
          for (const field of section.fields || []) {
            if (field.type === "list-of-records") continue;
            hidden.append(renderScalar(field));
          }
        }
      }
      shell.append(navigation, main);
      root.append(shell, hidden);
      if (page === "floor") {
        const floor = element("section", "settings-block");
        floor.dataset.section = "floor";
        floor.append(element("h2", "", "Floor definition"));
        floor.append(element("p", "settings-start set-hint",
          "What a floor wake says, and the rules that decide when one is worth sending. " +
          "Each value below is saved to the floor definition file, not to config.toml."));
        const floorHost = element("div", "floor-editor");
        floor.append(floorHost);
        // The editor belongs in the page body. As a sibling of the shell it
        // rendered under an empty main column, which read as a broken page.
        main.append(floor);
        if (window.TesseraFloor) window.TesseraFloor.mount(floorHost, {api, element, status});
      }
      if (page === "all") {
        main.append(element("p", "settings-empty", "No matching settings. Clear your search or try another word."));
        filterSections();
      }
      for (const button of navigation.querySelectorAll("button")) {
        button.setAttribute("aria-pressed", String(button.dataset.section === page));
      }
    }

    function appendGrouped(container, node, field, seen) {
      const name = field.group || "";
      if (name && name !== seen.last) {
        container.append(element("h3", "settings-subhead", name));
        seen.last = name;
      }
      container.append(node);
    }

    function sectionBlock(section) {
      const block = element("section", "settings-block");
      block.dataset.section = section.id;
      block.append(element("h2", "", section.label));
      if (section.start_hint) block.append(element("p", "settings-start set-hint", section.start_hint));
      const fields = section.fields || [];
      const advanced = fields.filter((field) => field.tier === "advanced");
      const seen = {last: ""};
      for (const field of fields.filter((field) => field.tier !== "advanced")) {
        const node = field.type === "list-of-records" ? renderRecords(field) : renderScalar(field);
        appendGrouped(block, node, field, seen);
      }
      if (advanced.length) {
        const fold = element("details", "settings-advanced");
        fold.open = advancedOpen.get(section.id) === true;
        fold.addEventListener("toggle", () => { advancedOpen.set(section.id, fold.open); });
        fold.append(element("summary", "", "Advanced (" + advanced.length + ")"));
        const seenAdvanced = {last: ""};
        for (const field of advanced) {
          const node = field.type === "list-of-records" ? renderRecords(field) : renderScalar(field);
          appendGrouped(fold, node, field, seenAdvanced);
        }
        block.append(fold);
      }
      return block;
    }

    function paint() {
      const active = document.activeElement;
      const focusId = active?.id;
      const unsavedValue = active?.dataset?.dirty === "true" ? active.value : undefined;
      const selection = typeof active?.selectionStart === "number"
        ? [active.selectionStart, active.selectionEnd] : null;
      if (view.data?.provider_pages) paintShell();
      else paintClassic();
      // Saves can change dependent controls. Restore the user's place after rendering.
      const replacement = focusId && ["input", "select", "textarea", "button"].flatMap((tag) => Array.from(root.querySelectorAll(tag)));
      const target = replacement && Array.from(replacement).find((node) => node.id === focusId);
      if (target && !target.disabled) {
        if (unsavedValue !== undefined) {
          target.value = unsavedValue;
          target.dataset.dirty = "true";
        }
        target.focus({preventScroll: true});
        if (selection && typeof target.setSelectionRange === "function") {
          try { target.setSelectionRange(...selection); } catch (_) { /* Number/select controls have no text selection. */ }
        }
      }
    }

    function paintClassic() {
      root.replaceChildren();
      const navigation = element("nav", "settings-nav");
      navigation.setAttribute("aria-label", "Settings sections");
      const choices = [{id: "all", label: "All settings"}, ...(view.data?.sections || [])];
      for (const section of choices) {
        const button = element("button", "", section.label);
        button.type = "button";
        button.dataset.section = section.id;
        button.addEventListener("click", () => { view.section = section.id; filterSections(); });
        navigation.append(button);
      }
      const search = element("input", "settings-search");
      search.type = "search";
      search.id = "settings-search";
      search.placeholder = "Find a setting";
      search.setAttribute("aria-label", "Find a setting");
      search.value = view.search;
      search.addEventListener("input", () => {
        view.search = search.value;
        if (view.search.trim()) view.section = "all";
        filterSections();
      });
      root.append(navigation, search);
      for (const section of view.data?.sections || []) {
        const block = element("section", "settings-block");
        block.dataset.section = section.id;
        block.append(element("h2", "", section.label));
        if (section.start_hint) block.append(element("p", "settings-start set-hint", section.start_hint));
        if (section.id === "models" && view.data?.ladder) {
          const ladder = view.data.ladder;
          block.append(element("p", "set-hint", ladder.note));
          for (const provider of ladder.providers || []) {
            const account = (ladder.accounts || []).find(item => item.id === provider.account);
            const roles = (ladder.roles || []).filter(item => item.provider === provider.id);
            block.append(element("p", "set-fact",
              (account?.label || "No account") + " → " + provider.id + " → " +
              (provider.models.join(", ") || "CLI default (not reported)") + " → " +
              (roles.map(item => item.id).join(", ") || "No assigned role")));
          }
          for (const role of ladder.roles || []) {
            if (role.warning) block.append(element("p", "set-fact", role.id + ": " + role.warning));
            if (role.configuration_section) block.append(element("p", "set-hint",
              role.id + ": " + (role.model || "Service default") + " · Configure in " + role.configuration_section));
          }
        }
        // Keep setup and preferences visible, with tuning in a section disclosure.
        const fields = section.fields || [];
        const advanced = fields.filter((field) => field.tier === "advanced");
        if (section.id === "update") {
          const host = element("div", "update-panel");
          block.append(host);
          paintUpdate(host);
        }
        for (const field of fields.filter((field) => field.tier !== "advanced")) {
          block.append(field.type === "list-of-records" ? renderRecords(field) : renderScalar(field));
        }
        if (advanced.length) {
          const fold = element("details", "settings-advanced");
          fold.open = advancedOpen.get(section.id) === true;
          fold.addEventListener("toggle", () => { advancedOpen.set(section.id, fold.open); });
          fold.append(element("summary", "", "Advanced (" + advanced.length + ")"));
          for (const field of advanced) {
            fold.append(field.type === "list-of-records" ? renderRecords(field) : renderScalar(field));
          }
          block.append(fold);
        }
        root.append(block);
      }
      root.append(element("p", "settings-empty", "No matching settings. Choose another section or clear your search."));
      // What the page does not edit, said on the page. A person asking "where do I change
      // worker concurrency" should get an answer here instead of a shrug and a grep.
      const listed = fileOnlyBlock();
      if (listed) root.append(listed);
      const floor = element("section", "settings-block");
      floor.dataset.section = "floor";
      floor.append(element("h2", "", "Floor definition"));
      const floorHost = element("div", "floor-editor");
      floor.append(floorHost);
      root.append(floor);
      const floorButton = element("button", "", "Floor definition");
      floorButton.type = "button";
      floorButton.dataset.section = "floor";
      floorButton.addEventListener("click", () => { view.section = "floor"; filterSections(); });
      navigation.append(floorButton);
      if (window.TesseraFloor) window.TesseraFloor.mount(floorHost, {api, element, status});
      filterSections();
    }

    function fileOnlyBlock() {
      const fileOnly = view.data?.file_only;
      if (!fileOnly || !Array.isArray(fileOnly.rows) || !fileOnly.rows.length) return null;
      const block = element("section", "settings-block settings-file-only");
      block.dataset.section = "file-only";
      block.append(element("h2", "", "Set in config.toml"));
      block.append(element("p", "settings-start set-hint",
        fileOnly.reachable + " top-level settings are on this page. The " + fileOnly.count +
        " below have no control here; each says why."));
      block.append(element("p", "set-hint",
        "Edit config.toml in the Tessera home on the computer running the core, not on a connected phone. " +
        "Stop the core, back up the file, edit the existing table (do not duplicate it), then start Tessera again. " +
        "Desktop lifecycle options belong in desktop.toml on the computer running the desktop app. " +
        "For phone access, first make that core reachable through your private network; localhost on a phone refers to the phone."));
      const fold = element("details", "settings-advanced");
      // Open by default. The answer to "where do I change worker concurrency"
      // is only useful when the list is on screen, not behind a summary line.
      fold.open = advancedOpen.get("file-only") !== false;
      fold.addEventListener("toggle", () => { advancedOpen.set("file-only", fold.open); });
      fold.append(element("summary", "", "Settings with no control on this page (" + fileOnly.count + ")"));
      const table = element("div", "settings-file-only-rows");
      const live = element("div", "file-only-live-tables");
      for (const row of fileOnly.rows) {
        const line = element("div", "set-row");
        line.append(element("code", "set-label", row.name));
        line.append(element("span", "set-hint", row.reason));
        table.append(line);
        if (Array.isArray(row.values) && row.values.length) live.append(behaviorValues(row));
      }
      for (const row of fileOnly.uncovered || []) {
        if (row && Array.isArray(row.values) && row.values.length) live.append(behaviorValues(row));
      }
      if (live.children.length) {
        block.append(element("h3", "", "Live values"));
        block.append(element("p", "set-hint",
          "These are the values in this core's loaded config. They are still edited in config.toml. A table that already has a control lists only the keys that control does not cover."));
        block.append(live);
      }
      fold.append(table);
      block.append(fold);
      block.append(backupList());
      block.append(changeList());
      return block;
    }

    function shownValue(item) {
      if (item.secret) return item.set ? "Set (not shown)" : "Empty";
      if (item.value !== null && typeof item.value === "object") return JSON.stringify(item.value, null, 2);
      return String(item.value);
    }

    function behaviorValues(row) {
      const fold = element("details", "file-only-values");
      fold.open = true;
      fold.append(element("summary", "", row.name + " live values"));
      for (const item of row.values) {
        if (!item || typeof item.key !== "string") continue;
        const line = element("div", "file-only-value");
        line.append(element("code", "set-label", row.name + "." + item.key));
        line.append(element("pre", "file-only-live", shownValue(item)));
        fold.append(line);
      }
      return fold;
    }

    function backupList() {
      const backups = Array.isArray(view.data?.config_backups) ? view.data.config_backups : [];
      const block = element("div", "config-backups");
      block.append(element("h3", "", "Config backups"));
      if (typeof view.data?.config_backups_error === "string" && view.data.config_backups_error) {
        block.append(element("p", "set-fact", "Config backups: " + view.data.config_backups_error));
      }
      block.append(element("p", "set-hint",
        backups.length
          ? "Each Settings save keeps the previous config.toml. Restore replaces the current file and keeps a new backup of it."
          : "No config backups yet. A Settings save writes one beside config.toml."));
      for (const backup of backups) {
        if (!backup || typeof backup.name !== "string") continue;
        const line = element("div", "set-row config-backup");
        line.append(element("code", "set-label", backup.name));
        const when = typeof backup.modified === "string" ? backup.modified : "";
        line.append(element("span", "set-hint", when));
        const button = element("button", "", view.restoreName === backup.name ? "Replace config.toml" : "Restore");
        button.type = "button";
        button.addEventListener("click", () => restoreBackup(backup.name));
        line.append(button);
        block.append(line);
      }
      return block;
    }

    async function restoreBackup(name) {
      if (view.restoreName !== name) {
        view.restoreName = name;
        status.textContent = "Restore replaces config.toml with " + name + ". Choose Restore again to confirm.";
        paint();
        return;
      }
      view.restoreName = "";
      status.textContent = "Restoring…";
      try {
        applyResult(await api("/api/config/restore", "POST", {name}));
      } catch (error) {
        status.textContent = error.message || "Restore failed";
      }
    }

    function changeList() {
      const changes = Array.isArray(view.data?.config_changes) ? view.data.config_changes : [];
      const block = element("div", "config-changes");
      block.append(element("h3", "", "Config changes"));
      block.append(element("p", "set-hint",
        "Saves through ConfigEditor are listed with their backup. A write that bypasses ConfigEditor is listed too, without copying secret values."));
      if (typeof view.data?.config_journal_error === "string" && view.data.config_journal_error) {
        block.append(element("p", "set-fact", "Config change log: " + view.data.config_journal_error));
      }
      const shown = view.data?.config_changes_shown;
      const total = view.data?.config_changes_total;
      if (typeof shown === "number" && typeof total === "number" && total > 0) {
        block.append(element("p", "set-hint", "Showing " + shown + " of " + total + "."));
      }
      if (!changes.length) {
        block.append(element("p", "set-hint", "No config changes recorded yet."));
        return block;
      }
      for (const change of changes) {
        if (!change || typeof change !== "object") continue;
        const line = element("div", "config-change");
        const source = typeof change.source === "string" ? change.source : "unreported";
        const summary = typeof change.summary === "string" ? change.summary : "";
        line.append(element("p", "set-fact", (change.at || "") + " · " + source + (summary ? " · " + summary : "")));
        const paths = Array.isArray(change.changes) ? change.changes : [];
        for (const item of paths) {
          if (!item || typeof item.path !== "string") continue;
          if (item.value === null || item.value === "removed") {
            line.append(element("pre", "file-only-live", item.path + " removed"));
            continue;
          }
          const value = item.value === undefined ? "" : (item.value !== null && typeof item.value === "object"
            ? JSON.stringify(item.value) : String(item.value));
          line.append(element("pre", "file-only-live", item.path + " = " + value));
        }
        if (typeof change.backup === "string" && change.backup) {
          line.append(element("p", "set-hint", "Backup: " + change.backup));
        }
        block.append(line);
      }
      return block;
    }

    async function load() {
      const data = await api("/api/settings");
      view.data = data;
      // A sign-in or a refresh can change what an account serves.
      view.accounts = null;
      view.form = null;
      view.confirm = "";
      if (!sectionChosen) {
        view.section = data.provider_pages ? "provider-accounts" : "all";
        sectionChosen = true;
      }
      restartButton.disabled = data.supervised !== true;
      restartButton.title = data.supervised === true ? "" : "Core is not supervised";
      paint();
      if (onAppearance) onAppearance(readAppearance(data));
    }

    async function restart() {
      restartButton.disabled = true;
      status.textContent = "Restarting…";
      // The old core drains while still answering /api/status, so a successful poll is
      // not proof the restart happened. Remember which process asked and only accept
      // an answer from a different one.
      // Read the generation from the same route the poll below uses, so both answers
      // come from one source. If it cannot be read, fall back to the old behaviour.
      let before = null;
      try { before = (await api("/api/status"))?.generation ?? null; } catch (_) {}
      try { await api("/api/core/restart", "POST", {}); }
      catch (error) {
        status.textContent = error.message || "Restart refused";
        restartButton.disabled = view.data?.supervised !== true;
        return;
      }
      for (let attempt = 0; attempt < 60; attempt += 1) {
        await new Promise((resolve) => setTimeout(resolve, 1000));
        try {
          const live = await api("/api/status");
          // A core that reports no generation cannot be told apart from the old one.
          if (before !== null && live?.generation === before) continue;
          status.textContent = "Core restarted";
          await load();
          if (onRestarted) onRestarted();
          return;
        } catch (_) { /* The supervisor is still bringing the core back. */ }
      }
      status.textContent = before === null
        ? "Core did not come back"
        : "The old core never exited; it is still draining and answering";
      restartButton.disabled = false;
    }

    function show(result) {
      if (!result?.settings || view.form) return;
      if (result.settings.provider_pages) view.data = result.settings;
      else view.data = Object.assign({}, view.data, result.settings);
      paint();
    }

    function noteApproval(approval) {
      const yolo = Boolean(approval && approval.yolo);
      for (const section of view.data?.sections || []) {
        if (section.id !== "permissions") continue;
        for (const field of section.fields || []) {
          if (field.id === "permissions.mode") field.value = yolo ? "yolo" : "safe";
        }
      }
      const mode = document.getElementById("setting-permissions_mode");
      if (mode && document.activeElement !== mode) mode.value = yolo ? "yolo" : "safe";
    }

    restartButton.addEventListener("click", () => restart());
    // An open record form with typed input is unsaved work. The page calls this
    // before reloading settings on navigation, so answering honestly is what keeps
    // a half-filled form alive across a page change instead of discarding it.
    function hasEdits() {
      return [view.form, ...Array.from(drafts.values(), (saved) => saved.form)].some((form) => {
        if (!form) return false;
        const draft = form.draft || {};
        const baseline = form.baseline || {};
        return Object.keys(draft).some((key) => draft[key] !== baseline[key]);
      });
    }

    return {load, show, noteApproval, hasEdits};
  }

  window.TesseraSettings = {mount};
})();
