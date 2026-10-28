"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const css = fs.readFileSync(path.join(__dirname, "..", "app.css"), "utf8");
const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");

// Every top-level block for this exact media query, concatenated (the sheet repeats some queries).
function blocks(query) {
  const found = [];
  let from = 0;
  for (;;) {
    const start = css.indexOf(`@media ${query} {`, from);
    if (start < 0) break;
    let depth = 0;
    let i = css.indexOf("{", start);
    for (; i < css.length; i += 1) {
      if (css[i] === "{") depth += 1;
      if (css[i] === "}" && --depth === 0) break;
    }
    found.push(css.slice(start, i + 1));
    from = i + 1;
  }
  assert.ok(found.length, `missing @media ${query}`);
  return found;
}
const block = (query) => blocks(query).join("\n");

test("viewport opts into safe-area insets", () => {
  assert.match(html, /name="viewport"[^>]*viewport-fit=cover/);
});

test("no-session desktop grid is scoped to wide screens", () => {
  const outside = [...blocks("(min-width: 761px)"), ...blocks("(max-width: 760px)")].reduce((rest, b) => rest.replace(b, ""), css);
  assert.doesNotMatch(outside, /body\.no-session \.workspace\s*\{[^}]*grid-template-columns/);
});

test("the menu stays reachable on a phone no matter how far the page scrolls", () => {
  // A page installed as a web app has no browser chrome. Once the topbar scrolled
  // away with a long page, the only way back was unreachable. The topbar holds the
  // menu control, so it has to stay put.
  assert.match(css, /\.topbar\s*\{[^}]*position:\s*sticky[^}]*top:\s*0/);
  assert.match(css, /\.topbar\s*\{[^}]*z-index:\s*20/);
});

test("phone breakpoint makes drawers off-canvas, 16px inputs, safe areas", () => {
  const phone = block("(max-width: 760px)");
  assert.match(phone, /overflow-x:\s*hidden/);
  assert.match(phone, /font-size:\s*16px/);
  assert.match(phone, /env\(safe-area-inset-bottom\)/);
  assert.match(phone, /translateX\(-105%\)/);
  assert.match(phone, /visibility:\s*hidden/);
  assert.match(phone, /--navigation-width:\s*0px/);
  assert.match(phone, /body\.show-navigation \.app-navigation/);
});

test("short viewport block keeps the thread visible", () => {
  const short = block("(max-height: 520px)");
  assert.match(short, /--topbar-height:\s*52px/);
  assert.match(short, /#voice-detail[^{]*\{\s*display:\s*none/);
  assert.doesNotMatch(short, /\.voice-controls[^{]*\{\s*display:\s*none/, "voice must stay reachable on phones");
});

test("session title stays one line and does not break inside a word", () => {
  assert.match(css, /\.session-heading h2[^{]*\{[^}]*white-space:\s*nowrap/);
  assert.match(css, /\.session-heading h2[^{]*\{[^}]*text-overflow:\s*ellipsis/);
  assert.doesNotMatch(css, /\.session-heading h2[^{]*\{[^}]*overflow-wrap:\s*anywhere/);
  assert.match(html, /id="session-more"/);
});

test("stylesheet braces are balanced", () => {
  let depth = 0;
  for (const ch of css) { if (ch === "{") depth += 1; if (ch === "}") depth -= 1; assert.ok(depth >= 0); }
  assert.equal(depth, 0);
});

test("thinking budget stays available in short windows and its narrow menu is anchored on screen", () => {
  const short = block("(max-height: 520px)");
  assert.match(short, /:not\(#thinking-budget-control\)/);
  const narrow = css.slice(css.indexOf("@container conversation (max-width: 720px)"));
  assert.match(narrow, /\.thinking-budget-popover\s*\{[^}]*left:\s*0;[^}]*right:\s*auto/);
  assert.match(narrow, /\.thinking-budget-popover\s*\{[^}]*max-height:[^}]*overflow-y:\s*auto/);
});
