import asyncio
import json
import unittest
from fastapi import FastAPI, HTTPException
from store import TaskStore


async def request(app, method, path, *, token='member', admin=False):
    messages = []
    async def receive(): return {'type': 'http.request', 'body': b'', 'more_body': False}
    async def send(message): messages.append(message)
    headers = [(b'authorization', ('Bearer ' + token).encode())] if token else []
    if admin: headers.append((b'x-admin-key', b'admin'))
    await app(dict(type='http', asgi={'version': '3.0'}, http_version='1.1', method=method,
                   scheme='http', path=path, raw_path=path.encode(), query_string=b'', root_path='',
                   headers=headers, client=('127.0.0.1', 1), server=('test', 80)), receive, send)
    status = next(m['status'] for m in messages if m['type'] == 'http.response.start')
    return status, json.loads(b''.join(m.get('body', b'') for m in messages))


class DeletionRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from task_deletion import register_routes
        self.store = TaskStore(':memory:')
        self.store.create_user('owner@test', 'hash')
        key = self.store.create_key('owner-key')['key']
        self.store.assign_key_owner(key, 1)
        self.hash = self.store.hash_api_key(key)
        self.store.create('owned', 'seedance-2.0', 'x', '16:9', 10, api_key_hash=self.hash)
        self.store.update('owned', status='failed')
        self.store.create('foreign', 'seedance-2.0', 'x', '16:9', 10, api_key_hash='foreign')
        self.store.update('foreign', status='completed')
        self.app = FastAPI()
        def auth(key, bearer):
            if key != 'admin': raise HTTPException(401, 'unauthorized')
            return {'kind': 'admin_key'}
        def user(token):
            if token != 'Bearer member': raise HTTPException(401, 'unauthorized')
            return {'id': 1}
        self.ns = dict(app=self.app, store=self.store, _admin_auth=auth, _require_user=user,
                       _active_task_ids=set(), _result_checks={},
                       _auth=lambda bearer: {'api_key_hash':self.hash},
                       _member_task=lambda row:{k:row.get(k) for k in ('id','status','deleted_at')})
        register_routes(self.ns)

    async def asyncTearDown(self): self.store._conn.close()

    async def test_member_can_delete_and_restore_only_owned_terminal_tasks(self):
        status, result = await request(self.app, 'DELETE', '/api/me/tasks/owned')
        self.assertEqual(status, 200, result)
        self.assertIsNone(self.store.get('owned'))
        self.assertEqual(self.store.key_usage(self.hash)['total'], 1)
        status, _ = await request(self.app, 'DELETE', '/api/me/tasks/foreign')
        self.assertEqual(status, 404)
        status, restored = await request(self.app, 'POST', '/api/me/tasks/owned/restore')
        self.assertEqual(status, 200, restored)
        self.assertEqual(self.store.get('owned')['status'], 'failed')

    async def test_active_collection_and_worker_block_deletion_even_for_failed_row(self):
        lock = self.ns['_result_checks'].setdefault('owned', asyncio.Lock())
        async with lock:
            status, _ = await request(self.app, 'DELETE', '/api/admin/tasks/owned', admin=True)
            self.assertEqual(status, 409)
        self.ns['_active_task_ids'].add('owned')
        status, _ = await request(self.app, 'DELETE', '/api/admin/tasks/owned', admin=True)
        self.assertEqual(status, 409)
        self.assertIsNotNone(self.store.get('owned'))

    async def test_batch_member_response_hides_deleted_clip_media_and_foreign_batch(self):
        def task(i): return dict(id='child'+str(i),model='seedance-2.0',prompt='x',ratio='16:9',duration=30)
        self.store.create_batch('batch', [task(1), task(2)], api_key_hash=self.hash)
        self.store.merge_clips('child1', [dict(clip_id='a',video_url='/videos/a.mp4')])
        self.store.update('child1', status='completed')
        self.store.soft_delete('child1','admin')
        status, data = await request(self.app, 'GET', '/api/me/batches/batch')
        self.assertEqual(status, 200, data)
        self.assertEqual(data['batch']['counts']['videos'], 1)
        self.assertTrue(data['batch']['tasks'][0]['deleted_at'])
        self.assertNotIn('clips', data['batch']['tasks'][0])
        self.assertNotIn('account', data['batch']['tasks'][1])


if __name__ == '__main__': unittest.main()
