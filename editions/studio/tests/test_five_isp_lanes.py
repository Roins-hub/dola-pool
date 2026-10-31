"""Each physical ISP owns one video until completion, including recovery."""
import asyncio
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from test_five_account_retry import pool_module as pm


class FiveISPLaneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in 'abcdefghij':
            (self.root / name).mkdir()
        self.config = patch.object(pm.config, 'MAX_INFLIGHT', 5)
        self.config.start()
        self.pool = pm.BrowserPool(self.root, ':memory:', max_concurrency=5)
        self.pool._proxy_state = lambda name: {
            'proxy_server': f'http://line-{(ord(name)-ord("a")) % 5}:80', 'proxy_blocked': False}
        self.pool.accounts
        self.jobs = []
        self.started = []
        self.gates = {name: asyncio.Event() for name in 'abcdefghij'}

    async def asyncTearDown(self):
        for job in self.jobs:
            job.cancel()
        await asyncio.gather(*self.jobs, return_exceptions=True)
        self.pool._conn.close()
        self.config.stop()
        self.tmp.cleanup()

    def spawn(self, coroutine):
        job = asyncio.create_task(coroutine)
        self.jobs.append(job)
        return job

    async def until(self, condition):
        async def wait():
            while not condition():
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait(), 1)

    async def worker(self, account, *args, **callbacks):
        self.started.append(account)
        callbacks['on_prompt_sent'](account)
        callbacks['on_conversation_id'](account, 'conv-'+account, time.time()+60)
        callbacks['on_browser_closed']()
        await self.gates[account].wait()
        return {'account': account}

    def enable_only(self, names):
        for name in 'abcdefghij':
            self.pool.set_scheduling(name, name in names)

    async def test_five_distinct_lines_start_before_any_finishes_then_refill_one_free_line(self):
        with patch.object(pm, 'generate_video', self.worker):
            for _ in range(10):
                self.spawn(self.pool.generate_video('fixture'))
            await self.until(lambda: len(self.started) == 5)
            self.assertEqual(set(self.started), set('abcde'))
            self.assertEqual(self.pool.inflight._value, 0)
            self.assertTrue(all(slot.locked() for slot in self.pool._proxy_slots.values()),
                            'Closing submission browsers must not free generating ISP lanes')
            self.gates['c'].set()
            await self.until(lambda: len(self.started) == 6)
            self.assertEqual(self.started[-1], 'h', 'Only the completed task\'s ISP may start another')
            await asyncio.sleep(.04)
            self.assertEqual(len(self.started), 6)

    async def test_same_isp_cannot_start_another_account_when_only_browser_closes(self):
        self.enable_only('af')
        with patch.object(pm, 'generate_video', self.worker):
            first = self.spawn(self.pool.generate_video('one'))
            await self.until(lambda: self.started == ['a'])
            self.spawn(self.pool.generate_video('two'))
            await asyncio.sleep(.05)
            self.assertEqual(self.started, ['a'])
            self.assertEqual(self.pool.semaphore._value, 5, 'Browser permit may be released early')
            self.gates['a'].set()
            await first
            await self.until(lambda: len(self.started) == 2)
            self.assertEqual(self.started, ['a', 'f'])

    async def test_recovered_video_reserves_its_original_line_until_completion(self):
        self.enable_only('af')
        resumed = asyncio.Event()
        async def resume(account, *args, **kwargs):
            resumed.set()
            await self.gates[account].wait()
            return {'account': account}
        self.pool.reserve_recoveries([{'account': 'a'}])
        with patch.object(pm, 'resume_video', resume), patch.object(pm, 'generate_video', self.worker):
            recovery = self.spawn(self.pool.resume_video('a', 'original', 60, accepted=True))
            await resumed.wait()
            self.spawn(self.pool.generate_video('new'))
            await asyncio.sleep(.05)
            self.assertEqual(self.started, [], 'Recovery must protect the entire ISP, not only one account')
            self.gates['a'].set()
            await recovery
            await self.until(lambda: len(self.started) == 1)

    async def test_pending_recovery_blocks_sibling_account_before_polling_starts(self):
        self.enable_only('af')
        self.pool.reserve_recoveries([{'account': 'a'}])
        with patch.object(pm, 'generate_video', self.worker):
            self.spawn(self.pool.generate_video('new'))
            await asyncio.sleep(.05)
            self.assertEqual(self.started, [], 'Queued recovery has first claim on its original ISP')
            self.pool._recovering.clear()
            self.pool._available.set()
            await self.until(lambda: len(self.started) == 1)

    async def test_cancellation_releases_generating_line_for_next_account(self):
        self.enable_only('af')
        with patch.object(pm, 'generate_video', self.worker):
            first = self.spawn(self.pool.generate_video('one'))
            await self.until(lambda: self.started == ['a'])
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)
            self.spawn(self.pool.generate_video('two'))
            await self.until(lambda: len(self.started) == 2)
            self.assertEqual(self.started, ['a', 'f'])


if __name__ == '__main__':
    unittest.main()
