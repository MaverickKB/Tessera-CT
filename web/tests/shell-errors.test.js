const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {resolve} = require("node:path");
const {test} = require("node:test");
const vm = require("node:vm");

const context = {
  window: {
    addEventListener: function () {}
  }
};
vm.runInNewContext(readFileSync(resolve(__dirname, "..", "shell-errors.js"), "utf8"), context);
const bannerText = context.window.TesseraShell.bannerText;

test("the missing-object sentence never becomes a banner", () => {
  assert.equal(bannerText("The object can not be found here."), "");
});

test("a replayed runtime interrupt is not a banner", () => {
  assert.equal(bannerText("Interrupted by a previous runtime; not replayed automatically"), "");
});

test("an existing platform mark survives the guard", () => {
  const again = {
    window: {
      addEventListener: function () {},
      TesseraShell: {platform: "linux", service: "child"}
    }
  };
  vm.runInNewContext(readFileSync(resolve(__dirname, "..", "shell-errors.js"), "utf8"), again);
  assert.equal(again.window.TesseraShell.platform, "linux");
  assert.equal(again.window.TesseraShell.service, "child");
  assert.equal(typeof again.window.TesseraShell.bannerText, "function");
});

test("a bare not-found says which side failed", () => {
  assert.equal(bannerText("Not found"), "The core does not have that file.");
  assert.equal(bannerText("Server unreachable; message kept"), "Server unreachable; message kept");
});
