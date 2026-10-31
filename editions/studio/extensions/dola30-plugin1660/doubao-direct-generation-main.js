(function () {
  "use strict";

  const INSTALL = "__doubaoDirectGenerationV1621";
  const API = "__DOUBAO_VIDEO_DIRECT_GENERATION__";
  const MARK = "【直接生成控制】";
  if (!/(^|\.)doubao\.com$/i.test(location.hostname) || globalThis[INSTALL]) return;
  globalThis[INSTALL] = true;

  // Reuse the original Dola prompt-constraint approach for domestic video
  // submissions only. It is a prefix, with no numeric duration or time suffix.
  const GUARD = [
    MARK,
    "用户已在页面选定视频参数，以本次请求携带的能力参数为准。",
    "直接调用一次视频生成能力，不要反问，不要要求用户再次确认，不要要求回复确认词或选择选项。",
    "不要先输出参数确认、分镜表或待确认方案；不要在回复文本中复述比例、时长、参考图或画面描述。",
    "能力调用必须完整携带下方剧本、关键词和全部素材引用，不得只传摘要，不得自行删改正文。",
    "仅在生成能力成功受理后回复：任务已受理，正在生成。调用失败时如实简要说明原因，不得声称已经受理或开始生成。",
    "这些控制语句只用于调度，不得渲染成画面字幕、旁白或水印。"
  ].join("");

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

  function prefix(text) {
    if (typeof text !== "string" || !text.trim() || text.includes(MARK)) return text;
    return `${GUARD}\n\n${text}`;
  }

  function atPath(value, path) {
    if (!path.length) return prefix(value);
    if (!object(value)) return value;
    const [key, ...rest] = path;
    if (!Object.hasOwn(value, key)) return value;
    const next = atPath(value[key], rest);
    return next === value[key] ? value : { ...value, [key]: next };
  }

  function patchText(message) {
    let next = message;
    for (const path of [["text"], ["prompt"], ["content", "text"], ["content", "text_block", "text"]]) {
      next = atPath(next, path);
    }
    if (typeof message.content === "string") {
      if (/^\s*[{[]/.test(message.content)) {
        let parsed;
        try { parsed = JSON.parse(message.content); }
        catch { next = atPath(next, ["content"]); }
        if (object(parsed)) {
          const content = atPath(parsed, ["text"]);
          if (content !== parsed) next = { ...next, content: JSON.stringify(content) };
        }
      } else next = atPath(next, ["content"]);
    }
    if (Array.isArray(message.content_blocks_v2)) {
      // Prefix one authored text block; retain the rich reference/attachment
      // blocks and the remaining text blocks in their original order.
      let added = false;
      const alreadyPresent = message.content_blocks_v2.some((block) => {
        if (![1, 10000].includes(Number(block?.block_type))) return false;
        return [block.text, block.content?.text, block.content?.text_block?.text]
          .some((text) => typeof text === "string" && text.includes(MARK));
      });
      if (!alreadyPresent) {
        const blocks = message.content_blocks_v2.map((block) => {
          if (added || ![1, 10000].includes(Number(block?.block_type))) return block;
          let output = block;
          for (const path of [["text"], ["content", "text"], ["content", "text_block", "text"]]) {
            output = atPath(output, path);
          }
          added = output !== block;
          return output;
        });
        if (added) next = { ...next, content_blocks_v2: blocks };
      }
    }
    return next;
  }

  function patchAbilityPrompt(message, ability) {
    const params = decode(ability.ability_param);
    if (!params) return message;
    const raw = params.input_box_content;
    const box = decode(raw);
    if (!box) return message;
    const updated = atPath(box, ["user_input_content"]);
    if (updated === box) return message;
    const nextParams = { ...params, input_box_content: typeof raw === "string" ? JSON.stringify(updated) : updated };
    const nextAbility = { ...ability, ability_param: typeof ability.ability_param === "string" ? JSON.stringify(nextParams) : nextParams };
    return { ...message, ext: { ...message.ext,
      chat_ability: typeof message.ext.chat_ability === "string" ? JSON.stringify(nextAbility) : nextAbility } };
  }

  function prepare(request) {
    if (!object(request) || !Array.isArray(request.messages)) return request;
    let index = request.messages.length - 1;
    while (index >= 0) {
      const message = request.messages[index];
      const role = String(message?.role || message?.sender_role || message?.sender_type || "");
      if (!/system|assistant|bot|tool/i.test(role)) break;
      index -= 1;
    }
    if (index < 0) return request;
    const message = request.messages[index];
    const ability = decode(message?.ext?.chat_ability);
    if (Number(ability?.ability_type) !== 17) return request;
    let next = patchText(message);
    next = patchAbilityPrompt(next, ability);
    if (next === message) return request;
    const messages = [...request.messages]; messages[index] = next;
    return { ...request, messages };
  }

  Object.defineProperty(globalThis, API, {
    configurable: true, value: Object.freeze({ version: "1.6.24", prepare })
  });
})();
