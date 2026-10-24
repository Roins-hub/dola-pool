/* tests/test_i18n_dom.py 用：在 node 里用一个极简 DOM 跑 web/i18n/i18n.js，
 * 再用面板 index.html 里真实的渲染函数（esc / noTr / egressLabel / noteCell / foldText）往页面里动态插内容，
 * 检查用户 / 上游数据（代理名、备注、dola 原话…）不会被词典改写，面板文案照常翻译。
 * 用法：node i18n_dom_harness.js <仓库根目录>；结果以 JSON 打印到 stdout。 */
'use strict';
const fs = require('fs');
const path = require('path');

const ROOT = process.argv[2];
const I18N_JS = fs.readFileSync(path.join(ROOT, 'web', 'i18n', 'i18n.js'), 'utf8');
const DICT = JSON.parse(fs.readFileSync(path.join(ROOT, 'web', 'i18n', 'vi.json'), 'utf8'));
const PANEL = fs.readFileSync(path.join(ROOT, 'web', 'index.html'), 'utf8');

/* ---------- 极简 DOM ---------- */
const observers = [];
function notify(rec) {
  observers.forEach(o => {
    if (rec.type === 'attributes' && o.opts.attributeFilter && o.opts.attributeFilter.indexOf(rec.attributeName) < 0) return;
    o.queue.push(rec);
    if (!o.scheduled) {
      o.scheduled = true;
      queueMicrotask(() => { o.scheduled = false; const q = o.queue.splice(0); if (q.length) o.cb(q); });
    }
  });
}
class MutationObserver {
  constructor(cb) { this.cb = cb; this.queue = []; this.scheduled = false; }
  observe(target, opts) { this.opts = opts; observers.push(this); }
}

class Text {
  constructor(data) { this.nodeType = 3; this._data = data; this.parentNode = null; }
  get data() { return this._data; }
  set data(v) { this._data = v; notify({ type: 'characterData', target: this }); }
  get parentElement() { return this.parentNode; }
  get textContent() { return this._data; }
}

const VOID = new Set(['input', 'br', 'img', 'hr', 'meta']);
class Element {
  constructor(tag) {
    this.nodeType = 1; this.tagName = tag.toUpperCase(); this.attrs = new Map();
    this.childNodes = []; this.parentNode = null;
    const self = this;
    this.classList = {
      add(c) { const s = self._classes(); s.add(c); self.attrs.set('class', [...s].join(' ')); },
      remove(c) { const s = self._classes(); s.delete(c); self.attrs.set('class', [...s].join(' ')); },
      contains(c) { return self._classes().has(c); }
    };
  }
  _classes() { return new Set((this.attrs.get('class') || '').split(/\s+/).filter(Boolean)); }
  get parentElement() { return this.parentNode; }
  getAttribute(a) { return this.attrs.has(a) ? this.attrs.get(a) : null; }
  setAttribute(a, v) { this.attrs.set(a, String(v)); notify({ type: 'attributes', target: this, attributeName: a }); }
  appendChild(n) {
    n.parentNode = this; this.childNodes.push(n);
    notify({ type: 'childList', target: this, addedNodes: [n] });
    return n;
  }
  get textContent() { return this.childNodes.map(c => c.textContent).join(''); }
  set textContent(v) { this.childNodes = []; this.appendChild(new Text(String(v))); }
  set innerHTML(html) {
    this.childNodes = [];
    const added = parseInto(this, html);
    notify({ type: 'childList', target: this, addedNodes: added });
  }
  matches(sel) {
    return sel.split(',').some(s => {
      s = s.trim();
      let m;
      if ((m = /^\[([\w-]+)="([^"]*)"\]$/.exec(s))) return this.getAttribute(m[1]) === m[2];
      if ((m = /^\.([\w-]+)$/.exec(s))) return this._classes().has(m[1]);
      return this.tagName === s.toUpperCase();
    });
  }
  closest(sel) {
    for (let e = this; e; e = e.parentNode) if (e.nodeType === 1 && e.matches(sel)) return e;
    return null;
  }
  querySelectorAll(sel) {
    const out = [];
    (function rec(n) { n.childNodes.forEach(c => { if (c.nodeType === 1) { if (c.matches(sel)) out.push(c); rec(c); } }); })(this);
    return out;
  }
  addEventListener() {}
}

function decode(s) {
  return s.replace(/&(amp|lt|gt|quot|#39);/g, (_, e) => ({ amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'" }[e]));
}
/** 够用的 HTML 解析：标签、属性（"" / '' / 无值）、文本、实体；<option> 里不允许标签（同浏览器）。 */
function parseInto(root, html) {
  const top = [];
  let cur = root;
  const re = /<\/([\w-]+)\s*>|<([\w-]+)((?:\s+[\w-]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*\/?>|([^<]+)/g;
  let m;
  const add = n => { n.parentNode = cur; cur.childNodes.push(n); if (cur === root) top.push(n); };
  while ((m = re.exec(html))) {
    if (m[1]) {
      for (let e = cur; e && e !== root; e = e.parentNode) if (e.tagName === m[1].toUpperCase()) { cur = e.parentNode; break; }
    } else if (m[2]) {
      const el = new Element(m[2]);
      const ar = /([\w-]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
      let a;
      while ((a = ar.exec(m[3] || ''))) el.attrs.set(a[1], decode(a[2] ?? a[3] ?? a[4] ?? ''));
      add(el);
      if (!VOID.has(m[2].toLowerCase())) cur = el;
    } else if (m[4]) {
      add(new Text(decode(m[4])));
    }
  }
  return top;
}

/* ---------- 全局环境 ---------- */
const html = new Element('html');
const head = new Element('head');
const body = new Element('body');
html.childNodes = [head, body]; head.parentNode = html; body.parentNode = html;
global.document = {
  documentElement: html, head, body, readyState: 'complete', title: 'dola-pool 管理面板',
  createElement: t => new Element(t),
  addEventListener() {},
  querySelectorAll: sel => html.querySelectorAll(sel),
  createTreeWalker(root) {
    const list = [];
    (function rec(n) { n.childNodes.forEach(c => { list.push(c); if (c.nodeType === 1) rec(c); }); })(root);
    let i = 0;
    return { nextNode: () => list[i++] || null };
  }
};
global.window = global;
global.NodeFilter = { SHOW_TEXT: 4, SHOW_ELEMENT: 1 };
global.MutationObserver = MutationObserver;
global.localStorage = { getItem: () => process.argv[3] || 'vi', setItem() {} };
global.location = { reload() {} };
const dialogs = [];
global.alert = m => { dialogs.push(String(m)); };
global.confirm = m => { dialogs.push(String(m)); return false; };
global.prompt = m => { dialogs.push(String(m)); return null; };
global.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve(DICT) });

/** 从 index.html 里按名字取出一个顶层 function 的源码（括号配平）。 */
function panelFn(name) {
  const match = new RegExp('\\n(?:async )?function ' + name + '\\(').exec(PANEL);
  const start = match ? match.index : -1;
  if (start < 0) throw new Error('index.html 里找不到 function ' + name);
  let i = PANEL.indexOf('{', start), depth = 0;
  for (; i < PANEL.length; i++) {
    const c = PANEL[i];
    if (c === '/' && PANEL[i + 1] === '/') {
      i = PANEL.indexOf('\n', i); if (i < 0) throw new Error('unterminated function');
      continue;
    }
    if (c === '/' && PANEL[i + 1] === '*') {
      i = PANEL.indexOf('*/', i + 2) + 1;
      continue;
    }
    // esc() contains quotes inside a regular expression character class.
    if (c === '/' && PANEL[i + 1] === '[') {
      for (i++; i < PANEL.length && PANEL[i] !== '/'; i++) if (PANEL[i] === '\\') i++;
      continue;
    }
    if (c === "'" || c === '"' || c === '`') {
      for (i++; i < PANEL.length && PANEL[i] !== c; i++) if (PANEL[i] === '\\') i++;
      continue;
    }
    if (c === '{') depth++;
    else if (c === '}' && --depth === 0) break;
  }
  return PANEL.slice(start, i + 1);
}

const tick = () => new Promise(r => setTimeout(r, 0));

(async () => {
  // 面板真实的渲染函数
  const OPEN_NOTES = new Set();
  const fns = ['esc', 'noTr', 'dialogText', 'egressLabel', 'noteCell', 'foldText', 'delAccount', 'delProxy', 'delUser', 'recoverAccount', 'rotateProxy'].map(panelFn).join('\n');
  const P = new Function('OPEN_NOTES', 'proxyCache', fns + '\nreturn {esc, noTr, dialogText, egressLabel, noteCell, foldText, delAccount, delProxy, delUser, recoverAccount, rotateProxy};')(OPEN_NOTES, [{id:'p1',name:'代理(默认)'}]);

  new Function(I18N_JS)();
  await tick(); await tick();

  // 用户把代理起名叫「代理(默认)」——正好和面板里「未绑定代理」按钮的文案一样，词典里有这一条
  const NAME = '代理(默认)';
  const out = { dict_has_name: !!DICT[NAME] };
  const box = new Element('div');
  body.appendChild(box);
  await tick();
  const render = async h => { box.innerHTML = h; await tick(); await tick(); return box; };

  // 账号行的代理按钮：绑定了代理显示代理名，没绑显示面板文案
  await render(`<button>${P.noTr(NAME)}</button><button>代理(默认)</button>`);
  out.proxy_button = box.childNodes[0].textContent;
  out.default_button = box.childNodes[1].textContent;

  // 设置代理对话框：代理名下拉选项 + 默认选项
  await render(`<h2>设置账号代理：${P.noTr('acc1')}</h2><select><option value="">默认代理（DOLA_PROXY）</option>`
    + `<option translate="no" value="p1">${P.esc(NAME)}</option></select>`);
  out.dialog_title = box.childNodes[0].textContent;
  const opts = box.childNodes[1].childNodes;
  out.default_option = opts[0].textContent;
  out.proxy_option = opts[1].textContent;

  // 出口限流提示：出口名带代理名；直连时是面板文案
  const accs = [{ effective_egress: 'http://1.2.3.4:8080', proxy_name: NAME }];
  await render('<div>⏳ 出口 <b>' + P.egressLabel('http://1.2.3.4:8080', accs) + '</b> 被上游限流</div>'
    + '<div><b>' + P.egressLabel('direct', accs) + '</b></div>');
  out.egress_proxy = box.childNodes[0].childNodes[1].textContent;
  out.egress_direct = box.childNodes[1].textContent;

  // 账号备注：备注原文不动，悬停提示照常翻译
  await render(P.noteCell({ name: 'acc1', note: NAME }));
  out.note_text = box.childNodes[0].textContent;
  out.note_title = box.childNodes[0].getAttribute('title');

  // dola 原话（任务列表「报错/备注」列）
  await render(P.foldText('t1:cur', 'acc1 · dola：', NAME, 60));
  out.upstream_note = box.childNodes[0].textContent;

  // 同一块再渲染一次（面板每 10 秒重画）、以及受保护文本节点被直接改写，都不应被翻译
  await render(`<button>${P.noTr(NAME)}</button>`);
  const w = document.createTreeWalker(box);
  let n;
  while ((n = w.nextNode()) && n.nodeType !== 3);
  n.data = NAME;
  await tick(); await tick();
  out.rerender = box.childNodes[0].textContent;

  // Run the actual destructive-action entry points. The dialog stub cancels before any API call.
  await P.delAccount('风控账号');
  out.delete_account_confirm = dialogs.at(-1);
  await P.delProxy('p1');
  out.delete_proxy_confirm = dialogs.at(-1);
  await P.delUser('风控账号');
  out.delete_user_confirm = dialogs.at(-1);
  await P.recoverAccount('风控账号','risk');
  out.recover_confirm = dialogs.at(-1);
  await P.rotateProxy('p1');
  out.rotate_confirm = dialogs.at(-1);
  const unusual = '  代理(默认)\n<script>风控</script> {0} $&  ';
  alert(P.dialogText`原因：${unusual}`);
  out.opaque_value = unusual;
  out.opaque_alert = dialogs.at(-1);
  prompt(P.dialogText`原因：${unusual}`, unusual);
  out.opaque_prompt = dialogs.at(-1);
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e && e.stack || e); process.exit(1); });
