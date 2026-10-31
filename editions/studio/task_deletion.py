"""Terminal task visibility and batch reads under the existing service identity."""
import asyncio

from fastapi import Header, HTTPException


def register_routes(ns):
    app, store = ns['app'], ns['store']

    def owned(task_id, hashes):
        row = store.get(task_id, include_deleted=True)
        if row is None or row.get('api_key_hash') not in hashes:
            raise HTTPException(404, 'task not found')
        return row

    def member_hashes(authorization):
        user = ns['_require_user'](authorization)
        return user, store.key_hashes_for_user(user['id'])

    def public_task(row):
        if row.get('deleted_at'):
            return {key: row.get(key) for key in ('id', 'status', 'batch_id', 'batch_index', 'deleted_at')}
        return ns['_member_task'](row)

    async def change(task_id, actor, *, restoring=False):
        lock = ns.setdefault('_result_checks', {}).setdefault(task_id, asyncio.Lock())
        if lock.locked():
            raise HTTPException(409, '任务正在查询、补收或删除，请稍后重试')
        async with lock:
            active = task_id in ns.get('_active_task_ids', set())
            if active:
                raise HTTPException(409, '任务仍有后台处理，暂时不能删除或恢复')
            try:
                row = store.restore(task_id, actor) if restoring else store.soft_delete(task_id, actor, active=active)
            except ValueError as error:
                raise HTTPException(409, str(error)) from None
            if row is None:
                raise HTTPException(404, 'task not found')
            return row

    @app.delete('/api/admin/tasks/{task_id}')
    async def admin_delete(task_id: str, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        identity = ns['_admin_auth'](x_admin_key, authorization)
        actor = 'admin:' + str((identity or {}).get('id') or (identity or {}).get('user_id') or 'key')
        row = await change(task_id, actor)
        return {'ok': True, 'task': row}

    @app.post('/api/admin/tasks/{task_id}/restore')
    async def admin_restore(task_id: str, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        ns['_admin_auth'](x_admin_key, authorization)
        row = await change(task_id, 'admin', restoring=True)
        return {'ok': True, 'task': row}

    @app.delete('/api/me/tasks/{task_id}')
    async def member_delete(task_id: str, authorization: str | None = Header(default=None)):
        user, hashes = member_hashes(authorization)
        owned(task_id, hashes)
        row = await change(task_id, 'user:' + str(user['id']))
        return {'ok': True, 'task': public_task(row)}

    @app.post('/api/me/tasks/{task_id}/restore')
    async def member_restore(task_id: str, authorization: str | None = Header(default=None)):
        user, hashes = member_hashes(authorization)
        owned(task_id, hashes)
        row = await change(task_id, 'user:' + str(user['id']), restoring=True)
        return {'ok': True, 'task': public_task(row)}

    @app.delete('/v1/videos/{task_id}')
    async def api_delete(task_id: str, authorization: str | None = Header(default=None)):
        client = ns['_auth'](authorization)
        owned(task_id, [client.get('api_key_hash')])
        row = await change(task_id, 'api:' + str(client.get('api_key_hash') or 'anonymous'))
        return {'ok': True, 'task': public_task(row)}

    @app.post('/v1/videos/{task_id}/restore')
    async def api_restore(task_id: str, authorization: str | None = Header(default=None)):
        client = ns['_auth'](authorization)
        owned(task_id, [client.get('api_key_hash')])
        row = await change(task_id, 'api:' + str(client.get('api_key_hash') or 'anonymous'), restoring=True)
        return {'ok': True, 'task': public_task(row)}

    def batch_for(batch_id, hashes=None):
        batch = store.get_batch(batch_id)
        if batch is None or (hashes is not None and batch.get('api_key_hash') not in hashes):
            raise HTTPException(404, 'batch not found')
        if hashes is not None:
            # The same sanitization as personal tasks: no account, signed source,
            # cookie, or another member's record is exposed through a batch.
            batch = {**batch, 'tasks': [public_task(row) for row in batch['tasks']]}
            batch.pop('api_key_hash', None)
        return batch

    @app.get('/api/admin/batches/{batch_id}')
    async def admin_batch(batch_id: str, x_admin_key: str | None = Header(default=None), authorization: str | None = Header(default=None)):
        ns['_admin_auth'](x_admin_key, authorization)
        return {'batch': batch_for(batch_id)}

    @app.get('/api/me/batches/{batch_id}')
    async def member_batch(batch_id: str, authorization: str | None = Header(default=None)):
        _, hashes = member_hashes(authorization)
        return {'batch': batch_for(batch_id, hashes)}

    @app.get('/v1/batches/{batch_id}')
    async def api_batch(batch_id: str, authorization: str | None = Header(default=None)):
        client = ns['_auth'](authorization)
        return {'batch': batch_for(batch_id, [client.get('api_key_hash')])}
