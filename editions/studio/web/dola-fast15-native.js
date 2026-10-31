/* Bind Fast 15/30 before the page signs its request; no network request is rewritten. */
(() => {
  'use strict';
  const seconds = globalThis.__DOLA_NATIVE_DURATION_TARGET__;
  if (!/(^|\.)dola\.com$/.test(location.hostname)
      || globalThis.__DOLA_NATIVE_MODEL_TARGET__ !== 'seedance_v2.0'
      || ![15, 30].includes(seconds)
      || globalThis.__DOLA_FAST_NATIVE__) return;
  const label = `Fast${seconds}`;
  const targets = {'video-model': ['seedance_v2.0', 'model'], 'video-duration': [String(seconds), 'duration']};
  const installed = new WeakMap();
  const failureCodes = new WeakMap();
  let input, runtime, pipelineNodes = [], pipelineModules = [];
  const state = {ready: false, model: '', duration: null, pipelineReady: false, executions: 0, reason: '', reasonCode: ''};
  const record = value => value && typeof value === 'object' && !Array.isArray(value);

  function inputApi(api) {
    if (['qM', 'gl', 'N5'].every(key => typeof api?.[key] === 'function')) return api;
    // Dola's 2026-10-20 store exposes the same record writer/value reader/record
    // reader as n/h/f. Require its observed store marker and accessor semantics;
    // do not guess other minified exports or call an unknown setter.
    if (api?.a !== '__input_action_control_values__'
        || !['n', 'h', 'f'].every(key => typeof api[key] === 'function')) return null;
    const writer = String(api.n), reader = String(api.h);
    if (!writer.includes('chatInputStore') || !writer.includes('.setState(')
        || !writer.includes('actionValue') || !writer.includes('.value')
        || !reader.includes('chatInputStore') || !reader.includes('.actionValue[')) return null;
    return {qM: (key, value) => api.n(key, value), gl: key => api.h(key), N5: key => api.f(key)};
  }
  function unavailable(code) {
    const error = new Error(code);
    failureCodes.set(error, code);
    throw error;
  }
  function videoNodes(exports) {
    return Object.values(exports || {}).flatMap(pipeline => Array.isArray(pipeline?.submitNodes)
      ? pipeline.submitNodes.filter(node => node?.id === 'video-creation:input-skill-node'
        && typeof node.execute === 'function') : []);
  }

  function getRuntime() {
    if (runtime?.m) return runtime;
    for (const candidate of [globalThis.__DOLA_TOOLKIT_WEBPACK_REQUIRE__, globalThis.__DOUBAO_DOLA_RESOLUTION_REQUIRE__]) {
      if (typeof candidate === 'function' && candidate.m) return (runtime = candidate);
    }
    const queues = [globalThis.__LOADABLE_LOADED_CHUNKS__];
    for (const key of Object.keys(globalThis)) if (/^webpackChunk/.test(key)) queues.push(globalThis[key]);
    for (const queue of queues) {
      if (!Array.isArray(queue)) continue;
      let captured;
      try { queue.push([[`dola-fast-${Date.now()}`], {}, require => { captured = require; }]); } catch { continue; }
      if (typeof captured === 'function' && captured.m) return (runtime = captured);
    }
    return null;
  }
  function readSelection() {
    state.model = String(input.gl('video-model') || '');
    state.duration = Number(input.gl('video-duration')) || null;
    return Object.entries(targets).every(([key, [value, submitKey]]) => {
      const previous = input.N5(key);
      return record(previous) && String(input.gl(key)) === value && String(previous.value) === value
        && previous.submitKey === submitKey && previous.messageText === '';
    });
  }
  function install(node) {
    if (installed.get(node) === node.execute) return true;
    const original = node.execute;
    function fast15BeforeSigning(context, ...args) {
      if (context?.content?.abilityType != null && Number(context.content.abilityType) !== 17)
        return original.call(this, context, ...args);
      if (!readSelection()) throw new Error(`${label}: native selection changed before signing`);
      const content = context?.content;
      if (!record(content) || !record(content.actionBarControlParams) || !record(content.actionBarControlValues))
        throw new Error(`${label}: unsupported native input context`);
      if (content.abilityType == null && !Object.keys(targets).every(key =>
        Object.hasOwn(content.actionBarControlParams, key) && record(content.actionBarControlValues[key])))
        throw new Error(`${label}: unrecognized video input context`);
      const params = {...content.actionBarControlParams}, values = {...content.actionBarControlValues};
      for (const [key, [value, submitKey]] of Object.entries(targets)) {
        if ((params[key] != null && String(params[key]) !== value)
            || (values[key]?.value != null && String(values[key].value) !== value))
          throw new Error(`${label}: native context selection conflicts with task`);
        params[key] = value;
        values[key] = {...values[key], value, submitKey, messageText: ''};
      }
      const prepared = {...context, content: {...content, actionBarControlParams: params, actionBarControlValues: values}};
      const result = original.call(this, prepared, ...args);
      function finish(output) {
        if (!readSelection() || !record(output?.content?.abilityParams)
            || typeof output.content.abilityParams.model !== 'string' || !output.content.abilityParams.model
            || !Number.isFinite(Number(output.content.abilityParams.duration)) || Number(output.content.abilityParams.duration) <= 0
            || (output.content.abilityType != null && Number(output.content.abilityType) !== 17))
          throw new Error(`${label}: unsupported native output or changed selection`);
        state.executions += 1;
        return {...output, content: {...output.content, abilityParams: {
          ...output.content.abilityParams, model: 'seedance_v2.0', duration: seconds}}};
      }
      return result && typeof result.then === 'function' ? result.then(finish) : finish(result);
    }
    try { node.execute = fast15BeforeSigning; } catch { return false; }
    if (node.execute !== fast15BeforeSigning) return false;
    installed.set(node, fast15BeforeSigning);
    return true;
  }
  function prepare() {
    state.ready = false; state.pipelineReady = false; state.reason = ''; state.reasonCode = '';
    input = undefined; pipelineNodes = []; pipelineModules = [];
    try {
      const require = getRuntime(), nodes = [], modules = [];
      if (!require) unavailable('runtime_unavailable');
      for (const [id, factory] of Object.entries(require.m)) {
        const source = String(factory);
        try {
          if (source.includes('__input_action_control_values__') && source.includes('chatInputStore')) {
            const api = inputApi(require(id));
            if (api) input = api;
          }
          if (source.includes('video-creation:input-skill-node')) {
            const current = videoNodes(require(id));
            if (current.length) { nodes.push(...current); modules.push(id); }
          }
        } catch { /* Another lazy candidate may already be usable. */ }
      }
      if (!input) unavailable('input_store_unavailable');
      if (!nodes.length) unavailable('video_pipeline_unavailable');
      for (const [key, [value, submitKey]] of Object.entries(targets)) {
        const previous = input.N5(key);
        if (!record(previous)) unavailable('record_unavailable');
        if (String(input.gl(key)) !== value || String(previous.value) !== value
            || previous.submitKey !== submitKey || previous.messageText !== '')
          input.qM(key, {...previous, value, submitKey, messageText: ''});
      }
      if (!readSelection()) unavailable('selection_readback_failed');
      state.pipelineReady = nodes.every(install);
      if (!state.pipelineReady) unavailable('pipeline_read_only');
      pipelineNodes = nodes; pipelineModules = modules;
      state.ready = true;
    } catch (error) {
      state.reasonCode = failureCodes.get(error) || 'native_prepare_failed';
      state.reason = `${label} 原生模型、时长或提交管线未就绪`;
    }
    return {...state};
  }
  function check() {
    try {
      state.pipelineReady = false;
      const current = pipelineModules.flatMap(id => videoNodes(runtime(id)));
      state.pipelineReady = Boolean(pipelineNodes.length && current.length === pipelineNodes.length
        && pipelineNodes.every(node => current.includes(node))
        && current.every(node => installed.get(node) === node.execute));
      if (!state.pipelineReady) unavailable('pipeline_changed');
      if (!input || !readSelection()) unavailable('selection_changed');
      state.ready = true; state.reason = ''; state.reasonCode = '';
    } catch (error) {
      state.ready = false;
      state.reasonCode = failureCodes.get(error) || 'native_prepare_failed';
      state.reason = `${label} 原生模型、时长或提交管线未就绪`;
    }
    return {...state};
  }
  const api = Object.freeze({prepare, check, status: () => ({...state})});
  globalThis.__DOLA_FAST_NATIVE__ = api;
  // Keep the v0.5.3 diagnostic entry point for 15-second callers.
  if (seconds === 15) globalThis.__DOLA_FAST15_NATIVE__ = api;
})();
