"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class El {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.className = "";
    this.hidden = false;
    this.checked = false;
    this.disabled = false;
    this.type = "";
    this.value = "";
    this.id = "";
    this.htmlFor = "";
    this.open = false;
    this._text = "";
    this.listeners = {};
    this.parent = null;
    this.style = {};
    this.classList = {
      add: () => {},
      remove: () => {},
      contains: (name) => String(this.className || "").split(/\s+/).includes(name),
    };
  }
  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }
  get textContent() {
    return this._text + this.children.map((child) => child.textContent || "").join("");
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
  append(...nodes) { for (const node of nodes) this.insertBefore(node, null); }
  insertBefore(node, cursor) {
    if (node.parent) node.parent.children.splice(node.parent.children.indexOf(node), 1);
    const index = cursor ? this.children.indexOf(cursor) : this.children.length;
    this.children.splice(index, 0, node);
    node.parent = this;
  }
  replaceChildren(...nodes) {
    this.children = [];
    this._text = "";
    this.append(...nodes);
  }
  replaceWith(node) {
    if (!this.parent) return;
    const parent = this.parent;
    const index = parent.children.indexOf(this);
    this.remove();
    parent.insertBefore(node, parent.children[index] || null);
  }
  remove() {
    if (!this.parent) return;
    this.parent.children.splice(this.parent.children.indexOf(this), 1);
    this.parent = null;
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  click() {
    const event = {preventDefault() {}, stopPropagation() {}};
    for (const fn of this.listeners.click || []) fn(event);
  }
  focus() {}
  classNames() {
    return String(this.className || "").split(/\s+/).filter(Boolean);
  }
  querySelector(selector) {
    const key = selector.match(/^\[data-key="([^"]+)"\]$/);
    const cls = selector.match(/^\.([A-Za-z0-9_-]+)$/);
    for (const child of this.children) {
      if (selector === child.tag) return child;
      if (key && child.dataset.key === key[1]) return child;
      if (cls && child.classNames().includes(cls[1])) return child;
      const found = child.querySelector(selector);
      if (found) return found;
    }
    return null;
  }
  querySelectorAll(selector) {
    const found = [];
    const visit = (node) => {
      for (const child of node.children) {
        if (selector === child.tag) found.push(child);
        if (selector.startsWith(".") && child.classNames().includes(selector.slice(1))) found.push(child);
        visit(child);
      }
    };
    visit(this);
    return found;
  }
}

function all(node, out = []) {
  out.push(node);
  for (const child of node.children) all(child, out);
  return out;
}

function rowFor(root, labelText) {
  const label = all(root).find((node) => node.tag === "label" && node.textContent === labelText);
  assert.ok(label, labelText);
  let row = label;
  while (row && !row.classNames().includes("set-row")) row = row.parent;
  assert.ok(row, labelText + " row");
  return {label, row};
}

function clickText(root, text) {
  const button = all(root).find((node) => node.tag === "button" && node.textContent === text);
  assert.ok(button, text);
  button.click();
}

const longHint = "L".repeat(200);
const settingsData = {
  supervised: false,
  provider_pages: {
    accounts: {
      crumb: ["Settings", "Providers", "Accounts"],
      title: "Connect an account",
      help: "Sign in.",
      local_row: {name: "Run models locally", line: "A model server on this machine."},
      connected: [],
      available: [{
        id: "xai", name: "xAI Grok", pill: "Not connected",
        line: "xAI Grok sign-in.",
        actions: ["sign-in"], kind: "signin",
      }],
    },
    keys: {
      crumb: ["Settings", "Providers", "API keys"],
      title: "API keys",
      help: "A key is an environment variable name or a keychain item.",
      records: [{
        id: "slot",
        name: "slot",
        pill: "Keychain item missing",
        line: "Keychain item: slot",
        type_tag: "Keychain",
        added: "Not recorded",
        kind: "key",
        removable: true,
        actions: ["edit", "remove"],
      }],
    },
    endpoints: {
      crumb: ["Settings", "Providers", "Endpoints"],
      title: "Endpoints",
      help: "A local server and any other address are both endpoints.",
      records: [],
    },
  },
  sections: [
    {id: "models", label: "Models", fields: [
      {id: "roles.main", type: "enum", label: "Chat model", value: "", hint: longHint,
        options: [{value: "local-test", label: "local-test"}]},
      {id: "roles.failover", type: "enum", label: "Failover", value: "",
        hint: "Used when the chat model is unreachable.",
        options: [{value: "", label: "Off"}]},
    ]},
    {id: "providers", label: "Providers", fields: [
      {id: "providers", type: "list-of-records", label: "Models", actions: ["add", "edit", "remove"],
        records: [{
          name: "local-test", title: "llama-3.1-8b-demo", status: "Local",
          actions: ["edit", "remove"], group: "local-test",
          source: "No credential",
          fields: [
            {key: "enabled", type: "bool", label: "Enabled", value: true, inline: true},
            {key: "chat", type: "bool", label: "Use for chat", value: true},
            {key: "model", type: "string", label: "Model", value: "llama-3.1-8b-demo",
              hint: "The model id the server expects, such as llama-3.1-8b-demo."},
            {key: "allow_api_key_fallback", type: "bool", label: "API key fallback", value: false,
              hint: "Off, a CLI login never uses an API key. On, a failed login may use the fallback env var."},
            {key: "max_concurrent_requests", type: "string", label: "Max concurrent requests", value: "",
              hint: longHint, status: "In effect: 1 at a time in Safe mode."},
          ],
        }],
        blank: [
          {key: "name", type: "string", label: "Name", value: "", hint: "A short name. Spaces are saved as dashes."},
          {key: "kind", type: "enum", label: "HTTP protocol", value: "openai_chat",
            options: [{value: "openai_chat", label: "OpenAI-compatible chat"}],
            hint: "For local servers, choose the protocol documented by your model server."},
          {key: "base_url", type: "string", label: "Base URL", value: "",
            hint: "The server address, with http:// and the port."},
          {key: "model", type: "string", label: "Model", value: "",
            hint: "The model id the server expects, such as llama-3.1-8b-demo."},
          {key: "api_key_env", type: "string", label: "API key env var", value: "",
            hint: "The name of an environment variable."},
        ]},
    ]},
  ],
};

function mount() {
  const source = fs.readFileSync(path.join(__dirname, "..", "settings.js"), "utf8");
  const root = new El("div");
  const document = {
    createElement: (tag) => new El(tag),
    createTextNode: (text) => {
      const node = new El("#text");
      node.textContent = String(text);
      return node;
    },
    getElementById: () => null,
    body: new El("body"),
    activeElement: null,
  };
  const context = vm.createContext({
    document,
    navigator: {},
    console,
    setTimeout: () => 0,
    clearTimeout: () => {},
    getComputedStyle: () => ({color: "rgb(0,0,0)"}),
    window: {},
  });
  vm.runInContext(source, context);
  const view = context.window.TesseraSettings.mount({
    api: async () => settingsData,
    element: (tag, cls, text) => {
      const node = new El(tag);
      if (cls) node.className = cls;
      if (text !== undefined) node.textContent = String(text);
      return node;
    },
    root,
    status: new El("p"),
    restartButton: new El("button"),
    onRestarted() {},
    onAppearance() {},
    onApproval() {},
    onChanged() {},
  });
  return {root, view};
}

test("model fields put the tier and disclosure on the label line", async () => {
  const {root, view} = mount();
  await view.load();
  clickText(root, "Models");
  const chat = rowFor(root, "Chat model");
  const line = chat.label.parent;
  assert.equal(line.className, "settings-label-line");
  assert.ok(line.children.some((node) => node.textContent === "What goes here"));
  assert.ok(line.children.some((node) => node.textContent === "Required"));
  const control = chat.row.children.find((node) => node.className === "set-control");
  const hint = chat.row.children.find((node) => node.className === "set-hint");
  assert.ok(chat.row.children.indexOf(line) < chat.row.children.indexOf(control));
  assert.ok(chat.row.children.indexOf(control) < chat.row.children.indexOf(hint));
  assert.equal(hint.hidden, true);
  const failover = rowFor(root, "Failover");
  const failoverLine = failover.label.parent;
  assert.ok(failoverLine.children.some((node) => node.textContent === "Optional"));
  assert.equal(failoverLine.children.some((node) => node.textContent === "What goes here"), false);
  const failoverHint = failover.row.children.find((node) => node.className === "set-hint");
  const failoverControl = failover.row.children.find((node) => node.className === "set-control");
  assert.equal(failoverHint.hidden, false);
  assert.equal(failoverHint.textContent, "Used when the chat model is unreachable.");
  assert.ok(failover.row.children.indexOf(failoverLine) < failover.row.children.indexOf(failoverControl));
  assert.ok(failover.row.children.indexOf(failoverControl) < failover.row.children.indexOf(failoverHint));
});

test("endpoint add form puts one help line under each field and Find models under Model", async () => {
  const {root, view} = mount();
  await view.load();
  clickText(root, "Endpoints");
  clickText(root, "Add endpoint");
  for (const [label, help] of [
    ["Name", "A short name. Spaces are saved as dashes."],
    ["Base URL", "The server address, with http:// and the port."],
    ["Model", "The model id the server expects, such as llama-3.1-8b-demo."],
  ]) {
    const field = rowFor(root, label);
    const hint = field.row.children.find((node) => node.classNames().includes("set-hint"));
    const control = field.row.children.find((node) => node.className === "set-control");
    assert.equal(field.label.parent.className, "settings-label-line");
    assert.equal(hint.hidden, false);
    assert.equal(hint.textContent, help);
    assert.ok(field.row.children.indexOf(field.label.parent) < field.row.children.indexOf(control));
    assert.ok(field.row.children.indexOf(control) < field.row.children.indexOf(hint));
  }
  const model = rowFor(root, "Model");
  const hint = model.row.children.find((node) => node.classNames().includes("set-hint"));
  const discover = model.row.children.find((node) => node.className === "set-discover");
  assert.ok(model.row.children.indexOf(hint) < model.row.children.indexOf(discover));
  assert.equal(discover.children[0].textContent, "Find models");
  const choices = discover.children.find((node) => node.className === "set-model-choices");
  assert.ok(discover.children.indexOf(discover.children[0]) < discover.children.indexOf(choices));
});

test("model edit keeps help under the control and actions at the end", async () => {
  const {root, view} = mount();
  await view.load();
  clickText(root, "Models");
  clickText(root, "Edit");
  const chat = rowFor(root, "Use for chat");
  const line = chat.label.parent;
  assert.equal(line.className, "settings-check-line");
  assert.equal(line.children[0].tag, "input");
  assert.equal(line.children[0].type, "checkbox");
  assert.equal(line.children[1], chat.label);
  const fallback = rowFor(root, "API key fallback");
  const fallbackLine = fallback.label.parent;
  const fallbackHelp = fallback.row.children.find((node) => node.className === "set-hint");
  assert.equal(fallbackLine.children[0].type, "checkbox");
  assert.equal(fallbackHelp.textContent, "Off, a CLI login never uses an API key. On, a failed login may use the fallback env var.");
  assert.equal(fallbackHelp.hidden, false);
  assert.ok(fallback.row.children.indexOf(fallbackLine) < fallback.row.children.indexOf(fallbackHelp));
  const cap = rowFor(root, "Max concurrent requests");
  const capLine = cap.label.parent;
  assert.ok(capLine.children.some((node) => node.textContent === "What goes here"));
  const control = cap.row.children.find((node) => node.className === "set-control");
  const fact = cap.row.children.find((node) => node.classNames().includes("set-fact"));
  const hint = cap.row.children.find((node) => node.className === "set-hint");
  assert.ok(cap.row.children.indexOf(capLine) < cap.row.children.indexOf(control));
  assert.ok(cap.row.children.indexOf(control) < cap.row.children.indexOf(fact));
  assert.ok(cap.row.children.indexOf(fact) < cap.row.children.indexOf(hint));
  assert.equal(hint.hidden, true);
  assert.match(fact.textContent, /In effect/);
  const form = all(root).find((node) => node.tag === "form");
  const actions = form.children[form.children.length - 1];
  assert.ok(actions.classNames().includes("record-actions"));
  assert.equal(actions.children[0].textContent, "Cancel");
  assert.equal(actions.children[1].textContent, "Save");
});

test("a keychain row shows the existence phrase once", async () => {
  const {root, view} = mount();
  await view.load();
  clickText(root, "API keys");
  assert.ok(all(root).some((node) => node.textContent === "Keychain item: slot"));
  assert.ok(all(root).some((node) => node.textContent === "Keychain item missing"));
  assert.equal(all(root).some((node) => node.textContent === "Keychain"), false);
  assert.equal(all(root).some((node) => node.textContent === "Configured"), false);
  assert.equal(all(root).some((node) => String(node.textContent).includes("Not recorded")), false);
});

test("the models row shows the full id and one credential line", async () => {
  const {root, view} = mount();
  await view.load();
  clickText(root, "Models");
  assert.ok(all(root).some((node) => node.textContent === "llama-3.1-8b-demo"));
  assert.ok(all(root).some((node) => node.textContent === "Credential: none"));
  assert.equal(all(root).some((node) => String(node.textContent).includes("Credential in use")), false);
  const summaries = all(root).filter((node) => node.tag === "summary" && node.textContent === "What goes here");
  assert.equal(summaries.length, 1);
  const models = all(root).find((node) => node.tag === "button" && node.textContent === "Models");
  const accounts = all(root).find((node) => node.tag === "button" && node.textContent === "Accounts");
  assert.equal(models.className, accounts.className);
});

test("accounts and endpoints each use one plain row", async () => {
  const {root, view} = mount();
  await view.load();
  clickText(root, "Accounts");
  assert.ok(all(root).some((node) => node.textContent === "Run models locally"));
  assert.ok(all(root).some((node) => node.textContent === "A model server on this machine."));
  assert.ok(all(root).some((node) => node.textContent === "xAI Grok sign-in."));
  assert.equal(all(root).some((node) => node.tag === "button" && node.textContent === "sign-in"), false);
  clickText(root, "Endpoints");
  const labels = all(root).filter((node) => node.tag === "button").map((node) => node.textContent);
  assert.ok(labels.includes("Add endpoint"));
  assert.equal(labels.includes("Add an endpoint"), false);
  assert.equal(labels.includes("Add a local server"), false);
  assert.equal(labels.includes("Add model"), false);
});
