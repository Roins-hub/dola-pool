const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const context={window:{}};vm.runInNewContext(fs.readFileSync('web/ui.js','utf8'),context);
test('structured provider errors show their readable message',()=>{
  assert.equal(context.window.UI.detailError({error_code:'proxy_unavailable',message:'绑定线路认证失败'}),'绑定线路认证失败');
  assert.equal(context.window.UI.detailError({detail:{error_code:'platform_disabled',message:'豆包能力尚未验证'}}),'豆包能力尚未验证');
});
test('validation arrays and legacy string errors remain readable',()=>{
  assert.equal(context.window.UI.detailError([{msg:'请选择时长'},{msg:'请选择平台'}]),'请选择时长；请选择平台');
  assert.equal(context.window.UI.detailError('登录已失效'),'登录已失效');
});
test('unrecognized objects do not expose private metadata or stringify to Object',()=>{
  assert.equal(context.window.UI.detailError({private_proxy:'secret'}),'请求失败，请稍后重试');
});
