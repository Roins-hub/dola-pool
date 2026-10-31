const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

test('Google batch sends selected category once and clears passwords after import', async () => {
  assert.ok(fs.existsSync('web/google-import.js'), 'Google import dialog is missing');
  const elements = new Map();
  const q = key => {
    if (!elements.has(key)) elements.set(key, {value:'', disabled:false, textContent:'', isConnected:true});
    return elements.get(key);
  };
  let dialog = '', calls = [];
  const context = {window:{}, q, showDlg:html => {dialog=html;}, refresh:async()=>{},
    api:async(url, options) => {calls.push({url, options}); return {imported:2,skipped:0};}};
  vm.runInNewContext(fs.readFileSync('web/google-import.js','utf8'), context);
  context.window.openGoogleImport();
  assert.match(dialog, /邮箱----密码----备用邮箱/);
  q('#googleImportText').value='owner@example.com----fixture-password----backup@example.net';
  q('#googleImportCategory').value='disposable';
  const pending = context.window.submitGoogleImport();
  await context.window.submitGoogleImport();
  await pending;
  assert.equal(calls.length,1);
  assert.equal(calls[0].url,'/api/admin/accounts/import-google');
  assert.equal(calls[0].options.body.category,'disposable');
  assert.equal(q('#googleImportText').value,'');
  assert.match(q('#googleImportStatus').textContent,/2/);
  assert.doesNotMatch(q('#googleImportStatus').textContent,/fixture-password/);
});
