"""Bind local reference bytes to native Expert attachment identities.

This observes the site's native uploader and block builder. It never sends a
prompt, invents resource IDs, signs requests, or uploads outside that pipeline.
Only hashes of resource identities leave the page.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from integrations.dola_ref_upload import (
    BRIDGE_SOURCE, FIND_INPUT, REVEAL_INPUT, _main_world, upload_reference_media,
)
from navigation_errors import BrowserSetupError


IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.webm'}
IDENTITY_FIELDS = {'uri', 'Uri', 'ImageUri', 'SourceUri', 'key', 'fileKey',
                   'FileKey', 'file_key', 'resourceUri', 'resource_uri',
                   'mediaResourceUri', 'Vid', 'vid'}


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _identity(item):
    if not isinstance(item, dict):
        return None
    if str(item.get('type')) == '1':
        kind, value = 'image', item.get('image')
    elif str(item.get('type')) == '2' and isinstance(item.get('media'), dict):
        value = item['media']
        kind = {'1': 'audio', '2': 'video'}.get(str(value.get('media_type')))
    else:
        return None
    if not kind or not isinstance(value, dict):
        return None
    for field in ('uri', 'key', 'file_key', 'fileKey', 'resource_uri', 'resourceUri'):
        resource = value.get(field)
        if isinstance(resource, str) and resource:
            return kind, _hash(resource)
    return None


def _request_attachments(body):
    """Decode only message/block containers; text which looks like JSON is text."""
    found = []
    def visit(value, depth=0):
        if depth > 25:
            raise ValueError('depth')
        if isinstance(value, list):
            for item in value:
                visit(item, depth + 1)
        elif isinstance(value, dict):
            if str(value.get('block_type')) == '10052':
                attachments = (value.get('content') or {}).get('attachment_block', {}).get('attachments')
                if not isinstance(attachments, list):
                    raise ValueError('attachment_schema')
                found.extend(attachments)
                return
            for key in ('content', 'body', 'content_blocks_v2', 'content_blocks', 'content_block', 'blocks'):
                child = value.get(key)
                if isinstance(child, str):
                    try:
                        child = json.loads(child)
                    except (ValueError, TypeError):
                        continue
                if isinstance(child, (dict, list)):
                    visit(child, depth + 1)
    messages = body.get('messages', []) if isinstance(body, dict) else []
    if isinstance(messages, str):
        messages = json.loads(messages)
    visit(messages)
    return found


def audit_plugin_attachments(body, manifest):
    """Return (matches, actual_count, redacted_diagnostics) for the final request."""
    actual = []
    try:
        actual = _request_attachments(body)
        entries = manifest['files']
        counts = manifest['counts']
        valid = (manifest.get('version') == 1 and isinstance(entries, list)
                 and isinstance(manifest.get('has_media'), bool)
                 and isinstance(counts, dict))
        expected_counts = {'image': 0, 'video': 0, 'audio': 0}
        for index, entry in enumerate(entries):
            valid = (valid and entry.get('index') == index + 1
                     and entry.get('kind') in expected_counts
                     and bool(re.fullmatch('[a-f0-9]{64}', str(entry.get('sha256', ''))))
                     and bool(re.fullmatch('[a-f0-9]{64}', str(entry.get('resource_sha256', '')))))
            if entry.get('kind') in expected_counts:
                expected_counts[entry['kind']] += 1
        valid = (valid and counts == expected_counts and manifest['has_media'] ==
                 bool(expected_counts['audio'] + expected_counts['video'])
                 and len({entry.get('resource_sha256') for entry in entries}) == len(entries))
        observed = [_identity(item) for item in actual]
        expected = [(entry.get('kind'), entry.get('resource_sha256')) for entry in entries]
        matches = bool(valid and observed == expected and all(observed)) if observed else bool(valid and not expected)
        return matches, len(actual), {'matches': matches, 'expected_count': len(entries),
                                      'actual_count': len(actual), 'identity_order_matches': matches}
    except (TypeError, KeyError, ValueError, AttributeError):
        return False, len(actual), {'matches': False, 'reason': 'attachment_manifest_or_schema'}


# The wrapped runner receives each original File. A unique new native localKey
# binds that exact upload invocation to its task, and the site's block builder
# supplies the final resource identity. Filenames never establish that binding.
INSTALL_GUARD = r"""async ({token,expected}) => {
  const roots=[document],inputs=[];
  for(let i=0;i<roots.length;i++)for(const el of roots[i].querySelectorAll('*')){
    if(el.shadowRoot)roots.push(el.shadowRoot);
    if(el.matches('input[type=file]'))inputs.push(el);
  }
  const input=inputs.find(el=>el.getAttribute('data-dola-ref-target')===token);
  const fiberKey=Object.keys(input||{}).find(k=>/^__react(?:Fiber|InternalInstance)\$/.test(k));
  let fiber=input?.[fiberKey],props;
  for(let i=0;fiber&&i<50;i++,fiber=fiber.return){const p=fiber.memoizedProps||fiber.pendingProps||{};
    if(p.chatId&&p.inputUploadConfig){props=p;break;}}
  if(!props||Number(props.skill?.skill_type)!==17)return {ok:false,reason:'context'};
  let runtime=window.__dolaReferenceRuntime;
  if(!runtime&&Array.isArray(window.__LOADABLE_LOADED_CHUNKS__)){
    window.__LOADABLE_LOADED_CHUNKS__.push([['expert-reference-'+token],{},r=>{runtime=r;}]);
    if(runtime)window.__dolaReferenceRuntime=runtime;
  }
  if(typeof runtime!=='function')return {ok:false,reason:'runtime'};
  const load=id=>{try{return runtime(id);}catch{return null;}};
  let get=load('qBy')?.Bc;
  if(typeof get!=='function')for(const [id,f]of Object.entries(runtime.m||{})){
    const source=String(f),legacy=/attachmentsStore|attachmentUploadStore/.test(source);
    const stateReader=/storeContainer/.test(source)&&/getAsyncStore/.test(source);
    if(!legacy&&!stateReader)continue;
    const m=load(id),modern=stateReader&&typeof m?.b==='function'&&
      /^function[\w$]*\(([\w$]+)\)\{var([\w$]+);returnnull==\(\2=[\w$]+\(\1\)\)\?void0:\2\.getState\(\);?\}$/.test(String(m.b).replace(/\s+/g,''))?m.b:null;
    const candidate=legacy&&(m?.Bc||m?.GX)||modern;
    try{if(typeof candidate==='function'&&candidate('attachmentsStore')?.attachmentsMap){get=candidate;break;}}catch{}
  }
  if(typeof get!=='function')return {ok:false,reason:'store'};
  const rows=()=>get('attachmentsStore')?.attachmentsMap?.[props.chatId]?._17||[];
  const tasks=()=>get('attachmentUploadStore')?.preprocessTaskMap||{};
  if(rows().length)return {ok:false,reason:'existing_attachments'};
  if(window.__pluginExpertMediaIdentity)return {ok:false,reason:'existing_guard'};
  const hash=async value=>Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',
    typeof value==='string'?new TextEncoder().encode(value):value))).map(x=>x.toString(16).padStart(2,'0')).join('');
  const candidates=(node,out=new Set(),depth=0)=>{
    if(!node||typeof node!=='object'||depth>5||node instanceof Blob)return out;
    for(const [k,v]of Object.entries(node)){
      if(['uri','Uri','ImageUri','SourceUri','key','fileKey','FileKey','file_key','resourceUri','resource_uri','mediaResourceUri','Vid','vid'].includes(k)&&typeof v==='string'&&v)out.add(v);
      else if(['image','media','uploadResult','Result','Results','PluginResult'].includes(k)){
        if(Array.isArray(v))for(const x of v)candidates(x,out,depth+1);else candidates(v,out,depth+1);
      }
    }return out;
  };
  const kind=item=>item?.type==='image'||Number(item?.type)===1?'image':
    item?.type==='audio'||Number(item?.type)===2&&Number(item?.media?.media_type)===1?'audio':
    item?.type==='video'||Number(item?.type)===2&&Number(item?.media?.media_type)===2?'video':'';
  const identity=item=>{const type=kind(item),owner=type==='image'?item?.image:item?.media;
    for(const k of ['uri','key','file_key','fileKey','resource_uri','resourceUri'])if(type&&typeof owner?.[k]==='string'&&owner[k])return {kind:type,resource:owner[k]};return null;};
  const bindings=[];let busy=false,error='',lastEvidence=null,recording=Promise.resolve();
  const bind=original=>async function(args,...rest){
    if(busy||!Array.isArray(args?.files)||args.files.length!==1){error='upload_invocation';throw Error('参考素材身份未核对');}
    busy=true;
    try{
      const file=args.files[0],index=bindings.length,entry=expected[index];
      if(!(file instanceof File)||!entry||await hash(await file.arrayBuffer())!==entry.sha256){error='file_hash';throw Error('参考素材字节不一致');}
      const before=new Set(rows().map(a=>a.localKey));
      const result=await original.call(this,args,...rest),deadline=Date.now()+120000;
      let added,task;
      while(Date.now()<deadline){
        added=rows().filter(a=>!before.has(a.localKey));
        if(added.length>1){error='ambiguous_task';throw Error('参考素材任务不唯一');}
        task=tasks()[added[0]?.localKey]||{};
        if(task.error||task.failed||/^(failed|error|retry|rejected|cancelled|canceled)$/i.test(task.status||task.state||'')){error='upload_failed';throw Error('参考素材上传失败');}
        if(added.length===1&&!task.loading&&!/^(uploading|pending|loading|processing|preprocessing)$/i.test(task.status||task.state||'')&&candidates(added[0]).size+candidates(task).size>0)break;
        await new Promise(r=>setTimeout(r,100));
      }
      if(added?.length!==1||!added[0].localKey||kind(added[0])!==entry.kind){error='task_identity';throw Error('参考素材任务身份不符');}
      const nativeFile=added[0].file||task.file||task.ctx?.file;
      if(nativeFile instanceof Blob&&await hash(await nativeFile.arrayBuffer())!==entry.sha256){error='native_file_hash';throw Error('参考素材原生字节不一致');}
      const keys=new Set([...candidates(added[0]),...candidates(task)]);
      bindings.push({...entry,localKey:added[0].localKey,keys});
      return result;
    }finally{busy=false;}
  };
  const record=async(block,args)=>{
    if(bindings.length!==expected.length)return;
    const list=block?.content?.attachment_block?.attachments;
    if(!Array.isArray(list)||!Array.isArray(args?.attachments)||list.length!==bindings.length||args.attachments.length!==bindings.length){error='native_block_count';return;}
    const entries=[];
    for(let i=0;i<bindings.length;i++){
      const b=bindings[i],resource=identity(list[i]);
      if(args.attachments[i]?.localKey!==b.localKey||!resource||resource.kind!==b.kind||!b.keys.has(resource.resource)){error='native_block_identity';return;}
      entries.push({index:i+1,kind:b.kind,sha256:b.sha256,resource_sha256:await hash(resource.resource),
        candidate_hashes:await Promise.all([...b.keys].map(hash))});
    }
    if(new Set(entries.map(e=>e.resource_sha256)).size!==entries.length){error='duplicate_identity';return;}
    lastEvidence=entries;
  };
  window.__pluginExpertMediaIdentity={
    run:async(original,args)=>bind(original)(args),
    record:(block,args)=>{recording=record(block,args).catch(()=>{error='evidence';});},
    evidence:async()=>{await recording;return {
    ready:!error&&!busy&&bindings.length===expected.length&&lastEvidence?.length===expected.length,
    error,entries:error?[]:lastEvidence||[]};}};
  return {ok:true};
}"""


def _expert_bridge_source():
    """Observe known bridge boundaries and reuse verified modern native accessors."""
    source = BRIDGE_SOURCE
    for before, after in (
        ('await runner({inputUploadConfig:inputConfig',
         'await window.__pluginExpertMediaIdentity.run(runner,{inputUploadConfig:inputConfig'),
        ('const block=fn(args);if(block&&covers(countBlocks([block]),snap.counts))return block;',
         'const block=fn(args);if(block&&covers(countBlocks([block]),snap.counts))'
         '{window.__pluginExpertMediaIdentity.record(block,args);return block;}'),
        ("if(!store)return {ok:false,reason:'store'};", r"""if(!store){
          for(const [id,factory] of Object.entries(runtime.m||{})){
            const source=String(factory);if(!/storeContainer/.test(source)||!/getAsyncStore/.test(source))continue;
            const module=load(id),candidate=module?.b;
            if(typeof candidate!=='function'||!/^function[\w$]*\(([\w$]+)\)\{var([\w$]+);returnnull==\(\2=[\w$]+\(\1\)\)\?void0:\2\.getState\(\);?\}$/.test(String(candidate).replace(/\s+/g,'')))continue;
            try{if(candidate('attachmentsStore')?.attachmentsMap&&candidate('attachmentUploadStore')?.preprocessTaskMap){store={Bc:candidate};break;}}catch{}
          }
        }
        if(!store)return {ok:false,reason:'store'};"""),
        ('  let messages;', r"""  if(typeof service?.K!=='function'){
          for(const [id,factory] of Object.entries(runtime.m||{})){
            const source=String(factory);if(!/serviceContainer/.test(source)||!/resourceStore/.test(source)||!/chat-ioc-manager/.test(source))continue;
            const module=load(id),resolver=module?.a;
            if(typeof resolver==='function'&&/^async\b/.test(String(resolver))&&/\.load\(/.test(String(resolver))&&!/useState|useMemo|useEffect/.test(String(resolver))){service={K:resolver};break;}
          }
        }
  let messages;"""),
    ):
        if source.count(before) != 1:
            raise BrowserSetupError('参考素材身份校验桥版本不兼容，未发送提示词。')
        source = source.replace(before, after, 1)
    return source


def _receipt_hashes(value, depth=0):
    if depth > 15:
        return set()
    if isinstance(value, list):
        return set().union(*(_receipt_hashes(item, depth + 1) for item in value)) if value else set()
    if not isinstance(value, dict):
        return set()
    found = {_hash(item) for key, item in value.items()
             if key in IDENTITY_FIELDS and isinstance(item, str) and item}
    for item in value.values():
        if isinstance(item, (dict, list)):
            found.update(_receipt_hashes(item, depth + 1))
    return found


async def upload_plugin_references(page, paths):
    """Upload once in the supplied order and return a redacted identity manifest."""
    files = [Path(path).resolve() for path in paths or []]
    if not files:
        return {'version': 1, 'has_media': False, 'files': [], 'counts': {'image': 0, 'audio': 0, 'video': 0}}
    if len(set(files)) != len(files) or any(not path.is_file() or not path.stat().st_size for path in files):
        raise BrowserSetupError('参考素材文件无效或重复，未发送提示词。')
    expected, counts = [], {'image': 0, 'audio': 0, 'video': 0}
    for index, path in enumerate(files):
        kind = ('image' if path.suffix.lower() in IMAGE_EXTENSIONS else
                'audio' if path.suffix.lower() in AUDIO_EXTENSIONS else
                'video' if path.suffix.lower() in VIDEO_EXTENSIONS else None)
        if not kind:
            raise BrowserSetupError('参考素材格式不支持，未发送提示词。')
        counts[kind] += 1
        expected.append({'index': index + 1, 'filename': path.name, 'kind': kind,
                         'sha256': await asyncio.to_thread(lambda p=path: hashlib.sha256(p.read_bytes()).hexdigest())})
    has_media = bool(counts['audio'] + counts['video'])
    receipts, observers = set(), set()
    async def observe(response):
        if (not any(marker in response.url for marker in ('CommitUpload', 'CommitImageUpload', '/commit_upload'))
                or response.request.method == 'OPTIONS' or not 200 <= response.status < 300):
            return
        try:
            value = await response.json()
            if (not isinstance(value, dict) or value.get('code', 0) not in (0, '0', None)
                    or value.get('status_code', 0) not in (0, '0', None)
                    or (value.get('ResponseMetadata') or {}).get('Error')
                    or (value.get('Result') or {}).get('Error')):
                return
            receipts.update(_receipt_hashes(value))
        except (ValueError, TypeError, AttributeError):
            return
    def on_response(response):
        task = asyncio.create_task(observe(response))
        observers.add(task)
        task.add_done_callback(observers.discard)
    target = None
    if has_media:
        token = uuid.uuid4().hex
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not target:
            for frame in page.frames:
                if await frame.evaluate(FIND_INPUT, {'token': token, 'accept': 'image/*,audio/*,video/*'}):
                    target = frame
                    break
                await frame.evaluate(REVEAL_INPUT)
            if not target:
                await asyncio.sleep(.25)
        if not target:
            raise BrowserSetupError('参考素材原生入口未就绪，未发送提示词。')
        installed = await _main_world(target, INSTALL_GUARD, {'token': token, 'expected': expected})
        if not installed or installed.get('ok') is not True:
            raise BrowserSetupError('参考素材身份校验入口未就绪，未发送提示词。')
        page.on('response', on_response)
    try:
        ok, _message = await upload_reference_media(page, [str(path) for path in files], auto_send=False,
            **({'bridge_source': _expert_bridge_source()} if has_media else {}))
        if not ok:
            raise BrowserSetupError('参考素材原生上传未完成，未发送提示词。')
        if observers:
            await asyncio.gather(*list(observers), return_exceptions=True)
        if has_media:
            state = await _main_world(target, '() => window.__dolaReferenceMediaBridge?.status()')
            evidence = await _main_world(target, '() => window.__pluginExpertMediaIdentity?.evidence()')
            if (not state or not state.get('ready') or state.get('pending') or state.get('error')
                    or state.get('count') != len(files) or state.get('counts') != counts
                    or not evidence or not evidence.get('ready')):
                raise BrowserSetupError('参考素材上传收据或原生附件身份未核对通过，未发送提示词。')
            entries = evidence.get('entries', [])
            for row, item in zip(expected, entries):
                if (item.get('sha256') != row['sha256'] or item.get('kind') != row['kind']
                        or not receipts.intersection(item.get('candidate_hashes', []))):
                    raise BrowserSetupError('参考素材提交收据与原生资源身份不一致，未发送提示词。')
        else:
            entries = None
            for frame in page.frames:
                proof = await _main_world(frame, '() => window.__dolaReferenceOrderGuard?.evidence()')
                if proof and proof.get('ready') and not proof.get('error'):
                    rows = proof.get('entries', [])
                    if len(rows) == len(expected) and all(row.get('sha256') == item['sha256'] for row, item in zip(rows, expected)):
                        entries = [{**item, 'resource_sha256': row['upstreamKeySha256']}
                                   for row, item in zip(rows, expected)]
                        break
            if entries is None:
                raise BrowserSetupError('参考图原生附件身份或顺序未核对通过，未发送提示词。')
        result = [{'index': index + 1, 'kind': item['kind'], 'sha256': item['sha256'],
                   'resource_sha256': item['resource_sha256'], 'receipt_verified': True}
                  for index, item in enumerate(entries)]
        return {'version': 1, 'has_media': has_media, 'files': result, 'counts': counts}
    finally:
        if has_media:
            page.remove_listener('response', on_response)
        if observers:
            await asyncio.gather(*list(observers), return_exceptions=True)
