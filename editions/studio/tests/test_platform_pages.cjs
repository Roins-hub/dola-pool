/* Real pages on local HTTP fixtures; no account or live generation is used. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('./browser-runtime.cjs');
const caps={dola:{platform:'dola',enabled:true,models:[{id:'seedance-2.0',label:'Fast 2.0',enabled:true,durations:[5,10,15],ratios:['default','16:9']}],references:{images:true,audio:true,video:true}},doubao:{platform:'doubao',enabled:false,reason:'豆包模型、时长和线路尚未完成验证',models:[]}};
async function fixture(role,{allowedPlatforms=['dola'],keyDurations=[5,10,15],doubaoEnabled=false,directVerify=false,resumeLoginId='',autoCapture=false,legacySkills=false,featureCaps=false,calibrationUnknown=false,missingNativeOptions=false}={}){
  const browser=await chromium.launch({headless:true});const context=await browser.newContext();
  await context.addInitScript(()=>{localStorage.setItem('dola_token','fixture');window.setInterval=()=>0;});
  const activeLoginId=resumeLoginId||'login_fixture';
  const requests=[],errors=[],state={holdCaps:false,releaseCaps:null,holdLogin:false,releaseLogin:null,holdUpload:false,releaseUpload:null,holdGenerate:false,releaseGenerate:null,generateFailures:0,capabilities:{},loginMode:resumeLoginId?'browser':'qr',loginConfirmFailures:0,loginConfirmed:false,browserClosed:false,loginState:'',loginMessage:'',reauthAccount:'',reauthFailure:false};
  await context.route('**/*',async route=>{
    const req=route.request(),u=new URL(req.url()),p=u.pathname;let body;try{body=req.postDataJSON?.();}catch{}requests.push({path:p,query:u.search,method:req.method(),body,bytes:req.postDataBuffer()?.length||0,headers:req.headers()});
    const json=data=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
    if(p.endsWith('/video-capabilities')){const value=state.capabilities[u.searchParams.get('platform')]||(u.searchParams.get('platform')==='doubao'&&doubaoEnabled?{platform:'doubao',enabled:true,models:[{id:'verified-doubao',label:'实测豆包模型',enabled:true,durations:[15],ratios:['default'],...(featureCaps?{features:{resolution:{enabled:true,values:['720p','1080p']},no_watermark:{enabled:true},cast:{enabled:true}},references:{images:true,audio:true,video:false}}:{})}],references:{images:true}}:caps[u.searchParams.get('platform')||'dola']);if(state.holdCaps&&u.searchParams.get('platform')==='doubao'){await new Promise(resolve=>{state.releaseCaps=async()=>{await json(value);resolve();};});return;}return json(value);}
    if(p==='/api/me')return json({user:{email:'fixture@test',role:'member'},keys:[{key:'fixture-key',name:'fixture',enabled:true,allowed_platforms:allowedPlatforms,allowed_durations:keyDurations,usage:{}}],usage:{},supported_durations:[5,10,15]});
    if(p==='/api/admin/accounts')return json({accounts:[{name:'dola-only',scheduling:true}],categories:[],quota_policy:{mode:'fast',limit:4}});
    if(p==='/api/admin/platforms/doubao/accounts')return json({accounts:state.accounts||[{name:'doubao-only',id:'original-account-id',enabled:true,proxy_label:'example.test:80',route_region:'未验证',...(state.reauthAccount&&state.loginConfirmed?{status:'ready'}:{})},...(state.loginConfirmed&&!state.reauthAccount?[{name:'扫码已保存',enabled:true,status:'unverified'}]:[])],quota_policy:{mode:'independent',limit:null},capabilities:caps.doubao});
    if(p==='/api/admin/platforms/doubao/accounts/doubao-only/proxy'&&req.method()==='PATCH'){if(state.proxyAccountUpdate)state.accounts=[state.proxyAccountUpdate];return json({});}
    if(p==='/api/admin/platforms/doubao/available-proxies')return json({proxies:[{id:'uk-fixture',label:'英国 ISP 1',server:'example.test:80',country:'英国'},{id:'local:hk',label:'香港代理（豆包专用）',server:'http://127.0.0.1:17897',country:'香港'},{id:'local:direct',label:'本机直连（豆包专用）',server:'direct',country:'本机网络'}]});
    if(p==='/api/admin/platforms/doubao/login'){state.loginMode=req.postDataJSON()?.mode||'qr';const value={id:'login_fixture',state:'starting',message:'准备登录',mode:state.loginMode,...(autoCapture?{auto_capture:true}:{})};if(state.holdLogin){await new Promise(resolve=>{state.releaseLogin=async()=>{await json(value);resolve();};});return;}return json(value);}
    if(p==='/api/admin/platforms/doubao/accounts/doubao-only/login'){
      if(state.reauthFailure)return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:{message:'原账号正在使用，请稍后重试'}})});
      state.reauthAccount='doubao-only';state.loginMode='browser';const value={id:activeLoginId,state:'starting',mode:'browser',auto_capture:true,account_name:'doubao-only'};
      if(state.holdLogin){await new Promise(resolve=>{state.releaseLogin=async()=>{await json(value);resolve();};});return;}return json(value);
    }
    if(p==='/api/admin/platforms/doubao/login/'+activeLoginId+'/confirm'){if(state.loginConfirmFailures-->0)return route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({detail:{message:'尚未发现已登录凭据，请在浏览器完成登录后重试'}})});state.loginConfirmed=true;return json({id:activeLoginId,mode:'browser',state:'completed',message:'账号已保存，等待检查',account:{name:'手动登录',status:'unverified'}});}
    if(p==='/api/admin/platforms/doubao/login/'+activeLoginId)return json(state.loginMode==='browser'?{id:activeLoginId,mode:'browser',...(autoCapture||state.reauthAccount?{auto_capture:true}:{}),state:req.method()==='DELETE'?'cancelled':state.browserClosed?'cancelled':state.loginConfirmed?'completed':state.loginState||'browser_open',message:state.loginMessage||(state.browserClosed?'登录浏览器已关闭，未保存账号':state.loginConfirmed?'账号已保存，待环境验证':state.reauthAccount?'正在检查原账号已有登录态；有效时自动保存，无需重新扫码':autoCapture?'请在官方页面扫码，登录成功后自动提取 CK 并加入豆包号池':'请在已打开的豆包浏览器手动登录'),account:state.loginConfirmed?{name:state.reauthAccount||'扫码已保存',id:state.reauthAccount?'original-account-id':'new-account-id',status:'unverified'}:undefined}:{id:activeLoginId,state:req.method()==='DELETE'?'cancelled':'waiting_scan',message:'请扫码',qr_image:'data:image/png;base64,iVBORw0KGgo='});
    if(p==='/api/admin/platforms/doubao/accounts/doubao-only/verify'){if(state.verifyCapabilities)state.capabilities.doubao=state.verifyCapabilities;return json({ok:state.verifyCapabilities?state.verifyCapabilities.enabled:true,checks:{configuration:true,...(directVerify?{route_mode:'direct',proxy_auth:null}:{}),...(legacySkills?{entry_mode:'work_skill',parameters_via_prompt:true,observed_skills:[{id:'skill-observed-25',label:'Seedance 2.5 实测技能',description:'最多30秒',selector:'button[data-skill="25"]',max_duration:30}]}:{observed_native_options:missingNativeOptions?[]:[{kind:'model',selector:'#model',options:[{value:'native-model',label:'原生实测模型'}]},{kind:'duration',selector:'#duration',options:[{value:'15',label:'15 秒'}]},{kind:'ratio',selector:'#ratio',options:[{value:'9:16',label:'竖屏'}]}]})}});}
    if(p.endsWith('/calibrate'))return json({id:'calibration_fixture',state:'queued',message:'校准排队'});
    if(p.endsWith('/calibration/calibration_fixture'))return json({id:'calibration_fixture',state:calibrationUnknown?'submission_unknown':'completed',message:calibrationUnknown?'原会话等待确认，未重复提交':'验证完成',...(calibrationUnknown?{}:{result:{video_url:'/videos/video_'+'a'.repeat(32)+'.mp4'}})});
    if(req.method()==='POST'&&p.endsWith('/reference-images')){if(state.holdUpload){await new Promise(resolve=>{state.releaseUpload=async()=>{await json({id:'opaque-image-token'});resolve();};});return;}return json({id:'opaque-image-token'});}
    if(req.method()==='POST'&&p.endsWith('/reference-media'))return json({id:'opaque-audio-token'});
    if(p.endsWith('/tasks'))return json({tasks:state.taskRecords?.[u.searchParams.get('platform')]||[{id:'video_fixture',platform:'doubao',prompt:'豆包任务',model:'fixture',duration:15,status:'failed'}]});
    if(p.endsWith('/tasks/video_verify_fixture'))return json({task:state.taskRecords.all[0],events:[]});
    if(p.endsWith('/config'))return json({max_count:30,max_bytes:15728640});
    if(p.endsWith('/generate')||p.endsWith('/generations')){if(state.holdGenerate){await new Promise(resolve=>{state.releaseGenerate=async()=>{await json({id:'video_old_platform',status:'queued'});resolve();};});return;}if(state.generateFailures-->0)return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'提交暂未完成'})});return json({id:'video_new',status:'queued'});}
    if(p.startsWith('/api/')||p.startsWith('/v1/'))return json({});
    const name=p==='/admin'?'index.html':p==='/member'?'member.html':p.slice(1),file=path.join(__dirname,'..','web',name);
    if(!fs.existsSync(file))return route.fulfill({status:404,body:''});
    return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(file)});
  });
  const page=await context.newPage();page.setDefaultTimeout(2500);page.on('pageerror',e=>errors.push(String(e)));
  await page.goto('https://fixture.test/'+role+(resumeLoginId?'?doubao_login='+resumeLoginId+'#accounts':role==='admin'?'#gallery':''));
  return {browser,page,requests,errors,state};
}
test('Dola 2.5 is delisted on both pages even when a stale catalog says enabled',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests,errors,state}=await fixture(role);
    const prefix=role==='admin'?'gen':'member';
    try{
      state.capabilities.dola={...caps.dola,models:[...caps.dola.models,
        {id:'seedance-2.5',label:'Seedance 2.5（10 秒试验）',enabled:true,durations:[5,10,15,30],ratios:['default','9:16'],validation_status:'experimental'}]};
      await page.reload();
      const model='#'+prefix+'Model',duration='#'+prefix+'Duration';
      await page.locator(model+' option[value="seedance-2.5"]').waitFor({state:'attached'});
      assert.equal(await page.locator(model+' option[value="seedance-2.5"]').isDisabled(),true);
      assert.deepEqual(await page.locator(model+' option').evaluateAll(nodes=>nodes.filter(n=>!n.hidden&&!n.disabled).map(n=>n.value)),['seedance-2.0']);
      await page.locator(model).evaluate(select=>{select.value='seedance-2.5';select.dispatchEvent(new Event('change',{bubbles:true}));});
      assert.equal(await page.locator(model).inputValue(),'seedance-2.0');
      assert.deepEqual((await page.locator(duration+' option').evaluateAll(nodes=>nodes.map(n=>n.value))).sort(),['10','5']);
      for(const width of [1440,375]){
        await page.setViewportSize({width,height:1000});
        assert.equal(await page.evaluate('document.documentElement.scrollWidth<=innerWidth'),true);
      }
      assert.equal(await page.locator(model+' option[value="seedance-1.0"]').isDisabled(),true);
      assert.equal(requests.filter(r=>r.method==='POST'&&(r.path==='/api/admin/generate'||r.path==='/v1/videos/generations')).length,0);
      assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});

test('admin can retrieve a queried media-ready video without a false downloading state',async()=>{
  const {browser,page,errors,state}=await fixture('admin');try{
    const task={id:'video_verify_fixture',platform:'doubao',prompt:'已完成的原会话',model:'dialog_seedance_2.5',duration:30,
      status:'processing',phase:'media_ready',conversation_id:'original',created_at:Date.now()/1000};
    state.taskRecords={all:[task],dola:[task],doubao:[task]};
    let downloads=0;
    await page.route('**/api/admin/tasks/video_verify_fixture/download',async route=>{
      downloads++;Object.assign(task,{status:'completed',phase:'completed',actual_duration:30,video_url:'https://fixture.test/videos/existing.mp4'});
      await route.fulfill({contentType:'application/json',body:JSON.stringify({task,state:'completed'})});
    });
    await page.reload();await page.locator('.sidebar [data-tab="tasks"]').click();
    await page.locator('#tasksTable [data-task-detail]').first().click();
    const drawer=page.locator('#detailDrawer');
    await drawer.getByText('已有成片，等待下载',{exact:true}).first().waitFor();
    assert.doesNotMatch(await drawer.innerText(),/已有成片，下载中/);
    await drawer.locator('[data-task-download]').click();
    await drawer.getByText('已完成，时长已核对',{exact:true}).first().waitFor();
    assert.equal(downloads,1);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

test('both pages show actionable manual verification without claiming generating',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests,errors,state}=await fixture(role,{allowedPlatforms:['dola','doubao']});try{
      const task={id:'video_verify_fixture',platform:'doubao',prompt:'验证等待示例（本地测试）',
        model:'dialog_seedance_2.0_fast',duration:15,status:'processing',phase:'waiting_verification',created_at:Date.now()/1000};
      state.taskRecords={all:[task],dola:[task],doubao:[task]};
      await page.reload();
      await page.locator(role==='admin'?'.sidebar [data-tab="tasks"]':'.sidebar [data-view="tasks"]').click();
      const table=page.locator(role==='admin'?'#tasksTable':'#memberTasks');
      await table.getByText('等待人工验证',{exact:true}).waitFor();
      assert.doesNotMatch(await table.innerText(),/生成中|已受理/);
      await table.locator(role==='admin'?'[data-task-detail]':'[data-member-task]').first().click();
      await page.locator('#detailDrawer').getByText(/不要重复点击生成/).waitFor();
      const out=path.join(__dirname,'..','.impeccable','doubao-integration','verification-v0813');fs.mkdirSync(out,{recursive:true});
      await page.screenshot({path:path.join(out,role+'-verification.png')});
      assert.equal(requests.some(r=>r.method==='POST'),false);assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});
test('admin switches account pools and blocks unverified Doubao generation',async()=>{
  const {browser,page,requests,errors}=await fixture('admin');try{
    await page.locator('#genPlatform').selectOption('doubao');
    await page.locator('#genPlatformNotice').filter({hasText:'尚未完成验证'}).waitFor();
    assert.equal(await page.locator('#genBtn').isDisabled(),true);
    assert.equal(await page.locator('#genModel option').count(),1);
    await page.locator('#genPrompt').fill('保留提示词');await page.locator('#genPlatform').selectOption('dola');
    assert.equal(await page.locator('#genPrompt').inputValue(),'保留提示词');
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');
    await page.locator('#doubaoAccountsTable').filter({hasText:'doubao-only'}).waitFor();
    assert.equal(await page.locator('#dolaAccountPane').isHidden(),true);
    assert.ok(await page.locator('#doubaoLogin').isVisible());
    assert.equal(requests.some(r=>r.method==='POST'),false);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('both ordinary Dola pages submit five and ten seconds without an expert profile and preserve references',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests,errors}=await fixture(role);try{const p=role==='admin'?'gen':'member',endpoint=role==='admin'?'/api/admin/generate':'/v1/videos/generations';
      await page.locator('#'+p+'Duration option[value="10"]').waitFor({state:'attached'});
      assert.equal(await page.locator('#'+p+'Duration option[value="15"]').count(),0);
      await page.locator('#'+p+'Prompt').fill('原创小猫');
      if(role==='admin'){await page.locator('#genRef').evaluate(n=>n.closest('details').open=true);await page.locator('#genRef').evaluate(n=>n.closest('details').open=true);await page.locator('#genRef').fill('https://example.test/cat.png');}else{await page.locator('#memberRefs').evaluate(n=>n.closest('details').open=true);await page.locator('#memberRefs').fill('https://example.test/cat.png');}
      for(const seconds of [10,5]){await page.locator('#'+p+'Duration').selectOption(String(seconds));await page.locator(role==='admin'?'#genBtn':'#memberSubmit').click();await page.locator('#'+p+'Status').filter({hasText:'已提交'}).waitFor();const body=requests.findLast(r=>r.path===endpoint).body;assert.equal(body.duration,seconds);assert.equal(body.duration_profile,undefined);assert.deepEqual(body.reference_images,['https://example.test/cat.png']);}
      assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});

test('member cannot submit an unpublished ordinary duration by tampering with a select',async()=>{
  for(const keyDurations of [[15],[30]]){const {browser,page,requests,errors}=await fixture('member',{keyDurations});try{
    await page.locator('#memberPrompt').fill('原文');assert.equal(await page.locator('#memberSubmit').isDisabled(),true);
    await page.locator('#memberDuration').evaluate(select=>{select.add(new Option('正常15','15-normal'));select.value='15-normal';});
    await page.locator('#memberForm').evaluate(form=>form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));await page.locator('#memberStatus').filter({hasText:'请选择所选模型与密钥允许的时长'}).waitFor();
    assert.equal(requests.some(r=>r.path==='/v1/videos/generations'),false);assert.deepEqual(errors,[]);
  }finally{await browser.close();}}
});

test('changing generation platform clears the old submission banner but preserves the draft and references',async()=>{
  const {browser,page,errors}=await fixture('admin');try{
    await page.locator('#genPrompt').fill('保留原创小猫提示词');await page.locator('#genBtn').click();await page.locator('#genStatus').filter({hasText:'已提交'}).waitFor();
    await page.locator('#genRef').evaluate(n=>n.closest('details').open=true);await page.locator('#genRef').fill('https://example.test/cat.png');await page.locator('#genRefFiles').setInputFiles({name:'cat.png',mimeType:'image/png',buffer:Buffer.from('fixture')});
    await page.locator('#genPlatform').selectOption('doubao');await page.locator('#genPlatformNotice').filter({hasText:'尚未完成验证'}).waitFor();
    assert.equal(await page.locator('#genStatus').textContent(),'');assert.equal(await page.locator('#genStatus').isVisible(),false);assert.equal(await page.locator('#genBtn').isDisabled(),true);
    assert.equal(await page.locator('#genPrompt').inputValue(),'保留原创小猫提示词');assert.equal(await page.locator('#genRef').inputValue(),'https://example.test/cat.png');assert.equal(await page.locator('#genRefPreview img').count(),1);
    await page.locator('#genPlatform').selectOption('dola');assert.equal(await page.locator('#genRefPreview img').count(),1);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('changing model duration or ratio clears stale submission errors and resets the idempotency attempt',async()=>{
  for(const field of ['Model','Duration','Ratio']){
    const {browser,page,requests,errors,state}=await fixture('admin',{doubaoEnabled:true});try{
      state.capabilities.doubao={platform:'doubao',enabled:true,models:[{id:'dialog-a',entry_mode:'ordinary_dialog',durations:[10,15],ratios:['16:9','9:16']},{id:'dialog-b',entry_mode:'ordinary_dialog',durations:[10,15],ratios:['16:9','9:16']}],references:{images:true}};
      await page.locator('#genPlatform').selectOption('doubao');await page.locator('#genModel option[value="dialog-a"]').waitFor({state:'attached'});await page.locator('#genPrompt').fill('原创猫咪');
      state.generateFailures=1;await page.locator('#genBtn').click();await page.locator('#genStatus').filter({hasText:'提交失败'}).waitFor();
      const select=page.locator('#gen'+field),before=await select.inputValue(),other={Model:'dialog-b',Duration:'15',Ratio:'9:16'}[field];await select.selectOption(other);
      assert.equal(await page.locator('#genStatus').textContent(),'');await select.selectOption(before);await page.locator('#genBtn').click();await page.locator('#genStatus').filter({hasText:'已提交'}).waitFor();
      const sent=requests.filter(r=>r.path==='/api/admin/generate');assert.equal(sent.length,2);assert.notEqual(sent[0].body.idempotency_key,sent[1].body.idempotency_key,field);assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});
test('changing parameters releases the old selected result association without deleting task history',async()=>{
  const {browser,page,errors,state}=await fixture('admin');try{
    state.taskRecords={dola:[{id:'video_recent',platform:'dola',prompt:'最近历史任务',duration:10,status:'failed'},{id:'video_new',platform:'dola',prompt:'原先选中的已提交任务',duration:10,status:'queued'}]};
    await page.locator('#genPrompt').fill('新任务正文');await page.locator('#genBtn').click();await page.locator('#genStatus').filter({hasText:'已提交'}).waitFor();await page.locator('#adminResult').filter({hasText:'原先选中的已提交任务'}).waitFor();
    await page.locator('#genDuration').selectOption('5');assert.match(await page.locator('#adminResult').innerText(),/最近历史任务/);assert.doesNotMatch(await page.locator('#adminResult').innerText(),/原先选中的已提交任务/);assert.match(await page.locator('#adminRecentTasks').innerText(),/原先选中的已提交任务/);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('an old platform submission response cannot display a submitted result on a newly selected platform',async()=>{
  const {browser,page,requests,errors,state}=await fixture('admin');try{
    await page.locator('#genPrompt').fill('一只原创猫咪');state.holdGenerate=true;await page.locator('#genBtn').click();for(let i=0;i<100&&!state.releaseGenerate;i++)await page.waitForTimeout(10);assert.ok(state.releaseGenerate);
    await page.locator('#genPlatform').selectOption('doubao',{force:true});await page.locator('#genPlatformNotice').filter({hasText:'尚未完成验证'}).waitFor();await state.releaseGenerate();
    await page.waitForFunction(()=>!document.querySelector('#genPlatform').disabled);
    assert.equal(await page.locator('#genStatus').textContent(),'');assert.equal(await page.locator('#genPlatform').inputValue(),'doubao');assert.equal(await page.locator('#genBtn').isDisabled(),true);assert.equal(requests.filter(r=>r.path==='/api/admin/generate').length,1);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('a verified dialogue model remains visible while unavailable accounts keep generation blocked with their reason',async()=>{
  const {browser,page,requests,errors,state}=await fixture('admin');try{
    state.capabilities.doubao={platform:'doubao',enabled:false,reason:'请恢复豆包账号登录',models:[{id:'dialog_seedance_2.0_fast',label:'Seedance 2.0 Fast（普通对话）',entry_mode:'ordinary_dialog',enabled:true,durations:[15],ratios:['16:9']}]};
    await page.locator('#genPlatform').selectOption('doubao');await page.locator('#genModel option[value="dialog_seedance_2.0_fast"]').waitFor({state:'attached'});
    assert.equal(await page.locator('#genDuration').inputValue(),'15');assert.equal(await page.locator('#genRatio').inputValue(),'16:9');assert.equal(await page.locator('#genBtn').isDisabled(),true);assert.match(await page.locator('#genPlatformNotice').innerText(),/恢复豆包账号登录/);assert.equal(requests.some(r=>r.path==='/api/admin/generate'),false);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('account environment results immediately refresh cached gallery availability while preserving selected parameters and draft',async()=>{
  const {browser,page,requests,errors,state}=await fixture('admin');try{
    const models=['dialog-a','dialog-b'].map(id=>({id,entry_mode:'ordinary_dialog',enabled:true,durations:[10,15],ratios:['16:9','9:16'],references:{images:true}}));
    state.capabilities.doubao={platform:'doubao',enabled:false,reason:'请检查豆包账号环境',models};await page.locator('#genPlatform').selectOption('doubao');await page.locator('#genModel option[value="dialog-b"]').waitFor({state:'attached'});
    await page.locator('#genModel').selectOption('dialog-b');await page.locator('#genDuration').selectOption('15');await page.locator('#genRatio').selectOption('9:16');await page.locator('#genPrompt').fill('保留原创小猫正文');await page.locator('#genRef').evaluate(n=>n.closest('details').open=true);await page.locator('#genRef').fill('https://example.test/keep.png');await page.locator('#genRefFiles').setInputFiles({name:'keep.png',mimeType:'image/png',buffer:Buffer.from('fixture')});assert.equal(await page.locator('#genBtn').isDisabled(),true);
    state.verifyCapabilities={platform:'doubao',enabled:true,models};await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');await page.locator('[data-doubao-check="doubao-only"]').click();await page.locator('#detailDrawer').filter({hasText:'豆包账号环境检查'}).waitFor();await page.locator('[data-tab="gallery"]').click();await page.waitForFunction(()=>!document.querySelector('#genBtn').disabled);
    assert.equal(await page.locator('#genModel').inputValue(),'dialog-b');assert.equal(await page.locator('#genDuration').inputValue(),'15');assert.equal(await page.locator('#genRatio').inputValue(),'9:16');assert.equal(await page.locator('#genPrompt').inputValue(),'保留原创小猫正文');assert.equal(await page.locator('#genRef').inputValue(),'https://example.test/keep.png');assert.equal(await page.locator('#genRefPreview img').count(),1);
    state.verifyCapabilities={platform:'doubao',enabled:false,reason:'豆包账号登录已失效，请恢复登录',models};await page.locator('[data-tab="accounts"]').click();await page.locator('[data-doubao-check="doubao-only"]').click();await page.locator('#detailDrawer').filter({hasText:'豆包账号环境检查'}).waitFor();await page.locator('[data-tab="gallery"]').click();await page.locator('#genPlatformNotice').filter({hasText:'登录已失效'}).waitFor();assert.equal(await page.locator('#genBtn').isDisabled(),true);assert.equal(await page.locator('#genModel').inputValue(),'dialog-b');assert.equal(await page.locator('#genRatio').inputValue(),'9:16');assert.equal(requests.some(r=>r.path==='/api/admin/generate'),false);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('member sees the verified dialogue parameters but cannot submit while the account is unavailable',async()=>{
  const {browser,page,requests,errors,state}=await fixture('member',{allowedPlatforms:['dola','doubao']});try{
    state.capabilities.doubao={platform:'doubao',enabled:false,reason:'请恢复豆包账号登录',models:[{id:'dialog_seedance_2.0_fast',label:'Seedance 2.0 Fast（普通对话）',entry_mode:'ordinary_dialog',enabled:true,durations:[15],ratios:['16:9']}]};
    await page.locator('#memberPlatform').selectOption('doubao');await page.locator('#memberModel option[value="dialog_seedance_2.0_fast"]').waitFor({state:'attached'});await page.locator('#memberPrompt').fill('原创小猫');
    assert.equal(await page.locator('#memberDuration').inputValue(),'15');assert.equal(await page.locator('#memberRatio').inputValue(),'16:9');assert.equal(await page.locator('#memberSubmit').isDisabled(),true);
    await page.locator('#memberForm').evaluate(form=>form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true})));await page.locator('#memberStatus').filter({hasText:'恢复豆包账号登录'}).waitFor();
    assert.equal(requests.some(r=>r.path==='/v1/videos/generations'),false);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('both pages expose experimental Doubao thirty seconds without treating it as verified or mapping it to fifteen',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests,errors,state}=await fixture(role,{allowedPlatforms:['dola','doubao'],keyDurations:[5,10,15,30]});try{
      state.capabilities.doubao={platform:'doubao',enabled:true,models:[
        {id:'dialog_seedance_2.0_fast',label:'Seedance 2.0 Fast（普通对话）',entry_mode:'ordinary_dialog',validation_status:'verified',enabled:true,durations:[15],ratios:['16:9']},
        {id:'dialog_seedance_2.5',label:'Seedance 2.5（30秒试验）',entry_mode:'ordinary_dialog',validation_status:'experimental',account_category:'标准账号',enabled:true,durations:[30],ratios:['16:9']}
      ]};
      const prefix=role==='admin'?'gen':'member',model=page.locator('#'+prefix+'Model'),duration=page.locator('#'+prefix+'Duration'),notice=page.locator('#'+prefix+'PlatformNotice'),endpoint=role==='admin'?'/api/admin/generate':'/v1/videos/generations',button=page.locator(role==='admin'?'#genBtn':'#memberSubmit');
      await page.locator('#'+prefix+'Platform').selectOption('doubao');await model.locator('option[value="dialog_seedance_2.5"]').waitFor({state:'attached'});await model.selectOption('dialog_seedance_2.5');
      assert.equal(await model.locator('option:checked').innerText(),'Seedance 2.5（30秒试验）');assert.equal(await duration.inputValue(),'30');assert.equal(await duration.locator('option').count(),1);assert.equal(await page.locator('#'+prefix+'Ratio').inputValue(),'16:9');
      const text=await notice.innerText();assert.match(text,/2\.5.*30秒试验/);assert.match(text,/真实申请30秒/);assert.match(text,/仅标准账号/);assert.match(text,/尚未实测/);assert.match(text,/5倍消耗/);assert.match(text,/费用以平台为准/);assert.doesNotMatch(text,/已验证普通对话|仅开放实测通过/);assert.equal(await button.isDisabled(),false);
      if(role==='member'){assert.match(await page.locator('#memberExample').innerText(),/dialog_seedance_2\.5/);assert.match(await page.locator('#memberExample').innerText(),/duration = 30/);assert.doesNotMatch(await page.locator('#memberExample').innerText(),/duration_profile/);}
      await page.locator('#'+prefix+'Prompt').fill('原创小猫在草地跳舞');await button.click();await page.locator('#'+prefix+'Status').filter({hasText:'已提交'}).waitFor();
      let body=requests.findLast(r=>r.path===endpoint).body;assert.equal(body.platform,'doubao');assert.equal(body.model,'dialog_seedance_2.5');assert.equal(body.duration,30);assert.equal(body.ratio,'16:9');assert.equal(body.duration_profile,undefined);
      await model.selectOption('dialog_seedance_2.0_fast');assert.equal(await duration.inputValue(),'15');assert.doesNotMatch(await notice.innerText(),/30秒试验|5倍消耗/);await button.click();await page.locator('#'+prefix+'Status').filter({hasText:'已提交'}).waitFor();body=requests.findLast(r=>r.path===endpoint).body;assert.equal(body.model,'dialog_seedance_2.0_fast');assert.equal(body.duration,15);assert.equal(body.duration_profile,undefined);
      await page.locator('#'+prefix+'Platform').selectOption('dola');assert.equal(await model.inputValue(),'seedance-2.0');assert.equal(await model.locator('option[value="dialog_seedance_2.5"]').count(),0);assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});
test('unavailable experimental Doubao keeps its account reason and trial explanation visible without allowing submission',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests,errors,state}=await fixture(role,{allowedPlatforms:['dola','doubao'],keyDurations:[30]});try{
      state.capabilities.doubao={platform:'doubao',enabled:false,reason:'暂无可用的标准账号，请检查登录',models:[{id:'dialog_seedance_2.5',label:'Seedance 2.5（30秒试验）',entry_mode:'ordinary_dialog',validation_status:'experimental',account_category:'标准账号',enabled:true,durations:[30],ratios:['16:9']}]};
      const prefix=role==='admin'?'gen':'member';await page.locator('#'+prefix+'Platform').selectOption('doubao');await page.locator('#'+prefix+'Model option[value="dialog_seedance_2.5"]').waitFor({state:'attached'});
      assert.equal(await page.locator('#'+prefix+'Duration').inputValue(),'30');const text=await page.locator('#'+prefix+'PlatformNotice').innerText();assert.match(text,/暂无可用的标准账号/);assert.match(text,/30秒试验/);assert.match(text,/尚未实测/);assert.equal(await page.locator(role==='admin'?'#genBtn':'#memberSubmit').isDisabled(),true);assert.equal(requests.some(r=>r.path.endsWith('/generate')||r.path.endsWith('/generations')),false);assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});
test('ordinary dialogue explains that reference video is pending verification without implying upload support',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests,errors,state}=await fixture(role,{allowedPlatforms:['dola','doubao']});try{
      state.capabilities.doubao={platform:'doubao',enabled:false,reason:'请在账号管理恢复豆包账号登录并检查线路',models:[{id:'dialog_seedance_2.0_fast',label:'Seedance 2.0 Fast（普通对话）',entry_mode:'ordinary_dialog',enabled:true,durations:[15],ratios:['16:9'],references:{images:{enabled:false},audio:{enabled:false},video:{enabled:false}}}]};
      const prefix=role==='admin'?'gen':'member';await page.locator('#'+prefix+'Platform').selectOption('doubao');await page.locator('#'+prefix+'Model option[value="dialog_seedance_2.0_fast"]').waitFor({state:'attached'});
      const note=await page.locator('#'+prefix+'PlatformNotice').innerText();assert.match(note,/恢复豆包账号登录/);assert.match(note,/参考视频.*待.*验证/);assert.match(note,/仅支持纯文本/);
      assert.equal(await page.locator('#'+prefix+'MediaFiles').isVisible(),false);assert.equal(await page.locator(role==='admin'?'#genBtn':'#memberSubmit').isDisabled(),true);assert.equal(requests.some(r=>/\/reference-media$/.test(r.path)&&r.method==='POST'),false);assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});

test('ordinary dialogue image experiments expose local previews and submit owned images without changing durations',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests,errors,state}=await fixture(role,{allowedPlatforms:['dola','doubao'],keyDurations:[15,30]});try{
      state.capabilities.doubao={platform:'doubao',enabled:true,models:[
        {id:'dialog_seedance_2.0_fast',label:'Seedance 2.0 Fast',entry_mode:'ordinary_dialog',enabled:true,durations:[15],ratios:['16:9'],references:{images:{enabled:true,validation_status:'experimental',local_only:true,max_count:1,max_bytes:15728640}}},
        {id:'dialog_seedance_2.5',label:'Seedance 2.5（30秒试验）',entry_mode:'ordinary_dialog',validation_status:'experimental',account_category:'标准账号',enabled:true,durations:[30],ratios:['16:9'],references:{images:{enabled:true,validation_status:'experimental',local_only:true,max_count:1,max_bytes:15728640}}}
      ]};
      const prefix=role==='admin'?'gen':'member',submit=role==='admin'?'#genBtn':'#memberSubmit',links=role==='admin'?'#genRef':'#memberRefs',endpoint=role==='admin'?'/api/admin/generate':'/v1/videos/generations';
      await page.locator('#'+prefix+'Platform').selectOption('doubao');await page.locator('#'+prefix+'Model option[value="dialog_seedance_2.0_fast"]').waitFor({state:'attached'});
      assert.equal(await page.locator('#'+prefix+'RefFiles').isVisible(),true);assert.equal(await page.locator(links).isVisible(),false);
      assert.match(await page.locator('#'+prefix+'RefHelp').innerText(),/最多 1 张/);assert.doesNotMatch(await page.locator('#'+prefix+'RefHelp').innerText(),/链接/);
      assert.match(await page.locator('#'+prefix+'PlatformNotice').innerText(),/参考图片.*试验/);assert.match(await page.locator('#'+prefix+'PlatformNotice').innerText(),/出片.*待.*实测|尚未.*验证.*出片/);
      await page.locator('#'+prefix+'Prompt').fill('参考图片中的原创小猫跳舞');
      for(const [model,duration] of [['dialog_seedance_2.0_fast',15],['dialog_seedance_2.5',30]]){
        await page.locator('#'+prefix+'Model').selectOption(model);
        await page.locator('#'+prefix+'RefFiles').setInputFiles({name:'cat.png',mimeType:'image/png',buffer:Buffer.from('image-fixture')});
        assert.equal(await page.locator('#'+prefix+'RefPreview img').count(),1);
        await page.getByRole('button',{name:'移除 cat.png',exact:true}).click();assert.equal(await page.locator('#'+prefix+'RefPreview img').count(),0);
        await page.locator('#'+prefix+'RefFiles').setInputFiles({name:'cat.png',mimeType:'image/png',buffer:Buffer.from('image-fixture')});
        assert.equal(requests.some(r=>r.path.endsWith('/reference-images')&&r.method==='POST'),duration===30);
        await page.locator(submit).click();await page.locator('#'+prefix+'Status').filter({hasText:'已提交'}).waitFor();
        const upload=requests.findLast(r=>r.path.endsWith('/reference-images')&&r.method==='POST');assert.equal(upload.query,'?platform=doubao');assert.equal(upload.bytes,13);
        const body=requests.findLast(r=>r.path===endpoint).body;assert.equal(body.platform,'doubao');assert.equal(body.model,model);assert.equal(body.duration,duration);assert.equal(body.duration_profile,undefined);assert.deepEqual(body.reference_uploads,['opaque-image-token']);assert.equal(body.reference_images,undefined);
      }
      assert.equal(requests.filter(r=>r.path===endpoint).length,2);assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});

test('ordinary dialogue refuses leftover image URLs instead of silently submitting an incomplete request',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests,errors,state}=await fixture(role,{allowedPlatforms:['dola','doubao']});try{
      state.capabilities.doubao={platform:'doubao',enabled:true,models:[{id:'dialog_seedance_2.0_fast',entry_mode:'ordinary_dialog',enabled:true,durations:[15],ratios:['16:9'],references:{images:{enabled:true,validation_status:'experimental',local_only:true,max_count:1,max_bytes:15728640}}}]};
      const prefix=role==='admin'?'gen':'member',links=role==='admin'?'#genRef':'#memberRefs';await page.locator('#'+prefix+'Platform').selectOption('doubao');await page.locator('#'+prefix+'Model option[value="dialog_seedance_2.0_fast"]').waitFor({state:'attached'});
      await page.locator(links).evaluate(input=>input.value='https://example.test/old-reference.png');await page.locator('#'+prefix+'Prompt').fill('保留参考图要求');await page.locator(role==='admin'?'#genBtn':'#memberSubmit').click();
      await page.locator('#'+prefix+'Status').filter({hasText:/本地图片/}).waitFor();
      assert.equal(requests.some(r=>r.path.endsWith('/generate')||r.path.endsWith('/generations')),false);assert.equal(await page.locator(links).inputValue(),'https://example.test/old-reference.png');assert.deepEqual(errors,[]);
      await page.getByRole('button',{name:'清空图片链接',exact:true}).click();assert.equal(await page.locator(links).inputValue(),'');assert.equal(await page.locator('#'+prefix+'RefFiles').isVisible(),true);
    }finally{await browser.close();}
  }
});

test('capability changes cannot silently drop already selected reference images from a text request',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests,errors,state}=await fixture(role,{allowedPlatforms:['dola','doubao']});try{
      state.capabilities.doubao={platform:'doubao',enabled:true,models:[{id:'dialog_seedance_2.0_fast',entry_mode:'ordinary_dialog',enabled:true,durations:[15],ratios:['16:9'],references:{images:{enabled:true,max_count:1,max_bytes:15728640}}}]};
      const prefix=role==='admin'?'gen':'member';await page.locator('#'+prefix+'Platform').selectOption('doubao');await page.locator('#'+prefix+'Model option[value="dialog_seedance_2.0_fast"]').waitFor({state:'attached'});
      await page.locator('#'+prefix+'RefFiles').setInputFiles({name:'cat.png',mimeType:'image/png',buffer:Buffer.from('image-fixture')});await page.locator('#'+prefix+'Prompt').fill('保留所选图片要求');
      state.capabilities.doubao.models[0].references.images={enabled:false};
      if(role==='admin')await page.evaluate(()=>window.dispatchEvent(new Event('doubao-capabilities-updated')));else await page.locator('#memberKey').dispatchEvent('change');
      await page.waitForFunction(id=>document.querySelector(id).disabled,'#'+prefix+'RefFiles');assert.equal(await page.locator('#'+prefix+'RefPreview img').isVisible(),true);
      await page.locator(role==='admin'?'#genBtn':'#memberSubmit').click();await page.locator('#'+prefix+'Status').filter({hasText:'尚不支持参考图片'}).waitFor();assert.equal(requests.some(r=>r.path.endsWith('/generate')||r.path.endsWith('/generations')),false);
      await page.getByRole('button',{name:'移除 cat.png',exact:true}).click();assert.equal(await page.locator('#'+prefix+'RefPreview img').count(),0);assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});

test('ordinary dialogue image selection applies model limits and keeps files when generation fails',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests,errors,state}=await fixture(role,{allowedPlatforms:['dola','doubao']});try{
      state.capabilities.doubao={platform:'doubao',enabled:true,models:[{id:'dialog_seedance_2.0_fast',entry_mode:'ordinary_dialog',enabled:true,durations:[15],ratios:['16:9'],references:{images:{enabled:true,validation_status:'experimental',local_only:true,max_count:1,max_bytes:15728640}}}]};
      const prefix=role==='admin'?'gen':'member';await page.locator('#'+prefix+'Platform').selectOption('doubao');await page.locator('#'+prefix+'Model option[value="dialog_seedance_2.0_fast"]').waitFor({state:'attached'});
      await page.locator('#'+prefix+'RefFiles').setInputFiles(['cat','dog'].map(name=>({name:name+'.png',mimeType:'image/png',buffer:Buffer.from('image-fixture')})));
      assert.equal(await page.locator('#'+prefix+'RefPreview img').count(),0);assert.match(await page.locator('#'+prefix+'RefError').innerText(),/最多 1 张/);
      await page.locator('#'+prefix+'RefFiles').setInputFiles({name:'cat.png',mimeType:'image/png',buffer:Buffer.from('image-fixture')});await page.locator('#'+prefix+'Prompt').fill('参考小猫跳舞');state.generateFailures=1;
      await page.locator(role==='admin'?'#genBtn':'#memberSubmit').click();await page.locator('#'+prefix+'Status').filter({hasText:'提交失败'}).waitFor();assert.equal(await page.locator('#'+prefix+'RefPreview img').count(),1);
      assert.equal(requests.filter(r=>r.path.endsWith('/reference-images')&&r.method==='POST').length,1);assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});
test('verified ordinary dialogue reference video reuses the existing uploader and preserves Doubao fifteen seconds',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests,errors,state}=await fixture(role,{allowedPlatforms:['dola','doubao']});try{
      state.capabilities.doubao={platform:'doubao',enabled:true,models:[{id:'dialog_seedance_2.0_fast',label:'Seedance 2.0 Fast（普通对话）',entry_mode:'ordinary_dialog',enabled:true,durations:[15],ratios:['16:9'],references:{images:{enabled:false},audio:{enabled:false},video:{enabled:true,max_count:1,max_bytes:52428800}}}]};
      const prefix=role==='admin'?'gen':'member';await page.locator('#'+prefix+'Platform').selectOption('doubao');await page.locator('#'+prefix+'Model option[value="dialog_seedance_2.0_fast"]').waitFor({state:'attached'});
      assert.equal(await page.locator('#'+prefix+'MediaFiles').isVisible(),true);assert.equal(await page.locator('#'+prefix+'MediaFiles').getAttribute('accept'),'.mp4,.mov,.webm');assert.doesNotMatch(await page.locator('#'+prefix+'PlatformNotice').innerText(),/仅支持纯文本|参考视频.*待.*验证/);
      await page.locator('#'+prefix+'MediaFiles').setInputFiles({name:'reference-cat.mp4',mimeType:'video/mp4',buffer:Buffer.from('video-fixture')});await page.locator('#'+prefix+'Prompt').fill('参考视频中的节奏制作原创猫咪跳舞');await page.locator(role==='admin'?'#genBtn':'#memberSubmit').click();await page.locator('#'+prefix+'Status').filter({hasText:'已提交'}).waitFor();
      const upload=requests.find(r=>r.path===(role==='admin'?'/api/admin/reference-media':'/api/me/reference-media')&&r.method==='POST');assert.ok(upload);assert.equal(upload.query,'?platform=doubao');assert.equal(upload.headers['x-file-name'],'reference-cat.mp4');assert.equal(upload.headers['content-type'],'video/mp4');assert.equal(upload.bytes,13);
      const body=requests.find(r=>r.path===(role==='admin'?'/api/admin/generate':'/v1/videos/generations')).body;assert.equal(body.platform,'doubao');assert.equal(body.model,'dialog_seedance_2.0_fast');assert.equal(body.duration,15);assert.equal(body.ratio,'16:9');assert.deepEqual(body.reference_media_uploads,['opaque-audio-token']);assert.equal(body.duration_profile,undefined);assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});
test('member platform and model changes clear the old submission banner and selected result association',async()=>{
  const {browser,page,errors,state}=await fixture('member',{allowedPlatforms:['dola','doubao']});try{
    state.capabilities.doubao={platform:'doubao',enabled:true,models:[{id:'dialog-a',entry_mode:'ordinary_dialog',durations:[15],ratios:['16:9']},{id:'dialog-b',entry_mode:'ordinary_dialog',durations:[15],ratios:['16:9']}]};
    state.taskRecords={all:[{id:'video_recent',platform:'dola',prompt:'最近历史任务',duration:10,status:'failed'},{id:'video_new',platform:'dola',prompt:'旧平台选中的任务',duration:10,status:'queued'}]};
    await page.locator('#memberPrompt').fill('原创猫咪');await page.locator('#memberSubmit').click();await page.locator('#memberStatus').filter({hasText:'已提交'}).waitFor();await page.locator('#memberResult').filter({hasText:'旧平台选中的任务'}).waitFor();
    await page.locator('#memberPlatform').selectOption('doubao');await page.locator('#memberModel option[value="dialog-a"]').waitFor({state:'attached'});assert.equal(await page.locator('#memberStatus').textContent(),'');assert.equal(await page.locator('#memberStatus').isVisible(),false);assert.match(await page.locator('#memberResult').innerText(),/最近历史任务/);
    await page.locator('#memberSubmit').click();await page.locator('#memberStatus').filter({hasText:'已提交'}).waitFor();await page.locator('#memberResult').filter({hasText:'旧平台选中的任务'}).waitFor();await page.locator('#memberModel').selectOption('dialog-b');assert.equal(await page.locator('#memberStatus').textContent(),'');assert.match(await page.locator('#memberResult').innerText(),/最近历史任务/);assert.equal(await page.locator('#memberPrompt').inputValue(),'原创猫咪');assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('switching platforms retains local files but never reuses the old platforms uploaded reference token',async()=>{
  const {browser,page,requests,errors,state}=await fixture('admin',{doubaoEnabled:true});try{
    await page.locator('#genPrompt').fill('原创小猫');await page.locator('#genRefFiles').setInputFiles({name:'cat.png',mimeType:'image/png',buffer:Buffer.from('fixture')});state.generateFailures=1;
    await page.locator('#genBtn').click();await page.locator('#genStatus').filter({hasText:'提交失败'}).waitFor();
    await page.locator('#genPlatform').selectOption('doubao');await page.locator('#genModel option[value="verified-doubao"]').waitFor({state:'attached'});await page.locator('#genBtn').click();await page.locator('#genStatus').filter({hasText:'已提交'}).waitFor();
    const uploaded=requests.filter(r=>r.path==='/api/admin/reference-images'&&r.method==='POST');assert.deepEqual(uploaded.map(r=>r.query),['','?platform=doubao']);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('pending reference upload stays on its captured platform and never submits generation after the form switches',async()=>{
  const {browser,page,requests,errors,state}=await fixture('admin',{doubaoEnabled:true});try{
    await page.locator('#genPrompt').fill('原创小猫');await page.locator('#genRefFiles').setInputFiles(['one','two'].map(name=>({name:name+'.png',mimeType:'image/png',buffer:Buffer.from('fixture')})));state.holdUpload=true;
    await page.locator('#genBtn').click();for(let i=0;i<100&&!state.releaseUpload;i++)await page.waitForTimeout(10);assert.ok(state.releaseUpload);
    await page.locator('#genPlatform').selectOption('doubao',{force:true});await page.locator('#genModel option[value="verified-doubao"]').waitFor({state:'attached'});state.holdUpload=false;await state.releaseUpload();await page.waitForFunction(()=>!document.querySelector('#genPlatform').disabled);
    assert.equal(requests.some(r=>r.path==='/api/admin/generate'),false);const uploaded=requests.filter(r=>r.path==='/api/admin/reference-images'&&r.method==='POST');assert.deepEqual(uploaded.map(r=>r.query),['','']);assert.equal(await page.locator('#genRefPreview img').count(),2);assert.equal(await page.locator('#genStatus').textContent(),'');assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('member cannot generate on a platform absent from selected Key permissions',async()=>{
  const {browser,page,requests,errors}=await fixture('member');try{
    await page.locator('#memberPlatform').selectOption('doubao');
    await page.locator('#keyNotice').filter({hasText:'密钥未获豆包权限'}).waitFor();
    assert.equal(await page.locator('#memberSubmit').isDisabled(),true);
    await page.locator('#memberPlatform').selectOption('dola');await page.locator('#memberDuration').selectOption('10');
    await page.locator('#memberPrompt').fill('普通十秒');await page.locator('#memberSubmit').click();
    await page.locator('#memberStatus').filter({hasText:'已提交'}).waitFor();
    const body=requests.find(r=>r.path==='/v1/videos/generations').body;
    assert.equal(body.platform,'dola');assert.equal(body.duration,10);assert.equal(body.duration_profile,undefined);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('verified Doubao 15 second submission never uses the Dola duration profile',async()=>{
  const {browser,page,requests,errors}=await fixture('member',{allowedPlatforms:['dola','doubao'],doubaoEnabled:true});try{
    await page.locator('#memberPlatform').selectOption('doubao');
    await page.locator('#memberModel option[value="verified-doubao"]').waitFor({state:'attached'});
    await page.locator('#memberPrompt').fill('豆包原文');await page.locator('#memberSubmit').click();
    await page.locator('#memberStatus').filter({hasText:'已提交'}).waitFor();
    const body=requests.find(r=>r.path==='/v1/videos/generations').body;
    assert.equal(body.platform,'doubao');assert.equal(body.model,'verified-doubao');assert.equal(body.duration,15);
    assert.equal(body.duration_profile,undefined);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('Doubao scan login uses its own endpoint, requires a proxy and cancels its job',async()=>{
  const {browser,page,requests,errors}=await fixture('admin');try{
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');
    await page.locator('#doubaoLogin').click();await page.locator('#dbConfirm').click();
    assert.match(await page.locator('#dbFormError').innerText(),/选择连接线路/);
    assert.equal(requests.some(r=>r.path.endsWith('/login')&&r.method==='POST'),false);
    await page.locator('#dbName').fill('扫码测试号');await page.locator('[data-doubao-manual-proxy]').click();await page.locator('#dbProxy').fill('http://fixture-user:fixture-pass@127.0.0.1:8888');
    await page.locator('#dbConfirm').click();await page.locator('#dbLoginStatus').filter({hasText:'请扫码'}).waitFor();
    assert.equal(await page.locator('#dbLoginQR').isVisible(),true);
    const body=requests.find(r=>r.path==='/api/admin/platforms/doubao/login'&&r.method==='POST').body;
    assert.equal(body.name,'扫码测试号');assert.match(body.proxy,/fixture-user/);
    await page.locator('[data-doubao-dialog-cancel]').click();
    assert.equal(requests.some(r=>r.path==='/api/admin/platforms/doubao/login/login_fixture'&&r.method==='DELETE'),true);
    assert.equal(requests.some(r=>r.path==='/api/admin/generate'),false);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('restoring an existing Doubao login reuses that account without a new-account form or another scan',async()=>{
  const {browser,page,requests,errors,state}=await fixture('admin');try{
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');await page.locator('[data-doubao-reauth="doubao-only"]').click();
    await page.locator('#dbLoginStatus').filter({hasText:'已有登录态'}).waitFor();assert.match(await page.locator('#dlg').innerText(),/doubao-only/);
    assert.equal(await page.locator('#dbName').count(),0);assert.equal(await page.locator('#dbProxyRef').count(),0);assert.equal(await page.locator('#dbLoginConfirm').isVisible(),false);
    const request=requests.find(r=>r.path==='/api/admin/platforms/doubao/accounts/doubao-only/login');assert.equal(request.method,'POST');assert.deepEqual(request.body,{mode:'browser'});
    state.loginConfirmed=true;await page.locator('#dbLoginStatus').filter({hasText:'账号已保存'}).waitFor();await page.locator('#doubaoAccountsTable').filter({hasText:'登录、线路正常'}).waitFor();
    assert.equal(await page.locator('#doubaoAccountsTable tbody tr').count(),1);assert.match(await page.locator('#doubaoAccountsTable').innerText(),/doubao-only/);
    assert.equal(requests.some(r=>r.path==='/api/admin/platforms/doubao/login'&&r.method==='POST'),false);assert.equal(requests.some(r=>r.path.endsWith('/confirm')&&r.method==='POST'),false);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('restoring login errors remain on the original account and never create a replacement',async()=>{
  const {browser,page,requests,errors,state}=await fixture('admin');try{
    state.reauthFailure=true;await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');await page.locator('[data-doubao-reauth="doubao-only"]').click();
    await page.locator('#dbLoginStatus').filter({hasText:'原账号正在使用'}).waitFor();assert.equal(await page.locator('#dbName').count(),0);assert.equal(requests.some(r=>r.path==='/api/admin/platforms/doubao/login'&&r.method==='POST'),false);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('closing or switching away from a pending restore cancels its late login job',async()=>{
  for(const switchPlatform of [false,true]){
    const {browser,page,requests,errors,state}=await fixture('admin');try{
      state.holdLogin=true;await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');await page.locator('[data-doubao-reauth="doubao-only"]').click();
      for(let i=0;i<100&&!state.releaseLogin;i++)await page.waitForTimeout(10);assert.ok(state.releaseLogin);
      if(switchPlatform)await page.locator('#accountPlatform').selectOption('dola');else await page.locator('[data-doubao-dialog-cancel]').click();
      await state.releaseLogin();for(let i=0;i<100&&!requests.some(r=>r.path.endsWith('/login/login_fixture')&&r.method==='DELETE');i++)await page.waitForTimeout(10);
      assert.equal(requests.filter(r=>r.path.endsWith('/login/login_fixture')&&r.method==='DELETE').length,1);assert.equal(await page.locator('#dlg').isVisible(),false);assert.equal(requests.some(r=>r.path==='/api/admin/platforms/doubao/login'&&r.method==='POST'),false);assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});
test('Doubao calibration uses observed native choices and starts only on explicit submission',async()=>{
  const {browser,page,requests,errors}=await fixture('admin');try{
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');
    await page.locator('[data-doubao-calibrate="doubao-only"]').click();
    await page.locator('#dbCalibrationModel option[value="native-model"]').waitFor({state:'attached'});
    assert.match(await page.locator('#dlg').innerText(),/实际生成.*可能消耗/);
    assert.equal(requests.some(r=>r.path.endsWith('/calibrate')),false);
    await page.locator('#dbCalibrationPrompt').fill('一只猫在窗台晒太阳');await page.locator('#dbCalibrationStart').click();
    await page.locator('#dbCalibrationStatus').filter({hasText:'验证完成'}).waitFor();
    const body=requests.find(r=>r.path.endsWith('/calibrate')).body;
    assert.deepEqual(body,{model:'native-model',duration:15,ratio:'9:16',prompt:'一只猫在窗台晒太阳'});
    assert.equal(await page.locator('#dbCalibrationCastReview').count(),0);
    assert.equal(body.duration_profile,undefined);assert.equal(requests.some(r=>r.path==='/api/admin/generate'),false);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('legacy skill diagnostics cannot create calibration options or submit a video',async()=>{
  const {browser,page,requests,errors}=await fixture('admin',{legacySkills:true});try{
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');await page.locator('[data-doubao-calibrate="doubao-only"]').click();
    await page.locator('#dbCalibrationModel').waitFor();assert.equal(await page.locator('#dbCalibrationStart').isDisabled(),true);
    assert.equal(await page.locator('#dbCalibrationModel option[value="skill-observed-25"]').count(),0);assert.doesNotMatch(await page.locator('#dlg').innerText(),/工作技能|Skills|云电脑/);
    assert.equal(requests.some(r=>r.path.endsWith('/calibrate')),false);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('Doubao ordinary calibration uses native options and uploads scoped references only when submitted',async()=>{
  const {browser,page,requests,errors}=await fixture('admin',{calibrationUnknown:true});try{
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');
    await page.locator('[data-doubao-calibrate="doubao-only"]').click();
    await page.locator('#dbCalibrationModel option[value="native-model"]').waitFor({state:'attached'});
    assert.match(await page.locator('#dlg').innerText(),/普通视频原生选项/);
    assert.equal(requests.some(r=>r.path.endsWith('/calibrate')),false);
    await page.locator('#dbCalibrationDuration').evaluate(select=>{select.add(new Option('31','31'));select.value='31';});await page.locator('#dbCalibrationPrompt').fill('原创虚拟人物在花园说话');
    await page.locator('#dbCalibrationStart').click();assert.equal(requests.some(r=>r.path.endsWith('/calibrate')),false);
    await page.locator('#dbCalibrationDuration').selectOption('15');await page.locator('#dbCalibrationRatio').selectOption('9:16');
    await page.locator('#dlg summary').filter({hasText:'可选功能实验'}).click();
    await page.locator('#dbCalibrationResolution').selectOption('720p');await page.locator('#dbCalibrationWatermark').check();
    await page.locator('#dbCalibrationImages').setInputFiles({name:'virtual.png',mimeType:'image/png',buffer:Buffer.from('image-fixture')});
    await page.locator('#dbCalibrationMedia').setInputFiles({name:'voice.mp3',mimeType:'audio/mpeg',buffer:Buffer.from('audio-fixture')});
    assert.equal(await page.locator('#dbCalibrationCast,#dbCalibrationCastLibrary').count(),0);
    assert.equal(requests.some(r=>r.path.endsWith('/reference-images')&&r.method==='POST'),false);
    await page.locator('#dbCalibrationStart').click();await page.locator('#dbCalibrationStatus').filter({hasText:'未重复提交'}).waitFor();
    const request=requests.find(r=>r.path.endsWith('/calibrate'));assert.deepEqual(request.body,{model:'native-model',duration:15,ratio:'9:16',prompt:'原创虚拟人物在花园说话',resolution:'720p',require_no_watermark:true,reference_uploads:['opaque-image-token'],reference_media_uploads:['opaque-audio-token']});
    assert.equal(requests.filter(r=>r.path.endsWith('/calibrate')).length,1);
    for(const upload of requests.filter(r=>r.method==='POST'&&/\/reference-(images|media)$/.test(r.path))){assert.equal(upload.query,'?platform=doubao');assert.ok(upload.bytes>0);}
    assert.equal(await page.locator('#dbCalibrationStart').isDisabled(),true);assert.deepEqual(errors,[]);
    assert.equal(await page.locator('#dbCalibrationCastReview').count(),0);
  }finally{await browser.close();}
});
test('Doubao advanced generation features appear only for verified capabilities and stay out of Dola requests',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests,errors}=await fixture(role,{allowedPlatforms:['dola','doubao'],doubaoEnabled:true,featureCaps:true});try{
      const prefix=role==='admin'?'gen':'member',submit=role==='admin'?'#genBtn':'#memberSubmit';
      await page.locator('#'+prefix+'Platform').selectOption('doubao');await page.locator('#'+prefix+'Model option[value="verified-doubao"]').waitFor({state:'attached'});
      await page.locator('#'+prefix+'Resolution').selectOption('1080p');await page.locator('#'+prefix+'NoWatermark').check();
      assert.equal(await page.locator('#'+prefix+'MediaFiles').getAttribute('accept'),'.mp3,.wav,.m4a,.aac,.flac,.ogg');
      assert.equal(await page.locator('#'+prefix+'Cast,#'+prefix+'CastLibrary').count(),0);
      const route=role==='admin'?'/api/admin/generate':'/v1/videos/generations';
      await page.locator('#'+prefix+'RefFiles').setInputFiles({name:'virtual.png',mimeType:'image/png',buffer:Buffer.from('image-fixture')});
      await page.locator('#'+prefix+'MediaFiles').setInputFiles({name:'voice.mp3',mimeType:'audio/mpeg',buffer:Buffer.from('audio-fixture')});
      await page.locator('#'+prefix+'Prompt').fill('一个原创虚拟角色');await page.locator(submit).click();
      await page.locator('#'+prefix+'Status').filter({hasText:'已提交'}).waitFor();
      const first=requests.find(r=>r.path===route).body;
      assert.equal(first.resolution,'1080p');assert.equal(first.require_no_watermark,true);assert.equal(first.cast,undefined);assert.equal(first.cast_ids,undefined);
      assert.deepEqual(first.reference_uploads,['opaque-image-token']);assert.deepEqual(first.reference_media_uploads,['opaque-audio-token']);
      await page.locator('#'+prefix+'Platform').selectOption('dola');assert.equal(await page.locator('#'+prefix+'Advanced').isHidden(),true);
      await page.locator(submit).click();for(let i=0;i<100&&requests.filter(r=>r.path===route).length<2;i++)await new Promise(resolve=>setTimeout(resolve,10));
      const second=requests.filter(r=>r.path===route)[1].body;assert.equal(second.resolution,undefined);assert.equal(second.require_no_watermark,undefined);assert.equal(second.cast,undefined);assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});
test('closing a Doubao calibration while a reference upload is pending never starts generation',async()=>{
  const {browser,page,requests,errors,state}=await fixture('admin');try{
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');await page.locator('[data-doubao-calibrate="doubao-only"]').click();
    await page.locator('#dbCalibrationModel option[value="native-model"]').waitFor({state:'attached'});await page.locator('#dbCalibrationPrompt').fill('一只猫');
    await page.locator('#dlg summary').filter({hasText:'可选功能实验'}).click();await page.locator('#dbCalibrationImages').setInputFiles({name:'cat.png',mimeType:'image/png',buffer:Buffer.from('fixture')});
    state.holdUpload=true;await page.locator('#dbCalibrationStart').click();for(let i=0;i<100&&!state.releaseUpload;i++)await new Promise(resolve=>setTimeout(resolve,10));assert.ok(state.releaseUpload);
    await page.locator('[data-doubao-dialog-cancel]').click();await state.releaseUpload();await page.waitForTimeout(100);
    assert.equal(requests.some(r=>r.path.endsWith('/calibrate')),false);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('Doubao ordinary calibration without observed native options stays closed to submission',async()=>{
  const {browser,page,requests,errors}=await fixture('admin',{missingNativeOptions:true});try{
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');await page.locator('[data-doubao-calibrate="doubao-only"]').click();
    await page.locator('#dbCalibrationStatus').filter({hasText:'原生选项尚未完整捕获'}).waitFor();assert.equal(await page.locator('#dbCalibrationStart').isDisabled(),true);
    assert.equal(requests.some(r=>r.path.endsWith('/calibrate')),false);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('removed character library has no account entry generation picker or background requests',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,requests,errors}=await fixture(role,{allowedPlatforms:['dola','doubao'],doubaoEnabled:true,featureCaps:true});try{
      const prefix=role==='admin'?'gen':'member';
      await page.locator('#'+prefix+'Platform').selectOption('doubao');await page.locator('#'+prefix+'Model option[value="verified-doubao"]').waitFor({state:'attached'});
      assert.equal(await page.locator('#'+prefix+'Cast,#'+prefix+'CastLibrary,[data-feature="cast"]').count(),0);
      if(role==='admin'){
        await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');await page.locator('#doubaoAccountsTable').filter({hasText:'doubao-only'}).waitFor();
        assert.equal(await page.getByRole('button',{name:'角色素材库',exact:true}).count(),0);
        await page.locator('#accountPlatform').selectOption('dola');assert.equal(await page.getByRole('button',{name:'角色素材库',exact:true}).count(),0);
      }
      assert.equal(requests.some(r=>/\/cast(?:\/|$)|\/cast-review$|doubao-cast\.js/.test(r.path)),false);assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});

test('calibration preserves lead prompt one-time confirmation and completed video without a character library or effect review',async()=>{
  const {browser,page,requests,errors}=await fixture('admin');try{
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');await page.locator('[data-doubao-calibrate="doubao-only"]').click();
    await page.locator('#dbCalibrationModel option[value="native-model"]').waitFor({state:'attached'});
    assert.equal(await page.locator('#dbCalibrationCast,#dbCalibrationCastLibrary').count(),0);
    await page.locator('#dbCalibrationPrompt').fill('原创虚拟小猫在花园走路');await page.locator('#dbCalibrationLead').fill('使用原创虚拟素材制作视频。');
    assert.equal(await page.locator('#dbCalibrationAutoConfirm').isChecked(),false);await page.locator('#dbCalibrationAutoConfirm').check();
    await page.locator('#dbCalibrationStart').click();await page.locator('#dbCalibrationStatus').filter({hasText:'验证完成'}).waitFor();
    const body=requests.find(r=>r.path.endsWith('/calibrate')).body;
    assert.deepEqual(body,{model:'native-model',duration:15,ratio:'9:16',prompt:'原创虚拟小猫在花园走路',lead_prompt:'使用原创虚拟素材制作视频。',auto_confirm:true});
    assert.match(await page.locator('#dbCalibrationOutcome a').getAttribute('href'),/\/videos\/video_a{32}\.mp4$/);assert.equal(await page.locator('#dbCalibrationOutcome video').count(),1);
    assert.equal(await page.locator('#dbCalibrationCastReview,#dbCalibrationReviewNotice').count(),0);
    assert.equal(requests.some(r=>/\/cast(?:\/|$)|\/cast-review$/.test(r.path)),false);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

test('unverified Doubao optional features remain hidden and cannot leak old choices',async()=>{
  const {browser,page,errors}=await fixture('admin',{doubaoEnabled:true});try{
    await page.locator('#genPlatform').selectOption('doubao');await page.locator('#genModel option[value="verified-doubao"]').waitFor({state:'attached'});
    assert.equal(await page.locator('#genAdvanced').count(),1);assert.equal(await page.locator('#genAdvanced').isHidden(),true);assert.equal(await page.locator('#genMediaFiles').locator('xpath=ancestor::*[contains(@class,"field")][1]').isHidden(),true);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('late Doubao capability responses cannot override the returned Dola form',async()=>{
  for(const role of ['admin','member']){
    const {browser,page,state,errors}=await fixture(role,{allowedPlatforms:['dola','doubao'],doubaoEnabled:true});try{
      const prefix=role==='admin'?'gen':'member';state.holdCaps=true;
      await page.locator('#'+prefix+'Platform').selectOption('doubao');
      for(let i=0;i<100&&!state.releaseCaps;i++)await new Promise(resolve=>setTimeout(resolve,10));
      assert.ok(state.releaseCaps,'Doubao request reached HTTP boundary');
      await page.locator('#'+prefix+'Platform').selectOption('dola');await state.releaseCaps();
      assert.equal(await page.locator('#'+prefix+'Model').inputValue(),'seedance-2.0');
      assert.equal(await page.locator('#'+prefix+'Model option[value="verified-doubao"]').count(),0);
      assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});
test('cancelled login stays closed when its initial server response arrives late',async()=>{
  const {browser,page,requests,errors,state}=await fixture('admin');try{
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');
    await page.locator('#doubaoLogin').click();await page.locator('#dbName').fill('取消测试');await page.locator('[data-doubao-manual-proxy]').click();await page.locator('#dbProxy').fill('http://fixture:secret@127.0.0.1:8888');
    state.holdLogin=true;await page.locator('#dbConfirm').click();
    for(let i=0;i<100&&!state.releaseLogin;i++)await new Promise(resolve=>setTimeout(resolve,10));
    assert.ok(state.releaseLogin);await page.locator('[data-doubao-dialog-cancel]').click();await state.releaseLogin();
    await new Promise(resolve=>setTimeout(resolve,30));
    assert.equal(await page.locator('#dlg').isHidden(),true);
    assert.equal(requests.some(r=>r.path==='/api/admin/platforms/doubao/login/login_fixture'&&r.method==='DELETE'),true);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('scan login can explicitly choose a configured ISP without copying credentials',async()=>{
  const {browser,page,requests,errors}=await fixture('admin');try{
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');
    await page.locator('#doubaoLogin').click();await page.locator('#dbProxyRef option[value="uk-fixture"]').waitFor({state:'attached'});
    assert.equal(await page.locator('#dbProxyRef').inputValue(),'');
    await page.locator('#dbName').fill('选择线路测试');await page.locator('#dbProxyRef').selectOption('uk-fixture');
    await page.locator('#dbConfirm').click();await page.locator('#dbLoginStatus').filter({hasText:'请扫码'}).waitFor();
    const body=requests.find(r=>r.path==='/api/admin/platforms/doubao/login'&&r.method==='POST').body;
    assert.equal(body.proxy_ref,'uk-fixture');assert.equal(body.proxy,undefined);
    assert.equal(await page.locator('#dlg').innerText().then(t=>t.includes('fixture-pass')),false);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('Doubao dedicated Hong Kong proxy is an explicit selection with no global proxy change',async()=>{
  const {browser,page,requests,errors}=await fixture('admin');try{
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');
    await page.locator('#doubaoLogin').click();await page.locator('#dbProxyRef option[value="local:hk"]').waitFor({state:'attached'});
    assert.match(await page.locator('#dlg').innerText(),/选择连接线路/);
    assert.equal(await page.locator('#dbProxyRef').inputValue(),'');
    await page.locator('#dbName').fill('香港专用');await page.locator('#dbProxyRef').selectOption('local:hk');
    await page.locator('#dbConfirm').click();await page.locator('#dbLoginStatus').filter({hasText:'请扫码'}).waitFor();
    const body=requests.find(r=>r.path==='/api/admin/platforms/doubao/login'&&r.method==='POST').body;
    assert.equal(body.proxy_ref,'local:hk');assert.equal(body.proxy,undefined);
    assert.equal(requests.some(r=>r.path.startsWith('/api/admin/proxies')&&r.method!=='GET'),false);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('Doubao local direct login is explicit and does not change Dola or Clash settings',async()=>{
  const {browser,page,requests,errors}=await fixture('admin');try{
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');
    await page.locator('#doubaoLogin').click();await page.locator('#dbProxyRef option[value="local:direct"]').waitFor({state:'attached'});
    assert.equal(await page.locator('#dbProxyRef').inputValue(),'');
    assert.match(await page.locator('#dlg').innerText(),/选择连接线路/);
    await page.locator('#dbName').fill('明确直连');await page.locator('#dbProxyRef').selectOption('local:direct');
    await page.locator('#dbConfirm').click();await page.locator('#dbLoginStatus').filter({hasText:'请扫码'}).waitFor();
    const body=requests.find(r=>r.path==='/api/admin/platforms/doubao/login'&&r.method==='POST').body;
    assert.equal(body.proxy_ref,'local:direct');assert.equal(body.proxy,undefined);
    assert.equal(requests.some(r=>r.path.startsWith('/api/admin/proxies')&&r.method!=='GET'),false);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('an existing Doubao account can explicitly switch its connection to local direct',async()=>{
  const {browser,page,requests,errors}=await fixture('admin');try{
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');
    await page.locator('[data-doubao-proxy="doubao-only"]').click();
    await page.locator('#dbNewProxyRef option[value="local:direct"]').waitFor({state:'attached'});
    assert.equal(await page.locator('#dbNewProxyRef').inputValue(),'');
    await page.locator('#dbNewProxyRef').selectOption('local:direct');await page.locator('#dbProxySave').click();
    for(let i=0;i<100&&!requests.some(r=>r.path.endsWith('/accounts/doubao-only/proxy')&&r.method==='PATCH');i++)await new Promise(resolve=>setTimeout(resolve,10));
    assert.deepEqual(requests.find(r=>r.path.endsWith('/accounts/doubao-only/proxy')&&r.method==='PATCH').body,{proxy_ref:'local:direct'});
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('switching a Doubao route explains the retained profile and refreshes its region environment from the server',async()=>{
  const {browser,page,requests,errors,state}=await fixture('admin');try{
    state.accounts=[{name:'doubao-only',enabled:true,proxy_label:'本机直连',route_region:'本机网络',browser_environment:{status:'matched',region:'本机网络',locale:'zh-CN',timezone_id:'Asia/Shanghai',viewport:{width:1365,height:900},hardware_mode:'native',isolated:true}}];
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');
    const table=page.locator('#doubaoAccountsTable');await table.filter({hasText:'地区环境：本机网络'}).waitFor();assert.equal(await table.locator('thead th').count(),6);
    await page.locator('[data-doubao-proxy="doubao-only"]').click();await page.locator('#dbNewProxyRef option[value="local:hk"]').waitFor({state:'attached'});
    const hint=await page.locator('#dlg').innerText();assert.match(hint,/同步地区环境/);assert.match(hint,/保留.*账号资料/);assert.match(hint,/重新检查环境/);
    state.proxyAccountUpdate={name:'doubao-only',enabled:true,status:'unverified',proxy_label:'香港代理',route_region:'香港',browser_environment:{status:'matched',region:'香港',locale:'zh-HK',timezone_id:'Asia/Hong_Kong',viewport:{width:1365,height:900},hardware_mode:'native',isolated:true}};
    await page.locator('#dbNewProxyRef').selectOption('local:hk');await page.locator('#dbProxySave').click();await table.filter({hasText:'地区环境：香港'}).waitFor();
    const text=await table.innerText();assert.match(text,/zh-HK/);assert.match(text,/Asia\/Hong_Kong/);assert.match(text,/固定视窗 1365×900/);assert.doesNotMatch(text,/Asia\/Shanghai/);assert.match(text,/待检查/);
    assert.deepEqual(requests.find(r=>r.path.endsWith('/accounts/doubao-only/proxy')&&r.method==='PATCH').body,{proxy_ref:'local:hk'});assert.equal(requests.some(r=>r.path.startsWith('/api/admin/proxies')&&r.method!=='GET'),false);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('direct environment checks explain that no proxy authentication is used',async()=>{
  for(const directVerify of [true,false]){
    const {browser,page,errors}=await fixture('admin',{directVerify});try{
      await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');
      await page.locator('[data-doubao-check="doubao-only"]').click();
      await page.locator('#detailDrawer').filter({hasText:'豆包账号环境检查'}).waitFor();
      const text=await page.locator('#detailDrawer').innerText();
      if(directVerify){assert.match(text,/连接方式/);assert.match(text,/本机直连（未使用代理）/);assert.doesNotMatch(text,/代理认证/);}
      else{assert.match(text,/代理认证/);assert.doesNotMatch(text,/本机直连（未使用代理）/);}
      assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});
test('interactive browser login defaults only its own form to direct and supports confirm retry without a QR image',async()=>{
  const {browser,page,requests,errors,state}=await fixture('admin');try{
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');
    await page.locator('#doubaoBrowserLogin').click();await page.locator('#dbProxyRef option[value="local:direct"]').waitFor({state:'attached'});
    assert.equal(await page.locator('#dbProxyRef').inputValue(),'local:direct');
    assert.match(await page.locator('#dlg').innerText(),/浏览器在运行项目的电脑上打开/);
    assert.match(await page.locator('#dlg').innerText(),/远程网页不会在访客电脑打开/);
    await page.locator('#dbName').fill('手动登录');await page.locator('#dbConfirm').click();
    await page.locator('#dbLoginStatus').filter({hasText:'请在已打开的豆包浏览器手动登录'}).waitFor();
    assert.equal(await page.locator('#dbLoginQR').isVisible(),false);
    assert.equal(await page.locator('#dbLoginConfirm').isEnabled(),true);
    const body=requests.find(r=>r.path==='/api/admin/platforms/doubao/login'&&r.method==='POST').body;
    assert.equal(body.mode,'browser');assert.equal(body.proxy_ref,'local:direct');
    state.loginConfirmFailures=1;await page.locator('#dbLoginConfirm').click();
    await page.locator('#dbLoginStatus').filter({hasText:'尚未发现已登录凭据'}).waitFor();
    assert.equal(await page.locator('#dbLoginConfirm').isEnabled(),true);
    assert.equal(requests.some(r=>r.method==='DELETE'),false);
    await page.locator('#dbLoginConfirm').click();await page.locator('#dbLoginStatus').filter({hasText:'账号已保存'}).waitFor();
    assert.match(await page.locator('#dbLoginStatus').innerText(),/检查/);
    assert.equal(await page.locator('#dbLoginConfirm').isVisible(),false);
    assert.equal(requests.filter(r=>r.path.endsWith('/confirm')).length,2);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('closing the interactive browser cannot be mistaken for a saved account',async()=>{
  const {browser,page,requests,errors,state}=await fixture('admin');try{
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');
    await page.locator('#doubaoBrowserLogin').click();await page.locator('#dbProxyRef option[value="local:direct"]').waitFor({state:'attached'});
    await page.locator('#dbName').fill('未保存');await page.locator('#dbConfirm').click();
    await page.locator('#dbLoginStatus').filter({hasText:'请在已打开的豆包浏览器手动登录'}).waitFor();
    state.browserClosed=true;await page.locator('#dbLoginStatus').filter({hasText:'登录浏览器已关闭，未保存账号'}).waitFor();
    assert.equal(await page.locator('#dbLoginConfirm').isVisible(),false);
    assert.equal(requests.some(r=>r.path.endsWith('/confirm')),false);
    assert.doesNotMatch(await page.locator('#dbLoginStatus').innerText(),/账号已保存/);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
test('a valid browser-login deep link only resumes its existing job with GET and never confirms automatically',async()=>{
  const job='b'.repeat(32),{browser,page,requests,errors}=await fixture('admin',{resumeLoginId:job});try{
    await page.locator('#dbLoginStatus').filter({hasText:'请在已打开的豆包浏览器手动登录'}).waitFor();
    assert.equal(await page.locator('#accountPlatform').inputValue(),'doubao');
    assert.equal(await page.locator('#doubaoAccountPane').isVisible(),true);
    assert.equal(await page.locator('#dbLoginConfirm').isEnabled(),true);
    assert.equal(requests.some(r=>r.path==='/api/admin/platforms/doubao/login/'+job&&r.method==='GET'),true);
    assert.equal(requests.some(r=>r.method!=='GET'),false);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

test('automatic browser login explains extraction, needs no confirm and refreshes the saved pool',async()=>{
  const {browser,page,requests,errors,state}=await fixture('admin',{autoCapture:true});try{
    await page.locator('[data-tab="accounts"]').click();await page.locator('#accountPlatform').selectOption('doubao');
    await page.locator('#doubaoBrowserLogin').click();await page.locator('#dbProxyRef option[value="local:direct"]').waitFor({state:'attached'});
    assert.match(await page.locator('#dlg').innerText(),/登录成功后自动提取 CK 并加入豆包号池/);
    await page.locator('#dbName').fill('自动登录');await page.locator('#dbConfirm').click();
    await page.locator('#dbLoginStatus').filter({hasText:'请在官方页面扫码'}).waitFor();
    assert.equal(await page.locator('#dbLoginConfirm').isVisible(),false);
    assert.equal(await page.locator('#dbLoginQR').isVisible(),false);
    state.loginConfirmed=true;
    await page.locator('#dbLoginStatus').filter({hasText:'账号已保存，待环境验证'}).waitFor();
    await page.locator('#doubaoAccountsTable').filter({hasText:'扫码已保存'}).waitFor();
    assert.equal(requests.some(r=>r.path.endsWith('/confirm')),false);
    assert.equal(requests.filter(r=>r.path==='/api/admin/platforms/doubao/login'&&r.method==='POST').length,1);
    assert.equal(requests.some(r=>r.method==='DELETE'),false);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

test('automatic login deep links preserve backend route errors and capturing progress without confirmation writes',async()=>{
  const job='c'.repeat(32),{browser,page,requests,errors,state}=await fixture('admin',{resumeLoginId:job,autoCapture:true});try{
    await page.locator('#dbLoginStatus').filter({hasText:'请在官方页面扫码'}).waitFor();
    assert.equal(await page.locator('#dbLoginConfirm').isVisible(),false);
    state.loginMessage='线路验证失败，请检查本机网络后重试；浏览器保持打开';
    await page.locator('#dbLoginStatus').filter({hasText:'线路验证失败'}).waitFor();
    assert.match(await page.locator('#dlg').innerText(),/登录成功后自动提取 CK 并加入豆包号池/);
    assert.equal(await page.locator('#dbLoginConfirm').isVisible(),false);
    state.loginState='capturing';state.loginMessage='已检测到登录，正在提取 CK 并保存账号';
    await page.locator('#dbLoginStatus').filter({hasText:'正在提取 CK 并保存账号'}).waitFor();
    assert.equal(await page.locator('#dbLoginConfirm').isVisible(),false);
    assert.equal(requests.some(r=>r.method!=='GET'),false);assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});

test('automatic browser terminal failures stop polling and cannot claim a saved account',async()=>{
  for(const terminal of ['failed','expired','cancelled','closed']){
    const job='d'.repeat(32),{browser,page,requests,errors,state}=await fixture('admin',{resumeLoginId:job,autoCapture:true});try{
      await page.locator('#dbLoginStatus').filter({hasText:'请在官方页面扫码'}).waitFor();
      state.loginState=terminal;state.loginMessage='登录未完成：'+terminal;
      await page.locator('#dbLoginStatus').filter({hasText:state.loginMessage}).waitFor();
      const reads=requests.filter(r=>r.path==='/api/admin/platforms/doubao/login/'+job).length;
      await new Promise(resolve=>setTimeout(resolve,1900));
      assert.equal(requests.filter(r=>r.path==='/api/admin/platforms/doubao/login/'+job).length,reads);
      assert.equal(await page.locator('#dbLoginConfirm').isVisible(),false);
      assert.doesNotMatch(await page.locator('#dbLoginStatus').innerText(),/账号已保存/);
      assert.equal(await page.locator('#doubaoAccountsTable').innerText().then(t=>t.includes('扫码已保存')),false);
      assert.equal(requests.some(r=>r.method!=='GET'),false);assert.deepEqual(errors,[]);
    }finally{await browser.close();}
  }
});
