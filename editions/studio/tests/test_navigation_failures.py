"""Replay observed navigation failures without a browser or network."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from test_video_pipeline import pool_module
import video_worker_ui as worker


class Page:
    def __init__(self, failures):
        self.failures = list(failures)
        self.calls = 0
        self.waits = []

    async def goto(self, *args, **kwargs):
        self.calls += 1
        if self.failures:
            raise RuntimeError(self.failures.pop(0))

    async def wait_for_timeout(self, ms):
        self.waits.append(ms)


async def available(page):
    return None


class NavigationFailuresTests(unittest.IsolatedAsyncioTestCase):
    async def test_authentication_failure_stops_without_retry_or_secret_in_error(self):
        page = Page(['net::ERR_INVALID_AUTH_CREDENTIALS http://name:secret@proxy.test:80'] * 3)
        with self.assertRaises(RuntimeError) as caught:
            await worker.goto_retry(page, 'https://dola.test/chat', tries=3)
        self.assertEqual(page.calls, 1)
        self.assertEqual(page.waits, [])
        self.assertIn('认证失败', str(caught.exception))
        self.assertNotIn('secret', str(caught.exception))

    async def test_closed_browser_stops_without_retry(self):
        page = Page(['Target page, context or browser has been closed'] * 3)
        with self.assertRaises(RuntimeError):
            await worker.goto_retry(page, 'https://dola.test/chat', tries=3)
        self.assertEqual(page.calls, 1)

    async def test_transient_reset_can_recover_with_bounded_retry(self):
        page = Page(['net::ERR_CONNECTION_RESET'])
        with patch.object(worker, '_page_unavailable_reason', available):
            await worker.goto_retry(page, 'https://dola.test/chat', tries=2)
        self.assertEqual(page.calls, 2)

    async def test_persistent_reset_ends_this_browser_after_two_attempts(self):
        from navigation_errors import BrowserSetupError, terminal_setup_reason
        page = Page(['net::ERR_CONNECTION_RESET'] * 5)
        with self.assertRaises(BrowserSetupError) as caught:
            await worker.goto_retry(page, 'https://dola.test/chat', tries=2)
        self.assertEqual(page.calls, 2)
        self.assertIn('结束本次浏览器提交', terminal_setup_reason(caught.exception))


class BrowserCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_close_completes_before_cleanup_returns(self):
        context = type('Context', (), {'close': AsyncMock()})()
        await worker._close_submission_context(context)
        context.close.assert_awaited_once()

    async def test_context_close_failure_is_typed_and_hides_credentials(self):
        from navigation_errors import BrowserCleanupError
        context = type('Context', (), {'close': AsyncMock(
            side_effect=RuntimeError('http://name:secret@proxy.test'))})()
        with self.assertRaises(BrowserCleanupError) as caught:
            await worker._close_submission_context(context)
        self.assertNotIn('secret', str(caught.exception))

    async def test_context_close_timeout_is_bounded_without_waiting_for_cancel_ack(self):
        from navigation_errors import BrowserCleanupError
        context = type('Context', (), {'close': AsyncMock(side_effect=asyncio.Event().wait)})()
        async def expired(tasks, **kwargs):
            await asyncio.sleep(0)
            return set(), tasks
        with patch.object(worker.asyncio, 'wait', expired):
            with self.assertRaises(BrowserCleanupError):
                await worker._close_submission_context(context, timeout=15)

    async def test_external_cancellation_is_preserved_after_successful_close(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def close():
            started.set()
            await release.wait()
        context = type('Context', (), {'close': AsyncMock(side_effect=close)})()
        task = asyncio.create_task(worker._close_submission_context(context))
        await started.wait()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError) as caught:
            await task
        self.assertFalse(getattr(caught.exception, 'browser_cleanup_failed', False))
        context.close.assert_awaited_once()

    async def test_cancelled_cleanup_timeout_preserves_cancel_and_marks_unsafe(self):
        context = type('Context', (), {'close': AsyncMock(side_effect=asyncio.Event().wait)})()
        with patch.object(worker.asyncio, 'wait', AsyncMock(side_effect=[
            asyncio.CancelledError(), (set(), set()),
        ])):
            with self.assertRaises(asyncio.CancelledError) as caught:
                await worker._close_submission_context(context)
        self.assertTrue(caught.exception.browser_cleanup_failed)

    async def test_cleanup_failure_during_original_cancellation_still_propagates_cancellation(self):
        context = type('Context', (), {'close': AsyncMock(side_effect=RuntimeError('closed failed'))})()
        async def cancelled_attempt():
            try:
                raise asyncio.CancelledError('user cancelled task')
            finally:
                await worker._close_submission_context(context)
        with self.assertRaises(asyncio.CancelledError) as caught:
            await cancelled_attempt()
        self.assertEqual(str(caught.exception), 'user cancelled task')
        self.assertTrue(caught.exception.browser_cleanup_failed)

    async def test_driver_exit_failure_is_typed_after_context_scope(self):
        from navigation_errors import BrowserCleanupError
        manager = type('Manager', (), {
            '__aenter__': AsyncMock(return_value='driver'),
            '__aexit__': AsyncMock(side_effect=RuntimeError('driver unavailable')),
        })()
        with patch.object(worker, 'async_playwright', return_value=manager):
            with self.assertRaises(BrowserCleanupError):
                async with worker._submission_playwright() as driver:
                    self.assertEqual(driver, 'driver')
        manager.__aexit__.assert_awaited_once()

    async def test_driver_exit_timeout_is_bounded(self):
        from navigation_errors import BrowserCleanupError
        async def hanging_exit(*args):
            await asyncio.Event().wait()
        manager = type('Manager', (), {
            '__aenter__': AsyncMock(return_value='driver'),
            '__aexit__': AsyncMock(side_effect=hanging_exit),
        })()
        async def expired(tasks, **kwargs):
            await asyncio.sleep(0)
            return set(), tasks
        with patch.object(worker, 'async_playwright', return_value=manager):
            with patch.object(worker.asyncio, 'wait', expired):
                with self.assertRaises(BrowserCleanupError) as caught:
                    async with worker._submission_playwright():
                        pass
        self.assertIn('TimeoutError', str(caught.exception))

    async def test_external_cancel_during_driver_exit_keeps_prior_unsafe_browser_marker(self):
        from navigation_errors import BrowserCleanupError
        started, release = asyncio.Event(), asyncio.Event()
        async def exit_driver(*args):
            started.set()
            await release.wait()
        manager = type('Manager', (), {
            '__aenter__': AsyncMock(return_value='driver'),
            '__aexit__': AsyncMock(side_effect=exit_driver),
        })()
        async def attempt():
            async with worker._submission_playwright():
                raise BrowserCleanupError('browser close failed')
        with patch.object(worker, 'async_playwright', return_value=manager):
            task = asyncio.create_task(attempt())
            await started.wait()
            task.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await task
        self.assertTrue(caught.exception.browser_cleanup_failed)
