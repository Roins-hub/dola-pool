const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const listener = {addListener(){}};
const context = {
  console, URL, TextEncoder, TextDecoder, Uint8Array,
  atob: value => Buffer.from(value, 'base64').toString('binary'),
  btoa: value => Buffer.from(value, 'binary').toString('base64'),
  chrome: {
    runtime: {onInstalled: listener, onStartup: listener, onMessage: listener},
    tabs: {onActivated: listener, onUpdated: listener, onRemoved: listener},
    action: {onClicked: listener},
    debugger: {onDetach: listener, onEvent: listener},
  },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('extensions/dola30/service-worker.js', 'utf8'), context);

const original = 'https://v16-dola.dola.com/video.mp4?lr=unwatermarked&token=signed';
const preview = 'https://v16-dola.dola.com/video.mp4?lr=video_gen_watermark_dyn';
const contradictory = 'https://v16-dola.dola.com/video.mp4?lr=unwatermarked&watermark=1';
const unknown = 'https://v16-dola.dola.com/video.mp4?token=signed';
const encode = url => Buffer.from(url).toString('base64');
const payload = {data:{videos:[
  {main_url: encode(original)},
  {main_url: encode(preview)},
  {man_url: encode(contradictory)},
  {main_url: encode(unknown)},
]}};
context.fixture = payload;
const items = vm.runInContext('extractDolaItems(fixture)', context);
assert.equal(items.filter(item => item.type === 'video').length, 1);
assert.equal(items.find(item => item.type === 'video').url, original);
console.log('Dola extension exposes only explicit verified original URL');
