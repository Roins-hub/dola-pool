const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

function harness() {
  const elements = new Map();
  const q = selector => {
    if (!elements.has(selector)) elements.set(selector, {
      textContent:'', disabled:false, value:'', children:[],
      replaceChildren() { this.children = []; },
      append(child) { this.children.push(child); },
      setAttribute(name) { if (name === 'disabled') this.disabled = true; }
    });
    return elements.get(selector);
  };
  const calls = [];
  const context = {
    window:{}, q, showDlg:() => {}, hide:() => {}, refresh:async () => {},
    document:{createElement:() => ({textContent:''})},
    api:async (url, options) => {
      calls.push({url, body:options.body});
      const existing=options.body.ck === 'sessionid=duplicate';
      return {results:[{name:options.body.name,existing}],imported:existing?0:1,skipped:existing?1:0,failed:0};
    }
  };
  vm.runInNewContext(fs.readFileSync('web/ck-batch-import.js', 'utf8'), context);
  return {ui:context.window, q, calls};
}
function file(name, text, size = text.length) { return {name, size, text:async () => text}; }

test('batch continues after invalid files, skips duplicates, and blocks repeated submission', async () => {
  const {ui, q, calls} = harness();
  ui.openBatchImportCK();
  ui.selectBatchCK({id:'batchCKFolder', files:[
    file('first.txt', 'sessionid=first'), file('duplicate.txt', 'sessionid=duplicate'),
    file('empty.txt', ''), file('large.txt', 'ignored', 131073),
    file('<last>.txt', '\ufeffsessionid=last'), file('ignore.png', 'image')
  ]});
  const pending = ui.submitBatchCK();
  await ui.submitBatchCK();
  await pending;
  assert.equal(calls.length, 3);
  assert.ok(calls.every(call=>call.url==='/api/admin/accounts/import-ck-batch'));
  assert.equal(calls[2].body.ck, 'sessionid=last');
  assert.equal(q('#batchCKStatus').textContent, '处理完成：导入 2 个，重复跳过 1 个，失败 2 个。');
  assert.equal(q('#batchCKResults').children.length, 5);
  assert.ok(q('#batchCKResults').children[4].textContent.startsWith('<last>.txt'));
  assert.equal(q('#batchCKSubmit').disabled, true);
  assert.equal(q('#batchCKClose').disabled, false);
  await ui.submitBatchCK();
  assert.equal(calls.length, 3);
});

test('empty or oversized batches make no import requests', async () => {
  const {ui, q, calls} = harness();
  ui.openBatchImportCK();
  await ui.submitBatchCK();
  ui.selectBatchCK({id:'batchCKFiles', files:Array.from({length:101}, (_, i) => file(i+'.txt', 'sessionid=x'))});
  await ui.submitBatchCK();
  assert.equal(q('#batchCKSubmit').disabled, true);
  assert.equal(calls.length, 0);
});

test('batch sends the selected category for every file and unlocks it afterwards', async () => {
  const {ui, q, calls} = harness();
  ui.openBatchImportCK();
  q('#batchCKCategory').value = 'disposable';
  ui.selectBatchCK({id:'batchCKFiles', files:[file('one.txt','sessionid=one'),file('two.txt','sessionid=two')]});
  const pending = ui.submitBatchCK();
  assert.equal(q('#batchCKCategory').disabled,true);
  await pending;
  assert.deepEqual(calls.map(call=>call.body.category), ['disposable','disposable']);
  assert.equal(q('#batchCKCategory').disabled,false);
});
