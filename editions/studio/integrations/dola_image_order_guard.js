/* Check native image identities and order without changing uploads or requests. */
async ({token,expected}) => {
  const VIDEO=17,roots=[document],inputs=[];
  for(let i=0;i<roots.length;i++)for(const el of roots[i].querySelectorAll('*')){
    if(el.shadowRoot)roots.push(el.shadowRoot);
    if(el.matches('input[type=file]'))inputs.push(el);
  }
  const input=inputs.find(el=>el.getAttribute('data-dola-ref-target')===token);
  const fiberKey=Object.keys(input||{}).find(key=>/^__react(?:Fiber|InternalInstance)\$/.test(key));
  let fiber=input?.[fiberKey],props;
  for(let i=0;fiber&&i<50;i++,fiber=fiber.return){
    const current=fiber.memoizedProps||fiber.pendingProps||{};
    if(current.chatId&&current.inputUploadConfig){props=current;break;}
  }
  if(!props||Number(props.skill?.skill_type)!==VIDEO)return {ok:false,reason:'context'};
  if(!Array.isArray(expected)||!expected.length||new Set(expected.map(x=>x.filename)).size!==expected.length||
      expected.some(x=>typeof x.filename!=='string'||!x.filename||! /^[a-f0-9]{64}$/.test(x.sha256)))return {ok:false,reason:'manifest'};
  let runtime=window.__dolaReferenceRuntime;
  if(!runtime&&Array.isArray(window.__LOADABLE_LOADED_CHUNKS__)){
    window.__LOADABLE_LOADED_CHUNKS__.push([['dola-image-order-'+token],{},value=>{runtime=value;}]);
    if(runtime)window.__dolaReferenceRuntime=runtime;
  }
  if(typeof runtime!=='function')return {ok:false,reason:'runtime'};
  const load=id=>{try{return runtime(id);}catch{return null;}};
  const isStateReader=fn=>typeof fn==='function'&&
    /^function[\w$]*\(([\w$]+)\)\{var([\w$]+);returnnull==\(\2=[\w$]+\(\1\)\)\?void0:\2\.getState\(\);?\}$/.test(String(fn).replace(/\s+/g,''));
  let get=load('qBy')?.Bc;
  if(typeof get!=='function')for(const [id,factory] of Object.entries(runtime.m||{})){
    const source=String(factory),legacy=/attachmentsStore|attachmentUploadStore/.test(source);
    const stateReader=/storeContainer/.test(source)&&/getAsyncStore/.test(source);
    if(!legacy&&!stateReader)continue;
    const mod=load(id);
    // Observed yDg.b reads the named Store.getState(); its other exports load a
    // Store instance or run React hooks. Never call those as state readers.
    const modern=stateReader&&isStateReader(mod?.b)?mod.b:null;
    const candidate=legacy&&(mod?.Bc||mod?.GX)||modern;
    try{if(typeof candidate==='function'&&candidate('attachmentsStore')?.attachmentsMap&&
        candidate('attachmentUploadStore')?.preprocessTaskMap){get=candidate;break;}}catch{}
  }
  if(typeof get!=='function')return {ok:false,reason:'store'};
  const attachments=()=>get('attachmentsStore')?.attachmentsMap?.[props.chatId]?.['_'+VIDEO]||[];
  if(!Array.isArray(attachments())||attachments().length)return {ok:false,reason:'existing_attachments'};
  let service=load('nR1');
  if(typeof service?.K!=='function')for(const [id,factory] of Object.entries(runtime.m||{})){
    const source=String(factory);
    if(!/serviceContainer|StoreService/.test(source))continue;
    const mod=load(id);
    // Observed xvn.a resolves the native service from its resource/container;
    // xvn.b is a React hook and must not be invoked outside the component.
    const resolver=/serviceContainer/.test(source)&&/resourceStore/.test(source)&&/chat-ioc-manager/.test(source)
      &&typeof mod?.a==='function'&&/^async\b/.test(String(mod.a))&&/\.load\(/.test(String(mod.a))
      &&!/useState|useMemo|useEffect/.test(String(mod.a))?mod.a:null;
    const candidate=mod?.D||mod?.K||resolver;
    if(typeof candidate==='function'){service={K:candidate};break;}
  }
  let messages;
  try{messages=await (await service?.K?.('chatIMService'))?.getMessageService?.();}catch{}
  if(!messages||typeof messages.sendMessage!=='function'||messages.sendMessage.__dolaImageOrderGuard)
    return {ok:false,reason:'send_guard'};
  // Native task.fileKey is a transport ID; task.key/commit URI is sent as image.uri.
  const keyOf=(a,t)=>t.key||t.uploadResult?.Uri||t.uploadResult?.ImageUri||t.uploadResult?.SourceUri||
    a.image?.uri||a.key||a.fileKey||t.fileKey||t.uploadResult?.FileKey||t.uploadResult?.file_key;
  const failed=t=>!!(t.error||t.failed||/^(failed|error|retry|rejected|cancelled|canceled)$/i.test(t.status||t.state||''));
  const loading=t=>!!(t.loading||/^(uploading|pending|loading|processing|preprocessing)$/i.test(t.status||t.state||''));
  let sendChecked=false,lastReason='',sentRows=null,rejectedRows=null,lastRequestSummary=null;
  const snapshot=()=>{
    const rows=attachments(),tasks=get('attachmentUploadStore')?.preprocessTaskMap||{};
    if(!Array.isArray(rows)||rows.length>expected.length)return {ready:false,error:true,reason:'count',rows:[]};
    if(rows.length<expected.length)return {ready:false,error:false,reason:'pending',rows:[]};
    const checked=[];
    for(let i=0;i<rows.length;i++){
      const a=rows[i],task=tasks[a.localKey]||{},name=a.fileName||a.file?.name,key=keyOf(a,task);
      if((a.type!=='image'&&Number(a.type)!==1)||name!==expected[i].filename||failed(task))
        return {ready:false,error:true,reason:'store_order',rows:[]};
      if(typeof key!=='string'||!key)return {ready:false,error:false,reason:'missing_key',rows:[]};
      if(loading(task))return {ready:false,error:false,reason:'loading',rows:[]};
      checked.push({...expected[i],key});
    }
    if(new Set(checked.map(x=>x.key)).size!==expected.length)return {ready:false,error:true,reason:'duplicate_key',rows:[]};
    return {ready:true,error:false,reason:'',rows:checked};
  };
  const digest=async value=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(value))))
    .map(x=>x.toString(16).padStart(2,'0')).join('');
  const summarizeRequest=async request=>{
    const fields=node=>node&&typeof node==='object'?Object.keys(node).slice(0,80):[];
    const valueType=value=>value===null?'null':Array.isArray(value)?'array':typeof value;
    const numeric=value=>['number','string'].includes(typeof value)&&Number.isFinite(Number(value))?Number(value):null;
    const ability=value=>{
      let parsed=value;try{if(typeof value==='string')parsed=JSON.parse(value);}catch{parsed=null;}
      return {valueType:valueType(value),fields:fields(parsed),abilityType:numeric(parsed?.ability_type)};
    };
    const blockFields=['content_blocks_v2','content_blocks','content_block'];
    const summary={topLevelKeys:fields(request),requestType:valueType(request),messagesIsArray:Array.isArray(request?.messages),
      messagesLength:Array.isArray(request?.messages)?request.messages.length:null,messages:[],chatAbilities:[],attachments:[],structures:[]};
    if(summary.messagesIsArray)summary.messages=request.messages.slice(0,30).map(message=>({keys:fields(message),
      extKeys:fields(message.ext),chatAbility:ability(message.ext?.chat_ability),
      blocks:blockFields.flatMap(field=>Array.isArray(message[field])?message[field].slice(0,40).map(block=>({
        collection:field,keys:fields(block),blockType:numeric(block.block_type),contentKeys:fields(block.content)})):[])}));
    const seen=new Set();let remaining=200;
    const walk=async(node,path,depth=0)=>{
      if(!node||typeof node!=='object'||depth>9||remaining--<=0||seen.has(node))return;
      seen.add(node);
      summary.structures.push({path,kind:valueType(node),...(Array.isArray(node)?{length:node.length}:{keys:fields(node)})});
      if(Array.isArray(node)){for(let i=0;i<Math.min(node.length,40);i++)await walk(node[i],path+'['+i+']',depth+1);return;}
      for(const field of fields(node)){
        // Do not inspect credential-bearing subtrees, even for their schema.
        if(/authorization|cookie|token|password|secret|credential|headers|proxy/i.test(field))continue;
        const value=node[field],location=path+'.'+field;
        if(field==='chat_ability')summary.chatAbilities.push({path:location,...ability(value)});
        if(field==='attachments'&&Array.isArray(value))summary.attachments.push({path:location,entries:await Promise.all(value.slice(0,40).map(async(item,i)=>{
          const uriCandidates=[];
          for(const [part,owner] of [['',item],['image',item?.image],['media',item?.media],['file',item?.file]])
            for(const key of ['uri','key','fileKey','file_key','ImageUri','SourceUri'])if(typeof owner?.[key]==='string'&&owner[key])
              uriCandidates.push({path:location+'['+i+']'+(part?'.'+part:'')+'.'+key,sha256:await digest(owner[key])});
          return {keys:fields(item),type:numeric(item?.type),imageKeys:fields(item?.image),uriCandidates};
        }))});
        if(typeof value==='object')await walk(value,location,depth+1);
        else if(blockFields.includes(field)&&typeof value==='string'){
          try{await walk(JSON.parse(value),location+'.parsed',depth+1);}catch{}
        }
      }
    };
    await walk(request,'request');return summary;
  };
  const evidence=async()=>{
    const state=sendChecked?{ready:true,error:false,reason:'',rows:sentRows}:
      lastReason&&rejectedRows?{ready:false,error:true,reason:lastReason,rows:rejectedRows}:snapshot();
    return {ready:state.ready,error:state.error,reason:lastReason||state.reason,sendChecked,
      lastRequestSummary:await lastRequestSummary,
      expectedCount:expected.length,entries:await Promise.all(state.rows.map(async(row,i)=>({
        index:i+1,filename:row.filename,sha256:row.sha256,upstreamKeySha256:await digest(row.key)})))};
  };
  const diagnostics=async()=>{
    const state=snapshot(),rows=attachments(),tasks=get('attachmentUploadStore')?.preprocessTaskMap||{};
    const candidates=async(node,prefix,depth=0)=>{
      if(!node||typeof node!=='object'||depth>4)return [];
      const result=[];
      for(const field of ['fileKey','key','FileKey','file_key','uri','Uri','ImageUri','SourceUri','resourceUri','mediaResourceUri']){
        const value=node[field];
        if(typeof value==='string'&&value)result.push({path:prefix+'.'+field,sha256:await digest(value)});
      }
      for(const field of ['image','media','uploadResult','result','Result','data'])
        result.push(...await candidates(node[field],prefix+'.'+field,depth+1));
      return result;
    };
    const atom=value=>typeof value==='number'||typeof value==='boolean'?value:
      typeof value==='string'&&/^[\w .-]{1,80}$/.test(value)?value:null;
    return {ready:state.ready,error:state.error,reason:lastReason||state.reason,expectedCount:expected.length,
      lastRequestSummary:await lastRequestSummary,
      rowCount:Array.isArray(rows)?rows.length:0,rows:await Promise.all((Array.isArray(rows)?rows:[]).map(async(a,i)=>{
        const task=tasks[a.localKey]||{},filename=a.fileName||a.file?.name;
        return {index:i+1,filename:expected.some(x=>x.filename===filename)?filename:null,type:atom(a.type),
          attachmentFields:Object.keys(a).sort(),taskFields:Object.keys(task).sort(),
          taskStatus:atom(task.status),taskState:atom(task.state),taskLoading:loading(task),
          keyCandidates:[...await candidates(a,'attachment'),...await candidates(task,'task')]};
      }))};
  };
  const stop=reason=>{lastReason=reason;sendChecked=false;throw Error('参考图身份或顺序未核对通过，已停止发送');};
  const originalSend=messages.sendMessage;
  const blocksOf=message=>message?.content_blocks_v2||message?.content_block;
  const abilityOf=message=>{
    const raw=message.ext?.chat_ability;if(raw==null)return 0;
    try{return Number((typeof raw==='string'?JSON.parse(raw):raw)?.ability_type);}catch{return NaN;}
  };
  const hasAttachment=node=>{
    if(Array.isArray(node))return node.some(hasAttachment);
    return !!node&&typeof node==='object'&&Object.entries(node).some(([key,value])=>
      (/^(attachments?|attachment_block|image)$/.test(key)&&value!=null&&(!Array.isArray(value)||value.length>0))||hasAttachment(value));
  };
  const send=function(request,...args){
    lastRequestSummary=summarizeRequest(request).catch(()=>({error:'summary_unavailable'}));
    const list=Array.isArray(request?.messages)?request.messages:[];
    if(sentRows){
      const blocks=blocksOf(list[0]),ability=list.length===1?abilityOf(list[0]):NaN;
      const text=blocks?.[0]?.content?.text_block?.text;
      if(Number.isFinite(ability)&&ability!==VIDEO&&blocks?.length===1&&Number(blocks[0].block_type)===10000&&
          typeof text==='string'&&/^Ja(?:, aber mit Dauer: (?:5|10|15|30) Sekunden)?$/.test(text.trim())&&!hasAttachment(request))
        return originalSend.call(this,request,...args);
      stop('repeat_or_non_confirmation');
    }
    const state=snapshot();if(!state.ready)stop(state.reason);
    rejectedRows=state.rows.map(row=>({...row}));
    const video=list.filter(message=>abilityOf(message)===VIDEO);
    let blocks;
    if(list.length===1&&video.length===1)blocks=blocksOf(video[0]);
    else if(list.length===2&&video.length===2){
      const first=blocksOf(list[0]),second=blocksOf(list[1]);
      if(!Array.isArray(first)||!Array.isArray(second)||first.length!==1||second.length!==1)stop('message_schema');
      blocks=[first[0],second[0]];
    }else stop('message_schema');
    if(!Array.isArray(blocks)||blocks.length!==2||Number(blocks[0].block_type)!==10052||
        Number(blocks[1].block_type)!==10000||typeof blocks[1].content?.text_block?.text!=='string'||hasAttachment(blocks[1]))
      stop('block_schema');
    const images=[];
    for(const block of blocks)if(Number(block.block_type)===10052){
      const items=block.content?.attachment_block?.attachments;
      if(!Array.isArray(items))stop('attachment_schema');
      for(const item of items){if(Number(item.type)!==1||typeof item.image?.uri!=='string')stop('image_schema');images.push(item.image.uri);}
    }
    if(images.length!==state.rows.length||images.some((key,i)=>key!==state.rows[i].key))stop('request_order');
    sentRows=state.rows.map(row=>({...row}));sendChecked=true;lastReason='';
    return originalSend.call(this,request,...args);
  };
  send.__dolaImageOrderGuard=true;
  messages.sendMessage=send;
  window.__dolaReferenceOrderGuard={status:()=>{
    const state=sendChecked?{ready:true,error:false,reason:''}:snapshot();
    return {ready:state.ready,error:state.error,reason:lastReason||state.reason,sendChecked};
  },evidence,diagnostics};
  return {ok:true};
}
