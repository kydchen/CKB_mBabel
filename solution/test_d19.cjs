// Offline device-notice regression. Run: node test_d19.cjs. No dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(__dirname + '/ui.html', 'utf8');
for (const script of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(script[1]);
assert.match(html, /id="device-dismiss"[^>]*type="button"[^>]*aria-label="[^"]+"/);
assert.match(html, /#device-dismiss:focus-visible/);
const block = html.match(/\/\/ ---- audio devices \(host only\) ----[^]*?(?=deviceBtn.onclick)/)[0];
const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    const classes = new Set();
    elements.set(id, {style: {}, textContent: '', options: [],
      classList: {toggle(name, on) { on ? classes.add(name) : classes.delete(name); },
        contains(name) { return classes.has(name); }},
      appendChild(option) { this.options.push(option); }});
  }
  return elements.get(id);
}
const context = vm.createContext({controlToken: 'host', document: {
  getElementById: element, createElement: () => ({}),
}});
vm.runInContext(block, context);
function notice(warning) {
  context.ev = {warning, microphones: [], outputs: [], capture_system: false, first_run: false};
  vm.runInContext('onDevices(ev)', context);
}
function visible(expected) {
  assert.equal(element('device-warning').style.display, expected ? 'flex' : 'none');
  assert.equal(element('device-btn').classList.contains('warning'), expected);
}
notice('Microphone disappeared: Wireless Microphone'); visible(true);
element('device-dismiss').onclick(); visible(false);
notice('Microphone disappeared: Wireless Microphone'); visible(false);
notice('Microphone reconnected: Wireless Microphone'); visible(true);
assert.equal(element('device-warning-text').textContent, 'Microphone reconnected: Wireless Microphone');
notice(''); visible(false);
notice('Microphone disappeared: Wireless Microphone'); visible(true);
vm.runInContext('controlToken = null; renderDeviceWarning()', context); visible(false);
notice('Cannot reopen receiver'); visible(false);
vm.runInContext('controlToken = "host"; renderDeviceWarning()', context); visible(true);
element('device-dismiss').onclick();
vm.runInContext('controlToken = null; renderDeviceWarning(); controlToken = "new-host"; renderDeviceWarning()', context);
visible(false);
console.log('D19: dismiss/repeat/change/clear, button accent, host-only, reconnect state and JS compile pass');
