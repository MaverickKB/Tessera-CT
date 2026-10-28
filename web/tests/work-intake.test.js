const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

function load(fields = {}) {
  const context = {
    window: {},
    document: { getElementById: (id) => fields[id] },
  };
  vm.createContext(context);
  vm.runInContext(
    fs.readFileSync(path.join(__dirname, "..", "work-intake.js"), "utf8"),
    context,
  );
  return context.window.TesseraWorkIntake;
}

test("quick reports stay concise and optional problem prompts carry their meaning", () => {
  const { composeNote } = load();
  assert.equal(
    composeNote({ "board-note-body": "A quick observation" }),
    "A quick observation",
  );
  assert.equal(
    composeNote({
      "work-actual": "It jumps",
      "work-expected": "Stay still",
      "work-steps": "Hover",
    }),
    "What happened:\nIt jumps\n\nExpected result:\nStay still\n\nSteps to reproduce:\nHover",
  );
});

test("successful submission preserves edits and new files added while evidence uploads", async () => {
  const fields = {};
  for (const id of [
    "board-note-body",
    "work-actual",
    "work-expected",
    "work-steps",
    "work-context",
  ])
    fields[id] = { value: "", hidden: false };
  fields["work-context-label"] = { hidden: false };
  fields["board-note-body"].value = "Original report";
  const { WorkIntake } = load(fields);
  const intake = Object.create(WorkIntake.prototype);
  const first = { file: { name: "first.png" } };
  intake.files = [first];
  intake.renderFiles = () => {};
  let finish;
  intake.options = {
    session: () => "one",
    upload: () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  };
  const preparing = intake.prepare();
  fields["board-note-body"].value = "A new draft";
  const second = { file: { name: "second.png" } };
  intake.files.push(second);
  finish({
    id: "a".repeat(32),
    name: "first.png",
    media_type: "image/png",
    bytes: 70,
  });
  const snapshot = await preparing;
  assert.equal(snapshot.note, "Original report");
  assert.equal(snapshot.evidence[0].size, 70);
  assert.equal(snapshot.evidence[0].session_slug, "one");
  intake.saved(snapshot);
  assert.equal(fields["board-note-body"].value, "A new draft");
  assert.deepEqual(intake.files, [second]);
});
