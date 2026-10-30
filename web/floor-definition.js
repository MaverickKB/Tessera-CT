/* Floor definition editor. The rules and the wake template are data the person owns. */
(() => {
  "use strict";

  let draft = null;

  function mount(host, {api, element, status}) {
    const el = element || ((tag, className, text) => {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text != null) node.textContent = text;
      return node;
    });

    function note(text) {
      if (status) status.textContent = text;
    }

    // What each field is, in one sentence, with an example where the value is a
    // shape a person cannot guess. A page of bare text boxes is not an editor.
    const FIELD_HELP = {
      "World view": "The first thing a wake says, in your words. Example: Here is what changed since the last wake.",
      "World view limit": "Characters kept from the world view. 0 keeps all of it.",
      "Wake score": "How much pressure a wake needs before it is delivered. Higher means fewer, more serious wakes.",
      "Hold seconds": "How long a reading stays current before it is treated as stale, or the minimum gap between two wakes from a rule.",
      "Hold label": "The word a stale reading is shown with. Example: stale.",
      "Disk free percent": "Wake when free disk falls below this percentage. Example: 10.",
      "Missing priority": "What a missing reading counts as when it ranks against others. Higher ranks first.",
      "World view introduction": "The lead-in line above the world view. Clear it to drop the line.",
      "Pressure introduction": "The lead-in line above the pressure list. Clear it to drop the line.",
      "Coordination paragraph": "The sentence a wake uses when another session is already working. Clear it to omit.",
      "Quiet-time paragraph": "The sentence a wake uses when nothing crossed the bar. Clear it to omit.",
      "Wake footer": "The last lines of every wake, for standing instructions. Clear it to omit.",
      "Enabled": "Off keeps the rule in the file but never runs it.",
      "Can wake": "Off lets the rule contribute pressure without waking you.",
      "Pressure": "On counts this rule's findings as pressure rather than a note.",
      "Safe-only guard": "On applies the rule only in Safe mode.",
      "Name": "The rule's own name, shown in this editor. It is not the wake text.",
      "Signal": "Which reading feeds the rule. Example: host.",
      "Emit": "One line per match, all matches joined, a single fallback line, or offers.",
      "Sort": "Order matches by input order or by priority.",
      "Score": "The pressure this rule adds when it fires. Example: 2.",
      "Condition": "A condition over the signal's fields. Example: disk_free_percent < 10.",
      "Template": "The wake line, with {placeholders} filled from the signal. Example: {name} disk at {disk}%.",
      "Name field": "The signal field used as the subject's name. Example: ordinal.",
      "Names shown": "How many names one line lists before it summarizes. Example: 3.",
      "Title priority pattern": "A pattern that raises a match's priority when the title matches it.",
      "Owner": "Who the wake is addressed to. Empty uses the floor session.",
      "Note": "A note for the next reader. The floor does not read it.",
    };

    function field(label, node) {
      const row = el("label", "floor-field");
      row.append(el("span", "set-label", label), node);
      const help = FIELD_HELP[label];
      if (help) row.append(el("span", "set-hint", help));
      return row;
    }

    function group(className, ...nodes) {
      const row = el("div", className);
      row.append(...nodes);
      return row;
    }

    function textInput(value, label) {
      const input = el("input");
      input.type = "text";
      input.value = value == null ? "" : String(value);
      input.setAttribute("aria-label", label);
      return input;
    }

    function area(value, label, rows) {
      const input = el("textarea");
      input.rows = rows || 3;
      input.value = value == null ? "" : String(value);
      input.setAttribute("aria-label", label);
      return input;
    }

    function numberInput(value, label) {
      const input = el("input");
      input.type = "number";
      input.value = value == null ? "" : String(value);
      input.setAttribute("aria-label", label);
      return input;
    }

    function readCompose(compose, nodes) {
      compose.world_view = nodes.world_view.value;
      compose.world_view_max_chars = Number(nodes.max_chars.value);
      compose.wake_score = Number(nodes.wake_score.value);
      compose.stale_seconds = Number(nodes.stale_seconds.value);
      compose.stale_label = nodes.stale_label.value;
      compose.disk_free_percent_below = Number(nodes.disk.value);
      compose.missing_priority = Number(nodes.missing.value);
      compose.footer = nodes.footer.value;
      compose.pressure_intro = nodes.pressure_intro.value;
      compose.coordination_paragraph = nodes.coordination.value;
      compose.quiet_paragraph = nodes.quiet.value;
      compose.world_view_intro = nodes.world_intro.value;
    }

    function choice(value, label, options) {
      const input = el("select");
      input.setAttribute("aria-label", label);
      for (const option of options) {
        const node = el("option", "", option);
        node.value = option;
        if (option === value) node.selected = true;
        input.append(node);
      }
      input.value = options.indexOf(value) === -1 ? (options[0] || "") : value;
      return input;
    }

    function readRule(rule, nodes) {
      rule.name = nodes.name.value;
      rule.enabled = nodes.enabled.checked;
      rule.wake = nodes.wake.checked;
      rule.guard = nodes.guard.checked;
      rule.pressure = nodes.pressure.checked;
      rule.signal = nodes.signal.value;
      rule.emit = nodes.emit.value;
      rule.sort = nodes.sort.value;
      rule.score = Number(nodes.score.value);
      rule.when = nodes.when.value;
      rule.template = nodes.template.value;
      rule.owner = nodes.owner.value;
      rule.note = nodes.note.value;
      rule.name_field = nodes.name_field.value;
      rule.name_limit = Number(nodes.name_limit.value);
      rule.title_priority_pattern = nodes.title_priority_pattern.value;
      const hold = nodes.hold.value.trim();
      rule.hold_seconds = hold === "" ? null : Number(hold);
      const missing = nodes.missing_priority.value.trim();
      rule.missing_priority = missing === "" ? null : Number(missing);
    }

    function appendReadingErrors(view) {
      if (view.approval_mode === "unknown") {
        host.append(el("p", "pressure-errors", "Approval status could not be read."));
      }
      const errors = Array.isArray(view.errors) ? view.errors : [];
      if (!errors.length) return;
      const list = el("ul", "pressure-errors");
      list.id = "floor-reading-errors";
      for (const item of errors) {
        if (!item || typeof item !== "object") continue;
        const source = typeof item.source === "string" ? item.source : "reading";
        const kind = typeof item.error_type === "string" ? item.error_type : "Error";
        const message = typeof item.message === "string" ? item.message : "";
        list.append(el("li", "", source + " failed: " + kind + (message ? ": " + message : "")));
      }
      if (list.childElementCount || (list.children && list.children.length)) host.append(list);
    }

    function render(view) {
      draft = view;
      host.replaceChildren();
      const definition = view.definition;
      if (!definition || view.source === "invalid") {
        host.append(el("p", "pressure-errors", view.error || "The floor definition file is invalid and was not replaced with the shipped rules."));
        appendReadingErrors(view);
        if (view.raw) {
          const raw = el("pre", "floor-preview");
          raw.textContent = view.raw;
          host.append(el("h3", "", "File on disk"), raw);
        }
        const last = view.last_wake || {};
        const sent = last.sent || last.posted || "";
        if (sent) {
          const lastBox = el("pre", "floor-last-wake");
          lastBox.textContent = sent;
          host.append(el("h3", "", "Last wake sent"), lastBox);
        }
        const transfer = area(view.raw || "", "Replacement definition", 8);
        const importButton = el("button", "", "Import");
        const reload = el("button", "", "Reload from disk");
        importButton.type = "button";
        reload.type = "button";
        importButton.addEventListener("click", () => importText(transfer.value));
        reload.addEventListener("click", () => { draft = null; load(); });
        host.append(el("h3", "", "Replace the file"), transfer, group("floor-save", importButton, reload));
        return;
      }
      if (view.error) host.append(el("p", "pressure-errors", view.error));
      appendReadingErrors(view);
      const form = el("div", "floor-form");
      const sources = definition.sources && typeof definition.sources === "object" ? definition.sources : {};
      const sourceNodes = {};
      const sourceBox = el("div", "floor-flags floor-sources");
      for (const [name, signal] of Object.entries(view.signals || {})) {
        const box = el("input");
        box.type = "checkbox";
        box.id = "floor-source-" + name;
        box.checked = sources[name] !== false;
        box.setAttribute("data-source", name);
        box.setAttribute("aria-label", (signal.label || name) + " pressure");
        sourceNodes[name] = box;
        sourceBox.append(field(signal.label || name, box));
      }
      form.append(
        el("h3", "", "Pressure sources"),
        el("p", "set-hint", "Each source has its own switch. Off stops that pressure, including offers and holds, in Safe and in Unleashed."),
        sourceBox,
      );
      const compose = definition.compose;
      const nodes = {
        world_view: area(compose.world_view, "World view", 4),
        max_chars: numberInput(compose.world_view_max_chars, "World view character limit"),
        wake_score: numberInput(compose.wake_score, "Wake score"),
        stale_seconds: numberInput(compose.stale_seconds, "Hold seconds"),
        stale_label: textInput(compose.stale_label, "Hold label"),
        disk: numberInput(compose.disk_free_percent_below, "Disk free percent"),
        missing: numberInput(compose.missing_priority, "Missing priority"),
        world_intro: textInput(compose.world_view_intro, "World view introduction"),
        pressure_intro: area(compose.pressure_intro, "Pressure introduction", 2),
        coordination: area(compose.coordination_paragraph, "Coordination paragraph", 4),
        quiet: area(compose.quiet_paragraph, "Quiet-time paragraph", 3),
        footer: area(compose.footer, "Wake footer", 6),
      };
      nodes.max_chars.min = "0";
      form.append(
        el("p", "set-hint", "These sentences are what a floor wake says. Clear one to turn that part off. A limit of 0 does not clip the world view."),
        field("World view", nodes.world_view),
        field("World view limit", nodes.max_chars),
        field("Wake score", nodes.wake_score),
        field("Hold seconds", nodes.stale_seconds),
        field("Hold label", nodes.stale_label),
        field("Disk free percent", nodes.disk),
        field("Missing priority", nodes.missing),
        field("World view introduction", nodes.world_intro),
        field("Pressure introduction", nodes.pressure_intro),
        field("Coordination paragraph", nodes.coordination),
        field("Quiet-time paragraph", nodes.quiet),
        field("Wake footer", nodes.footer),
      );
      const rules = el("div", "floor-rules");
      const ruleNodes = [];
      definition.rules.forEach((rule, index) => {
        const card = el("fieldset", "floor-rule");
        const legend = el("legend", "", rule.id);
        const controls = {
          name: textInput(rule.name, "Rule name"),
          enabled: el("input"),
          wake: el("input"),
          guard: el("input"),
          pressure: el("input"),
          signal: el("select"),
          emit: choice(rule.emit || "each", "Emit", ["each", "join", "fallback", "offers"]),
          sort: choice(rule.sort || "input", "Sort", ["input", "priority"]),
          score: numberInput(rule.score, "Score"),
          when: area(rule.when, "Condition", 2),
          template: area(rule.template, "Template", 3),
          owner: textInput(rule.owner, "Owner"),
          note: textInput(rule.note, "Note"),
          name_field: textInput(rule.name_field || "ordinal", "Name field"),
          name_limit: numberInput(rule.name_limit, "Name limit"),
          hold: numberInput(rule.hold_seconds, "Rule hold seconds"),
          missing_priority: numberInput(rule.missing_priority, "Missing priority"),
          title_priority_pattern: textInput(rule.title_priority_pattern || "", "Title priority pattern"),
        };
        controls.enabled.type = "checkbox";
        controls.enabled.checked = rule.enabled === true;
        controls.wake.type = "checkbox";
        controls.wake.checked = rule.wake === true;
        controls.guard.type = "checkbox";
        controls.guard.checked = rule.guard === true;
        controls.pressure.type = "checkbox";
        controls.pressure.checked = rule.pressure === true;
        const signals = Object.keys(view.signals || {});
        for (const name of signals) {
          const option = el("option", "", name);
          option.value = name;
          if (name === rule.signal) option.selected = true;
          controls.signal.append(option);
        }
        const flags = el("div", "floor-flags");
        flags.append(
          field("Enabled", controls.enabled),
          field("Can wake", controls.wake),
          field("Pressure", controls.pressure),
          field("Safe-only guard", controls.guard),
        );
        const actions = el("div", "floor-rule-actions");
        const up = el("button", "", "Move up");
        const down = el("button", "", "Move down");
        const copy = el("button", "", "Duplicate");
        const remove = el("button", "", "Delete");
        const dry = el("button", "", "Dry run");
        for (const button of [up, down, copy, remove, dry]) button.type = "button";
        up.disabled = index === 0;
        down.disabled = index === definition.rules.length - 1;
        up.addEventListener("click", () => move(index, -1));
        down.addEventListener("click", () => move(index, 1));
        copy.addEventListener("click", () => duplicate(index));
        remove.addEventListener("click", () => {
          // Preserve edits before removing a rule changes the control-to-rule indexes.
          capture();
          definition.rules.splice(index, 1);
          render(view);
        });
        dry.addEventListener("click", () => dryRun(index));
        actions.append(up, down, copy, dry, remove);
        const fired = rule.last_fired ? " Last fired " + rule.last_fired + "." : "";
        card.append(
          legend,
          field("Name", controls.name),
          flags,
          field("Signal", controls.signal),
          field("Emit", controls.emit),
          field("Sort", controls.sort),
          field("Score", controls.score),
          field("Condition", controls.when),
          field("Template", controls.template),
          field("Name field", controls.name_field),
          field("Names shown", controls.name_limit),
          field("Missing priority", controls.missing_priority),
          field("Title priority pattern", controls.title_priority_pattern),
          field("Hold seconds", controls.hold),
          field("Owner", controls.owner),
          field("Note", controls.note),
          el("p", "set-hint", (rule.note || "") + fired),
          actions,
        );
        rules.append(card);
        ruleNodes.push(controls);
      });
      const add = el("button", "", "Add rule");
      add.type = "button";
      add.addEventListener("click", () => {
        capture();
        const id = "rule-" + Date.now().toString(36);
        definition.rules.push({
          id, name: "New rule", enabled: true, signal: "host", when: "true", template: "",
          score: 0, wake: false, pressure: false, emit: "each", sort: "input",
          name_limit: 5, name_field: "label", title_priority_pattern: "",
          owner: "", note: "", hold_seconds: null, missing_priority: null, guard: false, last_fired: null,
        });
        render(view);
      });
      const save = el("button", "primary", "Save floor definition");
      save.type = "button";
      save.addEventListener("click", () => saveDefinition());
      const previewButton = el("button", "", "Preview");
      previewButton.type = "button";
      previewButton.addEventListener("click", () => preview());
      const previewBox = el("pre", "floor-preview");
      previewBox.id = "floor-preview";
      previewBox.textContent = view.preview && view.preview.world_view ? view.preview.world_view : "Preview shows the wake text for the current state.";
      const last = view.last_wake || {};
      const lastBox = el("pre", "floor-last-wake");
      lastBox.id = "floor-last-wake";
      const sent = last.sent || last.posted || "";
      lastBox.textContent = sent
        ? ((Array.isArray(last.rules) && last.rules.length ? "Rules: " + last.rules.join(", ") + "\n\n" : "") + sent)
        : "No floor wake has been sent yet.";
      const audit = el("ol", "floor-audit");
      for (const entry of view.audit || []) {
        const changes = (entry.changes || []).map((change) => change.rule + " " + change.change).join(", ") || "no rule diff";
        audit.append(el("li", "", (entry.at || "") + " · " + (entry.actor || "operator") + " · " + changes));
      }
      const transfer = area("", "Import or export text", 6);
      transfer.id = "floor-transfer";
      const exportJson = el("button", "", "Export JSON");
      const exportToml = el("button", "", "Export TOML");
      const importButton = el("button", "", "Import");
      for (const button of [exportJson, exportToml, importButton]) button.type = "button";
      exportJson.addEventListener("click", () => download("json"));
      exportToml.addEventListener("click", () => download("toml"));
      importButton.addEventListener("click", () => importText(transfer.value));
      const reference = el("details", "floor-signals");
      reference.append(el("summary", "", "Signal fields"));
      const list = el("dl", "floor-signal-list");
      for (const [name, signal] of Object.entries(view.signals || {})) {
        list.append(el("dt", "", name), el("dd", "", signal.summary || ""));
        const fields = signal.fields || {};
        for (const [key, description] of Object.entries(fields)) {
          list.append(el("dt", "", name + "." + key), el("dd", "", description));
        }
      }
      reference.append(list);
      host.append(form, el("h3", "", "Rules"), rules, add, group("floor-save", save, previewButton),
        el("h3", "", "Preview"), previewBox, el("h3", "", "Last wake sent"), lastBox,
        el("h3", "", "Changes"), audit, el("h3", "", "Import and export"), transfer,
        group("floor-save", exportJson, exportToml, importButton), reference);

      function capture() {
        readCompose(compose, nodes);
        definition.sources = Object.assign({}, sources);
        for (const [name, box] of Object.entries(sourceNodes)) definition.sources[name] = box.checked === true;
        definition.rules.forEach((rule, index) => readRule(rule, ruleNodes[index]));
      }

      function move(index, delta) {
        capture();
        const next = index + delta;
        const [rule] = definition.rules.splice(index, 1);
        definition.rules.splice(next, 0, rule);
        render(view);
      }

      function duplicate(index) {
        capture();
        const copy = Object.assign({}, definition.rules[index]);
        copy.id = copy.id + "-copy";
        copy.name = copy.name + " copy";
        definition.rules.splice(index + 1, 0, copy);
        render(view);
      }

      async function saveDefinition() {
        capture();
        note("Saving floor definition…");
        try {
          const result = await api("/api/autonomy/floor-definition", "PUT", {definition, actor: "operator"});
          note("Floor definition saved");
          render(result);
        } catch (error) {
          note(error.message || "Floor definition was not saved");
        }
      }

      async function preview() {
        capture();
        note("Previewing…");
        try {
          const result = await api("/api/autonomy/floor-definition/preview", "POST", {definition});
          previewBox.textContent = result.world_view || result.error || "Nothing to show.";
          note(result.kind ? "Preview is a " + result.kind + " wake" : "Preview ready");
        } catch (error) {
          note(error.message || "Preview failed");
        }
      }

      async function dryRun(index) {
        capture();
        const rule = definition.rules[index];
        note("Dry run " + rule.id + "…");
        try {
          const result = await api("/api/autonomy/floor-definition/dry-run", "POST", {rule_id: rule.id, definition});
          const lines = (result.hits || []).map((hit) => hit.text).join("\n");
          previewBox.textContent = lines || (rule.name + " matches nothing right now.");
          note("Dry run " + rule.id);
        } catch (error) {
          note(error.message || "Dry run failed");
        }
      }

      async function download(format) {
        try {
          const result = await api("/api/autonomy/floor-definition/export?format=" + format);
          transfer.value = result.text || "";
          note("Exported " + format);
        } catch (error) {
          note(error.message || "Export failed");
        }
      }

      async function importText(text) {
        note("Importing…");
        try {
          const result = await api("/api/autonomy/floor-definition/import", "POST", {text, actor: "operator"});
          note("Imported");
          render(result);
        } catch (error) {
          const message = error.message || "Import refused";
          note(message);
          const previous = host.querySelector("#floor-import-error");
          if (previous) previous.remove();
          const line = el("p", "pressure-errors", message);
          line.id = "floor-import-error";
          host.append(line);
        }
      }
    }

    async function load() {
      host.replaceChildren(el("p", "set-hint", "Loading floor definition…"));
      try {
        render(await api("/api/autonomy/floor-definition"));
      } catch (error) {
        host.replaceChildren(el("p", "pressure-errors", error.message || "Floor definition unavailable"));
      }
    }

    if (draft) render(draft);
    else load();
  }

  window.TesseraFloor = {mount};
})();
