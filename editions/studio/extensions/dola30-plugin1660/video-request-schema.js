(function () {
  "use strict";
  if (globalThis.__DOUBAO_DOLA_VIDEO_REQUEST_SCHEMA__) return;
  const LIMIT = 2_000_000;
  const object = value => value && typeof value === "object" && !Array.isArray(value);
  const keys = value => object(value) ? Object.keys(value).slice(0, 32).map(key => /^[a-zA-Z_][a-zA-Z0-9_-]{0,47}$/.test(key) ? key : "[key]") : [];
  function decode(value) {
    for (let count = 0; typeof value === "string" && count < 3; count += 1) {
      if (value.length > LIMIT) return null;
      try { value = JSON.parse(value); } catch { return null; }
    }
    return value;
  }
  function inspect(request, prefix = "【视频生成请求】", visitText = null) {
    const out = { videoRecognized: false, video: null, abilityType: null, abilitySource: "", abilityKeys: [], parameterKeys: [],
      messageCount: 0, messageKeys: [], shape: "unknown", textBlockTypes: [], textSources: [], unknownTextSources: [],
      prefixPresent: false, prefixChecked: false, inputBoxAvailable: false, inputBoxPrefixPresent: false, inputBoxKeys: [] };
    const messages = Array.isArray(request) ? request : decode(request?.messages);
    if (!Array.isArray(messages) || !messages.length) return out;
    out.messageCount = messages.length;
    const message = messages[messages.length - 1]; out.messageKeys = keys(message);
    if (!object(message) || /^(?:assistant|bot|tool|system)$/i.test(String(message.role || message.sender_role || message.sender_type || ""))) return out;
    let ability;
    if (object(request) && Object.hasOwn(request, "chat_ability")) {
      ability = decode(request.chat_ability); out.abilitySource = "request.chat_ability"; out.shape = "wire";
    } else if (message.ext?.chat_ability != null) {
      ability = decode(message.ext.chat_ability); out.abilitySource = "messages[].ext.chat_ability"; out.shape = "draft";
    } else if (message.extraExt?.input_skill != null || message.ext?.input_skill != null) {
      ability = decode(message.extraExt?.input_skill ?? message.ext?.input_skill); out.abilitySource = "messages[].input_skill"; out.shape = "legacy";
    }
    out.abilityKeys = keys(ability);
    const rawType = ability?.ability_type ?? ability?.skill_type;
    out.abilityType = (typeof rawType === "number" || /^\d{1,4}$/.test(String(rawType))) && Number.isFinite(Number(rawType)) ? Number(rawType) : null;
    const params = decode(ability?.ability_param ?? ability?.variables); out.parameterKeys = keys(params);
    out.videoRecognized = out.abilityType == null && out.abilitySource ? null : out.abilityType === 17 && object(params);
    if (out.abilityType === 17 && !object(params)) out.videoRecognized = null;
    if (out.videoRecognized) {
      out.video = { model: /^[a-zA-Z0-9_.-]{1,80}$/.test(params.model || "") ? params.model : "",
        resolution: /^(?:480|720|1080)p$/i.test(params.resolution || "") ? params.resolution : "",
        duration: (typeof params.duration === "number" || /^\d{1,4}(?:\.\d+)?$/.test(String(params.duration))) && Number.isFinite(Number(params.duration)) ? Number(params.duration) : null,
        ratio: /^\d{1,3}:\d{1,3}$/.test(params.ratio || "") ? params.ratio : "", keys: out.parameterKeys };
    }
    let found = false;
    const seen = new Set();
    function text(value, path) {
      if (typeof value !== "string") return;
      if (typeof visitText === "function") visitText(value, path, "body");
      if (!out.textSources.includes(path) && out.textSources.length < 32) out.textSources.push(path);
      if (value.includes(prefix)) found = true;
    }
    function blocks(value, path, depth) {
      if (!Array.isArray(value)) return false;
      for (const block of value.slice(0, 128)) {
        const type = Number(block?.block_type);
        if (Number.isFinite(type) && out.textBlockTypes.length < 32) out.textBlockTypes.push(type);
        if ([1, 10000].includes(type)) fields(block, path + "[]", depth + 1);
      }
      return true;
    }
    function content(value, path, depth) {
      if (depth > 8) { out.unknownTextSources.push(path); return; }
      if (typeof value === "string") {
        const parsed = /^\s*[\[{"]/.test(value) ? decode(value) : null;
        if (parsed != null && typeof parsed !== "string") { content(parsed, path + "<json>", depth + 1); return; }
        text(typeof parsed === "string" ? parsed : value, path); return;
      }
      if (blocks(value, path, depth)) return;
      if (object(value)) fields(value, path, depth + 1);
    }
    function fields(value, path, depth = 0) {
      if (!object(value) || seen.has(value) || depth > 8) return;
      seen.add(value); const previous = out.textSources.length;
      for (const key of ["text", "prompt", "message_text"]) if (Object.hasOwn(value, key)) text(value[key], path + "." + key);
      for (const key of ["content_blocks_v2", "content_blocks", "content_block", "blocks"]) {
        if (!Object.hasOwn(value, key)) continue;
        const decoded = decode(value[key]);
        if (!blocks(decoded, path + "." + key + (typeof value[key] === "string" ? "<json>" : ""), depth)) out.unknownTextSources.push(path + "." + key);
      }
      for (const key of ["content", "body", "text_block"]) if (Object.hasOwn(value, key)) content(value[key], path + "." + key, depth + 1);
      if (previous === out.textSources.length && !out.unknownTextSources.includes(path) &&
          !["content_blocks_v2", "content_blocks", "content_block", "blocks"].some(key => Object.hasOwn(value, key))) out.unknownTextSources.push(path);
    }
    fields(message, "messages[]");
    const box = decode(params?.input_box_content);
    out.inputBoxKeys = keys(box);
    if (object(box) && typeof box.user_input_content === "string") {
      if (typeof visitText === "function") visitText(box.user_input_content, "input_box_content.user_input_content", "inputBox");
      out.inputBoxAvailable = true; out.inputBoxPrefixPresent = box.user_input_content.includes(prefix);
      found ||= out.inputBoxPrefixPresent;
    }
    out.unknownTextSources = [...new Set(out.unknownTextSources)].slice(0, 16);
    out.prefixChecked = out.textSources.length > 0 && out.unknownTextSources.length === 0;
    // A recognized prefix in either representation proves presence. Unknown
    // text layouts cannot prove absence, even when an input-box mirror is empty.
    out.prefixPresent = found ? true : out.prefixChecked ? false : out.videoRecognized !== false ? null : false;
    return out;
  }
  Object.defineProperty(globalThis, "__DOUBAO_DOLA_VIDEO_REQUEST_SCHEMA__", { value: Object.freeze({ inspect }) });
})();
