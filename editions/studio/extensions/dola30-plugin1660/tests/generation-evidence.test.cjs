const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const evidenceSource = fs.readFileSync(path.join(root, 'generation-evidence-main.js'), 'utf8');
const transportSource = fs.readFileSync(path.join(root, 'resolution-main.js'), 'utf8');
const attribute = 'data-watermark-free-generation-evidence';
const eventName = 'watermark-free-generation-evidence';
const cleanPrompt = '原始剧本，请保留人物及素材引用。';
const params = { model: 'seedance_v2.5', resolution: '1080p', duration: 30 };
const body = () => JSON.stringify({ messages: [{ role: 'user', ext: { chat_ability: JSON.stringify({ ability_type: 17,
  ability_param: JSON.stringify(params) }) }, content_blocks_v2: [{ block_type: 1, text: cleanPrompt }] }] });
const normalized = value => JSON.parse(JSON.stringify(value));

function environment({ site = 'doubao', payload = { code: 0 }, status = 200, response, fetchHandler, rootPresent = true, compose = false, loadIntent = false } = {}) {
  const attrs = new Map();
  const element = { setAttribute(key, value) { attrs.set(key, value); }, getAttribute(key) { return attrs.get(key) ?? null; }, removeAttribute(key) { attrs.delete(key); } };
  const document = new EventTarget(); document.documentElement = rootPresent ? element : null;
  const calls = []; const sends = []; const intervals = [];
  class XHR extends EventTarget {
    constructor() { super(); this.readyState = 0; this.status = 0; this.responseType = ''; this._text = ''; this._json = null; }
    open(method, url) { this.method = method; this.url = url; this.readyState = 1; this.status = 0; this._text = ''; this._json = null; }
    send(value) { sends.push({ body: value, xhr: this }); }
    progress(text) { this._text += text; this.readyState = 3; this.status = 200; this.dispatchEvent(new Event('progress')); }
    respond(value, code = 200) {
      if (value != null) this._text = typeof value === 'string' ? value : JSON.stringify(value);
      try { this._json = JSON.parse(this._text); } catch { this._json = null; }
      this.readyState = 4; this.status = code;
      this.dispatchEvent(new Event('readystatechange')); this.dispatchEvent(new Event('load'));
    }
    get responseText() { if (this.responseType === 'json') throw Error('InvalidState'); return this._text; }
    get response() { return this.responseType === 'json' ? this._json : this._text; }
  }
  const context = { document, Event, CustomEvent, URL, Request, Response, Headers, TextEncoder, TextDecoder, Promise,
    XMLHttpRequest: XHR, console, setInterval(fn) { intervals.push(fn); },
    location: { hostname: `www.${site}.com`, href: `https://www.${site}.com/chat/current` },
    localStorage: { getItem() { return 'auto'; } },
    fetch(input, init) {
      const result = fetchHandler ? fetchHandler(input, init, calls.length) : Promise.resolve(response || new Response(
        typeof payload === 'string' ? payload : JSON.stringify(payload), { status, headers: { 'content-type': 'application/json' } }));
      calls.push({ input, init, result }); return result;
    }
  };
  context.window = context; vm.createContext(context);
  if (compose) vm.runInContext(fs.readFileSync(path.join(root, 'media-capture-main.js'), 'utf8'), context);
  if (loadIntent) {
    vm.runInContext(fs.readFileSync(path.join(root, 'video-request-schema.js'), 'utf8'), context);
    vm.runInContext(fs.readFileSync(path.join(root, 'doubao-generation-intent-main.js'), 'utf8'), context);
  }
  vm.runInContext(evidenceSource, context);
  if (compose && site === 'dola') vm.runInContext(fs.readFileSync(path.join(root, 'skill-pack-main.js'), 'utf8'), context);
  vm.runInContext(transportSource, context);
  return { context, document, element, calls, sends,
    status() { return normalized(context.__DOUBAO_DOLA_GENERATION_EVIDENCE__?.status); },
    tick() { for (const fn of intervals) fn(); },
    async settle() { for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve)); },
    submit(value = body()) { return context.fetch('/chat/completion?a_bogus=native-signature', { method: 'POST', body: value,
      credentials: 'include', headers: { 'x-signature': 'native-signature' } }); }
  };
}

test('assistant acceptance and point-consumption prose never become task or billing proof', async () => {
  const payload = { code: 0, message: { role: 'assistant', text: '已受理，开始生成该视频。将消耗 2 个视频生成额度。' } };
  const env = environment({ payload });
  const returned = env.submit(); assert.strictEqual(returned, env.calls[0].result);
  const response = await returned; assert.deepEqual(await response.json(), payload); await env.settle();
  const status = env.status();
  assert.equal(status.phase, 'response_ended'); assert.equal(status.verification, 'UNVERIFIED');
  assert.deepEqual(status.taskFields, []); assert.deepEqual(status.billingFields, []);
  assert.equal(status.videoParameters, true); assert.deepEqual(status.requestParameters, params);
  assert.equal(env.calls.length, 1); assert.equal(JSON.parse(env.calls[0].init.body).messages[0].content_blocks_v2[0].text, cleanPrompt);
});

test('prefix diagnostics inspect the exact outgoing fetch and XHR bodies without rewriting them or claiming task acceptance', async () => {
  const env = environment({ loadIntent: true, payload: { text: 'still an ordinary textual reply' } });
  const api = env.context.__DOUBAO_GENERATION_INTENT__;
  const prefixed = api.prepare(JSON.parse(body())); const serialized = JSON.stringify(prefixed);
  const request = new Request('https://www.doubao.com/chat/completion', { method: 'POST', body: serialized });
  const response = await env.context.fetch(request); await response.text(); await env.settle();
  assert.deepEqual(env.status().requestIntent, { video: true, present: true, inputBoxPresent: false });
  assert.strictEqual(env.calls[0].input, request); assert.equal(await request.clone().text(), serialized);
  assert.equal(env.status().verification, 'UNVERIFIED'); assert.deepEqual(env.status().taskFields, []);
  const xhr = new env.context.XMLHttpRequest(); xhr.open('POST', '/chat/completion'); xhr.send(serialized); xhr.respond({ text: 'text only' });
  assert.equal(env.sends[0].body, serialized); assert.equal(env.status().requestIntent.present, true);
  await (await env.submit()).text(); await env.settle();
  assert.deepEqual(env.status().requestIntent, { video: true, present: false, inputBoxPresent: false });
  assert.equal(env.status().verification, 'UNVERIFIED');
  await (await env.submit(JSON.stringify({ messages: [{ role: 'user', text: 'ordinary chat' }] }))).text(); await env.settle();
  assert.deepEqual(env.status().requestIntent, { video: false, present: false, inputBoxPresent: false });
});

test('actual wire-shaped top-level chat_ability and encoded content are observed without turning an unknown layout into a negative prefix verdict', async () => {
  const env = environment({ loadIntent: true }); const api = env.context.__DOUBAO_GENERATION_INTENT__;
  const native = api.prepare(JSON.parse(body())); const message = native.messages[0];
  const wire = { client_meta: {}, messages: [{ content: JSON.stringify(message.content_blocks_v2) }], option: {}, chat_ability: JSON.parse(message.ext.chat_ability), user_context: {}, ext: {} };
  const encoded = JSON.stringify(wire); await (await env.submit(encoded)).text(); await env.settle();
  assert.deepEqual(env.status().requestIntent, { video: true, present: true, inputBoxPresent: false });
  assert.equal(env.status().requestParameters.model, 'seedance_v2.5'); assert.equal(env.calls[0].init.body, encoded);
  wire.messages[0] = { future_layout: {} }; await (await env.submit(JSON.stringify(wire))).text(); await env.settle();
  assert.deepEqual(env.status().requestIntent, { video: true, present: null, inputBoxPresent: false });
});

test('structured task and numeric cost fields remain unverified candidates, with raw IDs, prompts and URLs omitted', async () => {
  const payload = { code: 0, data: { video_task: { task_id: 'private-server-task-123', status: 'queued', consumed_points: 2,
    remaining_quota: 0, task_url: 'https://example.test?token=private', prompt: 'private original prompt' } } };
  const env = environment({ payload }); const response = await env.submit(); await response.text(); await env.settle();
  const status = env.status();
  assert.equal(status.verification, 'UNVERIFIED'); assert.equal(status.taskFields.length, 2);
  assert.equal(status.taskFields[0].value.present, true);
  assert.deepEqual(status.billingFields.map(field => field.value), [2, 0]);
  assert.ok(status.billingFields.every(field => field.path.startsWith('$.data.video_task.')));
  assert.doesNotMatch(JSON.stringify(status), /private-server-task|private original prompt|token=private|a_bogus/);
});

test('price-like prose is not parsed as a number, and a numeric field is kept with its original path and type', async () => {
  const env = environment({ payload: { data: { consumed_points: '将消耗2点', remaining_credits: '2.5', cost: 99,
    text: JSON.stringify({ task_id: 'assistant fabricated', consumed_points: 999 }) } } });
  await (await env.submit()).text(); await env.settle();
  assert.deepEqual(env.status().billingFields, [{ path: '$.data.remaining_credits', key: 'remaining_credits', value: '2.5' }]);
  assert.deepEqual(env.status().taskFields, []);
});

test('SSE bytes split across chunks expose structured candidates before closing, without changing the original stream', async () => {
  let controller;
  const stream = new ReadableStream({ start(value) { controller = value; } });
  const response = new Response(stream, { headers: { 'content-type': 'text/event-stream' } });
  const env = environment({ response }); const returned = await env.submit(); assert.strictEqual(returned, response);
  const originalText = returned.text();
  const event = 'event: delta\r\ndata: {\n' + 'data: "event_data": "{\\"video_task\\":{\\"task_id\\":\\"候选编号\\",\\"point_cost\\":2}}"\n' + 'data: }\n\n';
  const encoded = new TextEncoder().encode(event);
  for (let offset = 0; offset < encoded.length; offset += 7) controller.enqueue(encoded.slice(offset, offset + 7));
  await env.settle();
  assert.equal(env.status().phase, 'response_streaming'); assert.equal(env.status().taskFields.length, 1);
  assert.equal(env.status().billingFields[0].value, 2); assert.equal(env.status().verification, 'UNVERIFIED');
  controller.close(); assert.equal(await originalText, event); await env.settle();
  assert.equal(env.status().phase, 'response_ended'); assert.equal(env.calls.length, 1);
});

test('business rejection and HTTP errors survive exactly; a 200 code does not manufacture video acceptance', async () => {
  for (const status of [200, 403]) {
    const payload = { code: 710022002, message: 'original rejection' };
    const env = environment({ status, payload }); const returned = await env.submit();
    assert.equal(returned.status, status); assert.deepEqual(await returned.json(), payload); await env.settle();
    assert.equal(env.status().phase, status === 200 ? 'response_ended' : 'http_error');
    assert.equal(env.status().statusFields[0].value, 710022002); assert.equal(env.status().verification, 'UNVERIFIED');
    assert.deepEqual(env.status().taskFields, []); assert.equal(env.calls.length, 1);
  }
});

test('network rejection is returned to the caller unchanged, with no retry', async () => {
  const failure = Error('native network failure');
  const env = environment({ fetchHandler() { return Promise.reject(failure); } });
  await assert.rejects(env.submit(), error => error === failure); await env.settle();
  assert.equal(env.status().phase, 'transport_error'); assert.equal(env.status().error, 'FETCH_FAILED'); assert.equal(env.calls.length, 1);
});

test('new submissions discard old responses; navigation retains the last record with explicit scope', async () => {
  let finishFirst;
  const first = new Promise(resolve => { finishFirst = resolve; });
  const env = environment({ fetchHandler(input, init, count) { return count === 0 ? first : Promise.resolve(new Response('{"text":"only text"}')); } });
  const old = env.submit(); await (await env.submit()).text(); await env.settle();
  finishFirst(new Response('{"task_id":"old task","consumed_points":2}')); await (await old).text(); await env.settle();
  assert.equal(env.status().sequence, 2); assert.deepEqual(env.status().taskFields, []); assert.deepEqual(env.status().billingFields, []);
  env.context.location.href += '-next'; env.tick(); assert.equal(env.status().phase, 'response_ended');
  assert.equal(env.status().scope, 'page_changed_since_submission'); assert.equal(env.status().routeChangeCount, 1);
  assert.deepEqual(JSON.parse(env.element.getAttribute(attribute)).taskFields, []);
});

test('new-chat URL promotion during an XHR response preserves request metadata, HTTP status and received fields', () => {
  const env = environment(); env.context.location.href = 'https://www.doubao.com/chat/';
  const xhr = new env.context.XMLHttpRequest(); xhr.open('POST', '/chat/completion'); xhr.send(body());
  assert.equal(env.status().sequence, 1); assert.equal(env.status().phase, 'request_observed');
  env.context.location.href = 'https://www.doubao.com/chat/new-conversation'; env.tick();
  xhr.progress('data: {"code":0,"video_task":{"task_id":"candidate","point_cost":2}}\n\n'); xhr.respond(null);
  const status = env.status();
  assert.equal(status.phase, 'response_ended'); assert.equal(status.sequence, 1); assert.equal(status.httpStatus, 200);
  assert.equal(status.videoParameters, true); assert.deepEqual(status.requestParameters, params);
  assert.equal(status.scope, 'page_changed_since_submission'); assert.equal(status.routeChangeCount, 1);
  assert.equal(status.bytesRead > 0, true); assert.equal(status.taskFields.length, 1); assert.equal(status.billingFields[0].value, 2);
  assert.equal(status.verification, 'UNVERIFIED'); assert.equal(env.sends.length, 1);
});

test('an asynchronously observed Request body and fetch response survive a route change before resolution', async () => {
  let deliver;
  const original = new Promise(resolve => { deliver = resolve; });
  const env = environment({ fetchHandler() { return original; } });
  const request = new Request('https://www.doubao.com/chat/completion', { method: 'POST', body: body() });
  const returned = env.context.fetch(request); assert.strictEqual(returned, original);
  env.context.location.href += '?conversation=created'; env.tick();
  const response = new Response('{"code":0,"video_task":{"task_id":"candidate","remaining_quota":0}}', { headers: { 'content-type': 'application/json' } });
  deliver(response); assert.strictEqual(await returned, response); await response.text(); await env.settle();
  const status = env.status();
  assert.equal(status.phase, 'response_ended'); assert.equal(status.videoParameters, true);
  assert.equal(status.requestParameters.model, 'seedance_v2.5'); assert.equal(status.taskFields.length, 1);
  assert.equal(status.billingFields[0].value, 0); assert.equal(status.scope, 'page_changed_since_submission');
  assert.equal(env.calls.length, 1); assert.strictEqual(env.calls[0].input, request);
});

test('retained evidence after a route change is cleared only by the next submission, without replaying a request', async () => {
  const env = environment({ payload: { video_task: { task_id: 'candidate', consumed_points: 2 } } });
  await (await env.submit()).text(); await env.settle();
  env.context.location.href += '-other-conversation'; env.tick();
  assert.equal(env.status().taskFields.length, 1); assert.equal(env.status().scope, 'page_changed_since_submission');
  assert.equal(env.calls.length, 1);
  const xhr = new env.context.XMLHttpRequest(); xhr.open('POST', '/chat/completion'); xhr.send(body());
  assert.equal(env.status().sequence, 2); assert.equal(env.status().scope, 'submission_page'); assert.equal(env.status().routeChangeCount, 0);
  assert.deepEqual(env.status().taskFields, []); assert.deepEqual(env.status().billingFields, []);
  xhr.respond({ text: 'only a reply' }); assert.equal(env.status().phase, 'response_ended');
  assert.equal(env.status().verification, 'UNVERIFIED'); assert.equal(env.calls.length, 1); assert.equal(env.sends.length, 1);
});

test('XHR progressive SSE and JSON responses retain their native values and expose only diagnostic candidates', async () => {
  const env = environment(); const xhr = new env.context.XMLHttpRequest(); xhr.open('POST', '/chat/completion'); xhr.send(body());
  const text = 'data: {"video_task":{"task_id":"native","consumed_credits":2}}\n\n';
  xhr.progress(text.slice(0, 20)); xhr.progress(text.slice(20));
  assert.equal(env.status().taskFields.length, 1); assert.equal(env.status().billingFields[0].value, 2);
  xhr.respond(null); assert.equal(xhr.responseText, text); assert.equal(env.status().phase, 'response_ended');
  xhr.open('POST', '/chat/completion'); xhr.responseType = 'json'; xhr.send(body());
  const payload = { code: 0, video_task: { taskId: 'native-json', pointCost: 0 } }; xhr.respond(payload);
  assert.deepEqual(xhr.response, payload); assert.equal(env.status().sequence, 2);
  assert.equal(env.status().billingFields[0].value, 0); assert.equal(env.status().verification, 'UNVERIFIED');
  assert.equal(env.sends.length, 2); assert.equal(env.sends[0].body, body());
});

test('reopening an XHR for ordinary endpoints removes old observers; XHR errors are not replaced by success', () => {
  const env = environment(); const xhr = new env.context.XMLHttpRequest(); xhr.open('POST', '/chat/completion'); xhr.send(body());
  xhr.dispatchEvent(new Event('timeout')); assert.equal(env.status().error, 'XHR_TIMEOUT');
  const before = env.status();
  xhr.open('GET', '/unrelated'); xhr.send(); xhr.respond({ task_id: 'unrelated', consumed_points: 99 });
  assert.deepEqual(env.status(), before);
});

test('pretty JSON with a missing JSON content type is understood without treating prose as an envelope', async () => {
  const payload = { video_task: { task_id: 'candidate', cost: 1 } };
  const response = new Response(JSON.stringify(payload, null, 2));
  const env = environment({ response }); assert.equal(await (await env.submit()).text(), JSON.stringify(payload, null, 2));
  await env.settle(); assert.equal(env.status().taskFields.length, 1); assert.equal(env.status().billingFields[0].value, 1);
});

test('binary and oversized responses are forwarded, with limits reported instead of fabricated fields', async () => {
  const binary = new Response(new Uint8Array([1, 2, 3]), { headers: { 'content-type': 'application/octet-stream' } });
  const env = environment({ response: binary }); const returned = await env.submit();
  assert.equal(returned.bodyUsed, false); assert.deepEqual([...new Uint8Array(await returned.arrayBuffer())], [1, 2, 3]);
  await env.settle(); assert.deepEqual(env.status().taskFields, []);
  const huge = environment({ response: new Response('x'.repeat(2_000_100)) });
  assert.equal((await (await huge.submit()).text()).length, 2_000_100); await huge.settle();
  assert.equal(huge.status().truncated, true); assert.deepEqual(huge.status().taskFields, []);
});

test('request-only model parameters are observations, never creation proof; signed Request input stays the same', async () => {
  const env = environment(); const input = JSON.parse(body());
  const ability = JSON.parse(input.messages[0].ext.chat_ability); ability.ability_param = '{"model":"seedance_v2.5"}';
  input.messages[0].ext.chat_ability = JSON.stringify(ability);
  const request = new Request('https://www.doubao.com/chat/completion?a_bogus=keep', { method: 'POST', body: JSON.stringify(input) });
  const result = await env.context.fetch(request); await result.text(); await env.settle();
  assert.strictEqual(env.calls[0].input, request); assert.equal(env.status().requestParameters.model, 'seedance_v2.5');
  assert.equal(env.status().verification, 'UNVERIFIED'); assert.deepEqual(env.status().taskFields, []);
});

test('installation is idempotent, ignores other endpoints, and buffers evidence until documentElement exists', async () => {
  const env = environment({ rootPresent: false }); const api = env.context.__DOUBAO_DOLA_GENERATION_EVIDENCE__;
  vm.runInContext(evidenceSource, env.context); assert.strictEqual(env.context.__DOUBAO_DOLA_GENERATION_EVIDENCE__, api);
  await env.context.fetch('/im/chain/single'); assert.equal(env.status().phase, 'idle');
  await (await env.submit()).text(); await env.settle();
  env.document.documentElement = env.element; env.document.dispatchEvent(new Event('DOMContentLoaded'));
  assert.equal(JSON.parse(env.element.getAttribute(attribute)).phase, 'response_ended');
});

for (const site of ['doubao', 'dola']) {
  test(`${site}: production-order media, duration and evidence hooks share one native response without submitting anything else`, async () => {
    const payload = { code: 0, ...(site === 'dola' ? { conversation_id: 'current' } : {}),
      video_task: { task_id: 'candidate', point_cost: 2, fallback_api: 'https://api.example.test/video?id=native' } };
    const response = new Response(JSON.stringify(payload), { headers: { 'content-type': 'application/json' } });
    const env = environment({ site, compose: true, response });
    const returned = await env.submit(); assert.strictEqual(returned, response); assert.deepEqual(await returned.json(), payload);
    await env.settle(); assert.equal(env.calls.length, 1); assert.equal(env.status().billingFields[0].value, 2);
    assert.equal(env.status().verification, 'UNVERIFIED'); assert.equal(env.status().taskFields.length, 1);
    const captured = JSON.parse(env.element.getAttribute('data-dola-media-fallback-apis'))[0];
    assert.equal(site === 'dola' ? JSON.parse(captured).url : captured, payload.video_task.fallback_api);
  });
}
