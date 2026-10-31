"""A low-send account that cannot open its composer must not strand the pool."""
import asyncio
import time
import unittest
from unittest.mock import patch

import test_account_rotation as rotation
from test_five_account_retry import pool_module as pm
from navigation_errors import BrowserSetupError


class PreparationRotationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = rotation.AccountRotationTests.asyncSetUp
    asyncTearDown = rotation.AccountRotationTests.asyncTearDown
    make_pool = rotation.AccountRotationTests.make_pool

    def enable(self, names):
        for name in 'abcde':
            self.pool.set_scheduling(name, name in names)

    async def prepare_worker(self, account, *args, **kwargs):
        self.calls.append(account)
        kwargs['on_browser_closed']()
        if account == 'a':
            raise BrowserSetupError('视频面板未打开，提示词未发送')
        kwargs['on_prompt_sent'](account)
        return {'account': account}

    async def test_new_tasks_skip_temporarily_failed_account_then_rejoin_lowest_round(self):
        self.enable('abc')
        self.pool._conn.execute("UPDATE accounts_meta SET send_count=1 WHERE name IN ('b','c')")
        self.pool._conn.commit()
        with patch.object(pm, 'generate_video', self.prepare_worker):
            await self.pool.generate_video('fixture')
            await self.pool.generate_video('fixture')
        self.assertEqual(self.calls, ['a', 'b', 'c'])
        rows = {a['name']: a for a in self.pool.list_accounts()}
        self.assertTrue(rows['a']['preparation_retrying'])
        self.assertFalse(rows['c']['preparation_retrying'], 'Same ISP must remain usable')
        self.assertFalse(rows['a']['proxy_cooling'])
        self.pool._preparation_backoff['a']['until'] = time.time() - 1
        async def recovered(account, *args, **kwargs):
            self.calls.append(account)
            kwargs['on_prompt_sent'](account)
            kwargs['on_browser_closed']()
            return {'account': account}
        with patch.object(pm, 'generate_video', recovered):
            await self.pool.generate_video('fixture')
        self.assertEqual(self.calls, ['a', 'b', 'c', 'a'])
        self.assertNotIn('a', self.pool._preparation_backoff)

    async def test_expiry_while_higher_round_isp_is_busy_does_not_strand_retry(self):
        self.enable('ab')
        self.pool._conn.execute("UPDATE accounts_meta SET send_count=1 WHERE name='b'")
        self.pool._conn.commit()
        slot = self.pool._proxy_slots.setdefault('http://line-b:80', asyncio.Semaphore(1))
        await slot.acquire()
        with patch.object(pm, 'generate_video', self.prepare_worker):
            pending = asyncio.create_task(self.pool.generate_video('fixture'))
            try:
                for _ in range(50):
                    if 'a' in self.pool._preparation_backoff:
                        break
                    await asyncio.sleep(.005)
                self.assertIn('a', self.pool._preparation_backoff)
                self.pool._preparation_backoff['a']['until'] = time.time() - 1
                slot.release()
                self.pool._available.set()
                result = await asyncio.wait_for(pending, 1)
                self.assertEqual(result['account'], 'b')
                self.assertEqual(self.calls, ['a', 'b'])
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)

    async def test_only_paused_account_wakes_without_external_event(self):
        self.enable('a')
        with patch.object(pm, 'PREPARATION_RETRY_BASE_SEC', .06), patch.object(pm, 'generate_video', self.prepare_worker):
            with self.assertRaises(pm.AccountAttemptsExhaustedError):
                await self.pool.generate_video('fixture')
        async def recovered(account, *args, **kwargs):
            kwargs['on_prompt_sent'](account)
            kwargs['on_browser_closed']()
            return {'account': account}
        with patch.object(pm, 'generate_video', recovered):
            result = await asyncio.wait_for(self.pool.generate_video('fixture'), 1)
        self.assertEqual(result['account'], 'a')

    async def test_five_bad_composers_stop_after_five_distinct_accounts_without_charging(self):
        for index, name in enumerate('abcde'):
            self.pool._conn.execute('UPDATE accounts_meta SET send_count=? WHERE name=?', (index, name))
        self.pool._conn.commit()
        async def broken(account, *args, **kwargs):
            self.calls.append(account)
            kwargs['on_browser_closed']()
            raise BrowserSetupError('视频面板未打开')
        with patch.object(pm, 'generate_video', broken):
            with self.assertRaises(pm.AccountAttemptsExhaustedError):
                await asyncio.wait_for(self.pool.generate_video('fixture'), 1)
        self.assertEqual(self.calls, list('abcde'))
        self.assertEqual(sum(self.pool.used_today(a) for a in 'abcde'), 0)
        self.assertEqual([self.pool._meta(a)['send_count'] for a in 'abcde'], list(range(5)))

    async def test_preparation_backoff_is_bounded_and_reason_redacted(self):
        with patch.object(pm.time, 'time', return_value=1000):
            for seconds in (30, 60, 120, 120):
                self.pool._pause_preparation_failure('a', BrowserSetupError('https://user:secret@fixture.test token=secret'))
                state = self.pool._preparation_backoff['a']
                self.assertEqual(state['until'], 1000 + seconds)
                self.assertNotIn('secret', state['reason'])
        self.pool._record_send('a', 'http://line-a:80')
        self.assertNotIn('a', self.pool._preparation_backoff)

    async def test_uncertain_send_keeps_quota_reservation_and_has_no_preparation_backoff(self):
        self.enable('a')
        async def sent(account, *args, **kwargs):
            kwargs['on_prompt_sent'](account)
            kwargs['on_browser_closed']()
            raise BrowserSetupError('提交后页面关闭')
        with patch.object(pm, 'generate_video', sent):
            with self.assertRaises(pm.AccountAttemptsExhaustedError):
                await self.pool.generate_video('fixture', task_id='uncertain-send-fixture')
        self.assertEqual(self.pool.used_today('a'), 1)
        self.assertNotIn('a', self.pool._preparation_backoff)

    async def test_lower_send_account_panel_failure_can_retry_higher_send_account(self):
        for name in 'cde':
            self.pool.set_scheduling(name, False)
        self.pool._conn.execute("UPDATE accounts_meta SET send_count=7 WHERE name='a'")
        self.pool._conn.execute("UPDATE accounts_meta SET send_count=8 WHERE name='b'")
        self.pool._conn.commit()

        async def worker(account, *args, **kwargs):
            self.calls.append(account)
            kwargs['on_browser_closed']()
            if account == 'a':
                raise BrowserSetupError('视频面板未打开（未出现模型/时长控件），已结束本次浏览器提交。')
            kwargs['on_prompt_sent'](account)
            return {'account': account}

        with patch.object(pm, 'generate_video', worker):
            result = await asyncio.wait_for(self.pool.generate_video('fixture'), 1)
        self.assertEqual(result['account'], 'b')
        self.assertEqual(self.calls, ['a', 'b'])
        self.assertEqual(self.pool._meta('a')['send_count'], 7)
        self.assertEqual(self.pool.used_today('a'), 0)
        self.assertIsNone(self.pool._meta('a')['login_ok'])


if __name__ == '__main__':
    unittest.main()
