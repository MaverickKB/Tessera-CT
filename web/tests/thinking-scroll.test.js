"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");

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
    this.open = false;
    this.children = [];
    this.parentNode = null;
    this._text = "";
    this._scrollTop = 0;
    this.listeners = {};
    this.dataset = {};
  }
  get textContent() {
    return this.children.length ? this.children.map((child) => child.textContent).join("") : this._text;
  }
  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }
  append(...nodes) {
    for (const node of nodes) {
      if (node.parentNode) {
        const index = node.parentNode.children.indexOf(node);
        if (index >= 0) node.parentNode.children.splice(index, 1);
      }
      node.parentNode = this;
      this.children.push(node);
    }
  }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    return this.parentNode.children[this.parentNode.children.indexOf(this) + 1] || null;
  }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  get clientHeight() {
    if (String(this.className).includes("thought-text")) return 80;
    if (this.id === "conversation-scroll") return 40;
    return 0;
  }
  layoutHeight() {
    if (String(this.className).includes("thought-text")) return Math.min(this.scrollHeight, this.clientHeight);
    let height = this._text.length;
    for (const child of this.children) height += child.layoutHeight();
    return height;
  }
  get scrollHeight() {
    if (String(this.className).includes("thought-text")) return Math.max(this.clientHeight, this._text.length);
    return this.layoutHeight();
  }
  get scrollTop() { return this._scrollTop; }
  set scrollTop(value) {
    this._scrollTop = Number(value);
    for (const fn of this.listeners.scroll || []) fn();
  }
}

function harness() {
  const ids = {};
  const document = {
    createElement: (tag) => new El(tag),
    getElementById: (id) => ids[id],
  };
  for (const id of ["messages", "conversation-scroll", "jump-latest"]) ids[id] = new El("div");
  ids.messages.id = "messages";
  ids["conversation-scroll"].id = "conversation-scroll";
  ids["jump-latest"].id = "jump-latest";
  ids["jump-latest"].hidden = true;
  ids["conversation-scroll"].append(ids.messages);
  const state = {stickToBottom: true, programmaticScroll: false, live: null, status: {agent_name: "Cairn"}};
  const observers = [];
  class ResizeObserver {
    constructor(fn) { this.fn = fn; observers.push(this); }
    observe() {}
  }
  class MutationObserver {
    constructor(fn) { this.fn = fn; observers.push(this); }
    observe() {}
  }
  const names = ["element", "agentName", "thoughtBlock", "liveTurn", "nearBottom", "showJumpLatest", "scrollToLatest", "scheduleFollow", "followLatest", "onDelta"];
  for (const name of ["followThought", "bindTranscriptFollow"]) {
    if (app.includes("function " + name + "(")) names.push(name);
  }
  // This test is about scrolling, not markdown. The live text grows either way, so a
  // plain stub keeps the formatter (and its window.TesseraFormat dependency) out of the rig.
  // bindTranscriptFollow's readerMoved handler arms a timer to clear the "reader is
  // moving" flag. Without timers in the context the handler throws the moment a test
  // fires it, which is why nothing could exercise the reader-scroll guard before.
  const timers = [];
  const context = {state, document, ResizeObserver, MutationObserver, $: (id) => ids[id],
    fillFormatted: (container, text) => { container.textContent = text; },
    setTimeout: (fn, ms) => { timers.push({fn, ms}); return timers.length; },
    clearTimeout: () => {}};
  vm.runInNewContext(names.map((name) => extract(app, name)).join("\n"), context);
  if (typeof context.bindTranscriptFollow === "function") context.bindTranscriptFollow();
  return {ids, state, context, observers, timers};
}

// The chat window jumped and missed characters, timed with replies and streams. Before
// this fix one streamed delta cost 12 layout
// operations (5 scrollHeight reads, 3 scrollTop writes, 2 rAF callbacks), because the
// delta pinned the transcript and the MutationObserver pinned it again for the same
// growth. A scrollHeight read forces the browser to lay out the whole conversation, so
// typing in the composer lost characters whenever a turn streamed. The follow now
// coalesces to one pin per frame. This test holds that number down.
test("a streamed delta costs one pin per frame, not one per observer callback", () => {
  const {ids, context} = harness();
  const scroller = ids["conversation-scroll"];
  let reads = 0, writes = 0, frames = 0;
  Object.defineProperty(scroller, "scrollHeight", {
    get() { reads++; return scroller._height || 0; },
  });
  const realTop = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(scroller), "scrollTop");
  Object.defineProperty(scroller, "scrollTop", {
    get() { return realTop.get.call(scroller); },
    set(value) { writes++; realTop.set.call(scroller, value); },
  });
  const queue = [];
  context.requestAnimationFrame = (fn) => { frames++; queue.push(fn); return frames; };
  // scheduleFollow closes over the global rAF, so install it before the deltas.
  const flush = () => { const batch = queue.splice(0); for (const fn of batch) fn(); };

  const deltas = 40;
  for (let i = 0; i < deltas; i++) {
    scroller._height = (scroller._height || 0) + 12;
    context.onDelta({kind: "text", text: "line\n"});
    flush();
  }
  assert.ok(reads / deltas <= 1.5, `scrollHeight reads per delta ${reads / deltas}, want <= 1.5`);
  assert.ok(writes / deltas <= 1.5, `scrollTop writes per delta ${writes / deltas}, want <= 1.5`);
  assert.ok(frames / deltas <= 1.5, `rAF callbacks per delta ${frames / deltas}, want <= 1.5`);
});

function thoughtPre(ids) {
  const article = ids.messages.children.find((node) => String(node.className).includes("live-turn"));
  const details = article.children.find((node) => String(node.className).includes("thought"));
  return details.children.find((node) => String(node.className).includes("thought-text"));
}

test("streaming reasoning follows inside the thinking block and in the transcript", () => {
  const {ids, state, context} = harness();
  const chunk = "reasoning line\n";
  for (let i = 0; i < 40; i++) context.onDelta({kind: "thought", text: chunk});
  const pre = thoughtPre(ids);
  assert.ok(pre.scrollHeight > pre.clientHeight, "thinking text overflows its own box");
  assert.ok(pre.scrollTop >= pre.scrollHeight - pre.clientHeight - 1, "thinking box stays on its newest line");
  const scroller = ids["conversation-scroll"];
  assert.equal(scroller.scrollTop, scroller.scrollHeight);
  assert.equal(ids["jump-latest"].hidden, true);

  pre.scrollTop = 0;
  const paused = pre.scrollTop;
  context.onDelta({kind: "thought", text: chunk});
  assert.equal(pre.scrollTop, paused, "reading earlier reasoning does not yank the box");

  scroller.scrollTop = 0;
  state.stickToBottom = false;
  context.onDelta({kind: "text", text: "answer line\n"});
  assert.equal(scroller.scrollTop, 0);
  assert.equal(ids["jump-latest"].hidden, false);

  // The jump-latest button is wired to scrollToLatest() (app.js:5319). The rig
  // never fired it, so neutering scrollToLatest left the file green. Pressing
  // the button must both return the reader to the newest line and re-engage
  // the follow, not just move the scroll position once.
  context.scrollToLatest();
  assert.equal(scroller.scrollTop, scroller.scrollHeight, "the button returns to the newest line");
  assert.equal(state.stickToBottom, true, "the button re-engages the follow");
  assert.equal(ids["jump-latest"].hidden, true, "the button hides once the reader is back at the bottom");
  // And the follow really is live again, not just a flag set.
  const back = scroller.scrollTop;
  scroller._height = (scroller._height || 0) + 100;
  context.onDelta({kind: "text", text: "after the jump\n"});
  assert.ok(scroller.scrollTop > back, "streaming resumes following after the jump");
});

// nearBottom() is extracted and was never asserted: neutering it to `return
// undefined` left the file green. It is what decides whether the thinking box
// keeps following, so a reader who nudges a few pixels and is still effectively
// at the bottom must NOT lose the follow. Within the 48px tolerance the box
// keeps pinning; beyond it the reader wins.
test("a reader within the near-bottom tolerance keeps the thinking box following", () => {
  const {ids, state, context} = harness();
  for (let i = 0; i < 40; i++) context.onDelta({kind: "thought", text: "reasoning line\n"});
  const pre = thoughtPre(ids);
  // Sit 20px above the bottom: inside the 48px tolerance.
  pre.scrollTop = pre.scrollHeight - pre.clientHeight - 20;
  assert.equal(pre.dataset.stick, "1", "inside the tolerance the box still follows");
  context.onDelta({kind: "thought", text: "one more line\n"});
  assert.ok(pre.scrollTop >= pre.scrollHeight - pre.clientHeight - 1,
    "the box stays on its newest line when the reader is still near the bottom");
});

// af19738's message claims "a reader who scrolls up still wins" but its test only held
// down the layout-operation count. Measured: deleting BOTH readerScrolling guards from
// followLatest and scheduleFollow left all 22 web tests green, so the claim had no proof.
// The guard is what stops the two corrections fighting: without it a streaming delta
// yanks a moving reader back to the bottom, which is the flicker this guards against.
//
// The case that matters is the one no scroll event covers yet: the finger goes down,
// stickToBottom is still true, and a delta arrives before the reader has moved far
// enough to trip the scroll listener.
test("a reader who is moving the transcript is not yanked back by a streaming delta", () => {
  const {ids, state, context, timers} = harness();
  const scroller = ids["conversation-scroll"];
  let height = 400;
  Object.defineProperty(scroller, "scrollHeight", {
    get() { return height; }, configurable: true,
  });

  for (let i = 0; i < 3; i++) context.onDelta({kind: "text", text: "line\n"});
  assert.equal(scroller.scrollTop, scroller.scrollHeight, "follows to the newest line while the reader is at rest");

  // The reader puts a finger down. No scroll has happened, so stickToBottom is still
  // true and only the readerScrolling flag knows a person is moving the transcript.
  const before = scroller.scrollTop;
  for (const fn of scroller.listeners.pointerdown || []) fn();
  assert.equal(state.readerScrolling, true, "a deliberate touch marks the reader as moving");

  height = 900; // the turn keeps streaming while the finger is down
  context.onDelta({kind: "text", text: "more\n"});
  assert.equal(scroller.scrollTop, before, "a moving reader is not pulled back to the bottom");

  // Finger up, the quiet window passes, and the follow resumes.
  assert.ok(timers.length, "the reader-moved handler armed its quiet-window timer");
  for (const timer of timers.splice(0)) timer.fn();
  assert.equal(state.readerScrolling, false, "the quiet window clears the moving flag");
  context.onDelta({kind: "text", text: "after\n"});
  assert.equal(scroller.scrollTop, scroller.scrollHeight, "the follow resumes once the reader stops");
});
