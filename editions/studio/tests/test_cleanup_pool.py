"""Unconfirmed browser cleanup quarantines the physical ISP in the real pool."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_five_account_retry as fixtures
from navigation_errors import BrowserCleanupError
from video_outcome import ExplicitNotAcceptedError
import video_worker_ui as worker_ui


class CleanupPoolTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.FiveAccountRetryTests.asyncSetUp
    asyncTearDown = fixtures.FiveAccountRetryTests.asyncTearDown
    assert_all_slots_released = fixtures.FiveAccountRetryTests.assert_all_slots_released

    async def check_quarantine(self, cancellation=False):
        calls, events = [], []
        context = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError('cannot close browser')))

        async def worker(account, *args, **kwargs):
            calls.append(account)
            if account == 'a':
                try:
                    if cancellation:
                        raise asyncio.CancelledError('user cancelled task')
                    raise ExplicitNotAcceptedError('请确认视频时长')
                finally:
                    await worker_ui._close_submission_context(context)
            kwargs['on_browser_closed']()
            return {'account': account}

        expected = asyncio.CancelledError if cancellation else BrowserCleanupError
        with patch.object(fixtures.pool_module, 'generate_video', worker):
            with self.assertRaises(expected) as caught:
                await self.pool.generate_video('原文', on_event=lambda phase, details: events.append(phase))
            if cancellation:
                self.assertTrue(caught.exception.browser_cleanup_failed)
            self.assertEqual(calls, ['a'], 'an unsafe old browser must stop this task, not rotate')
            self.assertNotIn('account_retry', events)
            self.assertIn('a', self.pool._recovering)
            self.assertEqual(self.pool._unsafe_proxies, {'http://line-a:80'})
            blocked = next(row for row in self.pool.list_accounts() if row['name'] == 'c')
            self.assertFalse(self.pool._schedulable(blocked), 'another account on the same ISP is also unsafe')
            self.assert_all_slots_released()

            result = await self.pool.generate_video('另一条任务')
        self.assertEqual(result['account'], 'b', 'the healthy second ISP remains available')
        self.assertEqual(calls, ['a', 'b'])
        context.close.assert_awaited_once()
        self.assert_all_slots_released()

    async def test_close_failure_stops_rotation_and_quarantines_only_its_isp(self):
        await self.check_quarantine()

    async def test_cancelled_close_failure_keeps_cancellation_and_quarantines_only_its_isp(self):
        await self.check_quarantine(cancellation=True)
