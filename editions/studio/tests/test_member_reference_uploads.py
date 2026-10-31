import ast
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from reference_uploads import save_image, attach_images, cleanup_task_images
from store import TaskStore, TaskQuotaExceeded, PendingTaskLimitExceeded
from test_reference_uploads import png
from video_capabilities import submission_duration, validate_video_parameters
import media


class MemberUploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = TaskStore(':memory:')
        self.runs = []
        outer = self
        def require_user(authorization):
            if authorization not in ('Bearer session1', 'Bearer session2'):
                raise HTTPException(401, '请先登录')
            return {'id': 1 if authorization.endswith('1') else 2}
        def client(authorization):
            if authorization not in ('Bearer key1', 'Bearer key2', 'Bearer unowned'):
                raise HTTPException(401, 'invalid key')
            return {'api_key_hash': authorization, 'api_key_name': 'fixture', 'daily_limit': 10,
                    'concurrency_limit': 1, 'allowed_durations': [5, 10, 15, 30],
                    'owner_user_id': None if authorization.endswith('unowned') else
                                     (1 if authorization.endswith('1') else 2)}
        async def validate_urls(urls): return list(urls)
        async def run(*args): outer.runs.append(args)
        self.app = FastAPI()
        names = {'VideoGenRequest', 'TaskResponse', 'create_video', 'member_reference_upload',
                 'member_reference_config', '_submission_identity', '_find_submission'}
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        self.ns = dict(app=self.app, config=SimpleNamespace(DEFAULT_MODEL='seedance-2.5',
            REFERENCE_UPLOAD_DIR=self.root, REFERENCE_IMAGE_MAX_BYTES=1024, REFERENCE_IMAGE_MAX_COUNT=2, MAX_PENDING_TASKS=10),
            BaseModel=BaseModel, Field=Field, model_validator=model_validator, Header=Header, HTTPException=HTTPException, Request=Request,
            asyncio=asyncio, uuid=uuid, json=json, hashlib=hashlib, store=self.store,
            _require_user=require_user, _auth=client, validate_reference_urls=validate_urls,
            save_image=save_image, attach_images=attach_images, cleanup_task_images=cleanup_task_images,
            validate_video_parameters=validate_video_parameters, submission_duration=submission_duration,
            SUPPORTED_DURATIONS=(5, 10, 15, 30),
            pool=SimpleNamespace(available=True, accounts=['fixture']), _run_task=run,
            _resolve_ratio=lambda size, ratio: ratio, TaskQuotaExceeded=TaskQuotaExceeded,
            PendingTaskLimitExceeded=PendingTaskLimitExceeded)
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), 'member-upload-api', 'exec'), self.ns)

    async def asyncTearDown(self):
        self.store._conn.close()
        self.tmp.cleanup()

    async def request(self, path, payload=None, authorization=None, method='POST'):
        content = json.dumps(payload).encode() if isinstance(payload, dict) else payload or b''
        headers = [(b'content-type', b'application/json' if isinstance(payload, dict) else b'image/png')]
        if authorization: headers.append((b'authorization', authorization.encode()))
        messages = []
        async def receive(): return {'type': 'http.request', 'body': content, 'more_body': False}
        async def send(message): messages.append(message)
        await self.app(dict(type='http', asgi={'version':'3.0'}, http_version='1.1', method=method,
            scheme='http', path=path, raw_path=path.encode(), query_string=b'', root_path='', headers=headers,
            client=('127.0.0.1',1), server=('test',80)), receive, send)
        await asyncio.sleep(0)
        return (next(m['status'] for m in messages if m['type']=='http.response.start'),
                json.loads(b''.join(m.get('body',b'') for m in messages)))

    async def upload(self):
        status, data = await self.request('/api/me/reference-images', png(), 'Bearer session1')
        self.assertEqual(status, 201, data)
        return data['id']

    async def test_member_upload_becomes_private_local_reference_for_same_owner_task(self):
        image = await self.upload()
        status, task = await self.request('/v1/videos/generations',
            {'prompt':'原文\n末尾空格 ', 'model':'seedance-2.5', 'duration':30,
             'reference_uploads':[image]}, 'Bearer key1')
        self.assertEqual(status, 200, task)
        refs = json.loads(self.store.get(task['id'])['reference_images'])
        self.assertTrue(refs[0].startswith('upload:'+task['id']+'/'))
        with patch.object(media.config, 'REFERENCE_UPLOAD_DIR', self.root):
            temporary, paths = await media.download_reference_images(refs, task['id'])
        try:
            self.assertEqual(Path(paths[0]).read_bytes(), png())
        finally:
            for p in paths: Path(p).unlink()
            temporary.rmdir()
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.runs[0][2], '原文\n末尾空格 ')

    async def test_upload_requires_member_session_and_verifies_size_and_image_bytes(self):
        self.assertEqual((await self.request('/api/me/reference-images', png()))[0], 401)
        self.assertEqual((await self.request('/api/me/reference-images', b'bad image', 'Bearer session1'))[0], 422)
        self.assertEqual((await self.request('/api/me/reference-images', b'x'*1025, 'Bearer session1'))[0], 413)
        status, limits = await self.request('/api/me/reference-images/config', authorization='Bearer session1', method='GET')
        self.assertEqual((status, limits), (200, {'max_count':2,'max_bytes':1024}))
        self.assertFalse(list(self.root.glob('incoming/*')))

    async def test_other_owner_unowned_key_and_path_traversal_cannot_use_member_image(self):
        image = await self.upload()
        for key, identifier in (('key2',image), ('unowned',image), ('key1','../private.png')):
            status, _ = await self.request('/v1/videos/generations',
                {'prompt':'test', 'reference_uploads':[identifier]}, 'Bearer '+key)
            self.assertEqual(status, 422)
        self.assertEqual(self.store.pending_task_count(), 0)
        self.assertEqual(self.runs, [])

    async def test_fast_unsupported_duration_fails_before_saving_task_or_attaching_file(self):
        for duration in (20, 35):
            status, data = await self.request('/v1/videos/generations',
                {'prompt':'test','model':'seedance-2.0','duration':duration}, 'Bearer key1')
            self.assertEqual(status, 422, data)
        self.assertEqual(self.store.pending_task_count(), 0)
        self.assertEqual(self.runs, [])

    async def test_fast_thirty_preserves_fast_model_duration_and_owned_reference(self):
        image = await self.upload()
        status, task = await self.request('/v1/videos/generations',
            {'prompt':'三十秒原文', 'model':'seedance-2.0', 'duration':30, 'reference_uploads':[image]}, 'Bearer key1')
        self.assertEqual(status, 200, task)
        row = self.store.get(task['id'])
        self.assertEqual((row['model'],row['duration'],row['prompt']),('seedance-2.0',30,'三十秒原文'))
        self.assertEqual(self.runs[0][1:5],('seedance-2.0','三十秒原文',None,30))
        self.assertEqual(len(json.loads(row['reference_images'])), 1)

    async def test_fast_fifteen_accepts_owned_references_and_preserves_requested_parameters(self):
        image = await self.upload()
        status, task = await self.request('/v1/videos/generations',
            {'prompt':'完整原文', 'model':'seedance-2.0', 'duration':15, 'reference_uploads':[image]}, 'Bearer key1')
        self.assertEqual(status, 200, task)
        row = self.store.get(task['id'])
        self.assertEqual((row['model'],row['duration'],row['prompt']),('seedance-2.0',15,'完整原文'))
        self.assertEqual(self.runs[0][1:5],('seedance-2.0','完整原文',None,15))

    async def test_remote_and_uploaded_count_limit_is_combined(self):
        image = await self.upload()
        status, _ = await self.request('/v1/videos/generations',
            {'prompt':'test','reference_uploads':[image],
             'reference_images':['https://public.test/1.png','https://public.test/2.png']}, 'Bearer key1')
        self.assertEqual(status, 422)
        self.assertEqual(self.runs, [])

    async def test_upload_task_replay_does_not_copy_or_generate_twice(self):
        image = await self.upload()
        body = {'prompt':'test','reference_uploads':[image],'idempotency_key':'member-test-duplicate-01'}
        first = await self.request('/v1/videos/generations', body, 'Bearer key1')
        second = await self.request('/v1/videos/generations', body, 'Bearer key1')
        self.assertEqual(first[0], 200)
        self.assertEqual(second[1]['id'], first[1]['id'])
        self.assertEqual(len(list((self.root/'tasks').iterdir())), 1)
        self.assertEqual(len(self.runs), 1)
