const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

const context = {window: {}};
vm.runInNewContext(readFileSync(resolve(__dirname, '..', 'format.js'), 'utf8'), context);
const {blocks} = context.window.TesseraFormat;

test('a markdown table becomes a header and rows, not a text wall', () => {
  const parsed = blocks(`| Name | State |
| --- | --- |
| desk | up |
| kiosk | down |`);
  assert.equal(parsed.length, 1);
  assert.equal(parsed[0].type, 'table');
  assert.equal(JSON.stringify(parsed[0].header), JSON.stringify(['Name', 'State']));
  assert.equal(JSON.stringify(parsed[0].rows), JSON.stringify([['desk', 'up'], ['kiosk', 'down']]));
});

test('fenced code stays literal and bold is marked, not left as stars', () => {
  const fence = "```";
  const source = [
    "Use **speaker** when nothing is paired.",
    "",
    fence,
    "| not | a table |",
    fence,
  ].join("\n");
  const parsed = blocks(source);
  assert.equal(parsed[0].type, 'paragraph');
  assert.equal(parsed[0].inlines[1].type, 'strong');
  assert.equal(parsed[0].inlines[1].text, 'speaker');
  assert.equal(parsed[1].type, 'code');
  assert.equal(parsed[1].text, '| not | a table |');
});

test('a list is items, not one preformatted wall', () => {
  const parsed = blocks(`- first
- second`);
  assert.equal(parsed[0].type, 'list');
  assert.equal(parsed[0].ordered, false);
  assert.equal(parsed[0].items.length, 2);
  assert.equal(parsed[0].items[0][0].text, 'first');
});
