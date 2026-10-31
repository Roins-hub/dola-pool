(function () {
  "use strict";

  const INSTALL = "__dolaSilentExpertV1656";
  const API = "__DOLA_SILENT_EXPERT__";
  const STATUS_ATTRIBUTE = "data-dola-silent-expert-status";
  const STATUS_EVENT = "dola-silent-expert-status";
  const ARM_TTL_MS = 20_000;
  const PRESERVED_BLOCK_KEY = "dola-toolkit-preserved-expert-media-v1";
  const BATCH_STORAGE_KEY = "dola-toolkit-protocol-batch-v1";
  const PROTOCOL_MODE_ATTRIBUTE = "data-dola-protocol-mode";
  const PARAMETER_ATTRIBUTE = "data-dola-protocol-parameter-selection";
  const SEND_SELECTOR = '#flow-end-msg-send, button[type="submit"], button[data-testid*="send"], button[aria-label*="发送"], button[aria-label*="send" i]';
  const COMPOSER_SELECTOR = '.tiptap.ProseMirror[contenteditable="true"], [contenteditable="true"][data-lexical-editor], [contenteditable="true"][role="textbox"], [contenteditable="true"], textarea';
  const VIDEO_CONTROL_SELECTOR = 'button[data-input-engine-actionbar-control-key="video-model"], button[data-input-engine-actionbar-render-entry-key="creation-video-generation-params-panel"]';
  if (!/(^|\.)dola\.com$/i.test(location.hostname) || globalThis[INSTALL]) return;
  globalThis[INSTALL] = true;

  const state = {
    version: "1.6.60", installed: true, enabled: true, armed: false,
    armCount: 0, appliedCount: 0, uiSwitchCount: 0, uiAlreadySelectedCount: 0,
    last: null, reason: "等待视频提交"
  };
  let armedAt = 0;
  let armedContext = null;
  let earlyDraftAt = 0;
  let earlyDraftContext = null;
  let uiRoutedAt = 0;
  let expertConfirmedAt = 0;
  let routing = false;
  let replaying = false;

  function object(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const prototype = Object.getPrototypeOf(value);
    return prototype === null || prototype?.constructor?.name === "Object";
  }

  function jsonObject(value) {
    if (object(value)) return value;
    if (typeof value !== "string" || !value.trim()) return {};
    try { const parsed = JSON.parse(value); return object(parsed) ? parsed : {}; }
    catch { return {}; }
  }

  function publish() {
    state.armed = armedAt > 0 && Date.now() - armedAt <= ARM_TTL_MS;
    document.documentElement?.setAttribute(STATUS_ATTRIBUTE, JSON.stringify(state));
    document.dispatchEvent(new Event(STATUS_EVENT));
  }

  function protocolMode() {
    return document.documentElement?.getAttribute?.(PROTOCOL_MODE_ATTRIBUTE) === "quick" ? "quick" : "expert";
  }

  function selectedProtocolParameters() {
    try {
      const value = JSON.parse(document.documentElement?.getAttribute?.(PARAMETER_ATTRIBUTE) || "null");
      return object(value) ? value : {};
    } catch { return {}; }
  }

  function text(node) {
    return String(node?.innerText ?? node?.textContent ?? "").replace(/\s+/g, " ").trim();
  }

  function visible(node) {
    if (!node?.isConnected || node.disabled || node.getAttribute?.("aria-disabled") === "true") return false;
    const rect = node.getBoundingClientRect?.();
    if (rect && (rect.width <= 1 || rect.height <= 1)) return false;
    try {
      const style = getComputedStyle(node);
      return style.display !== "none" && style.visibility !== "hidden";
    } catch { return true; }
  }

  function isVideoComposer() {
    return [...document.querySelectorAll(VIDEO_CONTROL_SELECTOR)].some(visible);
  }

  function actionable(node, allowHidden = false) {
    if (!node?.isConnected || node.disabled || node.getAttribute?.("aria-disabled") === "true") return false;
    return allowHidden || visible(node);
  }

  function modeCandidates(allowHidden = false) {
    return [...document.querySelectorAll('button, [role="button"], [role="menuitem"], [role="option"]')]
      .filter((node) => actionable(node, allowHidden))
      .filter((node) => /^(?:快速(?:模式)?|专家模式)(?:\s|$)/.test(text(node)));
  }

  function modeTrigger(send, allowHidden = false) {
    const candidates = modeCandidates(allowHidden).filter((node) => !node.closest?.('[role="menu"], [role="listbox"]'));
    if (!candidates.length) return null;
    const sendRect = send?.getBoundingClientRect?.();
    if (!sendRect) {
      // During the create-image -> chat route transition React can keep the
      // former action bar connected for several seconds. The active chat
      // action bar is the lowest visible mode trigger in the viewport.
      return candidates.sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom)[0];
    }
    return candidates.sort((a, b) => {
      const ar = a.getBoundingClientRect(); const br = b.getBoundingClientRect();
      const distance = (rect) => Math.abs(rect.bottom - sendRect.bottom) + Math.abs(rect.left - sendRect.left);
      return distance(ar) - distance(br);
    })[0];
  }

  function newChatControl() {
    return [...document.querySelectorAll('a[href], button, [role="button"]')]
      .filter((node) => visible(node))
      .find((node) => /^新对话(?:\s|$)/.test(text(node)) || /(?:^|\/)chat(?:\?.*)?$/.test(String(node.getAttribute?.("href") || ""))) || null;
  }

  function selected(node) {
    return node?.getAttribute?.("aria-selected") === "true" || node?.getAttribute?.("aria-checked") === "true" ||
      /^(?:active|checked|selected)$/.test(node?.getAttribute?.("data-state") || "");
  }

  async function waitFor(find, timeout = 3000) {
    const deadline = Date.now() + timeout;
    while (Date.now() < deadline) {
      const value = find();
      if (value) return value;
      await new Promise((resolve) => setTimeout(resolve, 50));
    }
    return null;
  }

  async function trustedClick(node, timeout = 4000) {
    if (!node?.isConnected) throw Error("可信点击目标已失效");
    // Minimal non-browser harnesses do not have the isolated-world bridge.
    // Production documents always expose getAttribute on documentElement.
    if (typeof document.documentElement?.getAttribute !== "function") {
      node.click?.();
      return true;
    }
    const rect = node.getBoundingClientRect?.();
    if (!rect || rect.width <= 1 || rect.height <= 1) throw Error("可信点击目标不可见");
    // The floating extension panel can overlap Dola's arrow button on narrow
    // windows. Coordinate-based trusted clicks then hit our own panel even
    // though the underlying native button is the validated target.
    const panel = document.querySelector?.("#watermark-free-media-panel");
    const previousPointerEvents = panel?.style?.pointerEvents || "";
    if (panel?.style) panel.style.pointerEvents = "none";
    const id = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    try {
      document.documentElement?.setAttribute("data-dola-trusted-click-request", JSON.stringify({
        id, x: rect.left + rect.width / 2, y: rect.top + rect.height / 2
      }));
      document.dispatchEvent(new Event("dola-trusted-click-request"));
      const result = await waitFor(() => {
        try {
          const value = JSON.parse(document.documentElement?.getAttribute("data-dola-trusted-click-result") || "null");
          return value?.id === id ? value : null;
        } catch { return null; }
      }, timeout);
      if (!result) throw Error("可信点击后台未响应");
      if (result.ok !== true) throw Error(`可信点击失败：${result.error || "未知错误"}`);
      return true;
    } finally {
      if (panel?.style) panel.style.pointerEvents = previousPointerEvents;
    }
  }

  function composerNode() {
    return [...document.querySelectorAll(COMPOSER_SELECTOR)].filter((node) => {
      const tag = String(node?.tagName || "").toUpperCase();
      return visible(node) && (tag === "TEXTAREA" || tag === "INPUT" || node?.isContentEditable === true || node?.getAttribute?.("contenteditable") === "true");
    }).sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom)[0] || null;
  }

  function sendForActiveComposer(allowHidden = false) {
    const composer = composerNode();
    if (!composer) return null;
    const composerRect = composer.getBoundingClientRect?.();
    let scope = composer.parentElement;
    for (let depth = 0; scope && depth < 9; depth += 1, scope = scope.parentElement) {
      const candidates = [...(scope.querySelectorAll?.(SEND_SELECTOR) || [])]
        .filter((node) => actionable(node, allowHidden));
      if (candidates.length) {
        return candidates.sort((a, b) => {
          const ar = a.getBoundingClientRect(); const br = b.getBoundingClientRect();
          const distance = (rect) => composerRect
            ? Math.abs(rect.bottom - composerRect.bottom) + Math.abs(rect.right - composerRect.right)
            : 0;
          return distance(ar) - distance(br);
        })[0];
      }
    }
    // React may briefly portal the send control outside the composer subtree.
    // Prefer the control geometrically closest to the lowest active editor;
    // never take querySelectorAll()[0], which can be the stale creation layer.
    return [...document.querySelectorAll(SEND_SELECTOR)]
      .filter((node) => actionable(node, allowHidden))
      .sort((a, b) => {
        const ar = a.getBoundingClientRect(); const br = b.getBoundingClientRect();
        const distance = (rect) => composerRect
          ? Math.abs(rect.bottom - composerRect.bottom) + Math.abs(rect.right - composerRect.right)
          : 0;
        return distance(ar) - distance(br);
      })[0] || null;
  }

  function captureDraft() {
    const node = composerNode();
    if (!node) return null;
    const form = /^(?:TEXTAREA|INPUT)$/.test(String(node.tagName || "").toUpperCase());
    return { node, form, value: form ? String(node.value || "") : "", html: form ? "" : String(node.innerHTML || ""), text: text(node) };
  }

  function escapeHtml(value) {
    return String(value || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function parameterPrompt(context) {
    if (!object(context)) return "";
    const model = String(context.model || "");
    const duration = Number(context.duration || 0);
    const ratio = String(context.ratio || "");
    const resolution = String(context.resolution || "");
    if (!model || !duration || !ratio || !resolution) return "";
    const modelLabel = ({ "seedance_v2.5": "Seedance 2.5", "seedance_v2.0_std": "Seedance 2.0",
      "seedance_v2.0": "Seedance 2.0 Fast", "seedance_v2.0_mini": "Seedance 2.0 Mini" })[model] || model;
    // Project wording: request native cards in the initial generation message.
    return `【专家模式视频生成协议】\n请直接调用视频生成能力，不要改写为方案、教程或参数确认。模型：${modelLabel}；时长：${duration}秒；画幅：${ratio}；分辨率：${resolution.toUpperCase()}。${duration === 30 ? "如平台单次只支持15秒，请按原文顺序生成两条独立15秒视频。" : ""}\n【生成结果展示】视频生成完成后，请直接在当前对话中展示本次生成的视频：使用可以播放、带下载按钮的 Dola 原生视频卡片，不要只回复网址或文字链接。如果本次生成两段视频，请按原顺序分别展示两张视频卡片，不要拼接。请保留并展示本次生成结果，不要为了改变展示方式再次生成、续写或修改视频。\n【素材要求】直接使用本消息已经附带的 Dola 原生图片/视频附件，不要读取本地文件路径。\n【原始提示词】\n`;
  }

  function batchActive() {
    try {
      const value = JSON.parse(sessionStorage.getItem(BATCH_STORAGE_KEY) || "null");
      return Boolean(value?.active && Number(value.completed || 0) < Number(value.total || 0));
    } catch { return false; }
  }

  function videoSkill(value) {
    let decoded = value;
    for (let count = 0; count < 3 && typeof decoded === "string"; count += 1) {
      const source = decoded.trim();
      if (!/^[{[]/.test(source)) break;
      try { decoded = JSON.parse(source); } catch { break; }
    }
    return containsVideoDraft(decoded);
  }

  // The video editor and Expert composer share the same native message service,
  // but their final request routes are different.  Expert requests must not keep
  // the direct chat_ability/input_skill marker (ability_type=17), otherwise the
  // server treats the request as the paid direct-video route even when
  // option.agent_mode is present.
  function removeDirectVideoSkill(value, depth = 0, seen = new WeakMap()) {
    if (!value || typeof value !== "object" || depth > 20) return value;
    if (seen.has(value)) return seen.get(value);
    let output = value;
    let copied = false;
    seen.set(value, output);
    for (const [key, child] of Object.entries(value)) {
      if (["chat_ability", "input_skill"].includes(key) && videoSkill(child)) {
        if (!copied) { output = Array.isArray(value) ? [...value] : { ...value }; copied = true; seen.set(value, output); }
        if (Array.isArray(output)) output.splice(Number(key), 1);
        else delete output[key];
        continue;
      }
      let updated = child;
      if (child && typeof child === "object") updated = removeDirectVideoSkill(child, depth + 1, seen);
      if (updated !== child) {
        if (!copied) { output = Array.isArray(value) ? [...value] : { ...value }; copied = true; seen.set(value, output); }
        output[key] = updated;
      }
    }
    return copied ? output : value;
  }

  function prefixExpertText(request, context) {
    const prefix = parameterPrompt(context);
    if (!prefix || !Array.isArray(request?.messages) || !request.messages.length) return { request, changed: false };
    const messages = [...request.messages];
    const index = messages.length - 1;
    const source = messages[index];
    if (!source || typeof source !== "object") return { request, changed: false };
    let applied = false;
    function patch(value, depth = 0) {
      if (applied || !value || typeof value !== "object" || depth > 14) return value;
      if (object(value.text_block) && typeof value.text_block.text === "string" && value.text_block.text.trim()) {
        if (value.text_block.text.startsWith(prefix)) { applied = true; return value; }
        applied = true;
        return { ...value, text_block: { ...value.text_block, text: prefix + value.text_block.text } };
      }
      let output = value;
      for (const [key, child] of Object.entries(value)) {
        if (applied) break;
        let updated = child;
        if (child && typeof child === "object") updated = patch(child, depth + 1);
        else if (typeof child === "string" && ["content", "body"].includes(key) && /^[\s]*[\[{]/.test(child)) {
          try {
            const parsed = JSON.parse(child);
            const next = patch(parsed, depth + 1);
            if (next !== parsed) updated = JSON.stringify(next);
          } catch { /* Keep opaque message payloads unchanged. */ }
        }
        if (updated !== child) {
          if (output === value) output = Array.isArray(value) ? [...value] : { ...value };
          output[key] = updated;
        }
      }
      return output;
    }
    messages[index] = patch(source);
    return applied ? { request: { ...request, messages }, changed: true } : { request, changed: false };
  }

  // Dola's video editor may stage an uploaded image as a standalone message
  // followed by a second text message.  That shape is valid for the direct
  // video ability, but the Expert Agent endpoint rejects it with
  // 710020202/common invalid param.  Native Expert requests carry the current
  // attachment and text blocks in one atomic user message.  Coalesce only
  // attachment-only drafts into the latest text message; unrelated messages
  // and their ordering remain untouched.
  function mergeCurrentAttachmentMessage(request) {
    if (!object(request) || !Array.isArray(request.messages) || request.messages.length < 2) {
      return { request, changed: false, attachmentCount: 0 };
    }
    const messages = request.messages;
    const blocksOf = (message) => Array.isArray(message?.content_blocks_v2)
      ? { key: "content_blocks_v2", blocks: message.content_blocks_v2 }
      : Array.isArray(message?.content_block)
        ? { key: "content_block", blocks: message.content_block }
        : { key: "", blocks: [] };
    let textIndex = -1;
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const blocks = blocksOf(messages[i]).blocks;
      if (blocks.some((block) => Number(block?.block_type) === 10000)) { textIndex = i; break; }
    }
    if (textIndex < 0) return { request, changed: false, attachmentCount: 0 };
    const movable = [];
    const remove = new Set();
    for (let i = 0; i < messages.length; i += 1) {
      if (i === textIndex) continue;
      const blocks = blocksOf(messages[i]).blocks;
      if (!blocks.length || blocks.some((block) => Number(block?.block_type) !== 10052)) continue;
      movable.push(...blocks);
      remove.add(i);
    }
    if (!movable.length) return { request, changed: false, attachmentCount: 0 };
    const target = messages[textIndex];
    const targetView = blocksOf(target);
    if (!targetView.key) return { request, changed: false, attachmentCount: 0 };
    const mergedTarget = { ...target, [targetView.key]: [...movable, ...targetView.blocks] };
    const mergedMessages = messages.map((message, index) => index === textIndex ? mergedTarget : message)
      .filter((_, index) => !remove.has(index));
    return { request: { ...request, messages: mergedMessages }, changed: true,
      attachmentCount: movable.reduce((sum, block) => sum + Number(block?.content?.attachment_block?.attachments?.length || 0), 0) };
  }

  function dedupeAttachmentBlocks(request) {
    if (!object(request) || !Array.isArray(request.messages)) return request;
    let changed = false;
    const messages = request.messages.map((message) => {
      if (!object(message)) return message;
      let next = message;
      for (const key of ["content_block", "content_blocks_v2"]) {
        const blocks = message[key];
        if (!Array.isArray(blocks)) continue;
        const attachmentBlocks = blocks.filter((block) => Number(block?.block_type) === 10052);
        if (attachmentBlocks.length < 2) continue;
        const attachments = [];
        const seen = new Set();
        for (const block of attachmentBlocks) {
          for (const item of block?.content?.attachment_block?.attachments || []) {
            const identity = String(item?.identifier || item?.image?.uri || item?.media?.uri || JSON.stringify(item));
            if (seen.has(identity)) continue;
            seen.add(identity); attachments.push(item);
          }
        }
        const first = attachmentBlocks[0];
        const merged = { ...first, content: { ...(first.content || {}), attachment_block: {
          ...(first.content?.attachment_block || {}), attachments
        } } };
        let kept = false;
        const nextBlocks = blocks.filter((block) => {
          if (Number(block?.block_type) !== 10052) return true;
          if (kept) return false;
          kept = true; return true;
        }).map((block) => block === first ? merged : block);
        next = { ...next, [key]: nextBlocks };
        changed = true;
      }
      return next;
    });
    if (changed) state.lastMediaDeduplication = { applied: true, blockType: 10052 };
    return changed ? { ...request, messages } : request;
  }

  function expertTaskParameters(request, option) {
    const messages = Array.isArray(request?.messages) ? request.messages : [];
    const localMessageId = String(messages.at(-1)?.local_message_id || "");
    const previous = object(option?.general_task_param) ? option.general_task_param : {};
    const previousAgent = object(previous.agent_task_param) ? previous.agent_task_param : {};
    const agentTask = { ...previousAgent, device_name: String(previousAgent.device_name || ""),
      folder_name: String(previousAgent.folder_name || ""), local_app_id: String(previousAgent.local_app_id || ""),
      local_device_id: String(previousAgent.local_device_id || ""), workspace: String(previousAgent.workspace || ""),
      runtime_type: 1 };
    return { ...previous, action: 0, runtime_type: 1, agent_task_param: agentTask,
      project_id: String(previous.project_id || ""),
      server_do_compare: true,
      thread_local_message_id: localMessageId ? [localMessageId] : Array.isArray(previous.thread_local_message_id) ? previous.thread_local_message_id : [] };
  }

  function expertMessageExt(message) {
    const clean = object(message?.ext) ? removeDirectVideoSkill(message.ext) : {};
    const { answer_with_suggest: _answerWithSuggest, use_deep_think_override: _deepThinkOverride,
      ...expertClean } = clean;
    const previousTask = object(expertClean.general_task_param) ? expertClean.general_task_param : {};
    const previousAgent = object(previousTask.agent_task_param) ? previousTask.agent_task_param : {};
    const localMessageId = String(message?.local_message_id || "");
    const agentTask = { ...previousAgent,
      device_name: String(previousAgent.device_name || ""),
      folder_name: String(previousAgent.folder_name || ""),
      local_app_id: String(previousAgent.local_app_id || ""),
      local_device_id: String(previousAgent.local_device_id || ""),
      workspace: String(previousAgent.workspace || ""),
      runtime_type: 1 };
    const generalTask = { ...previousTask, action: 0, runtime_type: 1,
      agent_task_param: agentTask, server_do_compare: true,
      thread_local_message_id: localMessageId ? [localMessageId] :
        Array.isArray(previousTask.thread_local_message_id) ? previousTask.thread_local_message_id : [] };
    const previousInit = object(expertClean.conversation_init_ext) ? expertClean.conversation_init_ext : {};
    const previousInitAgent = object(previousInit.agent_task_param) ? previousInit.agent_task_param : {};
    const previousInitOption = jsonObject(expertClean.conversation_init_option);
    return { ...expertClean,
      agent_mode: "1",
      use_deep_think: "4",
      model_item_key: "4",
      // The native serializer's parse helper consumes these two ext fields as
      // JSON strings and then places the decoded objects into option.
      conversation_init_ext: JSON.stringify({ ...previousInit, model_item_key: "4",
        agent_task_param: { ...agentTask, ...previousInitAgent, runtime_type: 1 } }),
      conversation_init_option: JSON.stringify({ ...previousInitOption, need_ack_conversation: true,
        project_id: String(previousInitOption.project_id || "") }),
      general_task_param: JSON.stringify(generalTask) };
  }

  // MessageService.sendMessage receives application messages, not the final
  // /chat/completion body. Dola's native iiR serializer builds option/ext and
  // signs the HTTP request afterwards. Patch this early draft so the native
  // serializer emits the expert route while attachments remain untouched.
  function patchEarlyExpertDraft(request, context) {
    if (!object(request) || !Array.isArray(request.messages) || !request.messages.length) {
      return { request, changed: false, routeRemoved: false, promptInjected: false };
    }
    let routeRemoved = false;
    const messages = request.messages.map((message) => {
      if (!object(message)) return message;
      const clean = removeDirectVideoSkill(message);
      routeRemoved ||= clean !== message;
      return { ...clean, ext: expertMessageExt(clean) };
    });
    let next = { ...request, messages };
    const prefixed = prefixExpertText(next, context);
    next = prefixed.request;
    const merged = mergeCurrentAttachmentMessage(next);
    next = dedupeAttachmentBlocks(merged.request);
    const rawRootExt = object(request.ext) ? removeDirectVideoSkill(request.ext) : {};
    const { answer_with_suggest: _answerWithSuggest, use_deep_think_override: _deepThinkOverride,
      ...rootExt } = rawRootExt;
    const messageTask = jsonObject(next.messages.at(-1)?.ext?.general_task_param);
    const generalTask = expertTaskParameters(next, { general_task_param: messageTask });
    const initOption = { ...jsonObject(rootExt.conversation_init_option), need_ack_conversation: true,
      project_id: String(jsonObject(rootExt.conversation_init_option).project_id || "") };
    next = { ...next, ext: { ...rootExt, agent_mode: "1", use_deep_think: "4",
      conversation_init_option: JSON.stringify(initOption), general_task_param: JSON.stringify(generalTask) } };
    return { request: next, changed: true, routeRemoved, promptInjected: prefixed.changed,
      atomicMessageMerge: merged.changed, mergedAttachmentCount: merged.attachmentCount };
  }

  function restoreDraft(draft, context = null) {
    if (!draft) return true;
    const node = composerNode();
    if (!node) return false;
    const prefix = parameterPrompt(context);
    const desired = prefix && !draft.text.startsWith(prefix) ? prefix + draft.text : draft.text;
    const current = text(node);
    if (current === desired.replace(/\s+/g, " ").trim()) return true;
    if (draft.form) {
      const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(node), "value");
      if (descriptor?.set) descriptor.set.call(node, desired);
      else node.value = desired;
    } else {
      node.innerHTML = desired.split(/\r?\n/).map((line) => `<p>${escapeHtml(line) || "<br>"}</p>`).join("");
    }
    node.dispatchEvent?.(new Event("input", { bubbles: true, inputType: "insertText" }));
    node.dispatchEvent?.(new Event("change", { bubbles: true }));
    return text(node) === desired.replace(/\s+/g, " ").trim();
  }

  async function dispatchNativeHttp(send, options = {}) {
    const allowHidden = options?.allowHidden === true;
    routing = true;
    try {
      const draft = captureDraft();
      if (!draft?.text) throw Error("当前提示词为空，未发送");
      const expectedMedia = Number(options?.media?.attachmentCount || 0);
      const mediaBridge = globalThis.__DOLA_TOOLKIT_MEDIA_MODE_SWITCH__;
      const preserved = mediaBridge?.preserve?.();
      // No UI mode switch occurs on this route, so the native composer keeps
      // its attachment blocks. A store snapshot may be unavailable for files
      // uploaded through Dola's own React input; do not reject those verified
      // native blocks merely because the optional preservation bridge is empty.
      const activeSend = send?.isConnected ? send : await waitFor(() => [...document.querySelectorAll(SEND_SELECTOR)]
        .find((node) => actionable(node, allowHidden)), 3000);
      if (!activeSend) throw Error("原生发送按钮不可用");
      uiRoutedAt = Date.now();
      state.reason = "不切换界面；正在把视频草稿转换为专家协议并交回原生 HTTP 签名";
      state.last = { applied: false, method: "native_http_pre_sign", source: String(options?.source || "protocol_button").slice(0, 40),
        promptPreserved: true, mediaCount: expectedMedia,
        mediaSnapshotCount: Number(preserved?.attachmentCount || 0) };
      publish();
      replaying = true;
      activeSend.click();
      return true;
    } catch (error) {
      state.last = { applied: false, method: "native_http_pre_sign", reason: String(error?.message || error) };
      state.reason = `静默 HTTP 发送失败：${state.last.reason}`;
      publish();
      return false;
    } finally {
      replaying = false;
      routing = false;
    }
  }

  async function dispatchQuickNativeHttp(send, options = {}) {
    routing = true;
    try {
      const draft = captureDraft();
      if (!draft?.text) throw Error("当前提示词为空，未发送");
      const activeSend = send?.isConnected ? send : await waitFor(() => [...document.querySelectorAll(SEND_SELECTOR)]
        .find((node) => actionable(node, options?.allowHidden === true)), 3000);
      if (!activeSend) throw Error("原生发送按钮不可用");
      disarm("quick_protocol");
      state.reason = "快速协议已就绪：保留 agent_mode=2，仅向现有视频能力字段注入所选参数";
      state.last = { applied: false, method: "quick_native_http", routePreserved: true,
        source: String(options?.source || "protocol_button").slice(0, 40) };
      publish();
      replaying = true;
      activeSend.click();
      return true;
    } catch (error) {
      state.last = { applied: false, method: "quick_native_http", routePreserved: true,
        reason: String(error?.message || error) };
      state.reason = `快速协议发送失败：${state.last.reason}`;
      publish();
      return false;
    } finally {
      replaying = false;
      routing = false;
    }
  }

  async function selectExpertAndReplay(send, options = {}) {
    const allowHidden = options?.allowHidden === true;
    routing = true;
    try {
      const draft = captureDraft();
      const expectedMedia = Number(options?.media?.attachmentCount || 0);
      const mediaBridge = globalThis.__DOLA_TOOLKIT_MEDIA_MODE_SWITCH__;
      const preserved = mediaBridge?.preserve?.();
      if (expectedMedia > 0 && (!preserved || preserved.ok !== true || Number(preserved.attachmentCount || 0) < expectedMedia)) {
        throw Error("图片素材未能在模式切换前锁定，未发送");
      }
      if (expectedMedia > 0 && preserved?.serialized === false) {
        throw Error("图片素材未能序列化为 Dola 原生附件块，未发送");
      }
      let trigger = modeTrigger(send, allowHidden);
      if (!trigger) {
        const route = newChatControl();
        if (!route) throw Error("当前是视频生成编辑器，但未找到新对话入口，无法进入专家模式");
        route.click();
        const chatReady = await waitFor(() => /(?:^|\/)chat\/?$/.test(location.pathname || "") && composerNode(), 15000);
        if (!chatReady) throw Error("已打开新对话，但新编辑器未就绪");
        // The AI-creation composer remains connected for a short React commit
        // after the route changes.  Reading the mode trigger immediately can
        // therefore return the stale Fast button from the previous layer.
        await new Promise((resolve) => setTimeout(resolve, 650));
        trigger = await waitFor(() => modeTrigger(null, allowHidden), 6000);
        if (!trigger) throw Error("已打开新对话，但专家模式入口未就绪");
      }
      if (!/^专家模式(?:\s|$)/.test(text(trigger))) {
        let option = null;
        for (let attempt = 0; attempt < 3 && !option; attempt += 1) {
          trigger = modeTrigger(null, allowHidden);
          if (!trigger) break;
          await trustedClick(trigger);
          option = await waitFor(() => modeCandidates(allowHidden).find((node) => node !== trigger &&
            /^专家模式(?:\s|$)/.test(text(node))), 1800);
          if (!option) await new Promise((resolve) => setTimeout(resolve, 400));
        }
        if (!option) throw Error("专家模式选项未出现");
        await trustedClick(option);
        const confirmed = await waitFor(() => {
          const current = modeTrigger(null, allowHidden);
          return current && !/^快速(?:模式)?(?:\s|$)/.test(text(current)) ? current : selected(option);
        }, 5000);
        if (!confirmed) throw Error("专家模式选择未确认");
        expertConfirmedAt = Date.now();
        state.uiSwitchCount += 1;
      } else {
        expertConfirmedAt = Date.now();
        state.uiAlreadySelectedCount += 1;
      }
      const draftReady = await waitFor(() => restoreDraft(draft, options?.context || armedContext), 4000);
      if (!draftReady) throw Error("切换专家模式后提示词未恢复，未发送");
      if (expectedMedia > 0) {
        const restored = await mediaBridge?.restore?.({ timeoutMs: 20000 });
        if (!restored || restored.ok !== true || Number(restored.attachmentCount || 0) < expectedMedia) {
          throw Error(`切换专家模式后图片素材未同步：${restored?.reason || "恢复失败"}`);
        }
      }
      const activeSend = await waitFor(() => sendForActiveComposer(allowHidden) ||
        (!composerNode() && actionable(send, allowHidden) ? send : null), 6000);
      if (!activeSend) throw Error("切换专家模式后发送按钮不可用");
      uiRoutedAt = Date.now();
      state.reason = "已进入专家模式并同步提示词、素材和参数，正在原生发送";
      publish();
      replaying = true;
      await trustedClick(activeSend);
      return true;
    } catch (error) {
      state.last = { applied: false, method: "native_ui", reason: String(error?.message || error) };
      state.reason = `静默专家切换失败：${state.last.reason}`;
      publish();
      return false;
    } finally {
      replaying = false;
      routing = false;
    }
  }

  function interceptSend(event) {
    if (replaying || routing || !event.isTrusted || event.button != null && event.button !== 0) return;
    const send = event.target?.closest?.(SEND_SELECTOR);
    if (!send || !visible(send) || !isVideoComposer()) return;
    if (protocolMode() === "quick") return;
    const trigger = modeTrigger(send);
    if (trigger && /^专家模式(?:\s|$)/.test(text(trigger))) {
      uiRoutedAt = Date.now();
      state.uiAlreadySelectedCount += 1;
      state.reason = "原生界面已处于专家模式";
      publish();
      return;
    }
    event.preventDefault();
    event.stopImmediatePropagation();
    arm({ source: "native_video_send" });
    void dispatchNativeHttp(send, { source: "native_video_send" });
  }

  function expire() {
    if (armedAt && Date.now() - armedAt > ARM_TTL_MS) {
      armedAt = 0;
      armedContext = null;
      state.reason = "上一次视频提交窗口已过期";
    }
    if (earlyDraftAt && Date.now() - earlyDraftAt > ARM_TTL_MS) {
      earlyDraftAt = 0;
      earlyDraftContext = null;
    }
  }

  function arm(context = {}) {
    const previous = armedAt > 0 && Date.now() - armedAt <= ARM_TTL_MS && object(armedContext) ? armedContext : {};
    armedAt = Date.now();
    armedContext = object(context) ? {
      model: typeof context.model === "string" && context.model ? context.model.slice(0, 80) : String(previous.model || ""),
      duration: Number.isFinite(Number(context.duration)) && Number(context.duration) > 0 ? Number(context.duration) : previous.duration ?? null,
      ratio: typeof context.ratio === "string" && context.ratio ? context.ratio.slice(0, 12) : String(previous.ratio || ""),
      resolution: typeof context.resolution === "string" && context.resolution ? context.resolution.slice(0, 20) : String(previous.resolution || ""),
      expectedMediaCounts: object(context.expectedMediaCounts) ? {
        image: Number(context.expectedMediaCounts.image || 0),
        video: Number(context.expectedMediaCounts.video || 0),
        audio: Number(context.expectedMediaCounts.audio || 0)
      } : object(previous.expectedMediaCounts) ? previous.expectedMediaCounts : null,
      expectedAttachmentCount: Number.isFinite(Number(context.expectedAttachmentCount))
        ? Number(context.expectedAttachmentCount) : Number(previous.expectedAttachmentCount || 0),
      source: typeof context.source === "string" && context.source ? context.source.slice(0, 40) : String(previous.source || "")
    } : {};
    state.armCount += 1;
    state.reason = "视频提交已切入静默专家协议，等待原生签名发送";
    publish();
  }

  function disarm(reason = "manual") {
    armedAt = 0;
    armedContext = null;
    earlyDraftAt = 0;
    earlyDraftContext = null;
    state.reason = reason === "quick_protocol"
      ? "已清除专家注入状态，下一次请求保持快速模式"
      : "已清除专家协议注入状态";
    publish();
  }

  function containsVideoDraft(value, depth = 0, seen = new Set()) {
    if (!value || typeof value !== "object" || depth > 16 || seen.has(value)) return false;
    seen.add(value);
    if (Number(value.ability_type) === 17 || Number(value.abilityType) === 17 || Number(value.skill_type) === 17) return true;
    for (const [key, child] of Object.entries(value)) {
      if (typeof child === "string" && ["chat_ability", "input_skill"].includes(key)) {
        try { if (containsVideoDraft(JSON.parse(child), depth + 1, seen)) return true; }
        catch { /* An unrelated or malformed string is not a video marker. */ }
      } else if (child && typeof child === "object" && containsVideoDraft(child, depth + 1, seen)) return true;
    }
    return false;
  }

  function patchVideoParameters(request, context) {
    if (!object(context) || !request || typeof request !== "object") return { request, changed: false, fields: [] };
    const desired = {};
    if (["seedance_v2.5", "seedance_v2.0_std", "seedance_v2.0", "seedance_v2.0_mini"].includes(context.model)) desired.model = context.model;
    if ([5, 10, 15, 30].includes(Number(context.duration))) desired.duration = Number(context.duration);
    if (["1:1", "3:4", "4:3", "9:16", "16:9", "21:9"].includes(context.ratio)) desired.ratio = context.ratio;
    if (["480p", "720p", "1080p"].includes(String(context.resolution || "").toLowerCase())) desired.resolution = String(context.resolution).toLowerCase();
    if (!Object.keys(desired).length) return { request, changed: false, fields: [] };
    const encodedKeys = new Set(["chat_ability", "input_skill", "video_generation"]);
    const parameterKeys = new Set(["ability_param", "abilityParams", "variables", "video_generation", "params"]);
    const changedFields = new Set();
    const seen = new WeakMap();

    function marker(value) {
      return object(value) && (Number(value.ability_type) === 17 || Number(value.abilityType) === 17 || Number(value.skill_type) === 17);
    }

    function applyParameters(value, parentKey, videoContext) {
      if (!object(value) || !videoContext) return value;
      let next = value;
      const assign = (key, target) => {
        if (target == null || target === "" || next[key] === target) return;
        if (next === value) next = { ...value };
        next[key] = target;
        changedFields.add(key);
      };
      if (parameterKeys.has(parentKey)) {
        for (const [key, target] of Object.entries(desired)) assign(key, target);
      } else if (parentKey === "actionBarControlParams") {
        for (const [key, target] of Object.entries(desired)) assign(`video-${key}`, target);
      } else {
        const existing = ["model", "duration", "ratio", "resolution"].filter((key) => Object.hasOwn(value, key));
        if (existing.length >= 2) for (const key of existing) if (Object.hasOwn(desired, key)) assign(key, desired[key]);
      }
      if (parentKey === "actionBarControlValues") {
        for (const [recordKey, record] of Object.entries(next)) {
          if (!object(record)) continue;
          const key = String(record.submitKey || recordKey).replace(/^video-/, "");
          if (!Object.hasOwn(desired, key) || record.value === desired[key]) continue;
          if (next === value) next = { ...value };
          next[recordKey] = { ...record, value: desired[key], messageText: "" };
          changedFields.add(key);
        }
      }
      return next;
    }

    function walk(value, inheritedVideo = false, parentKey = "", depth = 0) {
      if (!value || typeof value !== "object" || depth > 18) return value;
      if (seen.has(value)) return seen.get(value);
      const videoContext = inheritedVideo || marker(value) || /video[_-]generation|video[_-]creation/i.test(parentKey);
      let output = value;
      seen.set(value, output);
      let copied = false;
      for (const [key, child] of Object.entries(value)) {
        let updated = child;
        if (typeof child === "string" && encodedKeys.has(key)) {
          try {
            const parsed = JSON.parse(child);
            const next = walk(parsed, videoContext || containsVideoDraft(parsed), key, depth + 1);
            if (next !== parsed) updated = JSON.stringify(next);
          } catch { /* Preserve opaque native strings. */ }
        } else if (child && typeof child === "object") {
          updated = walk(child, videoContext, key, depth + 1);
        }
        if (updated !== child) {
          if (!copied) { output = Array.isArray(value) ? [...value] : { ...value }; copied = true; seen.set(value, output); }
          output[key] = updated;
        }
      }
      const parameterized = applyParameters(output, parentKey, videoContext);
      if (parameterized !== output) { output = parameterized; seen.set(value, output); }
      if (!copied && output !== value) copied = true;
      return copied ? output : value;
    }

    const output = walk(request, containsVideoDraft(request));
    return { request: output, changed: output !== request, fields: [...changedFields] };
  }

  function injectPersistedMedia(request) {
    if (!request || !Array.isArray(request.messages)) return request;
    let saved = null;
    try { saved = JSON.parse(sessionStorage.getItem(PRESERVED_BLOCK_KEY) || 'null'); }
    catch (_) { return request; }
    const ttl = batchActive() ? 30 * 60_000 : 30_000;
    if (!saved?.block || !saved?.savedAt || Date.now() - Number(saved.savedAt) > ttl) return request;
    const messages = request.messages.map(message => ({ ...message }));
    let target = -1;
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const blocks = messages[index]?.content_block || messages[index]?.content_blocks_v2 || [];
      if (blocks.some(block => Number(block?.block_type) === 10000)) { target = index; break; }
    }
    if (target < 0) return request;
    const key = Array.isArray(messages[target].content_block) ? 'content_block' : 'content_blocks_v2';
    const blocks = Array.isArray(messages[target][key]) ? [...messages[target][key]] : [];
    if (!blocks.some(block => Number(block?.block_type) === 10052)) blocks.unshift(saved.block);
    messages[target][key] = blocks;
    state.lastMediaInjection = { applied: true, source: 'session_native_block',
      counts: saved.counts || null, final: Boolean(request.option) };
    if (request.option && typeof request.option === 'object' && !batchActive()) {
      setTimeout(() => { try { sessionStorage.removeItem(PRESERVED_BLOCK_KEY); } catch (_) {} }, 0);
    }
    return { ...request, messages };
  }

  function prepare(request) {
    expire();
    // A genuine Expert composer has no video file input. Preserve the already
    // uploaded native attachment and inject its 10052 block into the final
    // Expert request before any route-specific checks consume the arm state.
    try { request = globalThis.__DOLA_TOOLKIT_MEDIA_MODE_SWITCH__?.inject?.(request) ?? request; }
    catch (_) { /* A missing block is caught by the protocol evidence gate. */ }
    request = injectPersistedMedia(request);
    request = dedupeAttachmentBlocks(request);
    const explicitVideo = containsVideoDraft(request);
    if (protocolMode() === "quick") {
      const context = selectedProtocolParameters();
      const parameters = explicitVideo ? patchVideoParameters(request, context) : { request, changed: false, fields: [] };
      state.appliedCount += parameters.changed ? 1 : 0;
      state.last = { applied: parameters.changed, method: "quick_route_parameter_patch", protocolMode: "quick",
        routePreserved: true, explicitVideo, parameterInjection: parameters.changed,
        parameterFields: parameters.fields,
        reason: explicitVideo
          ? parameters.changed ? "已在原生视频能力字段中写入快速协议参数" : "已识别原生视频能力，但参数字段没有变化"
          : "快速 Agent 请求未暴露视频能力字段；保留原请求，由服务端选择视频工具" };
      state.reason = state.last.reason;
      publish();
      return parameters.request;
    }
    const armed = armedAt > 0;
    const finalizingEarlyDraft = earlyDraftAt > 0 && object(request?.option);
    if (!armed && !explicitVideo && !finalizingEarlyDraft) return request;
    const effectiveContext = armed ? armedContext : finalizingEarlyDraft ? earlyDraftContext : null;
    const parameters = patchVideoParameters(request, effectiveContext);
    request = parameters.request;
    if (!object(request) || !object(request.option)) {
      if (Date.now() - expertConfirmedAt <= ARM_TTL_MS) {
        state.appliedCount += 1;
        state.last = { applied: true, method: "native_ui", armed, explicitVideo,
          parameterInjection: parameters.changed, parameterFields: parameters.fields,
          reason: "专家模式已由页面真实选中；参数同时写入专家编辑器" };
        state.reason = "专家模式已由原生界面选中并交回原生签名发送";
        armedAt = 0;
        armedContext = null;
        publish();
        return request;
      }
      const draft = patchEarlyExpertDraft(request, effectiveContext);
      if (draft.changed) {
        earlyDraftAt = Date.now();
        earlyDraftContext = object(effectiveContext) ? { ...effectiveContext } : {};
        state.appliedCount += 1;
        state.last = { applied: true, method: "native_message_pre_serialize", armed, explicitVideo,
          context: armedContext, parameterInjection: parameters.changed, parameterFields: parameters.fields,
          directVideoRouteRemoved: draft.routeRemoved, promptInjected: draft.promptInjected,
          atomicMessageMerge: draft.atomicMessageMerge, mergedAttachmentCount: draft.mergedAttachmentCount,
          fields: ["messages[].ext.agent_mode", "messages[].ext.use_deep_think",
            "messages[].ext.model_item_key", "messages[].ext.conversation_init_ext",
            "messages[].ext.general_task_param"] };
        state.reason = "早期视频草稿已转换为专家消息；等待 Dola 原生序列化、签名和 HTTP 发送";
        armedAt = 0;
        armedContext = null;
        publish();
        return draft.request;
      }
      state.last = { applied: false, armed, explicitVideo, reason: "提交草稿没有可识别的 messages，未消费发送状态" };
      state.reason = state.last.reason;
      publish();
      return request;
    }

    const stripped = removeDirectVideoSkill(request);
    const directVideoRouteRemoved = stripped !== request;
    const prefixed = prefixExpertText(stripped, effectiveContext);
    const merged = mergeCurrentAttachmentMessage(prefixed.request);
    request = merged.request;
    const option = request.option;
    const aggregate = object(option.aggregate_params) ? option.aggregate_params : {};
    const model = object(option.model_config) ? option.model_config : {};
    const generalTask = expertTaskParameters(request, option);
    const initExt = object(option.conversation_init_ext) ? option.conversation_init_ext : {};
    const initAgent = object(initExt.agent_task_param) ? initExt.agent_task_param : {};
    const conversationInitAgent = { ...generalTask.agent_task_param, ...initAgent, runtime_type: 1 };
    // The final wire schema differs from the early MessageService draft:
    // option.conversation_init_ext stays an object, but its agent_task_param
    // value is a JSON string. Root ext carries both task fields as JSON
    // strings. Sending objects here is accepted by fetch but rejected by Dola
    // with STREAM_ERROR 710020202/common invalid param.
    const conversationInitExt = { ...initExt, model_item_key: "4",
      agent_task_param: JSON.stringify(conversationInitAgent) };
    const conversationInitOption = { ...(object(option.conversation_init_option) ? option.conversation_init_option : {}),
      need_ack_conversation: true,
      project_id: String(option.conversation_init_option?.project_id || "") };
    const nextOption = {
      ...option,
      agent_mode: 1,
      need_deep_think: 4,
      aggregate_params: { ...aggregate, agent_mode: "1", model_item_key: "4" },
      model_config: { ...model, model_item_key: "4" },
      conversation_init_option: conversationInitOption,
      conversation_init_ext: conversationInitExt,
      general_task_param: generalTask
    };
    const rawExt = object(request.ext) ? removeDirectVideoSkill(request.ext) : {};
    const { answer_with_suggest: _answerWithSuggest, use_deep_think_override: _deepThinkOverride,
      ...ext } = rawExt;
    const next = dedupeAttachmentBlocks({ ...request, option: nextOption, ext: { ...ext, agent_mode: "1", use_deep_think: "4",
      conversation_init_option: JSON.stringify(conversationInitOption),
      general_task_param: JSON.stringify(generalTask) } });
    state.appliedCount += 1;
    state.last = { applied: true, armed, explicitVideo, finalizingEarlyDraft, context: effectiveContext,
      parameterInjection: parameters.changed, parameterFields: parameters.fields,
      directVideoRouteRemoved, promptInjected: prefixed.changed,
      atomicMessageMerge: merged.changed, mergedAttachmentCount: merged.attachmentCount,
      fields: ["option.agent_mode", "option.need_deep_think", "option.aggregate_params.agent_mode",
        "option.aggregate_params.model_item_key", "option.model_config.model_item_key",
        "option.conversation_init_ext", "option.general_task_param", "ext.general_task_param"] };
    state.reason = "本次请求已转换为真实专家 Agent 协议并交回 Dola 原生签名发送";
    armedAt = 0;
    armedContext = null;
    earlyDraftAt = 0;
    earlyDraftContext = null;
    publish();
    return next;
  }

  function completionUrl(value) {
    try {
      const raw = typeof value === "string" ? value : value?.url || String(value || "");
      const url = new URL(raw, location.href);
      return /(^|\.)dola\.com$/i.test(url.hostname) && /\/chat\/completion\/?$/i.test(url.pathname);
    } catch { return false; }
  }

  let httpEvidenceSequence = 0;
  function markHttpEvidence(phase, status = null) {
    httpEvidenceSequence += phase === "request_observed" ? 1 : 0;
    const value = { sequence: httpEvidenceSequence, phase, httpStatus: status, observedAt: Date.now() };
    document.documentElement?.setAttribute("data-dola-silent-expert-http-evidence", JSON.stringify(value));
    document.dispatchEvent(new Event("dola-silent-expert-http-evidence"));
  }

  function finalizeWireBody(raw) {
    if (!earlyDraftAt || typeof raw !== "string" || !raw.trim().startsWith("{")) return raw;
    try {
      const parsed = JSON.parse(raw);
      if (!object(parsed?.option)) return raw;
      const finalized = prepare(parsed);
      return finalized === parsed ? raw : JSON.stringify(finalized);
    } catch { return raw; }
  }

  function installWireFinalizer() {
    if (globalThis.__DOLA_SILENT_EXPERT_WIRE_V1__) return;
    globalThis.__DOLA_SILENT_EXPERT_WIRE_V1__ = true;
    const originalFetch = globalThis.fetch;
    if (typeof originalFetch === "function") {
      globalThis.fetch = function expertFinalFetch(input, init) {
        if (!completionUrl(input)) return originalFetch.call(this, input, init);
        markHttpEvidence("request_observed");
        if (init && typeof init.body === "string") {
          const body = finalizeWireBody(init.body);
          if (body !== init.body) init = { ...init, body };
          return originalFetch.call(this, input, init).then((response) => {
            markHttpEvidence("response_observed", response?.status ?? null); return response;
          });
        }
        if (typeof Request !== "undefined" && input instanceof Request) {
          return input.clone().text().then((raw) => {
            const body = finalizeWireBody(raw);
            return originalFetch.call(this, body === raw ? input : new Request(input, { body }), init).then((response) => {
              markHttpEvidence("response_observed", response?.status ?? null); return response;
            });
          });
        }
        return originalFetch.call(this, input, init).then((response) => {
          markHttpEvidence("response_observed", response?.status ?? null); return response;
        });
      };
    }
    if (typeof XMLHttpRequest !== "undefined") {
      const originalOpen = XMLHttpRequest.prototype.open;
      const originalSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function expertFinalOpen(method, url) {
        this.__DOLA_SILENT_EXPERT_COMPLETION__ = completionUrl(url) && String(method || "GET").toUpperCase() === "POST";
        return originalOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function expertFinalSend(body) {
        const next = this.__DOLA_SILENT_EXPERT_COMPLETION__ && typeof body === "string" ? finalizeWireBody(body) : body;
        return originalSend.call(this, next);
      };
    }
  }

  function installSerializeFinalizer() {
    if (globalThis.__DOLA_SILENT_EXPERT_SERIALIZE_V1__) return;
    globalThis.__DOLA_SILENT_EXPERT_SERIALIZE_V1__ = true;
    const originalStringify = JSON.stringify;
    JSON.stringify = function expertFinalStringify(value, replacer, space) {
      let next = value;
      if (earlyDraftAt && object(value) && object(value.client_meta) &&
          Array.isArray(value.messages) && object(value.option)) {
        try { next = prepare(value); } catch { next = value; }
      }
      return originalStringify.call(this, next, replacer, space);
    };
  }

  Object.defineProperty(globalThis, API, {
    configurable: true,
    value: Object.freeze({ version: "1.6.60", arm, disarm, prepare, sendNativeHttp: dispatchNativeHttp,
      sendQuickNativeHttp: dispatchQuickNativeHttp,
      selectBeforeSend: selectExpertAndReplay,
      get status() { return JSON.parse(JSON.stringify(state)); } })
  });
  installSerializeFinalizer();
  document.addEventListener("click", interceptSend, true);
  publish();
})();
