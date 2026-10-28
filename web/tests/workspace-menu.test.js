"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");
const app = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");

test("the session menu sets the workspace without a banner", () => {
  assert.match(html, /id="menu-workspace"/);
  assert.match(html, /id="menu-workspace-form"/);
  assert.doesNotMatch(html, /id="workspace-banner"/);
  const open = app.slice(app.indexOf("function openSessionMenu"), app.indexOf("function closeSessionMenu"));
  assert.match(open, /menu-workspace/);
  const saveAt = app.indexOf('menu-workspace-form").addEventListener');
  assert.ok(saveAt > 0);
  const save = app.slice(saveAt, saveAt + 600);
  assert.match(save, /PATCH/);
  assert.match(save, /workspace/);
  const heading = app.slice(app.indexOf("function updateHeading"), app.indexOf("function desktopWithoutBackend") > app.indexOf("function updateHeading")
    ? app.indexOf("function renderModelPicker")
    : app.indexOf("function renderModelPicker"));
  assert.match(heading, /workspace/);
});
