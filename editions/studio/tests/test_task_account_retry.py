"""Exercise real task persistence while replacing external browser/video work."""
import ast
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from types import SimpleNamespace
import unittest

from store import TaskStore
from task_results import complete_task, fail_task
from test_video_metadata import mp4
from video_outcome import ExplicitNotAcceptedError, PlatformVideoError, VideoQueryTimeout
from video_prompt import build_video_prompt
from video_capabilities import submission_duration
from video_evidence import SubmissionUnconfirmedError
from reference_uploads import cleanup_task_images


class TaskAccountRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_expert_task_passes_entry_mode_and_fifteen_seconds_to_pool(self):
        self.store.update('task', model='seedance-2.0', duration=15, duration_profile='pro-fast15')
        calls=[]
        async def worker(*args, **kwargs):
            calls.append((args, kwargs))
            raise RuntimeError('fixture stops before submit')
        self.ns['pool']=SimpleNamespace(generate_video=worker)
        await self.ns['_run_task']('task','seedance-2.0','original','16:9',15,[],{})
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][0], ('original','16:9',15,'seedance-2.0'))
        self.assertEqual(calls[0][1]['entry_mode'], 'pro-fast15')

    async def asyncSetUp(self):
        self.store = TaskStore(':memory:')
        self.store.create('task', 'seedance-2.5', '保持原文\n尾部空格 ', '16:9', 30)
        self.store.create('other', 'seedance-2.5', '其他任务', '9:16', 30)
        self.store.update('other', status='processing', account='other_account', conversation_id='999')
        self.other_before = self.store.get('other')
        self.root = tempfile.TemporaryDirectory()
        self.released = []
        async def acquire(*args): pass
        async def release(key): self.released.append(key)
        async def references(*args): return None, []
        node = next(node for node in ast.parse(Path('server.py').read_text(encoding='utf-8')).body
                    if isinstance(node, ast.AsyncFunctionDef) and node.name == '_run_task')
        self.ns = dict(store=self.store, asyncio=asyncio, time=time, shutil=shutil,
            submission_duration=submission_duration,
            key_limiter=SimpleNamespace(acquire=acquire, release=release),
            download_reference_images=references, complete_task=complete_task, fail_task=fail_task,
            config=SimpleNamespace(PUBLIC_BASE='http://local', REFERENCE_UPLOAD_DIR=self.root.name,
                                   LONG_VIDEO_TIMEOUT=7200, VIDEO_TIMEOUT=7200),
            AllAccountsLimitedError=type('AllAccountsLimitedError', (RuntimeError,), {}),
            AllAccountsQuotaBlockedError=type('AllAccountsQuotaBlockedError', (RuntimeError,), {}),
            AllAccountsProxyBlockedError=type('AllAccountsProxyBlockedError', (RuntimeError,), {}),
            ExplicitNotAcceptedError=ExplicitNotAcceptedError,
            SubmissionUnconfirmedError=SubmissionUnconfirmedError)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                     'server-task-retry', 'exec'), self.ns)

    def recovery_functions(self):
        names = {'_resume_original_task', '_task_client', '_task_reference_images'}
        nodes = [node for node in ast.parse(Path('server.py').read_text(encoding='utf-8')).body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
        self.ns.update(json=json, build_video_prompt=build_video_prompt,
                       SUPPORTED_DURATIONS=(5, 10, 15, 30), cleanup_task_images=cleanup_task_images)
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     'server-resume-retry', 'exec'), self.ns)

    async def asyncTearDown(self):
        self.store._conn.close()
        self.root.cleanup()

    async def run_task(self, worker):
        self.ns['pool'] = SimpleNamespace(generate_video=worker)
        await self.ns['_run_task']('task', 'seedance-2.5', '保持原文\n尾部空格 ', '16:9', 30, [], {})

    def rejected_attempt(self, callbacks):
        callbacks['on_prompt_sent']('old_account')
        callbacks['on_conversation_id']('old_account', '111', 100)
        callbacks['on_event']('submitted', {'matches': False, 'audit_status': 'mismatch'})
        callbacks['on_event']('awaiting_receipt', {'reply': '请确认5秒还是4到15秒，确认后生成。'})
        callbacks['on_event']('account_retry', {
            'account': 'old_account', 'conversation_id': '111',
            'reply': '请确认5秒还是4到15秒，确认后生成。', 'reason': '平台明确尚未开始生成'})

    async def test_retry_clears_old_attempt_and_recovery_queues_only_the_same_task(self):
        checkpoint = asyncio.Event()
        async def worker(prompt, ratio, duration, model, **callbacks):
            self.assertEqual((prompt, ratio, duration, model),
                             ('保持原文\n尾部空格 ', '16:9', 30, 'seedance-2.5'))
            self.rejected_attempt(callbacks)
            checkpoint.set()
            await asyncio.Future()
        running = asyncio.create_task(self.run_task(worker))
        try:
            await asyncio.wait_for(checkpoint.wait(), 2)
            row = self.store.get('task')
            self.assertEqual(row['status'], 'queued')
            self.assertEqual(row['phase'], 'account_retry')
            for field in ('account', 'conversation_id', 'deadline_at', 'accepted_at',
                          'request_evidence', 'dola_reply', 'actual_duration', 'error', 'failure_code'):
                self.assertIsNone(row[field], field)
            self.assertEqual([r['id'] for r in self.store.recoverable_queued_tasks()], ['task'])
            self.assertEqual([r['id'] for r in self.store.recoverable_tasks()], ['other'])
            retry = next(e for e in self.store.task_events('task') if e['phase'] == 'account_retry')
            self.assertEqual(retry['details']['account'], 'old_account')
            self.assertEqual(retry['details']['conversation_id'], '111')
            self.assertIn('请确认', retry['details']['reply'])
            self.assertEqual(self.store.get('other'), self.other_before)
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def test_second_attempt_owns_current_audit_reply_and_final_conversation(self):
        async def worker(prompt, ratio, duration, model, **callbacks):
            self.rejected_attempt(callbacks)
            callbacks['on_prompt_sent']('new_account')
            self.assertIsNone(self.store.get('task')['conversation_id'])
            self.assertIsNone(self.store.get('task')['dola_reply'])
            callbacks['on_conversation_id']('new_account', '222', time.time()+7200)
            callbacks['on_event']('submitted', {'matches': True, 'audit_status': 'passed'})
            callbacks['on_event']('accepted', {'reply': 'Seedance 2.5，消耗2个额度。'})
            path = Path(self.root.name) / 'result.mp4'
            path.write_bytes(mp4(30.08))
            return {'account': 'new_account', 'conversation_id': '222', 'local_path': str(path),
                    'download_source': 'dola_video_model', 'watermark': 'verified_download'}
        await self.run_task(worker)
        row = self.store.get('task')
        self.assertEqual(row['status'], 'completed')
        self.assertEqual((row['account'], row['conversation_id']), ('new_account', '222'))
        self.assertEqual(json.loads(row['request_evidence'])['audit_status'], 'passed')
        self.assertEqual(row['dola_reply'], 'Seedance 2.5，消耗2个额度。')
        self.assertAlmostEqual(row['actual_duration'], 30.08)
        self.assertIsNotNone(row['accepted_at'])
        self.assertEqual(self.store.pending_task_count(), 1)
        self.assertEqual(self.store.get('other'), self.other_before)
        self.assertEqual(self.released, [None])

    async def test_interrupted_new_send_is_requeued_once_with_both_previous_accounts_excluded(self):
        checkpoint = asyncio.Event()
        async def worker(*args, **callbacks):
            self.rejected_attempt(callbacks)
            callbacks['on_prompt_sent']('new_account')
            checkpoint.set()
            await asyncio.Future()
        running = asyncio.create_task(self.run_task(worker))
        try:
            await asyncio.wait_for(checkpoint.wait(), 2)
            reserved = []
            self.store.fail_unconfirmed_submissions(reserve=lambda account, task: reserved.append((task, account)))
            row = self.store.get('task')
            self.assertEqual(row['status'], 'queued')
            self.assertEqual(row['phase'], 'account_retry')
            self.assertIsNone(row['account'])
            self.assertIsNone(row['conversation_id'])
            self.assertEqual([r['id'] for r in self.store.recoverable_queued_tasks()], ['task'])
            self.assertNotIn('task', [r['id'] for r in self.store.recoverable_tasks()])
            self.assertEqual({e['details']['account'] for e in self.store.task_events('task') if e['phase'] == 'account_retry'},
                             {'old_account', 'new_account'})
            self.assertEqual(reserved, [('task', 'new_account')])
            self.store.fail_unconfirmed_submissions(reserve=lambda account, task: reserved.append((task, account)))
            self.assertEqual(reserved, [('task', 'new_account')])
            self.assertEqual(self.store.get('other'), self.other_before)
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)

    async def test_restart_reservation_failure_preserves_original_send_and_retry_history(self):
        self.store.update('task', status='processing', account='old_account')
        before = self.store.get('task')
        def unavailable(account, task):
            raise RuntimeError('quota database unavailable')
        with self.assertRaisesRegex(RuntimeError, 'quota database unavailable'):
            self.store.fail_unconfirmed_submissions(reserve=unavailable)
        self.assertEqual(self.store.get('task'), before)
        self.assertEqual(self.store.task_events('task'), [])

    async def test_retrying_startup_after_task_database_failure_does_not_double_reserve_unknown_send(self):
        from test_video_pipeline import pool_module
        pool = pool_module.BrowserPool(self.root.name, ':memory:')
        self.store.update('task', status='processing', account='unknown_account',
                          reference_images='["upload:task/reference.png"]')
        self.store._conn.execute("CREATE TRIGGER reject_restart BEFORE UPDATE OF status ON tasks "
                                 "WHEN NEW.status='queued' BEGIN SELECT RAISE(ABORT, 'fixture crash'); END")
        try:
            with self.assertRaisesRegex(Exception, 'fixture crash'):
                self.store.fail_unconfirmed_submissions(reserve=pool.reserve_unconfirmed_submission)
            self.assertEqual(pool.used_today('unknown_account'), 1)
            self.assertEqual(self.store.get('task')['status'], 'processing')
            self.assertEqual(self.store.task_events('task'), [])
            self.store._conn.execute('DROP TRIGGER reject_restart')
            self.store.fail_unconfirmed_submissions(reserve=pool.reserve_unconfirmed_submission)
            self.assertEqual(pool.used_today('unknown_account'), 1)
            self.assertEqual(self.store.get('task')['status'], 'queued')
            self.assertEqual(self.store.get('task')['reference_images'], '["upload:task/reference.png"]')
            self.assertEqual(len(self.store.task_events('task')), 1)
        finally:
            pool._conn.close()

    async def test_retry_queue_timeout_uses_new_wait_boundary_and_preserves_creation_time(self):
        self.store.update('task', status='queued', phase='account_retry', created_at=1, started_at=time.time())
        self.assertNotIn('task', [r['id'] for r in self.store.stalled_queued_tasks(time.time()-30)])
        self.assertEqual(self.store.get('task')['created_at'], 1)

    async def test_restart_excludes_accounts_already_proven_not_to_have_started(self):
        self.store.record_evidence('task', 'account_retry',
                                  {'account': 'old_account', 'conversation_id': '111', 'reply': '请确认时长'})
        async def worker(*args, **callbacks):
            self.assertEqual(set(callbacks.get('excluded_accounts', [])), {'old_account'})
            callbacks['on_prompt_sent']('new_account')
            callbacks['on_conversation_id']('new_account', '222', time.time()+7200)
            path = Path(self.root.name) / 'restarted.mp4'
            path.write_bytes(mp4(30))
            return {'account': 'new_account', 'conversation_id': '222', 'local_path': str(path),
                    'download_source': 'dola_video_model', 'watermark': 'verified_download'}
        await self.run_task(worker)
        self.assertEqual(self.store.get('task')['status'], 'completed')
        self.assertEqual(self.store.get('task')['account'], 'new_account')

    async def test_restart_excludes_browsers_opened_before_crash_and_keeps_attempt_history(self):
        for index in range(5):
            self.store.record_evidence('task', 'preparing', {'account': f'account_{index}', 'reason': f'第{index+1}/5个账号'})
            self.store.record_evidence('task', 'account_retry', {'account': f'account_{index}'})
        for index in range(105):
            self.store.record_evidence('task', 'awaiting_receipt', {'reply': f'等待{index}'})
        self.store.record_evidence('task', 'preparing', {'account': 'crashed_browser'})
        excluded = []
        async def worker(*args, **callbacks):
            excluded.extend(callbacks['excluded_accounts'])
            raise RuntimeError('fixture stops without opening a browser')
        await self.run_task(worker)
        self.assertEqual(set(excluded), {f'account_{i}' for i in range(5)} | {'crashed_browser'})
        history = self.store.task_events('task')
        self.assertEqual(len([e for e in history if e['phase'] == 'account_retry']), 5)
        self.assertEqual(len([e for e in history if e['phase'] == 'preparing']), 6)
        self.assertLessEqual(len([e for e in history if e['phase'] not in ('preparing', 'account_retry')]), 100)

    async def test_exhausted_retries_keep_last_original_conversation_available_without_requeue(self):
        async def worker(*args, **callbacks):
            error = ExplicitNotAcceptedError('请确认5秒还是4到15秒，确认后生成。')
            error.account = 'old_account'
            error.conversation_id = '111'
            raise error
        await self.run_task(worker)
        row = self.store.get('task')
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['phase'], 'not_accepted')
        self.assertEqual((row['account'], row['conversation_id']), ('old_account', '111'))
        self.assertIn('请确认', row['dola_reply'])
        self.assertIsNone(row['accepted_at'])
        self.assertEqual(self.store.recoverable_queued_tasks(), [])

    async def test_five_attempt_failure_keeps_last_original_conversation(self):
        async def worker(*args, **callbacks):
            self.rejected_attempt(callbacks)
            error = RuntimeError('已尝试5个不同账号，均未受理')
            error.attempts_exhausted = True
            error.account, error.conversation_id, error.reply = 'last_account', '555', '仍未取得回执'
            error.task_phase = 'failed'
            raise error
        await self.run_task(worker)
        row = self.store.get('task')
        self.assertEqual((row['status'], row['phase']), ('failed', 'failed'))
        self.assertEqual((row['account'], row['conversation_id'], row['dola_reply']),
                         ('last_account', '555', '仍未取得回执'))
        self.assertEqual(self.store.get('other'), self.other_before)

    async def test_restart_with_five_excluded_accounts_preserves_last_history(self):
        for index in range(5):
            self.store.record_evidence('task', 'account_retry', {
                'account': f'account_{index}', 'conversation_id': str(100 + index), 'reply': f'回执{index}'})
        async def worker(*args, **callbacks):
            self.assertEqual(set(callbacks['excluded_accounts']), {f'account_{i}' for i in range(5)})
            error = RuntimeError('已尝试5个不同账号，停止换号')
            error.attempts_exhausted = True
            error.account, error.conversation_id, error.reply = None, None, ''
            error.task_phase = 'failed'
            raise error
        await self.run_task(worker)
        row = self.store.get('task')
        self.assertEqual((row['status'], row['account'], row['conversation_id'], row['dola_reply']),
                         ('failed', 'account_4', '104', '回执4'))
        self.assertEqual(len([e for e in self.store.task_events('task') if e['phase'] == 'account_retry']), 5)
        self.assertEqual(self.store.recoverable_queued_tasks(), [])

    async def test_server_passes_task_id_for_idempotent_unknown_send_reservation(self):
        seen = []
        async def worker(*args, **callbacks):
            seen.append(callbacks.get('task_id'))
            raise RuntimeError('fixture ends before generation')
        await self.run_task(worker)
        self.assertEqual(seen, ['task'])

    async def test_last_attempt_without_conversation_does_not_inherit_previous_conversation(self):
        async def worker(*args, **callbacks):
            self.rejected_attempt(callbacks)
            error = RuntimeError('已尝试5个不同账号，停止换号')
            error.attempts_exhausted = True
            error.account, error.conversation_id, error.reply = 'last_account', '', ''
            raise error
        await self.run_task(worker)
        row = self.store.get('task')
        self.assertEqual(row['account'], 'last_account')
        self.assertFalse(row['conversation_id'])
        self.assertFalse(row['dola_reply'])

    async def test_retry_history_and_current_state_roll_back_together_when_sql_update_fails(self):
        self.store.update('task', status='processing', account='old_account', conversation_id='111')
        before = self.store.get('task')
        self.store._conn.execute("CREATE TRIGGER reject_retry BEFORE UPDATE OF account ON tasks "
                                "WHEN NEW.account IS NULL BEGIN SELECT RAISE(ABORT, 'fixture write failure'); END")
        with self.assertRaisesRegex(Exception, 'fixture write failure'):
            self.store.record_evidence('task', 'account_retry',
                                       {'account': 'old_account', 'conversation_id': '111'})
        self.assertEqual(self.store.get('task'), before)
        self.assertEqual(self.store.task_events('task'), [])

    async def test_recovered_confirmation_hands_off_after_key_and_old_claim_release_with_references_intact(self):
        await self.assert_recovered_handoff(ExplicitNotAcceptedError('请确认5秒还是4到15秒，确认后生成。'), True)

    async def test_recovered_unconfirmed_hands_off_without_releasing_old_claim_with_references_intact(self):
        await self.assert_recovered_handoff(SubmissionUnconfirmedError('未取得生成回执'), False)

    async def assert_recovered_handoff(self, error, releases_claim):
        self.recovery_functions()
        task_id = 'video_' + 'a' * 32
        reference = f'upload:{task_id}/image.png'
        self.store.create(task_id, 'seedance-2.5', '保持原文\n尾部空格 ', 'default', 30,
                          reference_images=json.dumps([reference]), api_key_hash='client', concurrency_limit=1)
        self.store.update(task_id, status='processing', account='old_account', conversation_id='111',
                          deadline_at=time.time()+7200)
        marker = Path(self.root.name) / 'tasks' / task_id / 'image.png'
        marker.parent.mkdir(parents=True)
        marker.write_bytes(b'private reference')
        events = []
        held = False
        resume_running = False
        async def acquire(*args):
            nonlocal held
            self.assertFalse(held, 'Release the recovered key permit before acquiring the next submission permit')
            held = True
            events.append('key_acquire')
        async def release(*args):
            nonlocal held
            held = False
            events.append('key_release')
        async def resume(account, conversation, timeout, **callbacks):
            nonlocal resume_running
            self.assertEqual((account, conversation), ('old_account', '111'))
            self.assertFalse(callbacks['accepted'])
            resume_running = True
            try:
                raise error
            finally:
                resume_running = False
                events.append('resume_unwound')
        def release_original(account, conversation):
            self.assertTrue(releases_claim, 'An unconfirmed old conversation may still consume its claimed credits')
            self.assertFalse(resume_running)
            self.assertEqual((account, conversation), ('old_account', '111'))
            events.append('claim_release')
        async def references(refs, sent_task):
            self.assertEqual((refs, sent_task), ([reference], task_id))
            self.assertTrue(marker.is_file(), 'Preserve uploaded references until the next attempt has used them')
            return None, [marker]
        async def generate(prompt, ratio, duration, model, **callbacks):
            events.append('new_generate')
            self.assertEqual((prompt, ratio, duration, model),
                             ('保持原文\n尾部空格 ', None, 30, 'seedance-2.5'))
            self.assertEqual(set(callbacks['excluded_accounts']), {'old_account'})
            self.assertEqual(callbacks['reference_image_paths'], [marker])
            callbacks['on_prompt_sent']('new_account')
            callbacks['on_conversation_id']('new_account', '222', time.time()+7200)
            callbacks['on_event']('accepted', {'reply': 'Seedance 2.5，消耗2个额度。'})
            path = Path(self.root.name) / 'recovered.mp4'
            path.write_bytes(mp4(30))
            return {'account': 'new_account', 'conversation_id': '222', 'local_path': str(path),
                    'download_source': 'dola_video_model', 'watermark': 'verified_download'}
        self.ns.update(key_limiter=SimpleNamespace(acquire=acquire, release=release),
                       download_reference_images=references,
                       pool=SimpleNamespace(resume_video=resume, generate_video=generate,
                                            release_unaccepted_conversation=release_original))
        await asyncio.wait_for(self.ns['_resume_original_task'](self.store.get(task_id)), 2)
        row = self.store.get(task_id)
        self.assertEqual((row['status'], row['account'], row['conversation_id']), ('completed', 'new_account', '222'))
        self.assertEqual(events, ['key_acquire', 'resume_unwound'] + (['claim_release'] if releases_claim else []) +
                                 ['key_release', 'key_acquire', 'new_generate', 'key_release'])
        self.assertFalse(marker.exists())
        self.assertEqual(self.store.get('other'), self.other_before)

    async def test_recovered_accepted_receipt_never_hands_off_even_if_confirmation_error_is_raised(self):
        self.recovery_functions()
        for receipt_during_poll in (False, True):
            with self.subTest(receipt_during_poll=receipt_during_poll):
                self.store.update('task', status='processing', account='old_account', conversation_id='111',
                                  accepted_at=None if receipt_during_poll else time.time())
                async def resume(*args, **callbacks):
                    if receipt_during_poll:
                        callbacks['on_event']('accepted', {'reply': 'Seedance 2.5，消耗2个额度。'})
                    raise ExplicitNotAcceptedError('请确认后生成。')
                async def generate(*args, **kwargs):
                    self.fail('An accepted conversation must not generate again')
                def release_original(*args):
                    self.fail('An accepted conversation must keep its daily claim')
                self.ns['pool'] = SimpleNamespace(resume_video=resume, generate_video=generate,
                                                  release_unaccepted_conversation=release_original)
                await self.ns['_resume_original_task'](self.store.get('task'))
                self.assertNotIn('account_retry', [event['phase'] for event in self.store.task_events('task')])
                self.assertEqual(self.store.get('task')['account'], 'old_account')

    async def test_recovered_unconfirmed_after_receipt_never_hands_off(self):
        self.recovery_functions()
        for receipt_during_poll in (False, True):
            with self.subTest(receipt_during_poll=receipt_during_poll):
                self.store.update('task', status='processing', account='old_account', conversation_id='111',
                                  accepted_at=None if receipt_during_poll else time.time())
                async def resume(*args, **callbacks):
                    if receipt_during_poll:
                        callbacks['on_event']('accepted', {'reply': 'Seedance 2.5，消耗2个额度。'})
                    raise SubmissionUnconfirmedError('未取得后续回执')
                async def generate(*args, **kwargs):
                    self.fail('An accepted conversation must not generate again')
                def release_original(*args):
                    self.fail('An accepted conversation must keep its daily claim')
                self.ns['pool'] = SimpleNamespace(resume_video=resume, generate_video=generate,
                                                  release_unaccepted_conversation=release_original)
                await self.ns['_resume_original_task'](self.store.get('task'))
                self.assertNotIn('account_retry', [event['phase'] for event in self.store.task_events('task')])
                self.assertEqual(self.store.get('task')['account'], 'old_account')

    async def test_recovered_timeout_and_copyright_refusal_do_not_hand_off_to_another_account(self):
        self.recovery_functions()
        for error in (VideoQueryTimeout('查询超时'),
                      PlatformVideoError('platform_audio_copyright', '平台音频版权限制', '版权限制')):
            with self.subTest(error=error.task_phase):
                self.store.update('task', status='processing', account='old_account', conversation_id='111')
                async def resume(*args, **callbacks):
                    raise error
                async def generate(*args, **kwargs):
                    self.fail('Timeout or copyright refusal is not permission to regenerate')
                self.ns['pool'] = SimpleNamespace(resume_video=resume, generate_video=generate)
                await self.ns['_resume_original_task'](self.store.get('task'))
                row = self.store.get('task')
                self.assertEqual(row['failure_code'], error.task_phase)
                self.assertEqual((row['account'], row['conversation_id']), ('old_account', '111'))
                self.assertNotIn('account_retry', [event['phase'] for event in self.store.task_events('task')])


class RetryPresentationTests(unittest.TestCase):
    def test_retry_badge_and_history_show_rejected_account_without_claiming_generation(self):
        node = shutil.which('node.exe') or shutil.which('node')
        if not node:
            self.skipTest('Node is not installed')
        program = """
const fs=require('fs'),vm=require('vm');
global.window={};
global.UI={esc:s=>String(s).replaceAll('<','&lt;').replaceAll('>','&gt;'),time:()=> '时间'};
vm.runInThisContext(fs.readFileSync('web/task-evidence.js','utf8'));
const task={status:'queued',phase:'account_retry',duration:30};
const events=[{at:1,phase:'account_retry',details:{account:'old_account',conversation_id:'111',
  reason:'尚未开始生成',reply:'请确认<5秒>。'}}];
console.log(JSON.stringify({badge:window.TaskEvidence.badge(task),content:window.TaskEvidence.content(task,events)}));
"""
        result = subprocess.run([node, '-e', program], check=True, capture_output=True, text=True, encoding='utf-8')
        html = json.loads(result.stdout)
        self.assertIn('正在换号重试', html['badge'])
        self.assertIn('b-orange', html['badge'])
        self.assertIn('old_account', html['content'])
        self.assertIn('111', html['content'])
        self.assertIn('请确认&lt;5秒&gt;。', html['content'])
        self.assertNotIn('请确认<5秒>', html['content'])
