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
  focus() { if (this.document) this.document.activeElement = this; }
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
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
  },
  sections: [
    {id: "models", label: "Models", fields: [
      {id: "roles.main", type: "enum", label: "Chat model", value: "", hint: longHint,
        options: [{value: "local-test", label: "local-test"}]},
      {id: "roles.failover", type: "enum", label: "Failover", value: "",
        hint: "Used when the chat model is unreachable.",
        options: [{value: "", label: "Off"}]},
    ]},
    {id: "voice", label: "Voice", fields: [
      {id: "voice.pipe", type: "display", label: "Pipe", value: "local"},
      {id: "voice.account", type: "enum", label: "Account", value: "xai",
        options: [{value: "xai", label: "xAI Grok"}], hint: "The account that mints the voice session.",
        hidden_when: {"voice.pipe": "local"}},
      {id: "voice.stt.base_url", type: "string", label: "Speech-to-text endpoint", value: "",
        hint: "The base URL of a local server."},
    ]},
    {id: "credentials", label: "Credentials", fields: [
      {id: "credentials", type: "list-of-records", label: "Credentials",
        actions: ["add", "edit", "remove"], records: [{
          name: "slot", title: "slot", pill: "Keychain item missing",
          line: "Keychain item: slot", added: "Not recorded", actions: ["edit", "remove"],
        }], blank: []},
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
          {key: "account", type: "enum", label: "Account", value: "other",
            options: [{value: "other", label: "Custom endpoint"}],
            hint: "A sign-in account supplies the endpoint and the credential."},
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

function mount(api) {
  const source = fs.readFileSync(path.join(__dirname, "..", "settings.js"), "utf8");
  const root = new El("div");
  const document = {
    createElement: (tag) => { const node = new El(tag); node.document = document; return node; },
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
    api: api || (async () => settingsData),
    element: (tag, cls, text) => {
      const node = new El(tag);
      node.document = document;
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
  return {root, view, document};
}

test("model fields put the tier and disclosure on the label line", async () => {
  const {root, view} = mount();
  await view.load();
  clickText(root, "Models");
  const chat = rowFor(root, "Chat model");
  const line = chat.label.parent;
  assert.equal(line.className, "settings-label-line");
  assert.ok(line.children.some((node) => node.textContent === "Details"));
  assert.ok(line.children.some((node) => node.textContent === "Required"));
  const control = chat.row.children.find((node) => node.className === "set-control");
  const hint = chat.row.children.find((node) => node.className === "set-hint");
  assert.ok(chat.row.children.indexOf(line) < chat.row.children.indexOf(control));
  assert.ok(chat.row.children.indexOf(control) < chat.row.children.indexOf(hint));
  assert.equal(hint.hidden, true);
  const failover = rowFor(root, "Failover");
  const failoverLine = failover.label.parent;
  assert.ok(failoverLine.children.some((node) => node.textContent === "Optional"));
  assert.equal(failoverLine.children.some((node) => node.textContent === "Details"), false);
  const failoverHint = failover.row.children.find((node) => node.className === "set-hint");
  const failoverControl = failover.row.children.find((node) => node.className === "set-control");
  assert.equal(failoverHint.hidden, false);
  assert.equal(failoverHint.textContent, "Used when the chat model is unreachable.");
  assert.ok(failover.row.children.indexOf(failoverLine) < failover.row.children.indexOf(failoverControl));
  assert.ok(failover.row.children.indexOf(failoverControl) < failover.row.children.indexOf(failoverHint));
});

test("add model asks which account first and keeps help under each field", async () => {
  const {root, view} = mount();
  await view.load();
  clickText(root, "Models");
  clickText(root, "Add model");
  const account = rowFor(root, "Account");
  assert.ok(account, "the account picker is the first question");
  const form = all(root).find((node) => node.tag === "form");
  assert.equal(form.children[0], account.row);
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
  // A form never folds its help: the sentence that says what to type stays on
  // the page beside the field.
  assert.equal(capLine.children.some((node) => node.textContent === "Details"), false);
  const control = cap.row.children.find((node) => node.className === "set-control");
  const fact = cap.row.children.find((node) => node.classNames().includes("set-fact"));
  const hint = cap.row.children.find((node) => node.className === "set-hint");
  assert.ok(cap.row.children.indexOf(capLine) < cap.row.children.indexOf(control));
  assert.ok(cap.row.children.indexOf(control) < cap.row.children.indexOf(fact));
  assert.ok(cap.row.children.indexOf(fact) < cap.row.children.indexOf(hint));
  assert.equal(hint.hidden, false);
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
  clickText(root, "Saved credentials");
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
  const summaries = all(root).filter((node) => node.tag === "summary" && node.textContent === "Details");
  assert.equal(summaries.length, 1);
  const models = all(root).find((node) => node.tag === "button" && node.textContent === "Models");
  const accounts = all(root).find((node) => node.tag === "button" && node.textContent === "Accounts");
  assert.equal(models.className, accounts.className);
});

test("accounts use one plain row and offer no endpoint add", async () => {
  const {root, view} = mount();
  await view.load();
  clickText(root, "Accounts");
  assert.ok(all(root).some((node) => node.textContent === "Run models locally"));
  assert.ok(all(root).some((node) => node.textContent === "A model server on this machine."));
  assert.ok(all(root).some((node) => node.textContent === "xAI Grok sign-in."));
  const labels = all(root).filter((node) => node.tag === "button").map((node) => node.textContent);
  assert.equal(labels.includes("Add endpoint"), false);
  assert.equal(labels.includes("Add model"), false);
});

test("a duplex control hides itself under the local speech pipe", async () => {
  const {root, view} = mount();
  await view.load();
  clickText(root, "Voice");
  const account = rowFor(root, "Account");
  assert.equal(account.row.hidden, true, "the duplex account does not apply to the local pipe");
  const endpoint = rowFor(root, "Speech-to-text endpoint");
  assert.equal(endpoint.row.hidden, false, "the local endpoint is the point of that pipe");
});


function change(input, value) {
  input.value = value;
  for (const fn of input.listeners.input || []) fn();
}

const settle = () => new Promise(resolve => setImmediate(resolve));

test("search filters sections, retains focus and reports no matches", async () => {
  const {root, view, document} = mount();
  await view.load();
  let search = all(root).find(n => n.type === "search");
  search.focus();
  search.selectionStart = search.selectionEnd = 5;
  change(search, "Voice");
  search = all(root).find(n => n.type === "search");
  assert.equal(document.activeElement, search);
  assert.equal(search.selectionStart, 5);
  const sections = () => all(root).filter(n => n.classNames().includes("settings-block") && !n.hidden);
  assert.deepEqual(sections().map(n => n.dataset.section), ["voice"]);
  change(search, "zzzz-no-match-zzzz");
  assert.equal(sections().length, 0);
  assert.equal(root.querySelector(".settings-empty").hidden, false);
  assert.equal(all(root).find(n => n.type === "search"), search, "subsequent keystrokes retain the node");
  change(search, "");
  assert.ok(sections().length > 1);
});

test("credential removal offers cancel and submits only after confirmation", async () => {
  const writes = [];
  const {root, view} = mount(async (url, method, body) => {
    if (method === "POST") writes.push(body);
    return method === "POST" ? {settings: settingsData} : settingsData;
  });
  await view.load();
  clickText(root, "Saved credentials");
  const remove = () => all(root).find(n => n.attributes["aria-label"] === "Remove slot").click();
  remove();
  assert.equal(writes.length, 0);
  clickText(root, "Cancel");
  assert.equal(all(root).some(n => n.tag === "button" && n.textContent === "Remove now"), false);
  remove();
  clickText(root, "Remove now");
  await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(writes)), [{id: "credentials", action: "remove", name: "slot"}]);
});

test("sidebar and mobile section changes keep drafts until explicit Cancel", async () => {
  const {root, view} = mount();
  await view.load();
  clickText(root, "Models");
  clickText(root, "Edit");
  let input = all(root).find(n => n.dataset.key === "model");
  change(input, "unfinished-model");
  assert.equal(view.hasEdits(), true);
  clickText(root, "Voice");
  assert.equal(view.hasEdits(), true, "hidden drafts also block background reloads");
  const section = all(root).find(n => n.id === "settings-section-select");
  section.value = "models";
  section.listeners.change[0]();
  input = all(root).find(n => n.dataset.key === "model");
  assert.equal(input.value, "unfinished-model");
  clickText(root, "Cancel");
  clickText(root, "Voice");
  clickText(root, "Models");
  assert.equal(all(root).some(n => n.tag === "form"), false);
  assert.equal(view.hasEdits(), false);
});

test("autosave restores the focused control after dependent settings render", async () => {
  const {root, view, document} = mount(async (url, method) => method === "POST" ? {settings: settingsData, changed: true} : settingsData);
  await view.load();
  clickText(root, "Models");
  const input = all(root).find(n => n.id === "setting-roles_main");
  input.focus();
  input.value = "local-test";
  for (const fn of input.listeners.change) fn();
  await settle();
  const current = all(root).find(n => n.id === "setting-roles_main");
  assert.notEqual(current, input);
  assert.equal(document.activeElement, current);
});

test("a delayed autosave preserves newer typing and reports errors beside the field", async () => {
  let finish;
  let fail = false;
  const {root, view, document} = mount(async (url, method) => {
    if (method !== "POST") return settingsData;
    if (fail) throw new Error("Server refused the value");
    return new Promise(resolve => { finish = resolve; });
  });
  await view.load();
  clickText(root, "Voice");
  const field = () => all(root).find(n => n.id === "setting-voice_stt_base_url");
  const input = field();
  input.focus();
  change(input, "http://first.example");
  for (const fn of input.listeners.change) fn();
  assert.equal(all(root).find(n => n.id === input.id + "-notice").textContent, "Saving…");
  change(input, "http://newer.example");
  input.selectionStart = input.selectionEnd = 12;
  finish({settings: settingsData, changed: true});
  await settle();
  assert.equal(field().value, "http://newer.example");
  assert.equal(document.activeElement, field());
  assert.equal(field().selectionStart, 12);
  fail = true;
  for (const fn of field().listeners.change) fn();
  await settle();
  assert.equal(all(root).find(n => n.id === input.id + "-notice").textContent, "Server refused the value");
});

test("search reveals matching advanced controls in the current settings shell", async () => {
  const data = structuredClone(settingsData);
  data.sections.find(s => s.id === "voice").fields.push({
    id: "voice.advanced", type: "string", label: "Advanced speech tuning", value: "", tier: "advanced",
  });
  const {root, view} = mount(async () => data);
  await view.load();
  change(all(root).find(n => n.type === "search"), "Advanced speech tuning");
  const section = all(root).find(n => n.dataset.section === "voice" && n.classNames().includes("settings-block"));
  assert.equal(section.hidden, false);
  assert.equal(section.querySelector(".settings-advanced").open, true);
});
