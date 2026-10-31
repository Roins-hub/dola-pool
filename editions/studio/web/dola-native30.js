/* Document-start capability patch. Submission bodies and signatures stay untouched. */
(() => {
  'use strict';
  const target = globalThis.__DOLA_NATIVE_DURATION_TARGET__ ?? globalThis.__DOLA_NATIVE30_TARGET__;
  if (![5, 10, 15, 30].includes(target) || globalThis.__dolaNative30Installed) return;
  const model = globalThis.__DOLA_NATIVE_MODEL_TARGET__ ?? 'seedance_v2.5';
  if (!['ic_mini', 'seedance_v2.0', 'seedance_v2.5'].includes(model)
      || (model === 'ic_mini' && ![5, 10, 15].includes(target))) return;
  globalThis.__dolaNative30Installed = true;
  const durations = target === 30 ? [15, 30] : target === 15 ? [15] : [];
  function supported(input) {
    try {
      const u = new URL(typeof input === 'string' ? input : input.url, location.href);
      return (u.hostname === 'dola.com' || u.hostname.endsWith('.dola.com')) &&
        ['/samantha/skill/pack', '/alice/slot/action_bar_v3/get_item_conf'].includes(u.pathname.replace(/\/$/, ''));
    } catch { return false; }
  }
  function patch(node, depth = 0) {
    if (depth > 24 || node == null) return node;
    if (typeof node === 'string' && /^[\[{]/.test(node.trim())) {
      try { return JSON.stringify(patch(JSON.parse(node), depth + 1)); } catch { return node; }
    }
    if (typeof node !== 'object') return node;
    for (const key of Object.keys(node)) node[key] = patch(node[key], depth + 1);
    const capabilities = node.model_capability;
    if (model === 'seedance_v2.5' && capabilities && typeof capabilities === 'object' && !capabilities[model]
        && capabilities['seedance_v2.0']) {
      capabilities[model] = JSON.parse(JSON.stringify(capabilities['seedance_v2.0']));
    }
    const cap = capabilities?.[model];
    if (cap && typeof cap === 'object') {
      cap.supported_durations = [...new Set([...(cap.supported_durations || []).map(String), ...durations.map(String)])];
    }
    if (node.value === 'duration' && Array.isArray(node.options)) {
      const template = node.options.find(x => String(x.value) === '10') || {};
      for (const seconds of durations) if (!node.options.some(x => Number(x.value) === seconds))
        node.options.push({...template, value: String(seconds), show_name: seconds + 's', is_default: false});
      for (const option of node.options) option.is_default = Number(option.value) === target;
    }
    if (node.value === 'model' && Array.isArray(node.options)
        && node.options.some(x => ['ic_mini', 'seedance_v2.0', 'seedance_v2.5'].includes(x.value))) {
      if (model === 'seedance_v2.5' && !node.options.some(x => x.value === model)) {
        const template = node.options.find(x => x.value === 'seedance_v2.0') || {};
        node.options.push({...template, value: model, show_name: 'Dreamina Seedance 2.5',
          sub_display: '', is_default: false});
      }
      for (const option of node.options) option.is_default = option.value === model;
    }
    if (node.key === 'video-duration' && Array.isArray(node.option_list)) {
      const template = node.option_list.find(x => String(x.option_key) === '10') || {};
      for (const seconds of durations) if (!node.option_list.some(x => Number(x.option_key) === seconds)) {
        const id = Math.max(0, ...node.option_list.map(x => Number(x.id) || 0)) + 1;
        node.option_list.push({...template, id, option_key: String(seconds), display_text: seconds + 's', message_text: ''});
      }
      node.default_selection_index = node.option_list.findIndex(x => Number(x.option_key) === target);
      node.default_selection_id = node.option_list[node.default_selection_index].id;
    }
    if (node.key === 'video-duration' && !Array.isArray(node.option_list)) {
      const snake = ['lower_bound', 'upper_bound', 'step_length', 'default_selected_duration'];
      const camel = ['lowerBound', 'upperBound', 'stepLength', 'defaultSelectedDuration'];
      const has = key => Object.hasOwn(node, key);
      if ([...snake, ...camel].some(has)) {
        const camelOnly = camel.some(has) && !snake.some(has);
        const upperKeys = ['upper_bound', 'upperBound'].filter(has);
        const defaultKeys = ['default_selected_duration', 'defaultSelectedDuration'].filter(has);
        if (!upperKeys.length) upperKeys.push(camelOnly ? 'upperBound' : 'upper_bound');
        if (!defaultKeys.length) defaultKeys.push(camelOnly ? 'defaultSelectedDuration' : 'default_selected_duration');
        const maximum = model === 'seedance_v2.5' || (model === 'seedance_v2.0' && target === 30) ? 30 : 15;
        for (const key of upperKeys) node[key] = Math.min(maximum, Math.max(Number(node[key]) || 0, target));
        for (const key of defaultKeys) node[key] = target;
      }
    }
    if (node.key === 'video-model' && Array.isArray(node.option_list)) {
      if (model === 'seedance_v2.5' && !node.option_list.some(x => x.option_key === model)) {
        const template = node.option_list.find(x => x.option_key === 'seedance_v2.0') || {};
        const id = Math.max(0, ...node.option_list.map(x => Number(x.id) || 0)) + 1;
        node.option_list.push({...template, id, option_key: model,
          display_text: 'Dreamina Seedance 2.5', selected_display_text: '2.5', message_text: '',
          extra: {...template.extra, sub_display: ''}});
      }
      const index = node.option_list.findIndex(x => x.option_key === model);
      if (index >= 0) { node.default_selection_index = index; node.default_selection_id = node.option_list[index].id; }
    }
    return node;
  }
  const patchText = raw => { try { return JSON.stringify(patch(JSON.parse(raw))); } catch { return raw; } };
  const fetch = window.fetch;
  window.fetch = function(input, ...rest) {
    const response = fetch.call(this, input, ...rest);
    if (!supported(input)) return response;
    return Promise.resolve(response).then(async original => {
      if (!original.ok) return original;
      try {
        const headers = new Headers(original.headers);
        headers.delete('content-length'); headers.delete('content-encoding');
        const rewritten = new Response(patchText(await original.clone().text()),
          {status: original.status, statusText: original.statusText, headers});
        for (const key of ['url', 'redirected', 'type']) Object.defineProperty(rewritten, key, {value: original[key]});
        return rewritten;
      } catch { return original; }
    });
  };
  const proto = XMLHttpRequest.prototype, open = proto.open;
  const textGetter = Object.getOwnPropertyDescriptor(proto, 'responseText')?.get;
  const responseGetter = Object.getOwnPropertyDescriptor(proto, 'response')?.get;
  proto.open = function(method, url, ...rest) {
    const selected = supported(String(url));
    let cachedRaw, cached;
    if (textGetter) Object.defineProperty(this, 'responseText', {configurable:true, get: () => {
      const raw = textGetter.call(this);
      if (!selected || this.readyState !== 4) return raw;
      if (raw !== cachedRaw) { cachedRaw = raw; cached = patchText(raw); }
      return cached;
    }});
    if (responseGetter) Object.defineProperty(this, 'response', {configurable:true, get: () => {
      const raw = responseGetter.call(this);
      if (!selected || this.readyState !== 4) return raw;
      if (this.responseType === 'json' && raw) {
        try { return patch(JSON.parse(JSON.stringify(raw))); } catch { return raw; }
      }
      return typeof raw === 'string' ? patchText(raw) : raw;
    }});
    return open.call(this, method, url, ...rest);
  };
})();
