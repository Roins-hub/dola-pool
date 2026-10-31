(function () {
  "use strict";
  const INSTALL = "__doubaoGenerationIntentV1629";
  const KEY = "intl_doubao_generation_intent_v1";
  const ATTRIBUTE = "data-watermark-free-generation-intent";
  const EVENT = "watermark-free-generation-intent";
  const PREF_ATTRIBUTE = "data-watermark-free-generation-intent-enabled";
  const PREF_EVENT = "watermark-free-generation-intent-change";
  const PREFIX = "【视频生成请求】\n请现在调用视频生成工具，依据以下内容和本条参考素材制作视频。模型、时长、比例、分辨率以本次视频工具参数为准，已确认，无需再次询问或改写为参数确认、分镜方案。若无法实际调用，请直接说明权限、额度或工具错误；不得用文字声称已经生成。\n【原始内容】\n";
  if (!/(^|\.)doubao\.com$/i.test(location.hostname) || globalThis[INSTALL]) return;
  globalThis[INSTALL] = true;

  let lastPublished = "";
  const state = { version: "1.6.35", enabled: true, preparedCount: 0, phase: "idle", last: null,
    reason: "等待视频生成提交，将添加调用视频工具的指令" };
  function enabled() { try { return localStorage.getItem(KEY) !== "0"; } catch { return false; } }
  function object(value) { return value && typeof value === "object" && !Array.isArray(value); }
  function decode(value) {
    try { const result = typeof value === "string" ? JSON.parse(value) : value; return object(result) ? result : null; }
    catch { return null; }
  }
  function publish() {
    state.enabled = enabled();
    const encoded = JSON.stringify(state);
    if (encoded === lastPublished || !document.documentElement) return;
    lastPublished = encoded;
    document.documentElement.setAttribute(ATTRIBUTE, encoded);
    document.dispatchEvent(new Event(EVENT));
  }
  function currentVideo(request) {
    if (!object(request) || !Array.isArray(request.messages) || !request.messages.length) return null;
    // Only the current message is eligible. Never search backwards into history.
    const index = request.messages.length - 1;
    const message = request.messages[index];
    if (!object(message) || /^(?:system|assistant|bot|tool)$/i.test(String(
      message.role || message.sender_role || message.sender_type || ""))) return null;
    const ability = decode(message.ext?.chat_ability);
    const params = decode(ability?.ability_param);
    return Number(ability?.ability_type) === 17 && params ? { index, message, ability, params } : null;
  }
  function atPath(value, path, transform) {
    if (!path.length) return transform(value);
    if (!object(value)) return value;
    const [key, ...rest] = path;
    if (!Object.hasOwn(value, key)) return value;
    const next = atPath(value[key], rest, transform);
    return next === value[key] ? value : { ...value, [key]: next };
  }
  function contentText(value, transform) {
    if (typeof value !== "string") return value;
    if (/^\s*[\[{]/.test(value)) {
      let parsed;
      try { parsed = JSON.parse(value); } catch { return transform(value); }
      if (!object(parsed)) return value;
      let next = atPath(parsed, ["text"], transform);
      next = atPath(next, ["text_block", "text"], transform);
      return next === parsed ? value : JSON.stringify(next);
    }
    return transform(value);
  }
  function patchFields(value, transform) {
    let next = value;
    for (const path of [["text"], ["prompt"], ["content", "text"], ["content", "text_block", "text"]]) {
      next = atPath(next, path, transform);
    }
    if (typeof value.content === "string") next = atPath(next, ["content"], text => contentText(text, transform));
    return next;
  }
  function mapMessage(message, transform) {
    let next = patchFields(message, transform);
    if (Array.isArray(message.content_blocks_v2)) {
      let used = false;
      const blocks = message.content_blocks_v2.map(block => {
        if (used || ![1, 10000].includes(Number(block?.block_type))) return block;
        let hasText = false;
        const output = patchFields(block, text => {
          if (typeof text !== "string" || !text.trim()) return text;
          hasText = true;
          return transform(text);
        });
        used = hasText;
        return output;
      });
      if (blocks.some((block, index) => block !== message.content_blocks_v2[index])) next = { ...next, content_blocks_v2: blocks };
    }
    return next;
  }
  function inspect(request) {
    const schema = globalThis.__DOUBAO_DOLA_VIDEO_REQUEST_SCHEMA__;
    if (schema) {
      const value = schema.inspect(request, PREFIX);
      return { video: value.videoRecognized, present: value.prefixPresent, inputBoxPresent: value.inputBoxPrefixPresent };
    }
    return { video: null, present: null, inputBoxPresent: false };
  }
  function prepare(request) {
    if (!enabled()) {
      state.phase = "disabled"; state.last = null; state.reason = "视频生成指令前缀已关闭"; publish(); return request;
    }
    const current = currentVideo(request);
    if (!current) {
      state.phase = "skipped"; state.last = { recognized: false, added: false, present: false };
      state.reason = "当前提交没有视频能力参数，请从视频生成入口发送"; publish(); return request;
    }
    let fieldsAdded = 0;
    const add = text => {
      if (typeof text !== "string" || !text.trim() || text.includes(PREFIX)) return text;
      fieldsAdded += 1; return PREFIX + text;
    };
    let next = mapMessage(current.message, add);
    const box = decode(current.params.input_box_content);
    const nextBox = box ? atPath(box, ["user_input_content"], add) : box;
    if (nextBox !== box) {
      const params = { ...current.params, input_box_content: typeof current.params.input_box_content === "string" ? JSON.stringify(nextBox) : nextBox };
      const ability = { ...current.ability, ability_param: typeof current.ability.ability_param === "string" ? JSON.stringify(params) : params };
      next = { ...next, ext: { ...current.message.ext,
        chat_ability: typeof current.message.ext.chat_ability === "string" ? JSON.stringify(ability) : ability } };
    }
    let output = request;
    if (next !== current.message) {
      const messages = [...request.messages]; messages[current.index] = next; output = { ...request, messages };
      state.preparedCount += 1;
    }
    const observed = inspect(output);
    state.last = { recognized: true, added: output !== request, present: observed.present, fieldsAdded,
      inputBoxPresent: observed.inputBoxPresent };
    state.phase = observed.present ? "prepared" : "skipped";
    state.reason = observed.present ? "本次视频提交已带生成指令；任务结果以平台返回为准" : "未识别当前视频消息的文字字段，未添加前缀";
    publish(); return output;
  }
  document.addEventListener(PREF_EVENT, () => {
    const value = document.documentElement?.getAttribute(PREF_ATTRIBUTE);
    if (value !== "0" && value !== "1") return;
    try { localStorage.setItem(KEY, value); } catch { return; }
    state.phase = value === "0" ? "disabled" : "idle";
    state.reason = value === "0" ? "视频生成指令前缀已关闭" : "等待视频生成提交，将添加调用视频工具的指令";
    publish();
  });
  Object.defineProperty(globalThis, "__DOUBAO_GENERATION_INTENT__", { configurable: true, value: Object.freeze({
    prefix: PREFIX, prepare, inspect,
    get status() { return JSON.parse(JSON.stringify({ ...state, enabled: enabled() })); }
  }) });
  document.addEventListener("DOMContentLoaded", publish, { once: true }); publish();
})();
