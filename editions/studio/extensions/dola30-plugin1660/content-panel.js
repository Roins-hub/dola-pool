(function () {
  const PANEL_ID = "watermark-free-media-panel";
  const INSTANCE_KEY = "__watermarkFreeMediaPanelActive";
  const DURATION_KEY = "intl_doubao_enable_30s_v1";
  const TARGET_DURATION_KEY = "intl_doubao_target_duration_v1";
  const DURATION_STATE_ATTRIBUTE = "data-watermark-free-duration-30";
  const DURATION_STATE_EVENT = "watermark-free-duration-30-change";
  const RESOLUTION_KEY = "intl_doubao_target_resolution_v1";
  const RESOLUTION_STATUS_ATTRIBUTE = "data-watermark-free-resolution-status";
  const RESOLUTION_STATUS_EVENT = "watermark-free-resolution-status";
  const RESOLUTION_PREFERENCE_ATTRIBUTE = "data-watermark-free-resolution-preference";
  const RESOLUTION_PREFERENCE_EVENT = "watermark-free-resolution-preference-change";
  const RESOLUTION_BRIDGE_ATTRIBUTE = "data-watermark-free-resolution-bridge";
  const RESOLUTION_BRIDGE_EVENT = "watermark-free-resolution-bridge-status";
  const MODEL_KEY = "intl_doubao_target_video_model_v1";
  const MODEL_STATUS_ATTRIBUTE = "data-watermark-free-model-status";
  const MODEL_STATUS_EVENT = "watermark-free-model-status";
  const MODEL_PREFERENCE_ATTRIBUTE = "data-watermark-free-model-preference";
  const MODEL_PREFERENCE_EVENT = "watermark-free-model-preference-change";
  const GENERATION_ATTRIBUTE = "data-watermark-free-generation-evidence";
  const GENERATION_EVENT = "watermark-free-generation-evidence";
  const AUTO_CONFIRM_ATTRIBUTE = "data-watermark-free-auto-confirm";
  const AUTO_CONFIRM_EVENT = "watermark-free-auto-confirm";
  const AUTO_CONFIRM_PREF_ATTRIBUTE = "data-watermark-free-auto-confirm-enabled";
  const AUTO_CONFIRM_PREF_EVENT = "watermark-free-auto-confirm-change";
  const GENERATION_INTENT_ATTRIBUTE = "data-watermark-free-generation-intent";
  const GENERATION_INTENT_EVENT = "watermark-free-generation-intent";
  const GENERATION_INTENT_PREF_ATTRIBUTE = "data-watermark-free-generation-intent-enabled";
  const GENERATION_INTENT_PREF_EVENT = "watermark-free-generation-intent-change";
  const MEDIA_APIS_ATTRIBUTE = "data-dola-media-fallback-apis";
  const MEDIA_APIS_EVENT = "dola-media-fallback-apis";
  const MEDIA_MODELS_ATTRIBUTE = "data-dola-media-video-models";
  const MEDIA_MODELS_EVENT = "dola-media-video-models";
  const MEDIA_CAPTURE_ATTRIBUTE = "data-watermark-free-media-capture";
  const MEDIA_CAPTURE_EVENT = "watermark-free-media-capture-status";
  const MEDIA_FORCE_ATTRIBUTE = "data-watermark-free-media-force";
  const MEDIA_RETRY_EVENT = "watermark-free-media-retry";
  const PROTOCOL_SEND_REQUEST_ATTRIBUTE = "data-dola-protocol-send-request";
  const PROTOCOL_SEND_REQUEST_EVENT = "dola-protocol-send-request";
  const PROTOCOL_SEND_STATUS_ATTRIBUTE = "data-dola-protocol-send-status";
  const PROTOCOL_SEND_STATUS_EVENT = "dola-protocol-send-status";
  const PROTOCOL_PARAMS_KEY = "dola_protocol_parameter_selection_v1";
  const PROTOCOL_PARAMS_ATTRIBUTE = "data-dola-protocol-parameter-selection";
  const PROTOCOL_PARAMS_EVENT = "dola-protocol-parameter-selection";
  const PROTOCOL_BATCH_KEY = "dola_protocol_batch_count_v1";
  const PROTOCOL_BATCH_ATTRIBUTE = "data-dola-protocol-batch-count";
  const PROTOCOL_MODE_KEY = "dola_protocol_mode_v1";
  const PROTOCOL_MODE_ATTRIBUTE = "data-dola-protocol-mode";
  let mediaReport = null;
  const MINIMIZED_KEY = "intl_doubao_media_panel_minimized_v1";
  const items = new Map();

  if (globalThis[INSTANCE_KEY]) {
    return;
  }
  globalThis[INSTANCE_KEY] = true;

  const existingHost = document.getElementById(PANEL_ID);
  existingHost?.remove();

  const host = document.createElement("div");
  host.id = PANEL_ID;
  forceHostVisible();
  mountHost();

  const shadow = host.attachShadow({ mode: "open" });
  const LOGO_URL = chrome.runtime.getURL("icons/logo96.png");
  shadow.innerHTML = `
    <style>
      :host {
        all: initial;
        position: fixed;
        right: 16px;
        bottom: 16px;
        z-index: 2147483647;
        font-family: "Microsoft YaHei UI", "Microsoft YaHei", Arial, sans-serif;
      }

      .panel {
        width: 340px;
        max-height: min(640px, calc(100vh - 32px));
        box-sizing: border-box;
        display: flex;
        flex-direction: column;
        color: #34304B;
        background: #FFFFFF;
        border: 1px solid rgba(122, 92, 240, 0.16);
        border-radius: 14px;
        box-shadow: 0 16px 44px rgba(122, 92, 240, 0.28);
        overflow-x: hidden;
        overflow-y: auto;
      }

      /* ===== 品牌头部：苏生阁渐变 ===== */
      .header {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 8px 10px 8px 12px;
        background: linear-gradient(100deg, #7A5CF0 0%, #B46FE0 55%, #5AA9F0 100%);
      }

      .logo {
        width: 22px;
        height: 22px;
        min-width: 22px;
        border-radius: 6px;
        object-fit: contain;
        display: block;
      }

      .title {
        flex: 1;
        min-width: 0;
        color: #fff;
        font-size: 13px;
        line-height: 20px;
        font-weight: 700;
        letter-spacing: 0.2px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .count {
        min-width: 24px;
        height: 20px;
        padding: 0 7px;
        border-radius: 999px;
        background: rgba(255, 255, 255, 0.92);
        color: #6A4BD8;
        font-size: 12px;
        line-height: 20px;
        font-weight: 700;
        text-align: center;
      }

      .panel-toggle {
        width: 26px;
        min-width: 26px;
        height: 26px;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        padding: 0;
        border: 0;
        border-radius: 8px;
        background: rgba(255, 255, 255, 0.22);
        color: #fff;
        font-size: 16px;
        line-height: 1;
        cursor: pointer;
        transition: background 0.15s ease;
      }

      .panel-toggle:hover {
        background: rgba(255, 255, 255, 0.38);
      }

      .panel-toggle-icon {
        display: block;
        line-height: 16px;
      }

      /* ===== 30 秒模式行：iOS 风格开关 ===== */
      .duration-control {
        min-height: 44px;
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 0 12px;
        background: #F8F5FE;
        border-bottom: 1px solid #EFE8FB;
        color: #34304B;
        font-size: 12px;
      }

      .dur-tag {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 999px;
        background: linear-gradient(100deg, #7A5CF0, #B46FE0);
        color: #fff;
        font-size: 11px;
        font-weight: 700;
        line-height: 16px;
        white-space: nowrap;
      }

      .dur-label {
        flex: 1;
        min-width: 0;
        color: #6E6A88;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }

      .switch {
        position: relative;
        display: inline-block;
        width: 38px;
        height: 22px;
        min-width: 38px;
        cursor: pointer;
        -webkit-user-select: none;
        user-select: none;
      }

      .switch input {
        position: absolute;
        width: 0;
        height: 0;
        opacity: 0;
        margin: 0;
      }

      .switch .track {
        position: absolute;
        inset: 0;
        border-radius: 999px;
        background: #D8D2E8;
        transition: background 0.18s ease;
      }

      .switch .thumb {
        position: absolute;
        top: 2px;
        left: 2px;
        width: 18px;
        height: 18px;
        border-radius: 50%;
        background: #fff;
        box-shadow: 0 1px 3px rgba(52, 48, 75, 0.3);
        transition: transform 0.18s ease;
      }

      .switch input:checked + .track {
        background: linear-gradient(100deg, #3FBF7F, #2FBF71);
      }

      .switch input:checked + .track .thumb {
        transform: translateX(16px);
      }

      .duration-state {
        color: #8A86A3;
        font-size: 11px;
        font-variant-numeric: tabular-nums;
        white-space: nowrap;
      }

      .duration-state.enabled {
        color: #2E9E5B;
        font-weight: 700;
      }

      .resolution-control, .model-control, .auto-confirm-control, .generation-intent-control, .debugger-control, .two-part-control, .protocol-send-control {
        display: flex;
        flex-wrap: wrap;
        align-items: center;
        gap: 6px;
        padding: 10px 12px;
        background: #F8F5FE;
        border-bottom: 1px solid #EFE8FB;
        color: #34304B;
        font-size: 12px;
      }
      .resolution-label { flex: 1; }
      .resolution-select, .resolution-apply, .resolution-diagnostics, .model-select, .protocol-param-select {
        border: 1px solid #D8D2E8;
        border-radius: 6px;
        background: #fff;
        color: #34304B;
        padding: 4px 6px;
        font: inherit;
      }
      .resolution-apply, .resolution-diagnostics { cursor: pointer; }
      .panel > div, .panel > section { flex-shrink: 0; }
      .resolution-state, .model-state { width: 100%; color: #6E6A88; font-size: 11px; line-height: 1.5; }
      .protocol-send-button { border:0;border-radius:8px;background:linear-gradient(100deg,#7A5CF0,#B46FE0);color:#fff;padding:7px 14px;font:inherit;font-weight:700;cursor:pointer; }
      .protocol-send-button:disabled { opacity:.58;cursor:wait; }
      .protocol-send-state { width:100%;color:#6E6A88;font-size:11px;line-height:1.5;overflow-wrap:anywhere; }
      .protocol-param-grid { width:100%;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:6px; }
      .protocol-param { display:grid;grid-template-columns:auto minmax(0,1fr);align-items:center;gap:5px;color:#6E6A88;font-size:11px; }
      .protocol-param-select { width:100%;min-width:0;padding:5px 4px; }
      .generation-control { padding: 9px 12px; border-bottom: 1px solid #EFE8FB; color: #6E6A88; font-size: 11px; line-height: 1.5; }
      .generation-state { display: block; color: #614C27; }
      .generation-fields { display: block; max-height: 4.5em; overflow: auto; overflow-wrap: anywhere; }
      .media-control { display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid #EFE8FB;font-size:11px;color:#6E6A88; }
      .media-state { flex:1;min-width:0; }
      .media-retry { border:1px solid #D8D2E8;border-radius:6px;background:#fff;color:#34304B;padding:4px 6px;font:inherit;cursor:pointer; }
      .quality-note { grid-column:1/-1;color:#6E6A88;font-size:11px;line-height:1.5;overflow-wrap:anywhere; }

      /* ===== 资源列表 ===== */
      .list {
        min-height: 78px;
        max-height: 356px;
        overflow: auto;
        padding: 8px;
        background: #FFFFFF;
      }

      .empty {
        padding: 20px 10px;
        color: #A79FBE;
        font-size: 13px;
        line-height: 20px;
        text-align: center;
      }

      .item {
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto auto;
        gap: 8px;
        align-items: center;
        padding: 7px 8px;
        border: 1px solid #EAE3F7;
        border-radius: 10px;
        background: #FFFFFF;
      }

      .item + .item {
        margin-top: 6px;
      }

      .download-note {
        grid-column: 1 / -1;
        color: #6E6A88;
        font-size: 11px;
        line-height: 16px;
        overflow-wrap: anywhere;
      }

      .label {
        min-width: 0;
        color: #34304B;
        font-size: 13px;
        line-height: 18px;
        font-weight: 600;
        display: flex;
        align-items: center;
        gap: 6px;
      }

      .label .index {
        color: #B3AACB;
        font-size: 11px;
        font-weight: 400;
        font-variant-numeric: tabular-nums;
      }

      .tag {
        display: inline-block;
        min-width: 32px;
        padding: 1px 6px;
        border-radius: 999px;
        color: #fff;
        font-size: 11px;
        font-weight: 700;
        line-height: 16px;
        text-align: center;
      }

      .tag.video {
        background: linear-gradient(100deg, #7A5CF0, #9A7BF4);
      }

      .tag.image {
        background: linear-gradient(100deg, #14B8A6, #2BB3A3);
      }

      button.dl {
        height: 28px;
        padding: 0 14px;
        border: 0;
        border-radius: 8px;
        background: linear-gradient(100deg, #7A5CF0, #9A7BF4);
        color: #fff;
        font-size: 12px;
        font-weight: 600;
        line-height: 28px;
        cursor: pointer;
        transition: filter 0.15s ease;
      }

      button.dl:hover {
        filter: brightness(1.08);
      }

      button.dl:disabled {
        opacity: 0.65;
        cursor: wait;
      }

      button.save {
        height: 28px;
        padding: 0 7px;
        border: 1px solid #DCD2F5;
        border-radius: 8px;
        background: #fff;
        color: #6A4BD8;
        font-size: 11px;
        cursor: pointer;
      }

      button.save:disabled {
        opacity: 0.5;
        cursor: wait;
      }

      /* ===== 最小化 ===== */
      :host([data-minimized="1"]) {
        right: 0 !important;
        bottom: 0 !important;
      }

      :host([data-minimized="1"]) .panel {
        width: 40px;
        height: 40px;
        min-height: 40px;
        max-height: 40px;
        border-radius: 12px;
        border: 0;
        background: linear-gradient(120deg, #7A5CF0, #5AA9F0);
        box-shadow: 0 10px 26px rgba(122, 92, 240, 0.35);
      }

      :host([data-minimized="1"]) .header {
        width: 40px;
        height: 40px;
        padding: 0;
        background: transparent;
        border: 0;
        justify-content: center;
      }

      :host([data-minimized="1"]) .logo,
      :host([data-minimized="1"]) .title,
      :host([data-minimized="1"]) .count,
      :host([data-minimized="1"]) .duration-control,
      :host([data-minimized="1"]) .resolution-control,
      :host([data-minimized="1"]) .model-control,
      :host([data-minimized="1"]) .auto-confirm-control,
      :host([data-minimized="1"]) .generation-intent-control,
      :host([data-minimized="1"]) .two-part-control,
      :host([data-minimized="1"]) .protocol-send-control,
      :host([data-minimized="1"]) .debugger-control,
      :host([data-minimized="1"]) .generation-control,
      :host([data-minimized="1"]) .media-control,
      :host([data-minimized="1"]) .list {
        display: none;
      }

      :host([data-minimized="1"]) .panel-toggle {
        width: 40px;
        height: 40px;
        border-radius: 12px;
        background: transparent;
        color: #fff;
        font-size: 20px;
      }

      :host([data-minimized="1"]) .panel-toggle:hover {
        background: rgba(255, 255, 255, 0.2);
      }
    </style>
    <section class="panel" aria-label="苏生阁AI 无水印资源面板">
      <div class="header">
        <img class="logo" src="${LOGO_URL}" alt="苏生阁AI">
        <div class="title">苏生阁AI · 无水印助手</div>
        <div class="count">0</div>
        <button class="panel-toggle" type="button" aria-label="最小化资源面板" title="最小化资源面板">
          <span class="panel-toggle-icon" aria-hidden="true">−</span>
        </button>
      </div>
      <div class="duration-control">
        <span class="dur-tag">30秒</span>
        <span class="dur-label">视频时长偏好</span>
        <label class="switch">
          <input class="duration-toggle" type="checkbox">
          <span class="track"><span class="thumb"></span></span>
        </label>
        <span class="duration-state">未开启</span>
      </div>
      <div class="resolution-control">
        <label class="resolution-label" for="resolution-default">分辨率</label>
        <select class="resolution-select" id="resolution-default">
          <option value="auto">跟随页面</option>
          <option value="480p">480P（2.5）</option>
          <option value="720p">720P</option>
          <option value="1080p">1080P</option>
        </select>
        <button class="resolution-apply" type="button">刷新配置</button>
        <button class="resolution-diagnostics" type="button">诊断</button>
        <span class="resolution-state" role="status" aria-live="polite">等待分辨率配置…</span>
      </div>
      <div class="model-control">
        <label class="resolution-label" for="video-model-default">国内模型</label>
        <select class="model-select" id="video-model-default">
          <option value="auto">跟随页面</option>
          <option value="seedance_v2.5">Seedance 2.5</option>
          <option value="seedance_v2.0_std">Seedance 2.0</option>
          <option value="seedance_v2.0">Seedance 2.0 Fast</option>
          <option value="seedance_v2.0_mini">Seedance 2.0 Mini</option>
        </select>
        <span class="model-state" role="status" aria-live="polite">等待原生模型状态…</span>
      </div>
      <div class="protocol-send-control">
        <span class="resolution-label">Dola 原生协议链路</span>
        <button class="protocol-send-button protocol-expert-send-button" type="button">专家模式协议</button>
        <button class="protocol-send-button protocol-quick-send-button" type="button">快速模式协议</button>
        <div class="protocol-param-grid">
          <label class="protocol-param">模型
            <select class="protocol-param-select protocol-model">
              <option value="seedance_v2.5">Seedance 2.5</option>
              <option value="seedance_v2.0_std">Seedance 2.0</option>
              <option value="seedance_v2.0" selected>Seedance 2.0 Fast</option>
              <option value="seedance_v2.0_mini">Seedance 2.0 Mini</option>
            </select>
          </label>
          <label class="protocol-param">秒数
            <select class="protocol-param-select protocol-duration">
              <option value="5">5 秒</option>
              <option value="10">10 秒</option>
              <option value="15">15 秒</option>
              <option value="30" selected>30 秒</option>
            </select>
          </label>
          <label class="protocol-param">尺寸
            <select class="protocol-param-select protocol-ratio">
              <option value="1:1">1:1</option>
              <option value="3:4">3:4</option>
              <option value="4:3">4:3</option>
              <option value="9:16">9:16</option>
              <option value="16:9" selected>16:9</option>
              <option value="21:9">21:9</option>
            </select>
          </label>
          <label class="protocol-param">分辨率
            <select class="protocol-param-select protocol-resolution">
              <option value="480p">480P</option>
              <option value="720p" selected>720P</option>
              <option value="1080p">1080P</option>
            </select>
          </label>
          <label class="protocol-param">发送数量
            <input class="protocol-param-select protocol-batch-count" type="number" min="1" max="20" step="1" value="1" inputmode="numeric">
          </label>
        </div>
        <span class="protocol-send-state" role="status" aria-live="polite">专家协议走 Agent 工具链；快速协议保留快速路由并尝试写入原生 Fast/15 参数</span>
      </div>
      <div class="media-control">
        <span class="media-state">等待捕获视频接口…</span>
        <button class="media-retry" type="button">重试提取</button>
      </div>
      <div class="generation-intent-control">
        <label class="resolution-label" for="generation-intent-enabled">国内生成指令前缀</label>
        <input id="generation-intent-enabled" class="generation-intent-toggle" type="checkbox">
        <span class="generation-intent-state resolution-state" role="status" aria-live="polite">等待视频生成提交</span>
      </div>
      <div class="auto-confirm-control">
        <label class="resolution-label" for="auto-confirm-enabled">国内自动续确认 · 每轮最多一次</label>
        <input id="auto-confirm-enabled" class="auto-confirm-toggle" type="checkbox">
        <span class="auto-confirm-state resolution-state" role="status" aria-live="polite">等待本轮手动提交的视频请求</span>
      </div>
      <div class="two-part-control">
        <label class="resolution-label" for="two-part-enabled">两段式规则 · 2 × 15 秒</label>
        <input id="two-part-enabled" class="two-part-toggle" type="checkbox">
        <span class="two-part-state resolution-state" role="status" aria-live="polite"></span>
        <details><summary>查看追加规则</summary><div class="two-part-rule"></div></details>
      </div>
      <div class="debugger-control">
        <span class="resolution-label">Debugger 网络调试</span>
        <button class="debugger-toggle resolution-apply" type="button">等待状态</button>
        <button class="debugger-clear resolution-apply" type="button">清空</button>
        <button class="debugger-export resolution-apply" type="button">导出调试</button>
        <span class="debugger-state resolution-state" role="status" aria-live="polite">正在读取浏览器调试状态…</span>
      </div>
      <div class="generation-control" role="status" aria-live="polite">
        <strong>最近一次提交响应</strong>
        <span class="generation-state">尚无提交响应记录；未验证视频任务</span>
        <span class="generation-fields"></span>
      </div>
      <div class="list">
        <div class="empty">等待捕获资源…</div>
      </div>
    </section>
  `;

  const list = shadow.querySelector(".list");
  const count = shadow.querySelector(".count");
  const durationToggle = shadow.querySelector(".duration-toggle");
  const durationState = shadow.querySelector(".duration-state");
  const durationControl = shadow.querySelector(".duration-control");
  const resolutionControl = shadow.querySelector(".resolution-control");
  const resolutionSelect = shadow.querySelector(".resolution-select");
  const resolutionState = shadow.querySelector(".resolution-state");
  const modelControl = shadow.querySelector(".model-control");
  const modelSelect = shadow.querySelector(".model-select");
  const modelState = shadow.querySelector(".model-state");
  const protocolSendControl = shadow.querySelector(".protocol-send-control");
  const protocolExpertSendButton = shadow.querySelector(".protocol-expert-send-button");
  const protocolQuickSendButton = shadow.querySelector(".protocol-quick-send-button");
  const protocolSendButtons = [protocolExpertSendButton, protocolQuickSendButton];
  const protocolSendState = shadow.querySelector(".protocol-send-state");
  const protocolModel = shadow.querySelector(".protocol-model");
  const protocolDuration = shadow.querySelector(".protocol-duration");
  const protocolRatio = shadow.querySelector(".protocol-ratio");
  const protocolResolution = shadow.querySelector(".protocol-resolution");
  const protocolBatchCount = shadow.querySelector(".protocol-batch-count");
  const generationControl = shadow.querySelector(".generation-control");
  const generationState = shadow.querySelector(".generation-state");
  const generationFields = shadow.querySelector(".generation-fields");
  const autoConfirmControl = shadow.querySelector(".auto-confirm-control");
  const autoConfirmToggle = shadow.querySelector(".auto-confirm-toggle");
  const autoConfirmState = shadow.querySelector(".auto-confirm-state");
  const generationIntentControl = shadow.querySelector(".generation-intent-control");
  const generationIntentToggle = shadow.querySelector(".generation-intent-toggle");
  const generationIntentState = shadow.querySelector(".generation-intent-state");
  const debuggerControl = shadow.querySelector(".debugger-control");
  const debuggerToggle = shadow.querySelector(".debugger-toggle");
  const debuggerState = shadow.querySelector(".debugger-state");
  let debuggerStatus = null;
  let debuggerChannelFailed = false;
  const twoPartControl = shadow.querySelector(".two-part-control");
  const twoPartToggle = shadow.querySelector(".two-part-toggle");
  const twoPartState = shadow.querySelector(".two-part-state");
  const mediaState = shadow.querySelector(".media-state");
  let resolutionPending = false;
  const panelToggle = shadow.querySelector(".panel-toggle");
  const panelToggleIcon = shadow.querySelector(".panel-toggle-icon");
  const durationControlSupported = /(^|\.)(doubao|dola)\.com$/i.test(location.hostname);
  let currentSourceKey = "";
  let statusText = "等待捕获资源";
  let minimized = false;

  // MAIN-world automation cannot manufacture a trusted pointer event. Bridge
  // the exact screen coordinate to the extension worker, which uses CDP's
  // Input domain. This is limited to the current Dola tab and only runs after
  // the user pressed the panel's protocol button.
  document.addEventListener("dola-trusted-click-request", async () => {
    let request = null;
    try { request = JSON.parse(document.documentElement?.getAttribute("data-dola-trusted-click-request") || "null"); }
    catch { /* The MAIN-world request was incomplete. */ }
    if (!request?.id || !Number.isFinite(Number(request.x)) || !Number.isFinite(Number(request.y))) return;
    let result;
    try {
      result = await chrome.runtime.sendMessage({ type: "DOLA_TRUSTED_CLICK", id: request.id,
        x: Number(request.x), y: Number(request.y) });
    } catch (error) {
      result = { ok: false, error: error?.message || String(error) };
    }
    document.documentElement?.setAttribute("data-dola-trusted-click-result", JSON.stringify({
      id: request.id, ok: result?.ok === true, error: String(result?.error || "")
    }));
    document.dispatchEvent(new Event("dola-trusted-click-result"));
  });

  function conversationContext() {
    let id = "new";
    try { id = location.pathname.match(/^\/chat(?:\/([^/?#]+))?/i)?.[1] || "new"; } catch { /* Keep a new-chat scope. */ }
    const source = /(^|\.)dola\.com$/i.test(location.hostname) ? "dola" : /(^|\.)doubao\.com$/i.test(location.hostname) ? "doubao" : "site";
    const title = String(document.title || "").replace(/\s*[-–—]\s*(?:Dola|豆包).*$/i, "").replace(/\s+/g, " ").trim().slice(0, 80);
    return { conversationKey: `${source}:${id}`, conversationId: id, title, capturedAt: Date.now() };
  }

  function readMinimizedState() {
    try {
      return localStorage.getItem(MINIMIZED_KEY) === "1";
    } catch {
      return false;
    }
  }

  function setMinimizedState(nextValue, persist = true) {
    minimized = Boolean(nextValue);
    if (minimized) {
      host.setAttribute("data-minimized", "1");
    } else {
      host.removeAttribute("data-minimized");
    }
    if (persist) {
      try {
        localStorage.setItem(MINIMIZED_KEY, minimized ? "1" : "0");
      } catch {
        // Keep the current visual state when storage is unavailable.
      }
    }
    const label = minimized ? "展开资源面板" : "最小化资源面板";
    panelToggle.setAttribute("aria-label", label);
    panelToggle.setAttribute("title", label);
    panelToggleIcon.textContent = minimized ? "＋" : "−";
  }

  setMinimizedState(readMinimizedState(), false);
  panelToggle.addEventListener("click", () => {
    setMinimizedState(!minimized);
  });

  function protocolParameterSelection() {
    return {
      model: protocolModel.value,
      duration: Number(protocolDuration.value),
      ratio: protocolRatio.value,
      resolution: protocolResolution.value
    };
  }

  function publishProtocolParameterSelection(persist = true) {
    const selection = protocolParameterSelection();
    if (persist) {
      try { localStorage.setItem(PROTOCOL_PARAMS_KEY, JSON.stringify(selection)); }
      catch { /* The DOM bridge still works when storage is unavailable. */ }
    }
    document.documentElement?.setAttribute(PROTOCOL_PARAMS_ATTRIBUTE, JSON.stringify(selection));
    document.dispatchEvent(new Event(PROTOCOL_PARAMS_EVENT));
    const batchCount = Math.max(1, Math.min(20, Math.trunc(Number(protocolBatchCount?.value || 1)) || 1));
    if (protocolBatchCount) protocolBatchCount.value = String(batchCount);
    document.documentElement?.setAttribute(PROTOCOL_BATCH_ATTRIBUTE, String(batchCount));
    if (persist) {
      try { localStorage.setItem(PROTOCOL_BATCH_KEY, String(batchCount)); } catch { /* Keep the DOM value. */ }
    }
    return selection;
  }

  function setProtocolMode(mode, persist = true) {
    const value = mode === "quick" ? "quick" : "expert";
    document.documentElement?.setAttribute(PROTOCOL_MODE_ATTRIBUTE, value);
    if (persist) {
      try { localStorage.setItem(PROTOCOL_MODE_KEY, value); } catch { /* The DOM bridge remains authoritative. */ }
    }
    return value;
  }

  function restoreProtocolParameterSelection() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem(PROTOCOL_PARAMS_KEY) || "null"); } catch { /* Use safe defaults. */ }
    const prepared = (() => {
      try { return JSON.parse(document.documentElement?.getAttribute("data-dola-protocol-prepared-context") || "null"); }
      catch { return null; }
    })();
    const source = saved && typeof saved === "object" ? saved : prepared && typeof prepared === "object" ? prepared : {};
    const assign = (select, value) => {
      const option = [...select.options].find((item) => item.value === String(value ?? ""));
      if (option) select.value = option.value;
    };
    assign(protocolModel, source.model);
    assign(protocolDuration, source.duration);
    assign(protocolRatio, source.ratio);
    assign(protocolResolution, source.resolution);
    let batchCount = 1;
    try { batchCount = Number(localStorage.getItem(PROTOCOL_BATCH_KEY) || 1); } catch { /* Use one request. */ }
    protocolBatchCount.value = String(Math.max(1, Math.min(20, Math.trunc(batchCount) || 1)));
    let mode = "expert";
    try { mode = localStorage.getItem(PROTOCOL_MODE_KEY) === "quick" ? "quick" : "expert"; } catch { /* Expert is the safe default. */ }
    setProtocolMode(mode, false);
    publishProtocolParameterSelection(false);
  }

  function syncProtocolSend() {
    let status = null;
    try { status = JSON.parse(document.documentElement?.getAttribute(PROTOCOL_SEND_STATUS_ATTRIBUTE) || "null"); } catch { /* Wait for a complete snapshot. */ }
    const running = ["validating", "awaiting_native_media", "awaiting_http_evidence", "awaiting_conversation", "batch_advancing"].includes(status?.phase);
    for (const button of protocolSendButtons) button.disabled = running;
    protocolBatchCount.disabled = running;
    const progress = status?.batch && Number(status.batch.total || 0) > 1
      ? `${Number(status.batch.completed || 0)}/${Number(status.batch.total || 0)}` : "";
    const activeMode = status?.protocolMode || document.documentElement?.getAttribute(PROTOCOL_MODE_ATTRIBUTE) || "expert";
    protocolExpertSendButton.textContent = running && activeMode === "expert" ? (progress ? `专家 ${progress}…` : "专家校验中…") : "专家模式协议";
    protocolQuickSendButton.textContent = running && activeMode === "quick" ? (progress ? `快速 ${progress}…` : "快速校验中…") : "快速模式协议";
    if (!status) {
      protocolSendState.textContent = "协议模块未加载，请重新加载扩展并刷新页面";
      return;
    }
    const snap = status.snapshot;
    const media = snap?.media?.counts;
    const summary = snap ? `；已读取 ${media?.image || 0} 图/${media?.video || 0} 视频，${snap.model || "模型未知"}，${snap.duration || "?"}s，${snap.ratio || "尺寸未知"}，${snap.resolution || "分辨率未知"}` : "";
    protocolSendState.textContent = `${status.reason}${summary}`;
  }

  if (/(^|\.)dola\.com$/i.test(location.hostname)) {
    restoreProtocolParameterSelection();
    for (const select of [protocolModel, protocolDuration, protocolRatio, protocolResolution, protocolBatchCount]) {
      select.addEventListener("change", () => {
        publishProtocolParameterSelection(true);
        protocolSendState.textContent = "参数已保存；专家协议走 Agent，快速协议仅在原生能力字段存在时注入参数";
      });
    }
    const sendProtocol = (mode) => {
      setProtocolMode(mode, true);
      publishProtocolParameterSelection(true);
      const requestId = `${Date.now()}-${Math.random().toString(36).slice(2, 9)}`;
      document.documentElement?.setAttribute(PROTOCOL_SEND_REQUEST_ATTRIBUTE, requestId);
      for (const button of protocolSendButtons) button.disabled = true;
      protocolSendState.textContent = mode === "quick"
        ? "正在校验快速模式；保留 agent_mode=2，并尝试注入 Fast/15 原生参数…"
        : "正在读取当前草稿并校验专家模式…";
      document.dispatchEvent(new Event(PROTOCOL_SEND_REQUEST_EVENT));
    };
    protocolExpertSendButton.addEventListener("click", () => sendProtocol("expert"));
    protocolQuickSendButton.addEventListener("click", () => sendProtocol("quick"));
    document.addEventListener(PROTOCOL_SEND_STATUS_EVENT, syncProtocolSend);
    syncProtocolSend();
  } else protocolSendControl.remove();
  if (durationControlSupported) {
    twoPartToggle.addEventListener("change", () => {
      document.documentElement?.setAttribute("data-dola-two-part-enabled", twoPartToggle.checked ? "1" : "0");
      document.dispatchEvent(new Event("dola-two-part-change"));
      syncTwoPart();
    });
    document.addEventListener("dola-two-part-status", syncTwoPart);
    syncTwoPart();
    syncDurationControl();
    durationToggle.addEventListener("change", () => {
      setDuration30Enabled(durationToggle.checked);
    });
    document.addEventListener(DURATION_STATE_EVENT, syncDurationControl);
    syncResolutionControl();
    resolutionSelect.addEventListener("change", () => {
      try {
        localStorage.setItem(RESOLUTION_KEY, resolutionSelect.value);
      } catch {
        resolutionState.textContent = "无法保存分辨率默认值";
        return;
      }
      resolutionPending = true;
      document.documentElement?.setAttribute(RESOLUTION_PREFERENCE_ATTRIBUTE, resolutionSelect.value);
      document.dispatchEvent(new Event(RESOLUTION_PREFERENCE_EVENT));
      syncResolutionControl();
    });
    shadow.querySelector(".resolution-apply").addEventListener("click", () => location.reload());
    shadow.querySelector(".resolution-diagnostics").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      try {
        let debuggerNetwork = debuggerStatus;
        try { debuggerNetwork = await requestDebugger("DEBUGGER_REPORT"); }
        catch (error) { debuggerNetwork = { ...debuggerNetwork, reportError: error.message }; }
        const diagnostic = { ...localDiagnostics(), debuggerNetwork };
        await navigator.clipboard.writeText(JSON.stringify(diagnostic, null, 2));
        button.textContent = "已复制";
      } catch {
        resolutionState.textContent = "请在网页控制台读取 __DOUBAO_DOLA_RESOLUTION_BRIDGE__.status";
      }
    });
    document.addEventListener(RESOLUTION_STATUS_EVENT, syncResolutionControl);
    document.addEventListener(RESOLUTION_BRIDGE_EVENT, syncResolutionControl);
    document.addEventListener(GENERATION_EVENT, syncGenerationEvidence);
    syncGenerationEvidence();
    debuggerToggle.addEventListener("click", async () => {
      debuggerToggle.disabled = true;
      try {
        // Reconnecting reads status only; never silently attach or stop capture.
        renderDebugger(await requestDebugger(debuggerChannelFailed ? "DEBUGGER_STATUS" : "DEBUGGER_CONTROL",
          debuggerChannelFailed ? {} : { enabled: debuggerStatus?.phase === "attach_error" || !(debuggerStatus?.enabled || debuggerStatus?.attached) }));
      }
      catch (error) { renderDebuggerError(error); }
      finally { debuggerToggle.disabled = false; }
    });
    shadow.querySelector(".debugger-clear").addEventListener("click", async () => {
      try { renderDebugger(await requestDebugger("DEBUGGER_CLEAR")); } catch (error) { renderDebuggerError(error); }
    });
    shadow.querySelector(".debugger-export").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      const label = button.textContent;
      button.textContent = "导出中…";
      try {
        const result = await requestDebugger("DEBUGGER_EXPORT", { localDiagnostics: localDiagnostics() });
        renderDebugger(result);
        button.textContent = "已导出";
      } catch (error) {
        renderDebuggerError(error);
        button.textContent = "导出失败";
      } finally {
        setTimeout(() => { button.textContent = label; button.disabled = false; }, 1500);
      }
    });
    requestDebugger("DEBUGGER_STATUS").then(renderDebugger).catch(renderDebuggerError);
  } else {
    twoPartControl.remove();
    durationControl.remove();
    resolutionControl.remove();
    generationControl.remove();
    debuggerControl.remove();
  }

  if (/(^|\.)doubao\.com$/i.test(location.hostname)) {
    modelSelect.addEventListener("change", () => {
      document.documentElement?.setAttribute(MODEL_PREFERENCE_ATTRIBUTE, modelSelect.value);
      document.dispatchEvent(new Event(MODEL_PREFERENCE_EVENT));
      syncModelControl();
    });
    document.addEventListener(MODEL_STATUS_EVENT, syncModelControl);
    document.addEventListener(RESOLUTION_STATUS_EVENT, syncModelControl);
    syncModelControl();
    generationIntentToggle.addEventListener("change", () => {
      document.documentElement?.setAttribute(GENERATION_INTENT_PREF_ATTRIBUTE, generationIntentToggle.checked ? "1" : "0");
      document.dispatchEvent(new Event(GENERATION_INTENT_PREF_EVENT));
    });
    document.addEventListener(GENERATION_INTENT_EVENT, syncGenerationIntent);
    syncGenerationIntent();
    autoConfirmToggle.addEventListener("change", () => {
      document.documentElement?.setAttribute(AUTO_CONFIRM_PREF_ATTRIBUTE, autoConfirmToggle.checked ? "1" : "0");
      document.dispatchEvent(new Event(AUTO_CONFIRM_PREF_EVENT));
    });
    document.addEventListener(AUTO_CONFIRM_EVENT, syncAutoConfirmation);
    syncAutoConfirmation();
  } else { modelControl.remove(); autoConfirmControl.remove(); generationIntentControl.remove(); }

  shadow.querySelector(".media-retry").addEventListener("click", () => {
    mediaReport = null;
    document.dispatchEvent(new Event(MEDIA_RETRY_EVENT));
    syncMediaCaptureState();
  });
  document.addEventListener(MEDIA_CAPTURE_EVENT, syncMediaCaptureState);
  syncMediaCaptureState();

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message) {
      return;
    }

    if (message.type === "DEBUGGER_STATUS") { renderDebugger(message.status); return; }

    if (message.type === "PANEL_PING") {
      sendResponse({ ready: true });
      return;
    }

    if (message.type === "MEDIA_STATUS" && typeof message.text === "string") {
      if (message.context?.conversationKey && message.context.conversationKey !== conversationContext().conversationKey) return;
      resetForSource(message.sourceKey);
      if (!items.size) {
        statusText = message.text;
      }
      render();
      return;
    }

    if (message.type === "MEDIA_FOUND" && Array.isArray(message.items)) {
      if (message.context?.conversationKey && message.context.conversationKey !== conversationContext().conversationKey) return;
      resetForSource(message.sourceKey);
      addItems(message.items);
      statusText = items.size ? "" : "未提取到资源";
      render();
      return;
    }

    if (message.type === "MEDIA_RESET") {
      currentSourceKey = typeof message.sourceKey === "string" ? message.sourceKey : "";
      items.clear();
      directSeen.clear();
      scheduleDirectScan();
      statusText = "";
      mediaReport = null;
      syncMediaCaptureState();
      render();
      return;
    }

  });

  // Direct playback URLs are downloadable resources, never proof of an unmarked original.
  let directScanTimer = null;
  let directPage = location.href;
  let directConversationKey = typeof conversationContext === "function" ? conversationContext().conversationKey : location.href;
  const directSeen = new Set();
  const markedResolveSeen = new Set();
  function resetConversationScope() {
    const current = typeof conversationContext === "function" ? conversationContext() : { conversationKey: location.href };
    if (directConversationKey === current.conversationKey && directPage === location.href) return false;
    directConversationKey = current.conversationKey;
    directPage = location.href;
    if (typeof currentSourceKey !== "undefined") currentSourceKey = "";
    directSeen.clear();
    markedResolveSeen.clear();
    items.clear();
    if (typeof mediaReport !== "undefined") mediaReport = null;
    if (typeof statusText !== "undefined") statusText = "等待捕获当前会话资源…";
    return true;
  }
  function scanDirectVideos() {
    directScanTimer = null;
    const pageChanged = resetConversationScope();
    const candidates = [];
    try {
      const batch = JSON.parse(document.documentElement?.getAttribute("data-dola-direct-videos") || "null");
      if (batch?.page === location.href && Array.isArray(batch.items)) candidates.push(...batch.items.slice(0, 32));
    } catch { /* Wait for complete metadata. */ }
    for (const link of document.querySelectorAll('a[href]')) candidates.push({ url: link.href });
    const fresh = [];
    for (const candidate of candidates) {
      const item = globalThis.DolaDirectVideo?.resource(candidate?.url);
      if (!item || directSeen.has(item.url)) continue;
      directSeen.add(item.url);
      if (directSeen.size > 256) directSeen.delete(directSeen.values().next().value);
      fresh.push(item);
    }
    if (fresh.length) {
      // Marked page playback links are resolver inputs, never download
      // results.  Keeping them in the list lets users download the exact
      // watermarked rendition we are supposed to replace.
      addItems(fresh.filter((item) => item.watermark !== "marked"));
      statusText = ""; render();
      resolveMarkedDirectVideos(fresh);
    }
    if (pageChanged && !fresh.length) render();
    syncMediaCaptureState();
  }
  function resolveMarkedDirectVideos(fresh) {
    if (!/(^|\.)dola\.com$/i.test(location.hostname)) return;
    const urls = fresh.filter((item) => item?.watermark === "marked" && !markedResolveSeen.has(item.url))
      .map((item) => item.url);
    if (!urls.length) return;
    urls.forEach((url) => markedResolveSeen.add(url));
    let resolution = "auto";
    try { resolution = localStorage.getItem(RESOLUTION_KEY) || "auto"; } catch { /* Prefer highest official source. */ }
    const context = conversationContext();
    chrome.runtime.sendMessage({ type: "RESOLVE_MARKED_VIDEO_URLS", urls, resolution, context }, (response) => {
      if (chrome.runtime.lastError || response?.ok === false) {
        mediaReport = { candidateCount: urls.length, count: 0, failures: ["DOLA_UNWATERMARKED_REQUEST_FAILED"], qualities: [] };
      } else {
        mediaReport = { candidateCount: response.candidateCount || urls.length, count: response.count || 0,
          failures: Array.isArray(response.failures) ? response.failures : [],
          qualities: Array.isArray(response.qualities) ? response.qualities : [] };
      }
      if (!mediaReport.count) statusText = mediaFailureText(mediaReport.failures[0]);
      syncMediaCaptureState();
      render();
    });
  }
  function scheduleDirectScan() {
    if (directScanTimer === null) directScanTimer = setTimeout(scanDirectVideos, 150);
  }
  document.addEventListener("dola-direct-videos", scheduleDirectScan);
  document.addEventListener(MEDIA_RETRY_EVENT, () => { directSeen.clear(); markedResolveSeen.clear(); scheduleDirectScan(); });
  document.addEventListener("DOMContentLoaded", scheduleDirectScan);
  const directObserver = new MutationObserver(scheduleDirectScan);
  directObserver.observe(document.documentElement, { subtree: true, childList: true,
    attributes: true, attributeFilter: ["href", "data-dola-direct-videos"] });
  scheduleDirectScan();
  if (typeof setInterval === "function") setInterval(scheduleDirectScan, 500);

  function consumeFallbackApis() {
    const raw = document.documentElement?.getAttribute(MEDIA_APIS_ATTRIBUTE) || "";
    if (!raw) return;
    document.documentElement?.removeAttribute(MEDIA_APIS_ATTRIBUTE);
    const force = document.documentElement?.getAttribute(MEDIA_FORCE_ATTRIBUTE) === "1";
    document.documentElement?.removeAttribute(MEDIA_FORCE_ATTRIBUTE);
    try {
      const captured = JSON.parse(raw);
      if (!Array.isArray(captured) || !captured.length) return;
      const current = conversationContext();
      const scoped = captured.map((entry, index) => {
        if (typeof entry === "string") {
          try {
            const parsed = JSON.parse(entry);
            if (parsed && typeof parsed === "object" && parsed.url) return { ...parsed, index };
          } catch { /* Legacy non-Dola URL. */ }
          return { url: entry, conversationId: /(^|\.)dola\.com$/i.test(location.hostname) ? "" : current.conversationId, index };
        }
        return entry && typeof entry === "object" ? { ...entry, index } : null;
      }).filter((entry) => entry?.url && String(entry.conversationId || "") === current.conversationId)
        .sort((a, b) => (Number(a.order) || a.index) - (Number(b.order) || b.index));
      const urls = scoped.map((entry) => entry.url);
      if (!urls.length) return;
      statusText = "正在解析无水印资源…";
      render();
      const requestStartedAt = Date.now();
      let resolution = "auto";
      try { resolution = localStorage.getItem(RESOLUTION_KEY) || "auto"; } catch { /* Prefer the highest available source. */ }
      const context = current;
      chrome.runtime.sendMessage({ type: "RESOLVE_FALLBACK_APIS", urls, resolution, force, context }, (response) => {
        if (chrome.runtime.lastError) {
          if (!items.size) {
            statusText = "后台解析未完成，请切换会话后重试";
            render();
          }
          return;
        }
        if (response?.ok === false && !items.size) {
          statusText = "无水印资源解析失败";
          render();
        }
        if (response?.ok) {
          mediaReport = { candidateCount: response.candidateCount || urls.length, count: response.count || 0,
            failures: Array.isArray(response.failures) ? response.failures : [],
            qualities: Array.isArray(response.qualities) ? response.qualities : [] };
          if (!mediaReport.count && !items.size) {
            statusText = mediaFailureText(mediaReport.failures[0]);
            render();
          }
          syncMediaCaptureState();
        }
      });
      setTimeout(() => {
        if (!items.size && statusText === "正在解析无水印资源…" && Date.now() - requestStartedAt >= 12000) {
          statusText = "本次未解析到资源，请切换会话后重试";
          render();
        }
      }, 12000);
    } catch {
      // Ignore malformed cross-world payloads.
    }
  }

  document.addEventListener(MEDIA_APIS_EVENT, consumeFallbackApis);
  consumeFallbackApis();

  function consumeVideoModels() {
    const raw = document.documentElement?.getAttribute(MEDIA_MODELS_ATTRIBUTE) || "";
    if (!raw) return;
    document.documentElement?.removeAttribute(MEDIA_MODELS_ATTRIBUTE);
    try {
      const captured = JSON.parse(raw);
      if (!Array.isArray(captured) || !captured.length) return;
      const current = conversationContext();
      const scoped = captured.map((entry, index) => {
        if (typeof entry !== "string") return null;
        try {
          const parsed = JSON.parse(entry);
          if (parsed && typeof parsed === "object" && parsed.model) return { ...parsed, index };
        } catch { /* Ignore unscoped legacy models on Dola. */ }
        return /(^|\.)dola\.com$/i.test(location.hostname) ? null : { model: entry, conversationId: current.conversationId, index };
      }).filter((entry) => entry?.model && String(entry.conversationId || "") === current.conversationId)
        .sort((a, b) => (Number(a.order) || a.index) - (Number(b.order) || b.index));
      const models = scoped.map((entry) => JSON.stringify(entry.model));
      if (!models.length) return;
      statusText = "正在还原 video_model 无水印原片…";
      render();
      let resolution = "auto";
      try { resolution = localStorage.getItem(RESOLUTION_KEY) || "auto"; } catch { /* Prefer the highest available source. */ }
      const context = current;
      chrome.runtime.sendMessage({ type: "RESOLVE_VIDEO_MODELS", models, resolution, context }, (response) => {
        if (chrome.runtime.lastError || response?.ok === false) {
          if (!items.size) { statusText = "video_model 原片地址还原失败"; render(); }
          return;
        }
        if (response?.ok) {
          const nextReport = { candidateCount: response.candidateCount || models.length, count: response.count || 0,
            failures: Array.isArray(response.failures) ? response.failures : [],
            qualities: Array.isArray(response.qualities) ? response.qualities : [] };
          // A marked video_model rejection must not overwrite a successful
          // fallback_api result which arrived first for the same clip.
          if (nextReport.count || !items.size || !mediaReport?.count) mediaReport = nextReport;
          if (!nextReport.count && !items.size) { statusText = mediaFailureText(nextReport.failures[0]); render(); }
          syncMediaCaptureState();
        }
      });
    } catch { /* Ignore malformed cross-world payloads. */ }
  }

  document.addEventListener(MEDIA_MODELS_EVENT, consumeVideoModels);
  consumeVideoModels();

  function mediaFailureText(code) {
    if (/^HTTP_/.test(code || "")) return `视频解析接口返回 ${code.slice(5)}`;
    if (code === "KEY_SEED_MISSING") return "地址需要 key_seed，但接口尚未提供";
    if (code === "URL_DECODE_FAILED") return "视频地址未能还原，请重试提取";
    if (code === "VIDEO_MODEL_IS_WATERMARKED") return "已拒绝带水印的 video_model 播放流，等待底层无水印接口返回";
    if (code === "NO_UNMARKED_VIDEO_CANDIDATE") return "接口未返回可用的无水印候选地址";
    if (code === "PLATFORM_NO_UNWATERMARKED_RESOURCE") return "平台已确认该专家模式成片仅提供带水印版本，未提供无水印原片";
    if (code === "EMBEDDED_VIDEO_ID_NOT_FOUND") return "带水印直链中未找到视频资源标识，无法请求官方原片";
    if (code === "UNWATERMARKED_RESOURCE_MISSING") return "平台标记可去水印，但未返回可下载的无水印原片";
    if (code === "DOLA_UNWATERMARKED_REQUEST_FAILED") return "官方无水印接口请求失败，请重试提取";
    if (/^DOLA_API_/.test(code || "")) return `官方无水印接口返回业务错误 ${code.slice(9)}`;
    if (code === "TIMEOUT") return "视频解析接口超时，请重试提取";
    if (code === "PAGE_CHANGED") return "会话已切换，请重新捕获当前视频";
    return "接口暂未返回完成的视频资源";
  }

  function syncMediaCaptureState() {
    let capture = null;
    try { capture = JSON.parse(document.documentElement?.getAttribute(MEDIA_CAPTURE_ATTRIBUTE) || "null"); } catch { /* Await a complete state snapshot. */ }
    const directCount = [...items.values()].filter((item) => item.provenance === "direct_link").length;
    const context = conversationContext();
    const prefix = context.title ? `当前会话「${context.title}」：` : "当前会话：";
    if (mediaReport?.failures?.[0] === "PLATFORM_NO_UNWATERMARKED_RESOURCE") mediaState.textContent = `${prefix}${mediaFailureText(mediaReport.failures[0])}`;
    else if (directCount && !mediaReport?.count) mediaState.textContent = `${prefix}已识别 ${directCount} 个视频直链，未取得无水印源`;
    else if (mediaReport?.count) mediaState.textContent = `${prefix}已解析 ${items.size - directCount} 个原始视频地址`;
    else if (mediaReport?.failures?.length) mediaState.textContent = mediaFailureText(mediaReport.failures[0]);
    else if (capture?.error === "REOPEN_VIDEO_CONVERSATION") mediaState.textContent = "请刷新或重新打开已生成视频的会话";
    else if (/^HTTP_/.test(capture?.error || "")) mediaState.textContent = `会话接口返回 ${capture.error.slice(5)}`;
    else if (capture?.capturedModels) mediaState.textContent = `已发现 ${capture.capturedModels} 个 video_model 原片描述`;
    else if (capture?.capturedCandidates) mediaState.textContent = `已发现 ${capture.capturedCandidates} 个视频解析入口`;
    else if (capture?.observedResponses) mediaState.textContent = "已捕获会话响应；这不代表视频任务已创建";
    else mediaState.textContent = "等待捕获视频接口…";
  }

  function resetForSource(sourceKey) {
    if (typeof sourceKey !== "string" || !sourceKey) {
      return;
    }

    if (sourceKey !== currentSourceKey) {
      currentSourceKey = sourceKey;
      items.clear();
      directSeen.clear();
      markedResolveSeen.clear();
      scheduleDirectScan();
      statusText = "";
      mediaReport = null;
    }
  }

  function syncResolutionControl() {
    let value = "auto";
    try { value = localStorage.getItem(RESOLUTION_KEY) || "auto"; } catch { /* Use native defaults. */ }
    resolutionSelect.value = ["480p", "720p", "1080p"].includes(value) ? value : "auto";
    let status = null;
    try { status = JSON.parse(document.documentElement?.getAttribute(RESOLUTION_STATUS_ATTRIBUTE) || "null"); } catch { /* Ignore incomplete state. */ }
    let bridge = null;
    try { bridge = JSON.parse(document.documentElement?.getAttribute(RESOLUTION_BRIDGE_ATTRIBUTE) || "null"); } catch { /* Await bridge initialization. */ }
    const chosen = resolutionSelect.value;
    if (status?.lastRequest) {
      const sent = String(status.lastRequest.resolution || "").toUpperCase();
      if (sent) {
        resolutionState.textContent = chosen === "auto" || status.lastRequest.resolution === chosen
          ? `最近请求参数：${sent}；尚不能据此确认视频任务`
          : `最近请求参数：${sent}；当前选择 ${chosen.toUpperCase()}，与最近请求不同`;
      } else {
        resolutionState.textContent = "最近请求未发现分辨率参数，请复制诊断核对";
      }
      return;
    }
    if (bridge?.pipelineCount > 0 && bridge.lastPipeline) {
      const resolution = String(bridge.lastPipeline.resolution || "").toUpperCase();
      resolutionState.textContent = resolution
        ? `原生提交管线：${resolution}；尚未观测到实际请求`
        : "原生提交管线未生成分辨率参数，请复制诊断核对";
      return;
    }
    if (bridge?.submitReady && chosen !== "auto") {
      if (bridge.lastDraft && !bridge.lastDraft.recognized && bridge.lastDraft.resolution !== "auto") {
        resolutionState.textContent = "未识别到视频参数，请核对实际请求";
      } else if (status?.lastRequest?.resolution === chosen) {
        resolutionState.textContent = `最近请求：${chosen.toUpperCase()}（以生成结果为准）`;
      } else {
        resolutionState.textContent = bridge.inputReady && bridge.inputResolution === chosen
          ? `原生控件已同步 ${chosen.toUpperCase()}；等待新生成请求`
          : `已保存 ${chosen.toUpperCase()}；等待新生成请求验证`;
      }
      return;
    }
    if (resolutionPending && !bridge?.submitReady) {
      resolutionState.textContent = "设置已保存，请刷新配置或等待网页服务就绪";
      return;
    }
    if (!status?.installed) resolutionState.textContent = "刷新页面后启用分辨率配置";
    else if (status.lastRequest?.resolution) resolutionState.textContent = `最近请求：${String(status.lastRequest.resolution).toUpperCase()}（以生成结果为准）`;
    else if (status.lastRequest) resolutionState.textContent = "最近请求未携带可识别的分辨率字段";
    else if (bridge?.submitReady) resolutionState.textContent = "视频提交服务已就绪，可直接选择分辨率";
    else if (bridge?.nativeControl) resolutionState.textContent = "网页已显示分辨率选项";
    else if (status.skillPack || status.actionBar) resolutionState.textContent = "配置已注入，等待网页提交服务";
    else resolutionState.textContent = status.error || "等待网页加载视频配置…";
  }

  function syncModelControl() {
    let status = null; let transport = null;
    try { status = JSON.parse(document.documentElement?.getAttribute(MODEL_STATUS_ATTRIBUTE) || "null"); } catch { /* Await model state. */ }
    try { transport = JSON.parse(document.documentElement?.getAttribute(RESOLUTION_STATUS_ATTRIBUTE) || "null"); } catch { /* Await a real request. */ }
    let value = status?.selected || "auto";
    try { value = localStorage.getItem(MODEL_KEY) || value; } catch { /* Use the bridge snapshot. */ }
    const option = [...modelSelect.options].find(item => item.value === value);
    modelSelect.value = option ? value : "auto";
    const sent = transport?.lastRequest?.model;
    const sentLabel = [...modelSelect.options].find(item => item.value === sent)?.textContent || sent;
    if (sent) {
      modelState.textContent = value === "auto" || sent === value
        ? `最近请求模型参数：${sentLabel}；不代表已调用该模型`
        : `最近请求模型：${sentLabel}；当前选择 ${option?.textContent || value}，等待新请求验证`;
    } else if (value === "auto") modelState.textContent = "跟随网页选择；点击模型选项后会记住选择";
    else if (status?.inputReady && status.inputModel === value) modelState.textContent = `原生状态已同步 ${option?.textContent || value}；等待真实请求验证`;
    else modelState.textContent = status?.reason || "选择已保存，等待网页原生模型状态";
  }

  function syncGenerationEvidence() {
    let result = null;
    try { result = JSON.parse(document.documentElement?.getAttribute(GENERATION_ATTRIBUTE) || "null"); } catch { /* Await a complete snapshot. */ }
    generationFields.textContent = "";
    if (!result || result.phase === "idle") { generationState.textContent = "尚无提交响应记录；未验证视频任务"; return; }
    if (result.phase === "request_observed") generationState.textContent = "已观察到提交，等待原始响应；未验证视频任务";
    else if (result.phase === "http_error") generationState.textContent = `接口 HTTP ${result.httpStatus}；未验证视频任务`;
    else if (result.phase === "transport_error") generationState.textContent = `响应读取或传输失败：${result.error}；未验证视频任务`;
    else if (result.taskFields?.length) generationState.textContent = `响应含 ${result.taskFields.length} 项任务候选字段；协议尚未核实受理`;
    else if (result.phase === "response_ended") generationState.textContent = "响应已结束，未见可识别任务字段；不能确认已创建视频任务";
    else generationState.textContent = "正在读取响应；目前未见可识别任务字段";
    if (result.requestIntent) generationState.textContent += result.requestIntent.video == null
      ? "；当前请求结构未完整识别"
      : !result.requestIntent.video
      ? "；当前消息未识别为视频生成模式"
      : result.requestIntent.present == null ? "；当前正文结构未知，不能判断前缀是否发出"
      : result.requestIntent.present ? "；发出请求含生成指令前缀" : "；发出请求未见生成指令前缀";
    if (result.scope === "page_changed_since_submission") generationState.textContent += "；提交后页面地址已变化，以上为最近一次提交记录";
    const fields = [...(result.statusFields || []), ...(result.billingFields || [])].slice(0, 5);
    generationFields.textContent = fields.map(field => `${field.path} = ${field.value}`).join("；");
    if (result.truncated) generationFields.textContent += "；响应过大，诊断已截断";
  }

  function syncAutoConfirmation() {
    let status = null;
    try { status = JSON.parse(document.documentElement?.getAttribute(AUTO_CONFIRM_ATTRIBUTE) || "null"); } catch { /* Await the native observer. */ }
    autoConfirmToggle.checked = status?.enabled === true;
    autoConfirmState.textContent = !status ? "自动续确认模块未就绪，请刷新页面" : !status.enabled ? "自动续确认已关闭" : status.reason;
  }

  function syncGenerationIntent() {
    let status = null;
    try { status = JSON.parse(document.documentElement?.getAttribute(GENERATION_INTENT_ATTRIBUTE) || "null"); } catch { /* Await the native hook. */ }
    generationIntentToggle.checked = status?.enabled === true;
    generationIntentState.textContent = !status ? "生成指令模块未就绪，请刷新页面" : status.reason;
  }

  function requestDebugger(type, values = {}) {
    return new Promise((resolve, reject) => {
      let settled = false;
      const finish = (error, value) => {
        if (settled) return;
        settled = true; clearTimeout(timer);
        if (error) reject(error); else resolve(value);
      };
      const timer = setTimeout(() => finish(new Error("调试后台 10 秒未响应，请重连后台；仍失败请重新加载扩展并刷新页面")), 10000);
      try {
        chrome.runtime.sendMessage({ type, ...values }, response => {
          const error = chrome.runtime.lastError;
          if (error) { finish(new Error(error.message || "调试后台不可用，请重新加载扩展并刷新页面")); return; }
          if (!response?.ok) { finish(new Error(response?.error || "未取得真实调试状态")); return; }
          finish(null, response);
        });
      } catch (error) { finish(error); }
    });
  }

  function renderDebuggerError(error) {
    debuggerChannelFailed = true;
    debuggerToggle.textContent = "重连后台";
    debuggerState.textContent = `调试连接失败：${error.message || error}。请先重连；仍失败，在 chrome://extensions 重新加载此扩展，再刷新当前页面。也可直接导出本地诊断。`;
  }

  function localDiagnostics() {
    const read = attribute => { try { return JSON.parse(document.documentElement?.getAttribute(attribute) || "null"); } catch { return null; } };
    let runtime = {};
    try { runtime = { extensionId: chrome.runtime.id, manifestVersion: chrome.runtime.getManifest().version }; }
    catch { runtime = { invalidated: true }; }
    return { version: "1.6.60", site: location.hostname, runtime, twoPart: read("data-dola-two-part-status"),
      configuration: read(RESOLUTION_STATUS_ATTRIBUTE), bridge: read(RESOLUTION_BRIDGE_ATTRIBUTE),
      modelSelection: read(MODEL_STATUS_ATTRIBUTE), generationEvidence: read(GENERATION_ATTRIBUTE),
      autoConfirmation: read(AUTO_CONFIRM_ATTRIBUTE), generationIntent: read(GENERATION_INTENT_ATTRIBUTE),
      silentExpert: read("data-dola-silent-expert-status"),
      protocolSend: read(PROTOCOL_SEND_STATUS_ATTRIBUTE),
      mediaCapture: read(MEDIA_CAPTURE_ATTRIBUTE), mediaParser: mediaReport };
  }

  function renderDebugger(status) {
    if (!status) return;
    debuggerChannelFailed = false;
    debuggerStatus = status;
    debuggerToggle.textContent = status.phase === "attach_error" ? "重试" : status.enabled || status.attached ? "停止" : "启用";
    const last = status.lastSubmission;
    const detail = last ? `；最近提交 HTTP ${last.status ?? "待返回"}` +
      (last.response?.businessErrors?.length ? `，响应代码 ${last.response.businessErrors[0].code}` :
        last.response?.taskFields?.length ? `，${last.response.taskFields.length} 项任务候选字段` :
        last.phase === "response_ended" ? "，未见可识别任务字段" : "") : "";
    const comparison = status.submissionComparison;
    const same = value => value == null ? "未知" : value ? "相同" : "不同";
    const difference = comparison ? `；与上次提交：提示词${same(comparison.equal.prompt)}、视频参数${same(comparison.equal.video)}、选项${same(comparison.equal.option)}` : "";
    debuggerState.textContent = `${status.reason}；捕获 ${status.requestCount || 0} 请求 / ${status.submissionCount || 0} 次提交${detail}${difference}`;
  }

  function syncTwoPart() {
    let status;
    try { status = JSON.parse(document.documentElement?.getAttribute("data-dola-two-part-status") || "null"); } catch {}
    twoPartToggle.checked = status?.enabled === true;
    twoPartToggle.disabled = !status;
    const hint = !status ? "规则模块未加载，请重新加载扩展并刷新网页" : !status.enabled ? "默认关闭；开启后会向提交的提示词追加规则" :
      status.phase === "storage_error" ? "规则开关保存失败" : status.phase === "requires_30s" ? "本次未追加：请先选择30秒，再提交" :
      "已开启：仅30秒视频提交追加两条各15秒规则；可能分别扣额度，结果以平台为准";
    twoPartState.textContent = hint;
    shadow.querySelector(".two-part-rule").textContent = document.documentElement?.getAttribute("data-dola-two-part-rule") || "请刷新页面加载规则预览。";
  }

  function duration30Enabled() {
    try {
      return localStorage.getItem(DURATION_KEY) === "1";
    } catch {
      return false;
    }
  }

  function setDuration30Enabled(enabled) {
    try {
      localStorage.setItem(DURATION_KEY, enabled ? "1" : "0");
      localStorage.setItem(TARGET_DURATION_KEY, enabled ? "30" : "15");
    } catch {
      return;
    }
    document.documentElement?.setAttribute(DURATION_STATE_ATTRIBUTE, enabled ? "1" : "0");
    document.dispatchEvent(new Event(DURATION_STATE_EVENT));
    syncDurationControl();
  }

  function syncDurationControl() {
    let selected = duration30Enabled() ? 30 : 15;
    try {
      const explicit = Number(localStorage.getItem(TARGET_DURATION_KEY));
      if (explicit === 15 || explicit === 30) selected = explicit;
    } catch {
      // Keep the legacy switch value.
    }
    const enabled = selected === 30;
    durationToggle.checked = enabled;
    durationState.textContent = `偏好 ${selected}s`;
    durationState.classList.toggle("enabled", true);
  }

  function render() {
    count.textContent = String(items.size);
    list.textContent = "";

    if (!items.size) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = statusText || "等待捕获资源…";
      list.appendChild(empty);
      return;
    }

    Array.from(items.entries()).sort(([, left], [, right]) =>
      Number(left.capturedAt || 0) - Number(right.capturedAt || 0) ||
      Number(left.captureOrder || 0) - Number(right.captureOrder || 0)).forEach(([key, item], index) => {
      const row = document.createElement("div");
      row.className = "item";

      const label = document.createElement("div");
      label.className = "label";
      label.title = item.url;

      const tag = document.createElement("span");
      tag.className = `tag ${item.type}`;
      tag.textContent = item.type === "image" ? "图片" : "视频";

      const indexText = document.createElement("span");
      indexText.className = "index";
      indexText.textContent = String(index + 1);

      label.append(tag, indexText);

      const button = document.createElement("button");
      button.type = "button";
      button.className = "dl";
      const download = item.download;
      button.textContent = download?.state === "complete" ? "打开所在目录"
        : download?.state === "selecting" ? "等待选址…"
        : download?.state === "starting" || download?.state === "in_progress" ? "下载中…"
        : download?.state === "interrupted" || download?.state === "failed" ? "重试下载" : "下载";
      button.disabled = ["starting", "selecting", "in_progress"].includes(download?.state);
      button.addEventListener("click", () => {
        if (download?.state === "complete" && Number.isInteger(download.id)) {
          chrome.runtime.sendMessage({ type: "REVEAL_DOWNLOAD", id: download.id }, (response) => {
            if (chrome.runtime.lastError || !response?.ok) {
              item.download = { ...download, error: response?.error || chrome.runtime.lastError?.message || "无法打开目录" };
              render();
            }
          });
          return;
        }
        startDownload(key, false);
      });

      const saveButton = document.createElement("button");
      saveButton.type = "button";
      saveButton.className = "save";
      saveButton.textContent = "选择位置";
      saveButton.disabled = ["starting", "selecting", "in_progress"].includes(download?.state);
      saveButton.addEventListener("click", () => startDownload(key, true));

      row.append(label, button, saveButton);
      if (item.conversationTitle || item.captureOrder) {
        const scope = document.createElement("div");
        scope.className = "quality-note";
        const time = item.capturedAt ? new Date(item.capturedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
        scope.textContent = [item.conversationTitle ? `会话：${item.conversationTitle}` : "当前会话",
          item.captureOrder ? `片段 ${item.captureOrder}` : "", time].filter(Boolean).join(" · ");
        row.appendChild(scope);
      }
      if (item.provenance === "direct_link") {
        const note = document.createElement("div");
        note.className = "quality-note";
        note.textContent = item.watermark === "marked"
          ? "视频直链 · 链接标记带水印 · 未取得无水印源"
          : "视频直链 · 水印状态未确认 · 未取得无水印源";
        row.appendChild(note);
      }
      if (item.type === "video" && item.provenance === "fallback_api") {
        const quality = document.createElement("div");
        quality.className = "quality-note";
        const size = item.width && item.height ? `${item.width} × ${item.height}` :
          item.resolution ? `${item.resolution.toUpperCase()}（接口规格，尺寸未提供）` : "尺寸未提供";
        const mismatch = item.requestedResolution === "1080p" && item.width && item.height &&
          Math.min(item.width, item.height) < 1080;
        quality.textContent = `${item.watermark === "unmarked" ? "接口标注无水印" : "无水印版本请求返回"} · ${size}` +
          (mismatch ? " · 请求 1080P，当前选中版本低于 1080P；可重试提取，完整候选尺寸见诊断" : "");
        row.appendChild(quality);
      }
      if (item.type === "video" && item.provenance === "dola_unwatermarked_api") {
        const quality = document.createElement("div");
        quality.className = "quality-note";
        const size = item.width && item.height ? `${item.width} × ${item.height}` : item.resolution ? item.resolution.toUpperCase() : "尺寸未提供";
        quality.textContent = `Dola 官方无水印接口返回 · ${size}`;
        row.appendChild(quality);
      }
      if (item.type === "video" && item.provenance === "video_model") {
        const quality = document.createElement("div");
        quality.className = "quality-note";
        const size = item.width && item.height ? `${item.width} × ${item.height}` : item.resolution ? item.resolution.toUpperCase() : "尺寸未提供";
        quality.textContent = `video_model 原片返回 · ${size}`;
        row.appendChild(quality);
      }
      if (download) {
        const note = document.createElement("div");
        note.className = "download-note";
        note.textContent = download.state === "complete"
          ? `已保存：${download.filename || "请打开所在目录查看"}${download.error ? `（打开目录失败：${download.error}）` : ""}`
          : download.error ? `下载失败：${download.error}`
          : download.state === "selecting" ? (download.saveAs
            ? "请选择保存位置；选定后会显示实际文件路径…"
            : "正在创建浏览器下载任务…")
          : "正在保存到当前浏览器的下载目录…";
        row.appendChild(note);
      }
      list.appendChild(row);
    });
  }

  function startDownload(key, saveAs) {
    const item = items.get(key);
    if (!item || ["starting", "selecting", "in_progress"].includes(item.download?.state)) return;
    item.download = { state: "starting" };
    render();
    chrome.runtime.sendMessage({ type: "DOWNLOAD_MEDIA", url: item.url, mediaType: item.type, saveAs }, (response) => {
      const current = items.get(key);
      if (!current) return;
      const error = chrome.runtime.lastError?.message || response?.error;
      if (error || !response?.ok || !Number.isInteger(response.id)) {
        if (!error && response?.ok && response.pending && Number.isFinite(response.startedAt)) {
          current.download = { state: "selecting", startedAt: response.startedAt, saveAs };
          render();
          pollDownloadLookup(key, item.url, response.startedAt, response.requestId);
          return;
        }
        current.download = { state: "failed", error: error || "浏览器未创建下载任务" };
        render();
        return;
      }
      current.download = { state: "in_progress", id: response.id };
      render();
      pollDownload(key, response.id);
    });
  }

  function pollDownloadLookup(key, url, startedAt, requestId, retries = 0) {
    chrome.runtime.sendMessage({ type: "DOWNLOAD_LOOKUP", url, startedAt, requestId }, (response) => {
      const current = items.get(key);
      if (!current || current.download?.state !== "selecting" || current.download.startedAt !== startedAt) return;
      const error = chrome.runtime.lastError?.message || response?.error;
      if (error || !response?.ok) {
        if (chrome.runtime.lastError && retries < 3) {
          setTimeout(() => pollDownloadLookup(key, url, startedAt, requestId, retries + 1), 1000);
          return;
        }
        current.download = { state: "failed", error: error || "无法查询浏览器下载记录" };
        render();
        return;
      }
      if (Number.isInteger(response.id)) {
        current.download = { state: "in_progress", id: response.id };
        render();
        pollDownload(key, response.id);
        return;
      }
      if (Date.now() - startedAt > 300000) {
        current.download = { state: "failed", error: "未找到下载记录，请检查浏览器下载栏或重试" };
        render();
        return;
      }
      setTimeout(() => pollDownloadLookup(key, url, startedAt, requestId), 1500);
    });
  }

  function pollDownload(key, id) {
    chrome.runtime.sendMessage({ type: "DOWNLOAD_STATUS", id }, (response) => {
      const current = items.get(key);
      if (!current || current.download?.id !== id) return;
      const error = chrome.runtime.lastError?.message || response?.error;
      if (error || !response?.ok) {
        current.download = { state: "failed", id, error: error || "无法读取下载状态" };
      } else {
        current.download = {
          state: response.state || "in_progress",
          id,
          filename: response.filename || "",
          error: response.state === "interrupted" ? response.error || "下载被中断" : ""
        };
      }
      render();
      if (current.download.state === "in_progress") {
        setTimeout(() => pollDownload(key, id), 1500);
      }
    });
  }

  function addItems(nextItems) {
    for (const item of nextItems) {
      if (!item || typeof item.url !== "string" || !isHttpUrl(item.url)) {
        continue;
      }
      const type = item.type === "image" ? "image" : "video";
      const fingerprint = mediaFingerprint(item.url);
      const key = `${type}:${fingerprint}`;
      const context = conversationContext();
      const conversationKey = item.conversationKey || context.conversationKey;
      const captureOrder = Number(item.captureOrder || 0);
      let previous = items.get(key);
      // One Dola creation can be exposed through both fallback_api and
      // video_model.  Their signed URLs differ, so URL-only dedupe renders the
      // same clip twice and destroys the visible segment order.  Merge only
      // when the conversation and native capture slot are identical, keeping
      // the explicit fallback/official no-watermark path as authoritative.
      if (!previous && type === "video" && captureOrder > 0) {
        const duplicate = [...items.entries()].find(([, value]) => value?.type === "video" &&
          value.conversationKey === conversationKey && Number(value.captureOrder || 0) === captureOrder &&
          ["fallback_api", "video_model", "dola_unwatermarked_api"].includes(value.provenance) &&
          ["fallback_api", "video_model", "dola_unwatermarked_api"].includes(item.provenance));
        if (duplicate) {
          const rank = { video_model: 1, fallback_api: 2, dola_unwatermarked_api: 3 };
          if ((rank[item.provenance] || 0) < (rank[duplicate[1].provenance] || 0)) continue;
          previous = duplicate[1];
          items.delete(duplicate[0]);
        }
      }
      const changedSource = previous && (mediaFingerprint(previous.url) !== fingerprint ||
        Number(previous.width || 0) !== Number(item.width || 0) || Number(previous.height || 0) !== Number(item.height || 0));
      items.set(key, {
        ...previous,
        ...(changedSource ? { download: undefined } : {}),
        width: item.width || 0, height: item.height || 0, resolution: item.resolution || "",
        requestedResolution: item.requestedResolution || "auto",
        definition: item.definition || "", watermark: item.watermark || "unknown",
        provenance: item.provenance || "", resourceKey: item.resourceKey || "",
        conversationKey,
        conversationTitle: item.title || item.conversationTitle || context.title,
        capturedAt: Number(item.capturedAt || previous?.capturedAt || Date.now()),
        captureOrder: Number(captureOrder || previous?.captureOrder || items.size + 1),
        type,
        url: item.url
      });
    }
  }

  function isHttpUrl(url) {
    return /^https?:\/\//i.test(url);
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

  function forceHostVisible() {
    const properties = {
      all: "initial",
      display: "block",
      position: "fixed",
      right: "16px",
      bottom: "16px",
      width: "auto",
      height: "auto",
      visibility: "visible",
      opacity: "1",
      "pointer-events": "auto",
      "z-index": "2147483647"
    };
    for (const [name, value] of Object.entries(properties)) {
      host.style.setProperty(name, value, "important");
    }
  }

  function mountHost() {
    if (!host.isConnected && document.documentElement) {
      document.documentElement.appendChild(host);
    }
  }

  const hostObserver = new MutationObserver(() => {
    const currentHost = document.getElementById(PANEL_ID);
    if (currentHost && currentHost !== host) {
      hostObserver.disconnect();
      return;
    }
    mountHost();
  });
  hostObserver.observe(document.documentElement, { childList: true });
})();
