(function () {
  "use strict";

  const INSTALL = "__doubaoDolaTimelineProgressV1622";
  const API = "__DOUBAO_DOLA_TIMELINE_PROGRESS__";
  const IS_DOLA = /(^|\.)dola\.com$/i.test(location.hostname);
  if (!/(^|\.)(doubao|dola)\.com$/i.test(location.hostname) || globalThis[INSTALL]) return;
  globalThis[INSTALL] = true;

  // Work only on native video drafts before serialization/signing. Never
  // change the composer, HTTP transport, capability settings or replies.
  const NUMBER = String.raw`(?:\d+(?:\.\d+)?|半|[零〇一二两三四五六七八九十]{1,3}(?:点[零〇一二两三四五六七八九]+)?)`;
  const SPACE = String.raw`[ \t\u00a0]*`;
  const UNIT = String.raw`(?:秒(?:钟)?|seconds?|secs?|s)(?![a-zA-Z0-9_])`;
  const CLOCK = String.raw`\d{1,2}(?:[:：][0-5]\d){1,2}`;
  const SEPARATOR = String.raw`(?:-|－|–|—|~|～|至|到|→)`;
  const BOUNDARY = String.raw`(^|[^\w.:：%\-－–—])`;

  function object(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === null || prototype?.constructor?.name === "Object";
  }

  function decode(value) {
    try {
      const parsed = typeof value === "string" ? JSON.parse(value) : value;
      return object(parsed) ? parsed : null;
    } catch { return null; }
  }

  function duration(value) {
    const result = Number(value);
    return Number.isFinite(result) && result > 0 && result <= 600 ? result : null;
  }

  function seconds(raw) {
    if (/^\d+(?:\.\d+)?$/.test(raw)) return Number(raw);
    if (raw === "半") return 0.5;
    const digits = { 零: 0, "〇": 0, 一: 1, 二: 2, 两: 2, 三: 3, 四: 4, 五: 5, 六: 6, 七: 7, 八: 8, 九: 9 };
    const [integerText, decimalText = ""] = raw.split("点");
    let integer;
    if (integerText.includes("十")) {
      const [tensText, onesText, extra] = integerText.split("十");
      if (extra != null || (tensText && !Object.hasOwn(digits, tensText)) ||
          (onesText && !Object.hasOwn(digits, onesText))) return NaN;
      integer = (tensText ? digits[tensText] : 1) * 10 + (onesText ? digits[onesText] : 0);
    } else {
      if (!Object.hasOwn(digits, integerText)) return NaN;
      integer = digits[integerText];
    }
    if (![...decimalText].every((character) => Object.hasOwn(digits, character))) return NaN;
    return decimalText ? Number(`${integer}.${[...decimalText].map((character) => digits[character]).join("")}`) : integer;
  }

  function clockSeconds(raw) {
    return raw.split(/[:：]/).reduce((sum, value) => sum * 60 + Number(value), 0);
  }

  function rewrite(text, total) {
    total = duration(total);
    if (typeof text !== "string" || !text || total == null) return text;
    // Signed media/reference URLs are not authored timeline instructions.
    return text.split(/(https?:\/\/[^\s<>"']+)/gi)
      .map((part) => /^https?:\/\//i.test(part) ? part : rewriteSegment(part, total)).join("");
  }

  function rewriteSegment(text, total) {
    const percent = (value) => Number((value / total * 100).toFixed(1)).toString();
    const valid = (value) => Number.isFinite(value) && value >= 0 && value <= total;
    const range = (match, prefix, start, end) => {
      if (!valid(start) || !valid(end) || end <= start) return match;
      return `${prefix}全片进度 ${percent(start)}%–${percent(end)}%`;
    };
    let output = text.replace(
      new RegExp(`${BOUNDARY}(${CLOCK})${SPACE}${SEPARATOR}${SPACE}(${CLOCK})(?:${SPACE}${UNIT})?(?![\\d:：])`, "gi"),
      (match, prefix, start, end) => range(match, prefix, clockSeconds(start), clockSeconds(end))
    );
    // Include the unit on either or both endpoints: 17–22 秒, 0s-3s,
    // [0-3s], 0秒至5秒 and 十七到二十二秒 all denote one interval.
    output = output.replace(
      new RegExp(`${BOUNDARY}(${NUMBER})${SPACE}(?:${UNIT}${SPACE})?${SEPARATOR}${SPACE}(${NUMBER})${SPACE}${UNIT}`, "gi"),
      (match, prefix, start, end) => range(match, prefix, seconds(start), seconds(end))
    );
    output = output.replace(
      new RegExp(`${BOUNDARY}(${CLOCK})(?:${SPACE}${UNIT})?(?![\\d:：])`, "gi"),
      (match, prefix, clock, offset, source) => {
        const point = clockSeconds(clock);
        // Do not partly convert an invalid range, URL or numeric suffix.
        const before = source.slice(Math.max(0, offset - 16), offset + prefix.length);
        const after = source.slice(offset + match.length);
        if (!valid(point) || /(?:https?:\/\/|[\-－–—~～→至到])[ \t]*$/i.test(before) ||
            new RegExp(`^${SPACE}${SEPARATOR}${SPACE}(?:${CLOCK}|${NUMBER})`).test(after)) return match;
        return `${prefix}全片进度约 ${percent(point)}% 处`;
      }
    );
    // Explicit time points and action durations retain their meaning. Bare
    // total-duration declarations and user-authored trailing 30s stay intact.
    output = output.replace(
      new RegExp(`(第|截至|接近|在|于|到|至|持续|保持|停留|停顿)(${SPACE})(${NUMBER})${SPACE}${UNIT}`, "gi"),
      (match, cue, gap, raw, offset, source) => {
        const value = seconds(raw);
        if (!valid(value)) return match;
        if (/到|至/.test(cue) && new RegExp(`${NUMBER}${SPACE}(?:${UNIT}${SPACE})?$`, "i")
          .test(source.slice(0, offset))) return match;
        if (/持续|保持|停留|停顿/.test(cue)) return `${cue}${gap}约占全片 ${percent(value)}% 的时长`;
        return `${cue === "第" ? "" : cue}${gap}全片进度约 ${percent(value)}% 处`;
      }
    );
    return output;
  }

  function atPath(value, path, transform) {
    if (!path.length) return transform(value);
    if (!object(value)) return value;
    const [key, ...rest] = path;
    if (!Object.hasOwn(value, key)) return value;
    const next = atPath(value[key], rest, transform);
    return next === value[key] ? value : { ...value, [key]: next };
  }

  function textValue(value, transform) {
    if (typeof value !== "string") return value;
    if (/^\s*[{[]/.test(value)) {
      let parsed;
      try { parsed = JSON.parse(value); }
      catch { return transform(value); }
      if (!object(parsed)) return value;
      const next = atPath(parsed, ["text"], transform);
      return next === parsed ? value : JSON.stringify(next);
    }
    return transform(value);
  }

  function patchText(message, transform, body = false) {
    let next = message;
    for (const path of [["text"], ["prompt"], ["content", "text"], ["content", "text_block", "text"]]) {
      next = atPath(next, path, transform);
    }
    for (const key of body ? ["content", "body"] : ["content"]) {
      if (typeof message[key] === "string") next = atPath(next, [key], (value) => textValue(value, transform));
      else if (body && key === "body") next = atPath(next, [key, "text"], transform);
    }
    if (Array.isArray(message.content_blocks_v2)) {
      const blocks = message.content_blocks_v2.map((block) => {
        if (![1, 10000].includes(Number(block?.block_type))) return block;
        let output = block;
        for (const path of [["text"], ["content", "text"], ["content", "text_block", "text"]]) {
          output = atPath(output, path, transform);
        }
        return output;
      });
      if (blocks.some((block, index) => block !== message.content_blocks_v2[index])) next = { ...next, content_blocks_v2: blocks };
    }
    return next;
  }

  function patchInputBox(params, transform) {
    const box = decode(params.input_box_content);
    if (!box) return params;
    const next = atPath(box, ["user_input_content"], transform);
    if (next === box) return params;
    return { ...params, input_box_content: typeof params.input_box_content === "string" ? JSON.stringify(next) : next };
  }

  function currentIndex(messages) {
    let index = messages.length - 1;
    while (index >= 0 && /system|assistant|bot|tool/i.test(String(
      messages[index]?.role || messages[index]?.sender_role || messages[index]?.sender_type || ""))) index -= 1;
    return index;
  }

  function prepare(request) {
    if (!object(request) || !Array.isArray(request.messages)) return request;
    const index = currentIndex(request.messages);
    const message = request.messages[index];
    if (!object(message)) return request;
    const ability = decode(message.ext?.chat_ability);
    const params = decode(ability?.ability_param);
    const total = duration(params?.duration);
    if (Number(ability?.ability_type) !== 17 || !params || total == null) return request;
    const transform = (text) => rewrite(text, total);
    let next = patchText(message, transform);
    const nextParams = patchInputBox(params, transform);
    if (nextParams !== params) {
      const nextAbility = { ...ability, ability_param: typeof ability.ability_param === "string" ? JSON.stringify(nextParams) : nextParams };
      next = { ...next, ext: { ...message.ext,
        chat_ability: typeof message.ext.chat_ability === "string" ? JSON.stringify(nextAbility) : nextAbility } };
    }
    if (next === message) return request;
    const messages = [...request.messages]; messages[index] = next;
    return { ...request, messages };
  }

  function preparePipeline(context) {
    const content = context?.content;
    const params = content?.abilityParams;
    if (!IS_DOLA || !object(context) || !object(content) || !object(params) ||
        (content.abilityType != null && Number(content.abilityType) !== 17)) return context;
    const total = duration(params.duration);
    if (total == null) return context;
    const transform = (text) => rewrite(text, total);
    let next = atPath(content, ["text"], transform);
    const nextParams = patchInputBox(params, transform);
    if (nextParams !== params) next = { ...next, abilityParams: nextParams };
    return next === content ? context : { ...context, content: next };
  }

  function prepareLegacy(request) {
    if (!IS_DOLA || !Array.isArray(request)) return request;
    const index = currentIndex(request);
    const message = request[index];
    if (!object(message)) return request;
    const key = message.extraExt?.input_skill != null ? "extraExt" : "ext";
    const skill = decode(message[key]?.input_skill);
    const variables = decode(skill?.variables);
    const total = duration(variables?.duration);
    if (Number(skill?.skill_type) !== 17 || !variables || total == null) return request;
    const transform = (text) => rewrite(text, total);
    let next = patchText(message, transform, true);
    const nextVariables = patchInputBox(variables, transform);
    if (nextVariables !== variables) {
      const nextSkill = { ...skill, variables: typeof skill.variables === "string" ? JSON.stringify(nextVariables) : nextVariables };
      next = { ...next, [key]: { ...message[key],
        input_skill: typeof message[key].input_skill === "string" ? JSON.stringify(nextSkill) : nextSkill } };
    }
    if (next === message) return request;
    const messages = [...request]; messages[index] = next;
    return messages;
  }

  Object.defineProperty(globalThis, API, {
    configurable: true, value: Object.freeze({ version: "1.6.24", rewrite, prepare, preparePipeline, prepareLegacy })
  });
})();
