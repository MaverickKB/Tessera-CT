// Board-hands proof from the page against the live core: create, edit, file, remove.
// Every action goes through the page's own controls, not the API directly.
// Usage: node scripts/check_board_hands.mjs <chrome-binary> <core-url>
import { spawn } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [chrome, base] = process.argv.slice(2);
const port = 9700 + Math.floor(Math.random() * 200);
const browser = spawn(chrome, [
  `--remote-debugging-port=${port}`,
  `--user-data-dir=${mkdtempSync(join(tmpdir(), 'cdp-board-'))}`,
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
await send('Page.navigate', { url: `${base}/#view=wip` });
await sleep(7000);

const TITLE = 'PROBE card-hands check — safe to remove';
const EDITED = 'PROBE edited title — card-hands check';

// --- 1. CREATE from the page form ---
const created = await evaluate(`(async () => {
  const what = document.getElementById('board-note-what');
  const body = document.getElementById('board-note-body');
  const submit = document.getElementById('board-note-submit');
  if (!what || !submit) return { ok: false, reason: 'form missing' };
  what.value = ${JSON.stringify(TITLE)};
  body.value = 'Created by a headless browser driving the real page against the live core.';
  submit.click();
  return { ok: true, status: document.getElementById('board-note-status').textContent };
})()`);
await sleep(2500);
console.log('CREATE ' + JSON.stringify(created));
check(created.ok && posts.some((p) => p.includes('/api/board/cards') && p.includes('PROBE card-hands check')),
  `the page POSTed the new card: ${posts.filter((p) => p.includes('/api/board/cards')).length} board POST(s)`);

const found = await evaluate(`(() => {
  const cards = [...document.querySelectorAll('#board-cards .board-card')];
  const hit = cards.find((c) => c.textContent.includes('PROBE card-hands check'));
  if (!hit) return { found: false, cards: cards.length };
  const ordinalText = hit.querySelector('.work-identity')?.textContent || '';
  return { found: true, ordinalText, cards: cards.length,
           status: hit.querySelector('.status-badge')?.textContent,
           projectLine: hit.querySelector('.board-project')?.textContent,
           unfiled: !!hit.querySelector('.board-project-none'),
           hasFilePicker: !!hit.querySelector('.board-file-select') };
})()`);
console.log('FOUND ' + JSON.stringify(found));
check(found.found, `the new card appears on the page (${found.cards} cards rendered)`);
check(found.unfiled && found.hasFilePicker,
  `a card created without a project shows as Unfiled and offers the file picker (${found.projectLine})`);

// --- 2. EDIT the title from the page ---
const edited = await evaluate(`(async () => {
  const cards = [...document.querySelectorAll('#board-cards .board-card')];
  const card = cards.find((c) => c.textContent.includes('PROBE card-hands check'));
  if (!card) return { ok: false, reason: 'card gone' };
  const details = card.querySelector('details');
  if (details) details.open = true;
  const buttons = [...card.querySelectorAll('button')];
  const editButton = buttons.find((b) => b.textContent === 'Edit');
  if (!editButton) return { ok: false, reason: 'no Edit button', buttons: buttons.map((b) => b.textContent) };
  editButton.click();
  const field = card.querySelector('.board-edit-title');
  if (!field) return { ok: false, reason: 'no title field' };
  field.value = ${JSON.stringify(EDITED)};
  const save = [...card.querySelectorAll('button')].find((b) => b.textContent === 'Save title');
  if (!save) return { ok: false, reason: 'no Save title' };
  save.click();
  return { ok: true };
})()`);
await sleep(2500);
console.log('EDIT ' + JSON.stringify(edited));
check(edited.ok && posts.some((p) => p.includes('/note') && p.includes('PROBE edited title')),
  'the Edit button POSTed the corrected title through /note');

const editedNow = await evaluate(`(() => {
  const cards = [...document.querySelectorAll('#board-cards .board-card')];
  return { newTitleVisible: cards.some((c) => c.textContent.includes('PROBE edited title')),
           oldTitleGone: !cards.some((c) => c.textContent.includes('PROBE card-hands check — safe to remove')) };
})()`);
console.log('EDITED-STATE ' + JSON.stringify(editedNow));
check(editedNow.newTitleVisible, 'the corrected title is what the page shows');

// --- 3. FILE the card under a project from the page ---
const filed = await evaluate(`(async () => {
  const cards = [...document.querySelectorAll('#board-cards .board-card')];
  const card = cards.find((c) => c.textContent.includes('PROBE edited title'));
  if (!card) return { ok: false, reason: 'card gone' };
  const picker = card.querySelector('.board-file-select');
  if (!picker) return { ok: false, reason: 'no picker', text: card.textContent.slice(0, 200) };
  const options = [...picker.options].map((o) => ({ value: o.value, label: o.textContent }));
  const target = options.find((o) => o.value && /Autonomy/i.test(o.label)) || options.find((o) => o.value);
  if (!target) return { ok: false, reason: 'no projects offered', options };
  picker.value = target.value;
  picker.dispatchEvent(new Event('change', { bubbles: true }));
  return { ok: true, chose: target.label, optionCount: options.length };
})()`);
await sleep(2500);
console.log('FILE ' + JSON.stringify(filed));
check(filed.ok && posts.some((p) => p.includes('/file')), `the picker POSTed a filing (${posts.filter((p) => p.includes('/file')).length} file call(s))`);

const filedNow = await evaluate(`(() => {
  const cards = [...document.querySelectorAll('#board-cards .board-card')];
  const card = cards.find((c) => c.textContent.includes('PROBE edited title'));
  if (!card) return { found: false };
  return { found: true, projectLine: card.querySelector('.board-project')?.textContent || '',
           stillUnfiled: !!card.querySelector('.board-project-none'),
           pickerGone: !card.querySelector('.board-file-select') };
})()`);
console.log('FILED-STATE ' + JSON.stringify(filedNow));
check(filedNow.found && !filedNow.stillUnfiled && filedNow.pickerGone,
  `after filing, the card names its project and the picker is gone (${filedNow.projectLine})`);

// --- 4. REMOVE the card from the page (through the page's own confirm dialog) ---
const removed = await evaluate(`(async () => {
  const cards = [...document.querySelectorAll('#board-cards .board-card')];
  const card = cards.find((c) => c.textContent.includes('PROBE edited title'));
  if (!card) return { ok: false, reason: 'card gone' };
  const details = card.querySelector('details');
  if (details) details.open = true;
  const removeButton = [...card.querySelectorAll('button')].find((b) => b.textContent === 'Remove');
  if (!removeButton) return { ok: false, reason: 'no Remove button', buttons: [...card.querySelectorAll('button')].map((b) => b.textContent) };
  removeButton.click();
  await new Promise((r) => setTimeout(r, 600));
  const dialog = document.getElementById('confirm-dialog');
  const open = dialog && dialog.open;
  const text = document.getElementById('confirm-text')?.textContent;
  const ok = document.getElementById('confirm-ok');
  if (open && ok) ok.click();
  return { ok: true, dialogOpened: Boolean(open), dialogText: text, action: ok?.textContent };
})()`);
await sleep(2500);
console.log('REMOVE ' + JSON.stringify(removed));
check(removed.ok && removed.dialogOpened, `Remove asks first in the page's own dialog: "${removed.dialogText}"`);
check(posts.some((p) => p.includes('/remove')), 'confirming POSTed the removal through /remove');

const gone = await evaluate(`(() => {
  const cards = [...document.querySelectorAll('#board-cards .board-card')];
  return { stillVisible: cards.some((c) => c.textContent.includes('PROBE edited title')), cards: cards.length };
})()`);
console.log('REMOVED-STATE ' + JSON.stringify(gone));
check(!gone.stillVisible, `the card is off the open board (${gone.cards} cards remain)`);

// Removal must take the card out of every reader, not just the open lane.
// Retention lives in the append-only journal (live-swarm/board.jsonl, removed:true),
// which is evidence rather than work — check that separately on the host.
const readers = await evaluate(`Promise.all([
  fetch('/api/board/view').then(r => r.json()),
  fetch('/api/board').then(r => r.json()),
]).then(([view, board]) => {
  const inView = (view.cards || []).some(x => (x.title || '').includes('PROBE edited title'));
  const inBoard = (board.cards || []).some(x => (x.title || '').includes('PROBE edited title'));
  return { inView, inBoard };
})`);
console.log('READERS ' + JSON.stringify(readers));
check(!readers.inView && !readers.inBoard, 'the removed card is gone from both reader routes, not just the open lane');

console.log('POSTS ' + JSON.stringify(posts, null, 1));
browser.kill();
process.exit();
