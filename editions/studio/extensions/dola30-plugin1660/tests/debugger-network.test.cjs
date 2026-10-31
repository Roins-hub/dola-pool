const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '..');
const summarySource = fs.readFileSync(path.join(root, 'debugger-summary.js'), 'utf8');
const debuggerSource = fs.readFileSync(path.join(root, 'debugger-network.js'), 'utf8');
const clean = value => JSON.parse(JSON.stringify(value));
function event() {
  const listeners = [];
  return { addListener(fn) { listeners.push(fn); }, emit(...args) { for (const fn of listeners) fn(...args); }, listeners };
}
function environment({ tabs: initialTabs, attachError, enableError, commandHandler, attachHandler, storage = {}, owned = new Set(), unavailable = false, detachError = false, cryptoUnavailable = false } = {}) {
  const tabs = new Map((initialTabs || [{ id: 1, url: 'https://www.doubao.com/chat/' }, { id: 2, url: 'https://www.dola.com/chat/' }]).map(tab => [tab.id, tab]));
  const commands = []; const attachments = []; const detachments = []; const messages = []; const badges = []; const timers = []; const downloads = [];
  const networkEvents = event(); const detached = event(); const onMessage = event(); const onUpdated = event(); const onActivated = event(); const onRemoved = event();
  const bodies = new Map();
  const chrome = {
    runtime: { id: 'fixture-extension', onMessage },
    tabs: { onUpdated, onActivated, onRemoved, get: async id => { if (!tabs.has(id)) throw Error('closed'); return tabs.get(id); },
      query: async () => [...tabs.values()], sendMessage: async (id, message) => { messages.push({ id, message }); } },
    storage: { session: { get: async key => ({ [key]: storage[key] }), set: async value => { Object.assign(storage, clean(value)); } } },
    downloads: { download: async options => { downloads.push(options); return downloads.length; } },
    action: { setBadgeText: async value => { badges.push(value); } },
    debugger: unavailable ? undefined : {
      onEvent: networkEvents, onDetach: detached,
      attach: async (target, version) => { attachments.push({ target, version }); if (attachHandler) await attachHandler(target); if (attachError) throw Error(attachError); owned.add(target.tabId); },
      detach: async target => { detachments.push(target); if (detachError) throw Error('detach rejected'); owned.delete(target.tabId); detached.emit(target, 'canceled_by_user'); },
      sendCommand: async (target, method, params = {}) => {
        commands.push({ target, method, params }); if (!owned.has(target.tabId)) throw Error('Debugger is not attached');
        if (method === 'Network.enable' && enableError) throw Error(enableError);
        if (commandHandler) { const result = await commandHandler(target, method, params); if (result !== undefined) return result; }
        if (method === 'Network.getResponseBody') { const body = bodies.get(params.requestId); if (body instanceof Error) throw body; return typeof body === 'object' ? body : { body: body || '{"code":0}', base64Encoded: false }; }
        if (method === 'Network.getRequestPostData') return { postData: JSON.stringify(videoRequest()) };
        return {};
      }
    }
  };
  const context = { chrome, URL, TextEncoder, TextDecoder, Uint8Array, atob, btoa, console, Date, crypto: cryptoUnavailable ? undefined : crypto.webcrypto,
    setTimeout(fn) { timers.push(fn); return timers.length; },
    fetch() { throw Error('Debugger capture must never make a platform HTTP request'); } };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root, 'video-request-schema.js'), 'utf8'), context);
  vm.runInContext(summarySource, context); vm.runInContext(debuggerSource, context);
  const api = context.__DOUBAO_DOLA_DEBUGGER_NETWORK__;
  async function settle() { for (let i = 0; i < 12; i++) await new Promise(resolve => setImmediate(resolve)); }
  return { context, chrome, tabs, commands, attachments, detachments, messages, badges, downloads, storage, owned, bodies, api, settle,
    async emit(method, params, id = 1) { networkEvents.emit({ tabId: id }, method, params); await settle(); },
    async report(id = 1) { return clean(await api.report(id)); },
    async message(message, sender = { id: 'fixture-extension', tab: { id: 1 } }) {
      return new Promise(resolve => onMessage.listeners[0](message, sender, resolve));
    },
    async navigate(id, url) { tabs.set(id, { id, url }); onUpdated.emit(id, { url }, tabs.get(id)); await settle(); },
    async flush() { for (const timer of timers.splice(0)) timer(); await settle(); }
  };
}
function videoRequest() {
  return { messages: [{ role: 'user', ext: { chat_ability: JSON.stringify({ ability_type: 17, ability_param: JSON.stringify({
    model: 'seedance_v2.5', duration: 30, resolution: '1080p', ratio: '16:9', input_box_content: JSON.stringify({ user_input_content: '【视频生成请求】私人原稿' })
  }) }) }, content_blocks_v2: [{ block_type: 10000, content: { text_block: { text: '【视频生成请求】私人原稿' } } },
    { block_type: 2, content: { attachment_block: { url: 'https://ref.test?token=private-ref' } } }] }], conversation_id: 'private-conversation' };
}
function sent(requestId = 'req1', data = {}) {
  return { requestId, type: 'Fetch', request: { url: 'https://www.doubao.com/chat/completion?a_bogus=private-signature&token=private',
    method: 'POST', postData: JSON.stringify(videoRequest()), headers: { Cookie: 'private-cookie', Authorization: 'private-auth' } }, ...data };
}

test('requested debugger permission loads both worker modules; domestic auto-attach enables only Network, Dola stays manual', async () => {
  const env = environment(); await env.api.ready;
  assert.equal(env.attachments.length, 1); assert.equal(env.attachments[0].target.tabId, 1); assert.equal(env.attachments[0].version, '1.3');
  assert.deepEqual(env.commands.map(value => value.method), ['Network.enable']);
  assert.equal((await env.report()).attached, true); assert.equal((await env.report(2)).enabled, false);
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'manifest.json')));
  assert.ok(manifest.permissions.includes('debugger')); assert.ok(manifest.permissions.includes('storage'));
  assert.match(fs.readFileSync(path.join(root, 'service-worker.js'), 'utf8'), /importScripts\("video-request-schema.js", "debugger-summary.js", "debugger-network.js"\)/);
  await env.api.activate(1); await env.api.activate(1); assert.equal(env.attachments.length, 1);
});

test('debug export is downloaded by the background worker without exposing captured secrets', async () => {
  const env = environment(); await env.api.ready;
  await env.emit('Network.requestWillBeSent', sent());
  const result = await env.message({ type: 'DEBUGGER_EXPORT', localDiagnostics: { version: '1.6.60', marker: 'local-diagnostics' } });
  assert.equal(result.ok, true); assert.equal(result.exportDownloadId, 1); assert.equal(env.downloads.length, 1);
  const download = env.downloads[0]; assert.equal(download.saveAs, false); assert.match(download.filename, /debugger-v1\.6\.60-/);
  assert.match(download.url, /^data:application\/json;charset=utf-8;base64,/);
  const exported = JSON.parse(Buffer.from(download.url.split(',')[1], 'base64').toString('utf8'));
  assert.equal(exported.marker, 'local-diagnostics'); assert.equal(exported.version, '1.6.60');
  assert.equal(exported.debuggerNetwork.requests.length, 1);
  assert.doesNotMatch(JSON.stringify(exported), /private-signature|private-cookie|private-auth|private-conversation|私人原稿|private-ref/);
});

test('attach rejection is surfaced without commands, false ON status or automatic repeated attach attempts', async () => {
  const env = environment({ attachError: 'Another debugger is already attached' }); await env.api.ready;
  const report = await env.report(); assert.equal(report.attached, false); assert.equal(report.phase, 'attach_error');
  assert.match(report.error, /Another debugger/); assert.equal(env.commands.length, 0);
  await env.api.activate(1); assert.equal(env.attachments.length, 1);
  await env.api.setEnabled(1, true); assert.equal(env.attachments.length, 2); assert.equal(env.commands.length, 0);
});

test('Network enable failure releases the actual session and reports the failure', async () => {
  const env = environment({ enableError: 'Network unavailable' }); await env.api.ready;
  const report = await env.report(); assert.equal(report.attached, false); assert.equal(report.phase, 'attach_error');
  assert.equal(env.detachments.length, 1); assert.equal(env.owned.has(1), false); assert.match(report.error, /Network unavailable/);
});

test('an unavailable debugger API never reports attachment', async () => {
  const env = environment({ unavailable: true }); await env.api.ready;
  assert.equal((await env.report()).attached, false); assert.equal((await env.report()).error, 'DEBUGGER_API_UNAVAILABLE');
});

test('the actual browser request, HTTP response, SSE task candidates and numeric costs are captured without headers, raw prompts or IDs', async () => {
  const env = environment(); await env.api.ready;
  const requestEvent = sent(); const before = JSON.stringify(requestEvent);
  await env.emit('Network.requestWillBeSent', requestEvent);
  await env.emit('Network.responseReceived', { requestId: 'req1', response: { status: 200, mimeType: 'text/event-stream', fromServiceWorker: true } });
  await env.emit('Network.dataReceived', { requestId: 'req1', dataLength: 340 });
  env.bodies.set('req1', 'data: {"event_data":"{\\"video_task\\":{\\"task_id\\":\\"private-task-123\\",\\"status\\":\\"queued\\",\\"consumed_points\\":2}}"}\n\ndata: [DONE]\n\n');
  await env.emit('Network.loadingFinished', { requestId: 'req1', encodedDataLength: 200 });
  const report = await env.report(); const record = report.requests[0];
  assert.equal(record.endpoint, 'https://www.doubao.com/chat/completion'); assert.equal(record.signaturePresent, true);
  assert.equal(record.request.video.model, 'seedance_v2.5'); assert.equal(record.request.video.duration, 30); assert.equal(record.request.video.resolution, '1080p');
  assert.equal(record.request.prefixMarkerPresent, true); assert.deepEqual(record.request.textBlockTypes, [10000, 2]);
  assert.equal(record.status, 200); assert.equal(record.phase, 'response_ended'); assert.equal(record.receivedBytes, 340); assert.equal(record.fromServiceWorker, true);
  assert.equal(record.response.taskFields.length, 2); assert.equal(record.response.billingFields[0].value, 2); assert.equal(report.verification, 'UNVERIFIED');
  assert.equal(report.lastSubmission.id, record.id); assert.equal(report.submissionCount, 1);
  assert.doesNotMatch(JSON.stringify(report), /private-signature|private-cookie|private-auth|private-task|private-conversation|私人原稿|private-ref/);
  assert.equal(JSON.stringify(requestEvent), before);
  assert.deepEqual(env.commands.map(value => value.method), ['Network.enable', 'Network.getResponseBody']);
});

test('pure assistant text including task-like JSON stays text; unknown schemas are visible by field path, with no success claim', async () => {
  const env = environment(); await env.api.ready; await env.emit('Network.requestWillBeSent', sent());
  env.bodies.set('req1', JSON.stringify({ code: 0, data: { novel_tool_envelope: { flag: 'unknown' } },
    message: { role: 'assistant', content: '{"task_id":"fake","consumed_points":999}', text: '已受理，消耗 2 点' } }));
  await env.emit('Network.loadingFinished', { requestId: 'req1' });
  const result = (await env.report()).requests[0].response;
  assert.deepEqual(result.taskFields, []); assert.deepEqual(result.billingFields, []); assert.equal(result.verification, 'UNVERIFIED');
  assert.ok(result.schema.includes('$.data.novel_tool_envelope.flag')); assert.equal(result.textFieldsSeen, 2);
});

test('business code and sanitized error message remain visible; HTTP errors and transport failures are preserved', async () => {
  const env = environment(); await env.api.ready; await env.emit('Network.requestWillBeSent', sent());
  await env.emit('Network.responseReceived', { requestId: 'req1', response: { status: 403, mimeType: 'application/json' } });
  env.bodies.set('req1', '{"code":710022002,"message":"权限不足 https://api.test?token=secret"}');
  await env.emit('Network.loadingFinished', { requestId: 'req1' });
  let record = (await env.report()).requests[0]; assert.equal(record.status, 403); assert.equal(record.response.businessErrors[0].code, 710022002);
  assert.equal(record.response.businessErrors[0].message, '权限不足 [url]');
  await env.emit('Network.requestWillBeSent', sent('req2')); await env.emit('Network.loadingFailed', { requestId: 'req2', errorText: 'net::ERR_ABORTED', canceled: true });
  record = (await env.report()).requests[1]; assert.equal(record.phase, 'transport_error'); assert.equal(record.error, 'net::ERR_ABORTED'); assert.equal(record.cancelled, true);
});

test('missing post data uses Network.getRequestPostData, and a slow result can enrich a response that already finished', async () => {
  let deliver;
  const env = environment({ commandHandler(target, method) { if (method === 'Network.getRequestPostData') return new Promise(resolve => { deliver = resolve; }); } });
  await env.api.ready; const event = sent(); delete event.request.postData; event.request.hasPostData = true;
  env.chrome.debugger.onEvent.emit({ tabId: 1 }, 'Network.requestWillBeSent', event); await env.settle();
  await env.emit('Network.loadingFinished', { requestId: 'req1' });
  deliver({ postData: JSON.stringify(videoRequest()) }); await env.settle();
  const record = (await env.report()).requests[0]; assert.equal(record.request.video.duration, 30); assert.equal(record.phase, 'response_ended');
  assert.equal(env.commands.filter(value => value.method === 'Network.getRequestPostData').length, 1);
});

test('base64 UTF-8 is decoded, while an evicted or inaccessible body has an explicit failure and is not retried', async () => {
  const env = environment(); await env.api.ready; await env.emit('Network.requestWillBeSent', sent());
  env.bodies.set('req1', { body: Buffer.from('{"code":123,"message":"额度不足"}').toString('base64'), base64Encoded: true });
  await env.emit('Network.loadingFinished', { requestId: 'req1' }); assert.equal((await env.report()).requests[0].response.businessErrors[0].message, '额度不足');
  await env.emit('Network.requestWillBeSent', sent('req2')); env.bodies.set('req2', Error('No resource with given identifier'));
  await env.emit('Network.loadingFinished', { requestId: 'req2' }); await env.emit('Network.loadingFinished', { requestId: 'req2' });
  const record = (await env.report()).requests[1]; assert.equal(record.phase, 'response_unavailable'); assert.equal(record.response, null);
  assert.equal(env.commands.filter(value => value.method === 'Network.getResponseBody' && value.params.requestId === 'req2').length, 1);
});

test('binary responses and oversized responses are skipped without response replacement', async () => {
  const env = environment(); await env.api.ready;
  for (const [key, mime, bytes] of [['binary', 'video/mp4', 5], ['large', 'application/json', 2_000_001]]) {
    await env.emit('Network.requestWillBeSent', sent(key)); await env.emit('Network.responseReceived', { requestId: key, response: { status: 200, mimeType: mime } });
    await env.emit('Network.dataReceived', { requestId: key, dataLength: bytes }); await env.emit('Network.loadingFinished', { requestId: key });
  }
  const records = (await env.report()).requests; assert.deepEqual(records.map(value => value.responseSkipped), ['NON_TEXT_RESPONSE', 'BODY_LIMIT']);
  assert.equal(env.commands.filter(value => value.method === 'Network.getResponseBody').length, 0);
  assert.equal(env.commands.some(value => /Fetch\.|replay|setExtraHTTPHeaders|setRequestInterception/.test(value.method)), false);
});

test('stopping during attach waits for the pending operation, detaches it and cannot leave a false attached status', async () => {
  let finish; const pendingAttach = new Promise(resolve => { finish = resolve; });
  const env = environment({ attachHandler: () => pendingAttach }); await env.settle();
  const stopping = env.api.setEnabled(1, false); finish(); await env.api.ready; await stopping;
  const report = await env.report(); assert.equal(report.attached, false); assert.equal(report.enabled, false); assert.equal(env.owned.has(1), false);
});

test('user or DevTools detach is respected across activation and navigation until the user explicitly re-enables', async () => {
  const env = environment(); await env.api.ready;
  env.owned.delete(1); env.chrome.debugger.onDetach.emit({ tabId: 1 }, 'canceled_by_user'); await env.settle();
  await env.navigate(1, 'https://www.doubao.com/chat/next'); assert.equal(env.attachments.length, 1); assert.equal((await env.report()).attached, false);
  await env.api.setEnabled(1, true); assert.equal(env.attachments.length, 2); assert.equal((await env.report()).attached, true);
});

test('navigation outside the supported sites detaches; Dola is an explicit opt-in and captures its genuine native request', async () => {
  const env = environment(); await env.api.ready;
  await env.navigate(1, 'https://example.test/private'); assert.equal(env.owned.has(1), false); assert.equal((await env.report()).phase, 'out_of_scope');
  const on = await env.message({ type: 'DEBUGGER_CONTROL', enabled: true }, { id: 'fixture-extension', tab: { id: 2 } }); assert.equal(on.attached, true);
  const request = sent(); request.request.url = 'https://www.dola.com/chat/completion';
  await env.emit('Network.requestWillBeSent', request, 2); assert.equal((await env.report(2)).submissionCount, 1);
  assert.equal((await env.message({ type: 'DEBUGGER_REPORT' })).ok, false);
});

test('a failed detach stays visible rather than silently reporting stopped', async () => {
  const env = environment({ detachError: true }); await env.api.ready;
  await env.api.setEnabled(1, false); const report = await env.report(); assert.equal(report.phase, 'detach_error'); assert.equal(report.attached, true);
  assert.match(report.reason, /停止未确认/);
  await env.emit('Network.requestWillBeSent', sent()); assert.equal((await env.report()).requests.length, 0);
  await env.navigate(1, 'https://example.test/'); assert.equal((await env.report()).phase, 'detach_error');
});

test('clear discards an in-flight asynchronous body without repopulating old records', async () => {
  let finish;
  const env = environment({ commandHandler(target, method) { if (method === 'Network.getResponseBody') return new Promise(resolve => { finish = resolve; }); } });
  await env.api.ready; await env.emit('Network.requestWillBeSent', sent());
  env.chrome.debugger.onEvent.emit({ tabId: 1 }, 'Network.loadingFinished', { requestId: 'req1' }); await env.settle();
  await env.api.clear(1); finish({ body: '{"task_id":"old"}' }); await env.settle();
  assert.deepEqual((await env.report()).requests, []); assert.equal((await env.report()).requestCount, 0);
});

test('worker restart restores sanitized records and verifies ownership through Network.enable before reporting attached', async () => {
  const storage = {}; const owned = new Set(); const first = environment({ storage, owned }); await first.api.ready;
  await first.emit('Network.requestWillBeSent', sent()); await first.report();
  const second = environment({ storage, owned }); await second.api.ready;
  const report = await second.report(); assert.equal(report.restored, true); assert.equal(report.attached, true); assert.equal(report.requests.length, 1);
  assert.equal(second.attachments.length, 0); assert.deepEqual(second.commands.map(value => value.method), ['Network.enable']);
  second.bodies.set('req1', '{"video_task":{"task_id":"native"}}'); await second.emit('Network.loadingFinished', { requestId: 'req1' });
  assert.equal((await second.report()).requests[0].response.taskFields.length, 1);
  assert.doesNotMatch(JSON.stringify(storage), /private-cookie|private-auth|私人原稿|private-ref|private-signature/);
});

test('the bounded ring keeps the latest submission while unrelated requests expire', async () => {
  const env = environment(); await env.api.ready; await env.emit('Network.requestWillBeSent', sent('submission'));
  for (let i = 0; i < 75; i++) await env.emit('Network.requestWillBeSent', sent('other-' + i, { request: { url: 'https://api.test/metrics', method: 'GET' } }));
  const report = await env.report(); assert.equal(report.requests.length, 60); assert.equal(report.dropped, 16); assert.equal(report.lastSubmission.id, 1);
});

test('sender checks prevent controlling a different or unsupported tab, and redirects do not merge unrelated response bodies', async () => {
  const env = environment(); await env.api.ready;
  assert.equal((await env.message({ type: 'DEBUGGER_CONTROL', enabled: true, tabId: 2 }, { id: 'other-extension', tab: { id: 1 } })).ok, false);
  assert.equal((await env.message({ type: 'DEBUGGER_CONTROL', enabled: 'yes' })).ok, false);
  await env.emit('Network.requestWillBeSent', sent());
  await env.emit('Network.requestWillBeSent', sent('req1', { type: 'Document', redirectResponse: { status: 302 }, request: { url: 'https://example.test/login', method: 'GET' } }));
  await env.emit('Network.loadingFinished', { requestId: 'req1' });
  const record = (await env.report()).requests[0]; assert.equal(record.phase, 'redirected'); assert.equal(record.redirectStatus, 302); assert.equal(record.response, null);
  assert.equal(env.commands.filter(value => value.method === 'Network.getResponseBody').length, 0);
});

test('SPA route promotion retains the in-flight browser response, and a later history task never replaces the latest submission', async () => {
  const env = environment(); await env.api.ready; await env.emit('Network.requestWillBeSent', sent());
  await env.navigate(1, 'https://www.doubao.com/chat/private-new-conversation');
  env.bodies.set('req1', '{"message":{"role":"assistant","text":"请确认参数"}}');
  await env.emit('Network.loadingFinished', { requestId: 'req1' });
  const history = sent('history', { request: { url: 'https://www.doubao.com/im/chain/single', method: 'GET' } });
  await env.emit('Network.requestWillBeSent', history); env.bodies.set('history', '{"task_id":"old-task"}');
  await env.emit('Network.loadingFinished', { requestId: 'history' });
  const report = await env.report(); assert.equal(report.lastSubmission.id, 1); assert.equal(report.lastSubmission.phase, 'response_ended');
  assert.deepEqual(report.lastSubmission.response.taskFields, []); assert.equal(report.requests[1].kind, 'history');
  assert.equal(report.requests[1].response.taskFields.length, 1); assert.equal(env.attachments.length, 1);
});

test('two actual observed submissions produce a semantic comparison without replay or additional protocol commands', async () => {
  const env = environment(); await env.api.ready;
  const first = videoRequest(); first.option = { cot_switch: false }; first.client_meta = { conversation_id: 'first-private-context' };
  const second = clean(first); second.option.cot_switch = true; second.client_meta.conversation_id = 'next-private-context';
  for (const [id, body] of [['before', first], ['after', second]]) {
    const event = sent(id); event.request.postData = JSON.stringify(body); await env.emit('Network.requestWillBeSent', event);
  }
  const report = await env.report(); const comparison = report.submissionComparison;
  assert.equal(comparison.previousId, 1); assert.equal(comparison.currentId, 2);
  assert.equal(comparison.equal.prompt, true, JSON.stringify(report.requests.map(record => record.request.comparison)));
  assert.equal(comparison.equal.video, true); assert.equal(comparison.equal.option, false);
  assert.deepEqual(comparison.routingChanges, [{ path: 'option.cot_switch', before: false, after: true }]);
  assert.ok(comparison.identifiers.some(field => field.path === 'clientMeta.conversation_id' && field.equal === false));
  assert.doesNotMatch(JSON.stringify(report), /first-private-context|next-private-context|私人原稿|private-cookie/);
  assert.deepEqual(env.commands.map(value => value.method), ['Network.enable']);
});

test('unavailable fingerprinting is explicit and cannot block network capture or manufacture equal requests', async () => {
  const env = environment({ cryptoUnavailable: true }); await env.api.ready;
  await env.emit('Network.requestWillBeSent', sent('first')); await env.emit('Network.requestWillBeSent', sent('second'));
  const report = await env.report(); assert.equal(report.attached, true); assert.equal(report.requests.length, 2);
  assert.equal(report.requests[0].request.comparison.error, 'SHA256_UNAVAILABLE');
  assert.equal(report.submissionComparison.equal.prompt, null); assert.equal(report.submissionComparison.equal.option, null);
});

test('field differences expose an unlisted option key and a separate conversation change using only observed requests', async () => {
  const env = environment(); await env.api.ready;
  const first = videoRequest(); first.option = { router: { mode: 'native_video' }, conversation_id: 'private-first-id' };
  const second = clean(first); second.option.router.mode = 'native_text'; second.option.conversation_id = 'private-next-id';
  for (const [id, body] of [['first', first], ['second', second]]) {
    const event = sent(id); event.request.postData = JSON.stringify(body); await env.emit('Network.requestWillBeSent', event);
  }
  const report = await env.report(); const diff = report.submissionComparison.fieldDiff;
  assert.equal(diff.complete, true); assert.equal(diff.totalChanges, 2);
  assert.deepEqual(report.submissionComparison.routingChanges, []);
  assert.ok(diff.changes.some(f => f.path === 'option.router.mode' && f.category === 'control_or_context'));
  const route = diff.changes.find(f => f.path === 'option.router.mode');
  assert.equal(route.before.value, 'native_video'); assert.equal(route.after.value, 'native_text');
  assert.ok(diff.changes.some(f => f.path === 'option.conversation_id' && f.category === 'identifier'));
  assert.doesNotMatch(JSON.stringify(report), /private-first-route|private-next-route|private-first-id|private-next-id/);
  assert.deepEqual(env.commands.map(value => value.method), ['Network.enable']);
});

test('missing fields under incomplete coverage are unobserved rather than falsely reported as removed', async () => {
  const env = environment(); await env.api.ready;
  const first = videoRequest(); first.option = { a: true };
  const second = clean(first); second.option = Object.fromEntries(Array.from({ length: 300 }, (_, i) => ['a_' + i, false]));
  for (const [id, body] of [['first', first], ['second', second]]) {
    const event = sent(id); event.request.postData = JSON.stringify(body); await env.emit('Network.requestWillBeSent', event);
  }
  const diff = (await env.report()).submissionComparison.fieldDiff;
  assert.equal(diff.complete, false); assert.equal(diff.truncated, true);
  const uncertainty = diff.changes.find(f => f.path === 'option.a');
  assert.equal(uncertainty.change, 'not_observed_after');
});
