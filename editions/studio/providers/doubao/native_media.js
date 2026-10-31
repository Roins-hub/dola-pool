/* Doubao legacy video-17 adapter. Never rewrites a signed request or its URL. */
(() => {
  'use strict';
  if (location.protocol !== 'https:' || !/(^|\.)doubao\.com$/.test(location.hostname)
      || globalThis.__DOUBAO_NATIVE_MEDIA__) return;
  const ATTR = 'data-doubao-native-media-status', COMMAND = 'data-doubao-native-media-command';
  const EVENT = 'doubao-native-media-command', VIDEO = 17, BLOCK = 10052;
  const record = value => value && typeof value === 'object' && !Array.isArray(value);
  const json = value => {try {return typeof value === 'string' ? JSON.parse(value) : value} catch {return null}};
  const clone = value => JSON.parse(JSON.stringify(value));
  let preference = globalThis.__DOUBAO_NATIVE_MEDIA_PREFERENCE__ || 'auto';
  let runtime, bindings = {}, expected = [], beforeKeys = [], chatId = null, uploadSignature = '';
  const wrappedServices = new WeakMap(), wrappedNodes = new WeakMap(), wrappedFactories = new WeakMap();
  const state = {supported:false, reason_code:'not_prepared', ready:false, pending:0, errors:[],
    before_keys:[], counts:{image:0,video:0,audio:0}, resolution:preference,
    selection_ready:false, media_ready:false, upload_pipeline_ready:false, upload_input_authorized:false,
    last_request:null, inserted_blocks:0};
  function fault(code) {const e = new Error(code);e.code=code;throw e}
  function publicState() {return clone(state)}
  function publish() {document.documentElement?.setAttribute(ATTR, JSON.stringify(publicState()));return publicState()}
  function getRuntime() {
    if (typeof runtime === 'function' && runtime.m) return runtime;
    const queues = [globalThis.__LOADABLE_LOADED_CHUNKS__];
    for (const key of Object.keys(globalThis)) if (/^webpackChunk/.test(key)) queues.push(globalThis[key]);
    for (const queue of queues) {
      if (!Array.isArray(queue)) continue;
      let found;
      try {queue.push([[`doubao-native-media-${Date.now()}`],{},require=>{found=require}])} catch {continue}
      if (typeof found === 'function' && found.m) return (runtime=found);
    }
    return null;
  }
  function objects(root, callback, depth=0, seen=new WeakSet()) {
    if ((!record(root) && !Array.isArray(root)) || depth>4 || seen.has(root)) return;
    seen.add(root);callback(root);
    for (const descriptor of Object.values(Object.getOwnPropertyDescriptors(root)).slice(0,100))
      if ('value' in descriptor && descriptor.value && typeof descriptor.value==='object') objects(descriptor.value,callback,depth+1,seen);
  }
  function inputApi(exports) {
    if (['qM','gl','N5'].every(key=>typeof exports?.[key]==='function')) return exports;
    if (exports?.a !== '__input_action_control_values__' || !['n','h','f'].every(key=>typeof exports[key]==='function')) return null;
    if (!String(exports.n).includes('chatInputStore') || !String(exports.n).includes('.setState(')
        || !String(exports.h).includes('chatInputStore')) return null;
    return {qM:(key,value)=>exports.n(key,value),gl:key=>exports.h(key),N5:key=>exports.f(key)};
  }
  function discover() {
    const require=getRuntime(), found={nodes:[],services:[],configs:[],builders:[],stores:[],uploads:[],input:null};
    if (!require) fault('runtime_unavailable');
    for (const [id,factory] of Object.entries(require.m).slice(0,15000)) {
      const source=String(factory);
      const stores=/attachmentsMap|preprocessTaskMap/.test(source);
      const builder=/attachmentStates/.test(source)&&/attachmentKeys/.test(source)&&/attachment_block/.test(source);
      const pipeline=source.includes('video-creation:input-skill-node');
      const service=/sendMessage/.test(source)&&/messageService|content_blocks_v2|chat_ability/.test(source);
      const input=source.includes('__input_action_control_values__')&&source.includes('chatInputStore');
      const config=source.includes('uploadPipelineConfig');
      if (!stores&&!builder&&!pipeline&&!service&&!input&&!config) continue;
      let exports;try {exports=require(id)} catch {continue}
      if (input) found.input=inputApi(exports)||found.input;
      if (builder&&typeof exports?.S==='function') found.builders.push(exports.S);
      if (builder) for (const candidate of Object.values(exports||{})) {
        const text=String(candidate);
        if (typeof candidate==='function'&&text.includes('attachmentKeys')&&text.includes('attachmentStates')&&text.includes('attachment_block')) found.builders.push(candidate);
      }
      objects(exports,node=>{
        if (stores&&record(node.attachmentsMap)) found.stores.push(node);
        if (stores&&record(node.preprocessTaskMap)) found.uploads.push(node);
        if (pipeline&&Array.isArray(node.submitNodes)) for (const item of node.submitNodes)
          if (item?.id==='video-creation:input-skill-node'&&typeof item.execute==='function') found.nodes.push(item);
        if (service&&typeof node.sendMessage==='function') found.services.push(node);
        if (config&&typeof node.uploadPipelineConfig==='function') found.configs.push(node);
      });
    }
    for (const key of ['nodes','services','configs','builders','stores','uploads']) found[key]=[...new Set(found[key])];
    if (!found.nodes.length) fault('video_pipeline_unavailable');
    if (expected.length && ['services','builders','stores','uploads','configs'].some(key=>found[key].length!==1)) fault('attachment_pipeline_unavailable');
    bindings=found;
  }
  function authorizeInput(selector) {
    refresh(false);state.upload_input_authorized=false;
    if (!state.supported||!expected.length||!state.upload_pipeline_ready) return publish();
    let candidates;try {candidates=document.querySelectorAll(selector)}catch{return publish()}
    if (candidates.length!==1||candidates[0].tagName!=='INPUT'||candidates[0].type!=='file') return publish();
    const input=candidates[0], config=bindings.configs[0];let proven=false;
    if (config.input===input||config.fileInput===input||config.inputRef?.current===input) proven=true;
    const inspect=props=>objects(props,value=>{
      if (Number(value.skill?.skill_type??value.skill?.ability_type)!==VIDEO) return;
      if (value.inputUploadConfig===config) proven=true;
    });
    for(let element=input,level=0;element&&level<12;element=element.parentElement,level++) {
      for(const key of Object.keys(element)) {
        if(key.startsWith('__reactProps$'))inspect(element[key]);
        if(key.startsWith('__reactFiber$'))for(let fiber=element[key],hop=0;fiber&&hop<80;fiber=fiber.return,hop++) {
          inspect(fiber.memoizedProps);inspect(fiber.pendingProps);
        }
      }
      if(proven)break;
    }
    if (!proven) return publish();
    const kinds=[...new Set(expected.map(item=>item.kind))];
    input.accept=[...new Set([...(input.accept||'').split(',').filter(Boolean),...kinds.map(kind=>kind+'/*')])].join(',');
    input.multiple=true;state.upload_input_authorized=true;return publish();
  }
  function selectResolution() {
    state.selection_ready=preference==='auto';
    if (preference==='auto') return;
    const input=bindings.input;
    if (!input) fault('resolution_store_unavailable');
    const previous=input.N5('video-resolution');
    if (!record(previous)) fault('resolution_record_unavailable');
    if (String(input.gl('video-resolution'))!==preference || previous.value!==preference
        || previous.submitKey!=='resolution' || previous.messageText!=='')
      input.qM('video-resolution',{...previous,value:preference,submitKey:'resolution',messageText:''});
    const selected=input.N5('video-resolution');
    state.selection_ready=String(input.gl('video-resolution'))===preference && selected?.value===preference
      && selected.submitKey==='resolution' && selected.messageText==='';
    if (!state.selection_ready) fault('resolution_readback_failed');
  }
  function kind(item) {
    if (['image','video','audio'].includes(item?.type)) return item.type;
    if (Number(item?.type)===1) return 'image';
    if (Number(item?.type)===2 && Number(item?.media?.media_type)===1) return 'audio';
    if (Number(item?.type)===2 && Number(item?.media?.media_type)===2) return 'video';
    return null;
  }
  const keyOf = item => item?.attachmentKey??item?.attachment_key??item?.image?.key??item?.media?.key??null;
  function scope() {
    const map=bindings.stores[0]?.attachmentsMap;
    if (!record(map)) fault('attachment_store_unavailable');
    const scopes=Object.entries(map).filter(([,value])=>value&&Object.hasOwn(value,'_17'));
    const selected=chatId ? scopes.find(([key])=>key===chatId) : scopes.length===1?scopes[0]:null;
    if (!selected) fault('attachment_chat_ambiguous');
    const entries=selected[1]._17;
    if (!record(entries)&&!Array.isArray(entries)) fault('attachment_store_schema_changed');
    return Object.entries(entries).map(([key,value])=>({localKey:String(value?.localKey??value?.local_key??key),value}));
  }
  function snapshot() {
    const all=scope(), fresh=all.filter(item=>!beforeKeys.includes(item.localKey));
    const uploads=bindings.uploads[0].preprocessTaskMap, counts={image:0,video:0,audio:0};
    const entries=[];let pending=0;const errors=[];
    for (const {localKey,value} of fresh) {
      const type=kind(value), name=value.name??value.file?.name??value.fileName??'', key=keyOf(value);
      const task=uploads[localKey], status=String(task?.status??task?.state??'').toLowerCase();
      const failed=Boolean(task?.failed||task?.error)||/failed|error|cancelled|canceled/.test(status);
      const knownDone=/^(success|succeeded|completed|complete|done|ready)$/.test(status);
      const loading=Boolean(task?.loading||task?.pending)||Boolean(task&&!knownDone)
        || typeof key!=='string'||!key;
      if (!type) errors.push('attachment_kind_unknown'); else counts[type]++;
      if (failed) errors.push('attachment_upload_failed');
      if (loading) pending++;
      entries.push({kind:type,name:typeof name==='string'?name:'',key,localKey,value});
    }
    if (fresh.length>expected.length) errors.push('unexpected_attachments');
    for (let index=0;index<Math.min(entries.length,expected.length);index++)
      if (entries[index].kind!==expected[index].kind || (entries[index].name&&entries[index].name!==expected[index].name)) errors.push('attachment_order_mismatch');
    if (new Set(entries.map(item=>item.key).filter(Boolean)).size<entries.filter(item=>item.key).length) errors.push('duplicate_attachment_keys');
    return {all,entries,counts,pending,errors:[...new Set(errors)],ready:entries.length===expected.length&&pending===0&&errors.length===0};
  }
  function requestAbility(message) {
    const ability=json(message?.ext?.chat_ability);
    return Number(ability?.ability_type)===VIDEO ? ability : null;
  }
  function observe(body) {
    try {
      const request=json(body);
      if (!Array.isArray(request?.messages)) return;
      for (const message of request.messages) {
        const ability=requestAbility(message), params=json(ability?.ability_param);
        if (!record(params)) continue;
        const safe={};
        for (const key of ['model','duration','resolution','ratio'])
          if (typeof params[key]==='string'||(typeof params[key]==='number'&&Number.isFinite(params[key]))) safe[key]=typeof params[key]==='string'?params[key].slice(0,120):params[key];
        state.last_request=safe;
      }
      publish();
    } catch { /* Read-only observation must never interfere with a send. */ }
  }
  function attachmentEntries(blocks) {
    const found=[];
    function visit(value,depth=0) {
      if (depth>12||value==null) return;
      const parsed=json(value);
      if (parsed!==value) {visit(parsed,depth+1);return}
      if (Array.isArray(value)) {for(const child of value)visit(child,depth+1);return}
      if (!record(value)) return;
      const type=kind(value);
      if (type) {found.push({kind:type,key:keyOf(value)});return}
      for (const child of Object.values(value)) if (child&&typeof child==='object'||typeof child==='string'&&/^[\[{]/.test(child)) visit(child,depth+1);
    }
    for (const block of blocks) if (Number(block?.block_type)===BLOCK) visit(block);
    return found;
  }
  function sameEntries(entries,snap) {
    return entries.length===snap.entries.length && entries.every((item,index)=>item.kind===snap.entries[index].kind
      &&typeof item.key==='string'&&item.key===snap.entries[index].key);
  }
  function patchSend(request) {
    observe(request);
    if (!expected.length) return request;
    refresh(false);
    if (!state.supported) fault(state.reason_code);
    const snap=snapshot();
    if (!snap.ready) fault(snap.errors[0]||'attachment_upload_pending');
    if (!Array.isArray(request?.messages)) fault('video_message_unavailable');
    if (request.signed===true || ['a_bogus','msToken','fp'].some(key=>Object.hasOwn(request,key))
        || (typeof request.url==='string'&&/[?&](?:a_bogus|msToken|fp)=/.test(request.url))) fault('signed_request_not_mutable');
    const indices=request.messages.flatMap((message,index)=>requestAbility(message)?[index]:[]);
    if (indices.length!==1) fault('video_message_ambiguous');
    const index=indices[0], message=request.messages[index], blocks=message.content_blocks_v2||[];
    if (!Array.isArray(blocks)) fault('attachment_block_schema_changed');
    const existing=blocks.filter(block=>Number(block?.block_type)===BLOCK);
    if (existing.length) {
      if (existing.length!==1||!sameEntries(attachmentEntries(existing),snap)) fault('native_attachment_mismatch');
      return request; // Already complete: no duplicate reference block.
    }
    const attachmentKeys=snap.entries.map(item=>item.key), attachments=snap.entries.map(item=>item.value);
    const generated=bindings.builders[0]({attachments,attachmentKeys,attachmentStates:attachments,content:attachmentKeys});
    if (!record(generated)||Number(generated.block_type)!==BLOCK||!sameEntries(attachmentEntries([generated]),snap)) fault('native_builder_mismatch');
    const messages=request.messages.slice();
    messages[index]={...message,content_blocks_v2:[generated,...blocks]};
    state.inserted_blocks++;publish();
    return {...request,messages};
  }
  function installPipeline() {
    for (const node of bindings.nodes) {
      if (wrappedNodes.get(node)===node.execute) continue;
      const original=node.execute;
      function checked(context,...args) {
        if (context?.content?.abilityType!=null&&Number(context.content.abilityType)!==VIDEO) return original.call(this,context,...args);
        if (preference!=='auto'&&!state.selection_ready) fault('resolution_selection_changed');
        const result=original.call(this,context,...args);
        function finish(output) {
          if (preference!=='auto' && (Number(output?.content?.abilityType)!==VIDEO
              || output?.content?.abilityParams?.resolution!==preference)) fault('resolution_native_output_mismatch');
          return output;
        }
        return result&&typeof result.then==='function'?result.then(finish):finish(result);
      }
      node.execute=checked;wrappedNodes.set(node,checked);
    }
    for (const service of bindings.services) {
      if (wrappedServices.get(service)===service.sendMessage) continue;
      const original=service.sendMessage;
      function beforeSigning(request,...args) {return original.call(this,patchSend(request),...args)}
      service.sendMessage=beforeSigning;wrappedServices.set(service,beforeSigning);
    }
    const kinds=[...new Set(expected.map(item=>item.kind))], signature=kinds.slice().sort().join(',');
    const extensions={image:['jpg','jpeg','png','webp','apng'],video:['mp4','mov'],audio:['mp3','wav','m4a','aac','flac','ogg']};
    for (const config of (kinds.length?bindings.configs:[])) {
      const installed=wrappedFactories.get(config);
      if (installed?.wrapper===config.uploadPipelineConfig&&installed.signature===signature) continue;
      const original=installed?.wrapper===config.uploadPipelineConfig?installed.original:config.uploadPipelineConfig;
      async function factory(...args) {
        const native=await original.apply(this,args);
        if (!record(native)||!Array.isArray(native.acceptedAttachmentTypes)||!Array.isArray(native.acceptedFileExtensions)) fault('upload_config_schema_changed');
        return {...native,acceptedAttachmentTypes:[...new Set([...native.acceptedAttachmentTypes,...kinds])],
          acceptedFileExtensions:[...new Set([...native.acceptedFileExtensions,...kinds.flatMap(kind=>extensions[kind])])],
          maxCount:Math.min(50,Number(native.maxCount)||50)};
      }
      config.uploadPipelineConfig=factory;wrappedFactories.set(config,{wrapper:factory,original,signature});
    }
    uploadSignature=signature;
    state.upload_pipeline_ready=Boolean(expected.length&&bindings.configs.length===1
      &&wrappedFactories.get(bindings.configs[0])?.wrapper===bindings.configs[0].uploadPipelineConfig);
  }
  function refresh(select=false) {
    state.supported=false;state.ready=false;state.errors=[];state.reason_code='';state.resolution=preference;
    try {
      discover();
      if (select) selectResolution();
      else if (preference!=='auto') {
        const selected=bindings.input?.N5('video-resolution');
        state.selection_ready=selected?.value===preference&&String(bindings.input?.gl('video-resolution'))===preference;
        if (!state.selection_ready) fault('resolution_selection_changed');
      } else state.selection_ready=true;
      installPipeline();
      if (expected.length) {
        const snap=snapshot();state.counts=snap.counts;state.pending=snap.pending;state.errors=snap.errors;state.media_ready=snap.ready;
      } else {state.counts={image:0,video:0,audio:0};state.pending=0;state.media_ready=true}
      state.supported=true;state.ready=state.selection_ready&&state.media_ready;
      if (state.errors.length) state.reason_code=state.errors[0];
      else if (!state.media_ready) state.reason_code='attachment_upload_pending';
    } catch(error) {state.reason_code=error.code||'native_pipeline_unsupported';state.media_ready=false}
    return publish();
  }
  function prepare(options={}) {
    preference=['auto','720p','1080p'].includes(options.resolution)?options.resolution:'auto';
    expected=Array.isArray(options.expected)?options.expected.map(item=>({kind:item.kind,name:item.name})):[];
    if (expected.length>50||expected.some(item=>!['image','video','audio'].includes(item.kind)||typeof item.name!=='string'||/[\x00-\x1f\x7f/\\]/.test(item.name))) fault('invalid_expected_attachments');
    if (Object.hasOwn(options,'chat_id'))chatId=typeof options.chat_id==='string'?options.chat_id:null;
    beforeKeys=Array.isArray(options.before_keys)?options.before_keys.slice():[];
    try {discover();if (!Array.isArray(options.before_keys)&&expected.length) beforeKeys=scope().map(item=>item.localKey)} catch {}
    state.before_keys=beforeKeys.slice();return refresh(true);
  }
  function optionValue(option) {return String(option?.option_key??option?.value??'').toLowerCase()}
  function patchCapability(value) {
    if (preference==='auto') return {value,changed:false};
    let changed=false;
    function resolutionControl(adjacent) {
      const control={...adjacent};
      for (const key of Object.keys(control)) if (/bound|step|duration|model|icon|badge|subscription|message_text|placeholder|option_list|options/i.test(key)) delete control[key];
      if (adjacent.key) {
        control.key='video-resolution';control.option_list=[{id:1,option_key:preference,display_text:preference,message_text:'',is_default:true}];
      } else {control.value='resolution';control.options=[{value:preference,show_name:preference,is_default:true,sub_display:''}]}
      for(const key of ['submit_key','param_key','submitKey','paramKey'])if(Object.hasOwn(control,key))control[key]='resolution';
      control.label='分辨率';control.show_name='分辨率';
      for(const key of ['hidden','is_hidden','isHidden'])control[key]=false;
      for(const key of ['visible','is_visible','isVisible'])control[key]=true;
      return control;
    }
    function visit(node,video=false,depth=0) {
      if (depth>24||node==null) return;
      if (Array.isArray(node)) {
        const duration=node.findIndex(item=>item?.key==='video-duration');
        if(duration>=0&&!node.some(item=>item?.key==='video-resolution')) {
          node.splice(duration+1,0,resolutionControl(node[duration]));changed=true;
        }
        for (const item of node)visit(item,video,depth+1);return;
      }
      if (!record(node)) return;
      if (node.ability_type!=null||node.skill_type!=null) video=Number(node.ability_type??node.skill_type)===VIDEO;
      if (node.key==='video-resolution'||node.key==='video-duration') video=true;
      if (video&&record(node.model_capability)) for (const cap of Object.values(node.model_capability)) {
        if (record(cap)&&Array.isArray(cap.supported_resolutions)&&!cap.supported_resolutions.includes(preference)) {
          cap.supported_resolutions.push(preference);changed=true;
        }
      }
      if (video) for (const [arrayKey,legacy] of [['option_list',false],['options',true]]) {
        const options=node[arrayKey];
        if (!Array.isArray(options)||!(node.key==='video-resolution'||node.value==='resolution')) continue;
        let index=options.findIndex(option=>optionValue(option)===preference);
        if (index<0) {
          const id=Math.max(0,...options.map(option=>Number(option.id)||0))+1;
          options.push(legacy?{value:preference,show_name:preference,is_default:false,sub_display:''}:
            {id,option_key:preference,display_text:preference,message_text:''});index=options.length-1;
        }
        options.forEach((option,i)=>{option.is_default=i===index});
        node.default_selection_index=index;
        if (options[index].id!=null) node.default_selection_id=options[index].id;
        node.default_selected_resolution=preference;changed=true;
      }
      if (video&&record(node.meta)&&Array.isArray(node.meta.option_list)) {
        const controls=node.meta.option_list, has=controls.some(control=>control.value==='resolution'||control.key==='video-resolution');
        const duration=controls.findIndex(control=>control.value==='duration'||control.key==='video-duration');
        if (!has&&duration>=0) {
          const control=resolutionControl(controls[duration]);
          controls.splice(duration+1,0,control);changed=true;
        }
      }
      for (const [key,child] of Object.entries(node)) {
        const childVideo=key==='video_generation'?true:/(?:image|audio)_generation/.test(key)?false:video;
        if (typeof child==='string'&&/^[\[{]/.test(child.trim())) {
          try {const parsed=JSON.parse(child), before=JSON.stringify(parsed);visit(parsed,childVideo,depth+1);if(JSON.stringify(parsed)!==before)node[key]=JSON.stringify(parsed)} catch {}
        } else if (child&&typeof child==='object') visit(child,childVideo,depth+1);
      }
    }
    const output=clone(value);visit(output);return {value:output,changed};
  }
  const capabilityUrl = input => {try {const url=new URL(typeof input==='string'?input:input.url,location.href);
    return url.protocol==='https:'&&/(^|\.)doubao\.com$/.test(url.hostname)
      &&['/samantha/skill/pack','/alice/slot/action_bar_v3/get_item_conf'].includes(url.pathname.replace(/\/$/,''))}catch{return false}};
  const completionUrl = input => {try {const url=new URL(typeof input==='string'?input:input.url,location.href);
    return /(^|\.)doubao\.com$/.test(url.hostname)&&url.pathname==='/chat/completion'}catch{return false}};
  function patchText(raw) {try {const result=patchCapability(JSON.parse(raw));return result.changed?JSON.stringify(result.value):raw}catch{return raw}}
  const originalFetch=globalThis.fetch;
  globalThis.fetch=function(input,...args) {
    if(completionUrl(input)&&typeof args[0]?.body==='string')observe(args[0].body);
    const response=originalFetch.call(this,input,...args);
    if(preference==='auto'||!capabilityUrl(input))return response;
    return Promise.resolve(response).then(async native=>{
      if(!native.ok)return native;
      try {const raw=await native.clone().text(),updated=patchText(raw);if(updated===raw)return native;
        const headers=new Headers(native.headers);headers.delete('content-length');headers.delete('content-encoding');
        const result=new Response(updated,{status:native.status,statusText:native.statusText,headers});
        for(const key of ['url','redirected','type'])Object.defineProperty(result,key,{value:native[key]});return result;
      }catch{return native}
    });
  };
  const proto=XMLHttpRequest.prototype, originalOpen=proto.open, originalSend=proto.send;
  const textGetter=Object.getOwnPropertyDescriptor(proto,'responseText')?.get,responseGetter=Object.getOwnPropertyDescriptor(proto,'response')?.get;
  const xhrUrls=new WeakMap();
  proto.open=function(method,url,...args) {
    xhrUrls.set(this,url);
    if(textGetter)Object.defineProperty(this,'responseText',{configurable:true,get:()=>{
      const raw=textGetter.call(this);return this.readyState===4&&capabilityUrl(url)?patchText(raw):raw}});
    if(responseGetter)Object.defineProperty(this,'response',{configurable:true,get:()=>{
      const raw=responseGetter.call(this);if(this.readyState!==4||!capabilityUrl(url)||preference==='auto')return raw;
      if(this.responseType==='json'&&record(raw))return patchCapability(raw).value;
      return typeof raw==='string'?patchText(raw):raw}});
    return originalOpen.call(this,method,url,...args);
  };
  proto.send=function(body) {if(completionUrl(xhrUrls.get(this))&&typeof body==='string')observe(body);return originalSend.call(this,body)};
  const api=Object.freeze({prepare,status:()=>refresh(false),check:()=>refresh(false)});
  globalThis.__DOUBAO_NATIVE_MEDIA__=api;
  document.addEventListener(EVENT,()=>{
    try {const options=JSON.parse(document.documentElement.getAttribute(COMMAND)||'{}');
      if(options.op==='prepare')prepare(options);
      else if(options.op==='authorize_upload')authorizeInput(options.selector);
      else refresh(false);
    }catch(error){state.supported=false;state.ready=false;state.reason_code=error.code||'invalid_native_command';publish()}
  });
  publish();
})();
