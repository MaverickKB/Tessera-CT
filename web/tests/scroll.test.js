"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "..", "app.css"), "utf8");
const app = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");

test("jump-to-latest sits on the composer and stays out of the way until needed", () => {
  assert.match(html, /<button id="jump-latest" class="jump-latest" type="button" hidden>Latest<\/button>/);
  assert.match(css, /\.jump-latest\s*\{[^}]*position:\s*absolute/);
  assert.match(app, /stickToBottom/);
  assert.match(app, /function followLatest\(\)/);
  assert.match(app, /function onDelta\(data\)/);
  const delta = app.slice(app.indexOf("function onDelta"), app.indexOf("function clearLive"));
  assert.match(delta, /followLatest\(\)/);
  assert.match(delta, /data\.kind === "thought"/);
  assert.match(delta, /data\.kind === "tool"/);
  const scroll = app.slice(app.indexOf('addEventListener("scroll"'), app.indexOf('addEventListener("click", scrollToLatest)'));
  assert.match(scroll, /nearBottom/);
  assert.match(scroll, /programmaticScroll/);
});
