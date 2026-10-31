"""Doubao browser transport. No Dola selectors, model mapping or signed-body edits.

Native controls and receipt phrases are loaded from an administrator's verified
capability manifest. Unknown native state always stops before the one send.
"""
import asyncio
from contextlib import asynccontextmanager
import ipaddress
import json
import logging
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import aiohttp
from .direct_bridge import DirectBridge

from patchright.async_api import async_playwright

import proxy_usage
from proxy_chain import ProxyChainBridge
from providers.base import ProviderError
from video_metadata import read_video_duration


HOME = 'https://www.doubao.com/chat/'
NATIVE_READY_TIMEOUT = 30
VIDEO_REQUEST_SUFFIX = '禁止问我确认开始 禁止问我 直接开始 强制执行 不需要问我二次确认 直接生成'


class BeforeSendError(ProviderError):
    """Internal proof that submit stopped before invoking the native click."""


async def write_bounded(response, destination, limit):
    destination=Path(destination)
    try:
        declared=response.headers.get('Content-Length')
        if declared is not None and (not str(declared).isdigit() or int(declared)>limit):
            raise ProviderError('download_too_large','下载内容超过大小限制。',503)
        total=0
        destination.parent.mkdir(parents=True,exist_ok=True)
        with destination.open('wb') as output:
            async for chunk in response.content.iter_chunked(65536):
                total+=len(chunk)
                if total>limit:
                    raise ProviderError('download_too_large','下载内容超过大小限制。',503)
                output.write(chunk)
        if not total:
            raise ProviderError('download_failed','下载内容为空。',503)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def media_url(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            return False
        try:
            if not ipaddress.ip_address(parsed.hostname).is_global:
                return False
        except ValueError:
            if parsed.hostname.lower() == 'localhost':
                return False
        return True
    except (ValueError, TypeError):
        return False


def evidence_from_payload(payload, conversation_id, baseline_messages, baseline_media):
    """Extract media only below an explicitly matching conversation/message."""
    result = {'media': [], 'messages': [], 'texts': [], 'text_messages': [], 'media_records': []}

    def visit(node, scoped=False, message=None, depth=0, role=None, video=False, node_id=None, fallback=None):
        if depth > 30:
            return
        if isinstance(node, str):
            if node[:1] in ('{', '['):
                try:
                    visit(json.loads(node), scoped, message, depth+1, role, video, node_id, fallback)
                except (ValueError, TypeError):
                    pass
            return
        if isinstance(node, list):
            for child in node:
                visit(child, scoped, message, depth+1, role, video, node_id, fallback)
            return
        if not isinstance(node, dict):
            return
        observed_node=next((node.get(key) for key in ('node_id','nodeId','node_id_str','nodeIdStr') if isinstance(node.get(key),str)),None)
        node_id=observed_node or node_id
        fallback=node.get('fallback_api') if isinstance(node.get('fallback_api'),str) else fallback
        role = node.get('role', node.get('message_role', role))
        assistant = role in ('assistant', 'model', 'bot', 2, '2')
        video = video or any(str(node.get(key, '')).lower() in ('video', 'video/mp4', 'video_mp4')
                             for key in ('type', 'media_type', 'content_type', 'mime_type'))
        conv = node.get('conversation_id') or node.get('conversationId') or node.get('local_conversation_id')
        if conv is not None:
            scoped = str(conv) == str(conversation_id)
        mid = node.get('message_id') or node.get('messageId')
        if mid is None and ('content' in node or any(k in node for k in ('main_url', 'fallback_api'))):
            mid = node.get('id')
        if mid is not None:
            message = str(mid)
        fresh = scoped and message is not None and message not in baseline_messages
        if fresh and assistant:
            if message not in result['messages']:
                result['messages'].append(message)
            for key in ('main_url', 'fallback_api', 'download_url', 'video_url'):
                value = node.get(key)
                if not isinstance(value, str):
                    continue
                parsed = urlsplit(value)
                video_url = bool(re.search(r'\.mp4$', parsed.path, re.I)
                                 or re.search(r'(?:mime_type|format)=(?:video_mp4|video%2Fmp4|mp4)(?:&|$)', parsed.query, re.I))
                recognized = video if key == 'fallback_api' else video or video_url or key == 'video_url'
                if recognized and value not in baseline_media and media_url(value) and value not in result['media']:
                    result['media'].append(value)
                    result['media_records'].append({'url':value,'conversation_id':conversation_id,
                        'message_id':message,'node_id':node_id,'fallback_api':fallback})
            for key in ('text', 'content'):
                value = node.get(key)
                if isinstance(value, str) and value[:1] not in ('{', '['):
                    result['texts'].append(value[:10000])
                    result['text_messages'].append({'id': message, 'text': value[:10000]})
        for key, value in node.items():
            if isinstance(value, (list, dict)) or (isinstance(value, str) and value[:1] in ('{', '[')):
                visit(value, scoped, message, depth+1, role,
                      video or key in ('video', 'video_info', 'video_result', 'generated_video', 'video_generation'),node_id,fallback)

    if conversation_id:
        visit(payload)
    return result


def chat_identity(url):
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('www.doubao.com', 'doubao.com'):
        return None
    match = re.fullmatch(r'/chat/([^/?#]+)/?', parsed.path)
    if not match or match.group(1) in ('new', 'local') or match.group(1).startswith('local_'):
        return None
    return match.group(1)


def receipt_state(text, capability):
    """Classify one assistant message, excluding hypothetical failure notices."""
    from .confirmation import receipt_text_without_confirmation
    from .review import review_rejection
    observed = receipt_text_without_confirmation(text)
    if capability.get('entry_mode') == 'ordinary_dialog' and re.search(r'已生成(?:程式碼|代码)|```', observed):
        return 'unknown'
    if review_rejection(observed):
        return 'rejected'
    if (capability.get('entry_mode') == 'ordinary_dialog'
            and not re.search(r'如果|假如|倘若|\bif\b', observed, re.I)
            and re.search(r'我(?:这边|這邊|目前|当前|當前|现在|現在)?(?:暂时|暫時)?(?:没有|沒有|无法|無法|不能).{0,12}(?:调用|調用|使用|访问|訪問).{0,12}(?:视频|視頻|影片)生成(?:工具|功能|能力)', observed)):
        return 'rejected'
    accepted = [(observed.rfind(pattern), 'accepted') for pattern in capability['receipt_patterns'] if pattern in observed]
    rejected = []
    for pattern in capability['rejection_patterns']:
        for match in re.finditer(re.escape(pattern), text):
            before = text[max(0, match.start()-25):match.start()]
            if re.search(r'(?:如果|假如|若|万一|如遇|倘若)[^。！？!?；;\n，,]{0,12}$|\bif\s+(?:the\s+)?(?:video\s+|generation\s+|task\s+)?$', before, re.I):
                continue
            rejected.append((match.start(), 'rejected'))
    signals = accepted + rejected
    # Native receipts can acknowledge a model, ETA and delivery
    # promise without using the legacy exact "开始生成" phrase.
    if not re.search(r'如果|假如',observed):
        model=re.search(r'Seedance\s*2\.(?:0|5)(?:\s*(?:Fast|Mini))?',observed,re.I)
        video=re.search(r'视频|Seedance',observed,re.I)
        progress=re.search(r'视频生成中|正在.{0,25}生成|创作中|排队中|视频任务.{0,12}(?:已提交|已受理|已创建)',observed)
        eta=re.search(r'(?:预计|大约|需要|约).{0,12}\d+\s*(?:秒|分钟|小时)',observed)
        delivery=re.search(r'(?:完成|生成好).{0,20}(?:通知|发给|发送给)',observed)
        acknowledgement=re.search(r'(?:已使用|已经使用|正在使用|本次使用|已开始).{0,50}(?:视频|Seedance)',observed,re.I)
        score=bool(model)+bool(eta)+2*bool(delivery)+3*bool(acknowledgement)+2*bool(progress)
        if video and (progress or score>=4):
            positions=[match.end() for match in (progress,acknowledgement,delivery) if match]
            signals.append((max(positions,default=0),'accepted'))
    result=max(signals)[1] if signals else 'unknown'
    if result=='accepted' and capability.get('entry_mode')=='ordinary_dialog':
        from .confirmation import ordinary_dialog_spec_matches
        if not ordinary_dialog_spec_matches(observed,{'model':capability.get('id')},require_all=False):
            return 'unknown'
    return result


VERIFICATION_GRACE_SECONDS = 30
VERIFICATION_WAIT_SECONDS = 15 * 60
VERIFICATION_POLL_SECONDS = 1


class BrowserSession:
    def __init__(self, provider, account, snapshot=None, login=False):
        self.provider, self.account, self.snapshot = provider, account, snapshot or {}
        self.login = login
        self.context = self.page = self.bridge = self.playwright = None
        self.payloads = []
        self.pending = set()
        self.capability = None
        self.binding = None
        self.request = self.snapshot.get('request',{})
        self.completion_error = None
        self.media_info = {}
        self.native_state = None
        self.dialog_media_state = None
        self.dialog_prompt_signature = None
        self.dialog_composer = None
        self.dialog_prompt = None
        self.player_attempts = set()
        self.auth_sync = {'saved': False, 'reason': 'not_checked'}
        self.on_progress = None
        self.execution_refresh_stages = set()
        self.receipt_activated = False

    @property
    def direct(self):
        # Only the provider's explicit binding enables direct access. Missing,
        # malformed, or extra-field bindings never become a fallback route.
        return self.binding == {'server': 'direct'}

    async def __aenter__(self):
        from .accounts import atomic_json
        from .environment import browser_options, create_environment, validate_environment
        if self.account.get('_login'):
            binding = self.account['_binding']
            folder = self.provider.root / 'login' / self.account['account_id']
            environment_path = folder / 'environment.json'
            if environment_path.exists():
                try:
                    env = json.loads(environment_path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    raise ProviderError('invalid_account', '豆包登录浏览器环境档案损坏，未重新创建或改用默认环境。', 409) from None
            else:
                env = create_environment(self.account['account_id'], binding)
                atomic_json(environment_path, env)
        else:
            binding = self.provider.accounts.bound_proxy(self.account['name'])
            folder = self.provider.root / 'accounts' / self.account['account_id']
            env = self.provider.accounts.browser_environment(self.account['name'])
        validate_environment(env, self.account['account_id'], binding)
        self.binding = binding
        try:
            if not self.direct:
                proxy_usage.usage.ensure_available(binding)
                self.bridge = ProxyChainBridge(binding)
                self.bridge.allowed_hosts.update({'www.doubao.com', 'doubao.com', 'api.ipify.org'})
                await self.bridge.__aenter__()
            else:
                self.bridge = DirectBridge()
                await self.bridge.__aenter__()
            self.playwright = await async_playwright().start()
            args = ['--no-first-run', '--disable-extensions', '--disable-background-networking',
                    '--disable-quic', '--disable-features=Translate,CalculateNativeWinOcclusion',
                    '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
                    '--disable-backgrounding-occluded-windows',
                    '--force-webrtc-ip-handling-policy=disable_non_proxied_udp']
            # One native Chromium channel and one saved environment are shared
            # by interactive login and background work. Device/UA rendering is
            # left to the installed browser; no JS fingerprint overrides.
            options = dict(browser_options(env), headless=self.account.get('_interactive') is not True,
                           channel='chromium', service_workers='block', args=args)
            options['proxy'] = self.bridge.browser_proxy
            self.context = await self.playwright.chromium.launch_persistent_context(str(folder / 'profile'), **options)
            if not self.login:
                from .native_bridge import install_native_media
                await install_native_media(self.context)
            await self.context.route('**/*', self.bridge.authorize_request)
            if not self.direct:
                await proxy_usage.attach_request_counter(self.context, 'doubao:'+self.account['name'], binding)
            if not self.account.get('_login') and not self.login:
                await self.seed_profile_session(folder / 'cookies.json')
            self.page = await self.context.new_page()
            for page in self.context.pages:
                if page != self.page:
                    await page.close()
            self.page.on('response', self._on_response)
            url = self.snapshot.get('page_url') or HOME
            host = urlsplit(url).hostname
            if host not in ('www.doubao.com', 'doubao.com'):
                raise ProviderError('invalid_snapshot', '恢复地址不属于豆包，已停止。')
            await self.page.goto(url, wait_until='domcontentloaded', timeout=60000)
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *_):
        failed = False
        cancelled = None
        if self.context:
            try:
                await asyncio.wait_for(self.persist_authenticated_session(), 5)
            except asyncio.CancelledError as error:
                cancelled = error
            except Exception:
                self.auth_sync = {'saved': False, 'reason': 'session_sync_unavailable'}
            try:
                await asyncio.wait_for(self.context.close(), 20)
            except BaseException:
                failed = True
        if self.pending:
            for task in self.pending:
                task.cancel()
            await asyncio.gather(*self.pending, return_exceptions=True)
        if self.playwright:
            await self.playwright.stop()
        if self.bridge:
            await self.bridge.close()
        if failed:
            scheduler = self.provider.scheduler
            if scheduler:
                scheduler.unsafe_lanes.add(scheduler.lane_id(self.binding))
            raise ProviderError('browser_cleanup_failed', '豆包浏览器未确认关闭，相关线路已隔离。', 503)
        if cancelled is not None:
            raise cancelled

    @staticmethod
    def _usable_session(cookies):
        now = time.time()
        return any(cookie.get('name') in ('sessionid', 'sessionid_ss') and cookie.get('value')
                   and (cookie.get('expires') in (None, -1) or
                        type(cookie.get('expires')) in (int, float) and cookie['expires'] > now)
                   for cookie in cookies)

    async def seed_profile_session(self, canonical_path):
        """A fresh native profile wins over older exported canonical cookies."""
        if self._usable_session(await self.context.cookies(HOME)):
            return False
        from .accounts import parse_cookies
        try:
            raw = Path(canonical_path).read_text(encoding='utf-8')
            cookies, _ = parse_cookies(raw)
        except (OSError, ValueError):
            if self.provider is not None:
                self.provider.accounts.mark_unavailable(self.account['name'], 'login_required')
            raise ProviderError('invalid_cookie', '浏览器与保存的登录会话均不可用，请恢复此账号登录。', 409) from None
        if not self._usable_session(cookies):
            if self.provider is not None:
                self.provider.accounts.mark_unavailable(self.account['name'], 'login_required')
            raise ProviderError('invalid_cookie', '保存的登录会话已到期，请恢复此账号登录。', 409)
        await self.context.add_cookies(cookies)
        return True

    async def official_user_id(self):
        """Read only the observed native account-info field in the main world."""
        if self._interactive_closed() or not self._official_login_page():
            return None
        try:
            value = await self.page.evaluate('''() => {
              const value=window._ROUTER_DATA?.loaderData?.chat_layout?.chat_layout?.accountInfo?.data?.user_id;
              if(typeof value==='string'&&/^[A-Za-z0-9_-]{1,128}$/.test(value)&&value!=='0')return value;
              if(typeof value==='number'&&Number.isSafeInteger(value)&&value>0)return String(value);
              return null;
            }''', isolated_context=False)
            from .accounts import normalize_official_user_id
            return normalize_official_user_id(value)
        except Exception:
            return None

    async def persist_authenticated_session(self):
        """Never replace canonical auth with logged-out, anonymous, or other auth."""
        self.auth_sync = {'saved': False, 'reason': 'not_authenticated'}
        if self.account.get('_login') or self.login or not self.provider or not getattr(self.provider, 'accounts', None):
            return False
        if self._interactive_closed() or not self._official_login_page():
            return False
        logged_in, _ = await self._logged_in()
        if not logged_in:
            from .page_guard import login_entry_visible
            if await login_entry_visible(self.page):
                self.provider.accounts.mark_unavailable(self.account['name'], 'login_required')
                self.auth_sync['reason'] = 'login_required'
            return False
        bound = self.provider.accounts.official_user_id(self.account['name'])
        if not bound:
            self.auth_sync['reason'] = 'official_identity_unbound'
            return False  # Initial binding requires explicit restore authorization.
        observed = await self.official_user_id()
        if observed != bound:
            self.auth_sync['reason'] = 'account_identity_mismatch' if observed else 'official_identity_unavailable'
            return False
        cookies = await self._interactive_login_cookies()
        self.provider.accounts.refresh_session(self.account['name'], json.dumps(cookies), observed)
        self.auth_sync = {'saved': True, 'reason': 'authenticated_profile_saved'}
        return True

    def _on_response(self, response):
        # Passive capture only. No fabricated HTTP polling endpoint or body edits.
        response_path=urlsplit(response.url).path
        if '/im/chain/single' not in response_path and response_path!='/chat/completion':
            return
        async def capture():
            try:
                raw = await response.text()
                if response_path=='/chat/completion':
                    from .completion import parse_completion_response
                    parsed=parse_completion_response(raw)
                    self.payloads.extend(parsed['payloads'])
                    if parsed['error']:self.completion_error=parsed['error']
                    self.payloads[:]=self.payloads[-100:]
                    return
                for line in raw.splitlines():
                    text = line.removeprefix('data:').strip()
                    if text[:1] in ('{', '['):
                        try:
                            self.payloads.append(json.loads(text))
                        except ValueError:
                            pass
                self.payloads[:] = self.payloads[-100:]
            except Exception:
                pass
        task = asyncio.create_task(capture())
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def baseline(self):
        state = await self.page.evaluate('''() => ({
            messages: Array.from(document.querySelectorAll('[data-message-id]')).map(n=>n.getAttribute('data-message-id')),
            media: Array.from(document.querySelectorAll('video,video source')).map(n=>n.currentSrc||n.src).filter(Boolean)
        })''')
        if self.capability:
            state['texts'] = await self.page.locator(self.capability['selectors']['assistant']).all_inner_texts()
        from .media_registry import VideoRegistry
        conversation=chat_identity(self.page.url)
        records=[record for payload in self.payloads for record in evidence_from_payload(payload,conversation,set(),set())['media_records']]
        state['media_keys']=VideoRegistry(conversation,records=records,urls=state['media']).keys()
        return state

    async def _logged_in(self):
        cookies = await self.context.cookies(HOME)
        auth = any(c['name'] in ('sessionid', 'sessionid_ss') and c['value'] for c in cookies)
        composer = self.page.locator('textarea, [contenteditable="true"]')
        visible_composer = any([await composer.nth(i).is_visible() for i in range(await composer.count())])
        from .page_guard import login_entry_visible
        login_visible = await login_entry_visible(self.page)
        avatar = self.page.locator('[data-testid*="avatar"], [data-testid*="user-menu"], [class*="user-avatar"], [class*="avatar"] img')
        visible_avatar = any([await avatar.nth(i).is_visible() for i in range(await avatar.count())])
        # Native account info takes precedence over layout-dependent avatars.
        # Its presence without a valid official ID cannot be rescued by stale
        # cookies or a leftover avatar. Older pages lacking this field retain
        # the existing DOM fallback until their native identity is available.
        native_info = await self.page.evaluate('''()=>{
          const layout=window._ROUTER_DATA?.loaderData?.chat_layout?.chat_layout;
          return Boolean(layout&&Object.prototype.hasOwnProperty.call(layout,'accountInfo'));
        }''',isolated_context=False)
        identity_ready=bool(await self.official_user_id()) if native_info else visible_avatar
        return bool(auth and visible_composer and identity_ready and not login_visible), cookies

    async def check_page_identity(self):
        """Require the current authenticated page, not a previous cookie check."""
        from .page_guard import login_entry_visible
        accounts = getattr(self.provider, 'accounts', None)
        deadline = time.monotonic() + NATIVE_READY_TIMEOUT
        while True:
            if await login_entry_visible(self.page):
                if accounts is not None:
                    accounts.mark_unavailable(self.account['name'], 'login_required')
                raise ProviderError('login_required', '豆包已退出登录，已暂停此账号；请恢复登录后再使用。', 409)
            identity = await self.official_user_id()
            if identity and accounts is not None:
                accounts.observe_identity(self.account['name'], identity)
            if accounts is None:
                return
            if identity and (await self._logged_in())[0]:
                return
            if time.monotonic() >= deadline:
                raise ProviderError('login_unverified', '豆包当前页面未确认有效登录和官方账号身份；提示词未发送，未标记账号失效。', 503)
            await asyncio.sleep(min(.1, max(0, deadline-time.monotonic())))

    async def verify(self, observe_only=False):
        from .page_guard import clear_download_promotion, login_entry_visible
        checks = {'configuration': True, 'proxy_auth': None if self.direct else False,
                  'route_mode': 'direct' if self.direct else 'proxy', 'egress': None, 'route_verified': False,
                  'login_verified': False, 'page': True, 'video_entry': False, 'capabilities': 'unverified'}
        ip_response = None
        try:
            if self.direct:
                async with aiohttp.ClientSession(trust_env=False) as direct_session:
                    async with direct_session.get('https://www.cloudflare.com/cdn-cgi/trace', proxy=None,
                            allow_redirects=False, timeout=aiohttp.ClientTimeout(total=15)) as response:
                        data = await response.content.read(4097)
                        if response.status != 200 or len(data) > 4096:
                            raise ValueError()
                        fields = dict(line.split('=', 1) for line in data.decode('utf-8').splitlines() if '=' in line)
                        observed = {'ip': fields['ip']}
                        response_status = response.status
            else:
                ip_response = await self.context.request.get('https://api.ipify.org?format=json', timeout=30000, max_redirects=0)
                observed = await ip_response.json()
                response_status = ip_response.status
            address = ipaddress.ip_address(observed['ip'])
            if response_status != 200 or not address.is_global:
                raise ValueError()
            checks.update(proxy_auth=None if self.direct else True, egress=str(address), route_verified=True)
        except Exception:
            checks['proxy_error'] = ('本机直连出口验证失败，未自动启用系统或环境代理。' if self.direct
                                     else '绑定 ISP 的出口验证失败，未回退其他线路。')
        finally:
            if ip_response:
                await ip_response.dispose()
        deadline=time.monotonic()+NATIVE_READY_TIMEOUT
        while True:
            await clear_download_promotion(self.page, reject_other_dialogs=False)
            checks['login_verified'], cookies = await self._logged_in()
            if await login_entry_visible(self.page):
                checks['login_state'] = 'signed_out'
                break
            if (checks['login_verified'] or time.monotonic()>=deadline
                or not any(cookie.get('name') in ('sessionid','sessionid_ss') and cookie.get('value') for cookie in cookies)):
                break
            await asyncio.sleep(min(.1,max(0,deadline-time.monotonic())))
        checks.setdefault('login_state', 'authenticated' if checks['login_verified'] else 'not_ready')
        if checks['login_verified']:
            await self.check_page_identity()
        for text in ('视频生成', '生成视频', '视频创作'):
            locator = self.page.get_by_text(text, exact=True)
            for index in range(await locator.count()):
                if await locator.nth(index).is_visible():
                    checks['video_entry'] = True
                    if not observe_only:
                        await locator.nth(index).click()
                    break
            if checks['video_entry']:
                break
        checks['observed_native_options'] = await self.page.evaluate('''() => Array.from(document.querySelectorAll('select')).map(s=>{
            const options=Array.from(s.options).map(o=>({value:o.value,label:o.textContent.trim()}));
            const hint=(s.name+' '+s.id+' '+(s.getAttribute('aria-label')||'')).toLowerCase();
            const kind=/model|模型/.test(hint)||options.some(o=>/seedance|模型/i.test(o.label))?'model':
                /duration|时长/.test(hint)||options.some(o=>/^\\d+\\s*秒$/.test(o.label))?'duration':
                /ratio|比例/.test(hint)||options.some(o=>/^\\d+:\\d+$/.test(o.value))?'ratio':'unknown';
            const selector=s.id?'#'+CSS.escape(s.id):s.name?'select[name="'+CSS.escape(s.name)+'"]':'';
            return {kind,selector,options};
        })''')
        checks['observed_selectors'] = {}
        candidates = {
            'video_entry': ['text="视频生成"', 'text="生成视频"', 'text="视频创作"'],
            'composer': ['textarea[data-testid*="input"]', '[contenteditable="true"][data-testid*="input"]', 'textarea', '[contenteditable="true"]'],
            'send': ['button[data-testid*="send"]', 'button[aria-label="发送"]', '[data-testid="chat_input_send_button"]'],
            'assistant': ['[data-message-id][data-role="assistant"]', '[data-testid="receive_message"]', '[data-role="assistant"]'],
            'upload': ['input[type="file"][accept*="image"]'],
            'upload_ready': ['[data-testid="uploaded-image"]', '[data-testid="image-preview"]'],
        }
        for key, selectors in candidates.items():
            for selector in selectors:
                candidate = self.page.locator(selector)
                count = await candidate.count()
                if (count == 1 and (key == 'upload' or await candidate.is_visible())) or (key in ('assistant','upload_ready') and count):
                    checks['observed_selectors'][key] = selector
                    break
        if not observe_only and checks['video_entry'] and checks['login_verified']:
            from .native_bridge import native_media_status
            native=await native_media_status(self.page)
            checks['native_media_bridge_available']=bool(native.get('supported'))
        checks['login'] = checks['login_verified']
        checks['observed_at'] = time.time()
        return checks

    def _interactive_closed(self):
        return self.page is None or self.page.is_closed()

    def _official_login_page(self):
        parsed = urlsplit(self.page.url)
        host = (parsed.hostname or '').lower()
        return parsed.scheme == 'https' and (host == 'doubao.com' or host.endswith('.doubao.com'))

    async def _interactive_login_cookies(self):
        from .accounts import parse_cookies
        cookies = await self.context.cookies([HOME, 'https://doubao.com/', 'https://accounts.doubao.com/'])
        cookies = [cookie for cookie in cookies
                   if (cookie.get('domain', '').lstrip('.').lower() == 'doubao.com'
                       or cookie.get('domain', '').lstrip('.').lower().endswith('.doubao.com'))]
        cookies, _ = parse_cookies(json.dumps(cookies))
        return cookies

    async def interactive_login_status(self):
        """Read official login readiness without controls or exposing credentials."""
        if self._interactive_closed():
            return {'state': 'closed', 'login_verified': False}
        if not self._official_login_page():
            return {'state': 'waiting_login', 'login_verified': False}
        try:
            try:
                await self._interactive_login_cookies()
            except ProviderError as error:
                if error.code != 'invalid_cookie':
                    raise
                return {'state': 'waiting_login', 'login_verified': False}
            logged_in, _ = await self._logged_in()
        except Exception:
            if self._interactive_closed():
                return {'state': 'closed', 'login_verified': False}
            raise
        return {'state': 'authenticated' if logged_in else 'session_ready', 'login_verified': logged_in}

    async def capture_interactive_login(self):
        """Capture a real official session, preserving uncertain native state."""
        if self._interactive_closed():
            return {'state': 'closed'}
        if not self._official_login_page():
            raise ProviderError('login_not_ready', '请回到官方豆包页面完成登录，系统会自动保存账号；浏览器仍保持打开。', 409)
        try:
            cookies = await self._interactive_login_cookies()
        except ProviderError as error:
            if error.code != 'invalid_cookie':
                raise
            raise ProviderError('login_not_ready', '尚未读取到豆包登录会话，请在浏览器完成登录，系统会继续自动检测；窗口仍保持打开。', 409) from None
        checks = await self.verify(observe_only=True)
        if not checks.get('route_verified'):
            raise ProviderError('proxy_unavailable', '登录会话已读取，但所选线路出口尚未验证；请稍后重试，浏览器仍保持打开。', 503)
        return {'state': 'captured', 'cookies': cookies, 'checks': checks,
                'login_verified': bool(checks.get('login_verified')),
                'official_user_id': await self.official_user_id()}

    async def login_step(self):
        if self.account.get('_interactive') is True:
            return await self.interactive_login_status()
        logged_in, cookies = await self._logged_in()
        if logged_in:
            checks = await self.verify()
            if not checks['route_verified']:
                raise ProviderError('proxy_unavailable',
                    '已登录，但本机直连出口尚未验证；未导入号池。' if self.direct
                    else '已登录，但绑定 ISP 出口尚未验证；未导入号池。', 503)
            return {'state': 'authenticated', 'cookies': [c for c in cookies if c.get('domain', '').lstrip('.').endswith('doubao.com')],
                    'checks': checks, 'official_user_id': await self.official_user_id()}
        # Trigger only explicit native login/QR controls, never phone-number or
        # verification-code submission and never a fabricated QR payload.
        for label in ('登录', '扫码登录', '二维码登录'):
            candidate = self.page.get_by_text(label, exact=True)
            if await candidate.count() == 1 and await candidate.is_visible():
                try:
                    await candidate.click(timeout=3000)
                except Exception:
                    pass
        dialogs = self.page.locator('[role="dialog"]')
        scopes = [dialogs.nth(i) for i in range(await dialogs.count()) if await dialogs.nth(i).is_visible()]
        for scope in scopes:
            # Only the observed native login dialog can fail the QR job. Chat
            # text or an unrelated modal mentioning this error is not evidence.
            native_login = scope.locator('[data-testid="login_phone_verify_button"], [data-testid="qrcode_image"]')
            if await native_login.count():
                errors = scope.get_by_text('二维码获取失败', exact=True)
                for index in range(await errors.count()):
                    if await errors.nth(index).is_visible():
                        raise ProviderError('qr_unavailable',
                            ('本机直连到豆包账号服务的连接异常，官方二维码获取失败；未自动启用代理，请检查网络后重新开始扫码。'
                             if self.direct else
                             '当前代理到豆包账号服务的连接异常，官方二维码获取失败；未自动切换线路，请检查线路后重新开始扫码。'), 503)
            # The official Doubao/Feishu dialog renders its QR as an SVG inside
            # this named container; the small centre logo is a separate image.
            # Restrict SVG capture to the observed QR container, never arbitrary
            # icons or illustrations in a login dialog.
            qr = scope.locator('[data-testid="qrcode_image"]:has(svg), '
                               'img[alt*="二维码"], img[src*="qrcode"], img[src*="qr_code"], canvas')
            for index in range(await qr.count()):
                target = qr.nth(index)
                if await target.is_visible():
                    size = await target.bounding_box()
                    if size and size['width'] >= 100 and size['height'] >= 100 and .75 < size['width']/size['height'] < 1.3:
                        import base64
                        data = await target.screenshot(type='png')
                        return {'state': 'waiting_scan', 'qr_image': 'data:image/png;base64,'+base64.b64encode(data).decode()}
        # Some official QR modals are not ARIA dialogs. Only explicitly named
        # QR images are accepted globally; arbitrary page canvases are not.
        qr = self.page.locator('img[alt*="二维码"], img[src*="qrcode"], img[src*="qr_code"]')
        for index in range(await qr.count()):
            target = qr.nth(index)
            if await target.is_visible():
                import base64
                return {'state': 'waiting_scan', 'qr_image': 'data:image/png;base64,'+base64.b64encode(await target.screenshot(type='png')).decode()}
        return {'state': 'preparing', 'message': '正在等待豆包官方扫码入口；未找到二维码时不会伪造或提交登录信息。'}

    async def _one(self, selector, *, timeout=NATIVE_READY_TIMEOUT, stage=None, state='visible'):
        locator = self.page.locator(selector)
        started=time.monotonic()
        deadline=started+timeout
        while True:
            count=await locator.count()
            ready=count==1 and await locator.is_visible()
            if ready and state=='editable':ready=await locator.is_editable()
            if ready and state=='enabled':ready=await locator.is_enabled()
            if ready and state=='named':ready=bool((await locator.inner_text()).strip())
            if ready:
                if stage:logging.getLogger(__name__).info('Doubao control ready: %s (%.2fs)',stage,time.monotonic()-started)
                return locator
            if count>1 or time.monotonic()>=deadline:
                reason='定位不唯一' if count>1 else '未就绪或等待超时'
                if stage:logging.getLogger(__name__).warning('Doubao control stopped: %s, count=%d (%.2fs)',stage,count,time.monotonic()-started)
                raise ProviderError('native_state_changed', f'豆包{stage or "原生控件"}{reason}；提示词未发送。')
            await asyncio.sleep(min(.1,max(0,deadline-time.monotonic())))

    async def prepare(self, request, capability, images):
        from .materials import reject_removed_cast
        reject_removed_cast(request)
        entry=capability.get('entry_mode','native_controls')
        if entry not in ('native_controls','ordinary_dialog') or 'skill' in capability:
            raise ProviderError('unsupported_entry', '此历史生成入口已移除，未打开页面控件或重新提交。', 409)
        from .page_guard import clear_download_promotion, install_download_promotion_guard
        await install_download_promotion_guard(self.page)
        await clear_download_promotion(self.page)
        await self.check_page_identity()
        self.request=request
        self.capability = capability
        self.dialog_media_state=None
        self.dialog_prompt_signature=None
        self.dialog_composer=None
        self.dialog_prompt=None
        selectors = capability['selectors']
        if entry=='ordinary_dialog':
            from .capabilities import ORDINARY_DIALOG_MODELS
            from .materials import compose_video_prompt
            label=ORDINARY_DIALOG_MODELS.get(request.get('model'))
            if not label or request.get('model')!=capability.get('id'):
                raise ProviderError('unsupported_reference','普通对话模型尚未验证；提示词未发送。')
            if images:
                kinds={item.get('kind') if isinstance(item,dict) else None for item in images}
                if kinds=={'image'}:
                    from .dialog_images import image_references,prepare_image_references,assert_image_references
                    images=image_references(images,capability)
                    prepare_references,assert_references=prepare_image_references,assert_image_references
                    media_kind='image'
                elif kinds=={'video'}:
                    from .dialog_media import video_references,prepare_video_references,assert_video_references
                    images=video_references(images,capability)
                    prepare_references,assert_references=prepare_video_references,assert_video_references
                    media_kind='video'
                else:
                    raise ProviderError('unsupported_reference','普通对话暂不支持混用图片与视频参考；提示词未发送。')
            from .composer import DialogComposer
            self.dialog_composer=DialogComposer(self.page,selectors['composer'])
            composer=await self.dialog_composer.resolve(stage='对话输入框')
            await self.check_page_identity()
            if (await composer.evaluate('(n)=>n.value??n.innerText')).strip() or await composer.locator('[contenteditable="false"]').count():
                raise ProviderError('native_state_changed','豆包输入框中已有草稿或附件，未覆盖或发送。')
            if selectors.get('upload_ready') and await self.page.locator(selectors['upload_ready']).count():
                raise ProviderError('native_state_changed','豆包编辑区中已有附件，未新建对话或发送。')
            await (await self._one(selectors['new_chat'],stage='新对话入口',state='enabled')).click(timeout=15000)
            await self.page.wait_for_function('''()=>!document.querySelector('[data-testid="receive_message"],[data-message-id]')''',timeout=15000)
            if chat_identity(self.page.url):
                raise ProviderError('native_state_changed','豆包新对话未建立，未发送到已有会话。')
            # The verified entry must remain the ordinary dialog, never a work
            # or Skills selector. The verified page exposes a named mode chip.
            composer=await self.dialog_composer.resolve(stage='新对话输入框')
            if (await composer.evaluate('(n)=>n.value??n.innerText')).strip() or await composer.locator('[contenteditable="false"]').count():
                raise ProviderError('native_state_changed','豆包新对话输入框不为空；提示词未发送。')
            if selectors.get('upload_ready') and await self.page.locator(selectors['upload_ready']).count():
                raise ProviderError('native_state_changed','豆包新对话仍有旧附件；提示词未发送。')
            # Some native home layouts mount their toolbar only on editor
            # focus. Focus the verified empty editor before waiting for it.
            if self.account.get('_interactive'):
                await self.page.bring_to_front()
            await composer.click(timeout=8000)
            mode=await self._one('[data-testid="chat_input_action_mode"]',stage='普通对话模式',state='named')
            if re.sub(r'\s+','',(await mode.inner_text())) not in ('对话','對話'):
                raise ProviderError('native_state_changed','豆包当前不是普通对话模式；提示词未发送。')
            self.payloads.clear()
            self.completion_error=None
            composer=await self.dialog_composer.resolve(stage='新对话输入框')
            await self.check_page_identity()
            if (await composer.evaluate('(n)=>n.value??n.innerText')).strip() or await composer.locator('[contenteditable="false"]').count():
                raise ProviderError('native_state_changed','豆包新对话输入框不为空；提示词未发送。')
            if selectors.get('upload_ready') and await self.page.locator(selectors['upload_ready']).count():
                raise ProviderError('native_state_changed','豆包新对话仍有旧附件；提示词未发送。')
            text=compose_video_prompt(request,label)
            # The existing helper avoids repeating a specification already at
            # the end of the user's body; retain the separate lead in that case.
            if text==request['prompt'] and (request.get('lead_prompt') or '').strip():
                text=request['lead_prompt']+'\n'+text
            self.dialog_prompt=text+'\n'+VIDEO_REQUEST_SUFFIX
            composer=await self.dialog_composer.fill(self.dialog_prompt)
            if images:
                from .dialog_media import composer_signature
                self.dialog_prompt_signature=await composer_signature(composer)
                self.dialog_media_state=await prepare_references(self.page,images,capability)
                self.dialog_media_state['media_kind']=media_kind
                await assert_references(self.page,self.dialog_media_state)
                composer=await self.dialog_composer.resolve(stage='素材上传后输入框')
                if await composer_signature(composer)!=self.dialog_prompt_signature:
                    raise ProviderError('native_state_changed','豆包上传参考素材时改变了原提示词；提示词未发送。')
            await self._one(selectors['send'],stage='发送按钮',state='enabled')
            return
        await (await self._one(selectors['video_entry'])).click()
        for field, value in (('model', request['model']), ('duration', str(request['duration'])), ('ratio', request['ratio'])):
            control = await self._one(selectors[field])
            tag = await control.evaluate('(n)=>n.tagName.toLowerCase()')
            if tag == 'select':
                await control.select_option(str(value))
                actual = await control.input_value()
            else:
                # Non-select custom menus require their observed selected value.
                # Never equate a capability patch or preferred model with selection.
                actual = await control.get_attribute('data-value')
            if str(actual) != str(value):
                raise ProviderError('native_state_changed', '豆包原生模型、时长或比例核对未通过；提示词未发送。')
        if capability.get('native_media_bridge') is True:
            from .native_bridge import prepare_native_media
            from .materials import attach_native,compose_video_prompt
            expected=[{'kind':item['kind'],'name':item['name']} for item in images]
            self.native_state=await prepare_native_media(self.page,resolution=request.get('resolution','auto'),expected=expected)
            if not self.native_state.get('supported'):
                raise ProviderError('native_media_unsupported','豆包原生视频素材管线未识别；提示词未发送。',503)
            if images:await attach_native(self.page,images,selectors,native_state=self.native_state)
            text=compose_video_prompt({**request,'resolution':'auto'},capability['label'])
        elif images:
            upload = self.page.locator(selectors['upload'])
            if await upload.count() != 1:
                raise ProviderError('unsupported_reference', '豆包原生图片上传入口未就绪。')
            await upload.set_input_files([str(p) for p in images])
            await self.page.wait_for_function('(a)=>document.querySelectorAll(a.selector).length===a.count',
                                            arg={'selector': selectors['upload_ready'], 'count': len(images)}, timeout=45000)
        composer = await self._one(selectors['composer'])
        text = text if capability.get('native_media_bridge') is True else request['prompt']
        await composer.fill(text + '\n' + VIDEO_REQUEST_SUFFIX)
        send = await self._one(selectors['send'])
        if not await send.is_enabled():
            raise ProviderError('native_state_changed', '豆包发送按钮不可用；提示词未发送。')

    async def submit(self):
        # Exactly one action. A timeout after clicking is ambiguous, never followed
        # by Enter/button fallback or resubmission on another account.
        try:
            from .page_guard import clear_download_promotion
            await clear_download_promotion(self.page)
            await self.check_page_identity()
            if self.dialog_composer is not None:
                composer=await self.dialog_composer.resolve(stage='发送前输入框')
                # Inline native reference chips have a separate text-only
                # signature; do not compare their labels to the user's prompt.
                if self.dialog_media_state is None:
                    await self.dialog_composer.verify_text(self.dialog_prompt)
            if self.dialog_media_state is not None:
                from .dialog_media import composer_signature
                if self.dialog_media_state.get('media_kind')=='image':
                    from .dialog_images import assert_image_references
                    await assert_image_references(self.page,self.dialog_media_state)
                else:
                    from .dialog_media import assert_video_references
                    await assert_video_references(self.page,self.dialog_media_state)
                composer=await self.dialog_composer.resolve(stage='发送前输入框')
                if await composer_signature(composer)!=self.dialog_prompt_signature:
                    raise ProviderError('native_state_changed','豆包参考素材对应的提示词已改变；提示词未发送。')
            if self.capability.get('native_media_bridge') is True:
                from .native_bridge import assert_native_ready
                await assert_native_ready(self.page)
            send=await self._one(self.capability['selectors']['send'])
            if not await send.is_enabled():
                raise ProviderError('native_state_changed','豆包发送按钮不可用；提示词未发送。')
        except ProviderError as error:
            raise BeforeSendError(error.code,str(error),error.status_code) from None
        except Exception:
            raise BeforeSendError('native_state_changed','豆包提交前检查未完成；提示词未发送。',503) from None
        # Outside the preflight guard: any dispatch/click exception remains
        # ambiguous and must retain the original account's durable send fence.
        await send.click(timeout=15000)

    def _remember_chat(self, snapshot, conversation, url):
        if snapshot.get('conversation_id') and snapshot['conversation_id'] != conversation:
            raise ProviderError('wrong_chat_page', '豆包页面已离开原会话，已停止收片且未重新发送。', 409)
        snapshot.update(conversation_id=conversation, page_url=url)
        if snapshot.get('id'):
            from .accounts import atomic_json
            atomic_json(self.provider._snapshot_path(snapshot['id']), snapshot)

    async def capture_submission_page(self, snapshot):
        """Persist a known native chat before the external send fence."""
        snapshot['submission_page_url'] = self.page.url
        if self.dialog_prompt:
            snapshot['submission_prompt'] = self.dialog_prompt
        conversation = chat_identity(self.page.url)
        if conversation:
            self._remember_chat(snapshot, conversation, self.page.url)

    async def _completed_card_media(self, snapshot, capability, conversation):
        """Open one fresh, message-scoped native video card; never send a chat.

        A cover image or a "ready" sentence is insufficient. Only the native
        player opened from the unique new assistant card supplies media URLs.
        """
        if not conversation or snapshot.get('sent') is not True or chat_identity(self.page.url)!=conversation:
            return []
        blocks=self.page.locator(capability['selectors']['assistant'])
        candidates={}
        for index in range(await blocks.count()):
            block=blocks.nth(index)
            state=await block.evaluate('''el=>({
                id:el.getAttribute('data-message-id')||el.querySelector('[data-message-id]')?.getAttribute('data-message-id'),
                text:el.innerText.slice(0,10000),plays:el.querySelectorAll('[class*="play-icon-wrapper-"]').length})''')
            mid=state.get('id')
            if (not isinstance(mid,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',mid)
                or mid in snapshot.get('baseline_messages',[]) or state.get('plays')!=1
                or not re.search(r'(?:视频|視頻|影片)(?:已)?生成(?:好了|完成|完毕|完畢)',state.get('text',''))):
                continue
            candidates[mid]=block
        if len(candidates)!=1:
            return []
        mid,card=next(iter(candidates.items()))
        cached=[record for record in self.media_info.values()
                if record.get('message_id')==mid and record.get('conversation_id')==conversation
                and record.get('source')=='native_player']
        if cached:
            return cached
        if mid in self.player_attempts:
            return []
        before=await self.page.locator('video,video source').evaluate_all('(nodes)=>nodes.map(n=>n.currentSrc||n.src).filter(Boolean)')
        self.player_attempts.add(mid)
        self._remember_chat(snapshot,conversation,self.page.url)
        # Play clicks are at most once per browser session. Reopening this same
        # card on a later recovery may renew an expired CDN URL, never a task.
        try:
            await card.locator('[class*="play-icon-wrapper-"]').click(force=True,timeout=10000)
            await self.page.wait_for_function('''({mid,before})=>[...document.querySelectorAll('video,video source')].some(n=>{
                const src=n.currentSrc||n.src||'',own=n.closest('[data-message-id]')?.getAttribute('data-message-id');
                return src.startsWith('https://')&&(own===mid||(!own&&!before.includes(src)));})''',
                arg={'mid':mid,'before':before},timeout=7000)
        except Exception:
            return []  # Keep the original task for recovery; never regenerate.
        if chat_identity(self.page.url)!=conversation:
            return []
        videos=await self.page.locator('video,video source').evaluate_all('''nodes=>nodes.map(n=>({
            url:n.currentSrc||n.src||'',message_id:n.closest('[data-message-id]')?.getAttribute('data-message-id')||null}))''')
        scoped=[row['url'] for row in videos if row.get('message_id')==mid and media_url(row.get('url'))]
        detached={row['url'] for row in videos if row.get('message_id') is None
                  and row.get('url') not in before and media_url(row.get('url'))}
        # An unrelated or ambiguous modal player cannot be assigned to a card.
        urls=list(dict.fromkeys(scoped)) or (list(detached) if len(detached)==1 else [])
        from .media_registry import VideoRegistry
        registry=VideoRegistry(conversation,keys=snapshot.get('baseline_media_keys',[]),urls=snapshot.get('baseline_media',[]))
        records=[]
        for value in urls:
            record={'url':value,'conversation_id':conversation,'message_id':mid,'source':'native_player'}
            if registry.is_new(record):
                self.media_info[value]=record
                records.append(record)
        return records

    async def _verification_progress(self, snapshot, phase, reason):
        if snapshot.get('state') == phase:
            return
        snapshot['state'] = phase
        if snapshot.get('id') and self.provider:
            from .accounts import atomic_json
            atomic_json(self.provider._snapshot_path(snapshot['id']), snapshot)
        if self.on_progress:
            await self.on_progress(phase, account=self.account['name'], reason=reason)

    async def _verify_execution_once(self, snapshot, capability):
        """Read fresh native records once per submitted conversation turn."""
        from .verification import challenge_visible
        stage = 'nudge' if snapshot.get('nudge_sent') else 'confirmation' if snapshot.get('confirmation_sent') else 'submission'
        if (not snapshot.get('sent') or not snapshot.get('conversation_id')
                or chat_identity(self.page.url) != snapshot['conversation_id']
                or stage in self.execution_refresh_stages or await challenge_visible(self.page)):
            return None
        from .composer import DialogComposer
        composer = await DialogComposer(self.page, capability['selectors']['composer']).resolve(stage='回执复查输入框')
        if ((await composer.evaluate('(n)=>n.value??n.innerText')).strip()
                or await composer.locator('[contenteditable="false"]').count()):
            return None
        self.execution_refresh_stages.add(stage)
        await self.page.bring_to_front()
        async with self.page.expect_response(lambda response: '/im/chain/single' in urlsplit(response.url).path, timeout=20000):
            await self.page.reload(wait_until='domcontentloaded', timeout=30000)
        await self.page.locator(capability['selectors']['assistant']).first.wait_for(timeout=10000)
        if self.pending:
            await asyncio.wait(list(self.pending), timeout=5)
        # A confirmation sent during this check needs its own reply window and
        # a fresh chain read. The stage fence bounds recursion to three reads.
        return await self._observe(snapshot, capability, timeout=30 if snapshot.get('text_ack_nudge_enabled') else 0)

    async def observe(self, snapshot, capability, timeout):
        from .verification import incomplete
        if self.account.get('_interactive') and not self.receipt_activated:
            await self.page.bring_to_front()
            self.receipt_activated = True
        while True:
            try:
                return await self._observe(snapshot, capability, timeout)
            except Exception:
                if not snapshot.get('verification_pending'):
                    raise
                if self.page.is_closed():
                    return incomplete(snapshot, '验证窗口已关闭，验证未完成；未重新发词，账号登录状态未作失效判定。')
                if time.time() >= snapshot.get('verification_deadline_at', 0):
                    return incomplete(snapshot, '验证期间未能读到原会话回执，验证未完成；未重新发词。')
                # Navigation/iframe teardown can transiently invalidate DOM
                # reads. Continue in this same browser within the saved limit.
                await asyncio.sleep(VERIFICATION_POLL_SECONDS)

    async def _observe(self, snapshot, capability, timeout):
        from .verification import challenge_visible, original_request_visible, incomplete
        self.capability = capability
        deadline = time.monotonic() + timeout
        conversation = snapshot.get('conversation_id')
        grace_deadline = (time.monotonic()+max(0,snapshot.get('verification_deadline_at',0)-time.time())
                          if snapshot.get('state')=='checking_verification' else None)
        verifying = bool(snapshot.get('verification_pending') and snapshot.get('verification_seen'))
        while True:
            if self.page.is_closed() and verifying:
                return incomplete(snapshot, '验证窗口已关闭，验证未完成；未重新发词。')
            challenge = await challenge_visible(self.page)
            if challenge:
                if not verifying:
                    verifying = True
                    snapshot.update(verification_pending=True, verification_seen=True,
                                    verification_deadline_at=time.time()+VERIFICATION_WAIT_SECONDS)
                await self._verification_progress(snapshot, 'waiting_verification',
                    '请在项目电脑当前豆包窗口手动完成验证；保留原账号、线路与会话，不要重复点击生成。')
            elif verifying:
                await self._verification_progress(snapshot, 'verification_continue',
                    '验证弹窗已消失，正在核查原会话回执；尚未确认受理，不会自动补发提示词。')
            url = self.page.url
            current = chat_identity(url)
            if verifying and conversation and current != conversation:
                return incomplete(snapshot, '页面已离开原会话，停止关联结果；未将其他申请计入本任务。')
            original_matches = not verifying or await original_request_visible(self.page, snapshot, capability)
            if verifying and current and not original_matches:
                # DOM hydration can lag a URL change. Wait within the same
                # bounded handoff; never bind an unverified new conversation.
                if time.time() >= snapshot['verification_deadline_at']:
                    return incomplete(snapshot, '未确认原请求回执，页面内容与原申请未完成核对；未关联其他申请。')
                await asyncio.sleep(VERIFICATION_POLL_SECONDS)
                continue
            if (conversation and current != conversation) or (not conversation and self.snapshot):
                return {'phase': 'unknown', 'conversation_id': conversation, 'media': [],
                        'platform_reply': '', 'page_url': snapshot.get('page_url') or HOME,
                        'reason': 'wrong_chat_page' if conversation else 'original_chat_unknown', 'no_charge': False}
            if (not conversation and not self.snapshot and current and snapshot.get('sent') is True
                and (capability.get('entry_mode')=='ordinary_dialog' or capability.get('require_native_execution'))):
                # Only the fresh, already fenced send may bind its new native
                # URL before a recognizable receipt. This is recovery evidence,
                # not acceptance. Recovery itself never guesses a conversation.
                conversation=current
                self._remember_chat(snapshot,conversation,url)
            candidate = conversation or current
            from .media_registry import VideoRegistry
            registry=VideoRegistry(candidate,keys=snapshot.get('baseline_media_keys',[]),urls=snapshot.get('baseline_media',[]))
            texts, media, messages = [], [], {}
            generation_evidence = None
            if capability.get('entry_mode') == 'ordinary_dialog' or capability.get('require_native_execution'):
                from .execution_evidence import native_execution_evidence
                generation_evidence, native_texts = native_execution_evidence(
                    self.payloads, candidate, set(snapshot.get('baseline_messages', [])), snapshot.get('request', {}))
                texts.extend(native_texts)
            for payload in self.payloads:
                evidence = evidence_from_payload(payload, candidate, set(snapshot.get('baseline_messages', [])), set(snapshot.get('baseline_media', [])))
                for message in evidence['text_messages']:
                    messages[message['id']] = message['text']
                for record in evidence['media_records']:
                    if registry.is_new(record):
                        media.append(record['url'])
                        self.media_info[record['url']]=record
            # Only a dedicated assistant-message selector from a verified manifest
            # is eligible. Historical messages are filtered by baseline text hash.
            blocks = self.page.locator(capability['selectors']['assistant'])
            texts.extend(messages.values())
            for index in range(await blocks.count()):
                text = (await blocks.nth(index).inner_text())[:10000]
                if text not in snapshot.get('baseline_texts', []):
                    texts.append(text)
            reply = '\n'.join(dict.fromkeys(texts))[:20000]
            phase, decisive_text = 'unknown', ''
            from .review import latest_review_rejection, review_rejection
            from .confirmation import _ordered_native_messages
            native_order = _ordered_native_messages(self.payloads,candidate,set(snapshot.get('baseline_messages',[])))
            review_failure = latest_review_rejection(self.payloads,candidate,set(snapshot.get('baseline_messages',[])))
            # Raw payload dictionaries and the rendered page can contain old,
            # unfinished or duplicated replies. Only the latest native reply
            # can turn an existing generation into a review failure.
            if native_order == [] and await blocks.count():
                latest_dom = (await blocks.last.inner_text())[:10000]
                if latest_dom not in snapshot.get('baseline_texts',[]) and review_rejection(latest_dom):
                    review_failure = {'text':latest_dom,'conversation_id':candidate,'source':'assistant_dom'}
            # Identical receipts on different messages still have distinct
            # chronological positions; de-duplicate only the display string.
            for text in texts:
                if review_rejection(text):
                    continue  # Applied once below, after chronological scoping.
                state = receipt_state(text, capability)
                if (state=='unknown' and capability.get('entry_mode')=='ordinary_dialog'
                    and re.search(r'Seedance\s*\d+\.\d+',text,re.I)
                    and receipt_state(text,{**capability,'entry_mode':'native_controls'})=='accepted'):
                    # A later explicit acknowledgement of a different model
                    # supersedes an earlier matching one; it cannot keep a
                    # stale verification stamp on the delivered file.
                    phase,decisive_text='unknown',text
                    snapshot['dialog_model_verified']=False
                if state != 'unknown':
                    phase, decisive_text = state, text
                    if state=='accepted' and capability.get('entry_mode')=='ordinary_dialog':
                        from .confirmation import ordinary_dialog_spec_matches
                        if not ordinary_dialog_spec_matches(text,snapshot.get('request',{}),require_all=False):
                            phase='unknown'
                            snapshot['dialog_model_verified']=False
                        else:
                            snapshot['dialog_model_verified']=True
            if review_failure:
                phase,decisive_text='rejected',review_failure['text']
                reply=decisive_text
            if not media and candidate and not challenge:
                records=await self._completed_card_media(snapshot,capability,candidate)
                for record in records:
                    media.append(record['url'])
                    self.media_info[record['url']]=record
            if generation_evidence and generation_evidence['confirmed']:
                checks = [generation_evidence.get(key) for key in ('parameters_match', 'model_matches')
                          if generation_evidence.get(key) is not None]
                if checks:
                    # A matching ability cannot override a contradictory actual
                    # task model, including when a completed player is present.
                    snapshot['dialog_model_verified'] = all(checks)
            if media:
                phase = 'media_ready'
            elif generation_evidence is not None and phase != 'rejected':
                if generation_evidence['confirmed']:
                    phase = ('request_mismatch' if generation_evidence['parameters_match'] is False
                             or generation_evidence.get('model_matches') is False else 'accepted')
                elif generation_evidence['final_text_only']:
                    phase = 'text_only_ack'
                    snapshot['dialog_model_verified'] = False
                elif phase == 'accepted':
                    # A plausible sentence does not prove a video invocation.
                    phase = 'unknown'
                    snapshot['dialog_model_verified'] = False
            if self.completion_error and phase in ('unknown', 'text_only_ack'):
                failure=self.completion_error
                if str(failure['code'])=='710022004' and not verifying:
                    if grace_deadline is None:
                        grace_deadline = time.monotonic()+VERIFICATION_GRACE_SECONDS
                        snapshot.update(verification_pending=True,
                                        verification_deadline_at=time.time()+VERIFICATION_GRACE_SECONDS)
                        await self._verification_progress(snapshot, 'checking_verification',
                            '平台返回验证相关错误，等待官方验证入口加载；未重新提交。')
                    if time.monotonic() < grace_deadline:
                        await asyncio.sleep(VERIFICATION_POLL_SECONDS)
                        continue
                if not verifying:
                    snapshot['verification_pending'] = False
                    return {'phase':'rejected','conversation_id':conversation,'platform_reply':failure['message'],
                        'media':[],'page_url':url,'no_charge':bool(failure.get('no_charge')),
                        'error_code':'completion_'+failure['code']}
            if not candidate:
                phase, media = 'unknown', []
            elif not conversation and phase in ('accepted', 'media_ready'):
                conversation = candidate
                self._remember_chat(snapshot, conversation, url)
            if phase in ('unknown','text_only_ack') and not verifying and not challenge and candidate and (not conversation or candidate==conversation):
                from .confirmation import (confirmation_due, native_confirmation_reply, CONFIRM_TEXT,
                                           native_nudge_reply, nudge_due, NUDGE_TEXT)
                native_reply=native_confirmation_reply(self.payloads,candidate,set(snapshot.get('baseline_messages',[])))
                if native_reply and native_reply.get('answered'):
                    snapshot['confirmation_answered']=True
                waiting_confirmation = (native_reply is not None and (native_reply.get('waiting_reply')
                    or (snapshot.get('confirmation_sent') and native_reply.get('message_id')==snapshot.get('confirmation_message_id'))))
                if waiting_confirmation:
                    phase='unknown'
                confirm_text=native_reply.get('text','') if native_reply is not None else reply
                eligible = native_reply is None or native_reply.get('eligible')
                nudge_reply=native_nudge_reply(self.payloads,candidate,set(snapshot.get('baseline_messages',[])),snapshot)
                should_nudge=nudge_due(snapshot,nudge_reply,generation_evidence,now=time.time())
                if snapshot.get('nudge_question') or (snapshot.get('nudge_sent') and nudge_reply
                        and nudge_reply['message_id']==snapshot.get('nudge_message_id')):
                    phase='unknown'
                followup=NUDGE_TEXT if should_nudge else CONFIRM_TEXT
                if (eligible and not (generation_evidence and generation_evidence.get('confirmed'))
                        and confirmation_due(snapshot,confirm_text,now=time.time(),native_unstarted=bool(native_reply and native_reply.get('eligible')))) or should_nudge:
                    from .composer import DialogComposer
                    dialog=DialogComposer(self.page,capability['selectors']['composer'])
                    composer=await dialog.resolve(stage='确认输入框')
                    content=await composer.evaluate('(n)=>n.value??n.innerText')
                    if not content.strip() and not await composer.locator('[contenteditable="false"]').count():
                        if not conversation:
                            conversation=candidate
                            self._remember_chat(snapshot,conversation,url)
                        # A durable fence permits only this one confirmation, even
                        # if closing the browser interrupts the native click.
                        if should_nudge:
                            snapshot.update(nudge_sent=True,nudge_sent_at=time.time(),nudge_message_id=nudge_reply['message_id'])
                        else:
                            snapshot.update(confirmation_sent=True,confirmation_sent_at=time.time(),
                                confirmation_message_id=(native_reply or {}).get('message_id'))
                        from .accounts import atomic_json
                        atomic_json(self.provider._snapshot_path(snapshot['id']),snapshot)
                        await dialog.fill(followup)
                        if chat_identity(self.page.url)!=conversation or await challenge_visible(self.page):
                            raise ProviderError('native_state_changed','确认前原会话或验证状态已变化，已停止自动确认。',409)
                        send_button=await self._one(capability['selectors']['send'])
                        if should_nudge:
                            latest_proof,_=native_execution_evidence(self.payloads,candidate,
                                set(snapshot.get('baseline_messages',[])),snapshot.get('request',{}))
                            latest=native_nudge_reply(self.payloads,candidate,
                                set(snapshot.get('baseline_messages',[])),snapshot)
                            draft=await composer.evaluate('(n)=>n.value??n.innerText')
                            if (latest_proof['confirmed'] or not (latest_proof['final_text_only']
                                    or (latest or {}).get('confirmed_parameter_context'))
                                    or latest!=nudge_reply or draft!=NUDGE_TEXT):
                                if draft==NUDGE_TEXT:
                                    await composer.fill('',timeout=8000)
                                return await self._observe(snapshot,capability,timeout=0)
                        await send_button.click(timeout=15000)
                        phase='unknown'
                        if timeout>0:deadline=max(deadline,time.monotonic()+30)
                        if self.on_progress:
                            await self.on_progress('awaiting_receipt',account=self.account.get('name'),
                                reason=f'已在原会话补发一次“{followup}”，等待真实生成记录；不会重复补发。')
            result = {'phase': phase, 'conversation_id': conversation, 'platform_reply': reply,
                      'media': list(dict.fromkeys(media)), 'page_url': url,
                      'media_metadata':{value:self.media_info[value] for value in media},
                      'no_charge': phase == 'rejected' and any(v in decisive_text for v in capability.get('no_charge_patterns', []))}
            if phase=='rejected' and review_failure:
                result.update(error_code='platform_review_rejected',review_evidence=review_failure)
            if capability.get('entry_mode')=='ordinary_dialog' or capability.get('require_native_execution'):
                result['dialog_model_verified']=snapshot.get('dialog_model_verified') is True
                result['generation_evidence']=generation_evidence
                if phase=='text_only_ack':
                    result['reason']='豆包只返回了已结束的普通文字消息，未发现视频能力调用、生成任务或成片记录；未确认实际生成，未自动重发。'
                elif phase=='request_mismatch':
                    result['reason']='豆包实际视频调用参数与所选模型、时长或比例不一致；已保留原会话，未再次提交。'
            if verifying:
                if phase in ('accepted', 'media_ready', 'rejected', 'text_only_ack', 'request_mismatch') and not challenge and original_matches:
                    snapshot['verification_pending'] = False
                    result['verification_pending'] = False
                    return result
                if time.time() >= snapshot['verification_deadline_at']:
                    return incomplete(snapshot, '等待人工验证或原会话回执超过 15 分钟，验证未完成；未重新发词，平台扣费尚未确认。')
                await asyncio.sleep(VERIFICATION_POLL_SECONDS)
                continue
            snapshot['verification_pending'] = False
            if phase in ('rejected', 'media_ready', 'request_mismatch') or time.monotonic() >= deadline:
                if (timeout >= 30 and phase in ('unknown', 'text_only_ack') and generation_evidence is not None
                        and not generation_evidence.get('confirmed')
                        and not challenge and not self.completion_error and candidate == conversation):
                    # Each authorized follow-up gets one fresh native-chain
                    # read; a stage can never refresh itself recursively.
                    try:
                        verified = await self._verify_execution_once(snapshot, capability)
                        if verified is not None:
                            return verified
                    except Exception:
                        pass  # Missing native records stay unknown, never accepted.
                return result
            await asyncio.sleep(min(2, max(0, deadline-time.monotonic())))

    async def download(self, url, task_id):
        if not media_url(url):
            raise ProviderError('invalid_media', '成片地址无效，未发起下载。')
        self.provider.downloads.mkdir(parents=True, exist_ok=True)
        path = self.provider.downloads / (task_id + '.mp4')
        partial = path.with_suffix('.mp4.part')
        try:
            from .downloads import resolve_download, read_video_dimensions
            metadata=self.media_info.get(url) or self.snapshot.get('media_metadata',{}).get(url) or {}
            resolved=await resolve_download(self.page,url,node_id=metadata.get('node_id'),message_id=metadata.get('message_id'),
                require_no_watermark=True)
            from video_metadata import InvalidVideoError
            for candidate in [resolved]+resolved.get('alternatives',[]):
                try:
                    await self.stream_to(candidate['url'],partial,1024*1024*1024,referer=HOME)
                    actual=read_video_duration(partial)
                    dimensions=read_video_dimensions(partial)
                    partial.replace(path)
                    resolved=candidate
                    break
                except (ProviderError,OSError,InvalidVideoError):
                    partial.unlink(missing_ok=True)
            else:
                raise ProviderError('download_failed','当前成片的可用下载地址均未取得完整 MP4，保留原任务等待补收，未重新生成。',503)
        finally:
            partial.unlink(missing_ok=True)
        return {'output_path': str(path), 'actual_duration': actual,'dimensions':dimensions,
                'download_source':resolved['source'],'watermark':resolved['watermark'],
                'source_url':resolved['url']}

    async def stream_to(self,url,path,limit,*,referer=None):
        from media import _validate_url_async
        try:
            if not media_url(url):raise ValueError()
            await _validate_url_async(url)
        except ValueError:
            raise ProviderError('invalid_media','素材地址不是有效的 HTTPS 公网地址。') from None
        proxy = None
        if not self.direct:
            if not self.bridge or not self.bridge.running:
                raise ProviderError('proxy_unavailable','原 ISP 认证通道未运行，已停止下载。',503)
            self.bridge.allowed_hosts.add(urlsplit(url).hostname)
            proxy = self.bridge.browser_proxy['server']
        cookies=await self.context.cookies(url)
        headers={'Cookie':'; '.join(c['name']+'='+c['value'] for c in cookies)} if cookies else {}
        if referer==HOME:headers['Referer']=HOME
        if not self.direct:
            proxy_usage.usage.reserve(self.binding)
        async with aiohttp.ClientSession(trust_env=False,cookie_jar=aiohttp.DummyCookieJar()) as session:
            try:
                async with session.get(url,proxy=proxy,headers=headers,
                                       allow_redirects=False,timeout=aiohttp.ClientTimeout(total=120)) as response:
                    if response.status!=200:
                        raise ProviderError('download_failed','所选线路上的素材下载未返回完整响应。',503)
                    await write_bounded(response,path,limit)
            except ProviderError:
                raise
            except Exception:
                raise ProviderError('download_failed','通过所选线路下载失败，未切换线路，请稍后补收。',503) from None
