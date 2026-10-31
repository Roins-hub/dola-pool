// Offline integration tests: actual hooks, actual Node Request/Response,
// and a reusable XHR test double. No account or generation credits needed.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'resolution-main.js'), 'utf8');
const durationSource = fs.readFileSync(path.join(root, 'skill-pack-main.js'), 'utf8');
const modelSource = fs.readFileSync(path.join(root, 'model-selection-main.js'), 'utf8');
const fixture = (site) => JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', `${site}-skill-pack.json`), 'utf8'));
const skillPath = '/samantha/skill/pack';
const actionPath = '/alice/slot/action_bar_v3/get_item_conf';
const preferenceKey = 'intl_doubao_target_resolution_v1';

function environment({ site = 'doubao', body = fixture(site), pref = 'auto', compose = false,
                       statusCode = 200, fetchImplementation, rootPresent = true,
                       loadModel = false, modelPreference = 'auto' } = {}) {
  const attrs = new Map();
  const element = { setAttribute: (key, value) => attrs.set(key, String(value)), getAttribute: (key) => attrs.get(key) ?? null };
  const document = new EventTarget();
  document.documentElement = rootPresent ? element : null;
  const storage = new Map(pref === 'auto' ? [] : [[preferenceKey, pref]]);
  if (modelPreference !== 'auto') storage.set('intl_doubao_target_video_model_v1', modelPreference);
  const calls = [];
  const xhrCalls = [];
  let rawBody = body;

  class XHR extends EventTarget {
    constructor() { super(); this.readyState = 0; this.status = 0; this.responseType = ''; this._text = ''; this._json = null; }
    open(method, url) {
      this.method = method; this.url = url; this.readyState = 1; this.status = 0;
      this._text = ''; this._json = null;
    }
    send(body) { xhrCalls.push({ xhr: this, body, method: this.method, url: this.url }); }
    respond(body, status = 200) {
      this._text = typeof body === 'string' ? body : JSON.stringify(body);
      this._json = typeof body === 'string' ? (() => { try { return JSON.parse(body); } catch { return null; } })() : structuredClone(body);
      this.status = status; this.readyState = 4;
      this.dispatchEvent(new Event('readystatechange'));
      this.dispatchEvent(new Event('load'));
    }
    get responseText() {
      if (this.responseType && this.responseType !== 'text') throw new DOMException('Invalid responseType', 'InvalidStateError');
      return this._text;
    }
    get response() { return this.responseType === 'json' ? this._json : this._text; }
  }

  const context = {
    URL, Request, Response, Headers, Event, CustomEvent, DOMException, Blob, Promise,
    console, XMLHttpRequest: XHR, document,
    location: { hostname: `www.${site}.com`, href: `https://www.${site}.com/chat/` },
    localStorage: { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, String(value)) },
    fetch: function (input, init) {
      calls.push({ input, init, receiver: this, argumentCount: arguments.length });
      if (fetchImplementation) return fetchImplementation(input, init);
      const text = typeof rawBody === 'string' ? rawBody : JSON.stringify(rawBody);
      const response = new Response(statusCode === 204 || statusCode === 205 ? null : text, {
        status: statusCode, statusText: statusCode === 200 ? 'OK' : '',
        headers: { 'content-type': 'application/json', 'content-length': '1234', 'content-encoding': 'gzip', 'etag': 'old', 'x-preserved': 'yes' }
      });
      Object.defineProperties(response, {
        url: { configurable: true, value: new URL(typeof input === 'string' ? input : input.url || input.href, context.location.href).href },
        type: { configurable: true, value: 'cors' }, redirected: { configurable: true, value: true }
      });
      calls[calls.length - 1].response = response;
      return Promise.resolve(response);
    }
  };
  context.window = context;
  vm.createContext(context);
  if (loadModel) {
    document.readyState = 'loading';
    vm.runInContext(modelSource, context);
  }
  if (compose) vm.runInContext(durationSource, context);
  vm.runInContext(source, context);
  return { context, calls, xhrCalls, storage, element,
    setBody(value) { rawBody = value; },
    status() { return JSON.parse(JSON.stringify(context.__DOUBAO_DOLA_RESOLUTION_STATUS__)); },
    async request(url = skillPath, init) { return context.fetch(url, init); }
  };
}

function resolutionControl(meta) { return meta.option_list.find((item) => item.value === 'resolution'); }
function resolutions(meta) { return resolutionControl(meta).options.map((item) => item.value.toLowerCase()); }
function actionFixture() {
  return { code: 0, data: { config: JSON.stringify({
    key: 'video-parameters', unknown: 'preserve', controls: [
      { key: 'video-model', option_list: [{ id: 41, option_key: 'seedance_v2.5', display_text: 'Seedance 2.5' }] },
      { key: 'video-resolution', label: '分辨率', type: 3, default_selection_index: 0, default_selection_id: 11,
        option_list: [{ id: 11, option_key: '720p', display_text: '720P', message_text: '', marker: 'keep' }] },
      { key: 'video-duration', option_list: [{ id: 45, option_key: '10', display_text: '10s' }, { id: 99, option_key: '15', display_text: '15s' }] }
    ]
  }) } };
}

test('manifest installs ordered early MAIN hooks and the requested debugger permissions', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'manifest.json')));
  assert.equal(manifest.version, '1.6.60');
  const hook = manifest.content_scripts.find((entry) => entry.js.includes('resolution-main.js'));
  assert.deepEqual(hook.matches, ['*://*.doubao.com/*', '*://*.dola.com/*']);
  assert.equal(hook.run_at, 'document_start'); assert.equal(hook.world, 'MAIN');
  assert.ok(hook.js.indexOf('skill-pack-main.js') < hook.js.indexOf('resolution-main.js'));
  assert.ok(hook.js.indexOf('video-request-schema.js') >= 0);
  assert.ok(hook.js.indexOf('video-request-schema.js') < hook.js.indexOf('resolution-main.js'));
  assert.ok(hook.js.indexOf('dola-silent-expert-main.js') < hook.js.indexOf('protocol-send-main.js'));
  assert.deepEqual(manifest.permissions, ['tabs', 'scripting', 'downloads', 'debugger', 'storage']);
  const intent = manifest.content_scripts.find(entry => entry.js.includes('doubao-generation-intent-main.js'));
  assert.deepEqual(intent.matches, ['*://*.doubao.com/*']);
  assert.equal(intent.run_at, 'document_start'); assert.equal(intent.world, 'MAIN');
});

for (const site of ['doubao', 'dola']) {
  test(`${site}: real attachment fixture gains resolution options; unrelated settings survive`, async () => {
    const original = fixture(site);
    const env = environment({ site, body: original });
    const json = await (await env.request()).json();
    const meta = json.data.video_generation.meta;
    assert.deepEqual(resolutions(meta), ['720p', '1080p', '480p']);
    for (const [model, cap] of Object.entries(meta.model_capability)) assert.deepEqual(cap.supported_resolutions,
      model === 'seedance_v2.5' ? ['720p', '1080p', '480p'] : ['720p', '1080p']);
    const preserved = structuredClone(meta.option_list.filter((item) => item.value !== 'resolution'));
    for (const control of preserved) {
      if (control.value === 'duration') for (const option of control.options) {
        assert.equal(option.message_text, ''); delete option.message_text;
      }
    }
    assert.deepEqual(preserved, original.data.video_generation.meta.option_list);
    assert.deepEqual(Object.keys(meta.model_capability), Object.keys(original.data.video_generation.meta.model_capability));
    assert.deepEqual(json.data.deep_search, original.data.deep_search);
    assert.equal(env.status().skillPack, true);
  });

  test(`${site}: explicit 1080p preference becomes a native config default`, async () => {
    const env = environment({ site, pref: '1080p', compose: true });
    const meta = (await (await env.request()).json()).data.video_generation.meta;
    assert.deepEqual(resolutionControl(meta).options.filter((option) => option.is_default).map((option) => option.value), ['1080p']);
    assert.equal(env.status().preference, '1080p');
  });
}

for (const site of ['dola', 'doubao']) {
  test(`${site}: 480p becomes a real native config default and is observed in the signed request`, async () => {
    const env = environment({site, pref: '480p'});
    const meta = (await (await env.request()).json()).data.video_generation.meta;
    assert.deepEqual(resolutionControl(meta).options.filter(o => o.is_default).map(o => o.value), ['480p']);
    const body = JSON.stringify({chat_ability: {ability_type: 17, ability_param: JSON.stringify({model:'seedance_v2.5', resolution:'480p', duration:30})}});
    await env.request('/chat/completion?signed=keep', {method:'POST', body});
    assert.equal(env.calls.at(-1).init.body, body);
    assert.equal(env.status().lastRequest.resolution, '480p');
  });
}

test('follow-page mode keeps native defaults and other resolutions; mixed casing does not duplicate options', async () => {
  const original = fixture('doubao');
  original.data.video_generation.meta.option_list.push({ type: 3, value: 'resolution', options: [
    { value: '720P', show_name: '720P', is_default: false }, { value: '1080p', show_name: '1080P', is_default: true },
    { value: '2160p', show_name: '4K', is_default: false }
  ] });
  original.data.video_generation.meta.model_capability['seedance_v2.0'].supported_resolutions = ['720P', '1080p', '2160p'];
  const env = environment({ body: original });
  const meta = (await (await env.request()).json()).data.video_generation.meta;
  assert.deepEqual(resolutions(meta), ['720p', '1080p', '2160p', '480p']);
  assert.equal(resolutionControl(meta).options[1].is_default, true);
  assert.deepEqual(meta.model_capability['seedance_v2.0'].supported_resolutions, ['720P', '1080p', '2160p']);
});

test('nested action-bar JSON adds unique ids, correct selected index and preserves unknown fields', async () => {
  const env = environment({ body: actionFixture(), pref: '1080p' });
  const outer = await (await env.request(actionPath)).json();
  const conf = JSON.parse(outer.data.config);
  const control = conf.controls[1];
  assert.deepEqual(control.option_list.map((option) => option.option_key), ['720p', '1080p', '480p']);
  assert.equal(control.default_selection_index, 1); assert.equal(control.default_selection_id, 12);
  assert.equal(control.option_list[0].marker, 'keep'); assert.equal(conf.unknown, 'preserve');
  assert.deepEqual(conf.controls[2].option_list.map((option) => option.option_key), ['10', '15']);
  assert.equal(env.status().actionBar, true);
});

test('label-only video controls work, while image resolution and capability data are preserved', async () => {
  const body = { code: 0, data: { config: JSON.stringify({
    video: [{ key: 'video-model' }, { label: '分辨率', option_list: [{ id: 1, option_key: '720p', display_text: '720P' }] }],
    image_generation: { meta: { model_capability: { image_model: { supported_resolutions: ['1024'] } } },
      control: { key: 'image-resolution', label: '分辨率', option_list: [{ option_key: '1024' }] } }
  }) } };
  const env = environment({ body });
  const conf = JSON.parse((await (await env.request(actionPath)).json()).data.config);
  assert.equal(conf.video[1].option_list.length, 3);
  assert.deepEqual(conf.image_generation, JSON.parse(body.data.config).image_generation);
});

test('double JSON encoding remains string-encoded, and the patch is idempotent', async () => {
  const body = { data: { nested: JSON.stringify({ encoded: JSON.stringify(actionFixture()) }) } };
  const env = environment({ body });
  const first = await (await env.request(actionPath)).json();
  env.setBody(first);
  const second = await (await env.request(actionPath)).json();
  assert.deepEqual(second, first);
  const conf = JSON.parse(JSON.parse(JSON.parse(first.data.nested).encoded).data.config);
  assert.equal(conf.controls[1].option_list.length, 3);
});

test('API prefixes, trailing slash, URL and Request inputs are handled', async () => {
  const env = environment();
  for (const input of [
    new URL('https://www.doubao.com/api/samantha/skill/pack/?v=2'),
    new Request('https://www.doubao.com/api/v2/samantha/skill/pack?x=1')
  ]) {
    const json = await (await env.request(input)).json();
    assert.equal(resolutionControl(json.data.video_generation.meta).options.length, 3);
    assert.strictEqual(env.calls.at(-1).input, input);
  }
});

test('unrelated endpoints and lookalike domains pass through with response identity intact', async () => {
  for (const site of ['doubao', 'dola']) {
    const env = environment({ site, compose: true });
    for (const input of ['/other', `https://${site}.com.evil.example/samantha/skill/pack`, 'https://example.com/samantha/skill/pack']) {
      const response = await env.request(input);
      assert.strictEqual(response, env.calls.at(-1).response);
    }
    assert.equal(env.status().skillPack, false);
  }
});

test('HTTP errors, empty responses, invalid JSON and nonzero API codes pass through', async () => {
  for (const statusCode of [401, 403, 500, 204, 205]) {
    const env = environment({ statusCode });
    assert.strictEqual(await env.request(), env.calls[0].response);
  }
  for (const body of ['not-json', { ...fixture('doubao'), code: 123 }]) {
    const env = environment({ body });
    assert.strictEqual(await env.request(), env.calls[0].response);
  }
});

test('changed fetch responses and clones retain URL, type, redirected and useful headers', async () => {
  const env = environment({ site: 'dola', compose: true });
  const response = await env.request();
  for (const value of [response, response.clone(), response.clone().clone()]) {
    assert.equal(value.url, 'https://www.dola.com/samantha/skill/pack');
    assert.equal(value.type, 'cors'); assert.equal(value.redirected, true);
    assert.equal(value.status, 200); assert.equal(value.headers.get('x-preserved'), 'yes');
    for (const key of ['content-length', 'content-encoding', 'etag']) assert.equal(value.headers.get(key), null);
    assert.equal(resolutionControl((await value.json()).data.video_generation.meta).options.length, 3);
  }
});

function generationBody() {
  return JSON.stringify({ prompt: 'private prompt', chat_ability: { ability_type: 17,
    ability_param: JSON.stringify({ model: 'seedance_v2.5', duration: 30, resolution: '1080p' }) } });
}

test('domestic model defaults reuse real options and preserve upgrade metadata and server errors', async () => {
  const original = fixture('doubao');
  const env = environment({ body: original, loadModel: true, modelPreference: 'seedance_v2.0_std' });
  const output = (await (await env.request()).json()).data.video_generation.meta;
  const input = original.data.video_generation.meta.option_list.find(control => control.value === 'model');
  const control = output.option_list.find(control => control.value === 'model');
  assert.equal(control.options.length, input.options.length);
  assert.equal(control.options.find(option => option.is_default).value, 'seedance_v2.0_std');
  for (let i = 0; i < input.options.length; i++) {
    const expected = { ...input.options[i], is_default: input.options[i].value === 'seedance_v2.0_std' };
    assert.deepEqual(control.options[i], expected);
  }
  assert.equal(env.calls.length, 1);
  env.setBody({ code: 403, msg: 'upgrade required', data: original.data });
  const rejection = await env.request(); assert.strictEqual(rejection, env.calls.at(-1).response);
  assert.equal((await rejection.json()).code, 403);
});

test('a saved 2.5 selection does not fabricate a model missing from a real capability response', async () => {
  const original = fixture('doubao');
  const env = environment({ body: original, loadModel: true, modelPreference: 'seedance_v2.5' });
  const output = (await (await env.request()).json()).data.video_generation.meta;
  assert.deepEqual(output.option_list.find(control => control.value === 'model'), original.data.video_generation.meta.option_list.find(control => control.value === 'model'));
  assert.equal(Object.hasOwn(output.model_capability, 'seedance_v2.5'), false);
});

test('Dola restores the former 2.5 model in native action-bar JSON with unique IDs and its current default intact', async () => {
  const control = { key: 'video-model', type: 1, default_selection_index: 0, default_selection_id: 7,
    option_list: [{ id: 7, option_key: 'seedance_v2.0', display_text: 'Dreamina Seedance 2.0 Fast', subscription_tag: 'native-policy', marker: 'preserve' },
      { id: 19, option_key: 'ic_mini', display_text: 'Dreamina Seedance 1.0' }] };
  const body = { code: 0, data: { config: JSON.stringify({ controls: [control] }) } };
  const env = environment({ site: 'dola', body, loadModel: true });
  const result = JSON.parse((await (await env.request(actionPath)).json()).data.config).controls[0];
  assert.deepEqual(result.option_list.map(o => o.option_key), ['seedance_v2.5', 'seedance_v2.0', 'ic_mini']);
  assert.equal(result.option_list[0].id, 20); assert.equal(result.option_list[0].message_text, '');
  assert.equal(result.default_selection_index, 1); assert.equal(result.default_selection_id, 7);
  assert.deepEqual(result.option_list[1], control.option_list[0]);
  assert.equal(result.option_list[0].subscription_tag, undefined); assert.equal(result.option_list[0].is_available, undefined);
  assert.equal(env.calls.length, 1); assert.equal(body.data.config, JSON.stringify({ controls: [control] }));
});

test('Dola skill metadata uses the bundled client profile only when 2.5 is missing, keeping live profiles and native policies', async () => {
  const body = fixture('dola'); const meta = body.data.video_generation.meta;
  meta.option_list.find(c => c.value === 'model').options = meta.option_list.find(c => c.value === 'model').options.filter(o => o.value !== 'seedance_v2.5');
  meta.option_list.find(c => c.value === 'model').options[0].is_default = true;
  delete meta.model_capability['seedance_v2.5'];
  meta.model_capability['seedance_v2.0'].native_policy = 'keep';
  const env = environment({ site: 'dola', body, loadModel: true });
  const response = await env.request(); const result = await response.json(); const next = result.data.video_generation.meta;
  const models = next.option_list.find(c => c.value === 'model').options;
  assert.deepEqual(models.map(o => o.value), ['seedance_v2.5', 'seedance_v2.0', 'ic_mini']);
  assert.equal(models.find(o => o.is_default).value, 'seedance_v2.0');
  assert.equal(next.model_capability['seedance_v2.0'].native_policy, 'keep');
  assert.deepEqual(next.model_capability['seedance_v2.5'].supported_durations, ['5', '10', '15', '30']);
  const status = env.context.__DOUBAO_DOLA_MODEL_SELECTION__.status;
  assert.equal(status.capabilitySource, 'bundled-dola-v1.6.24'); assert.equal(status.backendVerification, 'UNVERIFIED');
  env.setBody(result);
  const second = (await (await env.request()).json()).data.video_generation.meta;
  assert.equal(second.option_list.find(c => c.value === 'model').options.filter(o => o.value === 'seedance_v2.5').length, 1);
  const live = fixture('dola'); live.data.video_generation.meta.model_capability['seedance_v2.5'].native_limit = 123;
  env.setBody(live);
  const liveOut = (await (await env.request()).json()).data.video_generation.meta;
  assert.equal(liveOut.model_capability['seedance_v2.5'].native_limit, 123);
});

test('Dola restores XHR configuration defaults without transforming backend errors or image-only controls', async () => {
  const model = { key: 'video-model', default_selection_index: 0, default_selection_id: 8,
    option_list: [{ id: 8, option_key: 'seedance_v2.0' }, { id: 9, option_key: 'ic_mini' }] };
  const body = { code: 0, data: { config: JSON.stringify({ controls: [model], image_generation: { controls: [{ key: 'model', options: [{ value: 'image-only' }] }] } }) } };
  const env = environment({ site: 'dola', body, loadModel: true, modelPreference: 'seedance_v2.5' });
  for (const type of ['', 'json']) {
    const xhr = new env.context.XMLHttpRequest(); xhr.open('POST', actionPath); xhr.responseType = type; xhr.send('{}'); xhr.respond(body);
    const out = type === 'json' ? xhr.response : JSON.parse(xhr.responseText); const conf = JSON.parse(out.data.config);
    assert.equal(conf.controls[0].default_selection_id, 10); assert.equal(conf.controls[0].default_selection_index, 0);
    assert.equal(conf.image_generation.controls[0].options.length, 1);
  }
  env.setBody({ code: 403, msg: 'model not available', data: body.data });
  const rejected = await env.request(actionPath); assert.strictEqual(rejected, env.calls.at(-1).response);
  assert.equal((await rejected.json()).code, 403);
});

test('Dola does not restore model choices into empty or unrelated model families', async () => {
  for (const options of [[], [{ id: 1, option_key: 'other_vendor_model' }]]) {
    const env = environment({ site: 'dola', loadModel: true });
    const control = { key: 'video-model', option_list: options };
    assert.equal(env.context.__DOUBAO_DOLA_MODEL_SELECTION__.patchControl(control, true), false);
    assert.equal(control.option_list.length, options.length);
    const meta = { model_capability: {} }; assert.equal(env.context.__DOUBAO_DOLA_MODEL_SELECTION__.patchMeta(meta), false);
    assert.deepEqual(meta.model_capability, {});
  }
});

test('nested native action-bar model defaults and JSON XHR use actual option IDs without clearing subscription fields', async () => {
  const conf = { controls: [{ key: 'video-model', default_selection_index: 1, default_selection_id: 98,
    option_list: [{ id: 17, option_key: 'seedance_v2.5', subscription_tag: 'upgrade', badge: 'keep' },
      { id: 98, option_key: 'seedance_v2.0_mini' }] }] };
  const body = { code: 0, data: { config: JSON.stringify(conf) } };
  const env = environment({ body, loadModel: true, modelPreference: 'seedance_v2.5' });
  const response = await env.request(actionPath);
  const control = JSON.parse((await response.json()).data.config).controls[0];
  assert.equal(control.default_selection_index, 0); assert.equal(control.default_selection_id, 17);
  assert.deepEqual(control.option_list, conf.controls[0].option_list);
  const xhr = new env.context.XMLHttpRequest(); xhr.open('POST', actionPath); xhr.responseType = 'json'; xhr.send('{}'); xhr.respond(body);
  const xhrControl = JSON.parse(xhr.response.data.config).controls[0];
  assert.equal(xhrControl.default_selection_id, 17); assert.equal(env.xhrCalls.length, 1);
  assert.equal(body.data.config, JSON.stringify(conf));
});

test('fetch(url, init) forwards signed generation requests byte-for-byte, once', async () => {
  const env = environment({ site: 'dola', compose: true, body: { code: 0 } });
  const body = generationBody();
  const init = { method: 'POST', body, headers: { 'x-signature': 'unchanged' }, credentials: 'include' };
  const url = '/chat/completion?a_bogus=original&msToken=original';
  const response = await env.context.fetch(url, init);
  assert.equal(env.calls.length, 1); assert.strictEqual(env.calls[0].init, init);
  assert.equal(env.calls[0].input, url); assert.equal(env.calls[0].init.body, body);
  assert.strictEqual(response, env.calls[0].response);
  assert.deepEqual(env.status().lastRequest, { resolution: '1080p', model: 'seedance_v2.5', duration: 30 });
  assert.ok(!JSON.stringify(env.status()).includes('private prompt'));
});

test('fetch(Request) observes a clone and forwards the same unconsumed request', async () => {
  let consumed;
  const env = environment({ fetchImplementation: async (input) => { consumed = await input.text(); return new Response('ok'); } });
  const request = new Request('https://www.doubao.com/chat/completion?a_bogus=original', { method: 'POST', body: generationBody() });
  await env.context.fetch(request);
  await new Promise((resolve) => setImmediate(resolve));
  assert.strictEqual(env.calls[0].input, request); assert.equal(consumed, generationBody());
  assert.equal(env.status().lastRequest?.resolution, '1080p'); assert.equal(env.calls.length, 1);
});

test('fetch(Request, {body}) observes the override, without changing Request or init', async () => {
  const env = environment({ body: 'ok' });
  const request = new Request('https://www.doubao.com/chat/completion', { method: 'POST', body: generationBody() });
  const init = { body: generationBody().replace('1080p', '720p') };
  await env.context.fetch(request, init);
  assert.strictEqual(env.calls[0].input, request); assert.strictEqual(env.calls[0].init, init);
  assert.equal(request.bodyUsed, false); assert.equal(env.status().lastRequest.resolution, '720p');
});

test('Blob and non-JSON generation bodies are forwarded without errors', async () => {
  const env = environment({ body: 'ok' });
  for (const body of [new Blob(['binary']), 'not-json', null]) {
    const init = { method: 'POST', body };
    await env.context.fetch('/chat/completion', init);
    assert.strictEqual(env.calls.at(-1).init, init);
  }
  assert.equal(env.status().lastRequest, null);
});

test('a prompt mentioning a resolution is not mistaken for a video parameter', async () => {
  const env = environment({ body: 'ok' });
  await env.context.fetch('/chat/completion', { body: JSON.stringify({
    prompt: JSON.stringify({ model: 'seedance_v2.5', resolution: '1080p' })
  }) });
  assert.equal(env.status().lastRequest.resolution, '');
});

for (const site of ['doubao', 'dola']) {
  test(`${site}: XHR text and JSON compose with the duration hook and remain stable`, () => {
    const env = environment({ site, compose: true });
    const xhr = new env.context.XMLHttpRequest();
    xhr.open('GET', skillPath); xhr.send(); xhr.respond(fixture(site));
    const first = xhr.responseText;
    assert.equal(first, xhr.responseText);
    assert.equal(resolutionControl(JSON.parse(first).data.video_generation.meta).options.length, 3);
    xhr.open('GET', skillPath); xhr.responseType = 'json'; xhr.send(); xhr.respond(fixture(site));
    const json = xhr.response;
    assert.strictEqual(json, xhr.response);
    assert.equal(resolutionControl(json.data.video_generation.meta).options.length, 3);
    assert.deepEqual(xhr._json.data.video_generation.meta.model_capability[Object.keys(xhr._json.data.video_generation.meta.model_capability)[0]].supported_resolutions, ['720p']);
    assert.throws(() => xhr.responseText, { name: 'InvalidStateError' });
  });

  test(`${site}: reusing a configuration XHR for another endpoint removes every stale getter`, () => {
    const env = environment({ site, compose: true });
    const xhr = new env.context.XMLHttpRequest();
    xhr.open('GET', skillPath); xhr.respond(fixture(site)); void xhr.responseText;
    xhr.open('GET', '/unrelated'); xhr.respond(fixture(site));
    assert.equal(xhr.responseText, JSON.stringify(fixture(site)));
    assert.equal(Object.hasOwn(xhr, 'responseText'), false);
  });
}

test('XHR action-bar JSON preserves encoding and makes a 1080p default', () => {
  const env = environment({ pref: '1080p', compose: true });
  const xhr = new env.context.XMLHttpRequest();
  xhr.open('GET', actionPath); xhr.responseType = 'json'; xhr.respond(actionFixture());
  const conf = JSON.parse(xhr.response.data.config);
  assert.equal(conf.controls[1].default_selection_index, 1);
  assert.equal(conf.controls[1].option_list[1].option_key, '1080p');
});

test('XHR generation forwarding is byte-for-byte and is never duplicated', () => {
  const env = environment({ site: 'dola', compose: true });
  const xhr = new env.context.XMLHttpRequest();
  xhr.open('POST', '/chat/completion?a_bogus=unchanged');
  const body = generationBody(); xhr.send(body);
  assert.equal(env.xhrCalls.length, 1); assert.equal(env.xhrCalls[0].body, body);
  assert.equal(env.xhrCalls[0].url, '/chat/completion?a_bogus=unchanged');
  assert.equal(env.status().lastRequest.resolution, '1080p');
});

test('XHR HTTP errors pass through', () => {
  for (const site of ['doubao', 'dola']) {
    const env = environment({ site, compose: true }); const xhr = new env.context.XMLHttpRequest();
    xhr.open('GET', skillPath); xhr.respond(fixture(site), 403);
    assert.equal(xhr.responseText, JSON.stringify(fixture(site)));
    xhr.open('GET', skillPath);
    const error = { ...fixture(site), code: 401 };
    xhr.respond(error, 200); assert.equal(xhr.responseText, JSON.stringify(error));
  }
});

test('Dola duration defaults use an actual index and id; Doubao durations are preserved', async () => {
  for (const site of ['dola', 'doubao']) {
    const env = environment({ site, compose: true, body: actionFixture() });
    const conf = JSON.parse((await (await env.request(actionPath)).json()).data.config);
    const duration = conf.controls[2];
    if (site === 'dola') {
      assert.equal(duration.default_selection_index, 2); assert.equal(duration.default_selection_id, 100);
      assert.deepEqual(duration.option_list.map((option) => option.option_key), ['10', '15', '30']);
    } else assert.deepEqual(duration.option_list.map((option) => option.option_key), ['10', '15']);
  }
});

test('installation is idempotent and preferences do not trigger generation or reload', () => {
  const env = environment(); const originalFetch = env.context.fetch;
  vm.runInContext(source, env.context);
  assert.strictEqual(env.context.fetch, originalFetch);
  env.element.setAttribute('data-watermark-free-resolution-preference', '1080p');
  env.context.document.dispatchEvent(new Event('watermark-free-resolution-preference-change'));
  assert.equal(env.storage.get(preferenceKey), '1080p'); assert.equal(env.calls.length, 0);
});

test('status survives document_start when the document element is not constructed yet', () => {
  const env = environment({ rootPresent: false });
  env.context.document.documentElement = env.element;
  env.context.document.dispatchEvent(new Event('DOMContentLoaded'));
  assert.equal(JSON.parse(env.element.getAttribute('data-watermark-free-resolution-status')).installed, true);
});

test('the screenshot scenario with ratio and duration only receives a new resolution control', async () => {
  const original = { code: 0, data: { config: JSON.stringify({ key: 'video-parameters', controls: [
    { key: 'video-ratio', label: '比例', type: 1, option_list: [{ id: 1, option_key: '16:9', display_text: '16:9' }] },
    { key: 'video-duration', label: '时长', type: 2, lower_bound: 4, upper_bound: 30, default_selected_duration: 10 }
  ] }) } };
  const env = environment({ body: original, pref: '1080p' });
  const conf = JSON.parse((await (await env.request(actionPath)).json()).data.config);
  assert.deepEqual(conf.controls.map(item => item.key), ['video-ratio', 'video-duration', 'video-resolution']);
  const resolution = conf.controls[2];
  assert.equal(resolution.type, 1); assert.equal(resolution.label, '分辨率');
  assert.deepEqual(resolution.option_list.map(item => item.option_key), ['720p', '1080p', '480p']);
  assert.equal(resolution.default_selection_index, 1); assert.equal(resolution.default_selection_id, 2);
  assert.deepEqual(conf.controls[1], JSON.parse(original.data.config).controls[1]);
  assert.equal(env.status().registeredControls, 1);
  env.setBody({ code: 0, data: { config: JSON.stringify(conf) } });
  const second = JSON.parse((await (await env.request(actionPath)).json()).data.config);
  assert.deepEqual(second, conf);
});

test('range-only video controls gain a selector; ordinary ratio option arrays stay untouched', async () => {
  const env = environment({ body: { data: { controls: [
    { key: 'video-duration', type: 2, lower_bound: 4, upper_bound: 30 }
  ], image: { option_list: [{ value: '16:9' }, { value: '4:3' }] } } } });
  const json = await (await env.request(actionPath)).json();
  assert.equal(json.data.controls[1].key, 'video-resolution');
  assert.equal(json.data.controls[1].type, 1);
  assert.deepEqual(json.data.image.option_list, [{ value: '16:9' }, { value: '4:3' }]);
});

for (const site of ['doubao', 'dola']) {
  test(`${site}: duration labels and values survive while prompt-append metadata is cleared in native configs`, async () => {
    const body = fixture(site);
    const duration = body.data.video_generation.meta.option_list.find(item => item.value === 'duration');
    duration.message_text = '时长：{duration}s';
    for (const option of duration.options) option.message_text = option.show_name;
    body.data.image_generation = { option_list: [{ value: 'duration', message_text: 'keep image metadata',
      options: [{ value: '10', show_name: '10s', message_text: 'keep option' }] }] };
    const env = environment({ site, body, compose: true });
    const result = await (await env.request()).json();
    const cleaned = result.data.video_generation.meta.option_list.find(item => item.value === 'duration');
    assert.equal(cleaned.message_text, '');
    for (const option of cleaned.options) assert.equal(option.message_text, '');
    for (const original of duration.options) {
      const option = cleaned.options.find(item => item.value === original.value);
      assert.equal(option.show_name, original.show_name); assert.equal(option.value, original.value);
    }
    assert.deepEqual(result.data.image_generation, body.data.image_generation);
    const conf = { controls: [
      { key: 'video-duration', type: 2, lower_bound: 4, upper_bound: 30, default_selected_duration: 15, message_text: '30s' },
      { key: 'video-model', option_list: [{ id: 1, option_key: 'seedance_v2.5' }] },
      { key: 'audio-duration', message_text: 'audio stays', option_list: [{ option_key: '10', message_text: '10s' }] }
    ] };
    env.setBody({ code: 0, data: { config: JSON.stringify(conf) } });
    const action = JSON.parse((await (await env.request(actionPath)).json()).data.config);
    assert.equal(action.controls[0].message_text, ''); assert.equal(action.controls[0].upper_bound, 30);
    assert.deepEqual(action.controls.find(item => item.key === 'audio-duration'), conf.controls[2]);
  });
}

test('registering a resolution control cannot retain the ratio template submit key', async () => {
  const ratio = { key: 'video-ratio', label: '比例', type: 1, submit_key: 'ratio', submitKey: 'ratio', param_key: 'ratio',
    option_list: [{ id: 1, option_key: '16:9', display_text: '16:9', message_text: '16:9' }] };
  const body = { code: 0, data: { config: JSON.stringify({ controls: [ratio,
    { key: 'video-duration', type: 2, upper_bound: 30 }] }) } };
  const env = environment({ site: 'dola', body, pref: '1080p' });
  const conf = JSON.parse((await (await env.request(actionPath)).json()).data.config);
  assert.deepEqual(conf.controls[0], ratio);
  const resolution = conf.controls.find(control => control.key === 'video-resolution');
  assert.equal(resolution.submit_key, 'resolution'); assert.equal(resolution.submitKey, 'resolution');
  assert.equal(resolution.param_key, 'resolution');
  assert.equal(resolution.option_list[resolution.default_selection_index].option_key, '1080p');
});
