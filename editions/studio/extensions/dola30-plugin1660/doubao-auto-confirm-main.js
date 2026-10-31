(function () {
  "use strict";
  const INSTALL = "__doubaoAutoConfirmV1628";
  const KEY = "intl_doubao_auto_confirm_v1";
  const ATTRIBUTE = "data-watermark-free-auto-confirm";
  const EVENT = "watermark-free-auto-confirm";
  const PREF_ATTRIBUTE = "data-watermark-free-auto-confirm-enabled";
  const PREF_EVENT = "watermark-free-auto-confirm-change";
  const EDITORS = 'textarea, [contenteditable="true"], [role="textbox"]';
  const HEADINGS = 'strong, h1, h2, h3, h4, [role="heading"]';
  const MESSAGE = "确认以上视频生成参数。请现在使用上一条完整剧本与全部参考素材调用视频生成，不要再次列参数或分镜等待确认。如果无法调用，请直接说明实际原因。";
  if (!/(^|\.)doubao\.com$/i.test(location.hostname) || globalThis[INSTALL]) return;
  globalThis[INSTALL] = true;

  let interaction = 0;
  let active = null;
  let writing = false;
  let lastPublished = "";
  const state = { version: "1.6.35", enabled: true, phase: "idle", sourceSequence: null,
    confirmationSequence: null, actionTaken: false, mode: "", triggerCount: 0, reason: "等待本轮手动提交的视频请求" };
  function enabled() { try { return localStorage.getItem(KEY) !== "0"; } catch { return false; } }
  function text(node) { return String(node?.textContent || "").replace(/\s+/g, " ").trim(); }
  function read(editor) { return /^(?:TEXTAREA|INPUT)$/.test(editor?.tagName || "") ? String(editor.value || "") : String(editor?.innerText ?? editor?.textContent ?? ""); }
  function visible(node) {
    if (!node?.isConnected) return false;
    const rect = node.getBoundingClientRect(); const style = getComputedStyle(node);
    return rect.width > 1 && rect.height > 1 && style.display !== "none" && style.visibility !== "hidden";
  }
  function publish() {
    state.enabled = enabled();
    const encoded = JSON.stringify(state);
    if (encoded === lastPublished || !document.documentElement) return;
    lastPublished = encoded; document.documentElement.setAttribute(ATTRIBUTE, encoded); document.dispatchEvent(new Event(EVENT));
  }
  function evidence() {
    try { return JSON.parse(document.documentElement?.getAttribute("data-watermark-free-generation-evidence") || "null"); }
    catch { return null; }
  }
  function decode(value) {
    try { const result = typeof value === "string" ? JSON.parse(value) : value; return result && typeof result === "object" && !Array.isArray(result) ? result : null; }
    catch { return null; }
  }
  function videoParameters(request) {
    if (!Array.isArray(request?.messages)) return null;
    let index = request.messages.length - 1;
    while (index >= 0 && /system|assistant|bot|tool/i.test(String(request.messages[index]?.role || request.messages[index]?.sender_role || request.messages[index]?.sender_type || ""))) index -= 1;
    const ability = decode(request.messages[index]?.ext?.chat_ability);
    const params = decode(ability?.ability_param);
    const duration = Number(params?.duration);
    return Number(ability?.ability_type) === 17 && params && Number.isFinite(duration) && duration > 0 && duration <= 600 && typeof params.model === "string"
      ? { model: params.model, duration, ratio: params.ratio, resolution: params.resolution } : null;
  }
  function write(editor, value) {
    writing = true;
    try {
      editor.focus();
      if (/^(?:TEXTAREA|INPUT)$/.test(editor.tagName || "")) {
        let prototype = Object.getPrototypeOf(editor); let setter;
        while (prototype && !setter) { setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set; prototype = Object.getPrototypeOf(prototype); }
        if (!setter) return false;
        setter.call(editor, value);
        editor.dispatchEvent(new Event("input", { bubbles: true }));
      } else {
        if (read(editor) && read(editor) !== MESSAGE) return false;
        const selection = window.getSelection?.(); const range = document.createRange?.();
        if (!selection || !range || typeof document.execCommand !== "function") return false;
        range.selectNodeContents(editor); selection.removeAllRanges(); selection.addRange(range);
        if (!document.execCommand("insertText", false, value)) return false;
      }
      return read(editor).trim() === value;
    } catch { return false; }
    finally { writing = false; }
  }
  function clearPrepared() {
    const editor = active?.editor;
    if (active?.prepared && editor?.isConnected && read(editor).trim() === MESSAGE) write(editor, "");
  }
  function stop(phase, reason) { clearPrepared(); active = null; state.phase = phase; state.reason = reason; publish(); }
  function headings() {
    return [...document.querySelectorAll(HEADINGS)].filter(node => visible(node) && /^(?:视频生成参数确认|视频参数确认|生成参数确认)$/.test(text(node)));
  }
  function fingerprint(heading) {
    for (let node = heading.parentElement, depth = 0; node && depth < 8; node = node.parentElement, depth += 1) {
      if (node.tagName === "BODY" || node.tagName === "HTML" || node.querySelector(EDITORS)) break;
      const content = text(node);
      if (content.length > 24_000) break;
      const count = [...node.querySelectorAll(HEADINGS)].filter(item => /^(?:视频生成参数确认|视频参数确认|生成参数确认)$/.test(text(item))).length;
      if (count === 1 && /模型/.test(content) && /时长/.test(content)) return content;
    }
    return "";
  }
  function observeDraft(request, snapshot) {
    const age = Date.now() - snapshot?.at;
    if (!enabled() || !snapshot?.userGesture || snapshot.interactionToken !== interaction ||
        !snapshot.text?.trim() || !Number.isFinite(age) || age < 0 || age > 60_000 || snapshot.text.trim() === MESSAGE) return;
    const params = videoParameters(request); if (!params) return;
    if (/视频生成参数确认|视频参数确认|生成参数确认/.test(snapshot.text)) {
      stop("skipped_prompt_confirmation", "原稿含参数确认标题，自动续确认暂停；生成指令前缀可独立生效"); return;
    }
    if (active) clearPrepared();
    const latest = evidence();
    const previousHeadings = headings();
    active = { params, baseline: new Set(previousHeadings), baselineText: new Set(previousHeadings.map(fingerprint).filter(Boolean)), deadline: Date.now() + 180_000, responseEndedAt: null,
      interaction, editor: null, prepared: false, preparedAt: null };
    Object.assign(state, { phase: "waiting_response", sourceSequence: Number(latest?.sequence || 0) + 1,
      confirmationSequence: null, actionTaken: false, mode: "", triggerCount: 0, reason: "等待本轮视频请求的原始回复结束" });
    publish();
  }
  function confirmationRoot() {
    const params = active.params;
    for (const heading of headings()) {
      if (active.baseline.has(heading) || active.baselineText.has(fingerprint(heading))) continue;
      for (let node = heading.parentElement, depth = 0; node && depth < 8; node = node.parentElement, depth += 1) {
        if (node.tagName === "BODY" || node.tagName === "HTML") break;
        if (!visible(node) || node.querySelector(EDITORS)) continue;
        const full = text(node); if (full.length > 24_000) continue;
        const confirmationHeadings = [...node.querySelectorAll(HEADINGS)].filter(item => /^(?:视频生成参数确认|视频参数确认|生成参数确认)$/.test(text(item)));
        if (confirmationHeadings.length !== 1) continue;
        const content = full.slice(full.indexOf(text(heading)));
        const tail = content.replace(text(heading), "");
        const ask = /请(?:你)?(?:回复|确认)|(?:回复|点击)[^。！？]{0,24}(?:确认|开始生成)|确认(?:无误|以上(?:方案|参数))?[^。！？]{0,30}(?:后|即可|就|开始生成)/.test(tail);
        if (!ask || /无法(?:调用|生成)|不能(?:调用|生成)|额度不足|没有(?:可用)?(?:额度|权限)|需要(?:升级|开通|购买)|请(?:先)?(?:充值|购买|开通)/.test(tail)) continue;
        const model = content.match(/模型\s*[:：]?\s*Seedance\s*(2\.5|2\.0)(?:\s*(Fast|Mini))?/i);
        const selectedModel = !model ? "" : model[1] === "2.5" ? "seedance_v2.5" : /fast/i.test(model[2] || "") ? "seedance_v2.0" : /mini/i.test(model[2] || "") ? "seedance_v2.0_mini" : "seedance_v2.0_std";
        const duration = Number(content.match(/时长\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(?:秒|s)/i)?.[1]);
        if (selectedModel !== params.model || duration !== Number(params.duration)) continue;
        if (typeof params.ratio === "string" && !content.replace(/\s/g, "").includes(params.ratio)) continue;
        const resolution = content.match(/(?:分辨率|清晰度)\s*[:：]?\s*(480|720|1080)\s*p/i);
        if (resolution && params.resolution && `${resolution[1]}p` !== String(params.resolution).toLowerCase()) continue;
        return node;
      }
    }
    return null;
  }
  function label(button) { return [button.getAttribute("aria-label"), button.getAttribute("title"), text(button)].filter(Boolean).join(" ").trim(); }
  function ready(button) { return visible(button) && !button.disabled && button.getAttribute("aria-disabled") !== "true"; }
  function sendButton(editor) {
    for (let root = editor.parentElement, depth = 0; root && depth < 7; root = root.parentElement, depth += 1) {
      if (root.tagName === "BODY" || root.tagName === "HTML") break;
      for (const button of root.querySelectorAll('button, [role="button"]')) {
        const title = label(button);
        if (/语音|麦克风|停止|取消|升级|开通|支付|充值|购买|voice|microphone/i.test(title)) continue;
        const identified = /^(?:发送(?:消息)?|提交|send(?: message)?|submit)(?:\s|$)/i.test(title) ||
          /^(?:chat_input_send_button|chat-input-send-button|send-message-button|send-button)$/.test(button.getAttribute("data-testid") || "") ||
          button.getAttribute("type") === "submit";
        if (identified && ready(button)) return button;
      }
    }
    return null;
  }
  function clickOnce(button, mode) {
    state.actionTaken = true; state.triggerCount = 1; state.mode = mode;
    state.phase = "action_triggered"; state.reason = "已触发一次网页原生确认；尚未验证视频任务";
    active.prepared = false; active.actionAt = Date.now(); publish();
    try { button.click(); } catch { stop("blocked", "网页原生确认控件调用失败，未重试"); }
  }
  function advance() {
    if (!active) return;
    if (!enabled()) { stop("disabled", "自动续确认已关闭"); return; }
    if (active.interaction !== interaction) { stop("cancelled", "检测到新的用户操作，已取消本轮自动续确认"); return; }
    const result = evidence();
    if (state.actionTaken) {
      if (Number(result?.sequence) > state.sourceSequence) {
        state.confirmationSequence = result.sequence; active = null; state.phase = "confirmation_request_observed";
        state.reason = "已观察到原生确认后的后续请求；尚未验证视频任务"; publish();
      } else if (Date.now() - active.actionAt > 10_000) stop("action_unconfirmed", "原生确认已触发一次，未观察到后续请求；不会重试");
      return;
    }
    if (Date.now() > active.deadline) { stop("expired", "等待确认超时，未发送额外消息"); return; }
    if (Number(result?.sequence) > state.sourceSequence) { stop("cancelled", "另一提交已开始，已取消本轮续确认"); return; }
    if (result?.sequence !== state.sourceSequence) return;
    if (result.phase === "http_error" || result.phase === "transport_error") { stop("blocked", "原请求返回错误，不自动续确认"); return; }
    if (result.phase !== "response_ended" || result.httpStatus !== 200) return;
    if (result.taskFields?.length || result.statusFields?.some(field => /error_code/.test(field.key) && Number(field.value) !== 0 || field.path === "$.code" && Number(field.value) !== 0)) {
      stop("not_needed", "响应已含任务候选字段或业务错误，未追加确认"); return;
    }
    if (active.prepared) {
      if (!active.editor?.isConnected || read(active.editor).trim() !== MESSAGE) { stop("cancelled", "原生编辑框内容已变化，未发送确认"); return; }
      const button = sendButton(active.editor);
      if (button) clickOnce(button, "native_composer");
      else if (Date.now() - active.preparedAt > 3000) stop("blocked", "未识别原生发送按钮，已撤回自动填入的确认文字");
      return;
    }
    if (active.responseEndedAt == null) active.responseEndedAt = Date.now();
    const root = confirmationRoot();
    if (!root) {
      if (Date.now() - active.responseEndedAt > 8000) stop("not_needed", "未识别到本轮明确的参数确认，未追加消息");
      return;
    }
    const buttons = [...root.querySelectorAll('button, [role="button"]')].filter(button => /^(?:确认生成|开始生成|立即生成|按此生成|确认并生成|确认并生成视频|生成视频)$/.test(text(button)));
    if (buttons.length) { if (ready(buttons[0])) clickOnce(buttons[0], "native_button"); return; }
    const editors = [...document.querySelectorAll(EDITORS)].filter(editor => visible(editor) && !editor.disabled && !editor.readOnly);
    const editor = editors[editors.length - 1];
    if (!editor || read(editor).trim()) { stop("blocked", "编辑框已有内容或不可用，未覆盖用户文字"); return; }
    active.editor = editor; active.prepared = true; active.preparedAt = Date.now();
    if (!write(editor, MESSAGE)) { stop("blocked", "原生编辑器未接受确认文字，未发送"); return; }
    state.phase = "preparing_confirmation"; state.mode = "native_composer"; state.reason = "正在通过原生编辑框准备一次确认"; publish();
  }
  function userInteraction(event) {
    if (!event.isTrusted || writing) return;
    const path = event.composedPath?.() || [];
    if (path.some(node => node?.id === "watermark-free-media-panel") && path.some(node =>
      String(node?.getAttribute?.("class") || "").split(/\s+/).some(name => name === "panel-toggle" || name === "resolution-diagnostics" || name === "debugger-export"))) return;
    if (event.type === "input" && !read(event.target).trim()) return; // Native composer clearing is not a new authored request.
    interaction += 1;
    if (active) stop("cancelled", "检测到新的用户操作，已取消本轮自动续确认");
  }
  for (const name of ["pointerdown", "keydown", "input"]) document.addEventListener(name, userInteraction, true);
  document.addEventListener(PREF_EVENT, () => {
    const value = document.documentElement?.getAttribute(PREF_ATTRIBUTE); if (value !== "0" && value !== "1") return;
    try { localStorage.setItem(KEY, value); } catch { return; }
    if (value === "0" && active) stop("disabled", "自动续确认已关闭"); else publish();
  });
  document.addEventListener("watermark-free-generation-evidence", advance);
  Object.defineProperty(globalThis, "__DOUBAO_AUTO_CONFIRM__", { configurable: true, value: Object.freeze({
    get status() { return JSON.parse(JSON.stringify({ ...state, enabled: enabled() })); },
    get interactionToken() { return interaction; }, observeDraft, scan: advance
  }) });
  function start() { publish(); setInterval(advance, 300); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start, { once: true }); else start();
})();
