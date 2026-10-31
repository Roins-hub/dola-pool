(function () {
  "use strict";
  const API = "__DOUBAO_DOLA_TWO_PART__";
  if (!/(^|\.)(doubao|dola)\.com$/i.test(location.hostname) || globalThis[API]) return;
  const KEY = "dola_two_part_rule_v1";
  const ATTRIBUTE = "data-dola-two-part-status";
  const EVENT = "dola-two-part-status";
  const PREF = "data-dola-two-part-enabled";
  const CHANGE = "dola-two-part-change";
  const MARK = "【插件两段式生成规则 v2】";
  const LEGACY_MARK = "【插件两段式生成规则 v1】";
  const RULE = MARK + "\n生成两段视频拆分：本次需要两条独立视频，每条15秒，合计30秒，不要把两段压成一条15秒视频，也不要输出第三条。" +
    "优先按剧本已有的第01段和第02段划分；没有明确分段时，按剧情顺序分成前后两段。全片进度前半对应第一条，后半对应第二条。" +
    "两条均沿用本次选择的模型、分辨率和画幅比例，保持角色、服装、场景、参考素材、关键动作与对白一致，不随意改写剧本。" +
    "请实际生成并分别返回两条原生视频；禁止下载后用 ffmpeg 或云盘拼接，禁止输出合成文件，因为页面下载源可能带水印。" +
    "两条原始生成结果由本地无水印助手按当前会话解析、排序和下载；若平台不支持，请如实说明，不要声称已经完成。\n【插件两段式生成规则结束】";
  const object = value => value && typeof value === "object" && !Array.isArray(value) &&
    (Object.getPrototypeOf(value) === null || Object.getPrototypeOf(value)?.constructor?.name === "Object");
  const decode = value => { try { return typeof value === "string" ? JSON.parse(value) : value; } catch { return null; } };
  const encode = (original, value) => typeof original === "string" ? JSON.stringify(value) : value;
  const state = { version: "1.6.60", phase: "idle", applied: false };
  function enabled() { try { return localStorage.getItem(KEY) === "1"; } catch { return false; } }
  function publish() {
    document.documentElement?.setAttribute("data-dola-two-part-rule", RULE);
    document.documentElement?.setAttribute(ATTRIBUTE, JSON.stringify({ ...state, enabled: enabled(), mode: "prompt_rule", totalDuration: 30, segmentDuration: 15 }));
    document.dispatchEvent(new Event(EVENT));
  }
  function add(text) {
    return typeof text === "string" && text.trim() && !text.includes(MARK) && !text.includes(LEGACY_MARK) ? text + "\n\n" + RULE : text;
  }
  function content(value, depth = 0) {
    if (depth > 10) return value;
    if (typeof value === "string") {
      const parsed = /^\s*[\[{\"]/.test(value) ? decode(value) : null;
      if (parsed != null) { const next = content(parsed, depth + 1); return next === parsed ? value : JSON.stringify(next); }
      return add(value);
    }
    if (Array.isArray(value)) {
      // One rule per text-block list; attachments are never interpreted as text.
      let used = false;
      const next = value.map(block => {
        if (used || ![1, 10000].includes(Number(block?.block_type))) return block;
        const result = fields(block, depth + 1);
        used = result !== block || JSON.stringify(block).includes(MARK);
        return result;
      });
      return next.some((entry, i) => entry !== value[i]) ? next : value;
    }
    return fields(value, depth + 1);
  }
  function fields(value, depth = 0) {
    if (!object(value) || depth > 10) return value;
    let next = value;
    for (const key of ["text", "prompt", "message_text", "content", "body", "text_block", "content_blocks_v2", "content_blocks", "content_block", "blocks"]) {
      if (!Object.hasOwn(value, key)) continue;
      const updated = ["text", "prompt", "message_text"].includes(key) ? add(value[key]) : content(value[key], depth + 1);
      if (updated !== value[key]) next = { ...next, [key]: updated };
    }
    return next;
  }
  function parameters(params) {
    const box = decode(params.input_box_content);
    if (!object(box)) return params;
    const text = add(box.user_input_content);
    return text === box.user_input_content ? params : { ...params,
      input_box_content: encode(params.input_box_content, { ...box, user_input_content: text }) };
  }
  function eligible(params) {
    if (!enabled()) { state.phase = "disabled"; state.applied = false; publish(); return false; }
    if (!object(params) || Number(params.duration) !== 30) {
      state.phase = "requires_30s"; state.applied = false; publish(); return false;
    }
    return true;
  }
  function done(output, original) {
    state.phase = output === original ? "unchanged" : "prepared";
    state.applied = output !== original; publish(); return output;
  }
  function prepare(request) {
    const rawMessages = Array.isArray(request) ? request : request?.messages;
    const messages = decode(rawMessages);
    if (!Array.isArray(messages) || !messages.length) return request;
    const index = messages.length - 1, message = messages[index];
    if (!object(message) || /^(assistant|bot|tool|system)$/i.test(String(message.role || message.sender_role || message.sender_type || ""))) return request;
    const top = object(request) && Object.hasOwn(request, "chat_ability");
    const container = message.ext?.chat_ability != null ? "ext" : message.extraExt?.input_skill != null ? "extraExt" : "ext";
    const key = top || message[container]?.chat_ability != null ? "chat_ability" : "input_skill";
    const raw = top ? request.chat_ability : message[container]?.[key], ability = decode(raw);
    const paramKey = key === "input_skill" ? "variables" : "ability_param";
    const params = decode(ability?.[paramKey]);
    if (Number(ability?.ability_type ?? ability?.skill_type) !== 17 || !eligible(params)) return request;
    let next = fields(message);
    const updatedParams = parameters(params);
    const updatedAbility = updatedParams === params ? raw : encode(raw, { ...ability, [paramKey]: encode(ability[paramKey], updatedParams) });
    if (!top && updatedAbility !== raw) next = { ...next, [container]: { ...message[container], [key]: updatedAbility } };
    if (next === message && updatedAbility === raw) return done(request, request);
    const updatedMessages = [...messages]; updatedMessages[index] = next;
    const output = Array.isArray(request) ? updatedMessages : { ...request, messages: encode(rawMessages, updatedMessages), ...(top ? { chat_ability: updatedAbility } : {}) };
    return done(output, request);
  }
  function preparePipeline(context) {
    const current = context?.content;
    if (!object(current) || (current.abilityType != null && Number(current.abilityType) !== 17) || !eligible(current.abilityParams)) return context;
    const next = fields(current), params = parameters(current.abilityParams);
    return done(next === current && params === current.abilityParams ? context : { ...context, content: { ...next, abilityParams: params } }, context);
  }
  document.addEventListener(CHANGE, () => {
    const value = document.documentElement?.getAttribute(PREF);
    if (!["0", "1"].includes(value)) return;
    try { localStorage.setItem(KEY, value); } catch { state.phase = "storage_error"; publish(); return; }
    state.phase = value === "1" ? "idle" : "disabled"; state.applied = false; publish();
  });
  Object.defineProperty(globalThis, API, { value: Object.freeze({ prepare, prepareLegacy: prepare, preparePipeline, rule: RULE }) });
  document.addEventListener("DOMContentLoaded", publish, { once: true }); publish();
})();
