// Real page, isolated HTTP fixtures: never submits to the production server.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let playwright;
try { playwright = require('playwright'); } catch { playwright = require(process.argv[2]); }
const root = path.resolve(__dirname, '..', 'web');
const task = {id:'video_refresh_fixture',prompt:'刷新测试',model:'seedance-2.0',duration:15,duration_profile:'fast15-via30',status:'processing',phase:'polling',created_at:1,updated_at:1,accepted_at:1,conversation_id:'123',account:'fixture'};
const completed = {...task,status:'completed',phase:'completed',actual_duration:15.1,video_url:'/videos/fixture.mp4',updated_at:2};
async function until(fn, message) {
  for(let i=0;i<50;i++){if(await fn())return;await new Promise(resolve=>setTimeout(resolve,40));}
  throw new Error(message);
}
async function fixture(browser) {
  const context=await browser.newContext();
  const state={tasks:[{...task}],hold:null,fail:false,gets:0,posts:0};
  await context.addInitScript(()=>{
    localStorage.setItem('dola_token','fixture-session');
    window.refreshTicks=[];
    window.setInterval=fn=>{window.refreshTicks.push(fn);return window.refreshTicks.length;};
    const timeout=window.setTimeout;
    // Make a real fetch abort deadline fast, without replacing fetch or AbortController.
    window.setTimeout=(fn,ms,...args)=>timeout(fn,ms>=15000?100:ms,...args);
  });
  await context.route('**/*',async route=>{
    const req=route.request(),url=new URL(req.url()),p=url.pathname;
    const json=data=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
    if(p==='/api/admin/tasks'){
      state.gets++;
      const snapshot=structuredClone(state.tasks);
      if(state.hold){const hold=state.hold;state.hold=null;await hold(snapshot,route);return;}
      if(state.fail)return route.fulfill({status:503,contentType:'application/json',body:'{"detail":"fixture unavailable"}'});
      return json({tasks:snapshot});
    }
    if(p==='/api/admin/tasks/'+task.id)return json({task:state.tasks.find(t=>t.id===task.id),events:[]});
    if(p==='/api/admin/accounts')return json({accounts:[],categories:[],quota_policy:{mode:'fast',limit:4}});
    if(p==='/api/admin/reference-images/config')return json({max_count:30,max_bytes:15728640});
    if(p==='/api/admin/generate'){
      state.posts++;const body=JSON.parse(req.postData());
      const added={...task,id:'video_new_fixture',prompt:body.prompt,status:'queued',phase:'queued',created_at:3,updated_at:3};
      state.tasks.unshift(added);return json(added);
    }
    if(p.startsWith('/api/'))return json({});
    if(p.startsWith('/videos/'))return route.fulfill({contentType:'video/mp4',body:Buffer.alloc(0)});
    const name=p==='/admin'?'index.html':p.slice(1);
    if(!/^[\w.-]+$/.test(name)||!fs.existsSync(path.join(root,name)))return route.fulfill({status:404,body:''});
    return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(root,name))});
  });
  const page=await context.newPage();
  const evaluate=page.evaluate.bind(page);
  page.evaluate=(fn,arg)=>evaluate(fn,arg,undefined,false);
  await page.goto('https://refresh.test/admin#tasks');
  await until(()=>page.locator('#tasksTable').innerText().then(s=>s.includes('刷新测试')),'initial tasks not loaded');
  await page.evaluate(()=>refresh());
  return {context,page,state};
}
const cases=[
  ['hidden quota buttons cannot freeze completed task updates',async({page,state})=>{
    assert.ok(await page.locator('#accountQuotaFast').isDisabled());
    state.tasks=[completed];await page.evaluate(()=>refreshTicks.forEach(tick=>tick()));
    await until(()=>page.locator('#tasksTable').innerText().then(s=>s.includes('已完成')),'automatic timer did not update completion');
  }],
  ['typing a prompt and retained proxy selections do not freeze gallery results',async({page,state})=>{
    await page.evaluate(()=>switchTab('gallery'));
    await until(()=>page.locator('#adminRecentTasks').innerText().then(s=>s.includes('刷新测试')),'gallery not loaded');
    await page.fill('#genPrompt','下一条提示词不要丢失');
    await page.evaluate(()=>SELECTED.add('fixture'));
    state.tasks=[completed];await page.evaluate(()=>refresh({background:true}));
    assert.equal(await page.locator('#genPrompt').inputValue(),'下一条提示词不要丢失');
    assert.equal(await page.locator('#genPrompt').evaluate(n=>n===document.activeElement),true);
    assert.equal(await page.locator('#adminResult video').count(),1);
  }],
  ['task details follow completion without reopening or stealing focus',async({page,state})=>{
    await page.locator('[data-task-detail]').first().click();
    await until(()=>page.locator('#detailDrawer').innerText().then(s=>s.includes('后台查询')),'detail not loaded');
    await page.locator('#detailDrawer [data-copy-task]').focus();
    state.tasks=[completed];await page.evaluate(()=>refresh({background:true}));
    assert.match(await page.locator('#detailDrawer').innerText(),/已完成/);
    assert.equal(await page.locator('#detailDrawer video').count(),1);
    assert.equal(await page.locator('#detailDrawer [data-copy-task]').evaluate(n=>n===document.activeElement),true);
    await page.evaluate(()=>window.existingVideo=document.querySelector('#detailDrawer video'));
    await page.evaluate(()=>refresh({background:true}));
    assert.equal(await page.evaluate(()=>existingVideo===document.querySelector('#detailDrawer video')),true);
  }],
  ['returning to the page refreshes immediately and respects auto-refresh off',async({page,state})=>{
    await page.evaluate(()=>autoRefresh=false);state.tasks=[completed];
    const before=state.gets;await page.evaluate(()=>window.dispatchEvent(new Event('focus')));
    await new Promise(resolve=>setTimeout(resolve,80));assert.equal(state.gets,before);
    await page.evaluate(()=>{autoRefresh=true;window.dispatchEvent(new Event('focus'));});
    await until(()=>page.locator('#tasksTable').innerText().then(s=>s.includes('已完成')),'focus did not refresh task status');
  }],
  ['an older task response cannot overwrite a new submission',async({page,state})=>{
    await page.evaluate(()=>switchTab('gallery'));
    await until(()=>page.locator('#adminRecentTasks').innerText().then(s=>s.includes('刷新测试')),'gallery not loaded');
    let release;state.hold=(snapshot,route)=>new Promise(resolve=>{release=async()=>{await route.fulfill({contentType:'application/json',body:JSON.stringify({tasks:snapshot})});resolve();};});
    await page.evaluate(()=>{window.oldRefresh=refresh();});
    await until(()=>Boolean(release),'old request not started');
    await page.fill('#genPrompt','新提交任务');await page.locator('#genBtn').click();
    await until(()=>page.locator('#adminRecentTasks').innerText().then(s=>s.includes('新提交任务')),'submitted task not shown');
    await release();await page.evaluate(()=>window.oldRefresh);
    assert.match(await page.locator('#adminRecentTasks').innerText(),/新提交任务/);
    assert.equal(state.posts,1);
  }],
  ['a stalled read times out and the next refresh recovers',async({page,state})=>{
    let held;state.hold=async(_snapshot,route)=>{held=route;};
    const done=page.evaluate(()=>refresh()).catch(()=>{});
    await until(()=>page.locator('#pageError').innerText().then(s=>/超时/.test(s)).catch(()=>false),'stalled read never reported timeout');
    await done;await held.abort().catch(()=>{});
    state.tasks=[completed];await page.evaluate(()=>refresh());
    assert.match(await page.locator('#tasksTable').innerText(),/已完成/);
    assert.equal(state.posts,0);
  }]
];
(async()=>{
  const browser=await playwright.chromium.launch({headless:true,executablePath:process.argv[3]||undefined});
  const failures=[];
  try{for(const [name,run] of cases){const f=await fixture(browser);try{await run(f);console.log('PASS',name);}catch(error){failures.push(name);console.error('FAIL',name,'\n',error.message);}finally{await f.context.close();}}}
  finally{await browser.close();}
  console.log(JSON.stringify({passed:cases.length-failures.length,failed:failures}));
  if(failures.length)process.exitCode=1;
})().catch(error=>{console.error(error);process.exitCode=1;});
