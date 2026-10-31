"""Terminal policy failures keep their reply and release only their original reservation."""
import ast
from pathlib import Path
import tempfile
import unittest

from store import TaskStore
from task_results import fail_task
from test_five_account_retry import pool_module
from navigation_errors import BrowserCleanupError
from video_outcome import PlatformNoChargeError

REFUSAL = ("I can't generate the video because your input contains content that may violate our policies. "
           'Modify it and try again.')


class NoChargeTaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        accounts = Path(self.tmp.name) / 'accounts'
        (accounts / 'fixture').mkdir(parents=True)
        self.pool = pool_module.BrowserPool(accounts, ':memory:')
        self.pool.accounts
        self.store = TaskStore(':memory:')
        self.store.create('task', 'seedance-2.0', 'user prompt', '9:16', 15)
        self.store.update('task', status='processing', account='fixture', conversation_id='123', accepted_at=1)
        self.pool._claim_conversation('fixture', '123')
        self.pool._claim_conversation('fixture', 'other-video')

    def tearDown(self):
        self.store._conn.close()
        self.pool._conn.close()
        self.tmp.cleanup()

    def test_terminal_failure_is_persisted_with_exact_reason_and_reply(self):
        fail_task(self.store, 'task', PlatformNoChargeError(REFUSAL))
        row = self.store.get('task')
        self.assertEqual((row['status'], row['phase'], row['failure_code']),
                         ('failed', 'platform_rejected_no_charge', 'platform_rejected_no_charge'))
        self.assertEqual(row['dola_reply'], REFUSAL)
        self.assertTrue(row['finished_at'])
        self.assertEqual(row['accepted_at'], 1, 'Historical receipt evidence must remain intact')

    def test_terminal_event_stops_the_task_before_browser_cleanup_finishes(self):
        self.store.record_evidence('task', 'platform_rejected_no_charge',
                                   {'reply': REFUSAL, 'reason': '平台拒绝输入，本次未计号池额度'})
        row = self.store.get('task')
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['failure_code'], 'platform_rejected_no_charge')
        self.assertTrue(row['finished_at'])
        self.assertEqual(row['dola_reply'], REFUSAL)

    def test_browser_cleanup_error_keeps_already_confirmed_no_charge_failure_visible(self):
        error = PlatformNoChargeError(REFUSAL)
        fail_task(self.store, 'task', error)
        fail_task(self.store, 'task', BrowserCleanupError('浏览器关闭未确认，该ISP暂停'))
        row = self.store.get('task')
        self.assertEqual(row['phase'], 'platform_rejected_no_charge')
        self.assertEqual(row['error'], str(error)[:500])
        self.assertEqual(row['dola_reply'], REFUSAL)

    def reconciler(self):
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == '_reconcile_original_outcome')
        namespace = {'store': self.store, 'pool': self.pool, 'fail_task': fail_task,
                     '_complete_existing_video': lambda row: False, 'time': __import__('time')}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                     'no-charge-reconcile', 'exec'), namespace)
        return namespace['_reconcile_original_outcome']

    def test_original_query_releases_only_this_conversation_and_is_idempotent(self):
        reconcile = self.reconciler()
        outcome = {'state': 'failed', 'reply': REFUSAL, 'video_count': 0,
                   'error': PlatformNoChargeError(REFUSAL)}
        for _ in range(2):
            self.assertFalse(reconcile(self.store.get('task'), outcome))
            self.assertEqual(self.pool.used_today('fixture'), 1)
            self.assertEqual([row['conversation_id'] for row in self.pool._conn.execute(
                'SELECT conversation_id FROM accepted_videos')], ['other-video'])
            self.assertEqual(self.store.get('task')['phase'], 'platform_rejected_no_charge')

    def test_generic_failure_does_not_release_any_reservation(self):
        class GenericFailure(RuntimeError):
            task_phase = 'platform_copyright'
        reconcile = self.reconciler()
        outcome = {'state': 'failed', 'reply': 'generated video withheld', 'video_count': 0,
                   'error': GenericFailure('unknown billing')}
        self.assertFalse(reconcile(self.store.get('task'), outcome))
        self.assertEqual(self.pool.used_today('fixture'), 2)
        self.assertEqual(self.store.get('task')['phase'], 'platform_copyright')

    def test_member_gets_clear_failure_reason_without_account_or_conversation_internals(self):
        fail_task(self.store, 'task', PlatformNoChargeError(REFUSAL))
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_member_task')
        namespace = {}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                     'no-charge-member', 'exec'), namespace)
        member = namespace['_member_task'](self.store.get('task'))
        self.assertEqual((member['status'], member['phase']), ('failed', 'platform_rejected_no_charge'))
        self.assertIn('未计号池额度', member['error'])
        self.assertNotIn('account', member)
        self.assertNotIn('conversation_id', member)
