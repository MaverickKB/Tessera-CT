/* Persistent project objects, shared by pointer controls and agent operations. */
(() => {
  "use strict";
  const svgNS = "http://www.w3.org/2000/svg";
  function make(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function button(text, action, cls = "") {
    const node = make("button", cls, text);
    node.type = "button";
    node.addEventListener("click", action);
    return node;
  }
  function uid() {
    return crypto.randomUUID();
  }
  function roots(nodes, selected) {
    const byId = new Map();
    for (const node of nodes) byId.set(node.id, node);
    const result = [];
    for (const id of selected) {
      let parent = byId.get(id)?.parent;
      let included = false;
      while (parent) {
        if (selected.has(parent)) {
          included = true;
          break;
        }
        parent = byId.get(parent)?.parent;
      }
      if (!included && byId.has(id)) result.push(byId.get(id));
    }
    return result;
  }
  function descendants(nodes, selected) {
    const result = new Set(selected);
    let changed = true;
    while (changed) {
      changed = false;
      for (const node of nodes)
        if (node.parent && result.has(node.parent) && !result.has(node.id)) {
          result.add(node.id);
          changed = true;
        }
    }
    return result;
  }

  class ProjectCanvas {
    constructor(root, options) {
      this.root = root;
      this.options = options;
      this.data = {
        canvas: { nodes: [], links: [], version: 0 },
        cards: [],
        steps: [],
      };
      this.selected = new Set();
      this.views = new Map();
      this.pieces = new Map();
      this.libraryRows = new Map();
      this.view = { x: 40, y: 40, zoom: 1 };
      this.multi = false;
      this.pending = 0;
      this.optimistic = new Map();
      this.queue = Promise.resolve();
      this.destroyed = false;
      try {
        Object.assign(
          this.view,
          JSON.parse(
            localStorage.getItem("cairn.canvas." + options.projectId) || "{}",
          ),
        );
      } catch (_) {
        /* fresh view */
      }
      this.build();
      this.refresh();
      this.timer = setInterval(() => {
        if (!this.drag && !this.pending && !root.hidden) this.refresh();
      }, 4000);
    }
    destroy() {
      this.destroyed = true;
      clearInterval(this.timer);
      window.removeEventListener("cairn-local-artifacts", this.localArtifacts);
    }
    selectedIds() {
      return [...this.selected];
    }
    endpoint() {
      return (
        "/api/board/projects/" +
        encodeURIComponent(this.options.projectId) +
        "/canvas"
      );
    }
    async refresh() {
      try {
        const data = await this.options.api(this.endpoint());
        if (this.destroyed) return;
        if (this.pending || data.canvas.version < this.data.canvas.version)
          return;
        this.data = data;
        if (!this.pending)
          this.notice.textContent =
            data.canvas.nodes.length +
            " pieces · " +
            data.canvas.links.length +
            " connections";
        this.paint();
        if (!this.didFit && !this.data.canvas.nodes.length) this.didFit = true;
        else if (!this.didFit) {
          this.fit();
          this.didFit = true;
        }
      } catch (error) {
        this.notice.textContent = "Canvas unavailable: " + error.message;
      }
    }
    mutate(changes) {
      this.pending += 1;
      for (const patch of changes.nodes || [])
        this.optimistic.set(patch.id, {
          ...this.optimistic.get(patch.id),
          ...patch,
        });
      this.notice.textContent = "Saving workspace…";
      const execute = async () => {
        try {
          const data = await this.options.api(this.endpoint(), "POST", changes);
          if (!this.destroyed) this.data = data;
          this.notice.textContent = "Workspace saved";
          return true;
        } catch (error) {
          this.notice.textContent = "Not saved: " + error.message;
          return false;
        } finally {
          this.pending -= 1;
          if (!this.pending) this.optimistic.clear();
          if (!this.destroyed) this.paint();
        }
      };
      this.queue = this.queue.then(execute, execute);
      return this.queue;
    }
    build() {
      this.root.className = "project-canvas";
      const toolbar = make("div", "canvas-toolbar");
      this.multiButton = button("Multi-select", () => {
        this.multi = !this.multi;
        this.multiButton.setAttribute("aria-pressed", String(this.multi));
      });
      this.multiButton.setAttribute("aria-pressed", "false");
      this.connectButton = button("Connect", () => this.connect());
      this.stackButton = button("Stack", () => this.stack());
      toolbar.append(
        button("+ Note", () => this.addNote(), "primary"),
        button("+ Task", () => this.options.addTask()),
        button("Bring in tasks & plan", () => this.importWork()),
        button("Add artifact", () => this.file.click()),
        button("Pieces", () => {
          this.side.hidden = !this.side.hidden;
        }),
        this.multiButton,
        this.connectButton,
        this.stackButton,
        button("Fit", () => this.fit()),
      );
      this.file = make("input");
      this.file.type = "file";
      this.file.hidden = true;
      this.file.accept =
        "image/png,image/jpeg,image/gif,image/webp,text/plain,text/markdown,application/pdf,application/json";
      this.localArtifacts = async event => {
        for (const file of event.detail) await this.upload(file);
      };
      window.addEventListener("cairn-local-artifacts", this.localArtifacts);
      this.file.addEventListener("change", () =>
        this.upload(this.file.files[0]),
      );
      this.notice = make("p", "canvas-notice", "Reading workspace…");
      this.notice.setAttribute("role", "status");
      const layout = make("div", "canvas-layout");
      this.viewport = make("div", "canvas-viewport");
      this.viewport.tabIndex = 0;
      this.viewport.setAttribute(
        "aria-label",
        "Project canvas. Drag a piece by its heading. Drag empty space to pan. Shift-click or Multi-select to select several pieces.",
      );
      this.world = make("div", "canvas-world");
      this.lines = document.createElementNS(svgNS, "svg");
      this.lines.classList.add("canvas-lines");
      this.lines.setAttribute("aria-hidden", "true");
      this.world.append(this.lines);
      this.viewport.append(this.world);
      this.empty = make("div", "canvas-empty");
      this.empty.append(
        make("h3", "", "Make room for the idea."),
        make(
          "p",
          "",
          "Add a piece, bring in the work, or ask the agent to draw a plan here.",
        ),
      );
      this.viewport.append(this.empty);
      this.zoomLabel = make("span", "canvas-zoom", "100%");
      const zoom = make("div", "canvas-zoom-controls");
      zoom.append(
        button("−", () => this.zoomAt(0.8)),
        this.zoomLabel,
        button("+", () => this.zoomAt(1.25)),
      );
      this.viewport.append(zoom);
      this.side = make("aside", "canvas-side");
      this.side.hidden = true;
      this.side.append(
        button("Close panel", () => {
          this.side.hidden = true;
        }),
      );
      this.inspector = make("section", "canvas-inspector");
      const library = make("section", "canvas-library");
      library.append(make("h3", "", "Artifacts & pieces"));
      this.library = make("div", "canvas-library-list");
      library.append(this.library);
      this.side.append(this.inspector, library);
      layout.append(this.viewport, this.side);
      this.root.append(toolbar, this.file, this.notice, layout);
      this.viewport.addEventListener("pointerdown", (event) =>
        this.startPointer(event),
      );
      this.viewport.addEventListener("pointermove", (event) =>
        this.movePointer(event),
      );
      this.viewport.addEventListener("pointerup", (event) =>
        this.endPointer(event),
      );
      this.viewport.addEventListener("pointercancel", (event) =>
        this.endPointer(event),
      );
      this.viewport.addEventListener(
        "wheel",
        (event) => {
          if (event.target.closest("textarea,input,.stone-body")) return;
          event.preventDefault();
          if (event.ctrlKey || event.metaKey)
            this.zoomAt(
              Math.exp(-event.deltaY * 0.008),
              event.clientX,
              event.clientY,
            );
          else {
            this.view.x -= event.deltaX;
            this.view.y -= event.deltaY;
            this.transform();
          }
        },
        { passive: false },
      );
      this.viewport.addEventListener("keydown", (event) => {
        if (event.target.closest("input,textarea,button,a")) return;
        const deltas = {
          ArrowLeft: [-1, 0],
          ArrowRight: [1, 0],
          ArrowUp: [0, -1],
          ArrowDown: [0, 1],
        };
        if (deltas[event.key] && this.selected.size) {
          event.preventDefault();
          const [dx, dy] = deltas[event.key];
          const amount = event.shiftKey ? 50 : 10;
          const moving = descendants(this.nodes(), this.selected);
          const patches = [];
          for (const node of this.nodes())
            if (moving.has(node.id))
              patches.push({
                id: node.id,
                x: node.x + dx * amount,
                y: node.y + dy * amount,
              });
          this.mutate({ nodes: patches });
        }
        if (event.key === "Escape") {
          this.selected.clear();
          this.paintSelection();
        }
      });
    }
    nodes() {
      const nodes = [];
      const found = new Set();
      for (const node of this.data.canvas.nodes) {
        nodes.push({ ...node, ...this.optimistic.get(node.id) });
        found.add(node.id);
      }
      for (const [id, patch] of this.optimistic)
        if (!found.has(id))
          nodes.push({
            kind: "note",
            x: 0,
            y: 0,
            width: 260,
            height: 180,
            z: 0,
            ...patch,
          });
      return nodes;
    }
    transform() {
      this.world.style.transform =
        "translate(" +
        this.view.x +
        "px," +
        this.view.y +
        "px) scale(" +
        this.view.zoom +
        ")";
      this.zoomLabel.textContent = Math.round(this.view.zoom * 100) + "%";
      try {
        localStorage.setItem(
          "cairn.canvas." + this.options.projectId,
          JSON.stringify(this.view),
        );
      } catch (_) {
        /* private mode */
      }
    }
    point(event) {
      const rect = this.viewport.getBoundingClientRect();
      return {
        x: (event.clientX - rect.left - this.view.x) / this.view.zoom,
        y: (event.clientY - rect.top - this.view.y) / this.view.zoom,
      };
    }
    zoomAt(factor, clientX, clientY) {
      const rect = this.viewport.getBoundingClientRect();
      const x = clientX === undefined ? rect.width / 2 : clientX - rect.left;
      const y = clientY === undefined ? rect.height / 2 : clientY - rect.top;
      const next = Math.min(3, Math.max(0.15, this.view.zoom * factor));
      const ratio = next / this.view.zoom;
      this.view.x = x - (x - this.view.x) * ratio;
      this.view.y = y - (y - this.view.y) * ratio;
      this.view.zoom = next;
      this.transform();
    }
    fit() {
      const nodes = [];
      for (const node of this.nodes())
        if (node.placed !== false) nodes.push(node);
      if (!nodes.length) {
        this.view = { x: 40, y: 40, zoom: 1 };
        this.transform();
        return;
      }
      let left = Infinity,
        top = Infinity,
        right = -Infinity,
        bottom = -Infinity;
      for (const node of nodes) {
        left = Math.min(left, node.x);
        top = Math.min(top, node.y);
        right = Math.max(right, node.x + node.width);
        bottom = Math.max(bottom, node.y + node.height);
      }
      const rect = this.viewport.getBoundingClientRect();
      const zoom = Math.min(
        1.2,
        Math.max(
          0.15,
          Math.min(
            (rect.width - 80) / (right - left),
            (rect.height - 80) / (bottom - top),
          ),
        ),
      );
      this.view = {
        zoom,
        x: (rect.width - (right - left) * zoom) / 2 - left * zoom,
        y: (rect.height - (bottom - top) * zoom) / 2 - top * zoom,
      };
      this.transform();
    }
    paint() {
      if (this.destroyed || this.drag) return;
      const nodes = this.nodes();
      const ids = new Set();
      for (const node of nodes) {
        ids.add(node.id);
        let el = this.pieces.get(node.id);
        if (!el) {
          el = make("article", "canvas-stone");
          el.dataset.nodeId = node.id;
          const handle = button("", (event) => {
            if (event.detail === 0)
              this.select(
                node.id,
                event.shiftKey || event.metaKey || this.multi,
              );
          });
          handle.className = "stone-handle";
          const body = make("div", "stone-body");
          el.append(handle, body);
          el.addEventListener("click", (event) => {
            if (!event.target.closest("button,a,input,textarea"))
              this.select(
                node.id,
                event.shiftKey || event.metaKey || this.multi,
              );
          });
          this.world.append(el);
          this.pieces.set(node.id, el);
        }
        el.dataset.kind = node.kind;
        el.hidden = node.placed === false;
        el.style.left = node.x + "px";
        el.style.top = node.y + "px";
        el.style.width = node.width + "px";
        el.style.height = node.height + "px";
        el.style.zIndex = String(node.z || 0);
        const key = JSON.stringify([node, this.data.cards, this.data.steps]);
        if (el.renderKey !== key) {
          el.renderKey = key;
          const handle = el.firstChild;
          handle.textContent = this.titleOf(node);
          handle.setAttribute(
            "aria-label",
            "Select or move " + this.titleOf(node),
          );
          const body = el.lastChild;
          body.replaceChildren();
          if (node.kind === "task") {
            const task = this.data.cards.find(
              (card) => card.id === node.card_id,
            );
            if (task && !node.title) handle.textContent = task.title;
            body.append(
              make(
                "span",
                "stone-work-state",
                task
                  ? task.removed
                    ? "Removed from work board"
                    : task.status +
                      (task.owner ? " · " + task.owner : " · no owner")
                  : "Linked task unavailable",
              ),
            );
            if (task)
              body.append(
                button("Open task", () => this.options.openTask(node.card_id)),
              );
          }
          if (node.kind === "step") {
            const step = this.data.steps.find(
              (item) => item.id === node.step_id,
            );
            if (step && !node.title) handle.textContent = step.text;
            body.append(
              make(
                "span",
                "stone-work-state",
                step
                  ? step.done
                    ? "Plan step done"
                    : "Plan step pending"
                  : "Step no longer in this plan",
              ),
            );
            if (step)
              body.append(
                button(step.done ? "Mark pending" : "Mark done", () =>
                  this.options.checkStep(node.step_id, !step.done),
                ),
              );
          }
          if (node.kind === "image") {
            const img = make("img");
            img.alt = node.title || "Project image";
            img.draggable = false;
            img.src = this.attachmentUrl(node);
            body.append(img);
          }
          if (node.body) {
            const content = make("div", "stone-content");
            if (this.options.format) this.options.format(content, node.body);
            else content.textContent = node.body;
            body.append(content);
          }
          if (node.kind === "document") {
            const a = make("a", "", "Open document ↗");
            a.href = this.attachmentUrl(node);
            a.target = "_blank";
            a.rel = "noopener";
            body.append(a);
          }
          if (node.url) {
            const a = make("a", "", node.url);
            a.href = node.url;
            a.target = "_blank";
            a.rel = "noopener noreferrer";
            body.append(a);
          }
        }
        let item = this.libraryRows.get(node.id);
        if (!item) {
          item = button("", async () => {
            const current = this.nodes().find((piece) => piece.id === node.id);
            if (current?.placed === false) {
              const restored = descendants(this.nodes(), new Set([node.id]));
              const patches = [];
              for (const id of restored) patches.push({ id, placed: true });
              if (!(await this.mutate({ nodes: patches }))) return;
            }
            this.select(node.id, false);
            this.center(node.id);
          });
          this.libraryRows.set(node.id, item);
          this.library.append(item);
        }
        item.textContent =
          this.titleOf(node) +
          " · " +
          node.kind +
          (node.placed === false ? " · stored" : "");
      }
      for (const [id, el] of this.pieces)
        if (!ids.has(id)) {
          el.remove();
          this.pieces.delete(id);
          this.libraryRows.get(id)?.remove();
          this.libraryRows.delete(id);
          this.selected.delete(id);
        }
      this.empty.hidden = nodes.some((node) => node.placed !== false);
      this.paintLines();
      this.paintSelection();
      this.transform();
    }
    titleOf(node) {
      if (node.title) return node.title;
      if (node.kind === "task")
        return (
          this.data.cards.find((card) => card.id === node.card_id)?.title ||
          "Linked task"
        );
      if (node.kind === "step")
        return (
          this.data.steps.find((step) => step.id === node.step_id)?.text ||
          "Linked plan step"
        );
      return node.kind;
    }
    attachmentUrl(node) {
      return (
        "/api/sessions/" +
        encodeURIComponent(node.session_slug) +
        "/attachments?id=" +
        encodeURIComponent(node.attachment_id)
      );
    }
    paintLines() {
      this.lines.replaceChildren();
      const byId = new Map();
      for (const node of this.nodes()) byId.set(node.id, node);
      for (const link of this.data.canvas.links) {
        const a = byId.get(link.from),
          b = byId.get(link.to);
        if (!a || !b || a.placed === false || b.placed === false) continue;
        const x1 = a.x + a.width,
          y1 = a.y + a.height / 2,
          x2 = b.x,
          y2 = b.y + b.height / 2;
        const path = document.createElementNS(svgNS, "path");
        const bend = Math.max(60, Math.abs(x2 - x1) / 2);
        path.setAttribute(
          "d",
          "M" +
            x1 +
            "," +
            y1 +
            " C" +
            (x1 + bend) +
            "," +
            y1 +
            " " +
            (x2 - bend) +
            "," +
            y2 +
            " " +
            x2 +
            "," +
            y2,
        );
        this.lines.append(path);
        const arrow = document.createElementNS(svgNS, "path");
        arrow.setAttribute(
          "d",
          "M" +
            (x2 - 8) +
            "," +
            (y2 - 5) +
            " L" +
            x2 +
            "," +
            y2 +
            " L" +
            (x2 - 8) +
            "," +
            (y2 + 5),
        );
        this.lines.append(arrow);
        if (link.label) {
          const label = document.createElementNS(svgNS, "text");
          label.setAttribute("x", String((x1 + x2) / 2));
          label.setAttribute("y", String((y1 + y2) / 2 - 8));
          label.textContent = link.label;
          this.lines.append(label);
        }
      }
    }
    select(id, multiple) {
      this.side.hidden = false;
      if (!multiple) this.selected.clear();
      if (multiple && this.selected.has(id)) this.selected.delete(id);
      else this.selected.add(id);
      this.dirtyInspector = false;
      this.paintSelection();
    }
    paintSelection() {
      for (const [id, el] of this.pieces)
        el.dataset.selected = String(this.selected.has(id));
      for (const [id, el] of this.libraryRows)
        el.setAttribute("aria-pressed", String(this.selected.has(id)));
      this.connectButton.disabled = this.selected.size !== 2;
      this.stackButton.disabled = this.selected.size < 2;
      this.paintInspector();
      this.options.onSelection?.(this.selectedIds());
    }
    center(id) {
      const node = this.nodes().find((item) => item.id === id);
      if (!node) return;
      const rect = this.viewport.getBoundingClientRect();
      this.view.x = rect.width / 2 - (node.x + node.width / 2) * this.view.zoom;
      this.view.y =
        rect.height / 2 - (node.y + node.height / 2) * this.view.zoom;
      this.transform();
    }
    paintInspector() {
      if (this.dirtyInspector) return;
      const selectionKey = [...this.selected].join("|");
      const selectedNodes = [];
      const selectedLinks = [];
      for (const piece of this.nodes())
        if (this.selected.has(piece.id))
          selectedNodes.push([
            piece.id,
            piece.kind,
            piece.title,
            piece.body,
            piece.url,
          ]);
      for (const link of this.data.canvas.links) {
        if (this.selected.has(link.from) || this.selected.has(link.to))
          selectedLinks.push([link.id, link.from, link.to, link.label]);
      }
      const renderKey = JSON.stringify([
        selectionKey,
        selectedNodes,
        selectedLinks,
      ]);
      if (this.inspectorKey === renderKey) return;
      this.inspectorKey = renderKey;
      this.inspector.replaceChildren();
      this.inspector.append(
        make(
          "h3",
          "",
          this.selected.size
            ? this.selected.size + " selected"
            : "Touch the pieces",
        ),
      );
      if (!this.selected.size) {
        this.inspector.append(
          make(
            "p",
            "",
            "Select a piece to edit it. Drag its heading to move it. Multi-select pieces to connect or stack them.",
          ),
        );
        return;
      }
      this.inspector.append(button("Remove from canvas", () => this.remove()));
      if (this.selected.size !== 1) {
        this.inspector.append(
          make(
            "p",
            "",
            "Connect uses selection order: first piece → second piece.",
          ),
        );
        return;
      }
      const node = this.nodes().find((item) => this.selected.has(item.id));
      if (!node) return;
      const form = make("form", "stone-edit");
      const titleLabel = make("label", "", "Title");
      const title = make("input");
      title.value = node.title || "";
      titleLabel.append(title);
      const bodyLabel = make("label", "", "Content");
      const body = make("textarea");
      body.rows = 5;
      body.value = node.body || "";
      bodyLabel.append(body);
      const urlLabel = make("label", "", "Reference URL");
      const url = make("input");
      url.type = "url";
      url.value = node.url || "";
      urlLabel.append(url);
      const save = button("Save piece", () => {});
      save.type = "submit";
      let version = this.data.canvas.version;
      form.append(titleLabel, bodyLabel, urlLabel, save);
      form.addEventListener("input", () => {
        if (!this.dirtyInspector) version = this.data.canvas.version;
        this.dirtyInspector = true;
      });
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        save.disabled = true;
        const ok = await this.mutate({
          expected_version: version,
          nodes: [
            {
              id: node.id,
              title: title.value,
              body: body.value,
              url: url.value,
            },
          ],
        });
        if (ok) {
          this.dirtyInspector = false;
          this.inspectorKey = null;
          this.paintInspector();
        } else {
          await this.refresh();
          version = this.data.canvas.version;
          save.textContent = "Apply my edit to latest";
          save.disabled = false;
        }
      });
      this.inspector.append(form);
      if (node.kind === "group")
        this.inspector.append(
          button("Unstack", () => this.mutate({ remove_nodes: [node.id] })),
        );
      const connections = make("div", "stone-connections");
      for (const link of this.data.canvas.links)
        if (link.from === node.id || link.to === node.id) {
          const row = make("form", "stone-link-edit");
          const source = this.nodes().find((item) => item.id === link.from);
          const destination = this.nodes().find((item) => item.id === link.to);
          const label = make("input");
          label.value = link.label || "";
          label.setAttribute("aria-label", "Connection label");
          label.placeholder = "Describe the connection";
          const saveLink = button("Save connection", () => {});
          saveLink.type = "submit";
          let version = this.data.canvas.version;
          row.addEventListener("input", () => {
            if (!this.dirtyInspector) version = this.data.canvas.version;
            this.dirtyInspector = true;
          });
          row.addEventListener("submit", async (event) => {
            event.preventDefault();
            saveLink.disabled = true;
            const saved = await this.mutate({
              expected_version: version,
              links: [{ id: link.id, label: label.value }],
            });
            saveLink.disabled = false;
            if (saved) {
              this.dirtyInspector = false;
              this.inspectorKey = null;
              this.paintInspector();
            } else {
              await this.refresh();
              version = this.data.canvas.version;
              saveLink.textContent = "Apply my label to latest";
            }
          });
          row.append(
            make(
              "span",
              "",
              this.titleOf(source) + " → " + this.titleOf(destination),
            ),
            label,
            saveLink,
            button("Disconnect", () =>
              this.mutate({ remove_links: [link.id] }),
            ),
          );
          connections.append(row);
        }
      this.inspector.append(connections);
    }
    startPointer(event) {
      if (
        event.button !== 0 ||
        event.target.closest(
          "input,textarea,a,.stone-body,.canvas-zoom-controls",
        )
      )
        return;
      const handle = event.target.closest(".stone-handle");
      const stone = handle?.closest(".canvas-stone");
      if (event.target.closest("button") && !handle) return;
      if (stone) {
        const id = stone.dataset.nodeId;
        if (event.shiftKey || event.metaKey || this.multi) {
          this.select(id, true);
          return;
        }
        if (!this.selected.has(id)) this.select(id, false);
        const moving = descendants(this.nodes(), this.selected);
        const initial = [];
        for (const node of this.nodes())
          if (moving.has(node.id))
            initial.push({ id: node.id, x: node.x, y: node.y });
        this.drag = {
          type: "pieces",
          start: this.point(event),
          initial,
          moved: false,
        };
      } else
        this.drag = {
          type: "pan",
          x: event.clientX,
          y: event.clientY,
          view: { ...this.view },
          moved: false,
        };
      event.preventDefault();
      this.viewport.setPointerCapture(event.pointerId);
    }
    movePointer(event) {
      if (!this.drag) return;
      if (this.drag.type === "pan") {
        const dx = event.clientX - this.drag.x,
          dy = event.clientY - this.drag.y;
        this.drag.moved = this.drag.moved || Math.abs(dx) + Math.abs(dy) > 3;
        this.view.x = this.drag.view.x + dx;
        this.view.y = this.drag.view.y + dy;
        this.transform();
        return;
      }
      const now = this.point(event),
        dx = now.x - this.drag.start.x,
        dy = now.y - this.drag.start.y;
      this.drag.moved = this.drag.moved || Math.abs(dx) + Math.abs(dy) > 2;
      for (const node of this.drag.initial) {
        const patch = { id: node.id, x: node.x + dx, y: node.y + dy };
        this.optimistic.set(node.id, {
          ...this.optimistic.get(node.id),
          ...patch,
        });
        const el = this.pieces.get(node.id);
        el.style.left = patch.x + "px";
        el.style.top = patch.y + "px";
      }
      this.paintLines();
    }
    endPointer(event) {
      if (!this.drag) return;
      const drag = this.drag;
      this.drag = null;
      if (this.viewport.hasPointerCapture(event.pointerId))
        this.viewport.releasePointerCapture(event.pointerId);
      if (drag.type === "pieces" && drag.moved) {
        const nodes = [];
        for (const node of drag.initial)
          nodes.push({ ...this.optimistic.get(node.id) });
        this.mutate({ nodes });
      } else if (drag.type === "pan" && !drag.moved) {
        this.selected.clear();
        this.dirtyInspector = false;
        this.paintSelection();
      }
      if (!this.pending) {
        this.optimistic.clear();
        this.paint();
      }
    }
    async addNote() {
      const rect = this.viewport.getBoundingClientRect();
      const id = uid();
      const x = (rect.width / 2 - this.view.x) / this.view.zoom - 130,
        y = (rect.height / 2 - this.view.y) / this.view.zoom - 90;
      if (
        await this.mutate({
          nodes: [{ id, kind: "note", title: "New piece", body: "", x, y }],
        })
      ) {
        this.select(id, false);
        this.inspector.querySelector("input")?.focus();
      }
    }
    async importWork() {
      const nodes = [];
      const existing = new Map();
      for (const node of this.nodes())
        if (node.card_id || node.step_id)
          existing.set(node.card_id || node.step_id, node);
      let index = this.nodes().length;
      for (const [kind, items] of [
        ["task", this.data.cards],
        ["step", this.data.steps],
      ])
        for (const item of items) {
          if (item.removed) continue;
          const previous = existing.get(item.id);
          if (previous) {
            if (previous.placed === false)
              nodes.push({ id: previous.id, placed: true });
            continue;
          }
          const node = {
            id: uid(),
            kind,
            title: "",
            x: (index % 3) * 310,
            y: Math.floor(index / 3) * 230,
            width: 270,
            height: 180,
          };
          node[kind === "task" ? "card_id" : "step_id"] = item.id;
          nodes.push(node);
          index += 1;
        }
      if (nodes.length && (await this.mutate({ nodes }))) this.fit();
    }
    connect() {
      const ids = [...this.selected];
      if (ids.length !== 2) return;
      this.mutate({
        links: [{ id: uid(), from: ids[0], to: ids[1], label: "" }],
      });
    }
    async stack() {
      const selected = roots(this.nodes(), this.selected);
      if (selected.length < 2) return;
      let x = Infinity,
        y = Infinity,
        width = 0,
        height = 0;
      for (const node of selected) {
        x = Math.min(x, node.x);
        y = Math.min(y, node.y);
        width = Math.max(width, node.width);
        height = Math.max(height, node.height);
      }
      const id = uid();
      const nodes = [
        {
          id,
          kind: "group",
          title: "Stack · " + selected.length + " pieces",
          x: x - 20,
          y: y - 45,
          width: width + selected.length * 18 + 40,
          height: height + selected.length * 36 + 65,
          z: -1,
        },
      ];
      for (let index = 0; index < selected.length; index += 1) {
        const node = selected[index];
        const dx = x + index * 18 - node.x,
          dy = y + index * 36 - node.y;
        const moving = descendants(this.nodes(), new Set([node.id]));
        for (const child of this.nodes())
          if (moving.has(child.id))
            nodes.push({
              id: child.id,
              x: child.x + dx,
              y: child.y + dy,
              ...(child.id === node.id ? { parent: id, z: index } : {}),
            });
      }
      if (await this.mutate({ nodes })) this.select(id, false);
    }
    async remove() {
      if (!this.selected.size) return;
      if (
        !(await this.options.ask(
          "Remove the selected pieces from this canvas? Linked tasks and files stay in their libraries.",
          "Remove pieces",
        ))
      )
        return;
      const hidden = descendants(this.nodes(), this.selected);
      const patches = [];
      for (const id of hidden) patches.push({ id, placed: false });
      if (await this.mutate({ nodes: patches })) {
        this.selected.clear();
        this.dirtyInspector = false;
        this.paintSelection();
      }
    }
    async upload(file) {
      if (!file) return;
      try {
        this.notice.textContent = "Storing artifact…";
        const session = this.options.session();
        if (!session) throw new Error("Choose a conversation session first");
        const response = await fetch(
          "/api/sessions/" + encodeURIComponent(session) + (file.path ? "/attachment-references" : "/attachments"),
          {
            method: "POST",
            headers: {
              "Content-Type": file.path ? "application/json" : file.type || "application/octet-stream",
              "X-File-Name": encodeURIComponent(file.name),
            },
            body: file.path ? JSON.stringify({ path: file.path }) : file,
          },
        );
        const attachment = await response.json();
        if (!response.ok) throw new Error(attachment.error || "Upload failed");
        const node = {
          id: uid(),
          kind: attachment.media_type?.startsWith("image/")
            ? "image"
            : "document",
          title: attachment.name || file.name,
          session_slug: session,
          attachment_id: attachment.id,
          media_type: attachment.media_type || "",
          x: 80,
          y: 80,
          width: 320,
          height: 280,
        };
        if (!file.path && (file.type.startsWith("text/") || file.type === "application/json"))
          node.body = (await file.text()).slice(0, 1800);
        if (await this.mutate({ nodes: [node] })) {
          this.select(node.id, false);
          this.center(node.id);
        }
      } catch (error) {
        this.notice.textContent = "Artifact not added: " + error.message;
      } finally {
        this.file.value = "";
      }
    }
  }
  window.TesseraProjectCanvas = { ProjectCanvas, roots, descendants };
})();
