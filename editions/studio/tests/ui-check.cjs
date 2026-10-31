// Local UI integration fixture. All requests are intercepted; no real account or generation is touched.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
let playwright;
try { playwright = require('playwright'); } catch { playwright = require(process.argv[2]); }
const root = path.resolve(__dirname, '..');
const output = path.join(root, '.impeccable', 'review');
fs.mkdirSync(output, { recursive: true });
const now = Math.floor(Date.now() / 1000);
const accounts = Array.from({ length: 302 }, (_, i) => ({name:'acc'+(i+1),email:i % 7 ? `account${i+1}@example.test` : '',login_ok:i % 101 === 0 ? 0 : 1,scheduling:true,busy:i===6,rate_limited:i===7,rate_limited_until:now+600,quota_blocked:i===8,cooling:false,used_today:i%4,limit:2,completed_total:i,last_used_at:i%3?now-60*i:0,created_at:now-86400}));
Object.assign(accounts[2],{verification_required:'birthday',scheduling:false});
const adminTasks = Array.from({ length: 50 }, (_, i) => ({id:'video_'+String(i+1).padStart(12,'0'),prompt:i===0?'一只金毛在雪地里打滚':i===1?'很长的提示词：'+('橘猫在窗台晒太阳，镜头缓缓移动。'.repeat(20)):'视频提示词 '+(i+1),model:'seedance-2.5',duration:10,account:accounts[i%accounts.length].name,api_key_name:'示例客户',status:['failed','processing','completed','queued'][i%4],created_at:now-120*i,error:i%4===0?'503: 提交阶段失败，疑似代理出口；account=acc1 proxy=192.0.2.20':'',video_url:i%4===2?'/videos/fixture.webm':null}));
const quota = i => ({key:'fixture-proxy-'+i,limit:1000000000,used:26000,remaining:999974000,warn:false,paused:false,exhausted:false,last_run_at:now-600,last_run_cost:61,last_run_account:'acc1'});
const groups = [0,1].map(i => ({server:'http://192.0.2.'+(20+i)+':11000',own:true,count:151,accounts:accounts.slice(i*151,(i+1)*151).map(a=>a.name),quota:quota(i)}));
const proxyData={configured:302,total:302,independent:0,shared_groups:2,missing:0,groups,global_proxy:{server:'http://127.0.0.1:7897'},summary:{used:52000,limit:2000000000,available:2,count:2,warn:0,paused:0,exhausted:0},accounts:accounts.map((a,i)=>({...a,host:'192.0.2.'+(i<151?20:21),port:11000,scheme:'http',own:true,source:'file',quota:quota(i<151?0:1)}))};
const normalKeys=[{key:'sk-ui-fixture-primary-not-real',name:'创作密钥',enabled:true,daily_limit:100,concurrency_limit:2,allowed_durations:[10,15],expires_at:0,last_used_at:now,usage:{total:3,active:1,completed:2}},{key:'sk-ui-fixture-secondary-not-real',name:'第二密钥',enabled:true,daily_limit:5,concurrency_limit:1,allowed_durations:[30],expires_at:0,usage:{total:1,active:0,completed:1}}];
let memberTasks=adminTasks.slice(0,6).map(t=>({id:t.id,model:t.model,prompt:t.prompt,duration:t.duration,status:t.status,created_at:t.created_at,video_url:t.video_url,error:t.error?'这条视频任务未能完成。请联系管理员并提供任务 ID。':null}));
let scenario='normal',failStats=false,failSubmit=false,failAccounts=false,failCKImport=false,requests=[],assertions=0;
const screenshots=[];
const referenceImage={name:'本地参考.png',mimeType:'image/png',buffer:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j8WQAAAAASUVORK5CYII=','base64')};
const check=(condition,message)=>{assert.ok(condition,message);assertions++;};
async function until(fn,message){for(let i=0;i<80;i++){if(await fn())return;await new Promise(r=>setTimeout(r,50));}throw new Error(message);}
async function run(){
 const browser=await playwright.chromium.launch({headless:true,executablePath:process.argv[3]||undefined});
 try{
  const context=await browser.newContext({viewport:{width:1440,height:1000},locale:'zh-CN',reducedMotion:'reduce'});
  await context.route('**/*',async route=>{
   const request=route.request(),url=new URL(request.url()),p=url.pathname;
   requests.push({path:p,method:request.method(),authorization:request.headers().authorization,contentType:request.headers()['content-type'],body:request.postData()});
   const json=(data,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
   if(p==='/api/auth/status')return json({needs_setup:false,registration_open:false,admin_key_mode:false});
   if(p==='/api/auth/login'){const body=JSON.parse(request.postData());const admin=body.email.startsWith('admin');return json({token:admin?'admin-session':'member-session',user:{email:body.email,role:admin?'admin':'member'}});}
   if(p==='/api/auth/logout')return json({ok:true});
   if(p==='/api/me'){
    if(!['Bearer admin-session','Bearer member-session'].includes(request.headers().authorization))return json({detail:'请先登录'},401);
    let keys=structuredClone(normalKeys);
    if(scenario==='no-key')keys=[];
    if(scenario==='expired')keys=[{...keys[0],expires_at:now-60}];
    if(scenario==='disabled')keys=[{...keys[0],enabled:false}];
    if(scenario==='quota')keys=[{...keys[0],daily_limit:3}];
    return json({user:{id:2,email:'member@example.test',role:request.headers().authorization==='Bearer admin-session'?'admin':'member'},keys,usage:{total:4,completed:3,failed:1,active:1,queued:0},api_base:'https://example.test',supported_durations:[5,10,15,30],supported_durations_by_model:{'seedance-1.0':[5,10,15],'seedance-2.0':[5,10,15,30],'seedance-2.5':[5,10,15,30]}});
   }
   if(p==='/api/me/tasks')return json({tasks:scenario==='no-key'?[]:memberTasks.slice(0,Number(url.searchParams.get('limit')||50))});
   if(p==='/api/me/reference-images/config')return json({max_count:30,max_bytes:15*1024*1024});
   if(p==='/api/me/reference-images')return json({id:'b'.repeat(32)+'.png',size:request.postDataBuffer().length,width:1,height:1},201);
   if(p==='/v1/videos/generations'){
    await new Promise(r=>setTimeout(r,60));
    if(failSubmit)return json({detail:'今日额度不足，请明天再试'},429);
    const body=JSON.parse(request.postData()),task={id:'video_member_created',status:'queued',model:body.model,prompt:body.prompt,duration:body.duration,created_at:now,error:null,video_url:null};memberTasks=[task,...memberTasks];return json(task);
   }
   if(p.startsWith('/api/admin/')){
    if(request.headers().authorization==='Bearer member-session')return json({detail:'invalid admin credentials'},401);
    if(p==='/api/admin/reference-images/config')return json({max_count:30,max_bytes:15*1024*1024});
    if(p==='/api/admin/reference-images')return json({id:'a'.repeat(32)+'.png',size:request.postDataBuffer().length,width:1,height:1},201);
    if(p==='/api/admin/accounts/import-ck')return failCKImport?json({detail:'名称已存在，请换一个名称。'},409):json({ok:true,name:JSON.parse(request.postData()).name,cookie_count:2,status:'unverified'},201);
    if(p==='/api/admin/accounts'){await new Promise(r=>setTimeout(r,60));return failAccounts?json({detail:'账号信息暂不可用'},503):json({accounts});}
    if(p==='/api/admin/accounts/delete-invalid/preview')return json({ok:true,token:'cleanup-fixture',accounts:accounts.filter(a=>a.login_ok===0).map(a=>a.name),skipped:[]});
    if(p==='/api/admin/accounts/delete-invalid'){
      if(url.searchParams.get('token')!=='cleanup-fixture')return json({detail:'missing preview token'},400);
      const deleted=accounts.filter(a=>a.login_ok===0).map(a=>a.name);
      for(let i=accounts.length-1;i>=0;i--)if(deleted.includes(accounts[i].name))accounts.splice(i,1);
      return json({ok:true,deleted,skipped:[]});
    }
    if(p==='/api/admin/jobs')return json({jobs:{}});
    if(p==='/api/admin/proxies')return json(proxyData);
    if(p==='/api/admin/tasks')return json({tasks:adminTasks.slice(0,Number(url.searchParams.get('limit')||50))});
    if(request.method()==='GET'&&/^\/api\/admin\/tasks\/[^/]+$/.test(p)){
      const id=decodeURIComponent(p.slice('/api/admin/tasks/'.length)),task=adminTasks.find(task=>task.id===id);
      return task?json({task,events:[]}):json({detail:'任务不存在'},404);
    }
    if(p==='/api/admin/stats')return failStats?json({detail:'服务暂不可用'},503):json({today_completed:9,today_failed:47,success_rate:9/56,available_accounts:299,total_accounts:302,total_remaining:591,per_day:Array.from({length:7},(_,i)=>({day:'10-'+String(i+1).padStart(2,'0'),completed:i===6?9:i===5?8:0,failed:i===6?47:i===5?42:0})),per_account:accounts});
    if(p==='/api/admin/keys')return json({keys:normalKeys.map((k,i)=>({...k,owner_user_id:2,today_total:k.usage.total,today_active:k.usage.active,last_used_at:now})),env_keys:0});
    if(p==='/api/admin/users')return json({users:[{id:1,email:'admin@example.test',role:'admin',status:'active',keys:[],usage:{total:0,completed:0},last_login_at:now},{id:2,email:'member@example.test',role:'member',status:'active',keys:normalKeys,usage:{total:4,completed:3},last_login_at:now}]});
    if(p==='/api/admin/generate')return json({id:'video_admin_created',status:'queued'});
    return json({ok:true});
   }
   if(p.startsWith('/videos/'))return route.fulfill({status:200,contentType:'video/webm',body:Buffer.alloc(0)});
   let name=p==='/admin'||p==='/'?'index.html':p==='/member'?'member.html':p==='/login'?'login.html':p.slice(1);
   if(!/^[a-zA-Z0-9._-]+$/.test(name))return route.fulfill({status:404,body:'not found'});
   const file=path.join(root,'web',name);
   if(!fs.existsSync(file))return route.fulfill({status:404,body:'not found'});
   const type=name.endsWith('.css')?'text/css':name.endsWith('.js')?'text/javascript':'text/html';
   return route.fulfill({status:200,contentType:type+'; charset=utf-8',body:fs.readFileSync(file)});
  });
  const page=await context.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  async function capture(name,width=1440,height=1000){await page.setViewportSize({width,height});await page.evaluate(async()=>{await document.fonts.ready;scrollTo(0,0);await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));});await page.screenshot({path:path.join(output,name+'.png'),fullPage:true,animations:'disabled'});const size=await page.evaluate(()=>({width:innerWidth,body:document.documentElement.scrollWidth,title:document.querySelector('h1')?.textContent}));check(size.body<=size.width+1,name+' body overflow '+JSON.stringify(size));if(await page.locator('#sideToggle').count())check(await page.locator('#sideToggle').isVisible()===(width<=767),name+' navigation breakpoint');screenshots.push({name,...size});}
  if(!process.env.MEMBER_MEDIA_ONLY){
  await page.goto('https://dola-ui.test/login');await page.evaluate(()=>localStorage.setItem('dola_token','admin-session'));
  await page.goto('https://dola-ui.test/admin#tasks');await until(()=>page.locator('#tasksTable tbody tr').count().then(n=>n===50),'direct tasks missing');await page.selectOption('#taskState','failed');
  const taskOpener=page.locator('#tasksTable [data-task-detail]').first(),taskId=await taskOpener.getAttribute('data-task-detail');await taskOpener.click();failAccounts=true;await page.locator('#detailDrawer [data-account-detail]').click();await until(()=>page.locator('#detailDrawer').innerText().then(s=>s.includes('加载失败')),'associated account error missing');failAccounts=false;await page.locator('#detailDrawer [data-account-detail]').click();await until(()=>page.locator('#detailDrawer').innerText().then(s=>s.includes('今日已用')),'associated account missing');check((await page.locator('#detailDrawer').innerText()).includes('192.0.2.20:11000'),'direct task loads correct proxy');check(await page.locator('#taskState').inputValue()==='failed','task filter retained through drawer chain');await page.keyboard.press('Escape');check(await page.evaluate(id=>document.activeElement.dataset.taskDetail===id,taskId),'drawer chain returns to original task opener');
  await taskOpener.click();await page.locator('#detailDrawer [data-proxy-settings]').click();await until(()=>page.locator('#dlg').isVisible(),'associated proxy settings missing');check(await page.locator('#p_host').inputValue()==='192.0.2.20','correct associated proxy host');check(await page.locator('#p_port').inputValue()==='11000','correct associated proxy port');await page.keyboard.press('Escape');
  await taskOpener.focus();await page.evaluate(()=>window.fixtureFocused=document.activeElement);adminTasks[0].prompt+=' 更新焦点测试';await page.evaluate(()=>refresh({background:true}),undefined,undefined,false);await until(()=>page.locator('#tasksTable').innerText().then(s=>s.includes('更新焦点测试')),'admin changed task refresh missing');check(await page.evaluate(()=>document.activeElement===window.fixtureFocused),'admin task button identity survives changed refresh');check((await page.locator('#tasksTable').innerText()).includes('更新焦点测试'),'admin focused task data still updates');
  await page.setViewportSize({width:375,height:812});await page.locator('#tasksTable .table-scroll').focus();await page.evaluate(()=>{window.fixtureFocused=document.activeElement;document.activeElement.scrollLeft=160;});adminTasks[0].prompt+=' 第二次更新';await page.evaluate(()=>refresh({background:true}),undefined,undefined,false);await until(()=>page.locator('#tasksTable').innerText().then(s=>s.includes('第二次更新')),'admin second task refresh missing');check(await page.evaluate(()=>document.activeElement===window.fixtureFocused),'admin table region identity survives refresh');check(await page.locator('#tasksTable .table-scroll').evaluate(n=>n.scrollLeft===160),'admin table scroll survives refresh');adminTasks[0].prompt='一只金毛在雪地里打滚';await page.setViewportSize({width:1440,height:1000});
  await page.goto('https://dola-ui.test/admin');await until(()=>page.locator('#statCards').innerText().then(s=>s.includes('299')),'dashboard data missing');
  check((await page.locator('#dashAttention').innerText()).includes('47'),'failure banner');
  for(const width of [1920,1440,1024,375])await capture('admin-'+width,width);
  await page.setViewportSize({width:1440,height:1000});
  await page.locator('[data-tab="tasks"]').click();await until(()=>page.locator('#tasksTable tbody tr').count().then(n=>n===50),'tasks not loaded');
  await page.selectOption('#taskState','failed');check(await page.locator('#tasksTable tbody tr').count()===13,'failure filter');
  await page.locator('#tasksTable [data-task-detail]').first().click();await until(()=>page.locator('#detailDrawer').innerText().then(s=>s.includes('错误原文')),'task evidence drawer not loaded');check((await page.locator('#detailDrawer').innerText()).includes('192.0.2.20'),'full administrator error');await page.keyboard.press('Escape');
  await capture('admin-tasks');
  await page.locator('[data-tab="accounts"]').click();await until(()=>page.locator('#accountsTable tbody tr').count().then(n=>n===50),'accounts missing');check((await page.locator('#accountCount').innerText()).includes('302'),'full account count');
  check(await page.getByRole('button',{name:'导入 CK',exact:true}).count()===1,'account toolbar must offer CK import');
  await page.getByRole('button',{name:'导入 CK',exact:true}).click();
  check(await page.locator('#f_pass,#f_email,#f_totp').count()===0,'CK import must not ask for Google login credentials');
  await page.locator('#ckFile').setInputFiles({name:'cookie_scanned_user.txt',mimeType:'text/plain',buffer:Buffer.from('sessionid=fixture-only; token=a=b')});
  await until(()=>page.locator('#ckText').inputValue().then(v=>v.includes('fixture-only')),'CK file content not loaded');
  check(await page.locator('#ckName').inputValue()==='scanned_user','CK filename should provide the account label');
  failCKImport=true;await page.locator('#ckSubmit').click();
  await until(()=>page.locator('#ckError').innerText().then(v=>v.includes('名称已存在')),'CK import conflict feedback missing');
  check(await page.locator('#ckText').inputValue()==='sessionid=fixture-only; token=a=b','failed CK import should retain input for correction');
  failCKImport=false;await page.fill('#ckName','scanned_user_2');
  await page.fill('#ckProxy','http://user:fixture@proxy.example:8080');
  page.once('dialog',dialog=>dialog.accept());await page.locator('#ckSubmit').click();
  await until(()=>page.locator('#dlg').isHidden(),'CK dialog did not close on successful import');
  const importedCK=JSON.parse(requests.findLast(r=>r.path==='/api/admin/accounts/import-ck').body);
  check(importedCK.ck==='sessionid=fixture-only; token=a=b'&&importedCK.name==='scanned_user_2'&&importedCK.proxy==='http://user:fixture@proxy.example:8080','complete CK and optional ISP must reach import endpoint');
  check(await page.locator('#ckText').count()===0,'successful import must clear the CK form');
  check(await page.evaluate(()=>!JSON.stringify(localStorage).includes('fixture-only')),'CK must not be persisted in browser local storage');
  await page.getByRole('button',{name:'导入 CK',exact:true}).click();await capture('admin-import-ck');await capture('admin-import-ck-mobile',375,812);await page.keyboard.press('Escape');await page.setViewportSize({width:1440,height:1000});
  check(await page.locator('#accountState option[value="birthday"]').count()===1,'birthday verification filter must be available');
  await page.locator('[data-account-view="disabled"]').click();
  await page.selectOption('#accountState','birthday');
  check(await page.locator('#accountsTable tbody tr').count()===1,'birthday accounts are separate from invalid accounts');
  check((await page.locator('#accountsTable').innerText()).includes('待生日验证'),'birthday status must be visible');
  check(!(await page.locator('#accountsTable').getByLabel('acc3 单号调度',{exact:true}).isChecked()),'birthday verification pauses scheduling');
  await page.locator('#accountsTable [data-account-detail]').first().click();
  check((await page.locator('#detailDrawer').innerText()).includes('真实出生日期'),'birthday detail explains how to restore the account');
  await page.keyboard.press('Escape');
  await page.locator('[data-account-view="enabled"]').click();
  await page.selectOption('#accountState','invalid');check(await page.locator('#accountsTable tbody tr').count()===3,'account status filter');await page.fill('#accountSearch','acc1');check(await page.locator('#accountsTable tbody tr').count()===2,'account search');await page.fill('#accountSearch','');await page.selectOption('#accountState','all');await capture('admin-accounts');
  await page.locator('[data-tab="proxies"]').click();await until(()=>page.locator('.proxy-group').count().then(n=>n===2),'groups missing');check(await page.locator('.proxy-group[open]').count()===0,'groups initially collapsed');check(await page.locator('#proxyTable tbody tr').count()===50,'proxy pagination');await page.locator('#proxyTable .pchk[data-name="acc1"]').check();await page.locator('#proxyTable').getByRole('button',{name:'下一页'}).click();check(await page.locator('#selCount').innerText()==='1','selection survives proxy page');await page.locator('#pchkAll').check();check(await page.locator('#selCount').innerText()==='51','select all applies to current page');await page.locator('#pchkAll').uncheck();await page.locator('#proxyTable').getByRole('button',{name:'上一页'}).click();await page.locator('#proxyTable .pchk[data-name="acc1"]').uncheck();await page.fill('#proxySearch','acc302');check(await page.locator('#proxyTable tbody tr').count()===1,'proxy search');await page.fill('#proxySearch','');await page.locator('.proxy-group summary').first().click();await page.fill('#groupSearch0','acc151');check(await page.locator('.proxy-group').first().locator('.chip:visible').count()===1,'group search');await page.fill('#groupSearch0','');await page.locator('.proxy-group summary').first().click();await capture('admin-proxies');
  await capture('admin-proxies-mobile',375,812);await page.setViewportSize({width:1440,height:1000});
  for(const tab of ['gallery','keys','users']){await page.locator('[data-tab="'+tab+'"]').click();await until(()=>page.locator('#lastUpdate').innerText().then(s=>s.startsWith('上次成功')),'tab update missing');await page.waitForTimeout(100);await capture('admin-'+tab);if(tab==='gallery'){for(const id of ['genModel','genDuration','genRatio','genRef']){check(await page.locator('label[for="'+id+'"]').count()===1,id+' visible label association');await page.locator('label[for="'+id+'"]').click();check(await page.locator('#'+id).evaluate(n=>document.activeElement===n),id+' label focuses control');}await page.setViewportSize({width:375,height:812});await page.fill('#genPrompt','管理员键盘提交测试');const before=requests.filter(r=>r.path==='/api/admin/generate').length;await page.locator('#genBtn').focus();await page.keyboard.press('Enter');await until(()=>page.locator('#genStatus').innerText().then(s=>s.includes('已提交')),'mobile keyboard generation missing');check(requests.filter(r=>r.path==='/api/admin/generate').length===before+1,'mobile keyboard generation submits once');await capture('admin-gallery-mobile',375,812);await page.setViewportSize({width:1440,height:1000});}}
  await page.locator('[data-tab="gallery"]').click();
  check(await page.locator('#genRefFiles').count()===1,'video generation must accept local reference images');
  const imageRequestsBefore=requests.filter(r=>r.path==='/api/admin/reference-images').length;
  await page.locator('#genRefFiles').setInputFiles(referenceImage);
  await until(()=>page.locator('#genRefPreview img').count().then(n=>n===1),'local reference preview missing');
  check(requests.filter(r=>r.path==='/api/admin/reference-images').length===imageRequestsBefore,'selecting a file must only preview until generation is requested');
  await capture('admin-local-reference');await capture('admin-local-reference-mobile',375,812);await page.setViewportSize({width:1440,height:1000});
  await page.fill('#genPrompt','本地参考图测试');await page.locator('#genBtn').click();
  await until(()=>page.locator('#genStatus').innerText().then(s=>s.includes('已提交')),'image generation submit missing');
  const imageGeneration=JSON.parse(requests.findLast(r=>r.path==='/api/admin/generate').body);
  check(imageGeneration.reference_uploads?.[0]==='a'.repeat(32)+'.png','uploaded image must be attached to generation');
  check(!JSON.stringify(imageGeneration).includes('blob:'),'browser-only preview URL must not be sent as a reference');
  check(await page.locator('#genRefPreview img').count()===0,'accepted administrator submit clears its selected reference preview');
  const selectableDurations=()=>page.locator('#genDuration option').evaluateAll(options=>options.filter(o=>!o.disabled&&!o.hidden).map(o=>o.value));
  check(await page.locator('#genDuration option[value="30"]').count()===0,'standalone Fast30 is delisted from creation');
  check(await page.locator('#genDuration option[value="15"]').count()===0,'mapped Fast15 is delisted');
  check(await page.locator('#genDuration option[value="15-normal"]').textContent()==='15 秒（正常申请）','normal Fast15 stays available');
  check(!(await selectableDurations()).includes('30'),'Fast exposes only 5/10/new15 target durations');
  await page.selectOption('#genDuration','15-normal');
  check(await page.locator('#genModel').inputValue()==='seedance-2.0','Fast 30 retains the Fast model');
  check(await page.locator('#genFast15Hint').isVisible(),'new15 explains target output, upstream parameter and billing');
  await page.fill('#genPrompt','Fast新15秒目标时长测试');
  const fastLongBefore=requests.filter(r=>r.path==='/api/admin/generate').length;
  await page.locator('#genBtn').click();
  await until(()=>Promise.resolve(requests.filter(r=>r.path==='/api/admin/generate').length===fastLongBefore+1),'Fast 30 request missing');
  const fastLongRequest=JSON.parse(requests.findLast(r=>r.path==='/api/admin/generate').body);
  check(fastLongRequest.model==='seedance-2.0'&&fastLongRequest.duration===15&&!fastLongRequest.duration_profile,'normal Fast15 reaches API with a 15 second parameter only');
  check(fastLongRequest.prompt==='Fast新15秒目标时长测试','Fast 30 preserves the user prompt');
  await until(()=>page.locator('#genBtn').isEnabled(),'Fast 30 submit did not finish');
  check(await page.locator('#genModel option').evaluateAll(options=>JSON.stringify(options.filter(o=>!o.hidden&&!o.disabled).map(o=>o.value)))==='["seedance-2.0"]','only Fast is published for administrator generation');
  for(const model of ['seedance-1.0','seedance-2.5']){
    const option=page.locator('#genModel option[value="'+model+'"]');
    check(await option.count()===1&&await option.evaluate(o=>o.hidden&&o.disabled),'delisted administrator model remains preserved and unavailable: '+model);
  }
  await capture('admin-gallery-new15');
  await page.selectOption('#genDuration','5');
  check(await page.locator('#genModel').inputValue()==='seedance-2.0','duration changes retain published Fast');
  await page.locator('[data-tab="accounts"]').click();
  const cleanupDialogs=[];let acceptCleanup=false;
  const cleanupDialog=async dialog=>{cleanupDialogs.push({type:dialog.type(),message:dialog.message()});if(dialog.type()==='confirm'&&!acceptCleanup)await dialog.dismiss();else await dialog.accept();};
  page.on('dialog',cleanupDialog);
  const cleanupBefore=requests.filter(r=>r.path==='/api/admin/accounts/delete-invalid').length;
  await page.getByRole('button',{name:'删除失效号',exact:true}).click();
  await until(()=>Promise.resolve(cleanupDialogs.length>0),'cleanup confirmation missing');
  check(cleanupDialogs[0].type==='confirm'&&cleanupDialogs[0].message.includes('acc102'),'cleanup must preview invalid account names without requiring a full scan');
  check(requests.filter(r=>r.path==='/api/admin/accounts/delete-invalid').length===cleanupBefore,'cancelled cleanup must not delete');
  acceptCleanup=true;
  await page.getByRole('button',{name:'删除失效号',exact:true}).click();
  await until(()=>Promise.resolve(cleanupDialogs.some(d=>d.type==='alert'&&d.message.includes('3 个'))),'cleanup result missing');
  await until(()=>page.locator('#accountCount').innerText().then(s=>s.includes('299')),'cleanup must refresh account list');
  check(requests.filter(r=>r.path==='/api/admin/accounts/delete-invalid').length===cleanupBefore+1,'confirmed cleanup submits once');
  check(!requests.some(r=>r.path==='/api/admin/accounts/verify-all'),'cleanup must not start a full scan');
  page.off('dialog',cleanupDialog);
  await page.locator('[data-tab="dash"]').click();await until(()=>page.locator('#dashAttention').innerText().then(s=>s.includes('47')),'dashboard reload missing');const oldTime=await page.locator('#lastUpdate').innerText();failStats=true;await page.getByRole('button',{name:'手动刷新',exact:true}).click();await until(()=>page.locator('#pageError').isVisible(),'error state missing');check(await page.locator('#lastUpdate').innerText()===oldTime,'failed refresh must preserve success timestamp');failStats=false;
  }
  // A logged-in admin visiting /login redirects immediately; change the fixture
  // session on the current origin before navigating to the member page.
  if(process.env.MEMBER_MEDIA_ONLY)await page.goto('https://dola-ui.test/login');
  await page.evaluate(()=>localStorage.setItem('dola_token','member-session'));
  const memberStart=requests.length;await page.goto('https://dola-ui.test/member');await until(()=>page.locator('#memberSubmit').isEnabled(),'member form not ready');
  check(await page.locator('#memberModel option').evaluateAll(options=>JSON.stringify(options.filter(o=>!o.hidden&&!o.disabled).map(o=>o.value)))==='["seedance-2.0"]','only Fast is published for member generation');
  for(const model of ['seedance-1.0','seedance-2.5'])check(await page.locator('#memberModel option[value="'+model+'"]').evaluate(o=>o.hidden&&o.disabled),'delisted member option stays unavailable: '+model);
  check(await page.locator('#memberDuration option').evaluateAll(options=>options.map(o=>o.value)).then(values=>JSON.stringify(values)==='["10","15","15-normal"]'),'member Fast intersects model and key duration permissions for both fifteen second choices');
  check(await page.locator('#memberRefFiles').count()===1&&await page.locator('#memberRefFolder[webkitdirectory]').count()===1,'member accepts files and image folders');
  const memberUploadsBefore=requests.filter(r=>r.path==='/api/me/reference-images').length;
  await page.locator('#memberRefFiles').setInputFiles(referenceImage);
  await until(()=>page.locator('#memberRefPreview img').count().then(n=>n===1),'member local preview missing');
  check(requests.filter(r=>r.path==='/api/me/reference-images').length===memberUploadsBefore,'member file selection only previews');
  const folderFixture=path.join(output,'member-reference-fixture');fs.mkdirSync(folderFixture,{recursive:true});
  fs.writeFileSync(path.join(folderFixture,'first.png'),referenceImage.buffer);fs.writeFileSync(path.join(folderFixture,'second.png'),referenceImage.buffer);fs.writeFileSync(path.join(folderFixture,'readme.txt'),'skip me');
  await page.locator('#memberRefFolder').setInputFiles(folderFixture);
  await until(()=>page.locator('#memberRefPreview img').count().then(n=>n===3),'member folder images missing');
  check((await page.locator('#memberRefError').innerText()).includes('跳过 1'),'member folder reports skipped nonimages');
  check(await page.locator('.sidebar nav button').count()===4,'member navigation');check(!(await page.locator('main').innerText()).includes('192.0.2.'),'member resource privacy');check((await page.locator('#memberSummary').innerText()).includes('97'),'per-key remaining quota');
  for(const width of [1920,1440,1024,375])await capture('member-'+width,width);
  await page.setViewportSize({width:1440,height:1000});await page.locator('.sidebar [data-view="keys"]').click();
  for(const action of ['data-reveal-key','data-copy-key']){await page.locator('#memberKeys ['+action+']').first().focus();await page.evaluate(()=>window.fixtureFocused=document.activeElement);normalKeys[0].usage.total++;await page.evaluate(()=>document.getElementById('refreshBtn').click());await until(()=>page.locator('#memberKeys').innerText().then(s=>s.includes('今日已用 '+normalKeys[0].usage.total+' / 100')),'member usage update missing');check(await page.evaluate(()=>document.activeElement===window.fixtureFocused),'member '+action+' identity survives usage refresh');}
  await page.locator('#memberKeys [data-reveal-key]').first().click();check((await page.locator('#memberKeys .key-value').first().innerText())===normalKeys[0].key,'focused key reveal works');await page.locator('#memberKeys [data-reveal-key]').first().click();
  await page.locator('.sidebar [data-view="tasks"]').click();await page.setViewportSize({width:375,height:812});await page.locator('#memberTasks .table-scroll').focus();await page.evaluate(()=>{window.fixtureFocused=document.activeElement;document.activeElement.scrollLeft=160;});memberTasks[0].prompt+=' 成员刷新测试';await page.evaluate(()=>document.getElementById('refreshBtn').click());await until(()=>page.locator('#memberTasks').innerText().then(s=>s.includes('成员刷新测试')),'member task update missing');check(await page.evaluate(()=>document.activeElement===window.fixtureFocused),'member table region identity survives refresh');check(await page.locator('#memberTasks .table-scroll').evaluate(n=>n.scrollLeft===160),'member horizontal table scroll survives refresh');normalKeys[0].usage.total=3;memberTasks[0].prompt='一只金毛在雪地里打滚';await page.setViewportSize({width:1440,height:1000});await page.locator('.sidebar [data-view="create"]').click();
  await page.setViewportSize({width:1440,height:1000});await page.selectOption('#memberKey',normalKeys[1].key);check(await page.locator('#memberSubmit').isDisabled(),'30-only key cannot submit target15');check(await page.locator('#memberDuration').inputValue()==='','30 permission does not become target15 permission');await page.selectOption('#memberKey',normalKeys[0].key);await page.selectOption('#memberDuration','15-normal');check(await page.locator('#memberModel').inputValue()==='seedance-2.0','member new15 retains Fast');check(await page.locator('#memberFast15Hint').isVisible(),'member new15 explains the mapped pipeline');
  await page.fill('#memberPrompt','成员测试视频');const before=requests.filter(r=>r.path==='/v1/videos/generations').length;await page.evaluate(()=>{const form=document.getElementById('memberForm');form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));form.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));});await until(()=>page.locator('#memberStatus').innerText().then(s=>s.includes('已提交')),'member submit feedback missing');check(requests.filter(r=>r.path==='/v1/videos/generations').length===before+1,'duplicate submit prevented');const posted=requests.findLast(r=>r.path==='/v1/videos/generations');check(posted.authorization==='Bearer '+normalKeys[0].key,'submit uses own key');check(JSON.parse(posted.body).duration===15&&!JSON.parse(posted.body).duration_profile,'submit uses only the normal 15 second parameter');
  check(JSON.parse(posted.body).model==='seedance-2.0','member Fast 30 request retains Fast');
  const memberUploads=requests.filter(r=>r.path==='/api/me/reference-images');check(memberUploads.length===memberUploadsBefore+3,'selected member folder and file upload only on submit');check(memberUploads.every(r=>r.authorization==='Bearer member-session'&&r.contentType==='image/png'),'member raw files use member login session and image Content-Type');check(JSON.parse(posted.body).reference_uploads.length===3,'member uploaded references reach generation');check(await page.locator('#memberRefPreview img').count()===0,'accepted member submit clears previews');
  failSubmit=true;await until(()=>page.locator('#memberSubmit').isEnabled(),'submit did not reset');await page.locator('#memberSubmit').click();await until(()=>page.locator('#memberStatus').innerText().then(s=>s.includes('额度不足')),'quota error feedback missing');check(await page.locator('#memberPrompt').inputValue()==='成员测试视频','failed submit preserves prompt');failSubmit=false;
  for(const v of ['tasks','keys','docs']){await page.locator('.sidebar [data-view="'+v+'"]').click();await capture('member-'+v);}
  check(!(await page.locator('#memberKeys').innerText()).includes(normalKeys[0].key),'keys masked by default');check(!(await page.locator('#memberExample').innerText()).includes(normalKeys[0].key),'examples omit real keys');
  check(!requests.slice(memberStart).some(r=>r.path.startsWith('/api/admin/')),'member never calls administrator endpoints');
  for(const mode of ['no-key','expired','disabled','quota']){scenario=mode;await page.goto('https://dola-ui.test/member');await until(()=>page.locator('#keyNotice').isVisible(),mode+' notice missing');check(await page.locator('#memberSubmit').isDisabled(),mode+' must block submit');}
  scenario='normal';
  await page.setViewportSize({width:375,height:812});await page.goto('https://dola-ui.test/member');await page.locator('#sideToggle').click();check(await page.locator('body').evaluate(b=>b.classList.contains('nav-open')),'mobile menu opens');await page.keyboard.press('Escape');check(await page.locator('.sidebar').evaluate(n=>n.inert),'closed mobile nav inert');
  await page.evaluate(()=>localStorage.removeItem('dola_token'));await page.goto('https://dola-ui.test/login');await capture('login-mobile',375,812);await capture('login-desktop',1440,1000);
  await page.fill('#email','member@example.test');await page.fill('#password','test-password');await page.locator('#submitBtn').click();await until(()=>Promise.resolve(new URL(page.url()).pathname==='/member'),'member login redirect');check(new URL(page.url()).pathname==='/member','member login route');
  check(errors.length===0,'unexpected JavaScript errors: '+errors.join(' | '));
  const report={assertions,screenshots,errors,requests:requests.length,scope:'Isolated browser fixture; no real server, account, proxy or video generation was touched.'};fs.writeFileSync(path.join(output,'ui-check.json'),JSON.stringify(report,null,2));process.stdout.write(JSON.stringify({assertions,screenshots:screenshots.length,errors},null,2)+'\n');
 }finally{await browser.close();}
}
run().catch(e=>{process.stderr.write(e.stack+'\n');process.exitCode=1;});
