(function () {
  "use strict";
  if (globalThis.__DOUBAO_DOLA_NETWORK_SUMMARY__) return;
  const LIMIT = 2_000_000;
  const textKeys = new Set(["text", "prompt", "message_text", "user_input_content", "input_text", "reasoning_content"]);
  const encodedKeys = new Set(["data", "event_data", "payload", "result", "body", "content", "extra", "ext", "video_generation", "task", "message", "custom_logic", "tool_call", "tool_result", "general_task_param"]);
  const entitlementKeys = new Set(["has_active_subscription", "has_enterprise_subscription", "has_business_subscription", "has_team_subscription",
    "is_trialing", "usage_exhausted", "should_prompt_purchase", "entitlement_count", "used_percent", "less_than_one_percent", "feature_group", "feature_group_name", "item_type", "window_type"]);
  const ids = new Set(["task_id", "video_task_id", "generation_task_id", "generation_id", "video_id", "job_id"]);
  const statuses = new Set(["task_status", "video_status", "generation_status", "job_status"]);
  const costs = new Set(["credit_cost", "credits_cost", "point_cost", "points_cost", "cost_credits", "cost_points", "consume_credits", "consume_points",
    "consumed_credits", "consumed_points", "deducted_credits", "deducted_points", "quota_cost", "quota_consumed", "remaining_credits", "remaining_points", "remaining_quota", "generation_quota"]);
  const codes = new Set(["code", "error_code", "status_code", "biz_code", "ret_code"]);
  const normalize = key => String(key).replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase();
  const object = value => value && typeof value === "object" && !Array.isArray(value);
  const decode = value => { try { const next = typeof value === "string" ? JSON.parse(value) : value; return object(next) ? next : null; } catch { return null; } };
  const numeric = value => (typeof value === "number" || typeof value === "string" && /^-?\d{1,15}(?:\.\d{1,6})?$/.test(value)) && Number.isFinite(Number(value));
  const safeKey = key => /^[a-zA-Z_][a-zA-Z0-9_-]{0,47}$/.test(key) ? key : "[key]";
  const secretKey = key => /(?:token|cookie|authorization|password|secret|signature|a_bogus|x_bogus|api[-_]?key|access[-_]?key)/i.test(key);
  const volatileKey = key => /^(?:local_message_id|message_id|request_id|client_request_id|trace_id|log_id|timestamp|nonce|create_time|update_time)$/.test(key) || /^inner_/.test(key);
  function canonical(value, depth = 0) {
    if (depth > 16) return "[depth-limit]";
    if (Array.isArray(value)) return value.map(item => canonical(item, depth + 1));
    if (!object(value)) return value;
    const out = Object.create(null);
    for (const key of Object.keys(value).sort()) if (!secretKey(key) && !volatileKey(key)) {
      const next = /^(?:general_task_param|input_skill|chat_ability|ability_param|input_box_content)$/.test(key) ? decode(value[key]) ?? value[key] : value[key];
      out[key] = canonical(next, depth + 1);
    }
    return out;
  }
  async function digest(value) {
    if (!globalThis.crypto?.subtle || typeof TextEncoder !== "function") throw Error("SHA256_UNAVAILABLE");
    const bytes = new TextEncoder().encode(JSON.stringify(canonical(value)));
    const hash = await crypto.subtle.digest("SHA-256", bytes);
    return [...new Uint8Array(hash)].map(byte => byte.toString(16).padStart(2, "0")).join("");
  }
  async function requestFields(roots) {
    const fields = []; const pending = [];
    let visits = 0; let complete = true; let omittedSecrets = 0;
    function walk(value, path, key, depth = 0) {
      if (++visits > 4096 || depth > 12 || fields.length >= 256) { complete = false; return; }
      if (secretKey(key)) { omittedSecrets += 1; return; }
      if (typeof value === "string" && /^\s*[\[{]/.test(value)) {
        try { const decoded = JSON.parse(value); if (decoded && typeof decoded === "object") value = decoded; } catch {}
      }
      if (value && typeof value === "object" && Object.keys(value).length) {
        for (const childKey of Object.keys(value).sort()) {
          const safe = Array.isArray(value) ? /^\d{1,4}$/.test(childKey) : safeKey(childKey) === childKey;
          if (!safe) { complete = false; continue; }
          const childPath = path + (Array.isArray(value) ? `[${childKey}]` : "." + childKey);
          if (childPath.length > 280) { complete = false; continue; }
          walk(value[childKey], childPath, childKey, depth + 1);
          if (visits > 4096 || fields.length >= 256) { complete = false; break; }
        }
        return;
      }
      if (value === undefined) return;
      const type = value === null ? "null" : Array.isArray(value) ? "array" : typeof value;
      const name = normalize(key);
      const identifier = /(?:^|_)(?:id|uuid|cursor)$/.test(name);
      const transient = volatileKey(name) || /^(?:sequence_id|seq_id|create_timestamp|update_timestamp)$/.test(name);
      const category = identifier ? "identifier" : transient ? "transient" : "control_or_context";
      const entry = { path, type, category };
      // Unknown strings and identifiers stay hashes. Bounded enum values in
      // named control fields are retained so a route difference is actionable.
      if (category === "control_or_context" && (typeof value === "boolean" ||
          path.startsWith("option.") && typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= 1_000_000)) entry.value = value;
      if (category === "control_or_context" && typeof value === "string" &&
          /(?:^|_)(?:model|mode|type|scene|source|strategy|version|format|switch|enable|enabled)$/.test(name) &&
          /^[a-zA-Z_][a-zA-Z0-9_.:-]{0,63}$/.test(value)) entry.value = value;
      if (typeof value === "string") entry.characters = value.length;
      fields.push(entry); pending.push(digest(value).then(sha256 => { entry.sha256 = sha256; }));
    }
    for (const [key, value] of Object.entries(roots)) if (value !== undefined) walk(value, key, key);
    await Promise.all(pending);
    return { complete, omittedSecrets, fields };
  }
  async function compareRequest(raw) {
    if (typeof raw !== "string" || raw.length > LIMIT) return { error: "REQUEST_BODY_UNAVAILABLE" };
    let data; try { data = JSON.parse(raw); } catch { return { error: "REQUEST_NOT_JSON" }; }
    const parts = []; let box = null;
    const schema = globalThis.__DOUBAO_DOLA_VIDEO_REQUEST_SCHEMA__?.inspect(data, "【视频生成请求】", (value, path, kind) => {
      if (kind === "inputBox") box = value;
      else parts.push({ value: value.replace(/\r\n/g, "\n"), family: path.match(/^messages\[\]\.([a-z0-9_]+)/)?.[1] || "unknown" });
    });
    const families = ["content_block", "content_blocks_v2", "content_blocks", "blocks", "content", "body", "text", "prompt", "message_text"];
    const family = families.find(key => parts.some(part => part.family === key));
    const text = family ? parts.filter(part => part.family === family).map(part => part.value) : null;
    const message = Array.isArray(data?.messages) ? data.messages.at(-1) : Array.isArray(data) ? data.at(-1) : null;
    const parsed = value => decode(value) ?? value;
    const roots = { option: parsed(data?.option), ext: parsed(data?.ext), messageExt: parsed(message?.ext), clientMeta: parsed(data?.client_meta), userContext: parsed(data?.user_context) };
    const routeKeys = new Set(["cot_switch", "thinking", "think_mode", "thinking_mode", "deep_think", "is_general_task", "chat_next", "use_content_block",
      "answer_with_suggest", "search_engine_type", "llm_model_type", "model_type", "bot_source", "commerce_credit_config_enable", "read_conv_version", "pre_read_conv_version", "attachment_scene", "task_type"]);
    const routingFields = []; const identities = [];
    function walk(value, path, depth = 0) {
      if (!object(value) || depth > 6) return;
      for (const [key, child] of Object.entries(value)) {
        if (secretKey(key) || volatileKey(key) && key !== "inner_bot_id") continue;
        const here = path + "." + safeKey(key);
        if (/^(?:conversation_id|local_conversation_id|section_id|bot_id|inner_bot_id)$/.test(key) &&
            ["string", "number"].includes(typeof child) && String(child) && identities.length < 16) identities.push({ path: here, value: child });
        const optionBoolean = path.startsWith("option") && typeof child === "boolean";
        if ((routeKeys.has(key) || optionBoolean) && routingFields.length < 32 && (typeof child === "boolean" || numeric(child) || typeof child === "string" && /^[a-zA-Z_][a-zA-Z0-9_.:-]{0,79}$/.test(child))) {
          routingFields.push({ path: here, value: child });
        }
        const parsed = /^(?:general_task_param|input_skill|chat_ability)$/.test(key) ? decode(child) : child;
        if (object(parsed)) walk(parsed, here, depth + 1);
      }
    }
    for (const [key, value] of Object.entries(roots)) walk(value, key);
    const video = schema?.video ? { model: schema.video.model, duration: schema.video.duration, resolution: schema.video.resolution, ratio: schema.video.ratio } : null;
    const ability = data?.chat_ability ?? message?.ext?.chat_ability ?? message?.extraExt?.input_skill ?? message?.ext?.input_skill;
    const projections = { prompt: text, inputBox: box == null ? null : box.replace(/\r\n/g, "\n"), video, ability: ability == null ? null : parsed(ability),
      option: roots.option ?? null, ext: roots.ext ?? null, messageExt: roots.messageExt ?? null, userContext: roots.userContext ?? null };
    const hashes = Object.create(null);
    await Promise.all(Object.entries(projections).map(async ([key, value]) => { hashes[key] = value == null ? null : await digest(value); }));
    const identifierHashes = await Promise.all(identities.map(async item => ({ path: item.path, sha256: await digest(item.value) })));
    const rootAbility = parsed(ability);
    const abilityParams = parsed(rootAbility?.ability_param ?? rootAbility?.variables);
    const inputBox = parsed(abilityParams?.input_box_content);
    const fieldDetails = await requestFields({ ...roots, ability: rootAbility, inputBox });
    const format = inputBox?.reply_message_format;
    const replyFormat = format == null ? null : { type: typeof format, sha256: await digest(format),
      ...(typeof format === "string" ? { characters: format.length, hasPlaceholder: /%s|\{\{[^}]+\}\}|\{\d+\}/.test(format),
        mentionsConfirmation: /确认|confirm/i.test(format), mentionsScript: /剧本|分镜|\b(?:storyboard|script)\b/i.test(format),
        mentionsVideo: /视频|video/i.test(format) } : {}) };
    return { algorithm: "SHA-256", hashes, routingFields, identifiers: identifierHashes,
      fieldDetails, replyFormat,
      promptSource: family || "", promptCoverageComplete: schema?.prefixChecked === true, unknownTextSources: schema?.unknownTextSources || [] };
  }
  function redacted(value) {
    return String(value || "").replace(/https?:\/\/\S+/gi, "[url]")
      .replace(/(?:Bearer\s+)?[A-Za-z0-9_+./=-]{24,}/g, "[redacted]")
      .replace(/\b(?:token|cookie|authorization|signature|sessionid|a_bogus)\s*[:=]\s*[^\s,;]+/gi, "[credential]").slice(0, 160);
  }
  function endpoint(raw) {
    try {
      const url = new URL(raw);
      if (!/^https?:$/.test(url.protocol)) return null;
      const path = url.pathname.split("/").map(part => /^[a-zA-Z_-][a-zA-Z0-9_.-]{0,39}$/.test(part) && !/\d{6}|^[a-f0-9]{16,}$/i.test(part) ? part : part ? "[id]" : "").join("/").slice(0, 240);
      const kind = /\/chat\/completion\/?$/.test(url.pathname) ? "submission" : /\/im\/chain\/single\/?$/.test(url.pathname) ? "history" :
        /skill\/pack|action_bar/.test(url.pathname) ? "configuration" : /video|generat|seedance|task|quota|credits?|points?|upload/i.test(url.pathname) ? "video_related" : "other";
      return { endpoint: url.origin + path, kind, signaturePresent: [...url.searchParams.keys()].some(key => /^(?:a_bogus|x-bogus|signature|sign)$/i.test(key)) };
    } catch { return null; }
  }
  function request(raw) {
    const out = { bodyAvailable: typeof raw === "string", characters: typeof raw === "string" ? raw.length : 0,
      json: false, truncated: false, keys: [], video: null, prefixMarkerPresent: null };
    if (typeof raw !== "string") return out;
    if (raw.length > LIMIT) { out.truncated = true; return out; }
    let data; try { data = JSON.parse(raw); } catch { return out; }
    out.json = true; out.keys = object(data) ? Object.keys(data).slice(0, 20).map(safeKey) : [];
    const schema = globalThis.__DOUBAO_DOLA_VIDEO_REQUEST_SCHEMA__?.inspect(data);
    if (!schema) { out.parseError = "REQUEST_SCHEMA_UNAVAILABLE"; return out; }
    out.messageCount = schema.messageCount;
    out.messageKeys = schema.messageKeys;
    out.abilityType = schema.abilityType;
    out.abilitySource = schema.abilitySource;
    out.abilityKeys = schema.abilityKeys;
    out.shape = schema.shape;
    out.videoRecognized = schema.videoRecognized;
    out.video = schema.video;
    out.textBlockTypes = schema.textBlockTypes;
    out.textSources = schema.textSources;
    out.unknownTextSources = schema.unknownTextSources;
    out.prefixMarkerPresent = schema.prefixPresent;
    out.prefixChecked = schema.prefixChecked;
    out.inputBoxPrefixPresent = schema.inputBoxPrefixPresent;
    out.inputBoxKeys = schema.inputBoxKeys;
    return out;
  }
  function response(raw, endpoint = "") {
    const out = { bodyAvailable: typeof raw === "string", characters: typeof raw === "string" ? raw.length : 0,
      truncated: false, parsedObjects: 0, schema: [], taskFields: [], billingFields: [], statusFields: [], businessErrors: [], textFieldsSeen: 0,
      schemaTruncated: false, detailSchema: [], events: [], eventsTruncated: false, eventCounts: Object.create(null), entitlementFields: [],
      verification: "UNVERIFIED" };
    if (typeof raw !== "string") return out;
    if (raw.length > LIMIT) { out.truncated = true; return out; }
    function add(list, value) { if (list.length < 16 && !list.some(item => JSON.stringify(item) === JSON.stringify(value))) list.push(value); }
    let visited = 0;
    let currentEvent = null;
    const quota = /\/quota\/summary\/?$/.test(endpoint);
    function inspect(value, path = "$", parent = "", depth = 0, encodings = 0) {
      if (depth > 24 || visited > 10_000) { out.truncated = true; return; }
      if (typeof value === "string" && encodedKeys.has(parent) && encodings < 4 && /^\s*[\[{]/.test(value)) {
        try { inspect(JSON.parse(value), path + "<json>", parent, depth + 1, encodings + 1); } catch {} return;
      }
      if (!value || typeof value !== "object") return;
      visited += 1;
      const taskContext = Object.keys(value).some(key => ids.has(normalize(key)));
      const errorCode = Object.entries(value).find(([key, item]) => codes.has(normalize(key)) && (numeric(item) && Number(item) !== 0 || typeof item === "string" && /^[A-Z][A-Z_]{2,40}$/.test(item) && item !== "SUCCESS" && item !== "OK"));
      if (errorCode) {
        const message = [value.error_message, value.err_msg, value.message, value.msg].find(item => typeof item === "string");
        add(out.businessErrors, { path, code: errorCode[1], message: message ? redacted(message) : "" });
      }
      for (const [key, child] of Object.entries(value)) {
        visited += 1;
        if (visited > 10_000) { out.truncated = true; break; }
        const name = normalize(key);
        const childPath = (path + (Array.isArray(value) ? quota ? "[" + key + "]" : "[]" : "." + safeKey(key))).slice(0, 280);
        if (out.schema.length < 96 && !out.schema.includes(childPath)) out.schema.push(childPath);
        else if (!out.schema.includes(childPath)) out.schemaTruncated = true;
        if (/\.message(?:\.|<)|content_block|tool|video|generat|task|billing|quota|finish|answer|delta|error|credits?|points?/.test(childPath) &&
            !/^\$\.ack_client_meta/.test(childPath) && !out.detailSchema.includes(childPath) && out.detailSchema.length < 96) out.detailSchema.push(childPath);
        if (currentEvent && name === "block_type" && numeric(child) && !currentEvent.blockTypes.includes(Number(child))) currentEvent.blockTypes.push(Number(child));
        if (currentEvent && ["user_type", "content_type", "biz_content_type", "is_finish", "finish_reason", "end_type", "stop_reason", "finish_type", "cmd", "patch_type", "is_general_task", "tool_name", "function_name"].includes(name) &&
            (typeof child === "boolean" || numeric(child) || typeof child === "string" && /^[a-zA-Z_][a-zA-Z0-9_.:-]{0,47}$/.test(child))) add(currentEvent.flowFields, { path: childPath, value: child });
        if (quota && entitlementKeys.has(name) && out.entitlementFields.length < 64 &&
            (typeof child === "boolean" || numeric(child) || typeof child === "string" && /^[a-zA-Z0-9_\u3400-\u9fff .:-]{1,48}$/.test(child))) {
          const entry = { path: childPath, value: child }; if (!out.entitlementFields.some(item => item.path === childPath && item.value === child)) out.entitlementFields.push(entry);
        }
        if (textKeys.has(name)) { if (typeof child === "string" && child.length) out.textFieldsSeen += 1; continue; }
        if (["content", "message", "msg"].includes(name) && typeof child === "string" && /^(?:assistant|bot)$/i.test(String(value.role || value.sender_role || ""))) {
          if (child.length) out.textFieldsSeen += 1; continue;
        }
        if (ids.has(name) && (typeof child === "string" || typeof child === "number") && String(child).length) {
          add(out.taskFields, { path: childPath, key: name, present: true, type: typeof child, length: String(child).length });
        } else if ((statuses.has(name) || name === "status" && taskContext) && (numeric(child) || typeof child === "string" && /^[a-zA-Z_]{2,30}$/.test(child))) {
          add(out.taskFields, { path: childPath, key: name, value: child });
        } else if ((costs.has(name) || name === "cost" && /^(?:billing|quota|credits|points|video_task|video_generation)$/.test(parent)) && numeric(child)) {
          add(out.billingFields, { path: childPath, key: name, value: child });
        } else if (codes.has(name) && (numeric(child) || typeof child === "string" && /^[A-Z][A-Z_]{1,40}$/.test(child))) {
          add(out.statusFields, { path: childPath, key: name, value: child });
        }
        inspect(child, childPath, name, depth + 1, encodings);
      }
    }
    function parse(text, eventName = "") {
      try {
        const value = JSON.parse(text);
        const name = eventName || value?.event_type || value?.event || value?.type || "";
        currentEvent = { index: out.parsedObjects + 1, event: typeof name === "string" && /^[a-zA-Z0-9_.:-]{1,48}$/.test(name) ? name : "",
          keys: object(value) ? Object.keys(value).slice(0, 20).map(safeKey) : [],
          messageKeys: object(value?.message) ? Object.keys(value.message).slice(0, 24).map(safeKey) : [], blockTypes: [], flowFields: [] };
        visited = 0; inspect(value); out.parsedObjects += 1;
        const eventKey = currentEvent.event || "JSON";
        out.eventCounts[eventKey] = (out.eventCounts[eventKey] || 0) + 1;
        if (out.events.length < 32) out.events.push(currentEvent);
        else { out.events.splice(16, 1); out.events.push(currentEvent); out.eventsTruncated = true; }
        currentEvent = null; return true;
      } catch { currentEvent = null; return false; }
    }
    if (!parse(raw)) {
      const groups = raw.replace(/\r\n/g, "\n").split(/\n\s*\n/);
      for (const group of groups) {
        const lines = group.split("\n");
        const data = lines.filter(line => line.startsWith("data:")).map(line => line.slice(5).replace(/^ /, "")).join("\n");
        const eventName = lines.find(line => line.startsWith("event:"))?.slice(6).trim() || "";
        if (data && data !== "[DONE]") parse(data, eventName);
      }
      if (!out.parsedObjects) for (const line of raw.split(/\r?\n/)) if (/^\s*[\[{]/.test(line)) parse(line);
    }
    return out;
  }
  Object.defineProperty(globalThis, "__DOUBAO_DOLA_NETWORK_SUMMARY__", { value: Object.freeze({ LIMIT, endpoint, request, response, compareRequest, redacted }) });
})();
