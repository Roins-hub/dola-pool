"""A responsive partial composer may reload once before any prompt is sent."""
import ast
import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

from test_video_pipeline import pool_module
import video_worker_ui as worker
from navigation_errors import BrowserSetupError


def panel_flow(page, wait_panel, already_reloaded=False):
    tree=ast.parse(Path('video_worker_ui.py').read_text(encoding='utf-8'))
    def waits_for_panel(node):
        return isinstance(node,ast.Try) and any(
            isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='panel_ready' for t in n.targets)
            for n in node.body)
    body=next(n.body for n in ast.walk(tree) if isinstance(n,ast.Try) and any(waits_for_panel(x) for x in n.body))
    start=next(i for i,n in enumerate(body) if waits_for_panel(n))
    end=next(i+1 for i,n in enumerate(body[start:],start) if isinstance(n,ast.If)
             and isinstance(n.test,ast.UnaryOp) and isinstance(n.test.operand,ast.Name)
             and n.test.operand.id=='panel_ready')
    replay=ast.AsyncFunctionDef(name='run',args=ast.arguments(posonlyargs=[],args=[ast.arg(arg='page_reloaded')],
        kwonlyargs=[],kw_defaults=[],defaults=[ast.Constant(value=already_reloaded)]),
        body=body[start:end]+[ast.Return(ast.Name(id='panel_ready',ctx=ast.Load()))],decorator_list=[])
    namespace=dict(vars(worker))
    namespace.update(page=page,account='fixture',_wait_panel=wait_panel,page_reloaded=already_reloaded,
                     _find_entry=AsyncMock(return_value=page.entry),_ensure_new_chat=AsyncMock())
    real_wait=worker._bounded_panel_wait
    async def bounded(awaitable,timeout=120,stage='视频面板'):
        return await real_wait(awaitable,timeout=min(timeout,.03),stage=stage)
    namespace['_bounded_panel_wait']=bounded
    exec(compile(ast.fix_missing_locations(ast.Module(body=[replay],type_ignores=[])),
                 'production-panel-recovery','exec'),namespace)
    return namespace['run']


class Page:
    def __init__(self):
        self.reloaded=False
        async def reload(**kwargs):self.reloaded=True
        self.reload=AsyncMock(side_effect=reload)
        self.screenshot=AsyncMock()
        self.entry=type('Entry',(),{'click':AsyncMock()})()


class PanelRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_composer_reloads_once_and_opens_before_prompt_input(self):
        page=Page()
        async def wait_panel(**kwargs):return page.reloaded
        try:
            self.assertTrue(await panel_flow(page,wait_panel)())
        except BrowserSetupError as error:
            self.fail(f'A responsive partial page must reach its single reload: {error}')
        self.assertEqual(page.reload.await_count,1)
        self.assertEqual(page.entry.click.await_count,1)

    async def test_second_partial_load_stops_without_a_second_reload(self):
        page=Page()
        async def wait_panel(**kwargs):return False
        with self.assertRaises(BrowserSetupError):await panel_flow(page,wait_panel)()
        self.assertEqual(page.reload.await_count,1)

    async def test_ready_panel_is_never_reloaded(self):
        page=Page()
        async def wait_panel(**kwargs):return True
        self.assertTrue(await panel_flow(page,wait_panel)())
        page.reload.assert_not_awaited()

    async def test_frozen_dom_is_not_reloaded_or_clicked_again(self):
        page=Page()
        async def wait_panel(**kwargs):await asyncio.Future()
        with self.assertRaises(BrowserSetupError):await panel_flow(page,wait_panel)()
        page.reload.assert_not_awaited()
        page.entry.click.assert_not_awaited()

    async def test_entry_recovery_already_used_the_single_reload(self):
        page=Page()
        async def wait_panel(**kwargs):return False
        with self.assertRaises(BrowserSetupError):await panel_flow(page,wait_panel,already_reloaded=True)()
        page.reload.assert_not_awaited()
