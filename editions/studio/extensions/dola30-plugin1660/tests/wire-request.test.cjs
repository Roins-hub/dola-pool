const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const crypto = require('node:crypto');
const root = path.resolve(__dirname, '..');
const clean = value => JSON.parse(JSON.stringify(value));
function environment() {
  const document = new EventTarget(); const attrs = new Map();
  document.documentElement = { setAttribute: (key, value) => attrs.set(key, value), getAttribute: key => attrs.get(key) };
  const context = { document, Event, URL, TextEncoder, crypto: crypto.webcrypto, location: { hostname: 'www.doubao.com' }, localStorage: { getItem: () => null },
    fetch() { throw Error('Schema tests must never make a platform request'); } };
  vm.createContext(context);
  for (const file of ['video-request-schema.js', 'doubao-generation-intent-main.js', 'debugger-summary.js']) vm.runInContext(fs.readFileSync(path.join(root, file), 'utf8'), context);
  return { context, schema: context.__DOUBAO_DOLA_VIDEO_REQUEST_SCHEMA__, intent: context.__DOUBAO_GENERATION_INTENT__, summary: context.__DOUBAO_DOLA_NETWORK_SUMMARY__ };
}
// Synthetic offline body: the top-level keys match the uploaded 1.6.30
// observation. The export did not retain the real message/ability body values.
function wire({ encoded = false, representation = 'json', prefix = '' } = {}) {
  const content = [{ block_type: 10000, content: { text_block: { text: prefix + '私人原稿 @参考图' } } },
    { block_type: 2, content: { attachment_block: { url: 'https://ref.test?token=private' } } }];
  let body = representation === 'array' ? content : JSON.stringify(content);
  if (representation === 'unicode') body = body.replace(/[^\x00-\x7f]/g, char => '\\u' + char.charCodeAt(0).toString(16).padStart(4, '0'));
  const params = { model: 'seedance_v2.5', duration: 30, resolution: '1080p', ratio: '16:9',
    input_box_content: JSON.stringify({ user_input_content: prefix + '私人原稿 @参考图', reply_message_format: '生成视频：%s' }) };
  const ability = { ability_type: 17, ability_param: encoded ? JSON.stringify(params) : params };
  return { client_meta: { privateId: 'keep' }, messages: [{ content_type: 2001, content: body }], option: {},
    chat_ability: encoded ? JSON.stringify(ability) : ability, user_context: {}, ext: {} };
}

for (const encoded of [false, true]) for (const representation of ['array', 'json', 'unicode']) {
  test(`wire top-level ${encoded ? 'encoded' : 'object'} ability and ${representation} content are observed without mutating signed data`, () => {
    const env = environment(); const body = wire({ encoded, representation, prefix: env.intent.prefix });
    const before = JSON.stringify(body); const inspected = clean(env.schema.inspect(body));
    assert.equal(inspected.videoRecognized, true); assert.equal(inspected.abilitySource, 'request.chat_ability'); assert.equal(inspected.shape, 'wire');
    assert.equal(inspected.video.model, 'seedance_v2.5'); assert.equal(inspected.video.duration, 30); assert.equal(inspected.video.resolution, '1080p');
    assert.deepEqual(inspected.textBlockTypes, [10000, 2]); assert.equal(inspected.prefixPresent, true); assert.equal(inspected.prefixChecked, true);
    assert.deepEqual(clean(env.intent.inspect(body)), { video: true, present: true, inputBoxPresent: true });
    const summary = clean(env.summary.request(before)); assert.equal(summary.video.model, 'seedance_v2.5'); assert.equal(summary.prefixMarkerPresent, true);
    assert.ok(summary.messageKeys.includes('content')); assert.equal(summary.abilityType, 17); assert.equal(summary.abilitySource, 'request.chat_ability');
    assert.equal(JSON.stringify(body), before); assert.doesNotMatch(JSON.stringify(summary), /私人原稿|参考图|privateId|ref.test/);
    assert.strictEqual(env.intent.prepare(body), body); // The pre-sign mutator stays limited to its known native draft schema.
  });
}

test('a prefix in an attachment is not attributed to unprefixed message text, and unknown text structures cannot prove absence', () => {
  const env = environment(); const body = wire({ representation: 'array' });
  body.messages[0].content[1].text = env.intent.prefix;
  assert.equal(env.schema.inspect(body).prefixPresent, false);
  body.messages[0] = { future_payload: { text: env.intent.prefix } };
  const unknown = clean(env.schema.inspect(body)); assert.equal(unknown.videoRecognized, true); assert.equal(unknown.prefixPresent, null);
  assert.equal(unknown.prefixChecked, false); assert.ok(unknown.unknownTextSources.includes('messages[]'));
  assert.deepEqual(clean(env.intent.inspect(body)), { video: true, present: null, inputBoxPresent: false });
});

test('an explicit unknown top-level ability is reported unknown, rather than borrowing a historical or nested video ability', () => {
  const env = environment(); const body = wire(); body.messages[0].ext = { chat_ability: body.chat_ability };
  body.chat_ability = { future_ability: {} };
  let result = env.schema.inspect(body); assert.equal(result.videoRecognized, null); assert.equal(result.abilitySource, 'request.chat_ability');
  body.chat_ability = { ability_type: 1, ability_param: {} }; result = env.schema.inspect(body); assert.equal(result.videoRecognized, false);
  body.messages.push({ role: 'assistant', content: env.intent.prefix }); assert.equal(env.schema.inspect(body).prefixPresent, false);
});

test('the input-box mirror can establish presence independently, but does not establish absence in an unknown body', () => {
  const env = environment(); const body = wire({ prefix: env.intent.prefix }); body.messages[0] = { future: {} };
  assert.equal(env.schema.inspect(body, env.intent.prefix).prefixPresent, true);
  const absent = wire(); absent.messages[0] = { future: {} }; assert.equal(env.schema.inspect(absent, env.intent.prefix).prefixPresent, null);
});

test('late SSE message structures and event names survive a full ACK schema without exposing conversation or message content', () => {
  const env = environment(); const ack = { ack_client_meta: Object.fromEntries(Array.from({ length: 120 }, (_, i) => ['field_' + i, 'private-value'])) };
  const message = { message: { content: JSON.stringify([{ block_type: 10000, content: { text_block: { text: '私人回复' } } }]),
    content_block: [{ block_type: 10000, is_finish: true, content: { text_block: { text: '私人回复' } } }], user_type: 2, content_type: 2001 } };
  const raw = `event: ACK\ndata: ${JSON.stringify(ack)}\n\nevent: MESSAGE\ndata: ${JSON.stringify(message)}\n\n`;
  const result = clean(env.summary.response(raw));
  assert.equal(result.parsedObjects, 2); assert.equal(result.schemaTruncated, true); assert.equal(result.events.length, 2);
  assert.equal(result.events[1].event, 'MESSAGE'); assert.deepEqual(result.events[1].blockTypes, [10000]);
  assert.ok(result.detailSchema.includes('$.message.content<json>[].content.text_block.text'));
  assert.ok(result.events[1].flowFields.some(field => field.path === '$.message.user_type' && field.value === 2));
  assert.deepEqual(result.taskFields, []); assert.equal(result.verification, 'UNVERIFIED');
  assert.doesNotMatch(JSON.stringify(result), /私人回复|private-value/);
});

test('quota summary preserves actual entitlement booleans and usage values only for that endpoint', () => {
  const env = environment(); const raw = JSON.stringify({ code: 0, data: { member_info: { hasActiveSubscription: false, hasEnterpriseSubscription: false },
    current_subscription: { trial_info: { is_trialing: false }, subscription_id: 'private-subscription' },
    window_limit_section: { usage_exhausted: true, entitlement_count: 0, window_limit_groups: [
      { feature_group: 'video_generation', feature_group_name: '视频生成', should_prompt_purchase: true, window_limits: [{ used_percent: 100, window_type: 'daily' }] },
      { feature_group: 'image_generation', window_limits: [{ used_percent: 0 }] }
    ] } } });
  const result = clean(env.summary.response(raw, 'https://www.doubao.com/alice/commerce/sale/subscription/quota/summary/'));
  assert.ok(result.entitlementFields.some(field => field.path === '$.data.member_info.hasActiveSubscription' && field.value === false));
  assert.ok(result.entitlementFields.some(field => field.path.endsWith('.usage_exhausted') && field.value === true));
  assert.ok(result.entitlementFields.some(field => field.path.endsWith('.used_percent') && field.value === 100));
  assert.ok(result.entitlementFields.some(field => field.path === '$.data.window_limit_section.window_limit_groups[0].window_limits[0].used_percent' && field.value === 100));
  assert.ok(result.entitlementFields.some(field => field.path === '$.data.window_limit_section.window_limit_groups[1].window_limits[0].used_percent' && field.value === 0));
  assert.equal(result.verification, 'UNVERIFIED'); assert.doesNotMatch(JSON.stringify(result), /private-subscription/);
  assert.deepEqual(clean(env.summary.response(raw, 'https://www.doubao.com/chat/completion')).entitlementFields, []);
});

test('1260 events are all analyzed, retaining the beginning and end instead of dropping fields after event 256', () => {
  const env = environment(); const frames = ['event: SSE_ACK\ndata: {"ack_client_meta":{}}\n\n'];
  for (let i = 0; i < 1258; i++) frames.push('event: CHUNK_DELTA\ndata: {"text":"任务已受理 task_id=fake 消耗999点"}\n\n');
  frames.push('event: SSE_REPLY_END\ndata: {"end_type":0,"msg_finish_attr":{"video_task":{"task_id":"native-final","consumed_points":2}}}\n\n');
  const result = clean(env.summary.response(frames.join('')));
  assert.equal(result.parsedObjects, 1260); assert.equal(result.truncated, false); assert.equal(result.eventsTruncated, true);
  assert.equal(result.events.length, 32); assert.equal(result.events[0].index, 1); assert.equal(result.events[15].index, 16);
  assert.equal(result.events[16].index, 1245); assert.equal(result.events.at(-1).index, 1260); assert.equal(result.events.at(-1).event, 'SSE_REPLY_END');
  assert.equal(result.eventCounts.CHUNK_DELTA, 1258); assert.equal(result.eventCounts.SSE_REPLY_END, 1);
  assert.ok(result.events.at(-1).flowFields.some(field => field.path === '$.end_type' && field.value === 0));
  assert.equal(result.taskFields.length, 1); assert.equal(result.billingFields.length, 1); assert.equal(result.billingFields[0].value, 2);
  assert.equal(result.verification, 'UNVERIFIED'); assert.doesNotMatch(JSON.stringify(result), /native-final|fake|任务已受理/);
});

test('comparison hashes ignore transient message identifiers and property order, while preserving conversation differences separately', async () => {
  const env = environment(); const first = wire(); first.client_meta = { conversation_id: 'first-private-conversation' };
  first.messages[0].local_message_id = '1'; first.option = { cot_switch: false, request_id: 'a' };
  const second = clean(first); second.messages[0].local_message_id = 'longer-id'; second.option = { request_id: 'different', cot_switch: false };
  second.client_meta.conversation_id = 'second-private-conversation';
  const params = second.chat_ability.ability_param;
  second.chat_ability.ability_param = Object.fromEntries(Object.entries(params).reverse());
  const before = JSON.stringify(first); const left = clean(await env.summary.compareRequest(before)); const right = clean(await env.summary.compareRequest(JSON.stringify(second)));
  assert.equal(left.hashes.prompt.length, 64);
  for (const key of ['prompt', 'inputBox', 'video', 'ability', 'option']) assert.equal(left.hashes[key], right.hashes[key]);
  assert.equal(left.hashes.ability.length, 64);
  assert.notEqual(left.identifiers[0].sha256, right.identifiers[0].sha256);
  assert.equal(left.promptCoverageComplete, true); assert.equal(left.algorithm, 'SHA-256');
  assert.doesNotMatch(JSON.stringify(left), /私人原稿|first-private-conversation/); assert.equal(JSON.stringify(first), before);
});

test('comparison records semantic route switches and text differences without exporting prose or credentials', async () => {
  const env = environment(); const first = wire(); first.option = { cot_switch: false, token: 'private-token', custom: 'private-custom-text' };
  first.ext = { general_task_param: JSON.stringify({ is_general_task: true }), bot_id: 'private-bot' };
  const second = clean(first); second.option.cot_switch = true; second.option.token = 'different-token';
  const left = clean(await env.summary.compareRequest(JSON.stringify(first))); const right = clean(await env.summary.compareRequest(JSON.stringify(second)));
  assert.equal(left.hashes.prompt, right.hashes.prompt); assert.notEqual(left.hashes.option, right.hashes.option);
  assert.ok(right.routingFields.some(field => field.path === 'option.cot_switch' && field.value === true));
  assert.ok(right.routingFields.some(field => field.path === 'ext.general_task_param.is_general_task' && field.value === true));
  assert.ok(right.identifiers.some(field => field.path === 'ext.bot_id' && field.sha256.length === 64));
  const onlyCredential = clean(first); onlyCredential.option.token = 'new-token';
  assert.equal(left.hashes.option, (await env.summary.compareRequest(JSON.stringify(onlyCredential))).hashes.option);
  const differentText = wire({ prefix: 'different ' }); assert.notEqual(left.hashes.prompt, (await env.summary.compareRequest(JSON.stringify(differentText))).hashes.prompt);
  assert.doesNotMatch(JSON.stringify(left), /private-token|private-custom-text|private-bot|私人原稿/);
});

test('leaf comparisons locate an unlisted encoded option difference and distinguish identifiers without retaining private strings', async () => {
  const env = environment(); const first = wire();
  first.option = { general_task_param: JSON.stringify({ router: { mode: 'native_video', revision: 3 } }),
    conversation_id: 'private-conversation', timestamp: 100, token: 'private-token', custom: 'private-prose' };
  const second = clean(first); second.option.general_task_param = JSON.stringify({ router: { mode: 'native_text', revision: 3 } });
  const a = clean(await env.summary.compareRequest(JSON.stringify(first)));
  const b = clean(await env.summary.compareRequest(JSON.stringify(second)));
  const left = a.fieldDetails.fields.find(f => f.path === 'option.general_task_param.router.mode');
  const right = b.fieldDetails.fields.find(f => f.path === left.path);
  assert.notEqual(left.sha256, right.sha256); assert.equal(left.type, 'string'); assert.equal(left.value, 'native_video');
  assert.equal(a.fieldDetails.complete, true); assert.equal(a.fieldDetails.omittedSecrets, 1);
  assert.equal(a.fieldDetails.fields.find(f => f.path === 'option.conversation_id').category, 'identifier');
  assert.equal(a.fieldDetails.fields.find(f => f.path === 'option.timestamp').category, 'transient');
  assert.equal(a.fieldDetails.fields.find(f => f.path.endsWith('.revision')).value, 3);
  assert.doesNotMatch(JSON.stringify(a), /private-conversation|private-token|private-prose|私人原稿/);
});

test('reply template diagnostics retain only structure and flags, while limited leaf coverage is explicitly incomplete', async () => {
  const env = environment(); const request = wire();
  request.chat_ability.ability_param.input_box_content = JSON.stringify({ user_input_content: '私人输入',
    reply_message_format: '请确认视频分镜后生成：%s' });
  const result = clean(await env.summary.compareRequest(JSON.stringify(request)));
  assert.equal(result.replyFormat.mentionsConfirmation, true); assert.equal(result.replyFormat.mentionsScript, true);
  assert.equal(result.replyFormat.mentionsVideo, true); assert.equal(result.replyFormat.hasPlaceholder, true);
  assert.equal(result.replyFormat.sha256.length, 64); assert.doesNotMatch(JSON.stringify(result), /请确认视频分镜后生成|私人输入/);
  request.option = Object.fromEntries(Array.from({ length: 400 }, (_, i) => ['flag_' + i, false]));
  const limited = clean(await env.summary.compareRequest(JSON.stringify(request)));
  assert.equal(limited.fieldDetails.complete, false); assert.equal(limited.fieldDetails.fields.length, 256);
});

test('field details skip unsafe key paths and preserve null and empty containers without inventing complete coverage', async () => {
  const env = environment(); const request = wire();
  request.option = { 'untrusted.key': 'private-value', empty: {}, absent: null, nested: [], api_key: 'private-key' };
  const result = clean(await env.summary.compareRequest(JSON.stringify(request)));
  assert.equal(result.fieldDetails.complete, false);
  const fields = result.fieldDetails.fields;
  assert.equal(fields.find(f => f.path === 'option.absent').type, 'null');
  assert.equal(fields.find(f => f.path === 'option.empty').type, 'object');
  assert.equal(fields.find(f => f.path === 'option.nested').type, 'array');
  assert.doesNotMatch(JSON.stringify(result), /untrusted.key|private-value|private-key/);
});
