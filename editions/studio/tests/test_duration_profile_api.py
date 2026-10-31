"""Profile persistence and server boundaries without browsers or external generation."""
import ast
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from store import TaskStore
from video_capabilities import FAST15_PROFILE, submission_duration
from video_prompt import build_video_prompt


class DurationProfileStoreTests(unittest.TestCase):
    def test_profile_is_nullable_migrates_without_rewriting_old_tasks_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as root:
            path = str(Path(root) / 'tasks.db')
            conn = sqlite3.connect(path)
            conn.execute('CREATE TABLE tasks (id TEXT PRIMARY KEY, model TEXT, prompt TEXT, ratio TEXT, '
                         'duration INTEGER, status TEXT, video_url TEXT, error TEXT, created_at REAL, updated_at REAL)')
            conn.execute("INSERT INTO tasks VALUES ('legacy','seedance-2.0','old','default',30,'queued',NULL,NULL,1,1)")
            conn.commit(); conn.close()
            store = TaskStore(path)
            try:
                self.assertEqual(store.get('legacy')['duration'], 30)
                self.assertIsNone(store.get('legacy').get('duration_profile'))
                store.create('profile', 'seedance-2.0', 'new', 'default', 15, duration_profile=FAST15_PROFILE)
                store.update('profile', status='processing', account='a')
                store.fail_unconfirmed_submissions()
                self.assertEqual(store.get('profile')['duration_profile'], FAST15_PROFILE)
                store.record_evidence('profile', 'account_retry', {'account': 'b'})
                self.assertEqual(store.get('profile')['duration_profile'], FAST15_PROFILE)
            finally:
                store._conn.close()
            reopened = TaskStore(path)
            try:
                self.assertEqual((reopened.get('profile')['duration'], reopened.get('profile')['duration_profile']),
                                 (15, FAST15_PROFILE))
                self.assertIsNone(reopened.get('legacy')['duration_profile'])
            finally:
                reopened._conn.close()

    def test_gallery_projection_preserves_target_and_profile(self):
        store = TaskStore(':memory:')
        try:
            store.create('profile', 'seedance-2.0', 'test', 'default', 15, duration_profile=FAST15_PROFILE)
            store.update('profile', status='completed', video_url='http://local/videos/test.mp4')
            self.assertEqual(store.completed_videos()[0]['duration'], 15)
            self.assertEqual(store.completed_videos()[0]['duration_profile'], FAST15_PROFILE)
        finally:
            store._conn.close()


class DurationProfileApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_member_reference_uploads import MemberUploadTests
        from reference_uploads import admin_owner
        self.fixture = MemberUploadTests()
        await self.fixture.asyncSetUp()
        ns = self.fixture.ns
        ns.update(submission_duration=submission_duration, admin_owner=admin_owner,
                  _admin_auth=lambda *_: {'kind': 'admin_key'},
                  _anonymous_client=lambda: {'api_key_hash': None, 'api_key_name': 'admin'})
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        names = {'admin_generate', 'get_video', '_member_task'}
        nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), 'duration-profile-api', 'exec'), ns)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_admin_pins_one_expert_account_and_public_api_rejects_pin(self):
        ns = self.fixture.ns
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        helper = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                      and n.name == '_admin_expert_account')
        exec(compile(ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
                     'expert-account-admin', 'exec'), ns)
        ns['pool'].list_accounts = lambda: [{'name': 'fixture'}]
        ns['pool']._schedulable = lambda account, model: account['name'] == 'fixture'
        ns['config'].account_proxy_raw = lambda account: ('http://bound-isp:80', 'file')
        ns['config']._parse_proxy = lambda raw: {'server': raw}
        ns['config'].require_isp_proxy = lambda proxy: proxy
        ns['config'].proxy_for = lambda account: {'server': 'http://bound-isp:80'}
        body = {'prompt': 'test', 'model': 'seedance-2.0', 'duration': 15,
                'duration_profile': 'pro-fast15', 'expert_account': 'fixture'}
        status, task = await self.fixture.request('/api/admin/generate', body)
        self.assertEqual(status, 200, task)
        self.assertEqual(self.fixture.store.get(task['id'])['status'], 'queued')
        self.assertIn(('queued', 'fixture'), [(e['phase'], e['details'].get('account'))
            for e in self.fixture.store.task_events(task['id'])])
        count = len(self.fixture.runs)
        status, _ = await self.fixture.request('/v1/videos/generations', body, 'Bearer key1')
        self.assertEqual(status, 403)
        for invalid in ({**body, 'duration_profile': None},
                        {**body, 'expert_account': 'unknown'}):
            status, _ = await self.fixture.request('/api/admin/generate', invalid)
            self.assertEqual(status, 422)
        self.assertEqual(len(self.fixture.runs), count)

    async def test_member_target_permission_is_fifteen_and_profile_is_visible_on_get_and_member_projection(self):
        original_auth = self.fixture.ns['_auth']
        self.fixture.ns['_auth'] = lambda auth: {**original_auth(auth), 'allowed_durations': [15]}
        status, task = await self.fixture.request('/v1/videos/generations',
            {'prompt': 'test', 'model': 'seedance_v2.0', 'duration': 15, 'duration_profile': FAST15_PROFILE}, 'Bearer key1')
        self.assertEqual(status, 200, task)
        self.assertEqual((task.get('duration'), task.get('duration_profile')), (15, FAST15_PROFILE))
        row = self.fixture.store.get(task['id'])
        self.assertEqual((row['duration'], row['duration_profile']), (15, FAST15_PROFILE))
        status, fetched = await self.fixture.request('/v1/videos/' + task['id'], authorization='Bearer key1', method='GET')
        self.assertEqual(status, 200)
        self.assertEqual((fetched['duration'], fetched['duration_profile']), (15, FAST15_PROFILE))
        self.assertEqual(self.fixture.ns['_member_task'](row)['duration_profile'], FAST15_PROFILE)

    async def test_admin_profile_persists_and_invalid_profile_model_or_target_is_rejected_before_scheduling(self):
        status, task = await self.fixture.request('/api/admin/generate',
            {'prompt': 'test', 'model': 'seedance-2.0', 'duration': 15, 'duration_profile': FAST15_PROFILE})
        self.assertEqual(status, 200, task)
        self.assertEqual(self.fixture.store.get(task['id'])['duration_profile'], FAST15_PROFILE)
        count = len(self.fixture.runs)
        for path in ('/api/admin/generate', '/v1/videos/generations'):
            for fields in ({'duration_profile': ''}, {'duration_profile': 'unknown'},
                           {'model': 'seedance-2.5'}, {'model': 'seedance-1.0'}, {'duration': 30}):
                payload = {'prompt': 'test', 'model': 'seedance-2.0', 'duration': 15,
                           'duration_profile': FAST15_PROFILE, **fields}
                code, data = await self.fixture.request(path, payload, 'Bearer key1')
                self.assertEqual(code, 422, (path, payload, data))
        self.assertEqual(len(self.fixture.runs), count)

    async def test_profile_cannot_borrow_thirty_second_permission_for_a_fifteen_second_target(self):
        original_auth = self.fixture.ns['_auth']
        self.fixture.ns['_auth'] = lambda auth: {**original_auth(auth), 'allowed_durations': [30]}
        status, _ = await self.fixture.request('/v1/videos/generations',
            {'prompt': 'test', 'model': 'seedance-2.0', 'duration': 15, 'duration_profile': FAST15_PROFILE}, 'Bearer key1')
        self.assertEqual(status, 422)
        self.assertFalse(self.fixture.runs)

    async def test_idempotency_keeps_old_fingerprint_and_separates_profile_tasks(self):
        req_type = self.fixture.ns['VideoGenRequest']
        common = dict(prompt='test', model='seedance-2.0', duration=15, idempotency_key='profile-key')
        old = req_type(**common)
        identity = self.fixture.ns['_submission_identity'](old, 'scope')
        body = dict(model=old.model, prompt=old.prompt, ratio=None, duration=15, reference_images=[], reference_uploads=[])
        expected = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self.assertEqual(identity['request_fingerprint'], expected)
        profiled = self.fixture.ns['_submission_identity'](req_type(**common, duration_profile=FAST15_PROFILE), 'scope')
        self.assertNotEqual(identity['request_fingerprint'], profiled['request_fingerprint'])
        status, first = await self.fixture.request('/v1/videos/generations', common, 'Bearer key1')
        self.assertEqual(status, 200)
        status, conflict = await self.fixture.request('/v1/videos/generations',
            {**common, 'duration_profile': FAST15_PROFILE}, 'Bearer key1')
        self.assertEqual(status, 409, conflict)
        status, invalid = await self.fixture.request('/v1/videos/generations',
            {**common, 'duration_profile': ''}, 'Bearer key1')
        self.assertEqual(status, 422, invalid)
        status, replay = await self.fixture.request('/v1/videos/generations', common, 'Bearer key1')
        self.assertEqual(status, 200)
        self.assertEqual(replay['id'], first['id'])
        self.assertTrue(replay['replayed'])

    async def test_legacy_no_profile_replay_keeps_its_existing_permission_change_behavior(self):
        common = dict(prompt='test', model='seedance-2.0', duration=15, idempotency_key='legacy-key')
        status, original = await self.fixture.request('/v1/videos/generations', common, 'Bearer key1')
        self.assertEqual(status, 200)
        old_auth = self.fixture.ns['_auth']
        self.fixture.ns['_auth'] = lambda auth: {**old_auth(auth), 'allowed_durations': [30]}
        status, replay = await self.fixture.request('/v1/videos/generations', common, 'Bearer key1')
        self.assertEqual(status, 200, replay)
        self.assertEqual(replay['id'], original['id'])
        self.assertTrue(replay['replayed'])


class DurationProfileRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_task_account_retry import TaskAccountRetryTests
        self.fixture = TaskAccountRetryTests()
        await self.fixture.asyncSetUp()
        self.fixture.ns['submission_duration'] = submission_duration

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_dispatch_uses_wire_thirty_but_saved_target_and_final_verification_stay_fifteen(self):
        from test_video_metadata import mp4
        fixture = self.fixture
        fixture.store.update('task', model='seedance-2.0', duration=15, duration_profile=FAST15_PROFILE)
        calls = []
        async def generate(prompt, ratio, duration, model, **callbacks):
            calls.append((model, duration))
            self.assertEqual(fixture.store.get('task')['duration'], 15)
            path = Path(fixture.root.name) / 'profile-result.mp4'
            path.write_bytes(mp4(15.105))
            return {'local_path': str(path), 'account': 'a',
                    'download_source': 'dola_video_model', 'watermark': 'verified_download'}
        fixture.ns['pool'] = __import__('types').SimpleNamespace(generate_video=generate)
        await fixture.ns['_run_task']('task', 'seedance-2.0', '原文', None, 15, [], {})
        self.assertEqual(calls, [('seedance-2.0', 30)])
        row = fixture.store.get('task')
        self.assertEqual((row['status'], row['duration'], row['duration_profile']), ('completed', 15, FAST15_PROFILE))


class DurationProfileRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_task_evidence_api import TaskEvidenceAPITests
        self.fixture = TaskEvidenceAPITests()
        await self.fixture.asyncSetUp()
        self.fixture.ns['submission_duration'] = submission_duration
        self.fixture.store.update('video_fixture', model='seedance-2.0', duration=15, duration_profile=FAST15_PROFILE)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def test_original_query_filters_the_actual_thirty_second_prompt_and_collection_uses_it(self):
        fixture = self.fixture
        row = fixture.store.get('video_fixture')
        expected = build_video_prompt(row['prompt'], 30, row['model'])
        fixture.poll = {'assistant_texts': ['Generated video: ' + expected], 'videos': []}
        _, outcome = await fixture.ns['_query_original_task'](row)
        self.assertEqual(outcome['texts'], [])
        fixture.video_duration = 15.105
        await fixture.ns['_collect_original_video'](row)
        self.assertEqual(fixture.resume_options[-1]['submitted_prompt'], expected)
        self.assertEqual(fixture.store.get(row['id'])['status'], 'completed')

    async def test_profile_alias_whitespace_rebuilds_the_canonical_fast_thirty_prefix(self):
        fixture = self.fixture
        fixture.store.update('video_fixture', model=' seedance_v2.0 ')
        row = fixture.store.get('video_fixture')
        expected = build_video_prompt(row['prompt'], 30, 'seedance_v2.0')
        fixture.poll = {'assistant_texts': ['Generated video: ' + expected], 'videos': []}
        _, outcome = await fixture.ns['_query_original_task'](row)
        self.assertEqual(outcome['texts'], [])
        fixture.video_duration = 15.105
        await fixture.ns['_collect_original_video'](row)
        self.assertEqual(fixture.resume_options[-1]['submitted_prompt'], expected)

    async def test_restart_resume_uses_saved_profile_prompt_and_long_default_deadline(self):
        fixture = self.fixture
        fixture.store.update('video_fixture', status='processing', deadline_at=None, accepted_at=1)
        fixture.video_duration = 15.105
        row = fixture.store.get('video_fixture')
        await fixture.ns['_resume_original_task'](row)
        options = fixture.resume_options[-1]
        self.assertEqual(options['submitted_prompt'], build_video_prompt(row['prompt'], 30, row['model']))
        self.assertGreater(options['deadline_at'] - __import__('time').time(), 250)
        self.assertTrue(options['accepted'])
        self.assertEqual(fixture.store.get(row['id'])['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
