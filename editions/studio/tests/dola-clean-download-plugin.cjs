const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

class Element {
  constructor(tag = 'div') {
    this.tagName = tag;
    this.children = [];
    this.listeners = {};
    this.disabled = false;
    this.textContent = '';
  }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
}

const context = {window: {}, Element, document: {createElement: tag => new Element(tag)}};
vm.runInNewContext(fs.readFileSync('web/dola-clean-download-plugin.js', 'utf8'), context);
const plugin = context.window.DolaCleanDownloadPlugin;
const all = node => [node, ...node.children.flatMap(all)];
const button = (container, text) => all(container).find(x => x.tagName === 'button' && x.textContent === text);

(async () => {
  const id = 'video_1234567890abcdef';
  assert.equal(plugin.fileUrl(id, 'official'), '/api/admin/tasks/' + id + '/clean-download/file?kind=official');
  assert.throws(() => plugin.taskBase('../other'), /任务 ID/);
  assert.throws(() => plugin.fileUrl(id, 'processed'), /官方原片/);

  const calls = [], downloads = [];
  let status = {official: {available: true, verified: false, reason: '平台未提供原片'}};
  const api = async (url, opts = {}) => {
    calls.push({url, opts});
    return url.endsWith('/status') ? status : {};
  };
  const container = new Element();
  const mounted = plugin.mount({container, task: {id, platform: 'dola', conversation_id: '123'}, api,
    downloadFile: async (...args) => downloads.push(args)});
  await mounted.ready;
  const officialDownload = button(container, '下载官方无水印原片');
  const check = button(container, '同账号同线路查询官方原片');
  assert.equal(officialDownload.disabled, true, 'preview must never enable official download');
  assert.equal(check.disabled, false);
  assert.equal(all(container).filter(x => x.tagName === 'button').length, 2,
    'only official check and verified download controls are visible');
  assert.equal(all(container).filter(x => x.tagName === 'textarea').length, 0);

  await officialDownload.listeners.click();
  assert.equal(downloads.length, 0);
  await check.listeners.click();
  assert.equal(calls.filter(x => x.url.endsWith('/download')).length, 1);
  assert.equal(calls.find(x => x.url.endsWith('/download')).opts.method, 'POST');
  assert.equal(downloads.length, 0);

  status = {official: {available: true, verified: true}};
  await mounted.refresh();
  assert.equal(officialDownload.disabled, false);
  await officialDownload.listeners.click();
  assert.equal(downloads.length, 1);
  assert.ok(downloads[0][0].endsWith('/file?kind=official'));
  assert.ok(!calls.some(x => /\/repair|\/generations|processed/.test(x.url)),
    'plugin must never process locally or submit a new generation');
  mounted.destroy();
  console.log('clean download plugin: verified official original only OK');
})().catch(error => { console.error(error); process.exitCode = 1; });
