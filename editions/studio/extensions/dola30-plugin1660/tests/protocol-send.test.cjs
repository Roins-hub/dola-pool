const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'protocol-send-main.js'), 'utf8');

function node(text, tagName = 'BUTTON') {
  return {
    innerText: text, textContent: text, tagName, value: text, isConnected: true, disabled: false,
    parentElement: null, getAttribute: () => null,
    getBoundingClientRect: () => ({ width: 100, height: 30 }), querySelectorAll: () => []
  };
}

function environment({ prompt = '测试提示词', media, expertMode = false, preparedCounts = null, ensureResult = null,
  legacyMediaInject = true, expertMediaInjection = null,
  createConversation = true,
  batchCount = 1,
  protocolMode = 'expert',
  selection = { model: 'seedance_v2.0', duration: 30, ratio: '21:9', resolution: '720p' } } = {}) {
  const composer = node(prompt, 'DIV');
  const model = node('Dreamina Seedance 2.0 Fast');
  const params = node('21:9 · 30s');
  const send = node('发送');
  const attributes = new Map([
    ['data-watermark-free-resolution-bridge', JSON.stringify({ inputResolution: '720p' })],
    ['data-dola-protocol-prepared-context', JSON.stringify({ taskId: 'task-1', referenceCounts: preparedCounts })],
    ['data-dola-protocol-parameter-selection', JSON.stringify(selection)],
    ['data-dola-protocol-send-request', 'request-1'],
    ['data-dola-protocol-batch-count', String(batchCount)],
    ['data-dola-protocol-mode', protocolMode]
  ]);
  const document = new EventTarget();
  document.documentElement = {
    setAttribute(name, value) { attributes.set(name, value); },
    getAttribute(name) { return attributes.get(name) || null; }
  };
  const conversationIds = [];
  const newChat = node('新对话');
  newChat.click = () => {
    location.pathname = '/chat';
    location.href = 'https://www.dola.com/chat';
  };
  document.querySelectorAll = (selector) => {
    if (selector.includes('a[href*="/chat/"]')) return conversationIds.map(id => ({
      href: `https://www.dola.com/chat/${id}`, getAttribute(name) { return name === 'href' ? `/chat/${id}` : null; }
    }));
    if (selector.includes('contenteditable') || selector.includes('textarea')) return [composer];
    if (selector.includes('video-model')) return expertMode ? [] : [model];
    if (selector.includes('creation-video-generation-params-panel')) return expertMode ? [] : [params];
    if (selector.includes('flow-end-msg-send')) return [send];
    if (selector.includes('a,button')) return [newChat];
    return [];
  };
  let calls = 0; let quickCalls = 0; let armed = null; let disarmed = 0; let ensureCalls = 0; let begun = 0; let completed = 0;
  const location = { hostname: 'www.dola.com', pathname: '/chat/create-image', href: 'https://www.dola.com/chat/create-image' };
  const expert = {
    arm(value) { armed = value; },
    disarm() { armed = null; disarmed += 1; },
    async selectBeforeSend(target, options) {
      calls += 1; assert.equal(target, send); assert.equal(options.allowHidden, false);
      assert.equal(options.context.duration, selection.duration);
      if (createConversation) {
        const id = `3841800000000000${calls}`;
        conversationIds.push(id);
        location.pathname = `/chat/${id}`;
        location.href = `https://www.dola.com/chat/${id}`;
      }
      attributes.set('data-watermark-free-generation-evidence', JSON.stringify({
        sequence: calls, phase: 'request_observed', httpStatus: null
      }));
      return true;
    },
    async sendNativeHttp(target, options) {
      return this.selectBeforeSend(target, options);
    },
    async sendQuickNativeHttp(target, options) {
      quickCalls += 1; assert.equal(target, send); assert.equal(options.context.protocolMode, 'quick');
      if (createConversation) {
        const id = `3841900000000000${quickCalls}`;
        conversationIds.push(id);
        location.pathname = `/chat/${id}`;
        location.href = `https://www.dola.com/chat/${id}`;
      }
      attributes.set('data-watermark-free-generation-evidence', JSON.stringify({
        sequence: quickCalls, phase: 'request_observed', httpStatus: null
      }));
      return true;
    },
    prepare(value) { return value; },
    status: { reason: '', lastMediaInjection: expertMediaInjection }
  };
  const sessionValues = new Map();
  const sessionStorage = { getItem: key => sessionValues.get(key) || null,
    setItem: (key, value) => sessionValues.set(key, String(value)), removeItem: key => sessionValues.delete(key) };
  const context = { document, Event, setTimeout, clearTimeout, URL, location, sessionStorage,
    getComputedStyle: () => ({ display: 'block', visibility: 'visible' }), globalThis: null,
    __DOLA_SILENT_EXPERT__: expert,
    __DOLA_TOOLKIT_LAST_MEDIA_INJECT__: legacyMediaInject
      ? { reason: 'injected', source: 'session', messageKey: 'content_block' } : null,
    __DOLA_TOOLKIT_MEDIA_SNAPSHOT__: () => media || ({ ready: true, pending: 0, counts: { image: 2, video: 0, audio: 0 }, attachmentCount: 2 }),
    async __DOLA_TOOLKIT_MEDIA_ENSURE__(expected) {
      ensureCalls += 1;
      return ensureResult || { ok: true, ready: true, pending: 0, counts: { ...expected },
        attachmentCount: Object.values(expected).reduce((sum, value) => sum + Number(value || 0), 0), submitPatched: true, source: 'native_store' };
    },
    __DOLA_TOOLKIT_MEDIA_MODE_SWITCH__: {
      beginRound() { begun += 1; return { valid: true }; },
      completeRound() { completed += 1; return true; }
    }
  };
  context.globalThis = context;
  vm.createContext(context); vm.runInContext(source, context);
  return { context, document, attributes, conversationIds,
    get calls() { return calls; }, get quickCalls() { return quickCalls; }, get armed() { return armed; },
    get disarmed() { return disarmed; }, get ensureCalls() { return ensureCalls; },
    get begun() { return begun; }, get completed() { return completed; } };
}

test('protocol send reads current draft and native parameters then dispatches expert send once', async () => {
  const env = environment();
  env.document.dispatchEvent(new Event('dola-protocol-send-request'));
  await new Promise(resolve => setImmediate(resolve));
  const status = JSON.parse(env.attributes.get('data-dola-protocol-send-status'));
  assert.equal(env.calls, 1);
  assert.equal(status.phase, 'conversation_confirmed');
  assert.equal(status.snapshot.conversation.createdId, '38418000000000001');
  assert.deepEqual(JSON.parse(JSON.stringify(status.snapshot.media.counts)), { image: 2, video: 0, audio: 0 });
  assert.equal(status.snapshot.duration, 30);
  assert.equal(status.snapshot.ratio, '21:9');
  assert.equal(status.snapshot.resolution, '720p');
  assert.equal(status.snapshot.promptLength, 5);
  assert.equal(env.armed.duration, 30);
  assert.equal(env.armed.model, 'seedance_v2.0');
  assert.equal(env.armed.ratio, '21:9');
  assert.equal(env.armed.resolution, '720p');
});

test('expert mode uses panel parameters when Dola hides every native video parameter control', async () => {
  const env = environment({ expertMode: true,
    selection: { model: 'seedance_v2.5', duration: 10, ratio: '9:16', resolution: '1080p' } });
  env.document.dispatchEvent(new Event('dola-protocol-send-request'));
  await new Promise(resolve => setImmediate(resolve));
  const status = JSON.parse(env.attributes.get('data-dola-protocol-send-status'));
  assert.equal(status.phase, 'conversation_confirmed');
  assert.equal(status.snapshot.parameterSource, 'expert_panel');
  assert.equal(status.snapshot.duration, 10);
  assert.equal(status.snapshot.ratio, '9:16');
  assert.equal(status.snapshot.resolution, '1080p');
  assert.equal(env.calls, 1);
  assert.equal(env.armed.model, 'seedance_v2.5');
});

test('quick protocol preserves the quick route and never arms expert conversion', async () => {
  const env = environment({ protocolMode: 'quick', media: {
    ready: true, pending: 0, counts: { image: 0, video: 0, audio: 0 }, attachmentCount: 0
  }, selection: { model: 'seedance_v2.0', duration: 15, ratio: '16:9', resolution: '720p' } });
  env.document.dispatchEvent(new Event('dola-protocol-send-request'));
  await new Promise(resolve => setImmediate(resolve));
  const status = JSON.parse(env.attributes.get('data-dola-protocol-send-status'));
  assert.equal(status.phase, 'conversation_confirmed');
  assert.equal(status.protocolMode, 'quick');
  assert.equal(status.snapshot.protocolMode, 'quick');
  assert.equal(status.snapshot.duration, 15);
  assert.equal(env.calls, 0);
  assert.equal(env.quickCalls, 1);
  assert.equal(env.disarmed, 1);
  assert.equal(env.armed, null);
  assert.equal(status.snapshot.conversation.createdId, '38419000000000001');
});

test('protocol send blocks an empty prompt before touching expert mode', async () => {
  const env = environment({ prompt: '' });
  env.document.dispatchEvent(new Event('dola-protocol-send-request'));
  await new Promise(resolve => setImmediate(resolve));
  const status = JSON.parse(env.attributes.get('data-dola-protocol-send-status'));
  assert.equal(env.calls, 0);
  assert.equal(status.phase, 'blocked');
  assert.match(status.reason, /提示词为空/);
});

test('first protocol click waits for the native attachment block before dispatching', async () => {
  const env = environment({ preparedCounts: { image: 1, video: 0, audio: 0 },
    media: { ready: false, pending: 1, counts: { image: 0, video: 0, audio: 0 }, attachmentCount: 0 } });
  env.document.dispatchEvent(new Event('dola-protocol-send-request'));
  await new Promise(resolve => setImmediate(resolve));
  const status = JSON.parse(env.attributes.get('data-dola-protocol-send-status'));
  assert.equal(env.ensureCalls, 1);
  assert.equal(env.calls, 1);
  assert.equal(status.phase, 'conversation_confirmed');
  assert.equal(status.snapshot.media.source, 'native_store');
  assert.equal(status.snapshot.media.attachmentCount, 1);
  assert.equal(env.armed.expectedAttachmentCount, 1);
});

test('expert pre-sign media evidence confirms the real HTTP submission without the legacy marker', async () => {
  const env = environment({ preparedCounts: { image: 1, video: 0, audio: 0 }, legacyMediaInject: false,
    expertMediaInjection: { applied: true, source: 'session_native_block',
      counts: { image: 1, video: 0, audio: 0 }, final: true } });
  env.document.dispatchEvent(new Event('dola-protocol-send-request'));
  await new Promise(resolve => setImmediate(resolve));
  const status = JSON.parse(env.attributes.get('data-dola-protocol-send-status'));
  assert.equal(env.calls, 1);
  assert.equal(status.phase, 'conversation_confirmed');
  assert.deepEqual(status.snapshot.httpEvidence.mediaInjection, {
    reason: 'injected', source: 'session_native_block', messageKey: 'content_block'
  });
});

test('two sequential protocol clicks create two conversations and reset both media rounds', async () => {
  const env = environment({ preparedCounts: { image: 1, video: 0, audio: 0 } });
  env.document.dispatchEvent(new Event('dola-protocol-send-request'));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(JSON.parse(env.attributes.get('data-dola-protocol-send-status')).phase, 'conversation_confirmed');
  env.attributes.set('data-dola-protocol-send-request', 'request-2');
  env.document.dispatchEvent(new Event('dola-protocol-send-request'));
  await new Promise(resolve => setImmediate(resolve));
  const status = JSON.parse(env.attributes.get('data-dola-protocol-send-status'));
  assert.equal(status.phase, 'conversation_confirmed');
  assert.equal(status.snapshot.conversation.createdId, '38418000000000002');
  assert.equal(env.calls, 2);
  assert.equal(env.ensureCalls, 2);
  assert.equal(env.begun, 2);
  assert.equal(env.completed, 2);
  assert.deepEqual(env.conversationIds, ['38418000000000001', '38418000000000002']);
});

test('one batch click creates the requested number of distinct left-sidebar conversations', async () => {
  const env = environment({ batchCount: 3, preparedCounts: { image: 1, video: 0, audio: 0 } });
  env.document.dispatchEvent(new Event('dola-protocol-send-request'));
  await new Promise(resolve => setTimeout(resolve, 2600));
  const status = JSON.parse(env.attributes.get('data-dola-protocol-send-status'));
  assert.equal(status.phase, 'conversation_confirmed');
  assert.equal(status.batch.total, 3);
  assert.equal(status.batch.completed, 3);
  assert.equal(status.batch.createdIds.length, 3);
  assert.equal(env.calls, 3);
  assert.equal(env.completed, 3);
  assert.deepEqual(env.conversationIds, ['38418000000000001', '38418000000000002', '38418000000000003']);
});

test('protocol send intercepts the first click when text and media are not in one native message', async () => {
  const env = environment({ preparedCounts: { image: 1, video: 0, audio: 0 },
    media: { ready: false, pending: 1, counts: { image: 0, video: 0, audio: 0 }, attachmentCount: 0 },
    ensureResult: { ok: false, ready: false, pending: 1, counts: { image: 0, video: 0, audio: 0 }, attachmentCount: 0 } });
  env.document.dispatchEvent(new Event('dola-protocol-send-request'));
  await new Promise(resolve => setImmediate(resolve));
  const status = JSON.parse(env.attributes.get('data-dola-protocol-send-status'));
  assert.equal(env.ensureCalls, 1);
  assert.equal(env.calls, 0);
  assert.equal(status.phase, 'blocked');
  assert.match(status.reason, /原生附件请求/);
});
