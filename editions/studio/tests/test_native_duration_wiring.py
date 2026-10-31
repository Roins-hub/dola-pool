"""Exercise UI worker launch configuration without opening accounts or spending credits."""
import ast
import copy
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_video_pipeline import pool_module  # Isolate the imported ISP-usage database.
import browser
import video_worker_ui as worker


def actual_worker_browser_launch():
    """Run production validation and launch, then stop before any navigation."""
    tree = ast.parse(Path(worker.__file__).read_text(encoding='utf-8'))
    function = copy.deepcopy(next(node for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == 'generate_video'))
    for index, block in enumerate(function.body):
        if not isinstance(block, ast.AsyncWith):
            continue
        for launch_index, statement in enumerate(block.body):
            if not (isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Await)):
                continue
            call = statement.value.value
            if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                    and call.func.id == 'launch_account_context'):
                block.body = block.body[:launch_index + 1] + ast.parse('return context').body
                function.body = function.body[:index + 1]
                namespace = dict(vars(worker))
                exec(compile(ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[])),
                             'actual-worker-browser-launch', 'exec'), namespace)
                return namespace['generate_video']
    raise AssertionError('Cannot locate the production browser launch boundary')


@contextmanager
def directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class Context:
    def __init__(self):
        self.scripts, self.routes = [], []
    async def add_init_script(self, script):
        self.scripts.append(script)
    async def route(self, *args):
        self.routes.append(args)


class Playwright:
    def __init__(self):
        self.chromium = self
        self.context = Context()
        self.launches = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        pass
    async def launch_persistent_context(self, profile, **kwargs):
        self.launches.append((profile, kwargs))
        return self.context


class NativeDurationWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_seedance_one_actual_worker_launches_native_model_and_never_old_extension(self):
        for model, duration in (('seedance-1.0', 5), ('ic_mini', 10), ('ic_mini', 15), ('seedance_v1.0', None)):
            with self.subTest(model=model, duration=duration):
                launched = await self.launch(model, duration)
                expected_duration = 10 if duration is None else duration
                self.assertEqual(len(launched.context.scripts), 1)
                self.assertIn('__DOLA_NATIVE_MODEL_TARGET__ = "ic_mini"', launched.context.scripts[0])
                self.assertIn(f'__DOLA_NATIVE_DURATION_TARGET__ = {expected_duration}', launched.context.scripts[0])
                args = launched.launches[0][1]['args']
                self.assertIn('--disable-extensions', args)
                self.assertFalse(any('load-extension' in arg for arg in args))
                self.assertEqual(launched.context.routes, [])

    async def test_seedance_one_actual_worker_rejects_thirty_before_browser_launch(self):
        for duration in (30,):
            with self.assertRaisesRegex(ValueError, r'Seedance 1\.0.*5.*10'):
                await actual_worker_browser_launch()('fixture', '原文', model='ic_mini', duration=duration)

    async def test_seedance_one_browser_configuration_uses_ic_mini_without_legacy_request_rewriter(self):
        for duration in (5, 10, 15):
            with self.subTest(duration=duration):
                playwright = Playwright()
                with tempfile.TemporaryDirectory() as root, directory(root):
                    Path('accounts/fixture').mkdir(parents=True)
                    with patch.object(browser.config, 'proxy_for', return_value=None), \
                         patch.object(browser.config, 'account_proxy_raw', return_value=('', 'default')):
                        await browser.launch_account_context(playwright, 'fixture', native_model='seedance-1.0',
                                                             native_duration=duration, use_extension=True)
                self.assertEqual(len(playwright.context.scripts), 1)
                self.assertIn('__DOLA_NATIVE_MODEL_TARGET__ = "ic_mini"', playwright.context.scripts[0])
                self.assertIn(f'__DOLA_NATIVE_DURATION_TARGET__ = {duration}', playwright.context.scripts[0])
                self.assertIn('--disable-extensions', playwright.launches[0][1]['args'])
                self.assertEqual(playwright.context.routes, [])

    async def test_seedance_one_rejects_long_duration_before_launching_browser(self):
        for duration in (30,):
            playwright = Playwright()
            with self.assertRaisesRegex(ValueError, r'Seedance 1\.0.*5.*10'):
                await browser.launch_account_context(playwright, 'fixture', native_model='ic_mini', native_duration=duration)
            self.assertEqual(playwright.launches, [])

    async def test_25_short_durations_restore_missing_model_before_navigation(self):
        for duration in (5, 10):
            with self.subTest(duration=duration):
                launched = await self.launch('seedance-2.5', duration)
                self.assertEqual(len(launched.context.scripts), 1)
                self.assertRegex(launched.context.scripts[0],
                                 rf'globalThis\.__DOLA_NATIVE_DURATION_TARGET__\s*=\s*{duration}\s*;')
                self.assertIn('--disable-extensions', launched.launches[0][1]['args'])
                self.assertEqual(launched.context.routes, [])

    async def launch(self, model, duration):
        playwright = Playwright()
        # The real launcher gets an empty throwaway profile. No account data,
        # browser engine, request transport, or real proxy is used by these tests.
        with tempfile.TemporaryDirectory() as root, directory(root):
            Path('accounts/fixture').mkdir(parents=True)
            Path('fixture-extension').mkdir()
            with patch.object(worker, '_submission_playwright', return_value=playwright), \
                 patch.object(browser.config, 'proxy_for', return_value=None), \
                 patch.object(browser.config, 'account_proxy_raw', return_value=('', 'default')), \
                 patch.object(browser.config, 'EXTENSION_ENABLED', True), \
                 patch.object(browser.config, 'EXTENSION_DIR', 'fixture-extension'):
                result = await actual_worker_browser_launch()(
                    'fixture', '完整原提示词', model=model, duration=duration, use_extension=True)
        self.assertIs(result, playwright.context)
        self.assertEqual(len(playwright.launches), 1)
        return playwright

    async def test_25_15_configures_ui_before_navigation_without_old_10_second_extension(self):
        launched = await self.launch('seedance-2.5', 15)
        self.assertEqual(len(launched.context.scripts), 1,
                         '2.5 / 15 must configure the visible duration UI before navigation')
        self.assertRegex(launched.context.scripts[0],
                         r'globalThis\.__DOLA_NATIVE_DURATION_TARGET__\s*=\s*15\s*;')
        self.assertIn('--disable-extensions', launched.launches[0][1]['args'])
        self.assertFalse(any('load-extension' in arg for arg in launched.launches[0][1]['args']))
        self.assertEqual(launched.context.routes, [],
                         'A native 15-second selection must not rewrite a signed submission body')

    async def test_25_30_keeps_native_ui_and_never_loads_legacy_rewriter(self):
        launched = await self.launch('seedance_v2.5', 30)
        self.assertEqual(len(launched.context.scripts), 1)
        self.assertRegex(launched.context.scripts[0],
                         r'globalThis\.__DOLA_NATIVE_DURATION_TARGET__\s*=\s*30\s*;')
        self.assertIn('--disable-extensions', launched.launches[0][1]['args'])
        self.assertEqual(launched.context.routes, [])

    async def test_fast_10_keeps_existing_extension_and_duration_route(self):
        launched = await self.launch('seedance-2.0', 10)
        self.assertEqual(launched.context.scripts, [])
        self.assertNotIn('--disable-extensions', launched.launches[0][1]['args'])
        self.assertTrue(any(arg.startswith('--load-extension=')
                            for arg in launched.launches[0][1]['args']))
        self.assertEqual([route[0] for route in launched.context.routes],
                         ['**/chat/completion**'])

    async def test_fast_15_configures_fast_model_and_duration_without_rewriting_submission(self):
        launched = await self.launch('seedance-2.0', 15)
        self.assertEqual(len(launched.context.scripts), 1)
        self.assertIn('__DOLA_NATIVE_MODEL_TARGET__ = "seedance_v2.0"', launched.context.scripts[0])
        self.assertIn('__DOLA_FAST15_NATIVE__', launched.context.scripts[0],
                      'Fast15 must initialize its pre-sign native state bridge')
        self.assertRegex(launched.context.scripts[0], r'__DOLA_NATIVE_DURATION_TARGET__\s*=\s*15\s*;')
        self.assertIn('--disable-extensions', launched.launches[0][1]['args'])
        self.assertEqual(launched.context.routes, [])

    async def test_fast_30_configures_fast_model_in_native_ui_without_old_rewriter(self):
        launched = await self.launch('seedance-2.0', 30)
        self.assertEqual(len(launched.context.scripts), 1)
        self.assertIn('__DOLA_NATIVE_MODEL_TARGET__ = "seedance_v2.0"', launched.context.scripts[0])
        self.assertIn('__DOLA_FAST_NATIVE__', launched.context.scripts[0])
        self.assertRegex(launched.context.scripts[0], r'__DOLA_NATIVE_DURATION_TARGET__\s*=\s*30\s*;')
        self.assertIn('--disable-extensions', launched.launches[0][1]['args'])
        self.assertFalse(any('load-extension' in arg for arg in launched.launches[0][1]['args']))
        self.assertEqual(launched.context.routes, [])


if __name__ == '__main__':
    unittest.main()
