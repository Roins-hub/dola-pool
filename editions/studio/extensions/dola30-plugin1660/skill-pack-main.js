(function () {
  "use strict";

  const INSTALL_MARK = "__dolaNative30SkillPackInstalledV162";
  const DURATION_KEY = "intl_doubao_enable_30s_v1";
  const TARGET_DURATION_KEY = "intl_doubao_target_duration_v1";
  const READY_ATTRIBUTE = "data-dola-native-30-ready";
  const STATUS_EVENT = "dola-native-30-status";
  const TARGET_MODEL = "seedance_v2.5";
  const MEDIA_APIS_ATTRIBUTE = "data-dola-media-fallback-apis";
  const MEDIA_APIS_EVENT = "dola-media-fallback-apis";

  // Doubao already has native duration controls. Its resolution hook is separate.
  if (!/(^|\.)dola\.com$/i.test(location.hostname)) return;
  if (globalThis[INSTALL_MARK]) return;
  globalThis[INSTALL_MARK] = true;

  const originalFetch = window.fetch;
  if (typeof originalFetch !== "function") return;

  function requestUrl(input) {
    return typeof input === "string" ? input : (input && (input.url || input.href)) || "";
  }

  function isSkillPackUrl(rawUrl) {
    try {
      const url = new URL(rawUrl, location.href);
      return /(^|\.)dola\.com$/i.test(url.hostname) &&
        url.pathname.replace(/\/$/, "") === "/samantha/skill/pack";
    } catch {
      return false;
    }
  }

  function isActionBarConfigUrl(rawUrl) {
    try {
      const url = new URL(rawUrl, location.href);
      return /(^|\.)dola\.com$/i.test(url.hostname) && url.pathname.replace(/\/$/, "") ===
        "/alice/slot/action_bar_v3/get_item_conf";
    } catch {
      return false;
    }
  }

  function isChainSingleUrl(rawUrl) {
    try {
      const url = new URL(rawUrl, location.href);
      return /(^|\.)dola\.com$/i.test(url.hostname) &&
        url.pathname.replace(/\/$/, "") === "/im/chain/single";
    } catch {
      return false;
    }
  }

  function decodeEscapedFragment(value) {
    let text = String(value || "");
    for (let index = 0; index < 3; index += 1) {
      try {
        const decoded = JSON.parse(`"${text.replace(/"/g, '\\"')}"`);
        if (decoded === text) break;
        text = decoded;
      } catch {
        break;
      }
    }
    return text.replace(/\\u0026/gi, "&").replace(/\\u003d/gi, "=").replace(/\\\//g, "/");
  }

  function publishFallbackApis(rawText) {
    const urls = new Set();
    const patterns = [
      /fallback_api\\*"\s*:\s*\\*"(.+?)\\*"/g,
      /"fallback_api"\s*:\s*"([^"]+)"/g
    ];
    for (const pattern of patterns) {
      let match = pattern.exec(String(rawText || ""));
      while (match && urls.size < 8) {
        const value = decodeEscapedFragment(match[1]);
        try {
          const parsed = new URL(value);
          if (parsed.protocol === "https:" || parsed.protocol === "http:") urls.add(parsed.toString());
        } catch {
          // Ignore malformed candidates from unrelated response fields.
        }
        match = pattern.exec(String(rawText || ""));
      }
    }
    if (!urls.size || !document.documentElement) return;
    document.documentElement.setAttribute(MEDIA_APIS_ATTRIBUTE, JSON.stringify(Array.from(urls)));
    document.dispatchEvent(new Event(MEDIA_APIS_EVENT));
  }

  async function observeChainResponse(response) {
    try {
      publishFallbackApis(await response.clone().text());
    } catch {
      // Passive capture must never affect the page response.
    }
  }

  function modeEnabled() {
    try {
      return localStorage.getItem(DURATION_KEY) === "1";
    } catch {
      return false;
    }
  }

  function targetDuration() {
    try {
      const value = Number(localStorage.getItem(TARGET_DURATION_KEY));
      if (value === 15 || value === 30) return value;
    } catch {
      // Fall through to the legacy 30-second switch.
    }
    return modeEnabled() ? 30 : 30;
  }

  function publishReady(ready, reason = "") {
    document.documentElement?.setAttribute(READY_ATTRIBUTE, ready ? "1" : "0");
    document.dispatchEvent(new CustomEvent(STATUS_EVENT, {
      detail: { ready: Boolean(ready), reason: String(reason || "") }
    }));
  }

  function patchDurationOptions(node) {
    let patched = false;
    const seen = new Set();

    function walk(value, videoContext = false) {
      if (!value || typeof value !== "object" || seen.has(value)) return;
      seen.add(value);
      const key = String(value.key || value.value || value.name || "");
      const context = !/^(?:image[-_]|image_generation$|photo[-_])/i.test(key) &&
        (videoContext || /^video[-_]/i.test(key) || Number(value.ability_type) === 17 ||
          Boolean(value.model_capability?.[TARGET_MODEL]));

      if (context && value.value === "duration" && Array.isArray(value.options)) {
        const template = value.options.find((option) => String(option?.value) === "10") || value.options[0] || {};
        for (const seconds of [15, 30]) {
          if (!value.options.some((option) => String(option?.value) === String(seconds))) {
            value.options.push({
              ...template,
              show_name: `${seconds}s`,
              value: String(seconds),
              is_default: false,
              sub_display: template.sub_display || ""
            });
          }
        }
        const selected = targetDuration();
        for (const option of value.options) option.is_default = Number(option?.value) === selected;
        patched = true;
      }

      if (value.model_capability && typeof value.model_capability === "object") {
        const capability = value.model_capability[TARGET_MODEL];
        if (capability && typeof capability === "object") {
          const durations = Array.isArray(capability.supported_durations)
            ? capability.supported_durations.map(String)
            : [];
          for (const seconds of ["15", "30"]) {
            if (!durations.includes(seconds)) durations.push(seconds);
          }
          capability.supported_durations = durations;
          patched = true;
        }
      }

      for (const [childKey, child] of Object.entries(value)) {
        const childContext = /^(?:image[-_]|image_generation$|photo[-_])/i.test(childKey) ? false :
          context || childKey === "video_generation" || /^video[-_]/i.test(childKey);
        walk(child, childContext);
      }
    }

    walk(node);
    return patched;
  }

  function preserveResponseMetadata(response, original) {
    for (const key of ["url", "type", "redirected"]) {
      Object.defineProperty(response, key, { configurable: true, get: () => original[key] });
    }
    const nativeClone = response.clone.bind(response);
    Object.defineProperty(response, "clone", {
      configurable: true, value: () => preserveResponseMetadata(nativeClone(), original)
    });
    return response;
  }

  async function patchSkillPackResponse(response) {
    if (!response.ok || response.status === 204 || response.status === 205) return response;
    try {
      const json = await response.clone().json();
      if (Object.hasOwn(json, "code") && Number(json.code) !== 0) return response;
      const patched = patchDurationOptions(json);
      publishReady(patched, patched ? "" : "能力响应中未找到视频时长配置");
      if (!patched) return response;
      const headers = new Headers(response.headers);
      headers.delete("content-length");
      headers.delete("content-encoding");
      headers.set("content-type", "application/json; charset=utf-8");
      return preserveResponseMetadata(new Response(JSON.stringify(json), {
        status: response.status,
        statusText: response.statusText,
        headers
      }), response);
    } catch (error) {
      publishReady(false, error?.message || String(error));
      return response;
    }
  }

  function patchSkillPackText(rawText) {
    try {
      const json = JSON.parse(rawText);
      if (Object.hasOwn(json, "code") && Number(json.code) !== 0) return rawText;
      const patched = patchDurationOptions(json);
      publishReady(patched, patched ? "" : "能力响应中未找到视频时长配置");
      return patched ? JSON.stringify(json) : rawText;
    } catch (error) {
      publishReady(false, error?.message || String(error));
      return rawText;
    }
  }

  function patchActionBarConfig(node) {
    let patched = false;
    const seen = new Set();

    function walk(value, parent = null, parentKey = "") {
      if (typeof value === "string") {
        const trimmed = value.trim();
        if ((trimmed.startsWith("{") || trimmed.startsWith("[")) &&
            (trimmed.includes("video-duration") || trimmed.includes("video-model"))) {
          try {
            const parsed = JSON.parse(value);
            const changed = walk(parsed);
            if (changed && parent) parent[parentKey] = JSON.stringify(parsed);
            return changed;
          } catch {
            return false;
          }
        }
        return false;
      }
      if (!value || typeof value !== "object" || seen.has(value)) return false;
      seen.add(value);
      let changed = false;

      if (value.key === "video-duration" && Array.isArray(value.option_list)) {
        const template = value.option_list.find((option) => String(option?.option_key) === "10") || value.option_list[0] || {};
        for (const seconds of [15, 30]) {
          if (!value.option_list.some((option) => Number(option?.option_key) === seconds)) {
            value.option_list.push({
              ...template,
              id: Math.max(0, ...value.option_list.map((option) => Number(option?.id) || 0)) + 1,
              display_text: `${seconds}s`,
              message_text: "",
              option_key: String(seconds)
            });
          }
        }
        const selectedIndex = value.option_list.findIndex((option) => Number(option?.option_key) === targetDuration());
        value.default_selection_index = selectedIndex;
        if (value.option_list[selectedIndex]?.id != null) value.default_selection_id = value.option_list[selectedIndex].id;
        changed = true;
      }

      // 2026-10 起部分 Dola 账号命中新版创作页：时长不再是
      // option_list，而是 type=2 的 4~10 秒滑杆。旧逻辑因此完全看不到 30 秒。
      // 必须在 React 读取配置前扩展官方 range，页面才能自己维护选中状态并
      // 用正常签名链路提交 30 秒；不能在生成接口签名后硬改 body。
      if (value.key === "video-duration" && !Array.isArray(value.option_list)) {
        const hasRangeShape = [
          "lower_bound", "upper_bound", "step_length", "default_selected_duration",
          "lowerBound", "upperBound", "stepLength", "defaultSelectedDuration"
        ].some((key) => Object.hasOwn(value, key));
        if (hasRangeShape) {
          const selected = targetDuration();
          if (Object.hasOwn(value, "upper_bound")) value.upper_bound = Math.max(Number(value.upper_bound) || 0, 30);
          if (Object.hasOwn(value, "upperBound")) value.upperBound = Math.max(Number(value.upperBound) || 0, 30);
          if (!Object.hasOwn(value, "upper_bound") && !Object.hasOwn(value, "upperBound")) value.upper_bound = 30;
          if (Object.hasOwn(value, "default_selected_duration")) value.default_selected_duration = selected;
          if (Object.hasOwn(value, "defaultSelectedDuration")) value.defaultSelectedDuration = selected;
          if (!Object.hasOwn(value, "default_selected_duration") && !Object.hasOwn(value, "defaultSelectedDuration")) {
            value.default_selected_duration = selected;
          }
          changed = true;
        }
      }

      if (value.key === "video-model" && Array.isArray(value.option_list)) {
        const modelIndex = value.option_list.findIndex((option) => option?.option_key === TARGET_MODEL);
        if (modelIndex >= 0) {
          value.default_selection_index = modelIndex;
          value.default_selection_id = value.option_list[modelIndex].id;
          changed = true;
        }
      }

      for (const [key, child] of Object.entries(value)) {
        changed = walk(child, value, key) || changed;
      }
      return changed;
    }

    patched = walk(node);
    return patched;
  }

  async function patchActionBarResponse(response) {
    if (!response.ok || response.status === 204 || response.status === 205) return response;
    try {
      const json = await response.clone().json();
      if (Object.hasOwn(json, "code") && Number(json.code) !== 0) return response;
      patchActionBarConfig(json);
      const headers = new Headers(response.headers);
      headers.delete("content-length");
      headers.delete("content-encoding");
      headers.set("content-type", "application/json; charset=utf-8");
      return preserveResponseMetadata(new Response(JSON.stringify(json), {
        status: response.status,
        statusText: response.statusText,
        headers
      }), response);
    } catch {
      return response;
    }
  }

  function patchActionBarText(rawText) {
    try {
      const json = JSON.parse(rawText);
      if (Object.hasOwn(json, "code") && Number(json.code) !== 0) return rawText;
      patchActionBarConfig(json);
      return JSON.stringify(json);
    } catch {
      return rawText;
    }
  }

  window.fetch = function dolaNative30Fetch(input, init = {}) {
    const url = requestUrl(input);
    const result = originalFetch.call(this, input, init);
    if (isSkillPackUrl(url)) return Promise.resolve(result).then(patchSkillPackResponse);
    if (isActionBarConfigUrl(url)) return Promise.resolve(result).then(patchActionBarResponse);
    if (!globalThis.__doubaoDolaMediaCaptureV1620 && isChainSingleUrl(url)) {
      Promise.resolve(result).then(observeChainResponse).catch(() => {});
    }
    return result;
  };

  // Dola 的不同构建版本可能用 XMLHttpRequest 获取能力配置。与 fetch 一样，
  // 只改未签名的能力配置响应；绝不改生成提交请求。
  const xhrOpen = XMLHttpRequest.prototype.open;
  const responseTextGetter = Object.getOwnPropertyDescriptor(XMLHttpRequest.prototype, "responseText")?.get;
  const responseGetter = Object.getOwnPropertyDescriptor(XMLHttpRequest.prototype, "response")?.get;
  const xhrGetterMark = Symbol("dola-native-duration-getters");
  XMLHttpRequest.prototype.open = function dolaNative30XhrOpen(method, url) {
    // XHR objects may be opened again for an unrelated endpoint.
    if (this[xhrGetterMark]) {
      delete this.responseText;
      delete this.response;
      this[xhrGetterMark] = false;
    }
    const requestUrl = String(url || "");
    const skillRequest = isSkillPackUrl(requestUrl);
    const actionBarRequest = isActionBarConfigUrl(requestUrl);
    const chainRequest = isChainSingleUrl(requestUrl);
    if (chainRequest && !globalThis.__doubaoDolaMediaCaptureV1620) {
      this.addEventListener("load", () => {
        try {
          const raw = typeof this.responseText === "string"
            ? this.responseText
            : JSON.stringify(this.response || "");
          publishFallbackApis(raw);
        } catch {
          // Read-only fallback; never block or replace the XHR response.
        }
      }, { once: true });
    }
    if (skillRequest || actionBarRequest) {
      if (responseTextGetter) {
        try {
          Object.defineProperty(this, "responseText", {
            configurable: true,
            get: () => {
              const raw = responseTextGetter.call(this);
              if (this.readyState !== 4 || this.status < 200 || this.status >= 300) return raw;
              return actionBarRequest ? patchActionBarText(raw) : patchSkillPackText(raw);
            }
          });
          this[xhrGetterMark] = true;
        } catch {
          // 当前 Chromium 不允许覆盖实例 getter 时，fetch 路径仍然有效。
        }
      }
      if (responseGetter) {
        try {
          Object.defineProperty(this, "response", {
            configurable: true,
            get: () => {
              const raw = responseGetter.call(this);
              if (this.readyState !== 4 || this.status < 200 || this.status >= 300) return raw;
              if (typeof raw === "string") return actionBarRequest ? patchActionBarText(raw) : patchSkillPackText(raw);
              if (raw && typeof raw === "object") {
                if (Object.hasOwn(raw, "code") && Number(raw.code) !== 0) return raw;
                const patched = actionBarRequest ? patchActionBarConfig(raw) : patchDurationOptions(raw);
                publishReady(patched, patched ? "" : "能力响应中未找到视频时长配置");
              }
              return raw;
            }
          });
          this[xhrGetterMark] = true;
        } catch {
          // See responseText fallback above.
        }
      }
    }
    return xhrOpen.apply(this, arguments);
  };
})();
