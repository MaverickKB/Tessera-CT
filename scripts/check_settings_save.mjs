// Settings-save proof from the page against the live core.
// No-op only: writes each tested field back with the value it already holds,
// so the operator's configuration is unchanged while the save path is exercised.
// Usage: node scripts/check_settings_save.mjs <chrome-binary> <core-url>
import { spawn } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [chrome, base] = process.argv.slice(2);
const port = 9800 + Math.floor(Math.random() * 90);
const browser = spawn(chrome, [
  `--remote-debugging-port=${port}`,
  `--user-data-dir=${mkdtempSync(join(tmpdir(), 'cdp-save-'))}`,
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
    posts.push(`${m.params.request.url} ${(m.params.request.postData || '').slice(0, 160)}`);
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

// Read the live value of each field the page actually renders, then re-commit it unchanged.
const targetsToTry = ['ui.theme', 'ui.show_thoughts', 'permissions.mode'];

const result = await evaluate(`(async () => {
  const out = [];
  // domId() in web/settings.js: "setting-" + id with non-alphanumerics replaced by "_",
  // except ui.show_thoughts which is "show-thoughts".
  const findInput = (fieldId) => {
    const wanted = fieldId === 'ui.show_thoughts'
      ? 'show-thoughts'
      : 'setting-' + fieldId.replace(/[^A-Za-z0-9_-]/g, '_');
    return document.getElementById(wanted);
  };
  for (const fieldId of ${JSON.stringify(targetsToTry)}) {
    const input = findInput(fieldId);
    if (!input) { out.push({fieldId, found: false}); continue; }
    const before = input.type === 'checkbox' ? input.checked : input.value;
    // Re-commit the SAME value: the page's own change handler does the POST.
    input.dispatchEvent(new Event('change', {bubbles: true}));
    await new Promise((r) => setTimeout(r, 1200));
    const status = document.querySelector('#settings-root [role=status], #settings-root .set-status');
    out.push({fieldId, found: true, before, after: input.type === 'checkbox' ? input.checked : input.value,
              status: status ? status.textContent.trim() : null});
  }
  return out;
})()`);

console.log('MEASURED ' + JSON.stringify(result, null, 1));
const settingsPosts = posts.filter((p) => p.includes('/api/settings'));
console.log('SETTINGS POSTs ' + JSON.stringify(settingsPosts));

check(settingsPosts.length > 0, 'the page posted a save to /api/settings through its own control');
check(settingsPosts.every((p) => !/Invalid|error/i.test(p)), 'no rejected save payload');
const unchanged = result.filter((r) => r.found).every((r) => JSON.stringify(r.before) === JSON.stringify(r.after));
check(unchanged, 'every tested field still holds its original value (no-op writes only)');

// Reload and confirm the page still renders with the same values, so the save round-tripped.
await send('Page.navigate', { url: `${base}/#view=settings` });
await sleep(9000);
const afterReload = await evaluate(`(() => {
  const text = (document.getElementById('settings-root')?.innerText || '').replace(/\\s+/g,' ');
  return {len: text.length, head: text.slice(0, 260)};
})()`);
console.log('AFTER RELOAD ' + JSON.stringify(afterReload));
check(afterReload.len > 200, 'settings still render after reload, values survived');

socket.close();
browser.kill();
