const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'dola-silent-expert-main.js'), 'utf8');

function node(label, click) {
  return {
    innerText: label, textContent: label, disabled: false, isConnected: true,
    getAttribute: () => null, closest: () => null,
    getBoundingClientRect: () => ({ width: 90, height: 30, left: 100, bottom: 100 }),
    click
  };
}

test('native fast mode is switched to the newly opened expert option before the original send is replayed', async () => {
  let menuOpen = false; let expert = false; let sends = 0;
  const trigger = node('快速', () => { menuOpen = true; });
  const option = node('专家模式', () => { expert = true; trigger.innerText = trigger.textContent = '专家模式'; });
  const send = node('发送', () => { sends += 1; });
  const rootElement = { setAttribute() {} };
  const document = new EventTarget();
  document.documentElement = rootElement;
  document.querySelectorAll = selector => selector.includes('flow-end-msg-send') ? [send] : menuOpen ? [trigger, option] : [trigger];
  const context = {
    document, Event, setTimeout, clearTimeout,
    location: { hostname: 'www.dola.com' },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible' }),
    globalThis: null
  };
  context.globalThis = context;
  vm.createContext(context); vm.runInContext(source, context);
  assert.equal(await context.__DOLA_SILENT_EXPERT__.selectBeforeSend(send), true);
  assert.equal(expert, true); assert.equal(sends, 1);
  const status = context.__DOLA_SILENT_EXPERT__.status;
  assert.equal(status.uiSwitchCount, 1); assert.equal(status.last, null);
});

test('an unavailable expert option blocks replay and exposes a diagnostic instead of sending fast mode', async () => {
  let sends = 0;
  const trigger = node('快速', () => {});
  const send = node('发送', () => { sends += 1; });
  const document = new EventTarget(); document.documentElement = { setAttribute() {} };
  document.querySelectorAll = selector => selector.includes('flow-end-msg-send') ? [send] : [trigger];
  const immediateTimeout = (fn) => { Promise.resolve().then(fn); return 1; };
  const context = { document, Event, setTimeout: immediateTimeout, clearTimeout() {},
    Date: class extends Date { static now() { this.value = (this.value || 0) + 1000; return this.value; } },
    location: { hostname: 'www.dola.com' }, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }), globalThis: null };
  context.globalThis = context;
  vm.createContext(context); vm.runInContext(source, context);
  assert.equal(await context.__DOLA_SILENT_EXPERT__.selectBeforeSend(send), false);
  assert.equal(sends, 0);
  assert.match(context.__DOLA_SILENT_EXPERT__.status.reason, /专家模式选项未出现/);
});

test('protocol mode may use a connected hidden native trigger without allowing a fast send', async () => {
  let menuOpen = false; let sends = 0;
  const trigger = node('快速', () => { menuOpen = true; });
  trigger.getBoundingClientRect = () => ({ width: 0, height: 0, left: 0, bottom: 0 });
  const option = node('专家模式', () => { trigger.innerText = trigger.textContent = '专家模式'; });
  option.getBoundingClientRect = () => ({ width: 0, height: 0, left: 0, bottom: 0 });
  const send = node('发送', () => { sends += 1; });
  const document = new EventTarget(); document.documentElement = { setAttribute() {} };
  document.querySelectorAll = selector => selector.includes('flow-end-msg-send') ? [send] : menuOpen ? [trigger, option] : [trigger];
  const context = { document, Event, setTimeout, clearTimeout,
    location: { hostname: 'www.dola.com' }, getComputedStyle: () => ({ display: 'none', visibility: 'hidden' }), globalThis: null };
  context.globalThis = context;
  vm.createContext(context); vm.runInContext(source, context);
  assert.equal(await context.__DOLA_SILENT_EXPERT__.selectBeforeSend(send, { allowHidden: true }), true);
  assert.equal(sends, 1);
});

test('final video request is converted to expert route without mutating the input', () => {
  const document = new EventTarget(); document.documentElement = { setAttribute() {} };
  document.querySelectorAll = () => [];
  const context = { document, Event, setTimeout, clearTimeout,
    location: { hostname: 'www.dola.com' }, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }), globalThis: null };
  context.globalThis = context;
  vm.createContext(context); vm.runInContext(source, context);
  context.__DOLA_SILENT_EXPERT__.arm({ model: 'seedance_v2.5', duration: 10, ratio: '9:16', resolution: '1080p', source: 'protocol_button' });
  const ability = { ability_type: 17, ability_param: { model: 'old', duration: 30, ratio: '16:9', resolution: '720p', keep: true } };
  const input = { option: { aggregate_params: {}, model_config: {} }, messages: [{ ext: { chat_ability: JSON.stringify(ability) } }] };
  const before = JSON.stringify(input);
  const output = context.__DOLA_SILENT_EXPERT__.prepare(input);
  assert.equal(JSON.stringify(input), before);
  assert.equal(Object.hasOwn(output.messages[0].ext, 'chat_ability'), false);
  assert.equal(output.option.agent_mode, 1);
  assert.equal(output.option.need_deep_think, 4);
  assert.equal(output.option.model_config.model_item_key, '4');
  assert.equal(context.__DOLA_SILENT_EXPERT__.status.last.parameterInjection, true);
});

test('quick protocol patches an existing native video ability without converting agent_mode 2', () => {
  const attributes = new Map([
    ['data-dola-protocol-mode', 'quick'],
    ['data-dola-protocol-parameter-selection', JSON.stringify({
      model: 'seedance_v2.0', duration: 15, ratio: '16:9', resolution: '720p'
    })]
  ]);
  const document = new EventTarget();
  document.documentElement = { setAttribute(name, value) { attributes.set(name, value); },
    getAttribute(name) { return attributes.get(name) || null; } };
  document.querySelectorAll = () => [];
  const context = { document, Event, setTimeout, clearTimeout,
    location: { hostname: 'www.dola.com' }, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }), globalThis: null };
  context.globalThis = context; vm.createContext(context); vm.runInContext(source, context);
  const ability = { ability_type: 17, ability_param: {
    model: 'legacy_quick', duration: 10, ratio: '9:16', resolution: '480p', keep: true
  } };
  const input = { option: { agent_mode: 2, need_deep_think: 0, aggregate_params: { agent_mode: '2' } },
    messages: [{ ext: { chat_ability: JSON.stringify(ability) } }] };
  const output = context.__DOLA_SILENT_EXPERT__.prepare(input);
  const patched = JSON.parse(output.messages[0].ext.chat_ability);
  assert.equal(output.option.agent_mode, 2);
  assert.equal(output.option.need_deep_think, 0);
  assert.equal(output.option.aggregate_params.agent_mode, '2');
  assert.equal(patched.ability_param.model, 'seedance_v2.0');
  assert.equal(patched.ability_param.duration, 15);
  assert.equal(patched.ability_param.ratio, '16:9');
  assert.equal(patched.ability_param.resolution, '720p');
  assert.equal(patched.ability_param.keep, true);
  const status = context.__DOLA_SILENT_EXPERT__.status;
  assert.equal(status.last.routePreserved, true);
  assert.equal(status.last.parameterInjection, true);
  assert.equal(status.last.protocolMode, 'quick');
});

test('quick agent request without a video ability remains byte-for-byte route compatible', () => {
  const attributes = new Map([
    ['data-dola-protocol-mode', 'quick'],
    ['data-dola-protocol-parameter-selection', JSON.stringify({
      model: 'seedance_v2.0', duration: 15, ratio: '16:9', resolution: '720p'
    })]
  ]);
  const document = new EventTarget();
  document.documentElement = { setAttribute(name, value) { attributes.set(name, value); },
    getAttribute(name) { return attributes.get(name) || null; } };
  document.querySelectorAll = () => [];
  const context = { document, Event, setTimeout, clearTimeout,
    location: { hostname: 'www.dola.com' }, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }), globalThis: null };
  context.globalThis = context; vm.createContext(context); vm.runInContext(source, context);
  const input = { option: { agent_mode: 2, need_deep_think: 0 }, messages: [{ role: 'user', text: '生成视频' }] };
  const before = JSON.stringify(input);
  const output = context.__DOLA_SILENT_EXPERT__.prepare(input);
  assert.equal(JSON.stringify(output), before);
  assert.equal(context.__DOLA_SILENT_EXPERT__.status.last.routePreserved, true);
  assert.equal(context.__DOLA_SILENT_EXPERT__.status.last.explicitVideo, false);
  assert.match(context.__DOLA_SILENT_EXPERT__.status.reason, /未暴露视频能力字段/);
});

test('a persisted native image block survives the expert layer switch and joins the final signed message', () => {
  const document = new EventTarget(); document.documentElement = { setAttribute() {} }; document.querySelectorAll = () => [];
  const values = new Map();
  const sessionStorage = {
    getItem: key => values.get(key) || null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: key => values.delete(key)
  };
  const attachment = { block_type: 10052, block_id: 'image-block',
    content: { attachment_block: { attachments: [{ type: 1, identifier: 'image-1', image: { uri: 'tos/test.png' } }] } } };
  sessionStorage.setItem('dola-toolkit-preserved-expert-media-v1', JSON.stringify({
    savedAt: Date.now(), block: attachment, counts: { image: 1, video: 0, audio: 0 }
  }));
  const context = { document, Event, setTimeout, clearTimeout, sessionStorage,
    location: { hostname: 'www.dola.com' }, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }), globalThis: null };
  context.globalThis = context; vm.createContext(context); vm.runInContext(source, context);
  context.__DOLA_SILENT_EXPERT__.arm({ model: 'seedance_v2.0', duration: 5, ratio: '16:9', resolution: '720p' });
  const output = context.__DOLA_SILENT_EXPERT__.prepare({
    option: { aggregate_params: {}, model_config: {} },
    messages: [{ local_message_id: 'message', content_block: [
      { block_type: 10000, content: { text_block: { text: '原始提示词' } } }
    ] }]
  });
  assert.deepEqual(Array.from(output.messages[0].content_block, block => block.block_type), [10052, 10000]);
  assert.equal(context.__DOLA_SILENT_EXPERT__.status.lastMediaInjection.source, 'session_native_block');
  assert.match(output.messages[0].content_block[1].content.text_block.text, /可以播放、带下载按钮的 Dola 原生视频卡片/);
  assert.match(output.messages[0].content_block[1].content.text_block.text, /不要只回复网址或文字链接/);
});

test('final expert wire request collapses duplicate native 10052 blocks', () => {
  const document = new EventTarget(); document.documentElement = { setAttribute() {} }; document.querySelectorAll = () => [];
  const context = { document, Event, setTimeout, clearTimeout,
    location: { hostname: 'www.dola.com' }, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }), globalThis: null };
  context.globalThis = context; vm.createContext(context); vm.runInContext(source, context);
  context.__DOLA_SILENT_EXPERT__.arm({ model: 'seedance_v2.0', duration: 5, ratio: '16:9', resolution: '720p' });
  const item = { type: 1, identifier: 'same-image', image: { uri: 'tos/same.png' } };
  const block = id => ({ block_type: 10052, block_id: id,
    content: { attachment_block: { attachments: [item] } } });
  const output = context.__DOLA_SILENT_EXPERT__.prepare({
    option: { aggregate_params: {}, model_config: {} },
    messages: [{ content_block: [block('a'), block('b'),
      { block_type: 10000, content: { text_block: { text: '原始提示词' } } }] }]
  });
  assert.deepEqual(Array.from(output.messages[0].content_block, value => value.block_type), [10052, 10000]);
  assert.equal(output.messages[0].content_block[0].content.attachment_block.attachments.length, 1);
  assert.equal(context.__DOLA_SILENT_EXPERT__.status.lastMediaDeduplication.applied, true);
});

test('attachments merged from separate native messages become one 10052 block before signing', () => {
  const document = new EventTarget(); document.documentElement = { setAttribute() {} }; document.querySelectorAll = () => [];
  const context = { document, Event, setTimeout, clearTimeout,
    location: { hostname: 'www.dola.com' }, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }), globalThis: null };
  context.globalThis = context; vm.createContext(context); vm.runInContext(source, context);
  context.__DOLA_SILENT_EXPERT__.arm({ model: 'seedance_v2.0', duration: 30, ratio: '16:9', resolution: '720p' });
  const attachment = identifier => ({ type: 1, identifier, image: { uri: `tos/${identifier}.png` } });
  const block = identifier => ({ block_type: 10052, block_id: identifier,
    content: { attachment_block: { attachments: [attachment(identifier)] } } });
  const output = context.__DOLA_SILENT_EXPERT__.prepare({
    option: { aggregate_params: {}, model_config: {} },
    messages: [
      { local_message_id: 'old', content_block: [block('first')] },
      { local_message_id: 'current', content_block: [block('second'),
        { block_type: 10000, content: { text_block: { text: '原始提示词' } } }] }
    ]
  });
  assert.equal(output.messages.length, 1);
  assert.deepEqual(Array.from(output.messages[0].content_block, value => value.block_type), [10052, 10000]);
  assert.deepEqual(Array.from(output.messages[0].content_block[0].content.attachment_block.attachments, value => value.identifier), ['first', 'second']);
});

test('expert remount restores the authored prompt and requires preserved image references before replay', async () => {
  let menuOpen = false; let sends = 0; let currentComposer;
  const composer = (value) => {
    const target = node(value, () => {}); target.tagName = 'DIV'; target.isContentEditable = true;
    target.getAttribute = name => name === 'contenteditable' ? 'true' : null;
    target.dispatchEvent = () => true;
    let html = value;
    Object.defineProperty(target, 'innerHTML', { get: () => html, set(next) {
      html = next; target.innerText = target.textContent = String(next).replace(/<[^>]+>/g, ' ').replace(/&amp;/g, '&').replace(/\s+/g, ' ').trim();
    } });
    return target;
  };
  const original = composer('保留这段提示词'); currentComposer = original;
  const trigger = node('快速', () => { menuOpen = true; });
  const option = node('专家模式', () => {
    trigger.innerText = trigger.textContent = '专家模式';
    currentComposer = composer('');
  });
  const send = node('发送', () => { sends += 1; });
  const document = new EventTarget(); document.documentElement = { setAttribute() {} };
  document.querySelectorAll = selector => selector.includes('contenteditable') ? [currentComposer] :
    selector.includes('flow-end-msg-send') ? [send] : menuOpen ? [trigger, option] : [trigger];
  const media = { preserve: () => ({ ok: true, attachmentCount: 1 }),
    restore: async () => ({ ok: true, attachmentCount: 1 }), status: () => ({ attachmentCount: 1 }) };
  const context = { document, Event, setTimeout, clearTimeout, __DOLA_TOOLKIT_MEDIA_MODE_SWITCH__: media,
    location: { hostname: 'www.dola.com' }, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }), globalThis: null };
  context.globalThis = context; vm.createContext(context); vm.runInContext(source, context);
  assert.equal(await context.__DOLA_SILENT_EXPERT__.selectBeforeSend(send, { media: { attachmentCount: 1 },
    context: { model: 'seedance_v2.0', duration: 30, ratio: '16:9', resolution: '720p' } }), true);
  assert.match(currentComposer.textContent, /专家模式视频生成协议/);
  assert.match(currentComposer.textContent, /保留这段提示词/); assert.equal(sends, 1);
});

test('video creation composer routes through new chat, selects expert, restores prompt and only then sends', async () => {
  let page = 'creation'; let menuOpen = false; let sends = 0;
  const location = { hostname: 'www.dola.com', pathname: '/chat/create-image' };
  const composer = value => {
    const target = node(value, () => {}); target.tagName = 'DIV'; target.isContentEditable = true;
    target.getAttribute = name => name === 'contenteditable' ? 'true' : null; target.dispatchEvent = () => true;
    Object.defineProperty(target, 'innerHTML', { get: () => target.textContent, set(next) {
      target.innerText = target.textContent = String(next).replace(/<[^>]+>/g, ' ').replace(/&amp;/g, '&').replace(/\s+/g, ' ').trim();
    } });
    return target;
  };
  const creationComposer = composer('创作页提示词'); const chatComposer = composer('');
  const newChat = node('新对话', () => { page = 'chat'; location.pathname = '/chat'; });
  newChat.tagName = 'A'; newChat.getAttribute = name => name === 'href' ? '/chat' : null;
  const trigger = node('快速', () => { menuOpen = true; });
  const option = node('专家模式', () => { trigger.innerText = trigger.textContent = '专家模式'; });
  const send = node('发送', () => { sends += 1; });
  const document = new EventTarget(); document.documentElement = { setAttribute() {} };
  document.querySelectorAll = selector => {
    if (selector.includes('contenteditable')) return [page === 'creation' ? creationComposer : chatComposer];
    if (selector.includes('flow-end-msg-send')) return [send];
    if (selector.includes('a[href]')) return page === 'creation' ? [newChat] : [];
    if (page === 'creation') return [];
    return menuOpen ? [trigger, option] : [trigger];
  };
  const context = { document, Event, setTimeout, clearTimeout, location,
    getComputedStyle: () => ({ display: 'block', visibility: 'visible' }), globalThis: null };
  context.globalThis = context; vm.createContext(context); vm.runInContext(source, context);
  assert.equal(await context.__DOLA_SILENT_EXPERT__.selectBeforeSend(send, {
    context: { model: 'seedance_v2.0', duration: 30, ratio: '16:9', resolution: '720p' }
  }), true);
  assert.equal(location.pathname, '/chat'); assert.equal(trigger.textContent, '专家模式');
  assert.match(chatComposer.textContent, /创作页提示词/); assert.equal(sends, 1);
});

test('an early video draft is converted before Dola serializes and signs it', () => {
  const document = new EventTarget(); document.documentElement = { setAttribute() {} }; document.querySelectorAll = () => [];
  const context = { document, Event, setTimeout, clearTimeout, location: { hostname: 'www.dola.com', pathname: '/chat' },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible' }), globalThis: null };
  context.globalThis = context; vm.createContext(context); vm.runInContext(source, context);
  context.__DOLA_SILENT_EXPERT__.arm({ model: 'seedance_v2.0', duration: 30, ratio: '16:9', resolution: '720p' });
  const early = { messages: [{ ext: { chat_ability: JSON.stringify({ ability_type: 17, ability_param: {} }) } }] };
  const prepared = context.__DOLA_SILENT_EXPERT__.prepare(early);
  assert.equal(Object.hasOwn(prepared, 'option'), false);
  assert.equal(context.__DOLA_SILENT_EXPERT__.status.armed, false);
  assert.equal(context.__DOLA_SILENT_EXPERT__.status.last.applied, true);
  assert.equal(Object.hasOwn(prepared.messages[0].ext, 'chat_ability'), false);
  assert.equal(prepared.messages[0].ext.agent_mode, '1');
  assert.equal(JSON.parse(prepared.messages[0].ext.general_task_param).runtime_type, 1);
  assert.equal(JSON.parse(prepared.ext.general_task_param).project_id, '');
  assert.equal(JSON.parse(prepared.ext.conversation_init_option).project_id, '');
  const wire = context.__DOLA_SILENT_EXPERT__.prepare({
    messages: [{ local_message_id: 'wire-id', content_block: [{ block_type: 10000, content: { text_block: { text: '原始提示词' } } }] }],
    option: { aggregate_params: {}, model_config: {}, conversation_init_option: { need_ack_conversation: true } }, ext: {}
  });
  assert.equal(wire.option.general_task_param.project_id, '');
  assert.equal(wire.option.conversation_init_option.project_id, '');
  assert.equal(JSON.parse(wire.ext.general_task_param).project_id, '');
  assert.equal(JSON.parse(wire.ext.conversation_init_option).project_id, '');
  assert.equal(typeof wire.option.conversation_init_ext.agent_task_param, 'string');
  assert.equal(JSON.parse(wire.option.conversation_init_ext.agent_task_param).runtime_type, 1);
  assert.equal(context.__DOLA_SILENT_EXPERT__.status.last.finalizingEarlyDraft, true);
});

test('an early standalone 10052 attachment is merged into the current text message', () => {
  const document = new EventTarget(); document.documentElement = { setAttribute() {} }; document.querySelectorAll = () => [];
  const context = { document, Event, setTimeout, clearTimeout, location: { hostname: 'www.dola.com', pathname: '/chat' },
    getComputedStyle: () => ({ display: 'block', visibility: 'visible' }), globalThis: null };
  context.globalThis = context; vm.createContext(context); vm.runInContext(source, context);
  context.__DOLA_SILENT_EXPERT__.arm({ model: 'seedance_v2.0', duration: 5, ratio: '16:9', resolution: '720p' });
  const attachment = { block_type: 10052, content: { attachment_block: { attachments: [{ type: 1, identifier: 'image' }] } } };
  const textBlock = { block_type: 10000, content: { text_block: { text: '原提示词' } } };
  const prepared = context.__DOLA_SILENT_EXPERT__.prepare({ messages: [
    { local_message_id: 'attachment-id', content_blocks_v2: [attachment], ext: {} },
    { local_message_id: 'text-id', content_blocks_v2: [textBlock], ext: { chat_ability: JSON.stringify({ ability_type: 17 }) } }
  ] });
  assert.equal(prepared.messages.length, 1);
  assert.equal(JSON.stringify(prepared.messages[0].content_blocks_v2.map(block => block.block_type)), '[10052,10000]');
  assert.equal(prepared.messages[0].local_message_id, 'text-id');
  assert.equal(context.__DOLA_SILENT_EXPERT__.status.last.atomicMessageMerge, true);
  assert.equal(context.__DOLA_SILENT_EXPERT__.status.last.mergedAttachmentCount, 1);
});

test('silent native HTTP dispatch does not require or click a mode selector and still arms pre-sign injection', async () => {
  let sends = 0;
  const composer = node('原提示词保持在当前层级', () => {}); composer.tagName = 'DIV'; composer.isContentEditable = true;
  composer.getAttribute = name => name === 'contenteditable' ? 'true' : null;
  const send = node('发送', () => { sends += 1; });
  const document = new EventTarget(); document.documentElement = { setAttribute() {} };
  document.querySelectorAll = selector => selector.includes('contenteditable') ? [composer] : [];
  const context = { document, Event, setTimeout, clearTimeout,
    location: { hostname: 'www.dola.com' }, getComputedStyle: () => ({ display: 'block', visibility: 'visible' }), globalThis: null };
  context.globalThis = context; vm.createContext(context); vm.runInContext(source, context);
  context.__DOLA_SILENT_EXPERT__.arm({ model: 'seedance_v2.0', duration: 30, ratio: '16:9', resolution: '720p', source: 'protocol_button' });
  assert.equal(await context.__DOLA_SILENT_EXPERT__.sendNativeHttp(send, { source: 'protocol_button' }), true);
  assert.equal(sends, 1);
  const request = { option: { aggregate_params: {}, model_config: {} }, messages: [{ role: 'user', text: '原提示词保持在当前层级' }] };
  const output = context.__DOLA_SILENT_EXPERT__.prepare(request);
  assert.equal(output.option.agent_mode, 1); assert.equal(output.option.need_deep_think, 4);
  assert.equal(output.messages[0].text, request.messages[0].text);
});
