"""Doubao task preferences and owned media survive queueing and recovery."""
import ast
import asyncio
import hashlib
import json
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError, model_validator

from platform_runtime import create_platform_task, run_platform_task, inspect_platform_task, generation_options, task_generation_options
from providers.base import ProviderError
from reference_uploads import admin_owner, save_media
from store import TaskStore


def request_namespace(store=None):
    names = {'VideoGenRequest', 'TaskResponse', '_submission_identity', '_find_submission'}
    nodes = [node for node in ast.parse(Path('server.py').read_text(encoding='utf-8')).body
             if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    namespace = {'BaseModel': BaseModel, 'Field': Field, 'model_validator': model_validator, 'hashlib': hashlib, 'json': json,
                 'config': SimpleNamespace(DEFAULT_MODEL='seedance-2.0'), 'store': store,
                 'HTTPException': HTTPException, '_resolve_ratio': lambda size, ratio: ratio}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), 'generation-options-api', 'exec'), namespace)
    return namespace


class GenerationSchemaTests(unittest.TestCase):
    def setUp(self):
        self.ns = request_namespace()
        self.Request = self.ns['VideoGenRequest']

    def test_doubao_preferences_are_typed_and_survive_request_validation(self):
        request = self.Request(platform='doubao', prompt='cat', resolution='1080p',
                               require_no_watermark=True, lead_prompt='keep consistent')
        self.assertEqual(request.model_dump()['resolution'], '1080p')
        self.assertTrue(request.require_no_watermark)
        with self.assertRaises(ValidationError):
            self.Request(prompt='cat', resolution='arbitrary')

    def test_only_new_doubao_requests_default_to_one_confirmation(self):
        self.assertTrue(self.Request(platform='doubao',prompt='cat').auto_confirm)
        self.assertFalse(self.Request(prompt='cat').auto_confirm)
        self.assertFalse(self.Request(platform='doubao',prompt='cat',auto_confirm=False).auto_confirm)
        for enabled in (True,False):
            values=self.Request(platform='doubao',prompt='cat',auto_confirm=enabled).model_dump()
            row={'generation_options':json.dumps(generation_options(values))}
            self.assertEqual(task_generation_options(row).get('auto_confirm'),enabled)

    def test_optional_preferences_do_not_change_legacy_dola_fingerprint(self):
        base = {'prompt': 'cat', 'idempotency_key': 'same-intent-1234'}
        old = self.ns['_submission_identity'](self.Request(**base), 'member:1')
        extra = self.ns['_submission_identity'](self.Request(**base, resolution='1080p',
                    require_no_watermark=True, lead_prompt='before'), 'member:1')
        self.assertEqual(old, extra)

    def test_each_doubao_preference_changes_identity_but_omitted_defaults_do_not(self):
        base = {'platform': 'doubao', 'prompt': 'cat', 'idempotency_key': 'same-intent-1234'}
        identity = self.ns['_submission_identity']
        old = identity(self.Request(**base), 'member:1')
        self.assertEqual(old, identity(self.Request(**base, resolution='auto',
                                      require_no_watermark=False, cast=[], lead_prompt=''), 'member:1'))
        for extra in ({'resolution': '720p'}, {'require_no_watermark': True},
                      {'lead_prompt': 'before'}):
            self.assertNotEqual(old['request_fingerprint'],
                identity(self.Request(**base, **extra), 'member:1')['request_fingerprint'])

    def test_retired_cast_fields_are_not_advertised_and_nonempty_values_cannot_disappear(self):
        properties = self.Request.model_json_schema()['properties']
        self.assertNotIn('cast', properties)
        self.assertNotIn('cast_ids', properties)
        for platform in ('dola', 'doubao'):
            for legacy in ({'cast': [{'name': 'cat'}]}, {'cast_ids': ['a'*32]}):
                with self.subTest(platform=platform, legacy=legacy):
                    with self.assertRaises(HTTPException) as caught:
                        self.Request(platform=platform, prompt='cat', **legacy)
                    self.assertEqual(caught.exception.status_code, 422)
                    self.assertEqual(caught.exception.detail['error_code'], 'unsupported_cast')

    def test_empty_legacy_cast_defaults_are_accepted_without_becoming_generation_options(self):
        request = self.Request(platform='doubao', prompt='cat', cast=[], cast_ids=[])
        self.assertNotIn('cast', request.model_dump())
        self.assertNotIn('cast_ids', request.model_dump())
        self.assertEqual(generation_options(request.model_dump()), {'auto_confirm':True})

    def test_recovery_cannot_silently_strip_nonempty_legacy_cast_preferences(self):
        for options in ({'cast': [{'name': 'cat'}]}, {'cast_ids': ['a'*32]}):
            with self.subTest(options=options):
                with self.assertRaises(ProviderError) as caught:
                    task_generation_options({'generation_options': json.dumps(options)})
                self.assertEqual(caught.exception.code, 'unsupported_cast')


class GenerationPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = TaskStore(str(self.root / 'tasks.db'))
        self.rows = asyncio.Queue()
        rows = self.rows
        class Provider:
            def __init__(self):
                self.submissions = 0
                self.recovery_checks = 0
                self.inspections = []
            def validate_request(self, values):
                return {'model': values['model'], 'duration': values['duration'],
                        'ratio': values['ratio'], 'capability_version': 'fixture-v1'}
            def list_accounts(self):
                return [{'name': 'fixture'}]
            async def run_task(self, row, on_event):
                self.submissions += 1
                rows.put_nowait(dict(row))
                return {'status': 'failed', 'phase': 'rejected', 'error': 'offline fixture'}
            async def resume_task(self, row, on_event):
                self.recovery_checks += 1
                rows.put_nowait(dict(row))
                return {'status': 'failed', 'phase': 'query_timeout', 'error': 'offline recovery fixture'}
            async def inspect_task(self, row, download=False):
                self.inspections.append((row, download))
                return {'status': 'failed', 'phase': 'query_timeout', 'error': 'offline recovery fixture'}
        self.ns = request_namespace(self.store)
        self.ns.update(store=self.store, doubao_provider=Provider(), admin_owner=admin_owner,
            validate_reference_urls=AsyncMock(side_effect=lambda refs: list(refs)),
            config=SimpleNamespace(REFERENCE_UPLOAD_DIR=self.root / 'references', MAX_PENDING_TASKS=20,
                                   REFERENCE_MEDIA_MAX_COUNT=10, PUBLIC_BASE='http://fixture'),
            key_limiter=SimpleNamespace(acquire=AsyncMock(), release=AsyncMock()), _result_checks={})
        self.admin = {'kind': 'admin_key'}
        self.client = {'allowed_platforms': ['doubao'], 'allowed_durations': [15],
                       'owner_user_id': 1, 'api_key_hash': 'fixture-key'}

    async def asyncTearDown(self):
        await asyncio.sleep(0)
        self.store._conn.close()
        self.temp.cleanup()

    def request(self, **extras):
        return self.ns['VideoGenRequest'](platform='doubao', model='fixture-model',
            prompt='original', duration=15, ratio='16:9', **extras)

    def audio(self, owner='admin_key', platform='doubao'):
        # The existing upload validator accepts an MP4-family audio container.
        box = lambda kind, payload: struct.pack('>I4s', len(payload)+8, kind)+payload
        data = box(b'ftyp', b'isom0000') + box(b'moov', b'0') + box(b'mdat', b'1234')
        folder = self.root / 'references' / platform
        return save_media(data, 'voice.m4a', folder, owner, 1024)['id']

    async def test_owned_audio_is_attached_and_preferences_restore_in_real_worker(self):
        identifier = self.audio()
        response = await create_platform_task(self.ns, self.request(reference_media_uploads=[identifier],
            resolution='1080p', require_no_watermark=True, lead_prompt='before'), self.client, self.admin)
        observed = await asyncio.wait_for(self.rows.get(), 2)
        self.assertEqual(observed['resolution'], '1080p')
        self.assertTrue(observed['require_no_watermark'])
        self.assertEqual(observed['lead_prompt'], 'before')
        self.assertNotIn('cast', observed)
        self.assertEqual(observed['reference_image_paths'], [])
        media = observed['reference_media_paths']
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0]['kind'], 'audio')
        self.assertTrue(Path(media[0]['path']).is_file())
        self.assertTrue(Path(media[0]['path']).is_relative_to(self.root / 'references' / 'doubao' / 'tasks' / response.id))
        stored = self.store.get(response.id)
        self.assertEqual(json.loads(stored['generation_options'])['resolution'], '1080p')
        self.store._conn.close()
        self.store = TaskStore(str(self.root / 'tasks.db'))
        self.ns['store'] = self.store
        self.store.update(response.id, status='processing')
        await run_platform_task(self.ns, self.store.get(response.id), self.client, resume=True)
        recovered = await asyncio.wait_for(self.rows.get(), 2)
        self.assertEqual(recovered['reference_media_paths'], media)
        self.assertEqual(recovered['resolution'], '1080p')

    async def test_other_owner_or_platform_audio_is_rejected_before_queue_creation(self):
        identifiers = (self.audio(owner='user:2'), self.audio(platform='dola'))
        for identifier in identifiers:
            with self.assertRaises(HTTPException) as error:
                await create_platform_task(self.ns, self.request(reference_media_uploads=[identifier]),
                                           self.client, self.admin)
            self.assertEqual(error.exception.status_code, 422)
        self.assertEqual(self.store.recent_tasks(platform='all'), [])
        self.assertTrue(self.rows.empty())

    async def test_unowned_key_cannot_attach_media_and_no_job_or_private_copy_is_created(self):
        identifier = self.audio(owner='user:None')
        from providers.base import ProviderError
        with self.assertRaises(ProviderError):
            await create_platform_task(self.ns, self.request(reference_media_uploads=[identifier]),
                {**self.client, 'owner_user_id': None})
        self.assertEqual(self.store.recent_tasks(platform='all'), [])
        self.assertFalse((self.root / 'references' / 'doubao' / 'tasks').exists())

    async def test_same_intent_replays_but_changed_doubao_options_cannot_replace_original(self):
        first = await create_platform_task(self.ns, self.request(idempotency_key='intent-123456',
            resolution='720p'), self.client, self.admin)
        await asyncio.wait_for(self.rows.get(), 2)
        replay = await create_platform_task(self.ns, self.request(idempotency_key='intent-123456',
            resolution='720p'), self.client, self.admin)
        self.assertEqual(replay.id, first.id)
        self.assertTrue(replay.replayed)
        with self.assertRaises(HTTPException) as error:
            await create_platform_task(self.ns, self.request(idempotency_key='intent-123456',
                resolution='1080p'), self.client, self.admin)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(len(self.store.recent_tasks(platform='all')), 1)

    async def test_legacy_task_generation_options_default_is_empty(self):
        self.store.create('legacy', 'seedance-2.0', 'old', 'default', 10)
        self.assertEqual(json.loads(self.store.get('legacy')['generation_options']), {})

    async def test_historical_role_task_can_resume_and_inspect_without_sending_a_new_request(self):
        options = {'resolution': '720p', 'lead_prompt': 'original instructions',
                   'cast': [{'name': 'cat', 'image_indices': [1]}], 'cast_ids': ['a'*32]}
        self.store.create('legacy', 'fixture-model', 'old', '16:9', 15, platform='doubao',
            generation_options=json.dumps(options))
        self.store.update('legacy', status='processing', phase='accepted', conversation_id='original-conversation')
        provider = self.ns['doubao_provider']
        await run_platform_task(self.ns, self.store.get('legacy'), self.client, resume=True)
        self.assertEqual(provider.recovery_checks, 1)
        resumed = await asyncio.wait_for(self.rows.get(), 2)
        self.assertEqual(resumed['conversation_id'], 'original-conversation')
        self.assertEqual((resumed['resolution'], resumed['lead_prompt']), ('720p', 'original instructions'))
        self.assertNotIn('cast', resumed)
        self.assertNotIn('cast_ids', resumed)
        result = await inspect_platform_task(self.ns, self.store.get('legacy'), download=True)
        self.assertEqual(result['state'], 'query_timeout')
        self.assertEqual(provider.submissions, 0)
        self.assertEqual(provider.recovery_checks, 1)
        self.assertEqual(len(provider.inspections), 1)
        inspected, download = provider.inspections[0]
        self.assertTrue(download)
        self.assertEqual(inspected['conversation_id'], 'original-conversation')
        self.assertEqual(inspected['resolution'], '720p')
        self.assertNotIn('cast', inspected)
        self.assertNotIn('cast_ids', inspected)


if __name__ == '__main__':
    unittest.main()
