// Offline scroll-follow regression. Run: node test_d18.cjs. No dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(__dirname + '/ui.html', 'utf8');
const demo = fs.readFileSync(__dirname + '/../docs/index.html', 'utf8');
const block = source => source.match(/\/\/ ---- scroll follow[^]*?(?=\/\/ ---- helpers)/)[0];
assert.equal(block(html), block(demo), 'live page and demo must share the fix');
for (const source of [html, demo]) {
  for (const s of source.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(s[1]);
  assert(/scroll-behavior:\s*smooth/.test(source), 'keep smooth scrolling');
}
let now = 0;
const listeners = {}, keys = {};
const main = {scrollHeight: 2000, scrollTop: 200, clientHeight: 600, clientWidth: 390,
  addEventListener(name, fn) { listeners[name] = fn; },
  scrollTo(options) { this.scrollTop = options.top; }};
const button = {style: {}};
const context = vm.createContext({main, performance: {now: () => now}, document: {
  getElementById() { return button; }, addEventListener(name, fn) { keys[name] = fn; },
}});
vm.runInContext(block(html), context);
const pinned = () => vm.runInContext('pinned', context);
const reset = () => { now += 2000; vm.runInContext('pinned = true', context); main.scrollTop = 200; };
listeners.scroll();
assert.equal(pinned(), true, 'animation/trimming scroll must not unpin');
for (const input of ['wheel', 'touchmove']) {
  reset(); listeners[input](); listeners.scroll();
  assert.equal(pinned(), false, input + ' away from bottom must unpin');
  vm.runInContext('scrollFollow()', context);
  assert.equal(main.scrollTop, 200, 'new captions must not yank a reader');
  main.scrollTop = 1400; listeners.scroll();
  assert.equal(pinned(), true, 'returning to bottom restores follow');
  reset(); listeners[input](); now += 1001; listeners.scroll();
  assert.equal(pinned(), true, 'stale input must not unpin later programmatic scroll');
}
for (const offsetX of [389, 390]) {
  reset(); listeners.mousedown({offsetX}); listeners.scroll();
  assert.equal(pinned(), offsetX < 390, 'only the scrollbar records reader input');
}
for (const key of ['PageUp', 'PageDown', 'ArrowUp', 'ArrowDown', 'Home', 'End', ' ']) {
  for (const editing of [true, false]) {
    reset(); keys.keydown({key, target: {closest: () => editing}}); listeners.scroll();
    assert.equal(pinned(), editing, 'navigation keys in editors must not unpin');
  }
}
reset(); keys.keydown({key: 'a', target: {closest: () => false}}); listeners.scroll();
assert.equal(pinned(), true, 'ordinary typing is not scroll input');
vm.runInContext('pinned = false', context);
listeners.wheel(); // The reader may click the button immediately after scrolling.
let pinnedAtScroll;
let scrollTop = 200;
Object.defineProperty(main, 'scrollTop', {get() { return scrollTop; }, set(value) {
  pinnedAtScroll = pinned();
  listeners.scroll(); // Intermediate smooth-scroll event, still away from bottom.
  scrollTop = value;
}});
button.onclick();
assert.equal(pinnedAtScroll, true, 'follow button must re-arm before scrolling');
assert.equal(pinned(), true, 'follow button must supersede recent reader input');
console.log('D18: programmatic follow, reader input, freshness, editors, scrollbar, bottom recovery and demo sync pass');
