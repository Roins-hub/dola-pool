// Real admin/member pages and browser downloads; all HTTP traffic stays in fixtures.
const {test,before,after}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('./browser-runtime.cjs');
const root=path.resolve(__dirname,'../web');
const bytes=Buffer.from('\x00\x00\x00\x18ftypmp42-task-download-fixture','binary');
const single={id:'video_download_fixture',prompt:'单视频下载',platform:'dola',model:'seedance-2.0',duration:10,status:'completed',phase:'completed',created_at:1,video_url:'https://public.example/videos/one.mp4?old=1#preview'};
const multi={...single,id:'video_multi_fixture',prompt:'双视频下载',video_url:'/videos/one.mp4',clips:[
  {index:1,status:'completed',video_url:'/videos/one.mp4'},
  {index:2,status:'completed',video_url:'/videos/two.mp4'},
  {index:3,status:'failed',video_url:'/videos/unverified.mp4'}
]};
const waiting={...single,id:'video_waiting_fixture',prompt:'等待视频',status:'processing',phase:'polling',video_url:''};
let browser;
before(async()=>{browser=await chromium.launch({headless:true});});
after(async()=>{await browser?.close();});

async function fixture(t,role='admin',tasks=[single,multi,waiting]){
  const context=await browser.newContext({acceptDownloads:true});
  t.after(()=>context.close());
  const state={tasks,requests:[],status:200,body:bytes,contentType:'video/mp4',hold:null,holdOfficial:null};
  await context.addInitScript(()=>{
    localStorage.setItem('dola_token','fixture-session');
    window.setInterval=()=>0;
  });
  await context.route('**/*',async route=>{
    const req=route.request(),url=new URL(req.url()),p=url.pathname;
    const json=data=>route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
    if(p.startsWith('/api/')){
      state.requests.push({path:p,method:req.method()});
      if(p==='/api/admin/tasks'||p==='/api/me/tasks')return json({tasks:state.tasks});
      if(p.startsWith('/api/admin/tasks/')&&p.endsWith('/download')&&state.holdOfficial){await state.holdOfficial();return json({});}
      if(p.startsWith('/api/admin/tasks/')&&!p.endsWith('/status'))return json({task:state.tasks.find(task=>task.id===p.split('/')[4]),events:[]});
      if(p==='/api/admin/accounts')return json({accounts:[],categories:[],quota_policy:{mode:'fast',limit:4}});
      if(p==='/api/me')return json({user:{email:'fixture@example.test',role:'member'},keys:[],usage:{completed:0,total:0},supported_durations:[10,15,30]});
      if(p.endsWith('/config'))return json({max_count:30,max_bytes:15728640});
      return json({});
    }
    if(p.startsWith('/videos/')){
      if(req.resourceType()==='fetch'){
        state.requests.push({path:p,method:req.method(),origin:url.origin});
        if(state.hold){const hold=state.hold;state.hold=null;await hold();}
        return route.fulfill({status:state.status,contentType:state.contentType,body:state.body});
      }
      return route.fulfill({contentType:'video/mp4',body:bytes});
    }
    const name=p==='/admin'?'index.html':p==='/member'?'member.html':p.slice(1);
    if(url.origin!=='https://download.test'||!/^[\w.-]+$/.test(name)||!fs.existsSync(path.join(root,name)))return route.fulfill({status:404,body:''});
    return route.fulfill({contentType:name.endsWith('.js')?'text/javascript':name.endsWith('.css')?'text/css':'text/html',body:fs.readFileSync(path.join(root,name))});
  });
  const page=await context.newPage();
  const pageErrors=[];page.on('pageerror',error=>pageErrors.push(error.message));
  page.setDefaultTimeout(4000);
  const evaluate=page.evaluate.bind(page);
  page.evaluate=(fn,arg)=>evaluate(fn,arg,undefined,false);
  await page.goto(`https://download.test/${role}#tasks`);
  const list=page.locator(role==='admin'?'#tasksTable':'#memberTasks');
  try{await list.getByText('单视频下载',{exact:true}).waitFor();}
  catch(error){throw new Error(`${error.message}\nPage errors: ${pageErrors.join('; ')}\n${await page.locator('body').innerText()}`);}
  return {page,list,state};
}

for(const role of ['admin','member'])test(`${role}: task list saves single and separate clip files with one click`,async t=>{
  const {page,list,state}=await fixture(t,role);
  const row=list.locator('tr').filter({hasText:'单视频下载'});
  const button=row.getByRole('button',{name:'一键下载',exact:true});
  assert.equal(await button.count(),1,'published video needs a direct download button');
  assert.equal(await list.locator('tr').filter({hasText:'等待视频'}).locator('[data-task-file-download]').count(),0);
  const url=page.url();
  const save=page.waitForEvent('download');
  await button.click();
  const download=await save;
  assert.equal(download.suggestedFilename(),'video_download_fixture.mp4');
  assert.deepEqual(fs.readFileSync(await download.path()),bytes);
  assert.equal(page.url(),url,'saving must keep the task list open');
  const clips=list.locator('tr').filter({hasText:'双视频下载'});
  assert.equal(await clips.locator('[data-task-file-download]').count(),2,'each verified clip appears once');
  const clipSave=page.waitForEvent('download');
  await clips.getByRole('button',{name:'下载视频 2',exact:true}).click();
  const clip=await clipSave;
  assert.equal(clip.suggestedFilename(),'video_multi_fixture_2.mp4');
  assert.deepEqual(fs.readFileSync(await clip.path()),bytes);
  assert.deepEqual(state.requests.filter(r=>r.origin).map(r=>[r.origin,r.path]),[
    ['https://download.test','/videos/one.mp4'],['https://download.test','/videos/two.mp4']
  ],'published public URLs must download through the current project origin');
  assert.equal(state.requests.filter(r=>r.method!=='GET').length,0,'downloads must never resubmit tasks');
});

test('download failure is visible, restores the button, and can be retried',async t=>{
  const {page,list,state}=await fixture(t,'admin',[single]);
  const button=list.getByRole('button',{name:'一键下载',exact:true});
  assert.equal(await button.count(),1,'direct download button is missing');
  let downloads=0;page.on('download',()=>downloads++);
  for(const [status,body,contentType,message] of [
    [404,Buffer.from('missing'),'text/plain','视频文件不存在'],
    [200,Buffer.alloc(0),'video/mp4','视频文件为空'],
    [200,Buffer.from('<html>login</html>'),'text/html','未返回视频文件']
  ]){
    Object.assign(state,{status,body,contentType});
    await button.click();
    await page.locator('.toast.is-error').filter({hasText:message}).waitFor();
    assert.equal(await button.isEnabled(),true);
  }
  assert.equal(downloads,0,'failed responses must not save an error page as a video');
  Object.assign(state,{status:200,body:bytes,contentType:'video/mp4'});
  const save=page.waitForEvent('download');await button.click();
  assert.deepEqual(fs.readFileSync(await (await save).path()),bytes);
});

test('an in-flight download survives list refresh without allowing duplicate downloads',async t=>{
  const {page,list,state}=await fixture(t,'admin',[single]);
  const button=list.getByRole('button',{name:'一键下载',exact:true});
  assert.equal(await button.count(),1,'direct download button is missing');
  let release,started;
  const pending=new Promise(resolve=>started=resolve);
  state.hold=()=>{started();return new Promise(resolve=>release=resolve);};
  await button.click();await pending;
  await page.evaluate(()=>refresh());
  const busy=list.getByRole('button',{name:'下载中…',exact:true});
  assert.equal(await busy.isDisabled(),true);
  await busy.evaluate(node=>node.click());
  assert.equal(state.requests.filter(r=>r.origin).length,1);
  const save=page.waitForEvent('download');release();
  assert.deepEqual(fs.readFileSync(await (await save).path()),bytes);
  await list.getByRole('button',{name:'一键下载',exact:true}).waitFor();
  assert.equal(await list.getByRole('button',{name:'一键下载',exact:true}).isEnabled(),true);
});

test('unpublished, unsafe and deleted results do not offer downloads',async t=>{
  const {page}=await fixture(t);
  const counts=await page.evaluate(()=>[
    {},{video_url:'javascript:alert(1)'},{video_url:'https://external.example/movie.mp4'},
    {video_url:'/videos/nested/file.mp4'},{video_url:'/videos/a%2Fb.mp4'},
    {video_url:'/videos/one.mp4',deleted_at:1},
    {clips:[{status:'failed',video_url:'/videos/one.mp4'}]}
  ].map(task=>TaskResultsUI.downloadButtons(task)));
  assert.deepEqual(counts,Array(7).fill(''));
});

test('unavailable original downloads allow detail updates, while an active retrieval stays mounted',async t=>{
  const processing={...single,status:'processing',phase:'polling',video_url:'',conversation_id:'123'};
  const {page,list,state}=await fixture(t,'admin',[processing]);
  await list.getByRole('button',{name:'查看详情',exact:true}).click();
  const drawer=page.locator('#detailDrawer');
  const check=drawer.getByRole('button',{name:'同账号同线路查询官方原片',exact:true});
  await check.waitFor();
  await page.waitForFunction(()=>document.querySelector('[data-clean-download-plugin]')?.textContent.includes('平台尚未提供'));
  let started,release;
  const pending=new Promise(resolve=>started=resolve);
  state.holdOfficial=()=>{started();return new Promise(resolve=>release=resolve);};
  await check.click();await pending;
  await page.evaluate(()=>window.originalDownloadControl=document.querySelector('[data-clean-download-plugin] button'));
  state.tasks=[single];await page.evaluate(()=>refresh({background:true}));
  assert.equal(await page.evaluate(()=>originalDownloadControl===document.querySelector('[data-clean-download-plugin] button')),true,'refresh must not replace an active retrieval');
  release();state.holdOfficial=null;
  await check.waitFor({state:'visible'});
  await page.waitForFunction(()=>!document.querySelector('[data-clean-download-plugin] button').disabled);
  await page.evaluate(()=>refresh({background:true}));
  assert.match(await drawer.innerText(),/已完成/,'an unavailable download button must not freeze task status');
});
