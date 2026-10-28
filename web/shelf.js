/* Skills and tools. Every row comes from /api/skills or /api/tools.

   Switches and the directory form write those routes and paint the reply.
   A failed save leaves the previous row in place and shows the error.
*/
(() => {
  "use strict";

  const OUTCOME = {
    runs: "Runs without asking",
    asks: "Asks first",
    refused: "Refused under this policy",
    disabled: "Off. A call is refused.",
  };
  const WHEN_ON = {runs: "runs without asking", asks: "asks first", refused: "is refused under this policy"};
  const SKILL_SOURCE = {user: "User shelf", project: "Project", added: "Added directory"};
  const TOOL_ORIGIN = {builtin: "Built-in", mcp: "MCP", session: "This session"};

  function mount({api, root}) {
    const view = {skills: null, tools: null, skillError: "", toolError: "", search: "", directory: "", onlyRisky: false, notice: "", busy: false};

    function node(tag, className, text) {
      const created = document.createElement(tag);
      if (className) created.className = className;
      if (text !== undefined) created.textContent = String(text);
      return created;
    }

    function matches(text) {
      const query = view.search.trim().toLowerCase();
      return !query || String(text || "").toLowerCase().includes(query);
    }

    function checkbox(label, checked, onChange) {
      const box = node("input", "shelf-switch");
      box.type = "checkbox";
      box.checked = checked;
      box.setAttribute("aria-label", label);
      box.disabled = view.busy;
      box.addEventListener("change", () => onChange(box.checked));
      return box;
    }

    function applySkills(data) {
      view.skills = data;
      view.notice = data && data.effect ? data.effect : view.notice;
      view.busy = false;
      paint();
    }

    function applyTools(data) {
      view.tools = data;
      view.notice = data && data.effect ? data.effect : view.notice;
      view.busy = false;
      paint();
    }

    function fail(error) {
      view.notice = error && error.message ? error.message : "Save failed";
      view.busy = false;
      paint();
    }

    function save(path, body, apply) {
      if (view.busy) return;
      view.busy = true;
      view.notice = "Saving…";
      paint();
      return api(path, "POST", body).then(apply).catch(fail);
    }

    function renderDirectories(data) {
      const wrap = node("div", "shelf-dirs");
      wrap.append(node("h3", "", "Skill directories"));
      const project = (data.directories || []).some((dir) => dir.source === "project");
      if (project && data.workspace) {
        wrap.append(node("p", "set-hint", "Project directories are read from " + data.workspace + "."));
      }
      for (const dir of data.directories || []) {
        const line = node("div", "shelf-dir");
        line.append(node("code", "set-label", dir.path));
        line.append(node("span", "set-fact", SKILL_SOURCE[dir.source] || dir.source));
        const status = node("span", dir.error ? "shelf-error" : "set-hint", dir.error || dir.status || "");
        line.append(status);
        if (dir.removable) {
          const button = node("button", "", "Remove");
          button.type = "button";
          button.disabled = view.busy;
          button.addEventListener("click", () => save("/api/skills", {remove_dir: dir.path}, applySkills));
          line.append(button);
        }
        wrap.append(line);
      }
      const form = node("form", "shelf-add");
      const input = node("input", "");
      input.type = "text";
      input.placeholder = "Absolute path to a skills directory";
      input.setAttribute("aria-label", "Skills directory to add");
      input.value = view.directory;
      input.disabled = view.busy;
      input.addEventListener("input", () => { view.directory = input.value; });
      const add = node("button", "", "Add directory");
      add.type = "submit";
      add.disabled = view.busy;
      form.append(input, add);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        view.directory = input.value;
        const path = input.value.trim();
        if (!path) {
          view.notice = "Enter the directory path.";
          paint();
          return;
        }
        save("/api/skills", {add_dir: path}, (data) => {
          view.directory = "";
          if (root.querySelector) {
            const current = root.querySelector("[aria-label='Skills directory to add']");
            if (current) current.value = "";
          }
          applySkills(data);
        });
      });
      wrap.append(form);
      return wrap;
    }

    function renderSkills() {
      const block = node("section", "settings-block");
      block.dataset.section = "skills";
      const data = view.skills;
      block.append(node("h2", "", "Skills"));
      if (!data) {
        block.append(node("p", "set-hint",
          "Skills could not be read from this core" + (view.skillError ? ": " + view.skillError : ".")));
        return block;
      }
      if (typeof data.enabled === "boolean") {
        const row = node("div", "set-row");
        const label = node("label", "set-label");
        label.append(checkbox(data.enabled ? "Turn the skills shelf off" : "Turn the skills shelf on", data.enabled,
          (on) => save("/api/skills", {shelf_enabled: on}, applySkills)));
        label.append(node("span", "", "Shelf on"));
        row.append(label);
        row.append(node("span", "set-hint",
          "An off switch applies in every mode, including Unleashed. With the shelf off, search and load say so. A session that started with the shelf off has no skill tools until it is started again."));
        block.append(row);
      }
      if (data.note) block.append(node("p", "set-hint", data.note));
      if (Array.isArray(data.directories)) block.append(renderDirectories(data));
      if (!data.enabled && !(data.skills || []).length) return block;
      if (data.enabled) {
        block.append(node("p", "settings-start set-hint",
          data.count + " skills on the shelf. A skill is loaded by name, never injected into "
          + "the prompt, so editing one does not change what the model already knows."));
      }
      const broken = (data.skills || []).filter((skill) => skill.error);
      if (broken.length) {
        const warning = node("div", "shelf-broken");
        warning.append(node("p", "set-hint",
          broken.length + " skill" + (broken.length === 1 ? "" : "s") + " cannot be loaded:"));
        for (const skill of broken) {
          const line = node("div", "set-row");
          line.append(node("code", "set-label", skill.name));
          line.append(node("span", "shelf-error", skill.error));
          warning.append(line);
        }
        block.append(warning);
      }
      const list = node("div", "shelf-list");
      let shown = 0;
      for (const skill of data.skills || []) {
        if (skill.error) continue;
        if (!matches(skill.name) && !matches(skill.description)) continue;
        const row = node("div", "set-row");
        const label = node("label", "set-label");
        if (typeof skill.enabled === "boolean") {
          label.append(checkbox((skill.enabled ? "Disable skill " : "Enable skill ") + skill.name, skill.enabled,
            (on) => save("/api/skills", {name: skill.name, enabled: on}, applySkills)));
        }
        const name = node("code", "", skill.name);
        name.title = skill.dir || "";
        label.append(name);
        row.append(label);
        row.append(node("span", "set-hint", skill.description));
        const facts = [SKILL_SOURCE[skill.source] || skill.source];
        if (skill.offered === false) facts.push("Not offered");
        row.append(node("span", "set-fact", facts.filter(Boolean).join(" · ")));
        list.append(row);
        shown += 1;
      }
      if (!shown && data.enabled) {
        list.append(node("p", "set-hint", view.search.trim()
          ? "No skill matches " + JSON.stringify(view.search) + "."
          : "No skills in these directories."));
      } else if (data.enabled && shown < data.count - broken.length) {
        list.append(node("p", "set-hint",
          (data.count - broken.length - shown) + " more not shown; narrow the search."));
      }
      block.append(list);
      return block;
    }

    function renderTools() {
      const block = node("section", "settings-block");
      block.dataset.section = "tools";
      const data = view.tools;
      block.append(node("h2", "", "Tools"));
      if (!data) {
        block.append(node("p", "set-hint",
          "Tools could not be read from this core" + (view.toolError ? ": " + view.toolError : ".")));
        return block;
      }
      block.append(node("p", "settings-start set-hint",
        data.count + " tools, " + data.risky + " of them risky. Under the current policy ("
        + data.policy + ") an enabled tool either runs, asks, or is refused. "
        + "An off switch applies in every mode, including Unleashed."));
      block.append(node("p", "set-hint",
        "A skill file that failed to parse, and the last failure while listing an MCP server, are shown on this page. What a tool returned on a call stays in the session."));
      if (data.source) block.append(node("p", "set-hint", "Read from: " + data.source + "."));
      if (data.callable === false) {
        block.append(node("p", "set-hint", data.gap || "No session is loaded, so these tools are not callable yet."));
      } else if (data.gap) {
        block.append(node("p", "set-hint", data.gap));
      }
      for (const item of data.mcp_errors || []) {
        block.append(node("p", "shelf-error", (item.server || "MCP") + ": " + (item.error || "The server did not list tools.")));
      }
      const list = node("div", "shelf-list");
      let shown = 0;
      for (const tool of data.tools || []) {
        if (view.onlyRisky && !tool.risky) continue;
        if (!matches(tool.name) && !matches(tool.description)) continue;
        const row = node("div", "set-row");
        if (tool.risky) row.classList.add("set-row-risky");
        const label = node("label", "set-label");
        if (typeof tool.enabled === "boolean") {
          label.append(checkbox((tool.enabled ? "Disable tool " : "Enable tool ") + tool.name, tool.enabled,
            (on) => save("/api/tools", {name: tool.name, enabled: on}, applyTools)));
        }
        label.append(node("code", "", tool.name));
        row.append(label);
        row.append(node("span", "set-hint", tool.description));
        let outcome = OUTCOME[tool.outcome] || tool.outcome;
        if (tool.outcome === "disabled" && WHEN_ON[tool.policy_outcome]) {
          outcome += " When on, it " + WHEN_ON[tool.policy_outcome] + ".";
        }
        const origin = TOOL_ORIGIN[tool.origin] || "";
        row.append(node("span", "set-fact set-fact-wide shelf-outcome shelf-" + tool.outcome,
          origin ? origin + " · " + outcome : outcome));
        list.append(row);
        shown += 1;
      }
      if (!shown) list.append(node("p", "set-hint", "No tool matches this filter."));
      block.append(list);
      return block;
    }

    function rememberedField() {
      if (!root.querySelector) return null;
      const search = root.querySelector("input[type='search']");
      const directory = root.querySelector("[aria-label='Skills directory to add']");
      const active = root.ownerDocument && root.ownerDocument.activeElement;
      if (active && active === directory && typeof directory.value === "string") view.directory = directory.value;
      if (active && active === search && typeof search.value === "string") view.search = search.value;
      if (active && active === search) return {selector: "input[type='search']", start: search.selectionStart, end: search.selectionEnd};
      if (active && active === directory) return {selector: "[aria-label='Skills directory to add']", start: directory.selectionStart, end: directory.selectionEnd};
      return null;
    }

    function paint() {
      const field = rememberedField();
      root.replaceChildren();
      if (view.notice) {
        const notice = node("p", "shelf-notice", view.notice);
        notice.setAttribute("role", "status");
        root.append(notice);
      }
      const controls = node("div", "settings-actions shelf-controls");
      const search = node("input", "settings-search");
      search.type = "search";
      search.placeholder = "Find a skill or tool";
      search.setAttribute("aria-label", "Find a skill or tool");
      search.value = view.search;
      search.addEventListener("input", () => { view.search = search.value; paint(); });
      controls.append(search);
      const risky = node("button", "", "Risky tools only");
      risky.type = "button";
      risky.setAttribute("aria-pressed", String(view.onlyRisky));
      risky.addEventListener("click", () => { view.onlyRisky = !view.onlyRisky; paint(); });
      controls.append(risky);
      root.append(controls);
      root.append(renderSkills());
      root.append(renderTools());
      if (field) {
        const next = root.querySelector(field.selector);
        if (next) {
          next.focus();
          if (field.start != null && next.setSelectionRange) next.setSelectionRange(field.start, field.end == null ? field.start : field.end);
        }
      }
    }

    async function load() {
      view.skillError = "";
      view.toolError = "";
      view.notice = "";
      const [skills, tools] = await Promise.all([
        api("/api/skills").catch((error) => { view.skillError = error.message || String(error); return null; }),
        api("/api/tools").catch((error) => { view.toolError = error.message || String(error); return null; }),
      ]);
      view.skills = skills;
      view.tools = tools;
      view.busy = false;
      paint();
    }

    return {load, paint};
  }

  window.TesseraShelf = {mount};
})();
