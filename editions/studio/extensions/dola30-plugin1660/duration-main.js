(function () {
  "use strict";

  const INSTALL_KEY = "__watermarkFreeDuration30Installed";
  const DURATION_KEY = "intl_doubao_enable_30s_v1";
  const TARGET_DURATION_KEY = "intl_doubao_target_duration_v1";
  const STATE_ATTRIBUTE = "data-watermark-free-duration-30";
  const STATE_EVENT = "watermark-free-duration-30-change";
  const MENU_MARK = "data-watermark-free-duration-option";
  const NATIVE_BOUND_MARK = "data-watermark-free-native-duration-bound";
  const MODEL_SELECTOR = "[data-input-engine-actionbar-control-key=\"video-model\"], [data-input-engine-actionbar-control-key=\"model\"]";

  if (globalThis[INSTALL_KEY]) {
    return;
  }
  globalThis[INSTALL_KEY] = true;

  const host = location.hostname.replace(/^www\./i, "").toLowerCase();
  if (!(host === "dola.com" || host.endsWith(".dola.com") || host === "doubao.com" || host.endsWith(".doubao.com"))) {
    return;
  }

  let lastKnownModelTarget = 0;
  let native30Observed = false;

  function textOf(element) {
    return String(
      element?.textContent ||
      element?.getAttribute?.("aria-label") ||
      element?.getAttribute?.("title") ||
      ""
    ).replace(/\s+/g, " ").trim();
  }

  function visible(element) {
    try {
      const rect = element?.getBoundingClientRect();
      const style = element && getComputedStyle(element);
      return Boolean(
        element &&
        element.isConnected &&
        rect.width > 1 &&
        rect.height > 1 &&
        style.display !== "none" &&
        style.visibility !== "hidden" &&
        Number(style.opacity || 1) > 0.01
      );
    } catch {
      return false;
    }
  }

  function durationText(value) {
    return String(value || "").replace(/\s+/g, "").replace(/[✓✔√]/g, "").trim();
  }

  function selectedModelText() {
    return durationText(textOf(document.querySelector(MODEL_SELECTOR)));
  }

  function modelTargetDuration() {
    // 开启 30 秒模式后，以扩展状态为准，不再依赖 Dola 易变的按钮文案。
    // 这样页面重绘或平台把模型文案暂时恢复成 Fast 时，也不会丢失 30 秒入口。
    if (duration30Enabled()) {
      lastKnownModelTarget = 30;
      return 30;
    }
    const text = selectedModelText();
    if (/2\.5|seedance[^\d]*2[^\d]*5/i.test(text)) {
      lastKnownModelTarget = 30;
    } else if (/2\.0|seedance[^\d]*2[^\d]*0|seedance[^\d]*fast/i.test(text)) {
      lastKnownModelTarget = 0;
    }
    return lastKnownModelTarget;
  }

  function duration30Enabled() {
    try {
      return localStorage.getItem(DURATION_KEY) === "1";
    } catch {
      return false;
    }
  }

  function selectedDuration() {
    try {
      const value = Number(localStorage.getItem(TARGET_DURATION_KEY));
      if (value === 15 || value === 30) return value;
    } catch {
      // Fall through to the legacy switch.
    }
    return duration30Enabled() ? 30 : 30;
  }

  function saveTargetDuration(seconds) {
    const value = Number(seconds) === 15 ? 15 : 30;
    try {
      localStorage.setItem(TARGET_DURATION_KEY, String(value));
      localStorage.setItem(DURATION_KEY, value === 30 ? "1" : "0");
    } catch {
      return;
    }
    publishDurationState();
  }

  function publishDurationState() {
    const root = document.documentElement;
    if (!root) {
      return;
    }
    root.setAttribute(STATE_ATTRIBUTE, duration30Enabled() ? "1" : "0");
    document.dispatchEvent(new Event(STATE_EVENT));
  }

  function saveDuration30(enabled) {
    try {
      localStorage.setItem(DURATION_KEY, enabled ? "1" : "0");
    } catch {
      return;
    }
    publishDurationState();
  }

  document.addEventListener(STATE_EVENT, () => {
    const value = document.documentElement?.getAttribute(STATE_ATTRIBUTE);
    if (value !== "0" && value !== "1") {
      return;
    }
    try {
      localStorage.setItem(DURATION_KEY, value);
    } catch {
      return;
    }
    enhanceDurationOptions();
  });

  function exactDuration(element) {
    const match = durationText(textOf(element)).match(/^(5|10|15|30)(s|秒)$/i);
    return match ? Number(match[1]) : 0;
  }

  function findDurationMenuRoot() {
    const candidates = Array.from(document.querySelectorAll(
      "[role=\"menu\"], [role=\"listbox\"], [data-slot*=\"dropdown-menu\"], [class*=\"popover\"], [class*=\"dropdown\"], div"
    ))
      .filter((element) => visible(element))
      .filter((element) => {
        const rect = element.getBoundingClientRect();
        if (rect.width < 70 || rect.width > 520 || rect.height < 40 || rect.height > 520) {
          return false;
        }
        const text = durationText(textOf(element));
        const hasNativeDurations = /5(s|秒)/i.test(text) && /10(s|秒)/i.test(text);
        const looksLikeWholeToolbar = /Seedance|比例|参考图|模型|Model|Fast/i.test(text) && rect.width > 360;
        return hasNativeDurations && !looksLikeWholeToolbar;
      })
      .sort((first, second) => {
        const firstRect = first.getBoundingClientRect();
        const secondRect = second.getBoundingClientRect();
        return firstRect.width * firstRect.height - secondRect.width * secondRect.height;
      });
    return candidates[0] || null;
  }

  function durationCheckMarks(item) {
    return Array.from(item?.querySelectorAll?.("svg") || []);
  }

  function setDurationMenuSelected(item, selected) {
    item?.setAttribute?.("aria-selected", selected ? "true" : "false");
    if (selected) {
      item?.setAttribute?.("data-state", "checked");
    } else {
      item?.removeAttribute?.("data-state");
    }
    for (const mark of durationCheckMarks(item)) {
      mark.style.visibility = selected ? "visible" : "hidden";
    }
  }

  function syncDurationMenuSelection(root) {
    if (!root) {
      return;
    }
    const targetDuration = selectedDuration();
    const options = Array.from(root.querySelectorAll("[role=\"menuitem\"], [role=\"option\"], li, button, div"))
      .filter((element) => element !== root && visible(element) && exactDuration(element) > 0);
    for (const option of options) {
      const value = exactDuration(option);
      if (targetDuration === 15 || targetDuration === 30) {
        setDurationMenuSelected(option, value === targetDuration);
      } else if (option.getAttribute(MENU_MARK) === "30") {
        setDurationMenuSelected(option, false);
      }
    }
  }

  function injectDurationMenuOption() {
    const root = findDurationMenuRoot();
    if (!root) {
      return false;
    }

    const target = modelTargetDuration();
    let options = Array.from(root.querySelectorAll("[role=\"menuitem\"], [role=\"option\"], li, button, div"))
      .filter((element) => element !== root && visible(element) && exactDuration(element) > 0);

    for (const option of options) {
      if (option.getAttribute(MENU_MARK) === "30" && target !== 30) {
        option.remove();
      }
    }

    options = Array.from(root.querySelectorAll("[role=\"menuitem\"], [role=\"option\"], li, button, div"))
      .filter((element) => element !== root && visible(element) && exactDuration(element) > 0);

    // 15/30 必须来自能力配置形成的原生 React 选项，不再克隆 DOM 菜单。
    // document_start 的能力配置响应钩子提供原生时长选项。

    options = Array.from(root.querySelectorAll("[role=\"menuitem\"], [role=\"option\"], li, button, div"))
      .filter((element) => element !== root && visible(element) && exactDuration(element) > 0);
    for (const option of options) {
      const value = exactDuration(option);
      if (value === 30 && !option.hasAttribute(MENU_MARK)) native30Observed = true;
      if ((value === 5 || value === 10 || value === 15 || value === 30) && !option.hasAttribute(NATIVE_BOUND_MARK)) {
        option.setAttribute(NATIVE_BOUND_MARK, String(value));
        option.addEventListener("click", () => {
          if (value === 15 || value === 30) {
            saveTargetDuration(value);
          } else {
            try { localStorage.removeItem(TARGET_DURATION_KEY); } catch {}
            saveDuration30(false);
          }
        }, true);
      }
    }

    syncDurationMenuSelection(root);
    return true;
  }

  function enhanceDurationOptions() {
    injectDurationMenuOption();
  }

  // 1.4.6 起不再在 fetch/XHR 最后一刻改写 /chat/completion body。
  // 请求 URL 已包含 a_bogus/msToken/fp 等风控字段，签名完成后再改 body
  // 可能触发 710022002。30 秒能力由 document_start 的 skill-pack-main.js
  // 在页面初始化阶段注入，让 Dola 自己构造并签名最终请求。
  // 时长只由原生选项 / ability_param.duration 传递。
  // 不改写编辑框、用户时间轴或聊天文本，也不添加提示词控制段落。
  publishDurationState();
  enhanceDurationOptions();
  setInterval(enhanceDurationOptions, 1000);
})();
