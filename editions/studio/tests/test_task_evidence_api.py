import ast
import asyncio
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from store import TaskStore
from task_results import complete_task, fail_task
from video_evidence import safe_text, matches_submitted_prompt
from test_video_metadata import mp4
import video_polling
from video_prompt import build_video_prompt
from video_capabilities import submission_duration
from video_outcome import conversation_outcome, VideoQueryTimeout
from account_errors import LoginExpiredError


class TaskEvidenceAPITests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_limit_reply_reconciles_processing_task_and_blocks_its_quota(self):
        from test_daily_video_limit import REPLY
        blocks = []
        self.ns['pool'].quota_ledger = SimpleNamespace(block=lambda *args: blocks.append(args))
        self.store.update('video_fixture', model='seedance-2.0', duration=10,
                          status='processing', phase='awaiting_receipt', failure_code=None)
        self.poll = {'assistant_messages': [[REPLY]], 'videos': []}
        result = await self.request('/api/admin/tasks/video_fixture/conversation')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['state'], 'failed')
        row = self.store.get('video_fixture')
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['phase'], 'platform_daily_limit')
        self.assertEqual(row['dola_reply'], REPLY)
        self.assertEqual(row['conversation_id'], '123')
        self.assertIsNone(row['accepted_at'])
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0][:2], ('original', 'ordinary_fast10'))
        self.assertFalse(self.resumes)

    async def asyncSetUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.store = TaskStore(':memory:')
        self.store.create('video_fixture', 'seedance-2.5', '用户原文', '9:16', 30)
        self.store.update('video_fixture', status='failed', account='original', conversation_id='123',
                          phase='request_mismatch', failure_code='request_mismatch', deadline_at=time.time()+300)
        self.polls = []
        self.resumes = []
        self.resume_options = []
        self.resume_gate = None
        self.poll_error = None
        self.poll_gate = None
        self.active_polls = 0
        self.max_active_polls = 0
        self.expired_accounts = []
        self.reserved_accounts = []
        self.video_duration = 30
        self.poll = {'assistant_texts':['视频生成目前支持4到15秒。'], 'videos':[]}
        outer = self
        class Session:
            def __init__(self, account, conversation): outer.polls.append((account, conversation))
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def poll(self):
                if outer.poll_gate:
                    outer.active_polls += 1
                    outer.max_active_polls = max(outer.max_active_polls, outer.active_polls)
                    await outer.poll_gate.wait()
                    outer.active_polls -= 1
                if outer.poll_error:
                    raise outer.poll_error
                return outer.poll
        self.patch = patch.object(video_polling, 'HttpVideoSession', Session)
        self.patch.start()
        class Pool:
            def reserve_unconfirmed_submission(self, account, task_id):
                outer.fail('Read-only audit fixtures must not reserve or resubmit an unknown send')
            def mark_login_expired(self, account): outer.expired_accounts.append(account)
            def reserve_recoveries(self, rows):
                outer.reserved_accounts.extend(row['account'] for row in rows)
            async def generate_video(self, *args, **kwargs): outer.fail('A read/recovery endpoint sent a new generation')
            async def resume_video(self, account, conversation, timeout, **kwargs):
                outer.resumes.append((account, conversation))
                outer.resume_options.append(kwargs)
                if outer.resume_gate is not None:
                    await outer.resume_gate.wait()
                path = Path(outer.root.name) / 'fixture.mp4'
                path.write_bytes(mp4(outer.video_duration))
                return {'local_path':str(path),'account':account,
                        'download_source':'dola_video_model','watermark':'verified_download'}
        def auth(key, bearer):
            if key != 'fixture-admin': raise HTTPException(401, 'unauthorized')
        self.app = FastAPI()
        self.app.mount('/videos', StaticFiles(directory=self.root.name))
        names = {'admin_task_detail','_query_original_task','admin_task_conversation','admin_task_download',
                 '_reconcile_original_outcome', '_complete_existing_video', '_collect_original_video',
                 '_resume_task', '_resume_original_task', '_recover_audit_task', '_recover_audit_tasks',
                 'resume_incomplete_tasks'}
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        nodes = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        for node in nodes:
            if node.name == 'resume_incomplete_tasks':
                node.decorator_list = []
        self.ns = dict(app=self.app, Header=Header, HTTPException=HTTPException, store=self.store,
            pool=Pool(), _admin_auth=auth, _result_checks={}, asyncio=asyncio, time=time,
            safe_text=safe_text, matches_submitted_prompt=matches_submitted_prompt, build_video_prompt=build_video_prompt,
            submission_duration=submission_duration,
            complete_task=complete_task, fail_task=fail_task, Path=Path,
            conversation_outcome=conversation_outcome, VideoQueryTimeout=VideoQueryTimeout,
            key_limiter=SimpleNamespace(acquire=self.acquire_key, release=self.release_key),
            _watchdog=self.noop, _run_task=self.forbid_submission,
            config=SimpleNamespace(PUBLIC_BASE='http://local', DOWNLOAD_DIR=self.root.name, ISP_ONLY=False,
                                   LONG_VIDEO_TIMEOUT=300, VIDEO_TIMEOUT=60))
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), 'task-evidence-api', 'exec'), self.ns)

    async def acquire_key(self, *args):
        pass

    async def release_key(self, *args):
        pass

    async def noop(self):
        pass

    async def forbid_submission(self, *args):
        self.fail('Audit recovery submitted a new generation')

    async def asyncTearDown(self):
        if self.resume_gate is not None:
            self.resume_gate.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        self.patch.stop()
        self.store._conn.close()
        self.root.cleanup()

    async def request(self, path, method='GET', auth=True, data=None):
        messages = []
        async def receive():
            return {'type':'http.request', 'body':json.dumps(data).encode() if data else b'', 'more_body':False}
        async def send(message): messages.append(message)
        await self.app(dict(type='http',asgi={'version':'3.0','spec_version':'2.4'},http_version='1.1',
            method=method,scheme='http',path=path,raw_path=path.encode(),query_string=b'',root_path='',
            headers=([(b'x-admin-key',b'fixture-admin')] if auth else [])+[(b'content-type',b'application/json')],
            client=('127.0.0.1',1),server=('local',80)),receive,send)
        body=b''.join(item.get('body',b'') for item in messages)
        return SimpleNamespace(status_code=next(item['status'] for item in messages if item['type']=='http.response.start'),
            content=body,text=body.decode('utf-8',errors='replace'),json=lambda:json.loads(body))

    async def test_auth_is_required_before_accessing_task_content_or_polling(self):
        for suffix in ('', '/conversation', '/download'):
            result = await self.request('/api/admin/tasks/video_fixture'+suffix,
                                        'POST' if suffix=='/download' else 'GET', auth=False)
            self.assertEqual(result.status_code, 401)
        self.assertEqual(self.polls, [])

    async def test_expert_query_resumes_original_after_network_failure_without_normal_worker(self):
        self.resume_gate=asyncio.Event()
        self.store.update('video_fixture',model='seedance-2.0',duration=15,duration_profile='pro-fast15',
                          status='failed',phase='failed',failure_code='failed',deadline_at=time.time()+60)
        self.poll['expert']={'accepted':True,'videos':[],'reply':'原生视频工具正在运行'}
        result=await self.request('/api/admin/tasks/video_fixture/conversation')
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(result.json()['state'],'accepted')
        self.assertEqual(self.resumes,[('original','123')])
        self.assertEqual(self.resume_options[0]['entry_mode'],'pro-fast15')
        self.assertEqual(self.store.get('video_fixture')['status'],'processing')

    async def test_query_returns_full_safe_reply_without_claiming_generation_or_leaking_media(self):
        self.poll['assistant_texts'] = ['说明。'*50+'视频生成目前支持4到15秒，我可以按15秒生成。 https://cdn.test/a?sign=private']
        result = await self.request('/api/admin/tasks/video_fixture/conversation')
        self.assertEqual(result.status_code, 200)
        self.assertIn('按15秒生成', result.json()['reply'])
        self.assertNotIn('private', result.text)
        self.assertEqual(result.json()['video_count'], 0)
        self.assertEqual(self.store.get('video_fixture')['status'], 'failed')
        self.assertEqual(self.resumes, [])

    async def test_query_excludes_native_label_prompt_echo_from_dola_reply(self):
        prompt = build_video_prompt('用户原文', 30, 'seedance-2.5')
        self.poll['assistant_texts'] = ['Generiertes Video: ' + prompt, '真实平台回复']
        result = await self.request('/api/admin/tasks/video_fixture/conversation')
        self.assertEqual(result.json()['reply'], '真实平台回复')
        self.assertEqual(self.resumes, [])

    async def test_query_excludes_native_ratio_prompt_echo_from_dola_reply(self):
        prompt = build_video_prompt('用户原文', 30, 'seedance-2.5')
        self.poll['assistant_texts'] = ['Generiertes Video: ' + prompt + ', 9:16', '真实平台回复']
        result = await self.request('/api/admin/tasks/video_fixture/conversation')
        self.assertEqual(result.json()['reply'], '真实平台回复')
        self.assertEqual(self.resumes, [])

    async def test_retry_only_collects_original_result_and_checks_mp4_duration(self):
        url = '/api/admin/tasks/video_fixture/download'
        result = await self.request(url, 'POST')
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.resumes, [])
        self.poll['videos'] = ['https://cdn.test/result.mp4?sign=private']
        result = await self.request(url, 'POST')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.resumes, [('original','123')])
        self.assertEqual(result.json()['task']['actual_duration'], 30)
        self.assertEqual(result.json()['task']['phase'], 'completed')
        self.assertNotIn('private', result.text)
        video = await self.request('/videos/fixture.mp4')
        self.assertEqual(video.content, mp4())

    async def test_late_copyright_overrides_prior_receipt_and_preserves_request_audit(self):
        audit = {'matches': False, 'prompt_matches': False, 'model': 'seedance_v2.5', 'duration': 30}
        self.store.record_evidence('video_fixture', 'submitted', audit)
        self.store.update('video_fixture', status='processing', phase='polling', accepted_at=time.time()-60)
        self.poll['assistant_texts'] = [
            'Das Video wird mit Dreamina Seedance 2.5 generiert. Dafür werden 2 Credits verwendet.',
            'Zum Schutz des Urheberrechts kann ich dir das generierte Video nicht anzeigen.']
        result = await self.request('/api/admin/tasks/video_fixture/conversation')
        self.assertEqual(result.status_code, 200)
        row = self.store.get('video_fixture')
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['failure_code'], 'platform_copyright')
        self.assertEqual(json.loads(row['request_evidence']), audit)
        self.assertEqual(self.resumes, [])

    async def test_query_of_audit_mismatch_with_video_collects_and_corrects_false_failure(self):
        self.poll['videos'] = ['https://cdn.test/result.mp4?sign=private']
        self.poll['assistant_texts'] = ['Dein Video ist fertig.']
        result = await self.request('/api/admin/tasks/video_fixture/conversation')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['video_count'], 1)
        row = self.store.get('video_fixture')
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['actual_duration'], 30)
        self.assertEqual(self.resumes, [('original', '123')])

    async def test_audit_mismatch_restores_only_on_confirmed_receipt_before_deadline(self):
        self.resume_gate = asyncio.Event()
        self.poll['assistant_texts'] = ['Das Video wird mit Dreamina Seedance 2.5 generiert. Dafür werden 2 Credits verwendet.']
        result = await self.request('/api/admin/tasks/video_fixture/conversation')
        self.assertEqual(result.status_code, 200)
        await asyncio.sleep(0)
        row = self.store.get('video_fixture')
        self.assertEqual(row['status'], 'processing')
        self.assertIsNotNone(row['accepted_at'])
        self.assertIsNone(row['failure_code'])
        self.assertEqual(self.resumes, [('original', '123')])
        self.assertEqual(self.reserved_accounts, ['original'])
        self.assertTrue(self.resume_options[0].get('accepted'))

    async def test_manual_fast_receipt_restores_false_not_accepted_without_resubmitting_or_extending_deadline(self):
        self.store.update('video_fixture', model='seedance-2.0', duration=10,
                          phase='not_accepted', failure_code='not_accepted', finished_at=time.time())
        original = self.store.get('video_fixture')
        self.resume_gate = asyncio.Event()
        self.poll['assistant_texts'] = [
            'Das Video wird mithilfe des Seedance 2.0 Modells generiert. Dafür werden 2 Punkte verwendet. '
            'Es ist in 5 Minuten fertig. Ich informiere dich, wenn es fertig ist.']
        result = await self.request('/api/admin/tasks/video_fixture/conversation')
        self.assertEqual(result.status_code, 200)
        await asyncio.sleep(0)
        row = self.store.get('video_fixture')
        self.assertEqual(row['status'], 'processing')
        self.assertIsNone(row['failure_code'])
        self.assertIsNone(row['finished_at'])
        self.assertIsNotNone(row['accepted_at'])
        for field in ('id', 'account', 'conversation_id', 'prompt', 'model', 'duration', 'deadline_at'):
            self.assertEqual(row[field], original[field], field)
        self.assertEqual(self.resumes, [('original', '123')])
        self.assertEqual(self.resume_options[0]['deadline_at'], original['deadline_at'])
        self.assertTrue(self.resume_options[0]['accepted'])
        self.assertEqual(len(self.store.recent_tasks()), 1)
        repeated = await self.request('/api/admin/tasks/video_fixture/conversation')
        self.assertEqual(repeated.status_code, 409)
        self.assertEqual(len(self.resumes), 1)

    async def test_manual_receipt_for_false_not_accepted_respects_expired_deadline(self):
        deadline = time.time() - 1
        self.store.update('video_fixture', failure_code='not_accepted', deadline_at=deadline)
        self.poll['assistant_texts'] = [
            'Das Video wird mit Dreamina Seedance 2.5 generiert. Dafür werden 2 Credits verwendet.']
        await self.request('/api/admin/tasks/video_fixture/conversation')
        row = self.store.get('video_fixture')
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['failure_code'], 'query_timeout')
        self.assertEqual(row['deadline_at'], deadline)
        self.assertEqual(self.resumes, [])

    async def test_manual_query_cannot_restore_not_accepted_without_receipt_or_with_platform_refusal(self):
        for reply, failure_code in (
            ('视频生成目前支持4到15秒。', 'not_accepted'),
            ('Please confirm whether to generate a 15-second video.', 'not_accepted'),
            ('Zum Schutz des Urheberrechts kann ich dir das generierte Video nicht anzeigen.', 'platform_copyright'),
        ):
            with self.subTest(reply=reply):
                self.store.update('video_fixture', status='failed', phase='not_accepted',
                                  failure_code='not_accepted', accepted_at=None)
                self.poll['assistant_texts'] = [reply]
                await self.request('/api/admin/tasks/video_fixture/conversation')
                row = self.store.get('video_fixture')
                self.assertEqual(row['status'], 'failed')
                self.assertEqual(row['failure_code'], failure_code)
                self.assertIsNone(row['accepted_at'])
                self.assertEqual(self.resumes, [])

    async def test_manual_query_collects_existing_video_for_false_not_accepted(self):
        self.store.update('video_fixture', model='seedance-2.0', duration=10,
                          phase='not_accepted', failure_code='not_accepted')
        self.video_duration = 10
        self.poll['videos'] = ['https://cdn.test/result.mp4?sign=private']
        result = await self.request('/api/admin/tasks/video_fixture/conversation')
        self.assertEqual(result.status_code, 200)
        row = self.store.get('video_fixture')
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['actual_duration'], 10)
        self.assertEqual(self.resumes, [('original', '123')])
        self.assertNotIn('private', result.text)

    async def test_startup_does_not_query_or_restore_old_not_accepted_tasks(self):
        self.store.update('video_fixture', phase='not_accepted', failure_code='not_accepted')
        self.poll['assistant_texts'] = [
            'Das Video wird mit Dreamina Seedance 2.5 generiert. Dafür werden 2 Credits verwendet.']
        await self.ns['resume_incomplete_tasks']()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(self.polls, [])
        self.assertEqual(self.resumes, [])
        self.assertEqual(self.store.get('video_fixture')['status'], 'failed')

    async def test_receipt_does_not_restore_other_failure_or_expired_audit(self):
        self.poll['assistant_texts'] = ['Das Video wird mit Dreamina Seedance 2.5 generiert. Dafür werden 2 Credits verwendet.']
        self.store.update('video_fixture', failure_code='platform_copyright')
        await self.request('/api/admin/tasks/video_fixture/conversation')
        self.assertEqual(self.store.get('video_fixture')['failure_code'], 'platform_copyright')
        self.store.update('video_fixture', phase='request_mismatch', failure_code='request_mismatch', deadline_at=time.time()-1)
        await self.request('/api/admin/tasks/video_fixture/conversation')
        self.assertEqual(self.store.get('video_fixture')['status'], 'failed')
        self.assertEqual(self.store.get('video_fixture')['failure_code'], 'query_timeout')
        self.assertEqual(self.resumes, [])

    async def test_repeated_download_reuses_validated_local_video_without_query_or_new_file(self):
        self.poll['videos'] = ['https://cdn.test/result.mp4?sign=private']
        first = await self.request('/api/admin/tasks/video_fixture/download', 'POST')
        self.assertEqual(first.status_code, 200)
        first_events = self.store.task_events('video_fixture')
        second = await self.request('/api/admin/tasks/video_fixture/download', 'POST')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(self.polls), 1)
        self.assertEqual(len(self.resumes), 1)
        self.assertEqual(first.json()['task']['video_url'], second.json()['task']['video_url'])
        self.assertEqual(second.json()['task']['actual_duration'], 30)
        self.assertEqual(self.store.task_events('video_fixture'), first_events)
        self.assertTrue(any(event['phase'] == 'download_metadata' for event in first_events))

    async def test_concurrent_download_is_exclusive_and_never_generates(self):
        self.poll['videos'] = ['https://cdn.test/result.mp4?sign=private']
        self.resume_gate = asyncio.Event()
        first = asyncio.create_task(self.request('/api/admin/tasks/video_fixture/download', 'POST'))
        await asyncio.sleep(0)
        second = await self.request('/api/admin/tasks/video_fixture/download', 'POST')
        self.assertEqual(second.status_code, 409)
        self.resume_gate.set()
        self.assertEqual((await first).status_code, 200)
        self.assertEqual(len(self.resumes), 1)

    async def test_startup_audit_recovery_collects_original_video_even_after_deadline(self):
        self.store.update('video_fixture', deadline_at=time.time()-60)
        self.poll['videos'] = ['https://cdn.test/result.mp4?sign=private']
        recover = self.ns.get('_recover_audit_task')
        self.assertIsNotNone(recover, 'Startup must recheck saved request-mismatch conversations')
        await recover(self.store.get('video_fixture'))
        self.assertEqual(self.store.get('video_fixture')['status'], 'completed')
        self.assertEqual(self.resumes, [('original', '123')])

    async def test_startup_audit_recovery_without_receipt_never_claims_accepted(self):
        recover = self.ns.get('_recover_audit_task')
        self.assertIsNotNone(recover, 'Startup must recheck saved request-mismatch conversations')
        await recover(self.store.get('video_fixture'))
        self.assertEqual(self.store.get('video_fixture')['status'], 'failed')
        self.assertIsNone(self.store.get('video_fixture')['accepted_at'])
        self.assertEqual(self.resumes, [])

    async def test_real_startup_rechecks_saved_audit_failures_without_queuing_new_generation(self):
        self.poll['videos'] = ['https://cdn.test/result.mp4?sign=private']
        startup = self.ns.get('resume_incomplete_tasks')
        self.assertIsNotNone(startup)
        await startup()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(self.store.get('video_fixture')['status'], 'completed')
        self.assertEqual(self.resumes, [('original', '123')])

    async def test_startup_audit_pass_limits_queries_instead_of_opening_all_accounts_at_once(self):
        self.store.create('second', 'seedance-2.5', '原文', '16:9', 30)
        self.store.update('second', status='failed', failure_code='request_mismatch',
                          account='second_account', conversation_id='456', deadline_at=time.time()+300)
        self.poll_gate = asyncio.Event()
        await self.ns['resume_incomplete_tasks']()
        await asyncio.sleep(0)
        self.assertEqual(self.max_active_polls, 1)
        self.poll_gate.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(len(self.polls), 2)

    async def test_login_expiry_returns_conflict_and_marks_only_original_account(self):
        self.poll_error = LoginExpiredError('登录已失效，请重新登录')
        result = await self.request('/api/admin/tasks/video_fixture/conversation')
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.expired_accounts, ['original'])
        self.assertEqual(self.resumes, [])

    async def test_network_error_does_not_mark_account_invalid_or_generation_rejected(self):
        self.poll_error = TimeoutError('network timeout')
        result = await self.request('/api/admin/tasks/video_fixture/conversation')
        self.assertEqual(result.status_code, 502)
        self.assertEqual(self.expired_accounts, [])
        self.assertEqual(self.store.get('video_fixture')['failure_code'], 'request_mismatch')


class AuditRecoverySelectionTests(unittest.TestCase):
    def test_only_bounded_failed_request_mismatch_with_original_account_and_conversation_is_selected(self):
        store = TaskStore(':memory:')
        self.addCleanup(store._conn.close)
        for task_id in ('eligible_old', 'eligible_new', 'copyright', 'no_account', 'no_conversation', 'processing'):
            store.create(task_id, 'seedance-2.5', '原文', '16:9', 30)
            store.update(task_id, status='failed', failure_code='request_mismatch', account='original', conversation_id='123')
        store.update('copyright', failure_code='platform_copyright')
        store.update('eligible_old', created_at=1)
        store.update('eligible_new', created_at=2)
        store.update('no_account', account=None)
        store.update('no_conversation', conversation_id=None)
        store.update('processing', status='processing')
        select = getattr(store, 'audit_mismatch_tasks', None)
        self.assertIsNotNone(select, 'Only local submission audit failures are recovery candidates')
        self.assertEqual([r['id'] for r in select(1)], ['eligible_new'])
        self.assertEqual({r['id'] for r in select(50)}, {'eligible_old', 'eligible_new'})
