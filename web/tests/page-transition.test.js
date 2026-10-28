const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const app = fs.readFileSync(path.join(__dirname, '..', 'app.js'), 'utf8');
const start = app.indexOf('  function revealPage(');
const end = app.indexOf('\n  function agentName(', start);
function harness() {
  const animations = [];
  function animate(frames, options) {
    const animation = {frames, options, cancelled: false, cancel() { this.cancelled = true; }};
    animations.push(animation);
    return animation;
  }
  const page = {animate};
  const seam = {animate, querySelectorAll: () => [{animate}, {animate}, {animate}, {animate}]};
  const preference = {matches: false};
  const context = {state: {pageAnimations: []}, window: {matchMedia: () => preference}, $: id => id === 'page-seam' ? seam : page};
  vm.createContext(context);
  vm.runInContext(app.slice(start, end), context);
  return {context, animations, page, preference};
}
test('rapid navigation cancels the previous transition before revealing the new page', () => {
  const h = harness();
  h.context.revealPage('wip');
  const previous = [...h.animations];
  h.context.revealPage('settings');
  assert(previous.every(animation => animation.cancelled));
  assert(h.context.state.pageAnimations.every(animation => !animation.cancelled));
  for (const animation of h.animations) {
    for (const frame of animation.frames) {
      assert.equal(frame.transform, undefined, 'page geometry must remain stable');
      assert.equal(frame.height, undefined);
    }
  }
});
test('reduced motion cancels any old effect and leaves navigation without new animation', () => {
  const h = harness();
  h.context.revealPage('wip');
  const count = h.animations.length;
  h.preference.matches = true;
  h.context.revealPage('settings');
  assert.equal(h.animations.length, count);
  assert(h.animations.every(animation => animation.cancelled));
  assert.equal(h.context.state.pageAnimations.length, 0);
});
test('a surface without Web Animations keeps the page immediately available', () => {
  const h = harness();
  delete h.page.animate;
  h.context.revealPage('chat');
  assert.equal(h.animations.length, 0);
});
