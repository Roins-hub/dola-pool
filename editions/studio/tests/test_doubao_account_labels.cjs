const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const test=require('node:test');

test('Doubao labels use persisted groups while action keys keep the original identity',async()=>{
  const accounts=[
    {name:'备用2',category:'标准账号'},
    {name:'2',category:'普通账号'},
    {name:'臣臣',category:'标准账号'},
    {name:'old',category:'default'},
    {name:'<旧名>',category:'普通账号'},
  ];
  const nodes={'#accountPlatform':{value:'doubao'},'#doubaoAccountError':{},'#doubaoAccountNotice':{}};
  let rendered='';
  const esc=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
  const context={window:{},document:{addEventListener(){}},q:selector=>nodes[selector],
    UI:{esc,time:()=>'',render:(selector,value)=>{rendered=value;}},
    table:(headers,rows)=>rows.map(row=>row.join('')).join(''),
    api:async()=>({accounts,capabilities:{enabled:true}})};
  vm.runInNewContext(fs.readFileSync('web/doubao-accounts.js','utf8'),context);
  await context.window.DoubaoAccounts.load();
  assert.match(rendered,/<strong>标准账号01_备用2<\/strong>/);
  assert.match(rendered,/<strong>标准账号02_臣臣<\/strong>/);
  assert.match(rendered,/<strong>普通账号01_2<\/strong>/);
  assert.match(rendered,/<strong>普通账号02_&lt;旧名&gt;<\/strong>/);
  assert.match(rendered,/<strong>old<\/strong>/);
  assert.match(rendered,/data-doubao-check="备用2"/);
  assert.doesNotMatch(rendered,/data-doubao-check="标准账号/);
  assert.match(nodes['#doubaoAccountNotice'].textContent,/试验状态/);
  assert.doesNotMatch(nodes['#doubaoAccountNotice'].textContent,/已开放已验证能力/);
});

test('Doubao shows actionable login and duplicate states instead of raw English status',async()=>{
  const accounts=[
    {name:'logged-out',enabled:false,status:'login_required',diagnostics:{blocked_reason:'login_required'}},
    {name:'alias',enabled:false,status:'duplicate',diagnostics:{blocked_reason:'duplicate',duplicate_of:'<owner>'}},
    {name:'risk',category:'风控',enabled:false,status:'ready'},
  ];
  const nodes={'#accountPlatform':{value:'doubao'},'#doubaoAccountError':{},'#doubaoAccountNotice':{}};
  let rendered='';
  const esc=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
  const context={window:{},document:{addEventListener(){}},q:s=>nodes[s],
    UI:{esc,time:()=>'',render:(selector,value)=>rendered=value},
    table:(headers,rows)=>rows.map(row=>row.join('')).join(''),
    api:async()=>({accounts,capabilities:{enabled:true}})};
  vm.runInNewContext(fs.readFileSync('web/doubao-accounts.js','utf8'),context);
  await context.window.DoubaoAccounts.load();
  assert.match(rendered,/需要恢复登录/);
  assert.match(rendered,/重复账号/);
  assert.match(rendered,/风控停用/);
  assert.match(rendered,/重复于：&lt;owner&gt;/);
  assert.match(rendered,/<button[^>]*data-doubao-toggle="alias"[^>]*disabled/);
  assert.doesNotMatch(rendered,/ · login_required| · duplicate/);
});
