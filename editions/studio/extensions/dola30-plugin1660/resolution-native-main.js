(function () {
  "use strict";

  const INSTALL = "__doubaoDolaResolutionNativeV1620";
  const PREF_KEY = "intl_doubao_target_resolution_v1";
  const PREF_ATTRIBUTE = "data-watermark-free-resolution-preference";
  const PREF_EVENT = "watermark-free-resolution-preference-change";
  const STATUS_ATTRIBUTE = "data-watermark-free-resolution-bridge";
  const STATUS_EVENT = "watermark-free-resolution-bridge-status";
  const MENU_MARK = "data-watermark-free-resolution-ui";
  const VALUES = ["720p", "1080p", "480p"];
  const IS_DOLA = /(^|\.)dola\.com$/i.test(location.hostname);
  if (!/(^|\.)(doubao|dola)\.com$/i.test(location.hostname) || globalThis[INSTALL]) return;
  globalThis[INSTALL] = true;

  const state = {
    version: "1.6.60", runtimeFound: false, submitReady: false,
    menuVisible: false, menuInjected: false, nativeControl: false,
    selected: "auto", lastDraft: null, reason: "等待网页提交服务",
    inputReady: false, inputResolution: "", pipelineReady: false, legacyReady: false,
    draftCount: 0, pipelineCount: 0, lastPipeline: null, serviceRebinds: 0
  };
  const installedServices = new WeakMap();
  const installedAccessors = new WeakMap();
  const installedLegacyServices = new WeakMap();
  const installedNodes = new WeakMap();
  let nativeInput = null;
  let scannedModuleCount = -1;
  const inputModuleIds = new Set(["Kyj", "rM"]);
  const pipelineModuleIds = new Set(["sAh"]);
  const serviceModuleIds = new Set(["nR1", "431565", "645906"]);
  let searching = false;
  let renderQueued = false;
  let lastPageUrl = location.href;
  let pendingPrompt = null;
  const EDITOR_SELECTOR = 'textarea, input[type="text"], [contenteditable="true"], [role="textbox"]';

  function selected() {
    try {
      const value = localStorage.getItem(PREF_KEY);
      return VALUES.includes(value) ? value : "auto";
    } catch { return "auto"; }
  }

  function publish() {
    state.selected = selected();
    document.documentElement?.setAttribute(STATUS_ATTRIBUTE, JSON.stringify(state));
    document.dispatchEvent(new Event(STATUS_EVENT));
  }

  function save(value) {
    if (!VALUES.includes(value) && value !== "auto") return false;
    try { localStorage.setItem(PREF_KEY, value); } catch { return false; }
    document.documentElement?.setAttribute(PREF_ATTRIBUTE, value);
    document.dispatchEvent(new Event(PREF_EVENT));
    syncNativeInput();
    renderMenu();
    publish();
    return true;
  }

  // Both sites keep the raw value and its submission record separately.
  // Dola exports qM/gl/N5; the current Doubao runtime exports n/h/f.
  function syncNativeInput() {
    if (!nativeInput) return;
    const value = selected();
    try {
      if (value !== "auto") {
        const previous = nativeInput.getRecord?.("video-resolution");
        if (nativeInput.getValue("video-resolution") !== value || previous?.value !== value ||
            previous?.messageText !== "" || previous?.submitKey !== "resolution") {
          nativeInput.setRecord("video-resolution", { ...previous, value, submitKey: "resolution", messageText: "" });
        }
      }
      state.inputResolution = String(nativeInput.getValue("video-resolution") || "").toLowerCase();
      state.inputReady = true;
    } catch {
      state.inputReady = false;
      state.inputResolution = "";
    }
  }

  function stripAddedDuration(text, snapshot, duration) {
    if (!snapshot || snapshot.page !== location.href || Date.now() - snapshot.at > 60_000 ||
        typeof text !== "string" || !text.startsWith(snapshot.text) || !Number.isFinite(duration) || duration <= 0) return text;
    const number = String(duration).replace(/\./g, "\\.");
    const suffix = new RegExp(`^(?:[ \\t]*[,，][ \\t]*|[ \\t]*\\n[ \\t]*)(?:(?:时长|duration)[ \\t]*[:：]?[ \\t]*)?${number}[ \\t]*(?:s|sec(?:onds?)?|秒(?:钟)?)[ \\t]*$`, "i");
    return suffix.test(text.slice(snapshot.text.length)) ? snapshot.text : text;
  }

  function prepareVideoContext(context, resolution) {
    const content = context?.content;
    if (!content || typeof content !== "object" ||
        (content.abilityType != null && Number(content.abilityType) !== 17)) return context;
    const params = { ...content.actionBarControlParams };
    const values = { ...content.actionBarControlValues };
    if (VALUES.includes(resolution)) {
      params["video-resolution"] = resolution;
      values["video-resolution"] = { ...values["video-resolution"], value: resolution,
        submitKey: "resolution", messageText: "" };
      // A native record may have a different valueKey but the same submitKey.
      for (const [key, record] of Object.entries(values)) {
        if (record?.submitKey === "resolution") values[key] = { ...record, value: resolution, messageText: "" };
      }
    }
    for (const [key, record] of Object.entries(values)) {
      if (key === "video-duration" || record?.submitKey === "duration") {
        values[key] = { ...record, messageText: "" };
      }
    }
    const next = { ...context, content: { ...content, actionBarControlParams: params, actionBarControlValues: values } };
    return globalThis.__DOUBAO_DOLA_MODEL_SELECTION__?.prepareContext(next) ?? next;
  }

  function installVideoPipeline(pipeline) {
    if (!Array.isArray(pipeline?.submitNodes)) return false;
    const node = pipeline.submitNodes.find((entry) => entry?.id === "video-creation:input-skill-node");
    if (!node || typeof node.execute !== "function") return false;
    if (installedNodes.get(node) === node.execute) return true;
    const original = node.execute;
    function resolutionInNativePipeline(context, ...args) {
      if (context?.content?.abilityType != null && Number(context.content.abilityType) !== 17) {
        return original.call(this, context, ...args);
      }
      const value = selected();
      const prepared = prepareVideoContext(context, value);
      const result = original.call(this, prepared, ...args);
      function finish(output) {
        const params = output?.content?.abilityParams;
        const recognized = Boolean(params && typeof params === "object" && !Array.isArray(params) &&
          (output.content.abilityType == null || Number(output.content.abilityType) === 17));
        let next = output;
        const corrected = recognized && VALUES.includes(value) && String(params.resolution || "").toLowerCase() !== value;
        if (corrected) {
          // The native node may rebuild parameters from stale/default options.
          // Correct its known video result before serialization and signing.
          next = { ...output, content: { ...output.content, abilityParams: { ...params, resolution: value } } };
        }
        state.pipelineCount += 1;
        state.lastPipeline = { selected: value, recognized, corrected,
          nativeResolution: recognized ? String(params.resolution || "").toLowerCase() : "",
          resolution: recognized ? String(next.content.abilityParams.resolution || "").toLowerCase() : "",
          model: typeof params?.model === "string" ? params.model.slice(0, 80) : "",
          duration: Number.isFinite(Number(params?.duration)) ? Number(params.duration) : null };
        if (IS_DOLA && recognized) {
          try { globalThis.__DOLA_SILENT_EXPERT__?.arm({
            model: state.lastPipeline.model,
            duration: state.lastPipeline.duration,
            resolution: state.lastPipeline.resolution
          }); } catch { /* Expert routing must never block the native video pipeline. */ }
        }
        if (recognized) {
          const text = stripAddedDuration(output.content.text, pendingPrompt, Number(params.duration));
          if (text !== output.content.text) next = { ...next, content: { ...next.content, text } };
        }
        try { next = globalThis.__DOUBAO_DOLA_MODEL_SELECTION__?.preparePipeline(next) ?? next; }
        catch { /* Preserve an unrecognized native model result. */ }
        const beforeTimeline = next;
        try { next = globalThis.__DOUBAO_DOLA_TIMELINE_PROGRESS__?.preparePipeline(next) ?? next; }
        catch { /* Preserve an unknown native pipeline result. */ }
        state.lastPipeline.timelineConverted = next !== beforeTimeline;
        try { next = globalThis.__DOUBAO_DOLA_TWO_PART__?.preparePipeline(next) ?? next; }
        catch { /* Never block native submission on an unsupported rule layout. */ }
        publish();
        return next;
      }
      return result && typeof result.then === "function" ? result.then(finish) : finish(result);
    }
    try { node.execute = resolutionInNativePipeline; } catch { return false; }
    if (node.execute !== resolutionInNativePipeline) return false;
    installedNodes.set(node, resolutionInNativePipeline);
    return true;
  }

  function discoverVideoPipeline(require) {
    if (!require?.m) return;
    const count = Object.keys(require.m).length;
    if (count !== scannedModuleCount) {
      scannedModuleCount = count;
      for (const [id, factory] of Object.entries(require.m)) {
        const source = String(factory);
        if (source.includes("__input_action_control_values__") && source.includes("chatInputStore")) inputModuleIds.add(id);
        if (source.includes("video-creation:input-skill-node")) pipelineModuleIds.add(id);
        if (source.includes("serviceContainer") && source.includes("resourceStore")) serviceModuleIds.add(id);
      }
    }
    for (const id of inputModuleIds) {
      if (!Object.hasOwn(require.m, id)) continue;
      try {
        const api = require(id);
        if (typeof api?.qM === "function" && typeof api?.gl === "function") {
          nativeInput = { setRecord: api.qM, getValue: api.gl, getRecord: api.N5 }; break;
        }
        // Validate the module signature before using compact domestic exports.
        const source = String(require.m[id]);
        if (source.includes("__input_action_control_values__") && source.includes("chatInputStore") &&
            typeof api?.n === "function" && typeof api?.h === "function" && typeof api?.f === "function") {
          nativeInput = { setRecord: api.n, getValue: api.h, getRecord: api.f }; break;
        }
      } catch { /* The native input store has not hydrated yet. */ }
    }
    syncNativeInput();
    if (nativeInput) globalThis.__DOUBAO_DOLA_MODEL_SELECTION__?.bindInput(nativeInput);
    let ready = false;
    for (const id of pipelineModuleIds) {
      if (!Object.hasOwn(require.m, id)) continue;
      try {
        const module = require(id);
        for (const pipeline of Object.values(module || {})) ready = installVideoPipeline(pipeline) || ready;
      } catch { /* Wait for the remaining lazy video modules. */ }
    }
    state.pipelineReady = ready;
    if (ready) { state.submitReady = true; state.reason = ""; }
  }

  function promptEditors(root = document) {
    return Array.from(root?.querySelectorAll?.(EDITOR_SELECTOR) || []).filter((editor) => {
      if (!editor.isConnected || editor.disabled || editor.readOnly) return false;
      const rect = editor.getBoundingClientRect();
      const style = getComputedStyle(editor);
      return rect.width > 1 && rect.height > 1 && style.display !== "none" && style.visibility !== "hidden";
    });
  }

  function editorForSubmit(node) {
    let parent = node?.tagName === "FORM" ? node : node?.parentElement || node;
    for (let depth = 0; parent && depth < 9; depth += 1, parent = parent.parentElement) {
      const editors = promptEditors(parent);
      if (editors.length) return editors[editors.length - 1];
    }
    const editors = promptEditors();
    return editors[editors.length - 1] || null;
  }

  // Read the authored prompt before the page clears its composer. These
  // listeners never change the editor, cancel an event or submit a second time.
  function rememberPrompt(event) {
    let editor = null;
    if (event.type === "keydown") {
      if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
      editor = event.target?.closest?.(EDITOR_SELECTOR);
    } else if (event.type === "submit") {
      editor = editorForSubmit(event.target);
    } else {
      const button = event.target?.closest?.('button, [role="button"]');
      if (!button || button.disabled || button.getAttribute("aria-haspopup") ||
          button.closest('[data-input-engine-actionbar-control-key]') || button.closest(`[${MENU_MARK}]`)) return;
      editor = editorForSubmit(button);
      if (!editor) return;
      const label = [button.textContent, button.getAttribute("aria-label"), button.getAttribute("title")].join(" ");
      if (/模型|比例|时长|seedance/i.test(label)) return;
      if (button.type !== "submit" && button.getAttribute("type") !== "submit" &&
          !/发送|提交|生成|\bsend\b|\bsubmit\b/i.test(label)) {
        const buttonRect = button.getBoundingClientRect();
        const editorRect = editor.getBoundingClientRect();
        if (!(buttonRect.left >= editorRect.right - 90 && buttonRect.top >= editorRect.top - 20 &&
              buttonRect.bottom <= editorRect.bottom + 80)) return;
      }
    }
    const text = /^(TEXTAREA|INPUT)$/.test(editor?.tagName || "")
      ? String(editor.value || "") : String(editor?.innerText ?? editor?.textContent ?? "");
    pendingPrompt = text.trim() ? { text, page: location.href, at: Date.now(), userGesture: event.isTrusted === true,
      interactionToken: globalThis.__DOUBAO_AUTO_CONFIRM__?.interactionToken } : null;
  }

  for (const type of ["pointerdown", "click", "keydown", "submit"]) {
    document.addEventListener(type, rememberPrompt, true);
  }
  document.addEventListener("input", (event) => {
    if (event.isTrusted && event.target?.closest?.(EDITOR_SELECTOR)) pendingPrompt = null;
  }, true);

  function durationOfMessage(message) {
    if (/system|assistant|bot|tool/i.test(String(message?.role || message?.sender_role || ""))) return null;
    try {
      const raw = message?.ext?.chat_ability;
      const ability = typeof raw === "string" ? JSON.parse(raw) : raw;
      if (Number(ability?.ability_type) !== 17) return null;
      const params = typeof ability.ability_param === "string" ? JSON.parse(ability.ability_param) : ability.ability_param;
      const duration = Number(params?.duration);
      return Number.isFinite(duration) && duration > 0 && duration <= 600 ? duration : null;
    } catch { return null; }
  }

  function removeGeneratedDuration(request, snapshot) {
    if (!snapshot || snapshot.page !== location.href || Date.now() - snapshot.at > 60_000 ||
        !Array.isArray(request?.messages)) return { request, changed: false };
    let changed = false;
    let target = -1;
    for (let index = request.messages.length - 1; index >= 0; index -= 1) {
      if (durationOfMessage(request.messages[index]) != null) { target = index; break; }
    }
    const messages = request.messages.map((message, index) => {
      if (index !== target) return message;
      const duration = durationOfMessage(message);
      if (duration == null) return message;
      const number = String(duration).replace(/\./g, "\\.");
      const suffix = new RegExp(`^(?:[ \\t]*[,，][ \\t]*|[ \\t]*\\n[ \\t]*)(?:(?:时长|duration)[ \\t]*[:：]?[ \\t]*)?${number}[ \\t]*(?:s|sec(?:onds?)?|秒(?:钟)?)[ \\t]*$`, "i");
      function clean(text) {
        if (typeof text !== "string" || !text.startsWith(snapshot.text)) return text;
        // A user-authored "30s" is part of the snapshot and remains intact.
        // Remove only an additional suffix equal to the structured duration.
        return suffix.test(text.slice(snapshot.text.length)) ? snapshot.text : text;
      }
      function replacePath(value, path) {
        if (!path.length) return clean(value);
        if (!value || typeof value !== "object" || Array.isArray(value)) return value;
        const [key, ...rest] = path;
        if (!Object.hasOwn(value, key)) return value;
        const next = replacePath(value[key], rest);
        return next === value[key] ? value : { ...value, [key]: next };
      }
      let output = message;
      for (const path of [["content"], ["text"], ["prompt"], ["content", "text"], ["content", "text_block", "text"]]) {
        output = replacePath(output, path);
      }
      if (Array.isArray(message.content_blocks_v2)) {
        let blocksChanged = false;
        const blocks = message.content_blocks_v2.map((block) => {
          if (![1, 10000].includes(Number(block?.block_type))) return block;
          let next = block;
          for (const path of [["text"], ["content", "text"], ["content", "text_block", "text"]]) {
            next = replacePath(next, path);
          }
          blocksChanged ||= next !== block;
          return next;
        });
        if (blocksChanged) output = { ...output, content_blocks_v2: blocks };
      }
      changed ||= output !== message;
      return output;
    });
    return { request: changed ? { ...request, messages } : request, changed };
  }

  // Patch an application-level message object BEFORE the native IM service
  // serializes/signs it. Never patch the fetch/XHR body or a signed URL.
  function patchDraft(request, resolution) {
    if (!VALUES.includes(resolution)) return { request, found: false, changed: false };
    const seen = new Set();
    let found = false;
    let changed = false;
    function walk(value, depth = 0) {
      if (!value || typeof value !== "object" || depth > 24 || seen.has(value)) return value;
      const prototype = Object.getPrototypeOf(value);
      if (!Array.isArray(value) && prototype !== null && prototype?.constructor?.name !== "Object") return value;
      seen.add(value);
      let output = value;
      if (IS_DOLA && Number(value.skill_type) === 17) {
        const encoded = typeof value.variables === "string";
        try {
          const vars = encoded ? JSON.parse(value.variables) : value.variables;
          if (vars == null || (typeof vars === "object" && !Array.isArray(vars))) {
            found = true;
            if (String(vars?.resolution || "").toLowerCase() !== resolution) {
              const next = { ...vars, resolution };
              output = { ...value, variables: encoded ? JSON.stringify(next) : next };
              changed = true;
            }
          }
        } catch { /* Leave malformed legacy variables intact. */ }
      }
      if (Number(value.ability_type) === 17 && value.ability_param != null) {
        const encoded = typeof value.ability_param === "string";
        try {
          const params = encoded ? JSON.parse(value.ability_param) : value.ability_param;
          if (params && typeof params === "object" && !Array.isArray(params)) {
            found = true;
            if (String(params.resolution || "").toLowerCase() !== resolution) {
              const updated = { ...params, resolution };
              output = { ...value, ability_param: encoded ? JSON.stringify(updated) : updated };
              changed = true;
            }
          }
        } catch { /* Leave an unrecognized draft intact. */ }
      }
      for (const [key, child] of Object.entries(output)) {
        if (["prompt", "text", "content", "content_blocks_v2"].includes(key)) continue;
        let updated = child;
        if ((key === "chat_ability" || (IS_DOLA && key === "input_skill")) && typeof child === "string") {
          try {
            const parsed = JSON.parse(child);
            const next = walk(parsed, depth + 1);
            if (next !== parsed) updated = JSON.stringify(next);
          } catch { /* Do not reinterpret other strings as configuration. */ }
        } else if (key !== "ability_param") updated = walk(child, depth + 1);
        if (updated !== child) {
          if (output === value) output = Array.isArray(value) ? [...value] : { ...value };
          output[key] = updated;
        }
      }
      return output;
    }
    return { request: walk(request), found, changed };
  }

  function installService(service) {
    if (!service || typeof service.sendMessage !== "function") return false;
    if (installedServices.get(service) === service.sendMessage ||
        (installedServices.has(service) && service.sendMessage.__DOUBAO_DOLA_RESOLUTION_BRIDGE__)) {
      state.submitReady = true;
      state.reason = "";
      return true;
    }
    const original = service.sendMessage;
    function resolutionBeforeSigning(request, ...args) {
      const snapshot = pendingPrompt;
      pendingPrompt = null;
      let prompt = { request, changed: false };
      try { prompt = removeGeneratedDuration(request, snapshot); } catch { /* An unknown native draft stays intact. */ }
      const beforeTimeline = prompt.request;
      try { prompt.request = globalThis.__DOUBAO_DOLA_TIMELINE_PROGRESS__?.prepare(prompt.request) ?? prompt.request; }
      catch { /* Preserve a draft with an unknown timeline representation. */ }
      const timelineConverted = prompt.request !== beforeTimeline;
      try { prompt.request = globalThis.__DOUBAO_DOLA_MODEL_SELECTION__?.prepare(prompt.request) ?? prompt.request; }
      catch { /* Preserve an unrecognized model draft. */ }
      const value = selected();
      let result = { request: prompt.request, found: false, changed: false };
      if (value !== "auto") result = patchDraft(prompt.request, value);
      const beforeIntent = result.request;
      try { result.request = globalThis.__DOUBAO_GENERATION_INTENT__?.prepare(result.request) ?? result.request; }
      catch { /* An unknown intent representation cannot block native sending. */ }
      const intentPrefixAdded = result.request !== beforeIntent;
      try { result.request = globalThis.__DOUBAO_DOLA_TWO_PART__?.prepare(result.request) ?? result.request; }
      catch { /* Preserve an unknown native draft. */ }
      const beforeExpert = result.request;
      try { result.request = globalThis.__DOLA_SILENT_EXPERT__?.prepare(result.request) ?? result.request; }
      catch { /* Preserve an unknown native draft if expert routing cannot be applied. */ }
      const silentExpertApplied = result.request !== beforeExpert;
      try { globalThis.__DOUBAO_AUTO_CONFIRM__?.observeDraft(result.request, snapshot); }
      catch { /* Auto-confirm observation cannot change or block native sending. */ }
      state.draftCount += 1;
      state.lastDraft = { resolution: value, recognized: result.found, changed: result.changed, promptSuffixRemoved: prompt.changed, timelineConverted, intentPrefixAdded, silentExpertApplied };
      if (value !== "auto" && !result.found) state.reason = "提交对象未包含可识别的视频能力参数";
      else state.reason = "";
      publish();
      // Exactly one native call; its return value, this and extra arguments survive.
      return original.call(this, result.request, ...args);
    }
    Object.defineProperty(resolutionBeforeSigning, "__DOUBAO_DOLA_RESOLUTION_BRIDGE__", { value: true });
    try { service.sendMessage = resolutionBeforeSigning; } catch { return false; }
    if (service.sendMessage !== resolutionBeforeSigning) return false;
    if (installedServices.has(service)) state.serviceRebinds += 1;
    installedServices.set(service, resolutionBeforeSigning);
    state.submitReady = true;
    state.reason = "";
    return true;
  }

  function installIMAccessor(im) {
    if (!im || typeof im.getMessageService !== "function") return;
    if (installedAccessors.get(im) === im.getMessageService) return;
    const original = im.getMessageService;
    function currentMessageService(...args) {
      const result = original.apply(this, args);
      const install = (service) => {
        try { installService(service); publish(); } catch { /* Preserve the native accessor result. */ }
      };
      if (result && typeof result.then === "function") {
        // Register before the caller awaits this same promise; keep its identity.
        result.then(install, () => {});
      } else install(result);
      return result;
    }
    try { im.getMessageService = currentMessageService; } catch { return; }
    if (im.getMessageService === currentMessageService) installedAccessors.set(im, currentMessageService);
  }

  function installLegacyService(service) {
    if (!IS_DOLA || !service || typeof service.sendMessage !== "function") return false;
    if (installedLegacyServices.get(service) === service.sendMessage) return true;
    const original = service.sendMessage;
    function legacyResolutionBeforeSigning(request, ...args) {
      let prompt = request;
      try { prompt = globalThis.__DOUBAO_DOLA_TIMELINE_PROGRESS__?.prepareLegacy(request) ?? request; }
      catch { /* Preserve an unknown legacy draft. */ }
      const timelineConverted = prompt !== request;
      const beforeModel = prompt;
      try { prompt = globalThis.__DOUBAO_DOLA_MODEL_SELECTION__?.prepareLegacy(prompt) ?? prompt; }
      catch { /* Preserve an unknown legacy model representation. */ }
      const value = selected();
      const result = value === "auto" ? { request: prompt, found: false, changed: false } : patchDraft(prompt, value);
      state.lastLegacy = { selected: value, recognized: result.found, changed: result.changed, timelineConverted, modelCorrected: prompt !== beforeModel };
      try { result.request = globalThis.__DOUBAO_DOLA_TWO_PART__?.prepareLegacy(result.request) ?? result.request; }
      catch { /* Preserve an unknown legacy draft. */ }
      try { result.request = globalThis.__DOLA_SILENT_EXPERT__?.prepare(result.request) ?? result.request; }
      catch { /* Preserve an unknown legacy draft if expert routing cannot be applied. */ }
      publish();
      return original.call(this, result.request, ...args);
    }
    try { service.sendMessage = legacyResolutionBeforeSigning; } catch { return false; }
    if (service.sendMessage !== legacyResolutionBeforeSigning) return false;
    installedLegacyServices.set(service, legacyResolutionBeforeSigning);
    return true;
  }

  function runtime() {
    if (typeof window.__DOLA_TOOLKIT_WEBPACK_REQUIRE__ === "function") return window.__DOLA_TOOLKIT_WEBPACK_REQUIRE__;
    if (typeof window.__DOUBAO_DOLA_RESOLUTION_REQUIRE__ === "function") return window.__DOUBAO_DOLA_RESOLUTION_REQUIRE__;
    const candidates = [window.__LOADABLE_LOADED_CHUNKS__];
    for (const key of Object.keys(window)) {
      if (/^webpackChunk/.test(key) && Array.isArray(window[key])) candidates.push(window[key]);
    }
    for (const chunks of candidates) {
      if (!Array.isArray(chunks)) continue;
      try {
        let captured = null;
        chunks.push([[`resolution-${Date.now()}`], {}, (require) => { captured = require; }]);
        if (typeof captured === "function") {
          window.__DOUBAO_DOLA_RESOLUTION_REQUIRE__ = captured;
          return captured;
        }
      } catch { /* Another chunk format; try the next known Webpack array. */ }
    }
    return null;
  }

  async function discoverService() {
    if (searching) return;
    searching = true;
    try {
      const require = runtime();
      if (!require?.m) return;
      state.runtimeFound = true;
      discoverVideoPipeline(require);
      for (const id of serviceModuleIds) {
        if (!Object.hasOwn(require.m, id)) continue;
        try {
          const module = require(id);
          const source = String(require.m[id]);
          const accessor = module?.K || module?.D ||
            (source.includes("serviceContainer") && source.includes("resourceStore") ? module?.a : null);
          if (typeof accessor !== "function") continue;
          const im = await accessor("chatIMService");
          installIMAccessor(im);
          const service = await im?.getMessageService?.();
          if (IS_DOLA) {
            try { state.legacyReady = installLegacyService(await accessor("chatMessageService")); } catch { /* A legacy service may be absent. */ }
          }
          if (installService(service)) break;
        } catch { /* A service may not be available until video-mode hydration. */ }
      }
    } finally {
      searching = false;
      if (state.pipelineReady || state.legacyReady) { state.submitReady = true; state.reason = ""; }
      publish();
      renderMenu();
    }
  }

  function visible(element) {
    if (!element?.isConnected) return false;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width >= 100 && rect.width <= 680 && rect.height >= 55 && rect.height <= 900 &&
      style.display !== "none" && style.visibility !== "hidden";
  }

  function menu() {
    const candidates = new Set(document.querySelectorAll(
      '[role="dialog"], [role="menu"], [role="listbox"], [data-popper-placement], ' +
      '[class*="popover"], [class*="Popover"], [class*="popper"], [class*="dropdown"], [class*="Dropdown"]'
    ));
    for (const slider of document.querySelectorAll('[role="slider"], input[type="range"]')) {
      let parent = slider.parentElement;
      for (let depth = 0; parent && depth < 8; depth += 1, parent = parent.parentElement) candidates.add(parent);
    }
    return [...candidates].filter(visible).filter((node) => {
      const text = [...node.children].filter(child => !child.hasAttribute(MENU_MARK)).map(child => child.textContent).join(" ") || String(node.textContent || "");
      if (node.querySelector(EDITOR_SELECTOR)) return false;
      const durationAndRatio = /时长|duration/i.test(text) &&
        (/比例|ratio|aspect/i.test(text) || /(?:1\s*:\s*1|3\s*:\s*4|16\s*:\s*9|9\s*:\s*16)/.test(text));
      const resolutionOnly = /分辨率|resolution/i.test(text) && /720\s*p/i.test(text) && /1080\s*p/i.test(text);
      return durationAndRatio || resolutionOnly;
    }).sort((a, b) => {
      const ar = a.getBoundingClientRect(); const br = b.getBoundingClientRect();
      return ar.width * ar.height - br.width * br.height;
    })[0] || null;
  }

  function renderMenu() {
    if (!document.documentElement) return;
    const root = menu();
    const before = `${state.menuVisible}:${state.menuInjected}:${state.nativeControl}`;
    state.menuVisible = Boolean(root);
    state.menuInjected = false;
    state.nativeControl = false;
    if (root) {
      const nativeText = [...root.children].filter((node) => !node.hasAttribute(MENU_MARK)).map((node) => node.textContent).join(" ");
      state.nativeControl = /720\s*p/i.test(nativeText) && /1080\s*p/i.test(nativeText);
      const missing = VALUES.filter(value => !new RegExp(value.replace('p', '\\s*p'), 'i').test(nativeText));
      let row = root.querySelector(`[${MENU_MARK}]`);
      if (state.nativeControl) {
        for (const node of root.querySelectorAll('button, [role="button"], [role="radio"], [role="option"], [role="menuitem"], div')) {
          const label = String(node.textContent || "").replace(/\s+/g, " ").trim();
          const value = label.match(/^(480|720|1080)\s*p(?:\b|升级|[✓✔√])/i)?.[1];
          if (!value || (label.match(/(?:480|720|1080)\s*p/gi) || []).length !== 1 || !state.submitReady) continue;
          if (node.hasAttribute("disabled")) node.removeAttribute("disabled");
          if (node.getAttribute("aria-disabled") === "true") node.setAttribute("aria-disabled", "false");
          if (node.style.pointerEvents === "none") node.style.pointerEvents = "auto";
          if (selected() !== "auto" && state.inputReady && state.inputResolution === selected()) {
            const attribute = node.getAttribute("role") === "radio" ? "aria-checked" : "aria-selected";
            const active = selected() === `${value}p`;
            if (node.getAttribute(attribute) !== String(active)) node.setAttribute(attribute, String(active));
          }
        }
      }
      if (!missing.length) row?.remove();
      else {
        if (row && row.getAttribute('data-resolution-values') !== missing.join(',')) { row.remove(); row = null; }
        if (!row) {
          row = document.createElement("div");
          row.setAttribute(MENU_MARK, "1");
          row.setAttribute('data-resolution-values', missing.join(','));
          row.style.cssText = "box-sizing:border-box;width:100%;grid-column:1/-1;flex-shrink:0;padding:12px 0 4px;margin-top:12px;border-top:1px solid #eee;font-family:inherit;";
          const label = document.createElement("div");
          label.textContent = "分辨率 · 插件";
          label.style.cssText = "color:#929292;font-size:12px;margin-bottom:8px;";
          row.appendChild(label);
          const buttons = document.createElement("div");
          buttons.style.cssText = "display:flex;gap:8px;";
          for (const value of missing) {
            const button = document.createElement("button");
            button.type = "button";
            button.textContent = value.toUpperCase();
            button.setAttribute("data-resolution", value);
            button.addEventListener("click", (event) => {
              event.stopPropagation();
              if (state.submitReady) save(value);
            });
            buttons.appendChild(button);
          }
          row.appendChild(buttons);
          const note = document.createElement("div");
          note.setAttribute("data-resolution-note", "1");
          note.style.cssText = "color:#929292;font-size:11px;line-height:1.5;margin-top:6px;";
          row.appendChild(note);
          root.appendChild(row);
        }
        state.menuInjected = true;
        for (const button of row.querySelectorAll("button")) {
          const active = selected() === button.getAttribute("data-resolution");
          button.disabled = !state.submitReady;
          button.setAttribute("aria-pressed", String(active));
          button.style.cssText = `flex:1;height:36px;border-radius:9px;font:inherit;font-size:13px;cursor:${state.submitReady ? "pointer" : "wait"};border:1px solid ${active ? "#347eff" : "#eee"};background:${active ? "#eff5ff" : "#f7f7f7"};color:${active ? "#347eff" : "#333"};opacity:${state.submitReady ? "1" : ".6"};`;
        }
        const note = row.querySelector("[data-resolution-note]");
        const text = state.submitReady ? "选择将用于下一次视频生成" : "等待网页提交服务，暂未应用";
        if (note.textContent !== text) note.textContent = text;
      }
    }
    if (before !== `${state.menuVisible}:${state.menuInjected}:${state.nativeControl}`) publish();
  }

  function queueRender() {
    if (renderQueued) return;
    renderQueued = true;
    setTimeout(() => { renderQueued = false; renderMenu(); }, 0);
  }

  function resolutionGesture(event) {
    if (event.type === "keydown" && !["Enter", " "].includes(event.key)) return;
    if (event.isComposing || event.ctrlKey || event.metaKey || event.altKey || (event.button != null && event.button !== 0)) return;
    const root = menu();
    let target = event.target;
    while (target && target !== root) {
      const label = String(target.textContent || "").replace(/\s+/g, " ").trim();
      const value = label.match(/^(480|720|1080)\s*p(?:\b|升级|[✓✔√])/i)?.[1];
      if (value && (label.match(/(?:480|720|1080)\s*p/gi) || []).length === 1 && root?.contains(target) &&
          !target.closest(`[${MENU_MARK}]`) && state.submitReady) {
        if (save(`${value}p`)) {
          if (/升级|\bupgrade\b/i.test(label)) {
            event.preventDefault(); event.stopImmediatePropagation();
          }
        }
        break;
      }
      target = target.parentElement;
    }
    queueRender();
  }
  const captureTarget = typeof window.addEventListener === "function" ? window : document;
  for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click", "keydown"]) captureTarget.addEventListener(type, resolutionGesture, true);
  document.addEventListener(PREF_EVENT, () => {
    syncNativeInput(); renderMenu(); publish();
    void discoverService();
  });
  document.addEventListener("watermark-free-resolution-status", queueRender);

  function start() {
    renderMenu();
    void discoverService();
    if (typeof MutationObserver === "function") {
      new MutationObserver((records) => {
        if (records.some((record) => !record.target?.closest?.(`[${MENU_MARK}]`))) queueRender();
      }).observe(document.documentElement, { childList: true, subtree: true });
    }
    setInterval(() => {
      if (location.href !== lastPageUrl) {
        pendingPrompt = null;
        lastPageUrl = location.href;
        state.submitReady = false;
        state.reason = "页面已切换，等待当前会话提交服务";
      }
      renderMenu();
      // Both sites lazily mount the video node and may replace IM instances
      // after the first service has already reported readiness.
      void discoverService();
    }, 1000);
    publish();
  }

  Object.defineProperty(window, "__DOUBAO_DOLA_RESOLUTION_BRIDGE__", {
    configurable: true, value: Object.freeze({
      get status() { return JSON.parse(JSON.stringify({ ...state, selected: selected() })); },
      retry: discoverService
    })
  });
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
})();
