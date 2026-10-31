/* Adapted from the media-upload-main.js accompanying the supplied integration spec.
 * Configure the page's uploader and verify its store; never construct HTTP requests.
 */
async ({token,names,timeoutMs=120000}) => {
  const VIDEO=17,BLOCK=10052,types=['image','audio','video'];
  const extensions=['jpg','jpeg','png','webp','mp3','wav','m4a','aac','flac','ogg','mp4','mov','webm'];
  const roots=[document],inputs=[];
  for(let i=0;i<roots.length;i++)for(const el of roots[i].querySelectorAll('*')){
    if(el.shadowRoot)roots.push(el.shadowRoot);
    if(el.matches('input[type=file]'))inputs.push(el);
  }
  const input=inputs.find(el=>el.getAttribute('data-dola-ref-target')===token);
  const fiberKey=Object.keys(input||{}).find(key=>key.startsWith('__reactFiber$')||key.startsWith('__reactInternalInstance$'));
  let fiber=input?.[fiberKey],props;
  for(let i=0;fiber&&i<50;i++,fiber=fiber.return){
    const current=fiber.memoizedProps||fiber.pendingProps||{};
    if(current.chatId&&current.inputUploadConfig){props=current;break;}
  }
  if(!props||Number(props.skill?.skill_type)!==VIDEO)return {ok:false,reason:'context'};
  let runtime=window.__dolaReferenceRuntime;
  if(!runtime&&Array.isArray(window.__LOADABLE_LOADED_CHUNKS__)){
    window.__LOADABLE_LOADED_CHUNKS__.push([['dola-reference-'+token],{},value=>{runtime=value;}]);
    if(runtime)window.__dolaReferenceRuntime=runtime;
  }
  if(typeof runtime!=='function')return {ok:false,reason:'runtime'};
  const load=id=>{try{return runtime(id);}catch{return null;}};
  const findRunner=()=>{
    const known=load('IK');
    if(typeof known?.runConfiguredUploadPipeline==='function')return known.runConfiguredUploadPipeline;
    for(const [id,factory] of Object.entries(runtime.m||{})){
      const source=String(factory);
      if(!/inputUploadConfig/.test(source)||!/targetAttachmentChannel/.test(source)||!/uploadPipelineConfig/.test(source))continue;
      const mod=load(id),run=mod?.runConfiguredUploadPipeline||mod?.Z||mod?.f;
      if(typeof run==='function')return run;
    }
  };
  let runner=findRunner();
  if(!runner){
    const entries=Object.entries(window._MODERNJS_ROUTE_MANIFEST?.routeAssets||{})
      .filter(([key])=>/shared-upload-pipeline/i.test(key)).map(([,value])=>value);
    for(const id of [...new Set(entries.flatMap(e=>e.chunkIds||[]).concat('3rw'))]){
      try{await runtime.e?.(id);}catch{}
      runner=findRunner();if(runner)break;
    }
  }
  if(!runner)return {ok:false,reason:'upload_pipeline'};
  let store=load('qBy');
  if(typeof store?.Bc!=='function'){
    store=null;
    for(const [id,factory] of Object.entries(runtime.m||{})){
      if(!/attachmentsStore|attachmentUploadStore/.test(String(factory)))continue;
      const mod=load(id),get=mod?.Bc||mod?.GX;
      if(typeof get==='function'&&get('attachmentsStore')?.attachmentsMap){store={Bc:get};break;}
    }
  }
  if(!store)return {ok:false,reason:'store'};
  const get=name=>{try{return store.Bc(name)||{};}catch{return {};}};
  const attachments=()=>get('attachmentsStore').attachmentsMap?.[props.chatId]?.['_'+VIDEO]||[];
  const baseline=new Set(attachments().map(a=>a.localKey));
  const kind=file=>{
    const mime=String(file?.type||'').split('/')[0];if(types.includes(mime))return mime;
    const ext=String(file?.name||'').split('.').pop().toLowerCase();
    return /^(mp3|wav|m4a|aac|flac|ogg)$/.test(ext)?'audio':/^(mp4|mov|webm)$/.test(ext)?'video':/^(jpg|jpeg|png|webp)$/.test(ext)?'image':'';
  };
  const widen=config=>{
    if(!config||typeof config!=='object')throw Error('config');
    // Copy only this video composer's config; keep the site's upload implementation.
    return {...config,acceptedAttachmentTypes:types,acceptedFileExtensions:extensions,
      maxCount:Math.max(Number(config.maxCount)||0,names.length),
      addAttachmentType:({file}={})=>types.includes(kind(file))?kind(file):'file',
      isReusableAttachment:attachment=>types.includes(attachment?.type)};
  };
  const inputConfig=props.inputUploadConfig;
  if(typeof inputConfig.uploadPipelineConfig!=='function')return {ok:false,reason:'config'};
  let pipelineConfig=await inputConfig.uploadPipelineConfig();
  if(!pipelineConfig)return {ok:false,reason:'config'};
  pipelineConfig=widen(pipelineConfig);
  if(!inputConfig.uploadPipelineConfig.__dolaReferenceMedia){
    const original=inputConfig.uploadPipelineConfig;
    const config=await original.call(inputConfig);
    if(!config)return {ok:false,reason:'config'};
    // Some current components capture the object before the input change event.
    Object.assign(config,widen(config));
    const wrapped=async function(...args){return widen(await original.apply(this,args));};
    wrapped.__dolaReferenceMedia=true;
    inputConfig.uploadPipelineConfig=wrapped;
  }
  if(!window.__dolaReferenceUploadUrls){
    window.__dolaReferenceUploadUrls=true;
    const normalize=url=>{
      const text=String(url||''),match=text.match(/^(?:https?:\/\/www\.dola\.com\/chat\/)?(vod-[a-z0-9.-]+\.bytevcloudapi\.com)(\/?\?.*)$/i);
      return match?'https://'+match[1]+match[2]:text;
    };
    const fetch=window.fetch;
    window.fetch=function(input,init){
      const before=typeof input==='string'?input:input?.url||String(input),after=normalize(before);
      if(after!==before)input=input instanceof Request?new Request(after,input):after;
      return fetch.call(this,input,init);
    };
    const open=XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open=function(method,url,...rest){return open.call(this,method,normalize(url),...rest);};
  }
  const attachmentKind=a=>types.includes(a?.type)?a.type:Number(a?.type)===1?'image':
    Number(a?.type)===2?Number(a.media?.media_type)===1?'audio':'video':'';
  const failed=task=>!!(task?.error||task?.failed||/^(failed|error|retry|rejected|cancelled|canceled)$/i.test(task?.status||task?.state||''));
  const loading=task=>!!(task?.loading||/^(uploading|pending|loading|processing|preprocessing)$/i.test(task?.status||task?.state||''));
  const fileKey=(a,t)=>a.key||a.fileKey||a.media?.key||t.key||t.fileKey||t.uploadResult?.FileKey||t.uploadResult?.file_key||t.uploadResult?.media?.key;
  const snapshot=()=>{
    const rows=attachments().filter(a=>!baseline.has(a.localKey));
    const tasks=get('attachmentUploadStore').preprocessTaskMap||{};
    let pending=0,error=false;const counts={image:0,audio:0,video:0},keys=[],states=[];
    for(const a of rows){
      const t=tasks[a.localKey]||{},type=attachmentKind(a),key=fileKey(a,t),result=t.uploadResult||{},meta=result.VideoMeta||{},extra=t.ctx?.extraParams||{};
      if(type in counts)counts[type]++;
      if(!key||loading(t))pending++;
      if(failed(t))error=true;
      keys.push(key);
      states.push({type,localKey:a.localKey,fileKey:key,fileName:a.fileName||a.file?.name||meta.FileName||'',
        size:Number(a.size||a.file?.size||meta.Size||0),md5:a.md5||t.md5||meta.Md5,
        mediaDuration:Number(a.mediaDuration||meta.Duration||extra.duration_seconds||0),
        mediaResourceUri:a.mediaResourceUri||t.mediaResourceUri,mediaCoverUrl:a.mediaCoverUrl||extra.cover_local_url||t.blobUrl||'',
        mediaCoverWidth:Number(a.mediaCoverWidth||extra.cover_width||meta.Width||0),mediaCoverHeight:Number(a.mediaCoverHeight||extra.cover_height||meta.Height||0),
        imageWidth:Number(a.imageWidth||result.ImageWidth||0),imageHeight:Number(a.imageHeight||result.ImageHeight||0),
        url:a.url||t.blobUrl||'',src:a.src||t.blobUrl||'',uploadPercent:Number(t.percent)||100,status:failed(t)?'Retry':loading(t)?'Uploading':'Normal'});
    }
    return {rows,keys,states,counts,pending,error};
  };
  const countBlocks=blocks=>{
    const counts={image:0,audio:0,video:0};
    for(const block of blocks||[])if(Number(block.block_type)===BLOCK)
      for(const a of block.content?.attachment_block?.attachments||[]){const type=attachmentKind(a);if(type in counts)counts[type]++;}
    return counts;
  };
  const covers=(a,b)=>types.every(type=>(a[type]||0)>=(b[type]||0));
  const buildBlock=snap=>{
    for(const [id,factory] of Object.entries(runtime.m||{})){
      const source=String(factory);
      if(!/attachmentStates/.test(source)||!/attachmentKeys/.test(source)||!/attachment_block/.test(source))continue;
      const module=load(id),args={attachments:snap.rows,attachmentKeys:snap.keys,attachmentStates:snap.states};
      const candidates=[module?.S,...Object.values(module||{}).filter(fn=>typeof fn==='function'&&
        /attachmentKeys/.test(String(fn))&&/attachmentStates/.test(String(fn))&&!/useMemo|useEffect|\.jsx/.test(String(fn)))];
      for(const fn of new Set(candidates)){
        if(typeof fn!=='function')continue;
        try{const block=fn(args);if(block&&covers(countBlocks([block]),snap.counts))return block;}catch{}
      }
    }
  };
  let service=load('nR1');
  if(typeof service?.K!=='function'){
    for(const [id,factory] of Object.entries(runtime.m||{})){
      if(!/serviceContainer|StoreService/.test(String(factory)))continue;
      const m=load(id);if(typeof (m?.D||m?.K)==='function'){service={K:m.D||m.K};break;}
    }
  }
  let messages;
  try{messages=await (await service?.K?.('chatIMService'))?.getMessageService?.();}catch{}
  if(!messages||typeof messages.sendMessage!=='function')return {ok:false,reason:'submit_guard'};
  const originalSend=messages.sendMessage;
  if(originalSend.__dolaReferenceGuard)return {ok:false,reason:'existing_guard'};
  const send=function(request,...args){
    const list=Array.isArray(request?.messages)?request.messages:[];
    const index=list.findIndex(m=>{try{const ability=typeof m.ext?.chat_ability==='string'?JSON.parse(m.ext.chat_ability):m.ext?.chat_ability;return Number(ability?.ability_type)===VIDEO;}catch{return false;}});
    if(index<0)return originalSend.call(this,request,...args);
    const snap=snapshot();
    if(snap.pending||snap.error||snap.rows.length!==names.length)throw Error('参考素材尚未全部就绪，已停止发送');
    const blocks=list[index].content_blocks_v2||[];
    if(covers(countBlocks(blocks),snap.counts))return originalSend.call(this,request,...args);
    // Use the site's own attachment block builder before its native signing/send.
    const block=buildBlock(snap);
    if(!block||!covers(countBlocks([block]),snap.counts))throw Error('参考音视频未进入生成请求，已停止发送');
    const content=blocks.filter(b=>Number(b.block_type)!==BLOCK);
    const copied=list.map((m,i)=>i===index?{...m,content_blocks_v2:[block,...content]}:m);
    return originalSend.call(this,{...request,messages:copied},...args);
  };
  send.__dolaReferenceGuard=true;
  messages.sendMessage=send;
  // A dedicated CDP input keeps the site's image-only prefilter from silently
  // dropping media; the actual uploads still run in its current native pipeline.
  input.removeAttribute('data-dola-ref-target');
  const bridgeInput=document.createElement('input');
  bridgeInput.type='file';bridgeInput.multiple=true;bridgeInput.hidden=true;
  bridgeInput.setAttribute('data-dola-ref-target',token);document.body.append(bridgeInput);
  let runError=false,started=false;
  const duration=file=>new Promise(resolve=>{
    const type=kind(file);if(type==='image')return resolve(0);
    const el=document.createElement(type),url=URL.createObjectURL(file);let settled=false;
    const finish=value=>{if(settled)return;settled=true;clearTimeout(timer);URL.revokeObjectURL(url);resolve(Number.isFinite(value)?value:0);};
    const timer=setTimeout(()=>finish(0),5000);el.preload='metadata';
    el.onloadedmetadata=()=>finish(el.duration);el.onerror=()=>finish(0);el.src=url;
  });
  const start=async()=>{
    if(started)return;started=true;
    try{
      const files=Array.from(bridgeInput.files||[]);
      if(files.length!==names.length)throw Error('files');
      const deadline=Date.now()+timeoutMs;
      for(const file of files){
        const before=new Set(attachments().map(a=>a.localKey));
        const seconds=await duration(file),originalContext=pipelineConfig.buildAttachmentCtx;
        await runner({inputUploadConfig:inputConfig,trigger:'click',chatId:props.chatId,
          chatType:props.chatType,skill:props.skill,reportParams:props.reportParams,files:[file],
          inputConfigKey:props.inputConfigKey,attachmentChannel:VIDEO,targetAttachmentChannel:VIDEO,
          uploadPipelineConfig:{...pipelineConfig,buildAttachmentCtx:async args=>{
            const base=typeof originalContext==='function'?await originalContext(args):{
              conversationId:props.chatId,chatType:'other_default',skillType:VIDEO,skillId:String(VIDEO),
              from:'',uploadCollectionWay:'single',uploadId:'',uploadWay:'browse_folder',uploadFrom:'video_generation',
              uploadCollectionId:args.uploadCollectionId,uploadType:file.type};
            return {...base,extraParams:{...base.extraParams,...seconds>0?{duration_seconds:String(seconds)}:{}}};
          }}});
        let finished=false;
        while(Date.now()<deadline){
          const a=attachments().find(a=>!before.has(a.localKey)&&(a.fileName||a.file?.name||file.name)===file.name);
          const task=get('attachmentUploadStore').preprocessTaskMap?.[a?.localKey]||{};
          if(failed(task))throw Error('upload');
          if(a&&fileKey(a,task)&&!loading(task)){finished=true;break;}
          await new Promise(resolve=>setTimeout(resolve,250));
        }
        if(!finished)throw Error('timeout');
      }
    }catch{runError=true;}
  };
  window.__dolaReferenceMediaBridge={status:()=>{
    const s=snapshot(),uploaded=s.rows.length===names.length&&!s.pending&&!s.error;
    let referencesReady=false;
    if(uploaded){try{const draft=buildBlock(s);referencesReady=!!draft&&covers(countBlocks([draft]),s.counts);}catch{}}
    return {count:s.rows.length,pending:s.pending,error:s.error||runError||uploaded&&!referencesReady,counts:s.counts,
      referencesReady,ready:uploaded&&referencesReady&&!runError};
  },start};
  return {ok:true};
}
