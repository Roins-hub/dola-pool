(function () {
  "use strict";
  const INSTALL = "__doubaoDolaMediaCaptureV1656";
  const APIS_ATTRIBUTE = "data-dola-media-fallback-apis";
  const APIS_EVENT = "dola-media-fallback-apis";
  const MODELS_ATTRIBUTE = "data-dola-media-video-models";
  const MODELS_EVENT = "dola-media-video-models";
  const FORCE_ATTRIBUTE = "data-watermark-free-media-force";
  const STATUS_ATTRIBUTE = "data-watermark-free-media-capture";
  const STATUS_EVENT = "watermark-free-media-capture-status";
  const RETRY_EVENT = "watermark-free-media-retry";
  const LIMIT = 8;
  const TAIL_LIMIT = 65536;
  const IS_DOLA = /(^|\.)dola\.com$/i.test(location.hostname);
  const supported = (host) => /(^|\.)(doubao|dola)\.com$/i.test(host);
  if (!supported(location.hostname) || globalThis[INSTALL]) return;
  globalThis[INSTALL] = true;

  const state = { version: "1.6.60", installed: true, observedResponses: 0,
    capturedCandidates: 0, capturedModels: 0, captureEvents: 0, bytesRead: 0, transport: "", lastStatus: 0, error: "" };
  const directRecent = new Map();
  const recent = new Set();
  const pending = new Set();
  const modelRecent = new Set();
  const modelPending = new Set();
  const seenAt = new Map();
  const xhrStates = new WeakMap();
  let page = location.href;

  function publish() {
    document.documentElement?.setAttribute(STATUS_ATTRIBUTE, JSON.stringify(state));
    document.dispatchEvent(new Event(STATUS_EVENT));
  }

  function syncPage() {
    if (page === location.href) return;
    page = location.href;
    recent.clear(); pending.clear(); modelRecent.clear(); modelPending.clear(); seenAt.clear(); directRecent.clear();
    document.documentElement?.removeAttribute("data-dola-direct-videos");
    state.observedResponses = 0; state.capturedCandidates = 0; state.capturedModels = 0; state.captureEvents = 0;
    state.bytesRead = 0; state.error = "";
    document.documentElement?.removeAttribute(APIS_ATTRIBUTE);
    document.documentElement?.removeAttribute(MODELS_ATTRIBUTE);
    document.documentElement?.removeAttribute(FORCE_ATTRIBUTE);
  }

  function endpoint(input) {
    try {
      const raw = typeof input === "string" ? input : input?.url || input?.href || "";
      const url = new URL(raw, location.href);
      return supported(url.hostname) && /^https?:$/.test(url.protocol) &&
        /(?:^|\/)(?:im\/|chat\/completion(?:\/|$))/.test(url.pathname);
    } catch { return false; }
  }

  function flush(force = false) {
    const root = document.documentElement;
    if (!root || (!pending.size && !modelPending.size)) return;
    let existing = [];
    try { existing = JSON.parse(root.getAttribute(APIS_ATTRIBUTE) || "[]"); } catch { /* Start a new batch. */ }
    const values = [...new Set([...(Array.isArray(existing) ? existing : []), ...pending])].slice(-LIMIT);
    pending.clear();
    if (values.length) root.setAttribute(APIS_ATTRIBUTE, JSON.stringify(values));
    let existingModels = [];
    try { existingModels = JSON.parse(root.getAttribute(MODELS_ATTRIBUTE) || "[]"); } catch { /* Start a new model batch. */ }
    const models = [...new Set([...(Array.isArray(existingModels) ? existingModels : []), ...modelPending])].slice(-LIMIT);
    modelPending.clear();
    if (models.length) root.setAttribute(MODELS_ATTRIBUTE, JSON.stringify(models));
    if (force) root.setAttribute(FORCE_ATTRIBUTE, "1");
    state.captureEvents += 1;
    publish();
    if (values.length) document.dispatchEvent(new Event(APIS_EVENT));
    if (models.length) document.dispatchEvent(new Event(MODELS_EVENT));
  }

  function captureMeta(value, inherited = {}) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return inherited;
    const id = [value.conversation_id, value.conversationId, value.local_conversation_id, value.localConversationId]
      .find((item) => ["string", "number"].includes(typeof item) && String(item));
    const order = [value.message_index, value.index_in_conv, value.create_time, value.createTime]
      .find((item) => ["string", "number"].includes(typeof item) && String(item));
    return { conversationId: id == null ? String(inherited.conversationId || "") : String(id),
      order: order == null ? inherited.order ?? null : Number.isFinite(Number(order)) ? Number(order) : String(order) };
  }

  function scopeMetaToCurrentConversation(meta = {}) {
    // Dola history payloads can expose an internal/local conversation id even
    // though the browser has already promoted the route to its numeric id.
    // The response was observed on this exact page, so retain the requirement
    // that an owning id exists but normalize it to the visible conversation.
    // This prevents the panel's strict filter from discarding valid
    // video_model blocks and also keeps resources out of neighbouring chats.
    if (!IS_DOLA || !String(meta.conversationId || "")) return meta;
    try {
      const current = new URL(location.href).pathname.match(/^\/chat\/(\d{8,})(?:\/|$)/i)?.[1] || "";
      return current ? { ...meta, conversationId: current } : meta;
    } catch { return meta; }
  }

  function addFallback(raw, meta = {}) {
    meta = scopeMetaToCurrentConversation(meta);
    if (IS_DOLA && !String(meta.conversationId || "")) return;
    try {
      const url = new URL(decoded(raw));
      if (!/^https?:$/.test(url.protocol)) return;
      const envelope = JSON.stringify({ conversationId: String(meta.conversationId || ""), order: meta.order ?? null, url: url.toString() });
      if (recent.has(envelope)) return;
      recent.add(envelope); pending.add(envelope);
      while (recent.size > LIMIT) recent.delete(recent.values().next().value);
      state.capturedCandidates = recent.size;
    } catch { /* Ignore malformed candidates; never guess a URL. */ }
  }

  function addVideoModel(raw, meta = {}) {
    meta = scopeMetaToCurrentConversation(meta);
    if (IS_DOLA && !String(meta.conversationId || "")) return;
    let value = raw;
    for (let count = 0; count < 4 && typeof value === "string"; count += 1) {
      const text = value.trim();
      if (!text || text.length > 500_000 || !/^[{[]/.test(text)) return;
      try { value = JSON.parse(text); } catch { return; }
    }
    if (!value || typeof value !== "object" || Array.isArray(value)) return;
    const videoList = value.video_list || value.video_info?.video_list || value.data?.video_list || value.data?.video_info?.video_list;
    if (!videoList || typeof videoList !== "object") return;
    let encoded;
    try { encoded = JSON.stringify({ conversationId: String(meta.conversationId || ""), order: meta.order ?? null, model: value }); } catch { return; }
    if (!encoded || encoded.length > 500_000 || modelRecent.has(encoded)) return;
    modelRecent.add(encoded); modelPending.add(encoded);
    while (modelRecent.size > LIMIT) modelRecent.delete(modelRecent.values().next().value);
    state.capturedModels = modelRecent.size;
  }

  function captureStructuredModels(text) {
    if (typeof text !== "string" || (!text.includes("video_model") && !(IS_DOLA && text.includes("fallback_api"))) || text.length > 2_000_000) return;
    const seen = new Set();
    let steps = 0;
    function walk(value, depth = 0, inheritedMeta = {}) {
      if (value == null || depth > 20 || steps++ > 20_000) return;
      if (typeof value === "string") {
        const source = value.trim();
        if (source.length <= 500_000 && /^[{[]/.test(source) && (source.includes("video_model") || IS_DOLA && source.includes("fallback_api"))) {
          try { walk(JSON.parse(source), depth + 1, inheritedMeta); } catch { /* Not a nested JSON container. */ }
        }
        return;
      }
      if (typeof value !== "object" || seen.has(value)) return;
      seen.add(value);
      const meta = captureMeta(value, inheritedMeta);
      if (!Array.isArray(value) && Object.hasOwn(value, "video_model")) addVideoModel(value.video_model, meta);
      if (IS_DOLA && !Array.isArray(value) && Object.hasOwn(value, "fallback_api")) addFallback(value.fallback_api, meta);
      for (const child of Object.values(value)) walk(child, depth + 1, meta);
    }
    let parsed = false;
    try { walk(JSON.parse(text)); parsed = true; } catch { /* A streaming response can contain multiple SSE envelopes. */ }
    if (!parsed) {
      for (const line of text.split(/\r?\n/)) {
        const match = /^data:\s*(.+)$/.exec(line);
        if (!match || match[1] === "[DONE]" || (!match[1].includes("video_model") && !(IS_DOLA && match[1].includes("fallback_api")))) continue;
        try { walk(JSON.parse(match[1])); } catch { /* Ignore incomplete heartbeat or event frames. */ }
      }
    }
    flush();
  }

  function decoded(value) {
    let text = String(value || "");
    for (let count = 0; count < 5; count += 1) {
      try {
        const next = JSON.parse(`"${text.replace(/"/g, '\\"')}"`);
        if (next === text) break;
        text = next;
      } catch { break; }
    }
    return text.replace(/\\u0026/gi, "&").replace(/\\u003d/gi, "=").replace(/\\\//g, "/");
  }

  function capture(text) {
    // Dola hydrates multiple sidebar conversations into the same document.
    // A raw URL has no owner, so only Doubao may use this unscoped fast path.
    const direct = IS_DOLA ? [] : globalThis.DolaDirectVideo?.extract(text, true) || [];
    let directChanged = false;
    for (const item of direct) {
      if (!directRecent.has(item.url)) directChanged = true;
      directRecent.set(item.url, item);
    }
    while (directRecent.size > 32) directRecent.delete(directRecent.keys().next().value);
    if (directChanged && document.documentElement) {
      document.documentElement.setAttribute("data-dola-direct-videos", JSON.stringify({ page: location.href, items: [...directRecent.values()] }));
      document.dispatchEvent(new Event("dola-direct-videos"));
    }
    const patterns = [/fallback_api\\*"\s*:\s*\\*"(.+?)\\*"/g, /"fallback_api"\s*:\s*"([^"]+)"/g];
    // Dola can hydrate several sidebar conversations in the background. Raw
    // matches have no trustworthy owner; keep only structured candidates that
    // carried an enclosing conversation_id.
    if (IS_DOLA) patterns.length = 0;
    for (const pattern of patterns) {
      for (const match of String(text || "").matchAll(pattern)) {
        try {
          const url = new URL(decoded(match[1]));
          if (!/^https?:$/.test(url.protocol)) continue;
          const value = url.toString();
          const now = Date.now();
          if (now - (seenAt.get(value) || 0) < 1500) continue;
          seenAt.set(value, now);
          while (seenAt.size > 200) seenAt.delete(seenAt.keys().next().value);
          recent.delete(value); recent.add(value);
          while (recent.size > LIMIT) recent.delete(recent.values().next().value);
          pending.add(value);
        } catch { /* Ignore malformed candidates; never guess a URL. */ }
      }
    }
    state.capturedCandidates = recent.size;
    flush();
  }

  function scanner() {
    let tail = "";
    return (text) => {
      for (let offset = 0; offset < text.length; offset += TAIL_LIMIT) {
        const part = text.slice(offset, offset + TAIL_LIMIT);
        capture(tail + part);
        tail = (tail + part).slice(-TAIL_LIMIT);
      }
    };
  }

  async function observeResponse(response, capturedPage) {
    if (capturedPage !== location.href) return;
    state.observedResponses += 1; state.transport = "fetch"; state.lastStatus = response?.status || 0;
    publish();
    if (!response?.ok) { state.error = `HTTP_${response?.status || 0}`; publish(); return; }
    const type = response.headers?.get("content-type") || "";
    if (type && !/json|event-stream|text\/plain/i.test(type)) return;
    let reader;
    let fullText = "";
    try {
      const copy = response.clone();
      const scan = scanner();
      if (!copy.body?.getReader) { fullText = await copy.text(); scan(fullText); captureStructuredModels(fullText); return; }
      reader = copy.body.getReader();
      const decoder = new TextDecoder();
      while (true) {
        const result = await reader.read();
        if (capturedPage !== location.href) { void reader.cancel().catch(() => {}); break; }
        if (result.done) { const tail = decoder.decode(); scan(tail); fullText += tail; break; }
        state.bytesRead += result.value.byteLength;
        const chunk = decoder.decode(result.value, { stream: true });
        scan(chunk);
        if (fullText.length < 2_000_000) fullText += chunk.slice(0, 2_000_000 - fullText.length);
      }
      captureStructuredModels(fullText);
      state.error = "";
    } catch {
      state.error = "RESPONSE_READ_FAILED";
    } finally {
      try { reader?.releaseLock(); } catch { /* Reader may have been canceled. */ }
      publish();
    }
  }

  const nativeFetch = window.fetch;
  if (typeof nativeFetch === "function") {
    window.fetch = function passiveMediaFetch(input) {
      syncPage();
      const watched = endpoint(input);
      const capturedPage = location.href;
      const result = nativeFetch.apply(this, arguments);
      if (watched) {
        // Register on the returned promise itself. Assimilating a foreign promise
        // first can let the page consume the body before our clone is created.
        if (typeof result?.then === "function") {
          result.then((response) => { void observeResponse(response, capturedPage).catch(() => {}); }, () => {});
        } else void observeResponse(result, capturedPage).catch(() => {});
      }
      return result;
    };
  }

  if (typeof XMLHttpRequest !== "undefined") {
    const proto = XMLHttpRequest.prototype;
    const nativeOpen = proto.open;
    proto.open = function passiveMediaOpen(method, url) {
      const previous = xhrStates.get(this);
      if (previous) for (const event of ["readystatechange", "progress", "load"]) this.removeEventListener(event, previous.observe);
      xhrStates.delete(this);
      const result = nativeOpen.apply(this, arguments);
      syncPage();
      if (!endpoint(url)) return result;
      const local = { page: location.href, offset: 0, jsonRead: false, started: false, scan: scanner(), observe: null };
      local.observe = () => {
        if (local.page !== location.href || this.readyState < 3) return;
        if (!local.started) {
          local.started = true; state.observedResponses += 1; state.transport = "xhr";
        }
        state.lastStatus = this.status;
        if (this.status < 200 || this.status >= 300) { state.error = `HTTP_${this.status}`; publish(); return; }
        try {
          if (this.responseType === "json") {
            if (this.readyState === 4 && !local.jsonRead) {
              local.jsonRead = true;
              local.scan(JSON.stringify(this.response || {}));
            }
          } else if (!this.responseType || this.responseType === "text") {
            const raw = this.responseText;
            const next = raw.slice(local.offset);
            local.offset = raw.length; state.bytesRead += next.length;
            local.scan(next);
            if (this.readyState === 4) captureStructuredModels(raw);
          }
          state.error = "";
        } catch { state.error = "RESPONSE_READ_FAILED"; }
        publish();
      };
      xhrStates.set(this, local);
      for (const event of ["readystatechange", "progress", "load"]) this.addEventListener(event, local.observe);
      return result;
    };
  }

  function bootstrapJson() {
    // Existing conversations may be hydrated from server-delivered JSON rather
    // than making a new history request. Read JSON scripts, never rendered text.
    if (typeof document.querySelectorAll !== "function") return;
    for (const node of document.querySelectorAll('script[type="application/json"]')) {
      const text = String(node.textContent || "");
      if ((!text.includes("fallback_api") && !text.includes("video_model") && !text.includes("dola.com")) || text.length > 2_000_000) continue;
      try {
        JSON.parse(text);
        state.transport = "bootstrap-json";
        scanner()(text);
        captureStructuredModels(text);
      } catch { /* Not JSON metadata; do not interpret arbitrary script source. */ }
    }
  }

  document.addEventListener("DOMContentLoaded", () => { bootstrapJson(); flush(); publish(); }, { once: true });
  if (document.readyState && document.readyState !== "loading") bootstrapJson();
  document.addEventListener(RETRY_EVENT, () => {
    syncPage();
    if (!recent.size && !modelRecent.size) bootstrapJson();
    if (!recent.size && !modelRecent.size) { state.error = "REOPEN_VIDEO_CONVERSATION"; publish(); return; }
    for (const url of recent) pending.add(url);
    for (const model of modelRecent) modelPending.add(model);
    flush(true);
  });
  if (typeof setInterval === "function") setInterval(() => {
    const before = page;
    syncPage();
    if (before !== page) publish();
  }, 500);
  Object.defineProperty(window, "__DOUBAO_DOLA_MEDIA_CAPTURE__", {
    configurable: true, get: () => JSON.parse(JSON.stringify(state))
  });
  publish();
})();
