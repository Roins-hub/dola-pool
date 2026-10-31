const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '..');
const captureSource = fs.readFileSync(path.join(root, 'media-capture-main.js'), 'utf8');
const workerSource = fs.readFileSync(path.join(root, 'service-worker.js'), 'utf8');
const apisAttribute = 'data-dola-media-fallback-apis';
const apisEvent = 'dola-media-fallback-apis';
const modelsAttribute = 'data-dola-media-video-models';
const modelsEvent = 'dola-media-video-models';
const fallback = 'https://api.example.test/video?video_id=fixture&key_seed=seed';
const plain = url => Buffer.from(url).toString('base64');
const normalized = value => JSON.parse(JSON.stringify(value));

function worker({ site = 'doubao', payload, status = 200, fetchHandler, clock, downloads } = {}) {
  const listeners = [];
  const messages = []; const requests = [];
  const event = () => ({ addListener() {} });
  const context = {
    URL, Response, Request, Headers, AbortController, TextEncoder, TextDecoder, Uint8Array,
    atob, btoa, crypto: crypto.webcrypto, setTimeout, clearTimeout, console,
    Date: clock ? class extends Date { static now() { return clock.now; } } : Date,
    importScripts(...names) { for (const name of names) vm.runInContext(fs.readFileSync(path.join(root, name), 'utf8'), context); },
    chrome: {
      runtime: { id: 'fixture-extension', onInstalled: event(), onStartup: event(), onMessage: { addListener(fn) { listeners.push(fn); } }, getURL: value => `https://extension.example.test/${value}` },
      tabs: { onActivated: event(), onUpdated: event(), onRemoved: event(), query: async () => [],
        get: async () => ({ id: 1, url: `https://www.${site}.com/chat/fixture` }),
        sendMessage: async (id, message) => { messages.push({ id, message }); return { ready: true }; } },
      action: { onClicked: event(), setBadgeText: async () => {} },
      downloads: { onDeterminingFilename: event(), ...downloads }, scripting: {}
    },
    fetch: async (url, init) => {
      requests.push({ url, init });
      if (fetchHandler) return fetchHandler(url, init);
      return new Response(JSON.stringify(payload || {}), { status, headers: { 'content-type': 'application/json' } });
    }
  };
  vm.createContext(context); vm.runInContext(workerSource, context);
  return { context, messages, requests, listeners };
}

function capture({ site = 'doubao', body = { fallback_api: fallback }, response, rootPresent = true, compose = false } = {}) {
  const attrs = new Map();
  const element = { setAttribute: (key, value) => attrs.set(key, String(value)), getAttribute: key => attrs.get(key) ?? null, removeAttribute: key => attrs.delete(key) };
  const document = new EventTarget(); document.documentElement = rootPresent ? element : null;
  const calls = []; const events = []; const modelEvents = []; const sends = [];
  document.addEventListener(apisEvent, () => events.push(JSON.parse(element.getAttribute(apisAttribute) || '[]')));
  document.addEventListener(modelsEvent, () => modelEvents.push(JSON.parse(element.getAttribute(modelsAttribute) || '[]')));
  class XHR extends EventTarget {
    constructor() { super(); this.readyState = 0; this.status = 0; this.responseType = ''; this._text = ''; this._json = null; }
    open(method, url) { this.method = method; this.url = url; this.readyState = 1; this.status = 0; this._text = ''; }
    send(body) { sends.push({ body, xhr: this }); }
    progress(text) { this._text += text; this.status = 200; this.readyState = 3; this.dispatchEvent(new Event('progress')); }
    complete(value) {
      if (this.responseType === 'json') this._json = value;
      else if (value != null) this._text = typeof value === 'string' ? value : JSON.stringify(value);
      this.status = 200; this.readyState = 4;
      this.dispatchEvent(new Event('readystatechange')); this.dispatchEvent(new Event('load'));
    }
    get responseText() { if (this.responseType === 'json') throw new DOMException('json', 'InvalidStateError'); return this._text; }
    get response() { return this.responseType === 'json' ? this._json : this._text; }
  }
  const context = {
    document, Event, URL, Request, Response, Headers, TextDecoder, TextEncoder, XMLHttpRequest: XHR, console,
    location: { hostname: `www.${site}.com`, href: `https://www.${site}.com/chat/fixture` },
    localStorage: { getItem: () => null },
    fetch(input, init) {
      const value = response || new Response(typeof body === 'string' ? body : JSON.stringify(body), { headers: { 'content-type': 'application/json' } });
      const result = Promise.resolve(value); calls.push({ input, init, value, result }); return result;
    }
  };
  context.window = context; vm.createContext(context); vm.runInContext(captureSource, context);
  if (compose) {
    vm.runInContext(fs.readFileSync(path.join(root, 'skill-pack-main.js'), 'utf8'), context);
    vm.runInContext(fs.readFileSync(path.join(root, 'resolution-main.js'), 'utf8'), context);
  }
  return { context, document, element, calls, events, modelEvents, sends,
    status: () => normalized(context.__DOUBAO_DOLA_MEDIA_CAPTURE__),
    async settle() { for (let i = 0; i < 6; i++) await new Promise(resolve => setImmediate(resolve)); } };
}

test('media capture loads before the legacy Dola hook on both sites', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'manifest.json')));
  const entry = manifest.content_scripts.find(value => value.js.includes('media-capture-main.js'));
  assert.deepEqual(entry.matches, ['*://*.doubao.com/*', '*://*.dola.com/*']);
  assert.deepEqual(entry.js.slice(0, 2), ['direct-video-utils.js', 'media-capture-main.js']); assert.equal(entry.run_at, 'document_start'); assert.equal(entry.world, 'MAIN');
});

for (const site of ['doubao', 'dola']) {
  test(`${site}: native response reaches the media bridge and remains unmodified`, async () => {
    const responseBody = site === 'dola' ? { conversation_id: 'fixture', fallback_api: fallback } : { fallback_api: fallback };
    const env = capture({ site, compose: true, body: responseBody });
    const response = await env.context.fetch('/im/chain/single');
    assert.strictEqual(response, env.calls[0].value);
    assert.deepEqual(await response.json(), responseBody);
    await env.settle();
    assert.equal(env.events.length, 1, JSON.stringify(env.status()));
    assert.equal(site === 'dola' ? JSON.parse(env.events[0][0]).url : env.events[0][0], fallback);
    assert.equal(env.status().capturedCandidates, 1);
  });
}

test('streaming fallback data is captured across byte chunks before the original response closes', async () => {
  let controller;
  const stream = new ReadableStream({ start(value) { controller = value; } });
  const env = capture({ response: new Response(stream, { headers: { 'content-type': 'text/event-stream' } }) });
  const received = new Promise(resolve => env.document.addEventListener(apisEvent, resolve, { once: true }));
  await env.context.fetch('/chat/completion');
  const text = `data: ${JSON.stringify({ fallback_api: fallback })}\n\n`;
  controller.enqueue(new TextEncoder().encode(text.slice(0, 18)));
  controller.enqueue(new TextEncoder().encode(text.slice(18)));
  try {
    await Promise.race([received, new Promise((_, reject) => setTimeout(() => reject(Error('capture timeout')), 500))]);
    assert.equal(env.events[0][0], fallback); assert.equal(env.calls[0].value.bodyUsed, false);
  } finally { controller.close(); }
  assert.equal(await env.calls[0].value.text(), text);
});

test('Dola history JSON captures nested video_model without treating the watermarked download URL as original', async () => {
  const model = qualities();
  const body = { downlink_body: { pull_singe_chain_downlink_body: { messages: [{ conversation_id: 'fixture',
    content: JSON.stringify([{ block_type: 2074, content: { creation_block: { creations: [{
      type: 2, video: { download_url: 'https://cdn.example.test/watermarked.mp4', video_model: JSON.stringify(model) }
    }] } } }])
  }] } } };
  const env = capture({ site: 'dola', body });
  await env.context.fetch('/im/chain/single'); await env.settle();
  assert.equal(env.modelEvents.length, 1, JSON.stringify(env.status()));
  assert.equal(env.modelEvents[0].length, 1);
  const captured = JSON.parse(env.modelEvents[0][0]);
  assert.equal(captured.conversationId, 'fixture');
  assert.deepEqual(captured.model, model);
  assert.equal(env.status().capturedModels, 1);
});

test('Dola normalizes an internal response id to the current numeric conversation before resolving video_model', async () => {
  const model = qualities();
  const body = { conversation_id: 'local_internal_123', result: { video_model: JSON.stringify(model) } };
  const env = capture({ site: 'dola', body });
  env.context.location.href = 'https://www.dola.com/chat/70000000000000007';
  await env.context.fetch('/im/chain/single'); await env.settle();
  assert.equal(env.modelEvents.length, 1, JSON.stringify(env.status()));
  const captured = JSON.parse(env.modelEvents[0][0]);
  assert.equal(captured.conversationId, '70000000000000007');
  assert.deepEqual(captured.model, model);
});

test('Dola SSE captures video_model from a data envelope split across response chunks', async () => {
  let controller;
  const stream = new ReadableStream({ start(value) { controller = value; } });
  const env = capture({ site: 'dola', response: new Response(stream, { headers: { 'content-type': 'text/event-stream' } }) });
  await env.context.fetch('/chat/completion');
  const text = `event: STREAM_CHUNK\ndata: ${JSON.stringify({ conversation_id: 'fixture', patch_op: [{ patch_value: {
    video_model: JSON.stringify(qualities())
  } }] })}\n\n`;
  controller.enqueue(new TextEncoder().encode(text.slice(0, 37)));
  controller.enqueue(new TextEncoder().encode(text.slice(37)));
  controller.close(); await env.settle();
  assert.equal(env.modelEvents.length, 1, JSON.stringify(env.status()));
  assert.equal(JSON.parse(env.modelEvents[0][0]).conversationId, 'fixture');
  assert.equal(env.status().capturedModels, 1);
});

test('multi-layer JSON escapes preserve signed query bytes after decoding', async () => {
  const url = 'https://api.example.test/video?id=1&key_seed=abc%2Bxyz%3D';
  const raw = JSON.stringify({ data: JSON.stringify({ encoded: JSON.stringify({ fallback_api: url }) }) });
  const env = capture({ body: raw }); await env.context.fetch('/im/message/list'); await env.settle();
  assert.equal(env.events[0][0], url);
});

test('a result arriving before documentElement is buffered until DOMContentLoaded', async () => {
  const env = capture({ rootPresent: false }); await env.context.fetch('/im/chain/single'); await env.settle();
  assert.equal(env.events.length, 0);
  env.document.documentElement = env.element; env.document.dispatchEvent(new Event('DOMContentLoaded'));
  assert.equal(env.events[0][0], fallback);
});

test('an existing conversation can supply a resolver through JSON hydration without generating again', () => {
  const env = capture();
  env.document.querySelectorAll = () => [{ textContent: JSON.stringify({ video: { fallback_api: fallback } }) }];
  env.document.dispatchEvent(new Event('DOMContentLoaded'));
  assert.equal(env.events[0][0], fallback); assert.equal(env.calls.length, 0);
  assert.equal(env.status().transport, 'bootstrap-json');
});

test('signed generation fetch(Request) input and init are forwarded exactly once', async () => {
  const env = capture(); const request = new Request('https://www.doubao.com/chat/completion?a_bogus=keep', { method: 'POST', body: 'unchanged' });
  const init = { headers: { 'x-signature': 'keep' } };
  const promise = env.context.fetch(request, init);
  assert.strictEqual(promise, env.calls[0].result); assert.strictEqual(env.calls[0].input, request);
  assert.strictEqual(env.calls[0].init, init); assert.equal(request.bodyUsed, false);
  await promise; await env.settle(); assert.equal(env.calls.length, 1);
});

test('unrelated responses, lookalike domains and HTTP errors never produce candidates', async () => {
  for (const input of ['/samantha/skill/pack', 'https://doubao.com.evil.example/im/chain/single', 'https://example.com/im/chain/single']) {
    const env = capture(); await env.context.fetch(input); await env.settle(); assert.equal(env.events.length, 0);
  }
  const env = capture({ response: new Response(JSON.stringify({ fallback_api: fallback }), { status: 403 }) });
  await env.context.fetch('/im/chain/single'); await env.settle();
  assert.equal(env.events.length, 0); assert.equal(env.status().error, 'HTTP_403');
});

test('XHR progressive and JSON responses are captured; object reuse removes stale listeners', () => {
  const env = capture(); const xhr = new env.context.XMLHttpRequest();
  xhr.open('GET', '/im/chain/single'); xhr.send('preserve');
  xhr.progress('{"fallback_api":"https://api.example.test/'); xhr.progress('video?id=xhr"}'); xhr.complete();
  assert.equal(env.events[0][0], 'https://api.example.test/video?id=xhr'); assert.equal(env.sends[0].body, 'preserve');
  xhr.open('GET', '/im/message/list'); xhr.responseType = 'json'; xhr.complete({ fallback_api: fallback });
  assert.equal(env.events.at(-1).includes(fallback), true);
  const count = env.events.length; xhr.open('GET', '/unrelated'); xhr.complete({ fallback_api: 'https://api.example.test/ignored' });
  assert.equal(env.events.length, count);
});

test('manual retry forwards recent entries with force, without submitting generation', async () => {
  const env = capture(); await env.context.fetch('/im/chain/single'); await env.settle();
  env.element.removeAttribute(apisAttribute);
  env.document.dispatchEvent(new Event('watermark-free-media-retry'));
  assert.equal(env.events.length, 2); assert.equal(env.element.getAttribute('data-watermark-free-media-force'), '1');
  assert.equal(env.calls.length, 1);
});

test('page navigation discards results from the preceding conversation', async () => {
  const env = capture(); const promise = env.context.fetch('/im/chain/single');
  env.context.location.href = 'https://www.doubao.com/chat/new';
  await promise; await env.settle(); assert.equal(env.events.length, 0);
  env.document.dispatchEvent(new Event('watermark-free-media-retry'));
  assert.equal(env.status().error, 'REOPEN_VIDEO_CONVERSATION');
});

function qualities() {
  return { data: { video_list: {
    high_bitrate_720: { main_url: plain('https://cdn.example.test/720.mp4'), vwidth: 1280, vheight: 720, bitrate: 25000000 },
    full_hd_1080: { main_url: plain('https://cdn.example.test/1080.mp4'), vwidth: 1920, vheight: 1080, bitrate: 2000000 }
  } } };
}

test('1080p outranks a 720p stream with much higher bitrate; URL and actual dimensions survive', async () => {
  const env = worker({ payload: qualities() });
  const resource = await env.context.getUnwatermarkedVideoResourceFromFallbackApi(fallback, 'doubao', '1080p');
  assert.equal(resource.url, 'https://cdn.example.test/1080.mp4');
  assert.equal(resource.width, 1920); assert.equal(resource.height, 1080); assert.equal(resource.resolution, '1080p');
  const url = new URL(env.requests[0].url);
  assert.equal(url.searchParams.get('logo_type'), 'unwatermarked'); assert.equal(url.searchParams.get('codec_type'), '8');
  assert.equal(url.searchParams.get('channel'), 'no'); assert.equal(url.searchParams.get('video_id'), 'fixture');
  assert.equal(env.requests[0].init.credentials, 'omit');
});

test('domestic first-party resolution uses normal cookies; third-party resolution omits cookies', async () => {
  const env = worker({ payload: qualities() });
  await env.context.getUnwatermarkedVideoResourceFromFallbackApi('https://www.doubao.com/video/info?id=fixture', 'doubao');
  assert.equal(env.requests[0].init.credentials, 'include');
  await env.context.getUnwatermarkedVideoResourceFromFallbackApi(fallback, 'doubao');
  assert.equal(env.requests[1].init.credentials, 'omit');
});

for (const site of ['dola', 'doubao']) {
  test(`${site}: missing watermark metadata on 1080p does not incorrectly select labelled 720p`, async () => {
    const payload = qualities();
    payload.data.video_list.high_bitrate_720.has_watermark = false;
    const env = worker({ site, payload });
    const resource = await env.context.getUnwatermarkedVideoResourceFromFallbackApi(fallback, site, '1080p');
    assert.equal(resource.url, 'https://cdn.example.test/1080.mp4');
    assert.equal(resource.height, 1080);
    assert.equal(resource.watermark, 'unknown');
  });
}

test('explicitly watermarked variants are rejected; an available unmarked 720p is reported honestly', async () => {
  const payload = qualities(); payload.data.video_list.full_hd_1080.has_watermark = true;
  payload.data.video_list.high_bitrate_720.has_watermark = false;
  const env = worker({ payload });
  const resource = await env.context.getUnwatermarkedVideoResourceFromFallbackApi(fallback, 'dola', '1080p');
  assert.equal(resource.url, 'https://cdn.example.test/720.mp4'); assert.equal(resource.resolution, '720p');
  assert.equal(resource.watermark, 'unmarked'); assert.equal(resource.requestedResolution, '1080p');
  assert.deepEqual(normalized(resource.variants.map(value => [value.width, value.height, value.watermark])),
    [[1280, 720, 'unmarked'], [1920, 1080, 'marked']]);
  payload.data.video_list.high_bitrate_720.has_watermark = true;
  const empty = await env.context.getUnwatermarkedVideoResourceFromFallbackApi(fallback, 'dola');
  assert.equal(empty.url, ''); assert.equal(empty.error, 'NO_UNMARKED_VIDEO_CANDIDATE');
});

test('a malformed primary token can use a backup token at the same high resolution', async () => {
  const payload = qualities(); payload.data.video_list.full_hd_1080.main_url = 'invalid-token';
  payload.data.video_list.full_hd_1080.backup_url = plain('https://cdn.example.test/1080-backup.mp4');
  const env = worker({ payload });
  const resource = await env.context.getUnwatermarkedVideoResourceFromFallbackApi(fallback, 'dola');
  assert.equal(resource.url, 'https://cdn.example.test/1080-backup.mp4'); assert.equal(resource.height, 1080);
});

test('the chosen rendition key_seed takes precedence over an unrelated global seed', async () => {
  const vector = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/qaab-synthetic.json')));
  const payload = { key_seed: Buffer.alloc(32, 42).toString('base64'), data: { video_list: {
    fhd: { main_url: vector.token, key_seed: vector.seed, vwidth: 1920, vheight: 1080 }
  } } };
  const env = worker({ payload });
  const resource = await env.context.getUnwatermarkedVideoResourceFromFallbackApi('https://api.example.test/info?id=qaab', 'dola');
  assert.equal(resource.url, vector.url); assert.equal(resource.resolution, '1080p');
});

test('missing key_seed and HTTP rejection return useful failure codes, not guessed URLs', async () => {
  const vector = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/qaab-synthetic.json')));
  const env = worker({ payload: { video_list: { fhd: { main_url: vector.token } } } });
  const result = await env.context.getUnwatermarkedVideoResourceFromFallbackApi('https://api.example.test/info?id=1', 'dola');
  assert.equal(result.url, ''); assert.equal(result.error, 'KEY_SEED_MISSING');
  const rejected = worker({ status: 403 });
  assert.equal((await rejected.context.getUnwatermarkedVideoResourceFromFallbackApi(fallback, 'doubao')).error, 'HTTP_403');
});

test('definition-only entries keep their uncertainty and portrait dimensions are ranked correctly', async () => {
  const env = worker({ payload: { video_list: {
    '1080p': { main_url: plain('https://cdn.example.test/definition-only.mp4') }
  } } });
  const result = await env.context.getUnwatermarkedVideoResourceFromFallbackApi(fallback, 'dola');
  assert.equal(result.width, 0); assert.equal(result.height, 0); assert.equal(result.resolutionEvidence, 'definition');
  const portrait = env.context.rankVideoCandidates({ video_list: {
    a: { main_url: 'https://cdn.example.test/portrait-1080', width: 1080, height: 1920 },
    b: { main_url: 'https://cdn.example.test/portrait-720', width: 720, height: 1280, bitrate: 99999999 }
  } });
  assert.equal(portrait[0].token, 'https://cdn.example.test/portrait-1080');
});

test('cache coalesces calls, isolates source/preference and force retries a fresh variant', async () => {
  const env = worker({ payload: qualities() });
  const first = env.context.getCachedUnwatermarkedVideoResource(fallback, 'doubao', '1080p');
  const second = env.context.getCachedUnwatermarkedVideoResource(fallback, 'doubao', '1080p');
  assert.strictEqual(first, second); await first; assert.equal(env.requests.length, 1);
  await env.context.getCachedUnwatermarkedVideoResource(fallback, 'dola', '1080p');
  await env.context.getCachedUnwatermarkedVideoResource(fallback, 'doubao', '720p');
  await env.context.getCachedUnwatermarkedVideoResource(fallback, 'doubao', '1080p', true);
  assert.equal(env.requests.length, 4);
});

test('temporary lower resolution expires quickly while waiting for 1080p', async () => {
  const clock = { now: 0 }; let ready = false;
  const env = worker({ clock, fetchHandler: async () => {
    const data = qualities(); if (!ready) delete data.data.video_list.full_hd_1080;
    return new Response(JSON.stringify(data));
  } });
  assert.equal((await env.context.getCachedUnwatermarkedVideoResource(fallback, 'doubao', '1080p')).height, 720);
  ready = true; clock.now = 3001;
  assert.equal((await env.context.getCachedUnwatermarkedVideoResource(fallback, 'doubao', '1080p')).height, 1080);
  assert.equal(env.requests.length, 2);
});

test('domestic capture -> native bridge -> background resolver delivers the 1080p resource', async () => {
  const background = worker({ payload: qualities() }); const page = capture({ site: 'doubao', compose: true });
  let done;
  const completed = new Promise(resolve => { done = resolve; });
  page.document.addEventListener(apisEvent, async () => {
    const urls = JSON.parse(page.element.getAttribute(apisAttribute)); page.element.removeAttribute(apisAttribute);
    done(await background.context.resolveFallbackApisForTab(1, 'https://www.doubao.com/chat/fixture', urls, '1080p'));
  }, { once: true });
  await page.context.fetch('/im/chain/single');
  const report = await completed;
  assert.equal(report.count, 1); assert.equal(report.candidateCount, 1);
  assert.equal(report.qualities[0].selected, '1080p');
  assert.equal(report.qualities[0].variants.length, 2);
  const message = background.messages.find(value => value.message.type === 'MEDIA_FOUND').message;
  assert.equal(message.items[0].url, 'https://cdn.example.test/1080.mp4');
  assert.equal(message.items[0].width, 1920); assert.equal(message.items[0].height, 1080);
});

test('Dola video_model resolves its original main_url and never selects the watermarked page link', async () => {
  const env = worker({ site: 'dola' });
  const report = await env.context.resolveVideoModelsForTab(1, 'https://www.dola.com/chat/fixture',
    [JSON.stringify(qualities())], '1080p');
  assert.equal(report.count, 1); assert.equal(report.candidateCount, 1);
  const message = env.messages.find(value => value.message.type === 'MEDIA_FOUND').message;
  assert.equal(message.items[0].url, 'https://cdn.example.test/1080.mp4');
  assert.equal(message.items[0].provenance, 'video_model');
  assert.equal(message.items[0].watermark, 'unmarked');
  assert.equal(message.items[0].conversationKey, 'dola:fixture');
  assert.equal(message.items[0].captureOrder, 1);
});

test('Dola video_model rejects a decoded watermark rendition instead of labelling it original', async () => {
  const marked = 'https://v16-dola.dola.com/hash/video/tos/mya/file/?mime_type=video_mp4&lr=video_gen_watermark_dyn';
  const model = { data: { video_list: {
    high_bitrate_720: { main_url: plain(marked), vwidth: 1280, vheight: 720, bitrate: 25000000 }
  } } };
  const env = worker({ site: 'dola' });
  const report = await env.context.resolveVideoModelsForTab(1, 'https://www.dola.com/chat/fixture',
    [JSON.stringify(model)], '720p');
  assert.equal(report.count, 0);
  assert.deepEqual(normalized(report.failures), ['VIDEO_MODEL_IS_WATERMARKED']);
  assert.equal(env.messages.some(value => value.message.type === 'MEDIA_FOUND'), false);
});

test('marked Dola MP4 exposes its embedded vid through a bounded range request', async () => {
  const vid = 'v186a3gm000cdbe6dcfog65orn9v951g';
  const marked = 'https://v16-dola.dola.com/hash/video/tos/mya/file/?mime_type=video_mp4&lr=video_gen_watermark_dyn';
  const env = worker({ site: 'dola', fetchHandler: async (_url, init) =>
    new Response(new TextEncoder().encode(`ftyp....comment\0vid:${vid}\0tail`), { status: 206 }) });
  assert.equal(await env.context.extractEmbeddedDolaVideoId(marked), vid);
  assert.equal(env.requests[0].init.headers.Range, 'bytes=0-262143');
  assert.equal(env.requests[0].init.credentials, 'omit');
});

test('official Dola response false is reported honestly and never falls back to watermarked play-info', async () => {
  const vid = 'v186a3gm000cdbe6dcfog65orn9v951g';
  const marked = 'https://v16-dola.dola.com/hash/video/tos/mya/file/?mime_type=video_mp4&lr=video_gen_watermark_dyn';
  const env = worker({ site: 'dola', fetchHandler: async (url) => {
    if (String(url).includes('/creativity/resource/get_without_watermark')) {
      return new Response(JSON.stringify({ code: 0, data: { without_watermark: false } }));
    }
    return new Response(new TextEncoder().encode(`header vid:${vid} trailer`), { status: 206 });
  } });
  const report = await env.context.resolveMarkedVideoUrlsForTab(1, 'https://www.dola.com/chat/fixture', [marked], '720p');
  assert.equal(report.count, 0); assert.equal(report.videoIdCount, 1);
  assert.deepEqual(normalized(report.failures), ['PLATFORM_NO_UNWATERMARKED_RESOURCE']);
  assert.equal(env.requests.some(value => String(value.url).includes('get_play_info')), false);
  const api = env.requests.find(value => String(value.url).includes('get_without_watermark'));
  assert.equal(api.init.credentials, 'include');
  assert.deepEqual(JSON.parse(api.init.body), { vid: [vid] });
  assert.equal(env.messages.some(value => value.message.type === 'MEDIA_FOUND'), false);
});

test('official Dola no-watermark download is delivered only after an affirmative API response', async () => {
  const vid = 'v186a3gm000cdbe6eunog65qvf0aar90';
  const marked = 'https://v16-dola.dola.com/hash/video/tos/mya/file/?mime_type=video_mp4&lr=video_gen_watermark_dyn';
  const original = 'https://v16-dola.dola.com/hash/video/tos/mya/original/?mime_type=video_mp4&lr=video_gen_no_watermark';
  const env = worker({ site: 'dola', fetchHandler: async (url) => {
    if (String(url).includes('/creativity/resource/get_without_watermark')) {
      return new Response(JSON.stringify({ code: 0, data: { without_watermark: true,
        download_video: { [vid]: { download_url: original, width: 1920, height: 1080 } } } }));
    }
    return new Response(new TextEncoder().encode(`header vid:${vid} trailer`), { status: 206 });
  } });
  const report = await env.context.resolveMarkedVideoUrlsForTab(1, 'https://www.dola.com/chat/fixture', [marked], '1080p');
  assert.equal(report.count, 1); assert.deepEqual(normalized(report.failures), []);
  const item = env.messages.find(value => value.message.type === 'MEDIA_FOUND').message.items[0];
  assert.equal(item.url, original); assert.equal(item.watermark, 'unmarked');
  assert.equal(item.provenance, 'dola_unwatermarked_api');
  assert.equal(item.width, 1920); assert.equal(item.height, 1080);
});

test('resolver rejects an async result after the selected conversation changed even on the same host', async () => {
  const env = worker({ site: 'dola' });
  const report = await env.context.resolveVideoModelsForTab(1, 'https://www.dola.com/chat/previous',
    [JSON.stringify(qualities())], '720p', { conversationKey: 'dola:previous', title: '旧会话' });
  assert.deepEqual(normalized(report.failures), ['PAGE_CHANGED']);
  assert.equal(env.messages.some(value => value.message.type === 'MEDIA_FOUND'), false);
});

test('download acknowledges immediately even when the browser download API is slow', async () => {
  let finishDownload;
  const pending = new Promise(resolve => { finishDownload = resolve; });
  const env = worker({ downloads: {
    download: () => pending,
    search: async () => []
  } });
  const replies = [];
  env.listeners[0]({ type: 'DOWNLOAD_MEDIA', url: 'https://cdn.example.test/video.mp4', mediaType: 'video' }, {}, value => replies.push(value));
  assert.equal(replies.length, 1);
  assert.equal(replies[0].pending, true);
  assert.equal(typeof replies[0].requestId, 'string');
  finishDownload(41);
  await new Promise(resolve => setImmediate(resolve));
  const lookup = [];
  env.listeners[0]({ type: 'DOWNLOAD_LOOKUP', url: 'https://cdn.example.test/video.mp4',
    startedAt: replies[0].startedAt, requestId: replies[0].requestId }, {}, value => lookup.push(value));
  assert.equal(lookup[0].id, 41);
});

test('download creation errors are returned through lookup instead of closing the message port', async () => {
  const env = worker({ downloads: {
    download: () => Promise.reject(Error('Download blocked')),
    search: async () => []
  } });
  let start;
  env.listeners[0]({ type: 'DOWNLOAD_MEDIA', url: 'https://cdn.example.test/video.mp4', mediaType: 'video' }, {}, value => { start = value; });
  await new Promise(resolve => setImmediate(resolve));
  let lookup;
  env.listeners[0]({ type: 'DOWNLOAD_LOOKUP', url: 'https://cdn.example.test/video.mp4',
    startedAt: start.startedAt, requestId: start.requestId }, {}, value => { lookup = value; });
  assert.equal(lookup.ok, false);
  assert.equal(lookup.error, 'Download blocked');
});

test('download lookup recovers an extension download after service worker restart', async () => {
  const url = 'https://cdn.example.test/video.mp4';
  const startedAt = Date.now();
  const env = worker({ downloads: {
    search: async () => [{ id: 42, url, byExtensionId: 'fixture-extension', startTime: new Date(startedAt).toISOString() }]
  } });
  let result;
  env.listeners[0]({ type: 'DOWNLOAD_LOOKUP', url, startedAt, requestId: 'lost-on-restart' }, {}, value => { result = value; });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(result.id, 42);
});
