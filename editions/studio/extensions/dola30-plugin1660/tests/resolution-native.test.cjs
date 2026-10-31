const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '..');
const nativeSource = fs.readFileSync(path.join(root, 'resolution-native-main.js'), 'utf8');
const transportSource = fs.readFileSync(path.join(root, 'resolution-main.js'), 'utf8');
const durationSource = fs.readFileSync(path.join(root, 'duration-main.js'), 'utf8');
const timelineSource = fs.readFileSync(path.join(root, 'timeline-progress-main.js'), 'utf8');
const modelSource = fs.readFileSync(path.join(root, 'model-selection-main.js'), 'utf8');
const intentSource = fs.readFileSync(path.join(root, 'doubao-generation-intent-main.js'), 'utf8');
const expertSource = fs.readFileSync(path.join(root, 'dola-silent-expert-main.js'), 'utf8');
const modelKey = 'intl_doubao_target_video_model_v1';
const prefKey = 'intl_doubao_target_resolution_v1';
const menuMark = 'data-watermark-free-resolution-ui';

// Small DOM test double for menu placement, disabled state and button events.
// It does not claim to render pixels or reproduce a live site's React tree.
class Element extends EventTarget {
  constructor(tag) { super(); this.tagName = tag.toUpperCase(); this.attrs = new Map(); this.nodes = []; this.parentElement = null; this.style = {}; this._text = ''; this.disabled = false; this.rect = { width: 40, height: 20 }; }
  setAttribute(key, value) { this.attrs.set(key, String(value)); }
  getAttribute(key) { return this.attrs.get(key) ?? null; }
  removeAttribute(key) { this.attrs.delete(key); }
  hasAttribute(key) { return this.attrs.has(key); }
  get children() { return this.nodes; }
  get isConnected() { return this.root || Boolean(this.parentElement?.isConnected); }
  get textContent() { return this._text + this.nodes.map(node => node.textContent).join(''); }
  set textContent(text) { for (const node of this.nodes) node.parentElement = null; this.nodes = []; this._text = String(text); }
  appendChild(node) { node.remove(); node.parentElement = this; this.nodes.push(node); return node; }
  remove() { if (this.parentElement) this.parentElement.nodes = this.parentElement.nodes.filter(node => node !== this); this.parentElement = null; }
  contains(node) { for (let value = node; value; value = value.parentElement) if (value === this) return true; return false; }
  getBoundingClientRect() { return this.rect; }
  matches(selector) {
    return selector.split(',').some(part => {
      part = part.trim();
      const tag = part.match(/^[a-z]+/i)?.[0];
      if (tag && tag.toUpperCase() !== this.tagName) return false;
      for (const match of part.matchAll(/\[([^\]*=]+)(?:(\*?=)"?([^"\]]*)"?)?\]/g)) {
        const actual = this.getAttribute(match[1]);
        if (actual === null) return false;
        if (match[2] === '=' && actual !== match[3]) return false;
        if (match[2] === '*=' && !actual.includes(match[3])) return false;
      }
      return Boolean(tag || part.includes('['));
    });
  }
  querySelectorAll(selector) {
    const result = [];
    const visit = node => { for (const child of node.nodes) { if (child.matches(selector)) result.push(child); visit(child); } };
    visit(this); return result;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; }
  click() { if (!this.disabled) this.dispatchEvent(new Event('click')); }
}

function environment({ site = 'doubao', preference = 'auto', missingService = false,
                       existingNativeControl = false, readonlyService = false, loadDuration = false,
                       nativeDola = false, nativeDoubao = false, nativeResetResolution = false,
                       latePipeline = false, frozenPipeline = false,
                       renamedModules = false, legacyServiceEnabled = false,
                       loadTimeline = false, loadModel = false, modelPreference = 'auto',
                       modelMenu = false, nativeResetModel = false, loadIntent = false,
                       loadExpert = false, backendStatus = 200, backendBody = { code: 0 } } = {}) {
  const document = new EventTarget();
  const html = new Element('html'); html.root = true; html.rect = { width: 1400, height: 800 };
  document.documentElement = html; document.readyState = 'complete';
  document.createElement = tag => new Element(tag);
  document.querySelectorAll = selector => html.querySelectorAll(selector);
  document.querySelector = selector => html.querySelector(selector);
  const popup = new Element('div'); popup.setAttribute('class', 'settings-popover'); popup.setAttribute('role', 'dialog'); popup.rect = { width: 360, height: 240 };
  const originalContent = new Element('div'); originalContent.textContent = '比例 自动 3:4 4:3 9:16 16:9 1:1 21:9 时长 10s 4s 30s';
  popup.appendChild(originalContent);
  if (existingNativeControl) {
    const resolution = new Element('div'); resolution.textContent = '分辨率 720P 1080P'; popup.appendChild(resolution);
  }
  html.appendChild(popup);
  const modelPopup = new Element('div'); modelPopup.setAttribute('class', 'model-popover'); modelPopup.setAttribute('role', 'menu'); modelPopup.rect = { width: 130, height: 230 };
  const modelRows = new Map();
  if (modelMenu) {
    const title = new Element('div'); title.textContent = '模型'; modelPopup.appendChild(title);
    const rows = site === 'dola' ? [['seedance_v2.5', 'Dreamina Seedance 2.5'],
      ['seedance_v2.0', 'Dreamina Seedance 2.0 Fast 快速出片选择'], ['ic_mini', 'Dreamina Seedance 1.0 基础视频']] :
      [['seedance_v2.5', 'Seedance 2.5 升级 旗舰视频标杆 · 5倍消耗'], ['seedance_v2.0_std', 'Seedance 2.0 升级 进阶画面表现 · 2倍消耗'], ['seedance_v2.0', 'Seedance 2.0 Fast 快速出片选择'], ['seedance_v2.0_mini', 'Seedance 2.0 Mini 日常生成使用']];
    for (const [value, label] of rows) {
      const row = new Element('div'); row.setAttribute('role', 'menuitem'); row.rect = { width: 120, height: 40 };
      const text = new Element('span'); text.textContent = label; row.appendChild(text);
      modelPopup.appendChild(row); modelRows.set(value, row);
    }
    html.appendChild(modelPopup);
  }
  const storage = new Map(preference === 'auto' ? [] : [[prefKey, preference]]);
  if (modelPreference !== 'auto') storage.set(modelKey, modelPreference);
  const nativeCalls = []; const networkCalls = []; const intervals = [];
  const returnValue = { nativeResult: true };
  let available = !missingService;
  let clock = 1700000000000;
  let context;
  const service = {
    sendMessage(request, ...args) {
      nativeCalls.push({ request, args, receiver: this });
      const body = JSON.stringify(request);
      const signature = crypto.createHash('sha256').update(body).digest('hex');
      void context.fetch(`https://www.${site}.com/chat/completion?a_bogus=${signature}`, { method: 'POST', body, headers: { 'x-signature': signature } });
      return returnValue;
    }
  };
  const originalSend = service.sendMessage;
  let activeService = service;
  let lastAccessorResult;
  const im = { getMessageService(...args) {
    lastAccessorResult = Promise.resolve(activeService); return lastAccessorResult;
  } };
  const legacyCalls = [];
  const legacyService = { sendMessage(request, ...args) {
    legacyCalls.push({ request, args, receiver: this });
    const body = JSON.stringify(request); const signature = crypto.createHash('sha256').update(body).digest('hex');
    void context.fetch(`https://www.${site}.com/chat/completion?a_bogus=${signature}`, { method: 'POST', body, headers: { 'x-signature': signature } });
    return returnValue;
  } };
  const actionValue = {}; const inputWrites = [];
  const nativeInput = {
    gl: key => actionValue[key], N5: key => actionValue.__input_action_control_values__?.[key],
    qM(key, value) {
      inputWrites.push({ key, value }); actionValue[key] = value.value;
      actionValue.__input_action_control_values__ = { ...actionValue.__input_action_control_values__, [key]: value };
    }
  };
  const pipelineCalls = [];
  const permission = { id: 'native-authorization-guard', execute: context => Promise.resolve(context) };
  const nativeNode = { id: 'video-creation:input-skill-node', async execute(request, ...args) {
    pipelineCalls.push({ request, args, receiver: this });
    const content = request.content;
    if (content.abilityType != null && Number(content.abilityType) !== 17) return request;
    const value = content.actionBarControlParams?.['video-resolution'] ||
      content.actionBarControlValues?.['video-resolution']?.value || nativeInput.gl('video-resolution') || '720p';
    content.abilityParams = { model: nativeResetModel ? (site === 'dola' ? 'seedance_v2.0' : 'seedance_v2.0_mini') : content.actionBarControlParams?.['video-model'] || nativeInput.gl('video-model') || 'seedance_v2.5', duration: 30, ratio: '16:9', resolution: nativeResetResolution ? '720p' : value, native_extra: 'preserve' };
    const time = content.actionBarControlValues?.['video-duration']?.messageText;
    if (time) content.text += '，' + time;
    return request;
  } };
  if (frozenPipeline) Object.freeze(nativeNode);
  const afterSubmit = { id: 'video-generation:clear-action-values', execute: context => Promise.resolve(context) };
  const pipeline = { submitNodes: [permission, nativeNode], afterSubmitNodes: [afterSubmit] };
  const inputId = renamedModules ? 'next-input-runtime' : nativeDoubao ? 'rM' : 'Kyj';
  const pipelineId = renamedModules ? 'next-video-pipeline' : 'sAh';
  const serviceId = nativeDoubao ? 'xvn' : 'nR1';
  const hasPipeline = nativeDola || nativeDoubao;
  if (readonlyService) Object.defineProperty(service, 'sendMessage', { writable: false });
  const requireFixture = id => {
    if (hasPipeline && id === inputId) return nativeDoubao
      ? { n: nativeInput.qM, h: nativeInput.gl, f: nativeInput.N5 } : nativeInput;
    if (hasPipeline && id === pipelineId) return { p: pipeline };
    if (id !== serviceId) throw Error('Unknown fixture module');
    return { [nativeDoubao ? 'a' : 'K']: async name => available && name === 'chatIMService' ? im :
      available && legacyServiceEnabled && name === 'chatMessageService' ? legacyService : null };
  };
  requireFixture.m = { [serviceId]: function serviceContainer() { return 'resourceStore'; } };
  if (hasPipeline) {
    requireFixture.m[inputId] = function inputRuntime() { return '__input_action_control_values__ chatInputStore'; };
    if (!latePipeline) requireFixture.m[pipelineId] = function videoRuntime() { return 'video-creation:input-skill-node'; };
  }
  context = {
    document, Event, URL, Request, Response, Headers, Promise, console,
    Date: class extends Date { static now() { return clock; } },
    location: { hostname: `www.${site}.com`, href: `https://www.${site}.com/chat/` },
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, String(value)) },
    getComputedStyle: node => ({ display: node.style.display || 'block', visibility: node.style.visibility || 'visible' }),
    setInterval: callback => { intervals.push(callback); return intervals.length; }, setTimeout: callback => { callback(); return 0; },
    __DOLA_TOOLKIT_WEBPACK_REQUIRE__: requireFixture,
    fetch(input, init) {
      const response = new Response(JSON.stringify(backendBody), { status: backendStatus });
      networkCalls.push({ input, init, response }); return Promise.resolve(response);
    }
  };
  context.window = context; vm.createContext(context);
  if (loadModel) vm.runInContext(modelSource, context);
  if (loadIntent) {
    vm.runInContext(fs.readFileSync(path.join(root, 'video-request-schema.js'), 'utf8'), context);
    vm.runInContext(intentSource, context);
  }
  if (loadExpert) vm.runInContext(expertSource, context);
  vm.runInContext(transportSource, context);
  if (loadTimeline) vm.runInContext(timelineSource, context);
  vm.runInContext(nativeSource, context);
  if (loadDuration) vm.runInContext(durationSource, context);
  return {
    context, document, popup, originalContent, storage, modelPopup, modelRows, get service() { return activeService; },
    nativeCalls, networkCalls, returnValue, im, legacyService, legacyCalls, nativeInput,
    actionValue, inputWrites, pipeline, nativeNode, permission, afterSubmit, pipelineCalls,
    accessorPromise() { return lastAccessorResult; },
    replaceService() { activeService = { sendMessage: originalSend }; return activeService; },
    resetMethod() { activeService.sendMessage = originalSend; },
    async mountPipeline() {
      requireFixture.m[pipelineId] = function videoRuntime() { return 'video-creation:input-skill-node'; };
      await context.__DOUBAO_DOLA_RESOLUTION_BRIDGE__.retry();
    },
    async settle() { await new Promise(resolve => setImmediate(resolve)); },
    async tick() { for (const callback of intervals) callback(); await this.settle(); },
    advanceTime(ms) { clock += ms; },
    async makeAvailable() { available = true; await context.__DOUBAO_DOLA_RESOLUTION_BRIDGE__.retry(); },
    row() { return popup.querySelector(`[${menuMark}]`); },
    button(value) { return this.row().querySelector(`[data-resolution="${value}"]`); },
    status() { return JSON.parse(JSON.stringify(context.__DOUBAO_DOLA_RESOLUTION_BRIDGE__.status)); }
  };
}

function draft({ type = 17, encoded = true, resolution = '720p' } = {}) {
  const params = { model: 'seedance_v2.5', duration: 10, ratio: '16:9', resolution, private_extra: 'retain' };
  const ability = { ability_type: type, ability_param: encoded ? JSON.stringify(params) : params };
  return { messages: [{ ext: { chat_ability: encoded ? JSON.stringify(ability) : ability },
    content_blocks_v2: [{ block_type: 1, text: 'unchanged fixture content' }] }], session_flag: 'preserve' };
}
function params(request) {
  let ability = request.messages[0].ext.chat_ability;
  if (typeof ability === 'string') ability = JSON.parse(ability);
  return typeof ability.ability_param === 'string' ? JSON.parse(ability.ability_param) : ability.ability_param;
}

function gesture(env, node, type = 'click', key) {
  const event = new Event(type, { cancelable: true });
  Object.defineProperty(event, 'target', { value: node });
  if (key) Object.defineProperty(event, 'key', { value: key });
  env.document.dispatchEvent(event);
  return event;
}

for (const value of ['seedance_v2.5', 'seedance_v2.0_std']) {
  test(`Doubao ${value}: native menu gesture blocks upgrade, persists real state and signs one native request`, async () => {
    const env = environment({ nativeDoubao: true, loadModel: true, modelMenu: true, preference: '1080p' }); await env.settle();
    let upgrade = 0; env.document.addEventListener('click', () => upgrade++);
    const row = env.modelRows.get(value);
    for (const type of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'click']) {
      assert.equal(gesture(env, row.children[0], type).defaultPrevented, true);
    }
    assert.equal(upgrade, 0); assert.equal(env.storage.get(modelKey), value);
    assert.equal(env.nativeInput.gl('video-model'), value);
    assert.equal(env.nativeInput.N5('video-model').submitKey, 'model');
    assert.equal(env.nativeInput.N5('video-model').messageText, '');
    assert.equal(row.getAttribute('aria-selected'), 'true');
    assert.equal(env.modelRows.get('seedance_v2.0_mini').getAttribute('aria-selected'), 'false');
    assert.equal(env.networkCalls.length, 0);
    const input = draft(); const snapshot = JSON.stringify(input);
    assert.strictEqual(env.service.sendMessage(input, 'argument'), env.returnValue); await env.settle();
    assert.equal(JSON.stringify(input), snapshot);
    assert.equal(params(env.nativeCalls[0].request).model, value);
    assert.equal(params(env.nativeCalls[0].request).resolution, '1080p');
    assert.equal(params(env.nativeCalls[0].request).duration, 10);
    assert.strictEqual(env.nativeCalls[0].request.messages[0].content_blocks_v2, input.messages[0].content_blocks_v2);
    assert.equal(env.nativeCalls.length, 1); assert.equal(env.networkCalls.length, 1);
    const sent = env.networkCalls[0];
    assert.equal(sent.init.headers['x-signature'], crypto.createHash('sha256').update(sent.init.body).digest('hex'));
    assert.equal(env.context.__DOUBAO_DOLA_RESOLUTION_STATUS__.lastRequest.model, value);
  });
}

test('Doubao native pipeline corrects a rebuilt model and resolution before the native signer, keeping authorization nodes', async () => {
  const env = environment({ nativeDoubao: true, loadModel: true, modelPreference: 'seedance_v2.5',
    preference: '1080p', nativeResetResolution: true, nativeResetModel: true }); await env.settle();
  const input = engineContext(); const snapshot = JSON.stringify(input);
  const output = await env.nativeNode.execute(input, 'extra');
  assert.equal(JSON.stringify(input), snapshot);
  assert.equal(output.content.abilityParams.model, 'seedance_v2.5');
  assert.equal(output.content.abilityParams.resolution, '1080p');
  assert.equal(output.content.abilityParams.native_extra, 'preserve');
  assert.strictEqual(env.pipeline.submitNodes[0], env.permission);
  assert.strictEqual(env.pipeline.afterSubmitNodes[0], env.afterSubmit);
  const status = env.context.__DOUBAO_DOLA_MODEL_SELECTION__.status;
  assert.equal(status.lastPipeline.nativeModel, 'seedance_v2.0_mini'); assert.equal(status.lastPipeline.corrected, true);
  assert.equal(env.networkCalls.length, 0);
});

test('model selection survives native state resets, menu remount and service replacement, and can return to follow-page mode', async () => {
  const env = environment({ nativeDoubao: true, loadModel: true, modelMenu: true, modelPreference: 'seedance_v2.5' }); await env.settle();
  env.nativeInput.qM('video-model', { value: 'seedance_v2.0_mini', submitKey: 'model' });
  env.modelPopup.remove(); env.document.documentElement.appendChild(env.modelPopup);
  await env.tick();
  assert.equal(env.nativeInput.gl('video-model'), 'seedance_v2.5');
  assert.equal(env.modelRows.get('seedance_v2.5').getAttribute('aria-selected'), 'true');
  env.replaceService(); await env.im.getMessageService(); await env.settle();
  gesture(env, env.modelRows.get('seedance_v2.0'), 'keydown', 'Enter');
  env.service.sendMessage(draft()); assert.equal(params(env.nativeCalls[0].request).model, 'seedance_v2.0');
  const root = env.document.documentElement;
  root.setAttribute('data-watermark-free-model-preference', 'auto');
  env.document.dispatchEvent(new Event('watermark-free-model-preference-change'));
  env.nativeInput.qM('video-model', { value: 'seedance_v2.0_mini', submitKey: 'model' }); await env.tick();
  assert.equal(env.storage.get(modelKey), 'auto'); assert.equal(env.nativeInput.gl('video-model'), 'seedance_v2.0_mini');
  const next = draft(); const before = JSON.stringify(next); env.service.sendMessage(next);
  assert.equal(JSON.stringify(env.nativeCalls[1].request), before);
});

test('a missing native model store leaves the upgrade handler and unknown menu interactions intact', async () => {
  const env = environment({ loadModel: true, modelMenu: true }); await env.settle();
  let upgrade = 0; env.document.addEventListener('click', () => upgrade++);
  assert.equal(gesture(env, env.modelRows.get('seedance_v2.5')).defaultPrevented, false);
  assert.equal(upgrade, 1); assert.equal(env.storage.get(modelKey), undefined);
  assert.equal(env.context.__DOUBAO_DOLA_MODEL_SELECTION__.status.inputReady, false);
  assert.equal(env.networkCalls.length, 0);
});

test('a failed native model write does not pretend to select the row or suppress the native handler', async () => {
  const env = environment({ nativeDoubao: true, loadModel: true, modelMenu: true }); await env.settle();
  env.context.__DOUBAO_DOLA_MODEL_SELECTION__.bindInput({ getValue: () => 'seedance_v2.0_mini', getRecord: () => ({ value: 'seedance_v2.0_mini', submitKey: 'model' }), setRecord() { throw Error('readonly'); } });
  let upgrade = 0; env.document.addEventListener('click', () => upgrade++);
  const event = gesture(env, env.modelRows.get('seedance_v2.5'));
  assert.equal(event.defaultPrevented, false); assert.equal(upgrade, 1);
  assert.equal(env.context.__DOUBAO_DOLA_MODEL_SELECTION__.status.inputReady, false);
  assert.notEqual(env.modelRows.get('seedance_v2.5').getAttribute('aria-selected'), 'true');
  assert.equal(env.storage.get(modelKey), 'auto');
  const unchanged = draft({ encoded: false }); unchanged.messages[0].ext.chat_ability.ability_param.model = 'seedance_v2.0_mini';
  assert.strictEqual(env.context.__DOUBAO_DOLA_MODEL_SELECTION__.prepare(unchanged), unchanged);
  assert.equal(env.networkCalls.length, 0);
});

test('the domestic model adapter installs early, once, and ignores unrelated clicks and modifier keys', async () => {
  const env = environment({ nativeDoubao: true, loadModel: true, modelMenu: true }); await env.settle();
  const adapter = env.context.__DOUBAO_DOLA_MODEL_SELECTION__;
  vm.runInContext(modelSource, env.context); assert.strictEqual(env.context.__DOUBAO_DOLA_MODEL_SELECTION__, adapter);
  const unknown = new Element('button'); unknown.textContent = 'Seedance 9.0'; env.modelPopup.appendChild(unknown);
  assert.equal(gesture(env, unknown).defaultPrevented, false);
  assert.equal(gesture(env, env.modelRows.get('seedance_v2.5'), 'keydown', 'Escape').defaultPrevented, false);
  assert.equal(env.storage.get(modelKey), undefined); assert.equal(env.networkCalls.length, 0);
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'manifest.json')));
  const script = manifest.content_scripts.find(entry => entry.js.includes('model-selection-main.js'));
  assert.deepEqual(script.matches, ['*://*.doubao.com/*', '*://*.dola.com/*']); assert.equal(script.world, 'MAIN'); assert.equal(script.run_at, 'document_start');
  assert.ok(script.js.indexOf('model-selection-main.js') < script.js.indexOf('resolution-main.js'));
});

test('current video model patch preserves history, assistant replies, prompts, attachments and object encodings', async () => {
  const env = environment({ nativeDoubao: true, loadModel: true, modelPreference: 'seedance_v2.0_std' }); await env.settle();
  const history = draft().messages[0]; const assistant = { role: 'assistant', text: 'unchanged' };
  const current = draft({ encoded: false }).messages[0]; current.attachments = [{ id: 'keep' }];
  const input = { messages: [history, assistant, current], extra: 'retain' }; const before = JSON.stringify(input);
  env.service.sendMessage(input); const output = env.nativeCalls[0].request;
  assert.strictEqual(output.messages[0], history); assert.strictEqual(output.messages[1], assistant);
  assert.strictEqual(output.messages[2].attachments, current.attachments);
  assert.strictEqual(output.messages[2].content_blocks_v2, current.content_blocks_v2);
  assert.equal(output.messages[2].ext.chat_ability.ability_param.model, 'seedance_v2.0_std');
  assert.equal(JSON.stringify(input), before);
  for (const bad of [{ messages: [{ ext: { chat_ability: 'malformed' } }] }, draft({ type: 3 }), { messages: [history, { role: 'user', text: 'ordinary chat' }] }]) {
    assert.strictEqual(env.context.__DOUBAO_DOLA_MODEL_SELECTION__.prepare(bad), bad);
  }
});

test('a model preference never rewrites or retries an already signed fetch generation request', async () => {
  const env = environment({ nativeDoubao: true, loadModel: true, modelPreference: 'seedance_v2.5', preference: '1080p' }); await env.settle();
  const request = draft(); const ability = JSON.parse(request.messages[0].ext.chat_ability);
  const options = JSON.parse(ability.ability_param); options.model = 'seedance_v2.0_mini';
  ability.ability_param = JSON.stringify(options); request.messages[0].ext.chat_ability = JSON.stringify(ability);
  const init = { method: 'POST', body: JSON.stringify(request), headers: { 'x-signature': 'keep' } };
  const url = 'https://www.doubao.com/chat/completion?a_bogus=unchanged';
  await env.context.fetch(url, init);
  assert.equal(env.networkCalls.length, 1); assert.strictEqual(env.networkCalls[0].init, init);
  assert.equal(env.networkCalls[0].input, url);
  assert.equal(env.context.__DOUBAO_DOLA_RESOLUTION_STATUS__.lastRequest.model, 'seedance_v2.0_mini');
});

test('Dola installs the shared adapter but ignores domestic-only model preferences', async () => {
  const env = environment({ site: 'dola', loadModel: true, nativeDola: true, modelPreference: 'seedance_v2.0_std' }); await env.settle();
  assert.equal(env.context.__DOUBAO_DOLA_MODEL_SELECTION__.status.selected, 'auto');
  env.service.sendMessage(draft()); assert.equal(params(env.nativeCalls[0].request).model, 'seedance_v2.5');
});

test('Dola Dreamina model choices update native records, sign the chosen 2.5 once and can return to 1.0', async () => {
  const env = environment({ site: 'dola', nativeDola: true, loadModel: true, modelMenu: true, preference: '1080p' });
  await env.settle();
  const api = env.context.__DOUBAO_DOLA_MODEL_SELECTION__;
  const control = { key: 'video-model', option_list: [{ id: 3, option_key: 'seedance_v2.0' }, { id: 4, option_key: 'ic_mini' }] };
  assert.equal(api.patchControl(control, true), true);
  assert.equal(control.option_list[0].option_key, 'seedance_v2.5');
  let callbacks = 0; env.document.addEventListener('click', () => callbacks++);
  assert.equal(gesture(env, env.modelRows.get('seedance_v2.5')).defaultPrevented, false);
  assert.equal(callbacks, 1); assert.equal(env.nativeInput.gl('video-model'), 'seedance_v2.5');
  assert.equal(env.nativeInput.N5('video-model').submitKey, 'model');
  assert.equal(env.modelRows.get('seedance_v2.5').getAttribute('aria-selected'), 'true');
  assert.equal(env.networkCalls.length, 0);
  const input = draft({ encoded: false }); input.messages[0].ext.chat_ability.ability_param.model = 'seedance_v2.0';
  const before = JSON.stringify(input); env.service.sendMessage(input, 'native-extra'); await env.settle();
  assert.equal(JSON.stringify(input), before); assert.equal(params(env.nativeCalls[0].request).model, 'seedance_v2.5');
  assert.equal(params(env.nativeCalls[0].request).resolution, '1080p');
  assert.strictEqual(env.nativeCalls[0].request.messages[0].content_blocks_v2, input.messages[0].content_blocks_v2);
  assert.equal(env.nativeCalls.length, 1); assert.equal(env.networkCalls.length, 1);
  const sent = env.networkCalls[0];
  assert.equal(sent.init.headers['x-signature'], crypto.createHash('sha256').update(sent.init.body).digest('hex'));
  gesture(env, env.modelRows.get('seedance_v2.0')); assert.equal(env.nativeInput.gl('video-model'), 'seedance_v2.0');
  gesture(env, env.modelRows.get('ic_mini')); assert.equal(env.nativeInput.gl('video-model'), 'ic_mini');
  env.service.sendMessage(draft()); await env.settle();
  assert.equal(params(env.nativeCalls[1].request).model, 'ic_mini');
  assert.equal(env.nativeCalls.length, 2); assert.equal(env.networkCalls.length, 2);
  assert.equal(api.status.backendVerification, 'UNVERIFIED');
});

test('Dola native pipeline corrects stale model and resolution without skipping permission or completion nodes', async () => {
  const env = environment({ site: 'dola', nativeDola: true, loadModel: true, modelPreference: 'seedance_v2.5',
    preference: '1080p', nativeResetModel: true, nativeResetResolution: true }); await env.settle();
  const input = engineContext(); const before = JSON.stringify(input);
  const result = await env.nativeNode.execute(input, 'pipeline-extra');
  assert.equal(JSON.stringify(input), before);
  assert.equal(result.content.abilityParams.model, 'seedance_v2.5'); assert.equal(result.content.abilityParams.resolution, '1080p');
  assert.strictEqual(env.pipeline.submitNodes[0], env.permission); assert.strictEqual(env.pipeline.afterSubmitNodes[0], env.afterSubmit);
  assert.equal(env.context.__DOUBAO_DOLA_MODEL_SELECTION__.status.lastPipeline.nativeModel, 'seedance_v2.0');
  const request = draft({ encoded: false }); request.messages[0].ext.chat_ability.ability_param = result.content.abilityParams;
  env.service.sendMessage(request); await env.settle();
  assert.equal(env.networkCalls.length, 1); const network = env.networkCalls[0];
  assert.equal(params(JSON.parse(network.init.body)).model, 'seedance_v2.5');
  assert.equal(new URL(network.input).searchParams.get('a_bogus'), crypto.createHash('sha256').update(network.init.body).digest('hex'));
});

for (const encodedVariables of [false, true]) {
  test(`Dola legacy model and resolution enter ${encodedVariables ? 'encoded' : 'object'} variables before one signed submission`, async () => {
    const env = environment({ site: 'dola', nativeDola: true, loadModel: true, legacyServiceEnabled: true,
      modelPreference: 'seedance_v2.5', preference: '1080p' }); await env.settle();
    const variables = { model: 'seedance_v2.0', duration: 30, resolution: '720p', ratio: '16:9', custom: 'keep' };
    const skill = { skill_type: 17, variables: encodedVariables ? JSON.stringify(variables) : variables };
    const attachment = { kind: 'image', id: 'private-reference' };
    const input = [{ role: 'user', text: '私人原稿保持不变', attachments: [attachment], extraExt: { input_skill: JSON.stringify(skill) } }];
    const before = JSON.stringify(input);
    assert.strictEqual(env.legacyService.sendMessage(input, 'legacy-extra'), env.returnValue); await env.settle();
    const output = env.legacyCalls[0].request; const raw = JSON.parse(output[0].extraExt.input_skill).variables;
    const params = encodedVariables ? JSON.parse(raw) : raw;
    assert.equal(typeof raw, encodedVariables ? 'string' : 'object');
    assert.deepEqual(params, { ...variables, model: 'seedance_v2.5', resolution: '1080p' });
    assert.equal(JSON.stringify(input), before); assert.strictEqual(output[0].attachments[0], attachment);
    assert.equal(output[0].text, input[0].text); assert.equal(env.legacyCalls.length, 1); assert.equal(env.networkCalls.length, 1);
    assert.equal(env.status().lastLegacy.timelineConverted, false); assert.equal(env.status().lastLegacy.modelCorrected, true);
    const network = env.networkCalls[0];
    assert.equal(new URL(network.input).searchParams.get('a_bogus'), crypto.createHash('sha256').update(network.init.body).digest('hex'));
  });
}

test('Dola restoration forwards an already signed request and its backend error without rewriting or retrying it', async () => {
  const env = environment({ site: 'dola', nativeDola: true, loadModel: true, modelPreference: 'seedance_v2.5',
    backendStatus: 403, backendBody: { code: 40009, msg: 'model not available' } }); await env.settle();
  const request = draft({ encoded: false }); request.messages[0].ext.chat_ability.ability_param.model = 'ic_mini';
  const init = { method: 'POST', body: JSON.stringify(request), headers: { 'x-signature': 'original' } };
  const url = 'https://www.dola.com/chat/completion?a_bogus=original';
  const response = await env.context.fetch(url, init);
  assert.equal(env.networkCalls.length, 1); assert.strictEqual(env.networkCalls[0].init, init);
  assert.equal(env.networkCalls[0].input, url); assert.equal(params(JSON.parse(init.body)).model, 'ic_mini');
  assert.strictEqual(response, env.networkCalls[0].response);
  assert.equal(response.status, 403); assert.equal((await response.json()).code, 40009);
  assert.equal(env.context.__DOUBAO_DOLA_RESOLUTION_STATUS__.lastRequest.model, 'ic_mini');
});

for (const site of ['doubao', 'dola']) {
  test(`${site}: compact ratio settings and a separate native resolution popup are recognized without fixed ratios`, async () => {
    const env = environment({ site, nativeDoubao: site === 'doubao', nativeDola: site === 'dola' }); await env.settle();
    env.originalContent.textContent = '比例 自动 时长 10s'; env.popup.rect.width = 135; await env.tick();
    assert.ok(env.row()); env.button('1080p').click(); assert.equal(env.nativeInput.gl('video-resolution'), '1080p');
    env.originalContent.textContent = '分辨率'; env.row()?.remove();
    for (const label of ['480P', '720P', '1080P 升级']) {
      const row = new Element('button'); row.textContent = label; row.setAttribute('role', 'option'); env.popup.appendChild(row);
    }
    await env.tick(); assert.equal(env.row(), null);
    let upgrade = 0; env.document.addEventListener('click', () => upgrade++);
    const target = env.popup.children.at(-2);
    assert.equal(gesture(env, target).defaultPrevented, false);
    assert.equal(upgrade, 1); assert.equal(env.storage.get(prefKey), '720p');
    assert.equal(env.nativeInput.gl('video-resolution'), '720p');
    assert.equal(gesture(env, env.popup.children.at(-1)).defaultPrevented, true);
    assert.equal(upgrade, 1); assert.equal(env.storage.get(prefKey), '1080p'); assert.equal(env.networkCalls.length, 0);
  });
}

for (const site of ['doubao', 'dola']) {
  test(`${site}: screenshot menu gains buttons, selection enters draft before signing, native transport retains signature`, async () => {
    const env = environment({ site }); await env.settle();
    assert.ok(env.row()); assert.equal(env.status().submitReady, true);
    assert.equal(env.button('1080p').disabled, false);
    assert.equal(env.originalContent.textContent.includes('时长 10s'), true);
    env.button('1080p').click();
    assert.equal(env.storage.get(prefKey), '1080p');
    assert.equal(env.button('1080p').getAttribute('aria-pressed'), 'true');
    const input = draft(); const originalText = JSON.stringify(input);
    assert.strictEqual(env.service.sendMessage(input, 'argument'), env.returnValue);
    await env.settle();
    assert.equal(JSON.stringify(input), originalText);
    assert.equal(env.nativeCalls.length, 1); assert.strictEqual(env.nativeCalls[0].receiver, env.service);
    assert.deepEqual(env.nativeCalls[0].args, ['argument']);
    const expected = { ...params(input), resolution: '1080p' };
    assert.deepEqual(params(env.nativeCalls[0].request), expected);
    assert.strictEqual(env.nativeCalls[0].request.messages[0].content_blocks_v2, input.messages[0].content_blocks_v2);
    assert.equal(env.networkCalls.length, 1);
    const network = env.networkCalls[0];
    const signature = crypto.createHash('sha256').update(network.init.body).digest('hex');
    assert.equal(new URL(network.input).searchParams.get('a_bogus'), signature);
    assert.equal(network.init.headers['x-signature'], signature);
    assert.equal(env.context.__DOUBAO_DOLA_RESOLUTION_STATUS__.lastRequest.resolution, '1080p');
    env.button('720p').click(); env.service.sendMessage(draft({ resolution: '1080p' }));
    assert.equal(params(env.nativeCalls[1].request).resolution, '720p');
  });
}

test('controls are visibly disabled until service hydration; retry enables them without refreshing', async () => {
  const env = environment({ missingService: true }); await env.settle();
  assert.ok(env.row()); assert.equal(env.button('1080p').disabled, true);
  assert.match(env.row().textContent, /暂未应用/);
  env.button('1080p').click(); assert.equal(env.storage.get(prefKey), undefined);
  await env.makeAvailable();
  assert.equal(env.button('1080p').disabled, false);
  env.button('1080p').click(); assert.equal(env.storage.get(prefKey), '1080p');
});

test('an existing native selector gains only the missing 480p option without duplicating 720p/1080p', async () => {
  const env = environment({ existingNativeControl: true }); await env.settle();
  assert.ok(env.row()); assert.equal(env.status().nativeControl, true);
  assert.deepEqual(env.row().querySelectorAll('button').map(button => button.getAttribute('data-resolution')), ['480p']);
});

test('following the native page forwards the exact draft object unchanged', async () => {
  const env = environment(); await env.settle();
  const input = draft({ resolution: '1080p' }); env.service.sendMessage(input);
  assert.strictEqual(env.nativeCalls[0].request, input);
  assert.equal(env.status().lastDraft.resolution, 'auto');
});

test('object-encoded video params retain model, duration, ratio and unknown fields', async () => {
  const env = environment({ preference: '1080p' }); await env.settle();
  const input = draft({ encoded: false }); env.service.sendMessage(input);
  const result = params(env.nativeCalls[0].request);
  assert.deepEqual(JSON.parse(JSON.stringify(result)), { model: 'seedance_v2.5', duration: 10, ratio: '16:9', resolution: '1080p', private_extra: 'retain' });
  assert.equal(params(input).resolution, '720p');
});

test('ordinary messages, malformed video params and already selected resolutions are not rewritten', async () => {
  const env = environment({ preference: '1080p' }); await env.settle();
  for (const input of [draft({ type: 3 }), draft({ resolution: '1080p' }), { messages: [{ ext: { chat_ability: 'not-json' } }] }]) {
    env.service.sendMessage(input); assert.strictEqual(env.nativeCalls.at(-1).request, input);
  }
  assert.equal(env.status().lastDraft.recognized, false);
  assert.match(env.status().reason, /未包含/);
});

test('a readonly service leaves buttons disabled and preserves its original method', async () => {
  const env = environment({ readonlyService: true }); await env.settle();
  assert.equal(env.status().submitReady, false); assert.equal(env.button('1080p').disabled, true);
  assert.equal(env.service.sendMessage.__DOUBAO_DOLA_RESOLUTION_BRIDGE__, undefined);
});

test('reinstalling or re-discovering the hook creates neither duplicate controls nor native calls', async () => {
  const env = environment({ preference: '1080p' }); await env.settle();
  const original = env.service.sendMessage;
  vm.runInContext(nativeSource, env.context);
  await env.context.__DOUBAO_DOLA_RESOLUTION_BRIDGE__.retry();
  assert.strictEqual(env.service.sendMessage, original);
  assert.equal(env.popup.querySelectorAll(`[${menuMark}]`).length, 1);
  env.service.sendMessage(draft()); assert.equal(env.nativeCalls.length, 1);
});

test('SPA navigation rechecks the current service and preserves a working selection', async () => {
  const env = environment({ preference: '1080p' }); await env.settle();
  env.context.location.href = 'https://www.doubao.com/chat/new-session';
  await env.tick();
  assert.equal(env.status().submitReady, true); assert.equal(env.button('1080p').disabled, false);
  env.service.sendMessage(draft());
  assert.equal(env.nativeCalls.length, 1); assert.equal(params(env.nativeCalls[0].request).resolution, '1080p');
});

function composer(env, text, rich = false) {
  const form = env.document.createElement('form');
  const editor = env.document.createElement(rich ? 'div' : 'textarea');
  if (rich) { editor.setAttribute('contenteditable', 'true'); editor.textContent = text; }
  else editor.value = text;
  editor.rect = { width: 600, height: 180, left: 30, right: 630, top: 200, bottom: 380 };
  const button = env.document.createElement('button'); button.type = 'submit'; button.textContent = '发送';
  form.appendChild(editor); form.appendChild(button); env.document.documentElement.appendChild(form);
  return { form, editor, button, text() { return rich ? editor.textContent : editor.value; },
    clear() { if (rich) editor.textContent = ''; else editor.value = ''; } };
}
function submitEvent(env, compose, type = 'click', overrides = {}) {
  const event = new Event(type, { cancelable: true, bubbles: true });
  const target = type === 'keydown' ? compose.editor : type === 'submit' ? compose.form : compose.button;
  Object.defineProperties(event, Object.fromEntries(Object.entries({ target, key: 'Enter', shiftKey: false,
    isComposing: false, ...overrides }).map(([key, value]) => [key, { value }])));
  env.document.dispatchEvent(event); return event;
}
function promptDraft(text, duration = 30, nested = false) {
  const input = draft();
  const ability = JSON.parse(input.messages[0].ext.chat_ability);
  const parameters = JSON.parse(ability.ability_param); parameters.duration = duration;
  ability.ability_param = JSON.stringify(parameters); input.messages[0].ext.chat_ability = JSON.stringify(ability);
  input.messages[0].content = text;
  input.messages[0].content_blocks_v2 = [nested
    ? { block_type: 1, content: { text_block: { text, extra: 'keep' } } }
    : { block_type: 1, text },
    { block_type: 2, content: { attachment_block: { attachments: [{ id: 'reference', name: '30s', type: 1 }] } } }];
  return input;
}
function submittedText(request, nested = false) {
  const block = request.messages[0].content_blocks_v2[0];
  return nested ? block.content.text_block.text : block.text;
}

for (const rich of [false, true]) {
  test(`domestic generation intent enters the single native signed ${rich ? 'nested' : 'plain'} request after timeline and duration cleanup`, async () => {
    const env = environment({ loadIntent: true, loadTimeline: true, preference: '1080p' }); await env.settle();
    const original = '视频生成参数确认\n[0-3s] 推镜\n[3-30s] 收尾';
    const expected = '视频生成参数确认\n[全片进度 0%–10%] 推镜\n[全片进度 10%–100%] 收尾';
    const compose = composer(env, original, rich); const input = promptDraft(original + '，30s', 30, rich);
    const before = JSON.stringify(input); submitEvent(env, compose, rich ? 'keydown' : 'click');
    assert.strictEqual(env.service.sendMessage(input, 'native-argument'), env.returnValue); await env.settle();
    const prefix = env.context.__DOUBAO_GENERATION_INTENT__.prefix; const output = env.nativeCalls[0].request;
    assert.equal(submittedText(output, rich), prefix + expected); assert.equal(output.messages[0].content, prefix + expected);
    assert.equal(compose.text(), original); assert.equal(JSON.stringify(input), before);
    assert.strictEqual(output.messages[0].content_blocks_v2[1], input.messages[0].content_blocks_v2[1]);
    assert.equal(params(output).resolution, '1080p'); assert.equal(params(output).duration, 30);
    assert.equal(env.status().lastDraft.intentPrefixAdded, true); assert.equal(env.nativeCalls.length, 1); assert.equal(env.networkCalls.length, 1);
    const sent = env.networkCalls[0]; const signature = crypto.createHash('sha256').update(sent.init.body).digest('hex');
    assert.equal(new URL(sent.input).searchParams.get('a_bogus'), signature); assert.equal(sent.init.headers['x-signature'], signature);
    assert.equal(JSON.parse(sent.init.body).messages[0].content, prefix + expected);
    assert.deepEqual(env.nativeCalls[0].args, ['native-argument']); assert.strictEqual(env.nativeCalls[0].receiver, env.service);
  });
}

test('an intent module error cannot block the original native request or create a retry', async () => {
  const env = environment({ preference: '1080p' }); await env.settle();
  env.context.__DOUBAO_GENERATION_INTENT__ = { prepare() { throw Error('unknown prompt'); } };
  assert.strictEqual(env.service.sendMessage(draft()), env.returnValue); await env.settle();
  assert.equal(env.nativeCalls.length, 1); assert.equal(env.networkCalls.length, 1);
  assert.equal(env.status().lastDraft.intentPrefixAdded, false);
});

for (const site of ['doubao', 'dola']) {
  for (const rich of [false, true]) {
    test(`${site}: ${rich ? 'rich Enter' : 'textarea click'} preserves authored times and removes only the new duration suffix before signing`, async () => {
      const env = environment({ site, preference: '1080p', loadDuration: true }); await env.settle();
      const original = '[0-3s] 推镜；00:03-00:05 保持人物位置，持续半秒。\n“东北人的客气：嘴上说不用，腿已经到饭店了。”';
      const compose = composer(env, original, rich);
      env.storage.set('intl_doubao_enable_30s_v1', '1');
      const input = promptDraft(original + '， 30s', 30, rich); const before = JSON.stringify(input);
      env.document.addEventListener(rich ? 'keydown' : 'click', () => {
        assert.equal(compose.text(), original); compose.clear();
        assert.strictEqual(env.service.sendMessage(input, 'native-argument'), env.returnValue);
      }, { once: true });
      const event = submitEvent(env, compose, rich ? 'keydown' : 'click');
      await env.settle();
      assert.equal(event.defaultPrevented, false); assert.equal(env.nativeCalls.length, 1);
      assert.equal(JSON.stringify(input), before);
      const result = env.nativeCalls[0].request;
      assert.equal(result.messages[0].content, original); assert.equal(submittedText(result, rich), original);
      assert.equal(params(result).duration, 30); assert.equal(params(result).resolution, '1080p');
      assert.strictEqual(result.messages[0].content_blocks_v2[1], input.messages[0].content_blocks_v2[1]);
      const network = env.networkCalls[0];
      assert.equal(new URL(network.input).searchParams.get('a_bogus'), crypto.createHash('sha256').update(network.init.body).digest('hex'));
      assert.equal(env.status().lastDraft.promptSuffixRemoved, true);
      assert.equal(JSON.stringify(env.status()).includes('东北人的客气'), false);
    });
  }
}

test('authored trailing 30s survives; only a second platform-added 30s is removed', async () => {
  const env = environment({ loadDuration: true }); await env.settle();
  const original = '用户要求总时长30秒，末尾手写 30s'; const compose = composer(env, original);
  submitEvent(env, compose); const unchanged = promptDraft(original); env.service.sendMessage(unchanged);
  assert.strictEqual(env.nativeCalls[0].request, unchanged);
  submitEvent(env, compose, 'submit'); env.service.sendMessage(promptDraft(original + ',30s'));
  assert.equal(submittedText(env.nativeCalls[1].request), original); assert.equal(params(env.nativeCalls[1].request).duration, 30);
});

test('native duration 15 seconds also stays structured, including follow-page resolution mode', async () => {
  const env = environment({ loadDuration: true }); await env.settle();
  const original = '镜头描述，无自动时间'; const compose = composer(env, original);
  submitEvent(env, compose, 'pointerdown'); compose.clear(); env.service.sendMessage(promptDraft(original + '\n15秒', 15));
  assert.equal(submittedText(env.nativeCalls[0].request), original); assert.equal(params(env.nativeCalls[0].request).duration, 15);
  assert.equal(params(env.nativeCalls[0].request).resolution, '720p');
});

test('absence of a submit snapshot, mismatched text, different duration and non-video requests are forwarded unchanged', async () => {
  const env = environment(); await env.settle(); const compose = composer(env, '原文');
  const inputs = [promptDraft('原文，30s'), promptDraft('另一条提示词，30s'), promptDraft('原文，15s')];
  env.service.sendMessage(inputs[0]);
  for (const input of inputs.slice(1)) { submitEvent(env, compose); env.service.sendMessage(input); }
  const ordinary = promptDraft('原文，30s'); ordinary.messages[0].ext.chat_ability = JSON.stringify({ ability_type: 3 });
  submitEvent(env, compose); env.service.sendMessage(ordinary);
  for (let index = 0; index < inputs.length; index++) assert.strictEqual(env.nativeCalls[index].request, inputs[index]);
  assert.strictEqual(env.nativeCalls.at(-1).request, ordinary);
});

test('a snapshot applies once; expired snapshots, navigation and IME/Shift Enter never authorize text removal', async () => {
  const env = environment({ loadDuration: true }); await env.settle(); const compose = composer(env, '原文');
  submitEvent(env, compose); env.service.sendMessage(promptDraft('原文，30s'));
  const second = promptDraft('原文，30s'); env.service.sendMessage(second);
  assert.strictEqual(env.nativeCalls.at(-1).request, second);
  submitEvent(env, compose); env.advanceTime(60001);
  const expired = promptDraft('原文，30s'); env.service.sendMessage(expired);
  assert.strictEqual(env.nativeCalls.at(-1).request, expired);
  submitEvent(env, compose); env.context.location.href += 'next';
  const otherPage = promptDraft('原文，30s'); env.service.sendMessage(otherPage);
  assert.strictEqual(env.nativeCalls.at(-1).request, otherPage);
  for (const overrides of [{ isComposing: true }, { shiftKey: true }]) {
    const event = submitEvent(env, compose, 'keydown', overrides);
    const input = promptDraft('原文，30s'); env.service.sendMessage(input);
    assert.equal(event.defaultPrevented, false); assert.strictEqual(env.nativeCalls.at(-1).request, input);
  }
});

test('history and assistant messages retain their exact content when the current video prompt is cleaned', async () => {
  const env = environment(); await env.settle(); const compose = composer(env, '原文');
  const history = promptDraft('原文，30s').messages[0];
  const assistant = { ...promptDraft('原文，30s').messages[0], role: 'assistant' };
  const current = promptDraft('原文，30s').messages[0];
  const input = { messages: [history, current, assistant] }; submitEvent(env, compose); env.service.sendMessage(input);
  const output = env.nativeCalls[0].request;
  assert.strictEqual(output.messages[0], history); assert.strictEqual(output.messages[2], assistant);
  assert.equal(output.messages[1].content, '原文');
});

function engineContext() {
  return { content: { abilityType: 17, text: '[0-3s] authored script', chatId: 'fixture-chat',
    actionBarControlParams: { 'video-model': 'seedance_v2.5', 'video-duration': 30, 'video-resolution': '720p', unknown: 'keep' },
    actionBarControlValues: {
      'video-resolution': { value: '720p', submitKey: 'resolution', messageText: '720P', extra: 'retain' },
      'video-duration': { value: 30, submitKey: 'duration', messageText: '30s', extra: 'retain' },
      'video-ratio': { value: '16:9', submitKey: 'ratio', messageText: '16:9' }
    }, attachmentStates: [{ type: 'image', key: 'reference-1' }], extra: { existing: 'keep' } }, pipelineExtra: 'preserve' };
}

test('Dola video send silently selects the captured expert protocol before one native signature', async () => {
  const env = environment({ site: 'dola', nativeDola: true, loadExpert: true }); await env.settle();
  await env.nativeNode.execute(engineContext());
  const input = { option: { keep: 'top', aggregate_params: { keep: 'aggregate' }, model_config: { keep: 'model' } },
    messages: [{ content_block: [{ content: { text_block: { text: 'authored prompt' } } }] }] };
  const before = JSON.stringify(input);
  assert.strictEqual(env.service.sendMessage(input, 'native-extra'), env.returnValue); await env.settle();
  assert.equal(JSON.stringify(input), before);
  assert.equal(env.nativeCalls.length, 1); assert.deepEqual(env.nativeCalls[0].args, ['native-extra']);
  const output = env.nativeCalls[0].request;
  assert.equal(output.option.keep, 'top'); assert.equal(output.option.agent_mode, 1);
  assert.equal(output.option.need_deep_think, 4);
  assert.deepEqual(JSON.parse(JSON.stringify(output.option.aggregate_params)), {
    keep: 'aggregate', agent_mode: '1', model_item_key: '4'
  });
  assert.deepEqual(JSON.parse(JSON.stringify(output.option.model_config)), { keep: 'model', model_item_key: '4' });
  assert.strictEqual(output.messages, input.messages);
  const network = env.networkCalls[0];
  assert.equal(new URL(network.input).searchParams.get('a_bogus'), crypto.createHash('sha256').update(network.init.body).digest('hex'));
  assert.equal(env.status().lastDraft.silentExpertApplied, true);
  assert.equal(env.context.__DOLA_SILENT_EXPERT__.status.appliedCount, 1);

  const ordinary = { option: { aggregate_params: {}, model_config: {} }, messages: [{ text: 'ordinary chat' }] };
  env.service.sendMessage(ordinary);
  assert.strictEqual(env.nativeCalls[1].request, ordinary);
  assert.equal(env.context.__DOLA_SILENT_EXPERT__.status.appliedCount, 1);
});

test('silent expert routing is Dola-only and loaded before the native signing bridge', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'manifest.json')));
  const entry = manifest.content_scripts.find(item => item.js.includes('dola-silent-expert-main.js'));
  assert.ok(entry); assert.equal(entry.world, 'MAIN'); assert.equal(entry.run_at, 'document_start');
  assert.ok(entry.js.indexOf('dola-silent-expert-main.js') < entry.js.indexOf('resolution-native-main.js'));
  const context = { location: { hostname: 'www.doubao.com' }, globalThis: null };
  context.globalThis = context; vm.createContext(context); vm.runInContext(expertSource, context);
  assert.equal(context.__DOLA_SILENT_EXPERT__, undefined);
});

test('Dola updates both native action values and submission records without generating or appending prompt metadata', async () => {
  const env = environment({ site: 'dola', nativeDola: true, preference: '1080p' }); await env.settle();
  assert.equal(env.nativeInput.gl('video-resolution'), '1080p');
  assert.deepEqual(JSON.parse(JSON.stringify(env.nativeInput.N5('video-resolution'))), { value: '1080p', submitKey: 'resolution', messageText: '' });
  assert.equal(env.status().inputReady, true); assert.equal(env.status().pipelineReady, true);
  assert.equal(env.nativeCalls.length, 0); assert.equal(env.pipelineCalls.length, 0);
  const writes = env.inputWrites.length; await env.tick(); assert.equal(env.inputWrites.length, writes);
  env.button('720p').click(); await env.settle();
  assert.equal(env.nativeInput.gl('video-resolution'), '720p'); assert.equal(env.nativeCalls.length, 0);
});

test('Dola native pipeline replaces a stale 720p snapshot before serialization while retaining prompts, attachments and duration', async () => {
  const env = environment({ site: 'dola', nativeDola: true, preference: '1080p', loadDuration: true }); await env.settle();
  const input = engineContext(); const before = JSON.stringify(input);
  const guard = env.permission.execute; const after = env.afterSubmit.execute;
  const result = await env.nativeNode.execute(input, 'native-argument');
  assert.equal(JSON.stringify(input), before); assert.equal(env.pipelineCalls.length, 1);
  assert.deepEqual(env.pipelineCalls[0].args, ['native-argument']); assert.strictEqual(env.pipelineCalls[0].receiver, env.nativeNode);
  assert.equal(result.content.abilityParams.resolution, '1080p'); assert.equal(result.content.abilityParams.duration, 30);
  assert.equal(result.content.abilityParams.native_extra, 'preserve'); assert.equal(result.content.text, input.content.text);
  assert.strictEqual(result.content.attachmentStates, input.content.attachmentStates);
  assert.strictEqual(result.content.extra, input.content.extra);
  assert.deepEqual(JSON.parse(JSON.stringify(result.content.actionBarControlValues['video-ratio'])), input.content.actionBarControlValues['video-ratio']);
  assert.strictEqual(env.permission.execute, guard); assert.strictEqual(env.afterSubmit.execute, after);
  const request = draft(); request.messages[0].ext.chat_ability = JSON.stringify({ ability_type: 17, ability_param: JSON.stringify(result.content.abilityParams) });
  env.service.sendMessage(request); await env.settle();
  const network = env.networkCalls[0];
  assert.equal(params(JSON.parse(network.init.body)).resolution, '1080p');
  assert.equal(new URL(network.input).searchParams.get('a_bogus'), crypto.createHash('sha256').update(network.init.body).digest('hex'));
  assert.equal(env.status().lastPipeline.resolution, '1080p'); assert.equal(env.status().pipelineCount, 1);
});

test('Dola follow-page mode retains native resolution while silencing only duration append metadata', async () => {
  const env = environment({ site: 'dola', nativeDola: true }); await env.settle();
  assert.equal(env.inputWrites.length, 0);
  const result = await env.nativeNode.execute(engineContext());
  assert.equal(result.content.abilityParams.resolution, '720p'); assert.equal(result.content.abilityParams.duration, 30);
  assert.equal(result.content.text, '[0-3s] authored script');
});

test('Dola lazily loaded and renamed native modules are discovered after initial IM readiness', async () => {
  const env = environment({ site: 'dola', nativeDola: true, preference: '1080p', latePipeline: true, renamedModules: true }); await env.settle();
  assert.equal(env.status().pipelineReady, false); assert.equal(env.status().submitReady, true);
  await env.mountPipeline(); assert.equal(env.status().pipelineReady, true);
  const original = env.nativeNode.execute; await env.tick(); assert.strictEqual(env.nativeNode.execute, original);
  assert.equal((await env.nativeNode.execute(engineContext())).content.abilityParams.resolution, '1080p');
});

test('a readonly Dola pipeline remains untouched and reports that the native pipeline hook is unavailable', async () => {
  const env = environment({ site: 'dola', nativeDola: true, frozenPipeline: true }); await env.settle();
  assert.equal(env.status().pipelineReady, false);
  assert.equal((await env.nativeNode.execute(engineContext())).content.abilityParams.resolution, '720p');
  assert.equal(env.status().pipelineCount, 0);
});

test('Dola IM accessor attaches to a replaced message service immediately, retaining its promise and native call result', async () => {
  const env = environment({ site: 'dola', preference: '1080p' }); await env.settle();
  const replacement = env.replaceService();
  const promise = env.im.getMessageService('native-argument'); assert.strictEqual(promise, env.accessorPromise());
  const current = await promise; assert.strictEqual(current, replacement);
  const input = draft(); assert.strictEqual(current.sendMessage(input), env.returnValue);
  assert.equal(env.nativeCalls.length, 1); assert.equal(params(env.nativeCalls[0].request).resolution, '1080p');
  assert.equal(env.status().draftCount, 1);
});

test('Dola refreshes an overwritten send method without URL navigation or duplicate generation', async () => {
  const env = environment({ site: 'dola', preference: '1080p' }); await env.settle();
  env.resetMethod(); await env.tick();
  assert.equal(env.status().serviceRebinds, 1);
  env.service.sendMessage(draft()); assert.equal(env.nativeCalls.length, 1);
  assert.equal(params(env.nativeCalls[0].request).resolution, '1080p');
  await env.tick(); env.service.sendMessage(draft()); assert.equal(env.nativeCalls.length, 2);
});

test('Dola legacy input_skill variables are set before signing without rewriting text or other fields', async () => {
  const env = environment({ site: 'dola', preference: '1080p', legacyServiceEnabled: true }); await env.settle();
  const input = [{ text: '用户自己的 30s', attachments: [{ type: 'image', id: 'ref' }],
    extraExt: { input_skill: JSON.stringify({ skill_type: 17, skill_id: '17', variables: { ratio: '16:9', duration: 30, custom: 'keep' } }), other: 'preserve' } }];
  const before = JSON.stringify(input);
  assert.strictEqual(env.legacyService.sendMessage(input, 'native-argument'), env.returnValue);
  const output = env.legacyCalls[0].request;
  assert.equal(JSON.stringify(input), before); assert.equal(output[0].text, input[0].text);
  assert.strictEqual(output[0].attachments, input[0].attachments);
  assert.deepEqual(JSON.parse(output[0].extraExt.input_skill).variables, { ratio: '16:9', duration: 30, custom: 'keep', resolution: '1080p' });
  assert.equal(env.legacyCalls.length, 1); assert.deepEqual(env.legacyCalls[0].args, ['native-argument']);
  await env.settle(); assert.equal(env.context.__DOUBAO_DOLA_RESOLUTION_STATUS__.lastRequest.resolution, '1080p');
});

test('current Doubao n/h/f input exports and a service accessor submit 1080p through the video pipeline', async () => {
  const env = environment({ site: 'doubao', nativeDoubao: true, preference: '1080p', legacyServiceEnabled: true }); await env.settle();
  assert.equal(env.status().pipelineReady, true); assert.equal(env.status().inputReady, true); assert.equal(env.status().legacyReady, false);
  assert.equal(env.nativeInput.gl('video-resolution'), '1080p');
  const result = await env.nativeNode.execute(engineContext()); assert.equal(result.content.abilityParams.resolution, '1080p');
  env.service.sendMessage(draft()); assert.equal(params(env.nativeCalls[0].request).resolution, '1080p');
});

for (const site of ['dola', 'doubao']) {
  test(`${site}: a native node rebuilding 720p is corrected before native signing`, async () => {
    const env = environment({ site, nativeDola: site === 'dola', nativeDoubao: site === 'doubao',
      nativeResetResolution: true, preference: '1080p' }); await env.settle();
    const input = engineContext();
    const before = JSON.stringify(input);
    const result = await env.nativeNode.execute(input);
    assert.equal(JSON.stringify(input), before);
    assert.equal(result.content.abilityParams.resolution, '1080p');
    assert.equal(result.content.abilityParams.model, 'seedance_v2.5');
    assert.equal(result.content.abilityParams.duration, 30);
    assert.strictEqual(result.content.attachmentStates, input.content.attachmentStates);
    assert.equal(env.status().lastPipeline.nativeResolution, '720p');
    assert.equal(env.status().lastPipeline.corrected, true);
    assert.equal(env.pipelineCalls.length, 1);
    const request = draft();
    request.messages[0].ext.chat_ability = JSON.stringify({ ability_type: 17, ability_param: result.content.abilityParams });
    env.service.sendMessage(request); await env.settle();
    const network = env.networkCalls[0];
    assert.equal(params(JSON.parse(network.init.body)).resolution, '1080p');
    assert.equal(new URL(network.input).searchParams.get('a_bogus'), crypto.createHash('sha256').update(network.init.body).digest('hex'));
  });
}

for (const site of ['dola', 'doubao']) {
  for (const [model, resolution] of [['seedance_v2.5', '480p'], ['seedance_v2.0', '720p'], ['seedance_v2.0', '1080p']]) {
    test(`${site}: ${model}/${resolution} survives native reconstruction and is signed exactly once`, async () => {
      const env = environment({ site, nativeDola: site === 'dola', nativeDoubao: site === 'doubao',
        loadModel: true, modelPreference: model, preference: resolution,
        nativeResetModel: true, nativeResetResolution: true });
      await env.settle();
      const result = await env.nativeNode.execute(engineContext());
      assert.equal(result.content.abilityParams.model, model);
      assert.equal(result.content.abilityParams.resolution, resolution);
      assert.equal(result.content.abilityParams.duration, 30);
      const request = draft();
      request.messages[0].ext.chat_ability = JSON.stringify({ability_type: 17, ability_param: result.content.abilityParams});
      env.service.sendMessage(request); await env.settle();
      assert.equal(env.networkCalls.length, 1);
      const network = env.networkCalls[0]; const sent = params(JSON.parse(network.init.body));
      assert.equal(sent.model, model); assert.equal(sent.resolution, resolution);
      assert.equal(new URL(network.input).searchParams.get('a_bogus'), crypto.createHash('sha256').update(network.init.body).digest('hex'));
    });
  }
}

test('Doubao discovers a late video node and reattaches to a replaced IM service after initial readiness', async () => {
  const env = environment({ site: 'doubao', nativeDoubao: true, preference: '1080p', latePipeline: true }); await env.settle();
  assert.equal(env.status().submitReady, true); assert.equal(env.status().pipelineReady, false);
  await env.mountPipeline(); await env.tick(); assert.equal(env.status().pipelineReady, true);
  env.replaceService(); const current = await env.im.getMessageService();
  current.sendMessage(draft());
  assert.equal(params(env.nativeCalls[0].request).resolution, '1080p');
  assert.equal(env.nativeCalls.length, 1);
});

test('Dola real text block type 10000 retains authored timelines and removes only a newly appended duration', async () => {
  const env = environment({ site: 'dola' }); await env.settle(); const compose = composer(env, '00:03 推镜，手写30s');
  const input = promptDraft('00:03 推镜，手写30s，30s', 30, true); input.messages[0].content_blocks_v2[0].block_type = 10000;
  submitEvent(env, compose); env.service.sendMessage(input);
  assert.equal(submittedText(env.nativeCalls[0].request, true), '00:03 推镜，手写30s');
  assert.equal(params(env.nativeCalls[0].request).duration, 30);
});

test('a non-video ability traverses the original Dola node without any adapter changes', async () => {
  const env = environment({ site: 'dola', nativeDola: true, preference: '1080p' }); await env.settle();
  const input = engineContext(); input.content.abilityType = 3;
  assert.strictEqual(await env.nativeNode.execute(input), input); assert.equal(env.status().pipelineCount, 0);
});

test('the production extension never loads or invokes the legacy prompt guard', () => {
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'manifest.json')));
  assert.equal(manifest.content_scripts.some(entry => entry.js.includes('doubao-direct-generation-main.js')), false);
  // Older installations may retain an unused file; manifest loading and
  // executable references determine whether it can run, not its existence.
  assert.doesNotMatch(nativeSource, /__DOUBAO_VIDEO_DIRECT_GENERATION__/);
});

test('auto-confirm observes the native video draft without rewriting it, and observer failure cannot prevent submission', async () => {
  const env = environment({ nativeDoubao: true, preference: '1080p' }); await env.settle();
  const captured = [];
  env.context.__DOUBAO_AUTO_CONFIRM__ = { observeDraft(request, snapshot) { captured.push({ request, snapshot }); throw Error('observer unavailable'); } };
  const input = draft({ resolution: '1080p' }); const before = JSON.stringify(input);
  assert.strictEqual(env.service.sendMessage(input, 'extra'), env.returnValue);
  assert.equal(env.nativeCalls.length, 1); assert.equal(JSON.stringify(env.nativeCalls[0].request), before);
  assert.equal(captured.length, 1); assert.strictEqual(captured[0].request, input);
  assert.equal(env.networkCalls.length, 1); assert.equal(JSON.stringify(input), before);
});

test('available domestic models keep their native selection callbacks, including entitled 2.5', async () => {
  const env = environment({ nativeDoubao: true, loadModel: true, modelMenu: true }); await env.settle();
  let nativeCallback = 0; env.document.addEventListener('click', () => nativeCallback++);
  assert.equal(gesture(env, env.modelRows.get('seedance_v2.0')).defaultPrevented, false);
  assert.equal(nativeCallback, 1); assert.equal(env.storage.get(modelKey), 'seedance_v2.0');
  env.modelRows.get('seedance_v2.5').children[0].textContent = 'Seedance 2.5 旗舰视频标杆 · 5倍消耗';
  assert.equal(gesture(env, env.modelRows.get('seedance_v2.5')).defaultPrevented, false);
  assert.equal(nativeCallback, 2); assert.equal(env.storage.get(modelKey), 'seedance_v2.5'); assert.equal(env.networkCalls.length, 0);
});

for (const encoded of [false, true]) {
  test(`domestic native submission preserves clean prompt and input box without injecting acceptance prose (${encoded})`, async () => {
    const env = environment({ nativeDoubao: true, loadModel: true, modelPreference: 'seedance_v2.5', preference: '1080p' }); await env.settle();
    const original = '[0-3s] 推镜，保留人物和素材引用。';
    const input = draft({ encoded });
    input.messages[0].content_blocks_v2[0].text = original;
    let ability = input.messages[0].ext.chat_ability;
    if (encoded) ability = JSON.parse(ability);
    let parameters = encoded ? JSON.parse(ability.ability_param) : ability.ability_param;
    parameters.input_box_content = JSON.stringify({ user_input_content: original, reply_message_format: '生成视频：%s', image: 'keep' });
    ability.ability_param = encoded ? JSON.stringify(parameters) : parameters;
    input.messages[0].ext.chat_ability = encoded ? JSON.stringify(ability) : ability;
    const before = JSON.stringify(input);
    env.service.sendMessage(input); await env.settle();
    const output = env.nativeCalls[0].request;
    assert.equal(output.messages[0].content_blocks_v2[0].text, original);
    assert.equal(JSON.parse(params(output).input_box_content).user_input_content, original);
    assert.equal(JSON.stringify(input), before);
    assert.doesNotMatch(JSON.stringify(output), /直接生成控制|任务已受理|正在生成|不要反问/);
    assert.equal(params(output).resolution, '1080p'); assert.equal(env.networkCalls.length, 1);
  });
}

for (const site of ['doubao', 'dola']) {
  for (const rich of [false, true]) {
    test(`${site}: percentage conversion, duration cleanup and native signing work together for ${rich ? 'rich Enter' : 'textarea click'}`, async () => {
      const env = environment({ site, preference: '1080p', loadTimeline: true,
        loadDuration: true }); await env.settle();
      const original = '17–22 秒 | 东北式客气\n22–26 秒 | 瞬间变脸\n26–30 秒 | 结尾包袱\n“我先热热身！”';
      const expected = '全片进度 56.7%–73.3% | 东北式客气\n全片进度 73.3%–86.7% | 瞬间变脸\n全片进度 86.7%–100% | 结尾包袱\n“我先热热身！”';
      const compose = composer(env, original, rich); const input = promptDraft(original + '，30s', 30, rich);
      input.messages[0].content_blocks_v2[0].block_type = 10000;
      const before = JSON.stringify(input);
      const event = submitEvent(env, compose, rich ? 'keydown' : 'click');
      assert.strictEqual(env.service.sendMessage(input, 'native-argument'), env.returnValue);
      const output = env.nativeCalls[0].request; const text = submittedText(output, rich);
      assert.equal(text, expected);
      assert.doesNotMatch(text, /直接生成控制|任务已受理|正在生成/);
      assert.ok(text.endsWith(expected)); assert.doesNotMatch(text, /，30s$/);
      assert.equal(compose.text(), original); assert.equal(event.defaultPrevented, false);
      assert.equal(JSON.stringify(input), before); assert.equal(env.nativeCalls.length, 1);
      assert.deepEqual(env.nativeCalls[0].args, ['native-argument']); assert.strictEqual(env.nativeCalls[0].receiver, env.service);
      assert.strictEqual(output.messages[0].content_blocks_v2[1], input.messages[0].content_blocks_v2[1]);
      assert.equal(params(output).duration, 30); assert.equal(params(output).resolution, '1080p');
      assert.equal(env.status().lastDraft.promptSuffixRemoved, true); assert.equal(env.status().lastDraft.timelineConverted, true);
      assert.equal(JSON.stringify(env.status()).includes('东北式客气'), false);
      await env.settle(); const network = env.networkCalls[0];
      const signature = crypto.createHash('sha256').update(network.init.body).digest('hex');
      assert.equal(new URL(network.input).searchParams.get('a_bogus'), signature);
      assert.equal(network.init.headers['x-signature'], signature);
      assert.equal(JSON.parse(network.init.body).messages[0].content, text);
    });
  }
}

for (const encoded of [false, true]) {
  test(`modern video text and ${encoded ? 'encoded' : 'object'} ability input boxes convert together without changing non-prompt fields`, async () => {
    const env = environment({ site: 'dola', loadTimeline: true }); await env.settle();
    const input = draft({ encoded }); const original = '[0-3s] 开场\n[3-15s] 收尾';
    const expected = '[全片进度 0%–20%] 开场\n[全片进度 20%–100%] 收尾';
    let ability = input.messages[0].ext.chat_ability;
    if (typeof ability === 'string') ability = JSON.parse(ability);
    let parameters = ability.ability_param;
    if (typeof parameters === 'string') parameters = JSON.parse(parameters);
    parameters.duration = 15;
    const box = { user_input_content: original, reply_message_format: '生成视频：%s', reference: '0-3s.png' };
    parameters.input_box_content = encoded ? JSON.stringify(box) : box;
    ability.ability_param = encoded ? JSON.stringify(parameters) : parameters;
    input.messages[0].ext.chat_ability = encoded ? JSON.stringify(ability) : ability;
    input.messages[0].content_blocks_v2[0].text = original;
    const before = JSON.stringify(input); env.service.sendMessage(input);
    const output = env.nativeCalls[0].request; const nextParams = params(output);
    const nextBox = typeof nextParams.input_box_content === 'string' ? JSON.parse(nextParams.input_box_content) : nextParams.input_box_content;
    assert.equal(submittedText(output), expected);
    assert.deepEqual(JSON.parse(JSON.stringify(nextBox)), { ...box, user_input_content: expected });
    assert.deepEqual({ ...nextParams, input_box_content: parameters.input_box_content }, parameters);
    assert.equal(typeof output.messages[0].ext.chat_ability, encoded ? 'string' : 'object');
    assert.equal(typeof nextParams.input_box_content, encoded ? 'string' : 'object');
    assert.equal(JSON.stringify(input), before); assert.strictEqual(env.context.__DOUBAO_DOLA_TIMELINE_PROGRESS__.prepare(output), output);
  });
}

test('Dola native video pipeline converts percentage text after native parameter construction and preserves other nodes', async () => {
  const env = environment({ site: 'dola', nativeDola: true, loadTimeline: true, preference: '1080p' }); await env.settle();
  const input = engineContext(); input.content.text = '17–22 秒 | 客气\n26–30 秒 | 包袱'; const before = JSON.stringify(input);
  const output = await env.nativeNode.execute(input, 'native-argument');
  assert.equal(output.content.text, '全片进度 56.7%–73.3% | 客气\n全片进度 86.7%–100% | 包袱');
  assert.equal(output.content.abilityParams.resolution, '1080p'); assert.equal(output.content.abilityParams.duration, 30);
  assert.equal(JSON.stringify(input), before); assert.strictEqual(output.content.attachmentStates, input.content.attachmentStates);
  assert.equal(env.pipelineCalls.length, 1); assert.deepEqual(env.pipelineCalls[0].args, ['native-argument']);
  assert.strictEqual(env.pipeline.submitNodes[0], env.permission); assert.strictEqual(env.pipeline.afterSubmitNodes[0], env.afterSubmit);
  assert.equal(env.status().lastPipeline.timelineConverted, true);
  assert.equal(JSON.stringify(env.status()).includes('客气'), false);
});

for (const encodedBody of [false, true]) {
  test(`Dola legacy ${encodedBody ? 'JSON' : 'plain'} message body converts before signing while preserving attachments`, async () => {
    const env = environment({ site: 'dola', preference: '1080p', legacyServiceEnabled: true, loadTimeline: true }); await env.settle();
    const attachment = { kind: 'image', name: '0-3s.png' }; const original = '0-3s 开场；26-30秒收尾';
    const expected = '全片进度 0%–10% 开场；全片进度 86.7%–100%收尾';
    const input = [{ role: 'user', body: encodedBody ? JSON.stringify({ text: original, references: [attachment.name] }) : original,
      attachments: [attachment], extraExt: { input_skill: JSON.stringify({ skill_type: 17,
        variables: { duration: 30, ratio: '16:9', custom: 'keep' } }), other: 'preserve' } }];
    const before = JSON.stringify(input); assert.strictEqual(env.legacyService.sendMessage(input, 'native-argument'), env.returnValue);
    const output = env.legacyCalls[0].request;
    if (encodedBody) assert.deepEqual(JSON.parse(output[0].body), { text: expected, references: [attachment.name] });
    else assert.equal(output[0].body, expected);
    assert.strictEqual(output[0].attachments, input[0].attachments); assert.equal(JSON.stringify(input), before);
    assert.deepEqual(JSON.parse(output[0].extraExt.input_skill).variables, { duration: 30, ratio: '16:9', custom: 'keep', resolution: '1080p' });
    assert.equal(env.legacyCalls.length, 1); assert.equal(env.status().lastLegacy.timelineConverted, true);
    await env.settle(); const network = env.networkCalls[0];
    assert.equal(new URL(network.input).searchParams.get('a_bogus'), crypto.createHash('sha256').update(network.init.body).digest('hex'));
  });
}
