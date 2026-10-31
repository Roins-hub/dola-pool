"""Only a proved pre-click failure may undo the local send fence's counters."""
from pathlib import Path
import tempfile
import unittest

from providers.base import ProviderError
from providers.doubao.accounts import AccountStore
from test_doubao_provider import ck


class BeforeSendAccountingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AccountStore(Path(self.temp.name))
        self.store.import_ck('one', ck(), 'direct')

    def attempt(self, task_id):
        with self.store.connect() as conn:
            row = conn.execute('SELECT * FROM attempts WHERE task_id=?', (task_id,)).fetchone()
            return dict(row) if row else None

    def account(self):
        return self.store.list_accounts()[0]

    def test_unsent_reservation_releases_slot_without_changing_rotation(self):
        self.store.reserve('never-clicked', 'one')
        self.store.reject_before_send('never-clicked', previous_last_sent=123)
        result = self.account()
        self.assertEqual(result['send_count'], 0)
        self.assertIsNone(result['last_sent'])
        self.assertEqual(result['quota_used'], 0)
        self.assertEqual(result['quota_reserved'], 0)
        self.assertEqual(result['inflight_tasks'], 0)
        self.assertEqual(self.attempt('never-clicked')['state'], 'rejected')
        self.assertEqual(self.attempt('never-clicked')['sent'], 0)

    def test_marked_fence_is_rolled_back_once_and_previous_real_usage_kept(self):
        self.store.reserve('completed', 'one')
        self.store.mark_sent('completed')
        self.store.settle('completed', 'completed')
        before = self.account()
        self.store.reserve('before-click', 'one')
        self.store.mark_sent('before-click')
        self.assertEqual(self.account()['send_count'], 2)
        self.store.reject_before_send('before-click', previous_last_sent=before['last_sent'])
        self.assertEqual(self.account(), before)
        self.assertEqual(self.attempt('before-click')['state'], 'rejected')
        self.assertEqual(self.attempt('before-click')['sent'], 0)
        self.store.reject_before_send('before-click', previous_last_sent=999)
        self.assertEqual(self.account(), before)

    def test_first_send_fence_restores_null_last_sent(self):
        self.store.reserve('before-click', 'one')
        self.store.mark_sent('before-click')
        self.store.reject_before_send('before-click')
        self.assertEqual(self.account()['send_count'], 0)
        self.assertIsNone(self.account()['last_sent'])

    def test_actual_or_uncertain_submissions_cannot_be_rolled_back(self):
        for state in ('accepted', 'unknown', 'completed', 'charge_unknown', 'rejected'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as folder:
                store = AccountStore(Path(folder))
                store.import_ck('one', ck(), 'direct')
                store.reserve('already-sent', 'one')
                store.mark_sent('already-sent')
                store.settle('already-sent', state)
                before = store.list_accounts()[0]
                with self.assertRaises(ProviderError) as caught:
                    store.reject_before_send('already-sent')
                self.assertEqual(caught.exception.code, 'unsafe_before_send_rollback')
                self.assertEqual(store.list_accounts()[0], before)
                with store.connect() as conn:
                    attempt = conn.execute('SELECT * FROM attempts').fetchone()
                    self.assertEqual(attempt['sent'], 1)
                    self.assertEqual(attempt['state'], state)

    def test_missing_task_or_invalid_prior_timestamp_never_mutates_account(self):
        self.store.reserve('before-click', 'one')
        self.store.mark_sent('before-click')
        before = self.account()
        for timestamp in (True, '123', -1, float('nan'), float('inf')):
            with self.subTest(timestamp=timestamp), self.assertRaises(ProviderError):
                self.store.reject_before_send('before-click', previous_last_sent=timestamp)
            self.assertEqual(self.account(), before)
            self.assertEqual(self.attempt('before-click')['state'], 'reserved')
        with self.assertRaises(ProviderError) as caught:
            self.store.reject_before_send('missing')
        self.assertEqual(caught.exception.code, 'task_not_found')
        self.assertEqual(self.account(), before)


if __name__ == '__main__':
    unittest.main()
