(function () {
  "use strict";
  const INSTALL = "__doubaoDolaGenerationEvidenceV1626";
  const ATTRIBUTE = "data-watermark-free-generation-evidence";
  const EVENT = "watermark-free-generation-evidence";
  const LIMIT = 2_000_000;
  if (!/(^|\.)(doubao|dola)\.com$/i.test(location.hostname) || globalThis[INSTALL]) return;
  globalThis[INSTALL] = true;

  let sequence = 0;
  let page = location.href;
  let state;
  let lastPublished = "";
  const taskIds = new Set(["task_id", "video_task_id", "generation_task_id", "generation_id", "video_id", "job_id"]);
  const taskStatuses = new Set(["task_status", "video_status", "generation_status", "job_status"]);
  const billingKeys = new Set(["credit_cost", "credits_cost", "point_cost", "points_cost", "cost_credits", "cost_points",
    "consume_credits", "consume_points", "consumed_credits", "consumed_points", "deducted_credits", "deducted_points",
    "quota_cost", "quota_consumed", "remaining_credits", "remaining_points", "remaining_quota", "generation_quota"]);
  const statusKeys = new Set(["code", "error_code", "status_code", "biz_code", "ret_code"]);
  const textKeys = new Set(["text", "prompt", "message_text", "user_input_content", "input_text", "reasoning_content"]);
  const encodedKeys = new Set(["data", "event_data", "payload", "result", "body", "content", "extra", "video_generation", "task"]);

  function fresh() {
    return { version: "1.6.35", sequence, phase: "idle", transport: "", httpStatus: null,
      scope: "submission_page", routeChangeCount: 0,
      videoParameters: false, requestParameters: null, requestIntent: null, bytesRead: 0, parsedObjects: 0,
      taskFields: [], billingFields: [], statusFields: [], truncated: false, error: "",
      verification: "UNVERIFIED" };
  }
  state = fresh();
  function publish() {
    const text = JSON.stringify(state);
    if (text === lastPublished || !document.documentElement) return;
    lastPublished = text;
    document.documentElement.setAttribute(ATTRIBUTE, text);
    document.dispatchEvent(new Event(EVENT));
  }
  function syncPage() {
    if (page === location.href) return;
    page = location.href;
    if (state.phase !== "idle") {
      // A first submission can promote /chat/ to a conversation URL while its
      // response is still arriving. Keep the response tied to its sequence,
      // and disclose the route change instead of silently erasing evidence.
      state.scope = "page_changed_since_submission";
      state.routeChangeCount += 1;
      publish();
    }
  }
  function current(ticket) { syncPage(); return ticket?.sequence === state.sequence && state.phase !== "idle"; }
  function begin() {
    syncPage(); sequence += 1; state = fresh(); state.phase = "request_observed"; publish();
    return Object.freeze({ sequence });
  }
  function noteRequest(ticket, parameters, intent) {
    if (!current(ticket)) return;
    state.videoParameters = Boolean(parameters);
    state.requestParameters = parameters ? { model: String(parameters.model || "").slice(0, 80),
      resolution: String(parameters.resolution || "").slice(0, 20), duration: parameters.duration ?? null } : null;
    state.requestIntent = intent ? { video: intent.video == null ? null : intent.video === true,
      present: intent.present == null ? null : intent.present === true,
      inputBoxPresent: intent.inputBoxPresent === true } : null;
    publish();
  }
  function normalized(key) { return String(key).replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase(); }
  function numeric(value) {
    return (typeof value === "number" || typeof value === "string" && /^-?\d{1,15}(?:\.\d{1,6})?$/.test(value)) && Number.isFinite(Number(value));
  }
  function add(list, item) {
    if (list.length < 16 && !list.some(existing => existing.path === item.path && JSON.stringify(existing.value) === JSON.stringify(item.value))) list.push(item);
  }
  function inspect(value) {
    const seen = new Set(); let count = 0;
    function walk(node, path = "$", parentKey = "", depth = 0, encodings = 0) {
      if (depth > 24 || count > 10_000) return;
      if (typeof node === "string" && encodedKeys.has(parentKey) && encodings < 4 && node.length <= LIMIT && /^[\s]*[\[{]/.test(node)) {
        try { walk(JSON.parse(node), path + "<json>", parentKey, depth + 1, encodings + 1); } catch { /* Unknown native envelope. */ }
        return;
      }
      if (!node || typeof node !== "object" || seen.has(node)) return;
      seen.add(node); count += 1;
      const taskContext = Object.keys(node).some(key => taskIds.has(normalized(key)));
      for (const [key, child] of Object.entries(node)) {
        const name = normalized(key);
        if (textKeys.has(name)) continue; // Never derive tasks or prices from assistant prose.
        const segment = Array.isArray(node) ? "[]" : /^[a-zA-Z_][a-zA-Z0-9_]{0,47}$/.test(key) ? "." + key : ".[key]";
        const childPath = (path + segment).slice(0, 500);
        if (taskIds.has(name) && (typeof child === "string" || typeof child === "number") && String(child).length > 0) {
          // IDs are diagnostic candidates, not proof of video acceptance. Keep
          // their presence/type/length, never an account's raw ID or token.
          add(state.taskFields, { path: childPath, key: name, value: { present: true, type: typeof child, length: String(child).length } });
        } else if ((taskStatuses.has(name) || name === "status" && taskContext) &&
                   (numeric(child) || typeof child === "string" && /^[a-zA-Z_]{2,30}$/.test(child))) {
          add(state.taskFields, { path: childPath, key: name, value: child });
        } else if ((billingKeys.has(name) || name === "cost" && /^(?:billing|quota|credits|points|video_task|video_generation)$/.test(parentKey)) && numeric(child)) {
          add(state.billingFields, { path: childPath, key: name, value: child });
        } else if (statusKeys.has(name) && (numeric(child) || typeof child === "string" && /^[A-Z][A-Z_]{1,40}$/.test(child))) {
          add(state.statusFields, { path: childPath, key: name, value: child });
        }
        walk(child, childPath, name, depth + 1, encodings);
      }
    }
    walk(value); state.parsedObjects += 1;
  }
  function observer(ticket, transport) {
    let buffer = ""; let whole = ""; let eventData = []; let finished = false;
    function parse(text) {
      const raw = text.trim(); if (!raw || raw === "[DONE]") return;
      try { inspect(JSON.parse(raw)); } catch { /* Text and unknown framing are not task evidence. */ }
    }
    return {
      start(status) {
        if (!current(ticket)) return;
        state.transport = transport; state.httpStatus = Number(status) || 0;
        state.phase = status >= 200 && status < 300 ? "response_streaming" : "http_error"; publish();
      },
      feed(text) {
        if (finished || !current(ticket) || typeof text !== "string") return;
        state.bytesRead += typeof TextEncoder === "function" ? new TextEncoder().encode(text).length : text.length;
        if (state.bytesRead > LIMIT) { state.truncated = true; publish(); return; }
        buffer += text; whole += text;
        let boundary;
        while ((boundary = buffer.indexOf("\n")) >= 0) {
          const line = buffer.slice(0, boundary).replace(/\r$/, ""); buffer = buffer.slice(boundary + 1);
          if (line.startsWith("data:")) eventData.push(line.slice(5).replace(/^ /, ""));
          else if (!line.trim() && eventData.length) { parse(eventData.join("\n")); eventData = []; }
          else if (/^\s*[\[{]/.test(line)) parse(line);
        }
        publish();
      },
      json(text) {
        if (!current(ticket) || typeof text !== "string") return;
        state.bytesRead = typeof TextEncoder === "function" ? new TextEncoder().encode(text).length : text.length;
        if (state.bytesRead > LIMIT) state.truncated = true; else parse(text);
        publish();
      },
      end() {
        if (finished || !current(ticket)) return;
        finished = true;
        if (buffer.startsWith("data:")) eventData.push(buffer.slice(5).replace(/^ /, ""));
        if (eventData.length) parse(eventData.join("\n"));
        parse(whole); // Also supports pretty JSON and unknown/missing Content-Type.
        buffer = ""; whole = ""; eventData = [];
        if (state.phase !== "http_error") state.phase = "response_ended";
        publish();
      }
    };
  }
  function fail(ticket, error) { if (current(ticket)) { state.phase = "transport_error"; state.error = error; publish(); } }
  async function observeFetch(response, ticket) {
    let copy; let reader;
    try {
      if (!current(ticket)) return;
      const capture = observer(ticket, "fetch"); capture.start(response.status);
      const type = response.headers?.get("content-type") || "";
      if (type && !/json|event-stream|text\/plain/i.test(type)) { capture.end(); return; }
      copy = response.clone(); // Clone synchronously, before the caller consumes it.
      if (!copy.body?.getReader) { capture.json(await copy.text()); capture.end(); return; }
      reader = copy.body.getReader(); const decoder = new TextDecoder(); let json = "";
      const jsonResponse = /json/i.test(type) && !/ndjson/i.test(type);
      while (true) {
        const part = await reader.read();
        if (!current(ticket)) { void reader.cancel().catch(() => {}); break; }
        const text = part.done ? decoder.decode() : decoder.decode(part.value, { stream: true });
        if (jsonResponse) {
          if (!part.done) state.bytesRead += part.value.byteLength;
          if (json.length + text.length <= LIMIT) json += text;
          else state.truncated = true;
          if (state.bytesRead > LIMIT) state.truncated = true;
        } else capture.feed(text);
        if (part.done) { if (jsonResponse && !state.truncated) capture.json(json); capture.end(); break; }
        if (state.truncated) { void reader.cancel().catch(() => {}); capture.end(); break; }
      }
    } catch { fail(ticket, "RESPONSE_READ_FAILED"); }
    finally { try { reader?.releaseLock(); } catch {} }
  }
  Object.defineProperty(globalThis, "__DOUBAO_DOLA_GENERATION_EVIDENCE__", { configurable: true, value: Object.freeze({
    get status() { syncPage(); return JSON.parse(JSON.stringify(state)); }, begin, noteRequest, observer, observeFetch, fail
  }) });
  document.addEventListener("DOMContentLoaded", publish, { once: true });
  if (typeof setInterval === "function") setInterval(syncPage, 1000);
  publish();
})();
