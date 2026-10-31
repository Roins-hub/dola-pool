"""Cross-platform leases exercise real pools and scheduler, with no network calls."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_five_account_retry import pool_module as pm
import browser
from navigation_errors import BrowserCleanupError
from shared_scheduler import SharedScheduler


class SharedSchedulerTests(unittest.IsolatedAsyncioTestCase):
    def scheduler(self, **kwargs):
        return SharedScheduler(**kwargs)

    async def test_credentials_and_protocol_do_not_split_one_physical_line(self):
        scheduler = self.scheduler()
        first = scheduler.lane_slot('http://alice:secret@ISP.EXAMPLE:11000')
        second = scheduler.lane_slot({'server': 'socks5://isp.example:11000',
                                      'username': 'bob', 'password': 'other'})
        self.assertIs(first, second)
        self.assertNotIn('secret', ' '.join(scheduler.proxy_slots))
        self.assertNotIn('alice', ' '.join(scheduler.proxy_slots))
        self.assertIsNot(first, scheduler.lane_slot('http://isp.example:11001'))

    async def test_explicit_lane_aliases_group_different_endpoints(self):
        scheduler = self.scheduler(lanes={'http://isp.example:11000': 'uk-one',
                                          'socks5://isp.example:22000': 'uk-one'})
        self.assertIs(scheduler.lane_slot('http://a:password@isp.example:11000'),
                      scheduler.lane_slot('socks5://isp.example:22000'))
        self.assertIs(scheduler.lane_slot('http://other.example:80', lane_id='uk-one'),
                      scheduler.lane_slot('http://isp.example:11000'))

    async def test_same_account_name_is_separate_between_platforms(self):
        scheduler = self.scheduler()
        self.assertIsNot(scheduler.account_lock('dola', 'one'),
                         scheduler.account_lock('doubao', 'one'))
        self.assertIs(scheduler.account_lock('dola', 'one'),
                      scheduler.account_locks('dola')['one'])

    async def test_doubao_cap_does_not_reserve_global_slots_while_waiting(self):
        scheduler = self.scheduler(max_browsers=5, max_inflight=5)
        entered = asyncio.Event()
        async with scheduler.task_slot('doubao'):
            async def queued():
                async with scheduler.task_slot('doubao'):
                    entered.set()
            pending = asyncio.create_task(queued())
            try:
                await asyncio.sleep(.02)
                async with scheduler.task_slot('dola'):
                    self.assertFalse(entered.is_set())
                    self.assertEqual(scheduler.inflight._value, 3)
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            finally:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        self.assertEqual(scheduler.inflight._value, 5)
        async with scheduler.task_slot('doubao'):
            pass

    async def test_cancelled_diagnostic_releases_partially_acquired_account(self):
        scheduler = self.scheduler(max_browsers=1, max_inflight=1)
        lane = scheduler.lane_slot('http://isp.example:80')
        await lane.acquire()
        pending = asyncio.create_task(scheduler.diagnostic('dola', 'one',
                                                           'http://isp.example:80').__aenter__())
        await asyncio.sleep(.02)
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)
        self.assertFalse(scheduler.account_lock('dola', 'one').locked())
        self.assertEqual(scheduler.browser_slots._value, 1)
        lane.release()

    async def test_close_failure_quarantines_lane_instead_of_reusing_permits(self):
        scheduler = self.scheduler(max_browsers=1, max_inflight=1)
        lease = scheduler.diagnostic('dola', 'one', 'http://isp.example:80')
        await lease.__aenter__()
        lease.quarantine()
        await lease.__aexit__(None, None, None)
        self.assertIn(scheduler.lane_id('http://isp.example:80'), scheduler.unsafe_lanes)
        self.assertEqual(scheduler.browser_slots._value, 0)
        with self.assertRaisesRegex(RuntimeError, '关闭|隔离|quarantin'):
            async with scheduler.diagnostic('doubao', 'two', 'http://isp.example:80'):
                self.fail('Unsafe line must not be opened again')
        lease.release()  # A later confirmed close safely returns resources.
        self.assertEqual(scheduler.browser_slots._value, 1)

    async def test_recovery_reservation_blocks_other_platform_diagnostics(self):
        scheduler = self.scheduler()
        scheduler.reserve_recovery('dola', 'one', 'http://isp.example:80')
        with self.assertRaisesRegex(RuntimeError, '恢复|recovery'):
            async with scheduler.diagnostic('doubao', 'two', 'http://isp.example:80'):
                self.fail('Recovery has first claim on the original ISP')
        scheduler.release_recovery('dola', 'one')
        async with scheduler.diagnostic('doubao', 'two', 'http://isp.example:80'):
            pass

    async def test_reserved_recovery_cannot_be_starved_by_new_waiting_tasks(self):
        scheduler = self.scheduler(max_inflight=1)
        scheduler.reserve_recovery('dola', 'one', 'http://isp.example:80')
        new_started = asyncio.Event()
        async def new_task():
            async with scheduler.task_slot('doubao'):
                new_started.set()
        pending = asyncio.create_task(new_task())
        try:
            await asyncio.sleep(.02)
            async with scheduler.task_slot('dola', recovery=True):
                self.assertFalse(new_started.is_set())
                scheduler.release_recovery('dola', 'one')
            await asyncio.wait_for(pending, .2)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_continuous_dola_queue_gives_doubao_next_global_turn(self):
        scheduler = self.scheduler(max_inflight=1)
        order = []
        async def task(platform, label):
            async with scheduler.task_slot(platform):
                order.append(label)
                await asyncio.sleep(.001)
        async with scheduler.task_slot('dola'):
            pending = [asyncio.create_task(task('dola', f'dola-{n}')) for n in range(3)]
            pending.append(asyncio.create_task(task('doubao', 'doubao')))
            await asyncio.sleep(.02)
        await asyncio.wait_for(asyncio.gather(*pending), .5)
        self.assertEqual(order[0], 'doubao')

    async def test_doubao_recovery_can_enter_ahead_of_its_own_queued_new_task(self):
        scheduler = self.scheduler(max_inflight=1)
        scheduler.reserve_recovery('doubao', 'old', 'http://isp.example:80')
        async def fresh():
            async with scheduler.task_slot('doubao'):
                pass
        async def recover():
            async with scheduler.task_slot('doubao', recovery=True):
                scheduler.release_recovery('doubao', 'old')
        pending = asyncio.create_task(fresh())
        try:
            await asyncio.sleep(.02)
            await asyncio.wait_for(recover(), .3)
            await asyncio.wait_for(pending, .3)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_shared_pool_keeps_isp_until_polling_ends_and_wakes_other_pool(self):
        scheduler = self.scheduler(max_browsers=1, max_inflight=5)
        with tempfile.TemporaryDirectory() as root:
            pools = []
            for platform in ('dola', 'doubao'):
                directory = Path(root) / platform
                (directory / 'same-name').mkdir(parents=True)
                pool = pm.BrowserPool(directory, ':memory:', max_concurrency=5,
                                      scheduler=scheduler, platform=platform)
                pool._proxy_state = lambda name: {'proxy_server': 'http://isp.example:11000',
                                                  'proxy_blocked': False}
                pools.append(pool)
            first_started, finish_polling, second_started = (asyncio.Event() for _ in range(3))
            calls = 0
            async def worker(account, *args, **kwargs):
                nonlocal calls
                calls += 1
                kwargs['on_prompt_sent'](account)
                kwargs['on_browser_closed']()
                if calls == 1:
                    first_started.set()
                    await finish_polling.wait()
                else:
                    second_started.set()
                return {'account': account}
            with patch.object(pm, 'generate_video', worker):
                first = asyncio.create_task(pools[0].generate_video('first'))
                second = None
                try:
                    await asyncio.wait_for(first_started.wait(), 1)
                    self.assertEqual(scheduler.browser_slots._value, 1)
                    second = asyncio.create_task(pools[1].generate_video('second'))
                    await asyncio.sleep(.03)
                    self.assertFalse(second_started.is_set())
                    finish_polling.set()
                    await asyncio.wait_for(asyncio.gather(first, second), 1)
                    self.assertTrue(second_started.is_set())
                    self.assertEqual(scheduler.browser_slots._value, 1)
                    self.assertEqual(scheduler.inflight._value, 5)
                finally:
                    first.cancel()
                    if second:
                        second.cancel()
                    await asyncio.gather(first, *([second] if second else []), return_exceptions=True)
                    for pool in pools:
                        pool._conn.close()

    async def test_manual_browser_holds_shared_lane_and_releases_on_confirmed_close(self):
        scheduler = self.scheduler(max_browsers=1)
        class Context:
            def __init__(self):
                self.closed = False
                self.handlers = []
            def on(self, event, callback):
                self.handlers.append(callback)
            async def close(self):
                self.closed = True
                for handler in self.handlers:
                    handler(self)
        context = Context()
        async def launch(*args, **kwargs):
            return context
        with patch.object(browser, '_shared_scheduler', scheduler, create=True), \
             patch.object(browser, '_launch_account_context', launch, create=True), \
             patch.object(browser.config, 'proxy_for', return_value={'server': 'http://isp.example:80'}):
            opened = await browser.launch_account_context(None, 'one')
            self.assertIs(opened, context)
            self.assertEqual(scheduler.browser_slots._value, 0)
            self.assertTrue(scheduler.account_lock('dola', 'one').locked())
            await opened.close()
            self.assertEqual(scheduler.browser_slots._value, 1)
            self.assertFalse(scheduler.account_lock('dola', 'one').locked())
            self.assertEqual(scheduler.lane_slot('http://isp.example:80')._value, 1)

    async def test_manual_browser_close_failure_preserves_lease(self):
        scheduler = self.scheduler(max_browsers=1)
        class Context:
            def on(self, *args):
                pass
            async def close(self):
                raise RuntimeError('browser did not close')
        async def launch(*args, **kwargs):
            return Context()
        with patch.object(browser, '_shared_scheduler', scheduler, create=True), \
             patch.object(browser, '_launch_account_context', launch, create=True), \
             patch.object(browser.config, 'proxy_for', return_value={'server': 'http://isp.example:80'}):
            opened = await browser.launch_account_context(None, 'one')
            with self.assertRaises(RuntimeError):
                await opened.close()
            self.assertEqual(scheduler.browser_slots._value, 0)
            self.assertEqual(scheduler.lane_slot('http://isp.example:80')._value, 0)
            self.assertIn(scheduler.lane_id('http://isp.example:80'), scheduler.unsafe_lanes)

    async def test_generation_owned_browser_does_not_reacquire_same_lane(self):
        scheduler = self.scheduler(max_browsers=1)
        marker = object()
        async def launch(*args, **kwargs):
            return marker
        with patch.object(browser, '_shared_scheduler', scheduler, create=True), \
             patch.object(browser, '_launch_account_context', launch, create=True):
            async with scheduler.diagnostic('dola', 'one', 'http://isp.example:80'):
                with scheduler.managed_account('dola', 'one'):
                    result = await asyncio.wait_for(browser.launch_account_context(None, 'one'), .2)
                self.assertIs(result, marker)

    async def test_pool_cleanup_failure_blocks_other_platform_and_retains_browser_capacity(self):
        scheduler = self.scheduler(max_browsers=1)
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'one').mkdir()
            pool = pm.BrowserPool(root, ':memory:', scheduler=scheduler)
            pool._proxy_state = lambda name: {'proxy_server': 'http://isp.example:80', 'proxy_blocked': False}
            async def worker(*args, **kwargs):
                raise BrowserCleanupError('unconfirmed browser close')
            try:
                with patch.object(pm, 'generate_video', worker):
                    with self.assertRaises(BrowserCleanupError):
                        await pool.generate_video('fixture')
                self.assertEqual(scheduler.browser_slots._value, 0)
                self.assertEqual(scheduler.lane_slot('http://isp.example:80')._value, 0)
                with self.assertRaisesRegex(RuntimeError, '关闭|隔离'):
                    async with scheduler.diagnostic('doubao', 'other', 'http://isp.example:80'):
                        self.fail('Both platforms must respect quarantine')
            finally:
                pool._conn.close()

    async def test_http_login_probe_waits_for_other_platform_on_same_isp(self):
        scheduler = self.scheduler(max_browsers=1)
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'one').mkdir()
            pool = pm.BrowserPool(root, ':memory:', scheduler=scheduler)
            pool._proxy_state = lambda name: {'proxy_server': 'http://isp.example:80', 'proxy_blocked': False}
            probed = asyncio.Event()
            async def check(account):
                probed.set()
                return True
            with patch.object(browser, 'check_login_state', check):
                async with scheduler.diagnostic('doubao', 'other', 'http://isp.example:80'):
                    pending = asyncio.create_task(pool.verify_account('one'))
                    await asyncio.sleep(.02)
                    self.assertFalse(probed.is_set())
                try:
                    self.assertTrue(await asyncio.wait_for(pending, .3))
                    self.assertTrue(probed.is_set())
                    self.assertEqual(scheduler.browser_slots._value, 1)
                finally:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
                    pool._conn.close()

    async def test_missing_recovery_proxy_does_not_prevent_service_startup(self):
        scheduler = self.scheduler()
        with tempfile.TemporaryDirectory() as root:
            pool = pm.BrowserPool(root, ':memory:', scheduler=scheduler)
            try:
                with patch.object(pool, '_proxy_state', return_value={}), \
                     patch.object(pm.config, 'proxy_for', side_effect=ValueError('ISP binding missing')):
                    pool.reserve_recoveries([{'account': 'removed-account'}])
                self.assertIn('removed-account', pool._recovering)
                async with scheduler.task_slot('doubao'):
                    pass
            finally:
                pool._conn.close()


if __name__ == '__main__':
    unittest.main()
