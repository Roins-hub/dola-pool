const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const html = fs.readFileSync(process.argv[2], 'utf8');
const source = html.match(/<script>\s*([\s\S]*?)<\/script>/)[1].split('/* 启动：')[0];
const nodes = new Map();
function node(selector) {
  if (!nodes.has(selector)) nodes.set(selector, {style:{display:'none'},hidden:true,disabled:false,textContent:'',innerHTML:'',dataset:{addProxy:'single'},value:''});
  return nodes.get(selector);
}
const requests = [];
const ctx = vm.createContext({
  console, localStorage:{getItem:()=>'',setItem(){}},
  document:{querySelector:node,getElementById:id=>node('#'+id),querySelectorAll:()=>[]},
  setInterval(){},clearInterval(){},setTimeout(){},clearTimeout(){},
  fetch:(path,options)=>new Promise((resolve,reject)=>requests.push({path,options,resolve,reject})),
  alert(){},confirm:()=>true,
});
vm.runInContext(source,ctx);
const execute = code => vm.runInContext(code,ctx);
const reply = (index,data,status=200) => requests[index].resolve({ok:status===200,status,text:async()=>JSON.stringify(data)});

(async()=>{
  // All local-only interactions must render even while the network is unavailable.
  execute(`renderAccounts=()=>{globalThis.renders=(globalThis.renders||0)+1;};
    updateSelButtons=()=>{}; filteredNames=['acc1','acc2'];
    filteredAccounts=[{name:'acc1'},{name:'acc2'}];
    accGo(1);toggleAll(true);clearSel();selPage();setGroupFilter('all');setSearch('accounts','acc');`);
  assert.equal(requests.length,0);
  assert.equal(ctx.renders,6);
  assert.deepEqual(Array.from(execute('[...selAccounts]')),['acc1','acc2']);

  const a=execute('api("/test")'),b=execute('api("/test")');
  assert.strictEqual(a,b);assert.equal(requests.length,1);
  assert.equal(node('#requestStatus').hidden,false);
  reply(0,{ok:true});await a;
  assert.equal(node('#requestStatus').hidden,true);
  const again=execute('api("/test")');assert.equal(requests.length,2);
  reply(1,{});await again;

  // Writes are never deduplicated.
  const w1=execute('api("/test",{method:"POST",body:{name:"one"}})');
  const w2=execute('api("/test",{method:"POST",body:{name:"two"}})');
  assert.equal(requests.length,4);reply(2,{});reply(3,{});await Promise.all([w1,w2]);

  const loading=execute('openAddAccount()');
  assert.equal(node('#dlg').style.display,'flex');
  assert(node('#dlgBox').innerHTML.includes('正在加载代理列表'));
  assert(node('#dlgBox').innerHTML.includes('disabled'));
  node('#f_email').value='still-typing@example.test';
  reply(4,{proxies:[{id:'p1',name:'Proxy one',enabled:true}]});await loading;
  assert(node('#dlg [data-add-proxy]').innerHTML.includes('Proxy one'));
  assert.equal(node('#f_email').value,'still-typing@example.test');
  assert.equal(node('#dlg .row .btn-p').disabled,false);

  const cancelled=execute('openAddAccount()');
  execute('hide("dlg");showDlg("other dialog");');
  reply(5,{proxies:[]});await cancelled;
  assert.equal(node('#dlgBox').innerHTML,'other dialog');

  const failure=execute('openBatchAdd()');
  requests[6].reject(new Error('offline'));await failure;
  assert.equal(node('#dlg .row .btn-p').disabled,true);
  assert(node('#dlg [data-add-proxy]').innerHTML.includes('重新加载代理'));
  const retry=execute('retryAddProxyChoices()');
  reply(7,{proxies:[]});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(node('#dlg .row .btn-p').disabled,false);
  assert.equal(node('#requestStatus').hidden,true);
  console.log('local controls, pending GET deduplication, immediate dialogs, input preservation, stale response and retry: passed');
})().catch(e=>{console.error(e);process.exit(1)});
