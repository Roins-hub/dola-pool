import unittest
from store import TaskStore
from video_capabilities import submission_duration, validate_video_parameters
import ast
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace


class IdempotencyTests(unittest.TestCase):
    def test_retries_return_original_task_without_using_another_queue_slot(self):
        store = TaskStore(':memory:')
        try:
            options = dict(idempotency_scope='admin:a', idempotency_key='fixture-key', request_fingerprint='same-body', max_pending=1)
            store.create('first', 'seedance-2.5', '原文', '9:16', 30, **options)
            replay = store.create('second', 'seedance-2.5', '原文', '9:16', 30, **options)
            self.assertEqual(replay['id'], 'first')
            self.assertIsNone(store.get('second'))
            self.assertEqual(store.pending_task_count(), 1)
            with self.assertRaises(ValueError):
                store.create('third', 'seedance-2.5', '改了原文', '9:16', 30,
                    **{**options, 'request_fingerprint':'changed-body'})
            self.assertIsNone(store.find_idempotent('admin:other', 'fixture-key', 'same-body'))
            self.assertEqual(store.find_idempotent('admin:a', 'fixture-key', 'same-body')['id'], 'first')
        finally:
            store._conn.close()


class IdempotencyAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_network_retry_schedules_generation_once_and_rejects_changed_body(self):
        from test_task_evidence_api import TaskEvidenceAPITests
        from pydantic import BaseModel, Field, model_validator
        from reference_uploads import attach_images, admin_owner, cleanup_task_images
        from media import validate_reference_urls
        fixture = TaskEvidenceAPITests()
        await fixture.asyncSetUp()
        queued = []
        def schedule(coroutine):
            queued.append(True)
            coroutine.close()
        async def run(*args): pass
        def auth(key, bearer):
            if key != 'fixture-admin': raise fixture.ns['HTTPException'](401, 'unauthorized')
            return {'kind':'admin_key'}
        try:
            fixture.ns.update(BaseModel=BaseModel, Field=Field, model_validator=model_validator, hashlib=hashlib, json=json,
                config=SimpleNamespace(DEFAULT_MODEL='seedance-2.5',PUBLIC_BASE='http://local',
                    MAX_PENDING_TASKS=1,REFERENCE_IMAGE_MAX_COUNT=3,REFERENCE_UPLOAD_DIR=fixture.root.name),
                SUPPORTED_DURATIONS=(5,10,15,30), SIZE_TO_RATIO={},
                asyncio=SimpleNamespace(create_task=schedule, to_thread=asyncio.to_thread), _run_task=run,
                _admin_auth=auth,admin_owner=admin_owner,attach_images=attach_images,
                validate_video_parameters=validate_video_parameters,
                submission_duration=submission_duration,
                cleanup_task_images=cleanup_task_images,validate_reference_urls=validate_reference_urls,
                pool=SimpleNamespace(accounts=['fixture']),
                _anonymous_client=lambda:{'api_key_hash':None,'api_key_name':'fixture'},
                TaskQuotaExceeded=__import__('store').TaskQuotaExceeded,
                PendingTaskLimitExceeded=__import__('store').PendingTaskLimitExceeded,
                uuid=__import__('uuid'))
            names={'VideoGenRequest','TaskResponse','_submission_identity','_find_submission','_resolve_ratio','admin_generate'}
            tree=ast.parse(Path('server.py').read_text(encoding='utf-8'))
            nodes=[node for node in tree.body if isinstance(node,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)) and node.name in names]
            exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),'idempotency-api','exec'),fixture.ns)
            body={'prompt':'完整用户原文','model':'seedance-2.5','duration':30,'idempotency_key':'fixture-key'}
            first=await fixture.request('/api/admin/generate','POST',data=body)
            second=await fixture.request('/api/admin/generate','POST',data=body)
            self.assertEqual(first.status_code,200)
            self.assertEqual(second.status_code,200)
            self.assertEqual(first.json()['id'],second.json()['id'])
            self.assertTrue(second.json()['replayed'])
            self.assertEqual(len(queued),1)
            changed=await fixture.request('/api/admin/generate','POST',data={**body,'prompt':'另一个内容'})
            self.assertEqual(changed.status_code,409)
            self.assertEqual(len(queued),1)
        finally:
            await fixture.asyncTearDown()
