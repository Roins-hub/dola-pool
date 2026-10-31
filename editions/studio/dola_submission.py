"""Project batch orchestration: each child owns one real native submission."""
import asyncio
import json
import time
import uuid

from fastapi import Header, HTTPException
from fastapi.responses import JSONResponse

from media import validate_reference_urls
from quota_ledger import QUOTA_TYPES
from reference_uploads import api_owner, attach_images, cleanup_task_images
from store import TaskQuotaExceeded, PendingTaskLimitExceeded
from video_capabilities import submission_duration, validate_video_parameters, validate_plugin_expert30_parameters


def quota_summary(accounts):
    result = {}
    for kind, policy in QUOTA_TYPES.items():
        values = [(a.get('quota_pools') or {}).get(kind, {}) for a in accounts]
        result[kind] = {
            **{key: value for key, value in policy.items() if key != 'limit'},
            **{key: sum(int(value.get(key) or 0) for value in values)
               for key in ('limit', 'used', 'reserved', 'remaining', 'uncertain')},
        }
    return result


def public_batch(ns, batch, *, admin=False):
    fields = ('batch_id', 'requested_count', 'status', 'reason', 'counts', 'progress', 'replayed')
    return {**{key: batch[key] for key in fields if key in batch},
            'tasks': [({key: row.get(key) for key in ('id', 'status', 'batch_id', 'batch_index', 'deleted_at')}
                       if row.get('deleted_at') else ns['TaskResponse'](**row).model_dump() if admin else ns['_member_task'](row))
                      for row in batch['tasks']]}


async def _cleanup_references(ns, batch_id):
    batch = ns['store'].get_batch(batch_id)
    if not batch or any(row['status'] not in ('completed', 'failed') for row in batch['tasks']):
        return
    if any(row['id'] in ns.get('_active_task_ids', set()) or
           (ns.get('_result_checks', {}).get(row['id']) and ns['_result_checks'][row['id']].locked())
           for row in batch['tasks']):
        return
    try:
        from batch_references import cleanup_batch_references
        await cleanup_batch_references(batch_id, ns['config'].REFERENCE_UPLOAD_DIR)
    except (OSError, ValueError):
        # Cleanup never changes generation results or quota accounting.
        pass


def start_batch(ns, batch_id, client):
    registry = ns.setdefault('_batch_runners', {})
    current = registry.get(batch_id)
    if current and not current.done():
        return current
    task = asyncio.create_task(run_batch(ns, batch_id, client))
    registry[batch_id] = task
    return task


async def run_batch(ns, batch_id, client):
    store = ns['store']
    workers = []
    signals = ns.setdefault('_batch_receipts', {})
    batch = store.get_batch(batch_id)
    if not batch or batch['status'] == 'paused':
        return
    store.set_batch_state(batch_id, 'processing')
    try:
        for saved in batch['tasks']:
            current = store.get_batch(batch_id)
            if not current or current['status'] == 'paused':
                break
            row = store.get(saved['id'])
            if not row or row['status'] != 'queued' or row.get('conversation_id'):
                continue
            # Never interpret a failed/unknown send as an unsubmitted child.
            if any(event['phase'] in ('sending', 'submitted', 'submission_unknown') for event in store.task_events(row['id'])):
                store.set_batch_state(batch_id, 'paused', '存在发送结果未知的申请，请核对原会话；未重新提交')
                break
            signal = signals.setdefault(row['id'], asyncio.Event())
            refs = json.loads(row.get('reference_images') or '[]')
            worker = asyncio.create_task(ns['_run_task'](row['id'], row['model'], row['prompt'],
                None if row.get('ratio') == 'default' else row.get('ratio'), row['duration'], refs, client))
            registry = ns.setdefault('_batch_workers', {})
            registry[row['id']] = worker
            def finished_worker(finished, key=row['id']):
                registry.pop(key, None)
                if not finished.cancelled():
                    finished.exception()
                asyncio.create_task(_cleanup_references(ns, batch_id))
            worker.add_done_callback(finished_worker)
            workers.append(worker)
            receipt = asyncio.create_task(signal.wait())
            done, _ = await asyncio.wait((worker, receipt), return_when=asyncio.FIRST_COMPLETED)
            if not signal.is_set():
                receipt.cancel()
                await asyncio.gather(receipt, return_exceptions=True)
                store.set_batch_state(batch_id, 'paused', '本轮未确认新会话，已暂停后续申请；已发送结果继续收取')
                break
            await receipt
            # The receipt callback has persisted the new conversation before this signal.
            store.set_batch_state(batch_id, 'processing')
            await asyncio.sleep(.5)
        # Collection tasks remain referenced independently. A paused submission
        # coordinator must finish so an explicit Resume can schedule unsent children.
    finally:
        # Cancellation preserves persisted children for startup recovery.
        for saved in batch['tasks']:
            signals.pop(saved['id'], None)


async def create_tasks(ns, req, client, admin_identity=None):
    store, config = ns['store'], ns['config']
    count = getattr(req, 'batch_count', 1)
    if type(count) is not int or not 1 <= count <= 20:
        raise HTTPException(422, '申请数量必须为1～20的整数')
    if req.platform != 'dola':
        raise HTTPException(422, '本次批量能力适用于Dola')
    keep_open = getattr(req, 'keep_browser_open', False)
    if keep_open:
        trusted = {store.hash_api_key(key) for key in getattr(config, 'API_KEYS', [])}
        if admin_identity is None and client.get('api_key_hash') not in trusted:
            raise HTTPException(403, '保留浏览器仅限管理员或服务端密钥测试')
        if req.duration_profile != 'plugin-expert30' or count != 1:
            raise HTTPException(422, '保留浏览器仅支持单次插件专家测试')
    if admin_identity is None and 'dola' not in client.get('allowed_platforms', ['dola']):
        raise HTTPException(403, '当前密钥未获准使用Dola')
    duration = req.duration or 10
    ratio = ns['_resolve_ratio'](req.size, req.ratio)
    options = {}
    try:
        if req.duration_profile == 'plugin-expert30':
            params = validate_plugin_expert30_parameters(req.model, duration, ratio, req.resolution)
            model, duration, ratio = params['model'], params['duration'], params['ratio']
            two_part = getattr(req, 'two_part', False)
            if type(two_part) is not bool or (two_part and duration != 30):
                raise ValueError('双段规则仅适用于插件专家30秒申请')
            options = {'resolution': params['resolution'], 'require_no_watermark': True,
                       'protocol': 'expert', 'two_part': two_part, 'requested_duration': duration}
            if keep_open:
                options['keep_browser_open'] = True
        else:
            model, duration = validate_video_parameters(req.model, duration)
            submission_duration(model, duration, req.duration_profile)
            if count > 1 and (model != 'seedance-2.0' or duration != 10 or req.duration_profile):
                raise ValueError('批量申请支持普通Fast10秒和插件专家')
        if len(req.reference_images)+len(req.reference_uploads) > config.REFERENCE_IMAGE_MAX_COUNT:
            raise ValueError('参考图片超过数量限制')
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    if admin_identity is None and duration not in client.get('allowed_durations', []):
        raise HTTPException(422, '当前密钥不允许此申请时长')
    scope = 'admin:' + ns['admin_owner'](admin_identity) if admin_identity is not None else 'api:' + str(client.get('api_key_hash'))
    identity = ns['_submission_identity'](req, scope)
    if identity:
        try:
            replay = store.find_idempotent(identity['idempotency_scope'], identity['idempotency_key'], identity['request_fingerprint'])
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        if replay:
            if replay.get('deleted_at'):
                raise HTTPException(409, '原任务已删除，未重新申请；请撤销删除后查看')
            if replay.get('batch_id'):
                batch = store.get_batch(replay['batch_id'])
                return JSONResponse({**public_batch(ns, batch, admin=admin_identity is not None), 'replayed': True})
            return ns['TaskResponse'](**replay, replayed=True)
    if not ns['pool'].accounts:
        raise HTTPException(503, 'Dola号池暂无账号')
    owner = ns['admin_owner'](admin_identity) if admin_identity is not None else api_owner(client)
    if admin_identity is None and (req.reference_uploads or req.reference_media_uploads) and owner is None:
        raise HTTPException(422, '上传参考图片需要有效的API密钥')
    try:
        refs = await validate_reference_urls(req.reference_images)
    except ValueError as error:
        raise HTTPException(422, str(error)) from None
    ids = ['video_' + uuid.uuid4().hex for _ in range(count)]
    rows = []
    try:
        for task_id in ids:
            try:
                attached = await asyncio.to_thread(attach_images, req.reference_uploads, config.REFERENCE_UPLOAD_DIR,
                    owner, task_id, media_ids=req.reference_media_uploads,
                    media_max_count=getattr(config, 'REFERENCE_MEDIA_MAX_COUNT', 10))
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            rows.append({'id': task_id, 'platform': 'dola', 'model': model, 'duration': duration,
                'prompt': req.prompt, 'ratio': ratio or 'default', 'duration_profile': req.duration_profile,
                'reference_images': json.dumps(refs+attached, ensure_ascii=False),
                'generation_options': json.dumps(options, ensure_ascii=False),
                'api_key_name': client.get('api_key_name'),
                'concurrency_limit': client.get('concurrency_limit', 0)})
        if count == 1:
            fields = {**rows[0]}
            fields.pop('id')
            fields.pop('api_key_name')
            replay = store.create(ids[0], **fields, api_key_hash=client.get('api_key_hash'),
                api_key_name=client.get('api_key_name'), daily_limit=client.get('daily_limit', 0),
                max_pending=config.MAX_PENDING_TASKS, **identity)
            if replay:
                cleanup_task_images(config.REFERENCE_UPLOAD_DIR, ids[0])
                if replay.get('deleted_at'):
                    raise HTTPException(409, '原任务已删除，未重新申请')
                return ns['TaskResponse'](**replay, replayed=True)
            row = store.get(ids[0])
            asyncio.create_task(ns['_run_task'](row['id'], model, req.prompt, ratio, duration, refs+attached, client))
            return ns['TaskResponse'](**row)
        batch = store.create_batch('batch_' + uuid.uuid4().hex, rows,
            api_key_hash=client.get('api_key_hash'),
            daily_limit=client.get('daily_limit', 0), max_pending=config.MAX_PENDING_TASKS, **identity)
        if batch.get('replayed'):
            for task_id in ids:
                cleanup_task_images(config.REFERENCE_UPLOAD_DIR, task_id)
        else:
            start_batch(ns, batch['batch_id'], client)
        return JSONResponse(public_batch(ns, batch, admin=admin_identity is not None))
    except (TaskQuotaExceeded, PendingTaskLimitExceeded) as error:
        for task_id in ids:
            cleanup_task_images(config.REFERENCE_UPLOAD_DIR, task_id)
        raise HTTPException(429, str(error)) from None
    except BaseException:
        for task_id in ids:
            cleanup_task_images(config.REFERENCE_UPLOAD_DIR, task_id)
        raise


def register_routes(ns):
    async def resume(batch_id, hashes=None):
        batch = ns['store'].get_batch(batch_id)
        if not batch or (hashes is not None and batch.get('api_key_hash') not in hashes):
            raise HTTPException(404, 'batch not found')
        if batch['status'] not in ('paused', 'waiting_quota', 'queued'):
            raise HTTPException(409, '此批次无需继续')
        ns['store'].set_batch_state(batch_id, 'queued')
        start_batch(ns, batch_id, ns['_task_client'](batch['tasks'][0]))
        return {'batch': public_batch(ns, ns['store'].get_batch(batch_id), admin=hashes is None)}

    @ns['app'].post('/api/admin/batches/{batch_id}/resume')
    async def admin_resume(batch_id: str, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        ns['_admin_auth'](x_admin_key, authorization)
        return await resume(batch_id)

    @ns['app'].post('/api/me/batches/{batch_id}/resume')
    async def member_resume(batch_id: str, authorization: str | None = Header(default=None)):
        user = ns['_require_user'](authorization)
        hashes = [ns['store'].hash_api_key(key['key']) for key in ns['store'].keys_for_user(user['id']) if key.get('enabled')]
        return await resume(batch_id, hashes)
