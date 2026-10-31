(function () {
  "use strict";
  const API = "__DOUBAO_DOLA_DEBUGGER_NETWORK__";
  if (globalThis[API]) return;
  const VERSION = "1.6.60";
  const STORAGE = "doubao_dola_debugger_session_v1";
  const MAX_RECORDS = 60;
  const MAX_EXPORT_BYTES = 2 * 1024 * 1024;
  const summary = globalThis.__DOUBAO_DOLA_NETWORK_SUMMARY__;
  const sessions = new Map();
  const pending = new Map();
  const pendingComparisons = new Map();
  let saveTimer = null;
  let saving = Promise.resolve();
  const clone = value => JSON.parse(JSON.stringify(value));
  function source(raw) { try { const url = new URL(raw); return /^https?:$/.test(url.protocol) ? /(^|\.)doubao\.com$/i.test(url.hostname) ? "doubao" : /(^|\.)dola\.com$/i.test(url.hostname) ? "dola" : "" : ""; } catch { return ""; } }
  async function tab(id) { try { return await chrome.tabs.get(id); } catch { return null; } }
  function session(id) {
    if (!sessions.has(id)) sessions.set(id, { tabId: id, source: "", enabled: null, attached: false, owned: false, phase: "idle", reason: "等待调试初始化",
      error: "", detachedReason: "", nextId: 0, records: [], inflight: new Map(), restored: false, requestCount: 0, submissionCount: 0, dropped: 0 });
    return sessions.get(id);
  }
  function status(s) {
    const submissions = s.records.filter(record => record.kind === "submission");
    const last = submissions.at(-1);
    return { version: VERSION, enabled: s.enabled === true, attached: s.attached, source: s.source,
      phase: s.phase, reason: s.reason, error: s.error, detachedReason: s.detachedReason, restored: s.restored,
      requestCount: s.requestCount, submissionCount: s.submissionCount, retainedRequests: s.records.length, dropped: s.dropped,
      lastSubmission: last ? clone(last) : null, submissionComparison: compareSubmissions(submissions.at(-2), last),
      verification: "UNVERIFIED", mode: "Network.readOnly" };
  }
  function compareSubmissions(previous, current) {
    if (!previous || !current) return null;
    const before = previous.request?.comparison; const after = current.request?.comparison;
    const equal = {};
    for (const key of ["prompt", "inputBox", "video", "ability", "option", "ext", "messageExt", "userContext"]) {
      const a = before?.hashes?.[key]; const b = after?.hashes?.[key];
      equal[key] = typeof a === "string" && typeof b === "string" ? a === b : null;
    }
    if (!before?.promptCoverageComplete || !after?.promptCoverageComplete) equal.prompt = null;
    const routeBefore = new Map((before?.routingFields || []).map(field => [field.path, field.value]));
    const routeAfter = new Map((after?.routingFields || []).map(field => [field.path, field.value]));
    const routingChanges = [...new Set([...routeBefore.keys(), ...routeAfter.keys()])].filter(key => routeBefore.get(key) !== routeAfter.get(key))
      .slice(0, 32).map(path => ({ path, before: routeBefore.get(path) ?? null, after: routeAfter.get(path) ?? null }));
    const idsBefore = new Map((before?.identifiers || []).map(field => [field.path, field.sha256]));
    const identifiers = (after?.identifiers || []).map(field => ({ path: field.path, equal: idsBefore.has(field.path) ? idsBefore.get(field.path) === field.sha256 : null }));
    let fieldDiff = null;
    if (before?.fieldDetails && after?.fieldDetails) {
      const left = new Map(before.fieldDetails.fields.map(field => [field.path, field]));
      const right = new Map(after.fieldDetails.fields.map(field => [field.path, field]));
      const paths = [...new Set([...left.keys(), ...right.keys()])].sort();
      const changes = [];
      for (const path of paths) {
        const a = left.get(path); const b = right.get(path);
        if (a?.type === b?.type && a?.sha256 === b?.sha256) continue;
        changes.push({ path, change: !a ? before.fieldDetails.complete ? "added" : "not_observed_before" :
          !b ? after.fieldDetails.complete ? "removed" : "not_observed_after" : "value_changed",
          category: b?.category || a?.category || "unknown", before: a || null, after: b || null });
      }
      fieldDiff = { complete: before.fieldDetails.complete && after.fieldDetails.complete,
        totalChanges: changes.length, truncated: changes.length > 64, changes: changes.slice(0, 64) };
    }
    return { previousId: previous.id, currentId: current.id, equal, routingChanges, identifiers, fieldDiff };
  }
  function persisted() {
    return [...sessions].map(([id, s]) => ({ id, source: s.source, enabled: s.enabled, attached: s.owned, phase: s.phase,
      error: s.error, reason: s.reason, detachedReason: s.detachedReason, nextId: s.nextId, requestCount: s.requestCount,
      submissionCount: s.submissionCount, dropped: s.dropped, records: s.records,
      inflight: [...s.inflight].map(([key, record]) => [key, record.id]) }));
  }
  function persist() {
    const value = clone(persisted());
    saving = saving.catch(() => {}).then(() => chrome.storage?.session?.set({ [STORAGE]: value })).catch(() => {});
    return saving;
  }
  function dirty() { if (saveTimer == null) saveTimer = setTimeout(() => { saveTimer = null; void persist(); }, 300); }
  function publish(s) {
    dirty();
    void chrome.tabs.sendMessage(s.tabId, { type: "DEBUGGER_STATUS", status: status(s) }).catch(() => {});
    void chrome.action.setBadgeText({ tabId: s.tabId, text: s.attached ? "DBG" : s.phase === "attach_error" ? "ERR" : "" }).catch(() => {});
  }
  async function command(id, method, params = {}) { return chrome.debugger.sendCommand({ tabId: id }, method, params); }
  async function release(s) {
    if (!s.owned) return;
    // Own-session commands cannot detach a DevTools or another extension's session.
    try { await chrome.debugger.detach({ tabId: s.tabId }); s.owned = false; s.attached = false; }
    catch (error) { s.error = summary.redacted(error?.message || error); s.phase = "detach_error"; s.reason = "Debugger 停止未确认，请使用浏览器调试提示停止"; }
  }
  async function attach(s) {
    if (s.attached || pending.has(s.tabId)) return pending.get(s.tabId);
    if (!chrome.debugger?.attach || !chrome.debugger?.sendCommand) {
      s.phase = "attach_error"; s.error = "DEBUGGER_API_UNAVAILABLE"; s.reason = "Debugger API 不可用，请重新加载含调试权限的扩展"; publish(s); return;
    }
    s.phase = "attaching"; s.error = ""; s.reason = "正在附加浏览器 Debugger"; publish(s);
    const operation = (async () => {
      let recovered = false;
      if (s.owned) {
        try { await command(s.tabId, "Network.enable"); recovered = true; } catch { s.owned = false; }
      }
      if (!recovered) {
        try { await chrome.debugger.attach({ tabId: s.tabId }, "1.3"); s.owned = true; }
        catch (error) { s.phase = "attach_error"; s.error = summary.redacted(error?.message || error); s.reason = "Debugger 附加失败：" + s.error; publish(s); return; }
        try { await command(s.tabId, "Network.enable"); }
        catch (error) {
          const failure = summary.redacted(error?.message || error); await release(s);
          s.phase = "attach_error"; s.error = failure; s.reason = "Debugger 网络观测启动失败：" + failure; publish(s); return;
        }
      }
      const current = await tab(s.tabId);
      if (!s.enabled || !current || source(current.url) !== s.source) { await release(s); publish(s); return; }
      s.attached = true; s.phase = "attached"; s.error = ""; s.detachedReason = "";
      s.reason = "Debugger 已附加，等待本页真实提交"; publish(s);
    })().finally(() => pending.delete(s.tabId));
    pending.set(s.tabId, operation); return operation;
  }
  async function activate(id, force = false) {
    const current = await tab(id); const site = source(current?.url);
    if (!site && !sessions.has(id)) return;
    const s = session(id);
    if (!site) {
      s.enabled = false; await pending.get(id); await release(s);
      if (!s.owned) { s.phase = "out_of_scope"; s.reason = "当前标签页不在豆包或 Dola，调试已停止"; }
      publish(s); return;
    }
    if (s.source && s.source !== site) {
      s.enabled = false; await pending.get(id); await release(s); s.records = []; s.inflight.clear(); s.nextId = 0;
      s.requestCount = 0; s.submissionCount = 0; s.dropped = 0; s.enabled = null; s.phase = "idle";
    }
    s.source = site;
    if (s.enabled == null) s.enabled = site === "doubao";
    if (!s.enabled) {
      await release(s);
      if (!s.owned) { s.phase = "disabled"; s.reason = "Debugger 已停止，可手动启用"; }
      publish(s); return;
    }
    if (!force && s.phase === "attach_error") { publish(s); return; }
    return attach(s);
  }
  async function setEnabled(id, enabled) {
    await ready;
    const current = await tab(id); if (!source(current?.url)) throw Error("仅支持当前豆包或 Dola 标签页");
    const s = session(id); s.source = source(current.url); s.enabled = enabled;
    if (enabled) { await pending.get(id); await activate(id, true); }
    else {
      s.phase = "disabled"; s.reason = "正在停止 Debugger";
      for (const record of s.inflight.values()) record.phase = "capture_interrupted";
      s.inflight.clear(); publish(s); await pending.get(id); await release(s);
      if (!s.owned) { s.phase = "disabled"; s.reason = "Debugger 已停止"; } publish(s);
    }
    await persist(); return status(s);
  }
  function currentRecord(s, key, record) { return s.enabled && s.attached && s.inflight.get(key) === record && s.records.includes(record); }
  function canKeepRequest(s, record) { return s.enabled && s.attached && s.records.includes(record) && record.phase !== "redirected"; }
  async function addComparison(s, record, body) {
    if (record.kind !== "submission" || typeof body !== "string") return;
    const job = (async () => {
      try {
        const value = await summary.compareRequest(body);
        if (canKeepRequest(s, record)) record.request.comparison = value;
      } catch (error) {
        if (canKeepRequest(s, record)) record.request.comparison = { error: summary.redacted(error?.message || error) };
      }
    })();
    pendingComparisons.set(record, job);
    try { await job; } finally { pendingComparisons.delete(record); }
  }
  function keep(s, key, record) {
    s.records.push(record); s.inflight.set(key, record);
    if (s.records.length > MAX_RECORDS) {
      let index = s.records.findIndex(item => item.kind === "other"); if (index < 0) index = 0;
      const [removed] = s.records.splice(index, 1); s.dropped += 1;
      for (const [id, value] of s.inflight) if (value === removed) s.inflight.delete(id);
    }
  }
  async function network(debuggee, method, params) {
    await ready;
    if (!Number.isInteger(debuggee?.tabId) || debuggee.sessionId || !params) return;
    const s = sessions.get(debuggee.tabId); if (!s?.attached || !s.enabled) return;
    const key = String(params.requestId || ""); if (!key) return;
    if (method === "Network.requestWillBeSent") {
      const prior = s.inflight.get(key);
      if (prior) { prior.phase = "redirected"; prior.redirectStatus = params.redirectResponse?.status ?? null; s.inflight.delete(key); }
      if (!["XHR", "Fetch", "EventSource"].includes(params.type)) return;
      const target = summary.endpoint(params.request?.url); if (!target) return;
      const request = params.request || {};
      let requestBody = request.postData;
      const record = { id: ++s.nextId, kind: target.kind, endpoint: target.endpoint, method: /^[A-Z]{1,12}$/.test(request.method || "") ? request.method : "",
        resourceType: params.type, signaturePresent: target.signaturePresent, phase: "request_sent", timestamp: Date.now(),
        status: null, receivedBytes: 0, request: summary.request(request.postData), response: null, error: "" };
      keep(s, key, record); s.requestCount += 1; if (record.kind === "submission") s.submissionCount += 1;
      if (typeof request.postData !== "string" && request.hasPostData) {
        try {
          const body = await command(s.tabId, "Network.getRequestPostData", { requestId: key });
          if (canKeepRequest(s, record)) { requestBody = body?.postData; record.request = summary.request(requestBody); }
        } catch (error) { if (s.enabled && s.attached && s.records.includes(record)) record.requestReadError = summary.redacted(error?.message || error); }
      }
      await addComparison(s, record, requestBody);
      publish(s); return;
    }
    const record = s.inflight.get(key); if (!record) return;
    if (method === "Network.responseReceived") {
      record.status = params.response?.status ?? null;
      record.mimeType = String(params.response?.mimeType || "").split(";", 1)[0].slice(0, 80);
      record.fromServiceWorker = params.response?.fromServiceWorker === true;
      record.phase = "response_streaming"; publish(s);
    } else if (method === "Network.dataReceived") {
      record.receivedBytes += Math.max(0, Number(params.dataLength) || 0); dirty();
    } else if (method === "Network.loadingFailed") {
      record.phase = "transport_error"; record.error = summary.redacted(params.errorText || "NETWORK_FAILED");
      record.cancelled = params.canceled === true; record.blockedReason = summary.redacted(params.blockedReason || ""); s.inflight.delete(key); publish(s);
    } else if (method === "Network.loadingFinished") {
      if (record.phase === "reading_response") return;
      record.phase = "reading_response"; record.encodedBytes = Math.max(0, Number(params.encodedDataLength) || 0); publish(s);
      const textual = !record.mimeType || /json|text\/|event-stream|javascript/i.test(record.mimeType);
      if (!textual || record.receivedBytes > summary.LIMIT || record.encodedBytes > summary.LIMIT) {
        record.phase = "response_ended"; record.responseSkipped = !textual ? "NON_TEXT_RESPONSE" : "BODY_LIMIT"; s.inflight.delete(key); publish(s); return;
      }
      try {
        const result = await command(s.tabId, "Network.getResponseBody", { requestId: key });
        if (!currentRecord(s, key, record)) return;
        if (typeof result?.body !== "string") throw Error("RESPONSE_BODY_MISSING");
        const body = result?.base64Encoded ? new TextDecoder().decode(Uint8Array.from(atob(result.body), value => value.charCodeAt(0))) : result?.body;
        record.response = summary.response(body, record.endpoint); record.phase = "response_ended";
      } catch (error) {
        if (!currentRecord(s, key, record)) return;
        record.phase = "response_unavailable"; record.error = summary.redacted(error?.message || error);
      }
      if (currentRecord(s, key, record)) { s.inflight.delete(key); publish(s); await persist(); }
    }
  }
  async function clear(id) { await ready; const s = session(id); s.records = []; s.inflight.clear(); s.requestCount = 0; s.submissionCount = 0; s.dropped = 0; publish(s); await persist(); return status(s); }
  async function report(id) {
    await ready; const s = session(id);
    await Promise.allSettled(s.records.map(record => pendingComparisons.get(record)).filter(Boolean));
    await persist(); return { ...status(s), capturedAt: new Date().toISOString(), scope: "current_tab_root_target", requests: clone(s.records) };
  }
  function encodeBase64Utf8(value) {
    const bytes = new TextEncoder().encode(value);
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
    }
    return btoa(binary);
  }
  async function exportReport(id, localDiagnostics) {
    if (!chrome.downloads?.download) throw Error("DOWNLOADS_API_UNAVAILABLE");
    const debuggerNetwork = await report(id);
    const local = localDiagnostics && typeof localDiagnostics === "object" ? clone(localDiagnostics) : {};
    const diagnostic = { ...local, debuggerNetwork };
    let json = JSON.stringify(diagnostic, null, 2);
    let droppedForExport = 0;
    // Defend the browser against an unexpectedly huge diagnostic. Keep the
    // newest request evidence and make truncation explicit in the file.
    while (new TextEncoder().encode(json).length > MAX_EXPORT_BYTES && debuggerNetwork.requests.length > 1) {
      debuggerNetwork.requests.shift(); droppedForExport += 1;
      debuggerNetwork.exportTruncated = true;
      debuggerNetwork.exportDroppedRequests = droppedForExport;
      json = JSON.stringify(diagnostic, null, 2);
    }
    const bytes = new TextEncoder().encode(json).length;
    if (bytes > MAX_EXPORT_BYTES) throw Error("DEBUG_REPORT_TOO_LARGE");
    const filename = `${debuggerNetwork.source || "video"}-debugger-v1.6.60-${Date.now()}.json`;
    const downloadId = await chrome.downloads.download({
      url: `data:application/json;charset=utf-8;base64,${encodeBase64Utf8(json)}`,
      filename,
      saveAs: false,
      conflictAction: "uniquify"
    });
    if (!Number.isInteger(downloadId)) throw Error("DEBUG_REPORT_DOWNLOAD_NOT_CREATED");
    return { ...status(session(id)), exportDownloadId: downloadId, exportBytes: bytes, exportFilename: filename };
  }
  chrome.debugger?.onEvent?.addListener((debuggee, method, params) => { void network(debuggee, method, params).catch(() => {}); });
  chrome.debugger?.onDetach?.addListener((debuggee, reason) => {
    const s = sessions.get(debuggee?.tabId); if (!s) return;
    s.owned = false; s.attached = false; s.enabled = false; s.phase = "detached"; s.detachedReason = summary.redacted(reason || "unknown");
    s.reason = "Debugger 已断开：" + s.detachedReason + "；需要时手动重新启用";
    for (const record of s.inflight.values()) if (!["response_ended", "transport_error"].includes(record.phase)) record.phase = "capture_interrupted";
    s.inflight.clear(); publish(s);
  });
  chrome.tabs.onRemoved.addListener(id => {
    const s = sessions.get(id); if (s) { s.attached = false; s.owned = false; s.inflight.clear(); }
    sessions.delete(id); dirty();
  });
  chrome.tabs.onUpdated.addListener((id, changes) => { if (changes.url || changes.status === "complete") void ready.then(() => activate(id)).catch(() => {}); });
  chrome.tabs.onActivated.addListener(({ tabId }) => { void ready.then(() => activate(tabId)).catch(() => {}); });
  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (!["DEBUGGER_CONTROL", "DEBUGGER_STATUS", "DEBUGGER_REPORT", "DEBUGGER_EXPORT", "DEBUGGER_CLEAR"].includes(message?.type)) return;
    if (!Number.isInteger(sender.tab?.id) || sender.id && sender.id !== chrome.runtime.id) { sendResponse({ ok: false, error: "无有效当前标签页" }); return; }
    const run = async () => {
      await ready;
      if (!source((await tab(sender.tab.id))?.url)) throw Error("仅支持当前豆包或 Dola 标签页");
      if (message.type === "DEBUGGER_CONTROL") { if (typeof message.enabled !== "boolean") throw Error("无效调试开关"); return setEnabled(sender.tab.id, message.enabled); }
      if (message.type === "DEBUGGER_CLEAR") return clear(sender.tab.id);
      if (message.type === "DEBUGGER_EXPORT") return exportReport(sender.tab.id, message.localDiagnostics);
      if (message.type === "DEBUGGER_REPORT") return report(sender.tab.id);
      return status(session(sender.tab.id));
    };
    run().then(value => sendResponse({ ok: true, ...value }), error => sendResponse({ ok: false, error: summary.redacted(error?.message || error) })); return true;
  });
  const ready = (async () => {
    try {
      const saved = (await chrome.storage?.session?.get(STORAGE))?.[STORAGE];
      if (Array.isArray(saved)) for (const entry of saved) {
        if (!Number.isInteger(entry?.id)) continue;
        const s = session(entry.id);
        Object.assign(s, { source: entry.source || "", enabled: typeof entry.enabled === "boolean" ? entry.enabled : null,
          owned: entry.attached === true, restored: true, phase: entry.phase === "attach_error" ? "attach_error" : "idle",
          error: entry.error || "", reason: entry.reason || "", detachedReason: entry.detachedReason || "",
          nextId: entry.nextId || 0, requestCount: entry.requestCount || 0, submissionCount: entry.submissionCount || 0,
          dropped: entry.dropped || 0, records: Array.isArray(entry.records) ? entry.records.slice(-MAX_RECORDS) : [] });
        for (const [key, id] of entry.inflight || []) { const record = s.records.find(item => item.id === id); if (record) s.inflight.set(key, record); }
      }
    } catch {}
    const tabs = await chrome.tabs.query({}); const open = new Set(tabs.map(value => value.id));
    for (const id of sessions.keys()) if (!open.has(id)) sessions.delete(id);
    await Promise.allSettled(tabs.filter(value => source(value.url) || sessions.has(value.id)).map(value => activate(value.id)));
  })();
  Object.defineProperty(globalThis, API, { value: Object.freeze({ ready, activate, setEnabled, clear, report, exportReport }) });
})();
