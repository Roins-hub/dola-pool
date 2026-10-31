const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const context = vm.createContext({ URL });
vm.runInContext(fs.readFileSync(path.join(root, 'direct-video-utils.js'), 'utf8'), context);
const api = context.DolaDirectVideo;
const url = 'http://v16-dola.dola.com/hash/video/tos/mya/id/?a=489823&lr=video_gen_watermark_dyn&mime_type=video_mp4&rc=abc%3D%3D&download=true';
test('markdown CDN link retains signed query and is never unmarked', () => {
  const list = api.extract(`[点击播放 / 下载](${url})`);
  assert.equal(list.length, 1);
  assert.equal(list[0].url, url);
  assert.equal(list[0].watermark, 'marked');
  assert.equal(list[0].provenance, 'direct_link');
});
test('escaped and nested JSON links are recognized', () => {
  for (const raw of [JSON.stringify({url}), JSON.stringify(JSON.stringify({url})), url.replaceAll('/', '\\/').replaceAll('&', '\\u0026'), url.replaceAll('&', '&amp;')]) {
    assert.equal(api.extract(raw)[0]?.url, url, raw);
  }
});
test('plain links deduplicate and missing watermark metadata stays unknown', () => {
  assert.equal(api.extract(`${url}\n${url}`).length, 1);
  assert.equal(api.resource(url.replace('&lr=video_gen_watermark_dyn', '')).watermark, 'unknown');
});
test('reject lookalike hosts, credentials, non-video links and executable schemes', () => {
  for (const raw of [url.replace('dola.com/', 'dola.com.evil.test/'), url.replace('http://', 'http://user:pass@'), 'javascript:alert(1)', 'https://www.dola.com/chat/1', url.replace('/video/', '/image/'), url.replace('video_mp4','image_png')]) {
    assert.equal(api.resource(raw), null);
  }
});
test('Dola capture rejects a raw direct link that has no owning conversation', async () => {
  const attrs = new Map();
  const document = new EventTarget();
  document.documentElement = {setAttribute:(k,v)=>attrs.set(k,v), getAttribute:k=>attrs.get(k), removeAttribute:k=>attrs.delete(k)};
  const ctx = vm.createContext({URL, Event, document, TextDecoder, location:{hostname:'www.dola.com',href:'https://www.dola.com/chat/fixture'}, fetch:()=>Promise.resolve(new Response(JSON.stringify({text:`[下载](${url})`}),{headers:{'content-type':'application/json'}}))});
  ctx.window=ctx;
  for (const file of ['direct-video-utils.js','media-capture-main.js']) vm.runInContext(fs.readFileSync(path.join(root,file),'utf8'),ctx);
  await ctx.fetch('https://www.dola.com/chat/completion');
  for(let i=0;i<8;i++) await new Promise(resolve=>setImmediate(resolve));
  assert.equal(attrs.has('data-dola-direct-videos'), false);
  ctx.location.href='https://www.dola.com/chat/other';
  await ctx.fetch('https://www.dola.com/other');
  assert.equal(attrs.has('data-dola-direct-videos'),false);
});
test('panel uses marked anchors only as resolver inputs and discards old items on navigation', () => {
  let links = [{ href: url }];
  const attrs = new Map();
  const document = new EventTarget();
  document.documentElement = {getAttribute:k=>attrs.get(k)};
  document.querySelectorAll = () => links;
  const items = new Map();
  const ctx = vm.createContext({ URL, document, items, location:{href:'https://www.dola.com/chat/one'},
    MEDIA_RETRY_EVENT:'retry', setTimeout:()=>1,
    MutationObserver:class {observe(){}},
    addItems: list => list.forEach(item=>items.set(item.url,item)), render:()=>{}, syncMediaCaptureState:()=>{} });
  vm.runInContext(fs.readFileSync(path.join(root,'direct-video-utils.js'),'utf8'),ctx);
  const panel=fs.readFileSync(path.join(root,'content-panel.js'),'utf8');
  vm.runInContext(panel.slice(panel.indexOf('  // Direct playback URLs'),panel.indexOf('  function consumeFallbackApis()')),ctx);
  ctx.scanDirectVideos();
  assert.equal(items.size,0);
  const second=url.replace('/hash/', '/second/');
  links.push({href:second}); ctx.scanDirectVideos();
  assert.equal(items.size,0);
  ctx.scanDirectVideos(); assert.equal(items.size,0);
  items.set('resolved-from-old-chat', { url: 'https://cdn.example.test/original.mp4', provenance: 'video_model' });
  // A response batch from the preceding conversation cannot restore its URLs.
  attrs.set('data-dola-direct-videos',JSON.stringify({page:ctx.location.href,items:[{url}]}));
  ctx.location.href='https://www.dola.com/chat/two';links=[];
  ctx.scanDirectVideos();assert.equal(items.size,0);
});
test('stream scanner waits for the end of a signed URL instead of publishing partial links', () => {
  assert.equal(api.extract(url.slice(0,-12), true).length,0);
  assert.equal(api.extract('"'+url+'"',true)[0].url,url);
});
