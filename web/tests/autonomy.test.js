const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

const source = readFileSync(resolve(__dirname, '..', 'autonomy.js'), 'utf8');
const context = {window: {}};
vm.runInNewContext(source, context);
const {hiddenIndices} = context.window.TesseraAutonomy;

test('wake inputs, silent wake turns and their tool traffic stay hidden; spoken wakes and operator turns show', () => {
  const log = [
    {i: 0, kind: 'user', meta: {turn: 1}},
    {i: 1, kind: 'tool', meta: {turn: 1}},
    {i: 2, kind: 'agent', meta: {turn: 1}},
    {i: 3, kind: 'user', meta: {turn: 2, wake: {events: ['timer']}}},
    {i: 4, kind: 'tool', meta: {turn: 2}},
    {i: 5, kind: 'echo', meta: {turn: 2}},
    {i: 6, kind: 'agent', meta: {turn: 2, silent: true}},
    {i: 7, kind: 'user', meta: {turn: 3, wake: {events: ['presence']}}},
    {i: 8, kind: 'agent', meta: {turn: 3, proactive: true}},
    {i: 10, kind: 'user', meta: {turn: 4, wake: {events: ['timer']}}},
    {i: 11, kind: 'agent', meta: {turn: 4, tool_calls: [{name: 'rest'}]}},
    {i: 12, kind: 'note', meta: {approval: {id: 'ap-2', state: 'pending'}}},
    {i: 9, kind: 'note', meta: {approval_result: {id: 'ap-1', state: 'approved'}}},
  ];
  assert.deepEqual([...hiddenIndices(log)].sort((a, b) => a - b), [3, 4, 5, 6, 7, 9, 10, 11]);
});

test('a voice transcript superseded by its longer re-send is hidden', () => {
  const log = [
    {i: 0, kind: 'user', meta: {voice_transcript: true}},
    {i: 1, kind: 'user', meta: {voice_transcript: true, supersedes: 0}},
    {i: 2, kind: 'agent', meta: {voice_transcript: true}},
  ];
  assert.deepEqual([...hiddenIndices(log)], [0]);
});
