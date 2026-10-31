"""Exercise real pool scheduling; only replace remote video work."""
import asyncio
import atexit
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

_imports = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {'DOLA_PROXY_USAGE_DB': str(Path(_imports.name) / 'proxy.db')}):
    import browser_pool as pool_module

atexit.register(pool_module.proxy_usage.usage._conn.close)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_limit_is_terminal_and_blocks_account_without_retrying(self):
        from video_outcome import PlatformDailyLimitError
        from test_daily_video_limit import REPLY
        calls = []
        async def worker(account, *args, **kwargs):
            calls.append(account)
            kwargs['on_prompt_sent'](account)
            raise PlatformDailyLimitError(REPLY)
        with patch.object(pool_module, 'generate_video', worker):
            with self.assertRaises(PlatformDailyLimitError):
                await self.pool.generate_video('original')
        self.assertEqual(calls, ['a'])
        row = self.pool._conn.execute('SELECT rate_limited_until FROM accounts_meta WHERE name=?',('a',)).fetchone()
        self.assertGreater(row[0], time.time())

    async def test_daily_limit_on_recovery_blocks_account_without_a_new_submission(self):
        from video_outcome import PlatformDailyLimitError
        from test_daily_video_limit import REPLY
        from unittest.mock import AsyncMock
        self.assertIn('a', self.pool.accounts)
        worker = AsyncMock(side_effect=PlatformDailyLimitError(REPLY))
        with patch.object(pool_module, 'resume_video', worker), patch.object(pool_module, 'generate_video') as generate:
            with self.assertRaises(PlatformDailyLimitError):
                await self.pool.resume_video('a','123',30,model='seedance-2.5')
            generate.assert_not_called()
        self.assertEqual(worker.await_count, 1)
        row = self.pool._conn.execute('SELECT rate_limited_until FROM accounts_meta WHERE name=?',('a',)).fetchone()
        self.assertGreater(row[0], time.time())

    async def test_admin_pinned_expert_uses_selected_account_above_rotation_floor(self):
        calls = []
        self.pool._conn.execute("UPDATE accounts_meta SET send_count=5 WHERE name='b'")
        self.pool._conn.commit()

        async def expert(account, *args, **kwargs):
            calls.append(account)
            return {'account': account, 'conversation_id': 'fixture'}

        with patch('dola_expert.generate_video', expert), patch.object(pool_module, 'generate_video') as normal:
            result = await asyncio.wait_for(self.pool.generate_video(
                'one request', duration=15, entry_mode='pro-fast15', preferred_account='b'), 1)
        self.assertEqual((calls, result['account']), (['b'], 'b'))
        normal.assert_not_called()

    async def test_admin_pinned_expert_failure_does_not_rotate_or_use_normal_worker(self):
        from dola_expert import ExpertSubmissionError
        calls = []

        async def expert(account, *args, **kwargs):
            calls.append(account)
            raise ExpertSubmissionError('not accepted')

        with patch('dola_expert.generate_video', expert), patch.object(pool_module, 'generate_video') as normal:
            with self.assertRaises(ExpertSubmissionError):
                await self.pool.generate_video('one request', duration=15,
                                               entry_mode='pro-fast15', preferred_account='b')
        self.assertEqual(calls, ['b'])
        normal.assert_not_called()

    async def test_expert_single_attempt_never_falls_back_or_rotates(self):
        from dola_expert import ExpertSubmissionError
        from unittest.mock import AsyncMock
        worker=AsyncMock(side_effect=ExpertSubmissionError('未确认受理'))
        ordinary=AsyncMock()
        with patch('dola_expert.generate_video', worker), patch.object(pool_module, 'generate_video', ordinary):
            with self.assertRaises(ExpertSubmissionError):
                await self.pool.generate_video('original', duration=15, entry_mode='pro-fast15')
        self.assertEqual(worker.await_count, 1)
        self.assertEqual(worker.await_args.args[:4], ('a','original',None,15))
        ordinary.assert_not_awaited()
        self.assertEqual(self.pool.used_today('a'), 0)
        self.assertEqual(self.pool.used_today('b'), 0)

    async def test_unconfirmed_watermark_setting_stops_before_send_without_rotation_or_charge(self):
        from dola_watermark import WatermarkPreferenceError
        calls = []
        async def worker(account, *args, **kwargs):
            calls.append(account)
            raise WatermarkPreferenceError('设置未通过，提示词未发送')
        with patch.object(pool_module, 'generate_video', worker):
            with self.assertRaises(WatermarkPreferenceError):
                await self.pool.generate_video('original')
        self.assertEqual(calls, ['a'])
        self.assertEqual(self.pool.used_today('a'), 0)
        self.assertEqual(self.pool.used_today('b'), 0)

    async def test_missing_no_watermark_asset_never_rotates_or_releases_accepted_credit(self):
        from video_delivery import NoWatermarkAvailable
        calls = []
        async def worker(account, *args, **kwargs):
            calls.append(account)
            kwargs['on_prompt_sent'](account)
            kwargs['on_conversation_id'](account, '111', time.time()+60)
            kwargs['on_event']('accepted', {'video_count': 1})
            raise NoWatermarkAvailable()
        with patch.object(pool_module, 'generate_video', worker):
            with self.assertRaises(NoWatermarkAvailable):
                await self.pool.generate_video('original')
        self.assertEqual(calls, ['a'])
        self.assertEqual(self.pool.used_today('a'), 1)
        self.assertEqual(self.pool.used_today('b'), 0)

    async def test_restarted_task_skips_persisted_no_start_accounts(self):
        calls = []
        async def worker(account, *args, **kwargs):
            calls.append(account)
            kwargs['on_conversation_id'](account, '222', time.time()+60)
            kwargs['on_browser_closed']()
            return {'account': account, 'conversation_id': '222'}
        with patch.object(pool_module, 'generate_video', worker):
            try:
                result = await self.pool.generate_video('原文', excluded_accounts=['a'])
            except TypeError:
                self.fail('Persisted no-start attempts cannot yet be excluded after restart')
        self.assertEqual(calls, ['b'])
        self.assertEqual(result['account'], 'b')

    async def test_explicit_no_generation_rotates_after_closed_browser_with_same_prompt_and_frees_claim(self):
        from video_outcome import ExplicitNotAcceptedError
        calls, events = [], []
        self.pool.accounts
        self.pool.set_scheduling('c', False)
        self.pool.set_scheduling('d', False)
        self.pool.set_scheduling('e', False)
        self.pool.set_scheduling('f', False)
        self.pool.set_scheduling('g', False)
        self.pool.set_scheduling('h', False)
        prompt = '保持原文\n末尾空格 '
        browser_closed = False
        async def worker(account, sent_prompt, *args, **kwargs):
            nonlocal browser_closed
            calls.append((account, sent_prompt))
            kwargs['on_prompt_sent'](account)
            kwargs['on_conversation_id'](account, '111' if account == 'a' else '222', time.time()+60)
            if account == 'a':
                browser_closed = True
                raise ExplicitNotAcceptedError('请确认：用5秒生成还是指定4–15秒？确认后我生成。')
            self.assertTrue(browser_closed, 'Do not open a new account before the previous browser closed')
            kwargs['on_event']('accepted', {'reply': 'Seedance 2.5 消耗2个额度'})
            kwargs['on_browser_closed']()
            return {'account': account, 'conversation_id': '222'}
        with patch.object(pool_module, 'generate_video', worker):
            try:
                result = await self.pool.generate_video(prompt, model='seedance-2.5',
                    on_event=lambda phase, details: events.append((phase, details)))
            except ExplicitNotAcceptedError:
                self.fail('A confirmed no-start response ended the task instead of trying the next account')
        self.assertEqual(calls, [('a', prompt), ('b', prompt)])
        self.assertEqual(result['account'], 'b')
        self.assertEqual(self.pool.used_today('a'), 0)
        self.assertEqual(self.pool.used_today('b'), 1)
        self.assertEqual([details['account'] for phase, details in events if phase == 'account_retry'], ['a'])
        self.assertEqual(self.pool.semaphore._value, 2)
        self.assertFalse(self.pool._locks['a'].locked())

    async def test_no_start_error_after_a_real_receipt_does_not_rotate_or_release_daily_claim(self):
        from video_outcome import ExplicitNotAcceptedError
        calls = []
        async def worker(account, *args, **kwargs):
            calls.append(account)
            kwargs['on_prompt_sent'](account)
            kwargs['on_conversation_id'](account, '111', time.time()+60)
            kwargs['on_event']('accepted', {'reply': 'Seedance 2.5 消耗2个额度'})
            raise ExplicitNotAcceptedError('请确认生成下一个视频')
        with patch.object(pool_module, 'generate_video', worker):
            with self.assertRaises(ExplicitNotAcceptedError):
                await self.pool.generate_video('原文')
        self.assertEqual(calls, ['a'])
        self.assertEqual(self.pool.used_today('a'), 1)

    async def test_all_accounts_waiting_for_confirmation_are_tried_only_once(self):
        from video_outcome import ExplicitNotAcceptedError
        calls = []
        async def worker(account, *args, **kwargs):
            calls.append(account)
            kwargs['on_prompt_sent'](account)
            kwargs['on_conversation_id'](account, str(100 + len(calls)), time.time()+60)
            raise ExplicitNotAcceptedError('请确认用5秒还是15秒生成')
        with patch.object(pool_module, 'generate_video', worker):
            with self.assertRaises(pool_module.AccountAttemptsExhaustedError):
                await self.pool.generate_video('原文')
        self.assertEqual(calls, list('abcde'))
        self.assertEqual(sum(self.pool.used_today(account) for account in 'abcdefgh'), 0)

    async def asyncSetUp(self):
        self.root = tempfile.TemporaryDirectory()
        for name in 'abcdefgh':
            (Path(self.root.name) / name).mkdir()
        self.config = patch.object(pool_module.config, 'MAX_INFLIGHT', 4, create=True)
        self.config.start()
        self.pool = pool_module.BrowserPool(self.root.name, ':memory:', max_concurrency=2)
        self.proxies = patch.object(self.pool, '_proxy_state', side_effect=lambda name: {
            'proxy_server': 'http://line-a:80' if name in 'abef' else 'http://line-b:80',
            'proxy_blocked': False})
        self.proxies.start()

    async def asyncTearDown(self):
        self.proxies.stop()
        self.config.stop()
        self.pool._conn.close()
        self.root.cleanup()

    async def test_next_wave_waits_for_isp_completion_even_when_browsers_close(self):
        finish = asyncio.Event()
        submitted = []

        async def worker(account, *args, on_conversation_id=None, **kwargs):
            conversation_id = str(100 + len(submitted))
            on_conversation_id(account, conversation_id, time.time() + 60)
            submitted.append(account)
            if kwargs.get('on_browser_closed'):
                kwargs['on_browser_closed']()
            await finish.wait()
            return {'account': account, 'conversation_id': conversation_id, 'local_path': 'result.mp4'}

        with patch.object(pool_module, 'generate_video', worker):
            tasks = [asyncio.create_task(self.pool.generate_video('fixture')) for _ in range(5)]
            try:
                await asyncio.sleep(.2)
                self.assertEqual(len(submitted), 2, 'two ISP lines can own only two unfinished videos')
                self.assertEqual(len(set(submitted)), 2, 'an active account must not be reused')
                self.assertTrue(all(not t.done() for t in tasks))
                finish.set()
                await asyncio.wait_for(asyncio.gather(*tasks), 2)
                self.assertEqual(len(submitted), 5)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_two_browsers_do_not_submit_on_the_same_isp(self):
        finish = asyncio.Event()
        active_lines = []

        async def worker(account, *args, **kwargs):
            active_lines.append(self.pool._proxy_state(account)['proxy_server'])
            await finish.wait()
            raise RuntimeError('fixture stops before submission')

        with patch.object(pool_module, 'generate_video', worker):
            tasks = [asyncio.create_task(self.pool.generate_video('fixture')) for _ in range(2)]
            try:
                await asyncio.sleep(.1)
                self.assertEqual(len(active_lines), 2)
                self.assertEqual(len(set(active_lines)), 2)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_error_after_acceptance_never_submits_on_another_account(self):
        async def worker(account, *args, on_conversation_id=None, **kwargs):
            on_conversation_id(account, '12345', time.time() + 60)
            kwargs['on_event']('accepted', {'reply': 'Seedance 2.5 消耗2个额度'})
            if kwargs.get('on_browser_closed'):
                kwargs['on_browser_closed']()
            raise pool_module.CreditError('accepted conversation reported insufficient credits')

        with patch.object(pool_module, 'generate_video', worker):
            with self.assertRaises(Exception) as caught:
                await self.pool.generate_video('fixture')
            self.assertIsInstance(caught.exception, pool_module.CreditError)
        self.assertEqual(sum(self.pool.used_today(a) for a in 'abcdefgh'), 1)

    async def test_auth_or_closed_browser_tries_five_distinct_accounts(self):
        for message, expected in (
                ('net::ERR_INVALID_AUTH_CREDENTIALS', 'abcde'),
                ('Target page, context or browser has been closed', 'fghab')):
            calls = []
            async def worker(account, *args, **kwargs):
                calls.append(account)
                raise RuntimeError(message)
            with patch.object(pool_module, 'generate_video', worker):
                with self.assertRaises(Exception):
                    await self.pool.generate_video('fixture')
            self.assertEqual(calls, list(expected))
            self.assertFalse(self.pool._locks['a'].locked())
            self.assertEqual(self.pool.used_today('a'), 0)

    async def test_exhausted_page_retry_rotates_and_releases_each_account(self):
        from navigation_errors import BrowserSetupError
        calls = []
        async def worker(account, *args, **kwargs):
            calls.append(account)
            raise BrowserSetupError('打开页面失败，已停止自动刷新和换号')
        with patch.object(pool_module, 'generate_video', worker):
            with self.assertRaises(pool_module.AccountAttemptsExhaustedError):
                await self.pool.generate_video('fixture')
        self.assertEqual(calls, list('abcde'))
        self.assertFalse(self.pool._locks['a'].locked())
        self.assertFalse(self.pool._proxy_slots['http://line-a:80'].locked())

    async def test_error_after_send_without_conversation_rotates_by_user_policy(self):
        calls = []
        async def worker(account, *args, on_prompt_sent=None, **kwargs):
            calls.append(account)
            if on_prompt_sent:
                on_prompt_sent(account)
            raise RuntimeError('connection closed after Enter')
        with patch.object(pool_module, 'generate_video', worker):
            with self.assertRaises(RuntimeError):
                await self.pool.generate_video('fixture')
        self.assertEqual(calls, list('abcde'))
        self.assertEqual(sum(self.pool.used_today(a) for a in calls), 5)

    async def test_login_wall_after_send_marks_invalid_and_rotates(self):
        self.pool.accounts
        self.pool.set_login_status('a', True)
        calls = []
        async def worker(account, *args, on_prompt_sent=None, **kwargs):
            calls.append(account)
            on_prompt_sent(account)
            raise pool_module.LoginExpiredError('login wall after Enter')
        with patch.object(pool_module, 'generate_video', worker):
            with self.assertRaises(pool_module.AccountAttemptsExhaustedError):
                await self.pool.generate_video('fixture')
        self.assertEqual(calls, list('abcde'))
        self.assertEqual(self.pool._meta('a')['login_ok'], 0)

    async def test_recovery_does_not_charge_daily_quota_twice(self):
        async def resume(account, conversation_id, *args, **kwargs):
            return {'account': account, 'conversation_id': conversation_id}

        with patch.object(pool_module, 'resume_video', resume):
            await self.pool.resume_video('a', '12345', 30)
            await self.pool.resume_video('a', '12345', 30)
        self.assertEqual(self.pool.used_today('a'), 1)

    async def test_birthday_gate_pauses_account_without_marking_login_invalid_or_using_quota(self):
        self.pool.accounts
        self.pool.set_login_status('a', True)
        calls = []
        async def worker(account, *args, **kwargs):
            calls.append(account)
            if account == 'a':
                raise pool_module.BirthdayVerificationRequired('待生日验证')
            kwargs['on_conversation_id'](account, '333', time.time() + 60)
            kwargs['on_browser_closed']()
            return {'account': account, 'conversation_id': '333'}
        with patch.object(pool_module, 'generate_video', worker):
            result = await self.pool.generate_video('fixture')
        row = next(a for a in self.pool.list_accounts() if a['name'] == 'a')
        self.assertEqual(row.get('verification_required'), 'birthday')
        self.assertFalse(row['scheduling'])
        self.assertEqual(row['login_ok'], 1)
        self.assertEqual(row['used_today'], 0)
        self.assertNotEqual(result['account'], 'a')
        self.assertEqual(calls.count('a'), 1)
        self.pool.reset_all_accounts()
        row = next(a for a in self.pool.list_accounts() if a['name'] == 'a')
        self.assertFalse(self.pool._schedulable(row), 'bulk reset must not undo required verification')
        self.pool.set_scheduling('a', True)
        row = next(a for a in self.pool.list_accounts() if a['name'] == 'a')
        self.assertFalse(row['verification_required'])
        self.assertTrue(self.pool._schedulable(row), 'manual re-enable allows checking the account again')

    async def test_recovery_reserves_accounts_before_new_submissions_start(self):
        self.pool.reserve_recoveries([{'account': 'a'}])
        async def worker(account, *args, on_conversation_id=None, **kwargs):
            on_conversation_id(account, '555', time.time() + 60)
            if kwargs.get('on_browser_closed'):
                kwargs['on_browser_closed']()
            return {'account': account, 'conversation_id': '555'}
        with patch.object(pool_module, 'generate_video', worker):
            result = await self.pool.generate_video('fixture')
        self.assertNotEqual(result['account'], 'a')

    async def test_accepted_quota_survives_pool_restart(self):
        database = str(Path(self.root.name) / 'pool.db')
        first = pool_module.BrowserPool(self.root.name, database, max_concurrency=2)
        first._claim_conversation('a', '12345')
        first._conn.close()
        restarted = pool_module.BrowserPool(self.root.name, database, max_concurrency=2)
        try:
            restarted._claim_conversation('a', '12345')
            self.assertEqual(restarted.used_today('a'), 1)
        finally:
            restarted._conn.close()

    async def test_invalid_account_cleanup_preserves_profile_in_restorable_trash(self):
        self.pool.accounts
        self.pool.set_login_status('a', False)
        profile = Path(self.root.name) / 'a'
        marker = profile / 'profile-marker.txt'
        marker.write_text('retained account data', encoding='utf-8')
        self.pool.delete_account('a')
        self.assertNotIn('a', self.pool.accounts)
        trash = self.pool.list_trash()
        self.assertEqual([row['name'] for row in trash], ['a'])
        self.assertEqual(self.pool.restore_account(trash[0]['backup']), 'a')
        self.assertEqual(marker.read_text(encoding='utf-8'), 'retained account data')
        self.assertIn('a', self.pool.accounts)

    async def test_cancelled_task_releases_slots_and_account_for_next_task(self):
        accepted = asyncio.Event()
        async def blocked(account, *args, on_conversation_id=None, **kwargs):
            on_conversation_id(account, '111', time.time() + 60)
            kwargs['on_browser_closed']()
            accepted.set()
            await asyncio.Event().wait()
        with patch.object(pool_module, 'generate_video', blocked):
            task = asyncio.create_task(self.pool.generate_video('fixture'))
            await asyncio.wait_for(accepted.wait(), 1)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertFalse(self.pool._locks['a'].locked())
        self.assertFalse(self.pool._proxy_slots['http://line-a:80'].locked())
        async def completed(account, *args, on_conversation_id=None, **kwargs):
            on_conversation_id(account, '222', time.time() + 60)
            kwargs['on_browser_closed']()
            return {'account': account, 'conversation_id': '222'}
        with patch.object(pool_module, 'generate_video', completed):
            result = await asyncio.wait_for(self.pool.generate_video('fixture'), 1)
        # The cancelled attempt releases a's lock, but b has not used its first turn.
        self.assertEqual(result['account'], 'b')


if __name__ == '__main__':
    unittest.main()
