// Offline presentation/fullscreen checks. Run: node test_d20.cjs. No dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const live = fs.readFileSync(__dirname + '/ui.html', 'utf8');
const demo = fs.readFileSync(__dirname + '/../docs/index.html', 'utf8');
const prefs = html => html.match(/\/\/ ---- prefs: view, font size, theme ----[^]*?(?=\/\/ ---- sentence rendering)/)[0];
assert.equal(prefs(live), prefs(demo), 'live/demo preferences must stay identical');
for (const html of [live, demo]) {
  for (const script of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(script[1]);
}
function setup(saved = {}, enabled = true) {
  const nodes = new Map(), listeners = {}, classes = new Set(), storage = new Map();
  storage.set('babel_prefs', JSON.stringify(saved));
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {value: id === 'fs-sel' ? 'm' : 'both', hidden: true,
      textContent: '', closest: () => null});
    return nodes.get(id);
  };
  const doc = {fullscreenEnabled: enabled, fullscreenElement: null,
    getElementById: node, body: {classList: {
      [Symbol.iterator]: () => classes.values(), add: c => classes.add(c), remove: c => classes.delete(c),
      contains: c => classes.has(c), toggle(c, on) { on ? classes.add(c) : classes.delete(c); },
    }}, addEventListener(type, fn) { (listeners[type] ||= []).push(fn); }};
  const state = {requests: 0, exits: 0, denied: false};
  const changed = () => { for (const fn of listeners.fullscreenchange || []) fn(); };
  doc.documentElement = {async requestFullscreen() {
    state.requests++;
    if (state.denied) throw new Error('denied');
    doc.fullscreenElement = this; changed();
  }};
  doc.exitFullscreen = async () => { state.exits++; doc.fullscreenElement = null; changed(); };
  const ctx = vm.createContext({document: doc,
    localStorage: {getItem: k => storage.get(k), setItem: (k, v) => storage.set(k, v)},
    matchMedia: () => ({matches: false}), renderAll() {},
  });
  vm.runInContext(prefs(live), ctx);
  return {node, doc, state, ctx, storage, changed,
    click(target = node('main')) { for (const fn of listeners.click || []) fn({target}); },
    keydown(key, defaultPrevented = false) {
      for (const fn of listeners.keydown || []) fn({key, defaultPrevented});
    }};
}
const settle = async () => { for (let i = 0; i < 4; i++) await Promise.resolve(); };
(async () => {
  let s;
  for (const original of ['s', 'm', 'l']) {
    s = setup({fs: original});
    s.node('fs-sel').value = 'xl'; s.node('fs-sel').onchange(); await settle();
    assert.equal(s.state.requests, 1, 'selecting XL must request root fullscreen');
    assert.equal(s.doc.fullscreenElement, s.doc.documentElement);
    assert.equal(s.node('pres-hint').hidden, true);
    s.doc.fullscreenElement = null; s.changed();
    assert.equal(s.node('fs-sel').value, original, 'Esc must restore the pre-presentation size');
    assert.equal(s.node('pres-hint').hidden, true, 'Esc must not offer fullscreen re-entry');
    s.click(); await settle();
    assert.equal(s.state.requests, 1, 'ordinary clicks after Esc must not re-enter fullscreen');
    s.node('fs-sel').value = 'xl'; s.node('fs-sel').onchange(); await settle();
    s.node('pres-exit').onclick(); await settle();
    assert.equal(s.node('fs-sel').value, original, 'the exit button must restore the same size as Esc');
    assert.equal(s.state.exits, 1);
    assert.equal(JSON.parse(s.storage.get('babel_prefs')).fs, original);
  }
  s.node('fs-sel').value = 'xl'; s.node('fs-sel').onchange(); await settle();
  s.node('fs-sel').value = 'l'; s.node('fs-sel').onchange(); await settle();
  assert.equal(s.state.exits, 2, 'changing to another size must leave root fullscreen');
  const other = {}; s.doc.fullscreenElement = other;
  s.node('fs-sel').onchange(); await settle();
  assert.equal(s.doc.fullscreenElement, other, 'font changes must not exit another fullscreen owner');

  s = setup({fs: 'l'});
  let finishRequest;
  s.doc.documentElement.requestFullscreen = () => new Promise(resolve => {
    finishRequest = () => { s.doc.fullscreenElement = s.doc.documentElement; s.changed(); resolve(); };
  });
  s.node('fs-sel').value = 'xl'; s.node('fs-sel').onchange();
  s.node('pres-exit').onclick(); finishRequest(); await settle();
  assert.equal(s.node('fs-sel').value, 'l');
  assert.equal(s.doc.fullscreenElement, null, 'a late fullscreen request must not undo an earlier exit');

  s = setup({fs: 'xl', view: 'zh', previousFs: 'l'});
  assert.equal(s.state.requests, 0, 'load must not request fullscreen without a gesture');
  assert.equal(s.doc.body.classList.contains('fs-xl'), true);
  assert.equal(s.doc.body.classList.contains('view-zh'), true);
  assert.equal(s.node('pres-hint').hidden, false);
  s.click({closest: () => ({})}); await settle();
  assert.equal(s.state.requests, 0, 'controls must not re-enter fullscreen');
  s.state.denied = true; s.click(); await settle();
  assert.equal(s.node('fs-sel').value, 'xl'); assert.equal(s.node('pres-hint').hidden, false);
  s.state.denied = false; s.click(); await settle();
  assert.equal(s.node('pres-hint').hidden, true);
  s.doc.fullscreenElement = null; s.changed();
  assert.equal(s.node('fs-sel').value, 'l', 'reload must preserve the pre-presentation size');
  s = setup({fs: 'xl', previousFs: 'invalid'});
  s.node('pres-exit').onclick(); await settle();
  assert.equal(s.node('fs-sel').value, 'm', 'legacy or invalid saved return sizes must fall back to M');
  s = setup({fs: 'xl'}, false);
  s.click(); s.node('fs-sel').onchange(); await settle();
  assert.equal(s.state.requests, 0); assert.equal(s.node('pres-hint').hidden, true);
  assert.equal(s.doc.body.classList.contains('fs-xl'), true);
  s.keydown('Escape', true);
  assert.equal(s.node('fs-sel').value, 'xl', 'handled Escape must not close presentation');
  s.keydown('Escape');
  assert.equal(s.node('fs-sel').value, 'm', 'Escape must also leave windowed presentation');

  s = setup({fs: 'xl'});
  s.click(); await settle();
  s.ctx.qrProjection = s.node('qr-projection');
  s.ctx.qrProjection.hidden = false;
  s.ctx.projectedKey = 'public'; s.ctx.projectionTrigger = null;
  vm.runInContext(live.match(/function closeProjection\([^]*?\n}/)[0], s.ctx);
  vm.runInContext(live.match(/document.addEventListener\('fullscreenchange', \(\) => \{\n  if \(!qrProjection.hidden[^]*?\n\}\);/)[0], s.ctx);
  s.doc.fullscreenElement = s.ctx.qrProjection;
  vm.runInContext('closeProjection()', s.ctx); await settle();
  assert.equal(s.node('fs-sel').value, 'xl', 'closing QR before its queued fullscreenchange must preserve XL');
  s.ctx.qrProjection.hidden = false;
  s.doc.fullscreenElement = s.ctx.qrProjection; s.changed();
  assert.equal(s.node('pres-hint').hidden, true);
  vm.runInContext('closeProjection()', s.ctx); await settle();
  assert.equal(s.ctx.qrProjection.hidden, true);
  assert.equal(s.doc.fullscreenElement, s.doc.documentElement, 'closing QR must restore XL fullscreen');
  assert.equal(s.node('pres-hint').hidden, true);
  s.ctx.qrProjection.hidden = false;
  s.doc.fullscreenElement = s.ctx.qrProjection; s.changed();
  s.doc.fullscreenElement = null; s.changed(); await settle();
  assert.equal(s.node('fs-sel').value, 'xl', 'browser exit from QR must not leave presentation');
  assert.equal(s.doc.fullscreenElement, s.doc.documentElement);
  s.ctx.qrProjection.hidden = false;
  s.doc.fullscreenElement = null; s.state.denied = true; s.changed(); await settle();
  assert.equal(s.node('fs-sel').value, 'xl', 'a windowed QR overlay must not be mistaken for leaving presentation');
  assert.equal(s.node('pres-hint').hidden, false);
  s.doc.fullscreenElement = null; s.ctx.qrProjection.hidden = false;
  s.state.denied = true;
  vm.runInContext('closeProjection()', s.ctx); await settle();
  assert.equal(s.node('pres-hint').hidden, false, 'denied QR restoration must leave the retry hint');
  for (const html of [live, demo]) assert.match(html, /id="pres-hint"[^>]*role="status"[^>]*hidden/);
  console.log('D20: XL, Esc/button restore S/M/L, refresh/legacy preferences, controls, unsupported/denied, QR isolation, prefs parity and JS compile pass');
})().catch(error => { console.error(error); process.exitCode = 1; });
