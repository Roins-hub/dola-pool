"""Sending, not failed browser preparation, advances a pool round."""
import asyncio
import time
import unittest
from unittest.mock import patch

import test_account_rotation as rotation
from test_five_account_retry import pool_module as pm
from navigation_errors import BrowserSetupError


class SendRotationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = rotation.AccountRotationTests.asyncSetUp
    asyncTearDown = rotation.AccountRotationTests.asyncTearDown
    make_pool = rotation.AccountRotationTests.make_pool
    worker = rotation.AccountRotationTests.worker
    def single_lines(self):
        self.pool._proxy_state = lambda name: {'proxy_server': f'http://line-{name}:80', 'proxy_blocked': False}

    def enabled(self, names):
        for name in 'abcde':
            self.pool.set_scheduling(name, name in names)

    def row(self, name):
        return next(a for a in self.pool.list_accounts() if a['name'] == name)

    async def test_failed_preparation_does_not_advance_send_round(self):
        self.single_lines()
        self.enabled('ab')
        async def worker(account, *args, **kw):
            self.calls.append(account)
            kw['on_browser_closed']()
            if account == 'a':
                raise BrowserSetupError('ERR_CONNECTION_RESET')
            kw['on_prompt_sent'](account)
            return {'account': account}
        with patch.object(pm, 'generate_video', worker):
            await self.pool.generate_video('fixture')
        self.assertEqual(self.row('a').get('send_count'), 0)
        self.assertEqual(self.row('a')['dispatch_count'], 1)
        self.assertEqual(self.row('b').get('send_count'), 1)
        self.assertGreater(self.row('a').get('proxy_cooling_until', 0), time.time())
        self.assertIsNone(self.row('a')['login_ok'])

    async def test_sent_and_receipt_callbacks_count_once_and_survive_restart(self):
        async def worker(account, *args, **kw):
            kw['on_prompt_sent'](account)
            kw['on_prompt_sent'](account)
            kw['on_conversation_id'](account, 'once', time.time()+60)
            kw['on_browser_closed']()
            return {'account': account}
        with patch.object(pm, 'generate_video', worker):
            await self.pool.generate_video('fixture')
        self.assertEqual(self.row('a').get('send_count'), 1)
        self.pool._conn.close()
        self.pool = self.make_pool()
        self.assertEqual(self.row('a').get('send_count'), 1)
        with patch.object(pm, 'generate_video', self.worker):
            await self.pool.generate_video('fixture')
        self.assertEqual(self.calls, ['b'])

    async def test_failed_line_is_skipped_but_rejoins_before_healthy_second_send(self):
        self.single_lines()
        self.enabled('ab')
        fail = True
        async def worker(account, *args, **kw):
            self.calls.append(account)
            kw['on_browser_closed']()
            if account == 'a' and fail:
                raise BrowserSetupError('ERR_CONNECTION_RESET')
            kw['on_prompt_sent'](account)
            return {'account': account}
        with patch.object(pm, 'PROXY_RETRY_BASE_SEC', .04, create=True), patch.object(pm, 'generate_video', worker):
            await self.pool.generate_video('fixture')
            fail = False
            await asyncio.sleep(.06)
            await self.pool.generate_video('fixture')
        self.assertEqual(self.calls, ['a', 'b', 'a'])

    async def test_all_cooling_lines_wake_on_expiry_without_another_pool_event(self):
        self.single_lines()
        self.enabled('a')
        fail = True
        async def worker(account, *args, **kw):
            kw['on_browser_closed']()
            if fail:
                raise BrowserSetupError('ERR_CONNECTION_RESET')
            kw['on_prompt_sent'](account)
            return {'account': account}
        with patch.object(pm, 'PROXY_RETRY_BASE_SEC', .15, create=True), patch.object(pm, 'generate_video', worker):
            with self.assertRaises(pm.AccountAttemptsExhaustedError):
                await self.pool.generate_video('fixture')
            fail = False
            start = time.monotonic()
            result = await asyncio.wait_for(self.pool.generate_video('fixture'), 1)
        self.assertGreater(time.monotonic()-start, .08)
        self.assertEqual(result['account'], 'a')

    async def test_migration_uses_usage_not_failed_dispatches(self):
        self.pool._conn.execute('UPDATE accounts_meta SET dispatch_count=50')
        self.pool._conn.executemany('INSERT INTO usage VALUES (?,?,?)', [('a', '2000-01-01', 2), ('a', '2000-01-02', 1)])
        columns = {r['name'] for r in self.pool._conn.execute('PRAGMA table_info(accounts_meta)')}
        for name in ('send_count', 'last_sent_at'):
            if name in columns:
                self.pool._conn.execute(f'ALTER TABLE accounts_meta DROP COLUMN {name}')
        self.pool._conn.commit()
        self.pool._conn.close()
        self.pool = self.make_pool()
        self.assertEqual(self.row('a').get('send_count'), 3)
        self.assertEqual(self.row('b').get('send_count'), 0)
        self.assertEqual(self.row('a')['dispatch_count'], 50)

    async def test_busy_lower_current_usage_cannot_be_overtaken_by_higher_usage(self):
        self.single_lines()
        self.enabled('ab')
        self.pool._record_dispatch('a')
        self.pool._record_send('a', 'http://line-a:80')
        self.pool._claim('a')
        lock = self.pool._locks.setdefault('b', asyncio.Lock())
        await lock.acquire()
        with patch.object(pm, 'generate_video', self.worker):
            pending = asyncio.create_task(self.pool.generate_video('fixture'))
            try:
                await asyncio.sleep(.05)
                self.assertEqual(self.calls, [])
                lock.release()
                self.pool._available.set()
                await asyncio.wait_for(pending, 1)
                self.assertEqual(self.calls, ['b'])
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_excluded_lower_usage_does_not_allow_cross_quota_round_retry_or_hang(self):
        self.single_lines()
        self.enabled('ab')
        self.pool._record_dispatch('b')
        self.pool._record_send('b', 'http://line-b:80')
        self.pool._claim('b')
        with patch.object(pm, 'generate_video', self.worker):
            with self.assertRaises(pm.AccountAttemptsExhaustedError):
                await asyncio.wait_for(self.pool.generate_video('fixture', excluded_accounts=['a']), .5)
        self.assertEqual(self.calls, [])

    async def test_two_healthy_accounts_rotate_while_three_broken_lines_are_paused(self):
        self.single_lines()
        async def worker(account, *args, **kw):
            self.calls.append(account)
            kw['on_browser_closed']()
            if account in 'cde':
                raise BrowserSetupError('ERR_CONNECTION_RESET')
            kw['on_prompt_sent'](account)
            return {'account': account}
        with patch.object(pm, 'generate_video', worker):
            for _ in range(4):
                await asyncio.wait_for(self.pool.generate_video('fixture'), 1)
        self.assertEqual(self.calls, list('abcdeab'))
        for a in 'ab':
            self.assertEqual(self.row(a)['send_count'], 2)
        for a in 'cde':
            self.assertEqual(self.row(a)['send_count'], 0)
            self.assertGreater(self.row(a)['proxy_cooling_until'], time.time())

    async def test_one_line_error_pauses_all_accounts_on_same_physical_line(self):
        self.enabled('abc')
        async def worker(account, *args, **kw):
            self.calls.append(account)
            kw['on_browser_closed']()
            if account == 'a':
                raise BrowserSetupError('ERR_CONNECTION_RESET')
            kw['on_prompt_sent'](account)
            return {'account': account}
        with patch.object(pm, 'generate_video', worker):
            await self.pool.generate_video('fixture')
            await self.pool.generate_video('fixture')
        self.assertEqual(self.calls, list('abb'))
        self.assertEqual(self.row('a')['proxy_cooling_until'], self.row('c')['proxy_cooling_until'])

    async def test_network_backoff_caps_and_success_resets_it(self):
        key = 'http://line-a:80'
        with patch.object(pm.time, 'time', return_value=1000):
            for seconds in (30, 60, 120, 120):
                self.pool._pause_network_failure(key, BrowserSetupError('ERR_CONNECTION_RESET'))
                self.assertEqual(self.row('a')['proxy_cooling_until'], 1000+seconds)
            self.pool._record_send('a', key)
            self.assertEqual(self.row('a')['proxy_cooling_until'], 0)
            self.pool._pause_network_failure(key, BrowserSetupError('ERR_CONNECTION_RESET'))
            self.assertEqual(self.row('a')['proxy_cooling_until'], 1030)

    async def test_sent_failure_does_not_quarantine_a_healthy_line_or_refund_send_turn(self):
        self.enabled('a')
        async def worker(account, *args, **kw):
            kw['on_prompt_sent'](account)
            kw['on_browser_closed']()
            raise BrowserSetupError('ERR_CONNECTION_RESET')
        with patch.object(pm, 'generate_video', worker):
            with self.assertRaises(pm.AccountAttemptsExhaustedError):
                await self.pool.generate_video('fixture')
        self.assertEqual(self.row('a')['send_count'], 1)
        self.assertEqual(self.row('a')['proxy_cooling_until'], 0)

    async def test_cooling_expiry_wakes_even_when_other_eligible_account_stays_busy(self):
        self.single_lines()
        self.enabled('ab')
        self.pool._record_send('a', 'http://line-a:80')
        lock = self.pool._locks.setdefault('a', asyncio.Lock())
        await lock.acquire()
        try:
            with patch.object(pm, 'PROXY_RETRY_BASE_SEC', .25), patch.object(pm, 'generate_video', self.worker):
                self.pool._pause_network_failure('http://line-b:80', BrowserSetupError('ERR_CONNECTION_RESET'))
                await asyncio.wait_for(self.pool.generate_video('fixture'), .8)
            self.assertEqual(self.calls, ['b'])
        finally:
            lock.release()
