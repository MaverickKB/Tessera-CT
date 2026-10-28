// Settings-render proof from the page against the live core.
// Read-only: navigates, measures what a human sees, submits nothing.
// Usage: node scripts/check_settings_render.mjs <chrome-binary> <core-url>
import { spawn } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [chrome, base] = process.argv.slice(2);
const port = 9900 + Math.floor(Math.random() * 90);
const browser = spawn(chrome, [
  `--remote-debugging-port=${port}`,
  `--user-data-dir=${mkdtempSync(join(tmpdir(), 'cdp-settings-'))}`,
  '--no-first-run', '--no-default-browser-check', 'about:blank',
], { stdio: 'ignore' });

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let targets;
for (let i = 0; i < 60 && !targets; i++) {
  await sleep(200);
  targets = await fetch(`http://127.0.0.1:${port}/json`).then((r) => r.json()).catch(() => null);
}
const socket = new WebSocket(targets.find((t) => t.type === 'page').webSocketDebuggerUrl);
await new Promise((r) => socket.addEventListener('open', r));
let id = 0;
const pending = new Map();
const posts = [];
socket.addEventListener('message', (event) => {
  const m = JSON.parse(event.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  if (m.method === 'Network.requestWillBeSent' && m.params.request.method === 'POST') {
    posts.push(`${m.params.request.url} ${(m.params.request.postData || '').slice(0, 200)}`);
  }
});
const send = (method, params = {}) => new Promise((r) => {
  const n = ++id; pending.set(n, r); socket.send(JSON.stringify({ id: n, method, params }));
});
const evaluate = async (expression) => {
  const reply = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (reply.result?.exceptionDetails) throw new Error(JSON.stringify(reply.result.exceptionDetails).slice(0, 400));
  return reply.result.result.value;
};
const check = (ok, label) => { console.log(`${ok ? 'PASS' : 'FAIL'} ${label}`); if (!ok) process.exitCode = 1; };

await send('Network.enable');
await send('Page.enable');
await send('Runtime.enable');
await send('Page.navigate', { url: `${base}/#view=settings` });
await sleep(9000);

const seen = await evaluate(`(() => {
  const root = document.getElementById('settings-root');
  const page = document.getElementById('page-settings');
  const nav = document.querySelectorAll('#settings-root nav a, #settings-root nav button');
  const headings = [...document.querySelectorAll('#settings-root h2, #settings-root h3')].map(h => h.textContent.trim());
  const selects = document.querySelectorAll('#settings-root select').length;
  const inputs = document.querySelectorAll('#settings-root input').length;
  const buttons = [...document.querySelectorAll('#settings-root button')].map(b => b.textContent.trim()).filter(Boolean);
  const text = (root && root.innerText || '').replace(/\\s+/g, ' ').trim();
  return {
    pageHidden: page ? page.hidden : null,
    rootChildren: root ? root.children.length : 0,
    navCount: nav.length,
    headings,
    selects, inputs,
    buttons: buttons.slice(0, 24),
    textLen: text.length,
    textHead: text.slice(0, 700),
    hasProviderWord: /provider|model/i.test(text),
  };
})()`);

console.log('MEASURED ' + JSON.stringify(seen, null, 1));
check(seen.pageHidden === false, 'settings page is visible at #view=settings');
check(seen.rootChildren > 0, 'settings-root rendered content');
check(seen.navCount > 1, 'section navigation rendered (more than "All settings")');
check(seen.selects + seen.inputs > 0, 'editable controls rendered, not just labels');
check(seen.hasProviderWord, 'provider/model language is visible to a human');
check(posts.length === 0, `no write was submitted by this probe (POSTs: ${JSON.stringify(posts)})`);

socket.close();
browser.kill();
