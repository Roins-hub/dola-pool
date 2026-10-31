"""A stalled DOM operation must not multiply the panel's 120-second budget."""
import asyncio
import ast
from pathlib import Path
import time
import unittest
from navigation_errors import BrowserSetupError


class PanelDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def production_wait(self):
        tree = ast.parse(Path('video_worker_ui.py').read_text(encoding='utf-8'))
        nodes = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
                 and n.name == '_bounded_panel_wait']
        self.assertTrue(nodes, 'Panel wait still has only a loop counter, no wall-clock deadline')
        ns = dict(asyncio=asyncio, BrowserSetupError=BrowserSetupError)
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'panel-deadline', 'exec'), ns)
        return ns['_bounded_panel_wait']

    async def test_frozen_dom_operation_stops_with_setup_error(self):
        wait = self.production_wait()
        async def frozen():
            await asyncio.Future()
        start = time.monotonic()
        with self.assertRaisesRegex(BrowserSetupError, '提示词未发送'):
            await wait(frozen(), timeout=.02)
        self.assertLess(time.monotonic()-start, 1)

    async def test_ready_panel_proceeds_and_external_cancellation_is_preserved(self):
        wait = self.production_wait()
        async def ready(): return True
        self.assertTrue(await wait(ready(), timeout=.1))
        async def cancelled(): raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await wait(cancelled(), timeout=.1)

    async def test_unresponsive_abort_cleanup_cannot_extend_the_deadline(self):
        wait = self.production_wait()
        cleanup_release, cleanup_started = asyncio.Event(), asyncio.Event()
        child = None
        async def frozen_with_abort():
            nonlocal child
            child = asyncio.current_task()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                cleanup_started.set()
                # Patchright waits for __abort__ acknowledgement after cancellation.
                while not cleanup_release.is_set():
                    try: await cleanup_release.wait()
                    except asyncio.CancelledError: pass
                raise
        task = asyncio.create_task(wait(frozen_with_abort(), timeout=.02))
        try:
            await cleanup_started.wait()
            done, _ = await asyncio.wait({task}, timeout=.35)
            self.assertIn(task, done, 'Deadline must not wait indefinitely for a frozen __abort__ reply')
            with self.assertRaises(BrowserSetupError): await task
        finally:
            cleanup_release.set()
            await asyncio.gather(task, return_exceptions=True)
            if child: await asyncio.gather(child, return_exceptions=True)

    async def test_external_cancel_returns_without_waiting_for_abort_acknowledgement(self):
        wait = self.production_wait()
        started, cleanup_release = asyncio.Event(), asyncio.Event()
        child = None
        async def frozen():
            nonlocal child
            child = asyncio.current_task()
            started.set()
            try: await asyncio.Future()
            except asyncio.CancelledError:
                await cleanup_release.wait()
                raise
        task = asyncio.create_task(wait(frozen(), timeout=10))
        try:
            await started.wait(); task.cancel()
            done, _ = await asyncio.wait({task}, timeout=.1)
            self.assertIn(task, done)
            with self.assertRaises(asyncio.CancelledError): await task
        finally:
            cleanup_release.set()
            await asyncio.gather(task, return_exceptions=True)
            if child: await asyncio.gather(child, return_exceptions=True)

    async def test_entry_stage_uses_its_actual_timeout_and_never_calls_followup(self):
        wait = self.production_wait()
        following = []
        async def entry(): await asyncio.Future()
        async def pre_submission():
            await wait(entry(), timeout=.02, stage='视频入口')
            following.append('would input prompt')
        with self.assertRaisesRegex(BrowserSetupError, '视频入口加载超过 0.02 秒'):
            await pre_submission()
        self.assertEqual(following, [])
