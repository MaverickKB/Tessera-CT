"use strict";
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function picker() {
  const app = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
  const select = {value: '', dataset: {}, options: [],
    replaceChildren() { this.options = []; },
    append(option) { this.options.push(option); }};
  const context = {
    state: {status: {voice: {voices: ['first', 'second'], voice: 'first'}}},
    desktop: null, voice: {running: () => false},
    $: () => select,
    element: (tag, cls, text) => ({textContent: text}),
    localStorage: {getItem() { return 'second'; }},
  };
  vm.createContext(context);
  const start = app.indexOf('  const voicePrefs =');
  const end = app.indexOf('  async function refreshMics()', start);
  vm.runInContext(app.slice(start, end), context);
  return {context, select, run: code => vm.runInContext(code, context)};
}

test('Settings wins over stale storage, with an explicit visible tab override', () => {
  const {context, select, run} = picker();
  run('renderVoicePicker()');
  assert.equal(select.value, 'first');
  assert.equal(JSON.stringify(run('voiceStartOptions()')), '{}');
  select.value = 'second';
  run('chooseVoiceOverride()');
  assert.equal(JSON.stringify(run('voiceStartOptions()')), '{"voice":"second"}');
  assert.match(select.options[1].textContent, /override for this tab/);
  run('renderVoicePicker()');
  assert.equal(select.value, 'second', 'routine polls preserve the explicit choice');
  context.state.status.voice.voice = 'second';
  run('renderVoicePicker()');
  assert.equal(select.value, 'second');
  assert.equal(JSON.stringify(run('voiceStartOptions()')), '{}');
  assert.match(select.options[1].textContent, /Settings/);
});

test('selecting Settings clears the override and a reload starts from Settings', () => {
  const {select, run} = picker();
  run('renderVoicePicker()');
  select.value = 'second';
  run('chooseVoiceOverride()');
  select.value = 'first';
  run('chooseVoiceOverride()');
  assert.equal(JSON.stringify(run('voiceStartOptions()')), '{}');
  const reloaded = picker();
  reloaded.run('renderVoicePicker()');
  assert.equal(reloaded.select.value, 'first');
});

test('native voice shows its Settings choice without offering an ignored override', () => {
  const {context, select, run} = picker();
  context.desktop = {};
  run('renderVoicePicker()');
  assert.equal(select.disabled, true);
  assert.match(select.title, /Uses Settings on the next voice start/);
});
