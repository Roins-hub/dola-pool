"""Entry loading may retry once; a frozen renderer must still stop before send."""
import ast
import asyncio
import io
from contextlib import redirect_stdout
from pathlib import Path
import re
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from navigation_errors import BrowserSetupError, terminal_setup_reason


class Entry:
    async def is_visible(self): return True
    async def bounding_box(self): return {"y": 500}


class EntryLocator:
    def __init__(self, page): self.page = page

    async def count(self):
        if self.page.frozen:
            await asyncio.Future()
        await asyncio.sleep(.02)
        return int(self.page.ready)

    def nth(self, index): return self.page.entry


class Page:
    def __init__(self, ready_after_reload=False, frozen=False):
        self.entry = Entry()
        self.ready = False
        self.ready_after_reload = ready_after_reload
        self.frozen = frozen
        self.reloads = 0
        self.screenshots = 0
        self.mouse = SimpleNamespace(wheel=AsyncMock())

    def get_by_text(self, *_args, **_kwargs): return EntryLocator(self)

    async def wait_for_timeout(self, ms):
        # The fixed post-reload settlement is external browser time; only the
        # actual production polling interval participates in this replay.
        if ms <= 1000:
            await asyncio.sleep(ms / 1000)

    async def wait_for_load_state(self, *_args, **_kwargs): pass

    async def reload(self, **_kwargs):
        self.reloads += 1
        self.ready = self.ready_after_reload

    async def screenshot(self, **_kwargs): self.screenshots += 1


def production_entry_flow(page, clock=time):
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'video_worker_ui.py').read_text(encoding='utf-8'))
    names = {'_bounded_panel_wait', 'PageUnavailableError'}
    nodes = [node for node in tree.body if isinstance(node, (ast.AsyncFunctionDef, ast.ClassDef))
             and node.name in names]
    body = next(node.body for node in ast.walk(tree)
                if isinstance(node, ast.Try) and any(isinstance(child, ast.AsyncFunctionDef)
                    and child.name == '_find_entry' for child in node.body))
    start = next(i for i, node in enumerate(body) if isinstance(node, ast.AsyncFunctionDef)
                 and node.name == '_find_entry')
    end = next(i for i, node in enumerate(body[start:], start)
               if isinstance(node, ast.Expr) and isinstance(node.value, ast.Await)
               and isinstance(node.value.value, ast.Call) and isinstance(node.value.value.func, ast.Attribute)
               and isinstance(node.value.value.func.value, ast.Name)
               and node.value.value.func.value.id == 'btn' and node.value.value.func.attr == 'click')
    replay = ast.AsyncFunctionDef(name='entry_flow', args=ast.arguments(posonlyargs=[], args=[],
        kwonlyargs=[], kw_defaults=[], defaults=[]), body=body[start:end] +
        [ast.Return(value=ast.Name(id='btn', ctx=ast.Load()))], decorator_list=[])
    async def composer_entry(target):
        # The lightweight page controls entry availability and DOM latency;
        # production geometry is exercised separately in the real-browser suite.
        entries = target.get_by_text(re.compile('Videos erstellen'), exact=True)
        for index in range(await entries.count()):
            entry = entries.nth(index)
            if await entry.is_visible():
                return entry
        return None
    namespace = dict(asyncio=asyncio, time=clock, BrowserSetupError=BrowserSetupError,
        terminal_setup_reason=terminal_setup_reason, page=page, account='fixture',
        config=SimpleNamespace(ENTRY_WAIT_SECONDS=1), VIDEO_BTN=re.compile('Videos erstellen'),
        _handle_birthday_modal=AsyncMock(), _dismiss_login_modal=AsyncMock(),
        _dismiss_cookie_banner=AsyncMock(), _page_unavailable_reason=AsyncMock(return_value=None),
        goto_retry=AsyncMock(), _ensure_new_chat=AsyncMock(),
        _find_composer_video_entry=composer_entry)
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes + [replay, body[start]],
        type_ignores=[])), 'entry-readiness-flow', 'exec'), namespace)
    return namespace


class EntryReadinessDeadlineTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_entry_reloads_once_and_uses_late_entry(self):
        # The previous equal inner/outer deadlines aborted before reload.
        page = Page(ready_after_reload=True)
        ns = production_entry_flow(page)
        try:
            with redirect_stdout(io.StringIO()):
                result = await ns['entry_flow']()
        except BrowserSetupError as exc:
            self.fail(f'Missing entry must reach its single reload before giving up: {exc}')
        self.assertIs(result, page.entry)
        self.assertEqual(page.reloads, 1)

    async def test_missing_entry_after_retry_stops_without_third_load(self):
        page = Page()
        ns = production_entry_flow(page)
        with redirect_stdout(io.StringIO()):
            with self.assertRaises(BrowserSetupError):
                await ns['entry_flow']()
        self.assertEqual(page.reloads, 1)
        self.assertEqual(page.screenshots, 1)

    async def test_frozen_dom_stops_without_reloading_unresponsive_page(self):
        page = Page(frozen=True)
        ns = production_entry_flow(page)
        started = time.monotonic()
        with self.assertRaisesRegex(BrowserSetupError, '提示词未发送'):
            await ns['entry_flow']()
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(page.reloads, 0)

    async def test_dom_work_counts_toward_entry_deadline(self):
        # A controlled clock avoids wall-clock tolerances: each absent-entry
        # DOM lookup costs .4 s and each requested sleep advances by its value.
        clock = SimpleNamespace(now=0.)
        clock.monotonic = lambda: clock.now
        page = Page()
        class SlowLocator:
            async def count(self):
                clock.now += .4
                return 0
        page.get_by_text = lambda *_args, **_kwargs: SlowLocator()
        async def sleep(ms): clock.now += ms / 1000
        page.wait_for_timeout = sleep
        ns = production_entry_flow(page, clock)
        self.assertIsNone(await ns['_find_entry'](max_seconds=3))
        self.assertLessEqual(clock.now, 3.4,
            'DOM work must consume the same total wait budget, not extend every poll')


if __name__ == '__main__': unittest.main()
