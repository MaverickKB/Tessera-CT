"use strict";
// The board has to be an easy to read message board that conveys planned work, progress
// tracking, tasks, to-dos and whatever the system identifies as pressure. That must be
// communicated effectively, not just with pretty glowing icons and cards that do not
// communicate.
//
// Measured before this test: the live board payload carried 7 unfinished steps across
// active projects, and the project card rendered only "2 of 3 steps done" and "67% of the
// plan". Not one unfinished step was named on the board, so a person had to open every
// project to learn what was left. This test runs the REAL projectCard out of web/app.js
// against the live payload shape and asserts the next open step is on the card.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");

function extract(name) {
  const start = app.indexOf("function " + name + "(");
  if (start < 0) return "";
  let i = app.indexOf("{", start), depth = 0;
  for (; i < app.length; i++) {
    if (app[i] === "{") depth += 1;
    else if (app[i] === "}") { depth -= 1; if (depth === 0) break; }
  }
  return app.slice(start, i + 1);
}

class El {
  constructor(tag) {
    this.tag = tag; this.children = []; this._text = ""; this.attributes = {};
    this.dataset = {}; this.hidden = false; this.style = {}; this.id = "";
  }
  get textContent() {
    return this.children.length ? this.children.map((c) => c.textContent).join("\n") : this._text;
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...nodes) { for (const node of nodes) this.children.push(node); }
  replaceChildren(...nodes) { this.children = nodes; }
  setAttribute(key, value) { this.attributes[key] = value; }
  addEventListener() {}
  get value() { return ""; }
}

function mount() {
  const names = ["safeBoardTitle", "nonnegativeInteger", "projectStanding", "projectStepRatio",
    "projectCard", "element"];
  let body = "";
  for (const name of names) {
    const fn = extract(name);
    assert.ok(fn, name + " is missing from web/app.js");
    body += fn + "\n";
  }
  const ctx = {
    console,
    state: { boardView: null, boardProjectFilter: "", boardRevision: "rev" },
    document: { getElementById: () => new El("div"), createElement: (tag) => new El(tag) },
  };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(body, ctx);
  return ctx;
}

// The shape the live core serves: /api/board/view projects carry steps with id/text/done.
const project = {
  id: "second-brain",
  title: "Second brain: Obsidian as the place research lands",
  status: "active",
  manager: "operations",
  description: "Research needs a human-reviewable place to land.",
  steps_total: 3,
  steps_done: 2,
  cards_open: 0,
  cards_total: 1,
  unclaimed: 0,
  steps: [
    {id: "n1", text: "Stand up the Obsidian vault as a place Cairn can read and write", done: true},
    {id: "n2", text: "Write the NVIDIA flare-local-inference paper summary", done: true},
    {id: "n3", text: "Prove the vault is navigable from the vault alone", done: false},
  ],
};

test("a project card names the next open step, not only a percentage", () => {
  const ctx = mount();
  const card = ctx.projectCard(project);
  const read = card.textContent;

  // The progress facts stay.
  assert.match(read, /2 of 3 steps done/);
  assert.match(read, /67% of the plan/);

  // The work left is named. This is the assertion that fails on the old card.
  assert.match(read, /Next: Prove the vault is navigable from the vault alone/);

  // And a finished step's words are not what it names.
  assert.doesNotMatch(read, /Next: Stand up the Obsidian vault/);
});

test("a project with every step done shows no next step", () => {
  const ctx = mount();
  const done = Object.assign({}, project, {
    steps_done: 3,
    steps: project.steps.map((step) => Object.assign({}, step, {done: true})),
  });
  const read = ctx.projectCard(done).textContent;
  assert.match(read, /3 of 3 steps done/);
  assert.doesNotMatch(read, /Next:/);
});

test("a project with no plan does not invent a next step", () => {
  const ctx = mount();
  const bare = Object.assign({}, project, {steps: [], steps_total: 0, steps_done: 0});
  const read = ctx.projectCard(bare).textContent;
  assert.match(read, /No plan written yet/);
  assert.doesNotMatch(read, /Next:/);
});
