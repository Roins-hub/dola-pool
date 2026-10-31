/**
 * Pure local Dola BDMS URL signer.
 *
 * Loads captured bdms-sdk.js in a Node VM, initializes window.bdms, then
 * lets the SDK's patched fetch rewrite a URL. The fake fetch captures the
 * rewritten URL without making a network request.
 */
'use strict';

const vm = require('vm');
const fs = require('fs');
const path = require('path');

const SCRIPT_DIR = __dirname;
const BDMS_SDK_PATH = path.join(SCRIPT_DIR, 'bdms-sdk.js');
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36';

const daemon = process.env.DOLA_SIGNER_DAEMON === '1' || process.argv.includes('--daemon');

if (daemon) {
  const readline = require('readline');
  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  rl.on('line', async (line) => {
    const text = String(line || '').trim();
    if (!text) return;
    let rid = '';
    try {
      const req = JSON.parse(text);
      rid = req && req.id ? String(req.id) : '';
      const out = await signUrl(req);
      if (rid) out.id = rid;
      process.stdout.write(JSON.stringify(out) + '\n');
    } catch (e) {
      const err = { error: e && e.stack ? e.stack : String(e) };
      if (rid) err.id = rid;
      process.stdout.write(JSON.stringify(err) + '\n');
    }
  });
} else {
  let input = '';
  process.stdin.setEncoding('utf8');
  process.stdin.on('data', d => input += d);
  process.stdin.on('end', async () => {
    try {
      const req = JSON.parse(input || '{}');
      const out = await signUrl(req);
      process.stdout.write(JSON.stringify(out));
    } catch (e) {
      process.stdout.write(JSON.stringify({ error: e && e.stack ? e.stack : String(e) }));
      process.exitCode = 1;
    }
  });
}

function randomUUID() {
  if (globalThis.crypto && typeof globalThis.crypto.randomUUID === 'function') {
    return globalThis.crypto.randomUUID();
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0;
    return (c === 'x' ? r : (r & 3 | 8)).toString(16);
  });
}

function cookieString(cookies) {
  if (!cookies || typeof cookies !== 'object') return '';
  return Object.entries(cookies).map(([k, v]) => `${k}=${v}`).join('; ');
}

function makeStorage() {
  return new class StorageShim {
    constructor() { this.d = Object.create(null); }
    getItem(k) { return Object.prototype.hasOwnProperty.call(this.d, k) ? this.d[k] : null; }
    setItem(k, v) { this.d[k] = String(v); }
    removeItem(k) { delete this.d[k]; }
    clear() { this.d = Object.create(null); }
    key(i) { return Object.keys(this.d)[i] || null; }
    get length() { return Object.keys(this.d).length; }
  }();
}

function buildCtx(cookies) {
  const eventListeners = Object.create(null);
  const t0 = Date.now();
  const captured = [];
  const ctx = {};

  Object.assign(ctx, {
    Date, Math, Object, Array, String, Number, Boolean, RegExp, Error,
    TypeError, SyntaxError, ReferenceError, URIError, RangeError,
    JSON, parseInt, parseFloat, isNaN, isFinite, decodeURI, encodeURI,
    decodeURIComponent, encodeURIComponent, NaN, Infinity, undefined,
    Promise, Proxy, Reflect, Symbol, Map, Set, WeakMap, WeakSet,
    ArrayBuffer, Uint8Array, Uint8ClampedArray, Int8Array,
    Uint16Array, Int16Array, Uint32Array, Int32Array,
    Float32Array, Float64Array, DataView,
    Intl, BigInt, BigInt64Array, BigUint64Array,
    TextEncoder, TextDecoder, URL, URLSearchParams,
    setTimeout, setInterval, clearTimeout, clearInterval,
  });

  ctx.navigator = {
    userAgent: UA,
    platform: 'Win32', language: 'zh-CN', languages: ['zh-CN', 'zh'],
    hardwareConcurrency: 8, deviceMemory: 8, webdriver: false,
    cookieEnabled: true, vendor: 'Google Inc.', maxTouchPoints: 0,
    appVersion: '5.0', appCodeName: 'Mozilla', appName: 'Netscape',
    product: 'Gecko', productSub: '20030107', vendorSub: '',
    doNotTrack: null,
    plugins: { length: 0, item: () => null, namedItem: () => null, refresh: () => {} },
    mimeTypes: { length: 0, item: () => null, namedItem: () => null },
    connection: { effectiveType: '4g', rtt: 50, downlink: 10, saveData: false },
  };

  ctx.document = {
    cookie: cookieString(cookies), hidden: false, visibilityState: 'visible',
    referrer: '', title: 'Dola', characterSet: 'UTF-8', charset: 'UTF-8',
    readyState: 'complete', domain: 'www.dola.com', compatMode: 'CSS1Compat',
    createElement(tag) {
      const el = {
        style: {}, tagName: (tag || '').toUpperCase(), src: '', async: false,
        appendChild: c => c, removeChild: c => c,
        setAttribute() {}, getAttribute() { return null; },
        addEventListener() {}, removeEventListener() {},
      };
      if (tag === 'canvas') {
        el.getContext = () => ({
          fillRect() {}, clearRect() {}, getImageData() { return { data: new Uint8ClampedArray(4) }; },
          putImageData() {}, measureText() { return { width: 0 }; }, fillText() {},
          beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, fill() {}, arc() {},
          save() {}, restore() {}, scale() {}, translate() {}, rotate() {}, drawImage() {},
        });
      }
      return el;
    },
    getElementById: () => null, getElementsByTagName: () => [], getElementsByClassName: () => [],
    querySelector: () => null, querySelectorAll: () => [], createEvent: () => ({ initEvent() {} }),
    body: { appendChild() {}, removeChild() {}, style: {} },
    head: { appendChild() {} }, documentElement: { style: {} },
    all: [], forms: [], images: [], links: [],
    addEventListener(t, h) { (eventListeners[t] ||= []).push(h); },
    removeEventListener() {},
  };

  ctx.location = {
    href: 'https://www.dola.com/chat/create-image', origin: 'https://www.dola.com',
    protocol: 'https:', host: 'www.dola.com', hostname: 'www.dola.com',
    pathname: '/chat/create-image', search: '', hash: '', port: '',
    assign() {}, replace() {}, reload() {},
  };

  ctx.screen = { width: 1920, height: 1080, availWidth: 1920, availHeight: 1040, colorDepth: 24, pixelDepth: 24 };
  ctx.performance = {
    now: () => Date.now() - t0 - 5000,
    timeOrigin: t0 - 5000,
    timing: { navigationStart: t0 - 5000, domComplete: t0 - 4000, domLoading: t0 - 4500 },
    getEntriesByType: () => [], mark() {}, measure() {},
  };
  ctx.localStorage = makeStorage();
  ctx.sessionStorage = makeStorage();

  ctx.XMLHttpRequest = class XMLHttpRequestShim {
    constructor() { this.readyState = 0; this.status = 200; this.statusText = 'OK'; this.responseText = '{}'; this.response = null; }
    open(method, url) { this.readyState = 1; this._method = method; this._url = url; }
    send() { this.readyState = 4; if (this.onreadystatechange) this.onreadystatechange(); if (this.onload) this.onload(); }
    setRequestHeader() {}
    getResponseHeader() { return null; }
    getAllResponseHeaders() { return ''; }
    abort() {}
  };
  ctx.XMLHttpRequest.UNSENT = 0; ctx.XMLHttpRequest.OPENED = 1; ctx.XMLHttpRequest.DONE = 4;

  ctx.fetch = function fakeFetch(url, opts) {
    const u = typeof url === 'string' ? url : (url && url.url) || String(url);
    captured.push({ url: u, method: opts && opts.method });
    return Promise.resolve({ ok: true, status: 200, text: () => Promise.resolve('{}'), json: () => Promise.resolve({}), headers: new Map() });
  };
  ctx.Request = globalThis.Request;
  ctx.Headers = globalThis.Headers;
  ctx.Response = globalThis.Response;

  ctx.crypto = {
    getRandomValues(arr) { for (let i = 0; i < arr.length; i++) arr[i] = Math.floor(Math.random() * 256); return arr; },
    randomUUID,
    subtle: { digest: () => Promise.resolve(new ArrayBuffer(32)), encrypt: () => Promise.resolve(new ArrayBuffer(32)), decrypt: () => Promise.resolve(new ArrayBuffer(32)) },
  };

  ctx.atob = s => Buffer.from(s, 'base64').toString('binary');
  ctx.btoa = s => Buffer.from(s, 'binary').toString('base64');
  ctx.console = { log() {}, warn() {}, error() {}, info() {}, debug() {} };
  ctx.matchMedia = () => ({ matches: false, addListener() {}, removeListener() {} });
  ctx.getComputedStyle = () => ({});
  ctx.open = () => null;
  ctx.requestAnimationFrame = cb => setTimeout(() => cb(Date.now()), 16);
  ctx.cancelAnimationFrame = clearTimeout;
  ctx.innerWidth = 1920; ctx.innerHeight = 1080; ctx.outerWidth = 1920; ctx.outerHeight = 1080;
  ctx.devicePixelRatio = 2; ctx.isSecureContext = true;
  ctx.screenX = 0; ctx.screenY = 0; ctx.screenLeft = 0; ctx.screenTop = 0;
  ctx.scrollX = 0; ctx.scrollY = 0; ctx.pageXOffset = 0; ctx.pageYOffset = 0;
  ctx.name = ''; ctx.closed = false;
  ctx.addEventListener = (t, h) => { (eventListeners[t] ||= []).push(h); };
  ctx.removeEventListener = () => {};
  ctx.dispatchEvent = ev => { (eventListeners[ev.type] || []).forEach(h => { try { h(ev); } catch (_) {} }); return true; };
  ctx.Event = function Event(t) { this.type = t || ''; };
  ctx.CustomEvent = function CustomEvent(t, o) { this.type = t || ''; if (o) Object.assign(this, o); };
  ctx.Blob = class BlobShim { constructor(_p, o) { this.size = 0; this.type = (o && o.type) || ''; } };
  ctx.File = class FileShim { constructor(_p, n, o) { this.size = 0; this.name = n || ''; this.type = (o && o.type) || ''; } };
  ctx.FormData = class FormDataShim { append() {} };
  ctx.Image = function ImageShim() { return { src: '', onload: null, onerror: null, width: 0, height: 0, complete: false }; };
  ctx.Worker = function WorkerShim() {};
  ctx.WebSocket = function WebSocketShim() {};
  ctx.CSS = { escape: s => s, supports: () => false };

  ctx.window = ctx; ctx.self = ctx; ctx.top = ctx; ctx.parent = ctx; ctx.frames = ctx; ctx.globalThis = ctx;
  ctx.__captured = captured;
  return ctx;
}

function extractBdmsModule(ctx) {
  ctx.self.__LOADABLE_LOADED_CHUNKS__ = { push(x) { ctx.__bdms_chunk = x; } };
  ctx.__LOADABLE_LOADED_CHUNKS__ = ctx.self.__LOADABLE_LOADED_CHUNKS__;
  new vm.Script(fs.readFileSync(BDMS_SDK_PATH, 'utf8'), { filename: 'bdms-sdk.js' }).runInContext(ctx, { timeout: 30000 });
  if (!ctx.__bdms_chunk || !ctx.__bdms_chunk[1] || !ctx.__bdms_chunk[1][341690]) {
    throw new Error('BDMS webpack module 341690 not found');
  }
  return ctx.__bdms_chunk[1][341690];
}

function execBdmsModule(ctx, moduleFn) {
  function req(id) {
    if (id === 288976) return { C: url => url };
    if (id === 515122) return {};
    return {};
  }
  req.r = exports => {
    Object.defineProperty(exports, Symbol.toStringTag, { value: 'Module' });
    Object.defineProperty(exports, '__esModule', { value: true });
  };
  req.d = (exports, defs) => {
    for (const k of Object.keys(defs)) Object.defineProperty(exports, k, { enumerable: true, get: defs[k] });
  };
  req.g = ctx;
  req.o = (obj, prop) => Object.prototype.hasOwnProperty.call(obj, prop);
  moduleFn.call(ctx, {}, {}, req);
  const bdms = ctx.bdms || ctx.window.bdms;
  if (!bdms || typeof bdms.init !== 'function') throw new Error('window.bdms.init not available');
  return bdms;
}

async function signUrl({ url, method = 'POST', headers = {}, body = '{}', cookies = {} }) {
  if (!url) throw new Error('missing url');
  const ctx = buildCtx(cookies);
  vm.createContext(ctx);
  const moduleFn = extractBdmsModule(ctx);
  const bdms = execBdmsModule(ctx, moduleFn);

  bdms.init({
    aid: 495671,
    pageId: 26930,
    paths: ['/alice', '/samantha', '/passport', '/chat/completion', '/im/chain', '/im/chain/single'],
    ic: 0,
    ddrt: 0,
  });

  for (let i = 0; i < 12; i++) {
    ctx.dispatchEvent({ type: 'mousemove', clientX: 380 + i * 7, clientY: 240 + i * 3, timeStamp: Date.now() + i * 25, bubbles: true });
  }
  for (const type of ['mousedown', 'mouseup', 'click']) {
    ctx.dispatchEvent({ type, clientX: 600, clientY: 400, timeStamp: Date.now(), bubbles: true });
  }

  await ctx.fetch(url, { method, headers, body });
  const signed = ctx.__captured.length ? ctx.__captured[ctx.__captured.length - 1].url : url;
  const u = new URL(signed);
  return {
    signed_url: signed,
    a_bogus: u.searchParams.get('a_bogus') || '',
    msToken: u.searchParams.get('msToken') || '',
    x_bogus: u.searchParams.get('X-Bogus') || '',
    signature: u.searchParams.get('_signature') || '',
  };
}

// 一次性模式：签完就退，给父进程收尾留 25ms。
// 守护模式（--daemon）必须保活，否则每次签名都要冷启动 node（约 475ms）。
if (!daemon) {
  setTimeout(() => process.exit(0), 25);
}
