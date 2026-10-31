"""Prioritize current route occupancy through the real pool, with remote IO replaced."""
import asyncio
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_five_account_retry import pool_module


class CurrentQuotaRotationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / 'accounts'
        for name in 'ab':
            (root / name).mkdir(parents=True)
        self.config = patch.multiple(pool_module.config, MAX_INFLIGHT=4, PURE_HTTP_VIDEO=False)
        self.config.start()
        self.pool = pool_module.BrowserPool(root, str(Path(self.tmp.name) / 'pool.db'), max_concurrency=2)
        self.pool._proxy_state = lambda name: {'proxy_server': 'http://line-' + name + ':80', 'proxy_blocked': False}
        self.pool.accounts
        self.calls = []

    async def asyncTearDown(self):
        self.pool._conn.close()
        self.config.stop()
        self.tmp.cleanup()

    def history(self, name, *, sends, dispatched=None, last_sent=0, last_dispatched=0):
        self.pool._conn.execute('''UPDATE accounts_meta
            SET send_count=?,dispatch_count=?,last_sent_at=?,last_dispatched_at=? WHERE name=?''',
            (sends, sends if dispatched is None else dispatched, last_sent, last_dispatched, name))
        self.pool._conn.commit()

    def occupy(self, name, kind, count=1):
        for index in range(count):
            identity = f'previous-{kind}-{name}-{index}'
            self.pool.quota_ledger.confirm(name, kind, identity, identity)

    async def worker(self, account, *args, **kwargs):
        self.calls.append(account)
        cid = 'new-' + str(len(self.calls))
        kwargs['on_prompt_sent'](account)
        kwargs['on_conversation_id'](account, cid, time.time() + 60)
        kwargs['on_browser_closed']()
        return {'account': account, 'conversation_id': cid}

    async def daily(self, identity):
        return await self.pool.generate_video('fixture', duration=10, model='seedance-2.0', task_id=identity)

    async def test_unused_daily_account_wins_even_with_more_lifetime_sends(self):
        self.occupy('a', 'ordinary_fast10')
        self.history('a', sends=0)
        self.history('b', sends=100, last_sent=100)
        with patch.object(pool_module, 'generate_video', self.worker):
            result = await self.daily('prefer-current-quota')
        self.assertEqual(result['account'], 'b', 'An unused account must win over an exhausted 1/1 account regardless of lifetime history')
        self.assertEqual(self.calls, ['b'])

    async def test_equal_daily_usage_uses_oldest_send_then_skips_full_accounts(self):
        self.history('a', sends=0, last_sent=200)
        self.history('b', sends=100, last_sent=100)
        with patch.object(pool_module, 'generate_video', self.worker):
            await self.daily('oldest-unused')
            await self.daily('other-unused')
            with self.assertRaises(pool_module.AllAccountsLimitedError):
                await self.daily('both-at-one')
        self.assertEqual(self.calls, ['b', 'a'])
        for name in 'ab':
            self.assertEqual(self.pool.quota_ledger.snapshot(name, 'ordinary_fast10')['used'], 1)

    async def test_expert_and_daily_selection_use_their_own_current_quota(self):
        self.occupy('a', 'expert_fast', 2)
        self.occupy('b', 'ordinary_fast10')
        self.history('a', sends=0)
        self.history('b', sends=100)
        with patch.dict(sys.modules, {'plugin_expert30': SimpleNamespace(generate_video=self.worker)}):
            expert = await self.pool.generate_video('fixture', duration=30, model='seedance-2.0',
                duration_profile='plugin-expert30', task_id='expert-current')
        with patch.object(pool_module, 'generate_video', self.worker):
            daily = await self.daily('daily-current')
        self.assertEqual(expert['account'], 'b', 'expert must ignore the daily count')
        self.assertEqual(daily['account'], 'a', 'daily must ignore the expert count')
        self.assertEqual(self.pool.quota_ledger.snapshot('a', 'expert_fast')['used'], 2)
        self.assertEqual(self.pool.quota_ledger.snapshot('b', 'expert_fast')['used'], 1)
        self.assertEqual(self.pool.quota_ledger.snapshot('a', 'ordinary_fast10')['used'], 1)
        self.assertEqual(self.pool.quota_ledger.snapshot('b', 'ordinary_fast10')['used'], 1)

    async def test_reserved_current_quota_is_not_treated_as_unused(self):
        self.pool.quota_ledger.reserve('a', 'ordinary_fast10', 'other-reservation')
        self.history('a', sends=0)
        self.history('b', sends=100)
        with patch.object(pool_module, 'generate_video', self.worker):
            result = await self.daily('avoid-reserved')
        self.assertEqual(result['account'], 'b')
        self.assertEqual(self.pool.quota_ledger.snapshot('a', 'ordinary_fast10')['reserved'], 1)

    async def test_uncertain_current_quota_is_not_treated_as_unused(self):
        self.pool.quota_ledger.mark_sent('a', 'ordinary_fast10', 'other-unknown-send')
        self.history('a', sends=0)
        self.history('b', sends=100)
        with patch.object(pool_module, 'generate_video', self.worker):
            result = await self.daily('avoid-uncertain')
        self.assertEqual(result['account'], 'b')
        self.assertEqual(self.pool.quota_ledger.snapshot('a', 'ordinary_fast10')['uncertain'], 1)

    async def test_blocked_account_is_skipped_even_with_zero_local_usage(self):
        self.pool.quota_ledger.block('a', 'ordinary_fast10', time.time() + 3600,
            'Dola 回复当日视频额度已用完')
        self.history('a', sends=0)
        self.history('b', sends=100)
        with patch.object(pool_module, 'generate_video', self.worker):
            result = await self.daily('avoid-platform-exhausted')
        self.assertEqual(result['account'], 'b')
        self.assertEqual(self.pool.quota_ledger.snapshot('a', 'ordinary_fast10')['used'], 0)

    async def test_quota_is_rechecked_after_waiting_for_submission_locks(self):
        self.history('a', sends=0)
        self.history('b', sends=100)
        original = self.pool._rotation_eligible
        reserved_elsewhere = False

        def eligible(accounts, model, attempted):
            nonlocal reserved_elsewhere
            lock = self.pool._locks.get('a')
            if lock and lock.locked() and not reserved_elsewhere:
                reserved_elsewhere = True
                self.pool.quota_ledger.reserve('a', 'ordinary_fast10', 'concurrent-process')
            return original(accounts, model, attempted)

        with patch.object(self.pool, '_rotation_eligible', eligible), patch.object(pool_module, 'generate_video', self.worker):
            result = await self.daily('refresh-after-lock')
        self.assertTrue(reserved_elsewhere)
        self.assertEqual(result['account'], 'b', 'the selected account became more occupied before the lock was acquired')
        self.assertEqual(self.calls, ['b'])

    async def test_concurrent_preparation_counts_reservations_and_never_exceeds_daily_capacity(self):
        self.history('a', sends=0)
        self.history('b', sends=100)
        started, finish = asyncio.Event(), asyncio.Event()

        async def worker(account, *args, **kwargs):
            self.calls.append(account)
            cid = 'concurrent-' + str(len(self.calls))
            if len(self.calls) == 1:
                started.set()
                await finish.wait()
            kwargs['on_prompt_sent'](account)
            kwargs['on_conversation_id'](account, cid, time.time() + 60)
            kwargs['on_browser_closed']()
            return {'account': account}

        with patch.object(pool_module, 'generate_video', worker):
            first = asyncio.create_task(self.daily('inflight-reservation'))
            try:
                await asyncio.wait_for(started.wait(), 1)
                self.assertEqual(self.pool.quota_ledger.snapshot('a', 'ordinary_fast10')['reserved'], 1)
                second = await asyncio.wait_for(self.daily('concurrent-other-account'), 1)
                self.assertEqual(second['account'], 'b', 'the unused account wins while the first account is preparing')
                self.assertFalse(first.done())
            finally:
                finish.set()
                await asyncio.wait_for(first, 1)
            with self.assertRaises(pool_module.AllAccountsLimitedError):
                await self.daily('no-third-slot')
        self.assertEqual(self.calls[:2], ['a', 'b'])
        self.assertEqual(len(self.calls), 2)
        for name in 'ab':
            quota = self.pool.quota_ledger.snapshot(name, 'ordinary_fast10')
            self.assertEqual((quota['used'], quota['occupied'], quota['remaining']), (1, 1, 0))


if __name__ == '__main__':
    unittest.main()
