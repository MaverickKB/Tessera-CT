"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const root = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const css = fs.readFileSync(path.join(root, "app.css"), "utf8");
const app = fs.readFileSync(path.join(root, "app.js"), "utf8");
const source = fs.readFileSync(path.join(root, "gremlin-trail.js"), "utf8");
const logo = fs.readFileSync(path.join(root, "brand", "gremlin-logo.svg"), "utf8");
const cartoon = fs.readFileSync(path.join(root, "brand", "f-it-gremlin.svg"), "utf8");
const brand = fs.readFileSync(path.join(root, "..", "docs", "brand", "README.md"), "utf8");
const config = fs.readFileSync(path.join(root, "..", "docs", "reference", "CONFIG.md"), "utf8");

function load() {
  const context = vm.createContext({window: {}});
  vm.runInContext(source, context);
  return context.window.TesseraGremlinTrail;
}

const idle = {pack: true, connected: true, busy: false, activeAgents: 0, queued: 0, jobs: [], cards: []};

function crew(actual, pose, goblins, caption) {
  assert.equal(actual.pose, pose);
  assert.equal(actual.goblins, goblins);
  assert.equal(actual.caption, caption);
}

test("the pack off leaves the tree in place", () => {
  const api = load();
  const trail = {dataset: {}};
  const caption = {textContent: ""};
  const result = api.apply(trail, caption, {...idle, pack: false, busy: true, activeAgents: 4, sessionError: "storage failed"});
  assert.equal(result.pose, "tree");
  assert.equal(result.goblins, 0);
  assert.equal(trail.dataset.motion, "tree");
  assert.equal(trail.dataset.goblins, "0");
  assert.equal(caption.textContent, api.TREE);
});

test("an idle connected page naps, and a disconnected page naps even when work is reported", () => {
  const api = load();
  crew(api.motion(idle), "nap", 1, api.NAP);
  crew(api.motion({...idle, connected: false, busy: true, activeAgents: 3, sessionError: "down"}),
    "nap", 1, api.NAP);
  assert.equal(api.motion({pack: true}).pose, "nap");
});

test("busy sessions, agents, queued turns, running jobs and running cards add haulers", () => {
  const api = load();
  crew(api.motion({...idle, busy: true}), "haul", 1, api.HAUL);
  assert.equal(api.motion({...idle, activeAgents: 1}).goblins, 1);
  assert.equal(api.motion({...idle, queued: 1, busy: true}).goblins, 2);
  assert.equal(api.motion({...idle, activeAgents: 1, queued: 1, busy: true}).goblins, 2);
  assert.equal(api.motion({...idle, jobs: [{status: "running"}, {status: "queued"}, {status: "succeeded"}]}).goblins, 1);
  assert.equal(api.motion({...idle, cards: [{status: "running"}, {status: "running"}, {status: "queued"}]}).goblins, 2);
  assert.equal(api.motion({...idle, busy: true, activeAgents: 2, queued: 1, jobs: [{status: "running"}], cards: [{status: "running"}]}).goblins, 3);
  assert.equal(api.motion({...idle, activeAgents: "2", queued: -1, jobs: "running", cards: null}).pose, "nap");
});

test("a live error tumbles one goblin and keeps haulers when work is still running", () => {
  const api = load();
  crew(api.motion({...idle, sessionError: "Session storage failed"}), "tumble", 1, api.TUMBLE);
  assert.equal(api.motion({...idle, sessionError: "   "}).pose, "nap");
  assert.equal(api.motion({...idle, turnError: true, busy: true, activeAgents: 1}).goblins, 3);
  assert.equal(api.motion({...idle, jobs: [{status: "failed"}]}).pose, "tumble");
  assert.equal(api.motion({...idle, jobs: [{status: "blocked"}], cards: [{status: "running"}]}).goblins, 2);
  assert.equal(api.motion({...idle, cards: [{status: "blocked"}, {status: "failed"}]}).goblins, 1);
  assert.equal(api.motion({...idle, cards: [{status: "succeeded"}]}).pose, "nap");
});

test("the page switches the trail with the pack and keeps the tree as the default", () => {
  assert.match(html, /id="brand-trail"/);
  assert.match(html, /class="trail-tree"/);
  assert.match(html, /class="trail-goblins"/);
  assert.match(html, /data-motion="tree"/);
  assert.match(html, /Tessera\./);
  assert.match(css, /:root\[data-pack="gremlin"\] \.brand-trail \.trail-tree \{ display: none; \}/);
  assert.match(css, /:root\[data-pack="gremlin"\] \.brand-trail \.trail-goblins \{ display: block; \}/);
  assert.match(app, /document\.documentElement\.dataset\.pack = pack \? "gremlin" : "off"/);
  assert.match(app, /pack: document\.documentElement\.dataset\.pack === "gremlin"/);
  assert.match(css, /\.brand-trail \.trail-goblins \{ display: none; \}/);
  assert.match(app, /TesseraGremlinTrail/);
  assert.match(app, /activity\.phase === api\.TURN_ERROR/);
  assert.match(app, /document\.body\.classList\.toggle\("cairn-working", busy && connected\)/);
  const motionStart = css.indexOf("@media (prefers-reduced-motion: no-preference)");
  const motionEnd = css.indexOf("@keyframes cairn-choice");
  const motion = css.slice(motionStart, motionEnd);
  assert.match(motion, /goblin-breathe/);
  assert.match(motion, /goblin-scurry/);
  assert.match(motion, /goblin-tumble/);
  assert.doesNotMatch(css.slice(0, motionStart), /goblin-scurry/);
  assert.match(css, /@media \(max-width: 1020px\) \{ \.brand-trail \{ display: none; \} \}/);
});

test("the logo is an original sticker and the button cartoon stays the other file", () => {
  assert.notEqual(logo, cartoon);
  assert.match(logo, /class="gremlin-logo"/);
  assert.match(logo, /Cartoon gremlin on a yellow sticker/);
  assert.doesNotMatch(logo, /<script/i);
  assert.doesNotMatch(logo, /f-it-letters/);
  assert.match(cartoon, /class="f-it-gremlin"/);
  assert.match(html, /\/static\/gremlin-trail\.js/);
  for (const text of [brand, config]) {
    assert.match(text, /napping/);
    assert.match(text, /hauling/);
    assert.match(text, /tumbles/);
    assert.match(text, /active_agents/);
    assert.match(text, /prefers-reduced-motion/);
    assert.match(text, /1020px/);
    assert.match(text, /no native iOS crew|No native iOS crew|No separate iOS crew/);
  }
});
