"use strict";
// The approval mode the core stores is still yolo. What a person reads on the
// button is a label: Unleashed, unless the visible easter-egg setting is on.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const app = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");
const settingsSource = fs.readFileSync(path.join(__dirname, "..", "settings.js"), "utf8");
const labelSource = fs.readFileSync(path.join(__dirname, "..", "unleashed-label.js"), "utf8");

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
    this._text = "";
    this.listeners = {};
    this.parent = null;
    this.style = {};
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
  append(...nodes) {
    for (const node of nodes) this.insertBefore(node, null);
  }
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
  remove() {
    if (!this.parent) return;
    this.parent.children.splice(this.parent.children.indexOf(this), 1);
    this.parent = null;
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  async fire(type) {
    for (const fn of this.listeners[type] || []) await fn({preventDefault() {}});
  }
  classNames() {
    return String(this.className || this.attributes.class || "").split(/\s+/).filter(Boolean);
  }
  querySelector(selector) {
    const key = selector.match(/^\[data-key="([^"]+)"\]$/);
    const role = selector.match(/^\[role="([^"]+)"\]$/);
    const cls = selector.match(/^\.([A-Za-z0-9_-]+)$/);
    for (const child of this.children) {
      if (selector === child.tag) return child;
      if (key && child.dataset.key === key[1]) return child;
      if (role && child.getAttribute("role") === role[1]) return child;
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
        if (selector === "svg" && child.tag === "svg") found.push(child);
        if (selector.startsWith(".") && child.classNames().includes(selector.slice(1))) found.push(child);
        visit(child);
      }
    };
    visit(this);
    return found;
  }
}

function labelApi() {
  const document = {
    createElement: (tag) => new El(tag),
    createElementNS: (_ns, tag) => new El(tag),
  };
  const context = {document, window: {}};
  vm.createContext(context);
  vm.runInContext(labelSource, context);
  return context.window.TesseraUnleashed;
}

test("the static approval control says Unleashed and ships no cartoon", () => {
  assert.match(html, /id="approval-yolo"[^>]*>Unleashed</);
  assert.doesNotMatch(html, /id="approval-yolo"[^>]*>YOLO</);
  assert.match(html, /Unleashed mode is on/);
  assert.doesNotMatch(html, /YOLO mode is on/);
  assert.doesNotMatch(html, /f-it-mark/);
  assert.match(html, /src="\/static\/unleashed-label\.js"/);
  assert.match(app, /TesseraUnleashed\.paint/);
});

test("the button reads Unleashed and draws nothing until the setting is on", () => {
  const api = labelApi();
  const button = new El("button");
  for (const ui of [undefined, null, {}, {easter_egg_f_it: false}, {easter_egg_f_it: "true"}, {easter_egg_f_it: 1}]) {
    api.paint(button, ui);
    assert.equal(button.textContent, "Unleashed");
    assert.equal(button.querySelector("svg"), null);
    assert.equal(button.querySelector("img"), null);
    assert.equal(button.querySelectorAll(".f-it-mark").length, 0);
    assert.equal(button.textContent.includes("F-it"), false);
  }
  api.paint(button, {easter_egg_f_it: true});
  assert.equal(button.querySelector(".approval-label").textContent, "F-it");
  const image = button.querySelector("img");
  assert.ok(image, "the cartoon is the shared svg file");
  assert.equal(image.getAttribute("src"), api.MARK_URL);
  assert.equal(image.getAttribute("alt"), api.ALT);
  assert.equal(image.className, "f-it-mark");
  assert.equal(button.querySelector("svg"), null);
  const svg = fs.readFileSync(path.join(__dirname, "..", "brand", "f-it-gremlin.svg"), "utf8");
  assert.match(svg, /class="f-it-gremlin"/);
  assert.match(svg, /class="f-it-letters"/);
  assert.match(svg, new RegExp(api.ALT));
  assert.doesNotMatch(labelSource, /M44 50/);
  api.paint(button, {easter_egg_f_it: false});
  assert.equal(button.textContent, "Unleashed");
  assert.equal(button.querySelector("svg"), null);
  assert.equal(button.querySelector("img"), null);
  assert.equal(button.textContent.includes("F-it"), false);
});

test("F-it alone turns on the gremlin icon set", () => {
  const api = labelApi();
  assert.equal(api.activeMark(undefined), "cairn");
  assert.equal(api.activeMark({mark: "cairn", easter_egg_f_it: true}), "gremlin");
  assert.equal(api.activeMark({mark: "gremlin", easter_egg_f_it: true}), "gremlin");
  assert.equal(api.packOn({mark: "cairn", easter_egg_f_it: true}), true);
  assert.equal(api.packOn({mark: "gremlin", easter_egg_f_it: false}), false);
  assert.equal(api.packOn({mark: "gremlin", easter_egg_f_it: "true"}), false);
  assert.equal(api.activeMark({easter_egg_f_it: false}), "cairn");
});

test("the icon picker stays a plain logo choice and never gates the pack", () => {
  const api = labelApi();
  assert.equal(api.activeMark({mark: "gremlin", easter_egg_f_it: false}), "gremlin");
  assert.equal(api.activeMark({mark: "gremlin"}), "gremlin");
  assert.equal(api.activeMark({mark: "nope", easter_egg_f_it: false}), "cairn");
  assert.equal(api.packOn({mark: "cairn", easter_egg_f_it: false}), false);
  assert.equal(api.iconFor("cairn"), null);
  assert.equal(api.iconFor("gremlin").href, "/static/brand/gremlin-logo.svg");
  assert.equal(api.LOGO_URL, "/static/brand/gremlin-logo.svg");
  assert.notEqual(api.LOGO_URL, api.MARK_URL);
  assert.equal(api.iconFor("gremlin").type, "image/svg+xml");
});

test("Settings lists both icon sets and writes the chosen one", async () => {
  const appearances = [];
  const writes = [];
  const settingsData = {sections: [{id: "appearance", label: "Appearance", fields: [
    {id: "ui.mark", key: "mark", label: "Icon and logo", type: "enum",
      options: [{value: "cairn", label: "Tessera"}, {value: "gremlin", label: "Gremlin"}],
      value: "cairn", write: "set", apply: "live"},
    {id: "ui.easter_egg_f_it", key: "easter_egg_f_it", label: "F-it button", type: "bool",
      value: false, write: "set", apply: "live"},
  ]}]};
  const document = {
    createElement: (tag) => new El(tag),
    createTextNode: (text) => { const node = new El("#text"); node.textContent = String(text); return node; },
    getElementById: () => null,
    body: new El("body"),
    addEventListener() {},
  };
  const root = new El("div");
  const context = vm.createContext({
    document, console,
    getComputedStyle: () => ({color: "rgb(0,0,0)"}),
    setTimeout: () => 0, clearTimeout: () => {}, window: {},
  });
  vm.runInContext(settingsSource, context);
  const view = context.window.TesseraSettings.mount({
    api: async (url, method, body) => {
      if (method === "POST") {
        writes.push(body);
        const field = settingsData.sections[0].fields.find((item) => item.id === body.id);
        field.value = body.value;
        return {changed: true, applied: "live", settings: settingsData};
      }
      return settingsData;
    },
    element: (tag, cls, text) => {
      const node = new El(tag);
      if (cls) node.className = cls;
      if (text !== undefined) node.textContent = String(text);
      return node;
    },
    root, status: new El("p"), restartButton: new El("button"),
    onAppearance: (patch) => appearances.push(patch),
  });
  await view.load();
  assert.ok(root.textContent.includes("Icon and logo"));
  assert.ok(root.textContent.includes("Tessera"));
  assert.ok(root.textContent.includes("Gremlin"));
  const select = root.querySelector('[data-key="mark"]');
  assert.equal(select.tag, "select");
  assert.equal(select.value, "cairn");
  const previews = root.querySelectorAll(".mark-preview");
  assert.equal(previews.length, 2);
  select.value = "gremlin";
  await select.fire("change");
  assert.equal(writes.at(-1).id, "ui.mark");
  assert.equal(writes.at(-1).value, "gremlin");
  assert.equal(appearances.some((patch) => patch.mark === "gremlin"), true);
});

test("the Settings checkbox shows the real value and only then asks for F-it", async () => {
  const appearances = [];
  const writes = [];
  const settingsData = {sections: [{id: "appearance", label: "Appearance", fields: [
    {id: "ui.easter_egg_f_it", key: "easter_egg_f_it", label: "F-it button", type: "bool",
      value: false, write: "set", apply: "live", tier: "optional",
      hint: "When on, the Unleashed button reads F-it and shows the cartoon."},
    {id: "permissions.mode", key: "mode", label: "Mode", type: "enum",
      options: [{value: "safe", label: "Safe"}, {value: "yolo", label: "Unleashed"}],
      value: "yolo", write: "approval", apply: "live"},
  ]}]};
  const document = {
    createElement: (tag) => new El(tag),
    createTextNode: (text) => { const node = new El("#text"); node.textContent = String(text); return node; },
    getElementById: () => null,
    body: new El("body"),
    addEventListener() {},
  };
  const root = new El("div");
  const context = vm.createContext({
    document, console,
    getComputedStyle: () => ({color: "rgb(0,0,0)"}),
    setTimeout: () => 0, clearTimeout: () => {}, window: {},
  });
  vm.runInContext(settingsSource, context);
  const view = context.window.TesseraSettings.mount({
    api: async (url, method, body) => {
      if (method === "POST") {
        writes.push(body);
        const field = settingsData.sections[0].fields.find((item) => item.id === body.id);
        field.value = body.value;
        return {changed: true, applied: "live", settings: settingsData};
      }
      return settingsData;
    },
    element: (tag, cls, text) => {
      const node = new El(tag);
      if (cls) node.className = cls;
      if (text !== undefined) node.textContent = String(text);
      return node;
    },
    root, status: new El("p"), restartButton: new El("button"),
    onAppearance: (patch) => appearances.push(patch),
  });
  await view.load();
  assert.ok(root.textContent.includes("F-it button"));
  assert.ok(root.textContent.includes("Unleashed"));
  assert.equal(root.textContent.includes("YOLO"), false);
  const input = root.querySelector('[data-key="easter_egg_f_it"]');
  assert.equal(input.type, "checkbox");
  assert.equal(input.checked, false);
  input.checked = true;
  await input.fire("change");
  assert.equal(writes.at(-1).id, "ui.easter_egg_f_it");
  assert.equal(writes.at(-1).value, true);
  assert.equal(appearances.some((patch) => patch.easter_egg_f_it === true), true);
  assert.equal(root.querySelector('[data-key="easter_egg_f_it"]').checked, true);
});
