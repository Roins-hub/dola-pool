"""Exercise account retries through the real pool without opening browsers or sending video requests."""
import asyncio
import atexit
from collections import defaultdict
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch


_imports = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {'DOLA_PROXY_USAGE_DB': str(Path(_imports.name) / 'proxy.db')}):
    import browser_pool as pool_module

from navigation_errors import BrowserSetupError
from video_outcome import ExplicitNotAcceptedError

atexit.register(pool_module.proxy_usage.usage._conn.close)


class FiveAccountRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = tempfile.TemporaryDirectory()
        self.names = list('abcdefghijkl')
        for name in self.names:
            (Path(self.root.name) / name).mkdir()
        self.inflight_config = patch.object(pool_module.config, 'MAX_INFLIGHT', 4, create=True)
        self.http_config = patch.object(pool_module.config, 'PURE_HTTP_VIDEO', False, create=True)
        self.inflight_config.start()
        self.http_config.start()
        self.pool = pool_module.BrowserPool(self.root.name, ':memory:', max_concurrency=2)
        self.proxies = patch.object(self.pool, '_proxy_state', side_effect=lambda name: {
            'proxy_server': 'http://line-a:80' if name in 'acegik' else 'http://line-b:80',
            'proxy_blocked': False,
        })
        self.proxies.start()

    async def asyncTearDown(self):
        self.proxies.stop()
        self.pool._conn.close()
        self.http_config.stop()
        self.inflight_config.stop()
        self.root.cleanup()

    def assert_all_slots_released(self):
        self.assertEqual(self.pool.semaphore._value, 2, 'submission slots leaked or released twice')
        self.assertEqual(self.pool.inflight._value, 4, 'inflight slots leaked or released twice')
        self.assertTrue(all(not lock.locked() for lock in self.pool._locks.values()))
        self.assertTrue(all(slot._value == 1 for slot in self.pool._proxy_slots.values()))

    async def test_white_page_retries_new_account_with_exact_same_request(self):
        prompt = '保持原文\n末尾空格  '
        references = ['one.png', 'two.webp']
        calls = []

        async def worker(account, sent_prompt, ratio, duration, **kwargs):
            calls.append((account, sent_prompt, ratio, duration, kwargs['model'],
                          list(kwargs['reference_image_paths'])))
            if len(calls) == 1:
                kwargs['on_browser_closed']()
                raise BrowserSetupError('白屏，面板等待超时，提示词未发送')
            kwargs['on_prompt_sent'](account)
            kwargs['on_conversation_id'](account, '5001', time.time() + 60)
            kwargs['on_event']('accepted', {'reply': 'Seedance 2.5 消耗2个额度'})
            kwargs['on_browser_closed']()
            return {'account': account, 'conversation_id': '5001'}

        with patch.object(pool_module, 'generate_video', worker):
            result = await self.pool.generate_video(prompt, ratio='9:16', duration=30,
                                                   model='seedance_v2.5',
                                                   reference_image_paths=references)
        self.assertEqual([call[0] for call in calls], ['a', 'b'])
        for call in calls:
            self.assertEqual(call[1:], (prompt, '9:16', 30, 'seedance_v2.5', references))
        self.assertEqual(result['account'], 'b')
        self.assertEqual(self.pool.used_today('a'), 0)
        self.assertEqual(self.pool.used_today('b'), 1)
        self.assert_all_slots_released()

    async def test_timeout_before_sending_retries_without_charging_failed_account(self):
        calls = []

        async def worker(account, *args, **kwargs):
            calls.append(account)
            if len(calls) == 1:
                kwargs['on_browser_closed']()
                raise TimeoutError('等待输入框超时，尚未输入提示词')
            kwargs['on_conversation_id'](account, '5002', time.time() + 60)
            kwargs['on_browser_closed']()
            return {'account': account}

        with patch.object(pool_module, 'generate_video', worker):
            result = await self.pool.generate_video('原文')
        self.assertEqual(calls, ['a', 'b'])
        self.assertEqual(result['account'], 'b')
        self.assertEqual(self.pool.used_today('a'), 0)
        self.assert_all_slots_released()

    async def test_five_explicit_no_start_accounts_end_task_without_sixth(self):
        calls = []

        async def worker(account, *args, **kwargs):
            calls.append(account)
            kwargs['on_prompt_sent'](account)
            kwargs['on_conversation_id'](account, 'no-start-' + account, time.time() + 60)
            kwargs['on_browser_closed']()
            raise ExplicitNotAcceptedError('请确认用5秒还是15秒生成')

        with patch.object(pool_module, 'generate_video', worker):
            with self.assertRaises(RuntimeError) as caught:
                await self.pool.generate_video('原文')
        self.assertEqual(len(calls), 5)
        self.assertEqual(len(set(calls)), 5)
        self.assertIn('5', str(caught.exception))
        self.assertIn('账号', str(caught.exception))
        self.assertEqual(sum(self.pool.used_today(name) for name in self.names), 0)
        self.assert_all_slots_released()

    async def test_page_timeout_and_no_start_share_one_five_account_budget(self):
        errors = [
            BrowserSetupError('白屏'),
            TimeoutError('发送前超时'),
            ExplicitNotAcceptedError('请确认时长再生成'),
            BrowserSetupError('面板不完整'),
            TimeoutError('发送前网络超时'),
        ]
        calls = []

        async def worker(account, *args, **kwargs):
            calls.append(account)
            kwargs['on_browser_closed']()
            if len(calls) <= len(errors):
                raise errors[len(calls) - 1]
            return {'account': account, 'unexpected_sixth_attempt': True}

        with patch.object(pool_module, 'generate_video', worker):
            with self.assertRaises(RuntimeError) as caught:
                await self.pool.generate_video('原文')
        self.assertEqual(len(calls), 5)
        self.assertEqual(len(set(calls)), 5)
        self.assertIn('5', str(caught.exception))
        self.assertIn('账号', str(caught.exception))
        self.assert_all_slots_released()

    async def test_restarted_task_counts_distinct_persisted_accounts_toward_five(self):
        calls = []

        async def worker(account, *args, **kwargs):
            calls.append(account)
            kwargs['on_browser_closed']()
            raise ExplicitNotAcceptedError('请确认时长再生成')

        with patch.object(pool_module, 'generate_video', worker):
            with self.assertRaises(RuntimeError) as caught:
                await self.pool.generate_video('原文', excluded_accounts=['a', 'b', 'b', 'c', 'd'])
        self.assertEqual(calls, ['e'], 'four distinct persisted attempts leave only one new account')
        self.assertIn('5', str(caught.exception))
        self.assertIn('账号', str(caught.exception))
        self.assert_all_slots_released()

    async def test_five_persisted_accounts_do_not_open_any_more_browsers(self):
        calls = []

        async def worker(account, *args, **kwargs):
            calls.append(account)
            kwargs['on_browser_closed']()
            return {'account': account}

        with patch.object(pool_module, 'generate_video', worker):
            with self.assertRaises(RuntimeError) as caught:
                await self.pool.generate_video('原文', excluded_accounts=list('abcde'))
        self.assertEqual(calls, [])
        self.assertIn('5', str(caught.exception))
        self.assertIn('账号', str(caught.exception))
        self.assert_all_slots_released()

    async def test_two_tasks_retry_to_fifth_account_without_account_or_isp_conflicts(self):
        calls = defaultdict(list)
        active_accounts, active_lines = set(), set()
        conflicts = []
        peak = 0
        first_pair = asyncio.Event()

        async def worker(account, prompt, ratio, duration, **kwargs):
            nonlocal peak
            line = self.pool._proxy_state(account)['proxy_server']
            if account in active_accounts:
                conflicts.append('account collision')
            if line in active_lines:
                conflicts.append('ISP collision')
            active_accounts.add(account)
            active_lines.add(line)
            peak = max(peak, len(active_accounts))
            calls[prompt].append((account, ratio, duration, kwargs['model'],
                                  list(kwargs['reference_image_paths'])))
            if len(active_accounts) == 2:
                first_pair.set()
            try:
                await asyncio.wait_for(first_pair.wait(), 1)
                await asyncio.sleep(.01)
                if len(calls[prompt]) < 5:
                    raise BrowserSetupError('本次浏览器加载失败，未发送')
                kwargs['on_prompt_sent'](account)
                kwargs['on_conversation_id'](account, 'accepted-' + prompt, time.time() + 60)
                kwargs['on_event']('accepted', {'reply': 'Seedance 2.5 消耗2个额度'})
                return {'account': account, 'prompt': prompt}
            finally:
                active_accounts.discard(account)
                active_lines.discard(line)
                kwargs['on_browser_closed']()

        inputs = [('第一条原文  ', 'first.png'), ('第二条原文\n', 'second.png')]
        with patch.object(pool_module, 'generate_video', worker):
            tasks = [asyncio.create_task(self.pool.generate_video(prompt, ratio='9:16', duration=30,
                        model='seedance_v2.5', reference_image_paths=[reference]))
                     for prompt, reference in inputs]
            try:
                results = await asyncio.wait_for(asyncio.gather(*tasks), 3)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        self.assertEqual(conflicts, [])
        self.assertEqual(peak, 2, 'both original ISP lines should work concurrently')
        self.assertEqual([result['prompt'] for result in results], [item[0] for item in inputs])
        for prompt, reference in inputs:
            self.assertEqual(len(calls[prompt]), 5)
            self.assertEqual(len({call[0] for call in calls[prompt]}), 5)
            for call in calls[prompt]:
                self.assertEqual(call[1:], ('9:16', 30, 'seedance_v2.5', [reference]))
        self.assert_all_slots_released()

    async def test_cancellation_during_second_account_releases_every_slot(self):
        second_opened = asyncio.Event()
        calls = []

        async def worker(account, *args, **kwargs):
            calls.append(account)
            try:
                if len(calls) == 1:
                    raise BrowserSetupError('第一次白屏，未发送')
                second_opened.set()
                await asyncio.Future()
            finally:
                kwargs['on_browser_closed']()

        with patch.object(pool_module, 'generate_video', worker):
            task = asyncio.create_task(self.pool.generate_video('原文'))
            try:
                await asyncio.wait_for(second_opened.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(set(calls)), 2)
        self.assert_all_slots_released()


if __name__ == '__main__':
    unittest.main()
