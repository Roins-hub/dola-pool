"""Round-robin fairness through the real scheduler; remote video calls are fixtures."""
import asyncio
from collections import Counter
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from test_five_account_retry import pool_module


class AccountRotationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'accounts'
        for name in 'abcde':
            (self.root / name).mkdir(parents=True)
        self.db = str(Path(self.tmp.name) / 'pool.db')
        self.inflight = patch.object(pool_module.config, 'MAX_INFLIGHT', 5)
        self.inflight.start()
        self.pool = self.make_pool()
        self.calls = []

    def make_pool(self):
        pool = pool_module.BrowserPool(self.root, self.db, max_concurrency=2)
        pool._proxy_state = lambda name: {
            'proxy_server': 'http://line-a:80' if name in 'ace' else 'http://line-b:80',
            'proxy_blocked': False}
        pool.accounts
        return pool

    async def asyncTearDown(self):
        self.pool._conn.close()
        self.inflight.stop()
        self.tmp.cleanup()

    async def worker(self, account, *args, **kwargs):
        self.calls.append(account)
        kwargs['on_conversation_id'](account, str(len(self.calls)), time.time()+60)
        kwargs['on_browser_closed']()
        return {'account': account}

    async def test_every_account_is_used_before_any_gets_a_second_video(self):
        with patch.object(pool_module, 'generate_video', self.worker):
            for _ in range(10):
                await self.pool.generate_video('fixture')
        self.assertEqual(self.calls, list('abcdeabcde'))

    async def test_concurrent_fast_completions_do_not_overtake_unused_accounts(self):
        self.pool.inflight = asyncio.Semaphore(2)
        async def worker(account, *args, **kwargs):
            result = await self.worker(account, *args, **kwargs)
            await asyncio.sleep(.001 if account == 'a' else .03)
            return result
        with patch.object(pool_module, 'generate_video', worker):
            await asyncio.wait_for(asyncio.gather(*[
                self.pool.generate_video('fixture') for _ in range(10)]), 3)
        self.assertEqual(len(set(self.calls[:5])), 5, self.calls)
        self.assertEqual(Counter(self.calls), Counter(dict.fromkeys('abcde', 2)))

    async def test_dispatch_order_survives_restart_and_daily_quota_reset(self):
        with patch.object(pool_module, 'generate_video', self.worker):
            await self.pool.generate_video('fixture')
            await self.pool.generate_video('fixture')
            self.pool._conn.execute('DELETE FROM usage')
            self.pool._conn.commit()
            self.pool._conn.close()
            self.pool = self.make_pool()
            await self.pool.generate_video('fixture')
        self.assertEqual(self.calls, list('abc'))

    async def test_inflight_dispatch_counts_before_receipt_or_completion(self):
        async def worker(account, *args, **kwargs):
            self.calls.append(account)
            # Real preparation failures need not create a quota claim, but must rotate.
            kwargs['on_browser_closed']()
            return {'account': account}
        with patch.object(pool_module, 'generate_video', worker):
            for _ in range(5):
                await self.pool.generate_video('fixture')
        self.assertEqual(self.calls, list('abcde'))
        self.assertEqual(sum(self.pool.used_today(a) for a in 'abcde'), 0)

    async def test_busy_unused_isp_waits_instead_of_reusing_an_available_account(self):
        for name in 'cde':
            self.pool.set_scheduling(name, False)
        with patch.object(pool_module, 'generate_video', self.worker):
            await self.pool.generate_video('fixture')
            slot = self.pool._proxy_slots.setdefault('http://line-b:80', asyncio.Semaphore(1))
            await slot.acquire()
            pending = asyncio.create_task(self.pool.generate_video('fixture'))
            try:
                await asyncio.sleep(.05)
                self.assertEqual(self.calls, ['a'], 'The unused account is temporarily busy, not excluded')
                slot.release()
                self.pool._available.set()
                await asyncio.wait_for(pending, 1)
                self.assertEqual(self.calls, ['a', 'b'])
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_disabled_or_invalid_accounts_do_not_block_the_next_round(self):
        self.pool.set_scheduling('c', False)
        self.pool.mark_login_expired('d')
        self.pool.set_onboarding('e', 'attention')
        with patch.object(pool_module, 'generate_video', self.worker):
            for _ in range(4):
                await self.pool.generate_video('fixture')
        self.assertEqual(self.calls, list('abab'))

    async def test_migration_keeps_history_without_penalizing_current_unused_quota(self):
        self.pool.accounts
        columns = {r['name'] for r in self.pool._conn.execute('PRAGMA table_info(accounts_meta)')}
        for name in ('dispatch_count', 'last_dispatched_at'):
            if name in columns:
                self.pool._conn.execute(f'ALTER TABLE accounts_meta DROP COLUMN {name}')
        self.pool._conn.executemany('INSERT INTO usage VALUES (?,?,?)',
                                   [('a', '2000-01-01', 2), ('b', '2000-01-01', 1)])
        self.pool._conn.commit()
        self.pool._conn.close()
        self.pool = self.make_pool()
        self.assertEqual(self.pool._meta('a')['dispatch_count'], 2)
        self.assertEqual(self.pool._meta('b')['dispatch_count'], 1)
        self.assertEqual(self.pool.used_today('a'), 0)
        with patch.object(pool_module, 'generate_video', self.worker):
            await self.pool.generate_video('fixture')
        self.assertEqual(self.calls, ['a'])


if __name__ == '__main__':
    unittest.main()
