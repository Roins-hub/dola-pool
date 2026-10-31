const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');
const path = 'web/dola-fast15-native.js';
const source = fs.existsSync(path) ? fs.readFileSync(path, 'utf8') : '';

function fixture({readOnly = false, missingPipeline = false, missingStore = false, renamed = false, poison = false, hostname = 'www.dola.com', model = 'seedance_v2.0', duration = 15} = {}) {
  const records = {
    'video-model': {value: 'seedance_v2.5', submitKey: 'model', messageText: 'old model', extra: 'keep'},
    'video-duration': {value: '10', submitKey: 'duration', messageText: '10 seconds'},
    'video-ratio': {value: '9:16', submitKey: 'ratio'}
  };
  let writes = 0;
  let input = {qM(key, value) { writes++; if (!readOnly) records[key] = value; }, gl(key) { return records[key]?.value; }, N5(key) { return records[key]; }};
  if (renamed) {
    // The read/write exports and their bodies observed in Dola's rM module on 2026-10-20.
    const u = '__input_action_control_values__';
    const store = {actionValue: {...Object.fromEntries(Object.entries(records).map(([key, value]) => [key, value.value])), [u]: records}};
    const i = {b: () => store, c: () => ({setState(update) {
      writes++; if (!readOnly) Object.assign(store, update(store));
    }})};
    const d = e => 'object' === typeof e && null !== e && !Array.isArray(e);
    const p = () => {let e = (0,i.b)('chatInputStore').actionValue[u]; return d(e) ? e : {};};
    input = {a: u, f: e => e ? p()[e] : void 0,
      h: e => (0,i.b)('chatInputStore').actionValue[null != e ? e : ''],
      n: (e,t) => {e && (0,i.c)('chatInputStore').setState(n => {let {actionValue:a} = n;
        return {actionValue: {...a, [e]: t.value, [u]: {...d(a[u]) ? a[u] : {}, [e]: t}}};});}};
    // Record writes remain observable through the fixture, without exposing the whole live store.
    input.snapshot = key => store.actionValue[u][key];
  }
  const node = {id: 'video-creation:input-skill-node', execute(context, suffix) {
    return {...context, marker: this.marker, suffix, content: {...context.content,
      abilityParams: {model: 'seedance_v2.5', duration: 10, ratio: '9:16', references: ['original-reference']}}};
  }};
  let pipelineExports = {nativePipeline: {submitNodes: [node]}};
  function runtime(id) { if (id === 'bad') throw new Error('lazy chunk unavailable'); return id === 'input' ? input : pipelineExports; }
  runtime.m = {
    ...(poison && {bad: function () { /* __input_action_control_values__ chatInputStore */ }}),
    ...(!missingStore && {input: function () { /* __input_action_control_values__ chatInputStore */ }}),
    ...(!missingPipeline && {pipeline: function () { /* video-creation:input-skill-node */ }})
  };
  const chunks = [];
  chunks.push = value => { value[2](runtime); return 1; };
  const network = () => { throw new Error('The bridge must not send network requests'); };
  const context = {location: {hostname}, __LOADABLE_LOADED_CHUNKS__: chunks,
    __DOLA_NATIVE_MODEL_TARGET__: model, __DOLA_NATIVE_DURATION_TARGET__: duration,
    fetch: network, XMLHttpRequest: network};
  context.window = context;
  vm.runInNewContext(source, context);
  return {context, records, input, node, network, runtime, writes: () => writes,
    pipelines: () => pipelineExports, replacePipelines: value => { pipelineExports = value; }};
}
function videoContext() {
  return {trace: 'preserve', content: {abilityType: 17, text: '用户原始提示词',
    references: ['original-reference'], actionBarControlParams: {'video-model': 'seedance_v2.0', 'video-duration': '15', 'video-ratio': '9:16'},
    actionBarControlValues: {'video-model': {value: 'seedance_v2.0', submitKey: 'model', messageText: ''},
      'video-duration': {value: '15', submitKey: 'duration', messageText: ''}}}};
}
function prepare(fixture) {
  const api = fixture.context.__DOLA_FAST_NATIVE__ || fixture.context.__DOLA_FAST15_NATIVE__;
  assert.ok(api, 'Fast must expose native preparation before Enter');
  return api.prepare();
}
test('writes and reads back Fast15 native records without touching ratio or transport', () => {
  const f = fixture(); const state = prepare(f);
  assert.equal(state.ready, true);
  assert.equal(state.model, 'seedance_v2.0'); assert.equal(state.duration, 15);
  assert.deepEqual(JSON.parse(JSON.stringify(f.records['video-model'])), {value: 'seedance_v2.0', submitKey: 'model', messageText: '', extra: 'keep'});
  assert.equal(f.records['video-duration'].value, '15'); assert.equal(f.records['video-duration'].messageText, '');
  assert.equal(f.records['video-ratio'].value, '9:16');
  assert.equal(f.context.fetch, f.network); assert.equal(f.context.XMLHttpRequest, f.network);
  const execute = f.node.execute; prepare(f); assert.equal(f.node.execute, execute, 'Repeated preparation must not stack wrappers');
});
test('Fast30 stays Fast and carries 30 through native store and pre-sign node', () => {
  const f = fixture({duration: 30});
  const state = prepare(f);
  assert.equal(state.ready, true); assert.equal(state.model, 'seedance_v2.0'); assert.equal(state.duration, 30);
  assert.equal(f.records['video-duration'].value, '30');
  const context = videoContext();
  context.content.actionBarControlParams['video-duration'] = '30';
  context.content.actionBarControlValues['video-duration'].value = '30';
  const result = f.node.execute(context);
  assert.equal(result.content.abilityParams.model, 'seedance_v2.0');
  assert.equal(result.content.abilityParams.duration, 30);
  assert.equal(result.content.text, '用户原始提示词');
  assert.equal(result.content.abilityParams.ratio, '9:16');
  assert.equal(f.context.fetch, f.network);
  f.records['video-duration'].value = '15';
  assert.equal(f.context.__DOLA_FAST_NATIVE__.check().ready, false);
  assert.throws(() => f.node.execute(context), /Fast30/);
});
test('pre-sign node corrects native defaults, preserving text, references, this and extra arguments', () => {
  const f = fixture(); prepare(f); const original = videoContext();
  const result = f.node.execute.call({marker: 'receiver'}, original, 'argument');
  assert.equal(result.marker, 'receiver'); assert.equal(result.suffix, 'argument');
  assert.equal(result.content.abilityParams.model, 'seedance_v2.0'); assert.equal(result.content.abilityParams.duration, 15);
  assert.equal(result.content.text, '用户原始提示词'); assert.deepEqual(result.content.abilityParams.references, ['original-reference']);
  assert.equal(result.content.abilityParams.ratio, '9:16');
  assert.equal(original.content.abilityParams, undefined, 'Do not mutate the original context');
});
test('Promise outputs are checked before the native pipeline continues', async () => {
  const f = fixture(); const execute = f.node.execute;
  f.node.execute = function (...args) { return Promise.resolve(execute.apply(this, args)); };
  prepare(f); const result = await f.node.execute(videoContext());
  assert.equal(result.content.abilityParams.model, 'seedance_v2.0'); assert.equal(result.content.abilityParams.duration, 15);
});
test('unsupported output and a selection changed after preparation stop before the next node', () => {
  const f = fixture(); f.node.execute = context => context; prepare(f);
  assert.throws(() => f.node.execute(videoContext()), /Fast15/);
  const second = fixture(); prepare(second); const changed = videoContext();
  changed.content.actionBarControlParams['video-model'] = 'seedance_v2.0_std';
  assert.throws(() => second.node.execute(changed), /Fast15/);
});
test('read-only store or missing pipeline cannot report ready', () => {
  for (const options of [{readOnly: true}, {missingPipeline: true}]) assert.equal(prepare(fixture(options)).ready, false);
});

test('observed renamed native store exports keep Fast30 and its pre-sign pipeline', () => {
  const f = fixture({renamed: true, duration: 30});
  const beforeRatio = f.input.snapshot('video-ratio');
  const state = prepare(f);
  assert.equal(state.ready, true, 'The observed n/h/f store replaces the old qM/gl/N5 exports');
  assert.equal(state.pipelineReady, true); assert.equal(state.reasonCode, '');
  assert.equal(state.model, 'seedance_v2.0'); assert.equal(state.duration, 30);
  assert.equal(f.input.snapshot('video-model').value, 'seedance_v2.0');
  assert.equal(f.input.snapshot('video-duration').value, '30');
  assert.equal(f.input.snapshot('video-ratio'), beforeRatio);
  const original = videoContext();
  original.content.actionBarControlParams['video-duration'] = '30';
  original.content.actionBarControlValues['video-duration'].value = '30';
  const result = f.node.execute(original);
  assert.equal(result.content.abilityParams.model, 'seedance_v2.0');
  assert.equal(result.content.abilityParams.duration, 30);
  assert.equal(result.content.text, original.content.text);
  assert.deepEqual(result.content.abilityParams.references, ['original-reference']);
  assert.equal(result.content.abilityParams.ratio, '9:16');
  assert.equal(f.context.fetch, f.network); assert.equal(f.context.XMLHttpRequest, f.network);
  const writes = f.writes(); prepare(f); assert.equal(f.writes(), writes);
});

test('renamed read-only, missing and unsupported stores return safe failure codes', () => {
  for (const [options, code] of [
    [{renamed: true, readOnly: true}, 'selection_readback_failed'],
    [{missingStore: true}, 'input_store_unavailable'],
    [{missingPipeline: true}, 'video_pipeline_unavailable'],
  ]) {
    const state = prepare(fixture(options));
    assert.equal(state.ready, false); assert.equal(state.reasonCode, code);
  }
  const unsupported = fixture({renamed: true}); unsupported.input.a = 'different-record-store';
  assert.equal(prepare(unsupported).reasonCode, 'input_store_unavailable');
  assert.equal(unsupported.writes(), 0);
  const unknownWriter = fixture({renamed: true}); let calls = 0;
  unknownWriter.input.n = () => { calls++; throw new Error('An unrecognized setter must never be called'); };
  assert.equal(prepare(unknownWriter).reasonCode, 'input_store_unavailable');
  assert.equal(calls, 0);
});

test('runtime, record, installation and unexpected failures expose stable codes only', () => {
  const noRuntime = fixture(); noRuntime.context.__LOADABLE_LOADED_CHUNKS__ = [];
  assert.equal(prepare(noRuntime).reasonCode, 'runtime_unavailable');
  const noRecord = fixture(); noRecord.input.N5 = () => null;
  assert.equal(prepare(noRecord).reasonCode, 'record_unavailable');
  const frozen = fixture(); Object.defineProperty(frozen.node, 'execute', {writable: false});
  assert.equal(prepare(frozen).reasonCode, 'pipeline_read_only');
  const broken = fixture(); broken.input.N5 = () => {
    throw Object.assign(new Error('cookie=private-fixture-token'), {nativeReasonCode: 'cookie=private-fixture-token'});
  };
  const state = prepare(broken);
  assert.equal(state.reasonCode, 'native_prepare_failed');
  assert.ok(!JSON.stringify(state).includes('private-fixture-token'));
});

test('final native check fails if the page replaces the pre-sign node after preparation', () => {
  const f = fixture(); prepare(f);
  f.node.execute = context => context;
  const state = f.context.__DOLA_FAST_NATIVE__.check();
  assert.equal(state.ready, false); assert.equal(state.pipelineReady, false);
  assert.equal(state.reasonCode, 'pipeline_changed');
});

test('read-only final check detects replacement of the node array, pipeline or module export', () => {
  for (const replacement of ['array', 'pipeline', 'module']) {
    const f = fixture(); assert.equal(prepare(f).ready, true);
    const writes = f.writes();
    const newNode = {id: 'video-creation:input-skill-node', execute: context => context};
    if (replacement === 'array') f.pipelines().nativePipeline.submitNodes = [newNode];
    if (replacement === 'pipeline') f.pipelines().nativePipeline = {submitNodes: [newNode]};
    if (replacement === 'module') f.replacePipelines({nativePipeline: {submitNodes: [newNode]}});
    const state = f.context.__DOLA_FAST_NATIVE__.check();
    assert.equal(state.ready, false, replacement);
    assert.equal(state.pipelineReady, false, replacement);
    assert.equal(state.reasonCode, 'pipeline_changed', replacement);
    assert.equal(f.writes(), writes, 'check must never re-prepare native state');
  }
});
test('empty controls or empty ability parameters cannot be promoted to a video request', () => {
  const f = fixture(); prepare(f);
  assert.throws(() => f.node.execute({content: {actionBarControlParams: {}, actionBarControlValues: {}}}), /Fast15/);
  const empty = fixture(); empty.node.execute = context => ({...context, content: {...context.content, abilityParams: {}}});
  prepare(empty); assert.throws(() => empty.node.execute(videoContext()), /Fast15/);
});
test('live store change and rejected native Promise never continue a submission', async () => {
  const changed = fixture(); prepare(changed); changed.records['video-model'].value = 'seedance_v2.0_std';
  assert.throws(() => changed.node.execute(videoContext()), /Fast15/);
  const rejected = fixture(); const error = new Error('native failure');
  rejected.node.execute = () => Promise.reject(error); prepare(rejected);
  await assert.rejects(rejected.node.execute(videoContext()), value => value === error);
});
test('preparation skips unavailable candidates and repeat checks never rewrite selected records', () => {
  const f = fixture({poison: true}); assert.equal(prepare(f).ready, true);
  const writes = f.writes(); assert.equal(writes, 2);
  prepare(f); assert.equal(f.writes(), writes, 'Idempotent preparation must not reset attached media');
  assert.equal(f.context.__DOLA_FAST15_NATIVE__.check().ready, true);
  f.records['video-duration'].value = '10';
  assert.equal(f.context.__DOLA_FAST15_NATIVE__.check().ready, false, 'Final check must observe page rollback');
  assert.equal(f.writes(), writes, 'Final check must never force another model change');
});
test('non-video context and unrelated model, duration or origin remain untouched', () => {
  const f = fixture(); prepare(f); const chat = {content: {abilityType: 1, text: 'ordinary chat'}};
  assert.equal(f.node.execute(chat).content.text, 'ordinary chat');
  for (const options of [{hostname: 'example.com'}, {model: 'ic_mini'}, {model: 'seedance_v2.5'}, {duration: 5}, {duration: 10}]) {
    const other = fixture(options); assert.equal(other.context.__DOLA_FAST15_NATIVE__, undefined);
    assert.equal(other.records['video-model'].value, 'seedance_v2.5');
  }
});
