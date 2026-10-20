/* dola-pool 管理面板多语言（i18n）
 *
 * 面板源码里的中文就是源语言；web/i18n/<lang>.json 是「中文原文 -> 译文」映射。
 * 本脚本在页面上就地替换文字（文本节点、title / placeholder 等属性、alert / confirm / prompt），
 * 并用 MutationObserver 跟上面板动态渲染的内容，所以面板代码不需要为翻译做任何改动。
 *
 * 匹配顺序：整句精确匹配 -> 带 {0} {1} 占位符的模板 -> 按已知片段拼接（必须把中文全部覆盖才采用）。
 * 不想被翻译的元素加 translate="no" 或 class="notranslate"（如提示词、用户备注）。
 * 新增语言、更新词条的方法见 I18N.md。
 */
(function () {
  'use strict';
  // 语言列表：key = 下拉框选项；dict = 用哪个 json；bilingual = 译文后附中文原文
  var LANGS = {
    'zh': { label: '中文', html: 'zh-CN' },
    'vi': { label: 'Tiếng Việt', html: 'vi', dict: 'vi' },
    'vi-zh': { label: 'Song ngữ Việt + 中文', html: 'vi', dict: 'vi', bilingual: true }
  };
  var LS_KEY = 'dola_lang';
  var ATTRS = ['title', 'placeholder', 'alt', 'aria-label'];
  var CJK = /[一-鿿]/;
  var PH = /\{(\d+)\}/g;

  function pickLang() {
    var saved = null;
    try { saved = localStorage.getItem(LS_KEY); } catch (e) {}
    if (saved && LANGS[saved]) return saved;
    var nav = (navigator.languages && navigator.languages[0]) || navigator.language || '';
    return /^vi\b/i.test(nav) ? 'vi' : 'zh';
  }

  var lang = pickLang();
  var conf = LANGS[lang];
  var dict = new Map();
  var patterns = [];
  var fragRe = null;
  var cache = new Map();
  var ready = !conf.dict;

  function normalize(s) { return s.replace(/\s+/g, ' ').trim(); }
  function escRe(s) { return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }

  function load(data) {
    var frags = [];
    Object.keys(data).forEach(function (src) {
      var dst = data[src];
      if (!dst) return;  // 缺译：保持中文
      if (/\{\d+\}/.test(src)) {
        var parts = src.split(/\{\d+\}/);
        var order = [];
        src.replace(PH, function (_, i) { order.push(+i); });
        patterns.push({
          re: new RegExp('^' + parts.map(escRe).join('([\\s\\S]*?)') + '$'),
          prefix: parts[0], suffix: parts[parts.length - 1], order: order, dst: dst,
          weight: parts.join('').length
        });
      } else {
        dict.set(src, dst);
        frags.push(src);
      }
    });
    patterns.sort(function (a, b) { return b.weight - a.weight; });
    frags.sort(function (a, b) { return b.length - a.length; });
    if (frags.length) fragRe = new RegExp(frags.map(escRe).join('|'), 'g');
  }

  function trLine(raw) {
    var s = normalize(raw);
    if (!CJK.test(s)) return null;
    if (cache.has(s)) return cache.get(s);
    var out = dict.has(s) ? dict.get(s) : null;
    if (out == null) {
      for (var i = 0; i < patterns.length && out == null; i++) {
        var p = patterns[i];
        if (!s.startsWith(p.prefix) || !s.endsWith(p.suffix)) continue;
        var m = p.re.exec(s);
        if (!m) continue;
        var vals = {};
        p.order.forEach(function (idx, k) { vals[idx] = m[k + 1]; });
        out = p.dst.replace(PH, function (_, idx) {
          var v = vals[idx] == null ? '' : vals[idx];
          var t = trLine(v);
          return t == null ? v : t;
        });
      }
    }
    if (out == null && fragRe) {
      var joined = s.replace(fragRe, function (k) { return dict.get(k); });
      if (!CJK.test(joined)) out = joined;  // 中文没被完全覆盖就不采用，免得中越混杂
    }
    if (cache.size > 5000) cache.clear();
    cache.set(s, out);
    return out;
  }

  /** 译一段文字。多行文字优先逐行译（保留换行）；逐行译不全再整句译，仍不行就能译几行译几行。
   *  没有可译内容返回 null。 */
  function tr(text) {
    if (!ready || !conf.dict || text == null) return null;
    text = String(text);
    if (!CJK.test(text)) return null;
    if (text.indexOf('\n') < 0) return trLine(text);
    var changed = false, complete = true;
    var lines = text.split('\n').map(function (line) {
      var t = trLine(line);
      if (t == null) { if (CJK.test(line)) complete = false; return line; }
      changed = true;
      return line.match(/^\s*/)[0] + t;
    });
    if (complete) return lines.join('\n');
    var whole = trLine(text);
    if (whole != null) return whole;
    return changed ? lines.join('\n') : null;
  }

  // 双语：译文后附中文原文；「点」「条」这类单字片段不附，免得满屏括号
  function bi(src, out) {
    if (!conf.bilingual) return out;
    var hanzi = src.match(/[一-鿿]/g);
    return hanzi && hanzi.length >= 2 ? out + ' (' + normalize(src) + ')' : out;
  }

  /* ---------- DOM ---------- */
  var textRec = new WeakMap();   // 文本节点 -> {out}
  var attrRec = new WeakMap();   // 元素 -> {attr: out}

  // textarea 里的文字是用户输入，不翻；但它自己的 placeholder / title 要翻
  function skipped(el, forAttrs) {
    var sel = '[translate="no"],.notranslate,script,style' + (forAttrs ? '' : ',textarea');
    return !el || !!el.closest(sel);
  }

  function doText(node) {
    var v = node.data;
    var rec = textRec.get(node);
    if (rec && rec.out === v) return;  // 我们自己刚写进去的
    if (!CJK.test(v) || skipped(node.parentElement)) return;
    var t = tr(v);
    if (t == null) return;
    var out = v.match(/^\s*/)[0] + bi(v, t) + v.match(/\s*$/)[0];
    textRec.set(node, { out: out });
    node.data = out;
  }

  function doAttrs(el) {
    if (skipped(el, true)) return;
    var rec = attrRec.get(el);
    for (var i = 0; i < ATTRS.length; i++) {
      var a = ATTRS[i];
      var v = el.getAttribute(a);
      if (!v || (rec && rec[a] === v) || !CJK.test(v)) continue;
      var t = tr(v);
      if (t == null) continue;
      var out = a === 'title' ? bi(v, t) : t;
      if (!rec) { rec = {}; attrRec.set(el, rec); }
      rec[a] = out;
      el.setAttribute(a, out);
    }
  }

  function walk(root) {
    if (root.nodeType === 3) { doText(root); return; }
    if (root.nodeType !== 1) return;
    doAttrs(root);
    var w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    var n;
    while ((n = w.nextNode())) {
      if (n.nodeType === 3) doText(n); else doAttrs(n);
    }
  }

  function translateTitle() {
    var t = tr(document.title);
    if (t != null) document.title = t;
  }

  function start() {
    translateTitle();
    walk(document.body);
    new MutationObserver(function (records) {
      records.forEach(function (r) {
        if (r.type === 'childList') r.addedNodes.forEach(walk);
        else if (r.type === 'characterData') doText(r.target);
        else if (r.type === 'attributes') doAttrs(r.target);
      });
    }).observe(document.body, {
      subtree: true, childList: true, characterData: true,
      attributes: true, attributeFilter: ATTRS
    });
    mountSwitchers();
    document.documentElement.classList.remove('i18n-pending');
  }

  /* ---------- alert / confirm / prompt ---------- */
  function wrapDialog(name) {
    var orig = window[name];
    window[name] = function (msg) {
      var args = Array.prototype.slice.call(arguments);
      var t = tr(msg);
      if (t != null) args[0] = conf.bilingual ? t + '\n\n' + msg : t;
      return orig.apply(window, args);
    };
  }

  /* ---------- 语言下拉框：<select data-i18n-switcher></select> ---------- */
  function mountSwitchers() {
    document.querySelectorAll('select[data-i18n-switcher]').forEach(function (sel) {
      if (sel.options.length) return;
      sel.setAttribute('translate', 'no');
      Object.keys(LANGS).forEach(function (k) {
        var o = document.createElement('option');
        o.value = k;
        o.textContent = LANGS[k].label;
        o.selected = k === lang;
        sel.appendChild(o);
      });
      sel.addEventListener('change', function () { setLang(sel.value); });
    });
  }

  function setLang(k) {
    if (!LANGS[k] || k === lang) return;
    try { localStorage.setItem(LS_KEY, k); } catch (e) {}
    location.reload();
  }

  function onReady(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else fn();
  }

  window.I18N = {
    lang: lang,
    languages: LANGS,
    setLang: setLang,
    /** 代码里需要主动翻译时用：I18N.t('中文原文') */
    t: function (s) { var r = tr(s); return r == null ? s : r; }
  };

  document.documentElement.lang = conf.html;
  if (!conf.dict) { onReady(mountSwitchers); return; }

  // 词典加载完之前先把页面藏起来，避免先闪一下中文
  document.documentElement.classList.add('i18n-pending');
  var style = document.createElement('style');
  style.textContent = 'html.i18n-pending body{visibility:hidden}';
  document.head.appendChild(style);
  setTimeout(function () { document.documentElement.classList.remove('i18n-pending'); }, 3000);

  ['alert', 'confirm', 'prompt'].forEach(wrapDialog);

  fetch('i18n/' + conf.dict + '.json', { cache: 'no-cache' })
    .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then(function (data) { load(data); ready = true; })
    .catch(function (e) { console.warn('[i18n] 词典加载失败，保持中文：', e); })
    .then(function () { onReady(start); });
})();
