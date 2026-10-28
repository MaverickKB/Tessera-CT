/* One Add Work form, available in place or in a global report dialog. */
(() => {
  "use strict";
  const fieldIds = [
    "board-note-body",
    "work-actual",
    "work-expected",
    "work-steps",
    "work-context",
  ];
  function composeNote(values) {
    const sections = [];
    const labels = [
      "",
      "What happened",
      "Expected result",
      "Steps to reproduce",
      "Page context",
    ];
    for (let index = 0; index < fieldIds.length; index += 1) {
      const text = (values[fieldIds[index]] || "").trim();
      if (text)
        sections.push((labels[index] ? labels[index] + ":\n" : "") + text);
    }
    return sections.join("\n\n");
  }
  class WorkIntake {
    constructor(options) {
      this.options = options;
      this.files = [];
      this.dialog = document.getElementById("quick-work-dialog");
      this.block = document.getElementById("board-note-block");
      this.form = document.getElementById("board-note-form");
      document
        .getElementById("quick-work-open")
        .addEventListener("click", () => this.open());
      document
        .getElementById("work-popout")
        .addEventListener("click", () => this.open(true));
      document
        .getElementById("quick-work-close")
        .addEventListener("click", () => this.dialog.close());
      this.dialog.addEventListener("close", () => {
        if (this.placeholder?.parentNode)
          this.placeholder.replaceWith(this.block);
        this.returnFocus?.focus();
      });
      document.addEventListener("keydown", (event) => {
        if (
          (event.metaKey || event.ctrlKey) &&
          event.shiftKey &&
          event.key.toLowerCase() === "k" &&
          !event.isComposing
        ) {
          event.preventDefault();
          this.open();
        }
      });
      const input = document.getElementById("work-file-input");
      document
        .getElementById("work-attach")
        .addEventListener("click", () => input.click());
      input.addEventListener("change", () => {
        this.addFiles(input.files);
        input.value = "";
      });
      this.form.addEventListener("paste", (event) => {
        if (event.clipboardData?.files.length) {
          event.preventDefault();
          this.addFiles(event.clipboardData.files);
        }
      });
      this.form.addEventListener("dragover", (event) => {
        if ([...event.dataTransfer.types].includes("Files")) {
          event.preventDefault();
          this.form.classList.add("work-drop-target");
        }
      });
      this.form.addEventListener("dragleave", () =>
        this.form.classList.remove("work-drop-target"),
      );
      window.addEventListener("cairn-local-evidence", event => this.addFiles(event.detail));
      this.form.addEventListener("drop", (event) => {
        if (event.dataTransfer?.files.length) {
          event.preventDefault();
          this.addFiles(event.dataTransfer.files);
        }
        this.form.classList.remove("work-drop-target");
      });
      document
        .getElementById("work-page-context")
        .addEventListener("click", () => {
          const context = this.origin || this.captureContext();
          document.getElementById("work-context").value = context.text;
          document.getElementById("work-context").hidden = false;
          document.getElementById("work-context-label").hidden = false;
        });
    }
    captureContext() {
      const context = this.options.context();
      const lines = ["Page: " + context.page, "Link: " + location.href];
      if (context.session) lines.push("Session: " + context.session);
      const selected = window.getSelection()?.toString().trim();
      if (selected) lines.push("Selected text: " + selected.slice(0, 2000));
      return { text: lines.join("\n"), project: context.project || "" };
    }
    open(keepGroup = false) {
      if (this.dialog.open) return;
      this.origin = this.captureContext();
      this.returnFocus = document.activeElement;
      this.placeholder = document.createElement("span");
      this.placeholder.hidden = true;
      this.block.before(this.placeholder);
      document.getElementById("quick-work-slot").append(this.block);
      this.block.hidden = false;
      if (
        !keepGroup &&
        !document.getElementById("board-note-what").value.trim()
      ) {
        document.getElementById("board-note-project").value = "";
      }
      this.dialog.showModal();
      document.getElementById("board-note-what").focus();
      this.options.loadGroups?.().catch(() => {});
    }
    addFiles(files) {
      for (const file of files) {
        if (this.files.length >= 6) {
          document.getElementById("board-note-status").textContent =
            "Up to six evidence files fit on one card.";
          break;
        }
        if (!file.path && file.size > 8 * 1024 * 1024) {
          document.getElementById("board-note-status").textContent =
            file.name + " exceeds the 8 MiB attachment limit.";
          continue;
        }
        const item = { file, preview: "" };
        this.files.push(item);
        if (file.type.startsWith("image/")) {
          const reader = new FileReader();
          reader.addEventListener("load", () => {
            if (!this.files.includes(item)) return;
            item.preview = reader.result;
            this.renderFiles();
          });
          reader.readAsDataURL(file);
        }
      }
      this.renderFiles();
    }
    renderFiles() {
      const tray = document.getElementById("work-evidence-tray");
      tray.replaceChildren();
      tray.hidden = !this.files.length;
      for (const item of this.files) {
        const chip = document.createElement("div");
        chip.className = "work-evidence-chip";
        if (item.preview) {
          const image = document.createElement("img");
          image.src = item.preview;
          image.alt = "";
          chip.append(image);
        }
        const name = document.createElement("span");
        name.textContent = item.file.name;
        const remove = document.createElement("button");
        remove.type = "button";
        remove.textContent = "Remove";
        remove.setAttribute("aria-label", "Remove " + item.file.name);
        remove.addEventListener("click", () => {
          this.files.splice(this.files.indexOf(item), 1);
          this.renderFiles();
        });
        chip.append(name, remove);
        tray.append(chip);
      }
    }
    async prepare() {
      const values = {};
      for (const id of fieldIds) values[id] = document.getElementById(id).value;
      const files = [...this.files];
      const evidence = [];
      for (const item of files) {
        if (!item.stored) {
          const session = this.options.session();
          if (!session)
            throw new Error("Create a session before attaching evidence.");
          const ref = await this.options.upload(session, item.file);
          item.stored = {
            session_slug: session,
            id: ref.id,
            name: ref.name,
            media_type: ref.media_type,
            size: ref.bytes,
          };
        }
        evidence.push(item.stored);
      }
      return { note: composeNote(values), evidence, values, files };
    }
    saved(snapshot) {
      for (const id of fieldIds) {
        const field = document.getElementById(id);
        if (field.value === snapshot.values[id]) field.value = "";
      }
      for (const item of snapshot.files) {
        const index = this.files.indexOf(item);
        if (index >= 0) this.files.splice(index, 1);
      }
      this.renderFiles();
      if (!document.getElementById("work-context").value) {
        document.getElementById("work-context").hidden = true;
        document.getElementById("work-context-label").hidden = true;
      }
    }
  }
  window.TesseraWorkIntake = { WorkIntake, composeNote };
})();
