const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const test=require('node:test');

async function renderAccounts(accounts){
  const nodes={'#accountPlatform':{value:'doubao'},'#doubaoAccountError':{},'#doubaoAccountNotice':{}};
  let rows=[];
  const esc=value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
  const context={window:{},document:{addEventListener(){}},q:selector=>nodes[selector],
    UI:{esc,time:()=>'',render(){}},table:(headers,items)=>{rows=items;return '';},
    api:async()=>({accounts,capabilities:{enabled:true}})};
  vm.runInNewContext(fs.readFileSync('web/doubao-accounts.js','utf8'),context);
  await context.window.DoubaoAccounts.load();
  return rows.map(row=>row[2]);
}

test('Doubao account rows show each persisted region environment without implying a unique hardware fingerprint',async()=>{
  const cells=await renderAccounts([
    {name:'local',route_region:'本机网络',browser_environment:{status:'matched',region:'本机网络',locale:'zh-CN',timezone_id:'Asia/Shanghai',viewport:{width:1365,height:900},hardware_mode:'native',isolated:true}},
    {name:'hk',route_region:'香港',browser_environment:{status:'matched',region:'香港',locale:'zh-HK',timezone_id:'Asia/Hong_Kong',viewport:{width:1440,height:900},hardware_mode:'native',isolated:true}},
    {name:'tw',route_region:'台湾',browser_environment:{status:'matched',region:'台湾',locale:'zh-TW',timezone_id:'Asia/Taipei',viewport:{width:1280,height:800},hardware_mode:'native',isolated:true}},
  ]);
  assert.match(cells[0],/地区环境：本机网络/);assert.match(cells[0],/zh-CN/);assert.match(cells[0],/Asia\/Shanghai/);assert.match(cells[0],/固定视窗 1365×900/);
  assert.match(cells[1],/地区环境：香港/);assert.match(cells[1],/zh-HK/);assert.match(cells[1],/Asia\/Hong_Kong/);assert.match(cells[1],/固定视窗 1440×900/);
  assert.match(cells[2],/地区环境：台湾/);assert.match(cells[2],/zh-TW/);assert.match(cells[2],/Asia\/Taipei/);assert.match(cells[2],/固定视窗 1280×800/);
  for(const cell of cells){assert.match(cell,/账号资料独立/);assert.match(cell,/硬件标识原生/);assert.doesNotMatch(cell,/唯一指纹|防关联|出口已验证|纯净指纹/);}
});

test('old or mismatched browser environments remain visibly pending rather than borrowing a route label',async()=>{
  const cells=await renderAccounts([
    {name:'old',route_region:'香港'},
    {name:'legacy',route_region:'台湾',browser_environment:{status:'legacy'}},
    {name:'unmatched',route_region:'香港',browser_environment:{status:'unmatched',region:'未匹配'}},
    {name:'invalid',route_region:'香港',browser_environment:{status:'invalid'}},
    {name:'partial',route_region:'台湾',browser_environment:{status:'matched',region:'台湾',locale:'zh-TW'}},
  ]);
  assert.match(cells[0],/环境待更新/);assert.match(cells[1],/环境待更新/);assert.match(cells[2],/环境待配置/);assert.match(cells[3],/环境异常/);assert.match(cells[4],/环境待更新/);
  for(const cell of cells){assert.doesNotMatch(cell,/地区环境：|账号资料独立|硬件标识原生/);}
});

test('browser environment data is escaped in rendered account cells and unsupported isolation is not claimed',async()=>{
  const [cell]=await renderAccounts([{name:'escape',browser_environment:{status:'matched',region:'<img src=x onerror=alert(1)>',locale:'zh-HK<script>',timezone_id:'Asia/Hong_Kong<svg>',viewport:{width:1440,height:900},hardware_mode:'other',isolated:false}}]);
  assert.match(cell,/&lt;img src=x onerror=alert\(1\)&gt;/);assert.match(cell,/zh-HK&lt;script&gt;/);assert.match(cell,/Asia\/Hong_Kong&lt;svg&gt;/);
  assert.doesNotMatch(cell,/<img|<script|<svg|账号资料独立|硬件标识原生/);
  assert.match(cell,/资料隔离未确认/);assert.match(cell,/硬件标识未确认/);
});
