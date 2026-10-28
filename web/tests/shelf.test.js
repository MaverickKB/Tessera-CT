// A person answers what this agent can do, and what it will ask about,
// from the page alone.
//
// These run web/shelf.js against fixture payloads shaped exactly like the routes. The
// point is not that some text appears; it is that the page names the outcome per tool
// and says when the shelf is broken or off, instead of rendering an empty list that
// would read as "you have nothing".
const assert = require("node:assert/strict");
const {test} = require("node:test");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..", "..");

class El {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this._classes = new Set();
    this.dataset = {};
    this.hidden = false;
    this.textContent = "";
    this.attributes = {};
    this.listeners = {};
    this.open = false;
  }
  get className() { return [...this._classes].join(" "); }
  set className(value) { this._classes = new Set(String(value || "").split(/\s+/).filter(Boolean)); }
  get classList() {
    const classes = this._classes;
    return {
      add: (...names) => { for (const name of names) classes.add(name); },
      remove: (...names) => { for (const name of names) classes.delete(name); },
      contains: (name) => classes.has(name),
      toggle: (name, force) => {
        const on = force === undefined ? !classes.has(name) : Boolean(force);
        if (on) classes.add(name); else classes.delete(name);
        return on;
      },
    };
  }
  append(...items) { for (const item of items) if (item) this.children.push(item); }
  prepend(item) { this.children.unshift(item); }
  replaceChildren(...items) { this.children = items.filter(Boolean); }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, fn) { (this.listeners[name] = this.listeners[name] || []).push(fn); }
  querySelectorAll() { return []; }
  querySelector(selector) {
    if (selector === "input[type='search']") {
      return this.all.find((node) => node.attributes && node.attributes.type === "search") || null;
    }
    if (selector === "[aria-label='Skills directory to add']") {
      return this.all.find((node) => node.attributes && node.attributes["aria-label"] === "Skills directory to add") || null;
    }
    return null;
  }
  focus() {
    if (this.ownerDocument) this.ownerDocument.activeElement = this;
  }
  setSelectionRange(start, end) {
    this.selectionStart = start;
    this.selectionEnd = end;
  }
  get all() {
    const out = [];
    const walk = (node) => { out.push(node); for (const child of node.children) walk(child); };
    walk(this);
    return out;
  }
  get text() { return this.all.map((node) => node.textContent).join(" "); }
}

function mountWith(payloads) {
  const doc = {
    activeElement: null,
    createElement: (tag) => { const el = new El(tag); el.ownerDocument = doc; return el; },
    createElementNS: (ns, tag) => { const el = new El(tag); el.ownerDocument = doc; return el; },
    createTextNode: (text) => { const n = new El("#text"); n.textContent = String(text); return n; },
    querySelector: () => null,
    addEventListener() {},
    getElementById: () => null,
    body: new El("body"),
  };
  const context = {document: doc, window: {}, console, setTimeout(){return 0;}, clearTimeout(){}};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(ROOT, "web", "shelf.js"), "utf8"), context);
  const api = typeof payloads === "function" ? payloads : async (url) => {
    if (url in payloads) {
      const value = payloads[url];
      if (value instanceof Error) throw value;
      return value;
    }
    throw new Error("404 Not found");
  };
  const root = new El("div");
  root.ownerDocument = doc;
  const view = context.window.TesseraShelf.mount({api, root});
  return {view, root};
}

const SKILLS = {
  enabled: true,
  count: 2,
  note: "",
  skills: [
    {name: "a-real-skill", description: "Does a real thing", source: "user", dir: "/skills/a-real-skill", error: ""},
    {name: "broken-one", description: "", source: "user", dir: "/skills/broken-one", error: "SKILL.md must start with a --- frontmatter line"},
  ],
};

const TOOLS = {
  count: 3,
  risky: 1,
  policy: "deny_risky",
  source: "base registry (no session loaded)",
  tools: [
    {name: "read_file", description: "Read UTF-8 text.", risky: false, outcome: "runs", asks: false},
    {name: "shell", description: "Run a shell command.", risky: true, outcome: "refused", asks: false},
    {name: "write_file", description: "Write UTF-8 text.", risky: true, outcome: "asks", asks: true},
  ],
};

test("a healthy shelf and tool set answer the question", async () => {
  const {view, root} = mountWith({"/api/skills": SKILLS, "/api/tools": TOOLS});
  await view.load();
  const text = root.text;
  assert.ok(text.includes("a-real-skill"), "a skill name is missing");
  assert.ok(text.includes("Does a real thing"), "a skill description is missing");
  assert.ok(text.includes("Refused under this policy"), "a refused tool must say so");
  assert.ok(text.includes("Asks first"), "an asking tool must say so");
  assert.ok(text.includes("Runs without asking"), "a free tool must say so");
  assert.ok(text.includes("deny_risky"), "the live policy must be named");
  assert.ok(text.includes("base registry"), "the set's source must be named");
});

test("a broken skill is surfaced, not silently dropped", async () => {
  const {view, root} = mountWith({"/api/skills": SKILLS, "/api/tools": TOOLS});
  await view.load();
  const text = root.text;
  assert.ok(text.includes("broken-one"), "a broken skill must still be listed");
  assert.ok(text.includes("frontmatter"), "its error must be shown");
  assert.ok(/cannot be loaded/.test(text), "the page must say these cannot load");
});

test("an empty shelf says it is empty, not broken", async () => {
  const empty = {enabled: true, count: 0, note: "", skills: []};
  const {view, root} = mountWith({"/api/skills": empty, "/api/tools": TOOLS});
  await view.load();
  assert.ok(root.text.includes("0 skills on the shelf"));
});

test("skills off is stated, not rendered as a missing shelf", async () => {
  const off = {enabled: false, count: 0, skills: [], note: "skills.enabled is off"};
  const {view, root} = mountWith({"/api/skills": off, "/api/tools": TOOLS});
  await view.load();
  assert.ok(root.text.includes("skills.enabled is off"));
});

test("a core that predates the routes says so instead of showing an empty shelf", async () => {
  const {view, root} = mountWith({});
  await view.load();
  const text = root.text;
  assert.ok(/could not be read/i.test(text), "a missing route must be reported");
  assert.ok(!text.includes("0 skills on the shelf"), "a missing route is not an empty shelf");
});

test("one route failing must not hide the other", async () => {
  const {view, root} = mountWith({"/api/tools": TOOLS});
  await view.load();
  const text = root.text;
  assert.ok(/could not be read/i.test(text), "the failed route must be reported");
  assert.ok(text.includes("Asks first"), "the route that worked must still render");
});

test("a tool switch posts the new state and paints the saved reply", async () => {
  const tools = {
    ...TOOLS,
    tools: TOOLS.tools.map((tool) => ({...tool, enabled: true, origin: "builtin"})),
  };
  const skills = {
    ...SKILLS,
    directories: [{path: "/tmp/skills", source: "user", error: "", status: "1 skill file", removable: false}],
    skills: SKILLS.skills.map((skill) => ({...skill, enabled: !skill.error, offered: !skill.error})),
  };
  let posted = null;
  const api = async (url, method, body) => {
    if (method === "POST") {
      posted = {url, body};
      return {
        ...tools,
        effect: "Saved. Every loaded session omits a disabled tool from the next turn and refuses a call to it.",
        tools: tools.tools.map((tool) => tool.name === body.name
          ? {...tool, enabled: body.enabled, outcome: "disabled", policy_outcome: tool.outcome}
          : tool),
      };
    }
    if (url === "/api/skills") return skills;
    if (url === "/api/tools") return tools;
    throw new Error("404 Not found");
  };
  const {view, root} = mountWith(api);
  await view.load();
  const box = root.all.find((node) => node.attributes["aria-label"] === "Disable tool shell");
  assert.ok(box, "the shell switch is missing");
  box.checked = false;
  box.listeners.change[0]();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(posted.url, "/api/tools");
  assert.equal(posted.body.name, "shell");
  assert.equal(posted.body.enabled, false);
  assert.ok(root.text.includes("Off. A call is refused."));
  assert.ok(root.text.includes("When on, it is refused under this policy."));
  assert.ok(root.text.includes("Built-in"));
  assert.ok(/next turn/.test(root.text));
});

test("a failed save shows the error and leaves the switch where the server left it", async () => {
  const tools = {...TOOLS, tools: TOOLS.tools.map((tool) => ({...tool, enabled: true, origin: "builtin"}))};
  const api = async (url, method) => {
    if (method === "POST") throw new Error("Config file is missing");
    if (url === "/api/skills") return {...SKILLS, skills: []};
    if (url === "/api/tools") return tools;
    throw new Error("404 Not found");
  };
  const {view, root} = mountWith(api);
  await view.load();
  const box = root.all.find((node) => node.attributes["aria-label"] === "Disable tool shell");
  box.checked = false;
  box.listeners.change[0]();
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.ok(root.text.includes("Config file is missing"));
  const again = root.all.find((node) => node.attributes["aria-label"] === "Disable tool shell");
  assert.equal(again.checked, true);
});

test("an off switch stays refused in yolo and the page says so in every mode", async () => {
  const tools = {
    ...TOOLS,
    policy: "yolo",
    yolo: true,
    tools: TOOLS.tools.map((tool) => ({
      ...tool,
      enabled: tool.name !== "shell",
      origin: "builtin",
      outcome: tool.name === "shell" ? "disabled" : "runs",
      policy_outcome: "runs",
    })),
  };
  const skills = {
    ...SKILLS,
    enabled: false,
    yolo: true,
    note: "skills.enabled is off. The loop does not offer these skills. An off switch applies in every mode, including Unleashed.",
    skills: SKILLS.skills.map((skill) => ({...skill, enabled: false, offered: false})),
  };
  const {view, root} = mountWith({"/api/skills": skills, "/api/tools": tools});
  await view.load();
  const text = root.text;
  assert.ok(text.includes("Off. A call is refused."));
  assert.ok(text.includes("When on, it runs without asking."));
  assert.ok(text.includes("An off switch applies in every mode, including Unleashed."));
  assert.ok(text.includes("Not offered"));
  assert.ok(!text.includes("this still runs"));
  assert.ok(!text.includes("does not stop"));
  assert.ok(!text.includes("YOLO still loads"));
});

test("adding a directory posts the path", async () => {
  const skills = {...SKILLS, skills: [], directories: [], enabled: true, count: 0};
  let posted = null;
  const api = async (url, method, body) => {
    if (method === "POST") {
      posted = body;
      return {...skills, effect: "Saved. No session is loaded. A session started from now on will use this.",
        directories: [{path: body.add_dir, source: "added", error: "Directory does not exist", status: "Directory does not exist", removable: true}]};
    }
    if (url === "/api/skills") return skills;
    if (url === "/api/tools") return TOOLS;
    throw new Error("404 Not found");
  };
  const {view, root} = mountWith(api);
  await view.load();
  const input = root.all.find((node) => node.attributes["aria-label"] === "Skills directory to add");
  input.value = "/tmp/more-skills";
  const form = root.all.find((node) => node.listeners.submit);
  form.listeners.submit[0]({preventDefault() {}});
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(posted.add_dir, "/tmp/more-skills");
  assert.ok(root.text.includes("Directory does not exist"));
  assert.ok(root.text.includes("Remove"));
  const cleared = root.all.find((node) => node.attributes["aria-label"] === "Skills directory to add");
  assert.equal(cleared.value, "");
});

test("a typed skills directory survives a repaint and a failed save", async () => {
  const skills = {...SKILLS, skills: [], directories: [], enabled: true, count: 0};
  const api = async (url, method) => {
    if (method === "POST") throw new Error("Not a directory");
    if (url === "/api/skills") return skills;
    if (url === "/api/tools") return TOOLS;
    throw new Error("404 Not found");
  };
  const {view, root} = mountWith(api);
  await view.load();
  let input = root.all.find((node) => node.attributes["aria-label"] === "Skills directory to add");
  input.value = "/tmp/draft-skills";
  input.selectionStart = 4;
  input.selectionEnd = 4;
  input.focus();
  input.listeners.input[0]();
  const risky = root.all.find((node) => node.textContent === "Risky tools only");
  risky.listeners.click[0]();
  input = root.all.find((node) => node.attributes["aria-label"] === "Skills directory to add");
  assert.equal(input.value, "/tmp/draft-skills");
  assert.equal(root.ownerDocument.activeElement, input);
  assert.equal(input.selectionStart, 4);
  const form = root.all.find((node) => node.listeners.submit);
  form.listeners.submit[0]({preventDefault() {}});
  await new Promise((resolve) => setTimeout(resolve, 0));
  input = root.all.find((node) => node.attributes["aria-label"] === "Skills directory to add");
  assert.equal(input.value, "/tmp/draft-skills");
  assert.equal(root.ownerDocument.activeElement, input);
  assert.equal(input.selectionStart, 4);
  assert.ok(root.text.includes("Not a directory"));
});
