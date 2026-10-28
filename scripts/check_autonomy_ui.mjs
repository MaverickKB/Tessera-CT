// Headless check of the autonomy UI against a running core over CDP.
// Usage: node scripts/check_autonomy_ui.mjs <chrome-binary> <core-url> <session-slug> [--approve <approval-id>]
import { spawn } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [chrome, base, slug, flag, approvalId] = process.argv.slice(2);
const port = 9300 + Math.floor(Math.random() * 500);
const browser = spawn(chrome, [`--remote-debugging-port=${port}`, `--user-data-dir=${mkdtempSync(join(tmpdir(), 'cdp-'))}`,
  '--window-size=1400,1000', 'about:blank'], { stdio: 'ignore' });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let targets;
for (let i = 0; i < 50 && !targets; i++) {
  await sleep(200);
  targets = await fetch(`http://127.0.0.1:${port}/json`).then((r) => r.json()).catch(() => null);
}
const socket = new WebSocket(targets.find((t) => t.type === 'page').webSocketDebuggerUrl);
await new Promise((r) => socket.addEventListener('open', r));
let id = 0;
const pending = new Map();
const requests = [];
socket.addEventListener('message', (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) { pending.get(message.id)(message); pending.delete(message.id); }
  if (message.method === 'Network.requestWillBeSent') requests.push(`${message.params.request.method} ${message.params.request.url} ${message.params.request.postData || ''}`);
});
const send = (method, params = {}) => new Promise((r) => { const n = ++id; pending.set(n, r); socket.send(JSON.stringify({ id: n, method, params })); });
const evaluate = async (expression) => (await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })).result.result.value;
const check = (ok, label) => { console.log(`${ok ? 'PASS' : 'FAIL'} ${label}`); if (!ok) process.exitCode = 1; };

await send('Network.enable');
await send('Page.enable');
await send('Page.navigate', { url: `${base}/#view=chat&session=${encodeURIComponent(slug)}` });
await sleep(3000);
await evaluate('window.dispatchEvent(new Event("blur")); true');
await sleep(300);
await evaluate('window.dispatchEvent(new Event("focus")); true');
await sleep(2500);
const presence = requests.filter((r) => r.includes('/presence'));
check(presence.some((r) => r.includes('"leave"')) && presence.some((r) => r.includes('"arrive"')), `presence reported: ${presence.length} calls`);
await evaluate('document.getElementById("desk-toggle").click(); true');
await sleep(8000);
const desk = await evaluate(`(() => { const img = document.querySelector('#desk-view img');
  return { open: document.getElementById('desk-dialog').open, tabs: [...document.querySelectorAll('#desk-tabs button')].map(b => b.textContent),
           width: img && !img.hidden ? img.naturalWidth : 0, status: document.getElementById('desk-status').textContent }; })()`);
check(desk.open && desk.width > 0, `desk window shows ${desk.tabs.join('/')}: ${desk.width}px wide, "${desk.status}"`);
const visible = await evaluate(`[...document.querySelectorAll('#messages article')].map(a => a.className + ': ' + a.textContent.slice(0, 80))`);
console.log('VISIBLE ' + JSON.stringify(visible));
if (flag === '--approve') {
  const clicked = await evaluate(`(() => { for (const card of document.querySelectorAll('.approval-card[data-approval-id="${approvalId}"]')) {
      const button = [...card.querySelectorAll('button')].find(b => b.textContent === 'Approve'); if (button) { button.click(); return true; } } return false; })()`);
  await sleep(1500);
  check(clicked && requests.some((r) => r.includes(`/api/approvals/${approvalId}`)), `approved ${approvalId} from the chat card`);
}
browser.kill();
process.exit();
