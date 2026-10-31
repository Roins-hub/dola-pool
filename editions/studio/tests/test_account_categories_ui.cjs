const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {test} = require('node:test');

function harness() {
  const elements = new Map(), rendered = new Map(), calls = [], notifications = [];
  const q = key => {
    if (!elements.has(key)) elements.set(key, {value: '', textContent: '', disabled: false, dataset:{}, style:{display:'none'}, attributes:{},setAttribute(name,value){this.attributes[name]=String(value);},classList:{remove(){}}});
    return elements.get(key);
  };
  q('#accountState').value = 'all';
  q('#accountCategory').value = 'all';
  const data = {
    accounts: [{name:'legacy', scheduling:true}, {name:'day', category:'disposable', scheduling:false}],
    categories: [{id:'long_term', enabled:true, count:1}, {id:'disposable', enabled:false, count:1}]
  };
  const window = {addEventListener(){}};
  const context = {window, q, document:{addEventListener(){},documentElement:{dataset:{}},querySelector:()=>null},
    UI:{esc:String, number:String, time:()=>'', empty:(title)=>title, icon:()=>'',
      render:(key, html)=>rendered.set(key, html), toast:(...args)=>notifications.push(args), detailError:String, setError(){}},
    natSort:rows=>rows.slice(), statusBadge:()=>'', proxyBadge:()=>'', applyProxyData(){},
    table:(head, rows)=>rows.map(row=>row.join('|')).join('\n'),
    api: async(url, options) => {
      if (options) { calls.push({url, options}); if(data.write)await data.write(url, options); if (data.fail) throw new Error('保存失败');
        if(url==='/api/admin/quota-policy'){
          data.quota_policy={mode:options.body.mode,limit:options.body.mode==='fast'?4:2};
          data.accounts.forEach(account=>account.limit=data.quota_policy.limit);
          return {ok:true,...JSON.parse(JSON.stringify(data))};
        } else if(url.includes('/account-categories/')) {
          const group=data.categories.find(c=>url.endsWith(c.id));group.enabled=options.body.enabled;
          data.accounts.filter(a=>(a.category||'long_term')===group.id).forEach(a=>{
            a.category_enabled=group.enabled;a.category_override=false;a.effective_scheduling=group.enabled&&a.scheduling;
          });
        } else {
          const account=data.accounts.find(a=>url.endsWith(a.name));Object.assign(account,options.body);
          account.category_enabled=data.categories.find(c=>c.id===(account.category||'long_term')).enabled;
          if('category' in options.body)account.category_override=false;
          if('scheduling' in options.body)account.category_override=options.body.scheduling&&!account.category_enabled;
          account.effective_scheduling=account.scheduling&&(account.category_enabled||account.category_override);
          return {ok:true,account:JSON.parse(JSON.stringify(account))};
        }
        return {ok:true};
      }
      if(url==='/api/admin/accounts') return data.read ? data.read() : JSON.parse(JSON.stringify(data));
      if(url==='/api/admin/stats') return data.statsRead ? data.statsRead() : {per_account:data.accounts};
      return {};
    }
  };
  vm.runInNewContext(fs.readFileSync('web/platform-ui.js','utf8'), context);context.PlatformUI=window.PlatformUI;
  vm.runInNewContext(fs.readFileSync('web/task-results-ui.js','utf8'), context);
  vm.runInNewContext(fs.readFileSync('web/dashboard-trend.js','utf8'), context);context.DashboardTrend=window.DashboardTrend;
  vm.runInNewContext(fs.readFileSync('web/admin-ui.js','utf8'), context);
  return {ui:window.AdminUI, window, context, q, rendered, calls, data, notifications};
}

test('pending Google login cannot show restore or enabled controls even after a stale category update', async()=>{
  const h=harness();
  h.data.accounts=[{name:'pending_google',category:'disposable',scheduling:true,
    effective_scheduling:true,onboarding_state:'attention'}];
  await h.window.loadAccounts();
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/data-account-category="pending_google"/);
  h.ui.setAccountView('disabled');
  assert.match(h.rendered.get('#accountsTable'),/重试登录/);
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/恢复使用/);
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/单号调度/);
});

test('category filtering includes legacy accounts in long term and combines with search', async()=>{
  const h=harness(); await h.window.loadAccounts();
  h.q('#accountCategory').value='long_term'; h.ui.filterAccounts();
  assert.equal(h.q('#accountCount').textContent,'1 / 2 个账号');
  assert.match(h.rendered.get('#accountsTable'), /legacy/);
  assert.doesNotMatch(h.rendered.get('#accountsTable'), /data-account-category="day"/);
  h.q('#accountSearch').value='day'; h.ui.filterAccounts();
  assert.equal(h.q('#accountCount').textContent,'0 / 2 个账号');
});

test('one category action persists only that group, with failed saves left unchanged', async()=>{
  const h=harness(); await h.window.loadAccounts();
  const button={disabled:false};
  await h.ui.setCategoryScheduling('long_term',false,button);
  assert.equal(h.calls[0].url,'/api/admin/account-categories/long_term');
  assert.equal(h.calls[0].options.body.enabled,false);
  assert.equal(h.data.accounts[0].scheduling,true);
  assert.match(h.rendered.get('#accountCategories'), /已停用/);
  h.data.fail=true;
  await h.ui.setCategoryScheduling('long_term',true,button);
  assert.equal(h.data.categories[0].enabled,false);
  assert.equal(button.disabled,false);
  assert.match(h.q('#accountCategoryError').textContent,/保存失败/);
});

test('moving an account changes its category and restores selection on a failed save', async()=>{
  const h=harness(); await h.window.loadAccounts();
  const select={value:'disposable',disabled:false};
  await h.ui.setAccountCategory('legacy',select);
  assert.equal(h.calls[0].options.body.category,'disposable');
  assert.equal(h.data.accounts[0].category,'disposable');
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/data-account-category="legacy"/);
  h.data.fail=true; select.value='long_term';
  await h.ui.setAccountCategory('legacy',select);
  assert.equal(select.value,'disposable');
  assert.equal(select.disabled,false);
});

test('enabled and disabled lists never mix, including category-disabled accounts with a checked single switch', async()=>{
  const h=harness();
  h.data.accounts.push({name:'category-off',scheduling:true,category:'disposable',category_enabled:false,effective_scheduling:false});
  await h.window.loadAccounts();
  assert.match(h.rendered.get('#accountsTable'),/data-account-category="legacy"/);
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/data-account-category="(?:day|category-off)"/);
  h.ui.setAccountView('disabled');
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/data-account-category="legacy"/);
  assert.match(h.rendered.get('#accountsTable'),/data-account-category="day"/);
  assert.match(h.rendered.get('#accountsTable'),/data-account-category="category-off"/);
  await h.window.loadAccounts();
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/data-account-category="legacy"/);
  h.q('#accountSearch').value='category-off';h.ui.filterAccounts();
  assert.equal(h.q('#accountCount').textContent,'1 / 3 个账号');
});

test('category pause moves rows out of enabled; category resume does not reopen individually paused rows', async()=>{
  const h=harness();
  h.data.accounts.push({name:'individual-off',category:'long_term',scheduling:false});
  await h.window.loadAccounts();
  await h.ui.setCategoryScheduling('long_term',false,{disabled:false});
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/data-account-category=/);
  h.ui.setAccountView('disabled');
  assert.match(h.rendered.get('#accountsTable'),/data-account-category="legacy"/);
  await h.ui.setCategoryScheduling('long_term',true,{disabled:false});
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/data-account-category="legacy"/);
  assert.match(h.rendered.get('#accountsTable'),/data-account-category="individual-off"/);
});

test('single toggle moves rows only after success and restores its checkbox on rejection', async()=>{
  const h=harness(); await h.window.loadAccounts();
  const checkbox={checked:false,disabled:false};
  h.data.fail=true;
  await h.ui.setAccountScheduling('legacy',false,checkbox);
  assert.equal(checkbox.checked,true);
  assert.match(h.rendered.get('#accountsTable'),/data-account-category="legacy"/);
  h.data.fail=false;checkbox.checked=false;
  await h.ui.setAccountScheduling('legacy',false,checkbox);
  assert.equal(checkbox.disabled,false);
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/data-account-category="legacy"/);
  h.ui.setAccountView('disabled');
  assert.match(h.rendered.get('#accountsTable'),/data-account-category="legacy"/);
});

test('a read started before a category save cannot restore stale enabled rows', async()=>{
  const h=harness(); await h.window.loadAccounts();
  const stale=JSON.parse(JSON.stringify(h.data));let resolve;
  h.data.read=()=>new Promise(r=>{resolve=r;});
  const read=h.window.loadAccounts();
  await h.ui.setCategoryScheduling('long_term',false,{disabled:false});
  resolve(stale); await read;
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/data-account-category="legacy"/);
  assert.match(h.rendered.get('#accountCategories'),/已停用/);
});

test('switching enablement pages resets pagination without dropping category filters', async()=>{
  const h=harness();
  h.data.accounts=Array.from({length:61},(_,i)=>({name:'active-'+i,category:'long_term',scheduling:true}));
  h.data.accounts.push({name:'off-long',category:'long_term',scheduling:false}, {name:'off-day',category:'disposable',scheduling:false});
  await h.window.loadAccounts();h.ui.accountPage(1);
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/data-account-category="active-0"/);
  h.q('#accountCategory').value='long_term';h.ui.setAccountView('disabled');
  assert.match(h.rendered.get('#accountsTable'),/data-account-category="off-long"/);
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/data-account-category="off-day"/);
  h.ui.setAccountView('enabled');
  assert.match(h.rendered.get('#accountsTable'),/data-account-category="active-0"/);
});

test('render caused by another save cannot unlock or repeat a pending account edit', async()=>{
  const h=harness(); await h.window.loadAccounts();
  let finish;
  h.data.write=(url)=>url.endsWith('/legacy')?new Promise(r=>{finish=r;}):Promise.resolve();
  const first=h.ui.setAccountCategory('legacy',{value:'disposable',disabled:false});
  await h.ui.setAccountScheduling('day',true,{disabled:false});
  assert.match(h.rendered.get('#accountsTable'),/data-account-category="legacy"[^>]*disabled/);
  await h.ui.setAccountCategory('legacy',{value:'long_term',disabled:false});
  assert.equal(h.calls.filter(c=>c.url.endsWith('/legacy')).length,1);
  finish();await first;
  assert.equal(h.data.accounts[0].category,'disposable');
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/data-account-category="legacy"[^>]*disabled/);
});

test('a stale dashboard read cannot overwrite a saved account toggle after navigation', async()=>{
  const h=harness();await h.window.loadAccounts();
  const stale=JSON.parse(JSON.stringify(h.data.accounts));let finish;
  h.data.statsRead=()=>new Promise(r=>{finish=r;});
  const read=h.window.loadDash();
  await h.ui.setAccountScheduling('legacy',false,{checked:false,disabled:false});
  finish({per_account:stale});await read;
  h.ui.filterAccounts();
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/data-account-category="legacy"/);
});

test('a dashboard toggle refreshes the displayed switch after the save lock is released', async()=>{
  const h=harness();h.context.curTab='dash';
  await h.window.loadDash();
  assert.match(h.rendered.get('#perAccount'),/legacy 单号调度" type="checkbox" checked/);
  await h.ui.setAccountScheduling('legacy',false,{checked:false,disabled:false});
  await new Promise(resolve=>setImmediate(resolve));
  assert.doesNotMatch(h.rendered.get('#perAccount'),/legacy 单号调度" type="checkbox" checked/);
});

test('restore is visible beside a disabled account and reopens only that account in a paused category', async()=>{
  const h=harness();
  h.data.accounts.push({name:'paused-peer',category:'disposable',scheduling:true,category_enabled:false,effective_scheduling:false});
  await h.window.loadAccounts();h.ui.setAccountView('disabled');
  assert.match(h.rendered.get('#accountsTable'),/data-account-restore="paused-peer"[^>]*>恢复使用/);
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/paused-peer 单号调度" type="checkbox" checked/);
  await h.ui.restoreAccount('paused-peer',{disabled:false});
  assert.equal(h.calls.length,1);
  assert.equal(h.calls[0].url,'/api/admin/accounts/paused-peer');
  assert.equal(h.data.categories[1].enabled,false);
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/data-account-category="paused-peer"/);
  assert.match(h.rendered.get('#accountsTable'),/data-account-category="day"/);
  h.ui.setAccountView('enabled');
  assert.match(h.rendered.get('#accountsTable'),/data-account-category="paused-peer"/);
  assert.match(h.rendered.get('#accountCategories'),/单独启用 1 个/);
  assert.match(h.rendered.get('#accountCategories'),/data-category-toggle="disposable"[^>]*>全部停用/);
});

test('failed single restore stays actionable and repeated clicks while saving make only one request', async()=>{
  const h=harness();await h.window.loadAccounts();h.ui.setAccountView('disabled');
  let release;h.data.write=()=>new Promise(resolve=>{release=resolve;});h.data.fail=true;
  const save=h.ui.restoreAccount('day',{disabled:false});
  assert.match(h.rendered.get('#accountsTable'),/data-account-restore="day"[^>]*disabled/);
  await h.ui.restoreAccount('day',{disabled:false});assert.equal(h.calls.length,1);
  release();await save;
  assert.match(h.rendered.get('#accountsTable'),/data-account-restore="day"[^>]*>恢复使用/);
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/data-account-restore="day"[^>]*disabled/);
  assert.match(h.q('#accountCategoryError').textContent,/保存失败/);
});

test('pausing a partially restored category stops its restored accounts again', async()=>{
  const h=harness();await h.window.loadAccounts();
  await h.ui.restoreAccount('day',{disabled:false});
  assert.match(h.rendered.get('#accountsTable'),/data-account-category="day"/);
  await h.ui.setCategoryScheduling('disposable',false,{disabled:false});
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/data-account-category="day"/);
  assert.equal(h.data.accounts[1].category_override,false);
  assert.equal(h.data.accounts[1].effective_scheduling,false);
});

test('single restore waits for an in-flight category update instead of racing it', async()=>{
  const h=harness();await h.window.loadAccounts();h.ui.setAccountView('disabled');
  let release;h.data.write=()=>new Promise(resolve=>{release=resolve;});
  const save=h.ui.setCategoryScheduling('disposable',false,{disabled:false});
  assert.match(h.rendered.get('#accountsTable'),/data-account-restore="day"[^>]*disabled/);
  await h.ui.restoreAccount('day',{disabled:false});
  assert.equal(h.calls.length,1);
  release();await save;
  h.data.write=null;await h.ui.restoreAccount('day',{disabled:false});
  assert.equal(h.calls.length,2);
  assert.equal(h.data.accounts[1].effective_scheduling,true);
});


function dualPools(){return {ordinary_fast10:{limit:1,used:0,reserved:1,uncertain:0,remaining:0,period:'day'},expert_fast:{limit:4,used:2,reserved:0,uncertain:1,remaining:1,period:'rolling_7_days'}};}

test('Fast day and week policies coexist without a global quota switch',async()=>{
  const h=harness();h.data.quota_policy={mode:'seedance25',limit:2};await h.window.loadAccounts();
  assert.match(h.q('#accountQuotaCurrent').textContent,/普通10秒.*每天1次.*北京时间凌晨01:00重置/);
  assert.match(h.q('#accountQuotaCurrent').textContent,/专家.*4次申请.*每次使用满7天恢复1次/);
  assert.match(h.q('#accountQuotaCurrent').textContent,/两条15秒只计1次/);
  assert.equal(h.q('#accountQuotaFast').hidden,true);assert.equal(h.q('#accountQuota25').hidden,true);
  assert.equal(h.calls.length,0);
});

test('unavailable account quota snapshots never invent a remaining allowance',async()=>{
  const h=harness();await h.window.loadAccounts();
  assert.doesNotMatch(h.rendered.get('#accountsTable'),/剩余 2 \/ 2|剩余 4 \/ 4/);
  assert.equal(h.q('#accountQuotaFast').hidden,true);
  assert.equal(h.q('#accountQuota25').hidden,true);
});

test('account day and week views use independent server balances including uncertainty',async()=>{
  const h=harness();h.data.accounts[0].quota_pools=dualPools();Object.assign(h.data.accounts[0],{used_today:6,limit:2});await h.window.loadAccounts();
  const html=h.rendered.get('#accountsTable');
  assert.match(html,/普通Fast 10秒.*已用 0.*预占 1.*剩余 0 \/ 1 次\/日.*北京时间凌晨01:00重置/);
  assert.match(html,/专家Fast.*已用 2.*待核对 1.*剩余 1 \/ 4 次\/7天.*每次使用满7天恢复1次/);
  const detail=h.window.TaskResultsUI.quotaHTML(h.data.accounts[0].quota_pools);
  assert.match(detail,/普通Fast 10秒.*北京时间凌晨01:00重置/);
  assert.match(detail,/专家Fast.*每次使用满7天恢复1次/);
  assert.doesNotMatch(html,/<b>6<\/b> \/ 2/);
});

test('quota details distinguish future replenishment from waiting for availability',()=>{
  const h=harness(),pools=dualPools();
  h.context.UI.time=value=>`time-${value}`;
  Object.assign(pools.expert_fast,{reset_at:100,next_available_at:null});
  let html=h.window.TaskResultsUI.quotaHTML({expert_fast:pools.expert_fast});
  assert.match(html,/剩余 1 \/ 4.*下次恢复 time-100/);
  assert.doesNotMatch(html,/下次可用/,'an account with a free slot is usable now');
  Object.assign(pools.expert_fast,{remaining:0,next_available_at:200});
  html=h.window.TaskResultsUI.quotaHTML({expert_fast:pools.expert_fast});
  assert.match(html,/下次可用 time-200/,'a later platform block takes priority over replenishment');
});

test('retired global quota action cannot overwrite fixed Fast policies',async()=>{
  const h=harness();h.data.quota_policy={mode:'seedance25',limit:2};h.data.accounts[0].quota_pools=dualPools();await h.window.loadAccounts();
  const before=JSON.stringify(h.data.accounts[0].quota_pools);
  await h.ui.setQuotaPolicy('fast',{disabled:false});
  assert.equal(h.calls.length,0);
  assert.equal(JSON.stringify(h.data.accounts[0].quota_pools),before);
});

test('category scheduling never resets or merges day and week usage',async()=>{
  const h=harness();h.data.accounts[0].quota_pools=dualPools();await h.window.loadAccounts();const before=JSON.stringify(h.data.accounts[0].quota_pools);
  await h.ui.setCategoryScheduling('long_term',false,{disabled:false});
  assert.equal(JSON.stringify(h.data.accounts[0].quota_pools),before);
  assert.equal(h.calls[0].url,'/api/admin/account-categories/long_term');
});

test('failed account writes preserve the displayed independent quota snapshots',async()=>{
  const h=harness();h.data.accounts[0].quota_pools=dualPools();await h.window.loadAccounts();const before=h.rendered.get('#accountsTable');h.data.fail=true;
  await h.ui.setAccountScheduling('legacy',false,{disabled:false,checked:false});
  assert.equal(h.rendered.get('#accountsTable'),before);
  assert.match(h.q('#accountCategoryError').textContent,/保存失败/);
});

test('a stale read cannot undo the quota snapshot returned with a confirmed account change',async()=>{
  const h=harness();h.data.accounts[0].quota_pools=dualPools();await h.window.loadAccounts();const stale=JSON.parse(JSON.stringify(h.data));let resolve;
  h.data.read=()=>new Promise(done=>{resolve=done;});const read=h.window.loadAccounts();
  h.data.accounts[0].quota_pools.ordinary_fast10.used=1;h.data.accounts[0].quota_pools.ordinary_fast10.reserved=0;
  await h.ui.setAccountScheduling('legacy',false,{disabled:false,checked:false});resolve(stale);await read;h.ui.setAccountView('disabled');
  assert.match(h.rendered.get('#accountsTable'),/普通Fast 10秒.*已用 1.*预占 0/);
  assert.match(h.rendered.get('#accountsTable'),/专家Fast.*待核对 1/);
});

test('confirmed daily exhaustion displays Fast 1/1 without changing recorded usage or expert quota',async()=>{
  const h=harness(),pools=dualPools();
  Object.assign(pools.ordinary_fast10,{used:0,reserved:0,remaining:1,blocked:true,
    reason:'Dola 返回无法生成视频（每日免费视频额度已用完），未重新提交：Du hast das tägliche kostenlose Nutzungslimit für Videogenerierung erreicht.'});
  h.data.accounts[0].quota_pools=pools;const before=JSON.stringify(pools);
  await h.window.loadAccounts();
  const html=h.rendered.get('#accountsTable');
  assert.match(html,/>Fast 1\/1<\/div>/);
  assert.match(html,/平台已确认额度用完/);
  assert.match(html,/已用 0/,'keep the actual local request count available');
  assert.match(html,/>专家 2\/4<\/div>/);
  assert.equal(JSON.stringify(pools),before,'display must not invent generated requests');
  assert.equal(h.calls.length,0);
  for(const reason of ['账号每日生成上限: daily limit','Dola 回复当日视频额度已用完']){
    pools.ordinary_fast10.reason=reason;await h.window.loadAccounts();
    assert.match(h.rendered.get('#accountsTable'),/>Fast 1\/1<\/div>/);
  }
  Object.assign(pools.ordinary_fast10,{blocked:false,reason:''});
  await h.window.loadAccounts();
  assert.match(h.rendered.get('#accountsTable'),/>Fast 0\/1<\/div>/,'expired or cleared blocks restore the actual count');
});

test('only explicit quota exhaustion fills a blocked mode to its own limit',async()=>{
  const h=harness(),pools=dualPools();h.data.accounts[0].quota_pools=pools;
  Object.assign(pools.expert_fast,{used:1,blocked:true,
    reason:'Dola 专家 Pro 免费 7 天额度已用完，本次未进入视频生成：Komm später wieder.'});
  await h.window.loadAccounts();
  assert.match(h.rendered.get('#accountsTable'),/>专家 4\/4<\/div>/);
  assert.match(h.rendered.get('#accountsTable'),/>Fast 0\/1<\/div>/);
  for(const reason of ['Dola Plus erforderlich','网络错误：额度状态未能读取','']){
    pools.expert_fast.reason=reason;await h.window.loadAccounts();
    assert.match(h.rendered.get('#accountsTable'),/>专家 1\/4<\/div>/,'unknown or subscription blocks are not evidence of consumed quota');
  }
});
