const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {resolve} = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
    this.className = "";
    this._text = "";
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  append(...nodes) { this.children.push(...nodes); }
  get textContent() { return this._text + this.children.map((child) => child.textContent || "").join(""); }
  set textContent(value) { this._text = String(value); this.children = []; }
}

test("board chrome keeps metric text and only draws a sparkline from two samples", () => {
  const document = {createElement: (tag) => new Element(tag)};
  const context = {document, window: {}};
  vm.createContext(context);
  vm.runInContext(readFileSync(resolve(__dirname, "..", "design.js"), "utf8"), context);
  const design = context.window.TesseraDesign;
  assert.equal(design.metricRow("CPU", "13%").textContent, "CPU13%");
  assert.equal(design.sparkline([1]), null);
  assert.equal(design.sparkline([Number.NaN]), null);
  assert.equal(design.emptyState("No hosts").textContent, "No hosts");
  const spark = design.sparkline([1, 3], "mem");
  assert.ok(spark);
  assert.equal(spark.children[0].tag, "svg");
  assert.match(spark.children[0].children[0].attributes.points, /,/);
  const row = design.metricRow("Memory", "1.0 GiB", {ratio: 0.25, tone: "mem"});
  assert.equal(row.textContent, "Memory1.0 GiB");
  const meter = row.children.at(-1);
  assert.equal(meter.className, "ds-meter");
  const fill = meter.children[0];
  assert.equal(fill.className, "ds-meter-fill");
  assert.equal(fill.dataset.tone, "mem");
  // viewBox is 100 units wide, so the geometry width is the used percentage.
  assert.equal(fill.attributes.width, "25");
  const sample = design.metricRow("Memory", "12 GiB of 16 GiB", {ratio: 0.75, tone: "warn"}).children.at(-1).children[0];
  assert.equal(sample.className, "ds-meter-fill");
  assert.equal(sample.attributes.width, "75");
  assert.equal(sample.dataset.tone, "warn");
});

test("meter stylesheet paints the fill from the geometry attribute, not a zero width", () => {
  const css = readFileSync(resolve(__dirname, "..", "design.css"), "utf8");
  assert.match(css, /\.ds-meter-fill\[data-tone="ok"\]\s*\{[^}]*fill:\s*var\(--ds-green\)/);
  assert.match(css, /\.ds-meter-fill\[data-tone="warn"\]\s*\{[^}]*fill:\s*var\(--ds-amber\)/);
  assert.match(css, /\.ds-meter-fill\[data-tone="danger"\]\s*\{[^}]*fill:\s*var\(--ds-red\)/);
  assert.doesNotMatch(css, /\.ds-meter-fill[^{]*\{[^}]*width:\s*0/);
  // The WIP page leads with pressure and projects, not a row of counters.
  assert.match(css, /\.pressure-item\[data-state="high"\]\s*\{[^}]*border-left-color:\s*var\(--ds-red\)/);
  assert.match(css, /\.project-grid\s*\{[^}]*display:\s*grid/);
  assert.doesNotMatch(css, /#wip-kpis/);
});
