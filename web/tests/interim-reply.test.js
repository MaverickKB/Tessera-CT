"use strict";
// An in-thought reply shows below the thinking stream and stays visible after
// the turn commits. This drives the real page path (delta stream, then the
// committed message) through a wake turn.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");
const autonomySource = fs.readFileSync(path.join(__dirname, "..", "autonomy.js"), "utf8");

function extract(source, name) {
  const start = source.indexOf("function " + name + "(");
  if (start < 0) throw new Error("missing " + name);
  let depth = 0;
  for (let i = source.indexOf("{", start); i < source.length; i++) {
    if (source[i] === "{") depth++;
    else if (source[i] === "}") {
      depth--;
      if (depth === 0) return source.slice(start, i + 1);
    }
  }
  throw new Error("unclosed " + name);
}

class El {
  constructor(tag) {
    this.tagName = tag;
    this.id = "";
    this.className = "";
    this.hidden = false;
    this.children = [];
    this.parentNode = null;
    this._text = "";
    this.dataset = {};
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      contains: (name) => classes.has(name),
    };
  }
  get textContent() {
    return this.children.length ? this.children.map((child) => child.textContent).join("") : this._text;
  }
  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    return this.parentNode.children[this.parentNode.children.indexOf(this) + 1] || null;
  }
  append(...nodes) { for (const node of nodes) this.insertBefore(node, null); }
  insertBefore(node, ref) {
    if (node.parentNode) {
      const index = node.parentNode.children.indexOf(node);
      if (index >= 0) node.parentNode.children.splice(index, 1);
    }
    node.parentNode = this;
    const index = ref ? this.children.indexOf(ref) : -1;
    if (index < 0) this.children.push(node); else this.children.splice(index, 0, node);
    return node;
  }
  replaceWith(node) { if (this.parentNode) this.parentNode.insertBefore(node, this); }
  remove() {
    if (!this.parentNode) return;
    const index = this.parentNode.children.indexOf(this);
    if (index >= 0) this.parentNode.children.splice(index, 1);
    this.parentNode = null;
  }
  replaceChildren(...nodes) {
    this.children = [];
    this._text = "";
    for (const node of nodes) this.insertBefore(node, null);
  }
  addEventListener() {}
  querySelector() { return new El("div"); }
  querySelectorAll() { return []; }
  setAttribute() {}
  focus() {}
  contains(node) {
    if (node === this) return true;
    return this.children.some((child) => child.contains && child.contains(node));
  }
  find(predicate, found = []) {
    if (predicate(this)) found.push(this);
    for (const child of this.children) if (child.find) child.find(predicate, found);
    return found;
  }
}

function harness() {
  const ids = {};
  const document = {
    createElement: (tag) => new El(tag),
    getElementById: (id) => ids[id],
  };
  for (const id of ["messages", "conversation-scroll", "jump-latest", "empty-state", "empty-create",
    "send-state", "message-input", "queue-list", "reply-chip", "reply-text"]) {
    ids[id] = new El("div");
  }
  ids.messages.id = "messages";
  ids["conversation-scroll"].append(ids.messages);

  // The real wake filter, so the test fails wherever the rule itself is wrong.
  const autonomyContext = {window: {}};
  vm.runInNewContext(autonomySource, autonomyContext);
  const autonomyApi = autonomyContext.window.TesseraAutonomy;

  const timers = [];
  const state = {
    slug: "probe", epoch: 1, live: null, busy: false,
    messages: new Map(), nodes: new Map(), pendingMessages: [], messageTimer: null,
    callNodes: new Map(), stickToBottom: true, programmaticScroll: false,
    readerScrolling: false, status: {agent_name: "Cairn"}, feedback: {}, detailsTimer: null,
    oldest: 0, lastEvent: "",
  };
  const context = {
    state, document, console,
    $: (id) => ids[id],
    window: {TesseraItems: {describe: () => null}, TesseraFormat: {render: () => []}},
    uiSetting: () => 120,
    element: (tag, className, text) => {
      const node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined) node.textContent = String(text);
      return node;
    },
    fillFormatted: (container, text) => { container.textContent = text; },
    fillInlines: () => {},
    agentName: () => "Cairn",
    thoughtBlock: (text, open) => {
      const details = new El("details");
      details.className = "thought";
      details.open = open;
      const pre = new El("pre");
      pre.className = "thought-text";
      pre.textContent = text;
      details.append(new El("summary"), pre);
      return details;
    },
    messageActions: () => new El("div"),
    paintFeedback: () => {},
    loadFullBody: () => Promise.resolve(),
    speakHarnessReply: () => {},
    loadQueue: () => Promise.resolve(),
    updateHeading: () => {},
    observeActivity: () => {},
    renderActivity: () => {},
    renderMemory: () => {},
    sessionStatus: () => ({}),
    toolResultNotice: () => "",
    itemBody: () => new El("div"),
    queueSessionDetails: () => {},
    followLatest: () => {},
    showJumpLatest: () => {},
    scheduleFollow: () => {},
    nearBottom: () => true,
    followThought: () => {},
    requestAnimationFrame: (fn) => fn(),
    setTimeout: (fn) => { timers.push(fn); return timers.length; },
    clearTimeout: () => {},
    autonomy: {
      hiddenIndices: autonomyApi.hiddenIndices,
      resolvedApprovals: () => new Map(),
      notify: () => {},
      approvalNode: () => new El("article"),
    },
    Date, Number, Array, Map, Set, Math, String, JSON, Promise, RegExp, Object, Error,
    encodeURIComponent, isNaN: Number.isNaN,
  };
  const names = ["interimReply", "liveTurn", "onDelta", "clearLive", "messageNode", "addMessages", "handleEvent"];
  vm.runInNewContext(names.map((name) => extract(app, name)).join("\n"), context);
  const flush = () => { let guard = 0; while (timers.length && guard++ < 50) timers.shift()(); };
  return {ids, state, context, flush};
}

// An interim reply (an agent row carrying text plus tool_calls) is streamed live,
// then the committed row is filtered out by the wake-turn rule in
// web/autonomy.js, and handleEvent clears the live text in the same breath.
// The reply has nowhere left to live unless the committed row is kept.
// A wake turn's tool traffic and its empty scaffolding still stay hidden;
// a reply the model actually wrote is speech.
test("an interim reply in a wake turn survives the change from streaming to committed", () => {
  const {ids, state, context, flush} = harness();
  const reply = "The Mac ledger is already at the NAS head.";

  context.handleEvent("message", {
    message: {i: 1, kind: "user", text: "[wake] Standing agenda is due.", meta: {turn: 99, wake: {events: ["timer"]}}},
  }, null, "probe", 1);
  flush();

  context.onDelta({kind: "thought", text: "Let me check the ledger.\n"});
  context.onDelta({kind: "text", text: reply});
  flush();
  assert.ok(ids.messages.find((node) => node.textContent.includes(reply)).length, "the reply streams into view");

  context.handleEvent("message", {
    message: {i: 2, kind: "agent", text: reply, meta: {turn: 99, tool_calls: [{name: "shell"}]}},
  }, null, "probe", 1);
  flush();

  // Only the message articles carry an index; the search also walks text nodes.
  const visible = ids.messages.find((node) => node.tagName === "article" && node.textContent.includes(reply));
  assert.ok(visible.length, "the committed interim reply stays on screen");
  assert.equal(visible[0].dataset.index, "2");
  assert.ok(state.messages.has(2), "the row is in the transcript model");
  // interimReply() is the function this file is named after and it was extracted
  // but never asserted: neutering it to `return false` left the file 6/6 green.
  // Both of its call sites are observable, so assert both.
  // app.js:1087 -> messageNode() tags the committed row
  assert.ok(visible[0].classList.contains("interim"),
    "the committed interim row carries the interim class");
  // app.js:1587 -> handleEvent() takes the commitInterim branch instead of
  // replaceLive, so the live box survives because the turn has more to say
  assert.ok(state.live, "the live box stays open after an interim reply commits");
});

test("a wake turn's empty scaffolding and its tool traffic still stay hidden", () => {
  const {ids, context, flush} = harness();
  context.handleEvent("message", {
    message: {i: 1, kind: "user", text: "[wake] Standing agenda is due.", meta: {turn: 99, wake: {events: ["timer"]}}},
  }, null, "probe", 1);
  context.handleEvent("message", {
    message: {i: 2, kind: "agent", text: "", meta: {turn: 99, tool_calls: [{name: "shell"}]}},
  }, null, "probe", 1);
  context.handleEvent("message", {
    message: {i: 3, kind: "tool", text: "{\"name\":\"shell\"}", meta: {turn: 99}},
  }, null, "probe", 1);
  context.handleEvent("message", {
    message: {i: 4, kind: "echo", text: "{\"exit_code\":0}", meta: {turn: 99}},
  }, null, "probe", 1);
  flush();
  assert.equal(ids.messages.children.length, 0);
});


test("a final reply stays visible until its committed row is inserted", () => {
  const {ids, context, flush} = harness();
  context.onDelta({kind: "text", text: "Keep this answer in place."});
  const hasReply = () => ids.messages.textContent.includes("Keep this answer in place.");
  assert(hasReply());
  context.handleEvent("message", {message: {i: 8, kind: "agent", text: "Keep this answer in place.", meta: {turn: 1}}}, null, "probe", 1);
  assert(hasReply(), "there must be no empty frame while the message batch waits");
  flush();
  assert(hasReply());
  const articles = ids.messages.find(node => node.tagName === "article" && node.textContent.includes("Keep this answer in place."));
  assert.equal(articles.length, 1);
  assert.equal(articles[0].dataset.index, "8");
});

test("streaming preserves completed formatted blocks while extending the next paragraph", () => {
  const {context} = harness();
  context.document.createDocumentFragment = () => new El('fragment');
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '..', 'format.js'), 'utf8'), context);
  vm.runInNewContext(extract(app, 'replaceChangedChildren') + '\n' + extract(app, 'fillFormatted'), context);
  const container = new El('div');
  context.fillFormatted(container, 'First paragraph.\n\nSecond');
  const first = container.firstChild;
  context.fillFormatted(container, 'First paragraph.\n\nSecond paragraph grows.');
  assert.equal(container.firstChild, first, 'the paragraph being read keeps its DOM identity');
  assert.equal(container.children.length, 2);
});

test("an anonymous checkbox does not block an interface update, but unsaved text does", () => {
  const context = {document: {querySelectorAll: () => fields}};
  let fields = [{type:'checkbox',id:'',value:'on'}];
  vm.runInNewContext(extract(app, 'holdsUnrecoverableText'), context);
  assert.equal(context.holdsUnrecoverableText(), false);
  fields.push({type:'text',id:'',value:'Words to preserve'});
  assert.equal(context.holdsUnrecoverableText(), true);
});

test('replacing a group heading does not move retained controls out of their DOM position', () => {
  const parent = new El('ul');
  const oldHeading = new El('h3');
  const retained = new El('button');
  parent.append(oldHeading, retained);
  let moves = 0;
  const insert = parent.insertBefore.bind(parent);
  parent.insertBefore = (node, ref) => { if (node === retained) moves++; return insert(node, ref); };
  const context = {};
  vm.runInNewContext(extract(app, 'replaceChangedChildren'), context);
  const heading = new El('h3');
  context.replaceChangedChildren(parent, [heading, retained]);
  assert.equal(moves, 0, 'a focused unchanged button must remain connected');
  assert.deepEqual(parent.children, [heading, retained]);
});
