// Phone-shell proof against the live core over CDP, real iPhone emulation.
// Measures #conversation-scroll (the actual scroller), not #messages.
// Usage: node scripts/check_phone_shell.mjs <chrome-binary> <core-url> <session-slug>
import { spawn } from 'node:child_process';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

const [chrome, base, slug] = process.argv.slice(2);
const port = 9600 + Math.floor(Math.random() * 300);
const browser = spawn(chrome, [
  `--remote-debugging-port=${port}`,
  `--user-data-dir=${mkdtempSync(join(tmpdir(), 'cdp-phone2-'))}`,
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
socket.addEventListener('message', (event) => {
  const m = JSON.parse(event.data);
  if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
});
const send = (method, params = {}) => new Promise((r) => {
  const n = ++id; pending.set(n, r); socket.send(JSON.stringify({ id: n, method, params }));
});
const evaluate = async (expression) => {
  const reply = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
  if (reply.result?.exceptionDetails) throw new Error(JSON.stringify(reply.result.exceptionDetails).slice(0, 300));
  return reply.result.result.value;
};
const check = (ok, label) => { console.log(`${ok ? 'PASS' : 'FAIL'} ${label}`); if (!ok) process.exitCode = 1; };
const frames = () => evaluate('new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))');

await send('Page.enable');
await send('Runtime.enable');
await send('Emulation.setDeviceMetricsOverride', {
  width: 390, height: 844, deviceScaleFactor: 3, mobile: true, screenWidth: 390, screenHeight: 844,
});
await send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
await send('Emulation.setUserAgentOverride', {
  userAgent: 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1',
});
console.log('viewport: iPhone 390x844, mobile=true, touch enabled');

const measure = `(() => {
  const s = document.getElementById('conversation-scroll');
  if (!s) return { found: false };
  const arts = [...document.querySelectorAll('#messages article')];
  const last = arts[arts.length - 1] || null;
  const lr = last ? last.getBoundingClientRect() : null;
  const sr = s.getBoundingClientRect();
  return {
    found: true,
    scrollTop: Math.round(s.scrollTop),
    scrollHeight: s.scrollHeight,
    clientHeight: s.clientHeight,
    fromBottom: Math.round(s.scrollHeight - s.scrollTop - s.clientHeight),
    overflow: s.scrollHeight > s.clientHeight + 8,
    articles: arts.length,
    lastTop: lr ? Math.round(lr.top) : null,
    lastBottom: lr ? Math.round(lr.bottom) : null,
    scrollerTop: Math.round(sr.top),
    scrollerBottom: Math.round(sr.bottom),
    viewportHeight: window.innerHeight,
    jumpLatestHidden: document.getElementById('jump-latest')?.hidden,
  };
})()`;

await send('Page.navigate', { url: `${base}/#view=chat&session=${encodeURIComponent(slug)}` });
await sleep(8000);
await frames();
await sleep(1500);

const pin = await evaluate(measure);
console.log('PIN ' + JSON.stringify(pin));
check(pin.found && pin.overflow, `the transcript really overflows at phone width (${pin.scrollHeight}px content in ${pin.clientHeight}px)`);
check(pin.found && pin.fromBottom < 48, `session opens at the newest line: ${pin.fromBottom}px from the bottom`);
check(pin.found && pin.lastBottom !== null && pin.lastBottom <= pin.scrollerBottom + 2 && pin.lastBottom > pin.scrollerTop,
  `the newest line is inside the visible transcript box (last bottom ${pin.lastBottom}, box ${pin.scrollerTop}..${pin.scrollerBottom})`);

// A reader who scrolls up is not yanked back.
await evaluate(`(() => { const s = document.getElementById('conversation-scroll'); s.scrollTop = 0; return true; })()`);
await sleep(1500);
await frames();
const away = await evaluate(measure);
console.log('AFTER-SCROLL-UP ' + JSON.stringify(away));
check(away.scrollTop === 0 && away.fromBottom > 500, `scrolling up sticks: scrollTop ${away.scrollTop}, ${away.fromBottom}px from the bottom`);
check(away.jumpLatestHidden === false, 'the "Latest" button appears when the reader is away from the newest line');

// And the way back works.
await evaluate(`(() => { document.getElementById('jump-latest').click(); return true; })()`);
await sleep(1200);
await frames();
const back = await evaluate(measure);
console.log('AFTER-LATEST ' + JSON.stringify(back));
check(back.fromBottom < 48, `the "Latest" control returns to the newest line: ${back.fromBottom}px from the bottom`);

// Settings page: menu reachable without a scroll.
await send('Page.navigate', { url: `${base}/#view=settings` });
await sleep(5000);
await frames();
await sleep(800);
const menu = await evaluate(`(() => {
  const t = document.getElementById('navigation-toggle');
  const tb = document.querySelector('.topbar');
  const r = t ? t.getBoundingClientRect() : null;
  return { exists: !!t, top: r ? Math.round(r.top) : null, bottom: r ? Math.round(r.bottom) : null,
           visible: t ? getComputedStyle(t).visibility !== 'hidden' && r.width > 0 : false,
           topbarPosition: tb ? getComputedStyle(tb).position : null,
           pageScrollHeight: document.documentElement.scrollHeight, viewportHeight: window.innerHeight };
})()`);
console.log('SETTINGS-MENU ' + JSON.stringify(menu));
check(menu.exists && menu.visible && menu.top >= 0 && menu.bottom <= menu.viewportHeight,
  `on Settings at phone width the Menu control is on the first screen (top ${menu.top}, bottom ${menu.bottom})`);
check(menu.topbarPosition === 'sticky', `topbar is sticky (${menu.topbarPosition})`);

await evaluate('window.scrollTo(0, document.documentElement.scrollHeight); true');
await sleep(800);
await frames();
const scrolled = await evaluate(`(() => {
  const t = document.getElementById('navigation-toggle');
  const r = t.getBoundingClientRect();
  return { top: Math.round(r.top), bottom: Math.round(r.bottom), scrollY: Math.round(window.scrollY), vh: window.innerHeight };
})()`);
console.log('SETTINGS-AFTER-SCROLL ' + JSON.stringify(scrolled));
check(scrolled.top >= 0 && scrolled.bottom <= scrolled.vh,
  `after scrolling ${scrolled.scrollY}px the Menu control is still on screen (top ${scrolled.top})`);

const opened = await evaluate(`(() => {
  const t = document.getElementById('navigation-toggle');
  t.click();
  const nav = document.getElementById('app-navigation');
  const chat = document.getElementById('nav-chat');
  const r = chat.getBoundingClientRect();
  return { expanded: t.getAttribute('aria-expanded'), navDisplay: getComputedStyle(nav).display,
           href: chat.getAttribute('href'), top: Math.round(r.top), bottom: Math.round(r.bottom), vh: window.innerHeight };
})()`);
console.log('SETTINGS-MENU-OPEN ' + JSON.stringify(opened));
check(opened.expanded === 'true' && opened.href === '#view=chat' && opened.top >= 0 && opened.bottom <= opened.vh,
  `menu opens and the chat link is on screen (${opened.top}..${opened.bottom} of ${opened.vh})`);

browser.kill();
process.exit();
