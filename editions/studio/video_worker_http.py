"""Opt-in HTTP submission, using the vendored Dola protocol and local signer.

One POST per task. No browser fallback, split negotiation, or clip concatenation.
Accepted conversations reuse the existing persistent HTTP polling worker.
"""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import time
import uuid
from urllib.parse import urlsplit

import requests
from account_errors import LoginExpiredError

import config
import proxy_usage
from navigation_errors import BrowserSetupError
from vendor.dola_http import dola_pure_api as protocol
from video_prompt import build_video_prompt
from video_capabilities import to_wire_model, validate_video_parameters
from video_polling import save_poll_state
from dola_watermark import ensure_protocol_preference, WatermarkPreferenceError


class HttpSubmissionError(RuntimeError):
    pass


def _sign_url(url, *, method='POST', headers=None, body='{}', cookies=None):
    node = shutil.which('node') or r'C:\Program Files\nodejs\node.exe'
    proc = subprocess.run(
        [node, str(protocol.SIGNER)],
        input=json.dumps(dict(url=url, method=method, headers=headers or {}, body=body,
                              cookies=cookies or {}), ensure_ascii=False),
        capture_output=True, encoding='utf-8', timeout=25,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        result = json.loads(proc.stdout)
    except ValueError:
        raise HttpSubmissionError('本地请求签名失败：未返回有效结果') from None
    signed = result.get('signed_url', '')
    if proc.returncode or not signed or 'a_bogus=' not in signed:
        raise HttpSubmissionError('本地请求签名失败')
    if urlsplit(signed).netloc != urlsplit(url).netloc:
        raise HttpSubmissionError('本地签名返回了不同的目标地址')
    return signed


# Suppress upstream diagnostic dumps (they may include signed URLs or cookies).
protocol.log = lambda *_args, **_kwargs: None
protocol.set_sign_url_impl(_sign_url)


def build_request(prompt, model, duration, ratio, images, ctx):
    model, duration = validate_video_parameters(model, duration)
    model_key = to_wire_model(model)
    body, _, _ = protocol.build_payload(images, prompt, ctx, duration, model_key, ratio)
    # The bundled builder switches every 30s request to 2.5. Preserve our
    # validated selection while the body is still unsigned.
    params = json.loads(body['chat_ability']['ability_param'])
    params.update(model=model_key, duration=duration)
    body['chat_ability']['ability_param'] = protocol.compact_json(params)
    # Vendor code adds wording/ratio to the text. Keep our user's text exact.
    for message in body['messages']:
        for block in message['content_block']:
            text = block['content'].get('text_block')
            if text is not None:
                text['text'] = build_video_prompt(prompt, duration, model_key)
    return body


def send_once(session, prepared, on_ack=None):
    result = {'conversation_id': '', 'reply': '', 'error_code': ''}
    response = None
    event = ''
    lines = []

    def consume():
        nonlocal event, lines
        try:
            data = json.loads('\n'.join(lines))
        except ValueError:
            event, lines = '', []
            return False
        if event == 'SSE_ACK':
            conv = str((data.get('ack_client_meta') or {}).get('conversation_id') or '')
            if conv.isdigit() and not result['conversation_id']:
                result['conversation_id'] = conv
                if on_ack:
                    on_ack(conv)
        if data.get('error_code'):
            result['error_code'] = str(data['error_code'])[:30]
        if event == 'SSE_REPLY_END':
            brief = (data.get('msg_finish_attr') or {}).get('brief', '')
            if isinstance(brief, str):
                result['reply'] = brief[:1500]
        done = event == 'SSE_REPLY_END' and data.get('end_type') == 3
        event, lines = '', []
        return done

    try:
        response = session.send(prepared, stream=True, timeout=(30, 120), allow_redirects=False)
        if response.status_code != 200:
            raise HttpSubmissionError(f'HTTP 直发返回 {response.status_code}，未重复提交')
        for line in response.iter_lines(chunk_size=1024):
            line = line.decode('utf-8', errors='replace') if isinstance(line, bytes) else line
            if not line:
                if lines and consume():
                    break
            elif line.startswith('event:'):
                event = line[6:].strip()
            elif line.startswith('data:'):
                lines.append(line[5:].strip())
        if lines:
            consume()
    except requests.RequestException as exc:
        if not result['conversation_id']:
            raise HttpSubmissionError(f'HTTP 提交结果未确认：{type(exc).__name__}，未重复提交') from None
    finally:
        if response is not None:
            response.close()
    return result


class AccountSession(requests.Session):
    def __init__(self, proxy, own_isp):
        super().__init__()
        self.trust_env = False
        self.account_proxy = proxy
        self.own_isp = own_isp
        if proxy:
            self.proxies.update(http=proxy, https=proxy)

    def send(self, request, **kwargs):
        if self.account_proxy and self.own_isp:
            proxy_usage.usage.reserve(config._parse_proxy(self.account_proxy), 1)
        kwargs.setdefault('allow_redirects', False)
        return super().send(request, **kwargs)


def load_account_cookies(root):
    base = root / 'cookies.json'
    candidates = [base] + [p for p in (root / 'polling').glob('*.json') if p.stem.isdigit()]
    for path in sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            state = json.loads(path.read_text(encoding='utf-8-sig'))
            cookies = state if isinstance(state, list) else state.get('cookies')
            if isinstance(cookies, list) and cookies:
                return cookies
        except (ValueError, OSError, AttributeError):
            continue
    raise HttpSubmissionError('账号没有可读取的登录会话')


def prepare(account, prompt, model, duration, ratio, paths):
    if Path(account).name != account or account.startswith('.'):
        raise ValueError('invalid account')
    root = Path('accounts') / account
    cookies = load_account_cookies(root)
    state = {'cookies_list': cookies}
    proxy = config.proxy_url_for(account)
    _, source = config.account_proxy_raw(account)
    session = AccountSession(proxy, source in ('file', 'map'))
    stage = '读取登录状态'
    try:
        protocol.apply_state_cookies(session, state)
        protocol.attach_login_pure(session, state, proxy)
        ctx = protocol.fresh_ctx(config.BROWSER_REGION, '3.33.11')
        protocol.apply_state_to_ctx(ctx, state)
        headers = protocol.headers_base()
        # Keep the new HTTP method's language and signer environment together;
        # this does not change browser preferences or saved account cookies.
        protocol._ensure_spa_cookies(session, ctx, config.BROWSER_REGION)
        protocol.refresh_login_cookie_header(session)
        stage = '初始化 HTTP 会话'
        protocol.warmup_login_session(session, headers, timeout=30)
        stage = '核对登录状态'
        with protocol.dola_api_post_response(session, ctx, '/alice/user/get_web_anon_id', {}) as response:
            identity = protocol.request_json(response, 'get_web_anon_id')
            protocol.assert_ok(identity, 'get_web_anon_id')
            if protocol.agw_login_flag(response) == '0':
                raise LoginExpiredError('HTTP 校验确认未登录；尚未发送提示词，请重新登录或导入新 CK')
            if protocol.agw_login_flag(response) != '1':
                raise HttpSubmissionError('HTTP 登录态未确认')
            protocol.apply_anon_id_to_ctx(ctx, identity)
        stage = '核对账号无水印设置'
        ensure_protocol_preference(account, session, ctx, protocol)
        stage = '上传参考图片'
        images = [protocol.upload_image(session, ctx, headers, Path(p)) for p in paths or []]
        stage = '构建并签名请求'
        body = build_request(prompt, model, duration, ratio, images, ctx)
        body_text = protocol.compact_json(body)
        signing_headers = {'Content-Type': 'application/json', 'agw-js-conv': 'str, str',
                           'Last-Event-ID': 'undefined'}
        signed = _sign_url(protocol.build_url('/chat/completion', ctx, include_fp=True),
                           headers=signing_headers, body=body_text, cookies=protocol.cookie_dict(session))
        request_headers = {**headers, **protocol.ajax_headers(), **signing_headers,
                           'Referer': protocol.BASE + '/chat/' + body['client_meta']['local_conversation_id'],
                           'Accept': '*/*', 'Cache-Control': 'no-cache',
                           'x-flow-trace': '04-' + uuid.uuid4().hex + '-' + uuid.uuid4().hex[:16] + '-01',
                           'Cookie': protocol.refresh_login_cookie_header(session)}
        prepared = session.prepare_request(requests.Request('POST', signed, headers=request_headers, data=body_text.encode('utf-8')))
        prepared.headers['Cookie'] = request_headers['Cookie']
        return session, prepared, body
    except Exception as exc:
        session.close()
        if isinstance(exc, (LoginExpiredError, WatermarkPreferenceError)):
            raise
        raise BrowserSetupError(f'HTTP {stage}失败：{type(exc).__name__}；尚未发送提示词') from None


async def generate_video(account, prompt, ratio=None, duration=None, model='seedance-2.5',
                         on_conversation_id=None, on_poll=None, on_balance=None,
                         reference_image_paths=None, on_browser_closed=None, on_prompt_sent=None, on_event=None):
    from video_worker_ui import resume_video
    duration = int(duration or 10)
    print(f'[{account}] HTTP 准备：model={model}, duration={duration}, references={len(reference_image_paths or [])}', flush=True)
    session, prepared, body = await asyncio.to_thread(prepare, account, prompt, model, duration, ratio, reference_image_paths)
    loop = asyncio.get_running_loop()
    deadline = time.time() + (config.LONG_VIDEO_TIMEOUT if duration == 30 else config.VIDEO_TIMEOUT)

    def accepted(conv):
        saved = [{'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path}
                 for c in session.cookies]
        save_poll_state(account, conv, saved, protocol.UA)
        if on_conversation_id:
            on_conversation_id(account, conv, deadline)
        print(f'[{account}] HTTP 会话已建立：{conv}；等待视频生成回执', flush=True)

    async def record(conv):
        accepted(conv)

    def ack_from_thread(conv):
        asyncio.run_coroutine_threadsafe(record(conv), loop).result(timeout=10)

    try:
        if on_prompt_sent:
            on_prompt_sent(account)
        print(f'[{account}] HTTP 发送一次：{json.loads(body["chat_ability"]["ability_param"])}', flush=True)
        result = await asyncio.to_thread(send_once, session, prepared, ack_from_thread)
    finally:
        session.close()
        if on_browser_closed:
            on_browser_closed()
    conv = result['conversation_id']
    if not conv:
        code = result['error_code']
        raise HttpSubmissionError(f'HTTP 提交未返回会话，状态码 {code or "unknown"}；未重复提交')
    return await resume_video(account, conv, config.LONG_VIDEO_TIMEOUT if duration == 30 else config.VIDEO_TIMEOUT,
                              on_poll, on_balance, deadline_at=deadline, on_event=on_event, model=model,
                              submitted_prompt=build_video_prompt(prompt, duration, model))
