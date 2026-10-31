const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'doubao-generation-intent-main.js'), 'utf8');
function environment({ site = 'doubao', disabled = false, brokenStorage = false } = {}) {
  const attrs = new Map(); const storage = new Map(disabled ? [['intl_doubao_generation_intent_v1', '0']] : []);
  const document = new EventTarget();
  document.documentElement = { setAttribute: (key, value) => attrs.set(key, value), getAttribute: key => attrs.get(key) ?? null };
  const context = { document, Event, location: { hostname: `www.${site}.com` }, localStorage: {
    getItem(key) { if (brokenStorage) throw Error('unavailable'); return storage.get(key) ?? null; },
    setItem(key, value) { storage.set(key, value); }
  }, fetch() { throw Error('An intent prefix must not send HTTP requests'); } };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'video-request-schema.js'), 'utf8'), context);
  vm.runInContext(source, context);
  return { context, document, storage, api: context.__DOUBAO_GENERATION_INTENT__ };
}
function draft({ encoded = true, boxEncoded = true, role = 'user', type = 17 } = {}) {
  const text = '生成视频：视频生成参数确认 模型：Seedance 2.5\n私人剧情 @图一 [0-3s] 推镜';
  const box = { user_input_content: text, reply_message_format: '生成视频：%s', reference: 'keep.jpg' };
  const params = { model: 'seedance_v2.5', duration: 30, resolution: '1080p', ratio: '16:9',
    input_box_content: boxEncoded ? JSON.stringify(box) : box };
  const ability = { ability_type: type, ability_param: encoded ? JSON.stringify(params) : params };
  return { messages: [{ role, content: text, content_blocks_v2: [
    { block_type: 10000, content: { text_block: { text, custom: 'keep' } } },
    { block_type: 2, content: { attachment_block: { name: '私人素材.png', url: 'https://ref.test/private' } } }
  ], ext: { chat_ability: encoded ? JSON.stringify(ability) : ability, other: 'keep' } }], custom: 'keep' };
}
function params(request) {
  const raw = request.messages.at(-1).ext.chat_ability; const ability = typeof raw === 'string' ? JSON.parse(raw) : raw;
  return typeof ability.ability_param === 'string' ? JSON.parse(ability.ability_param) : ability.ability_param;
}

for (const encoded of [false, true]) for (const boxEncoded of [false, true]) {
  test(`intent updates message mirrors and ${boxEncoded ? 'encoded' : 'object'} input box before ${encoded ? 'encoded' : 'object'} ability serialization`, () => {
    const { api } = environment(); const input = draft({ encoded, boxEncoded }); const before = JSON.stringify(input);
    const output = api.prepare(input); const prefix = api.prefix; const original = input.messages[0];
    assert.equal(output.messages[0].content, prefix + original.content);
    assert.equal(output.messages[0].content_blocks_v2[0].content.text_block.text, prefix + original.content);
    assert.strictEqual(output.messages[0].content_blocks_v2[1], original.content_blocks_v2[1]);
    const nextParams = params(output); const box = typeof nextParams.input_box_content === 'string' ? JSON.parse(nextParams.input_box_content) : nextParams.input_box_content;
    assert.equal(box.user_input_content, prefix + original.content); assert.equal(box.reply_message_format, '生成视频：%s');
    assert.equal(box.reference, 'keep.jpg'); assert.equal(nextParams.duration, 30); assert.equal(nextParams.resolution, '1080p');
    assert.equal(nextParams.model, 'seedance_v2.5'); assert.equal(nextParams.ratio, '16:9');
    assert.equal(typeof nextParams.input_box_content, boxEncoded ? 'string' : 'object');
    assert.equal(JSON.stringify(input), before); assert.equal(api.inspect(output).present, true);
    assert.strictEqual(api.prepare(output), output); assert.equal(api.status.preparedCount, 1);
    assert.equal(api.status.last.added, false); assert.equal(api.status.last.present, true);
    assert.doesNotMatch(JSON.stringify(api.status), /私人剧情|私人素材|ref.test/);
  });
}

test('only the latest video message is modified; a newer ordinary, assistant, system or tool message prevents history modification', () => {
  for (const role of ['user', 'assistant', 'system', 'tool', 'bot']) {
    const { api } = environment(); const input = draft(); input.messages.push({ role, text: 'new ordinary message' });
    assert.strictEqual(api.prepare(input), input); assert.equal(api.inspect(input).present, false);
  }
  const { api } = environment(); const input = draft(); const historical = { role: 'assistant', text: 'leave this unchanged' };
  input.messages.unshift(historical); const output = api.prepare(input);
  assert.strictEqual(output.messages[0], historical); assert.match(output.messages[1].content, /【视频生成请求】/);
});

test('non-video, malformed and unsupported message schemas are preserved and never claim a prefix was added', () => {
  const { api } = environment();
  for (const input of [null, {}, { messages: [] }, draft({ type: 1 }), { messages: [{ ext: { chat_ability: '{invalid' } }] },
    { messages: [{ ext: { chat_ability: { ability_type: 17, ability_param: {} } }, metadata: { text: 'not a prompt' } }] }]) {
    assert.strictEqual(api.prepare(input), input); assert.equal(api.status.last.added, false); assert.notEqual(api.status.last.present, true);
  }
});

test('multiple textual blocks receive one leading instruction, while attachments, later text and unrelated fields survive', () => {
  const { api } = environment(); const input = draft(); const later = { block_type: 1, text: 'second paragraph' };
  input.messages[0].content_blocks_v2.unshift({ block_type: 1, text: ' ' }); input.messages[0].content_blocks_v2.push(later);
  const output = api.prepare(input);
  assert.strictEqual(output.messages[0].content_blocks_v2[0], input.messages[0].content_blocks_v2[0]);
  assert.strictEqual(output.messages[0].content_blocks_v2.at(-1), later);
  assert.equal(output.messages[0].content_blocks_v2.filter(block => JSON.stringify(block).includes('【视频生成请求】')).length, 1);
  assert.equal(output.custom, 'keep'); assert.equal(output.messages[0].ext.other, 'keep');
});

test('JSON text envelopes remain parseable and unknown JSON, blank text and attachment text are not reinterpreted', () => {
  const { api } = environment(); const input = draft();
  input.messages[0].content = JSON.stringify({ text: 'story', private_metadata: 'preserve' });
  input.messages[0].content_blocks_v2 = [{ block_type: 2, text: 'attachment description' }];
  const output = api.prepare(input); assert.deepEqual(JSON.parse(output.messages[0].content), { text: api.prefix + 'story', private_metadata: 'preserve' });
  assert.strictEqual(output.messages[0].content_blocks_v2, input.messages[0].content_blocks_v2);
  for (const content of [' ', '{"unknown":"preserve"}', '[]']) {
    const minimal = { messages: [{ content, ext: { chat_ability: { ability_type: 17, ability_param: {} } } }] };
    assert.strictEqual(api.prepare(minimal), minimal); assert.notEqual(api.status.last.present, true);
  }
});

test('preference is default on, can be disabled and re-enabled, and unavailable storage makes no changes', () => {
  const env = environment(); assert.equal(env.api.status.enabled, true);
  env.document.documentElement.setAttribute('data-watermark-free-generation-intent-enabled', '0');
  env.document.dispatchEvent(new Event('watermark-free-generation-intent-change'));
  const input = draft(); assert.strictEqual(env.api.prepare(input), input); assert.equal(env.api.status.enabled, false);
  env.document.documentElement.setAttribute('data-watermark-free-generation-intent-enabled', '1');
  env.document.dispatchEvent(new Event('watermark-free-generation-intent-change'));
  assert.notStrictEqual(env.api.prepare(input), input);
  for (const options of [{ disabled: true }, { brokenStorage: true }]) {
    const off = environment(options); assert.strictEqual(off.api.prepare(input), input); assert.equal(off.api.status.enabled, false);
  }
});

test('Dola does not install the prefix; repeated installation is idempotent', () => {
  assert.equal(environment({ site: 'dola' }).api, undefined);
  const env = environment(); vm.runInContext(source, env.context); assert.strictEqual(env.context.__DOUBAO_GENERATION_INTENT__, env.api);
});

test('a quoted old prefix in history cannot make an unprefixed current request appear prefixed', () => {
  const { api } = environment(); const historical = api.prepare(draft()).messages[0];
  const input = draft(); input.messages.unshift(historical);
  assert.equal(api.inspect(input).present, false); assert.equal(api.inspect(api.prepare(input)).present, true);
  assert.doesNotMatch(api.prefix, /已受理|请回复.*生成中|开通成功|合法能力/);
});
