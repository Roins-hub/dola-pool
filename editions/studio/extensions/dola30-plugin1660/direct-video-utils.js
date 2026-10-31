(function () {
  'use strict';
  function normalize(raw) {
    return String(raw || '').replace(/\\+u0026/gi, '&').replace(/\\+u003d/gi, '=')
      .replace(/\\+u002f/gi, '/').replace(/\\+\//g, '/').replace(/&amp;/gi, '&').replace(/\\&/g, '&');
  }
  function resource(raw) {
    const value = normalize(raw);
    try {
      const url = new URL(value);
      if (!/^https?:$/.test(url.protocol) || url.username || url.password ||
          !/^v\d+-dola\.dola\.com$/i.test(url.hostname) || !url.pathname.includes('/video/')) return null;
      if (url.searchParams.get('mime_type') !== 'video_mp4' && !/\.mp4$/i.test(url.pathname)) return null;
      const rendition = (url.searchParams.get('lr') || '').toLowerCase();
      const marked = /(?:^|_)watermark(?:_|$)/.test(rendition) &&
        !/(?:^|_)(?:no|without)_?watermark(?:_|$)|unwatermarked/.test(rendition);
      return { type: 'video', url: value, provenance: 'direct_link', watermark: marked ? 'marked' : 'unknown' };
    } catch { return null; }
  }
  function extract(text, requireTerminator = false) {
    const normalized = normalize(text);
    const found = new Map();
    for (const match of normalized.matchAll(/https?:\/\/[^\s<>"'\\\x00-\x1f]+/gi)) {
      if (requireTerminator && match.index + match[0].length === normalized.length &&
          !/[)\]},;。；，]$/.test(match[0])) continue;
      const item = resource(match[0].replace(/[)\]},;。；，]+$/g, ''));
      if (item) found.set(item.url, item);
      if (found.size >= 32) break;
    }
    return [...found.values()];
  }
  globalThis.DolaDirectVideo = Object.freeze({ resource, extract });
})();
