const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const context={};context.window=context;
const source=fs.existsSync('web/platform-ui.js')?fs.readFileSync('web/platform-ui.js','utf8'):'';
vm.runInNewContext(source,context);

test('Dola keeps 2.5 delisted even with a stale enabled catalog and retains legacy request support',()=>{
  const p=context.PlatformUI,value={platform:'dola',enabled:true,models:[
    {id:'seedance-2.0',enabled:true,durations:[5,10,15]},
    {id:'seedance-1.0',enabled:true,durations:[5,10,15]},
    {id:'seedance-2.5',enabled:true,durations:[5,10,15,30],validation_status:'experimental'}]};
  const models=p.models('dola',value);
  assert.deepEqual(Array.from(models,m=>m.id),['seedance-2.0']);
  assert.deepEqual(value.models[2].durations,[5,10,15,30]);
  assert.equal(p.modelNotice('dola',value.models[2]),'');
  const body=p.request({platform:'dola',model:'seedance-2.5',duration:'10',prompt:'用户原文',ratio:'9:16'});
  assert.equal(body.model,'seedance-2.5');assert.equal(body.duration,10);assert.equal(body.prompt,'用户原文');assert.equal(body.ratio,'9:16');
  assert.equal(Object.hasOwn(body,'duration_profile'),false);
  assert.equal(p.models('dola',{...value,models:[{...value.models[2],durations:[15,30]}]}).length,0);
});
test('ordinary Dola and Doubao requests retain their explicit model and duration without inferred profiles',()=>{
  const p=context.PlatformUI;for(const platform of ['dola','doubao']){const body=p.request({platform,model:'seedance-2.0',duration:15,prompt:'原文'});assert.equal(body.duration,15);assert.equal(body.duration_profile,undefined);}
  assert.throws(()=>p.request({platform:'typo',model:'x',duration:15,prompt:'x'}),/平台/);
});

test('unpublished pseudo-duration choices cannot become malformed requests',()=>{
  const p=context.PlatformUI;for(const duration of ['15-normal','15-pro'])assert.throws(()=>p.request({platform:'dola',model:'seedance-2.0',duration,prompt:'原文'}));
  for(const duration of [5,10,15]){const body=p.request({platform:'dola',model:'seedance-2.0',duration,prompt:'原文'});assert.equal(body.duration,duration);assert.equal(body.duration_profile,undefined);}
});

test('unknown capabilities fail closed and old permissions only allow Dola',()=>{
  assert.ok(context.PlatformUI,'platform helper available');
  const p=context.PlatformUI;
  assert.equal(p.capabilities('doubao',{}).enabled,false);
  assert.equal(p.models('doubao',{enabled:true,models:[{id:'x',enabled:false,durations:[15]}]}).length,0);
  assert.equal(p.allowed({},'doubao'),false);assert.equal(p.allowed({},'dola'),true);
  assert.equal(p.allowed({allowed_platforms:['doubao']},'doubao'),true);
  assert.equal(p.models('dola',{enabled:true,models:[{id:'seedance-1.0',enabled:true,durations:[15]},{id:'seedance-2.0',enabled:true,durations:[5,10,15]}]}).length,1);
});
test('late reads from a previous platform cannot replace latest state',()=>{
  assert.ok(context.PlatformUI,'platform helper available');
  const reads=context.PlatformUI.reads(),old=reads.start('dola'),latest=reads.start('doubao');
  assert.equal(reads.current(old,'dola'),false);
  assert.equal(reads.current(latest,'doubao'),true);
  assert.equal(reads.current(latest,'dola'),false);
});
test('reference capabilities require explicit boolean or verified enabled metadata',()=>{
  const p=context.PlatformUI;
  assert.equal(p.supports?.({enabled:true,max_count:2}),true);
  assert.equal(p.supports?.({enabled:false}),false);
  assert.equal(p.supports?.(true),true);
  assert.equal(p.supports?.(undefined),false);
});
test('stale non-native capabilities never become ordinary Doubao generation choices',()=>{
  const value={platform:'doubao',enabled:true,models:[
    {id:'ordinary-native',entry_mode:'native_controls',durations:[15]},
    {id:'legacy-skill',entry_mode:'work_skill',durations:[30]},
    {id:'older-native',durations:[10]}]};
  assert.deepEqual(Array.from(context.PlatformUI.models('doubao',value),item=>item.id),['ordinary-native','older-native']);
});
test('verified ordinary dialogue remains visible when the account needs restoration without exposing unsupported entries',()=>{
  const value={platform:'doubao',enabled:false,reason:'请恢复豆包账号登录',models:[
    {id:'dialog_seedance_2.0_fast',entry_mode:'ordinary_dialog',enabled:true,durations:[15],ratios:['16:9']},
    {id:'ordinary-native',entry_mode:'native_controls',enabled:true,durations:[15]},
    {id:'legacy-skill',entry_mode:'work_skill',enabled:true,durations:[30]},
    {id:'unverified-dialog',entry_mode:'ordinary_dialog',enabled:false,durations:[30]},
    {id:'unknown-entry',entry_mode:'other',enabled:true,durations:[15]}]};
  assert.deepEqual(Array.from(context.PlatformUI.models('doubao',value),item=>item.id),['dialog_seedance_2.0_fast','ordinary-native']);
  assert.equal(context.PlatformUI.capabilities('doubao',value).enabled,false);
});
test('experimental Doubao thirty seconds uses the real model and duration without the Dola profile',()=>{
  const p=context.PlatformUI,model={id:'dialog_seedance_2.5',label:'Seedance 2.5（30秒试验）',validation_status:'experimental',account_category:'标准账号',entry_mode:'ordinary_dialog',enabled:true,durations:[30],ratios:['16:9']};
  const choices=p.models('doubao',{platform:'doubao',enabled:true,models:[model]});
  assert.equal(choices.length,1);assert.equal(choices[0].id,model.id);assert.equal(choices[0].label,model.label);
  assert.equal(typeof p.modelNotice,'function');
  const notice=p.modelNotice('doubao',model);assert.match(notice,/2\.5.*30秒试验/);assert.match(notice,/真实申请30秒/);assert.match(notice,/仅标准账号/);assert.match(notice,/尚未实测/);assert.match(notice,/5倍消耗/);assert.match(notice,/费用以平台为准/);assert.doesNotMatch(notice,/已验证|实测通过/);
  assert.equal(p.modelNotice('doubao',{...model,validation_status:'verified'}),'');assert.equal(p.modelNotice('dola',model),'');
  const body=p.request({platform:'doubao',model:model.id,duration:'30',prompt:'原创猫咪',ratio:'16:9'});
  assert.equal(body.duration,30);assert.equal(body.model,'dialog_seedance_2.5');assert.equal(Object.hasOwn(body,'duration_profile'),false);
});

test('ordinary duration choices do not expose retired fifteen second shortcuts',()=>{
  const choices=context.PlatformUI.durationChoices('dola','seedance-2.0',[5,10,15]);assert.deepEqual(Array.from(choices,c=>c.value),['5','10']);
  assert.equal(context.PlatformUI.durationHint('dola','seedance-2.0','10'),'');
});
