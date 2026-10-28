"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const app = fs.readFileSync(path.join(__dirname, "..", "app.js"), "utf8");

function source(name) {
  const start = app.indexOf("function " + name + "(");
  assert.ok(start >= 0);
  let depth = 0;
  for (let i = app.indexOf("{", start); i < app.length; i += 1) {
    if (app[i] === "{") depth += 1;
    if (app[i] === "}") {
      depth -= 1;
      if (depth === 0) return app.slice(start, i + 1);
    }
  }
  throw new Error("Unclosed function: " + name);
}

function controls() {
  const nodes = {};
  for (const id of ["thinking-budget", "thinking-budget-enabled", "thinking-budget-value", "thinking-budget-summary", "thinking-budget-control"]) {
    nodes[id] = {value: "4096", checked: false, disabled: false, textContent: "", dataset: {}, style: {values: {}, setProperty(name, value) { this.values[name] = value; }}};
  }
  const session = {slug: "test", reasoning: "low", thinking_budget: 0};
  const context = {
    state: {status: {}, chatNames: []},
    activeSession: () => session,
    $: (id) => nodes[id],
    credentialOf: () => null,
    renders: 0,
  };
  vm.createContext(context);
  vm.runInContext(source("renderThinkingEnergy") + "\n" + source("renderThinkingBudget") + "\n" + source("renderModelPickerOnce") +
    "\nfunction renderModelPicker() { renders += 1; renderThinkingBudget(); }", context);
  return {nodes, session, context};
}

test("saved budget changes invalidate the picker cache and show the selected value", () => {
  const {nodes, session, context} = controls();
  context.renderModelPickerOnce();
  assert.equal(nodes["thinking-budget-summary"].textContent, "Thinking budget: engine default");
  assert.equal(nodes["thinking-budget"].disabled, true);
  session.thinking_budget = 2048;
  context.renderModelPickerOnce();
  assert.equal(context.renders, 2);
  assert.equal(nodes["thinking-budget-summary"].textContent, "Thinking budget: 2,048 tokens");
  assert.equal(nodes["thinking-budget"].disabled, false);
  assert.equal(nodes["thinking-budget-enabled"].checked, true);
  session.thinking_budget = 0;
  context.renderModelPickerOnce();
  assert.equal(nodes["thinking-budget-summary"].textContent, "Thinking budget: engine default");
  assert.equal(nodes["thinking-budget-enabled"].checked, false);
});

test("turning reasoning off preserves the saved budget and clearly disables its controls", () => {
  const {nodes, session, context} = controls();
  session.thinking_budget = 4096;
  context.renderModelPickerOnce();
  session.reasoning = "off";
  context.renderModelPickerOnce();
  assert.equal(nodes["thinking-budget-summary"].textContent, "Thinking budget: off");
  assert.equal(nodes["thinking-budget-enabled"].disabled, true);
  assert.equal(nodes["thinking-budget"].disabled, true);
  assert.equal(session.thinking_budget, 4096);
  session.reasoning = "high";
  context.renderModelPickerOnce();
  assert.equal(nodes["thinking-budget"].disabled, false);
});


test("slider energy increases with the selected budget and turns off with the control", () => {
  const {nodes, context} = controls();
  const slider = nodes["thinking-budget"];
  const control = nodes["thinking-budget-control"];
  slider.value = "256";
  context.renderThinkingEnergy();
  const slow = parseFloat(control.style.values["--thinking-period"]);
  const small = parseFloat(control.style.values["--thinking-halo"]);
  slider.value = "32768";
  context.renderThinkingEnergy();
  assert.ok(parseFloat(control.style.values["--thinking-period"]) < slow);
  assert.ok(parseFloat(control.style.values["--thinking-halo"]) > small);
  assert.equal(control.style.values["--thinking-fill"], "100.00%");
  slider.disabled = true;
  context.renderThinkingEnergy();
  assert.equal(control.dataset.energized, "false");
});
