import asyncio
from contextlib import asynccontextmanager
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import time
import uuid

from providers.base import ProviderError
from .accounts import AccountStore, atomic_json
from .capabilities import Capabilities
from .runtime import BeforeSendError, BrowserSession


class DoubaoProvider:
    platform = 'doubao'

    def __init__(self, root=Path('data/doubao'), scheduler=None, downloads=Path('downloads'), enabled=None, session_factory=None, login_session_factory=None):
        from shared_scheduler import SharedScheduler
        self.root = Path(root)
        self.scheduler = scheduler or SharedScheduler()
        self.downloads = Path(downloads)
        self.enabled = os.getenv('DOUBAO_ENABLED', '0') == '1' if enabled is None else bool(enabled)
        self.accounts = AccountStore(self.root)
        self.manifest = Capabilities(self.root / 'capabilities.json')
        self.session_factory = session_factory or BrowserSession
        self.login_session_factory = login_session_factory or BrowserSession
        self._tasks = {}
        self._login_jobs = {}
        self._calibration_jobs = {}
        self._invalid_previews = {}

    def capabilities(self):
        manifest = self.manifest.load()
        models = self.manifest.public_models()
        verified = any(model.get('enabled') is True for model in models)
        ready = any(a['enabled'] and a['login_verified'] and a['route_verified'] for a in self.list_accounts())
        available = self.enabled and verified and ready
        reason = '' if available else ('豆包新任务入口未启用。' if not self.enabled else
                 '尚无已验证并启用的豆包生成参数，请先完成真实生成验证。' if not verified else
                 '已验证生成参数；当前没有已启用且登录、线路检查通过的豆包账号。请在账号管理检查或恢复登录。')
        return {'platform': 'doubao', 'enabled': available, 'models': models, 'version': manifest['version'],
                'reason': reason, 'max_inflight': 1, 'verified_at': manifest.get('verified_at')}

    def save_capabilities(self, manifest):
        self.manifest.save(manifest)
        return self.capabilities()

    def validate_request(self, request):
        from .materials import reject_removed_cast
        reject_removed_cast(request)
        request = {key: value for key, value in request.items() if key not in ('cast', 'cast_ids')}
        if request.get('duration_profile'):
            raise ProviderError('unsupported_duration', '豆包不支持 Dola 专属时长档位。')
        caps = self.capabilities()
        if not caps['enabled']:
            raise ProviderError('platform_disabled', caps['reason'], 503)
        model = next((m for m in self.manifest.load()['models'] if m['id'] == request.get('model') and m.get('enabled')), None)
        if model is None:
            raise ProviderError('unsupported_model', '请从豆包已验证能力中明确选择模型。')
        category = request.get('account_category')
        if category not in (None, '普通账号', '标准账号'):
            raise ProviderError('invalid_account_category', '请选择普通账号或标准账号。', 422)
        if category and model.get('account_category') not in (None, category):
            raise ProviderError('invalid_account_category', '所选账号类别不支持这个模型。', 422)
        if type(request.get('duration')) is not int or request['duration'] not in model['durations']:
            raise ProviderError('unsupported_duration', '该豆包模型尚未验证此时长。')
        ratio = request.get('ratio') or request.get('size') or model['ratios'][0]
        if ratio not in model['ratios']:
            raise ProviderError('unsupported_ratio', '该豆包模型尚未验证此比例。')
        mode = model.get('entry_mode', 'native_controls')
        if request.get('entry_mode', mode) != mode:
            raise ProviderError('unsupported_entry', '请求入口与该豆包已验证模型不一致。')
        if mode == 'ordinary_dialog':
            if type(request.get('auto_confirm', True)) is not bool:
                raise ProviderError('invalid_confirmation', '豆包参数确认选项必须为布尔值。')
            if request.get('resolution', 'auto') not in (None, 'auto'):
                raise ProviderError('unsupported_resolution', '普通对话入口尚未验证指定清晰度。')
            if type(request.get('require_no_watermark', False)) is not bool or request.get('require_no_watermark'):
                raise ProviderError('no_watermark_unavailable', '普通对话入口尚未验证官方无水印下载。')
            if any(key in request for key in ('chat_ability', 'ability_param')):
                raise ProviderError('unsupported_entry', '普通对话入口不接受原生能力参数。')
        if model.get('native_media_bridge') is True:
            from .calibration import verified_request
            return verified_request({**request,'ratio':ratio},model,caps['version'])
        combinations=model.get('combinations')
        if combinations and not any(item['duration'] == request['duration'] and item['ratio'] == ratio for item in combinations):
            raise ProviderError('unsupported_duration', '该豆包时长和比例组合尚未验证。')
        prompt = request.get('prompt')
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 20000:
            raise ProviderError('invalid_prompt', '提示词不能为空且最多 20000 字符。')
        if mode == 'ordinary_dialog':
            from reference_uploads import IMAGE_ID, MEDIA_ID
            from .materials import normalize_references
            for key in ('reference_audio', 'reference_audios', 'reference_video', 'reference_videos',
                        'reference_media', 'reference_images'):
                if request.get(key) not in (None, '', [], '[]'):
                    raise ProviderError('unsupported_reference', '普通对话参考素材请使用本平台的本地上传标识；不接受公网地址或未验证素材。')
            image_tokens = request.get('reference_uploads') or []
            image_paths = request.get('reference_image_paths') or []
            if not isinstance(image_tokens, list) or not isinstance(image_paths, list):
                raise ProviderError('unsupported_reference', '参考图片上传列表无效。')
            if (any(not isinstance(token, str) or not IMAGE_ID.fullmatch(token) for token in image_tokens)
                    or len(set(image_tokens)) != len(image_tokens)):
                raise ProviderError('unsupported_reference', '参考图片上传标识无效或重复。')
            image_reference = model.get('references', {}).get('images', {})
            image_count = max(len(image_tokens), len(image_paths))
            if image_count and (image_reference.get('enabled') is not True
                    or image_count > image_reference['max_count']):
                raise ProviderError('unsupported_reference', '该豆包模型未开放参考图片上传，或超过数量限制。')
            normalized_images = []
            if image_paths:
                root = request.get('reference_upload_root')
                if not isinstance(root, str) or not root:
                    raise ProviderError('unsupported_reference', '参考图片必须由服务端从当前任务上传目录解析。')
                try:
                    root = Path(root).resolve()
                    for raw in image_paths:
                        if not isinstance(raw, str):
                            raise ValueError()
                        path = Path(raw).resolve()
                        if (not path.is_relative_to(root) or not path.is_file()
                                or not IMAGE_ID.fullmatch(path.name)):
                            raise ValueError()
                        normalized_images.append({'kind': 'image', 'path': path, 'size_bytes': path.stat().st_size})
                    names = [item['path'].name for item in normalized_images]
                    if len(set(names)) != len(names) or (image_tokens and image_tokens != names):
                        raise ValueError()
                except (OSError, ValueError, TypeError):
                    raise ProviderError('unsupported_reference', '参考图片不属于当前任务，格式无效或与上传标识不符。') from None
                normalized_images = normalize_references(normalized_images, model)
            tokens = request.get('reference_media_uploads') or []
            paths = request.get('reference_media_paths') or []
            if not isinstance(tokens, list) or not isinstance(paths, list):
                raise ProviderError('unsupported_reference', '参考视频上传列表无效。')
            if (any(not isinstance(token, str) or not MEDIA_ID.fullmatch(token)
                    or Path(token).suffix not in ('.mp4', '.mov', '.webm') for token in tokens)
                    or len(set(tokens)) != len(tokens)):
                raise ProviderError('unsupported_reference', '参考视频上传标识无效或重复。')
            reference = model.get('references', {}).get('video', {})
            count = max(len(tokens), len(paths))
            if image_count and count:
                raise ProviderError('unsupported_reference', '普通对话目前不支持图片与视频混合参考，请只选择一种素材。')
            if count and (reference.get('enabled') is not True or count > reference['max_count']):
                raise ProviderError('unsupported_reference', '该豆包模型的参考视频尚未验证，或超过已验证数量限制。')
            normalized_paths = []
            if paths:
                root = request.get('reference_upload_root')
                if not isinstance(root, str) or not root:
                    raise ProviderError('unsupported_reference', '参考视频必须由服务端从当前任务上传目录解析。')
                try:
                    root = Path(root).resolve()
                    for item in paths:
                        if (not isinstance(item, dict) or set(item) != {'kind', 'path'}
                                or item.get('kind') != 'video' or not isinstance(item.get('path'), str)):
                            raise ValueError()
                        path = Path(item['path']).resolve()
                        if (not path.is_relative_to(root) or not path.is_file()
                                or not MEDIA_ID.fullmatch(path.name)
                                or path.suffix not in ('.mp4', '.mov', '.webm')):
                            raise ValueError()
                        normalized_paths.append({'kind': 'video', 'path': path, 'size_bytes': path.stat().st_size})
                    names = [item['path'].name for item in normalized_paths]
                    if len(set(names)) != len(names) or (tokens and tokens != names):
                        raise ValueError()
                except (OSError, ValueError, TypeError):
                    raise ProviderError('unsupported_reference', '参考视频不属于当前任务，格式无效或与上传标识不符。') from None
                normalized_paths = normalize_references(normalized_paths, model)
            return {**request, 'platform': 'doubao', 'prompt': prompt.strip(), 'ratio': ratio,
                    'submitted_duration': request['duration'], 'capability_version': caps['version'],
                    'entry_mode': mode, 'auto_confirm': request.get('auto_confirm', True), 'resolution': 'auto',
                    'reference_images': [], 'reference_uploads': image_tokens,
                    'reference_image_paths': [str(item['path']) for item in normalized_images],
                    'reference_media_uploads': tokens,
                    'reference_media_paths': [{'kind': item['kind'], 'path': str(item['path'])} for item in normalized_paths]}
        for key in ('reference_audio', 'reference_audios', 'reference_video', 'reference_videos', 'reference_media', 'reference_media_uploads'):
            if request.get(key) not in (None, '', [], '[]'):
                raise ProviderError('unsupported_reference', '豆包首版暂未验证参考音频或视频。')
        images = request.get('reference_images') or []
        if isinstance(images, str):
            try:
                images = json.loads(images)
            except ValueError:
                raise ProviderError('unsupported_reference', '参考图片列表无效。') from None
        paths = request.get('reference_image_paths') or []
        if not isinstance(images, list) or not isinstance(paths, list):
            raise ProviderError('unsupported_reference', '参考图片列表无效。')
        uploads = request.get('reference_uploads') or []
        if not isinstance(uploads, list):
            raise ProviderError('unsupported_reference', '参考图片上传列表无效。')
        count = len(images) + max(len(paths), len(uploads))
        refs = model.get('references', {}).get('images', {})
        if count and (not refs.get('enabled') or count > refs['max_count']):
            raise ProviderError('unsupported_reference', '参考图片未验证或超出该模型数量限制。')
        normalized = {**request, 'platform': 'doubao', 'prompt': prompt.strip(), 'ratio': ratio,
                'submitted_duration': request['duration'], 'capability_version': caps['version'],
                'reference_images': images, 'reference_image_paths': paths}
        return normalized

    def list_accounts(self):
        return self.accounts.list_accounts()

    def import_ck(self, name, ck, proxy, category='default'):
        return self.accounts.import_ck(name, ck, proxy, category)

    def patch_account(self, name, **changes):
        return self.accounts.patch(name, **changes)

    def delete_account(self, name):
        return self.accounts.delete(name)

    def list_trash(self):
        return self.accounts.list_accounts(trash=True)

    def restore_account(self, token):
        return self.accounts.restore(token)

    def quota_policy(self, limit=...):
        return self.accounts.quota_policy(limit)

    async def verify_account(self, name):
        account = next((a for a in self.list_accounts() if a['name'] == name), None)
        if account is None:
            raise ProviderError('account_not_found', '豆包账号不存在。', 404)
        binding = self.accounts.bound_proxy(name)
        if account['inflight_tasks']:
            raise ProviderError('account_busy','该豆包账号有在途或待核查任务，请先补收。',409)
        async with self._leased(account):
            try:
                async with self.session_factory(self, account) as session:
                    checks = await session.verify()
            except ProviderError:
                raise
            except Exception:
                checks = {'configuration': True, 'proxy_auth': False, 'route_verified': False,
                          'login_verified': False, 'page': False, 'error': '绑定 ISP 的浏览器检查失败，未标记账号失效。'}
        self.accounts.save_diagnostics(name, checks)
        return {'platform': 'doubao', 'account': name,
                'ok': bool(checks.get('route_verified') and checks.get('login_verified')),
                'checks': checks, 'checked_at': time.time()}

    def start_login(self, name, proxy, category='default', mode='qr'):
        from .accounts import valid_name, parse_proxy
        if mode not in ('qr', 'browser'):
            raise ProviderError('invalid_login_mode', '请选择扫码登录或本机浏览器登录。')
        valid_name(name)
        normalized, binding = parse_proxy(proxy)
        if any(a['name'] == name for a in self.list_accounts() + self.list_trash()):
            raise ProviderError('duplicate_account', '该豆包账号名已存在于号池或回收站。', 409)
        return self._new_login_job(name, normalized, binding, category, mode)

    def start_account_login(self, name, mode='browser'):
        if mode != 'browser':
            raise ProviderError('invalid_login_mode', '恢复已有账号请使用原账号浏览器。')
        account = next((item for item in self.list_accounts() if item['name'] == name), None)
        if account is None:
            raise ProviderError('account_not_found', '豆包账号不存在。', 404)
        binding = self.accounts.bound_proxy(name)
        return self._new_login_job(name, None, binding, account['category'], mode, target=account)

    def _new_login_job(self, name, normalized, binding, category, mode, *, target=None):
        active = [j for j in self._login_jobs.values() if j['state'] not in ('completed', 'failed', 'cancelled', 'expired')]
        if len(active) >= 2 or any(j['name'] == name for j in active):
            raise ProviderError('login_busy', '已有登录正在进行，请完成或取消后重试。', 409)
        identifier = uuid.uuid4().hex
        job = {'id': identifier, 'platform': 'doubao', 'name': name, 'state': 'waiting_lane',
               'message': '等待所选线路空闲后打开官方扫码登录。', 'created_at': time.time(),
               'expires_at': time.time()+(1200 if mode == 'browser' else 300), 'mode': mode,
               'route_label': '本机直连' if binding == {'server': 'direct'} else '所选代理线路',
               'qr_image': None, '_binding': binding, '_proxy': normalized,
               '_category': category}
        if target is not None:
            job.update(_target_name=target['name'], _target_id=target['account_id'], restoring=True)
        if mode == 'browser':
            job.update(browser_location='project_host', auto_capture=True, _interactive_lock=asyncio.Lock(),
                       _capture_ready=asyncio.Event())
        self._login_jobs[identifier] = job
        job['_task'] = asyncio.create_task(self._login_worker(job))
        return self.login_status(identifier)

    def login_status(self, identifier):
        job = self._login_jobs.get(identifier)
        if job is None:
            raise ProviderError('login_not_found', '扫码登录任务不存在或服务已重启，请重新开始。', 404)
        return {key: value for key, value in job.items() if not key.startswith('_')}

    async def cancel_login(self, identifier):
        self.login_status(identifier)
        job = self._login_jobs[identifier]
        task = job.get('_task')
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if job['state'] not in ('completed', 'failed', 'expired'):
            job.update(state='cancelled', message='扫码登录已取消。', qr_image=None)
        return self.login_status(identifier)

    async def confirm_login(self, identifier):
        self.login_status(identifier)
        job = self._login_jobs[identifier]
        if job.get('mode') != 'browser':
            raise ProviderError('login_mode_mismatch', '该任务使用自动扫码入口，不能通过浏览器确认保存。', 409)
        if job['state'] == 'completed':
            return self.login_status(identifier)
        if job['state'] in ('cancelled', 'expired', 'failed'):
            raise ProviderError('login_closed', '该登录窗口已关闭或超时，请重新打开。', 409)
        closed = False
        async with job['_interactive_lock']:
            if job['state'] == 'completed':
                return self.login_status(identifier)
            if job['state'] in ('cancelled', 'expired', 'failed') or job['_task'].cancelling():
                raise ProviderError('login_closed', '该登录窗口已关闭或超时，请重新打开。', 409)
            if not job.get('_captured'):
                session = job.get('_session')
                task = job['_task']
                if session is None or task.done() or job['expires_at'] <= time.time():
                    raise ProviderError('login_not_ready', '登录浏览器尚未就绪或已关闭，请稍后再试。', 409)
                try:
                    closed = not await self._capture_interactive_login(job, session)
                except ProviderError as error:
                    job.update(message=str(error), error_code=error.code)
                    raise
        if closed:
            return await self.cancel_login(identifier)
        # A disconnected HTTP client must not cancel browser close/profile save.
        task = job['_task']
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                raise
        return self.login_status(identifier)

    async def _capture_interactive_login(self, job, session, *, native_verified=None):
        # Both the watcher and the optional manual retry hold this job's lock.
        # Never export twice or save a capture that finished after cancellation.
        if job.get('_captured'):
            return True
        step = await session.capture_interactive_login()
        task = job['_task']
        if task.done() or task.cancelling() or job['state'] in ('cancelled', 'expired', 'failed'):
            return False
        if job['expires_at'] <= time.time():
            raise ProviderError('login_not_ready', '登录已超时，请重新打开。', 409)
        if step.get('state') == 'closed':
            return False
        if step.get('state') != 'captured' or not step.get('cookies'):
            raise ProviderError('login_not_ready', '请先在豆包官方页面完成登录。', 409)
        checks = dict(step.get('checks') or {})
        if not checks.get('route_verified'):
            raise ProviderError('proxy_unavailable', '尚未确认所选线路，将保持窗口并自动重试。', 503)
        checks['login_verified'] = (False if native_verified is False else
                                   bool(step.get('login_verified', checks.get('login_verified'))))
        if job.get('_target_name') and (not checks['login_verified'] or not step.get('official_user_id')):
            raise ProviderError('login_not_ready', '正在确认原账号的官方登录身份；窗口保持打开，不会新增账号。', 409)
        job['_captured'] = {**step, 'checks': checks}
        job.update(state='capturing', message='已检测到登录，正在自动提取 CK 并保存账号。')
        job.pop('error_code', None)
        job['_capture_ready'].set()
        return True

    async def _wait_interactive_login(self, job, session):
        job['_session'] = session
        message = ('正在复用原账号浏览器登录；已有登录会自动保存 CK，仅失效时需要扫码。' if job.get('_target_name') else
                   '登录浏览器已打开，请在本机窗口完成官方登录；成功后会自动提取 CK 并加入号池。')
        job.update(state='browser_open', message=message)
        next_capture_at = 0
        try:
            while not job['_capture_ready'].is_set():
                if job['expires_at'] <= time.time():
                    raise TimeoutError()
                async with job['_interactive_lock']:
                    if job['_capture_ready'].is_set():
                        break
                    status = await session.interactive_login_status()
                    if status.get('state') == 'closed':
                        job.update(state='cancelled', message='登录浏览器已关闭，未保存账号。', qr_image=None)
                        return None
                    if status.get('state') in ('authenticated', 'session_ready') and time.monotonic() >= next_capture_at:
                        job.update(state='capturing', message='已检测到登录，正在自动提取 CK 并核对所选线路。')
                        try:
                            captured = await self._capture_interactive_login(job, session,
                                native_verified=False if status['state'] == 'session_ready' else None)
                        except ProviderError as error:
                            if error.code not in ('login_not_ready', 'proxy_unavailable'):
                                raise
                            next_capture_at = time.monotonic() + 5
                            job.update(state='browser_open', message=str(error) + '；窗口保持打开，将自动重试。',
                                       error_code=error.code)
                        else:
                            if not captured:
                                job.update(state='cancelled', message='登录浏览器已关闭，未保存账号。', qr_image=None)
                                return None
                    else:
                        job.update(state='browser_open')
                try:
                    await asyncio.wait_for(job['_capture_ready'].wait(),
                        timeout=2 if self.login_session_factory == BrowserSession else .1)
                except TimeoutError:
                    pass
            return job.get('_captured')
        finally:
            job.pop('_session', None)

    async def _login_worker(self, job):
        if job.get('_target_name'):
            account = next((dict(item) for item in self.list_accounts()
                            if item['account_id'] == job['_target_id'] and item['name'] == job['_target_name']), None)
            if account is None:
                job.update(state='failed', message='原豆包账号已不存在，未创建其他账号。', error_code='account_not_found')
                return
        else:
            account = {'name': 'login:'+job['id'], 'account_id': job['id'], '_login': True, '_binding': job['_binding']}
        if job.get('mode') == 'browser':
            account['_interactive'] = True
        exported = None
        try:
            if account.get('_login'):
                from .environment import create_environment
                environment_path = self.root / 'login' / job['id'] / 'environment.json'
                atomic_json(environment_path, create_environment(job['id'], job['_binding']))
            async with asyncio.timeout(max(1, job['expires_at']-time.time())):
                async with self._leased(account):
                    async with self.login_session_factory(self, account, login=True) as session:
                        job.update(state='preparing', message='正在通过所选线路打开豆包官方登录页。')
                        if job.get('mode') == 'browser':
                            exported = await self._wait_interactive_login(job, session)
                        else:
                            while True:
                                step = await session.login_step()
                                if step['state'] == 'authenticated':
                                    exported = step
                                    break
                                job.update({key: value for key, value in step.items() if key in ('state', 'qr_image', 'message')})
                                await asyncio.sleep(.1 if self.login_session_factory != BrowserSession else 2)
                    if exported:
                        if job['expires_at'] <= time.time():
                            raise TimeoutError()
                        if job['state'] in ('cancelled', 'expired', 'failed') or asyncio.current_task().cancelling():
                            return
                        if job.get('_target_name'):
                            if self.accounts.bound_proxy(job['_target_name']) != job['_binding']:
                                raise ProviderError('proxy_unavailable', '恢复期间账号线路已变化，未替换原账号凭证。', 409)
                            item = self.accounts.refresh_session(job['_target_name'], json.dumps(exported['cookies']),
                                                                 exported.get('official_user_id'))
                        else:
                            official_id = exported.get('official_user_id') if exported.get('checks', {}).get('login_verified') else None
                            if official_id:
                                with self.accounts.connect() as conn:
                                    for saved in conn.execute('SELECT * FROM accounts'):
                                        identity = self.accounts._official_identity(saved)
                                        if identity and identity['user_id'] == official_id:
                                            raise ProviderError('duplicate_account', '该官方账号已经保存，请在原账号行恢复登录，不要重复添加。', 409)
                            environment_path = self.root / 'login' / job['id'] / 'environment.json'
                            from .environment import validate_environment
                            try:
                                environment = json.loads(environment_path.read_text(encoding='utf-8'))
                            except (OSError, ValueError):
                                raise ProviderError('environment_unavailable', '登录窗口环境档案损坏，未导入其他环境的账号。', 503) from None
                            validate_environment(environment, job['id'], job['_binding'])
                            # This trusted, closed browser's UUID and device
                            # environment become the new account together.
                            # Public import_ck never accepts environments.
                            item = self.accounts.import_ck(job['name'], json.dumps(exported['cookies']), job['_proxy'],
                                                           job['_category'], environment=environment)
                            if official_id:
                                item = self.accounts.refresh_session(item['name'], json.dumps(exported['cookies']), official_id)
                            # Move only a new account's closed browser profile.
                            source = self.root / 'login' / job['id'] / 'profile'
                            target = self.root / 'accounts' / item['account_id'] / 'profile'
                            if source.is_dir() and not target.exists():
                                source.replace(target)
                        item = self.accounts.save_diagnostics(item['name'], exported.get('checks', {}))
                        message = ('原账号登录已恢复，CK 已更新，任务和额度记录保持。' if job.get('_target_name') else
                                   '账号已保存，待环境验证。' if job.get('mode') == 'browser' and not item['login_verified'] else
                                   '登录成功，已加入独立豆包号池。')
                        job.update(state='completed', message=message, account=item, qr_image=None)
                        if job.get('_target_name'):
                            job['restored'] = True
                        job.pop('error_code', None)
        except asyncio.CancelledError:
            job.update(state='cancelled', message='扫码登录已取消。', qr_image=None)
            raise
        except TimeoutError:
            job.update(state='expired', message='扫码登录已超时，请重新开始。', qr_image=None)
        except ProviderError as error:
            job.update(state='failed', message=str(error), error_code=error.code, qr_image=None)
        except Exception:
            job.update(state='failed', message='豆包扫码入口或绑定线路未能正常打开，请检查线路后重试。', error_code='login_unavailable', qr_image=None)
        finally:
            job.pop('_captured', None)

    def _snapshot_path(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r'video_[a-f0-9]{32}', task_id):
            raise ProviderError('invalid_task', '任务标识无效。')
        return self.root / 'polling' / (task_id + '.json')

    def _read_snapshot(self, task_id):
        path = self._snapshot_path(task_id)
        if not path.exists():
            return None
        try:
            snapshot = json.loads(path.read_text(encoding='utf-8'))
            if snapshot['platform'] != 'doubao' or snapshot['id'] != task_id:
                raise ValueError()
            return snapshot
        except (ValueError, TypeError, KeyError):
            raise ProviderError('invalid_snapshot', '豆包恢复快照损坏，已停止且不会重新提交。', 409) from None

    @staticmethod
    def _binding_digest(binding):
        return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()

    async def _event(self, callback, phase, **details):
        event = {'platform': 'doubao', 'phase': phase, **details}
        if callback:
            result = callback(event)
            if inspect.isawaitable(result):
                await result

    async def _choose_account(self, category=None):
        while True:
            candidates = [a for a in self.list_accounts() if a['enabled'] and a['route_verified']
                          and a['login_verified'] and a['quota_remaining'] != 0
                          and (category is None or a['category'] == category)]
            if not candidates:
                label = category or '豆包账号'
                raise ProviderError('account_unavailable', f'没有已验证登录、线路且本地次数允许的{label}。', 503)
            floor = min(a['send_count'] for a in candidates)
            cohort = sorted((a for a in candidates if a['send_count'] == floor), key=lambda a: a['last_sent'] or 0)
            for account in cohort:
                binding = self.accounts.bound_proxy(account['name'])
                lane = self.scheduler.lane_id(binding)
                if lane in self.scheduler.unsafe_lanes:
                    continue
                if (not self.scheduler.account_lock('doubao', account['name']).locked()
                        and not self.scheduler.lane_slot(binding).locked()
                        and not self.scheduler.recovery_reserved(binding)
                        and not account['inflight_tasks']):
                    return account
            self.scheduler.available.clear()
            await self.scheduler.available.wait()

    @asynccontextmanager
    async def _leased(self, account, *, browser=True):
        binding = account['_binding'] if account.get('_login') else self.accounts.bound_proxy(account['name'])
        lane = self.scheduler.lane_id(binding)
        if lane in self.scheduler.unsafe_lanes:
            raise ProviderError('proxy_unavailable', '该 ISP 的前一个浏览器未确认关闭，请先检查。', 503)
        if self.scheduler.recovery_reserved(binding, owner=('doubao',account['name'])):
            raise ProviderError('account_busy','该 ISP 正在等待原任务恢复，请先完成补收。',409)
        resources = [self.scheduler.account_lock('doubao', account['name']), self.scheduler.lane_slot(binding), self.scheduler.browser_slots]
        held = []
        try:
            for resource in resources:
                await resource.acquire()
                held.append(resource)
            if lane in self.scheduler.unsafe_lanes:
                raise ProviderError('proxy_unavailable', '该 ISP 浏览器关闭异常，已隔离。', 503)
            yield
        finally:
            if lane not in self.scheduler.unsafe_lanes:
                for resource in reversed(held):
                    resource.release()

    async def run_task(self, row, on_event=None, *, _calibration=None):
        task_id = row['id']
        lock = self._tasks.setdefault(task_id, asyncio.Lock())
        async with lock:
            if self._read_snapshot(task_id):
                return await self._resume(row, on_event)
            request = self.validate_request(row) if _calibration is None else _calibration['request']
            async with self.scheduler.task_slot('doubao'):
                await self._event(on_event, 'waiting_lane', reason='等待豆包账号及绑定 ISP')
                capability = (next(m for m in self.manifest.load()['models'] if m['id'] == request['model'])
                              if _calibration is None else _calibration['capability'])
                account = (await self._choose_account(category=capability.get('account_category') or row.get('account_category'))
                           if _calibration is None else _calibration['account'])
                async with self._leased(account):
                    self.accounts.reserve(task_id, account['name'])
                    snapshot = {'id': task_id, 'platform': 'doubao', 'account': account['name'],
                                'account_id': account['account_id'], 'request': request, 'capability': capability,
                                'binding_digest': self._binding_digest(self.accounts.bound_proxy(account['name'])),
                                'capability_version': request['capability_version'], 'state': 'preparing',
                                'created_at': time.time(), 'submitted_duration': request['duration'], 'sent': False,
                                'text_ack_nudge_enabled': True}
                    atomic_json(self._snapshot_path(task_id), snapshot)
                    try:
                        await self._event(on_event, 'preparing_page', account=account['name'])
                        async with self.session_factory(self, {**account, '_interactive': True}) as session:
                            session.capability = capability
                            session.on_progress = lambda phase, **details: self._event(on_event, phase, **details)
                            baseline = await session.baseline()
                            snapshot.update(baseline_messages=baseline.get('messages', []),
                                            baseline_media=baseline.get('media', []), baseline_texts=baseline.get('texts', []),baseline_media_keys=baseline.get('media_keys',[]))
                            images = await self._images(session, request, capability, task_id)
                            ordinary = capability.get('entry_mode') == 'ordinary_dialog'
                            if capability.get('native_media_bridge') is True or ordinary:
                                from .materials import normalize_references
                                count=len(request.get('reference_image_paths',[]))
                                if ordinary and request.get('reference_uploads') and len(request['reference_uploads'])!=count:
                                    raise ProviderError('unsupported_reference','参考图片未完整暂存到当前任务，未提交提示词。')
                                images=images[count:]+images[:count]
                                references=[{'kind':'image','path':p,'size_bytes':p.stat().st_size} for p in images]
                                root=Path(request.get('reference_upload_root',self.root/'incoming')).resolve()
                                media_paths = request.get('reference_media_paths', [])
                                if ordinary and request.get('reference_media_uploads') and (
                                        len(request['reference_media_uploads']) != len(media_paths)):
                                    raise ProviderError('unsupported_reference', '参考视频未完整暂存到当前任务，未提交提示词。')
                                for item in media_paths:
                                    if not isinstance(item,dict) or item.get('kind') not in ('audio','video'):
                                        raise ProviderError('unsupported_reference','参考音视频列表格式不正确。')
                                    if ordinary and (item['kind'] != 'video' or not isinstance(item.get('path'), str)):
                                        raise ProviderError('unsupported_reference', '普通对话仅接收已确认归属的参考视频。')
                                    path=Path(item['path']).resolve()
                                    if not path.is_relative_to(root) or not path.is_file():
                                        raise ProviderError('unsupported_reference','参考音视频不属于当前任务。')
                                    if ordinary and path.suffix.lower() not in ('.mp4', '.mov', '.webm'):
                                        raise ProviderError('unsupported_reference', '参考视频扩展名与类型不符，未提交提示词。')
                                    references.append({'kind':item['kind'],'path':path,'size_bytes':path.stat().st_size})
                                images=normalize_references(references,capability)
                            await session.prepare(request, capability, images)
                            if capability.get('entry_mode') == 'ordinary_dialog':
                                # Preparing this mode opens a fresh ordinary chat.
                                baseline = await session.baseline()
                                snapshot.update(baseline_messages=baseline.get('messages', []),
                                                baseline_media=baseline.get('media', []),
                                                baseline_texts=baseline.get('texts', []),
                                                baseline_media_keys=baseline.get('media_keys', []))
                            await session.capture_submission_page(snapshot)
                            previous_last_sent = next(a.get('last_sent') for a in self.list_accounts()
                                                      if a['account_id'] == account['account_id'])
                            # Durable fence precedes the first external send. A crash
                            # in the next two lines becomes a verification-only task.
                            snapshot.update(sent=True, state='submission_unknown')
                            atomic_json(self._snapshot_path(task_id), snapshot)
                            self.accounts.mark_sent(task_id)
                            await self._event(on_event, 'submitting', account=account['name'], submitted_duration=request['duration'])
                            try:
                                await session.submit()
                            except BeforeSendError as error:
                                self.accounts.reject_before_send(task_id, previous_last_sent)
                                self.scheduler.release_recovery('doubao', account['name'])
                                result = {'platform': 'doubao', 'account': account['name'],
                                          'status': 'failed', 'phase': 'failed_before_submit',
                                          'error_code': error.code, 'error': str(error),
                                          'submitted_duration': request['duration']}
                                snapshot.update(result, sent=False, state='failed_before_submit')
                                atomic_json(self._snapshot_path(task_id), snapshot)
                                await self._event(on_event, 'failed_before_submit',
                                                  **{k:v for k,v in result.items() if k not in ('phase','platform')})
                                return result
                            except Exception:
                                self.accounts.settle(task_id, 'unknown')
                                self.scheduler.reserve_recovery('doubao', account['name'], self.accounts.bound_proxy(account['name']))
                                return self._unknown(snapshot)
                            outcome = await session.observe(snapshot, capability, timeout=90)
                            return await self._finish(session, snapshot, outcome, on_event)
                    except BaseException:
                        self.accounts.settle(task_id, 'unknown' if snapshot['sent'] else 'rejected')
                        if snapshot['sent']:
                            self.scheduler.reserve_recovery('doubao', account['name'], self.accounts.bound_proxy(account['name']))
                        else:
                            snapshot['state'] = 'failed_before_submit'
                            atomic_json(self._snapshot_path(task_id), snapshot)
                        raise

    def _unknown(self, snapshot):
        return {'status': 'running', 'phase': 'submission_unknown', 'platform': 'doubao',
                'account': snapshot['account'], 'conversation_id': snapshot.get('conversation_id'),
                'platform_reply': snapshot.get('platform_reply', ''),
                'error_code': 'submission_unknown', 'error': '豆包提交结果待核查，保留原账号与线路，不重复发词。'}

    async def _finish(self, session, snapshot, outcome, on_event=None, download=True):
        task_id = snapshot['id']
        phase = outcome.get('phase', 'unknown')
        snapshot.update({key: value for key, value in outcome.items() if value is not None})
        snapshot['state'] = phase
        atomic_json(self._snapshot_path(task_id), snapshot)
        result = {'platform': 'doubao', 'account': snapshot['account'], 'conversation_id': snapshot.get('conversation_id'),
                  'platform_reply': snapshot.get('platform_reply', ''), 'submitted_duration': snapshot['submitted_duration']}
        if snapshot.get('generation_evidence') is not None:
            result['generation_evidence'] = snapshot['generation_evidence']
        if phase in ('text_only_ack', 'request_mismatch'):
            self.accounts.settle(task_id, 'charge_unknown')
            self.scheduler.release_recovery('doubao', snapshot['account'])
            result.update(status='failed', phase=phase, error_code=phase, error=outcome.get('reason') or '未确认实际生成；保留原会话供补查，未自动重发。')
            snapshot.update(result, state=phase)
            atomic_json(self._snapshot_path(task_id), snapshot)
        elif phase == 'verification_incomplete':
            self.accounts.settle(task_id, 'charge_unknown')
            self.scheduler.release_recovery('doubao', snapshot['account'])
            result.update(status='failed', phase=phase, error_code=phase,
                          error=outcome.get('reason') or '验证未完成，未重新提交；平台扣费尚未确认，保留本地预留次数。')
            snapshot.update(result, state=phase, verification_pending=False)
            atomic_json(self._snapshot_path(task_id), snapshot)
        elif phase == 'rejected':
            no_charge = bool(outcome.get('no_charge'))
            self.accounts.settle(task_id, 'rejected' if no_charge else 'charge_unknown')
            self.scheduler.release_recovery('doubao', snapshot['account'])
            result.update(status='failed', phase='platform_rejected_no_charge' if no_charge else 'platform_rejected', error='豆包明确拒绝此次生成，未重新提交。'+('' if no_charge else '扣费尚未确认，保留本地预留次数。'), error_code=outcome.get('error_code') or 'platform_rejected')
            if outcome.get('error_code')=='platform_review_rejected':
                if not no_charge:result['phase']='platform_review_rejected'
                result['error']='豆包内容／版权审核未通过，已停止等待成片，未重新提交。'+('平台明确未扣费，本地预留已释放。' if no_charge else '扣费尚未确认，保留本地预留次数。')
            snapshot.update(result,state='rejected')
            atomic_json(self._snapshot_path(task_id),snapshot)
        elif phase == 'media_ready' and outcome.get('media') and download:
            await self._event(on_event, 'downloading', **result)
            try:
                media = await session.download(outcome['media'][0], task_id)
            except ProviderError as error:
                if error.code != 'no_watermark_unavailable':
                    raise
                # Generation succeeded even though its delivery is disqualified.
                # Preserve charged usage and the original conversation, but do
                # not hold a browser lane or start another generation.
                self.accounts.settle(task_id, 'completed')
                self.scheduler.release_recovery('doubao', snapshot['account'])
                result.update(status='failed', phase=error.code, error_code=error.code, error=str(error))
                snapshot.update(result, state=error.code)
                atomic_json(self._snapshot_path(task_id), snapshot)
                await self._event(on_event, result['phase'],
                    **{k:v for k,v in result.items() if k not in ('phase', 'platform')})
                return result
            self.accounts.settle(task_id, 'completed')
            self.scheduler.release_recovery('doubao', snapshot['account'])
            mismatch = abs(media['actual_duration'] - snapshot['request']['duration']) > 1
            result.update(media, status='failed' if mismatch else 'completed', phase='duration_mismatch' if mismatch else 'completed')
            if mismatch:
                result.update(error='成片实际时长与豆包请求不符，已保留视频，未重新生成。', error_code='duration_mismatch')
            elif (snapshot['capability'].get('native_media_bridge') is True
                  or snapshot['capability'].get('entry_mode') == 'ordinary_dialog'
                  or snapshot['capability'].get('require_native_execution')):
                from .result_validation import validate_delivered
                try:
                    validate_delivered(snapshot['request'],media)
                    if ((snapshot['capability'].get('entry_mode') == 'ordinary_dialog' or snapshot['capability'].get('require_native_execution'))
                            and snapshot.get('dialog_model_verified') is not True):
                        from .capabilities import ORDINARY_DIALOG_MODELS
                        label = ORDINARY_DIALOG_MODELS.get(snapshot['request']['model'], '所选')
                        raise ProviderError('model_unverified', f'视频已保留，但原对话缺少确认 {label} 模型的实际回执，未标记为参数验证成功。')
                except ProviderError as error:
                    result.update(status='failed',phase=error.code,error_code=error.code,error=str(error))
            snapshot.update(result, state=result['phase'])
            atomic_json(self._snapshot_path(task_id), snapshot)
        elif phase in ('accepted', 'media_ready'):
            self.accounts.settle(task_id, 'accepted')
            self.scheduler.reserve_recovery('doubao', snapshot['account'], self.accounts.bound_proxy(snapshot['account']))
            result.update(status='running', phase='accepted' if phase == 'accepted' else 'media_ready')
        else:
            self.accounts.settle(task_id, 'unknown')
            self.scheduler.reserve_recovery('doubao', snapshot['account'], self.accounts.bound_proxy(snapshot['account']))
            result.update(self._unknown(snapshot))
        await self._event(on_event, result['phase'], **{k:v for k,v in result.items() if k not in ('phase', 'platform')})
        return result

    async def resume_task(self, row, on_event=None):
        lock = self._tasks.setdefault(row['id'], asyncio.Lock())
        async with lock:
            return await self._resume(row, on_event)

    async def _resume(self, row, on_event=None, download=True, recheck_verification=False):
        snapshot = self._read_snapshot(row['id'])
        if snapshot is None:
            raise ProviderError('submission_unknown', '豆包任务缺少恢复快照，禁止重新发词。', 409)
        recheck = bool(recheck_verification and snapshot.get('phase') == 'verification_incomplete'
                       and snapshot.get('sent') and snapshot.get('conversation_id'))
        if not recheck and ((snapshot.get('status') in ('completed','failed') and snapshot.get('phase') != 'text_only_ack') or snapshot['state'] in ('completed', 'duration_mismatch', 'rejected')):
            return {key: snapshot[key] for key in ('platform','account','conversation_id','platform_reply','output_path','actual_duration','status','phase','error','error_code','dimensions','download_source','watermark') if key in snapshot}
        capability = snapshot.get('capability') or {}
        if capability.get('entry_mode', 'native_controls') not in ('native_controls', 'ordinary_dialog') or 'skill' in capability:
            raise ProviderError('unsupported_entry', '此历史任务的生成入口已移除，已停止恢复且未重新提交。', 409)
        if not snapshot.get('sent'):
            raise ProviderError('submission_unknown', '任务在发送前停止，请确认后创建新任务，不自动重复提交。', 409)
        if snapshot.get('verification_pending') and not snapshot.get('conversation_id'):
            from .verification import incomplete
            return await self._finish(None, snapshot, incomplete(snapshot,
                '服务中断时尚未取得原会话编号，验证未完成；保留原浏览器资料，未自动重新发送。'), on_event)
        binding = self.accounts.bound_proxy(snapshot['account'])
        if self._binding_digest(binding) != snapshot['binding_digest']:
            raise ProviderError('proxy_unavailable', '恢复任务必须使用原账号的原始 ISP 绑定，已停止。', 503)
        account = next((a for a in self.list_accounts() if a['account_id'] == snapshot['account_id']), None)
        if account is None:
            raise ProviderError('account_not_found', '原豆包账号不存在，未使用其他账号恢复。', 404)
        async with self.scheduler.task_slot('doubao', recovery=True):
            async with self._leased(account):
                if recheck:
                    # Only explicit inspection can reopen a timed-out handoff.
                    # Keep the original chat and billing ledger; this bounded
                    # recheck cannot send either a prompt or a confirmation.
                    history = snapshot.setdefault('verification_rechecks', [])
                    history.append({'at': time.time(), 'previous_reason': snapshot.get('error')})
                    snapshot['verification_rechecks'] = history[-10:]
                    for key in ('status', 'phase', 'error', 'error_code', 'reason'):
                        snapshot.pop(key, None)
                    snapshot['request'] = {**snapshot['request'], 'auto_confirm': False}
                    snapshot.update(state='checking_verification', verification_pending=True,
                                    verification_seen=True, verification_deadline_at=time.time()+30)
                async with self.session_factory(self, {**account, '_interactive': True}, snapshot=snapshot) as session:
                    session.on_progress = lambda phase, **details: self._event(on_event, phase, **details)
                    interrupted_verification = bool(snapshot.get('verification_pending'))
                    outcome = await session.observe(snapshot, snapshot['capability'], timeout=30)
                    if interrupted_verification and outcome.get('phase') == 'unknown':
                        from .verification import incomplete
                        outcome = incomplete(snapshot, '恢复原会话后仍未确认验证或生成回执，验证未完成；未重新发送。')
                    return await self._finish(session, snapshot, outcome, on_event, download)

    async def inspect_task(self, row, download=False):
        lock = self._tasks.setdefault(row['id'], asyncio.Lock())
        async with lock:
            return await self._resume(row, download=download, recheck_verification=True)

    def reserve_recoveries(self, rows):
        for row in rows:
            try:
                snapshot=self._read_snapshot(row['id'])
                if not snapshot or not snapshot.get('sent') or snapshot.get('status') in ('completed','failed') or snapshot.get('state') in ('completed','duration_mismatch','rejected'):
                    continue
                capability = snapshot.get('capability') or {}
                if capability.get('entry_mode', 'native_controls') not in ('native_controls', 'ordinary_dialog') or 'skill' in capability:
                    continue
                binding=self.accounts.bound_proxy(snapshot['account'])
                if self._binding_digest(binding)!=snapshot['binding_digest']:
                    self.scheduler.unsafe_lanes.add(self.scheduler.lane_id(binding))
                    continue
                self.scheduler.reserve_recovery('doubao',snapshot['account'],binding)
            except (ProviderError, ValueError, KeyError):
                # A damaged platform snapshot must not prevent the Dola service
                # starting. Isolate the known original lane for manual recovery.
                try:
                    binding=self.accounts.bound_proxy(row.get('account'))
                    self.scheduler.unsafe_lanes.add(self.scheduler.lane_id(binding))
                except (ProviderError, ValueError, KeyError, TypeError):
                    pass

    def start_calibration(self, name, request, *, upload_owner=None, upload_root=None):
        from .materials import reject_removed_cast
        reject_removed_cast(request)
        request = {key: value for key, value in request.items() if key not in ('cast', 'cast_ids')}
        account=next((a for a in self.list_accounts() if a['name']==name),None)
        if not account or not account['enabled'] or not account['route_verified'] or not account['login_verified']:
            raise ProviderError('account_unavailable','请先扫码登录并通过豆包账号环境检查。',503)
        if account['inflight_tasks']:
            raise ProviderError('account_busy','此账号仍有待补收任务，请先处理。',409)
        if any(j['state'] in ('queued','running','submission_unknown') for j in self._calibration_jobs.values()):
            raise ProviderError('calibration_busy','已有豆包验证任务，请先完成或核查。',409)
        checks=account['diagnostics']
        if not account['last_checked_at'] or time.time()-account['last_checked_at']>3600:
            raise ProviderError('native_state_changed','环境检查已过期，请重新读取原生选项。')
        if checks.get('entry_mode', 'native_controls') != 'native_controls':
            raise ProviderError('unsupported_entry', '环境记录来自已移除的生成入口，请重新检查普通视频页面。', 409)
        controls=checks.get('observed_native_options',[])
        selectors=dict(checks.get('observed_selectors',{}))
        values={}
        label=''
        for kind in ('model','duration','ratio'):
            candidates=[c for c in controls if c.get('kind')==kind and c.get('selector')]
            wanted=str(request.get(kind,''))
            matched=[(c,o) for c in candidates for o in c.get('options',[]) if str(o.get('value'))==wanted]
            if len(matched)!=1:
                raise ProviderError('native_state_changed','选择的豆包模型、时长或比例不是本次环境检查捕获的唯一原生选项。')
            control,option=matched[0]
            selectors[kind]=control['selector']
            values[kind]=wanted
            if kind=='model':label=option.get('label') or wanted
        if any(not selectors.get(k) for k in ('video_entry','composer','send','assistant')):
            raise ProviderError('native_state_changed','未完整识别豆包视频入口、发送框和独立助手消息，需要进一步检查原生页面，未提交。')
        duration=request.get('duration')
        prompt=request.get('prompt')
        if type(duration) is not int or not 1<=duration<=300 or not isinstance(prompt,str) or not prompt.strip() or len(prompt)>20000:
            raise ProviderError('invalid_request','请明确填写有效提示词及原生时长。')
        if checks.get('native_media_bridge_available') is True:
            from .calibration import native_plan
            plan,previous=native_plan(self,account,request,{**checks,'observed_selectors':selectors},
                upload_owner=upload_owner,upload_root=upload_root,
                native_model={'id':values['model'],'label':label,'max_duration':duration})
            identifier=plan['request']['id']
            job={'id':identifier,'state':'queued','message':'等待所选线路，执行一次豆包原生视频管线验证。',
                 'account':name,'created_at':time.time()}
            self._calibration_jobs[identifier]=job
            job['_task']=asyncio.create_task(self._calibration_worker(job,plan,previous))
            return self.calibration_status(identifier)
        if request.get('duration_profile') or any(request.get(k) for k in ('reference_uploads','reference_media','reference_media_uploads','reference_audios','reference_videos')):
            raise ProviderError('unsupported_reference','验证任务当前仅接收文本和公网参考图片；本地素材请通过有归属校验的正式接口。')
        images=request.get('reference_images') or []
        if not isinstance(images,list) or len(images)>1:
            raise ProviderError('unsupported_reference','首轮图片验证一次只允许一张公网参考图。')
        if images and not all(selectors.get(k) for k in ('upload','upload_ready')):
            raise ProviderError('unsupported_reference','尚未观测到豆包图片上传及完成标记，未提交。')
        previous=next((m for m in self.manifest.load()['models'] if m['id']==values['model']),None)
        if images and not previous:
            raise ProviderError('unsupported_reference','先验证此模型的纯文本生成，再验证参考图片。')
        identifier='video_'+uuid.uuid4().hex
        model={'id':values['model'],'label':label,'enabled':True,'durations':[duration],'ratios':[values['ratio']],
               'selectors':selectors,'combinations':[{'duration':duration,'ratio':values['ratio']}],
               'references':{'images':{'enabled':bool(images),'max_count':1,'max_bytes':15*1024*1024}},
               'receipt_patterns':['正在生成视频','视频生成中','开始生成','视频已开始生成'],
               'rejection_patterns':['无法生成','不能生成','生成失败','违反','不支持生成'],
               'evidence':{}}
        normalized={**request,'id':identifier,'platform':'doubao','duration':duration,'ratio':values['ratio'],
                    'model':values['model'],'prompt':prompt.strip(),'submitted_duration':duration,
                    'capability_version':'calibration-'+identifier,'reference_images':images}
        job={'id':identifier,'state':'queued','message':'等待绑定 ISP，准备执行一次真实验证生成。','account':name,'created_at':time.time()}
        self._calibration_jobs[identifier]=job
        job['_task']=asyncio.create_task(self._calibration_worker(job,{'account':account,'request':normalized,'capability':model},previous))
        return self.calibration_status(identifier)

    def calibration_status(self, identifier):
        job=self._calibration_jobs.get(identifier)
        if job is None:
            raise ProviderError('calibration_not_found','验证任务不存在或服务已重启，请通过任务快照核查。',404)
        public={k:v for k,v in job.items() if not k.startswith('_')}
        result=dict(public.get('result') or {})
        path=Path(result['output_path']).resolve() if result.get('output_path') else None
        if path and path.parent==self.downloads.resolve() and path.is_file() and re.fullmatch(r'video_[a-f0-9]{32}\.mp4',path.name):
            result['video_url']='/videos/'+path.name
        if result:
            result.pop('output_path',None);result.pop('local_path',None)
            public['result']=result
        return public

    async def _calibration_worker(self, job, plan, previous):
        from video_metadata import read_video_duration
        try:
            job.update(state='running',message='正在执行一次豆包原生验证生成，请勿重复提交。')
            result=await self.run_task(plan['request'],_calibration=plan)
            deadline=time.time()+1800
            while result.get('status')=='running' and time.time()<deadline:
                job.update(state='submission_unknown' if result.get('phase')=='submission_unknown' else 'running',message='已提交验证任务，等待原会话成片；不会重复发送提示词。',result=result)
                await asyncio.sleep(3)
                result=await self.resume_task(plan['request'])
            if result.get('status')!='completed':
                job.update(state='submission_unknown' if result.get('status')=='running' else 'failed',message=result.get('error') or '验证尚未取得完整成片，未开放能力。',result=result,error_code=result.get('error_code'))
                return
            actual=read_video_duration(result['output_path'])
            if abs(actual-plan['request']['duration'])>1:
                raise ProviderError('duration_mismatch','实际成片时长不符，能力未开放。')
            model=plan['capability']
            image_test=bool(plan['request'].get('reference_images'))
            if model.get('native_media_bridge') is True:
                from .calibration import validate_completed_native
                validate_completed_native(plan,result,job['id'])
                if model.get('native_media_bridge') is True:
                    model['evidence']['native_store_ready_task_id']=job['id']
                image_test=bool(plan['request'].get('reference_images') or plan['request'].get('reference_image_paths'))
            if previous:
                combined={**previous,'selectors':model['selectors']}
                combined['durations']=sorted(set(previous['durations']+model['durations']))
                combined['ratios']=list(dict.fromkeys(previous['ratios']+model['ratios']))
                combined['combinations']=previous.get('combinations',[])+[c for c in model['combinations'] if c not in previous.get('combinations',[])]
                if model.get('native_media_bridge') is True:
                    combined['native_media_bridge']=True
                    combined['references']={**previous.get('references',{}),**{k:v for k,v in model['references'].items() if v['enabled']}}
                    combined['features']={**previous.get('features',{}),**model.get('features',{})}
                    if model.get('features',{}).get('resolution',{}).get('enabled'):
                        combined['features']['resolution']={**model['features']['resolution'],'values':list(dict.fromkeys(
                            previous.get('features',{}).get('resolution',{}).get('values',[])+model['features']['resolution']['values']))}
                    combined['evidence']={**previous.get('evidence',{}),**model.get('evidence',{})}
                elif image_test:combined['references']=model['references']
                model=combined
            if model.get('native_media_bridge') is True:
                if not model.get('evidence',{}).get('text_task_id'):
                    model['evidence']={**model.get('evidence',{}),'text_task_id':job['id']}
            else:
                model['evidence']={**model.get('evidence',{}),('image_task_id' if image_test else 'text_task_id'):job['id']}
            model['verified_at']=time.time()
            manifest=self.manifest.load()
            models=[m for m in manifest['models'] if m['id']!=model['id']]+[model]
            self.manifest.save({'version':uuid.uuid4().hex,'verified_at':time.time(),'expires_at':time.time()+86400,'models':models})
            job.update(state='completed',message='实际成片和时长已核对，该组合验证记录已保存。',result=result)
        except ProviderError as error:
            job.update(state='failed',message=str(error),error_code=error.code)
        except asyncio.CancelledError:
            job.update(state='submission_unknown',message='服务停止，验证任务保留恢复快照，未重新提交。')
            raise
        except Exception:
            job.update(state='failed',message='豆包验证未完成；请检查线路和原生页面，未开放能力。',error_code='calibration_failed')

    async def shutdown(self):
        tasks=[j['_task'] for j in list(self._login_jobs.values())+list(self._calibration_jobs.values()) if j.get('_task') and not j['_task'].done()]
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        for job in self._login_jobs.values():
            if job['state'] not in ('completed', 'failed', 'expired', 'cancelled'):
                job.update(state='cancelled', message='登录已取消。', qr_image=None)

    async def _images(self, session, request, capability, task_id):
        from PIL import Image
        from .runtime import media_url
        refs = capability.get('references', {}).get('images', {})
        paths = []
        for raw in request.get('reference_image_paths', []):
            path = Path(raw).resolve()
            root = Path(request.get('reference_upload_root', self.root / 'incoming')).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ProviderError('unsupported_reference', '豆包本地素材不属于当前任务。')
            paths.append(path)
        for index, url in enumerate(request.get('reference_images', [])):
            if not media_url(url):
                raise ProviderError('unsupported_reference', '豆包参考图必须使用有效 HTTPS 公网地址。')
            folder = self.root / 'polling' / (task_id + '-images')
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f'{index}.image'
            await session.stream_to(url,path,refs['max_bytes'])
            paths.append(path)
        for path in paths:
            try:
                if path.stat().st_size > refs['max_bytes']:
                    raise ValueError()
                with Image.open(path) as image:
                    if image.format not in ('PNG', 'JPEG', 'WEBP') or image.width * image.height > 50_000_000:
                        raise ValueError()
                    image.verify()
            except (OSError, ValueError, KeyError):
                raise ProviderError('unsupported_reference', '豆包参考图无效、格式不受支持或超出大小限制。') from None
        return paths
