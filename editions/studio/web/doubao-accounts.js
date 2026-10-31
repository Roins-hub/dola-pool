/* Independent Doubao account operations never call legacy Dola account routes. */
window.DoubaoAccounts=(()=>{
  const base='/api/admin/platforms/doubao',E=UI.esc;
  let accounts=[],revision=0,loginJob='',loginRead=0,loginTimer,calibrationRead=0,formRead=0,loginMode='qr',loginSaving=false,loginAutoCapture=false,loginAccount='';
  const selected=()=>q('#accountPlatform')?.value==='doubao';
  const accountPath=name=>base+'/accounts/'+encodeURIComponent(name);
  function displayName(name){
    const account=accounts.find(a=>a.name===name);
    if(!account||!['标准账号','普通账号'].includes(account.category))return name;
    const number=accounts.filter(a=>a.category===account.category).findIndex(a=>a.name===name)+1;
    return `${account.category}${String(number).padStart(2,'0')}_${name}`;
  }
  function environmentSummary(account){
    const env=account.browser_environment;
    if(env?.status==='invalid')return '<div class="tiny">环境异常，请检查配置</div>';
    if(env?.status==='unmatched')return '<div class="tiny">环境待配置：线路地区未匹配</div>';
    const view=env?.viewport;
    if(env?.status!=='matched'||![env.region,env.locale,env.timezone_id].every(value=>typeof value==='string'&&value.trim())||!Number.isInteger(view?.width)||view.width<=0||!Number.isInteger(view?.height)||view.height<=0)return '<div class="tiny">环境待更新</div>';
    return `<div class="tiny">地区环境：${E(env.region)} · ${E(env.locale)}</div><div class="tiny">${E(env.timezone_id)} · 固定视窗 ${view.width}×${view.height}</div><div class="tiny">${env.isolated===true?'账号资料独立':'资料隔离未确认'} · ${env.hardware_mode==='native'?'硬件标识原生':'硬件标识未确认'}</div>`;
  }
  function statusLabel(account){
    const labels={ready:'登录、线路正常',unverified:'待检查',invalid:'登录失效',login_required:'需要恢复登录',duplicate:'重复账号',identity_mismatch:'账号身份不符'};
    const status=account.category==='风控'&&!account.enabled?'风控停用':labels[account.status]||'待检查';
    const owner=account.diagnostics?.duplicate_of;
    return E(status)+(owner?`<div class="tiny">重复于：${E(owner)}</div>`:'');
  }
  const blocked=a=>['login_required','duplicate','identity_mismatch'].includes(a.status);
  const json=async(path,options)=>api(base+path,options);
  const capabilitiesUpdated=()=>window.dispatchEvent(new Event('doubao-capabilities-updated'));
  async function load(){
    const read=++revision;const result=await json('/accounts');if(!selected()||read!==revision)return;
    accounts=result.accounts||[];q('#doubaoAccountError').textContent='';
    const caps=result.capabilities||{};q('#doubaoAccountNotice').textContent=(caps.enabled?'可用规格与试验状态以视频创作中的模型说明为准。':caps.reason||'豆包生成能力尚未完成验证。')+' 本地额度独立统计，平台余额未知时显示“未验证”。';
    UI.render('#doubaoAccountsTable',accounts.length?table(['豆包账号','状态 / 轮次','绑定线路','本地用量 / 平台余额','检查时间','操作'],accounts.map(a=>[
      `<strong>${E(displayName(a.name))}</strong>`,`${a.enabled?'启用':'停用'} · ${statusLabel(a)}<div class="tiny">轮询已用 ${E(String(a.send_count||0))} 次</div>`,`${E(a.route_region||'未验证')}<div class="tiny">${E(a.proxy_label||'未绑定')}</div>${environmentSummary(a)}`,`${E(String(a.quota_used||0))} / ${a.quota_limit==null?'未设置':E(String(a.quota_limit))}<div class="tiny">预留 ${E(String(a.quota_reserved||0))} · 平台积分 ${a.platform_credits==null?'未验证':E(String(a.platform_credits))}</div>`,UI.time(a.last_checked_at),
      `<div class="toolbar-actions"><button class="btn" data-doubao-reauth="${E(a.name)}">恢复登录</button><button class="btn" data-doubao-check="${E(a.name)}">检查环境</button><button class="btn" data-doubao-calibrate="${E(a.name)}" ${blocked(a)?'disabled':''}>验证生成</button><button class="btn" data-doubao-toggle="${E(a.name)}" ${blocked(a)?'disabled title="请先恢复登录或处理重复记录"':''}>${blocked(a)?'已隔离':a.enabled?'停用':'启用'}</button><button class="btn" data-doubao-proxy="${E(a.name)}">线路</button><button class="btn btn-danger" data-doubao-delete="${E(a.name)}">删除豆包号</button></div>`
    ])):UI.empty('豆包号池暂无账号','扫码登录添加豆包账号，或导入已有豆包 Cookie，并明确绑定线路。'));
  }
  function error(error){q('#doubaoAccountError').textContent=UI.detailError(error.message||String(error));}
  function switchPool(){revision++;const isDoubao=selected();if(!isDoubao&&loginAccount){cancelLogin();hide('dlg');}q('#dolaAccountPane').hidden=isDoubao;q('#doubaoAccountPane').hidden=!isDoubao;UI.closeDrawer();if(isDoubao)load().catch(error);else window.loadAccounts().catch(error);}
  function form(title,extra,action,defaultProxy=''){cancelLogin();const read=++formRead;showDlg(`<h2>${title}</h2><label for="dbName">豆包账号名称</label><input id="dbName" maxlength="80" autocomplete="off"><label for="dbProxyRef">选择连接线路</label><select id="dbProxyRef"><option value="">请选择连接线路（直连或代理）</option></select><p id="dbProxyRefHelp" class="hint">正在读取已配置线路；请选择此豆包账号要使用的线路。</p><details><summary data-doubao-manual-proxy>手动配置另一条代理线路</summary><label for="dbProxy">完整代理地址</label><input id="dbProxy" type="password" autocomplete="new-password" placeholder="http://用户名:密码@主机:端口"><p class="hint">仅在需要新增线路时填写；已有账号的线路绑定保持独立。</p></details>${extra}<p id="dbFormError" class="error-text" role="alert"></p><div class="row"><button class="btn" data-doubao-dialog-cancel>取消</button><button class="btn btn-p" id="dbConfirm">${action}</button></div>`);q('#dbProxyRef').onchange=()=>{if(q('#dbProxyRef').value)q('#dbProxy').value='';};q('#dbProxy').oninput=()=>{if(q('#dbProxy').value.trim())q('#dbProxyRef').value='';};json('/available-proxies').then(result=>{if(read!==formRead||!q('#dbProxyRef'))return;const proxies=result.proxies||[];q('#dbProxyRef').innerHTML='<option value="">请选择连接线路（直连或代理）</option>'+proxies.map(p=>`<option value="${E(String(p.id))}">${E(p.label||[p.country,p.server].filter(Boolean).join(' · '))}</option>`).join('');if(defaultProxy&&!q('#dbProxyRef').value&&!q('#dbProxy').value.trim()&&proxies.some(p=>p.id===defaultProxy))q('#dbProxyRef').value=defaultProxy;q('#dbProxyRefHelp').textContent=proxies.length?'请明确选择本机直连或代理线路；已有 Dola 绑定保持独立，代理密码不会显示。':'暂无已配置线路，请手动配置一条可用代理线路。';}).catch(()=>{if(read===formRead&&q('#dbProxyRefHelp'))q('#dbProxyRefHelp').textContent='已配置线路读取失败，可稍后重试或手动配置。';});}
  function selectedBinding(){const proxy_ref=q('#dbProxyRef').value,proxy=q('#dbProxy').value.trim();return proxy_ref?{proxy_ref}:proxy?{proxy}:null;}

  async function cancelLogin(){const job=loginJob;loginJob='';loginRead++;loginSaving=false;loginAutoCapture=false;loginAccount='';clearTimeout(loginTimer);if(job)await json('/login/'+encodeURIComponent(job),{method:'DELETE'}).catch(()=>{});}
  function showLoginJob(job,read,interactive,autoCapture=false,account=''){
    loginAutoCapture=autoCapture;loginAccount=account;
    const locationHint='浏览器在运行项目的电脑上打开，远程网页不会在访客电脑打开。';
    showDlg(`<h2>${account?'恢复豆包账号登录':interactive?'豆包浏览器扫码登录':'豆包扫码登录'}</h2>${account?`<p>原账号：<strong>${E(displayName(account))}</strong></p>`:''}${interactive?`<p class="hint">${locationHint}</p>`:''}<p id="dbLoginInstructions" class="hint" ${interactive&&autoCapture?'':'hidden'}>${account?'优先复用原账号已有登录态，有效时自动保存 CK；仅在需要重新登录时才扫码。':'请在官方页面扫码，登录成功后自动提取 CK 并加入豆包号池'}</p><p id="dbLoginStatus" role="status">${interactive?'正在打开独立登录浏览器…':'正在打开绑定线路并读取二维码…'}</p><img id="dbLoginQR" alt="豆包登录二维码" hidden style="width:min(280px,100%);display:block;margin:16px auto"><div class="row"><button class="btn" data-doubao-dialog-cancel>取消登录</button><button class="btn btn-p" id="dbLoginConfirm" ${interactive&&!autoCapture?'disabled':'hidden'}>我已登录，保存账号</button></div>`);
    q('#dbLoginConfirm').onclick=()=>confirmLogin(job,read);pollLogin(job,read);
  }
  function resumeLogin(job){
    if(!/^[a-f0-9]{32}$/i.test(job))return;
    clearTimeout(loginTimer);loginJob=job;loginMode='browser';loginSaving=false;const read=++loginRead;
    q('#accountPlatform').value='doubao';q('#dolaAccountPane').hidden=true;q('#doubaoAccountPane').hidden=false;window.switchTab('accounts');
    showLoginJob(job,read,true);
  }
  function openLogin(mode='qr'){
    mode=mode==='browser'?'browser':'qr';
    const interactive=mode==='browser',locationHint='浏览器在运行项目的电脑上打开，远程网页不会在访客电脑打开。';
    form(interactive?'打开豆包登录浏览器':'扫码登录添加豆包账号',interactive?`<p class="hint">${locationHint}</p><p class="hint">使用所选线路打开可操作的独立浏览器；请在官方页面扫码，登录成功后自动提取 CK 并加入豆包号池。</p>`:'<p class="hint">提交后显示豆包实际登录二维码；请使用自己的豆包 App 扫码。登录态保存在豆包独立目录。</p>',interactive?'打开登录浏览器':'获取二维码',interactive?'local:direct':'');
    q('#dbConfirm').onclick=async()=>{
      const name=q('#dbName').value.trim(),binding=selectedBinding();if(!name||!binding){q('#dbFormError').textContent='请填写账号名称并选择连接线路。';return;}
      q('#dbConfirm').disabled=true;const read=++loginRead;
      try{const result=await json('/login',{method:'POST',body:{name,...binding,category:'default',...(interactive?{mode:'browser'}:{})}}),job=result.id||result.job_id;if(read!==loginRead||q('#dlg').style.display==='none'){if(job)await json('/login/'+encodeURIComponent(job),{method:'DELETE'}).catch(()=>{});return;}loginJob=job;loginMode=mode;loginSaving=false;if(!loginJob)throw new Error('登录任务未返回 ID，请刷新后检查。');showLoginJob(job,read,interactive,result.auto_capture===true);}
      catch(e){if(read!==loginRead||q('#dlg').style.display==='none')return;q('#dbFormError').textContent=UI.detailError(e.message);q('#dbConfirm').disabled=false;}
    };
  }
  async function restoreLogin(name){
    await cancelLogin();if(!selected())return;const read=++loginRead;loginMode='browser';loginAccount=name;
    showDlg(`<h2>恢复豆包账号登录</h2><p>原账号：<strong>${E(displayName(name))}</strong></p><p class="hint">使用原账号的浏览器资料和绑定线路；先检查已有登录态，有效时自动保存 CK，仅在需要重新登录时才扫码。</p><p id="dbLoginStatus" role="status">正在打开原账号登录环境…</p><div class="row"><button class="btn" data-doubao-dialog-cancel>取消登录</button></div>`);
    try{const result=await api(accountPath(name)+'/login',{method:'POST',body:{mode:'browser'}}),job=result.id||result.job_id;if(read!==loginRead||!selected()||q('#dlg').style.display==='none'){if(job)await json('/login/'+encodeURIComponent(job),{method:'DELETE'}).catch(()=>{});return;}if(!job)throw new Error('恢复登录未返回任务 ID，请检查原账号状态。');loginJob=job;loginSaving=false;showLoginJob(job,read,true,result.auto_capture===true,name);}
    catch(error){if(read===loginRead&&q('#dlg').style.display!=='none'&&q('#dbLoginStatus'))q('#dbLoginStatus').textContent=UI.detailError(error.message);}
  }
  async function applyLoginState(state,job,read){
    if(job!==loginJob||read!==loginRead||q('#dlg').style.display==='none')return true;
    const status=state.status||state.state,interactive=(state.mode||loginMode)==='browser',saved=['completed','saved','succeeded','success'].includes(status),terminal=saved||['failed','cancelled','expired','duplicate','closed','browser_closed'].includes(status),ready=['browser_open','awaiting_confirmation'].includes(status),confirm=q('#dbLoginConfirm');
    if(state.auto_capture===true)loginAutoCapture=true;
    const autoCapture=interactive&&loginAutoCapture;
    q('#dbLoginStatus').textContent=state.message||(autoCapture?(loginAccount?'正在检查原账号已有登录态；有效时自动保存，必要时再扫码。':'请在官方页面扫码，登录成功后自动提取 CK 并加入豆包号池'):interactive?'请在已打开的豆包浏览器手动登录，完成后点击“我已登录，保存账号”。':'等待扫码');
    if(q('#dbLoginInstructions'))q('#dbLoginInstructions').hidden=!autoCapture||terminal||status==='capturing';
    if(confirm){confirm.hidden=!interactive||autoCapture||terminal;confirm.disabled=loginSaving||!ready;}
    const qr=state.qr_image||'';if(!interactive&&/^data:image\/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(qr)){q('#dbLoginQR').src=qr;q('#dbLoginQR').hidden=false;}
    if(terminal){loginJob='';loginSaving=false;clearTimeout(loginTimer);q('#dbLoginQR').hidden=true;if(saved){q('#dbLoginStatus').textContent=(state.message||'账号已保存，待环境验证')+'；请检查环境与生成能力后使用。';UI.toast('豆包账号已保存，待检查');capabilitiesUpdated();if(selected())try{await load();}catch{UI.toast('账号已保存，列表刷新失败，请稍后刷新',true);}}return true;}
    return false;
  }
  async function confirmLogin(job,read){
    if(job!==loginJob||read!==loginRead||loginSaving||loginAutoCapture||q('#dbLoginConfirm')?.disabled)return;
    loginSaving=true;q('#dbLoginConfirm').disabled=true;q('#dbLoginStatus').textContent='正在读取登录状态并保存账号…';
    try{const state=await json('/login/'+encodeURIComponent(job)+'/confirm',{method:'POST'});await applyLoginState(state,job,read);}
    catch(e){if(job!==loginJob||read!==loginRead||q('#dlg').style.display==='none')return;q('#dbLoginStatus').textContent=UI.detailError(e.message)+'；登录浏览器保持打开，完成登录后可重新点击保存。';}
    finally{if(job===loginJob&&read===loginRead){loginSaving=false;if(q('#dbLoginConfirm'))q('#dbLoginConfirm').disabled=false;}}
  }
  async function pollLogin(job,read){
    if(loginJob!==job||read!==loginRead)return;
    if(q('#dlg').style.display==='none'){await cancelLogin();return;}
    try{const state=await json('/login/'+encodeURIComponent(job));if(await applyLoginState(state,job,read))return;}
    catch(e){if(job!==loginJob||read!==loginRead)return;q('#dbLoginStatus').textContent=UI.detailError(e.message);}
    loginTimer=setTimeout(()=>pollLogin(job,read),1800);
  }
  function openImport(){form('导入豆包 CK','<label for="dbCookieFile">豆包 Cookie 文件（可选）</label><input id="dbCookieFile" type="file" accept=".txt,.json"><label for="dbCookie">豆包 Cookie 内容</label><textarea id="dbCookie" rows="6" autocomplete="off" placeholder="仅支持豆包 Cookie；不会改写 Dola Cookie 域名"></textarea>','导入豆包号');q('#dbCookieFile').onchange=async event=>{const file=event.target.files?.[0];if(!file)return;if(file.size>4*1024*1024){q('#dbFormError').textContent='Cookie 文件过大。';return;}q('#dbCookie').value=await file.text();};q('#dbConfirm').onclick=async()=>{const name=q('#dbName').value.trim(),binding=selectedBinding(),ck=q('#dbCookie').value;if(!name||!binding||!ck.trim()){q('#dbFormError').textContent='请填写账号名称、豆包 Cookie 和绑定代理。';return;}q('#dbConfirm').disabled=true;try{await json('/accounts/import-ck',{method:'POST',body:{name,ck,...binding,category:'default'}});capabilitiesUpdated();hide('dlg');UI.toast('豆包账号已导入，完成环境检查后使用');await load();}catch(e){q('#dbFormError').textContent=UI.detailError(e.message);q('#dbConfirm').disabled=false;}};}
  async function openTrash(){const read=++revision;const result=await json('/trash');if(read!==revision||!selected())return;const list=result.backups||result.accounts||result.trash||[];UI.openDrawer('豆包回收站',list.length?table(['豆包账号','删除时间','操作'],list.map(a=>[E(a.name||a.account||'—'),UI.time(a.deleted_at),`<button class="btn" data-doubao-restore="${E(String(a.token||a.id||''))}">恢复豆包号</button>`])):UI.empty('豆包回收站为空'));}
  async function quota(){const result=await json('/quota-policy'),policy=result.quota_policy||result;showDlg(`<h2>豆包独立本地额度</h2><p class="hint">这是豆包号池的本地次数上限，平台积分按实际观测显示；已用与预留保留，当前不自动按零点重置。</p><label for="dbQuota">每号本地次数上限（留空表示尚未验证）</label><input id="dbQuota" type="number" min="0" value="${policy.limit==null?'':E(String(policy.limit))}"><p id="dbQuotaError" class="error-text"></p><div class="row"><button class="btn" data-doubao-dialog-cancel>取消</button><button class="btn btn-p" id="dbQuotaSave">保存豆包额度</button></div>`);q('#dbQuotaSave').onclick=async()=>{const raw=q('#dbQuota').value.trim(),limit=raw===''?null:Number(raw);if(limit!==null&&(!Number.isInteger(limit)||limit<0)){q('#dbQuotaError').textContent='请输入非负整数或留空。';return;}q('#dbQuotaSave').disabled=true;try{await json('/quota-policy',{method:'PATCH',body:{limit}});hide('dlg');await load();}catch(e){q('#dbQuotaError').textContent=UI.detailError(e.message);q('#dbQuotaSave').disabled=false;}};}
  async function calibrate(name){
    const read=++calibrationRead;showDlg('<h2>豆包验证生成</h2><p id="dbCalibrationStatus" role="status">正在通过绑定线路读取原生模型和时长选项…</p><div class="row"><button class="btn" data-doubao-dialog-cancel>关闭</button></div>');
    try{const report=await api(accountPath(name)+'/verify',{method:'POST'});if(read!==calibrationRead||q('#dlg').style.display==='none')return;nativeCalibration(name,report.checks||{},read);}
    catch(error){if(read===calibrationRead&&q('#dbCalibrationStatus'))q('#dbCalibrationStatus').textContent=UI.detailError(error.message);}
    finally{capabilitiesUpdated();}
  }
  function nativeCalibration(name,checks,read){
    const observed=checks.observed_native_options||[],choices=kind=>{const unique=new Map();observed.filter(item=>item.kind===kind).flatMap(item=>item.options||[]).forEach(item=>{if(item.value!=null&&String(item.value))unique.set(String(item.value),item);});return [...unique.values()];};
    const models=choices('model'),durations=choices('duration').filter(item=>/^\d+$/.test(String(item.value))&&Number(item.value)>0),ratios=choices('ratio'),valid=models.length>0&&durations.length>0&&ratios.length>0;
    const options=items=>items.map(item=>`<option value="${E(String(item.value))}">${E(item.label||String(item.value))}</option>`).join('');
    showDlg(`<h2>豆包视频验证 · ${E(displayName(name))}</h2><p class="hint">此操作会实际生成一条测试视频，可能消耗平台额度。模型、时长和比例仅使用刚刚观测到的普通视频原生选项；成功核对成片后才登记能力。</p><label for="dbCalibrationModel">原生模型</label><select id="dbCalibrationModel">${options(models)||'<option value="">未捕获模型选项</option>'}</select><label for="dbCalibrationDuration">原生时长</label><select id="dbCalibrationDuration">${options(durations)||'<option value="">未捕获时长选项</option>'}</select><label for="dbCalibrationRatio">原生比例</label><select id="dbCalibrationRatio">${options(ratios)||'<option value="">未捕获比例选项</option>'}</select><label for="dbCalibrationPrompt">测试提示词</label><textarea id="dbCalibrationPrompt" rows="3" placeholder="描述一条原创测试视频"></textarea><label for="dbCalibrationLead">前置要求（可选）</label><textarea id="dbCalibrationLead" rows="2" maxlength="4000" placeholder="与本次提示词合成同一条消息，不单独发送"></textarea><label><input id="dbCalibrationAutoConfirm" type="checkbox"> 平台追问时确认一次</label><p class="hint">仅在同一会话明确追问确认时回复一次，不重发原提示词；默认关闭。</p><details><summary>可选功能实验：清晰度、无水印及参考素材</summary><p class="hint">这些能力尚未在当前账号验证；平台不支持时会记录失败或待确认，不会自动重发。</p><label for="dbCalibrationResolution">要求清晰度</label><select id="dbCalibrationResolution"><option value="auto">平台默认</option><option value="720p">720p</option><option value="1080p">1080p</option></select><label><input id="dbCalibrationWatermark" type="checkbox"> 必须取得官方无水印下载</label><label for="dbCalibrationImages">参考图片（可选）</label><input id="dbCalibrationImages" type="file" accept="image/jpeg,image/png,image/webp" multiple><label for="dbCalibrationMedia">参考音频 / 视频（可选）</label><input id="dbCalibrationMedia" type="file" accept=".mp3,.wav,.m4a,.aac,.flac,.ogg,.mp4,.mov,.webm" multiple><p class="hint">选择文件时仅本地预览，点击开始后才上传。</p></details><p id="dbCalibrationStatus" role="status">${valid?'填写提示词后手动开始一次验证。':'原生选项尚未完整捕获，请先完成登录或稍后检查；当前不能开始验证。'}</p><div class="row"><button class="btn" data-doubao-dialog-cancel>关闭</button><button class="btn btn-p" id="dbCalibrationStart" ${valid?'':'disabled'}>开始一次验证生成</button></div>`);
    q('#dbCalibrationStart').onclick=async()=>{
      const button=q('#dbCalibrationStart'),status=q('#dbCalibrationStatus');if(button.disabled)return;
      const prompt=q('#dbCalibrationPrompt').value,duration=Number(q('#dbCalibrationDuration').value),resolution=q('#dbCalibrationResolution').value,images=[...q('#dbCalibrationImages').files],media=[...q('#dbCalibrationMedia').files];
      if(!prompt.trim()){status.textContent='请先填写测试提示词。';return;}if(!models.some(item=>String(item.value)===q('#dbCalibrationModel').value)||!durations.some(item=>Number(item.value)===duration)||!ratios.some(item=>String(item.value)===q('#dbCalibrationRatio').value)){status.textContent='请选择刚观测到的原生模型、时长和比例。';return;}
      const body={model:q('#dbCalibrationModel').value,duration,ratio:q('#dbCalibrationRatio').value,prompt},lead=q('#dbCalibrationLead').value;if(resolution!=='auto')body.resolution=resolution;if(q('#dbCalibrationWatermark').checked)body.require_no_watermark=true;if(q('#dbCalibrationAutoConfirm').checked)body.auto_confirm=true;if(lead.trim())body.lead_prompt=lead;
      button.disabled=true;for(const input of q('#dlg').querySelectorAll('input,select,textarea'))input.disabled=true;status.textContent='正在上传本次验证素材…';
      try{
        const active=()=>read===calibrationRead&&q('#dlg').style.display!=='none';
        for(const [files,resource,key] of [[images,'reference-images','reference_uploads'],[media,'reference-media','reference_media_uploads']]){
          if(!files.length)continue;const path='/api/admin/'+resource+'?platform=doubao',limits=await api('/api/admin/'+resource+'/config?platform=doubao');if(!active())return;
          if(files.length>limits.max_count||files.some(file=>!file.size||file.size>limits.max_bytes))throw new Error('本次参考素材数量或大小超出服务端限制。');
          body[key]=[];for(const file of files){const result=await api(path,{method:'POST',file,headers:{'X-File-Name':encodeURIComponent(file.name)}});if(!active())return;if(!result.id)throw new Error('素材上传未返回可用凭据。');body[key].push(result.id);}
        }
        if(!active())return;status.textContent='正在提交一次验证请求…';const job=await api(accountPath(name)+'/calibrate',{method:'POST',body});if(!active())return;const id=job.id||job.job_id;if(!id)throw new Error('未读取到验证任务 ID，请核查账号现有任务。');pollCalibration(id,read);
      }catch(e){if(read===calibrationRead&&q('#dbCalibrationStatus'))q('#dbCalibrationStatus').textContent=UI.detailError(e.message)+' 请先核查是否已经提交，勿重复开始。';}
    };
  }
  async function pollCalibration(id,read){if(read!==calibrationRead||q('#dlg').style.display==='none')return;try{const job=await json('/calibration/'+encodeURIComponent(id));if(read!==calibrationRead||q('#dlg').style.display==='none')return;q('#dbCalibrationStatus').textContent=job.message||job.state||'验证中';if(['completed','failed','submission_unknown'].includes(job.state||job.status)){if(job.state==='completed'){showCalibrationOutcome(job);UI.toast('豆包验证完成，能力已更新');window.dispatchEvent(new Event('doubao-capabilities-updated'));}if(selected())await load();return;}}catch(e){if(read!==calibrationRead)return;q('#dbCalibrationStatus').textContent=UI.detailError(e.message)+'，正在继续查询原验证任务。';}setTimeout(()=>pollCalibration(id,read),2500);}
  function showCalibrationOutcome(job){
    const status=q('#dbCalibrationStatus'),host=document.createElement('div'),video=UI.safeURL(job.result?.video_url||'');host.id='dbCalibrationOutcome';
    if(!video)return;
    host.innerHTML=`<p><a class="btn" href="${E(video)}" target="_blank" rel="noopener">查看验证成片</a></p><video controls preload="metadata" class="result-video" src="${E(video)}"></video>`;
    status.insertAdjacentElement('afterend',host);
  }
  function openConnection(name){
    cancelLogin();const read=++formRead;
    showDlg(`<h2>设置豆包连接线路</h2><p>${E(displayName(name))}</p><label for="dbNewProxyRef">选择连接线路</label><select id="dbNewProxyRef"><option value="">请选择连接线路（直连或代理）</option></select><p id="dbNewProxyHelp" class="hint">正在读取可用线路…</p><details><summary>手动填写另一条代理</summary><label for="dbNewProxy">完整代理地址</label><input type="password" autocomplete="new-password" id="dbNewProxy"></details><p class="hint">明确选择后才保存；换线时同步地区环境，保留该账号资料，更换后需要重新检查环境。</p><p class="error-text" id="dbProxyError"></p><div class="row"><button class="btn" data-doubao-dialog-cancel>取消</button><button class="btn btn-p" id="dbProxySave">保存豆包线路</button></div>`);
    q('#dbNewProxyRef').onchange=()=>{if(q('#dbNewProxyRef').value)q('#dbNewProxy').value='';};q('#dbNewProxy').oninput=()=>{if(q('#dbNewProxy').value.trim())q('#dbNewProxyRef').value='';};
    json('/available-proxies').then(result=>{if(read!==formRead||!q('#dbNewProxyRef'))return;q('#dbNewProxyRef').innerHTML='<option value="">请选择连接线路（直连或代理）</option>'+(result.proxies||[]).map(p=>`<option value="${E(String(p.id))}">${E(p.label||p.country||p.server)}</option>`).join('');q('#dbNewProxyHelp').textContent='可选择本机直连、豆包专用香港代理或已配置的 ISP；不会修改 Dola 或系统网络设置。';}).catch(e=>{if(read===formRead&&q('#dbNewProxyHelp'))q('#dbNewProxyHelp').textContent=UI.detailError(e.message);});
    q('#dbProxySave').onclick=async()=>{const proxy_ref=q('#dbNewProxyRef').value,proxy=q('#dbNewProxy').value.trim(),body=proxy_ref?{proxy_ref}:proxy?{proxy}:null;if(!body){q('#dbProxyError').textContent='请明确选择连接线路。';return;}q('#dbProxySave').disabled=true;try{await api(accountPath(name)+'/proxy',{method:'PATCH',body});capabilitiesUpdated();hide('dlg');if(selected())await load();}catch(e){if(q('#dbProxyError'))q('#dbProxyError').textContent=UI.detailError(e.message);if(q('#dbProxySave'))q('#dbProxySave').disabled=false;}};
  }
  async function action(button){const d=button.dataset;button.disabled=true;try{
    if(d.doubaoReauth){await restoreLogin(d.doubaoReauth);return;}
    if(d.doubaoCalibrate){await calibrate(d.doubaoCalibrate);return;}
    if(d.doubaoCheck){const result=await api(accountPath(d.doubaoCheck)+'/verify',{method:'POST'}),labels={configuration:'配置完整',proxy_auth:'代理认证',egress:'实际出口',login:'登录状态',page:'页面可用',video_entry:'视频入口',capabilities:'模型 / 时长'};const checks=result.checks||{};UI.openDrawer('豆包账号环境检查',`<p>${E(displayName(d.doubaoCheck))}</p>`+table(['检查项','结果'],Object.entries(labels).map(([key,label])=>{if(key==='proxy_auth'&&checks.route_mode==='direct')return ['连接方式',E('本机直连（未使用代理）')];const value=checks[key]??(key==='login'?checks.login_verified:key==='egress'?checks.route_verified:undefined);return [label,E(typeof value==='object'?(value?.message||value?.reason||value?.status||String(value?.ok??'未验证')):value===true?'通过':value===false?'未通过':String(value??'未验证'))];}))+`<button class="btn btn-p" data-doubao-calibrate="${E(d.doubaoCheck)}">验证生成</button>`);}
    else if(d.doubaoToggle){const a=accounts.find(a=>a.name===d.doubaoToggle);if(a)await api(accountPath(a.name),{method:'PATCH',body:{enabled:!a.enabled}});}
    else if(d.doubaoDelete){if(!confirm('将豆包账号 '+displayName(d.doubaoDelete)+' 移入豆包回收站？'))return;await api(accountPath(d.doubaoDelete),{method:'DELETE'});}
    else if(d.doubaoProxy){openConnection(d.doubaoProxy);return;}
    else if(d.doubaoRestore){await json('/trash/restore',{method:'POST',body:{token:d.doubaoRestore}});UI.closeDrawer();}
    capabilitiesUpdated();if(selected())await load();
  }catch(e){if(d.doubaoCheck)capabilitiesUpdated();error(e);}finally{button.disabled=false;}}
  document.addEventListener('DOMContentLoaded',()=>{q('#accountPlatform').addEventListener('change',switchPool);q('#doubaoBrowserLogin').onclick=()=>openLogin('browser');q('#doubaoLogin').onclick=()=>openLogin('qr');q('#doubaoImport').onclick=openImport;q('#doubaoTrash').onclick=()=>openTrash().catch(error);q('#doubaoQuota').onclick=()=>quota().catch(error);const job=new URLSearchParams(location.search).get('doubao_login');if(job)resumeLogin(job);});
  document.addEventListener('click',event=>{const button=event.target.closest('[data-doubao-reauth],[data-doubao-check],[data-doubao-calibrate],[data-doubao-toggle],[data-doubao-proxy],[data-doubao-delete],[data-doubao-restore],[data-doubao-dialog-cancel]');if(!button)return;if(button.hasAttribute('data-doubao-dialog-cancel')){calibrationRead++;cancelLogin();hide('dlg');}else action(button);});
  return {load,selected,resumeLogin};
})();
