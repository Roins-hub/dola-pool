(function () {
  "use strict";

  const INSTALL_MARK = "__DOLA_TOOLKIT_MEDIA_UPLOAD_BRIDGE_V3__";
  const REQUEST_EVENT = "dola-toolkit-media-upload-request";
  const READY_ATTRIBUTE = "data-dola-toolkit-media-upload-ready";
  const RESULT_SELECTOR = "#dola-toolkit-media-upload-result";
  const PRESERVED_BLOCK_KEY = "dola-toolkit-preserved-expert-media-v1";
  const BATCH_STORAGE_KEY = "dola-toolkit-protocol-batch-v1";

  if (!/(^|\.)dola\.com$/i.test(location.hostname)) return;
  if (globalThis[INSTALL_MARK]) return;
  globalThis[INSTALL_MARK] = true;
  let activeRuntime = null;
  let activeChatId = '';
  let captureMediaSnapshot = null;
  let preservedSnapshot = null;
  let preservedFiles = [];
  let lastNativeFiles = [];
  let preservedInputNodes = new Set();
  let preservedAt = 0;
  let preservedChatId = '';
  let ensureNativeMediaRunner = null;
  let bindCurrentNativeContextRunner = null;
  let buildNativeBlockRunner = null;
  let injectPreservedRequestRunner = null;
  const PRESERVE_TTL_MS = 30_000;
  const BATCH_PRESERVE_TTL_MS = 30 * 60_000;
  let mediaRoundId = '';

  function batchActive() {
    try {
      const value = JSON.parse(sessionStorage.getItem(BATCH_STORAGE_KEY) || 'null');
      return Boolean(value?.active && Number(value.completed || 0) < Number(value.total || 0));
    } catch (_) { return false; }
  }

  function preserveTtl() {
    return batchActive() ? BATCH_PRESERVE_TTL_MS : PRESERVE_TTL_MS;
  }

  function readPersistedBlock() {
    try {
      const value = JSON.parse(sessionStorage.getItem(PRESERVED_BLOCK_KEY) || 'null');
      if (!value?.block || !value?.savedAt || Date.now() - Number(value.savedAt) > preserveTtl()) {
        sessionStorage.removeItem(PRESERVED_BLOCK_KEY);
        return null;
      }
      return value;
    } catch (_) { return null; }
  }

  function persistNativeBlock(snapshotValue) {
    if (!snapshotValue?.attachments?.length || snapshotValue.pending || !buildNativeBlockRunner) return null;
    const block = buildNativeBlockRunner(snapshotValue);
    if (!block) return null;
    const value = { savedAt: Date.now(), block, counts: snapshotValue.counts || attachmentCounts([block]) };
    try { sessionStorage.setItem(PRESERVED_BLOCK_KEY, JSON.stringify(value)); }
    catch (_) { return null; }
    return value;
  }

  function fallbackNativeBlock(snapshotValue) {
    const items = [];
    for (const state of snapshotValue?.states || []) {
      if (state?.type !== 'image' || !state?.fileKey) continue;
      const name = String(state.fileName || 'reference.png');
      const format = (name.split('.').pop() || 'png').toLowerCase();
      items.push({
        type: 1,
        identifier: state.localKey || crypto.randomUUID(),
        image: {
          name,
          uri: state.fileKey,
          image_ori: {
            url: '',
            width: Number(state.imageWidth || state.mediaCoverWidth || 0),
            height: Number(state.imageHeight || state.mediaCoverHeight || 0),
            format,
            url_formats: {}
          }
        },
        parse_state: 0,
        review_state: 1,
        upload_status: 1,
        progress: 100,
        src: ''
      });
    }
    if (!items.length || items.length < Number(snapshotValue?.counts?.image || 0)) return null;
    return {
      block_type: 10052,
      content: { attachment_block: { attachments: items }, pc_event_block: '' },
      block_id: crypto.randomUUID(),
      parent_id: '',
      meta_info: [],
      extra_info: {},
      append_fields: []
    };
  }

  function nativeFileInputs() {
    return Array.from(document.querySelectorAll('input[type="file"]'))
      .filter(node => !node.matches('#dola-toolkit-media-upload-bridge'));
  }

  function retainedComposerAddButton() {
    return Array.from(document.querySelectorAll('button')).find(node => {
      const rect = node.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0 || String(node.innerText || '').trim()) return false;
      const paths = Array.from(node.querySelectorAll('svg path')).map(path => path.getAttribute('d') || '').join(' ');
      return /V10\.9951H2|V11H3|M12(?:\.0005)?\s+2\.25/.test(paths);
    }) || null;
  }

  function uniqueFiles(files) {
    const output = [];
    const seen = new Set();
    for (const file of files || []) {
      if (!(file instanceof File)) continue;
      const key = `${file.name}\u0000${file.size}\u0000${file.lastModified}\u0000${file.type}`;
      if (seen.has(key)) continue;
      seen.add(key);
      output.push(file);
    }
    return output;
  }

  function retainedFileKind(file) {
    const mime = String(file?.type || '').toLowerCase();
    const name = String(file?.name || '').toLowerCase();
    if (mime.startsWith('image/') || /\.(?:jpe?g|png|webp|apng|gif|avif)$/.test(name)) return 'image';
    if (mime.startsWith('video/') || /\.(?:mp4|mov|m4v|webm|mkv)$/.test(name)) return 'video';
    if (mime.startsWith('audio/') || /\.(?:mp3|wav|m4a|aac|flac|ogg)$/.test(name)) return 'audio';
    return '';
  }

  function fileCounts(files) {
    const counts = { image: 0, video: 0, audio: 0 };
    for (const file of files || []) {
      const kind = retainedFileKind(file);
      if (kind && Object.hasOwn(counts, kind)) counts[kind] += 1;
    }
    return counts;
  }

  // Keep the actual File objects while Dola's video composer is still alive.
  // Uploaded attachment metadata alone cannot be moved into the Expert
  // composer because both views use different chat/store keys.
  document.addEventListener('change', (event) => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement) || input.type !== 'file' || input.matches('#dola-toolkit-media-upload-bridge')) return;
    const files = uniqueFiles(Array.from(input.files || []));
    if (files.length) lastNativeFiles = files;
  }, true);

  function normalizeUploadUrl(raw) {
    const value = String(raw || '');
    const match = value.match(/^(?:https?:\/\/www\.dola\.com\/chat\/)?(vod-[a-z0-9.-]+\.bytevcloudapi\.com)(\/?\?.*)$/i);
    return match ? `https://${match[1]}${match[2]}` : value;
  }

  function installUploadUrlNormalizer() {
    if (globalThis.__DOLA_TOOLKIT_UPLOAD_URL_NORMALIZER__) return;
    globalThis.__DOLA_TOOLKIT_UPLOAD_URL_NORMALIZER__ = true;
    const originalFetch = window.fetch;
    if (typeof originalFetch === 'function') {
      window.fetch = function normalizedUploadFetch(input, init) {
        const originalUrl = typeof input === 'string' ? input : (input?.url || String(input || ''));
        const normalizedUrl = normalizeUploadUrl(originalUrl);
        if (normalizedUrl !== originalUrl) {
          try {
            input = input instanceof Request ? new Request(normalizedUrl, input) : normalizedUrl;
          } catch (_) {
            input = normalizedUrl;
          }
        }
        return originalFetch.call(this, input, init);
      };
    }
    const originalOpen = XMLHttpRequest.prototype.open;
    if (typeof originalOpen === 'function') {
      XMLHttpRequest.prototype.open = function normalizedUploadOpen(method, url) {
        const args = Array.from(arguments);
        args[1] = normalizeUploadUrl(url);
        return originalOpen.apply(this, args);
      };
    }
  }

  installUploadUrlNormalizer();

  async function uploadReferenceMedia({ bridgeSelector }) {
  const VIDEO_SKILL = 17;
  const ATTACHMENT_BLOCK = 10052;
  const ACCEPTED_TYPES = ['image', 'video', 'audio'];
  const ACCEPTED_EXTENSIONS = ['jpg', 'jpeg', 'png', 'webp', 'apng', 'mp4', 'mov', 'mp3', 'wav'];
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  function safeDebugJson(value) {
    const seen = new WeakSet();
    try {
      return JSON.stringify(value, (key, child) => {
        if (child instanceof File) return { name: child.name, size: child.size, type: child.type };
        if (child instanceof Blob) return { size: child.size, type: child.type };
        if (child && typeof child === 'object') {
          if (seen.has(child)) return '[Circular]';
          seen.add(child);
        }
        if (typeof child === 'function') return `[Function ${child.name || 'anonymous'}]`;
        return child;
      }).slice(0, 12000);
    } catch (error) {
      return String(error?.message || error);
    }
  }

  function materialKind(file) {
    const type = String(file?.type || '').toLowerCase();
    const name = String(file?.name || '').toLowerCase();
    if (type.startsWith('image/') || /\.(jpe?g|png|webp|apng)$/.test(name)) return 'image';
    if (type.startsWith('video/') || /\.(mp4|mov)$/.test(name)) return 'video';
    if (type.startsWith('audio/') || /\.(mp3|wav)$/.test(name)) return 'audio';
    return '';
  }

  function attachmentKind(attachment) {
    const direct = String(attachment?.type || '').toLowerCase();
    if (ACCEPTED_TYPES.includes(direct)) return direct;
    const numeric = Number(attachment?.type);
    if (numeric === 1) return 'image';
    if (numeric === 2) {
      const mediaType = Number(attachment?.media?.media_type ?? attachment?.mediaType);
      if (mediaType === 1) return 'audio';
      if (mediaType === 2) return 'video';
    }
    const mime = String(attachment?.mimeType || attachment?.file?.type || '').toLowerCase();
    if (mime.startsWith('image/')) return 'image';
    if (mime.startsWith('video/')) return 'video';
    if (mime.startsWith('audio/')) return 'audio';
    return '';
  }

  function getRuntime() {
    if (typeof window.__DOLA_TOOLKIT_WEBPACK_REQUIRE__ === 'function') {
      return window.__DOLA_TOOLKIT_WEBPACK_REQUIRE__;
    }
    const chunks = window.__LOADABLE_LOADED_CHUNKS__;
    if (!Array.isArray(chunks)) return null;
    try {
      chunks.push([[`dola-toolkit-media-${Date.now()}`], {}, runtime => {
        window.__DOLA_TOOLKIT_WEBPACK_REQUIRE__ = runtime;
      }]);
    } catch (_) {}
    return window.__DOLA_TOOLKIT_WEBPACK_REQUIRE__ || null;
  }

  function getStoreApi(runtime) {
    if (typeof runtime !== 'function' || !runtime.m) return null;
    // Current Dola chunks expose the generic Zustand registry as yDg:
    //   b(name) -> state, c(name) -> store.
    // Older builds used qBy.Bc/QY. Restrict the probe to the two attachment
    // domains before accepting it so unrelated short minified exports cannot
    // be mistaken for the store API.
    try {
      const module = runtime('yDg');
      const getState = module?.b;
      const getStore = module?.c;
      if (typeof getState === 'function' && typeof getStore === 'function') {
        const attachments = getState('attachmentsStore');
        const uploads = getState('attachmentUploadStore');
        if (attachments?.attachmentsMap && uploads?.preprocessTaskMap) {
          return { ...module, GX: getState, Wp: getStore };
        }
      }
    } catch (_) {}
    // 新版 store runtime：qBy.Bc(name) 读取 state，qBy.QY(name) 读取 store。
    try {
      const module = runtime('qBy');
      if (typeof module?.Bc === 'function') {
        return { GX: module.Bc, Wp: module.QY };
      }
    } catch (_) {}
    for (const [rawId, factory] of Object.entries(runtime.m)) {
      const source = String(factory || '');
      if (!/GX:function|attachmentsStore|attachmentUploadStore/.test(source)) continue;
      try {
        const module = runtime(rawId);
        const getState = module?.GX || module?.Bc;
        const getStore = module?.Wp || module?.QY;
        if (typeof getState !== 'function') continue;
        const attachments = getState('attachmentsStore');
        const uploads = getState('attachmentUploadStore');
        if (attachments?.attachmentsMap && uploads?.preprocessTaskMap) {
          return { ...module, GX: getState, Wp: getStore };
        }
      } catch (_) {}
    }
    return null;
  }

  function getStoreState(runtime, name) {
    const api = getStoreApi(runtime);
    if (!api) return null;
    try { return api.GX(name) || null; } catch (_) {}
    try { return api.Wp(name)?.getState?.() || null; } catch (_) { return null; }
  }

  function getServiceApi(runtime) {
    if (typeof runtime !== 'function' || !runtime.m) return null;
    // 新版 service container 在 nR1.K。
    try {
      const module = runtime('nR1');
      if (typeof module?.K === 'function') return { ...module, D: module.K };
    } catch (_) {}
    for (const [rawId, factory] of Object.entries(runtime.m)) {
      const source = String(factory || '');
      if (!/serviceContainer|StoreService/.test(source)) continue;
      try {
        const module = runtime(rawId);
        const getService = module?.D || module?.K;
        if (typeof getService === 'function') return { ...module, D: getService };
      } catch (_) {}
    }
    for (const id of [431565, 645906]) {
      try {
        const module = runtime(id);
        if (typeof module?.D === 'function') return module;
      } catch (_) {}
    }
    return null;
  }

  function getMediaConfig(runtime) {
    if (typeof runtime !== 'function' || !runtime.m) return null;
    for (const [rawId, factory] of Object.entries(runtime.m)) {
      const source = String(factory || '');
      if (!/tV:function/.test(source) || !/Ew:function/.test(source) || !/eq:function/.test(source)) continue;
      try {
        const module = runtime(rawId);
        if (module?.tV && typeof module.Ew === 'string' && typeof module.eq === 'string') return module;
      } catch (_) {}
    }
    for (const id of [114816, 607159]) {
      try {
        const module = runtime(id);
        if (module?.tV && typeof module.Ew === 'string' && typeof module.eq === 'string') return module;
      } catch (_) {}
    }
    return null;
  }

  function findUploadRunner(runtime) {
    if (typeof runtime !== 'function' || !runtime.m) return null;
    // Dola 2026-10 的 async-shared-upload-pipeline 使用字符串 module id
    // `IK`，并改成 named export `runConfiguredUploadPipeline`。旧实现把所有
    // module id 强转 Number，`IK` 会变成 NaN，因此永远扫描不到音频管线。
    for (const rawId of ['IK']) {
      try {
        const module = runtime(rawId);
        if (typeof module?.runConfiguredUploadPipeline === 'function') {
          return { id: rawId, run: module.runConfiguredUploadPipeline };
        }
      } catch (_) {}
    }
    for (const [rawId, factory] of Object.entries(runtime.m)) {
      const source = String(factory || '');
      if (!/inputUploadConfig/.test(source) || !/targetAttachmentChannel/.test(source) ||
          !/uploadPipelineConfig/.test(source)) continue;
      try {
        const module = runtime(rawId);
        const run = module?.runConfiguredUploadPipeline || module?.Z;
        if (typeof run === 'function') return { id: rawId, run };
      } catch (_) {}
    }
    return null;
  }

  function staticAssetBase() {
    const urls = [
      ...performance.getEntriesByType('resource').map(entry => String(entry.name || '')),
      ...Array.from(document.querySelectorAll('script[src]')).map(node => String(node.src || ''))
    ];
    const url = urls.find(value => /\/static\/js\/.*\.js(?:[?#]|$)/i.test(value));
    return url ? url.slice(0, url.indexOf('/static/js/')) : '';
  }

  function loadScript(url) {
    if (!url) return Promise.resolve(false);
    return new Promise(resolve => {
      const script = document.createElement('script');
      let done = false;
      const finish = ok => { if (!done) { done = true; resolve(ok); } };
      script.onload = () => finish(true);
      script.onerror = () => finish(false);
      script.src = url;
      script.async = true;
      (document.head || document.documentElement).appendChild(script);
      setTimeout(() => finish(false), 15000);
    });
  }

  async function resolveUploadRunner(runtime) {
    let runner = findUploadRunner(runtime);
    if (runner) return runner;
    const routeAssets = window._MODERNJS_ROUTE_MANIFEST?.routeAssets || {};
    const entries = Object.entries(routeAssets)
      .filter(([name, value]) => /shared-upload-pipeline/i.test(name) ||
        (Array.isArray(value?.assets) && value.assets.some(asset => /shared-upload-pipeline/i.test(asset))))
      .map(([, value]) => value);
    // 当前官网 manifest 只给 async-shared-upload-pipeline.chunkIds=['3rw']，
    // 没有 assets 字段。直接走 webpack chunk loader，不能再依赖静态文件名。
    if (!entries.length) entries.push({ chunkIds: ['3rw'], assets: [] });
    for (const entry of entries) {
      for (const rawChunkId of entry.chunkIds || []) {
        if (typeof runtime.e !== 'function') continue;
        try { await runtime.e(rawChunkId); } catch (_) {}
        runner = findUploadRunner(runtime);
        if (runner) return runner;
      }
    }
    const base = staticAssetBase();
    for (const entry of entries) {
      for (const asset of entry.assets || []) {
        if (!/shared-upload-pipeline.*\.js(?:[?#]|$)/i.test(asset)) continue;
        await loadScript(`${base}/${asset}`);
        runner = findUploadRunner(runtime);
        if (runner) return runner;
      }
    }
    return null;
  }

  function findInputContext() {
    const inputs = Array.from(document.querySelectorAll('input[type="file"]'))
      .filter(node => !node.matches(bridgeSelector));
    for (const input of inputs) {
      const key = Object.keys(input).find(name => name.startsWith('__reactFiber$'));
      let fiber = key ? input[key] : null;
      for (let i = 0; fiber && i < 12; i += 1, fiber = fiber.return) {
        const props = fiber.memoizedProps || {};
        if (props.chatId && props.skill && props.inputUploadConfig) {
          return { input, props };
        }
      }
    }
    return null;
  }

  function findInputProps() {
    return findInputContext()?.props || null;
  }

  function enableAllReferenceTypes(pipelineConfig) {
    if (!pipelineConfig || typeof pipelineConfig !== 'object') return pipelineConfig;
    pipelineConfig.acceptedAttachmentTypes = ACCEPTED_TYPES;
    pipelineConfig.acceptedFileExtensions = ACCEPTED_EXTENSIONS;
    pipelineConfig.maxCount = 50;
    pipelineConfig.addAttachmentType = ({ file } = {}) => materialKind(file) || 'file';
    pipelineConfig.isReusableAttachment = attachment => ACCEPTED_TYPES.includes(attachment?.type);
    return pipelineConfig;
  }

  async function installNativeUploadFallback(inputUploadConfig, initialConfig) {
    const originalFactory = inputUploadConfig?.uploadPipelineConfig;
    if (typeof originalFactory !== 'function') return;
    if (originalFactory.__DOLA_TOOLKIT_ALL_MEDIA__) return;
    const wrapped = async function () {
      let current = await originalFactory.apply(this, arguments);
      if (!current) current = initialConfig || {};
      return enableAllReferenceTypes(current);
    };
    wrapped.__DOLA_TOOLKIT_ALL_MEDIA__ = true;
    try { inputUploadConfig.uploadPipelineConfig = wrapped; } catch (_) {}
  }

  async function dispatchNativeUpload(file) {
    const context = findInputContext();
    const input = context?.input;
    if (!input) throw new Error('Dola 官网文件入口尚未挂载');
    const transfer = new DataTransfer();
    transfer.items.add(file);
    input.accept = '.jpg,.jpeg,.png,.webp,.apng,.mp4,.mov,.mp3,.wav,image/*,video/*,audio/*';
    input.multiple = true;
    input.value = '';
    input.files = transfer.files;
    input.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
    input.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
  }

  function describeInputContexts() {
    const inputs = Array.from(document.querySelectorAll('input[type="file"]'))
      .filter(node => !node.matches(bridgeSelector))
      .slice(0, 12)
      .map((input, inputIndex) => {
        const fiberKey = Object.keys(input).find(name => name.startsWith('__reactFiber$'));
        let fiber = fiberKey ? input[fiberKey] : null;
        const levels = [];
        for (let depth = 0; fiber && depth < 32; depth += 1, fiber = fiber.return) {
          const props = fiber.memoizedProps || fiber.pendingProps || {};
          const keys = props && typeof props === 'object' ? Object.keys(props).slice(0, 40) : [];
          if (keys.length) levels.push({ depth, keys });
        }
        return {
          inputIndex,
          accept: input.getAttribute('accept') || '',
          multiple: !!input.multiple,
          fiber: !!fiberKey,
          ownKeys: Object.keys(input).filter(key => key.startsWith('__react')).slice(0, 20),
          ancestorReactKeys: (() => {
            const rows = [];
            let node = input.parentElement;
            for (let depth = 1; node && depth <= 8; depth += 1, node = node.parentElement) {
              const keys = Object.keys(node).filter(key => key.startsWith('__react'));
              if (keys.length) rows.push({ depth, tag: node.tagName, keys: keys.slice(0, 20) });
            }
            return rows;
          })(),
          levels,
        };
      });
    const controls = Array.from(document.querySelectorAll('button,[role="button"]'))
      .filter(node => {
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      })
      .slice(-80)
      .map((node, index) => ({
        index,
        text: String(node.innerText || '').trim().slice(0, 120),
        ariaLabel: node.getAttribute('aria-label') || '',
        title: node.getAttribute('title') || '',
        testId: node.getAttribute('data-testid') || '',
        actionKey: node.getAttribute('data-input-engine-actionbar-control-key') || '',
        className: String(node.className || '').slice(0, 180),
        html: String(node.outerHTML || '').slice(0, 420),
      }));
    return { inputs, controls };
  }

  function findComposerAddButton() {
    return Array.from(document.querySelectorAll('button')).find(node => {
      const rect = node.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) return false;
      const text = String(node.innerText || '').trim();
      const pathData = Array.from(node.querySelectorAll('svg path'))
        .map(path => path.getAttribute('d') || '').join(' ');
      return !text && /V10\.9951H2|V11H3|M12(?:\.0005)?\s+2\.25/.test(pathData);
    }) || null;
  }

  function readMediaDuration(file, kind) {
    return new Promise(resolve => {
      if (!['audio', 'video'].includes(kind)) return resolve(0);
      const url = URL.createObjectURL(file);
      const media = document.createElement(kind);
      let settled = false;
      const finish = value => {
        if (settled) return;
        settled = true;
        URL.revokeObjectURL(url);
        resolve(Number.isFinite(value) && value > 0 ? value : 0);
      };
      const timer = setTimeout(() => finish(0), 5000);
      media.preload = 'metadata';
      media.onloadedmetadata = () => { clearTimeout(timer); finish(media.duration); };
      media.onerror = () => { clearTimeout(timer); finish(0); };
      media.src = url;
    });
  }

  async function initMediaUploader(runtime, kind) {
    if (!['audio', 'video'].includes(kind)) return;
    const service = await getServiceApi(runtime)?.D?.('attachmentUploadStoreService');
    const config = getMediaConfig(runtime);
    const base = config?.tV;
    const scene = kind === 'audio' ? config?.Ew : config?.eq;
    if (!service?.initUploader || !base || !scene) {
      throw new Error(`${kind === 'audio' ? '音频' : '视频'} uploader 初始化失败`);
    }
    await service.initUploader({
      mediaUploaderOptions: {
        ...base,
        scene,
        resourcePrepareUploadParams: { ...(base.resourcePrepareUploadParams || {}), scene_id: scene }
      }
    });
  }

  function botId(runtime, chatId) {
    try { return runtime(372595)?.S?.(chatId, true) || runtime(29155)?.IO?.(runtime(986344)?.NW?.getState?.()) || ''; }
    catch (_) { return ''; }
  }

  function taskFailed(task) {
    const status = String(task?.status || task?.state || '').toLowerCase();
    return !!(task?.failed || task?.error || ['failed', 'error', 'retry', 'rejected', 'cancelled', 'canceled'].includes(status));
  }

  function taskLoading(task) {
    const status = String(task?.status || task?.state || '').toLowerCase();
    return !!(task?.loading || ['uploading', 'pending', 'loading', 'processing', 'preprocessing'].includes(status));
  }

  function taskFailureDetail(task) {
    if (!task || typeof task !== 'object') return '';
    const source = task.error || task.err || task.reason || task.message ||
      task.uploadResult?.error || task.uploadResult?.message || task.ctx?.error || '';
    if (typeof source === 'string') return source;
    try {
      return JSON.stringify(source, (key, value) => {
        if (key === 'file' || key === 'blob' || key === 'controller') return undefined;
        return value;
      }).slice(0, 2000);
    } catch (_) {
      return String(source || '');
    }
  }

  function attachmentKey(attachment, task) {
    const result = task?.uploadResult || {};
    return String(attachment?.key || attachment?.fileKey || attachment?.media?.key || task?.key || task?.fileKey ||
      result.FileKey || result.file_key || result?.media?.key || '').trim();
  }

  async function waitForUpload(runtime, chatId, beforeKeys, file) {
    const deadline = Date.now() + 10 * 60 * 1000;
    const kind = materialKind(file);
    while (Date.now() < deadline) {
      const attachments = getStoreState(runtime, 'attachmentsStore')?.attachmentsMap?.[chatId]?.[`_${VIDEO_SKILL}`] || [];
      const tasks = getStoreState(runtime, 'attachmentUploadStore')?.preprocessTaskMap || {};
      const attachment = attachments.find(item => {
        if (beforeKeys.has(item?.localKey || '')) return false;
        const name = item?.fileName || item?.file?.name || '';
        return attachmentKind(item) === kind && (!name || name === file.name);
      });
      if (attachment) {
        const task = tasks[attachment.localKey];
        if (taskFailed(task)) {
          const detail = taskFailureDetail(task);
          const state = String(task?.status || task?.state || 'failed');
          throw new Error(`${file.name} 上传失败（${state}）${detail ? `：${detail}` : ''}`);
        }
        const key = attachmentKey(attachment, task);
        if (key && !taskLoading(task)) return { attachment, key };
      }
      await sleep(300);
    }
    throw new Error(`${file.name} 上传超时`);
  }

  function snapshot(runtime, chatId) {
    const rawAttachments = getStoreState(runtime, 'attachmentsStore')?.attachmentsMap?.[chatId]?.[`_${VIDEO_SKILL}`] || [];
    const tasks = getStoreState(runtime, 'attachmentUploadStore')?.preprocessTaskMap || {};
    const attachments = [];
    const counts = { image: 0, video: 0, audio: 0 };
    let pending = 0;
    const keys = [];
    const states = [];
    for (const attachment of rawAttachments) {
      const task = tasks[attachment?.localKey] || {};
      // Dola keeps failed cards in attachmentsMap after the red exclamation
      // appears. They are UI history, not uploadable native references. Counting
      // them poisoned the next task (1 fresh image looked like 2) and could make
      // a later protocol round serialize a rejected attachment.
      if (taskFailed(task)) continue;
      attachments.push(attachment);
      const kind = attachmentKind(attachment);
      if (kind in counts) counts[kind] += 1;
      const result = task.uploadResult || {};
      const meta = result.VideoMeta || {};
      const extra = task?.ctx?.extraParams || {};
      const key = attachmentKey(attachment, task);
      if (ACCEPTED_TYPES.includes(kind) && (!key || taskLoading(task))) pending += 1;
      keys.push(key || undefined);
      states.push({
        type: kind,
        localKey: attachment?.localKey || '',
        fileKey: key || undefined,
        fileName: attachment?.fileName || attachment?.file?.name || meta.FileName || '',
        size: Number(attachment?.size || attachment?.file?.size || meta.Size || 0),
        md5: attachment?.md5 || task?.md5 || meta.Md5,
        mediaDuration: Number(attachment?.mediaDuration || meta.Duration || extra.duration_seconds || 0),
        mediaResourceUri: attachment?.mediaResourceUri || task?.mediaResourceUri,
        mediaCoverUrl: attachment?.mediaCoverUrl || extra.cover_local_url || task?.blobUrl || '',
        mediaCoverWidth: Number(attachment?.mediaCoverWidth || extra.cover_width || meta.Width || 0),
        mediaCoverHeight: Number(attachment?.mediaCoverHeight || extra.cover_height || meta.Height || 0),
        imageWidth: Number(attachment?.imageWidth || result.ImageWidth || 0),
        imageHeight: Number(attachment?.imageHeight || result.ImageHeight || 0),
        url: attachment?.url || task?.blobUrl || '',
        src: attachment?.src || task?.blobUrl || '',
        uploadPercent: Number.isFinite(Number(task?.percent)) ? Number(task.percent) : 100,
        status: taskFailed(task) ? 'Retry' : taskLoading(task) ? 'Uploading' : 'Normal'
      });
    }
    return { attachments, keys, states, counts, pending };
  }

  function effectiveSnapshot(runtime, chatId) {
    const current = snapshot(runtime, chatId);
    if (current.attachments.length || current.pending) return current;
    if (preservedSnapshot && Date.now() - preservedAt <= PRESERVE_TTL_MS && preservedChatId === chatId) return preservedSnapshot;
    return current;
  }

  function attachmentCounts(blocks) {
    const counts = { image: 0, video: 0, audio: 0 };
    for (const block of blocks || []) {
      for (const item of block?.content?.attachment_block?.attachments || []) {
        if (Number(item?.type) === 1) counts.image += 1;
        if (Number(item?.type) === 2 && Number(item?.media?.media_type) === 1) counts.audio += 1;
        if (Number(item?.type) === 2 && Number(item?.media?.media_type) === 2) counts.video += 1;
      }
    }
    return counts;
  }

  function injectPreservedRequest(request) {
    const memoryValid = preservedSnapshot?.attachments?.length && !preservedSnapshot.pending &&
      Date.now() - preservedAt <= PRESERVE_TTL_MS && activeRuntime;
    const persisted = readPersistedBlock();
    const valid = Boolean(memoryValid || persisted?.block);
    window.__DOLA_TOOLKIT_LAST_MEDIA_INJECT__ = {
      attemptedAt: Date.now(), valid: Boolean(valid), hasRequest: Boolean(request),
      hasMessages: Array.isArray(request?.messages), hasOption: Boolean(request?.option),
      preservedCounts: preservedSnapshot?.counts || persisted?.counts || null,
      source: memoryValid ? 'memory' : persisted?.block ? 'session' : 'none'
    };
    if (!valid || !request || !Array.isArray(request.messages)) return request;
    const builder = memoryValid ? findBlockBuilder(activeRuntime) : null;
    const generated = memoryValid ? builder?.({ attachments: preservedSnapshot.attachments,
      attachmentKeys: preservedSnapshot.keys, attachmentStates: preservedSnapshot.states }) : persisted.block;
    if (!generated) {
      window.__DOLA_TOOLKIT_LAST_MEDIA_INJECT__.reason = 'block_builder_missing';
      return request;
    }
    const expected = preservedSnapshot?.counts || persisted?.counts || { image: 0, video: 0, audio: 0 };
    const existingBlocks = request.messages.flatMap(message => message?.content_block || message?.content_blocks_v2 || [])
      .filter(block => Number(block?.block_type) === ATTACHMENT_BLOCK);
    const existing = attachmentCounts(existingBlocks);
    if (countsMeet(existing, expected)) {
      window.__DOLA_TOOLKIT_LAST_MEDIA_INJECT__.reason = 'already_present';
      return request;
    }
    const messages = request.messages.map(message => ({ ...message }));
    let index = -1;
    for (let i = messages.length - 1; i >= 0; i -= 1) {
      const blocks = messages[i]?.content_block || messages[i]?.content_blocks_v2 || [];
      if (blocks.some(block => Number(block?.block_type) === 10000)) { index = i; break; }
    }
    if (index < 0) {
      window.__DOLA_TOOLKIT_LAST_MEDIA_INJECT__.reason = 'text_block_missing';
      return request;
    }
    const key = Array.isArray(messages[index].content_block) ? 'content_block' : 'content_blocks_v2';
    const blocks = Array.isArray(messages[index][key]) ? [...messages[index][key]] : [];
    const oldIndex = blocks.findIndex(block => Number(block?.block_type) === ATTACHMENT_BLOCK);
    if (oldIndex >= 0) blocks[oldIndex] = generated;
    else blocks.unshift(generated);
    messages[index][key] = blocks;
    window.__DOLA_TOOLKIT_LAST_MEDIA_SUBMIT__ = { patched: true, source: 'preserved_expert_block',
      counts: attachmentCounts([generated]) };
    window.__DOLA_TOOLKIT_LAST_MEDIA_INJECT__.reason = 'injected';
    window.__DOLA_TOOLKIT_LAST_MEDIA_INJECT__.messageKey = key;
    // sendMessage first emits an intermediate message object and Dola later
    // builds the signed request with `option`.  Clearing here used to make the
    // image disappear between those two stages.  Keep the snapshot through
    // the intermediate pass and retire it only after the final request has
    // synchronously reached the signing hook.
    if (request.option && typeof request.option === 'object' && !batchActive()) {
      const consumed = preservedSnapshot;
      setTimeout(() => {
        if (preservedSnapshot !== consumed) return;
        preservedSnapshot = null;
        preservedAt = 0;
        preservedChatId = '';
        try { sessionStorage.removeItem(PRESERVED_BLOCK_KEY); } catch (_) {}
      }, 0);
    }
    return { ...request, messages };
  }

  function findBlockBuilder(runtime) {
    try { if (typeof runtime(597338)?.S === 'function') return runtime(597338).S; } catch (_) {}
    for (const [rawId, factory] of Object.entries(runtime.m || {})) {
      const source = String(factory || '');
      if (!/attachmentStates/.test(source) || !/attachmentKeys/.test(source) || !/attachment_block/.test(source)) continue;
      try {
        const module = runtime(rawId);
        if (typeof module?.S === 'function') return module.S;
      } catch (_) {}
    }
    return null;
  }

  async function installSubmitPatch(runtime, chatId) {
    const serviceApi = getServiceApi(runtime);
    const chatIMService = await serviceApi?.D?.('chatIMService');
    const messageService = await chatIMService?.getMessageService?.();
    if (!messageService || typeof messageService.sendMessage !== 'function') return false;
    if (messageService.sendMessage.__DOLA_TOOLKIT_MEDIA_PATCHED__) return true;
    const original = messageService.sendMessage;
    const patched = function (request, ...args) {
      // Preserve the uploaded 10052 block across the real Expert UI remount.
      // This helper understands both current `content_block` and legacy
      // `content_blocks_v2` message shapes.
      const preservedRequest = injectPreservedRequest(request);
      if (preservedRequest !== request) return original.call(this, preservedRequest, ...args);
      const effectiveChatId = activeChatId || chatId;
      const snap = effectiveSnapshot(runtime, effectiveChatId);
      const usingPreserved = snap === preservedSnapshot;
      if (!snap.attachments.length || snap.pending) return original.call(this, request, ...args);
      const messages = Array.isArray(request?.messages) ? request.messages : [];
      const nativeBlocks = messages.flatMap(message => message?.content_blocks_v2 || [])
        .filter(block => Number(block?.block_type) === ATTACHMENT_BLOCK);
      const native = attachmentCounts(nativeBlocks);
      if (native.image >= snap.counts.image && native.video >= snap.counts.video && native.audio >= snap.counts.audio) {
        return original.call(this, request, ...args);
      }
      const builder = findBlockBuilder(runtime);
      const generated = builder?.({ attachments: snap.attachments, attachmentKeys: snap.keys, attachmentStates: snap.states });
      if (!generated) throw new Error('素材已上传，但无法建立 Dola 原生引用块');
      const submitted = attachmentCounts([generated]);
      if (submitted.audio < snap.counts.audio || submitted.video < snap.counts.video) {
        throw new Error('音频/视频尚未进入生成请求，请稍后重试');
      }
      const cloned = messages.map(message => ({ ...message, content_blocks_v2: [...(message.content_blocks_v2 || [])] }));
      // If a compatibility patch must build 10052 itself, prefer the current
      // text draft so both blocks remain in the same atomic completion request.
      let index = -1;
      for (let i = cloned.length - 1; i >= 0; i -= 1) {
        if ((cloned[i]?.content_blocks_v2 || []).some(block => Number(block?.block_type) === 10000)) {
          index = i;
          break;
        }
      }
      if (index < 0) index = cloned.findIndex(message => {
        try {
          const ability = typeof message?.ext?.chat_ability === 'string'
            ? JSON.parse(message.ext.chat_ability) : message?.ext?.chat_ability;
          return Number(ability?.ability_type) === VIDEO_SKILL;
        } catch (_) { return false; }
      });
      if (index < 0) index = Math.max(0, cloned.length - 1);
      if (!cloned[index]) return original.call(this, request, ...args);
      const blockIndex = cloned[index].content_blocks_v2.findIndex(block => Number(block?.block_type) === ATTACHMENT_BLOCK);
      if (blockIndex >= 0) cloned[index].content_blocks_v2[blockIndex] = generated;
      else cloned[index].content_blocks_v2.unshift(generated);
      window.__DOLA_TOOLKIT_LAST_MEDIA_SUBMIT__ = { patched: true, counts: submitted };
      const result = original.call(this, { ...request, messages: cloned }, ...args);
      // Do not clear a preserved snapshot on this intermediate call.  The
      // final signed request consumes it in injectPreservedRequest().
      return result;
    };
    patched.__DOLA_TOOLKIT_MEDIA_PATCHED__ = true;
    if (original.__DOUBAO_DOLA_RESOLUTION_BRIDGE__) {
      patched.__DOUBAO_DOLA_RESOLUTION_BRIDGE__ = true;
    }
    messageService.sendMessage = patched;
    return true;
  }

  function expectedCounts(value) {
    const source = value && typeof value === 'object' ? value : {};
    return {
      image: Math.max(0, Number(source.image || 0)),
      video: Math.max(0, Number(source.video || 0)),
      audio: Math.max(0, Number(source.audio || 0))
    };
  }

  function countsMeet(actual, expected) {
    return ['image', 'video', 'audio'].every(kind => Number(actual?.[kind] || 0) >= Number(expected?.[kind] || 0));
  }

  function bindCurrentNativeContext() {
    const runtime = activeRuntime || getRuntime();
    const props = findInputProps();
    if (!runtime || !props?.chatId) return null;
    activeRuntime = runtime;
    activeChatId = props.chatId;
    captureMediaSnapshot = () => snapshot(activeRuntime, activeChatId);
    return { runtime, chatId: activeChatId };
  }

  async function ensureNativeMedia(rawExpected, options = {}) {
    const expected = expectedCounts(rawExpected);
    const expectedTotal = Object.values(expected).reduce((sum, value) => sum + value, 0);
    if (expectedTotal < 1) return { ok: true, ready: true, pending: 0, counts: expected, attachmentCount: 0, source: 'none' };
    const timeoutMs = Math.max(1000, Math.min(60000, Number(options?.timeoutMs || 25000)));
    const deadline = Date.now() + timeoutMs;
    let last = null;
    let submitPatched = false;
    while (Date.now() < deadline) {
      const persisted = readPersistedBlock();
      if (persisted?.block && countsMeet(persisted.counts, expected)) {
        return { ok: true, ready: true, pending: 0, counts: { ...persisted.counts },
          attachmentCount: Object.values(persisted.counts || {}).reduce((sum, value) => sum + Number(value || 0), 0),
          submitPatched, serialized: true, source: 'persisted_native_block' };
      }
      const context = bindCurrentNativeContext();
      if (context) {
        if (!submitPatched) submitPatched = await installSubmitPatch(context.runtime, context.chatId);
        const current = effectiveSnapshot(context.runtime, context.chatId);
        last = current;
        if (submitPatched && current.pending === 0 && countsMeet(current.counts, expected)) {
          preservedSnapshot = current;
          preservedAt = Date.now();
          preservedChatId = context.chatId;
          return { ok: true, ready: true, pending: 0, counts: { ...current.counts },
            attachmentCount: current.attachments.length, submitPatched: true, source: 'native_store' };
        }
      }
      await sleep(250);
    }
    return { ok: false, ready: false, pending: Number(last?.pending || expectedTotal),
      counts: { ...(last?.counts || { image: 0, video: 0, audio: 0 }) },
      attachmentCount: Number(last?.attachments?.length || 0), submitPatched,
      reason: submitPatched ? 'native_attachment_not_ready' : 'native_submit_patch_unavailable', source: 'native_store' };
  }

  ensureNativeMediaRunner = ensureNativeMedia;
  bindCurrentNativeContextRunner = bindCurrentNativeContext;
  buildNativeBlockRunner = (snapshotValue) => {
    const builder = activeRuntime && findBlockBuilder(activeRuntime);
    return builder?.({ attachments: snapshotValue.attachments,
      attachmentKeys: snapshotValue.keys, attachmentStates: snapshotValue.states }) || fallbackNativeBlock(snapshotValue);
  };
  injectPreservedRequestRunner = injectPreservedRequest;

  try {
    const bridge = document.querySelector(bridgeSelector);
    const files = Array.from(bridge?.files || []);
    if (!files.length) return { ok: false, reason: '没有读到待上传素材' };
    // The bridge input is removed after preparation. Retain its real File
    // objects now; the generic native-input listener intentionally ignores it.
    lastNativeFiles = uniqueFiles([...lastNativeFiles, ...files]);
    // Dola 是分阶段水合的 SPA：页面外壳、视频模式、React fiber 和
    // upload pipeline 会在慢网络下先后出现。不能在第一次查找失败时
    // 立即返回，否则工作台会误以为素材已处理。
    const hydrationDeadline = Date.now() + 150000;
    let props = findInputProps();
    let addButtonClicked = false;
    while ((!props || Number(props.skill?.skill_type) !== VIDEO_SKILL) && Date.now() < hydrationDeadline) {
      if (!props && !addButtonClicked) {
        const addButton = findComposerAddButton();
        if (addButton) {
          addButton.click();
          addButtonClicked = true;
        }
      }
      await sleep(1000);
      props = findInputProps();
    }
    if (!props) {
      const addButton = findComposerAddButton();
      if (addButton) {
        addButton.click();
        await sleep(500);
      }
      const debugRuntime = getRuntime();
      let knownUploadModule = null;
      try {
        const module = debugRuntime?.(784675);
        knownUploadModule = {
          keys: Object.keys(module || {}),
          configType: typeof module?.videoUploadPipelineConfig,
          configKeys: Object.keys(module?.videoUploadPipelineConfig || {}).slice(0, 80),
        };
      } catch (error) {
        knownUploadModule = { error: String(error?.message || error) };
      }
      const chunkGlobals = Object.keys(window)
        .filter(key => /webpack|chunk|loadable|modern/i.test(key))
        .slice(0, 80)
        .map(key => {
          let value;
          try { value = window[key]; } catch (_) { value = null; }
          return {
            key,
            type: typeof value,
            array: Array.isArray(value),
            length: Array.isArray(value) ? value.length : undefined,
            keys: value && typeof value === 'object' ? Object.keys(value).slice(0, 30) : [],
          };
        });
      return {
        ok: false,
        reason: '未找到 Dola 视频上传上下文',
        detail: JSON.stringify({
          addButtonFound: !!addButton,
          runtime: typeof debugRuntime,
          runtimeKeys: Object.keys(debugRuntime || {}).slice(0, 80),
          runtimeFactoryCount: Object.keys(debugRuntime?.m || {}).length,
          uploadRunner: findUploadRunner(debugRuntime)?.id || null,
          knownUploadModule,
          attachmentChatIds: Object.keys(getStoreState(debugRuntime, 'attachmentsStore')?.attachmentsMap || {}),
          chunkGlobals,
          afterAddClick: describeInputContexts()
        })
      };
    }
    if (Number(props.skill?.skill_type) !== VIDEO_SKILL) return { ok: false, reason: '当前页面尚未进入视频创作模式' };
    let runtime = getRuntime();
    while (!runtime && Date.now() < hydrationDeadline) {
      await sleep(1000);
      runtime = getRuntime();
    }
    if (!runtime) return { ok: false, reason: '未找到 Dola Webpack 运行时' };
    activeRuntime = runtime;
    activeChatId = props.chatId || '';
    captureMediaSnapshot = () => snapshot(activeRuntime, activeChatId);
    const inputUploadConfig = props.inputUploadConfig || {};
    let pipelineConfig = await inputUploadConfig.uploadPipelineConfig?.();
    while (!pipelineConfig && Date.now() < hydrationDeadline) {
      await sleep(1000);
      pipelineConfig = await inputUploadConfig.uploadPipelineConfig?.();
    }
    if (!pipelineConfig) return { ok: false, reason: 'Dola 上传配置尚未加载' };
    enableAllReferenceTypes(pipelineConfig);
    await installNativeUploadFallback(inputUploadConfig, pipelineConfig);
    let runner = await resolveUploadRunner(runtime);
    // 新版页面已经移除了旧 module 特征时，不再傻等完整的 150 秒水合窗口。
    // 给动态 chunk 12 秒加载机会，随后立即转官网 input 兜底。
    const runnerDeadline = Math.min(hydrationDeadline, Date.now() + 12000);
    while (!runner && Date.now() < runnerDeadline) {
      await sleep(1500);
      runner = await resolveUploadRunner(runtime);
    }
    // Dola 会不定期调整 shared-upload-pipeline 的 chunk/module id。模块扫描不到时，
    // 不再丢弃音频和视频：把文件交给官网 React input 的 onChange，让当前版本的
    // Composer 自己调用上传管线；仍然使用 attachmentsStore + fileKey 做最终校验。
    const transport = runner ? 'internal_pipeline' : 'native_react_input';
    if (!runner && !findInputContext()?.input) {
      return { ok: false, reason: '未找到 Dola 官方上传管线，且官网文件入口尚未挂载' };
    }

    for (const file of files) {
      const kind = materialKind(file);
      if (!kind) return { ok: false, reason: `不支持的素材格式：${file.name}` };
      const existing = getStoreState(runtime, 'attachmentsStore')?.attachmentsMap?.[props.chatId]?.[`_${VIDEO_SKILL}`] || [];
      const beforeKeys = new Set(existing.map(item => item?.localKey).filter(Boolean));
      try {
        await initMediaUploader(runtime, kind);
      } catch (error) {
        // 官网的 runConfiguredUploadPipeline / React onChange 会按当前素材类型
        // 初始化 uploader。私有 media config 改名时不能在调用官方管线前先行阻断。
      }
      const duration = await readMediaDuration(file, kind);
      const uploadPipelineConfig = {
        ...pipelineConfig,
        buildAttachmentCtx: ({ file: currentFile, skill, uploadCollectionId }) => {
          const skillType = Number(skill?.skill_type || VIDEO_SKILL);
          return {
            conversationId: props.chatId || '',
            chatType: 'other_default',
            skillType,
            skillId: String(skillType),
            botId: botId(runtime, props.chatId),
            from: '',
            uploadCollectionWay: 'single',
            uploadId: '',
            uploadWay: 'browse_folder',
            uploadFrom: 'video_generation',
            uploadCollectionId,
            uploadType: String(currentFile?.type || ''),
            extraParams: duration > 0 ? { duration_seconds: String(duration) } : {}
          };
        }
      };
      if (runner) {
        await runner.run({
          inputUploadConfig,
          trigger: 'click',
          chatId: props.chatId,
          chatType: props.chatType,
          skill: props.skill,
          reportParams: props.reportParams,
          files: [file],
          inputConfigKey: props.inputConfigKey,
          attachmentChannel: VIDEO_SKILL,
          targetAttachmentChannel: VIDEO_SKILL,
          uploadPipelineConfig
        });
      } else {
        await dispatchNativeUpload(file);
      }
      await waitForUpload(runtime, props.chatId, beforeKeys, file);
    }

    const submitPatched = await installSubmitPatch(runtime, props.chatId);
    const result = snapshot(runtime, props.chatId);
    // Expert mode mounts another composer/store. Freeze the verified native
    // attachment block and its File objects before the video composer unmounts.
    preservedSnapshot = result;
    preservedFiles = uniqueFiles([...preservedFiles, ...files]);
    preservedAt = Date.now();
    preservedChatId = props.chatId || '';
    const persisted = persistNativeBlock(result);
    window.__DOLA_TOOLKIT_MEDIA_UPLOAD__ = { ok: true, chatId: props.chatId, counts: result.counts,
      submitPatched, serialized: Boolean(persisted?.block), retainedFiles: preservedFiles.length };
    return {
      ok: true,
      chatId: props.chatId,
      counts: result.counts,
      submitPatched,
      serialized: Boolean(persisted?.block),
      retainedFiles: preservedFiles.length,
      transport,
      files: files.map(file => ({ name: file.name, kind: materialKind(file), size: file.size }))
    };
  } catch (error) {
    let taskDebug = '';
    try {
      const attachments = getStoreState(activeRuntime, 'attachmentsStore')?.attachmentsMap?.[activeChatId]?.[`_${VIDEO_SKILL}`] || [];
      const tasks = getStoreState(activeRuntime, 'attachmentUploadStore')?.preprocessTaskMap || {};
      taskDebug = safeDebugJson({
        attachments: attachments.map(item => ({
          type: attachmentKind(item),
          localKey: item?.localKey,
          fileName: item?.fileName || item?.file?.name,
          keys: Object.keys(item || {})
        })),
        tasks: Object.fromEntries(attachments.map(item => [item?.localKey || '', tasks[item?.localKey] || null]))
      });
    } catch (_) {}
    const detail = `${String(error?.stack || error?.message || error)}${taskDebug ? `\nUPLOAD_DEBUG=${taskDebug}` : ''}`;
    window.__DOLA_TOOLKIT_MEDIA_UPLOAD__ = { ok: false, reason: detail };
    return { ok: false, reason: String(error?.message || error), detail };
  }
  }

  async function handleUploadRequest() {
    const bridge = document.querySelector("#dola-toolkit-media-upload-bridge");
    const resultNode = document.querySelector(RESULT_SELECTOR);
    if (!bridge || !resultNode || bridge.dataset.uploadRunning === "1") return;
    bridge.dataset.uploadRunning = "1";
    resultNode.removeAttribute("data-ready");
    resultNode.textContent = "";
    let result;
    try {
      result = await uploadReferenceMedia({ bridgeSelector: "#dola-toolkit-media-upload-bridge" });
    } catch (error) {
      const detail = String(error?.stack || error?.message || error);
      result = { ok: false, reason: String(error?.message || error), detail };
    } finally {
      delete bridge.dataset.uploadRunning;
    }
    resultNode.textContent = JSON.stringify(result || { ok: false, reason: "上传桥没有返回结果" });
    resultNode.setAttribute("data-ready", "1");
    resultNode.dispatchEvent(new Event("change", { bubbles: true }));
  }

  document.addEventListener(REQUEST_EVENT, handleUploadRequest);
  // Prime the runtime/store helpers even when the local console used Dola's
  // native image input instead of this extension's upload bridge.  The async
  // function initializes the closures synchronously, then exits harmlessly
  // because no bridge files are present at document_start.
  void uploadReferenceMedia({ bridgeSelector: "#dola-toolkit-media-upload-bridge" });
  Object.defineProperty(window, "__DOLA_TOOLKIT_MEDIA_SNAPSHOT__", {
    configurable: true,
    value() {
      if (!captureMediaSnapshot) bindCurrentNativeContextRunner?.();
      if (!captureMediaSnapshot) {
        const last = window.__DOLA_TOOLKIT_MEDIA_UPLOAD__;
        return { ready: false, pending: 0, counts: last?.counts || { image: 0, video: 0, audio: 0 } };
      }
      const current = captureMediaSnapshot();
      return {
        ready: current.pending === 0,
        pending: current.pending,
        counts: { ...current.counts },
        attachmentCount: current.attachments.length
      };
    }
  });
  Object.defineProperty(window, "__DOLA_TOOLKIT_MEDIA_ENSURE__", {
    configurable: true,
    value(rawExpected, options) {
      if (typeof ensureNativeMediaRunner === 'function') return ensureNativeMediaRunner(rawExpected, options);
      return Promise.resolve({ ok: false, ready: false, pending: Object.values(rawExpected || {}).reduce((sum, value) => sum + Number(value || 0), 0),
        counts: { image: 0, video: 0, audio: 0 }, attachmentCount: 0, submitPatched: false,
        reason: 'media_upload_bridge_not_initialized', source: 'native_store' });
    }
  });
  Object.defineProperty(window, "__DOLA_TOOLKIT_MEDIA_MODE_SWITCH__", {
    configurable: true,
    value: Object.freeze({
      beginRound(options = {}) {
        const requestId = String(options.requestId || '');
        if (requestId && requestId === mediaRoundId) return this.status();
        mediaRoundId = requestId;
        const reusePreserved = options.reusePreserved === true && Boolean(readPersistedBlock()?.block);
        if (reusePreserved) {
          window.__DOLA_TOOLKIT_LAST_MEDIA_INJECT__ = null;
          window.__DOLA_TOOLKIT_LAST_MEDIA_SUBMIT__ = null;
          return this.status();
        }
        // A completed submission must never lend its serialized attachment
        // block or File objects to the next click. Rebind first so a newly
        // uploaded draft in the current composer becomes the only source.
        const context = bindCurrentNativeContextRunner?.();
        const current = context && captureMediaSnapshot ? captureMediaSnapshot() : null;
        const currentFiles = (current?.attachments || []).map(item => item?.file).filter(Boolean);
        const inputFiles = nativeFileInputs().flatMap(input => Array.from(input.files || []));
        preservedSnapshot = current?.attachments?.length ? current : null;
        preservedFiles = uniqueFiles([...currentFiles, ...inputFiles]);
        preservedInputNodes = new Set(nativeFileInputs());
        preservedAt = preservedSnapshot || preservedFiles.length ? Date.now() : 0;
        preservedChatId = context?.chatId || activeChatId || '';
        try { sessionStorage.removeItem(PRESERVED_BLOCK_KEY); } catch (_) {}
        if (preservedSnapshot && !preservedSnapshot.pending) persistNativeBlock(preservedSnapshot);
        window.__DOLA_TOOLKIT_LAST_MEDIA_INJECT__ = null;
        window.__DOLA_TOOLKIT_LAST_MEDIA_SUBMIT__ = null;
        return this.status();
      },
      preserve() {
        const current = captureMediaSnapshot && activeChatId ? captureMediaSnapshot() : null;
        const snapshotFiles = (current?.attachments || []).map(item => item?.file).filter(Boolean);
        const inputFiles = nativeFileInputs().flatMap(input => Array.from(input.files || []));
        preservedInputNodes = new Set(nativeFileInputs());
        preservedFiles = uniqueFiles([...inputFiles, ...lastNativeFiles, ...snapshotFiles]);
        if (current?.pending) return { ok: false, ready: false, pending: current.pending,
          counts: { ...current.counts }, attachmentCount: Math.max(current.attachments.length, preservedFiles.length) };
        let persisted = null;
        if (current) {
          preservedSnapshot = current;
          persisted = persistNativeBlock(current);
        }
        preservedAt = Date.now();
        preservedChatId = activeChatId || '';
        const counts = fileCounts(preservedFiles);
        if (current) for (const kind of ['image', 'video', 'audio']) counts[kind] = Math.max(counts[kind], Number(current.counts?.[kind] || 0));
        return { ok: true, ready: true, pending: 0, counts,
          attachmentCount: Math.max(preservedFiles.length, Number(current?.attachments?.length || 0)),
          serialized: current?.attachments?.length ? Boolean(persisted?.block) : undefined,
          source: preservedFiles.length ? 'retained_files' : 'native_store' };
      },
      async restore(options = {}) {
        const nativeValid = preservedSnapshot?.attachments?.length && !preservedSnapshot.pending &&
          Date.now() - preservedAt <= PRESERVE_TTL_MS;
        if (nativeValid) {
          return { ok: true, ready: true, pending: 0, counts: { ...preservedSnapshot.counts },
            attachmentCount: preservedSnapshot.attachments.length, source: 'expert_preserved_native_block' };
        }
        const persisted = readPersistedBlock();
        if (persisted?.block) {
          return { ok: true, ready: true, pending: 0, counts: { ...(persisted.counts || {}) },
            attachmentCount: Object.values(persisted.counts || {}).reduce((sum, value) => sum + Number(value || 0), 0),
            source: 'expert_persisted_native_block' };
        }
        const valid = preservedFiles.length > 0 && Date.now() - preservedAt <= PRESERVE_TTL_MS;
        if (!valid) return { ok: false, ready: false, pending: 0, counts: { image: 0, video: 0, audio: 0 },
          attachmentCount: 0, reason: 'retained_files_expired' };
        const deadline = Date.now() + Math.max(3000, Math.min(30000, Number(options.timeoutMs || 20000)));
        let input = null;
        while (!input && Date.now() < deadline) {
          input = nativeFileInputs().filter(node => !preservedInputNodes.has(node)).at(-1) || null;
          if (!input) {
            retainedComposerAddButton()?.click();
            await new Promise(resolve => setTimeout(resolve, 250));
          }
        }
        if (!input) return { ok: false, ready: false, pending: preservedFiles.length,
          counts: fileCounts(preservedFiles), attachmentCount: 0, reason: 'expert_file_input_missing' };
        const transfer = new DataTransfer();
        for (const file of preservedFiles) transfer.items.add(file);
        input.accept = '.jpg,.jpeg,.png,.webp,.apng,.mp4,.mov,.mp3,.wav,image/*,video/*,audio/*';
        input.multiple = true;
        input.value = '';
        input.files = transfer.files;
        input.dispatchEvent(new Event('input', { bubbles: true, composed: true }));
        input.dispatchEvent(new Event('change', { bubbles: true, composed: true }));
        // The upload store is not exposed in every Expert-mode build. Give the
        // native uploader time to obtain its fileKey before enabling Send.
        await new Promise(resolve => setTimeout(resolve, 7000));
        const counts = fileCounts(preservedFiles);
        return { ok: true, ready: true, pending: 0, counts,
          attachmentCount: preservedFiles.length, source: 'expert_native_reupload' };
      },
      status() {
        const persisted = readPersistedBlock();
        const valid = Boolean(((preservedSnapshot || preservedFiles.length) && Date.now() - preservedAt <= PRESERVE_TTL_MS) || persisted?.block);
        const counts = valid && preservedFiles.length ? fileCounts(preservedFiles)
          : preservedSnapshot ? { ...preservedSnapshot.counts } : persisted?.counts ? { ...persisted.counts } : { image: 0, video: 0, audio: 0 };
        return { valid, ready: valid, pending: valid ? Number(preservedSnapshot?.pending || 0) : 0,
          counts, attachmentCount: valid ? Math.max(preservedFiles.length, Number(preservedSnapshot?.attachments?.length || 0)) : 0 };
      },
      completeRound(options = {}) {
        if (options.requestId && mediaRoundId && String(options.requestId) !== mediaRoundId) return false;
        if (options.keepPreserved === true) {
          mediaRoundId = '';
          return true;
        }
        preservedSnapshot = null;
        preservedFiles = [];
        lastNativeFiles = [];
        preservedInputNodes = new Set();
        preservedAt = 0;
        preservedChatId = '';
        mediaRoundId = '';
        try { sessionStorage.removeItem(PRESERVED_BLOCK_KEY); } catch (_) {}
        return true;
      },
      inject(request) { return typeof injectPreservedRequestRunner === 'function' ? injectPreservedRequestRunner(request) : request; },
      clear() { preservedSnapshot = null; preservedFiles = []; lastNativeFiles = []; preservedInputNodes = new Set(); preservedAt = 0; preservedChatId = ''; mediaRoundId = ''; try { sessionStorage.removeItem(PRESERVED_BLOCK_KEY); } catch (_) {} }
    })
  });
  document.documentElement?.setAttribute(READY_ATTRIBUTE, "4");
})();
