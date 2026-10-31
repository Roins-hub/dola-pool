"""Separate, single-submit Dola Pro 15-second experiment using native page controls."""
import asyncio
import html
import json
import re
import time
from urllib.parse import urlsplit
import aiohttp

from video_outcome import PlatformNoChargeError, VideoQueryTimeout
from video_prompt import build_video_prompt


class ExpertSubmissionError(RuntimeError):
    task_phase = 'not_accepted'


class ExpertWeeklyLimitError(PlatformNoChargeError):
    """The Pro conversation explicitly refused its free weekly allowance."""

    task_phase = 'expert_weekly_limit'

    def __init__(self, reply):
        from video_evidence import safe_text
        self.reply = safe_text(reply)
        RuntimeError.__init__(self, 'Dola 专家 Pro 免费 7 天额度已用完，本次未进入视频生成：' + self.reply)


EXPERT_LABEL = re.compile(r'^(Pro|Expert|Experte|专家|專家)$')
FAST_LABEL = re.compile(r'^(Schnell|Fast|快速)$')
EXPERT_WEEKLY_LIMIT = re.compile(
    r'^Du\s+hast\s+das\s+kostenlose\s+7-Tage-Limit\s+erreicht\.\s*'
    r'Komm\s+am\s+.{1,120}?\s+wieder\s+oder\s+chatte\s+im\s+Fast-Modus\.?$', re.I | re.S)


async def select_expert_mode(page):
    active = page.get_by_role('button', name=EXPERT_LABEL)
    if await active.is_visible():return
    fast = page.get_by_role('button', name=FAST_LABEL).last
    choice = page.get_by_text(EXPERT_LABEL)
    for attempt in range(2):
        try:
            # Dola occasionally renders the Schnell button before its menu is ready.
            # Reopen an empty menu once; no prompt is entered or sent here.
            await fast.click(timeout=30000)
            await choice.wait_for(state='visible', timeout=7000)
            await choice.click(timeout=5000)
            await active.wait_for(state='visible', timeout=20000)
            return
        except Exception as error:
            if await active.is_visible():return
            if attempt == 0:
                await page.keyboard.press('Escape')
                await page.wait_for_timeout(750)
                continue
            visible = await page.locator('[role="menuitem"],[role="option"],[role="menu"]').all_text_contents()
            detail = ' / '.join(visible)[-800:]
            raise ExpertSubmissionError('当前账号未找到可用的专家模式入口，提示词未发送。菜单：'+(detail or '没有专家选项')) from error


def build_expert_prompt(prompt, ratio=None):
    instructions = '请使用 Seedance 2.0 Fast 生成一条15秒视频。'
    if ratio and ratio != 'default':
        instructions += f'视频比例：{ratio}。'
    return instructions + '\n\n' + build_video_prompt(prompt, 15, 'seedance-2.0')


def media_url(url):
    parsed = urlsplit(url)
    return parsed.scheme in ('http', 'https') and bool(re.fullmatch(r'v\d+-dola\.dola\.com', parsed.hostname or ''))


def parse_expert_messages(data, conversation):
    result = {'accepted': False, 'videos': [], 'reply': ''}
    body = data.get('downlink_body') if isinstance(data, dict) else None
    chain = body.get('pull_singe_chain_downlink_body') if isinstance(body, dict) else None
    if not isinstance(chain, dict) or not isinstance(chain.get('messages'), list):return result
    for message in chain.get('messages') or []:
        if not isinstance(message, dict):continue
        if str(message.get('conversation_id')) != str(conversation):
            continue
        if message.get('user_type') != 2 and message.get('role') != 'assistant':
            continue
        blocks = message.get('content') or []
        if isinstance(blocks, str):
            try: blocks = json.loads(blocks)
            except ValueError: continue
        if not isinstance(blocks, list):continue
        fields = [block['content'] for block in blocks if isinstance(block, dict) and isinstance(block.get('content'), dict)]
        tools = [f['generic_tool_block'] for f in fields if isinstance(f.get('generic_tool_block'), dict)
                 and f['generic_tool_block'].get('tool_name') == 'text_to_video']
        invoked = any(t.get('status') in (1, 2, 3, 4) for t in tools)
        result['accepted'] |= invoked
        texts = [f['text_block']['text'] for f in fields if isinstance(f.get('text_block'), dict)
                 and isinstance(f['text_block'].get('text'), str)]
        result['reply'] = '\n\n'.join(texts)[-6000:]
        if not any(t.get('status') == 4 for t in tools):continue
        for text in texts:
            for value in re.findall(r'https?://[^\s<>"\)\]]+', text):
                url = html.unescape(value)
                if media_url(url) and url not in result['videos']:
                    result['videos'].append(url)
    return result


def native_request_audit(body, prompt):
    option = body.get('option') or {}
    texts = [block.get('content', {}).get('text_block', {}).get('text')
             for message in body.get('messages', []) for block in message.get('content_block', [])
             if 'text_block' in block.get('content', {})]
    values = {'model_item_key': (option.get('model_config') or {}).get('model_item_key'),
              'agent_mode': option.get('agent_mode'), 'conversation_mode': option.get('conversation_mode'),
              'prompt_matches': texts == [prompt], 'text_blocks': len(texts),
              'expected_chars': len(prompt), 'actual_chars': sum(len(t) for t in texts if isinstance(t, str))}
    # The current native Expert page omits conversation_mode; older Pro sends 2.
    # Keep the actual model/agent selection and complete prompt mandatory.
    values['matches'] = (str(values['model_item_key']) == '4' and str(values['agent_mode']) == '1'
                         and (values['conversation_mode'] is None or str(values['conversation_mode']) == '2')
                         and values['prompt_matches'])
    return values


def validate_native_request(body, prompt):
    return native_request_audit(body, prompt)['matches']


def expert_outcome(poll):
    from video_outcome import conversation_outcome
    expert = poll.get('expert') or {}
    scoped = {**poll, 'videos': expert.get('videos') or [],
              'assistant_messages': poll.get('assistant_messages') if poll.get('assistant_messages') is not None
                                    else [[expert.get('reply', '')]]}
    outcome = conversation_outcome(scoped, model='seedance-2.0')
    reply = expert.get('reply') or ''
    if (not expert.get('accepted') and not scoped['videos']
            and isinstance(reply, str) and EXPERT_WEEKLY_LIMIT.fullmatch(reply.strip())):
        outcome.update(state='failed', error=ExpertWeeklyLimitError(reply))
        return scoped, outcome
    if not outcome['error']:
        outcome['state'] = 'completed' if scoped['videos'] else 'accepted' if expert.get('accepted') else 'awaiting_receipt'
    return scoped, outcome


async def collect_result(session, url):
    """Ask the platform for the same video's original; never rewrite a media URL."""
    from video_worker import _download
    from dola_watermark import PARAMS
    from video_delivery import NoWatermarkAvailable, dola_download, known_watermarked
    from expert_trace import record_original
    import config, proxy_usage
    account = session.account
    local = await _download(url, account, proxy_url=session.proxy_url, own_isp=session.own_isp,
                            cookies=session.state.get('cookies'))
    result = {'local_path': str(local), 'video_url': url, 'account': account,
              'conversation_id': session.conversation_id, 'download_source': 'preview', 'watermark': 'unknown'}
    match = re.search(rb'vid:(v[0-9A-Za-z]{20,50})', local.read_bytes())
    if not match:return result
    vid = match.group(1).decode()
    if session.proxy_url and session.own_isp:
        proxy_usage.usage.reserve(config._parse_proxy(session.proxy_url), 1)
    async with session.session.post('https://www.dola.com/creativity/resource/get_without_watermark',
            params=PARAMS, json={'vid': [vid]}, proxy=session.proxy_url, allow_redirects=False) as response:
        if response.status != 200:
            record_original(account, session.conversation_id, vid, response.status)
            return result
        payload = await response.json(content_type=None)
        record_original(account, session.conversation_id, vid, response.status, payload)
    if not isinstance(payload, dict):return result
    data = payload.get('data') or {}
    if not isinstance(data, dict):return result
    if payload.get('code') not in (0, '0') or data.get('without_watermark') is not True:return result
    entries = data.get('download_video') or {}
    if not isinstance(entries, dict):return result
    item = entries.get(vid)
    # The official download UI prefers download_video[vid].video_model, then
    # download_url. Only use a decoded video_model URL that explicitly says
    # unwatermarked; the enclosing API must also confirm this same VID.
    originals = []
    if isinstance(item, dict):
        if item.get('video_model'):
            try:
                model_source = dola_download(item['video_model'], '')
            except NoWatermarkAvailable:
                model_source = None
            if model_source and model_source['watermark'] == 'verified_download':
                originals.append(model_source['url'])
        originals.extend((item.get('download_url'), item.get('url')))
    else:
        originals.append(item)
    for original in originals:
        if not isinstance(original, str) or known_watermarked(original):
            continue
        try:
            parsed = urlsplit(original)
            port = 443 if parsed.scheme == 'https' else 80
            if (not media_url(original) or parsed.username is not None or parsed.password is not None
                    or parsed.port not in (None, port)):
                continue
        except ValueError:
            continue
        local = await _download(original, account, proxy_url=session.proxy_url, own_isp=session.own_isp,
                                cookies=session.state.get('cookies'))
        return {**result, 'local_path': str(local), 'video_url': original,
                'download_source': 'dola_original_api', 'watermark': 'verified_download'}
    return result


async def resume_video(account, conversation_id, timeout, on_poll=None, deadline_at=None,
                       on_event=None, **_ignored):
    from video_polling import HttpVideoSession, PollingResponseError
    from expert_trace import record_poll
    deadline = deadline_at or time.time() + timeout
    accepted = False
    async with HttpVideoSession(account, conversation_id) as session:
        while time.time() < deadline:
            try:
                poll = await session.poll()
            except (aiohttp.ClientError, OSError, TimeoutError, PollingResponseError) as error:
                if on_event:on_event('polling' if accepted else 'awaiting_receipt',
                    {'reason': f'原会话查询暂时断开（{type(error).__name__}），保持原账号和线路重查；未重新发送'})
                await asyncio.sleep(min(15, max(0, deadline-time.time())))
                continue
            expert = poll.get('expert') or {}
            _, outcome = expert_outcome(poll)
            receipt = outcome['state'] in ('accepted', 'completed')
            record_poll(account, conversation_id, receipt, len(expert.get('videos') or []))
            if on_poll:on_poll(time.time())
            if receipt and not accepted:
                accepted = True
                if on_event:on_event('accepted', {'reason': '专家模式已收到视频生成受理回执'})
            if on_event and expert.get('reply'):
                on_event('polling' if accepted else 'awaiting_receipt', {'reply': expert['reply']})
            if expert.get('videos'):
                if on_event:on_event('downloading', {'reason': '专家模式工具已完成，核对同一视频的原片下载'})
                return await collect_result(session, expert['videos'][0])
            if outcome.get('error'):raise outcome['error']
            await asyncio.sleep(min(15, max(0, deadline-time.time())))
    raise VideoQueryTimeout('专家模式原会话未取得成片；没有再次提交或换号')


async def generate_video(account, prompt, ratio=None, duration=15, timeout=None, model='seedance-2.0',
                         on_conversation_id=None, on_poll=None, on_event=None, on_prompt_sent=None,
                         on_browser_closed=None, reference_image_paths=None, **_ignored):
    import config
    from browser import launch_account_context
    from dola_watermark import ensure_page_preference
    from expert_trace import BrowserTrace
    from video_polling import save_poll_state
    from video_worker_ui import (_submission_playwright, _close_submission_context,
        _fill_video_prompt, _dismiss_cookie_banner, _dismiss_login_modal, goto_retry)
    if duration != 15 or reference_image_paths:
        raise ExpertSubmissionError('专家模式试验当前仅支持文字提示词、15秒；未提交')
    sent = 0
    blocked = False
    text = build_expert_prompt(prompt, ratio)
    async with _submission_playwright() as driver:
        context = await launch_account_context(driver, account, headless=False, use_extension=False)
        trace = BrowserTrace(account)
        trace.attach(context)
        page = context.pages[0] if context.pages else await context.new_page()
        try:
            if trace.path and on_event:
                on_event('trace_ready', {'trace_id': trace.path.stem})
            await goto_retry(page, 'https://www.dola.com/chat')
            await page.wait_for_timeout(3000)
            await _dismiss_cookie_banner(page)
            await _dismiss_login_modal(page)
            preference = await ensure_page_preference(page, account)
            trace.record('preference_checked', enabled=preference['enabled'],
                         changed=preference['changed'])
            await select_expert_mode(page)
            for frame in await page.locator('iframe[src*="captcha"],iframe[src*="verify"]').all():
                if await frame.is_visible():raise ExpertSubmissionError('需要人工验证，未发送提示词')
            await _fill_video_prompt(page, text)
            async def guard(route):
                nonlocal sent, blocked
                request = route.request
                if request.method != 'POST':return await route.continue_()
                try:audit = native_request_audit(request.post_data_json, text)
                except Exception:audit = {'matches': False, 'reason': '原生请求不是预期的JSON结构'}
                valid = audit['matches']
                if sent or not valid:
                    blocked = True
                    if on_event:on_event('request_mismatch', {**audit, 'duplicate': bool(sent),
                        'reason': '专家模式发送前核对失败，已拦截；'+json.dumps(audit, ensure_ascii=False)})
                    await route.abort()
                    return
                if on_event:on_event('submitted', {'matches': True, 'entry_mode': 'Pro',
                    'reason': '原生 Pro 模式及完整提示词核对通过；Fast/15秒为提示词要求，实际出片另行核对'})
                if on_prompt_sent:on_prompt_sent(account)
                sent += 1
                await route.continue_()
            await page.route('**/chat/completion*', guard)
            await page.keyboard.press('Enter')
            until = time.monotonic()+90
            conversation = ''
            while time.monotonic() < until:
                if blocked:raise ExpertSubmissionError('专家模式原生请求核对未通过；未重复发送')
                tail = urlsplit(page.url).path.rstrip('/').split('/')[-1]
                if sent and tail.isdigit():
                    conversation = tail
                    break
                await asyncio.sleep(1)
            if not conversation:
                raise ExpertSubmissionError('专家模式提交结果未确认，未再次发送或换号')
            trace.record('conversation_confirmed', conversation_id=conversation, request_count=sent)
            deadline = time.time() + (timeout or config.VIDEO_TIMEOUT)
            save_poll_state(account, conversation, await context.cookies('https://www.dola.com'),
                            await page.evaluate('navigator.userAgent'))
            if on_conversation_id:on_conversation_id(account, conversation, deadline)
            result = await resume_video(account, conversation, timeout or config.VIDEO_TIMEOUT,
                                      deadline_at=deadline, on_poll=on_poll, on_event=on_event)
        finally:
            # Restore the account's ordinary composer for later normal tasks.
            try:
                if await page.get_by_role('button', name=EXPERT_LABEL).is_visible():
                    await page.get_by_role('button', name=EXPERT_LABEL).click(timeout=3000)
                    await page.get_by_text(FAST_LABEL).last.click(timeout=3000)
            except Exception:pass
            try:
                await trace.flush()
            except Exception:
                # Diagnostic capture must not strand the account browser.
                pass
            await _close_submission_context(context)
    if on_browser_closed:on_browser_closed()
    return result
