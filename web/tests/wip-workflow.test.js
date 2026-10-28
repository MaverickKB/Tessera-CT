const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const source = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");
const start = source.indexOf("  function boardLane(");
const end = source.indexOf("  function renderWorkOverview(", start);
const context = {};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
const filters = { history: false, project: "", focus: "all", query: "" };
test("show all includes open and completed work, including cancelled work", () => {
  for (const status of [
    "queued",
    "running",
    "blocked",
    "failed",
    "succeeded",
    "cancelled",
  ]) {
    const closed = ["succeeded", "cancelled"].includes(status);
    assert.equal(context.matchesBoardWork({ status }, {}, filters), !closed);
    assert.equal(
      context.matchesBoardWork({ status }, {}, { ...filters, history: true }),
      true,
    );
  }
});
test("work lanes distinguish questions, real activity, available work and owned waiting work", () => {
  assert.equal(
    context.boardLane({ status: "queued", question_pending: true }, {}),
    "attention",
  );
  assert.equal(
    context.boardLane({ status: "running" }, { owner: "agent" }),
    "running",
  );
  assert.equal(
    context.boardLane({ status: "queued" }, { claimable: true }),
    "available",
  );
  assert.equal(
    context.boardLane(
      { status: "queued" },
      { claimable: false, owner: "agent" },
    ),
    "waiting",
  );
  assert.equal(context.boardLane({ status: "unknown" }, {}), "other");
});
test("project, focus and text filters compose without revealing completed cards", () => {
  const card = { status: "queued", ordinal: 42 };
  const overlay = {
    project: "research",
    owner: "local-agent",
    title: "Investigate latency",
    note: "Check prefill",
    claimable: false,
  };
  assert(
    context.matchesBoardWork(card, overlay, {
      ...filters,
      project: "research",
      query: "prefill",
      focus: "waiting",
    }),
  );
  assert(
    !context.matchesBoardWork(card, overlay, { ...filters, project: "other" }),
  );
  assert(
    !context.matchesBoardWork(card, overlay, {
      ...filters,
      focus: "available",
    }),
  );
  assert(
    !context.matchesBoardWork({ ...card, status: "succeeded" }, overlay, {
      ...filters,
      query: "latency",
    }),
  );
});
const graph = { window: {} };
vm.createContext(graph);
vm.runInContext(
  fs.readFileSync(path.join(__dirname, "..", "project-canvas.js"), "utf8"),
  graph,
);
test("group movement includes descendants and stacking selection excludes nested duplicate roots", () => {
  const nodes = [
    { id: "g", kind: "group" },
    { id: "a", parent: "g" },
    { id: "b", parent: "a" },
    { id: "other" },
  ];
  const api = graph.window.TesseraProjectCanvas;
  assert.deepEqual([...api.descendants(nodes, new Set(["g"]))].sort(), [
    "a",
    "b",
    "g",
  ]);
  assert.deepEqual(
    Array.from(api.roots(nodes, new Set(["g", "a", "other"])), (n) => n.id),
    ["g", "other"],
  );
});

test("an older canvas read cannot replace a newer saved edit or an in-flight move", async () => {
  const canvas = Object.create(
    graph.window.TesseraProjectCanvas.ProjectCanvas.prototype,
  );
  canvas.data = { canvas: { version: 3, nodes: [], links: [] } };
  canvas.options = {
    api: async () => ({ canvas: { version: 2, nodes: [], links: [] } }),
  };
  canvas.notice = {};
  canvas.paint = () =>
    assert.fail("A stale read must leave visible pieces alone");
  canvas.pending = 0;
  await canvas.refresh();
  assert.equal(canvas.data.canvas.version, 3);
  canvas.options.api = async () => ({
    canvas: { version: 4, nodes: [], links: [] },
  });
  canvas.pending = 1;
  await canvas.refresh();
  assert.equal(canvas.data.canvas.version, 3);
});
