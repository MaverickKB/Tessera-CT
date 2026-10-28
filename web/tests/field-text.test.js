"use strict";
// A poll must not drop text while a note is being typed, on any page,
// including a reply or a streaming update.
//
// The board page rebuilds every card node on the 15-second poll. A half-typed note lives
// inside one of those nodes, so a rebuild would take the words with it. This test runs
// the REAL render path out of web/app.js against a small honest DOM: the field is found by
// walking the live tree, the way a browser finds it, so a cached-node shortcut cannot pass.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");

// The one focused element in the fake document. El#focus sets it, and the
// document stub reads it, so captureFocus sees the same thing a browser would.
let currentFocus = null;

// Names of every top-level function declaration in app.js, in file order.
function declarations() {
  const names = [];
  const re = /^  function ([A-Za-z_$][\w$]*)\s*\(/gm;
  let m;
  while ((m = re.exec(app)) !== null) names.push(m[1]);
  return names;
}

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
    this.tag = tag; this.children = []; this.className = ""; this._text = "";
    this.dataset = {}; this.attributes = {}; this.hidden = false; this.value = "";
    this.parentNode = null; this.listeners = {}; this.id = ""; this.style = {};
    this.selectionStart = 0; this.selectionEnd = 0;
  }
  get tagName() { return String(this.tag).toUpperCase(); }
  // The DOM has contains(); the harness did not, so boardFieldFocused() could
  // never return true here and the guard it implements was untestable.
  contains(other) {
    if (!other) return false;
    if (other === this) return true;
    for (const child of this.children || []) if (child.contains && child.contains(other)) return true;
    return false;
  }
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
  get textContent() {
    return this.children.length ? this.children.map((c) => c.textContent).join(" ") : this._text;
  }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...nodes) {
    for (const node of nodes) {
      if (node && node.parentNode) {
        const at = node.parentNode.children.indexOf(node);
        if (at >= 0) node.parentNode.children.splice(at, 1);
      }
      if (node) node.parentNode = this;
      this.children.push(node);
    }
  }
  replaceChildren(...nodes) { this.children = []; this._text = ""; this.append(...nodes); }
  setAttribute(key, value) { this.attributes[key] = value; }
  getAttribute(key) { return this.attributes[key]; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  remove() {
    if (!this.parentNode) return;
    this.parentNode.children.splice(this.parentNode.children.indexOf(this), 1);
    this.parentNode = null;
  }
  insertBefore(node, reference) {
    if (node.parentNode) node.remove();
    const index = reference ? this.children.indexOf(reference) : -1;
    node.parentNode = this;
    if (index < 0) this.children.push(node); else this.children.splice(index, 0, node);
  }
  replaceWith(node) { this.parentNode.insertBefore(node, this); this.remove(); }
  get nextSibling() {
    if (!this.parentNode) return null;
    return this.parentNode.children[this.parentNode.children.indexOf(this)+1] || null;
  }
  get lastChild() { return this.children[this.children.length - 1]; }
  get firstChild() { return this.children[0]; }
  // Focus has to be observable, or captureFocus/restoreFocus cannot be tested at all.
  focus() { currentFocus = this; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  querySelectorAll(selector) {
    const parts = String(selector).split(",").map((part) => part.trim());
    const matches = (node) => parts.some((part) => {
      if (part.startsWith(".")) return node.className.split(" ").includes(part.slice(1));
      const found = /^([a-z]+)(\[id\])?$/.exec(part);
      if (!found) return false;
      return node.tag === found[1] && (!found[2] || !!node.id);
    });
    const out = [];
    const walk = (node) => {
      for (const child of node.children || []) {
        if (matches(child)) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
}

function board(ordinal, status, title) {
  return {ordinal, status, title, note: "", owner: "", project: "", claimable: true,
    artifact_count: 0, attempt: 0, question_pending: false, questions: [],
    created_at: 0, updated_at: 0, log_start: 0, log_end: 0, session_ordinal: 1,
    updated_by: "", version: 1};
}

function mount(view) {
  const ids = new Proxy({}, {get(target, key) {
    if (!(key in target)) target[key] = new El("div");
    return target[key];
  }});
  const state = {
    boardView: view, boardViewEpoch: 0, boardRevision: null, page: "wip",
    boardProjectFilter: "", boardShowDone: false, openProject: "", pressure: null,
    status: {}, sessions: [], slug: null, epoch: 0, chatNames: [], live: null,
    messages: new Map(), nodes: new Map(), pendingFieldText: {}, pinPending: false,
    modelPickerKey: "", boardControlsMissing: false,
    // renderBoardColumns records a row's open state by identity on every rebuild.
    // Absent from this harness, it threw before any assertion ran.
    detailExpanded: {},
  };
  // Every top-level function declaration, not a hand-maintained list. A list
  // has to be edited whenever renderBoardStatus (or anything else on the path)
  // starts calling a new helper, and forgetting that edit makes the rig die with
  // a ReferenceError before any assertion runs. Extracting the whole declaration
  // set removes that maintenance step: a helper can be added, renamed or removed
  // in app.js and the rig still loads.
  let body = "";
  for (const name of declarations()) {
    const fn = extract(name);
    assert.ok(fn, name + " is declared in web/app.js but could not be extracted");
    body += fn + "\n";
  }
  const context = {
    state,
    console,
    BOARD_NOTE_PREVIEW: 240,
    document: {
      // No element is focused unless a test says so. captureFocus reads this, and
      // restoreFocus compares against it, so it has to exist as a real property.
      get activeElement() { return currentFocus; },
      set activeElement(value) { currentFocus = value; },
      // The live tree, not a cache: a stale node here would let a broken fix pass.
      getElementById: (id) => {
        const walk = (node) => {
          if (node.id === id) return node;
          for (const child of node.children || []) {
            const hit = walk(child);
            if (hit) return hit;
          }
          return null;
        };
        // `id in ids` uses the proxy's has trap, which does not create an entry.
        // A bare ids[id] get would auto-create an empty stub div, so a lookup for
        // a node that is NOT in the tree would return a phantom element -- and
        // restoreFocus() would then "succeed" against something the app never made.
        return walk(ids["board-cards"]) || (id in ids ? ids[id] : null);
      },
      createElement: (tag) => new El(tag),
      querySelectorAll: () => [],
    },
    element: (tag, cls, text) => {
      const node = new El(tag);
      if (cls) node.className = cls;
      if (text !== undefined) node.textContent = text;
      return node;
    },
    $: (id) => ids[id],
    api: async () => view,
    showError: () => {},
    relativeAge: () => "now",
    safeBoardTitle: (title) => String(title || ""),
    boardLabels: {queued: "Queued", running: "Running", blocked: "Blocked",
      succeeded: "Succeeded", failed: "Failed", cancelled: "Cancelled"},
    boardStatusLabel: (status) => String(status),
    boardStatusTone: () => "",
    setIntegrationBadge: () => {},
    paintBoardChrome: () => {},
    renderProjectPage: () => {},
    renderBoardNoteProjects: () => {},
    fileCardControl: () => new El("div"),
    moveCard: () => {},
    addBoardNote: () => {},
    setClaim: () => {},
    editCard: () => {},
    removeCard: () => {},
    ask: async () => true,
    viewCard: (ordinal) => (view.cards || []).find((card) => card.ordinal === ordinal) || null,
  };
  // page-wip is the container boardFieldFocused() checks. In the harness it was an
  // unrelated empty div, so a focused field was never "on the WIP page".
  ids["page-wip"].append(ids["board-cards"]);
  context.window = context;
  vm.createContext(context);
  vm.runInContext(body, context);
  return {
    ids,
    state,
    render: () => vm.runInContext("renderBoardStatus()", context),
    run: (expr) => vm.runInContext(expr, context),
  };
}

function findById(node, id) {
  if (!node) return null;
  if (node.id === id) return node;
  for (const child of node.children || []) {
    const hit = findById(child, id);
    if (hit) return hit;
  }
  return null;
}

test("a half-typed card note survives the board poll that rebuilds the cards", () => {
  const view = {enabled: true, configured: true, revision: "a".repeat(64),
    cards: [board(21, "queued", "sparkDash cached tokens"), board(22, "queued", "Second brain")],
    projects: []};
  const {ids, state, render} = mount(view);

  // First paint: the card exists and its note field is in the tree.
  render();
  const field = findById(ids["board-cards"], "card-note-21");
  assert.ok(field, "card 21 should render a note field");

  // The person types into it, with the caret mid-word and focus in the field.
  const typed = "measured: engine /metrics has no cached_tokens_total";
  field.value = typed;
  field.focus();
  field.setSelectionRange(12, 12);

  // An unchanged poll must not rebuild anything at all.
  render();
  assert.equal(findById(ids["board-cards"], "card-note-21"), field,
    "an unchanged poll preserves the editor node");
  assert.equal(state.boardCardsReused, undefined); // no such flag; kept explicit below

  // A CHANGED card is what actually forces a rebuild: row.renderKey is per-card
  // content, so bumping the board revision alone reuses every row and would let a
  // broken fix pass. Change the card itself.
  //
  // While a field on the WIP page has focus, renderBoardStatus() defers the
  // whole rebuild and returns. The assertion below is that hold.
  view.cards[0].status = "running";
  render();

  // While the field is focused, the changed card must NOT rebuild the row.
  // This is the assertion that makes boardFieldFocused() discriminate.
  assert.equal(findById(ids["board-cards"], "card-note-21"), field,
    "a focused WIP field holds the rebuild even when a card changed");
  assert.equal(field.value, typed, "the typing is untouched while the rebuild is held");

  // Release focus. The next render rebuilds, and the words come back through
  // captureFieldText/adoptPendingFieldText.
  currentFocus = null;
  render();

  const rebuilt = findById(ids["board-cards"], "card-note-21");
  assert.ok(rebuilt, "card 21 should still render a note field after the rebuild");
  assert.notEqual(rebuilt, field, "once focus is released the changed card rebuilds the row");
  assert.equal(rebuilt.value, typed, "the words the person typed must come back");
  assert.deepEqual(state.pendingFieldText, {}, "nothing should be left pending after adopt");
});

// captureFocus/restoreFocus are the save/restore half of the same fix. While a
// WIP field is focused renderBoardStatus defers, so the restore path is not
// reached from the poll test above any more -- asserting it there was asserting
// a contract 6a1e324 replaced. These are real functions with a real contract, so
// exercise them directly across a rebuild instead of inventing a caller.
test("focus and caret are saved before a rebuild and handed back after it", () => {
  const view = {enabled: true, configured: true, revision: "a".repeat(64),
    cards: [board(21, "queued", "sparkDash cached tokens")], projects: []};
  const ctx = mount(view);
  const {ids} = ctx;
  ctx.render();
  const field = findById(ids["board-cards"], "card-note-21");
  assert.ok(field, "card 21 should render a note field");
  field.value = "half typed";
  field.focus();
  field.setSelectionRange(5, 5);

  const saved = ctx.run("captureFocus()");
  assert.equal(saved.id, "card-note-21", "captureFocus records the focused field");
  assert.equal(saved.start, 5, "captureFocus records the caret");

  // Force a real rebuild: renderBoardStatus early-returns when renderKey is
  // unchanged, so emptying the list alone would leave `again` null and the
  // assertion would compare against nothing. Change the card, as the poll test does.
  view.cards[0].status = "running";
  currentFocus = null;   // the guard defers while a field is focused
  ctx.render();
  const again = findById(ids["board-cards"], "card-note-21");
  assert.notEqual(again, field, "the rows really were replaced");
  ctx.run("restoreFocus(" + JSON.stringify(saved) + ")");
  assert.equal(currentFocus, again, "restoreFocus puts focus back on the rebuilt field");
  assert.equal(again.selectionStart, 5, "restoreFocus puts the caret back");
});
