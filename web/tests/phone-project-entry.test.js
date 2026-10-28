"use strict";
// The phone project view.
//
// The card is one object, so a tap on the description, the manager line, the
// progress meter, or the card body hits the same handler as the title.
// Projects stay reachable without a closed disclosure behind the counters and
// the in-page Add work form. With the section open, the project grid precedes
// the create form.
//
// These tests pin the card's own click handler, the projects section opening by
// default at every width while respecting a reader's choice, and the project grid
// preceding the create form at every width. The phone layout stays scrollable.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const app = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");
const css = fs.readFileSync(path.join(__dirname, "..", "design.css"), "utf8");

function extract(name) {
  const start = app.indexOf("function " + name + "(");
  if (start < 0) return "";
  let i = app.indexOf("{", start), depth = 0;
  for (; i < app.length; i += 1) {
    if (app[i] === "{") depth += 1;
    else if (app[i] === "}") { depth -= 1; if (depth === 0) break; }
  }
  return app.slice(start, i + 1);
}

test("a project card opens on a tap anywhere, not only on its title", () => {
  const card = extract("projectCard");
  assert.ok(card, "projectCard is missing from web/app.js");
  // The card element itself carries a handler that opens the project.
  assert.match(card, /card\.addEventListener\("click"/);
  assert.match(card, /openProject\(project\.id\)/);
  // The two real buttons must not double-fire the card handler.
  assert.match(card, /closest\("\.project-open, \.project-filter"\)/);
  // The title button still opens it directly, so a keyboard press on it works.
  assert.match(card, /title\.addEventListener\("click", \(\) => openProject\(project\.id\)\)/);
});

test("the project card reads as clickable", () => {
  assert.match(css, /\.project-card \{ cursor: pointer; \}/);
});

test("the projects section opens by default at every width, and a reader's choice wins", () => {
  const block = app.slice(app.indexOf('const projectsBlock = $("board-projects-block");'));
  assert.ok(block, "the projects-block startup block is missing");
  assert.match(block, /tessera\.wip\.projects\.open/);
  // Open at every width unless the reader remembered an explicit "off".
  assert.match(block, /const initial = remembered === "off" \? false : true;/);
  // Setting .open fires toggle, so the default must not be written back as a choice.
  assert.match(block, /if \(projectsBlock\.open === initial\) localStorage\.removeItem/);
});

test("the project grid comes before the create form at every width", () => {
  const block = app.slice(app.indexOf('const projectsBlock = $("board-projects-block");'));
  assert.match(block, /projectsBlock\.append\(create\)/);
  // Guarded so the move only happens when both nodes really are children of the block.
  assert.match(block, /grid\.parentNode === projectsBlock && create\.parentNode === projectsBlock/);
  // No flex-order rule for <details> children: they render through a shadow slot, so
  // order is ignored there.
  assert.doesNotMatch(css, /#board-projects-block\[open\] > \* \{ flex:none; \}/);
});

test("the phone reorder keeps the page scrollable rather than squashing sections", () => {
  const narrow = css.slice(css.indexOf("@media(max-width:760px)"));
  assert.match(narrow, /#page-wip \{ display:flex; flex-direction:column; \}/);
  assert.match(narrow, /#page-wip > \* \{ flex:none; \}/);
  // The projects sit above the in-page Add work form, which the topbar dialog duplicates.
  assert.match(narrow, /#page-wip > #board-projects-block \{ order:2; \}/);
  assert.match(narrow, /#page-wip > #board-note-block \{ order:6; \}/);
});
