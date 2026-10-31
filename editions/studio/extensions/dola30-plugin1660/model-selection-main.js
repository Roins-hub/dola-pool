(function () {
  "use strict";

  // This adapter has no transport. It only changes the native input store and
  // known video objects BEFORE the page serializes and signs its own request.
  const INSTALL = "__doubaoModelSelectionV1625";
  const KEY = "intl_doubao_target_video_model_v1";
  const PREF_ATTRIBUTE = "data-watermark-free-model-preference";
  const PREF_EVENT = "watermark-free-model-preference-change";
  const STATUS_ATTRIBUTE = "data-watermark-free-model-status";
  const STATUS_EVENT = "watermark-free-model-status";
  const IS_DOLA = /(^|\.)dola\.com$/i.test(location.hostname);
  const LABELS = Object.freeze({
    "seedance_v2.5": "Seedance 2.5",
    "seedance_v2.0_std": "Seedance 2.0",
    "seedance_v2.0": "Seedance 2.0 Fast",
    "seedance_v2.0_mini": "Seedance 2.0 Mini",
    "ic_mini": "Seedance 1.0"
  });
  if (!/(^|\.)(doubao|dola)\.com$/i.test(location.hostname) || globalThis[INSTALL]) return;
  globalThis[INSTALL] = true;

  let input = null;
  let queued = false;
  let lastPublished = "";
  const state = { version: "1.6.35", selected: "auto", inputReady: false,
    inputModel: "", intercepted: 0, lastDraft: null, lastPipeline: null,
    lastLegacy: null, site: IS_DOLA ? "dola" : "doubao", restoredModels: [], restoredControls: 0,
    restoredCapabilities: [], capabilitySource: "", backendVerification: "UNVERIFIED",
    menuVisible: false, reason: "等待网页原生模型状态" };

  function known(value) {
    return Object.hasOwn(LABELS, String(value || "")) && (IS_DOLA
      ? ["seedance_v2.5", "seedance_v2.0", "ic_mini"].includes(value) : value !== "ic_mini");
  }
  function selected() {
    try { const value = localStorage.getItem(KEY); return known(value) ? value : "auto"; }
    catch { return "auto"; }
  }
  function publish() {
    state.selected = selected();
    const serialized = JSON.stringify(state);
    if (serialized === lastPublished || !document.documentElement) return;
    lastPublished = serialized;
    document.documentElement.setAttribute(STATUS_ATTRIBUTE, serialized);
    document.dispatchEvent(new Event(STATUS_EVENT));
  }
  function syncInput() {
    state.inputReady = false;
    if (!input) return false;
    try {
      const value = selected();
      if (known(value)) {
        const previous = input.getRecord?.("video-model");
        if (input.getValue("video-model") !== value || previous?.value !== value ||
            previous?.submitKey !== "model" || previous?.messageText !== "") {
          input.setRecord("video-model", { ...previous, value, submitKey: "model", messageText: "" });
        }
      }
      state.inputModel = String(input.getValue("video-model") || "");
      const record = input.getRecord?.("video-model");
      state.inputReady = value === "auto" || (state.inputModel === value && record?.value === value && record?.submitKey === "model");
      state.reason = state.inputReady ? "" : "网页未接受模型状态，保留原生点击行为";
    } catch { state.reason = "原生模型状态暂不可用"; }
    return state.inputReady;
  }
  function save(value) {
    if (value !== "auto" && !known(value)) return false;
    const previous = selected();
    try { localStorage.setItem(KEY, value); }
    catch { state.reason = "无法保存模型选择"; publish(); return false; }
    document.documentElement?.setAttribute(PREF_ATTRIBUTE, value);
    if (!syncInput() && input) {
      // A failed native write must not leave a hidden model override armed.
      try { localStorage.setItem(KEY, previous); } catch {}
      document.documentElement?.setAttribute(PREF_ATTRIBUTE, previous);
      publish(); return false;
    }
    publish();
    queueRender();
    return true;
  }

  function patchControl(control, videoContext) {
    if (!control || typeof control !== "object" || Array.isArray(control)) return false;
    const key = String(control.key || control.value || "").replace(/_/g, "-").toLowerCase();
    if (key !== "video-model" && !(videoContext && key === "model")) return false;
    const options = Array.isArray(control.option_list) ? control.option_list : control.options;
    if (!Array.isArray(options)) return false;
    let restored = false;
    if (IS_DOLA && options.some(option => ["seedance_v2.0", "ic_mini"].includes(option?.option_key ?? option?.value)) &&
        !options.some(option => (option?.option_key ?? option?.value) === "seedance_v2.5")) {
      // Restore the former Dola model key in the native config, not a DOM row.
      // Do not copy a domestic-only model or invent an entitlement flag.
      const action = Array.isArray(control.option_list);
      const next = action ? { id: options.reduce((max, option) => Math.max(max, Number(option?.id) || 0), 0) + 1,
        option_key: "seedance_v2.5", display_text: "Dreamina Seedance 2.5", message_text: "" } :
        { value: "seedance_v2.5", show_name: "Seedance2.5", is_default: false, sub_display: "" };
      options.unshift(next);
      if (Number.isInteger(control.default_selection_index) && control.default_selection_index >= 0) control.default_selection_index += 1;
      state.restoredControls += 1;
      if (!state.restoredModels.includes("seedance_v2.5")) state.restoredModels.push("seedance_v2.5");
      restored = true; publish();
    }
    const value = selected();
    const index = options.findIndex(option => (option?.option_key ?? option?.value) === value);
    if (index < 0) return restored;
    if (Array.isArray(control.option_list)) {
      control.default_selection_index = index;
      if (options[index].id != null) control.default_selection_id = options[index].id;
    }
    for (const option of options) {
      if (Array.isArray(control.options) || Object.hasOwn(option || {}, "is_default")) {
        option.is_default = (option.option_key ?? option.value) === value;
      }
    }
    // Entitlement fields and upgrade badges remain as returned by the server.
    return true;
  }

  function patchMeta(meta) {
    if (!IS_DOLA || !meta?.model_capability || typeof meta.model_capability !== "object" || Array.isArray(meta.model_capability) ||
        Object.hasOwn(meta.model_capability, "seedance_v2.5") ||
        !["seedance_v2.0", "ic_mini"].some(key => Object.hasOwn(meta.model_capability, key))) return false;
    // Former Dola client profile from the supplied 1.6.24 package. This keeps
    // native controls from reading an undefined profile; it grants no rights
    // and is not evidence of current server availability.
    meta.model_capability["seedance_v2.5"] = { supported_durations: ["5", "10", "15", "30"],
      supported_resolutions: ["720p"], support_video_ref: false };
    if (!state.restoredCapabilities.includes("seedance_v2.5")) state.restoredCapabilities.push("seedance_v2.5");
    state.capabilitySource = "bundled-dola-v1.6.24"; publish(); return true;
  }

  function prepareContext(context) {
    const value = selected(); const content = context?.content;
    if (!known(value) || !content || (content.abilityType != null && Number(content.abilityType) !== 17)) return context;
    const params = { ...content.actionBarControlParams, "video-model": value };
    const values = { ...content.actionBarControlValues };
    values["video-model"] = { ...values["video-model"], value, submitKey: "model", messageText: "" };
    for (const [key, record] of Object.entries(values)) {
      if (record?.submitKey === "model") values[key] = { ...record, value, messageText: "" };
    }
    return { ...context, content: { ...content, actionBarControlParams: params, actionBarControlValues: values } };
  }
  function preparePipeline(output) {
    const value = selected(); const content = output?.content; const params = content?.abilityParams;
    if (!known(value) || !params || typeof params !== "object" || Array.isArray(params) ||
        (content.abilityType != null && Number(content.abilityType) !== 17)) return output;
    const corrected = params.model !== value;
    state.lastPipeline = { selected: value, nativeModel: String(params.model || ""), model: value, corrected };
    publish();
    return corrected ? { ...output, content: { ...content, abilityParams: { ...params, model: value } } } : output;
  }
  function prepare(request) {
    const value = selected();
    if (!known(value) || !Array.isArray(request?.messages)) return request;
    // The last user message is the current submission. Do not modify history.
    let index = request.messages.length - 1;
    while (index >= 0 && /^(?:system|assistant|bot|tool)$/i.test(String(request.messages[index]?.role || request.messages[index]?.sender_role || ""))) index -= 1;
    const message = request.messages[index];
    let recognized = false; let changed = false; let model = ""; let output = request;
    try {
      const raw = message?.ext?.chat_ability;
      const encoded = typeof raw === "string";
      const ability = encoded ? JSON.parse(raw) : raw;
      if (Number(ability?.ability_type) === 17) {
        const encodedParams = typeof ability.ability_param === "string";
        const params = encodedParams ? JSON.parse(ability.ability_param) : ability.ability_param;
        if (params && typeof params === "object" && !Array.isArray(params)) {
          recognized = true; model = String(params.model || ""); changed = params.model !== value;
          if (changed) {
            const updated = { ...params, model: value };
            const next = { ...ability, ability_param: encodedParams ? JSON.stringify(updated) : updated };
            const messages = [...request.messages];
            messages[index] = { ...message, ext: { ...message.ext, chat_ability: encoded ? JSON.stringify(next) : next } };
            output = { ...request, messages };
          }
        }
      }
    } catch { /* Unknown or malformed objects are forwarded intact. */ }
    state.lastDraft = { selected: value, recognized, nativeModel: model, model: recognized ? value : "", changed };
    publish();
    return output;
  }

  function prepareLegacy(request) {
    const value = selected();
    const messages = Array.isArray(request) ? request : request?.messages;
    if (!IS_DOLA || !known(value) || !Array.isArray(messages) || !messages.length) return request;
    const index = messages.length - 1; const message = messages[index];
    if (/^(?:system|assistant|bot|tool)$/i.test(String(message?.role || message?.sender_role || ""))) return request;
    const owner = message?.extraExt?.input_skill != null ? "extraExt" : "ext";
    const raw = message?.[owner]?.input_skill;
    let output = request;
    try {
      const skill = typeof raw === "string" ? JSON.parse(raw) : raw;
      const variables = typeof skill?.variables === "string" ? JSON.parse(skill.variables) : skill?.variables;
      if (Number(skill?.skill_type) !== 17 || !variables || typeof variables !== "object" || Array.isArray(variables)) return request;
      const changed = variables.model !== value;
      state.lastLegacy = { selected: value, nativeModel: String(variables.model || ""), model: value, changed };
      if (changed) {
        const next = { ...skill, variables: typeof skill.variables === "string" ? JSON.stringify({ ...variables, model: value }) : { ...variables, model: value } };
        const list = [...messages];
        list[index] = { ...message, [owner]: { ...message[owner], input_skill: typeof raw === "string" ? JSON.stringify(next) : next } };
        output = Array.isArray(request) ? list : { ...request, messages: list };
      }
      publish();
    } catch { /* Preserve malformed legacy drafts. */ }
    return output;
  }

  function textOf(node) { return String(node?.textContent || "").replace(/\s+/g, " ").trim(); }
  function modelOf(node) {
    for (const name of ["data-option-key", "data-value", "data-model", "value"]) {
      const value = node?.getAttribute?.(name);
      if (known(value)) return value;
    }
    const text = textOf(node).replace(/^dreamina\s*/i, "");
    if ((text.match(/seedance/gi) || []).length !== 1) return "";
    if (/^seedance\s*2[.．]5(?![\d.])/i.test(text)) return "seedance_v2.5";
    if (/^seedance\s*2[.．]0\s*fast\b/i.test(text)) return "seedance_v2.0";
    if (/^seedance\s*2[.．]0\s*mini\b/i.test(text)) return "seedance_v2.0_mini";
    if (/^seedance\s*2[.．]0(?![\d.]|\s*(?:fast|mini))/i.test(text)) return "seedance_v2.0_std";
    if (IS_DOLA && /^seedance\s*1[.．]0(?:\s*fast)?(?![\d.])/i.test(text)) return "ic_mini";
    return "";
  }
  function visible(node) {
    if (!node?.isConnected) return false;
    const rect = node.getBoundingClientRect(); const style = getComputedStyle(node);
    return rect.width > 1 && rect.height > 1 && style.display !== "none" && style.visibility !== "hidden";
  }
  function isModelMenu(node) {
    if (!visible(node)) return false;
    const rect = node.getBoundingClientRect(); const text = textOf(node);
    return rect.width >= 100 && rect.width <= 580 && rect.height >= 55 && rect.height <= 650 &&
      (text.match(/seedance\s*\d[.．]\d/gi) || []).length >= 2 &&
      !node.querySelector('textarea, input[type="text"], [contenteditable="true"], [role="textbox"]');
  }
  function menuFor(target) {
    for (let node = target?.nodeType === 3 ? target.parentElement : target, depth = 0; node && depth < 12; node = node.parentElement, depth += 1) {
      if (isModelMenu(node)) return node;
    }
    return null;
  }
  function optionFor(target, root) {
    let option = null;
    for (let node = target?.nodeType === 3 ? target.parentElement : target; node && node !== root; node = node.parentElement) {
      if (modelOf(node)) option = node;
    }
    return option;
  }
  function intercept(event) {
    if (event.type === "keydown" && !["Enter", " "].includes(event.key)) return;
    if (event.isComposing || event.ctrlKey || event.metaKey || event.altKey || (event.button != null && event.button !== 0)) return;
    const root = menuFor(event.target); if (!root) return;
    const option = optionFor(event.target, root); const value = modelOf(option);
    if (!known(value)) return;
    if (!input) { state.reason = "原生模型状态未就绪，保留原生点击行为"; publish(); return; }
    if (!save(value) || !state.inputReady) return;
    // Available choices may initialize more than the input value. Preserve
    // their native callbacks; only intercept an explicitly labelled upgrade.
    if (!/升级|\bupgrade\b/i.test(textOf(option))) return;
    // Stop only this menu gesture before React's upgrade click handler.
    // No global dialog removal, permission-response rewriting or extra requests.
    event.preventDefault(); event.stopImmediatePropagation();
    if (event.type === "click" || event.type === "keydown") state.intercepted += 1;
    publish(); render();
  }
  const captureTarget = typeof window.addEventListener === "function" ? window : document;
  for (const type of ["pointerdown", "mousedown", "pointerup", "mouseup", "click", "keydown"]) captureTarget.addEventListener(type, intercept, true);

  function render() {
    const selectors = '[role="menu"], [role="listbox"], [role="dialog"], [data-popper-placement], [class*="popover"], [class*="Popover"], [class*="dropdown"], [class*="Dropdown"]';
    const menus = [...document.querySelectorAll(selectors)].filter(isModelMenu);
    state.menuVisible = menus.length > 0;
    syncInput();
    for (const root of menus) {
      for (const node of root.querySelectorAll('button, [role="option"], [role="menuitem"], [role="menuitemradio"], [role="radio"], li, div')) {
        const value = modelOf(node);
        if (!value || optionFor(node, root) !== node) continue;
        if (!input || !state.inputReady) continue;
        // A disabled row must receive the capture gesture before its native
        // upgrade handler. This does not alter server authorization.
        if (node.hasAttribute("disabled")) node.removeAttribute("disabled");
        if (node.getAttribute("aria-disabled") === "true") node.setAttribute("aria-disabled", "false");
        if (node.style.pointerEvents === "none") node.style.pointerEvents = "auto";
        // Native state must accept the selection before showing a selected row.
        if (selected() === "auto") continue;
        const active = state.inputModel === value;
        const attribute = /^(?:radio|menuitemradio)$/.test(node.getAttribute("role") || "") ? "aria-checked" : "aria-selected";
        if (node.getAttribute(attribute) !== String(active)) node.setAttribute(attribute, String(active));
      }
    }
    publish();
  }
  function queueRender() {
    if (queued) return;
    queued = true;
    setTimeout(() => { queued = false; render(); }, 0);
  }
  document.addEventListener(PREF_EVENT, () => {
    const value = document.documentElement?.getAttribute(PREF_ATTRIBUTE);
    if (value === "auto" || known(value)) save(value);
  });
  if (typeof window.addEventListener === "function") window.addEventListener("storage", event => { if (event.key === KEY) queueRender(); });
  document.addEventListener("watermark-free-resolution-bridge-status", queueRender);

  Object.defineProperty(globalThis, "__DOUBAO_DOLA_MODEL_SELECTION__", { configurable: true, value: Object.freeze({
    get status() { return JSON.parse(JSON.stringify({ ...state, selected: selected() })); },
    bindInput(adapter) { input = adapter; syncInput(); publish(); queueRender(); },
    patchControl, patchMeta, prepareContext, preparePipeline, prepare, prepareLegacy
  }) });
  function start() {
    render();
    if (typeof MutationObserver === "function") new MutationObserver(queueRender).observe(document.documentElement, { childList: true, subtree: true, characterData: true });
    setInterval(render, 1000);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true });
  else start();
})();
