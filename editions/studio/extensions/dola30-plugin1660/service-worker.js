const SOURCE_HOSTS = Object.freeze({
  doubao: "doubao.com",
  dola: "dola.com",
  qianwen: "qianwen.com",
  yunque: "jianying.com"
});
// 【v1.3.2 修复】原 FALLBACK_API_ROOT_HOSTS 域名白名单已移除。
// 主程序 LocalVideoResolver.BuildRequestUri 只校验「是合法 http/https 绝对 URL」，**没有任何域名限制**；
// 插件却把 doubao 限死在 doubao.com / byteintlapi.com、dola 限死在 dola.com / byteintlapi.com，
// 平台一旦把 fallback_api 指向其它域（CDN 等），URL 会在 addFallbackApi 里被**静默丢弃**，
// 表现就是「面板一直显示 未提取到资源」。
// 同时原实现强制 https，而主程序允许 http + https —— 这里一并放开，与主程序保持一致。
const MAX_FALLBACK_API_COUNT = 8;
const FALLBACK_API_TIMEOUT_MS = 8000;
const FALLBACK_VIDEO_CACHE_TTL_MS = 10 * 60 * 1000;
const FALLBACK_VIDEO_CACHE_MAX = 200;
const MARKED_VIDEO_HEADER_BYTES = 256 * 1024;
const DOLA_UNWATERMARKED_API = "https://www.dola.com/creativity/resource/get_without_watermark";
const QAAB_SALT_HEX = "4dd4c2e6b83162090e52b3c7a6733ba4"
  + "1cb2462b829ab58a196b39db57177524"
  + "f49baf7f08e8d68d26a72e37c1a95a2f"
  + "1f05a51892aef2949732b62a38aadd58";

const tabNavigationGenerations = new Map();
const panelInitializationByTab = new Map();
const fallbackVideoCache = new Map();
const downloadRequests = new Map();

async function dispatchTrustedClick(tabId, x, y) {
  if (!Number.isInteger(tabId)) throw new Error("无有效标签页");
  const point = { x: Number(x), y: Number(y) };
  if (!Number.isFinite(point.x) || !Number.isFinite(point.y)) throw new Error("可信点击坐标无效");
  const target = { tabId };
  let attachedHere = false;
  try {
    try {
      await chrome.debugger.sendCommand(target, "Runtime.evaluate", { expression: "void 0" });
    } catch {
      await chrome.debugger.attach(target, "1.3");
      attachedHere = true;
    }
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mouseMoved", x: point.x, y: point.y, button: "none", buttons: 0
    });
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mousePressed", x: point.x, y: point.y, button: "left", buttons: 1, clickCount: 1
    });
    await chrome.debugger.sendCommand(target, "Input.dispatchMouseEvent", {
      type: "mouseReleased", x: point.x, y: point.y, button: "left", buttons: 0, clickCount: 1
    });
    return { ok: true, attachedHere };
  } finally {
    if (attachedHere) {
      try { await chrome.debugger.detach(target); } catch { /* The tab may have navigated after the click. */ }
    }
  }
}

chrome.runtime.onInstalled.addListener(attachExistingTabs);
chrome.runtime.onStartup.addListener(attachExistingTabs);
attachExistingTabs().catch((error) => {
  console.warn("initial tab activation failed:", error.message || error);
});

chrome.tabs.onActivated.addListener(async ({ tabId }) => {
  const tab = await safeGetTab(tabId);
  if (tab && shouldAttachToTab(tab.url)) {
    activateTab(tabId);
  }
});

chrome.tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
  if (changeInfo.url) {
    const generation = (tabNavigationGenerations.get(tabId) || 0) + 1;
    tabNavigationGenerations.set(tabId, generation);
    const source = getSourceFromUrl(changeInfo.url);
    if (source) {
      sendToTab(tabId, {
        type: "MEDIA_RESET",
        sourceKey: `${conversationKeyFromUrl(changeInfo.url)}:${generation}`
      });
    }
  }
  const url = changeInfo.url || tab.url;
  if (shouldAttachToTab(url)) {
    activateTab(tabId);
  }
});

chrome.tabs.onRemoved.addListener((tabId) => {
  tabNavigationGenerations.delete(tabId);
  panelInitializationByTab.delete(tabId);
});

chrome.action.onClicked.addListener((tab) => {
  if (tab && tab.id && shouldAttachToTab(tab.url)) {
    // Network debugger lifecycle is managed by debugger-network.js.
    activateTab(tab.id);
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message) {
    return;
  }

  if (message.type === "DOLA_TRUSTED_CLICK" && sender.tab?.id) {
    dispatchTrustedClick(sender.tab.id, message.x, message.y).then(sendResponse).catch((error) => {
      sendResponse({ ok: false, error: error?.message || String(error) });
    });
    return true;
  }

  if (message.type === "RESOLVE_FALLBACK_APIS" && sender.tab?.id && Array.isArray(message.urls)) {
    resolveFallbackApisForTab(sender.tab.id, sender.tab.url || "", message.urls, message.resolution, message.force === true, message.context).then((report) => {
      sendResponse({ ok: true, ...report });
    }).catch(async (error) => {
      console.warn("passive media resolve failed:", error.message || error);
      await sendToTab(sender.tab.id, {
        type: "MEDIA_STATUS",
        sourceKey: mediaSourceKey(sender.tab.url || "", sender.tab.id, message.context),
        text: "无水印资源解析失败"
      }).catch(() => {});
      sendResponse({ ok: false, error: error.message || String(error) });
    });
    // Manifest V3 必须保持消息通道，避免 Service Worker 在异步解析完成前被挂起。
    return true;
  }

  if (message.type === "RESOLVE_VIDEO_MODELS" && sender.tab?.id && Array.isArray(message.models)) {
    resolveVideoModelsForTab(sender.tab.id, sender.tab.url || "", message.models, message.resolution, message.context).then((report) => {
      sendResponse({ ok: true, ...report });
    }).catch((error) => {
      console.warn("video_model resolve failed:", error.message || error);
      sendResponse({ ok: false, error: error.message || String(error) });
    });
    return true;
  }

  if (message.type === "RESOLVE_MARKED_VIDEO_URLS" && sender.tab?.id && Array.isArray(message.urls)) {
    resolveMarkedVideoUrlsForTab(sender.tab.id, sender.tab.url || "", message.urls, message.resolution, message.context).then((report) => {
      sendResponse({ ok: true, ...report });
    }).catch((error) => {
      console.warn("marked Dola video resolve failed:", error.message || error);
      sendResponse({ ok: false, error: error.message || String(error) });
    });
    return true;
  }

  if (message.type === "DOWNLOAD_STATUS" && Number.isInteger(message.id)) {
    chrome.downloads.search({ id: message.id }).then(([item]) => {
      sendResponse(item?.byExtensionId === chrome.runtime.id
        ? { ok: true, state: item.state, filename: item.filename, error: item.error || "" }
        : { ok: false, error: "未找到此浏览器实例的下载记录" });
    }).catch((error) => sendResponse({ ok: false, error: error.message || String(error) }));
    return true;
  }

  if (message.type === "DOWNLOAD_LOOKUP" && isHttpUrl(message.url) && Number.isFinite(message.startedAt)) {
    const request = downloadRequests.get(message.requestId);
    if (request?.error) {
      sendResponse({ ok: false, error: request.error });
      return;
    }
    if (Number.isInteger(request?.id)) {
      sendResponse({ ok: true, id: request.id });
      return;
    }
    // A restarted MV3 worker loses the in-memory request. Recover from Chrome's
    // download history; redirects can change finalUrl but not the original url.
    chrome.downloads.search({ orderBy: ["-startTime"], limit: 40 }).then((matches) => {
      const item = matches.find((candidate) =>
        candidate.byExtensionId === chrome.runtime.id &&
        Date.parse(candidate.startTime || "") >= message.startedAt - 5000 &&
        (candidate.url === message.url || candidate.finalUrl === message.url)
      );
      sendResponse({ ok: true, id: item?.id ?? null });
    }).catch((error) => sendResponse({ ok: false, error: error.message || String(error) }));
    return true;
  }

  if (message.type === "REVEAL_DOWNLOAD" && Number.isInteger(message.id)) {
    chrome.downloads.search({ id: message.id }).then(([item]) => {
      if (item?.byExtensionId !== chrome.runtime.id || item.state !== "complete") {
        sendResponse({ ok: false, error: "下载尚未完成或记录不在当前浏览器中" });
        return;
      }
      chrome.downloads.show(message.id);
      sendResponse({ ok: true });
    }).catch((error) => sendResponse({ ok: false, error: error.message || String(error) }));
    return true;
  }

  if (message.type !== "DOWNLOAD_MEDIA") return;
  if (!isHttpUrl(message.url)) {
    sendResponse({ ok: false, error: "视频链接无效" });
    return;
  }

  const saveAs = message.saveAs === true;
  try {
    const startedAt = Date.now();
    for (const [key, request] of downloadRequests) {
      if (startedAt - request.startedAt > 10 * 60 * 1000) downloadRequests.delete(key);
    }
    const requestId = `${startedAt}-${Math.random().toString(36).slice(2)}`;
    downloadRequests.set(requestId, { startedAt, id: null, error: "" });
    const pending = chrome.downloads.download({
      url: message.url,
      filename: buildDownloadFilename("", message.mediaType),
      conflictAction: "uniquify",
      saveAs
    });
    // The save dialog and some media hosts resolve downloads.download slowly.
    // A synchronous ACK avoids keeping a fragile runtime message port open.
    sendResponse({ ok: true, pending: true, startedAt, requestId });
    Promise.resolve(pending).then((id) => {
      const request = downloadRequests.get(requestId);
      if (request) request.id = id;
    }).catch((error) => {
      console.warn("download failed:", error);
      const request = downloadRequests.get(requestId);
      if (request) request.error = error.message || String(error);
    });
    return;
  } catch (error) {
    sendResponse({ ok: false, error: error.message || String(error) });
  }
});

async function resolveFallbackApisForTab(tabId, tabUrl, candidates, requestedResolution = "auto", force = false, rawContext = null) {
  const source = getSourceFromUrl(tabUrl) || "dola";
  const generation = tabNavigationGenerations.get(tabId) || 0;
  const context = sanitizeMediaContext(rawContext, tabUrl);
  const sourceKey = mediaSourceKey(tabUrl, tabId, context);
  const preference = ["480p", "720p", "1080p"].includes(requestedResolution) ? requestedResolution : "auto";
  const urls = Array.from(new Set(candidates.filter(isAllowedFallbackApiUrl))).slice(0, MAX_FALLBACK_API_COUNT);
  const resolved = await Promise.all(urls.map((url) => getCachedUnwatermarkedVideoResource(url, source, preference, force)));
  const items = [];
  const seenUrls = new Set();
  for (const resource of resolved) {
    if (resource?.url) addItem(items, seenUrls, "video", resource.url, resource);
  }
  const failures = resolved.filter((resource) => !resource?.url).map((resource) => resource?.error || "NO_VIDEO_URL");
  // Only quality metadata goes to diagnostics; never expose signed media URLs.
  const qualities = resolved.map((resource) => ({
    requested: resource?.requestedResolution || preference,
    selected: resource?.resolution || "",
    width: resource?.width || 0,
    height: resource?.height || 0,
    variants: resource?.variants || []
  }));
  const currentTab = await safeGetTab(tabId);
  if ((tabNavigationGenerations.get(tabId) || 0) !== generation || !currentTab || getSourceFromUrl(currentTab.url) !== source ||
      conversationKeyFromUrl(currentTab.url) !== context.conversationKey) {
    return { count: 0, candidateCount: urls.length, failures: ["PAGE_CHANGED"], qualities: [] };
  }
  await sendMediaResult(tabId, sourceKey, items, context);
  return { count: items.length, candidateCount: urls.length, failures, qualities };
}

async function resolveVideoModelsForTab(tabId, tabUrl, candidates, requestedResolution = "auto", rawContext = null) {
  const source = getSourceFromUrl(tabUrl) || "dola";
  const generation = tabNavigationGenerations.get(tabId) || 0;
  const context = sanitizeMediaContext(rawContext, tabUrl);
  const sourceKey = mediaSourceKey(tabUrl, tabId, context);
  const preference = ["480p", "720p", "1080p"].includes(requestedResolution) ? requestedResolution : "auto";
  const models = [...new Set(candidates.filter((value) => typeof value === "string" && value.length <= 500_000))].slice(0, MAX_FALLBACK_API_COUNT);
  const resolved = [];
  for (const encoded of models) {
    let payload;
    try { payload = JSON.parse(encoded); }
    catch { resolved.push({ url: "", error: "VIDEO_MODEL_JSON_INVALID", variants: [] }); continue; }
    const data = getVideoData(payload);
    const variants = summarizeVideoQualities(data);
    const ranked = rankVideoCandidates(data);
    let resource = null;
    let missingSeed = false;
    let markedOnly = false;
    const payloadSeed = findKeySeedDeep(payload);
    for (const candidate of ranked) {
      const keySeed = candidate.keySeed || payloadSeed;
      if (candidate.token.startsWith("qAAB") && !keySeed) { missingSeed = true; continue; }
      const url = await decodeMainUrl(candidate.token, keySeed);
      if (!url) continue;
      // video_model is only a transport container.  Dola can put the normal
      // watermarked playback rendition in it, so never infer "original" from
      // the field name alone.  The fallback API resolver below is the path
      // that explicitly requests logo_type=unwatermarked.
      if (isMarkedDolaVideoUrl(url)) { markedOnly = true; continue; }
      resource = { type: "video", url, width: candidate.width, height: candidate.height,
        definition: candidate.definition, resolution: candidate.resolution,
        resolutionEvidence: candidate.width && candidate.height ? "dimensions" : candidate.resolution ? "definition" : "unknown",
        bitrate: candidate.bitrate, watermark: "unmarked", provenance: "video_model",
        requestedResolution: preference, variants, resourceKey: `${source}:video-model:${mediaFingerprint(url)}` };
      break;
    }
    resolved.push(resource || { url: "", error: markedOnly ? "VIDEO_MODEL_IS_WATERMARKED" :
      missingSeed ? "KEY_SEED_MISSING" : "URL_DECODE_FAILED", variants });
  }
  const items = [];
  const seenUrls = new Set();
  for (const resource of resolved) if (resource?.url) addItem(items, seenUrls, "video", resource.url, resource);
  const failures = resolved.filter((resource) => !resource?.url).map((resource) => resource?.error || "NO_VIDEO_URL");
  const qualities = resolved.map((resource) => ({ requested: resource?.requestedResolution || preference,
    selected: resource?.resolution || "", width: resource?.width || 0, height: resource?.height || 0,
    variants: resource?.variants || [] }));
  const currentTab = await safeGetTab(tabId);
  if ((tabNavigationGenerations.get(tabId) || 0) !== generation || !currentTab || getSourceFromUrl(currentTab.url) !== source ||
      conversationKeyFromUrl(currentTab.url) !== context.conversationKey) {
    return { count: 0, candidateCount: models.length, failures: ["PAGE_CHANGED"], qualities: [] };
  }
  await sendMediaResult(tabId, sourceKey, items, context);
  return { count: items.length, candidateCount: models.length, failures, qualities };
}

async function resolveMarkedVideoUrlsForTab(tabId, tabUrl, candidates, requestedResolution = "auto", rawContext = null) {
  const source = getSourceFromUrl(tabUrl);
  const context = sanitizeMediaContext(rawContext, tabUrl);
  const sourceKey = mediaSourceKey(tabUrl, tabId, context);
  const generation = tabNavigationGenerations.get(tabId) || 0;
  const preference = ["480p", "720p", "1080p"].includes(requestedResolution) ? requestedResolution : "auto";
  const urls = [...new Set(candidates.filter(isMarkedDolaVideoUrl))].slice(0, MAX_FALLBACK_API_COUNT);
  if (source !== "dola" || !urls.length) {
    return { count: 0, candidateCount: urls.length, failures: [source === "dola" ? "NO_MARKED_VIDEO_URL" : "UNSUPPORTED_SOURCE"], qualities: [] };
  }

  const extracted = await Promise.all(urls.map(extractEmbeddedDolaVideoId));
  const vids = [...new Set(extracted.filter(Boolean))];
  let items = [];
  let failures = [];
  if (!vids.length) {
    failures = ["EMBEDDED_VIDEO_ID_NOT_FOUND"];
  } else {
    const response = await requestOfficialDolaUnwatermarkedResources(vids);
    if (response.error) {
      failures = [response.error];
    } else if (response.data?.without_watermark !== true) {
      failures = ["PLATFORM_NO_UNWATERMARKED_RESOURCE"];
    } else {
      items = await decodeOfficialDolaUnwatermarkedResources(response.data, vids, preference);
      if (!items.length) failures = ["UNWATERMARKED_RESOURCE_MISSING"];
    }
  }

  const currentTab = await safeGetTab(tabId);
  if ((tabNavigationGenerations.get(tabId) || 0) !== generation || !currentTab ||
      getSourceFromUrl(currentTab.url) !== "dola" || conversationKeyFromUrl(currentTab.url) !== context.conversationKey) {
    return { count: 0, candidateCount: urls.length, videoIdCount: vids.length, failures: ["PAGE_CHANGED"], qualities: [] };
  }
  if (items.length) await sendMediaResult(tabId, sourceKey, items, context);
  return { count: items.length, candidateCount: urls.length, videoIdCount: vids.length, failures,
    qualities: items.map((item) => ({ requested: preference, selected: item.resolution || "",
      width: item.width || 0, height: item.height || 0, variants: item.variants || [] })) };
}

function isMarkedDolaVideoUrl(rawUrl) {
  try {
    const url = new URL(rawUrl);
    const rendition = (url.searchParams.get("lr") || "").toLowerCase();
    const marked = /(?:^|_)watermark(?:_|$)/.test(rendition) &&
      !/(?:^|_)(?:no|without)_?watermark(?:_|$)|unwatermarked/.test(rendition);
    return /^https?:$/.test(url.protocol) && /^v\d+-dola\.dola\.com$/i.test(url.hostname) &&
      url.pathname.includes("/video/") && marked;
  } catch {
    return false;
  }
}

async function extractEmbeddedDolaVideoId(rawUrl) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FALLBACK_API_TIMEOUT_MS);
  try {
    const response = await fetch(rawUrl, {
      method: "GET",
      headers: { Range: `bytes=0-${MARKED_VIDEO_HEADER_BYTES - 1}` },
      credentials: "omit",
      cache: "no-store",
      signal: controller.signal
    });
    if (!response.ok) return "";
    const bytes = new Uint8Array(await response.arrayBuffer()).subarray(0, MARKED_VIDEO_HEADER_BYTES);
    const text = new TextDecoder("latin1").decode(bytes);
    return text.match(/vid:(v[0-9a-z]{20,})/i)?.[1] || "";
  } catch {
    return "";
  } finally {
    clearTimeout(timer);
  }
}

async function requestOfficialDolaUnwatermarkedResources(vids) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FALLBACK_API_TIMEOUT_MS);
  try {
    const response = await fetch(DOLA_UNWATERMARKED_API, {
      method: "POST",
      credentials: "include",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ vid: vids }),
      signal: controller.signal
    });
    if (!response.ok) return { error: `HTTP_${response.status}` };
    const payload = await response.json();
    if (Number(payload?.code || 0) !== 0) return { error: `DOLA_API_${payload.code}` };
    return { data: payload?.data || {} };
  } catch (error) {
    return { error: error?.name === "AbortError" ? "TIMEOUT" : "DOLA_UNWATERMARKED_REQUEST_FAILED" };
  } finally {
    clearTimeout(timer);
  }
}

async function decodeOfficialDolaUnwatermarkedResources(data, vids, preference) {
  const root = data?.download_video;
  const entries = [];
  if (Array.isArray(root)) entries.push(...root);
  else if (root && typeof root === "object") {
    for (const vid of vids) if (root[vid] != null) entries.push(root[vid]);
    if (!entries.length) entries.push(...Object.values(root));
  } else if (root != null) entries.push(root);

  const output = [];
  const seen = new Set();
  for (const entry of entries) {
    const model = entry && typeof entry === "object" ? (entry.video_model ?? entry.videoModel) : null;
    if (model != null) {
      let payload = model;
      if (typeof payload === "string") {
        try { payload = JSON.parse(payload); } catch { payload = null; }
      }
      if (payload && typeof payload === "object") {
        const dataNode = getVideoData(payload);
        const variants = summarizeVideoQualities(dataNode);
        const payloadSeed = findKeySeedDeep(payload);
        for (const candidate of rankVideoCandidates(dataNode)) {
          const keySeed = candidate.keySeed || payloadSeed;
          if (candidate.token.startsWith("qAAB") && !keySeed) continue;
          const url = await decodeMainUrl(candidate.token, keySeed);
          if (!url || isMarkedDolaVideoUrl(url) || seen.has(mediaFingerprint(url))) continue;
          seen.add(mediaFingerprint(url));
          output.push({ type: "video", url, width: candidate.width, height: candidate.height,
            definition: candidate.definition, resolution: candidate.resolution,
            resolutionEvidence: candidate.width && candidate.height ? "dimensions" : candidate.resolution ? "definition" : "unknown",
            bitrate: candidate.bitrate, watermark: "unmarked", provenance: "dola_unwatermarked_api",
            requestedResolution: preference, variants,
            resourceKey: `dola:unwatermarked:${mediaFingerprint(url)}` });
          break;
        }
      }
    }
    const url = typeof entry === "string" ? entry : entry?.download_url || entry?.downloadUrl || "";
    if (isHttpUrl(url) && !isMarkedDolaVideoUrl(url) && !seen.has(mediaFingerprint(url))) {
      seen.add(mediaFingerprint(url));
      output.push({ type: "video", url, width: Number(entry?.width || 0), height: Number(entry?.height || 0),
        resolution: String(entry?.resolution || ""), watermark: "unmarked", provenance: "dola_unwatermarked_api",
        requestedResolution: preference, variants: [], resourceKey: `dola:unwatermarked:${mediaFingerprint(url)}` });
    }
  }
  return output;
}

chrome.downloads.onDeterminingFilename.addListener((item, suggest) => {
  try {
    const filename = item.filename || "";
    const mime = (item.mime || "").toLowerCase();
    const url = (item.url || "").toLowerCase();

    // 本扩展发起的下载始终使用完整的品牌文件名和媒体扩展名。
    // byExtensionId 可避免误改用户在网页上正常下载的其它文件。
    if (item.byExtensionId === chrome.runtime.id) {
      const mediaType = mime.startsWith("image/") ? "image" : "video";
      suggest({
        filename: buildDownloadFilename(item.url || "", mediaType, mime),
        conflictAction: "uniquify"
      });
      return;
    }

    const match = filename.match(/\.([a-zA-Z0-9]{2,5})$/);
    const currentExt = match ? match[1].toLowerCase() : "";
    const validVideoExts = ["mp4", "m4v", "mov", "webm", "mkv", "avi"];
    const validImageExts = ["png", "jpg", "jpeg", "webp", "gif", "avif", "bmp"];
    const hasValidExt = validVideoExts.includes(currentExt) || validImageExts.includes(currentExt);

    if (!hasValidExt || !currentExt) {
      let isVideo = false;
      let targetExt = ".mp4";

      if (mime.startsWith("video/") || validVideoExts.some((e) => url.includes("." + e)) || url.includes("video") || (item.fileSize && item.fileSize > 1.5 * 1024 * 1024)) {
        isVideo = true;
        targetExt = ".mp4";
        if (mime.includes("webm") || url.includes(".webm")) targetExt = ".webm";
        else if (mime.includes("quicktime") || url.includes(".mov")) targetExt = ".mov";
      } else if (mime.startsWith("image/") || validImageExts.some((e) => url.includes("." + e)) || url.includes("image")) {
        isVideo = false;
        targetExt = ".jpg";
        if (mime.includes("png") || url.includes(".png")) targetExt = ".png";
        else if (mime.includes("webp") || url.includes(".webp")) targetExt = ".webp";
      } else {
        isVideo = true;
        targetExt = ".mp4";
      }

      const now = new Date();
      const pad = (n) => String(n).padStart(2, "0");
      const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;

      let base = filename.replace(/\.[a-zA-Z0-9]{2,5}$/, "").replace(/[<>:\"/\\|?*\x00-\x1F]/g, "_").trim();
      if (!base || base === "download" || base === "videoplayback" || base.startsWith("blob")) {
        base = `苏生阁${isVideo ? "视频" : "图片"}_${stamp}`;
      } else if (!base.includes("苏生阁") && !base.includes("Dola")) {
        base = `苏生阁_${base}_${stamp}`;
      }

      suggest({ filename: `${base}${targetExt}`, conflictAction: "uniquify" });
      return;
    }
  } catch (err) {
    console.warn("[苏生阁] onDeterminingFilename error:", err);
  }
  suggest();
});

async function attachExistingTabs() {
  const tabs = await chrome.tabs.query({});
  for (const tab of tabs) {
    if (tab.id && shouldAttachToTab(tab.url)) {
      activateTab(tab.id);
    }
  }
}

async function activateTab(tabId) {
  await ensurePanel(tabId);
}

async function ensurePanel(tabId) {
  if (panelInitializationByTab.has(tabId)) {
    return panelInitializationByTab.get(tabId);
  }

  const initialization = initializePanel(tabId).finally(() => {
    panelInitializationByTab.delete(tabId);
  });
  panelInitializationByTab.set(tabId, initialization);
  return initialization;
}

async function initializePanel(tabId) {
  try {
    const response = await chrome.tabs.sendMessage(tabId, { type: "PANEL_PING" });
    if (response?.ready) {
      return;
    }
  } catch {
    // The content script is absent, stale after an extension reload, or not ready yet.
  }

  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        delete globalThis.__watermarkFreeMediaPanelActive;
      }
    });
    await chrome.scripting.executeScript({
      target: { tabId },
      files: ["direct-video-utils.js", "content-panel.js"]
    });
    const response = await chrome.tabs.sendMessage(tabId, { type: "PANEL_PING" });
    if (!response?.ready) {
      throw new Error("content panel did not acknowledge initialization");
    }
  } catch (error) {
    console.warn("panel injection failed:", error.message || error);
  }
}

async function safeGetTab(tabId) {
  try {
    return await chrome.tabs.get(tabId);
  } catch {
    return null;
  }
}

function shouldAttachToTab(url) {
  return Boolean(getSourceFromUrl(url));
}

function getSourceFromUrl(rawUrl) {
  if (typeof rawUrl !== "string") {
    return "";
  }

  try {
    const parsed = new URL(rawUrl);
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return "";
    }

    const hostname = parsed.hostname.toLowerCase().replace(/\.$/, "");
    for (const [source, rootHost] of Object.entries(SOURCE_HOSTS)) {
      if (hostname === rootHost || hostname.endsWith(`.${rootHost}`)) {
        return source;
      }
    }
  } catch {
    return "";
  }

  return "";
}

function conversationKeyFromUrl(rawUrl) {
  try {
    const parsed = new URL(rawUrl);
    const match = parsed.pathname.match(/^\/chat(?:\/([^/?#]+))?/i);
    return `${getSourceFromUrl(rawUrl) || "site"}:${match?.[1] || "new"}`;
  } catch {
    return "site:new";
  }
}

function sanitizeMediaContext(rawContext, tabUrl) {
  const fallbackKey = conversationKeyFromUrl(tabUrl);
  const suppliedKey = typeof rawContext?.conversationKey === "string" ? rawContext.conversationKey.slice(0, 180) : "";
  const conversationKey = suppliedKey === fallbackKey ? suppliedKey : fallbackKey;
  const title = typeof rawContext?.title === "string"
    ? rawContext.title.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\s+/g, " ").trim().slice(0, 80) : "";
  const capturedAt = Number.isFinite(Number(rawContext?.capturedAt)) ? Number(rawContext.capturedAt) : Date.now();
  return { conversationKey, conversationId: conversationKey.split(":").slice(1).join(":"), title, capturedAt };
}

function mediaSourceKey(tabUrl, tabId, rawContext = null) {
  const context = sanitizeMediaContext(rawContext, tabUrl);
  return `${context.conversationKey}:${tabNavigationGenerations.get(tabId) || 0}`;
}

async function sendMediaResult(tabId, sourceKey, items, rawContext = null) {
  const context = rawContext && typeof rawContext.conversationKey === "string"
    ? { conversationKey: rawContext.conversationKey, conversationId: rawContext.conversationId || "",
      title: rawContext.title || "", capturedAt: Number(rawContext.capturedAt) || Date.now() }
    : sanitizeMediaContext(null, "");
  const ordered = items.map((item, index) => ({ ...item, ...context, captureOrder: index + 1 }));
  if (items.length) {
    await sendToTab(tabId, {
      type: "MEDIA_FOUND",
      sourceKey,
      context,
      items: ordered
    });
  } else {
    await sendToTab(tabId, {
      type: "MEDIA_STATUS",
      sourceKey,
      text: "未提取到资源"
    });
  }
}

async function extractUnwatermarkedItems(json, rawBody, source) {
  const items = [];
  const seenUrls = new Set();

  // 图片：json 可用时走结构化遍历；同时对原始 body 做正则兜底。
  // 豆包的 chain 响应不是单个合法 JSON（json 为 null），此时只能靠正则捞 image_ori_raw.url。
  const imageUrlsFromJson = findImageOriRawUrls(json);
  const imageUrlsFromBody = findImageOriRawUrlsFromBody(rawBody);
  for (const url of imageUrlsFromJson) {
    addItem(items, seenUrls, "image", url);
  }
  for (const url of imageUrlsFromBody) {
    addItem(items, seenUrls, "image", url);
  }

  // 【v1.3.2 诊断】统计 fallback_api 在原始 body 里出现的次数（未经任何过滤），
  // 用来区分「平台根本没给 fallback_api」和「给了但被丢弃/解析失败」。
  const rawFallbackHits = (rawBody.match(/fallback_api/g) || []).length;
  const fallbackApis = findFallbackApis(json, rawBody, source).slice(0, MAX_FALLBACK_API_COUNT);
  const videoUrls = await Promise.all(
    fallbackApis.map((fallbackApi) => getCachedUnwatermarkedVideoUrl(fallbackApi, source))
  );
  let videoCount = 0;
  for (const videoUrl of videoUrls) {
    if (addItem(items, seenUrls, "video", videoUrl)) {
      videoCount += 1;
    }
  }

  // 【v1.3.2 诊断】一次性输出全链路计数，便于定位"未提取到资源"到底卡在哪一步。
  let firstHost = "";
  if (fallbackApis.length) {
    try {
      firstHost = new URL(fallbackApis[0]).hostname;
    } catch {
      firstHost = "(无法解析)";
    }
  }
  console.warn(
    `[苏生阁] ${source} 提取诊断: body=${rawBody.length}B`
      + ` | image(json)=${imageUrlsFromJson.length}`
      + ` image(regex)=${imageUrlsFromBody.length}`
      + ` | fallback_api出现=${rawFallbackHits}`
      + ` 候选=${fallbackApis.length}`
      + ` 首个主机=${firstHost || "(无)"}`
      + ` | 视频成功=${videoCount}`
      + ` | items=${items.length}`
  );

  return items;
}

function extractQianwenItems(rawBody) {
  const items = [];
  const seenUrls = new Set();

  for (const url of findRegexUrls(
    rawBody,
    /\bsrc\s*=\s*\\?"(https?:\/\/.+?)\\?"\s*poster\b/gi
  )) {
    addItem(items, seenUrls, "video", url);
  }

  for (const url of findRegexUrls(
    rawBody,
    /\\?"thumbnail_url\\?"\s*:\s*\\?"(.+?)\\?"/gi
  )) {
    addItem(items, seenUrls, "image", url);
  }

  const json = parseJsonString(rawBody);
  if (json !== null) {
    for (const value of findValuesByKey(json, "thumbnail_url")) {
      addItem(items, seenUrls, "image", normalizeExtractedUrl(value));
    }
  }

  return items;
}

function extractYunqueThreadItems(rawBody) {
  const items = [];
  const seenUrls = new Set();

  for (const url of findRegexUrls(
    rawBody,
    /\\?"origin_url\\?"\s*:\s*\\?"(.+?)\\?"/gi
  )) {
    addItem(items, seenUrls, "video", url);
  }

  for (const url of findRegexUrls(
    rawBody,
    /\\?"url\\?"\s*:\s*\\?"(.+?)\\?"/gi
  )) {
    if (url.startsWith("https://") && url.toLowerCase().includes("origin_aigc")) {
      addItem(items, seenUrls, "image", url);
    }
  }

  const json = parseJsonString(rawBody);
  if (json !== null) {
    for (const value of findValuesByKey(json, "origin_url")) {
      addItem(items, seenUrls, "video", normalizeExtractedUrl(value));
    }
    for (const value of findValuesByKey(json, "url")) {
      const url = normalizeExtractedUrl(value);
      if (url.startsWith("https://") && url.toLowerCase().includes("origin_aigc")) {
        addItem(items, seenUrls, "image", url);
      }
    }
  }

  return items;
}

function extractYunquePackageItems(rawBody) {
  const items = [];
  const seenUrls = new Set();

  for (const url of findRegexUrls(
    rawBody,
    /\\?"url\\?"\s*:\s*\\?"(https:\/\/.+?)\\?"/gi
  )) {
    addItem(items, seenUrls, "video", url);
  }

  const json = parseJsonString(rawBody);
  if (json !== null) {
    for (const value of findValuesByKey(json, "url")) {
      const url = normalizeExtractedUrl(value);
      if (url.startsWith("https://")) {
        addItem(items, seenUrls, "video", url);
      }
    }
  }

  return items;
}

function findRegexUrls(text, pattern) {
  const urls = [];
  pattern.lastIndex = 0;
  let match = pattern.exec(text);
  while (match) {
    const url = normalizeExtractedUrl(match[1]);
    if (url) {
      urls.push(url);
    }
    match = pattern.exec(text);
  }
  return urls;
}

function normalizeExtractedUrl(value) {
  return typeof value === "string" ? decodeJsonEscapedFragment(value).trim() : "";
}

function findFallbackApis(json, rawBody, source) {
  const apis = new Set();

  for (const value of findValuesByKey(json, "fallback_api")) {
    addFallbackApi(apis, value, source);
  }

  // 【v1.3.2】主程序 FallbackApiStreamScanner 用 3 条正则覆盖「0/1/2 层 JSON 转义」，
  // 插件原先只有 0 层 + 1 层两条，遇到两层转义的响应（豆包 chain 会把 JSON 再套一层字符串）
  // 就完全匹配不到 → fallback_api 漏掉 → 视频提取为 0（面板显示"未提取到资源"）。
  // 这里改成一条通用正则，\\* 吃掉任意层数的转义反斜杠，覆盖 0/1/2/N 层。
  const patterns = [
    /fallback_api\\*"\s*:\s*\\*"(.+?)\\*"/g,
    /"fallback_api"\s*:\s*"([^"]+)"/g
  ];

  for (const pattern of patterns) {
    let match = pattern.exec(rawBody);
    while (match) {
      addFallbackApi(apis, decodeJsonEscapedFragment(match[1]), source);
      match = pattern.exec(rawBody);
    }
  }

  return Array.from(apis);
}

function addFallbackApi(apis, value, source) {
  if (typeof value !== "string" || !value) {
    return;
  }

  const url = decodeJsonEscapedFragment(value);
  if (isAllowedFallbackApiUrl(url)) {
    apis.add(url);
  }
}

// 【v1.3.2】与主程序 LocalVideoResolver.BuildRequestUri 对齐：只要求「绝对 URL + http/https + 有主机名」，
// 不再限制域名，也不再强制 https。平台换 CDN 域时不会再被静默丢弃。
function isAllowedFallbackApiUrl(rawUrl) {
  try {
    const parsed = new URL(rawUrl);
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
      return false;
    }
    const hostname = parsed.hostname.toLowerCase().replace(/\.$/, "");
    if (!hostname || hostname === "localhost" || hostname.endsWith(".local")) {
      return false;
    }
    return true;
  } catch {
    return false;
  }
}

function decodeJsonEscapedFragment(value) {
  let text = value;
  for (let index = 0; index < 3; index += 1) {
    try {
      const decoded = JSON.parse(`"${text.replace(/"/g, '\\"')}"`);
      if (decoded === text) {
        break;
      }
      text = decoded;
    } catch {
      break;
    }
  }
  // 【v1.3.2】补 \u003d → "=" 的还原（与主程序 FallbackApiStreamScanner.DecodeJsonStringLiteral 对齐）
  return text
    .replace(/\\u0026/gi, "&")
    .replace(/\\u003d/gi, "=")
    .replace(/\\\//g, "/");
}

async function getUnwatermarkedVideoUrlFromFallbackApi(fallbackApi, source) {
  const resource = await getUnwatermarkedVideoResourceFromFallbackApi(fallbackApi, source);
  return resource?.url || "";
}

async function getUnwatermarkedVideoResourceFromFallbackApi(fallbackApi, source, requestedResolution = "auto") {
  if (!isAllowedFallbackApiUrl(fallbackApi)) return { url: "", error: "INVALID_URL" };
  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), FALLBACK_API_TIMEOUT_MS);
  try {
    const url = replaceQueryParams(fallbackApi, {
      channel: "no",
      codec_type: "8",
      logo_type: "unwatermarked"
    });
    const requestHost = new URL(url).hostname;
    const rootHost = SOURCE_HOSTS[source];
    const firstParty = Boolean(rootHost && (requestHost === rootHost || requestHost.endsWith(`.${rootHost}`)));
    const response = await fetch(url, {
      method: "GET",
      credentials: firstParty ? "include" : "omit",
      signal: controller.signal,
      headers: {
        "accept": "application/json,text/plain,*/*"
      }
    });
    if (!response.ok) {
      return { url: "", error: `HTTP_${response.status}` };
    }
    if (response.url && !isAllowedFallbackApiUrl(response.url)) {
      return { url: "", error: "INVALID_REDIRECT" };
    }
    const payload = await response.json();
    const data = getVideoData(payload);
    const variants = summarizeVideoQualities(data);
    const candidates = rankVideoCandidates(data);
    if (!candidates.length) return { url: "", error: "NO_UNMARKED_VIDEO_CANDIDATE", variants };
    const responseKeySeed = findKeySeedDeep(payload);
    const urlKeySeed = getQueryParam(url, "key_seed");
    let missingSeed = false;
    for (const candidate of candidates) {
      const keySeed = candidate.keySeed || responseKeySeed || urlKeySeed;
      if (candidate.token.startsWith("qAAB") && !keySeed) {
        missingSeed = true;
        continue;
      }
      const decodedUrl = await decodeMainUrl(candidate.token, keySeed);
      if (!decodedUrl) continue;
      return {
        type: "video", url: decodedUrl, width: candidate.width, height: candidate.height,
        definition: candidate.definition, resolution: candidate.resolution,
        resolutionEvidence: candidate.width && candidate.height ? "dimensions" : candidate.resolution ? "definition" : "unknown",
        bitrate: candidate.bitrate, watermark: candidate.watermark,
        provenance: "fallback_api", requestedResolution, variants,
        resourceKey: `${source}:${mediaFingerprint(fallbackApi)}`
      };
    }
    return { url: "", error: missingSeed ? "KEY_SEED_MISSING" : "URL_DECODE_FAILED", variants };
  } catch (error) {
    const code = error?.name === "AbortError" ? "TIMEOUT" : "REQUEST_OR_PAYLOAD_FAILED";
    console.warn(`${source} fallback_api resolve failed: ${code}`);
    return { url: "", error: code };
  } finally {
    clearTimeout(timeoutId);
  }
}

function getCachedUnwatermarkedVideoUrl(fallbackApi, source) {
  return getCachedUnwatermarkedVideoResource(fallbackApi, source).then((resource) => resource?.url || "");
}

function getCachedUnwatermarkedVideoResource(fallbackApi, source, requestedResolution = "auto", force = false) {
  const preference = ["480p", "720p", "1080p"].includes(requestedResolution) ? requestedResolution : "auto";
  const key = `${source}:${preference}:${mediaFingerprint(fallbackApi)}`;
  const now = Date.now();
  const cached = fallbackVideoCache.get(key);
  if (cached && !force && (!cached.completed || now - cached.createdAt < cached.ttl)) {
    return cached.promise;
  }
  if (cached) {
    fallbackVideoCache.delete(key);
  }

  const promise = getUnwatermarkedVideoResourceFromFallbackApi(fallbackApi, source, preference);
  const entry = { createdAt: now, promise, completed: false, ttl: FALLBACK_VIDEO_CACHE_TTL_MS };
  fallbackVideoCache.set(key, entry);
  promise.then((resource) => {
    entry.completed = true;
    entry.createdAt = Date.now();
    // A temporary 720p rendition must not hide a later 1080p rendition.
    if (preference === "1080p" && Number.parseInt(resource?.resolution || "0", 10) < 1080) entry.ttl = 3000;
    // 未生成完成或解析失败不能长时间缓存，否则会错过后续真正完成的结果。
    if (!resource?.url && fallbackVideoCache.get(key)?.promise === promise) {
      fallbackVideoCache.delete(key);
    }
  }, () => {
    if (fallbackVideoCache.get(key)?.promise === promise) {
      fallbackVideoCache.delete(key);
    }
  });

  while (fallbackVideoCache.size > FALLBACK_VIDEO_CACHE_MAX) {
    fallbackVideoCache.delete(fallbackVideoCache.keys().next().value);
  }
  return promise;
}

function replaceQueryParams(url, params) {
  const parsedUrl = new URL(url);
  for (const [key, value] of Object.entries(params)) {
    parsedUrl.searchParams.set(key, value);
  }
  return parsedUrl.toString();
}

function getQueryParam(url, name) {
  try {
    return new URL(url).searchParams.get(name)?.trim() || "";
  } catch {
    return "";
  }
}

function getVideoData(payload) {
  const videoInfo = payload?.video_info || payload?.data?.video_info || payload;
  const data = videoInfo?.data || videoInfo;
  return data && typeof data === "object" ? data : {};
}

function pickMainUrlToken(data) {
  return rankVideoCandidates(data)[0]?.token || "";
}

function watermarkStatus(value) {
  if (!value || typeof value !== "object") return "unknown";
  for (const key of ["is_watermark", "is_watermarked", "has_watermark", "watermark", "logo_type"]) {
    const flag = value[key];
    if (flag === true || flag === 1 || /^(?:true|1|watermarked|with_watermark)$/i.test(String(flag ?? ""))) return "marked";
  }
  for (const key of ["is_watermark", "is_watermarked", "has_watermark", "watermark", "logo_type"]) {
    const flag = value[key];
    if (flag === false || flag === 0 || /^(?:false|0|unwatermarked|without_watermark|none|no_logo)$/i.test(String(flag ?? ""))) return "unmarked";
  }
  return "unknown";
}

function summarizeVideoQualities(data) {
  const videoList = data?.video_list;
  const entries = videoList && typeof videoList === "object" && Object.keys(videoList).length
    ? Object.entries(videoList) : [["", data]];
  return entries.slice(0, 30).filter(([, entry]) => entry && typeof entry === "object").map(([name, entry]) => {
    const width = Number(entry.vwidth || entry.width) || 0;
    const height = Number(entry.vheight || entry.height) || 0;
    const explicit = watermarkStatus(entry);
    return {
      definition: String(entry.definition || entry.resolution || entry.quality || name || "").slice(0, 60),
      width, height,
      watermark: explicit === "unknown" ? watermarkStatus(data) : explicit,
      hasUrl: Boolean(entry.main_url || entry.play_url || entry.backup_url || entry.backup_urls)
    };
  });
}

function rankVideoCandidates(data) {
  const videoList = data?.video_list;
  const entries = videoList && typeof videoList === "object" && Object.keys(videoList).length
    ? Object.entries(videoList) : [["", data]];
  const candidates = [];
  const number = (value) => Number.isFinite(Number(value)) && Number(value) > 0 ? Number(value) : 0;

  for (const [name, entry] of entries) {
    if (!entry || typeof entry !== "object") {
      continue;
    }
    const explicit = watermarkStatus(entry);
    const watermark = explicit === "unknown" ? watermarkStatus(data) : explicit;
    if (watermark === "marked") continue;
    const width = number(entry.vwidth || entry.width);
    const height = number(entry.vheight || entry.height);
    const definition = String(entry.definition || entry.resolution || entry.quality || name || "");
    const declared = Number(definition.match(/(?:4320|2160|1440|1080|720|540|480|360|240)/)?.[0] || 0);
    const pixels = width && height ? Math.min(width, height) : declared;
    const bitrate = number(entry.bitrate || entry.real_bitrate);
    const tokens = [entry.main_url, entry.play_url,
      ...(Array.isArray(entry.backup_url) ? entry.backup_url : [entry.backup_url]),
      ...(Array.isArray(entry.backup_urls) ? entry.backup_urls : [])];
    tokens.forEach((token, index) => {
      if (typeof token !== "string" || !token.trim()) return;
      candidates.push({ token: token.trim(), width, height, definition: definition.slice(0, 60),
        resolution: pixels ? `${pixels}p` : "", bitrate, watermark,
        keySeed: findKeySeedDeep(entry), watermarkRank: watermark === "unmarked" ? 2 : 1,
        resolutionRank: pixels, area: width * height, index });
    });
  }
  // Explicitly marked streams were already excluded. This is the response
  // to logo_type=unwatermarked: missing per-variant metadata must not make
  // an available 1080p rendition lose to an explicitly labelled 720p one.
  return candidates.sort((a, b) => b.resolutionRank - a.resolutionRank || b.area - a.area ||
    b.watermarkRank - a.watermarkRank || b.bitrate - a.bitrate || a.index - b.index);
}

function findKeySeedDeep(value, depth = 0) {
  if (depth > 10 || value == null) {
    return "";
  }

  if (typeof value === "string") {
    let match = value.match(/(?:^|[?&])key_seed=([^&"'<>\\\s]+)/i);
    if (match) {
      return decodeURIComponent(match[1]);
    }
    match = value.match(/["']key_seed["']\s*:\s*["']([^"']+)/i);
    return match ? decodeURIComponent(match[1]) : "";
  }

  if (typeof value !== "object") {
    return "";
  }

  if (typeof value.key_seed === "string" && value.key_seed.trim()) {
    return value.key_seed.trim();
  }

  for (const item of Object.values(value)) {
    const hit = findKeySeedDeep(item, depth + 1);
    if (hit) {
      return hit;
    }
  }

  return "";
}

async function decodeMainUrl(token, keySeed = "") {
  if (isHttpUrl(token)) {
    return token;
  }

  const plainUrl = tryDecodeBase64Url(token);
  if (plainUrl) {
    return plainUrl;
  }

  if (token.startsWith("qAAB") && keySeed) {
    return await decodeQaabToken(token, keySeed);
  }

  return "";
}

function tryDecodeBase64Url(token) {
  const bytes = base64DecodeLoose(token);
  if (!bytes) {
    return "";
  }
  const text = asciiUrlFromBytes(bytes);
  return isHttpUrl(text) ? text : "";
}

function base64DecodeLoose(text) {
  const input = String(text || "").trim();
  const variants = [
    input,
    input.replace(/[$@#]/g, (char) => ({ "$": "_", "@": "/", "#": "." }[char])),
    input.replace(/[$@#]/g, (char) => ({ "$": "+", "@": "/", "#": "=" }[char]))
  ];
  const seen = new Set();

  for (const candidate of variants) {
    if (!candidate || seen.has(candidate)) {
      continue;
    }
    seen.add(candidate);
    try {
      const normalized = padBase64(candidate).replace(/-/g, "+").replace(/_/g, "/");
      const binary = atob(normalized);
      const bytes = new Uint8Array(binary.length);
      for (let index = 0; index < binary.length; index += 1) {
        bytes[index] = binary.charCodeAt(index);
      }
      return bytes;
    } catch {
      // Try the next variant.
    }
  }

  return null;
}

function padBase64(text) {
  const pad = (4 - (text.length % 4)) % 4;
  return text + "=".repeat(pad);
}

function asciiUrlFromBytes(bytes) {
  if (!bytes || !bytes.length) {
    return "";
  }
  for (const byte of bytes) {
    if (byte !== 9 && byte !== 10 && byte !== 13 && (byte < 32 || byte > 126)) {
      return "";
    }
  }
  return new TextDecoder().decode(bytes);
}

async function decodeQaabToken(token, keySeed) {
  const data = base64DecodeLoose(token);
  const seed = base64DecodeLoose(keySeed);
  if (!data || !seed) {
    return "";
  }

  const digest1 = await crypto.subtle.digest("SHA-512", seed.slice(0, 32));
  const salt = hexToBytes(QAAB_SALT_HEX);
  const digest2Input = concatBytes(new Uint8Array(digest1), salt);
  const digest2 = new Uint8Array(await crypto.subtle.digest("SHA-512", digest2Input));
  const key = digest2.slice(0, 16);
  const iv = digest2.slice(16, 32);
  const attempts = [];

  if (data.length >= 4 && data[0] === 0xa8 && data[1] === 0x00 && data[2] === 0x01 && data[3] === 0x00) {
    attempts.push({ payload: data.slice(4), key, iv });
    attempts.push({ payload: data.slice(4), key: iv, iv: key });
    if (data.length > 36) {
      attempts.push({ payload: data.slice(36), key, iv: data.slice(20, 36) });
      attempts.push({ payload: data.slice(36), key, iv });
    }
  } else {
    attempts.push({ payload: data, key, iv });
  }

  for (const attempt of attempts) {
    const url = await decryptAesCbcUrl(attempt.payload, attempt.key, attempt.iv);
    if (url) {
      return url;
    }
  }

  return "";
}

async function decryptAesCbcUrl(payload, keyBytes, ivBytes) {
  if (!payload.length || payload.length % 16 !== 0) {
    return "";
  }

  try {
    const key = await crypto.subtle.importKey("raw", keyBytes, "AES-CBC", false, ["decrypt"]);
    const plain = new Uint8Array(await crypto.subtle.decrypt({ name: "AES-CBC", iv: ivBytes }, key, payload));
    const direct = asciiUrlFromBytes(plain);
    if (isHttpUrl(direct)) {
      return direct;
    }
    const stripped = stripPkcs7(plain);
    const url = asciiUrlFromBytes(stripped);
    return isHttpUrl(url) ? url : "";
  } catch {
    return "";
  }
}

function stripPkcs7(bytes) {
  if (!bytes || !bytes.length) {
    return new Uint8Array();
  }
  const pad = bytes[bytes.length - 1];
  if (pad < 1 || pad > 16 || pad > bytes.length) {
    return bytes;
  }
  for (let index = bytes.length - pad; index < bytes.length; index += 1) {
    if (bytes[index] !== pad) {
      return bytes;
    }
  }
  return bytes.slice(0, bytes.length - pad);
}

function hexToBytes(hex) {
  const bytes = new Uint8Array(hex.length / 2);
  for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = parseInt(hex.slice(index * 2, index * 2 + 2), 16);
  }
  return bytes;
}

function concatBytes(first, second) {
  const bytes = new Uint8Array(first.length + second.length);
  bytes.set(first, 0);
  bytes.set(second, first.length);
  return bytes;
}

function findImageOriRawUrls(value) {
  const urls = [];
  walkJsonAndStrings(value, (node) => {
    if (node && typeof node === "object" && !Array.isArray(node)) {
      const image = node.image_ori_raw;
      if (image && typeof image === "object" && isHttpUrl(image.url)) {
        urls.push(image.url);
      }
    }
  });
  return urls;
}

// 【修复】从原始响应文本里用正则捞 image_ori_raw.url，不依赖整段 JSON 可解析。
// 背景：豆包的 /im/chain/single 响应不是单个合法 JSON（多段拼接/外层包装），
// 结构化遍历拿不到任何节点，只能靠正则。
// 覆盖两种转义层级：普通 JSON（"image_ori_raw":{"url":...}）与
// JSON-in-JSON（\"image_ori_raw\":{\"url\":...}）。
function findImageOriRawUrlsFromBody(rawBody) {
  const urls = [];
  if (typeof rawBody !== "string" || !rawBody) {
    return urls;
  }

  const patterns = [
    /"image_ori_raw"\s*:\s*\{[\s\S]{0,600}?"url"\s*:\s*"([^"]+)"/g,
    /\\"image_ori_raw\\"\s*:\s*\{[\s\S]{0,600}?\\"url\\"\s*:\s*\\"([^"\\]+)\\"/g
  ];

  for (const pattern of patterns) {
    for (const url of findRegexUrls(rawBody, pattern)) {
      if (isHttpUrl(url)) {
        urls.push(url);
      }
    }
  }
  return urls;
}

function findValuesByKey(value, targetKey) {
  const values = [];
  walkJsonAndStrings(value, (node) => {
    if (!node || typeof node !== "object" || Array.isArray(node)) {
      return;
    }
    if (Object.prototype.hasOwnProperty.call(node, targetKey)) {
      values.push(node[targetKey]);
    }
  });
  return values;
}

function walkJsonAndStrings(value, visitor, seen = new Set()) {
  if (value == null) {
    return;
  }

  if (typeof value === "string") {
    const parsed = parseJsonString(value);
    if (parsed !== null) {
      walkJsonAndStrings(parsed, visitor, seen);
    }
    return;
  }

  if (typeof value !== "object" || seen.has(value)) {
    return;
  }

  seen.add(value);
  visitor(value);

  if (Array.isArray(value)) {
    for (const item of value) {
      walkJsonAndStrings(item, visitor, seen);
    }
    return;
  }

  for (const key of Object.keys(value)) {
    walkJsonAndStrings(value[key], visitor, seen);
  }
}

function parseJsonString(text) {
  const trimmed = text.trim();
  if (!trimmed || (!trimmed.startsWith("{") && !trimmed.startsWith("["))) {
    return null;
  }

  try {
    return JSON.parse(trimmed);
  } catch {
    return null;
  }
}

function addItem(items, seenUrls, type, url, metadata = {}) {
  const fingerprint = mediaFingerprint(url);
  const key = `${type}:${fingerprint}`;
  if (!isHttpUrl(url) || seenUrls.has(key)) {
    return false;
  }
  seenUrls.add(key);
  items.push({ ...metadata, type, url });
  return true;
}

function mediaFingerprint(rawUrl) {
  try {
    const url = new URL(rawUrl);
    url.hash = "";
    for (const key of Array.from(url.searchParams.keys())) {
      if (isVolatileMediaQueryKey(key)) {
        url.searchParams.delete(key);
      }
    }
    url.searchParams.sort();
    return url.toString();
  } catch {
    return String(rawUrl || "").split("#", 1)[0];
  }
}

function isVolatileMediaQueryKey(key) {
  const normalized = String(key || "").toLowerCase();
  return /^(?:x-amz-|x-oss-|x-cos-|x-qn-)/.test(normalized) ||
    /^(?:sign|signature|token|access_token|auth|auth_key|authorization|expires?|expire_at|timestamp|ts|wssecret|wstime|policy|key-pair-id)$/.test(normalized);
}

function buildDownloadFilename(rawUrl, mediaType, mimeType = "") {
  const isVideo = mediaType !== "image";
  const fallbackExtension = isVideo ? ".mp4" : ".jpg";
  let extension = fallbackExtension;
  let baseName = "";
  const mime = String(mimeType || "").toLowerCase();
  const mimeExtensions = isVideo
    ? { "video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov" }
    : { "image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif", "image/avif": ".avif" };
  if (mimeExtensions[mime]) extension = mimeExtensions[mime];
  try {
    const urlObj = new URL(rawUrl);
    baseName = decodeURIComponent(urlObj.pathname.split("/").pop() || "");
    const match = baseName.match(/\.([a-zA-Z0-9]{2,5})$/);
    if (match) {
      const ext = match[1].toLowerCase();
      const valid = isVideo ? ["mp4", "m4v", "mov", "webm", "mkv"] : ["png", "jpg", "jpeg", "webp", "gif", "avif"];
      if (valid.includes(ext) && !mimeExtensions[mime]) {
        extension = `.${ext === "jpeg" ? "jpg" : ext}`;
      }
    }
  } catch {
    baseName = "";
  }
  baseName = baseName.replace(/\.[a-zA-Z0-9]{2,5}$/, "").replace(/[<>:\"/\\|?*\x00-\x1F]/g, "_").replace(/[. ]+$/, "").slice(0, 100);

  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const stamp = `${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}_${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}`;

  if (!baseName || baseName === "download" || baseName === "videoplayback" || baseName.startsWith("blob")) {
    baseName = `苏生阁${isVideo ? "视频" : "图片"}_${stamp}`;
  } else if (!baseName.includes("苏生阁") && !baseName.includes("Dola")) {
    baseName = `苏生阁_${baseName}_${stamp}`;
  }
  return `${baseName}${extension}`;
}

function isHttpUrl(url) {
  return typeof url === "string" && /^https?:\/\//i.test(url);
}

async function sendToTab(tabId, message) {
  try {
    await chrome.tabs.sendMessage(tabId, message);
  } catch (error) {
    console.warn("send panel message failed:", error.message || error);
  }
}

importScripts("video-request-schema.js", "debugger-summary.js", "debugger-network.js");
