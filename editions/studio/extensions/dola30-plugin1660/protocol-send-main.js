(function () {
  "use strict";

  const INSTALL = "__dolaProtocolSendV1656";
  const REQUEST_EVENT = "dola-protocol-send-request";
  const STATUS_EVENT = "dola-protocol-send-status";
  const STATUS_ATTRIBUTE = "data-dola-protocol-send-status";
  const REQUEST_ATTRIBUTE = "data-dola-protocol-send-request";
  const PARAMETER_ATTRIBUTE = "data-dola-protocol-parameter-selection";
  const BATCH_COUNT_ATTRIBUTE = "data-dola-protocol-batch-count";
  const MODE_ATTRIBUTE = "data-dola-protocol-mode";
  const BATCH_STORAGE_KEY = "dola-toolkit-protocol-batch-v1";
  const MODEL_PREFERENCE_ATTRIBUTE = "data-watermark-free-model-preference";
  const MODEL_PREFERENCE_EVENT = "watermark-free-model-preference-change";
  const RESOLUTION_PREFERENCE_ATTRIBUTE = "data-watermark-free-resolution-preference";
  const RESOLUTION_PREFERENCE_EVENT = "watermark-free-resolution-preference-change";
  const DURATION_STATE_ATTRIBUTE = "data-watermark-free-duration-30";
  const DURATION_STATE_EVENT = "watermark-free-duration-30-change";
  const SEND_SELECTOR = '#flow-end-msg-send, button[type="submit"], button[data-testid*="send"], button[aria-label*="发送"], button[aria-label*="send" i]';
  const COMPOSER_SELECTOR = '.tiptap.ProseMirror[contenteditable="true"], [contenteditable="true"][data-lexical-editor], [contenteditable="true"][role="textbox"], textarea';
  const MODEL_SELECTOR = 'button[data-input-engine-actionbar-control-key="video-model"]';
  const PARAMS_SELECTOR = 'button[data-input-engine-actionbar-render-entry-key="creation-video-generation-params-panel"]';
  if (!/(^|\.)dola\.com$/i.test(location.hostname) || globalThis[INSTALL]) return;
  globalThis[INSTALL] = true;

  let busy = false;
  const MODELS = new Set(["seedance_v2.5", "seedance_v2.0_std", "seedance_v2.0", "seedance_v2.0_mini"]);
  const DURATIONS = new Set([5, 10, 15, 30]);
  const RATIOS = new Set(["1:1", "3:4", "4:3", "9:16", "16:9", "21:9"]);
  const RESOLUTIONS = new Set(["480p", "720p", "1080p"]);
  const EVIDENCE_ATTRIBUTE = "data-watermark-free-generation-evidence";
  const SILENT_HTTP_EVIDENCE_ATTRIBUTE = "data-dola-silent-expert-http-evidence";
  const state = { version: "1.6.60", phase: "idle", reason: "等待协议发送", requestId: "", protocolMode: "expert", snapshot: null, batch: null };

  function text(node) {
    if (!node) return "";
    if (/^(?:TEXTAREA|INPUT)$/.test(node.tagName || "")) return String(node.value || "").trim();
    return String(node.innerText ?? node.textContent ?? "").replace(/\s+/g, " ").trim();
  }

  function visible(node) {
    if (!node?.isConnected || node.disabled || node.getAttribute?.("aria-disabled") === "true") return false;
    const rect = node.getBoundingClientRect?.();
    if (rect && (rect.width <= 1 || rect.height <= 1)) return false;
    try { const style = getComputedStyle(node); return style.display !== "none" && style.visibility !== "hidden"; }
    catch { return true; }
  }

  function firstVisible(selector) {
    return [...document.querySelectorAll(selector)].find(visible) || null;
  }

  function publish(phase, reason, snapshot = state.snapshot) {
    state.phase = phase;
    state.reason = String(reason || "").slice(0, 300);
    state.snapshot = snapshot;
    document.documentElement?.setAttribute(STATUS_ATTRIBUTE, JSON.stringify(state));
    document.dispatchEvent(new Event(STATUS_EVENT));
  }

  function readBatch() {
    try {
      const value = JSON.parse(sessionStorage.getItem(BATCH_STORAGE_KEY) || "null");
      if (!value?.active || !Number.isInteger(Number(value.total)) || Date.now() - Number(value.startedAt || 0) > 30 * 60_000) return null;
      return value;
    } catch { return null; }
  }

  function writeBatch(value) {
    try { sessionStorage.setItem(BATCH_STORAGE_KEY, JSON.stringify(value)); } catch { /* Single-send mode still works. */ }
    state.batch = value ? { total: Number(value.total || 1), completed: Number(value.completed || 0),
      createdIds: Array.isArray(value.createdIds) ? [...value.createdIds] : [] } : null;
  }

  function clearBatch() {
    try { sessionStorage.removeItem(BATCH_STORAGE_KEY); } catch { /* Ignore unavailable storage. */ }
  }

  function requestedBatchCount() {
    const raw = Number(document.documentElement?.getAttribute(BATCH_COUNT_ATTRIBUTE) || 1);
    return Math.max(1, Math.min(20, Number.isFinite(raw) ? Math.trunc(raw) : 1));
  }

  function requestedProtocolMode(batch = null) {
    if (batch?.protocolMode === "quick" || batch?.protocolMode === "expert") return batch.protocolMode;
    return document.documentElement?.getAttribute(MODE_ATTRIBUTE) === "quick" ? "quick" : "expert";
  }

  function setComposerText(value) {
    const node = firstVisible(COMPOSER_SELECTOR);
    if (!node) return false;
    const desired = String(value || "");
    if (/^(?:TEXTAREA|INPUT)$/.test(String(node.tagName || "").toUpperCase())) {
      const descriptor = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(node), "value");
      if (descriptor?.set) descriptor.set.call(node, desired); else node.value = desired;
    } else {
      // ProseMirror owns its DOM.  Direct innerHTML can look correct for one
      // frame and then be reverted by React because no editor transaction was
      // produced.  Prefer the browser's editing transaction and retain the DOM
      // fallback for older/mocked editors.
      let inserted = false;
      try {
        node.focus?.();
        const selection = window.getSelection?.();
        const range = document.createRange?.();
        if (selection && range && typeof document.execCommand === "function") {
          range.selectNodeContents(node);
          selection.removeAllRanges();
          selection.addRange(range);
          inserted = document.execCommand("insertText", false, desired) === true;
        }
      } catch { inserted = false; }
      if (!inserted) {
        const escape = (part) => String(part).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
        node.innerHTML = desired.split(/\r?\n/).map((line) => `<p>${escape(line) || "<br>"}</p>`).join("");
      }
    }
    try {
      const BeforeInputEvent = globalThis.InputEvent || Event;
      node.dispatchEvent?.(new BeforeInputEvent("beforeinput", { bubbles: true, inputType: "insertText", data: desired }));
    } catch { /* The normal input event below is sufficient in older browsers. */ }
    node.dispatchEvent?.(new Event("input", { bubbles: true, inputType: "insertText" }));
    node.dispatchEvent?.(new Event("change", { bubbles: true }));
    return String(node.innerText || node.textContent || node.value || "").trim().length > 0;
  }

  async function restoreComposerTextStable(value, attempts = 6) {
    const desired = String(value || "").trim();
    if (!desired) return false;
    for (let attempt = 0; attempt < attempts; attempt += 1) {
      const editor = await waitFor(() => firstVisible(COMPOSER_SELECTOR), 10000);
      if (!editor) return false;
      const actual = text(editor);
      if (actual === desired || (actual.length >= desired.length * 0.9 && actual.includes(desired.slice(0, Math.min(80, desired.length))))) {
        await sleep(350);
        const current = firstVisible(COMPOSER_SELECTOR);
        const stable = text(current);
        if (stable === actual || stable === desired) return true;
      }
      setComposerText(desired);
      // The editor is commonly replaced once while the new-chat page and its
      // attachment store hydrate.  Re-resolve it instead of trusting this node.
      await sleep(500 + attempt * 200);
    }
    return text(firstVisible(COMPOSER_SELECTOR)).length > 0;
  }

  async function waitFor(check, timeoutMs = 10000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const value = check();
      if (value) return value;
      await sleep(150);
    }
    return null;
  }

  function newChatControl() {
    return [...document.querySelectorAll('a,button,[role="button"]')]
      .filter(visible).find((node) => /^(?:\+\s*)?新对话(?:\s|$)/.test(text(node))) || null;
  }

  async function prepareNextBatchComposer(batch) {
    if (conversationIdFromHref(location.href)) {
      const control = newChatControl();
      if (control) control.click();
      const ready = await waitFor(() => !conversationIdFromHref(location.href) && firstVisible(COMPOSER_SELECTOR), 10000);
      if (!ready) {
        try { location.assign(new URL('/chat', location.href).href); return false; }
        catch { throw Error("批量发送无法打开新对话页"); }
      }
    }
    const composer = await waitFor(() => firstVisible(COMPOSER_SELECTOR), 10000);
    if (!composer) throw Error("批量发送的新对话编辑器未就绪");
    document.documentElement?.setAttribute(PARAMETER_ATTRIBUTE, JSON.stringify(batch.selection || {}));
    document.documentElement?.setAttribute(BATCH_COUNT_ATTRIBUTE, String(batch.total || 1));
    if (!await restoreComposerTextStable(batch.prompt)) throw Error("批量发送未能稳定恢复提示词");
    return true;
  }

  function readJsonAttribute(name) {
    try { return JSON.parse(document.documentElement?.getAttribute(name) || "null"); }
    catch { return null; }
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, Math.max(0, ms)));
  }

  function generationEvidence() {
    return readJsonAttribute(EVIDENCE_ATTRIBUTE) || { sequence: 0, phase: "idle" };
  }

  function silentHttpEvidence() {
    return readJsonAttribute(SILENT_HTTP_EVIDENCE_ATTRIBUTE) || { sequence: 0, phase: "idle" };
  }

  async function waitForHttpSubmission(after, timeoutMs = 8000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const evidence = generationEvidence();
      if (Number(evidence.sequence || 0) > Number(after?.generation || 0) && evidence.phase !== "idle") return evidence;
      const silent = silentHttpEvidence();
      if (Number(silent.sequence || 0) > Number(after?.silent || 0) && silent.phase !== "idle") return silent;
      await sleep(100);
    }
    return null;
  }

  function conversationIdFromHref(value) {
    try {
      const pathname = new URL(String(value || ""), location.href).pathname;
      const id = pathname.match(/^\/chat\/([^/?#]+)/i)?.[1] || "";
      // Real Dola conversation routes are numeric. Navigation links such as
      // /chat/drive and /chat/skills must never satisfy the success gate.
      return /^\d{8,}$/.test(id) ? id : "";
    } catch { return ""; }
  }

  function conversationSnapshot() {
    const ids = new Set();
    const currentId = conversationIdFromHref(location.href);
    if (currentId) ids.add(currentId);
    for (const node of document.querySelectorAll('a[href*="/chat/"]')) {
      const id = conversationIdFromHref(node.getAttribute?.("href") || node.href || "");
      if (id) ids.add(id);
    }
    return { ids: [...ids], currentId, href: String(location.href || "") };
  }

  async function waitForConversationCreated(before, timeoutMs = 30000) {
    const previous = new Set(before?.ids || []);
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      const current = conversationSnapshot();
      const createdId = current.ids.find((id) => !previous.has(id));
      if (createdId) return { ...current, createdId };
      const generation = generationEvidence();
      const silent = silentHttpEvidence();
      const status = Number(generation?.httpStatus ?? silent?.httpStatus ?? 0);
      if (status >= 400) throw Error(`Dola 提交返回 HTTP ${status}，左侧未创建会话`);
      await sleep(150);
    }
    return null;
  }

  function modelFromText(value) {
    const raw = String(value || "").toLowerCase();
    if (/2\.5/.test(raw)) return "seedance_v2.5";
    if (/mini/.test(raw)) return "seedance_v2.0_mini";
    if (/fast|快速/.test(raw)) return "seedance_v2.0";
    if (/2\.0/.test(raw)) return "seedance_v2.0_std";
    return "";
  }

  function mediaSnapshot(composer, prepared = null) {
    const batch = readBatch();
    const expected = prepared?.referenceCounts && typeof prepared.referenceCounts === "object"
      ? prepared.referenceCounts : batch?.expectedCounts;
    const preparedCounts = expected && typeof expected === "object"
      ? { image: Number(expected.image || 0), video: Number(expected.video || 0), audio: Number(expected.audio || 0) }
      : null;
    const preparedTotal = preparedCounts ? preparedCounts.image + preparedCounts.video + preparedCounts.audio : 0;
    try {
      const result = globalThis.__DOLA_TOOLKIT_MEDIA_SNAPSHOT__?.();
      const total = Number(result?.attachmentCount || 0) || Object.values(result?.counts || {}).reduce((sum, value) => sum + Number(value || 0), 0);
      if (result && typeof result === "object" && (total > 0 || preparedTotal < 1)) return result;
    } catch { /* Fall through to a conservative DOM count. */ }
    // The local console only proves that a file was selected.  It does not prove
    // that Dola has hydrated the corresponding 10052 attachment block yet.
    // Treat prepared counts as an expectation and let __DOLA_TOOLKIT_MEDIA_ENSURE__
    // verify the native store before the first HTTP submission.
    if (preparedTotal > 0) return { ready: false, pending: preparedTotal, counts: preparedCounts,
      attachmentCount: 0, expectedAttachmentCount: preparedTotal, source: "prepared_unverified" };
    let scope = composer;
    for (let i = 0; i < 4 && scope?.parentElement; i += 1) scope = scope.parentElement;
    const thumbnails = [...(scope?.querySelectorAll?.('img[src^="blob:"], img[src^="data:"]') || [])].filter(visible);
    return { ready: true, pending: 0, counts: { image: thumbnails.length, video: 0, audio: 0 }, attachmentCount: thumbnails.length, source: "dom" };
  }

  function snapshot() {
    const composer = firstVisible(COMPOSER_SELECTOR);
    const modelControl = firstVisible(MODEL_SELECTOR);
    const paramsControl = firstVisible(PARAMS_SELECTOR);
    const prompt = text(composer);
    const modelText = text(modelControl);
    const paramsText = text(paramsControl) || [
      text(firstVisible('button[data-input-engine-actionbar-control-key="video-ratio"]')),
      text(firstVisible('button[data-input-engine-actionbar-control-key="video-duration"]'))
    ].filter(Boolean).join(" · ");
    const nativeDuration = Number((paramsText.match(/(?:^|\D)(\d{1,2})\s*s(?:\D|$)/i) || [])[1] || 0);
    const nativeRatio = (paramsText.match(/(?:^|\s)(1:1|3:4|4:3|9:16|16:9|21:9)(?:\s|$)/) || [])[1] || "";
    const resolutionBridge = readJsonAttribute("data-watermark-free-resolution-bridge");
    const prepared = readJsonAttribute("data-dola-protocol-prepared-context");
    const batch = readBatch();
    const selected = readJsonAttribute(PARAMETER_ATTRIBUTE) || batch?.selection || {};
    const model = MODELS.has(selected.model) ? selected.model : MODELS.has(prepared?.model) ? prepared.model : modelFromText(modelText);
    const durationChoice = Number(selected.duration);
    const duration = DURATIONS.has(durationChoice) ? durationChoice : DURATIONS.has(nativeDuration) ? nativeDuration : Number(prepared?.duration || 0);
    const ratio = RATIOS.has(selected.ratio) ? selected.ratio : RATIOS.has(nativeRatio) ? nativeRatio : String(prepared?.ratio || "");
    const resolutionChoice = String(selected.resolution || "").toLowerCase();
    const bridgeResolution = String(resolutionBridge?.inputResolution || resolutionBridge?.selected || prepared?.resolution || "").toLowerCase();
    const resolution = RESOLUTIONS.has(resolutionChoice) ? resolutionChoice : RESOLUTIONS.has(bridgeResolution) ? bridgeResolution : "";
    return {
      promptLength: prompt.length,
      model,
      duration,
      ratio,
      resolution,
      media: mediaSnapshot(composer, prepared),
      expectedMediaCounts: (prepared?.referenceCounts && typeof prepared.referenceCounts === "object") || (batch?.expectedCounts && typeof batch.expectedCounts === "object")
        ? { image: Number((prepared?.referenceCounts || batch.expectedCounts).image || 0), video: Number((prepared?.referenceCounts || batch.expectedCounts).video || 0), audio: Number((prepared?.referenceCounts || batch.expectedCounts).audio || 0) }
        : null,
      taskId: String(prepared?.taskId || "").slice(0, 160),
      preparedAt: Number(prepared?.preparedAt || 0),
      composerReady: Boolean(composer),
      videoControlsReady: MODELS.has(model) && DURATIONS.has(duration) && RATIOS.has(ratio) && RESOLUTIONS.has(resolution),
      parameterSource: readJsonAttribute(PARAMETER_ATTRIBUTE) ? "expert_panel" : paramsText ? "native_controls" : "prepared_context",
      sendReady: Boolean(firstVisible(SEND_SELECTOR))
    };
  }

  function publishPreferences(current) {
    try {
      localStorage.setItem("intl_doubao_target_video_model_v1", current.model);
      localStorage.setItem("intl_doubao_target_duration_v1", String(current.duration));
      localStorage.setItem("intl_doubao_enable_30s_v1", current.duration === 30 ? "1" : "0");
      localStorage.setItem("intl_doubao_target_resolution_v1", current.resolution);
    } catch { /* Request-level injection remains authoritative. */ }
    document.documentElement?.setAttribute(MODEL_PREFERENCE_ATTRIBUTE, current.model);
    document.dispatchEvent(new Event(MODEL_PREFERENCE_EVENT));
    document.documentElement?.setAttribute(RESOLUTION_PREFERENCE_ATTRIBUTE, current.resolution);
    document.dispatchEvent(new Event(RESOLUTION_PREFERENCE_EVENT));
    document.documentElement?.setAttribute(DURATION_STATE_ATTRIBUTE, current.duration === 30 ? "1" : "0");
    document.dispatchEvent(new Event(DURATION_STATE_EVENT));
  }

  async function run(options = {}) {
    if (busy) return;
    busy = true;
    let continueBatch = null;
    try {
      state.requestId = String(document.documentElement?.getAttribute(REQUEST_ATTRIBUTE) || "").slice(0, 120);
      const mediaBridge = globalThis.__DOLA_TOOLKIT_MEDIA_MODE_SWITCH__;
      let batch = readBatch();
      if (batch && options.resume === true) {
        const ready = await prepareNextBatchComposer(batch);
        if (!ready) return;
      }
      mediaBridge?.beginRound?.({ requestId: state.requestId, reusePreserved: Boolean(batch?.completed) });
      const protocolMode = requestedProtocolMode(batch);
      state.protocolMode = protocolMode;
      const current = snapshot();
      current.protocolMode = protocolMode;
      const conversationsBefore = conversationSnapshot();
      publish("validating", protocolMode === "quick"
        ? "已读取当前草稿，正在校验快速模式原生链路"
        : "已读取当前草稿，正在校验专家模式链路", current);
      if (!current.composerReady || current.promptLength < 1) throw Error("提示词为空或编辑器未就绪，未发送");
      if (!current.videoControlsReady) {
        throw Error(`${protocolMode === "quick" ? "快速" : "专家"}模式参数不完整：请选择模型、秒数、尺寸和分辨率，未发送`);
      }
      const expectedCounts = current.expectedMediaCounts || current.media?.counts || { image: 0, video: 0, audio: 0 };
      const expectedTotal = Object.values(expectedCounts).reduce((sum, value) => sum + Number(value || 0), 0);
      if (!batch) {
        batch = { active: true, total: requestedBatchCount(), completed: 0, createdIds: [], protocolMode,
          prompt: text(firstVisible(COMPOSER_SELECTOR)), selection: { model: current.model, duration: current.duration,
            ratio: current.ratio, resolution: current.resolution }, expectedCounts, startedAt: Date.now() };
        writeBatch(batch);
      } else {
        writeBatch(batch);
      }
      if (expectedTotal > 0 || current.media?.pending > 0) {
        const ensure = globalThis.__DOLA_TOOLKIT_MEDIA_ENSURE__;
        if (typeof ensure !== "function") throw Error("素材校验模块未加载，未发送；请重新加载扩展并刷新页面");
        publish("awaiting_native_media", `正在等待 ${expectedTotal || current.media.pending} 个素材进入同一次 Dola 原生请求`, current);
        const verified = await ensure(expectedCounts, { timeoutMs: 10000 });
        current.media = verified && typeof verified === "object" ? verified : current.media;
        const actualCounts = current.media?.counts || {};
        let complete = ["image", "video", "audio"].every((kind) => Number(actualCounts[kind] || 0) >= Number(expectedCounts[kind] || 0));
        if (!complete || current.media?.ready !== true || Number(current.media?.pending || 0) > 0) {
          const actual = Number(current.media?.attachmentCount || 0);
          throw Error(`素材尚未进入 Dola 原生附件请求（需要 ${expectedTotal}，当前 ${actual}），已拦截首次错误提交`);
        }
      }
      const send = firstVisible(SEND_SELECTOR);
      if (!send) throw Error("发送按钮不可用，未发送");
      const expert = globalThis.__DOLA_SILENT_EXPERT__;
      if (!expert?.prepare) throw Error("协议注入模块未加载，请重新加载扩展并刷新页面");
      publishPreferences(current);
      const evidenceBefore = { generation: generationEvidence().sequence, silent: silentHttpEvidence().sequence };
      let dispatch;
      if (protocolMode === "quick") {
        expert.disarm?.("quick_protocol");
        dispatch = typeof expert.sendQuickNativeHttp === "function" ? expert.sendQuickNativeHttp.bind(expert)
          : async (target) => { target.click(); return true; };
      } else {
        expert.arm?.({ model: current.model, duration: current.duration, ratio: current.ratio, resolution: current.resolution,
          expectedMediaCounts: current.media?.counts || expectedCounts,
          expectedAttachmentCount: Number(current.media?.attachmentCount || expectedTotal), source: "protocol_button" });
        dispatch = typeof expert.sendNativeHttp === "function" ? expert.sendNativeHttp.bind(expert)
          : expert.selectBeforeSend?.bind(expert);
      }
      if (!dispatch) throw Error(`${protocolMode === "quick" ? "快速" : "专家"}协议发送器未加载，请重新加载扩展并刷新页面`);
      const dispatched = await dispatch(send, { allowHidden: false, source: "protocol_button",
        media: current.media, context: current });
      if (!dispatched) throw Error(expert.status?.reason || `未能通过${protocolMode === "quick" ? "快速" : "专家"}协议发送`);
      publish("awaiting_http_evidence", "已点击原生发送，正在等待真实 /chat/completion 请求证据", current);
      const evidence = await waitForHttpSubmission(evidenceBefore, 8000);
      if (!evidence) throw Error("原生发送按钮未产生真实 HTTP 请求，未标记为成功；请导出调试记录");
      const inject = globalThis.__DOLA_TOOLKIT_LAST_MEDIA_INJECT__ || null;
      const expertMedia = expert.status?.lastMediaInjection || null;
      const expertCounts = expertMedia?.counts || {};
      const expertMediaConfirmed = expertMedia?.applied === true && ["image", "video", "audio"]
        .every((kind) => Number(expertCounts[kind] || 0) >= Number(expectedCounts[kind] || 0));
      const quickMediaConfirmed = protocolMode === "quick" && current.media?.ready === true && ["image", "video", "audio"]
        .every((kind) => Number(current.media?.counts?.[kind] || 0) >= Number(expectedCounts[kind] || 0));
      if (expectedTotal > 0 && !["injected", "already_present"].includes(String(inject?.reason || "")) && !expertMediaConfirmed && !quickMediaConfirmed) {
        throw Error(`已观察到 HTTP 请求，但原图附件块未获确认（${String(inject?.reason || expertMedia?.source || "no_media_injection_evidence")}）`);
      }
      current.httpEvidence = { sequence: evidence.sequence, phase: evidence.phase, httpStatus: evidence.httpStatus ?? null,
        mediaInjection: expertMediaConfirmed ? { reason: "injected", source: expertMedia.source || "expert_pre_sign", messageKey: "content_block" }
          : inject ? { reason: inject.reason, source: inject.source, messageKey: inject.messageKey || "" } : null };
      publish("awaiting_conversation", protocolMode === "quick"
        ? "快速路由真实 HTTP 请求已确认，正在等待左侧创建新会话；Fast/15 仍以服务端结果为准"
        : "专家路由真实 HTTP 请求已确认，正在等待左侧创建新会话", current);
      const conversation = await waitForConversationCreated(conversationsBefore, 30000);
      if (!conversation) throw Error("真实 HTTP 请求已发出，但左侧 30 秒内未出现新会话；本次不标记成功");
      current.conversation = conversation;
      batch.completed = Number(batch.completed || 0) + 1;
      batch.createdIds = [...new Set([...(batch.createdIds || []), conversation.createdId])];
      if (batch.completed < batch.total) {
        batch.phase = "advancing";
        writeBatch(batch);
        mediaBridge?.completeRound?.({ requestId: state.requestId, conversationId: conversation.createdId, keepPreserved: true });
        publish("batch_advancing", `批量发送 ${batch.completed}/${batch.total} 成功；正在打开新对话继续`, current);
        continueBatch = batch;
      } else {
        batch.active = false;
        writeBatch(batch);
        clearBatch();
        mediaBridge?.completeRound?.({ requestId: state.requestId, conversationId: conversation.createdId });
        publish("conversation_confirmed", `批量发送完成 ${batch.completed}/${batch.total}；左侧已创建 ${batch.createdIds.length} 个新会话`, current);
      }
    } catch (error) {
      const failedBatch = readBatch();
      if (failedBatch) {
        failedBatch.active = false;
        state.batch = { total: failedBatch.total, completed: failedBatch.completed || 0,
          createdIds: failedBatch.createdIds || [] };
        clearBatch();
      }
      publish("blocked", String(error?.message || error));
    } finally {
      busy = false;
      if (continueBatch) setTimeout(() => { void run({ resume: true }); }, 500);
    }
  }

  document.addEventListener(REQUEST_EVENT, () => { void run(); });
  publish("idle", "等待协议发送", null);
  if (readBatch()) setTimeout(() => { void run({ resume: true }); }, 800);
})();
