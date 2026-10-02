// Offline QR/URL checks. Run: node test_d14.cjs. No dependencies or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(__dirname + '/ui.html', 'utf8');
for (const m of html.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(m[1]);
const library = html.match(/<script id="qr-library">([\s\S]*?)<\/script>/)[1];
assert(library.includes('Copyright (c) Project Nayuki. (MIT License)'));
assert(!/<script\b[^>]*\bsrc=/.test(html), 'QR must work without external scripts');
const fn = name => html.match(new RegExp('function ' + name + '\\([^]*?\\n}'))[0];
const sandbox = {
  URL, shareInfo: {}, controlToken: null, publicLinkSeen: false, tunnelNoticeSeen: false,
  renderShareLinks() {}, notices: 0, showTunnelNotice() { sandbox.notices++; },
  document: {createElement() {
    const fills = [];
    const ctx = {fillRect(...args) { fills.push([this.fillStyle, ...args]); }};
    return {fills, getContext() { return ctx; }, setAttribute() {}};
  }},
};
vm.createContext(sandbox);
vm.runInContext(library + '\n' + ['shareURL', 'qrCanvas', 'onShare'].map(fn).join('\n'), sandbox);
const oldURL = 'https://old-conference.trycloudflare.com/Qr-path_8';
const newURL = 'https://new-conference.trycloudflare.com/Qr-path_8';
for (const value of [undefined, null, {}, 'javascript:alert(1)', 'data:text/plain,hello']) {
  assert.equal(sandbox.shareURL(value), '');
}
assert.equal(sandbox.shareURL(oldURL), oldURL);
const a = sandbox.qrCanvas(oldURL), b = sandbox.qrCanvas(newURL);
assert.equal(a.width, a.height);
assert.equal(a.width % 16, 0);
assert.deepEqual(a.fills[0], ['#fff', 0, 0, a.width, a.height]);
for (const [color, x, y, w, h] of a.fills.slice(1)) {
  assert.equal(color, '#000'); assert.equal(w, 16); assert.equal(h, 16);
  assert(x >= 64 && y >= 64 && x + w <= a.width - 64 && y + h <= a.height - 64);
}
assert.notDeepEqual(a.fills, b.fills, 'replacement URL must change the QR');
sandbox.onShare({type: 'share', public: oldURL, token: 'NEVER-IN-QR'});
assert.deepEqual(Object.keys(sandbox.shareInfo).sort(), ['lan', 'public']);
assert.equal(sandbox.shareInfo.public, oldURL);
assert.equal(sandbox.notices, 0, 'viewer has no host notice');
sandbox.controlToken = 'offline-host-only';
sandbox.onShare({type: 'share', public: newURL});
assert.equal(sandbox.notices, 1);
sandbox.onShare({type: 'share', lan: 'http://192.0.2.1:8765/Qr-path_8'});
assert.equal(sandbox.shareInfo.public, '');
assert.equal(sandbox.notices, 2);
assert.throws(() => sandbox.qrCanvas('x'.repeat(10000)), /Data too long/);
console.log('D14: JS compile, vendored license, exact URLs, four-module quiet zone, replacement and host notice pass');
