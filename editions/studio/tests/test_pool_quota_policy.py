"""Persistent local pool caps; external video work is replaced, platform gates stay real."""
import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from test_five_account_retry import pool_module


class PoolQuotaPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.accounts = Path(self.tmp.name) / 'accounts'
        self.db = str(Path(self.tmp.name) / 'pool.db')
        for name in 'abc':
            (self.accounts / name).mkdir(parents=True)
        self.pool = self.make_pool()

    def make_pool(self):
        pool = pool_module.BrowserPool(self.accounts, self.db, max_concurrency=2)
        pool._proxy_state = lambda name: {'proxy_server': 'http://line-' + name + ':80', 'proxy_blocked': False}
        pool.accounts
        return pool

    async def asyncTearDown(self):
        self.pool._conn.close()
        self.tmp.cleanup()

    def only_a(self):
        for name in 'bc':
            self.pool.set_scheduling(name, False)

    def snapshot_account_state(self):
        return {table: [tuple(row) for row in self.pool._conn.execute(f'SELECT * FROM {table} ORDER BY 1,2')]
                for table in ('usage', 'accepted_videos', 'accounts_meta')}

    async def test_default_old_pool_is_two_and_selected_mode_survives_restart(self):
        getter = getattr(self.pool, 'get_quota_policy', None)
        self.assertTrue(callable(getter), 'The pool needs one durable, inspectable local quota policy')
        self.assertEqual(getter(), {'mode': 'seedance25', 'limit': 2})
        self.assertTrue(all(row['limit'] == 2 for row in self.pool.list_accounts()))
        self.assertEqual(self.pool.set_quota_policy('fast'), {'mode': 'fast', 'limit': 4})
        self.pool._conn.close()
        self.pool = self.make_pool()
        self.assertEqual(self.pool.get_quota_policy(), {'mode': 'fast', 'limit': 4})
        self.assertTrue(all(row['limit'] == 4 and row['remaining'] == 4 for row in self.pool.list_accounts()))

    async def test_existing_database_without_policy_gets_two_without_rewriting_existing_counts(self):
        self.pool._claim_conversation('a', 'existing')
        self.pool._record_dispatch('a')
        before = self.snapshot_account_state()
        self.pool._conn.execute('DROP TABLE pool_settings')
        self.pool._conn.commit()
        self.pool._conn.close()
        self.pool = self.make_pool()
        self.assertEqual(self.pool.get_quota_policy(), {'mode': 'seedance25', 'limit': 2})
        self.assertEqual(self.snapshot_account_state(), before)

    async def test_switching_two_four_two_preserves_three_claims_rotation_and_all_account_metadata(self):
        for index in range(3):
            self.pool._claim_conversation('a', 'conv-' + str(index))
        self.pool._record_dispatch('a')
        self.pool.set_note('a', 'preserve note')
        before = self.snapshot_account_state()
        for mode, limit, remaining in [('fast', 4, 1), ('seedance25', 2, 0), ('fast', 4, 1)]:
            self.pool._available.clear()
            self.pool.set_quota_policy(mode)
            self.assertTrue(self.pool._available.is_set(), 'Waiting schedulers must re-read the new cap')
            row = next(row for row in self.pool.list_accounts() if row['name'] == 'a')
            self.assertEqual((row['used_today'], row['limit'], row['remaining']), (3, limit, remaining))
            self.assertEqual(self.snapshot_account_state(), before)
        self.pool._conn.close()
        self.pool = self.make_pool()
        self.assertEqual(self.snapshot_account_state(), before)
        self.assertEqual(self.pool.used_today('a'), 3)

    async def test_invalid_modes_are_value_errors_without_policy_or_account_changes(self):
        self.pool.set_quota_policy('fast')
        before = self.snapshot_account_state()
        self.pool._available.clear()
        for mode in ('', 'FAST', 'seedance-2.5', 'unknown', 4, True, None, []):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                self.pool.set_quota_policy(mode)
            self.assertEqual(self.pool.get_quota_policy(), {'mode': 'fast', 'limit': 4})
            self.assertEqual(self.snapshot_account_state(), before)
            self.assertFalse(self.pool._available.is_set())

    async def test_platform_daily_and_quota_locks_balance_and_switches_survive_a_larger_local_cap(self):
        self.pool._mark_daily_limit('a', 'platform daily maximum')
        self.pool._mark_quota_blocked('b', 'platform insufficient credit')
        self.pool._set_credit_balance('c', 1, 'known one-point balance')
        self.pool._conn.execute("UPDATE accounts_meta SET cooldown_until=?, login_ok=0 WHERE name='b'", (time.time()+300,))
        self.pool._conn.commit()
        before = self.snapshot_account_state()
        self.pool.set_quota_policy('fast')
        rows = self.pool.list_accounts()
        self.assertEqual(self.snapshot_account_state(), before)
        self.assertFalse(any(self.pool._schedulable(row, 'seedance-2.0') for row in rows))
        self.assertFalse(self.pool.available_for_model('seedance-2.0'))
        self.assertTrue(next(row for row in rows if row['name'] == 'a')['rate_limited'])
        self.assertTrue(next(row for row in rows if row['name'] == 'b')['quota_blocked'])
        self.assertEqual(next(row for row in rows if row['name'] == 'c')['credit_balance'], 1)

    async def test_all_limited_and_all_quota_blocked_checks_use_the_same_current_cap(self):
        self.pool.set_scheduling('c', False)
        self.pool._claim('a'); self.pool._claim('a')
        self.pool._claim('b'); self.pool._claim('b')
        self.assertTrue(self.pool.all_accounts_limited)
        self.pool.set_quota_policy('fast')
        self.assertFalse(self.pool.all_accounts_limited)
        self.assertTrue(self.pool.available_for_model('seedance-2.0'))
        self.pool._set_credit_balance('b', 0)
        self.pool.set_quota_policy('seedance25')
        self.assertTrue(self.pool.all_accounts_quota_blocked_for_model('seedance-2.0'))
        self.pool.set_quota_policy('fast')
        self.assertFalse(self.pool.all_accounts_quota_blocked_for_model('seedance-2.0'))

    async def test_third_and_fourth_submissions_are_allowed_in_fast_policy_but_fifth_is_not(self):
        self.only_a()
        calls = []
        async def worker(account, *args, **callbacks):
            calls.append(account)
            self.assertEqual(callbacks['model'], 'seedance-2.0', 'A quota preset must not switch generation models')
            callbacks['on_conversation_id'](account, str(len(calls)), time.time()+60)
            callbacks['on_browser_closed']()
            return {'account': account}
        with patch.object(pool_module, 'generate_video', worker):
            for _ in range(2):
                await self.pool.generate_video('fixture', duration=15, model='seedance-2.0')
            with self.assertRaises(pool_module.AllAccountsLimitedError):
                await self.pool.generate_video('fixture', duration=15, model='seedance-2.0')
            self.pool.set_quota_policy('fast')
            for _ in range(2):
                await self.pool.generate_video('fixture', duration=15, model='seedance-2.0')
            with self.assertRaises(pool_module.AllAccountsLimitedError):
                await self.pool.generate_video('fixture', duration=15, model='seedance-2.0')
        self.assertEqual(calls, ['a']*4)
        self.assertEqual(self.pool.used_today('a'), 4)

    async def test_new_platform_daily_limit_seals_at_selected_local_cap_and_never_unblocks_on_switch(self):
        self.only_a()
        self.pool.set_quota_policy('fast')
        self.pool._mark_daily_limit('a', 'platform reached maximum')
        self.assertEqual(self.pool.used_today('a'), 4)
        self.pool.set_quota_policy('seedance25')
        self.assertTrue(self.pool.all_accounts_limited)
        self.pool.set_quota_policy('fast')
        self.assertTrue(self.pool.all_accounts_limited)

    async def test_mode_switch_does_not_restart_rotation_or_reuse_previously_dispatched_accounts_first(self):
        self.pool._record_dispatch('a')
        self.pool._record_dispatch('b')
        before = {row['name']: (row['dispatch_count'], row['last_dispatched_at']) for row in self.pool.list_accounts()}
        self.pool.set_quota_policy('fast')
        self.assertEqual({row['name']: (row['dispatch_count'], row['last_dispatched_at'])
                          for row in self.pool.list_accounts()}, before)
        async def worker(account, *args, **callbacks):
            callbacks['on_browser_closed']()
            return {'account': account}
        with patch.object(pool_module, 'generate_video', worker):
            result = await self.pool.generate_video('fixture', duration=15, model='seedance-2.0')
        self.assertEqual(result['account'], 'c')

    async def test_lowering_policy_does_not_cancel_an_already_accepted_fourth_submission(self):
        self.only_a()
        self.pool.set_quota_policy('fast')
        for _ in range(3): self.pool._claim('a')
        accepted, finish = asyncio.Event(), asyncio.Event()
        async def worker(account, *args, **callbacks):
            callbacks['on_conversation_id'](account, 'fourth', time.time()+60)
            callbacks['on_event']('accepted', {'reply': 'existing accepted conversation'})
            callbacks['on_browser_closed']()
            accepted.set()
            await finish.wait()
            return {'account': account}
        with patch.object(pool_module, 'generate_video', worker):
            running = asyncio.create_task(self.pool.generate_video('fixture', duration=15, model='seedance-2.0'))
            try:
                await asyncio.wait_for(accepted.wait(), 1)
                self.pool.set_quota_policy('seedance25')
                self.assertFalse(running.done())
                self.assertEqual(self.pool.used_today('a'), 4)
                finish.set()
                self.assertEqual((await asyncio.wait_for(running, 1))['account'], 'a')
                with self.assertRaises(pool_module.AllAccountsLimitedError):
                    await self.pool.generate_video('next', duration=15, model='seedance-2.0')
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)


if __name__ == '__main__':
    unittest.main()
