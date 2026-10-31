const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'timeline-progress-main.js'), 'utf8');

function environment(site = 'dola') {
  const context = { location: { hostname: `www.${site}.com` } };
  vm.createContext(context); vm.runInContext(source, context);
  return { context, api: context.__DOUBAO_DOLA_TIMELINE_PROGRESS__ };
}

test('screenshot ranges become one contiguous percentage timeline for a 30-second video', () => {
  const { api } = environment();
  const original = '17–22 秒 | 东北式客气\n22–26 秒 | 瞬间变脸\n26–30 秒 | 结尾包袱';
  const expected = '全片进度 56.7%–73.3% | 东北式客气\n全片进度 73.3%–86.7% | 瞬间变脸\n全片进度 86.7%–100% | 结尾包袱';
  assert.equal(api.rewrite(original, 30), expected);
  assert.equal(api.rewrite(expected, 30), expected);
});

test('Arabic, Chinese, decimal and clock ranges keep brackets, shot descriptions and dialogue', () => {
  const { api } = environment();
  for (const [original, expected] of [
    ['[0-3s] 推镜', '[全片进度 0%–10%] 推镜'],
    ['0s-3s 远景', '全片进度 0%–10% 远景'],
    ['0 秒到 5 秒 广角', '全片进度 0%–16.7% 广角'],
    ['十七至二十二秒 “不用，我不饿。”', '全片进度 56.7%–73.3% “不用，我不饿。”'],
    ['[0.5~1.5 seconds] 停顿', '[全片进度 1.7%–5%] 停顿'],
    ['00:00-00:03 建立镜头', '全片进度 0%–10% 建立镜头'],
    ['00：03 至 00：05 跟拍', '全片进度 10%–16.7% 跟拍'],
    ['00:00:26→00:00:30s 收尾', '全片进度 86.7%–100% 收尾']
  ]) assert.equal(api.rewrite(original, 30), expected);
});

test('conversion uses the actual structured duration, including 15-second and 10-second videos', () => {
  const { api } = environment();
  assert.equal(api.rewrite('0-3s；3-15s', 15), '全片进度 0%–20%；全片进度 20%–100%');
  assert.equal(api.rewrite('0-3s', 10), '全片进度 0%–30%');
  assert.equal(api.rewrite('0-3s', 0), '0-3s');
  assert.equal(api.rewrite('0-3s', undefined), '0-3s');
});

test('explicit time points and action durations retain their position-versus-duration meaning', () => {
  const { api } = environment();
  assert.equal(api.rewrite('00:03 转身，第3秒抬头，在二十九点五秒停下，持续半秒。', 30),
    '全片进度约 10% 处 转身，全片进度约 10% 处抬头，在全片进度约 98.3% 处停下，持续约占全片 1.7% 的时长。');
});

test('there is no duration tail or extra control paragraph; unrelated numbers, URLs and authored tails survive', () => {
  const { api } = environment();
  const original = '总时长30秒，16:9，1080P，3个人，镜头35mm。\n参考 https://example.test/00:03/0-3s?duration=30s\n末尾手写30s';
  assert.equal(api.rewrite(original, 30), original);
  assert.equal(api.rewrite('0-3s\n最后一句对白。', 30), '全片进度 0%–10%\n最后一句对白。');
});

test('negative, reversed and out-of-range intervals are not partly converted or silently clamped', () => {
  const { api } = environment();
  for (const original of ['-3--1秒', '5-3秒', '22到17秒', '0-31秒', '00:05-00:03', '00:00-00:31']) {
    assert.equal(api.rewrite(original, 30), original);
  }
});

test('the module installs once on the two intended hosts and leaves other hosts alone', () => {
  for (const site of ['dola', 'doubao']) {
    const { context, api } = environment(site); vm.runInContext(source, context);
    assert.strictEqual(context.__DOUBAO_DOLA_TIMELINE_PROGRESS__, api);
  }
  assert.equal(environment('example').api, undefined);
  const manifest = JSON.parse(fs.readFileSync(path.join(root, 'manifest.json')));
  const entry = manifest.content_scripts.find((item) => item.js.includes('timeline-progress-main.js'));
  assert.deepEqual(entry.matches, ['*://*.doubao.com/*', '*://*.dola.com/*']);
  assert.equal(entry.world, 'MAIN'); assert.equal(entry.run_at, 'document_start');
  assert.ok(entry.js.indexOf('timeline-progress-main.js') < entry.js.indexOf('resolution-native-main.js'));
  assert.equal(manifest.permissions.includes('debugger'), true);
});

test('ordinary chat, history, assistants, malformed abilities and unknown classes are unchanged', () => {
  const { api } = environment('doubao');
  function message(type = 17, role = 'user') {
    return { role, text: '0-3s', ext: { chat_ability: { ability_type: type, ability_param: { duration: 30 } } } };
  }
  for (const request of [{ messages: [message(3)] }, { messages: [message(17, 'assistant')] },
    { messages: [message(), message(3)] }, { messages: [{ text: '0-3s', ext: { chat_ability: 'not-json' } }] }]) {
    assert.strictEqual(api.prepare(request), request);
  }
  class CustomRequest { constructor() { this.messages = [message()]; } }
  const custom = new CustomRequest(); assert.strictEqual(api.prepare(custom), custom);
  const history = message(); const assistant = message(17, 'assistant');
  const request = { messages: [history, message(), assistant] }; const next = api.prepare(request);
  assert.strictEqual(next.messages[0], history); assert.strictEqual(next.messages[2], assistant);
  assert.equal(next.messages[1].text, '全片进度 0%–10%');
  assert.strictEqual(api.prepare(next), next);
});

test('legacy JSON text is kept encoded and only known text blocks change; attachments and reference names survive', () => {
  const { api } = environment('doubao');
  const attachment = { block_type: 10052, content: { text: '0-3s', attachment_block: { name: '0-3s.png', duration: 3 } } };
  const request = { messages: [{ content: JSON.stringify({ text: '0-3s', images: ['0-3s.png'], extra: 7 }),
    content_blocks_v2: [{ block_type: 10000, content: { text_block: { text: '3-5s' } } }, attachment],
    ext: { chat_ability: { ability_type: 17, ability_param: { duration: 30 } } } }] };
  const before = JSON.stringify(request); const next = api.prepare(request);
  assert.deepEqual(JSON.parse(next.messages[0].content), { text: '全片进度 0%–10%', images: ['0-3s.png'], extra: 7 });
  assert.equal(next.messages[0].content_blocks_v2[0].content.text_block.text, '全片进度 10%–16.7%');
  assert.strictEqual(next.messages[0].content_blocks_v2[1], attachment);
  assert.equal(JSON.stringify(request), before);
});
