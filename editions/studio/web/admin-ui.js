/* Presentation layer; existing account/proxy/permission actions remain in index.html. */
window.AdminUI=(()=>{
  const titles={dash:['运行概览','查看任务结果与资源状态'],tasks:['任务列表','查看任务进度与完整错误，继续排查关联资源'],accounts:['账号管理','查看登录状态、调度与日／周额度'],proxies:['代理与 IP','按出口分组查看账号与请求额度'],gallery:['视频创作','提交视频任务，查看生成状态与结果'],keys:['API 密钥','管理成员归属、使用额度与允许参数'],users:['成员管理','管理成员身份、密钥与访问权限']};
  let accounts=[],categories=[],jobs={},tasks=[],accountPage=1,refreshing=false,pendingRefresh=false,selectedResult='',detailRequest=0,pendingSubmission=null,taskRead=0;
  let accountView='enabled',accountRevision=0,accountRead=0,accountMutations=0;
  let quotaPolicy=null,quotaSaving='';
  let generationPlatform='dola',generationCaps=null,capabilityRead=0,generating=false,generationAdvanced,generationRevision=0;
  const currentGenerationPlatform=()=>q('#genPlatform')?.value||'dola';
  const generationProfile=()=>currentGenerationPlatform()==='dola'?(q('#genProfile')?.value||''):'';
  const selectedGenerationCaps=()=>PlatformUI.capabilities(currentGenerationPlatform(),generationCaps,generationProfile());
  const selectedGenerationModels=()=>PlatformUI.models(currentGenerationPlatform(),generationCaps,generationProfile());
  let generationBatch=null;
  function renderProfile(){const select=q('#genProfile');if(!select)return;const previous=select.value,profiles=PlatformUI.profiles(currentGenerationPlatform(),generationCaps);select.innerHTML='<option value="">普通模式</option>'+profiles.map(p=>`<option value="${E(p.id)}">${E(p.label||p.id)}</option>`).join('');select.value=profiles.some(p=>p.id===previous)?previous:'';select.disabled=generating;q('#genProfileField').hidden=currentGenerationPlatform()!=='dola';q('#genBatchCount').disabled=generating||currentGenerationPlatform()!=='dola';q('#genQuotaNotice').innerHTML=(window.TaskResultsUI?.quotaHTML(generationCaps?.quota_summary)||'普通Fast 10秒：每账号每天1次，北京时间凌晨01:00重置；专家Fast：每账号4次申请，每次使用满7天恢复1次，一次双片也只计1次。');}
  function changeGenerationProfile(){clearGenerationSubmission();generationBatch=null;const profile=PlatformUI.profiles(currentGenerationPlatform(),generationCaps).find(p=>p.id===generationProfile());if(profile){q('#genModel').value=profile.default_model||'seedance-2.5';q('#genRatio').value='16:9';}renderGenerationCapabilities();if(profile){q('#genDuration').value=String(profile.default_duration||30);syncGenerationDuration();}}

  const taskPlatformLabel=t=>`<span class="badge b-gray">${E(PlatformUI.names[t.platform||'dola']||'未知平台')}</span>`;
  const pendingAccounts=new Set(),pendingCategories=new Set();
  const categoryLabels={disposable:'日抛号',long_term:'接码长期号'};
  const categoryOf=a=>a.category||'long_term';
  const accountEnabled=a=>(!a.onboarding_state||a.onboarding_state==='ready')&&Boolean(a.effective_scheduling??(a.scheduling&&(a.category_enabled!==false||a.category_override)));
  const E=UI.esc,N=UI.number;
  const badge=t=>{const m={queued:['b-orange','排队中'],processing:['b-blue','生成中'],completed:['b-green','已完成'],failed:['b-red','失败']};const [c,s]=m[t]||['b-gray',t||'未知'];return `<span class="badge ${c}">${E(s)}</span>`;};
  const score=a=>a.login_ok===0?5:(a.verification_required||a.rate_limited||a.quota_blocked||a.cooling||a.proxy_cooling||a.preparation_retrying)?4:a.busy?3:a.login_ok!==1?2:0;
  const accountLabel=a=>`<div class="account-label"><strong>${E(a.name||'—')}</strong><span>${E(a.email||'未录入邮箱')}</span></div>`;
  const used=(a,compact=true)=>a.quota_pools?(window.TaskResultsUI?.quotaHTML(a.quota_pools,compact)||''):`<span class="nowrap"><b>${N(a.used_today||0)}</b> / ${N(a.limit)}</span>${a.used_today>a.limit?'<div class="tiny" style="color:var(--red)">记录超限</div>':''}`;
  function accountStatusBadge(a){
    if(!a.proxy_cooling&&!a.preparation_retrying)return statusBadge(a);
    const otherState=(a.onboarding_state&&a.onboarding_state!=='ready')||a.login_ok===0||a.verification_required||a.busy||a.rate_limited||a.quota_blocked||a.cooling;
    const waits=[];
    if(a.proxy_cooling)waits.push(['线路重试等待',a.proxy_cooling_until,a.proxy_cooling_reason]);
    if(a.preparation_retrying)waits.push(['页面准备失败，稍后重试',a.preparation_retry_until,a.preparation_retry_reason]);
    return (otherState?statusBadge(a)+'<br>':'')+waits.map(([label,until,reason])=>`<span class="badge b-orange">${label}</span><div class="tiny">重试时间 ${UI.time(until)}</div>`+(reason?`<div class="tiny" style="max-width:260px;white-space:normal">${E(reason)}</div>`:'')).join('<br>');
  }
  const fields=rows=>`<dl class="detail-fields">${rows.map(([k,v])=>`<dt>${E(k)}</dt><dd>${v}</dd>`).join('')}</dl>`;
  const empty=UI.empty;
  function taskRows(list,compact=false){return list.map(t=>{
    const id=String(t.id||'');
    const action=`<button class="btn" data-task-detail="${E(id)}">查看详情</button>`+(window.TaskResultsUI?.downloadButtons(t)||'')+(window.TaskResultsUI?.deleteButton(t,'task')||'');
    if(compact)return [`<div class="mono">${E(id.replace('video_','').slice(0,10))}</div>${taskPlatformLabel(t)}<span class="prompt" title="${E(t.prompt)}">${E(t.prompt||'—')}</span>`,E(t.account||'—'),TaskEvidence.badge(t),`<span class="tiny">${E(String(t.error||'—').slice(0,50))}</span>`,action];
    return [`<span class="mono">${E(id.replace('video_','').slice(0,10))}</span><div>${taskPlatformLabel(t)}</div>`,`<span class="prompt" title="${E(t.prompt)}">${E(t.prompt||'—')}</span>`,E(t.account||'—'),E(t.api_key_name||'—'),`<span class="nowrap">${E(TaskEvidence.durationText(t))}</span>`,TaskEvidence.badge(t),`<span class="nowrap">${UI.time(t.created_at)}</span>`,action];
  });}
  async function loadDash(){
    const revision=accountRevision,request=++accountRead,taskRequest=++taskRead;
    const [stats,recent,proxies]=await Promise.allSettled([api('/api/admin/stats'),api('/api/admin/tasks?limit=50'),api('/api/admin/proxies')]);
    if(request!==accountRead||revision!==accountRevision||accountMutations)return;
    if(stats.status==='rejected')throw stats.reason;
    const s=stats.value;accounts=s.per_account||[];
    const fail=Number(s.today_failed||0),rate=s.success_rate==null?'—':Math.round(s.success_rate*100)+'%';
    UI.render('#dashAttention',`<div class="attention ${fail?'':'is-good'}"><div class="attention-copy">${UI.icon(fail?'alert':'check')}<div><strong>${fail?'今日 '+N(fail)+' 个任务失败':'今日暂无失败任务'}</strong><small>${fail?'查看失败任务，继续排查账号与代理':'当前为已加载的今日统计，请继续关注任务与资源状态'}</small></div></div><button class="btn ${fail?'btn-p':''}" onclick="AdminUI.goFailures()">查看失败任务</button></div>`);
    UI.render('#statCards',`<div class="stat"><div class="l">今日完成</div><div class="v">${N(s.today_completed||0)}</div></div><div class="stat ${fail?'s2':''}"><div class="l">今日失败</div><div class="v">${N(fail)}</div></div><div class="stat"><div class="l">成功率</div><div class="v">${rate}</div></div><div class="stat"><div class="l">可用账号</div><div class="v">${N(s.available_accounts)} / ${N(s.total_accounts)}</div><div class="stat-note">${s.quota_summary?(window.TaskResultsUI?.quotaHTML(s.quota_summary)||'Fast日／周额度已读取'):'旧模型剩余次数 '+N(s.total_remaining)+' · 记录值'}</div></div>`);
    DashboardTrend.render('#chart',s.per_day||[]);
    const p=proxies.status==='fulfilled'?proxies.value:null;if(p)applyProxyData(p);
    UI.render('#dashResources',`<dl class="resource-list"><div><dt>可用账号</dt><dd>${N(s.available_accounts)} / ${N(s.total_accounts)}</dd></div><div><dt>共享代理组</dt><dd>${p?N(p.shared_groups):'—'}</dd></div><div><dt>可用出口</dt><dd>${p?.summary?N(p.summary.available)+' / '+N(p.summary.count):'—'}</dd></div></dl><p class="hint" style="margin:0 0 16px">${p?'共享关系本身不代表故障。':'代理信息暂未加载，请重试刷新。'}</p><div class="resource-actions"><button class="btn" onclick="switchTab('accounts')">${UI.icon('accounts')}查看账号</button><button class="btn" onclick="switchTab('proxies')">${UI.icon('proxy')}查看代理</button></div>`);
    q('#dashResources').classList.remove('loading');q('#dashFailures').classList.remove('loading');
    if(recent.status==='fulfilled'){if(taskRequest===taskRead)tasks=recent.value.tasks||[];const failed=tasks.filter(t=>t.status==='failed').slice(0,5);UI.render('#dashFailures',(failed.length?table(['任务','账号','状态','错误摘要','操作'],taskRows(failed,true)):empty('最近读取范围内没有失败任务'))+'<p class="hint" style="margin-top:12px">仅展示最近 50 条任务中的记录，与今日失败总数的统计范围不同。</p>');}
    else UI.render('#dashFailures',empty('最近任务加载失败','可点击右上角手动刷新重试。'));
    const priority=accounts.slice().sort((a,b)=>score(b)-score(a)).slice(0,12);
    UI.render('#perAccount',priority.length?table(['账号','状态','调度','日／周额度','累计出片','最近使用'],priority.map(a=>[accountLabel(a),accountStatusBadge(a),accountScheduling(a),used(a),N(a.completed_total||0),UI.time(a.last_used_at)])):empty('尚未添加账号','前往账号管理导入 CK。'));
  }
  async function loadAccounts(){
    if(window.DoubaoAccounts?.selected())return DoubaoAccounts.load();
    const revision=accountRevision,request=++accountRead;
    const [a,j,p]=await Promise.allSettled([api('/api/admin/accounts'),api('/api/admin/jobs'),api('/api/admin/proxies')]);
    if(request!==accountRead||revision!==accountRevision||accountMutations||window.DoubaoAccounts?.selected())return;
    if(a.status==='rejected')throw a.reason;
    accounts=a.value.accounts||[];categories=a.value.categories||[];quotaPolicy=readQuotaPolicy(a.value.quota_policy);if(j.status==='fulfilled')jobs=j.value.jobs||{};if(p.status==='fulfilled')applyProxyData(p.value);
    q('#accountSupplementError').textContent=(j.status==='rejected'||p.status==='rejected')?'账号已加载，部分导入进度或代理信息暂不可用。':'';
    renderAccounts();
  }
  function readQuotaPolicy(value){return value&&typeof value==='object'?value:null;}
  function renderQuotaPolicy(){
    q('#accountQuotaError').hidden=!q('#accountQuotaError').textContent;
    q('#accountQuotaCurrent').textContent='Fast独立额度：普通10秒每号每天1次，北京时间凌晨01:00重置；专家每号4次申请，每次使用满7天恢复1次，一次返回两条15秒只计1次。2.5及其他模型沿用独立配置。';
    q('#accountQuotaFast').hidden=true;q('#accountQuota25').hidden=true;q('#accountQuotaFast').disabled=true;q('#accountQuota25').disabled=true;
  }
  async function setQuotaPolicy(){return;}
  function renderCategories(){
    UI.render('#accountCategories',Object.entries(categoryLabels).map(([id,label])=>{
      const group=categories.find(c=>c.id===id),enabled=group?.enabled,count=accounts.filter(a=>categoryOf(a)===id).length;
      const restored=enabled===false?accounts.filter(a=>categoryOf(a)===id&&accountEnabled(a)).length:0;
      const disabled=typeof enabled!=='boolean'||quotaSaving||pendingCategories.has(id)||pendingAccounts.size>0;
      return `<div class="account-category"><div><strong>${label}</strong><span class="category-count">${N(count)} 个账号</span><p class="hint">${restored?'单独启用 '+N(restored)+' 个，其余账号保持停用':enabled===false?'本类已暂停分配新任务':enabled===true?'本类参与新任务调度':'分类调度状态尚未加载'}</p></div><div class="category-action"><span class="badge ${restored?'b-orange':enabled===false?'b-gray':enabled===true?'b-green':'b-orange'}">${restored?'部分启用':enabled===false?'已停用':enabled===true?'已启用':'未加载'}</span><button class="btn ${enabled===false?'btn-p':''}" data-category-toggle="${id}" ${disabled?'disabled':''} onclick="AdminUI.setCategoryScheduling('${id}',${!enabled},this)">${pendingCategories.has(id)?'保存中…':enabled===false?'一键启用':'一键停用'}</button>${restored?`<button class="btn" data-category-toggle="${id}" ${disabled?'disabled':''} onclick="AdminUI.setCategoryScheduling('${id}',false,this)">全部停用</button>`:''}</div></div>`;
    }).join(''));
  }
  async function setCategoryScheduling(category,enabled,button){
    if(button.disabled||quotaSaving||pendingCategories.has(category)||pendingAccounts.size)return;button.disabled=true;q('#accountCategoryError').textContent='';
    accountRevision++;accountMutations++;pendingCategories.add(category);renderAccounts();
    try{
      await api('/api/admin/account-categories/'+category,{method:'PATCH',body:{enabled}});
      const group=categories.find(c=>c.id===category);if(group)group.enabled=enabled;
      accounts.forEach(a=>{if(categoryOf(a)===category){a.category_enabled=enabled;a.category_override=false;a.effective_scheduling=enabled&&a.scheduling;}});
      renderAccounts();UI.toast(categoryLabels[category]+(enabled?'已启用':'已停用，已受理任务继续查询'));
    }catch(error){q('#accountCategoryError').textContent=UI.detailError(error.message);}
    finally{accountRevision++;accountMutations--;pendingCategories.delete(category);button.disabled=false;renderAccounts();}
  }
  async function setAccountCategory(name,select){
    if(select.disabled)return;const account=accounts.find(a=>a.name===name);if(!account)return;
    if(quotaSaving||pendingCategories.size){select.value=categoryOf(account);return;}
    if(pendingAccounts.has(name)){select.value=categoryOf(account);return;}
    const previous=categoryOf(account),category=select.value;select.disabled=true;q('#accountCategoryError').textContent='';
    accountRevision++;accountMutations++;pendingAccounts.add(name);renderAccounts();
    try{
      const result=await api('/api/admin/accounts/'+encodeURIComponent(name),{method:'PATCH',body:{category}});
      account.category=category;account.category_enabled=categories.find(c=>c.id===category)?.enabled!==false;
      account.category_override=false;
      account.effective_scheduling=account.category_enabled&&account.scheduling;
      if(result.account)Object.assign(account,result.account);
      renderAccounts();UI.toast('已移入'+categoryLabels[category]);
    }catch(error){select.value=previous;q('#accountCategoryError').textContent=UI.detailError(error.message);}
    finally{accountRevision++;accountMutations--;pendingAccounts.delete(name);select.disabled=false;renderAccounts();}
  }
  async function setAccountScheduling(name,on,checkbox){
    const account=accounts.find(a=>a.name===name);if(!account||checkbox.disabled)return;
    if(quotaSaving||pendingAccounts.has(name)||pendingCategories.size){checkbox.checked=accountEnabled(account);return;}
    const previous=accountEnabled(account);checkbox.disabled=true;accountRevision++;accountMutations++;
    pendingAccounts.add(name);renderAccounts();
    q('#accountCategoryError').textContent='';
    try{
      const result=await api('/api/admin/accounts/'+encodeURIComponent(name),{method:'PATCH',body:{scheduling:on}});
      account.scheduling=on;if(!on)account.category_override=false;
      account.effective_scheduling=on&&(account.category_enabled!==false||Boolean(account.category_override));
      if(result.account)Object.assign(account,result.account);
      renderAccounts();
      if(on&&!accountEnabled(account))UI.toast('分类仍停用，尚未恢复使用，请刷新后重试。',true);
      else UI.toast(on?'已恢复此账号，移入已启用；登录与额度限制仍生效':'已移入已停用列表');
    }catch(error){checkbox.checked=previous;q('#accountCategoryError').textContent=UI.detailError(error.message);UI.toast(UI.detailError(error.message),true);}
    finally{accountRevision++;accountMutations--;pendingAccounts.delete(name);checkbox.disabled=false;renderAccounts();
      if(typeof curTab!=='undefined'&&curTab==='dash')refresh();
    }
  }
  function accountScheduling(a){if(a.onboarding_state&&a.onboarding_state!=='ready')return '<span class="tiny">登录成功后启用</span>';return `<label class="sw"><input aria-label="${E(a.name)} 单号调度" type="checkbox" ${accountEnabled(a)?'checked':''} ${quotaSaving||pendingAccounts.has(a.name)||pendingCategories.size?'disabled':''} onchange="AdminUI.setAccountScheduling('${E(a.name)}',this.checked,this)"><i></i></label>${a.category_enabled===false?`<div class="tiny">${accountEnabled(a)?'已单独启用':'随分类停用'}</div>`:''}`;}
  function restoreAccount(name,button){return setAccountScheduling(name,true,button);}
  function accountRestore(a){if(a.onboarding_state&&a.onboarding_state!=='ready')return a.onboarding_state==='attention'?'<div class="account-restore"><button class="btn" onclick="retryGoogleLogin(\''+E(a.name)+'\',this)">重试登录</button></div>':'';return accountEnabled(a)?'':`<div class="account-restore"><button class="btn btn-p" data-account-restore="${E(a.name)}" ${quotaSaving||pendingAccounts.has(a.name)||pendingCategories.size?'disabled':''} onclick="AdminUI.restoreAccount('${E(a.name)}',this)">${pendingAccounts.has(a.name)?'恢复中…':'恢复使用'}</button><span class="tiny">${a.category_enabled===false?'仅恢复此号，其余保持停用':'恢复后参与新任务调度'}</span></div>`;}
  function setAccountView(view){if(!['enabled','disabled'].includes(view))return;accountView=view;accountPage=1;renderAccounts();}
  function renderAccountViews(){
    const enabled=accounts.filter(accountEnabled).length;
    UI.render('#accountViews',[["enabled","已启用",enabled],["disabled","已停用",accounts.length-enabled]].map(([id,label,count])=>`<button type="button" class="account-view-button" data-account-view="${id}" aria-pressed="${accountView===id}" aria-controls="accountsTable" onclick="AdminUI.setAccountView('${id}')">${label}<span>${N(count)}</span></button>`).join(''));
  }
  function filterAccounts(){accountPage=1;renderAccounts();}
  function renderAccounts(){
    renderQuotaPolicy();renderCategories();renderAccountViews();
    const search=q('#accountSearch').value.trim().toLowerCase(),state=q('#accountState').value,category=q('#accountCategory').value;
    const filtered=natSort(accounts,a=>a.name).filter(a=>{
      if(accountEnabled(a)!==(accountView==='enabled'))return false;
      if(category!=='all'&&categoryOf(a)!==category)return false;
      if(search&&!`${a.name} ${a.email||''}`.toLowerCase().includes(search))return false;
      if(state==='birthday')return a.verification_required==='birthday';
      if(state==='invalid')return a.login_ok===0;
      if(state==='limited')return a.rate_limited||a.quota_blocked||a.cooling||a.proxy_cooling||a.preparation_retrying;
      if(state==='busy')return a.busy;
      if(state==='valid')return a.login_ok===1&&!a.verification_required&&!a.busy&&!a.rate_limited&&!a.quota_blocked&&!a.cooling&&!a.proxy_cooling&&!a.preparation_retrying;
      if(state==='unverified')return a.login_ok!==0&&a.login_ok!==1;
      return true;
    });
    const pages=Math.max(1,Math.ceil(filtered.length/50));accountPage=Math.min(accountPage,pages);
    q('#accountCount').textContent=N(filtered.length)+' / '+N(accounts.length)+' 个账号';
    const list=filtered.slice((accountPage-1)*50,accountPage*50);
    const rows=list.map(a=>{const job=jobs[a.name];const state=(job?.kind==='add'&&job.status==='running')?'<span class="badge b-blue">加号中</span>':job?.status==='failed'?'<span class="badge b-red">加号失败</span>':accountStatusBadge(a);return [accountLabel(a)+accountRestore(a),`<select class="account-category-select" data-account-category="${E(a.name)}" ${quotaSaving||pendingAccounts.has(a.name)?'disabled':''} aria-label="${E(a.name)} 的账号分类" onchange="AdminUI.setAccountCategory('${E(a.name)}',this)">${Object.entries(categoryLabels).map(([id,label])=>`<option value="${id}" ${categoryOf(a)===id?'selected':''}>${label}</option>`).join('')}</select>`,state,proxyBadge(a.name),accountScheduling(a),used(a),UI.time(a.last_used_at),`<div class="toolbar-actions"><button class="btn" data-account-detail="${E(a.name)}">详情</button><button class="btn" onclick="verify('${E(a.name)}',this)">验证</button><button class="btn btn-danger" onclick="delAccount('${E(a.name)}')">删除</button></div>`];});
    const pagination=`<div class="pagination"><span>每页 50 个账号</span><div class="pages"><button class="btn" onclick="AdminUI.accountPage(-1)" ${accountPage===1?'disabled':''}>上一页</button><span>${accountPage} / ${pages}</span><button class="btn" onclick="AdminUI.accountPage(1)" ${accountPage===pages?'disabled':''}>下一页</button></div></div>`;
    const viewEmpty=accounts.length&&!accounts.some(a=>accountEnabled(a)===(accountView==='enabled'));
    UI.render('#accountsTable',(rows.length?table(['账号','分类','状态','使用线路','单号调度','日／周额度','最近使用','操作'],rows):empty(!accounts.length?'尚未添加账号':viewEmpty?(accountView==='enabled'?'暂无已启用账号':'暂无已停用账号'):'没有匹配的账号',!accounts.length?'点击导入 CK 开始。':viewEmpty?(accountView==='enabled'?'可到已停用页点击“恢复使用”，或启用上方分类调度。':'关闭单号或分类调度后，账号会显示在这里。'):'调整分类、搜索或状态筛选。'))+(filtered.length>50?pagination:''));
  }
  function renderAccountDetail(name,a,proxyError=false){UI.openDrawer('账号详情',fields([['内部账号',`<span class="mono">${E(name)}</span>`],['邮箱',E(a.email||'未录入邮箱')],['状态',accountStatusBadge(a)],['登录检测时间',UI.time(a.login_checked_at)],['登录说明',E(a.onboarding_error||a.login_reason||'暂无运行中掉登录记录')],['分类',categoryLabels[categoryOf(a)]],['分类调度',a.category_enabled===false?'已停用':'已启用'],['单号调度',accountEnabled(a)?(a.category_enabled===false?'已单独恢复':'已开启'):'已停用'],['日／周额度',used(a,false)],['累计发送次数',a.send_count==null?'未同步':N(a.send_count)+' 次'],['浏览器尝试',a.dispatch_count==null?'未同步':N(a.dispatch_count)+' 次'],['最近发送',UI.time(a.last_sent_at)],['最近使用',UI.time(a.last_used_at)],['创建时间',UI.time(a.created_at)],['代理',proxyError?'暂未加载':proxyBadge(name)]])+'<p class="hint">轮询优先选择当前所选模式的额度占用最少的可用账号。Fast 与专家额度分别计算；已用、预占和待核对均计入占用，额度用完或已封锁的账号会跳过。占用相同时，优先选择更久未发送的账号。累计发送次数仅作历史记录，不参与选号优先级；发送前页面或线路失败不计入，发送结果不确定时保守计入。累计发送次数不等于扣费或出片次数。</p>'+(a.verification_required==='birthday'?'<p class="hint">请由账号使用者在 Dola 填写真实出生日期，完成生日验证后，再开启该账号的调度。</p>':'')+(proxyError?`<div class="error-text">代理信息暂不可用。<button class="btn" data-account-detail="${E(name)}">重试加载</button></div>`:'')+`<div class="toolbar-actions"><button class="btn" data-proxy-settings="${E(name)}">设置代理</button><button class="btn" onclick="verify('${E(name)}',this)">验证账号</button></div>`);}
  async function accountDetail(name){
    let a=accounts.find(x=>x.name===name);if(a&&PINFO[name]){renderAccountDetail(name,a);return;}
    const request='account-'+(++detailRequest),drawer=UI.openDrawer('账号详情',`<div class="loading" role="status"><span class="loading-dot"></span>正在加载账号与代理信息…</div>`);drawer.dataset.requestKey=request;
    const [accountResult,proxyResult]=await Promise.allSettled([a?Promise.resolve({accounts}):api('/api/admin/accounts'),PINFO[name]?Promise.resolve(PROXY_DATA):api('/api/admin/proxies')]);
    if(drawer.hidden||drawer.dataset.requestKey!==request)return;
    if(accountResult.status==='rejected'){UI.openDrawer('账号详情',`<div class="error-text">加载失败：${E(accountResult.reason.message)}</div><button class="btn" data-account-detail="${E(name)}">重试加载</button>`);return;}
    accounts=accountResult.value.accounts||[];a=accounts.find(x=>x.name===name);if(!a){UI.openDrawer('账号详情',empty('未找到此账号','账号可能已被移除，请返回任务查看记录。'));return;}
    if(proxyResult.status==='fulfilled')applyProxyData(proxyResult.value);renderAccountDetail(name,a,proxyResult.status==='rejected');
  }
  async function proxySettings(name){
    if(!PINFO[name]){const request='proxy-'+(++detailRequest),drawer=UI.openDrawer('代理设置',`<div class="loading" role="status"><span class="loading-dot"></span>正在加载代理信息…</div>`);drawer.dataset.requestKey=request;
      try{const data=await api('/api/admin/proxies');if(drawer.hidden||drawer.dataset.requestKey!==request)return;applyProxyData(data);}catch(error){if(!drawer.hidden&&drawer.dataset.requestKey===request)UI.openDrawer('代理设置',`<div class="error-text">加载失败：${E(error.message)}</div><button class="btn" data-proxy-settings="${E(name)}">重试加载</button>`);return;}
    }UI.closeDrawer();openProxyDialog([name]);
  }
  async function loadTasks(){const request=++taskRead,limit=Number(q('#taskRange').value)||50,platform=q('#taskPlatform')?.value||'all';const r=await api('/api/admin/tasks?limit='+limit+'&platform='+encodeURIComponent(platform));if(request!==taskRead||platform!==(q('#taskPlatform')?.value||'all'))return;tasks=r.tasks||[];renderTasks();}
  function renderTasks(){const search=q('#taskSearch').value.trim().toLowerCase(),state=q('#taskState').value;const filtered=tasks.filter(t=>(state==='all'||t.status===state)&&(!search||`${t.id} ${t.prompt} ${t.account||''} ${t.api_key_name||''}`.toLowerCase().includes(search)));q('#taskCount').textContent=N(filtered.length)+' / '+N(tasks.length)+' 条任务';UI.render('#tasksTable',filtered.length?table(['ID','提示词','账号','客户','时长','状态','创建时间','操作'],taskRows(filtered)):empty(tasks.length?'没有匹配的任务':'暂无任务记录',tasks.length?'调整搜索或状态筛选。':'前往视频创作提交一个任务。'));}
  async function taskDetail(id){
    const key='task-'+(++detailRequest), drawer=UI.openDrawer('任务详情','<p role="status">正在读取任务证据…</p>');
    drawer.dataset.requestKey=key;
    let result;
    try { result=await api('/api/admin/tasks/'+encodeURIComponent(id)); }
    catch(error){if(!drawer.hidden&&drawer.dataset.requestKey===key)UI.openDrawer('任务详情',`<p class="error-text">${E(UI.detailError(error.message))}</p>`);return;}
    if(drawer.hidden||drawer.dataset.requestKey!==key)return;
    UI.openDrawer('任务详情','<div data-task-live></div>');
    drawer.dataset.requestKey=key;drawer.dataset.taskId=id;
    UI.render(drawer.querySelector('[data-task-live]'),taskDetailHTML(result));
    mountCleanDownload(drawer,result.task);
  }
  async function downloadCleanFile(path,filename){
    if(typeof path!=='string'||!path.startsWith('/api/admin/tasks/')||!path.includes('/clean-download/file?kind='))throw new Error('无效的下载地址');
    const headers={};
    if(adminKey)headers['X-Admin-Key']=adminKey;
    const token=localStorage.getItem(LS_TOKEN);
    if(token)headers.Authorization='Bearer '+token;
    const response=await fetch(path,{headers,cache:'no-store'});
    if(!response.ok){
      let detail='下载失败（'+response.status+'）';
      try{detail=(await response.json()).detail||detail;}catch{}
      throw new Error(UI.detailError(detail));
    }
    const blob=await response.blob();
    if(!blob.size)throw new Error('下载文件为空');
    const objectUrl=URL.createObjectURL(blob);
    const link=document.createElement('a');
    link.href=objectUrl;link.download=filename;document.body.append(link);link.click();link.remove();
    setTimeout(()=>URL.revokeObjectURL(objectUrl),60000);
  }
  function mountCleanDownload(drawer,task){
    const container=drawer.querySelector('[data-clean-download-plugin]');
    if(container&&window.DolaCleanDownloadPlugin){
      window.DolaCleanDownloadPlugin.mount({container,task,api,downloadFile:downloadCleanFile,notify:UI.toast});
    }
  }
  function taskDetailHTML(result){
    const t=result.task,url=UI.safeURL(t.video_url||'');
    return fields([['任务 ID',`<span class="mono">${E(t.id)}</span>`],['平台',taskPlatformLabel(t)],['状态',TaskEvidence.badge(t)],['模型',E(t.model||'—')],['时长',E(TaskEvidence.durationText(t))],['批次',t.batch_id?E(t.batch_id)+' · 第'+E(t.batch_index)+'次':'—'],['受理时间',UI.time(t.accepted_at)],['原会话',E(t.conversation_id||'尚未建立')],['创建时间',UI.time(t.created_at)],['账号',E(t.account||'暂无派发记录')],['客户',E(t.api_key_name||'—')]])+
      `<div class="toolbar-actions"><button class="btn" data-copy-task="${E(t.id)}">复制任务 ID</button>${t.conversation_id?`<button class="btn" data-task-conversation="${E(t.id)}">查询原会话回复</button>${(!['queued','processing'].includes(t.status)||t.phase==='media_ready')?`<button class="btn" data-task-download="${E(t.id)}">取回已有视频</button>`:''}`:''}</div>`+
      `<div data-clean-download-plugin></div>`+
      `<div class="detail-block"><h3>完整提示词</h3><p style="white-space:pre-wrap">${E(t.prompt||'—')}</p></div>`+TaskEvidence.content(t,result.events)+(window.TaskResultsUI?.deleteButton(t,'task')||'')+
      (t.error?`<div class="detail-block"><h3>错误原文</h3><div class="error-text">${E(t.error)}</div></div>`:'')+
      (window.TaskResultsUI?.clipsHTML(t)||'')+
      (url?`<div class="detail-block"><h3>视频结果</h3><video class="result-video" controls preload="metadata" src="${E(url)}"></video><a class="btn" href="${E(url)}" target="_blank" rel="noopener">打开视频</a></div>`:'')+
      (t.account?`<div class="toolbar-actions" style="margin-top:20px"><button class="btn" data-account-detail="${E(t.account)}">查看关联账号</button><button class="btn" data-proxy-settings="${E(t.account)}">查看代理设置</button></div>`:'');
  }
  async function refreshTaskDetail(){
    const drawer=q('#detailDrawer'),key=drawer?.dataset.requestKey;
    if(!drawer||drawer.hidden||!key?.startsWith('task-')||!drawer.dataset.taskId)return;
    if(drawer.querySelector('[aria-busy="true"]')||[...drawer.querySelectorAll('button:disabled')].some(button=>!button.closest('[data-clean-download-plugin]')))return;
    const target=drawer.querySelector('[data-task-live]');if(!target)return;
    const result=await api('/api/admin/tasks/'+encodeURIComponent(drawer.dataset.taskId));
    if(!drawer.hidden&&drawer.dataset.requestKey===key){UI.render(target,taskDetailHTML(result));mountCleanDownload(drawer,result.task);}
  }
  async function queryTask(id,button,download=false){
    const request=detailRequest;
    button.disabled=true;
    try{
      const result=await api('/api/admin/tasks/'+encodeURIComponent(id)+(download?'/download':'/conversation'),download?{method:'POST'}:{});
      const finished=result.task?.status==='completed'||!!result.video_url;
      UI.toast(download&&finished?'已有视频已取回，实际时长见详情':result.state==='media_ready'?'已找到成片，可点击“取回已有视频”下载':Number.isFinite(result.video_count)?`已查询原会话：${result.video_count} 个视频结果`:'原会话状态已更新，结果见任务详情');
      if(request===detailRequest&&!q('#detailDrawer').hidden)await taskDetail(id);
      await loadTasks();
    }catch(error){UI.toast(UI.detailError(error.message),true);}
    finally{button.disabled=false;}
  }
  async function loadProxies(){
    const p=await api('/api/admin/proxies');applyProxyData(p);
    UI.render('#proxyStats',`<div class="stat"><div class="l">已配置代理账号</div><div class="v">${N(p.configured)} / ${N(p.total)}</div></div><div class="stat"><div class="l">独立出口 IP</div><div class="v">${N(p.independent)}</div></div><div class="stat"><div class="l">共享代理组</div><div class="v">${N(p.shared_groups)}</div></div><div class="stat"><div class="l">使用默认代理账号</div><div class="v">${N(p.missing)}</div></div>`);
    const s=p.summary||{},left=Math.max(0,Number(s.limit||0)-Number(s.used||0)),warn=(s.paused||0)+(s.exhausted||0);
    UI.render('#proxyQuota',`<div class="toolbar"><h2>ISP 请求统计</h2><span class="hint">本地请求统计，不是供应商流量余额</span></div><dl class="quota-metrics"><div><dt>已用请求</dt><dd>${N(s.used||0)}</dd></div><div><dt>${s.unlimited?'本地次数限制':'剩余请求'}</dt><dd>${s.unlimited?'次数不限':N(left)}</dd></div><div><dt>可用出口</dt><dd>${N(s.available||0)} / ${N(s.count||0)}</dd></div><div class="${warn?'is-warning':''}"><dt>预警 / 暂停或用完</dt><dd>${N(s.warn||0)} / ${N(warn)}</dd></div></dl><details class="hint"><summary>请求统计说明</summary><p>${s.unlimited?'当前仅记录请求次数，不因累计次数自动暂停；供应商的流量及有效期仍以其套餐为准。':''}检测代理约消耗一次请求；验证登录只读查询账号信息，通常一次请求，不发送聊天消息。第三方媒体与字体已自动拦截，轮询会按现有策略降频。</p></details>`);
    const gp=p.global_proxy;q('#proxyGlobal').innerHTML=p.network_mode==='isp_only'?'全 ISP 模式：所有账号请求走绑定线路；公共脚本使用本地持久缓存，缺失时仍走 ISP。':gp?.server?'全局默认代理：<span class="mono">'+E(gp.server)+'</span>':'未设置全局默认代理';
    const groups=p.groups||[];
    UI.render('#proxyGroups',groups.length?groups.map((g,i)=>{const qt=g.quota,count=g.count??g.accounts?.length??0,tag=g.own?(count>1?'共享 '+N(count)+' 个账号':'独立出口'):'默认 / 全局';return `<details class="proxy-group" data-group="${E(g.server||'default-'+i)}"><summary><div class="group-heading"><span class="addr">${E(g.server||'未配置代理')}</span><span class="badge ${g.own?'b-blue':'b-gray'}">${tag}</span>${qt?quotaState(qt):''}</div>${UI.icon('chevron')}</summary><div class="group-stats">${qt?quotaLine(qt):'<span>暂无额度记录</span>'}</div><div class="group-content"><div class="toolbar"><div class="hint">${g.own&&count>1?'组内账号共用此出口，请求合并统计。':'查看本组账号与检测结果。'}</div><div class="toolbar-actions"><button class="btn" onclick="testGroup('${E((g.accounts||[]).join(','))}',this)">检测本组</button>${qt?quotaButtons(qt):''}</div></div>${qt?quotaBar(qt):''}<label class="sr-only" for="groupSearch${i}">搜索本组账号</label><input class="search-input" id="groupSearch${i}" data-group-search placeholder="搜索本组账号" style="padding:8px 12px;border:1px solid var(--line);border-radius:8px"><div class="group-accounts">${natSort(g.accounts||[]).map(name=>`<button class="chip" data-proxy-settings="${E(name)}"><span>${E(name)}</span><span class="tiny">${E(proxyTestText(name))}</span></button>`).join('')}</div>${qt?.last_run_at?`<p class="hint">最近一次出片 ${UI.time(qt.last_run_at)}，消耗 ${N(qt.last_run_cost)} 次请求。</p>`:''}</div></details>`;}).join(''):empty('尚未配置代理','可导入代理或在账号详情中设置。'));
    renderProxyTable();
  }
  async function loadGallery(){const request=++taskRead,platform=currentGenerationPlatform();const r=await api('/api/admin/tasks?limit=50&platform='+platform);if(generationBatch?.batch_id){try{const response=await api('/api/admin/batches/'+encodeURIComponent(generationBatch.batch_id));generationBatch=response.batch||response;}catch{}}if(request!==taskRead||platform!==currentGenerationPlatform())return;tasks=r.tasks||[];renderGallery();}
  function syncGenerationDuration(){
    if(currentGenerationPlatform()==='doubao'){renderGenerationCapabilities();return;}
    const models=PlatformUI.models('dola',generationCaps,q('#genProfile')?.value||'');
    if(!models.some(m=>m.id===q('#genModel').value))q('#genModel').value=models[0]?.id||'seedance-2.0';
    const duration=q('#genDuration'),previous=duration.value;
    const model=q('#genModel').value,choice=models.find(m=>m.id===model),supported=(!(q('#genProfile')?.value||'')&&model==='seedance-2.0'?[10,5,15]:choice?.durations||[]).filter(n=>choice?.durations.includes(n));
    const choices=PlatformUI.durationChoices('dola',model,supported,generationProfile());
    duration.innerHTML=choices.map(({value,label})=>`<option value="${E(value)}">${E(label)}</option>`).join('');
    duration.value=choices.some(choice=>choice.value===previous)?previous:choices[0]?.value||'';
    const hint=PlatformUI.durationHint('dola',model,duration.value);q('#genFast15Hint').textContent=hint;q('#genFast15Hint').hidden=!hint;
    q('#genFast30Hint').hidden=true;
    PlatformUI.batchControl({query:q,prefix:'gen',platform:currentGenerationPlatform(),model:q('#genModel').value,duration:duration.value,duration_profile:q('#genProfile')?.value||'',busy:generating});
    renderGenerationAdvanced();
  }
  function renderGenerationAdvanced(){const model=selectedGenerationModels().find(m=>m.id===q('#genModel').value);generationAdvanced?.render(currentGenerationPlatform(),generationProfile()?{...model,duration_profile:generationProfile(),features:{...model?.features,resolution:{enabled:true,values:['480p','720p','1080p']}}}:model,generating,{duration:q('#genDuration').value,batch_count:q('#genBatchCount').value});}
  function renderGenerationCapabilities(){
    renderProfile();
    const platform=currentGenerationPlatform(),caps=selectedGenerationCaps(),models=selectedGenerationModels(),select=q('#genModel'),previous=select.value;
    if(platform==='doubao'||generationProfile()){
      select.innerHTML=models.length?models.map(m=>`<option value="${E(m.id)}">${E(m.label||m.id)}</option>`).join(''):'<option value="">暂无已验证模型</option>';
      if(models.some(m=>m.id===previous))select.value=previous;
      const model=models.find(m=>m.id===select.value),duration=q('#genDuration'),old=Number(duration.value),durations=model?.durations||[];
      duration.innerHTML=durations.length?durations.map(n=>`<option value="${E(String(n))}">${E(String(n))} 秒</option>`).join(''):'<option value="">无可用时长</option>';if(durations.includes(old))duration.value=String(old);
      q('#genFast15Hint').hidden=true;q('#genFast30Hint').hidden=true;
    }else{
      if(!select.querySelector('option[value="seedance-2.0"]'))select.innerHTML='<option value="seedance-2.0">Fast 2.0（约 10 分钟出片）</option><option value="seedance-1.0" hidden disabled>Seedance 1.0（试用）</option><option value="seedance-2.5" hidden disabled>Seedance 2.5（已下架）</option>';
      for(const id of ['seedance-2.0','seedance-1.0','seedance-2.5']){
        const option=select.querySelector(`option[value="${id}"]`),choice=models.find(m=>m.id===id);
        if(option){option.hidden=option.disabled=!choice;if(choice)option.textContent=choice.label||choice.id;}
      }
      select.value=models.some(m=>m.id===previous)?previous:models[0]?.id||'seedance-2.0';
      syncGenerationDuration();
    }
    const model=models.find(m=>m.id===select.value),ratios=model?.ratios||[],ratio=q('#genRatio'),oldRatio=ratio.value;
    ratio.innerHTML=(ratios.length?ratios:['default']).map(value=>`<option value="${E(value)}">${value==='default'?'自动':E(value)}</option>`).join('');if(ratios.includes(oldRatio))ratio.value=oldRatio;
    const trial=PlatformUI.modelNotice(platform,model);
    q('#genPlatformNotice').textContent=caps.enabled?(trial||(platform==='doubao'?(model?.entry_mode==='ordinary_dialog'?'已验证普通对话生成可用；仅开放实测通过的规格。':'仅提供已验证的豆包模型与素材能力。'):'Dola 账号、轮询和生成规则独立运行。')):(caps.reason||'当前平台尚无已验证能力')+(trial?' '+trial:'');
    const refs=model?.references||caps.references||{},imagePolicy=PlatformUI.imagePolicy(platform,model,caps),images=imagePolicy.enabled,media=(!generationProfile()&&platform==='dola')||PlatformUI.supports(refs.audio)||PlatformUI.supports(refs.video);
    if(imagePolicy.notice)q('#genPlatformNotice').textContent+=' '+imagePolicy.notice;ReferenceImages.setLimits(imagePolicy.limits);
    if(platform==='doubao'&&model?.entry_mode==='ordinary_dialog'&&!PlatformUI.supports(refs.video))q('#genPlatformNotice').textContent+=' 参考视频接入待当前账号验证'+(!images&&!PlatformUI.supports(refs.audio)?'，当前任务仅支持纯文本。':'，当前任务暂不支持参考视频。');
    const leftoverLinks=!!q('#genRef').value.trim();q('#genRefFiles').closest('.field').hidden=!images&&!ReferenceImages.count()&&!leftoverLinks;q('#genMediaFiles').closest('.field').hidden=!media&&!(ReferenceImages.mediaCount?.());
    q('#genRefLinks').hidden=!images||imagePolicy.localOnly;q('#genRefUrlNotice').hidden=!(leftoverLinks&&(!images||imagePolicy.localOnly));q('#genMediaFiles').accept=PlatformUI.mediaAccept(platform==='dola'?{audio:true,video:true}:refs);
    renderGenerationAdvanced();
    select.disabled=generating||!models.length;q('#genDuration').disabled=generating||!model;q('#genRatio').disabled=generating||!model;q('#genPlatform').disabled=generating;q('#genBtn').disabled=generating||!caps.enabled||!models.length;
    PlatformUI.batchControl({query:q,prefix:'gen',platform,model:select.value,duration:q('#genDuration').value,duration_profile:generationProfile(),busy:generating});
  }
  async function loadGenerationCapabilities(){const platform=currentGenerationPlatform(),request=++capabilityRead;if(generationCaps?.platform!==platform)generationCaps=null;renderGenerationCapabilities();try{const value=await api('/api/admin/video-capabilities?platform='+platform);if(request!==capabilityRead||platform!==currentGenerationPlatform())return;generationCaps=value;renderGenerationCapabilities();}catch(e){if(request!==capabilityRead||platform!==currentGenerationPlatform())return;generationCaps=platform==='dola'?null:{platform,enabled:false,models:[],reason:'豆包能力读取失败：'+UI.detailError(e.message)};renderGenerationCapabilities();}}
  function clearGenerationSubmission(){generationRevision++;pendingSubmission=null;selectedResult='';const status=q('#genStatus');status.textContent='';status.style.display='none';status.className='status';renderGallery();}
  function changeGenerationPlatform(){generationBatch=null;generationPlatform=currentGenerationPlatform();generationCaps=null;tasks=[];clearGenerationSubmission();loadGenerationCapabilities();loadGallery().catch(e=>UI.setError(e.message));}
  function renderGallery(){const list=tasks.slice(0,20);UI.render('#adminRecentTasks',(window.TaskResultsUI?.batchHTML(generationBatch)||'')+(list.length?table(['ID','提示词','账号','客户','时长','状态','创建时间','操作'],taskRows(list)):empty('暂无任务记录','填写提示词并提交一个视频任务。')));const t=tasks.find(x=>x.id===selectedResult)||tasks[0],url=UI.safeURL(t?.video_url||''),clips=window.TaskResultsUI?.clipsHTML(t)||'';UI.render('#adminResult',t?(clips||(url?`<video class="result-video" controls preload="metadata" src="${E(url)}"></video>`:`<div class="preview-empty">${UI.icon(t.status==='failed'?'alert':'video')}<strong>${t.status==='failed'?'这条任务未能完成':'正在等待视频结果'}</strong>${TaskEvidence.badge(t)}${TaskEvidence.failureExplanation(t)?`<p class="hint">${E(TaskEvidence.failureExplanation(t))}</p>`:''}</div>`))+`<div class="result-meta"><span class="mono">${E(t.id.replace('video_','').slice(0,12))}</span><button class="btn" data-task-detail="${E(t.id)}">查看详情</button>${window.TaskResultsUI?.deleteButton(t,'task')||''}</div><p class="result-prompt">${E(t.prompt||'')}</p>`:`<div class="preview-empty">${UI.icon('video')}<strong>提交任务后查看结果</strong><span>生成状态与视频预览将在这里显示</span></div>`);}
  async function submitGenerate(){const prompt=q('#genPrompt').value,status=q('#genStatus'),button=q('#genBtn'),platform=currentGenerationPlatform(),model=q('#genModel').value,durationSelection=q('#genDuration').value,duration=PlatformUI.durationValue(platform,model,durationSelection),ratio=q('#genRatio').value,revision=generationRevision,current=()=>revision===generationRevision&&platform===currentGenerationPlatform()&&model===q('#genModel').value&&durationSelection===q('#genDuration').value&&ratio===q('#genRatio').value,caps=PlatformUI.capabilities(platform,generationCaps,q('#genProfile')?.value||''),choice=PlatformUI.models(platform,generationCaps,q('#genProfile')?.value||'').find(m=>m.id===model);status.style.display='inline';if(!caps.enabled||!choice){status.className='status err';status.textContent=caps.reason||'该模型已下架或尚未验证';return;}if(!choice.durations.includes(duration)||!PlatformUI.durationChoices(platform,model,choice.durations,generationProfile()).some(item=>item.value===durationSelection)){status.className='status err';status.textContent='请选择当前可用时长';return;}if(!prompt.trim()){status.textContent='请先填写提示词';status.className='status err';q('#genPrompt').focus();return;}const imagePolicy=PlatformUI.imagePolicy(platform,choice,caps),rawRefs=q('#genRef').value.split(/[,，\n]/).map(x=>x.trim()).filter(Boolean);if(rawRefs.length&&imagePolicy.localOnly){renderGenerationCapabilities();status.className='status err';status.textContent='豆包普通对话只接受本地图片，请点击“清空图片链接”后再提交。';return;}if(!imagePolicy.enabled&&(ReferenceImages.count()||rawRefs.length)){renderGenerationCapabilities();status.className='status err';status.textContent='当前模型尚不支持参考图片，请移除已选图片和链接后再提交。';return;}let extra;try{extra=generationAdvanced?.values()||{};PlatformUI.request({platform,prompt,model,duration:durationSelection,duration_profile:q('#genProfile')?.value||'',batch_count:platform==='dola'?Number(q('#genBatchCount').value||1):1});}catch(e){status.className='status err';status.textContent=e.message;return;}if(button.disabled||generating)return;generating=true;generationPlatform=platform;button.disabled=true;renderGenerationCapabilities();ReferenceImages.setBusy(true);status.className='status';status.textContent='正在提交…';const body={...PlatformUI.request({platform,prompt,model,duration:durationSelection,ratio,duration_profile:(q('#genProfile')?.value||''),batch_count:platform==='dola'?Number(q('#genBatchCount').value||1):1}),...extra};const features=choice.references||caps.references||{},allowImages=imagePolicy.enabled,allowMedia=(!(q('#genProfile')?.value||'')&&platform==='dola')||PlatformUI.supports(features.audio)||PlatformUI.supports(features.video);const refs=(allowImages?q('#genRef').value:'').split(/[,，\n]/).map(x=>x.trim()).filter(Boolean);if(refs.length)body.reference_images=refs;const progress=text=>{if(current())status.textContent=text;};try{const uploads=allowImages?await ReferenceImages.upload(refs.length,progress):[];if(!current())return;if(uploads.length)body.reference_uploads=uploads;const mediaIds=allowMedia?await ReferenceImages.uploadMedia(0,progress):[];if(!current())return;if(mediaIds.length)body.reference_media_uploads=mediaIds;status.textContent='正在提交…';const fingerprint=JSON.stringify(body);if(!pendingSubmission||pendingSubmission.fingerprint!==fingerprint)pendingSubmission={fingerprint,key:crypto.randomUUID()};body.idempotency_key=pendingSubmission.key;const result=await api('/api/admin/generate',{method:'POST',body});if(!current())return;pendingSubmission=null;ReferenceImages.clear();generationBatch=result.batch_id?result:null;selectedResult=window.TaskResultsUI?.submissionTasks(result)[0]?.id||result.id||'';status.textContent=result.batch_id?'已创建批次，共'+result.requested_count+'次申请；提交进度与视频结果分别更新。':'已提交，任务 '+result.id.replace('video_','').slice(0,10)+'；实时状态见任务结果';UI.toast('视频任务已提交');try{await loadGallery();}catch{if(current())UI.toast('提交成功，任务列表暂未更新，请手动刷新',true);}}catch(e){if(current()){status.className='status err';status.textContent='提交失败：'+UI.detailError(e.message);}}finally{generating=false;ReferenceImages.setBusy(false);renderGenerationCapabilities();}}

  async function resumeBatch(id,button){if(button.disabled)return;button.disabled=true;try{const response=await api('/api/admin/batches/'+encodeURIComponent(id)+'/resume',{method:'POST'});generationBatch=response.batch||response;renderGallery();UI.toast('已继续调度尚未发送的申请');}catch(error){UI.toast(UI.detailError(error.message),true);}finally{button.disabled=false;}}
  document.addEventListener('click',event=>{const button=event.target.closest('[data-task-batch-resume]');if(button)resumeBatch(button.dataset.taskBatchResume,button);});
  async function deleteTask(id,button){if(button.disabled)return;button.disabled=true;try{await api('/api/admin/tasks/'+encodeURIComponent(id),{method:'DELETE'});tasks=tasks.filter(t=>t.id!==id);taskRead++;UI.closeDrawer();renderTasks();renderGallery();window.TaskResultsUI?.undoNotice(async()=>{await api('/api/admin/tasks/'+encodeURIComponent(id)+'/restore',{method:'POST'});await loadTasks();await loadGallery();});}catch(error){UI.toast(UI.detailError(error.message),true);}finally{button.disabled=false;}}
  document.addEventListener('click',event=>{const button=event.target.closest('[data-task-delete]');if(button)deleteTask(button.dataset.taskDelete,button);});
  function filterSimple(id,value){const search=value.trim().toLowerCase();document.querySelectorAll('#'+id+' tbody tr').forEach(row=>row.hidden=!!search&&!row.textContent.toLowerCase().includes(search));}
  async function refresh(options={}){
    if(q('#loginMask').style.display!=='none')return;
    if(options.background){
      if(document.hidden)return;
      // Task/result rendering leaves the generation form intact. Hidden settings
      // and permanently disabled quota choices must not suspend these live views.
      if(!['tasks','gallery'].includes(curTab)){
        const tab=q('#tab-'+curTab),active=document.activeElement;
        if(q('#dlg').style.display!=='none'||q('#videoModal').style.display!=='none'||
          (tab?.contains(active)&&active?.matches('input,select,textarea'))||
          [...(tab?.querySelectorAll('button:disabled:not(#accountQuotaFast):not(#accountQuota25)')||[])].some(b=>b.getClientRects().length&&!b.closest('.pagination'))||
          (curTab==='proxies'&&SELECTED.size))return;
      }
    }
    if(refreshing){pendingRefresh=true;return;}refreshing=true;
    const tab=curTab;q('#lastUpdate').textContent=q('#lastUpdate').textContent||'正在加载…';
    try{const loaders={dash:loadDash,accounts:loadAccounts,proxies:loadProxies,tasks:loadTasks,gallery:loadGallery,keys:window.loadKeys,users:window.loadUsers};await loaders[tab]();await refreshTaskDetail();if(curTab===tab){q('#lastUpdate').textContent='上次成功更新 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});UI.setError('');}}
    catch(e){if(e.status===401)handleUnauthorized();else UI.setError('加载失败：'+UI.detailError(e.message),()=>refresh());if(!q('#lastUpdate').textContent.startsWith('上次成功'))q('#lastUpdate').textContent='尚未成功更新';}
    finally{refreshing=false;if(pendingRefresh){pendingRefresh=false;refresh();}}
  }
  function switchTab(tab){if(!titles[tab])tab='dash';curTab=tab;document.querySelectorAll('.sidebar nav button').forEach(b=>{const on=b.dataset.tab===tab;b.classList.toggle('on',on);if(on)b.setAttribute('aria-current','page');else b.removeAttribute('aria-current');});Object.keys(titles).forEach(t=>q('#tab-'+t).style.display=t===tab?'block':'none');q('#pageTitle').textContent=titles[tab][0];q('#pageDescription').textContent=titles[tab][1];UI.setError('');UI.closeDrawer();UI.closeNavigation();const next='#'+tab;if(location.hash!==next)history.replaceState(null,'',next);refresh();}
  function goFailures(){q('#taskState').value='failed';switchTab('tasks');}
  const oldKeys=window.loadKeys,oldUsers=window.loadUsers;
  const oldAddAccount=window.openAddAccount;
  window.openAddAccount=()=>{oldAddAccount();let index=accounts.length+1;while(accounts.some(a=>a.name==='acc'+index))index++;q('#f_name').value='acc'+index;};
  window.loadKeys=async()=>{await oldKeys();filterSimple('keysTable',q('#keySearch').value);document.querySelectorAll('#keysTable .sw input').forEach(x=>x.setAttribute('aria-label','启用或停用此密钥'));};
  window.loadUsers=async()=>{await oldUsers();filterSimple('usersTable',q('#userSearch').value);};
  Object.assign(window,{loadDash,loadAccounts,loadTasks,loadProxies,submitGenerate,refresh,switchTab});
  const resumeRefresh=()=>{if(autoRefresh&&!document.hidden)refresh({background:true});};
  document.addEventListener('visibilitychange',resumeRefresh);
  window.addEventListener('focus',resumeRefresh);
  window.addEventListener('online',resumeRefresh);
  window.addEventListener('doubao-capabilities-updated',()=>{if(currentGenerationPlatform()==='doubao')loadGenerationCapabilities();});
  document.addEventListener('click',e=>{const b=e.target.closest('[data-task-detail],[data-account-detail],[data-proxy-settings],[data-proxy-test],[data-copy-task]');if(!b)return;if(b.dataset.taskDetail)taskDetail(b.dataset.taskDetail);else if(b.dataset.accountDetail)accountDetail(b.dataset.accountDetail);else if(b.dataset.proxySettings)proxySettings(b.dataset.proxySettings);else if(b.dataset.proxyTest)testOne(b.dataset.proxyTest,b);else if(b.dataset.copyTask)UI.copy(b.dataset.copyTask);});
  document.addEventListener('input',e=>{if(e.target.matches('[data-group-search]')){const query=e.target.value.toLowerCase();e.target.closest('details').querySelectorAll('.chip').forEach(x=>x.hidden=!x.textContent.toLowerCase().includes(query));}});
  document.addEventListener('click',e=>{const b=e.target.closest('[data-task-conversation],[data-task-download]');if(b)queryTask(b.dataset.taskConversation||b.dataset.taskDownload,b,!!b.dataset.taskDownload);});
  document.addEventListener('click',event=>window.TaskResultsUI?.downloadClick(event));
  document.addEventListener('DOMContentLoaded',()=>{UI.init();generationAdvanced=PlatformUI.advanced({query:q,prefix:'gen',anchor:'#genFast15Hint'});syncGenerationDuration();loadGenerationCapabilities();q('#genPlatform').addEventListener('change',changeGenerationPlatform);q('#genProfile')?.addEventListener('change',changeGenerationProfile);q('#genBatchCount')?.addEventListener('input',()=>{clearGenerationSubmission();renderGenerationAdvanced();});q('#genAdvanced').addEventListener('change',clearGenerationSubmission);q('#taskPlatform').addEventListener('change',()=>loadTasks().catch(e=>UI.setError(e.message)));q('#genModel').addEventListener('change',()=>{clearGenerationSubmission();renderGenerationCapabilities();});q('#genDuration').addEventListener('change',()=>{clearGenerationSubmission();if(currentGenerationPlatform()==='dola')syncGenerationDuration();});q('#genRatio').addEventListener('change',clearGenerationSubmission);q('#genRefClearLinks').addEventListener('click',()=>{q('#genRef').value='';clearGenerationSubmission();renderGenerationCapabilities();});const initial=location.hash.slice(1);if(titles[initial]&&initial!=='dash')switchTab(initial);});
  return {setQuotaPolicy,goFailures,filterAccounts,setCategoryScheduling,setAccountCategory,setAccountScheduling,restoreAccount,setAccountView,renderTasks,filterSimple,accountDetail,taskDetail,accountPage:delta=>{accountPage+=delta;renderAccounts();}};
})();
