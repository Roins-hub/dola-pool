// Execute the production guard against native store/service boundaries; no network.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {webcrypto, createHash} = require('node:crypto');
const guardFile = path.join(__dirname, '../integrations/dola_image_order_guard.js');

function fixture({renamed = false, count = 6} = {}) {
  const expected = Array.from({length:count}, (_,i)=>({filename:`image_${i}.png`, sha256:String(i+1).repeat(64)}));
  const states = {attachmentsStore:{attachmentsMap:{chat:{_17:[]}}}, attachmentUploadStore:{preprocessTaskMap:{}}};
  const sent = [];
  const service = {sendMessage(request) {sent.push(request);return request;}};
  let modules = {qBy:{Bc:name=>states[name]},nR1:{K:()=>({getMessageService:()=>service})}};
  const factories = {};
  if (renamed) {
    // Observed yDg.b reads Store.getState(); xvn.a resolves the native service.
    const c = name => ({getState: () => states[name]});
    const s = name => name === 'chatIMService' ? {getMessageService: () => service} : undefined;
    const r = {a: () => false};
    modules = {
      yDg: {b: function h(e){var t;return null==(t=c(e))?void 0:t.getState();}},
      xvn: {a: async(e,t)=>{let n=s(e,t);if(n)return(0,r.a)(n)?new n:n;{let n=c(e,t);
        try{let a=await(null==n?void 0:n.load());if(!a)return;l(e,a,t);let i=s(e,t);return(0,r.a)(i)?new i:i;}catch{return;}}}}
    };
    factories.yDg = function () { /* storeContainer getAsyncStore chat-ioc-manager: store */ };
    factories.xvn = function () { /* serviceContainer resourceStore chat-ioc-manager: cannot bind empty service */ };
  }
  const runtime = name=>modules[name];runtime.m = factories;
  const input = {getAttribute:name=>name==='data-dola-ref-target'?'fixture':null,
    matches:selector=>selector==='input[type=file]',
    __reactFiber$fixture:{memoizedProps:{chatId:'chat',skill:{skill_type:17},inputUploadConfig:{}}}};
  const context = vm.createContext({document:{querySelectorAll:()=>[input]},crypto:webcrypto,TextEncoder,
    window:{__dolaReferenceRuntime:runtime}});
  const source = fs.existsSync(guardFile)?fs.readFileSync(guardFile,'utf8'):'async () => ({ok:false,reason:"guard_missing"})';
  const install = ()=>vm.runInContext('('+source+')',context)({token:'fixture',expected});
  const fill = ()=>{states.attachmentsStore.attachmentsMap.chat._17 = expected.map((file,i)=>({
    localKey:`local-${i}`,fileName:file.filename,type:'image',fileKey:`asset/key-${i+1}`}));};
  const request = ()=>({marker:'keep',messages:[{ext:{chat_ability:JSON.stringify({ability_type:17})},
    content_blocks_v2:[{block_type:10052,content:{attachment_block:{attachments:expected.map((file,i)=>({
      type:1,image:{name:file.filename,uri:`asset/key-${i+1}`}}))}}},
      {block_type:10000,content:{text_block:{text:'人物和动作完整提示词 @图片1 @图片6'}}}]}]});
  return {expected,states,sent,service,modules,factories,install,fill,request,get guard(){return context.window.__dolaReferenceOrderGuard;}};
}

test('observed renamed store and service support seven reference identities before native signing',async()=>{
  const f=fixture({renamed:true,count:7});
  assert.equal((await f.install()).ok,true);
  f.fill();assert.equal(f.guard.status().ready,true);
  const request=f.request(),before=JSON.stringify(request);
  assert.equal(f.service.sendMessage(request),request);
  assert.equal(JSON.stringify(request),before,'The guard must never rewrite the native payload');
  assert.equal(f.sent.length,1);assert.equal((await f.guard.evidence()).entries.length,7);
});

test('renamed interfaces still reject seven images with swapped identity or native request order',async()=>{
  for(const swap of ['store','request']){
    const f=fixture({renamed:true,count:7});assert.equal((await f.install()).ok,true);f.fill();
    const request=f.request();
    if(swap==='store')f.states.attachmentsStore.attachmentsMap.chat._17.reverse();
    else request.messages[0].content_blocks_v2[0].content.attachment_block.attachments.reverse();
    assert.throws(()=>f.service.sendMessage(request),/参考图/);assert.equal(f.sent.length,0);
  }
});

test('unknown renamed getter or service export never calls a setter or React hook',async()=>{
  const unknownStore=fixture({renamed:true});let mutations=0;
  unknownStore.modules.yDg.b=()=>{mutations++;return {};};
  assert.equal((await unknownStore.install()).reason,'store');assert.equal(mutations,0);
  const unknownService=fixture({renamed:true});let hookCalls=0;
  delete unknownService.modules.xvn.a;
  unknownService.modules.xvn.b=()=>{hookCalls++;throw Error('React hooks cannot run outside the component');};
  assert.equal((await unknownService.install()).reason,'send_guard');assert.equal(hookCalls,0);
});

test('a renamed function mixing getState with a mutation or React hook is never invoked',async()=>{
  for(const mode of ['setter','hook']){
    const f=fixture({renamed:true});let calls=0;
    const accessor=name=>({getState:()=>f.states[name],setState:()=>{calls++;}});
    const useMemo=read=>{calls++;return read();};
    f.modules.yDg.b=mode==='setter'?name=>{
      const state=accessor(name);state.setState();return state.getState();
    }:name=>useMemo(()=>accessor(name).getState());
    assert.equal((await f.install()).reason,'store',mode);
    assert.equal(calls,0,mode);assert.equal(f.sent.length,0);
  }
});

test('six images in declared order pass without changing native request or prompt',async()=>{
  const f=fixture();assert.equal((await f.install()).ok,true);f.fill();
  assert.equal(f.guard.status().ready,true);
  const request=f.request();assert.equal(f.service.sendMessage(request),request);assert.equal(f.sent.length,1);
  const proof=await f.guard.evidence();
  assert.equal(proof.sendChecked,true);assert.equal(proof.entries.length,6);
  assert.deepEqual(JSON.parse(JSON.stringify(proof.entries[0])),{index:1,filename:'image_0.png',sha256:'1'.repeat(64),
    upstreamKeySha256:createHash('sha256').update('asset/key-1').digest('hex')});
  assert.equal(JSON.stringify(proof).includes('asset/key-'),false);
});

test('same six files in reversed native store order stop before native send',async()=>{
  const f=fixture();assert.equal((await f.install()).ok,true);f.fill();
  f.states.attachmentsStore.attachmentsMap.chat._17.reverse();
  assert.equal(f.guard.status().ready,false);
  assert.throws(()=>f.service.sendMessage(f.request()),/参考图/);assert.equal(f.sent.length,0);
});

test('same six final attachments with two swapped URI keys stop before native send',async()=>{
  const f=fixture();assert.equal((await f.install()).ok,true);f.fill();
  const request=f.request(),images=request.messages[0].content_blocks_v2[0].content.attachment_block.attachments;
  [images[1].image.uri,images[4].image.uri]=[images[4].image.uri,images[1].image.uri];
  assert.throws(()=>f.service.sendMessage(request),/参考图/);assert.equal(f.sent.length,0);
});

test('store changing after ready is caught again at send',async()=>{
  const f=fixture();assert.equal((await f.install()).ok,true);f.fill();assert.equal(f.guard.status().ready,true);
  f.states.attachmentsStore.attachmentsMap.chat._17.reverse();
  assert.throws(()=>f.service.sendMessage(f.request()),/参考图/);assert.equal(f.sent.length,0);
});

test('post-send evidence keeps the verified snapshot when native composer clears attachments',async()=>{
  const f=fixture();assert.equal((await f.install()).ok,true);f.fill();
  f.service.sendMessage(f.request());f.states.attachmentsStore.attachmentsMap.chat._17=[];
  assert.equal(f.guard.status().ready,true);assert.equal(f.guard.status().sendChecked,true);
  const proof=await f.guard.evidence();assert.equal(proof.ready,true);assert.equal(proof.entries.length,6);
  assert.equal(proof.entries[5].filename,'image_5.png');
});

test('ordinary text confirmation after verified video is allowed after composer clears',async()=>{
  const f=fixture();assert.equal((await f.install()).ok,true);f.fill();f.service.sendMessage(f.request());
  f.states.attachmentsStore.attachmentsMap.chat._17=[];
  const confirm={messages:[{ext:{},content_blocks_v2:[{block_type:10000,content:{text_block:{text:'Ja'}}}]}]};
  assert.equal(f.service.sendMessage(confirm),confirm);assert.equal(f.sent.length,2);
  assert.equal((await f.guard.evidence()).sendChecked,true);assert.equal((await f.guard.evidence()).entries.length,6);
});

test('a second video request cannot re-submit the verified six images',async()=>{
  const f=fixture();assert.equal((await f.install()).ok,true);f.fill();f.service.sendMessage(f.request());
  assert.throws(()=>f.service.sendMessage(f.request()),/参考图/);assert.equal(f.sent.length,1);
});

test('a confirmation containing an attachment is not an ordinary text confirmation',async()=>{
  const f=fixture();assert.equal((await f.install()).ok,true);f.fill();f.service.sendMessage(f.request());
  const request=f.request();delete request.messages[0].ext.chat_ability;
  request.messages[0].content_blocks_v2[1].content.text_block.text='Ja';
  assert.throws(()=>f.service.sendMessage(request),/参考图/);assert.equal(f.sent.length,1);
});

for(const mode of ['missing','duplicate-key','missing-key','missing-name','failed','unknown-block']) {
  test(`${mode} cannot submit a count-only false success`,async()=>{
    const f=fixture();assert.equal((await f.install()).ok,true);f.fill();const request=f.request();
    const rows=f.states.attachmentsStore.attachmentsMap.chat._17;
    if(mode==='missing')request.messages[0].content_blocks_v2[0].content.attachment_block.attachments.pop();
    if(mode==='duplicate-key')rows[1].fileKey=rows[0].fileKey;
    if(mode==='missing-key')delete rows[1].fileKey;
    if(mode==='missing-name')delete rows[1].fileName;
    if(mode==='failed')f.states.attachmentUploadStore.preprocessTaskMap['local-1']={status:'Error'};
    if(mode==='unknown-block')request.messages[0].content_blocks_v2[0].block_type=99999;
    assert.throws(()=>f.service.sendMessage(request),/参考图/);assert.equal(f.sent.length,0);
  });
}

test('unknown image URI schema fails closed instead of guessing a match',async()=>{
  const f=fixture();assert.equal((await f.install()).ok,true);f.fill();const request=f.request();
  const image=request.messages[0].content_blocks_v2[0].content.attachment_block.attachments[0].image;
  image.url=image.uri;delete image.uri;
  assert.throws(()=>f.service.sendMessage(request),/参考图/);assert.equal(f.sent.length,0);
});

test('a composer containing previous attachments is rejected before upload',async()=>{
  const f=fixture();f.fill();assert.equal((await f.install()).ok,false);
});

test('safe diagnostics identifies a missing key and hashes alternate native key paths',async()=>{
  const f=fixture();assert.equal((await f.install()).ok,true);f.fill();
  assert.equal(typeof f.guard.diagnostics,'function');
  const row=f.states.attachmentsStore.attachmentsMap.chat._17[0];delete row.fileKey;
  row.media={key:'https://upload.invalid/object?secret=never-print-this'};
  f.states.attachmentUploadStore.preprocessTaskMap['local-0']={status:'Normal',extraCredentials:'never-print-credentials',
    uploadResult:{media:{key:'native-alternate-key'}}};
  const report=await f.guard.diagnostics();assert.equal(report.rowCount,6);assert.equal(report.reason,'missing_key');
  const item=report.rows[0];assert.equal(item.filename,'image_0.png');
  assert.equal(item.attachmentFields.includes('media'),true);assert.equal(item.taskFields.includes('uploadResult'),true);
  assert.equal(item.keyCandidates.find(x=>x.path==='attachment.media.key').sha256,
    createHash('sha256').update('https://upload.invalid/object?secret=never-print-this').digest('hex'));
  assert.equal(item.keyCandidates.find(x=>x.path==='task.uploadResult.media.key').sha256,
    createHash('sha256').update('native-alternate-key').digest('hex'));
  const text=JSON.stringify(report);assert.equal(text.includes('https://'),false);
  assert.equal(text.includes('never-print-this'),false);assert.equal(text.includes('never-print-credentials'),false);
  assert.equal(f.guard.status().ready,false);
});

test('safe diagnostics distinguishes native processing from an absent file key',async()=>{
  const f=fixture();assert.equal((await f.install()).ok,true);f.fill();
  assert.equal(typeof f.guard.diagnostics,'function');
  f.states.attachmentUploadStore.preprocessTaskMap['local-0']={status:'processing',loading:true};
  const report=await f.guard.diagnostics();assert.equal(report.reason,'loading');
  assert.equal(report.rows[0].taskStatus,'processing');assert.equal(report.rows[0].taskLoading,true);
  assert.equal(report.rows[0].keyCandidates[0].path,'attachment.fileKey');assert.equal(f.guard.status().ready,false);
});

test('unknown native message schema retains rejected six-image mapping and safe request shape',async()=>{
  const f=fixture();assert.equal((await f.install()).ok,true);f.fill();const request=f.request();
  request.authorization='DO-NOT-EXPOSE-THIS';delete request.messages[0].ext.chat_ability;
  assert.throws(()=>f.service.sendMessage(request),/参考图/);f.states.attachmentsStore.attachmentsMap.chat._17=[];
  const proof=await f.guard.evidence();assert.equal(proof.sendChecked,false);assert.equal(proof.reason,'message_schema');
  assert.equal(proof.entries.length,6,'rejecting a message must retain the checked input mapping');
  const summary=proof.lastRequestSummary;assert.ok(summary);assert.equal(summary.messagesIsArray,true);
  assert.equal(summary.messagesLength,1);assert.equal(summary.topLevelKeys.includes('authorization'),true);
  assert.equal(summary.messages[0].chatAbility.valueType,'undefined');
  assert.equal(summary.messages[0].blocks[0].blockType,10052);
  assert.equal(summary.attachments[0].entries.length,6);
  assert.equal(summary.attachments[0].entries[0].uriCandidates[0].sha256,
    createHash('sha256').update('asset/key-1').digest('hex'));
  assert.equal((await f.guard.diagnostics()).lastRequestSummary.messagesLength,1);
  const text=JSON.stringify(proof);for(const secret of ['DO-NOT-EXPOSE-THIS','人物和动作完整提示词','asset/key-1'])
    assert.equal(text.includes(secret),false);
  assert.equal(f.sent.length,0);
});

test('safe rejected request summary reports top-level native blocks even without a messages array',async()=>{
  const f=fixture();assert.equal((await f.install()).ok,true);f.fill();const sample=f.request().messages[0];
  const request={chat_ability:{ability_type:17},content_blocks_v2:sample.content_blocks_v2};
  assert.throws(()=>f.service.sendMessage(request),/参考图/);
  const summary=(await f.guard.evidence()).lastRequestSummary;
  assert.ok(summary);assert.equal(summary.messagesIsArray,false);
  assert.equal(summary.chatAbilities[0].abilityType,17);assert.equal(summary.attachments[0].entries.length,6);
});

function nativeTwoMessage(request) {
  const message=request.messages[0],blocks=message.content_blocks_v2;
  request.messages=[{...message,local_message_id:'attachment-message',content_blocks_v2:[blocks[0]]},
    {...message,local_message_id:'text-message',content_blocks_v2:[blocks[1]]}];
  return request;
}

test('observed native attachment-message then text-message forwards unchanged exactly once',async()=>{
  const f=fixture();assert.equal((await f.install()).ok,true);f.fill();const request=nativeTwoMessage(f.request());
  const original=JSON.stringify(request);
  assert.equal(f.service.sendMessage(request),request);assert.equal(JSON.stringify(request),original);
  assert.equal(f.sent.length,1);assert.equal(f.sent[0],request);
  assert.equal((await f.guard.evidence()).sendChecked,true);
});

for(const mode of ['extra-message','duplicate-attachment-message','reversed-messages','reversed-uris','missing-video-ability','extra-block']) {
  test(`native two-message ${mode} stops before original send`,async()=>{
    const f=fixture();assert.equal((await f.install()).ok,true);f.fill();const request=nativeTwoMessage(f.request());
    if(mode==='extra-message')request.messages.push({ext:{},content_blocks_v2:[{block_type:10000,content:{text_block:{text:'extra'}}}]});
    if(mode==='duplicate-attachment-message')request.messages[1]=request.messages[0];
    if(mode==='reversed-messages')request.messages.reverse();
    if(mode==='reversed-uris')request.messages[0].content_blocks_v2[0].content.attachment_block.attachments.reverse();
    if(mode==='missing-video-ability')delete request.messages[0].ext.chat_ability;
    if(mode==='extra-block')request.messages[1].content_blocks_v2.push(request.messages[0].content_blocks_v2[0]);
    assert.throws(()=>f.service.sendMessage(request),/参考图/);assert.equal(f.sent.length,0);
  });
}

for(const mode of ['canonical-order','swapped-order','transport-only']) {
  test(`native transport fileKey differs from canonical task key: ${mode}`,async()=>{
    const f=fixture();assert.equal((await f.install()).ok,true);f.fill();
    f.states.attachmentsStore.attachmentsMap.chat._17.forEach((row,i)=>{
      delete row.fileKey;
      f.states.attachmentUploadStore.preprocessTaskMap[row.localKey]={status:'Normal',fileKey:`transport-${i}`,
        ...(mode==='transport-only'?{}:{key:`asset/key-${i+1}`,uploadResult:{Uri:`asset/key-${i+1}`}})};
    });
    const request=nativeTwoMessage(f.request());
    if(mode==='swapped-order')request.messages[0].content_blocks_v2[0].content.attachment_block.attachments.reverse();
    if(mode==='canonical-order'){
      assert.equal(f.service.sendMessage(request),request);assert.equal(f.sent.length,1);
      assert.equal((await f.guard.evidence()).entries[0].upstreamKeySha256,
        createHash('sha256').update('asset/key-1').digest('hex'));
    }else{
      assert.throws(()=>f.service.sendMessage(request),/参考图/);assert.equal(f.sent.length,0);
    }
  });
}
