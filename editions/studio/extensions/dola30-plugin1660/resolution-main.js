(function () {
  "use strict";

  const INSTALL_KEY = "__doubaoDolaResolutionInstalledV1620";
  const PREFERENCE_KEY = "intl_doubao_target_resolution_v1";
  const PREFERENCE_ATTRIBUTE = "data-watermark-free-resolution-preference";
  const PREFERENCE_EVENT = "watermark-free-resolution-preference-change";
  const STATUS_ATTRIBUTE = "data-watermark-free-resolution-status";
  const STATUS_EVENT = "watermark-free-resolution-status";
  const RESOLUTIONS = ["720p", "1080p", "480p"];
  const XHR_STATE = Symbol("doubao-dola-resolution-xhr");
  const host = location.hostname.toLowerCase().replace(/\.$/, "");
  const supportedHost = (value) => /(^|\.)(doubao|dola)\.com$/i.test(value);

  if (!supportedHost(host) || globalThis[INSTALL_KEY]) return;
  globalThis[INSTALL_KEY] = true;

  const status = {
    version: "1.6.35", site: /(^|\.)doubao\.com$/.test(host) ? "doubao" : "dola",
    installed: true, preference: "auto", skillPack: false, actionBar: false,
    registeredControls: 0, lastConfig: "", lastRequest: null, requestCount: 0, error: ""
  };

  function preference() {
    try {
      const value = localStorage.getItem(PREFERENCE_KEY);
      return RESOLUTIONS.includes(value) ? value : "auto";
    } catch {
      return "auto";
    }
  }

  function publish() {
    status.preference = preference();
    const root = document.documentElement;
    if (!root) return;
    root.setAttribute(STATUS_ATTRIBUTE, JSON.stringify(status));
    document.dispatchEvent(new Event(STATUS_EVENT));
  }

  Object.defineProperty(globalThis, "__DOUBAO_DOLA_RESOLUTION_STATUS__", {
    configurable: true,
    get: () => JSON.parse(JSON.stringify({ ...status, preference: preference() }))
  });

  document.addEventListener("DOMContentLoaded", publish, { once: true });
  document.addEventListener(PREFERENCE_EVENT, () => {
    const value = document.documentElement?.getAttribute(PREFERENCE_ATTRIBUTE);
    if (value !== "auto" && !RESOLUTIONS.includes(value)) return;
    try {
      localStorage.setItem(PREFERENCE_KEY, value);
    } catch {
      status.error = "无法保存分辨率默认值";
    }
    // Defaults are consumed when the page initializes its native video state.
    // Saving a preference never edits a signed generation request.
    publish();
  });

  function urlOf(input) {
    return typeof input === "string" ? input : String(input?.url || input?.href || "");
  }

  function endpoint(input) {
    try {
      const url = new URL(urlOf(input), location.href);
      if (!/^https?:$/.test(url.protocol) || !supportedHost(url.hostname)) return "";
      const path = url.pathname.replace(/\/+$/, "");
      if (/(?:^|\/)samantha\/skill\/pack$/.test(path)) return "skillPack";
      if (/(?:^|\/)alice\/slot\/action_bar_v3\/get_item_conf$/.test(path)) return "actionBar";
      if (/(?:^|\/)chat\/completion$/.test(path)) return "generation";
      return "";
    } catch {
      return "";
    }
  }

  function normalized(value) {
    return String(value ?? "").trim().toLowerCase().replace(/\s+/g, "");
  }

  function optionResolution(option) {
    return normalized(option?.option_key ?? option?.value);
  }

  function nextId(options) {
    return options.reduce((max, option) => {
      const id = Number(option?.id);
      return Number.isFinite(id) ? Math.max(max, id) : max;
    }, 0) + 1;
  }

  function addResolutions(options, actionBar) {
    const template = options.find((option) => optionResolution(option) === "720p") || options[0] || {};
    for (const resolution of RESOLUTIONS) {
      if (options.some((option) => optionResolution(option) === resolution)) continue;
      const option = { ...template };
      if (actionBar) {
        option.option_key = resolution;
        option.display_text = resolution.toUpperCase();
        option.message_text = "";
        // Action-bar ids and array indices are separate fields.
        option.id = nextId(options);
      } else {
        option.value = resolution;
        option.show_name = resolution.toUpperCase();
        option.is_default = false;
        option.sub_display = "";
        if (Object.hasOwn(template, "id")) option.id = nextId(options);
      }
      options.push(option);
    }
  }

  function extendCapability(capability, model) {
    if (!capability || typeof capability !== "object" || Array.isArray(capability)) return false;
    const existing = Array.isArray(capability.supported_resolutions) ? capability.supported_resolutions : [];
    const values = [...existing];
    for (const resolution of RESOLUTIONS) {
      if (resolution === "480p" && model !== "seedance_v2.5") continue;
      if (!values.some((value) => normalized(value) === resolution)) values.push(resolution);
    }
    capability.supported_resolutions = values;
    return true;
  }

  function patchVideoMeta(meta) {
    if (!meta || typeof meta !== "object" || Array.isArray(meta)) return false;
    let changed = globalThis.__DOUBAO_DOLA_MODEL_SELECTION__?.patchMeta?.(meta) || false;
    if (meta.model_capability && typeof meta.model_capability === "object") {
      for (const [model, capability] of Object.entries(meta.model_capability)) {
        changed = extendCapability(capability, model) || changed;
      }
    }
    if (Array.isArray(meta.option_list)) {
      let control = meta.option_list.find((option) => normalized(option?.value) === "resolution");
      if (!control) {
        control = { type: 3, value: "resolution", show_name: "", options: [] };
        meta.option_list.push(control);
      }
      if (Array.isArray(control.options)) {
        const nativeDefault = control.options.find((option) => option?.is_default);
        const selected = preference() === "auto" ? optionResolution(nativeDefault) || "720p" : preference();
        addResolutions(control.options, false);
        for (const option of control.options) option.is_default = optionResolution(option) === selected;
        changed = true;
      }
    }
    return changed;
  }

  function isVideoNode(value) {
    const keys = [value?.key, value?.value, value?.name, value?.skill_key, value?.ability_key];
    return keys.some((key) => /^(?:video[-_]|video_generation$)/i.test(String(key || ""))) ||
      Number(value?.ability_type) === 17;
  }

  function isResolutionControl(value, videoContext) {
    const key = normalized(value?.key || value?.value || value?.param_key).replace(/_/g, "-");
    if (key === "video-resolution") return true;
    const label = String(value?.label || value?.show_name || "").trim();
    return videoContext && (key === "resolution" || /^(?:分辨率|resolution)$/i.test(label));
  }

  function clearDurationMessageText(control, videoContext) {
    const key = normalized(control?.key || control?.value || control?.param_key).replace(/_/g, "-");
    if (key !== "video-duration" && !(videoContext && key === "duration")) return false;
    let changed = false;
    // Keep display labels and numeric values. Only silence the text which
    // a native control is permitted to append to the submitted prompt.
    if (Object.hasOwn(control, "message_text") && control.message_text !== "") {
      control.message_text = "";
      changed = true;
    }
    const options = Array.isArray(control.option_list) ? control.option_list : control.options;
    for (const option of Array.isArray(options) ? options : []) {
      if (option && typeof option === "object" && option.message_text !== "") {
        option.message_text = "";
        changed = true;
      }
    }
    return changed;
  }

  function patchActionControl(control) {
    const options = Array.isArray(control.option_list) ? control.option_list : control.options;
    if (!Array.isArray(options)) return false;
    const actionShape = options.some((option) => Object.hasOwn(option || {}, "option_key")) ||
      Array.isArray(control.option_list);
    const pref = preference();
    const existingIndex = Number(control.default_selection_index);
    const nativeOption = options.find((option) => option?.is_default) ||
      options.find((option) => control.default_selection_id != null && option?.id === control.default_selection_id) ||
      (Number.isInteger(existingIndex) ? options[existingIndex] : null) || options[0];
    const selected = pref === "auto" ? optionResolution(nativeOption) || "720p" : pref;
    addResolutions(options, actionShape);
    const index = options.findIndex((option) => optionResolution(option) === selected);
    if (index < 0) return false;
    control.default_selection_index = index;
    if (options[index].id != null) control.default_selection_id = options[index].id;
    for (const option of options) {
      if (!actionShape || Object.hasOwn(option, "is_default")) option.is_default = optionResolution(option) === selected;
    }
    for (const key of ["default_selected_resolution", "defaultSelectedResolution"]) {
      if (Object.hasOwn(control, key)) control[key] = options[index].option_key ?? options[index].value;
    }
    return true;
  }

  function registerMissingControl(list, videoContext) {
    if (!Array.isArray(list) || list.some((item) => isResolutionControl(item, videoContext))) return false;
    const keyOf = (item) => normalized(item?.key || item?.value).replace(/_/g, "-");
    const duration = list.find((item) => keyOf(item) === "video-duration");
    const ratio = list.find((item) => /^(?:video-ratio|video-aspect-ratio)$/.test(keyOf(item)));
    // This is a list of control definitions, not a list of model/ratio options.
    if (!duration && !ratio) return false;
    const template = ratio || list.find((item) => keyOf(item) === "video-model" && Array.isArray(item?.option_list));
    const control = template ? { ...template } : {};
    // Strip properties which belong to the source control, not the new one.
    for (const key of Object.keys(control)) {
      if (/bound|step|duration|model|icon|badge|subscription|message_text|placeholder/i.test(key)) delete control[key];
    }
    control.key = "video-resolution";
    if (Object.hasOwn(control, "value")) control.value = "video-resolution";
    control.label = "分辨率";
    if (Object.hasOwn(control, "show_name")) control.show_name = "分辨率";
    for (const key of ["submit_key", "submitKey", "param_key", "paramKey"]) {
      if (Object.hasOwn(control, key)) control[key] = "resolution";
    }
    control.type = template?.type ?? 1;
    if (Object.hasOwn(control, "id")) control.id = nextId(list);
    control.option_list = [
      { id: 1, option_key: "720p", display_text: "720P", message_text: "" },
      { id: 2, option_key: "1080p", display_text: "1080P", message_text: "" }
    ];
    delete control.options;
    for (const key of ["hidden", "is_hidden", "isHidden"]) if (Object.hasOwn(control, key)) control[key] = false;
    for (const key of ["visible", "is_visible", "isVisible"]) if (Object.hasOwn(control, key)) control[key] = true;
    patchActionControl(control);
    const index = duration ? list.indexOf(duration) + 1 : list.indexOf(ratio) + 1;
    list.splice(index, 0, control);
    status.registeredControls += 1;
    return true;
  }

  function patchConfiguration(json, kind) {
    if (!json || typeof json !== "object") return false;
    if (Object.hasOwn(json, "code") && Number(json.code) !== 0) return false;
    const seen = new Set();
    let changed = false;

    function walk(value, videoContext = false, depth = 0, stringDepth = 0) {
      if (depth > 32 || stringDepth > 4) return value;
      if (typeof value === "string") {
        const trimmed = value.trim();
        if (trimmed.length > 2_000_000 || !/^[{[]/.test(trimmed) ||
            !/resolution|分辨率|video[-_]|video_generation/.test(trimmed)) return value;
        try {
          const parsed = JSON.parse(value);
          const previous = changed;
          changed = false;
          walk(parsed, videoContext, depth + 1, stringDepth + 1);
          const localChanged = changed;
          changed = previous || localChanged;
          return localChanged ? JSON.stringify(parsed) : value;
        } catch {
          return value;
        }
      }
      if (!value || typeof value !== "object" || seen.has(value)) return value;
      seen.add(value);
      const name = normalized(value.key || value.value || value.name);
      const imageContext = /^(?:image[-_]|image_generation$|photo[-_])/.test(name);
      const context = !imageContext && (videoContext || isVideoNode(value) ||
        (Array.isArray(value) && value.some(isVideoNode)));

      changed = clearDurationMessageText(value, context) || changed;
      changed = (globalThis.__DOUBAO_DOLA_MODEL_SELECTION__?.patchControl(value, context) || false) || changed;

      if (kind === "actionBar" && Array.isArray(value) && !imageContext) {
        changed = registerMissingControl(value, context) || changed;
      }

      if (value.video_generation && typeof value.video_generation === "object") {
        changed = patchVideoMeta(value.video_generation.meta || value.video_generation) || changed;
      }
      if (kind === "actionBar" && isResolutionControl(value, context)) {
        changed = patchActionControl(value) || changed;
      }
      if (context && value.model_capability && typeof value.model_capability === "object") {
        changed = (globalThis.__DOUBAO_DOLA_MODEL_SELECTION__?.patchMeta?.(value) || false) || changed;
        for (const capability of Object.values(value.model_capability)) {
          changed = extendCapability(capability) || changed;
        }
      }
      for (const [key, child] of Object.entries(value)) {
        const childContext = /^(?:image[-_]|photo[-_])/i.test(key) ? false :
          context || key === "video_generation" || /^video[-_]/i.test(key);
        value[key] = walk(child, childContext, depth + 1, stringDepth);
      }
      return value;
    }

    walk(json);
    return changed;
  }

  function recordConfig(kind, changed) {
    status.lastConfig = kind;
    if (changed) {
      status[kind] = true;
      status.error = "";
    } else if (!status.skillPack && !status.actionBar) {
      status.error = "未发现可识别的视频分辨率配置";
    }
    publish();
  }

  function patchText(text, kind) {
    try {
      const json = JSON.parse(text);
      const changed = patchConfiguration(json, kind);
      recordConfig(kind, changed);
      return changed ? JSON.stringify(json) : text;
    } catch {
      return text;
    }
  }

  function withResponseMetadata(response, original) {
    for (const key of ["url", "type", "redirected"]) {
      Object.defineProperty(response, key, { configurable: true, get: () => original[key] });
    }
    const nativeClone = response.clone.bind(response);
    Object.defineProperty(response, "clone", {
      configurable: true, value: () => withResponseMetadata(nativeClone(), original)
    });
    return response;
  }

  async function patchResponse(response, kind) {
    if (!response?.ok || response.status === 204 || response.status === 205) return response;
    try {
      const raw = await response.clone().text();
      const body = patchText(raw, kind);
      if (body === raw) return response;
      const headers = new Headers(response.headers);
      for (const name of ["content-length", "content-encoding", "etag", "content-md5", "digest"]) headers.delete(name);
      headers.set("content-type", "application/json; charset=utf-8");
      return withResponseMetadata(new Response(body, {
        status: response.status, statusText: response.statusText, headers
      }), response);
    } catch {
      return response;
    }
  }

  function observeGeneration(body, ticket) {
    if (typeof body !== "string" || body.length > 2_000_000) return;
    try {
      const json = JSON.parse(body);
      const seen = new Set();
      let found = null;
      function walk(value, videoContext = false, depth = 0) {
        if (!value || typeof value !== "object" || seen.has(value) || depth > 24) return;
        seen.add(value);
        const context = videoContext || Number(value.ability_type) === 17 || Number(value.abilityType) === 17 ||
          Number(value.skill_type) === 17 ||
          /^(?:seedance|ic_mini)/i.test(String(value.model || ""));
        if (context && (value.resolution != null || typeof value.model === "string")) {
          found = {
            resolution: normalized(value.resolution),
            model: typeof value.model === "string" ? value.model.slice(0, 80) : "",
            duration: Number.isFinite(Number(value.duration)) ? Number(value.duration) : null
          };
        }
        for (const [key, child] of Object.entries(value)) {
          if (["prompt", "text", "content", "input_text"].includes(key)) continue;
          if (["ability_param", "chat_ability", "video_generation", "params", "input_skill", "abilityParams"].includes(key) &&
              typeof child === "string" && /^\s*[{[]/.test(child) && /resolution|model|ability_type|skill_type/.test(child)) {
            try { walk(JSON.parse(child), context, depth + 1); } catch { /* Not JSON. */ }
          } else {
            walk(child, context || key === "video_generation", depth + 1);
          }
        }
      }
      walk(json);
      let intent = null;
      try { intent = globalThis.__DOUBAO_GENERATION_INTENT__?.inspect(json) ?? null; } catch { /* Observation is optional. */ }
      globalThis.__DOUBAO_DOLA_GENERATION_EVIDENCE__?.noteRequest(ticket, found, intent);
      status.lastRequest = found || { resolution: "", model: "", duration: null };
      status.requestCount += 1;
      publish();
    } catch {
      // Observing a request cannot block, replace, or re-send it.
    }
  }

  const originalFetch = window.fetch;
  if (typeof originalFetch === "function") {
    window.fetch = function resolutionFetch(input, init) {
      const kind = endpoint(input);
      const evidence = globalThis.__DOUBAO_DOLA_GENERATION_EVIDENCE__;
      const ticket = kind === "generation" ? evidence?.begin() : null;
      if (kind === "generation") {
        try {
          if (init && Object.hasOwn(init, "body")) {
            observeGeneration(init.body, ticket);
          } else if (typeof Request !== "undefined" && input instanceof Request &&
                     !input.bodyUsed && !/^(?:GET|HEAD)$/i.test(init?.method || input.method)) {
            input.clone().text().then(body => observeGeneration(body, ticket)).catch(() => {});
          }
        } catch { /* Forward the original input even if observation fails. */ }
      }
      let result;
      try { result = originalFetch.apply(this, arguments); }
      catch (error) { if (ticket) evidence.fail(ticket, "FETCH_THROW"); throw error; }
      if (ticket) {
        const observe = response => { void evidence.observeFetch(response, ticket).catch(() => {}); };
        const failure = () => evidence.fail(ticket, "FETCH_FAILED");
        if (typeof result?.then === "function") result.then(observe, failure).catch(() => {});
        else observe(result);
      }
      return kind === "skillPack" || kind === "actionBar"
        ? Promise.resolve(result).then((response) => patchResponse(response, kind)) : result;
    };
  }

  if (typeof XMLHttpRequest !== "undefined") {
    const proto = XMLHttpRequest.prototype;
    const originalOpen = proto.open;
    const originalSend = proto.send;
    const nativeTextGetter = Object.getOwnPropertyDescriptor(proto, "responseText")?.get;
    const nativeResponseGetter = Object.getOwnPropertyDescriptor(proto, "response")?.get;
    proto.open = function resolutionOpen(method, url) {
      const previous = this[XHR_STATE];
      if (previous?.observe) {
        for (const event of ["readystatechange", "progress", "load"]) this.removeEventListener(event, previous.observe);
        for (const event of ["error", "abort", "timeout"]) this.removeEventListener(event, previous.fail);
      }
      if (previous?.wrapped) {
        delete this.responseText;
        delete this.response;
      }
      // Let earlier Dola hooks initialize first, then compose their getters.
      const result = originalOpen.apply(this, arguments);
      const state = { kind: endpoint(url), wrapped: false, cache: new Map() };
      this[XHR_STATE] = state;
      if (state.kind === "generation") {
        state.observe = () => {
          if (!state.ticket || state.done || this.readyState < 2) return;
          const evidence = globalThis.__DOUBAO_DOLA_GENERATION_EVIDENCE__;
          if (!state.capture) state.capture = evidence?.observer(state.ticket, "xhr");
          if (!state.capture) return;
          if (state.httpStatus !== this.status) { state.httpStatus = this.status; state.capture.start(this.status); }
          try {
            if (this.responseType === "json") {
              if (this.readyState === 4) state.capture.json(JSON.stringify(this.response));
            } else if (!this.responseType || this.responseType === "text") {
              const text = this.responseText;
              state.capture.feed(text.slice(state.offset || 0)); state.offset = text.length;
            }
            if (this.readyState === 4) { state.done = true; state.capture.end(); }
          } catch { evidence?.fail(state.ticket, "XHR_RESPONSE_READ_FAILED"); state.done = true; }
        };
        state.fail = event => { state.done = true; globalThis.__DOUBAO_DOLA_GENERATION_EVIDENCE__?.fail(state.ticket, `XHR_${event.type.toUpperCase()}`); };
        for (const event of ["readystatechange", "progress", "load"]) this.addEventListener(event, state.observe);
        for (const event of ["error", "abort", "timeout"]) this.addEventListener(event, state.fail);
      }
      if (state.kind !== "skillPack" && state.kind !== "actionBar") return result;
      const textGetter = Object.getOwnPropertyDescriptor(this, "responseText")?.get || nativeTextGetter;
      const responseGetter = Object.getOwnPropertyDescriptor(this, "response")?.get || nativeResponseGetter;
      const wrap = (name, getter) => {
        if (!getter) return;
        try {
          Object.defineProperty(this, name, {
            configurable: true,
            get: () => {
              const raw = getter.call(this);
              if (this.readyState !== 4 || this.status < 200 || this.status >= 300) return raw;
              const cached = state.cache.get(name);
              if (cached && cached.raw === raw) return cached.value;
              let value = raw;
              if (typeof raw === "string") value = patchText(raw, state.kind);
              else if (this.responseType === "json" && raw && typeof raw === "object") {
                try {
                  value = JSON.parse(JSON.stringify(raw));
                  const changed = patchConfiguration(value, state.kind);
                  recordConfig(state.kind, changed);
                  if (!changed) value = raw;
                } catch { value = raw; }
              }
              state.cache.set(name, { raw, value });
              return value;
            }
          });
          state.wrapped = true;
        } catch { /* Fetch still works if an instance getter is non-configurable. */ }
      };
      wrap("responseText", textGetter);
      wrap("response", responseGetter);
      return result;
    };
    proto.send = function resolutionSend(body) {
      if (this[XHR_STATE]?.kind === "generation") {
        const state = this[XHR_STATE];
        state.ticket = globalThis.__DOUBAO_DOLA_GENERATION_EVIDENCE__?.begin(); state.done = false; state.offset = 0;
        observeGeneration(body, state.ticket);
      }
      return originalSend.apply(this, arguments);
    };
  }

  publish();
})();
