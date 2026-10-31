"""Platform boundaries around the legacy Dola service; never fall back between pools."""
import asyncio
import hashlib
import json
from pathlib import Path
import time
import uuid

from fastapi import HTTPException
from providers.base import ProviderError
from reference_uploads import api_owner, attach_images, cleanup_task_images
from task_results import complete_task, fail_task
from store import TaskQuotaExceeded, PendingTaskLimitExceeded
from video_evidence import safe_text
from starlette.responses import JSONResponse
from urllib.parse import parse_qs


def expire_unknown_platform_tasks(ns, *, now=None):
    rows = ns['store'].expire_unknown_platform_tasks(time.time() if now is None else now)
    provider = ns.get('doubao_provider')
    if provider is not None:
        for row in rows:
            if row.get('account'):
                provider.scheduler.release_recovery('doubao', row['account'])
    return rows


def provider_for(ns, platform):
    if platform != 'doubao':
        raise ProviderError('unsupported_platform', '不支持的平台', 422)
    return ns['doubao_provider']


def upload_root(config, platform):
    if platform not in ('dola', 'doubao'):
        raise HTTPException(422, '不支持的平台')
    root = Path(config.REFERENCE_UPLOAD_DIR)
    return root if platform == 'dola' else root / 'doubao'


def request_values(req):
    values = req.model_dump() if hasattr(req, 'model_dump') else vars(req).copy()
    # A Dola schema default must never silently select a Doubao model.
    if hasattr(req, 'model_fields_set') and 'model' not in req.model_fields_set:
        values['model'] = None
    return values


def generation_options(values):
    """Keep only Doubao preferences; empty defaults preserve legacy identities."""
    if values.get('cast') or values.get('cast_ids'):
        raise ProviderError('unsupported_cast', '角色素材库已移除，请使用独立参考素材上传。', 422)
    defaults = {'resolution': 'auto', 'require_no_watermark': False, 'auto_confirm': False, 'lead_prompt': '', 'account_category': None}
    return {key: values[key] for key, default in defaults.items()
            if key in values and values[key] is not None and (values[key] != default or key=='auto_confirm')}


def task_generation_options(row, *, recovery=False):
    try:
        options = json.loads(row.get('generation_options') or '{}')
        if not isinstance(options, dict):
            raise ValueError()
    except (ValueError, TypeError):
        raise ProviderError('invalid_generation_options', '豆包任务参数损坏，未重新提交。') from None
    if recovery:
        # Previously submitted tasks are observed from their trusted snapshot;
        # retiring role bindings must not block collecting their existing video.
        options = {key: value for key, value in options.items() if key not in ('cast', 'cast_ids')}
    return generation_options(options)


async def create_platform_task(ns, req, client, admin_identity=None):
    if admin_identity is None and req.platform not in client.get('allowed_platforms', ['dola']):
        raise ProviderError('platform_not_allowed', '当前密钥未获准使用豆包', 403)
    values = request_values(req)
    generation_options(values)
    identity_scope = ('admin:' + ns['admin_owner'](admin_identity) if admin_identity is not None
                      else 'api:' + str(client.get('api_key_hash')))
    identity = ns['_submission_identity'](req, identity_scope)
    if identity:
        replay = ns['_find_submission'](identity)
        if replay:
            return replay
    provider = provider_for(ns, req.platform)
    values['ratio'] = ns.get('_resolve_ratio', lambda size, ratio: ratio)(req.size, req.ratio)
    normalized = provider.validate_request(values)
    duration = normalized.get('duration', req.duration)
    if admin_identity is None and duration not in client.get('allowed_durations', []):
        raise ProviderError('unsupported_duration', '当前密钥不允许此视频时长', 422)
    if not provider.list_accounts():
        raise ProviderError('no_account', '豆包号池没有账号，请先扫码登录添加', 503)
    root = upload_root(ns['config'], req.platform)
    task_id = 'video_' + uuid.uuid4().hex
    owner = ns['admin_owner'](admin_identity) if admin_identity is not None else api_owner(client)
    if (req.reference_uploads or req.reference_media_uploads) and admin_identity is None and owner is None:
        raise ProviderError('unsupported_reference', '上传参考素材需要有效的API密钥', 422)
    refs = await ns['validate_reference_urls'](req.reference_images)
    options = generation_options({**values, **normalized})
    try:
        refs += await asyncio.to_thread(attach_images, req.reference_uploads, root, owner, task_id,
            media_ids=req.reference_media_uploads,
            media_max_count=getattr(ns['config'], 'REFERENCE_MEDIA_MAX_COUNT', 10))
        existing = ns['store'].create(task_id, normalized['model'], req.prompt,
            normalized.get('ratio') or req.ratio or 'default', duration, platform=req.platform,
            submitted_duration=duration, capability_version=normalized.get('capability_version'),
            reference_images=json.dumps(refs, ensure_ascii=False),
            api_key_hash=client.get('api_key_hash'), api_key_name=client.get('api_key_name'),
            daily_limit=client.get('daily_limit', 0), concurrency_limit=client.get('concurrency_limit', 0),
            max_pending=ns['config'].MAX_PENDING_TASKS, **identity,
            **({'generation_options': json.dumps(options, ensure_ascii=False, sort_keys=True)} if options else {}))
        if existing:
            cleanup_task_images(root, task_id)
            return ns['TaskResponse'](**existing, replayed=True)
    except (TaskQuotaExceeded, PendingTaskLimitExceeded) as error:
        cleanup_task_images(root, task_id)
        raise HTTPException(429, str(error)) from None
    except ValueError as error:
        cleanup_task_images(root, task_id)
        raise HTTPException(409 if identity else 422, str(error)) from None
    except Exception:
        cleanup_task_images(root, task_id)
        raise
    asyncio.create_task(run_platform_task(ns, ns['store'].get(task_id), client))
    return ns['TaskResponse'](**ns['store'].get(task_id))


def persist_event(ns, task_id, event):
    if not isinstance(event, dict):
        return
    combined = {**(event.get('details') or {}), **event}
    fields = {key: combined[key] for key in ('account', 'conversation_id', 'deadline_at', 'submitted_duration')
              if combined.get(key) is not None}
    phase = {'preparing_page':'preparing', 'submitting':'sending', 'unknown':'submission_unknown'
             }.get(event.get('phase'), event.get('phase', 'polling'))
    if phase in ('sending', 'submitted', 'awaiting_receipt', 'accepted', 'polling', 'downloading', 'media_ready', 'submission_unknown',
                 'checking_verification', 'waiting_verification', 'verification_continue'):
        fields['status'] = 'processing'
    if fields:
        ns['store'].update(task_id, **fields)
    ns['store'].record_evidence(task_id, phase, event.get('details') or event)
    ns['store'].update(task_id, last_poll_at=time.time())


def persist_result(ns, row, result):
    store = ns['store']
    path = result.get('local_path') or result.get('output_path')
    if path:
        if row.get('platform') == 'doubao':
            store.record_evidence(row['id'], 'download_metadata', {
                'dimensions': result.get('dimensions'),
                'download_source': result.get('download_source'),
                'watermark': result.get('watermark')})
            if result.get('status') == 'failed':
                from video_delivery import qualified_delivery
                if not qualified_delivery(result):
                    complete_task(store, row['id'], {**result, 'local_path': path}, ns['config'].PUBLIC_BASE)
                    return
                from video_metadata import read_video_duration
                try:
                    actual = read_video_duration(path)
                except (OSError, ValueError):
                    actual = None
                code = result.get('error_code') or result.get('failure_code') or 'output_validation_failed'
                phase = result.get('phase') or code
                if phase == 'completed':
                    phase = code
                reason = safe_text(result.get('error') or result.get('message') or '成片未通过规格验证，文件已保留。')
                fields = {key: result[key] for key in ('account', 'conversation_id', 'platform_reply')
                          if result.get(key) is not None}
                store.update(row['id'], **fields, status='failed', phase=phase, error=reason,
                    failure_code=code, actual_duration=actual, last_poll_at=time.time(), finished_at=time.time(),
                    video_url=f"{ns['config'].PUBLIC_BASE.rstrip('/')}/videos/{Path(path).name}")
                store.record_evidence(row['id'], phase, {'reason': reason, 'actual_duration': actual})
                return
        complete_task(store, row['id'], {**result, 'local_path': path}, ns['config'].PUBLIC_BASE)
    else:
        fields = {key: result[key] for key in ('account', 'conversation_id', 'platform_reply', 'actual_duration')
                  if result.get(key) is not None}
        if isinstance(result.get('generation_evidence'), dict):
            try:
                audit = json.loads(row.get('request_evidence') or '{}')
            except (ValueError, TypeError):
                audit = {}
            if not isinstance(audit, dict):
                audit = {}
            audit['generation_evidence'] = result['generation_evidence']
            fields['request_evidence'] = json.dumps(audit, ensure_ascii=False)
        phase = {'unknown':'submission_unknown'}.get(
            result.get('phase'), result.get('phase') or 'submission_unknown')
        failed = result.get('status') == 'failed'
        store.update(row['id'], **fields, status='failed' if failed else 'processing',
                     phase=phase, error=safe_text(result.get('error')) or None,
                     failure_code=result.get('error_code'), last_poll_at=time.time(),
                     finished_at=time.time() if failed else None)
        store.record_evidence(row['id'], phase, {'reply': result.get('platform_reply'),
                              'reason': result.get('error') or result.get('message') or phase})


async def run_platform_task(ns, row, client, resume=False):
    acquired = False
    try:
        provider = provider_for(ns, row.get('platform'))
        await ns['key_limiter'].acquire(client.get('api_key_hash'), client.get('concurrency_limit', 0))
        acquired = True
        # A queued watchdog failure cannot be revived when a lease finally becomes free.
        current = ns['store'].get(row['id'])
        if not current or current['status'] not in ('queued', 'processing'):
            return
        folder = (upload_root(ns['config'], row['platform']) / 'tasks' / row['id']).resolve()
        refs, paths, media_paths = [], [], []
        from reference_uploads import FILE_ID, IMAGE_ID
        for ref in json.loads(current.get('reference_images') or '[]'):
            if ref.startswith('upload:'):
                prefix = 'upload:' + row['id'] + '/'
                identifier = ref[len(prefix):] if ref.startswith(prefix) else ''
                if not FILE_ID.fullmatch(identifier):
                    raise ValueError('参考素材不属于当前任务')
                path = (folder / identifier).resolve()
                if path.parent != folder or not path.is_file():
                    raise ValueError('任务参考素材不可用')
                if IMAGE_ID.fullmatch(identifier):
                    paths.append(str(path))
                else:
                    media_paths.append({'path': str(path),
                        'kind': 'video' if path.suffix.lower() in ('.mp4', '.mov', '.webm') else 'audio'})
            else:
                refs.append(ref)
        row = {**current, **task_generation_options(current, recovery=resume), 'reference_upload_root': str(folder),
               'reference_image_paths': paths, 'reference_media_paths': media_paths,
               'reference_images': json.dumps(refs)}
        ns['store'].update(row['id'], started_at=row.get('started_at') or time.time())
        callback = lambda event: persist_event(ns, row['id'], event)
        method = provider.resume_task if resume else provider.run_task
        result = await method(row, on_event=callback)
        persist_result(ns, row, result)
        # The provider's bounded observations release browser resources between
        # passes, but its recovery reservation keeps the physical ISP occupied.
        deadline = row.get('deadline_at') or time.time() + getattr(ns['config'], 'DOUBAO_VIDEO_TIMEOUT', 7200)
        ns['store'].update(row['id'], deadline_at=deadline)
        while (result.get('status') == 'running' and result.get('phase') in ('accepted', 'media_ready')
               and time.time() < deadline):
            await asyncio.sleep(min(15, max(.1, deadline-time.time())))
            result = await provider.resume_task(row, on_event=callback)
            persist_result(ns, row, result)
        if result.get('status') == 'running' and time.time() >= deadline:
            ns['store'].update(row['id'], status='failed', phase='query_timeout', failure_code='query_timeout',
                error='豆包原会话等待期限已到，保留补收入口，未重新提交', finished_at=time.time())
    except asyncio.CancelledError:
        raise
    except Exception as error:
        current = ns['store'].get(row['id']) or row
        if current.get('phase') in ('sending', 'submitted', 'awaiting_receipt', 'submission_unknown'):
            ns['store'].update(row['id'], status='processing', phase='submission_unknown',
                               error='提交结果待核查，未重复发送', failure_code='submission_unknown')
        else:
            fail_task(ns['store'], row['id'], error, getattr(error, 'code', None))
    finally:
        if acquired:
            await ns['key_limiter'].release(client.get('api_key_hash'))


async def inspect_platform_task(ns, row, download=False):
    provider = provider_for(ns, row.get('platform'))
    lock = ns['_result_checks'].setdefault(row['id'], asyncio.Lock())
    if lock.locked() or (row['status'] in ('queued', 'processing') and row.get('phase') not in ('submission_unknown', 'media_ready')):
        raise HTTPException(409, '原任务仍在运行，请等待当前查询完成')
    async with lock:
        result = await provider.inspect_task({**row, **task_generation_options(row, recovery=True), 'reference_upload_root':
            str(upload_root(ns['config'], row['platform']))}, download=download)
        persist_result(ns, row, result)
        updated = ns['store'].get(row['id'])
        response = {'task': updated, 'reply': result.get('platform_reply'),
                    'state': result.get('phase'), 'conversation_id': result.get('conversation_id')}
    # Manual recovery can discover acceptance after the initial worker exited
    # with submission_unknown. Rejoin the original polling loop after releasing
    # the query lock; never submit again or start a second watcher for this task.
    if result.get('status')=='running' and result.get('phase')=='accepted' and ns.get('_resume_task'):
        watchers=ns.setdefault('_platform_recovery_watchers',{})
        prior=watchers.get(row['id'])
        if prior is None or prior.done():
            job=asyncio.create_task(ns['_resume_task'](updated))
            watchers[row['id']]=job
            def finished(done):
                if watchers.get(row['id']) is done:watchers.pop(row['id'],None)
                if not done.cancelled():done.exception()
            job.add_done_callback(finished)
    return response


def dola_capabilities():
    from video_capabilities import PLUGIN_EXPERT30_MODELS, PLUGIN_EXPERT30_RATIOS, PLUGIN_EXPERT_DURATIONS
    labels = {'seedance-2.5': 'Seedance 2.5', 'seedance-2.0': 'Seedance 2.0 Fast',
              'seedance-2.0-std': 'Seedance 2.0', 'seedance-2.0-mini': 'Seedance 2.0 Mini'}
    return {'platform': 'dola', 'enabled': True, 'reason': '', 'models': [
        {'id': 'seedance-2.0', 'label': 'Fast 2.0', 'enabled': True, 'durations': [5, 10, 15],
         'ratios': ['default', '16:9', '9:16', '1:1', '4:3', '3:4']},
        {'id': 'seedance-2.5', 'label': 'Seedance 2.5（已下架）', 'enabled': False, 'durations': [10],
         'validation_status': 'experimental',
         'ratios': ['default', '16:9', '9:16', '1:1', '4:3', '3:4']}],
        'references': {'images': True, 'audio': True, 'video': True, 'max_images': 30},
        'batch': {'enabled': True, 'minimum': 1, 'maximum': 20},
        'quota_rules': {'ordinary_fast10': {'limit': 2, 'period': 'day'},
                        'expert_fast': {'limit': 4, 'period': 'rolling_7_days', 'unit': 'request'}},
        'profiles': [{'id': 'plugin-expert30', 'label': '专家模式', 'enabled': True,
            'default_model': 'seedance-2.5', 'default_ratio': '16:9', 'default_resolution': '720p',
            'default_duration': 30,
            'references': {'images': True, 'audio': True, 'video': True},
            'features': {'two_part': {'enabled': True, 'durations': [30]}},
            'models': [{'id': model, 'label': labels[model], 'enabled': True, 'durations': list(PLUGIN_EXPERT_DURATIONS),
                'duration_profile': 'plugin-expert30',
                'validation_status': 'sample_verified' if model == 'seedance-2.5' else 'experimental',
                'ratios': list(PLUGIN_EXPERT30_RATIOS),
                'features': {'resolution': {'enabled': True, 'values': ['480p', '720p', '1080p']}}}
                for model in PLUGIN_EXPERT30_MODELS]}]}


def filtered_capabilities(caps, client=None):
    caps = {**caps, 'models': [dict(m) for m in caps.get('models', [])]}
    if 'profiles' in caps:
        caps['profiles'] = [{**p, 'models': [dict(m) for m in p.get('models', [])]} for p in caps['profiles']]
    if client is not None:
        if caps['platform'] not in client.get('allowed_platforms', ['dola']):
            return {**caps, 'enabled': False, 'models': [], 'profiles': [], 'reason': '当前密钥未获准使用该平台'}
        for model in caps['models']:
            model['durations'] = [d for d in model.get('durations', []) if d in client['allowed_durations']]
            model['enabled'] = bool(model.get('enabled', True) and model['durations'])
        for profile in caps.get('profiles', []):
            for model in profile['models']:
                model['durations'] = [d for d in model.get('durations', []) if d in client['allowed_durations']]
                model['enabled'] = bool(model.get('enabled', True) and model['durations'])
            profile['enabled'] = bool(profile.get('enabled', True) and any(m['enabled'] for m in profile['models']))
    return caps


class PlatformAccountRouting:
    """Legacy query aliases target the independent router, never the Dola handler."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get('path', '')
        roots = ('/api/admin/accounts', '/api/admin/trash', '/api/admin/quota-policy',
                 '/api/admin/account-categories')
        if scope['type'] == 'http' and any(path == p or path.startswith(p + '/') for p in roots):
            values = parse_qs(scope.get('query_string', b'').decode('utf-8', 'replace')).get('platform', ['dola'])
            if len(values) != 1 or values[0] not in ('dola', 'doubao'):
                return await JSONResponse({'detail':'不支持的平台', 'error_code':'unsupported_platform'},
                                          status_code=422)(scope, receive, send)
            if values[0] == 'doubao':
                path = '/api/admin/platforms/doubao' + path[len('/api/admin'):]
                scope = {**scope, 'path': path, 'raw_path': path.encode()}
        await self.app(scope, receive, send)


def doubao_proxy_options(ns):
    """Reference choices contain no credentials; resolver retains private URLs."""
    candidates = list(ns['config']._load_proxy_map().values())
    for name in ns['pool'].accounts:
        raw, source = ns['config'].account_proxy_raw(name)
        if raw:
            candidates.append(raw)
    choices = {}
    for raw in candidates:
        if not isinstance(raw, str):
            continue
        try:
            binding = ns['config'].require_isp_proxy(ns['config']._parse_proxy(raw))
            display = ns['_proxy_display'](raw)
            if display['scheme'] != 'http':
                continue
            identifier = hashlib.sha256((binding['server'] + '\0' + binding.get('username', '')).encode()).hexdigest()[:24]
            label = display['route_label'] or display['server']
            country = '英国' if label.startswith('英国') else '德国' if label.startswith('德国') else '未验证地区'
            choices[identifier] = ({'id':identifier, 'label':label, 'server':display['server'], 'country':country}, raw)
        except (ValueError, TypeError, KeyError):
            continue
    for key, local in ns['config'].doubao_local_proxy_options().items():
        identifier = 'local:' + key
        choices[identifier] = ({'id': identifier, **local}, local['server'])
    choices['local:direct'] = ({'id':'local:direct', 'label':'本机直连（豆包专用）',
                               'server':'direct', 'country':'本机网络'}, 'direct')
    return choices


def register_platform_routes(app, ns):
    from fastapi import Header
    from providers.doubao import DoubaoProvider
    from providers.admin_api import create_admin_router
    provider = DoubaoProvider(root=Path(ns['config'].DOUBAO_DATA_DIR),
        scheduler=ns['shared_scheduler'], downloads=Path(ns['config'].DOWNLOAD_DIR),
        enabled=ns['config'].DOUBAO_ENABLED)
    ns['doubao_provider'] = provider
    def proxy_options():
        return doubao_proxy_options(ns)

    def resolve_proxy(identifier):
        item = proxy_options().get(identifier)
        if not item:
            raise ProviderError('proxy_unavailable', '所选代理线路已不存在，请重新选择', 422)
        return item[1]

    app.include_router(create_admin_router(provider, ns['_admin_auth'],
        proxy_choices=lambda: [item[0] for item in proxy_options().values()], resolve_proxy=resolve_proxy,
        reference_root=upload_root(ns['config'], 'doubao')))
    app.add_middleware(PlatformAccountRouting)

    @app.exception_handler(ProviderError)
    async def provider_error(request, error):
        return JSONResponse({'detail': safe_text(error), 'error_code': error.code}, status_code=error.status_code)

    def capabilities(platform, client=None):
        if platform == 'dola':
            caps = dola_capabilities()
            from dola_submission import quota_summary
            caps['quota_summary'] = quota_summary(ns['pool'].list_accounts())
            caps['references']['max_images'] = ns['config'].REFERENCE_IMAGE_MAX_COUNT
            caps['references']['max_bytes'] = ns['config'].REFERENCE_IMAGE_MAX_BYTES
        else:
            caps = provider_for(ns, platform).capabilities()
        return filtered_capabilities(caps, client)

    @app.get('/v1/video-capabilities')
    async def client_capabilities(platform: str = 'dola', authorization: str | None = Header(default=None)):
        return capabilities(platform, ns['_auth'](authorization))

    @app.get('/api/admin/video-capabilities')
    async def admin_capabilities(platform: str = 'dola', authorization: str | None = Header(default=None),
                                 x_admin_key: str | None = Header(default=None)):
        ns['_admin_auth'](x_admin_key, authorization)
        return capabilities(platform)

    @app.get('/api/me/video-capabilities')
    async def member_capabilities(platform: str = 'dola', authorization: str | None = Header(default=None)):
        user = ns['_require_user'](authorization)
        keys = ns['store'].keys_for_user(user['id'])
        granted = [k for k in keys if ns['store'].is_key_valid(k['key']) and
                   platform in k.get('allowed_platforms', ['dola'])]
        client = {'allowed_platforms': [platform] if granted else [],
                  'allowed_durations': sorted({d for k in granted for d in k['allowed_durations']})}
        return capabilities(platform, client)

    @app.on_event('shutdown')
    async def stop_platform_jobs():
        await provider.shutdown()
