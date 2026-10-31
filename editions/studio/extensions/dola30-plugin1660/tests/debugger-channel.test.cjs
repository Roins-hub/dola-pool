const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const panel = fs.readFileSync(path.join(__dirname, '..', 'content-panel.js'), 'utf8');
const source = panel.slice(panel.indexOf('  function requestDebugger('), panel.indexOf('  function renderDebuggerError('));
function setup(sendMessage) {
  let timeout;
  let cleared = 0;
  const context = { chrome: { runtime: { sendMessage } },
    setTimeout(fn, ms) { assert.equal(ms, 10000); timeout = fn; return 1; },
    clearTimeout() { cleared++; } };
  vm.createContext(context); vm.runInContext(source, context);
  return { context, request: context.requestDebugger, expire: () => timeout(), cleared: () => cleared };
}
test('a missing callback times out and a late response cannot change the outcome', async () => {
  let callback;
  const env = setup((message, cb) => { callback = cb; });
  const result = env.request('DEBUGGER_STATUS'); env.expire();
  await assert.rejects(result, /10 秒未响应/);
  callback({ ok: true }); assert.equal(env.cleared(), 1);
});
test('closed message ports and invalidated contexts reject with the real reason', async () => {
  const env = setup((message, cb) => {
    env.context.chrome.runtime.lastError = { message: 'The message port closed before a response was received.' };
    cb(); delete env.context.chrome.runtime.lastError;
  });
  await assert.rejects(env.request('DEBUGGER_STATUS'), /message port closed/);
  assert.equal(env.cleared(), 1);
  const invalid = setup(() => { throw Error('Extension context invalidated.'); });
  await assert.rejects(invalid.request('DEBUGGER_STATUS'), /context invalidated/);
  assert.equal(invalid.cleared(), 1);
});
test('an old background without a handler is not treated as a successful status', async () => {
  const env = setup((message, cb) => cb());
  await assert.rejects(env.request('DEBUGGER_STATUS'), /未取得真实调试状态/);
});
