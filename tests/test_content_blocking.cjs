const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../src/zocdoc_ortho/chrome_extension/content.js'), 'utf8');
function capture({ body = '', provider = true, notices = [], frame = false }) {
  let listener;
  const messages = [];
  const node = (text, review = false, hidden = false) => ({
    textContent: text, innerText: text, hidden,
    closest: () => review ? {} : null,
    getBoundingClientRect: () => ({ width: 100, height: 100 }),
  });
  const document = {
    title: 'Test profile', body: { innerText: body },
    documentElement: { outerHTML: '<html>valid capture</html>' },
    querySelector: selector => selector === '[data-test="provider-name"]' && provider ? node('Valid Doctor') : null,
    querySelectorAll: selector => {
      if (selector.includes('iframe')) return frame ? [node('challenge')] : [];
      if (selector.startsWith('h1,h2,h3,')) return notices.map(n => node(n.text, n.review, n.hidden));
      return [];
    },
  };
  const context = {
    document, location: { pathname: '/doctor/test-123', href: 'https://www.zocdoc.com/doctor/test-123' },
    performance: { now: () => 100, getEntriesByType: () => [] },
    window: { getComputedStyle: () => ({ display: 'block', visibility: 'visible', opacity: '1' }) },
    console, setInterval: () => 1, clearInterval: () => {},
    chrome: { runtime: { onMessage: { addListener: fn => { listener = fn; } },
      sendMessage: (message, callback) => {
        if (message.type !== 'GET_CAPTURE_CONFIG') messages.push(message);
        callback({ ok: true });
      } } },
  };
  vm.runInNewContext(source, context);
  listener({ type: 'FORCE_CAPTURE' }, null, () => {});
  return messages[0].type;
}

const terms = ['unusual activity', 'captcha', 'verify you are human', 'are you a human',
  'access is temporarily restricted', 'automated (bot) activity'];
for (const term of terms) {
  assert.equal(capture({ body: `Patient review: ${term}`, notices: [{ text: term, review: true }] }), 'SAVE_HTML');
  assert.equal(capture({ body: term, provider: false }), 'PAGE_BLOCKED');
  assert.equal(capture({ body: term, notices: [{ text: term }] }), 'PAGE_BLOCKED');
  assert.equal(capture({ body: term, notices: [{ text: term, hidden: true }] }), 'SAVE_HTML');
}
assert.equal(capture({ body: 'Initially there was some unusual activity that caused the long delay.' }), 'SAVE_HTML');
assert.equal(capture({ provider: false, frame: true }), 'PAGE_BLOCKED');
assert.equal(capture({ provider: true, frame: true }), 'SAVE_HTML');
assert.equal(capture({}), 'SAVE_HTML');
for (const text of ['Please enable JS and disable any ad blocker', 'Please enable JavaScript and disable your adblocker']) {
  assert.equal(capture({ body: text, provider: false }), 'PAGE_BLOCKED');
  assert.equal(capture({ body: text, provider: true }), 'SAVE_HTML');
  assert.equal(capture({ body: text, notices: [{ text }] }), 'PAGE_BLOCKED');
}
console.log('34 content-script challenge/review regression scenarios passed.');
