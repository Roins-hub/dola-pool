"""Upload a single image/audio/video batch through Dola's native file handler.

Only CDP selects files. The page performs prepare, transfer and commit itself;
this module never builds requests, signs payloads or sends a prompt.
"""
import asyncio
import hashlib
import inspect
import json
from pathlib import Path
import time
import uuid

EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.mp3', '.wav', '.m4a', '.aac',
              '.flac', '.ogg', '.mp4', '.mov', '.webm'}
ACCEPT = 'image/jpeg,image/png,image/webp,audio/*,video/*,' + ','.join(sorted(EXTENSIONS))
BRIDGE_SOURCE = Path(__file__).with_name('dola_ref_bridge.js').read_text(encoding='utf-8')
IMAGE_ORDER_SOURCE = Path(__file__).with_name('dola_image_order_guard.js').read_text(encoding='utf-8')


async def _main_world(frame, script, arg=None):
    options = {'isolated_context':False} if 'isolated_context' in inspect.signature(frame.evaluate).parameters else {}
    return await frame.evaluate(script, arg, **options)

FIND_INPUT = r"""({token,accept}) => {
  const roots=[document]; let inputs=[];
  for(let i=0;i<roots.length;i++) for(const el of roots[i].querySelectorAll('*')) {
    if(el.shadowRoot) roots.push(el.shadowRoot);
    if(el.matches('input[type=file]')) inputs.push(el);
  }
  // A generic chat attachment is not evidence of the video skill being active.
  if(!/Seedance|2\.0\s*(?:Fast|高速|Schnell)|(?:Model|Modell|モデル|模型)\s*2\.5/i.test(document.body.innerText)) return false;
  const score=el=>{
    const a=(el.accept||'').toLowerCase();
    if(/pdf|document|text\//.test(a)) return -100;
    return /audio|video|\.(?:mp3|wav|m4a|aac|flac|ogg|mp4|mov|webm)\b/.test(a)?100:
      /image|\.(?:jpg|jpeg|png|webp|apng)\b/.test(a)?30:!a?10:-100;
  };
  inputs.sort((a,b)=>score(b)-score(a)); const input=inputs[0];
  if(!input||score(input)<0) return false;
  input.setAttribute('data-dola-ref-target',token);
  input.accept=[...new Set((input.accept+','+accept).split(',').filter(Boolean))].join(',');
  input.multiple=true;
  return true;
}"""

READ_STATE = r"""({names,baseline}) => {
  const text=document.body.innerText;
  const roots=[document]; let images=0,busy=false,error=false,found=new Set();
  for(let i=0;i<roots.length;i++) for(const el of roots[i].querySelectorAll('*')) {
    if(el.shadowRoot) roots.push(el.shadowRoot);
    if(el.matches('img')&&el.getAttribute('src')) images++;
    // Inspect attachment widgets only, not unrelated page spinners/messages.
    if(!/attachment|upload|file-item|resource-item|reference|material/i.test(String(el.className))) continue;
    const content=(el.innerText||'')+' '+(el.getAttribute('title')||'')+' '+(el.getAttribute('aria-label')||'');
    for(const name of names) if(content.includes(name)) found.add(name);
    if(el.querySelector('[aria-busy=true],[role=progressbar]')||el.getAttribute('aria-busy')==='true') busy=true;
    if(/upload failed|unsupported|invalid file|上传失败|不支持|格式错误|fehlgeschlagen|nicht unterstützt/i.test(content)) error=true;
  }
  return {found:found.size,images:Math.max(0,images-baseline),busy,error,
    namesVisible:names.filter(n=>text.includes(n)).length};
}"""

REVEAL_INPUT = r"""() => {
  if(!/Seedance|2\.0\s*(?:Fast|高速|Schnell)|(?:Model|Modell|モデル|模型)\s*2\.5/i.test(document.body.innerText))return false;
  const buttons=[...document.querySelectorAll('button,[role=button]')]
    .filter(el=>el.getBoundingClientRect().width>0);
  const named=buttons.find(el=>/^(?:Add files|Add reference|Upload files|Upload reference files|添加素材|上传文件|参考素材|添加音频|上传音频|Dateien hinzufügen|Dateien hochladen)$/i.test(
    (el.getAttribute('aria-label')||el.getAttribute('title')||el.innerText||'').trim()));
  const plus=buttons.find(el=>!el.innerText.trim()&&[...el.querySelectorAll('svg path')].some(path=>
    /V10\.9951H2|V11H3|M12(?:\.0005)?\s+2\.(?:25|05)/.test(path.getAttribute('d')||'')));
  const button=named||plus;if(button){button.click();return true;}return false;
}"""


def _marked_node(node, token):
    attrs = node.get('attributes', [])
    if any(attrs[i:i+2] == ['data-dola-ref-target', token] for i in range(0, len(attrs), 2)):
        return node['nodeId']
    children = [*node.get('children', []), *node.get('shadowRoots', [])]
    if node.get('contentDocument'): children.append(node['contentDocument'])
    for child in children:
        found = _marked_node(child, token)
        if found: return found
    return None


async def _upload(page, paths, timeout_s, bridge_source=None):
    names = [p.name for p in paths]
    signature = hashlib.sha256('\n'.join(str(p)+':'+str(p.stat().st_size)+':'+str(p.stat().st_mtime_ns)
                                        for p in paths).encode()).hexdigest()
    token = uuid.uuid4().hex
    target = None
    slot_deadline = time.monotonic() + min(10, timeout_s)
    revealed = False
    while time.monotonic() < slot_deadline and page.frames:
        for frame in page.frames:
            try:
                if await frame.evaluate(FIND_INPUT, {'token':token, 'accept':ACCEPT}):
                    target = frame
                    break
                if not revealed:
                    revealed = bool(await frame.evaluate(REVEAL_INPUT))
            except Exception:
                continue
        if target: break
        await asyncio.sleep(.25)
    if target is None:
        return False, '没找到参考素材上传入口，确认已进入「视频生成」。'
    has_media = any(p.suffix.lower() not in {'.jpg','.jpeg','.png','.webp'} for p in paths)
    previous = await target.evaluate('() => window.__dolaReferenceBatch || null')
    if previous:
        if previous.get('signature') == signature and previous.get('ready'):
            if not has_media:
                order = await _main_world(target, '() => window.__dolaReferenceOrderGuard?.status()')
                if not order or not order.get('ready'):
                    return False, '参考图身份或顺序已改变，未发送提示词。'
            return True, f'已就绪：{len(paths)} 个参考素材（已上传，不重复添加）'
        return False, '当前页面已尝试添加素材，请重新打开视频页面后再试，避免重复上传。'
    await target.evaluate('(signature) => {window.__dolaReferenceBatch={signature,ready:false};}', signature)
    if has_media:
        bridge = await _main_world(target, bridge_source or BRIDGE_SOURCE, {'token':token, 'names':names, 'timeoutMs':int(timeout_s*1000)})
        if not bridge or not bridge.get('ok'):
            return False, '当前 Dola 视频上传配置未就绪或版本不兼容，未发送提示词。'
    else:
        expected = await asyncio.to_thread(lambda: [
            {'filename':p.name, 'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in paths])
        guard = await _main_world(target, IMAGE_ORDER_SOURCE, {'token':token, 'expected':expected})
        if not isinstance(guard, dict) or guard.get('ok') is not True:
            reasons = {
                'context': '未找到当前视频输入上下文',
                'manifest': '参考图身份清单无效',
                'runtime': '页面原生运行环境未就绪',
                'store': '参考图原生附件状态未就绪',
                'existing_attachments': '输入框已有附件，无法建立本次参考图顺序基线',
                'send_guard': '签名前参考图发送校验入口未就绪',
            }
            code = guard.get('reason') if isinstance(guard, dict) else None
            reason = reasons.get(code) if isinstance(code, str) else None
            # Only known reason codes may reach the task error, never arbitrary
            # native exception text, credentials, store values or signed URLs.
            reason = reason or '未取得有效校验状态'
            return False, f'参考图身份和顺序校验入口未就绪（{reason}），未上传或发送提示词。'
    baseline = await target.locator('img[src]').count()
    # Track completed native requests, rather than counting file change events.
    prepared, transferred, committed = set(), set(), set()
    failures = []
    handlers = set()

    async def observe(response):
        url = response.url
        stage = ('prepare' if '/alice/resource/prepare_upload' in url else
                 'transfer' if '/upload/v1/' in url else
                 'commit' if 'CommitImageUpload' in url or 'CommitUpload' in url else None)
        if not stage or response.request.method == 'OPTIONS': return
        if not 200 <= response.status < 300:
            failures.append(stage)
            return
        try:
            data = await response.json()
        except Exception:
            failures.append(stage)
            return
        allowed_codes = (0, '0', 2000, '2000', None) if stage == 'transfer' else (0, '0', None)
        if (not isinstance(data, dict) or data.get('code', 0) not in allowed_codes
                or data.get('status_code', 0) not in (0, '0', None)
                or (data.get('ResponseMetadata') or {}).get('Error')):
            failures.append(stage)
            return
        if stage == 'prepare':
            if data.get('data'): prepared.add(response.request)
        elif stage == 'transfer':
            transferred.add(url.split('?', 1)[0])
        elif data.get('Result') and not data['Result'].get('Error'):
            # A commit is the native uploader's durable receipt; no signed URL is logged.
            for item in data['Result'].get('Results', []) + data['Result'].get('PluginResult', []):
                key = item.get('Uri') or item.get('ImageUri') or item.get('SourceUri')
                if key: committed.add(key)

    def on_response(response):
        task = asyncio.create_task(observe(response))
        handlers.add(task)
        def finished(done):
            handlers.discard(done)
            if not done.cancelled() and done.exception() is not None:
                failures.append('unexpected_response')
        task.add_done_callback(finished)

    page.on('response', on_response)
    session = None
    try:
        session = await page.context.new_cdp_session(target)
        document = await session.send('DOM.getDocument', {'depth':-1, 'pierce':True})
        node_id = _marked_node(document['root'], token)
        if not node_id: return False, '文件没有写进页面上传控件。'
        await session.send('DOM.setFileInputFiles', {'nodeId':node_id, 'files':[str(p) for p in paths]})
        if has_media:
            await _main_world(target, '() => {window.__dolaReferenceMediaBridge.start();}')
        deadline = time.monotonic() + timeout_s
        image_count = sum(p.suffix.lower() in {'.jpg','.jpeg','.png','.webp'} for p in paths)
        state, visible, order = {'busy':False}, 0, None
        while time.monotonic() < deadline:
            state = await target.evaluate(READ_STATE, {'names':names, 'baseline':baseline})
            native = (await _main_world(target, '() => window.__dolaReferenceMediaBridge?.status()')) if has_media else None
            order = None if has_media else await _main_world(target, '() => window.__dolaReferenceOrderGuard?.status()')
            if not has_media and (not order or order.get('error')):
                return False, '参考图身份或顺序与输入不符，未发送提示词。'
            if native and native.get('error'):
                if native.get('count') == len(paths) and not native.get('pending') and not native.get('referencesReady'):
                    return False, '素材已上传，但当前 Dola 版本无法完整引用音视频，未发送提示词。'
                return False, 'Dola 音视频素材上传失败，未发送提示词。'
            if failures or state['error']:
                return False, 'Dola 拒绝素材或上传出错，未发送提示词。请检查素材格式、大小及账号支持情况。'
            # Pure-image prepare calls can batch files; final transfer/commit receipts count images.
            receipts = min(len(transferred), len(committed))
            if has_media:
                receipts = min(receipts, len(prepared))
            visible = max(state['found'], state['namesVisible']) + min(image_count, state['images'])
            ready = native.get('ready') if native else (receipts >= len(paths) and visible >= len(paths)
                                                       and not state['busy'] and order.get('ready'))
            if ready:
                if not has_media:
                    proof = await _main_world(target, '() => window.__dolaReferenceOrderGuard.evidence()')
                    print('[reference-order] ' + json.dumps(proof, ensure_ascii=False), flush=True)
                await target.evaluate('() => {window.__dolaReferenceBatch.ready=true;}')
                return True, f'已就绪：{len(paths)} 个参考素材'
            await asyncio.sleep(.25)
        if not has_media:
            try:
                diagnostics = await _main_world(target, '() => window.__dolaReferenceOrderGuard?.diagnostics()')
            except Exception:
                diagnostics = {'error':'diagnostics_unavailable'}
            diagnostics = {**(diagnostics or {}), 'prepareCount':len(prepared),
                'transferCount':len(transferred), 'commitCount':len(committed),
                'visibleCount':visible, 'busy':bool(state.get('busy')), 'orderReady':bool(order and order.get('ready'))}
            print('[reference-order-diagnostics] ' + json.dumps(diagnostics, ensure_ascii=False), flush=True)
        return False, f'素材未全部上传完成（确认 {min(len(committed),len(paths))}/{len(paths)} 个），未发送提示词。'
    finally:
        page.remove_listener('response', on_response)
        for task in list(handlers): task.cancel()
        if handlers: await asyncio.gather(*handlers, return_exceptions=True)
        if session:
            try: await session.detach()
            except Exception: pass


async def upload_reference_media(page, files: list[str], *, auto_send=True, timeout_s=120,
                                 bridge_source=None) -> tuple[bool, str]:
    """Never sends. auto_send is retained for spec compatibility; partial uploads fail closed."""
    paths = list(dict.fromkeys(Path(p).resolve() for p in files))
    if not paths or any(not p.is_file() or not p.stat().st_size for p in paths):
        return False, '没有找到可用文件。'
    if any(p.suffix.lower() not in EXTENSIONS for p in paths):
        return False, '只支持 JPG/PNG/WebP 图片、MP3/WAV/M4A/AAC/FLAC/OGG 音频与 MP4/MOV/WebM 视频。'
    try:
        return await asyncio.wait_for(_upload(page, paths, timeout_s, bridge_source), timeout=timeout_s+15)
    except asyncio.TimeoutError:
        return False, '参考素材上传超时，未发送提示词。'
    except Exception:
        return False, '参考素材上传失败，未发送提示词。请重新打开视频页面后重试。'
