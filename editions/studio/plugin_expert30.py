"""Fixed-plugin expert route, one submission and multiple independently verified results."""
import asyncio
import hashlib
import html
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from contextlib import asynccontextmanager

import aiohttp


def _decoded(value):
    if isinstance(value, str) and value.lstrip().startswith(('{', '[')):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def _walk(value, depth=0, *, decode_strings=True):
    if depth > 16:
        return
    value = _decoded(value) if decode_strings else value
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk(item, depth + 1, decode_strings=decode_strings)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item, depth + 1, decode_strings=decode_strings)


def parse_messages(data, conversation_id, baseline_messages=()):
    """Keep resources attached to their own creation; no response-array index joins."""
    from dola_expert import media_url
    body = data.get('downlink_body') if isinstance(data, dict) else None
    chain = body.get('pull_singe_chain_downlink_body') if isinstance(body, dict) else None
    result = {'accepted': False, 'tools_finished': False, 'tool_statuses': [], 'clips': [], 'reply': ''}
    if not isinstance(chain, dict):
        return result
    if chain.get('conversation_id') and str(chain['conversation_id']) != str(conversation_id):
        return result
    texts, sources = [], []
    for message in chain.get('messages') or []:
        if not isinstance(message, dict):
            continue
        if message.get('conversation_id') and str(message['conversation_id']) != str(conversation_id):
            continue
        if str(message.get('user_type')) != '2' and str(message.get('role', '')).lower() != 'assistant':
            continue
        if str(message.get('user_type')) == '1' or str(message.get('message_id') or message.get('id')) in baseline_messages:
            continue
        content = _decoded(message.get('content'))
        if isinstance(content, dict) and isinstance(content.get('text'), str):
            texts.append(content['text'])
        for field in _walk(content, decode_strings=False):
            tool = field.get('generic_tool_block')
            if isinstance(tool, dict) and tool.get('tool_name') == 'text_to_video':
                result['tool_statuses'].append(tool.get('status'))
            text = field.get('text_block')
            if isinstance(text, dict) and isinstance(text.get('text'), str):
                texts.append(text['text'])
            creation = ((field.get('content') or {}).get('creation_block')
                        if field.get('block_type') == 2074 and isinstance(field.get('content'), dict) else None)
            if not isinstance(creation, dict):
                continue
            for item in creation.get('creations') or []:
                if not isinstance(item, dict) or item.get('type') != 2 or not isinstance(item.get('video'), dict):
                    continue
                video = item['video']
                models, fallbacks, urls = [], [], []
                vid = str(video.get('vid') or video.get('video_id') or '')
                for node in _walk(video):
                    vid = vid or str(node.get('vid') or node.get('video_id') or '')
                    for key, bucket in (('video_model', models), ('fallback_api', fallbacks), ('download_url', urls)):
                        value = node.get(key)
                        if isinstance(value, dict) and key == 'video_model':
                            value = json.dumps(value, ensure_ascii=False)
                        if isinstance(value, str) and value and value not in bucket:
                            bucket.append(value)
                urls = [url for url in urls if media_url(url)]
                creation_id = str(item.get('id') or item.get('creation_id') or '')
                identity = creation_id or vid or (urlsplit(urls[0]).path if urls else '')
                if not identity:
                    continue
                sources.append({'clip_id': hashlib.sha256(identity.encode()).hexdigest()[:24],
                    'vid': vid, 'creation_id': creation_id, 'message_id': str(message.get('message_id') or message.get('id') or ''),
                    'video_models': models, 'fallback_apis': fallbacks, 'preview_urls': urls})
    result['reply'] = '\n\n'.join(texts)[-6000:]
    result['accepted'] = bool(sources or any(s in (1, 2, 3, 4) for s in result['tool_statuses']))
    result['tools_finished'] = bool(result['tool_statuses']) and all(s == 4 for s in result['tool_statuses'])
    seen = set()
    for source in sources:
        if source['clip_id'] not in seen:
            source['index'] = len(result['clips']) + 1
            result['clips'].append(source)
            seen.add(source['clip_id'])
    # Expert tool replies sometimes return only Markdown media, without a native card.
    if result['tools_finished']:
        paths = {urlsplit(u).path for c in sources for u in c['preview_urls']}
        for text in texts:
            for raw in re.findall(r'https?://[^\s<>"\)\]]+', text):
                url = html.unescape(raw)
                if not media_url(url) or urlsplit(url).path in paths:
                    continue
                paths.add(urlsplit(url).path)
                result['clips'].append({'clip_id': hashlib.sha256(urlsplit(url).path.encode()).hexdigest()[:24],
                    'index': len(result['clips']) + 1, 'vid': '', 'creation_id': '',
                    'video_models': [], 'fallback_apis': [], 'preview_urls': [url]})
    return result


def audit_request(body, expected_text, image_hashes=(), *, attachment_manifest=None):
    option = body.get('option') or {}
    texts, images = [], []
    for node in _walk(body.get('messages') or []):
        if isinstance(node.get('text_block'), dict) and isinstance(node['text_block'].get('text'), str):
            texts.append(node['text_block']['text'])
        if isinstance(node.get('image'), dict) and isinstance(node['image'].get('uri'), str):
            images.append(hashlib.sha256(node['image']['uri'].encode()).hexdigest())
    attachments_match = images == list(image_hashes)
    attachment_count = len(images)
    if attachment_manifest is not None:
        from plugin_expert_media import audit_plugin_attachments
        attachments_match, attachment_count, _ = audit_plugin_attachments(body, attachment_manifest)
    matches = (str(option.get('agent_mode')) == '1'
        and str((option.get('model_config') or {}).get('model_item_key')) == '4'
        and texts == [expected_text] and attachments_match)
    return {'matches': matches, 'entry_mode': 'plugin-expert30', 'prompt_matches': texts == [expected_text],
            'attachment_count': attachment_count, 'attachments_match': attachments_match,
            'agent_mode': option.get('agent_mode'), 'model_item_key': (option.get('model_config') or {}).get('model_item_key')}


@asynccontextmanager
async def _lane(scheduler, account):
    if scheduler:
        import config
        async with scheduler.lane_slot(config.proxy_for(account) or 'direct'):
            yield
    else:
        yield


def poll_outcome(poll):
    from dola_expert import expert_outcome
    parsed = poll.get('plugin_expert30') or {}
    normalized = {**poll, 'expert': {'accepted': parsed.get('accepted'),
        'videos': [u for c in parsed.get('clips', []) for u in c.get('preview_urls', [])], 'reply': parsed.get('reply', '')}}
    _, outcome = expert_outcome(normalized)
    reply = str(parsed.get('reply') or '').strip()
    plus = re.fullmatch(r'Dieses Feature ist nur für Dola Plus-Abonnent\*innen verfügbar\.\s*'
        r'Führe ein Upgrade auf Dola Plus durch, um es zu verwenden\.', reply)
    if plus and not parsed.get('accepted'):
        from video_outcome import PlatformVideoError
        outcome.update(state='failed', error=PlatformVideoError('subscription_required', '此能力需要Dola Plus', reply))
    # Tools/cards prove acceptance; an old textual refusal must not erase that evidence.
    if parsed.get('accepted'):
        outcome.update(state='completed' if parsed.get('tools_finished') else 'accepted', error=None)
    return parsed, outcome


def receipt_outcome(poll):
    """Require a native running/completed tool or card before closing the browser."""
    parsed, outcome = poll_outcome(poll)
    if parsed.get('clips'):
        return parsed, outcome
    # A completed tool can also contain a quota/refusal result. With no native
    # card, classify that reply before considering the tool's running state.
    _, refusal = poll_outcome({**poll, 'plugin_expert30': {**parsed, 'accepted': False}})
    if refusal.get('error'):
        return parsed, refusal
    # Status 2 is the observed running state, 4 the completed state. Unknown
    # states and generic assistant promises cannot release the browser permit.
    running = any(type(status) is int and status in (2, 4)
                  for status in parsed.get('tool_statuses') or [])
    return parsed, {**outcome, 'state': 'accepted' if running else 'awaiting_receipt', 'error': None}


async def wait_for_receipt(page, account, conversation_id, deadline_at, on_poll=None, on_event=None):
    """Read only this saved conversation while its original browser stays open."""
    import config
    from account_errors import LoginExpiredError
    from navigation_errors import BrowserSetupError, terminal_setup_reason
    from video_outcome import VideoQueryTimeout
    from video_worker_ui import _read_page_conversation
    last_reply = None
    if on_event:
        on_event('awaiting_receipt', {'reason': '原会话已保存，网页等待视频生成受理或明确拒绝回执；不会重复发送'})
    while time.time() < deadline_at:
        try:
            poll = await asyncio.wait_for(_read_page_conversation(page, conversation_id),
                                          timeout=min(30, max(0, deadline_at - time.time())))
        except LoginExpiredError:
            raise
        except Exception as error:
            terminal = terminal_setup_reason(error)
            if terminal:
                raise BrowserSetupError(terminal) from None
            if on_event:
                on_event('awaiting_receipt', {
                    'reason': f'原会话回执查询暂时失败（{type(error).__name__}），保留网页继续等待；未重新发送'})
        else:
            if poll.get('loginExpired'):
                raise LoginExpiredError('等待专家视频回执时 Dola 登录已失效，请重新登录或导入新 CK')
            parsed, outcome = receipt_outcome(poll)
            if on_poll:
                on_poll(time.time())
            reply = outcome.get('reply') or parsed.get('reply') or ''
            if reply and reply != last_reply:
                last_reply = reply
                if on_event:
                    on_event('awaiting_receipt', {'reply': reply})
            if outcome.get('error'):
                error = outcome['error']
                if on_event:
                    on_event(error.task_phase, {'reason': str(error), 'reply': reply})
                raise error
            if outcome.get('state') in ('accepted', 'completed'):
                if on_event:
                    on_event('accepted', {'reason': '原会话已收到视频生成受理回执，允许关闭网页并继续收取结果',
                                          'reply': reply, 'video_count': len(parsed.get('clips') or [])})
                return
        await asyncio.sleep(min(config.POLL_INTERVAL, max(0, deadline_at - time.time())))
    raise VideoQueryTimeout('等待专家视频生成回执超时，原会话已保存；未重新发送或换号，可从原会话补收')


async def resume_video(account, conversation_id, timeout, on_poll=None, deadline_at=None,
                       on_event=None, generation_options=None, scheduler=None, **_ignored):
    import config
    from video_polling import HttpVideoSession, PollingResponseError
    from plugin_resources import resolve_clip
    from video_delivery import NoWatermarkAvailable
    from video_evidence import safe_text
    from video_capabilities import plugin_expert_result_shape
    options = generation_options or {}
    results = {c['clip_id']: c for c in options.get('existing_clips', []) if c.get('local_path')}
    sources = {key: dict(item) for key, item in results.items()}
    notified = False
    deadline = deadline_at or time.time() + timeout
    finished = False
    reply = ''
    attempts = {}
    async with HttpVideoSession(account, conversation_id) as session:
        session.network_lane = scheduler.lane_slot(config.proxy_for(account) or 'direct') if scheduler else None
        while time.time() < deadline:
            try:
                async with _lane(scheduler, account):
                    poll = await session.poll()
            except (aiohttp.ClientError, OSError, TimeoutError, PollingResponseError) as error:
                if on_event:
                    on_event('polling', {'reason': f'原会话查询暂时断开（{type(error).__name__}），等待重查'})
                await asyncio.sleep(min(config.POLL_INTERVAL, max(0, deadline-time.time())))
                continue
            parsed, outcome = poll_outcome(poll)
            if on_poll:
                on_poll(time.time())
            reply = parsed.get('reply') or reply
            if on_event and reply:
                on_event('polling', {'reply': reply})
            if outcome['state'] in ('accepted', 'completed') and not notified:
                notified = True
                if on_event:
                    on_event('accepted', {'reason': '原会话已收到视频生成受理回执'})
            for source in parsed.get('clips') or []:
                key = source['clip_id']
                alias = next((old_key for old_key, old in sources.items() if any(
                    source.get(field) and old.get(field) == source[field] for field in ('vid', 'creation_id'))), None)
                if alias:
                    key = alias
                    source = {**source, 'clip_id': key}
                if key not in sources:
                    source = {**source, 'index': len(sources)+1}
                    sources[key] = source
                else:
                    old = sources[key]
                    source = {**old, **source, 'index': old['index'], **{
                        field: list(dict.fromkeys([*(old.get(field) or []), *(source.get(field) or [])]))
                        for field in ('video_models', 'fallback_apis', 'preview_urls')}}
                    sources[key] = source
                if key in results or time.time()-attempts.get(key, 0) < 60:
                    continue
                attempts[key] = time.time()
                try:
                    if on_event:
                        on_event('downloading', {'reason': f'逐条核对第{source["index"]}条原片'})
                    item = await resolve_clip(session, source, options.get('resolution', '720p'))
                    results[key] = item
                    if callable(options.get('on_clip')):
                        options['on_clip'](item)
                except (aiohttp.ClientError, OSError, ValueError, TimeoutError, NoWatermarkAvailable) as error:
                    sources[key]['error'] = safe_text(error)
            durations = [float(c.get('actual_duration') or 0) for c in results.values()]
            valid_shape = (len(results) == len(sources) and plugin_expert_result_shape(
                durations, options.get('requested_duration', 30), options.get('two_part', False)))
            if parsed.get('tools_finished') and valid_shape:
                finished = True
                break
            if outcome.get('error') and not sources:
                if on_event:
                    on_event(outcome['error'].task_phase, {'reason': str(outcome['error']), 'reply': reply})
                raise outcome['error']
            await asyncio.sleep(min(config.POLL_INTERVAL, max(0, deadline-time.time())))
    clips = [results.get(key) or {'clip_id': key, 'index': source['index'], 'vid': source.get('vid'),
             'creation_id': source.get('creation_id'), 'status': 'failed',
             'error': source.get('error') or '已发现视频卡片，尚未取得合格原片'} for key, source in sources.items()]
    # Cached identities can outlive a truncated poll page; preserve them too.
    clips += [item for key, item in results.items() if key not in sources]
    clips.sort(key=lambda item: item.get('index', 0))
    return {'clips': clips, 'generation_finished': finished, 'observation_finished': True,
            'account': account, 'conversation_id': str(conversation_id), 'platform_reply': reply}


async def _fill_plugin_draft(page, prompt):
    """Use the exact editor selected by the plugin, including its lowest-editor rule."""
    selector = '.tiptap.ProseMirror[contenteditable="true"], [contenteditable="true"][data-lexical-editor], [contenteditable="true"][role="textbox"], [contenteditable="true"], textarea'
    select_editor = '''selector=>{
      document.querySelectorAll('[data-project-plugin-composer]').forEach(n=>n.removeAttribute('data-project-plugin-composer'));
      const editors=[...document.querySelectorAll(selector)].filter(n=>{
        const r=n.getBoundingClientRect(),s=getComputedStyle(n);return !n.disabled&&r.width>1&&r.height>1&&s.display!=='none'&&s.visibility!=='hidden';
      }).sort((a,b)=>b.getBoundingClientRect().bottom-a.getBoundingClientRect().bottom);
      if(!editors.length)return null;
      const n=editors[0];n.setAttribute('data-project-plugin-composer','1');
      return {text:/^(TEXTAREA|INPUT)$/.test(n.tagName)?n.value:n.innerText};
    }'''
    for _ in range(3):
        selected = await page.evaluate(select_editor, selector)
        if selected is not None:
            editor = page.locator('[data-project-plugin-composer="1"]')
            await editor.fill(prompt)
            await page.wait_for_timeout(600)
            # Native editors can replace their DOM node while handling input.
            # Verify the plugin's current editor instead of waiting on a lost marker.
            current = await page.evaluate(select_editor, selector)
            # Paragraph layout can add visual blank lines to innerText. This is
            # only a readiness check; audit_request still checks the exact wire text.
            if current is not None and current['text'].split() == prompt.split():
                return
        await asyncio.sleep(.5)
    from dola_expert import ExpertSubmissionError
    raise ExpertSubmissionError('插件当前编辑器草稿未就绪，未发送')


async def prepare_protocol(prompt, model='seedance-2.5', duration=30, ratio=None, generation_options=None):
    """Resolve the exact shipped protocol and optional rule before opening a browser."""
    from plugin_resources import run_resolver
    from video_capabilities import validate_plugin_expert30_parameters
    options = generation_options or {}
    params = validate_plugin_expert30_parameters(model, duration, ratio, options.get('resolution'))
    two_part = options.get('two_part', False)
    if type(two_part) is not bool or (two_part and params['duration'] != 30):
        raise ValueError('两段式规则仅支持专家模式30秒，开关必须是布尔值')
    selection = {'model': params['wire_model'], 'duration': params['duration'],
                 'ratio': params['ratio'], 'resolution': params['resolution']}
    protocol = await run_resolver({'action': 'expert_protocol', 'context': selection,
                                   'prompt': prompt, 'two_part': two_part})
    if not protocol.get('prefix') or not isinstance(protocol.get('draft_text'), str):
        raise ValueError('专家模式协议未就绪，未发送')
    return {**protocol, 'selection': selection, 'two_part': two_part}


async def generate_video(account, prompt, ratio=None, duration=30, timeout=None, model='seedance-2.5',
                         on_conversation_id=None, on_poll=None, on_event=None, on_prompt_sent=None,
                         on_browser_closed=None, reference_image_paths=None, generation_options=None,
                         scheduler=None, **_ignored):
    import config
    from browser import launch_account_context, cookie_value
    from plugin_resources import EXTENSION_DIR
    from plugin_expert_media import upload_plugin_references
    from video_polling import save_poll_state
    from video_worker_ui import (_submission_playwright, _close_submission_context,
        _dismiss_cookie_banner, _dismiss_login_modal, _ensure_new_chat, goto_retry,
        _find_composer_video_entry)
    from dola_expert import ExpertSubmissionError
    options = {**(generation_options or {}), 'requested_duration': duration}
    protocol = await prepare_protocol(prompt, model, duration, ratio, options)
    selection = protocol['selection']
    options['requested_duration'] = selection['duration']
    expected_text = protocol['expected_text']
    if json.loads((EXTENSION_DIR/'manifest.json').read_text(encoding='utf-8'))['version'] != '1.6.60':
        raise ExpertSubmissionError('专家插件版本不符，未发送')
    attachment_manifest = {'version': 1, 'has_media': False, 'files': [],
                           'counts': {'image': 0, 'video': 0, 'audio': 0}}
    sent, armed, blocked = 0, False, False
    conversation = ''
    deadline = time.time() + (timeout or config.LONG_VIDEO_TIMEOUT)
    context = None
    page = None
    viewer_closed = asyncio.Event()
    async with _submission_playwright() as driver:
        context = await launch_account_context(driver, account, headless=False, use_extension=True,
                                               extension_dir=str(EXTENSION_DIR))
        context.on('close', lambda _: viewer_closed.set())
        try:
            async def guard(route):
                nonlocal sent, blocked
                if route.request.method != 'POST':
                    return await route.fallback()
                try:
                    audit = audit_request(route.request.post_data_json, expected_text,
                                          attachment_manifest=attachment_manifest)
                except Exception:
                    audit = {'matches': False}
                if not armed or sent or not audit['matches']:
                    blocked = True
                    if on_event:
                        on_event('request_mismatch', {**audit, 'reason': '专家参数、原始提示词或参考图身份核对失败，已拦截'})
                    return await route.abort('blockedbyclient')
                if on_prompt_sent:
                    on_prompt_sent(account)
                sent += 1
                if on_event:
                    on_event('submitted', {**audit, 'reason': '原插件专家请求与完整用户输入、参考图核对通过'})
                await route.fallback()
            await context.route('**/chat/completion*', guard)
            await context.add_init_script('''p=>{}'''.replace('p=>{}', '''(() => {
              if(!/(^|\\.)dola\\.com$/.test(location.hostname))return;
              const p=SELECTION;const twoPart=TWO_PART;
              const keys=['dola_protocol_parameter_selection_v1','dola_protocol_mode_v1','dola_protocol_batch_count_v1','dola_two_part_rule_v1','intl_doubao_target_duration_v1','intl_doubao_enable_30s_v1','intl_doubao_target_video_model_v1','intl_doubao_target_resolution_v1'];
              const saved='dola-project-plugin-preferences-v1';
              if(!sessionStorage.getItem(saved))sessionStorage.setItem(saved,JSON.stringify(Object.fromEntries(keys.map(k=>[k,localStorage.getItem(k)]))));
              localStorage.setItem(keys[0],JSON.stringify(p));localStorage.setItem(keys[1],'expert');localStorage.setItem(keys[2],'1');localStorage.setItem(keys[3],twoPart?'1':'0');
              localStorage.setItem(keys[4],String(p.duration));localStorage.setItem(keys[5],p.duration===30?'1':'0');localStorage.setItem(keys[6],p.model);localStorage.setItem(keys[7],p.resolution);
            })();'''.replace('SELECTION', json.dumps(selection)).replace('TWO_PART', json.dumps(protocol['two_part']))))
            page = context.pages[0] if context.pages else await context.new_page()
            await goto_retry(page, 'https://www.dola.com/chat')
            await _dismiss_cookie_banner(page)
            await _dismiss_login_modal(page)
            if not cookie_value(await context.cookies('https://www.dola.com'), 'sessionid'):
                raise ExpertSubmissionError('账号登录不可用，未发送')
            await _ensure_new_chat(page)
            await page.wait_for_function("document.documentElement.hasAttribute('data-dola-protocol-send-status')", timeout=60000)
            residual = await page.evaluate("()=>JSON.parse(sessionStorage.getItem('dola-toolkit-protocol-batch-v1')||'null')")
            if residual and residual.get('active'):
                raise ExpertSubmissionError('存在未接管的插件批次，未提交新申请')
            if reference_image_paths:
                # Upload in the native video editor before the plugin switches to Expert.
                until = time.monotonic() + config.ENTRY_WAIT_SECONDS
                entry = None
                while time.monotonic() < until and entry is None:
                    entry = await _find_composer_video_entry(page)
                    if entry is None:
                        await asyncio.sleep(.5)
                if entry is None:
                    raise ExpertSubmissionError('未找到原生参考素材视频编辑器，未发送')
                await entry.click(timeout=10000)
                await page.wait_for_timeout(1500)
                attachment_manifest = await upload_plugin_references(page, reference_image_paths)
            await page.wait_for_function("()=>{try{const s=JSON.parse(document.documentElement.getAttribute('data-watermark-free-resolution-bridge'));return s&&(s.submitReady||s.legacyReady)}catch{return false}}", timeout=45000)
            await _fill_plugin_draft(page, protocol['draft_text'])
            for frame in await page.locator('iframe[src*="captcha"],iframe[src*="verify"]').all():
                if await frame.is_visible():
                    raise ExpertSubmissionError('需要人工验证，未发送')
            armed = True
            await page.evaluate('''({p,twoPart})=>{const r=document.documentElement;
              r.setAttribute('data-dola-protocol-parameter-selection',JSON.stringify(p));
              r.setAttribute('data-dola-protocol-batch-count','1');r.setAttribute('data-dola-protocol-mode','expert');
              r.setAttribute('data-dola-two-part-enabled',twoPart?'1':'0');document.dispatchEvent(new Event('dola-two-part-change'));
              r.setAttribute('data-dola-protocol-send-request','project-'+Date.now());document.dispatchEvent(new Event('dola-protocol-send-request'));
            }''', {'p': selection, 'twoPart': protocol['two_part']})
            until = time.monotonic() + 100
            waiting_for_late_request = False
            while time.monotonic() < until:
                if blocked:
                    raise ExpertSubmissionError('专家原生请求核对失败，未重复发送')
                state = await page.evaluate("()=>{try{return JSON.parse(document.documentElement.getAttribute('data-dola-protocol-send-status'))}catch{return null}}") or {}
                if state.get('phase') == 'blocked':
                    reason = str(state.get('reason') or '插件停止发送')
                    if not reason.startswith('原生发送按钮未产生真实 HTTP 请求'):
                        raise ExpertSubmissionError(reason)
                    # The plugin's 8-second observation window is shorter than
                    # this worker's receipt deadline. Keep observing, never resend.
                    if not waiting_for_late_request and on_event:
                        on_event('awaiting_receipt', {'reason': '插件暂未捕获请求，继续等待原发送回执；不会重发'})
                    waiting_for_late_request = True
                cid = (((state.get('snapshot') or {}).get('conversation') or {}).get('createdId') or '')
                tail = urlsplit(page.url).path.rstrip('/').split('/')[-1]
                if sent and (str(cid).isdigit() or tail.isdigit()):
                    conversation = str(cid) if str(cid).isdigit() else tail
                    break
                await asyncio.sleep(.5)
            if not conversation:
                error = ExpertSubmissionError('生成请求会话未确认，保留发送记录，未再次申请')
                if sent:
                    error.task_phase = 'submission_unknown'
                raise error
            save_poll_state(account, conversation, await context.cookies('https://www.dola.com'),
                            await page.evaluate('navigator.userAgent'))
            if on_conversation_id:
                on_conversation_id(account, conversation, deadline)
            await wait_for_receipt(page, account, conversation, deadline,
                                   on_poll=on_poll, on_event=on_event)
        finally:
            armed = False
            if options.get('keep_browser_open') and not viewer_closed.is_set():
                if on_event:
                    from video_evidence import safe_text
                    pending_error = sys.exc_info()[1]
                    reason = '本次测试窗口保留中；查看完后请手动关闭，期间不会重复提交'
                    if pending_error:
                        reason += '；当前问题：' + safe_text(pending_error)
                    on_event('trace_ready', {'reason': reason})
                await viewer_closed.wait()
            if page and conversation:
                # Keep the early durable snapshot for recovery, and refresh it
                # before handing this same conversation to background HTTP IO.
                async def refresh_poll_state():
                    save_poll_state(account, conversation, await context.cookies('https://www.dola.com'),
                                    await page.evaluate('navigator.userAgent'))
                try:
                    await asyncio.wait_for(refresh_poll_state(), timeout=5)
                except Exception:
                    # A final snapshot failure must not replace a quota/refusal
                    # result, or discard the original recoverable snapshot.
                    pass
            if page:
                try:
                    await page.evaluate('''()=>{const k='dola-project-plugin-preferences-v1';const saved=JSON.parse(sessionStorage.getItem(k)||'null');
                      if(saved){for(const [key,value]of Object.entries(saved)){if(value===null)localStorage.removeItem(key);else localStorage.setItem(key,value);}sessionStorage.removeItem(k);}}''')
                except Exception:
                    pass
            await _close_submission_context(context)
    if on_browser_closed:
        on_browser_closed()
    return await resume_video(account, conversation, max(1, deadline-time.time()), on_poll=on_poll,
        deadline_at=deadline, on_event=on_event, generation_options=options, scheduler=scheduler)
