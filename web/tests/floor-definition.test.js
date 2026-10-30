const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {resolve} = require("node:path");
const {test} = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.listeners = {};
    this.attrs = {};
    this.className = "";
    this.textContent = "";
    this.id = "";
    this.type = "";
    this.value = "";
    this.checked = false;
    this.parent = null;
  }
  append(...nodes) {
    for (const node of nodes) {
      node.parent = this;
      this.children.push(node);
    }
  }
  replaceChildren(...nodes) {
    this.children = [];
    this.append(...nodes);
  }
  setAttribute(key, value) { this.attrs[key] = String(value); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async fire(type) {
    for (const fn of this.listeners[type] || []) await fn({preventDefault() {}});
  }
  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter((child) => child !== this);
  }
  querySelector(selector) {
    const id = selector.match(/^#([A-Za-z0-9_-]+)$/);
    if (id && this.id === id[1]) return this;
    for (const child of this.children) {
      const found = child.querySelector(selector);
      if (found) return found;
    }
    return null;
  }
}

function walk(node, visit) {
  visit(node);
  for (const child of node.children || []) walk(child, visit);
}

function textOf(node) {
  const parts = [];
  walk(node, (item) => { if (item.textContent) parts.push(item.textContent); });
  return parts.join("\n");
}

const SIGNALS = ["host", "board_card", "notice", "session", "engine", "endpoint", "timer", "agenda_item", "session_card"];

function view() {
  const sources = {};
  const signals = {};
  for (const name of SIGNALS) {
    sources[name] = true;
    signals[name] = {label: name, summary: "", fields: {}};
  }
  return {
    definition: {
      compose: {
        world_view: "Warm house.", world_view_max_chars: 2000, wake_score: 1, stale_seconds: 21600,
        stale_label: "six hours", disk_free_percent_below: 10, missing_priority: 100,
        footer: "", pressure_intro: "", coordination_paragraph: "", quiet_paragraph: "", world_view_intro: "",
      },
      rules: [],
      sources,
    },
    source: "file",
    signals,
    audit: [],
    errors: [{source: "approval-status", error_type: "RuntimeError", message: "approval unread"}],
    approval_mode: "unknown",
    last_wake: {},
    preview: null,
  };
}

test("each pressure source has a switch and a refused import stays visible", async () => {
  const source = readFileSync(resolve(__dirname, "..", "floor-definition.js"), "utf8");
  const context = {window: {}};
  vm.runInNewContext(source, context);
  const current = view();
  const posts = [];
  const status = new Element("p");
  const host = new Element("div");
  async function api(path, method, body) {
    if (method === "POST" && String(path).endsWith("/import")) {
      throw new Error("Floor definition rule 'broken' field 'score': must be a number");
    }
    if (method === "PUT") {
      posts.push(body);
      return current;
    }
    return current;
  }
  function element(tag, className, text) {
    const node = new Element(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }
  context.window.TesseraFloor.mount(host, {api, element, status});
  await new Promise((resolve) => setTimeout(resolve, 0));
  const shown = textOf(host);
  assert.match(shown, /Approval status could not be read/);
  assert.match(shown, /approval-status failed: RuntimeError: approval unread/);
  assert.match(shown, /Pressure sources/);
  const switches = [];
  walk(host, (node) => {
    if (node.tag === "input" && node.type === "checkbox" && node.attrs["data-source"]) switches.push(node);
  });
  assert.deepEqual(switches.map((node) => node.attrs["data-source"]), SIGNALS);
  switches.find((node) => node.attrs["data-source"] === "host").checked = false;
  const save = [];
  walk(host, (node) => { if (node.tag === "button" && node.textContent === "Save floor definition") save.push(node); });
  await save[0].fire("click");
  assert.equal(posts.at(-1).definition.sources.host, false);
  assert.equal(posts.at(-1).definition.sources.board_card, true);
  const transfer = host.querySelector("#floor-transfer");
  transfer.value = "{";
  const imports = [];
  walk(host, (node) => { if (node.tag === "button" && node.textContent === "Import") imports.push(node); });
  await imports[0].fire("click");
  assert.match(status.textContent, /field 'score'/);
  assert.match(textOf(host), /field 'score'/);
  assert.equal(host.querySelector("#floor-import-error").textContent, status.textContent);
});

test("every rule field is editable and saved", async () => {
  const source = readFileSync(resolve(__dirname, "..", "floor-definition.js"), "utf8");
  const context = {window: {}};
  vm.runInNewContext(source, context);
  const current = view();
  current.definition.rules = [{
    id: "sample", name: "Sample", enabled: true, signal: "board_card", when: "true",
    template: "line", score: 1.3, wake: true, pressure: true, emit: "offers", sort: "priority",
    name_limit: 5, name_field: "ordinal", owner: "", note: "", hold_seconds: null,
    missing_priority: 40, title_priority_pattern: "P([0-9]+)", guard: false, last_fired: null,
  }];
  const posts = [];
  const status = new Element("p");
  const host = new Element("div");
  async function api(path, method, body) {
    if (method === "PUT") {
      posts.push(body);
      return current;
    }
    return current;
  }
  function element(tag, className, text) {
    const node = new Element(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }
  context.window.TesseraFloor.mount(host, {api, element, status});
  await new Promise((resolve) => setTimeout(resolve, 0));
  const shown = textOf(host);
  for (const label of ["Emit", "Sort", "Name field", "Missing priority", "Title priority pattern", "Pressure"]) {
    assert.match(shown, new RegExp(label));
  }
  const save = [];
  walk(host, (node) => { if (node.tag === "button" && node.textContent === "Save floor definition") save.push(node); });
  await save[0].fire("click");
  const rule = posts.at(-1).definition.rules[0];
  assert.equal(rule.emit, "offers");
  assert.equal(rule.sort, "priority");
  assert.equal(rule.name_field, "ordinal");
  assert.equal(rule.missing_priority, 40);
  assert.equal(rule.title_priority_pattern, "P([0-9]+)");
  assert.equal(rule.pressure, true);
  assert.equal(rule.enabled, true);
  assert.equal(rule.wake, true);
});

for (const removedIndex of [0, 1, 2]) {
  test(`deleting rule ${removedIndex + 1} preserves unsaved edits elsewhere in the floor definition`, async () => {
    const source = readFileSync(resolve(__dirname, "..", "floor-definition.js"), "utf8");
    const context = {window: {}};
    vm.runInNewContext(source, context);
    const current = view();
    current.definition.rules = [0, 1, 2].map(index => ({
      id: `rule-${index}`, name: `Rule ${index}`, enabled: true, signal: "host", when: "true",
      template: "Original template", score: 1, wake: true, pressure: true, emit: "each", sort: "input",
      name_limit: 5, name_field: "label", owner: "", note: "", hold_seconds: null,
      missing_priority: null, title_priority_pattern: "", guard: false, last_fired: null,
    }));
    const saved = [];
    const host = new Element("div");
    const status = new Element("p");
    function element(tag, className, text) {
      const node = new Element(tag);
      if (className) node.className = className;
      if (text != null) node.textContent = String(text);
      return node;
    }
    async function api(path, method, body) {
      if (method === "PUT") saved.push(JSON.parse(JSON.stringify(body.definition)));
      return current;
    }
    function find(root, predicate) {
      const nodes = [];
      walk(root, node => { if (predicate(node)) nodes.push(node); });
      return nodes;
    }
    const input = (root, label) => find(root, node => node.attrs["aria-label"] === label)[0];
    const buttons = label => find(host, node => node.tag === "button" && node.textContent === label);
    context.window.TesseraFloor.mount(host, {api, element, status});
    await new Promise(resolve => setImmediate(resolve));
    const survivorIndex = (removedIndex + 1) % 3;
    const survivorId = `rule-${survivorIndex}`;
    input(host, "World view").value = "Keep this unfinished world view.";
    host.querySelector("#floor-source-host").checked = false;
    const cards = find(host, node => node.tag === "fieldset");
    input(cards[survivorIndex], "Rule name").value = "Edited surviving rule";
    input(cards[survivorIndex], "Template").value = "Keep this unfinished template.";

    await buttons("Delete")[removedIndex].fire("click");

    assert.equal(saved.length, 0, "deletion stays a draft until Save");
    assert.equal(input(host, "World view").value, "Keep this unfinished world view.");
    assert.equal(host.querySelector("#floor-source-host").checked, false);
    const remaining = find(host, node => node.tag === "fieldset");
    const survivor = remaining.find(card => card.children.some(node => node.tag === "legend" && node.textContent === survivorId));
    assert.equal(input(survivor, "Rule name").value, "Edited surviving rule");
    assert.equal(input(survivor, "Template").value, "Keep this unfinished template.");
    await buttons("Save floor definition")[0].fire("click");
    assert.deepEqual(saved[0].rules.map(rule => rule.id), [0, 1, 2].filter(index => index !== removedIndex).map(index => `rule-${index}`));
    assert.equal(saved[0].compose.world_view, "Keep this unfinished world view.");
    assert.equal(saved[0].sources.host, false);
    assert.equal(saved[0].rules.find(rule => rule.id === survivorId).template, "Keep this unfinished template.");
  });
}
