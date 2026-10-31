const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'doubao-auto-confirm-main.js'), 'utf8');
const generationAttribute = 'data-watermark-free-generation-evidence';

class Element extends EventTarget {
  constructor(tag) { super(); this.tagName = tag.toUpperCase(); this.attrs = new Map(); this.nodes = []; this.parentElement = null; this._text = ''; this._value = ''; this.disabled = false; this.style = {}; this.rect = { width: 500, height: 100 }; this.clicks = 0; }
  setAttribute(key, value) { this.attrs.set(key, String(value)); }
  getAttribute(key) { return this.attrs.get(key) ?? null; }
  get value() { return this._value; }
  set value(value) { this._value = String(value); }
  get textContent() { return this._text + this.nodes.map(node => node.textContent).join(' '); }
  set textContent(value) { this._text = String(value); this.nodes = []; }
  get innerText() { return this.textContent; }
  get isConnected() { return this.root || Boolean(this.parentElement?.isConnected); }
  appendChild(node) { node.parentElement = this; this.nodes.push(node); return node; }
  getBoundingClientRect() { return this.rect; }
  focus() {}
  matches(selector) {
    return selector.split(',').some(part => {
      part = part.trim(); const tag = part.match(/^[a-z]+/i)?.[0];
      if (tag && tag.toUpperCase() !== this.tagName) return false;
      for (const match of part.matchAll(/\[([^\]*=]+)(?:="?([^"\]]*)"?)?\]/g)) {
        const value = this.getAttribute(match[1]); if (value == null || match[2] != null && value !== match[2]) return false;
      }
      return Boolean(tag || part.includes('['));
    });
  }
  querySelectorAll(selector) { const result = []; const visit = node => { for (const child of node.nodes) { if (child.matches(selector)) result.push(child); visit(child); } }; visit(this); return result; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  click() { if (this.disabled) return; this.clicks += 1; this.dispatchEvent(new Event('click')); }
}
function videoRequest(params = {}) {
  return { messages: [{ role: 'user', content_blocks_v2: [{ block_type: 1, text: '私人原始剧本与参考图 @图一' }, { block_type: 2, attachment: 'preserve' }],
    ext: { chat_ability: JSON.stringify({ ability_type: 17, ability_param: JSON.stringify({ model: 'seedance_v2.5', duration: 30, ratio: '16:9', resolution: '1080p', ...params }) }) } }] };
}
function environment({ site = 'doubao', disabled = false, sendUnavailable = false, editable = false } = {}) {
  const html = new Element('html'); html.root = true; const body = html.appendChild(new Element('body'));
  const handlers = new Map(); const timers = []; const native = []; let now = 1700000000000; let selectedEditor;
  const document = { documentElement: html, body, readyState: 'complete',
    querySelectorAll: selector => html.querySelectorAll(selector),
    addEventListener(name, fn) { if (!handlers.has(name)) handlers.set(name, []); handlers.get(name).push(fn); },
    dispatchEvent(event) { for (const fn of handlers.get(event.type) || []) fn(event); return true; },
    createRange() { return { selectNodeContents(editor) { selectedEditor = editor; } }; },
    execCommand(command, ui, text) { if (command !== 'insertText' || !selectedEditor) return false; selectedEditor.textContent = text; selectedEditor.dispatchEvent(new Event('input')); return true; }
  };
  const storage = new Map(disabled ? [['intl_doubao_auto_confirm_v1', '0']] : []);
  const form = body.appendChild(new Element('form')); const editor = form.appendChild(new Element(editable ? 'div' : 'textarea'));
  if (editable) editor.setAttribute('contenteditable', 'true');
  const send = form.appendChild(new Element('button')); send.setAttribute('aria-label', '发送消息'); send.disabled = sendUnavailable;
  send.addEventListener('click', () => native.push({ kind: 'composer', text: editable ? editor.innerText : editor.value }));
  const context = { document, Event, console, Date: class extends Date { static now() { return now; } },
    location: { hostname: `www.${site}.com`, href: `https://www.${site}.com/chat/current` },
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, String(value)) },
    getComputedStyle: node => ({ display: node.style.display || 'block', visibility: 'visible' }),
    getSelection: () => ({ removeAllRanges() {}, addRange() {} }), setInterval(fn) { timers.push(fn); },
    fetch() { throw Error('The auto-confirm module must not send HTTP requests'); }
  };
  context.window = context; vm.createContext(context); vm.runInContext(source, context);
  const api = context.__DOUBAO_AUTO_CONFIRM__;
  function evidence(overrides = {}) {
    html.setAttribute(generationAttribute, JSON.stringify({ sequence: 1, phase: 'response_ended', httpStatus: 200,
      taskFields: [], statusFields: [], ...overrides })); document.dispatchEvent(new Event('watermark-free-generation-evidence'));
  }
  return { context, document, html, body, form, editor, send, native, storage, api, evidence,
    arm(request = videoRequest(), snapshot = {}) {
      const before = JSON.stringify(request);
      api.observeDraft(request, { userGesture: true, interactionToken: api.interactionToken, at: now, text: '私人原始剧本与参考图 @图一', ...snapshot });
      assert.equal(JSON.stringify(request), before); return request;
    },
    plan({ button = false, label = '确认生成', model = 'Seedance 2.5', duration = 30, ratio = '16:9', resolution = '1080P', ask = '请回复确认，确认后开始生成。', refusal = '' } = {}) {
      const root = body.appendChild(new Element('div')); const heading = root.appendChild(new Element('strong')); heading.textContent = '视频生成参数确认';
      const content = root.appendChild(new Element('p')); content.textContent = `模型：${model} 时长：${duration}秒 比例：${ratio} 分辨率：${resolution} ${ask} ${refusal}`;
      let control;
      if (button) { control = root.appendChild(new Element('button')); control.textContent = label; control.addEventListener('click', () => native.push({ kind: 'button' })); }
      return { root, heading, content, control };
    },
    tick(ms = 0) { now += ms; for (const timer of timers) timer(); },
    userEvent(type = 'pointerdown') { document.dispatchEvent({ type, isTrusted: true }); },
    status() { return JSON.parse(JSON.stringify(api.status)); }
  };
}

test('a newly returned native confirmation button is clicked once after the original response ends', () => {
  const env = environment(); env.arm(); const plan = env.plan({ button: true });
  env.evidence({ phase: 'response_streaming' }); env.tick(); assert.equal(plan.control.clicks, 0);
  env.evidence(); for (let i = 0; i < 6; i++) env.tick();
  assert.equal(plan.control.clicks, 1); assert.deepEqual(env.native, [{ kind: 'button' }]);
  assert.equal(env.status().triggerCount, 1); assert.equal(env.status().phase, 'action_triggered');
  assert.equal(env.editor.value, ''); assert.match(env.status().reason, /尚未验证视频任务/);
  env.evidence({ sequence: 2, phase: 'request_observed' }); assert.equal(env.status().phase, 'confirmation_request_observed');
  env.tick(); assert.equal(plan.control.clicks, 1);
});

for (const editable of [false, true]) {
  test(`text-only confirmation uses the empty native ${editable ? 'rich editor' : 'textarea'} and its send callback once`, () => {
    const env = environment({ editable }); env.arm(); env.plan(); env.evidence();
    assert.equal(env.status().phase, 'preparing_confirmation'); assert.equal(env.native.length, 0);
    env.tick(); assert.equal(env.native.length, 1); assert.equal(env.native[0].kind, 'composer');
    assert.match(env.native[0].text, /确认以上视频生成参数/); assert.doesNotMatch(env.native[0].text, /已受理|已开始|合法能力|私人原始剧本/);
    for (let i = 0; i < 6; i++) env.tick(); assert.equal(env.native.length, 1);
    env.arm(videoRequest(), { userGesture: false, text: env.native[0].text }); env.evidence({ sequence: 2 });
    env.plan(); env.tick(); assert.equal(env.native.length, 1); assert.equal(env.status().triggerCount, 1);
  });
}

test('old confirmation messages and a module loaded without a fresh user video submission cause no action', () => {
  const env = environment(); env.plan({ button: true }); env.evidence(); env.tick(); assert.equal(env.native.length, 0);
  env.arm(); env.evidence({ sequence: 2 }); env.tick(9000); assert.equal(env.native.length, 0); assert.equal(env.status().phase, 'not_needed');
});

test('a native video task candidate, business error or HTTP error prevents an extra confirmation', () => {
  for (const response of [{ taskFields: [{ key: 'task_id' }] }, { statusFields: [{ path: '$.code', key: 'code', value: 403 }] }, { phase: 'http_error', httpStatus: 403 }]) {
    const env = environment(); env.arm(); env.plan({ button: true }); env.evidence(response); env.tick(); assert.equal(env.native.length, 0);
  }
});

test('mismatching model, duration, ratio, resolution and plain summaries without a confirmation request are not auto-approved', () => {
  for (const plan of [{ model: 'Seedance 2.0' }, { duration: 15 }, { ratio: '9:16' }, { resolution: '720P' }, { ask: '以下是分镜方案。' }, { refusal: '当前账号无法生成视频。' }]) {
    const env = environment(); env.arm(); env.plan({ button: true, ...plan }); env.evidence(); env.tick(9000);
    assert.equal(env.native.length, 0); assert.equal(env.status().phase, 'not_needed');
  }
});

test('an existing user draft is never overwritten or submitted', () => {
  const env = environment(); env.editor.value = '用户另写的新草稿'; env.arm(); env.plan(); env.evidence();
  assert.equal(env.editor.value, '用户另写的新草稿'); assert.equal(env.native.length, 0); assert.equal(env.status().phase, 'blocked');
});

test('missing native send controls withdraw only the auto-filled text, without guessing a request or pressing Enter', () => {
  const env = environment({ sendUnavailable: true }); env.arm(); env.plan(); env.evidence();
  assert.match(env.editor.value, /确认以上/); env.tick(3100);
  assert.equal(env.editor.value, ''); assert.equal(env.native.length, 0); assert.equal(env.status().phase, 'blocked');
});

test('a real user interaction cancels a pending round, while the generated click itself cannot create a loop', () => {
  const env = environment(); env.arm(); env.plan({ button: true }); env.userEvent(); env.evidence();
  assert.equal(env.native.length, 0); assert.equal(env.status().phase, 'cancelled');
  const old = env.api.interactionToken; env.userEvent(); env.arm(videoRequest(), { interactionToken: old });
  env.evidence(); assert.equal(env.native.length, 0); assert.equal(env.status().phase, 'cancelled');
});

test('disabled auto-confirm, untrusted snapshots, ordinary chat and expired snapshots do not arm', () => {
  const off = environment({ disabled: true }); off.arm(); assert.equal(off.status().phase, 'idle');
  const env = environment(); env.arm(videoRequest(), { userGesture: false }); assert.equal(env.status().phase, 'idle');
  env.arm({ messages: [{ role: 'user', text: 'ordinary chat' }] }); assert.equal(env.status().phase, 'idle');
  env.arm(videoRequest(), { at: 0 }); assert.equal(env.status().phase, 'idle');
  env.arm({ messages: [{ role: 'user', text: 'new chat' }], history: videoRequest() }); assert.equal(env.status().phase, 'idle');
});

test('a disabled real confirmation button is waited for, rather than sending a duplicate message', () => {
  const env = environment(); env.arm(); const plan = env.plan({ button: true }); plan.control.disabled = true; env.evidence(); env.tick();
  assert.equal(env.native.length, 0); assert.equal(env.editor.value, '');
  plan.control.disabled = false; env.tick(); env.tick(); assert.equal(plan.control.clicks, 1); assert.equal(env.native.length, 1);
});

test('a newer request or an expired original request cancels the round without generating another task', () => {
  const env = environment(); env.arm(); env.plan(); env.evidence({ sequence: 2 }); assert.equal(env.status().phase, 'cancelled'); assert.equal(env.native.length, 0);
  const expired = environment(); expired.arm(); expired.plan(); expired.tick(181000); expired.evidence();
  assert.equal(expired.status().phase, 'expired'); assert.equal(expired.native.length, 0);
});

test('closing the toggle cancels a prepared confirmation and removes only its own generated text', () => {
  const env = environment(); env.arm(); env.plan(); env.evidence();
  env.html.setAttribute('data-watermark-free-auto-confirm-enabled', '0'); env.document.dispatchEvent(new Event('watermark-free-auto-confirm-change'));
  assert.equal(env.editor.value, ''); env.tick(); assert.equal(env.native.length, 0); assert.equal(env.status().enabled, false);
});

test('Dola stays unchanged, installation is idempotent, and diagnostics never include authored story or attachment names', () => {
  const dola = environment({ site: 'dola' }); assert.equal(dola.api, undefined);
  const env = environment(); vm.runInContext(source, env.context); assert.strictEqual(env.context.__DOUBAO_AUTO_CONFIRM__, env.api);
  env.arm(); env.plan(); env.evidence(); assert.doesNotMatch(JSON.stringify(env.status()), /私人原始剧本|图一/);
});

test('old confirmation content re-rendered into new DOM nodes is not approved as a new reply', () => {
  const env = environment(); const previous = env.plan({ button: true }); env.arm();
  env.body.nodes = env.body.nodes.filter(node => node !== previous.root); previous.root.parentElement = null;
  env.plan({ button: true }); env.evidence(); env.tick(9000);
  assert.equal(env.native.length, 0); assert.equal(env.status().phase, 'not_needed');
});

test('a user-authored prompt containing a parameter-confirmation heading cannot masquerade as an assistant confirmation', () => {
  const env = environment(); env.arm(videoRequest(), { text: '原稿引用了“视频生成参数确认”标题。' });
  env.plan({ button: true }); env.evidence(); env.tick(); assert.equal(env.status().phase, 'skipped_prompt_confirmation'); assert.equal(env.native.length, 0);
  assert.match(env.status().reason, /原稿含参数确认标题/);
});

test('a native action failure and an unobserved follow-up request do not cause retries', () => {
  const env = environment(); env.arm(); const plan = env.plan({ button: true });
  let calls = 0; plan.control.click = () => { calls++; throw Error('native control failure'); };
  env.evidence(); for (let i = 0; i < 3; i++) env.tick(); assert.equal(calls, 1); assert.equal(env.status().phase, 'blocked');
  const quiet = environment(); quiet.arm(); quiet.plan({ button: true }); quiet.evidence(); quiet.tick(11000); quiet.tick();
  assert.equal(quiet.native.length, 1); assert.equal(quiet.status().phase, 'action_unconfirmed');
});

test('viewing the extension diagnostics or expanding its panel does not cancel a pending native confirmation', () => {
  for (const name of ['panel-toggle', 'resolution-diagnostics', 'debugger-export']) {
    const env = environment(); env.arm(); env.plan({ button: true });
    env.document.dispatchEvent({ type: 'pointerdown', isTrusted: true, composedPath: () => [
      { getAttribute: key => key === 'class' ? name : null }, { id: 'watermark-free-media-panel' }
    ] });
    env.evidence(); assert.equal(env.native.length, 1);
  }
});

test('missing or future gesture timestamps and unsupported video durations cannot arm a confirmation', () => {
  const env = environment();
  env.arm(videoRequest(), { at: undefined }); assert.equal(env.status().phase, 'idle');
  env.arm(videoRequest(), { at: 1700000001000 }); assert.equal(env.status().phase, 'idle');
  env.arm(videoRequest({ duration: 700 })); assert.equal(env.status().phase, 'idle');
});
