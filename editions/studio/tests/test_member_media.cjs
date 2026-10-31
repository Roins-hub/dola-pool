// Execute the real member scripts with only DOM and HTTP boundaries replaced.
// No live service, login or video generation is touched.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { test } = require('node:test');
const root = path.resolve(__dirname, '..');

class Element {
  constructor() { this.children=[]; this.listeners={}; this.dataset={}; this.style={}; this.disabled=false; this.hidden=false; this._value=''; this.textContent=''; this._html=''; this.descendants=new Map(); this.classList={remove(){},toggle(){}}; }
  set innerHTML(html) { this._html=html; this.options=[...html.matchAll(/<option value="([^"]*)"/g)].map(m=>m[1]); if(this.options.length)this._value=this.options[0]; }
  get innerHTML() { return this._html; }
  set value(value) { this._value=String(value); }
  get value() { return this._value; }
  addEventListener(name,fn) { (this.listeners[name] ||= []).push(fn); }
  querySelector(selector) { if(!this.descendants.has(selector))this.descendants.set(selector,new Element());return this.descendants.get(selector); }
  async fire(name,event={}) { await Promise.all((this.listeners[name]||[]).map(fn=>fn({target:this,preventDefault(){},...event}))); }
  replaceChildren() { this.children=[]; }
  append(...items) { this.children.push(...items); }
  setAttribute() {}
  removeAttribute() {}
  focus() {}
}
const image=(name='ref.png',type='image/png',size=100)=>({name,type,size});
const turn=()=>new Promise(resolve=>setImmediate(resolve));
function harness({keyDurations=[10,15,30],limits={max_count:30,max_bytes:15*1024*1024},admin=false,tasks=[]}={}) {
  const elements=new Map(),calls=[],revoked=[],events={},q=selector=>{
    if(!elements.has(selector))elements.set(selector,new Element());
    return elements.get(selector);
  };
  const prefix=admin?'genRef':'memberRef';
  for(const suffix of ['Files','Folder','Preview','Help','Error'])q('#'+prefix+suffix);
  const me={user:{email:'member@example.test',role:'member'},keys:[{key:'fixture-api-key',name:'member key',enabled:true,daily_limit:2,concurrency_limit:1,allowed_durations:keyDurations,usage:{total:0,active:0}}],usage:{total:0,completed:0},supported_durations:[5,10,15,30]};
  q('#memberModel').value='seedance-2.0';q('#memberRatio').value='default';q('#memberTaskRange').value='50';q('#memberTaskState').value='all';
  let nextId=1,uploadFailure=0,generationFailure=false;
  const response=(data,status=200)=>({ok:status<400,status,json:async()=>data});
  const fetch=async(url,options={})=>{
    calls.push({url,...options});
    if(url.endsWith('/config'))return response(limits);
    if(url==='/api/me')return response(me);
    if(url.startsWith('/api/me/tasks'))return response({tasks});
    if(url.endsWith('/reference-images')||url.endsWith('/reference-media')){
      if(uploadFailure&&nextId===uploadFailure){uploadFailure=0;return response({detail:'临时上传失败'},503);}
      return response({id:'uploaded-'+nextId++});
    }
    if(url==='/v1/videos/generations')return generationFailure?response({detail:'暂时无法提交'},503):response({id:'video_fixture',status:'queued',prompt:JSON.parse(options.body).prompt});
    throw new Error('Unexpected endpoint '+url);
  };
  const document={hidden:false,querySelector:selector=>['#genRefFiles','#genMediaFiles'].includes(selector)&&!admin?null:q(selector),querySelectorAll:()=>[],createElement:()=>new Element(),addEventListener:(name,fn)=>(events[name] ||= []).push(fn)};
  const UI={esc:String,number:String,render:(selector,html)=>q(selector).innerHTML=html,icon:()=>'',empty:()=>'',time:()=>'',safeURL:value=>String(value||'').startsWith('/')?value:'',openDrawer:(title,html)=>q('#detailDrawer').innerHTML=html,closeNavigation(){},closeDrawer(){},init(){},setError(){},toast(){},detailError:String};
  const context={document,UI,fetch,localStorage:{getItem:()=> 'fixture-session',removeItem(){}},location:{origin:'https://example.test',hash:'#create'},history:{replaceState(){}},setInterval(){},URL:{createObjectURL:file=>'blob:'+file.name,revokeObjectURL:url=>revoked.push(url)},console};
  context.E=UI.esc;context.crypto=require('node:crypto');context.window=context;context.q=q;context.api=async(url,options={})=>{
    const result=await fetch(url,{...options,body:options.file});const data=await result.json();
    if(!result.ok){const error=new Error(data.detail);error.status=result.status;throw error;}
    return data;
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(root,'web/platform-ui.js'),'utf8'),context);
  // Extracted administrator functions normally close over this real helper.
  // Supply that lexical dependency while keeping its Dola request logic intact.
  context.generationAdvanced=context.PlatformUI.advanced({query:q,prefix:'gen',anchor:'#genFast15Hint'});
  context.currentGenerationPlatform=()=>q('#genPlatform').value||'dola';
  context.generationCaps=null;context.generating=false;context.generationPlatform='dola';context.generationRevision=0;
  context.renderGenerationCapabilities=()=>{q('#genBtn').disabled=context.generating;};
  vm.runInContext(fs.readFileSync(path.join(root,'web/task-evidence.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(root,'web/reference-images.js'),'utf8'),context);
  if(!admin)vm.runInContext(fs.readFileSync(path.join(root,'web/member.js'),'utf8'),context);
  const fireReady=async()=>{for(const fn of events.DOMContentLoaded||[])await fn();await turn();await turn();};
  const select=async(files,folder=false)=>{const input=q('#'+prefix+(folder?'Folder':'Files'));input.files=files;await input.fire('change');await turn();};
  const click=async dataset=>{for(const fn of events.click||[])await fn({target:{closest:()=>({dataset})}});};
  return {q,calls,revoked,context,me,fireReady,select,click,limits,setUploadFailure:n=>uploadFailure=n,setGenerationFailure:b=>generationFailure=b};
}
const uploads=h=>h.calls.filter(c=>c.url.endsWith('/reference-images'));
const generations=h=>h.calls.filter(c=>c.url==='/v1/videos/generations');
const preview=h=>h.q('#memberRefPreview').children;

test('member no-charge policy refusal shows its reason in rows result and details',async()=>{
  const task={id:'video_policy_fixture',model:'seedance-2.0',duration:15,status:'failed',phase:'platform_rejected_no_charge',prompt:'fixture'};
  const h=harness({tasks:[task]});await h.fireReady();
  for(const selector of ['#memberRecent','#memberResult']){
    assert.match(h.q(selector).innerHTML,/生成失败 · 内容限制（未计额度）/);
    assert.match(h.q(selector).innerHTML,/b-red/);
  }
  assert.match(h.q('#memberResult').innerHTML,/本地本次额度已释放/);
  await h.click({memberTask:task.id});
  assert.match(h.q('#detailDrawer').innerHTML,/平台已拒绝生成，本地本次额度已释放；请修改内容后手动提交。/);
  assert.equal(generations(h).length,0);
});

test('member ordinary refusals do not claim no charge and historical completed videos remain available',async()=>{
  for(const phase of ['platform_rejected','platform_copyright','failed']){
    const h=harness({tasks:[{id:'video_other',status:'failed',phase,prompt:'fixture',dola_reply:'0 credits'}]});await h.fireReady();
    assert.doesNotMatch(h.q('#memberRecent').innerHTML,/未计额度/);
    assert.doesNotMatch(h.q('#memberResult').innerHTML,/本地本次额度已释放/);
  }
  const h=harness({tasks:[{id:'video_history',status:'completed',phase:'platform_rejected_no_charge',actual_duration:15.105,video_url:'/videos/history.mp4'}]});await h.fireReady();
  assert.match(h.q('#memberResult').innerHTML,/<video[^>]+src="\/videos\/history.mp4"/);
  assert.doesNotMatch(h.q('#memberResult').innerHTML,/未计额度|本地本次额度已释放/);
});

test('ordinary Fast only publishes available ten seconds while stale fifteen remains blocked',async()=>{
  const h=harness();await h.fireReady();assert.deepEqual(h.q('#memberDuration').options,['10']);
  assert.equal(h.q('#memberDuration').value,'10');assert.equal(h.q('#memberFast15Hint').hidden,true);
  h.q('#memberDuration').value='15-normal';h.q('#memberPrompt').value='原文';await h.q('#memberForm').fire('submit');
  assert.equal(generations(h).length,0);assert.match(h.q('#memberStatus').textContent,/请选择/);
});

test('delisted models cannot submit even when server and key still support their durations',async()=>{
  for(const model of ['seedance-1.0','seedance-2.5']){
    const h=harness({keyDurations:[5,10,15,30]});
    h.me.supported_durations_by_model={'seedance-1.0':[5,10,15],'seedance-2.0':[5,10,15,30],'seedance-2.5':[5,10,15,30]};
    await h.fireReady();await h.select([image()]);
    h.q('#memberModel').value=model;
    h.q('#memberPrompt').value='保留原文';
    for(const seconds of [5,10,15,30]){
      h.q('#memberDuration').value=seconds===15?'15-normal':String(seconds);await h.q('#memberForm').fire('submit');
    }
    assert.equal(generations(h).length,0);assert.equal(uploads(h).length,0);
    assert.deepEqual(h.me.keys[0].allowed_durations,[5,10,15,30]);
  }
});

test('stale delisted models return to Fast without granting a fifteen-only key a published duration',async()=>{
  for(const model of ['seedance-1.0','seedance-2.5']){
    const h=harness({keyDurations:[15]});await h.fireReady();h.q('#memberModel').value=model;await h.q('#memberModel').fire('change');
    assert.equal(h.q('#memberModel').value,'seedance-2.0');assert.deepEqual(h.q('#memberDuration').options,['']);
    h.q('#memberPrompt').value='原文';await h.q('#memberForm').fire('submit');assert.equal(generations(h).length,0);assert.deepEqual(h.me.keys[0].allowed_durations,[15]);
  }
});

test('member key descriptions do not advertise an unpublished ordinary fifteen second option',async()=>{
  const h=harness({keyDurations:[15]});await h.fireReady();assert.match(h.q('#memberKeys').innerHTML,/Fast：无 秒/);
  assert.deepEqual(h.q('#memberDuration').options,['']);assert.deepEqual(h.me.keys[0].allowed_durations,[15]);
});

test('a key with only 30 has no published duration and cannot submit new15',async()=>{
  const h=harness({keyDurations:[30]});await h.fireReady();
  assert.equal(h.q('#memberSubmit').disabled,true);assert.equal(h.q('#memberDuration').disabled,true);
  assert.deepEqual(h.q('#memberDuration').options,['']);
  h.q('#memberPrompt').value='test';
  for(const seconds of [15,30]){
    h.q('#memberDuration').value=seconds===15?'15-normal':String(seconds);await h.q('#memberForm').fire('submit');
  }
  assert.equal(generations(h).length,0);assert.deepEqual(h.me.keys[0].allowed_durations,[30]);
});

test('a fifteen-only key cannot submit a hidden ordinary fifteen selection',async()=>{
  const h=harness({keyDurations:[15]});await h.fireReady();assert.equal(h.q('#memberSubmit').disabled,true);
  h.q('#memberPrompt').value='原文';h.q('#memberDuration').value='15';await h.q('#memberForm').fire('submit');
  assert.equal(generations(h).length,0);assert.deepEqual(h.me.keys[0].allowed_durations,[15]);
});

test('5 and 10 second requests never inherit the new15 profile',async()=>{
  const h=harness({keyDurations:[5,10,15]});await h.fireReady();h.q('#memberPrompt').value='原文';
  for(const seconds of [5,10]){
    h.q('#memberDuration').value=seconds===15?'15-normal':String(seconds);await h.q('#memberDuration').fire('change');
    await h.q('#memberForm').fire('submit');
    const body=JSON.parse(generations(h).at(-1).body);
    assert.equal(body.duration,seconds);
    assert.equal(body.duration_profile,undefined);
    if(seconds!==15)assert.doesNotMatch(h.q('#memberExample').textContent,/duration_profile/);
  }
});

test('local selection previews only; successful submit uses session upload and API key generation',async()=>{
  const h=harness();await h.fireReady();const file=image('猫窝.png');await h.select([file]);
  assert.equal(preview(h).length,1);assert.equal(uploads(h).length,0);assert.equal(generations(h).length,0);
  h.q('#memberPrompt').value='  用户原文\n不要背景音乐  ';
  await h.q('#memberForm').fire('submit');
  assert.equal(uploads(h).length,1);assert.equal(uploads(h)[0].body,file);
  assert.equal(uploads(h)[0].headers['Content-Type'],'image/png');assert.equal(uploads(h)[0].headers.Authorization,'Bearer fixture-session');
  assert.equal(generations(h)[0].headers.Authorization,'Bearer fixture-api-key');
  assert.deepEqual(JSON.parse(generations(h)[0].body).reference_uploads,['uploaded-1']);
  assert.equal(JSON.parse(generations(h)[0].body).prompt,'  用户原文\n不要背景音乐  ');
  assert.equal(preview(h).length,0);assert.deepEqual(h.revoked,['blob:猫窝.png']);
  assert.ok(!h.calls.some(c=>c.url.startsWith('/api/admin/')));
});

test('folder filters nonimages with feedback; too many valid images are rejected as a whole',async()=>{
  const h=harness({limits:{max_count:2,max_bytes:1024}});await h.fireReady();
  await h.select([image('a.png'),image('readme.txt','text/plain'),image('b.webp','image/webp')],true);
  assert.equal(preview(h).length,2);assert.match(h.q('#memberRefError').textContent,/跳过.*1/);
  preview(h)[0].children[2].onclick();preview(h)[0].children[2].onclick();
  assert.equal(preview(h).length,0);
  await h.select([image('a.png'),image('b.png'),image('c.png')],true);
  assert.equal(preview(h).length,0);assert.match(h.q('#memberRefError').textContent,/最多 2/);
  assert.equal(uploads(h).length,0);
});

test('nonimage, empty or oversized local files do not enter upload selection',async()=>{
  const h=harness({limits:{max_count:30,max_bytes:1024}});await h.fireReady();
  for(const file of [image('x.gif','image/gif'),image('empty.png','image/png',0),image('big.png','image/png',1025)]){
    await h.select([file]);assert.equal(preview(h).length,0);assert.ok(h.q('#memberRefError').textContent);
  }
  assert.equal(uploads(h).length,0);
});

test('partial upload failure retains selection and retry reuses completed upload IDs',async()=>{
  const h=harness();await h.fireReady();await h.select([image('a.png'),image('b.png')]);h.q('#memberPrompt').value='原样提交';h.setUploadFailure(2);
  await h.q('#memberForm').fire('submit');assert.equal(preview(h).length,2);assert.equal(generations(h).length,0);
  await h.q('#memberForm').fire('submit');assert.equal(uploads(h).length,3);
  assert.deepEqual(JSON.parse(generations(h)[0].body).reference_uploads,['uploaded-1','uploaded-2']);
});

test('generation failure retains files and cached IDs; retry does not upload them twice',async()=>{
  const h=harness();await h.fireReady();await h.select([image()]);h.q('#memberPrompt').value='重试原文';h.setGenerationFailure(true);
  await h.q('#memberForm').fire('submit');assert.equal(preview(h).length,1);assert.equal(h.revoked.length,0);
  h.setGenerationFailure(false);await h.q('#memberForm').fire('submit');
  assert.equal(uploads(h).length,1);assert.equal(generations(h).length,2);assert.equal(preview(h).length,0);
});

test('combined URL/local reference limit and tampered Fast duration stop before upload',async()=>{
  const h=harness({limits:{max_count:1,max_bytes:1024}});await h.fireReady();await h.select([image()]);h.q('#memberPrompt').value='test';h.q('#memberRefs').value='https://example.test/ref.png';
  await h.q('#memberForm').fire('submit');assert.equal(uploads(h).length,0);assert.equal(generations(h).length,0);assert.match(h.q('#memberStatus').textContent,/合计最多 1/);
  h.q('#memberRefs').value='';h.q('#memberDuration').value='20';await h.q('#memberForm').fire('submit');assert.equal(uploads(h).length,0);assert.equal(generations(h).length,0);
});

test('administrator shared helper keeps its existing upload contract and previews',async()=>{
  const h=harness({admin:true});await h.fireReady();await h.select([image()]);
  assert.equal(h.q('#genRefPreview').children.length,1);assert.equal(uploads(h).length,0);
  const ids=await h.context.ReferenceImages.upload(0,()=>{});
  assert.deepEqual(Array.from(ids),['uploaded-1']);assert.equal(uploads(h)[0].url,'/api/admin/reference-images');
  assert.equal(h.q('#genRefPreview').children.length,1);
});

test('administrator publishes only ordinary five and ten seconds and recovers stale model selections',()=>{
  const h=harness({admin:true}),duration=h.q('#genDuration');h.q('#genModel').value='seedance-2.0';
  h.context.generationProfile=()=>'';h.context.renderGenerationAdvanced=()=>{};
  const source=fs.readFileSync(path.join(root,'web/admin-ui.js'),'utf8').match(/  function syncGenerationDuration\(\)\{[\s\S]*?\n  \}/)[0];
  vm.runInContext(source+'\nsyncGenerationDuration();',h.context);assert.deepEqual(duration.options,['10','5']);
  duration.value='15';vm.runInContext('syncGenerationDuration();',h.context);assert.equal(duration.value,'10');
  for(const model of ['seedance-1.0','seedance-2.5']){h.q('#genModel').value=model;vm.runInContext('syncGenerationDuration();',h.context);assert.equal(h.q('#genModel').value,'seedance-2.0');assert.deepEqual(duration.options,['10','5']);}
  duration.value='5';vm.runInContext('syncGenerationDuration();',h.context);assert.equal(duration.value,'5');
});

test('administrator ordinary five and ten second requests preserve prompt without an expert profile',async()=>{
  const h=harness({admin:true});await h.fireReady();const posted=[];
  h.context.generationProfile=()=>'';
  h.context.pendingSubmission=null;h.context.loadGallery=async()=>{};
  h.context.api=async(url,options)=>{posted.push({url,...options});return {id:'video_admin_fixture'};};
  h.q('#genPrompt').value='  管理端原文\n不修改  ';h.q('#genRef').value='';h.q('#genRatio').value='default';h.q('#genModel').value='seedance-2.0';
  const source=fs.readFileSync(path.join(root,'web/admin-ui.js'),'utf8').match(/  async function submitGenerate\(\)\{[^\r\n]+/)[0];
  vm.runInContext(source+'\nwindow.fixtureSubmit=submitGenerate;',h.context);
  for(const seconds of [5,10]){
    h.q('#genDuration').value=seconds===15?'15-normal':String(seconds);await h.context.fixtureSubmit();
    const body=posted.at(-1).body;
    assert.equal(body.model,'seedance-2.0');assert.equal(body.duration,seconds);
    assert.equal(body.duration_profile,undefined);
    assert.equal(body.prompt,'  管理端原文\n不修改  ');
  }
});

test('administrator blocks a delisted model before uploading local references',async()=>{
  const h=harness({admin:true});await h.fireReady();await h.select([image()]);
  h.q('#genPrompt').value='保留原文';h.q('#genRef').value='';
  const source=fs.readFileSync(path.join(root,'web/admin-ui.js'),'utf8').match(/  async function submitGenerate\(\)\{[^\r\n]+/)[0];
  vm.runInContext(source+'\nwindow.fixtureSubmit=submitGenerate;',h.context);
  for(const model of ['seedance-1.0','seedance-2.5']){
    h.q('#genModel').value=model;h.q('#genDuration').value='30';
    await h.context.fixtureSubmit();
    assert.match(h.q('#genStatus').textContent,/已下架/);
  }
  assert.equal(uploads(h).length,0);
  assert.equal(h.calls.filter(c=>c.url==='/api/admin/generate').length,0);
});

test('a fifteen-only key stays blocked after recovering a stale ordinary model selection',async()=>{
  const h=harness({keyDurations:[15]});await h.fireReady();assert.equal(h.q('#memberSubmit').disabled,true);
  h.q('#memberModel').value='seedance-2.5';await h.q('#memberModel').fire('change');
  assert.equal(h.q('#memberModel').value,'seedance-2.0');assert.equal(h.q('#memberSubmit').disabled,true);
  h.q('#memberPrompt').value='原文';await h.q('#memberForm').fire('submit');assert.equal(generations(h).length,0);
});

test('mixed audio/video selection uploads privately once and reaches generation alongside images',async()=>{
  const h=harness();await h.fireReady();
  await h.select([image()]);
  const input=h.q('#memberMediaFiles');input.files=[image('旁白.wav','audio/wav'),image('镜头.mp4','video/mp4')];
  await input.fire('change');await turn();
  assert.equal(h.q('#memberMediaPreview').children.length,2);
  assert.equal(h.calls.filter(c=>c.url.endsWith('/reference-media')).length,0);
  h.q('#memberPrompt').value='参考音频的节奏和视频运镜';h.setGenerationFailure(true);
  await h.q('#memberForm').fire('submit');
  let posted=JSON.parse(generations(h)[0].body);
  assert.deepEqual(posted.reference_media_uploads,['uploaded-2','uploaded-3']);
  assert.deepEqual(posted.reference_uploads,['uploaded-1']);
  const files=h.calls.filter(c=>c.url.endsWith('/reference-media'));
  assert.equal(files[0].headers['X-File-Name'],encodeURIComponent('旁白.wav'));
  assert.equal(files[0].headers.Authorization,'Bearer fixture-session');
  h.setGenerationFailure(false);await h.q('#memberForm').fire('submit');
  assert.equal(h.calls.filter(c=>c.url.endsWith('/reference-media')).length,2);
  assert.equal(h.q('#memberMediaPreview').children.length,0);
});

test('media upload rejection retains selection and never submits a generation',async()=>{
  const h=harness();await h.fireReady();const input=h.q('#memberMediaFiles');
  input.files=[image('voice.wav','audio/wav')];await input.fire('change');
  h.setUploadFailure(1);h.q('#memberPrompt').value='test';
  await h.q('#memberForm').fire('submit');
  assert.equal(generations(h).length,0);
  assert.equal(h.q('#memberMediaPreview').children.length,1);
  assert.match(h.q('#memberStatus').textContent,/失败/);
});

test('member generation retries keep the same submission key and changed intent gets a new key',async()=>{
  const h=harness();await h.fireReady();h.q('#memberPrompt').value='same intent';h.setGenerationFailure(true);
  await h.q('#memberForm').fire('submit');await h.q('#memberForm').fire('submit');
  const bodies=generations(h).map(c=>JSON.parse(c.body));
  assert.equal(typeof bodies[0].idempotency_key,'string');
  assert.equal(bodies[0].idempotency_key,bodies[1].idempotency_key);
  h.q('#memberPrompt').value='changed intent';await h.q('#memberForm').fire('submit');
  assert.notEqual(JSON.parse(generations(h)[2].body).idempotency_key,bodies[0].idempotency_key);
});
