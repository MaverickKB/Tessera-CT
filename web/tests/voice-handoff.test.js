const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function fixture(events) {
  const timers = [];
  const context = {window:{}, setTimeout: callback => { timers.push(callback); return timers.length; }, clearTimeout: () => {}};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'voice.js'), 'utf8'), context);
  const voice = new context.window.TesseraVoice({events});
  const run = {slug:'one', settings:{voice_id:'lease'}, ready:true, sources:new Set(),
    pending:0, taskPollNeeded:true, answering:false, userSpeaking:false};
  const sent = [];
  voice.current = run;
  voice.send = (owner, frame) => { assert.equal(owner, run); sent.push(frame); };
  return {voice, run, sent, timers};
}

test('background completion waits for speech and playback, then delivers once', async () => {
  let polls = 0;
  const setup = fixture(async () => {
    polls += 1;
    setup.run.userSpeaking = true; // A new utterance begins during the HTTP request.
    return {send:[{type:'conversation.item.create'}, {type:'response.create'}]};
  });
  const {voice, run, sent, timers} = setup;
  voice.pollTasks(run);
  await timers.shift()();
  assert.equal(polls, 1);
  assert.equal(sent.length, 0);
  run.userSpeaking = false;
  run.sources.add('audio');
  await timers.shift()();
  assert.equal(sent.length, 0);
  run.sources.clear();
  await timers.shift()();
  assert.equal(sent.length, 2);
  assert.equal(timers.length, 0);
});

test('ending a call prevents pending completion from entering a later call', async () => {
  const {voice, run, sent, timers} = fixture(async () => ({send:[{type:'response.create'}]}));
  voice.pollTasks(run);
  voice.current = null;
  await timers.shift()();
  assert.equal(sent.length, 0);
});


test('a new task accepted during an older poll retains its completion watcher', async () => {
  const setup = fixture(async () => {
    setup.run.taskPollRevision += 1;
    setup.run.taskPollNeeded = true;
    return {send:[]};
  });
  setup.run.taskPollRevision = 1;
  setup.voice.pollTasks(setup.run);
  await setup.timers.shift()();
  assert.equal(setup.run.taskPollNeeded, true);
  assert.equal(setup.timers.length, 1);
  setup.voice.current = null;
});
