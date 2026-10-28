"use strict";
const assert = require("node:assert/strict");
const {readFileSync} = require("node:fs");
const {resolve} = require("node:path");
const {test} = require("node:test");
const vm = require("node:vm");

// Load the real module into a bare context, then hand it fakes for the two host
// objects it touches (window.location/history and localStorage).
// URL is a host global the module relies on; supply it rather than assume
// the bare context has it (it does not, which made the strip test read as a bug).
const context = {window: {}, URL};
vm.runInNewContext(readFileSync(resolve(__dirname, "..", "authkey.js"), "utf8"), context);
const authKey = context.window.TesseraAuthKey;

function fakeStorage(initial) {
  const map = new Map(Object.entries(initial || {}));
  return {
    getItem: key => (map.has(key) ? map.get(key) : null),
    setItem: (key, value) => map.set(key, String(value)),
    _map: map,
  };
}

function fakeWindow(href) {
  const replaced = [];
  return {
    location: {href},
    history: {replaceState: (_a, _b, url) => replaced.push(url)},
    _replaced: replaced,
  };
}

test("no stored key means no auth header and no query suffix", () => {
  const store = fakeStorage();
  assert.equal(authKey.headers(store).Accept, "application/json");
  assert.equal(authKey.headers(store)["X-Tessera-Key"], undefined);
  assert.equal(authKey.withKey("/api/sessions", store), "/api/sessions");
  assert.equal(authKey.withKey("/api/sessions?x=1", store), "/api/sessions?x=1");
});

test("a stored key is sent as X-Tessera-Key", () => {
  const store = fakeStorage({"tessera.auth.key": "abc123"});
  assert.equal(authKey.headers(store)["X-Tessera-Key"], "abc123");
});

test("the stream URL gets ?key= so EventSource can authenticate", () => {
  const store = fakeStorage({"tessera.auth.key": "abc123"});
  assert.equal(authKey.withKey("/api/sessions/x/events", store), "/api/sessions/x/events?key=abc123");
  // An existing query keeps working: the key is appended, not substituted.
  assert.equal(authKey.withKey("/api/sessions/x/events?messages=skip", store),
               "/api/sessions/x/events?messages=skip&key=abc123");
});

test("a key with reserved characters is encoded, not injected raw", () => {
  const store = fakeStorage({"tessera.auth.key": "a b&c=d"});
  assert.equal(authKey.withKey("/api/s", store), "/api/s?key=a%20b%26c%3Dd");
});

test("?key= on the URL is stored once and then stripped from the address", () => {
  const store = fakeStorage();
  const win = fakeWindow("http://127.0.0.1:8740/?key=secret#frag");
  const taken = authKey.takeKeyFromLocation(win, store);
  assert.equal(taken, "secret");
  assert.equal(store.getItem("tessera.auth.key"), "secret");
  // The key must not be left in the address bar or history.
  assert.equal(win._replaced.length, 1);
  assert.ok(!win._replaced[0].includes("secret"), "the key must not remain in the URL");
  assert.ok(win._replaced[0].includes("#frag"), "the fragment is preserved");
});

test("a page loaded with no ?key= leaves the stored key alone", () => {
  const store = fakeStorage({"tessera.auth.key": "already"});
  const win = fakeWindow("http://127.0.0.1:8740/");
  assert.equal(authKey.takeKeyFromLocation(win, store), "");
  assert.equal(store.getItem("tessera.auth.key"), "already");
  assert.equal(win._replaced.length, 0, "no history rewrite when nothing was supplied");
});

test("storage that throws (private mode) degrades to no key, not a crash", () => {
  const throwing = {
    getItem: () => { throw new Error("denied"); },
    setItem: () => { throw new Error("denied"); },
  };
  assert.equal(authKey.storedKey(throwing), "");
  assert.equal(authKey.headers(throwing).Accept, "application/json");
  assert.equal(authKey.headers(throwing)["X-Tessera-Key"], undefined);
  assert.equal(authKey.withKey("/api/s", throwing), "/api/s");
  // Supplying a key on the URL still must not crash the page.
  const win = fakeWindow("http://127.0.0.1:8740/?key=x");
  assert.equal(authKey.takeKeyFromLocation(win, throwing), "x");
});
