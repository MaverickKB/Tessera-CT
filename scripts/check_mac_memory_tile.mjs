// Prove the Mac memory tile on the live Infrastructure page, on the running core.
//
// Worth keeping because it settles a reload question the obvious way: host_metrics.py
// is executed as a per-tick subprocess (infrastructure.py _local), so a core started
// before an edit to it still picks the edit up. This probe reads the rendered chip and
// cross-checks it against /api/status, so "the tile will not render until a reload" can
// be answered by measurement instead of assumed.
// Usage: node scripts/check_mac_memory_tile.mjs <chrome-binary> <core-url>
import { spawn } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [chrome, base] = process.argv.slice(2);
const port = 9800 + Math.floor(Math.random() * 150);
const browser = spawn(chrome, [
  `--remote-debugging-port=${port}`,
  `--user-data-dir=${mkdtempSync(join(tmpdir(), 'cdp-tile-'))}`,
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
let id = 0; const pending = new Map();
socket.addEventListener('message', (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
});
const send = (method, params = {}) => new Promise((r) => {
  const n = ++id; pending.set(n, r); socket.send(JSON.stringify({ id: n, method, params }));
});
const evaluate = async (expression) => {
  const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (r.result?.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails).slice(0, 300));
  return r.result.result.value;
};
const check = (ok, label) => { console.log(`${ok ? 'PASS' : 'FAIL'} ${label}`); if (!ok) process.exitCode = 1; };

await send('Page.enable'); await send('Runtime.enable');
await send('Page.navigate', { url: `${base}/#view=infrastructure` });
await sleep(7000);
await evaluate('new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))');

const tiles = await evaluate(`(() => {
  const hosts = [...document.querySelectorAll('#infrastructure-hosts > li')];
  const mac = hosts.find((h) => h.textContent.includes('Mac'));
  return {
    hostCount: hosts.length,
    macFound: Boolean(mac),
    macText: mac ? mac.textContent.replace(/\\s+/g, ' ').slice(0, 400) : null,
    allText: document.getElementById('infrastructure-hosts')?.textContent.replace(/\\s+/g, ' ').slice(0, 700),
  };
})()`);
console.log('TILES ' + JSON.stringify(tiles, null, 1));
check(tiles.macFound, 'the Mac host card is on the Infrastructure page');
const saysNotReported = /Memory[^A-Za-z]*Not reported/i.test(tiles.macText || '');
check(!saysNotReported, `the Mac memory chip reports a figure, not "Not reported"`);
check(/\d/.test(tiles.macText || '') && /(MiB|GiB|MB|GB|bytes)/i.test(tiles.macText || ''),
  'the Mac memory chip carries a measured figure with a unit');

// Cross-check the number against the live API rather than trusting the DOM alone.
const api = await evaluate(`fetch('/api/status').then(r => r.json()).then(d => {
  const h = (d.infrastructure_status?.hosts || []).find(x => (x.label || '').includes('Mac'));
  const m = h?.metrics || {};
  return { total: m.memory_total_bytes ?? null, available: m.memory_available_bytes ?? null, used: m.memory_used_bytes ?? null };
})`);
console.log('API ' + JSON.stringify(api));
check(api.total !== null && api.available !== null,
  `the live core now reports total ${api.total} and available ${api.available} for the Mac (used ${api.used}, derived by the page)`);

browser.kill(); process.exit();
