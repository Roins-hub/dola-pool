"""Replay the observed HTTP-200 Dola error page in a real local browser."""
import ast
import re
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from patchright.async_api import async_playwright
from navigation_errors import BrowserSetupError, terminal_setup_reason, safe_navigation_reason


ERROR_PAGE = '<html><head><meta charset="utf-8"></head><body><div>该页面暂时不可用</div><button>刷新页面</button></body></html>'
ENGLISH_ERROR_PAGE = '<html><body><div>This page is temporarily unavailable</div><p>This may be a technical error that we are working on. Please try reloading this page.</p><button>Refresh</button><button>Back to homepage</button></body></html>'
READY_PAGE = '<html><head><meta charset="utf-8"></head><body><div style="width:600px"><textarea style="display:block;width:100%"></textarea><button>Videos erstellen</button></div></body></html>'


class PageReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        # Load the actual worker functions without opening production databases.
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'video_worker_ui.py').read_text(encoding='utf-8'))
        names = {'goto_retry', '_find_entry', '_page_unavailable_reason', 'PageUnavailableError',
                 '_first_visible', '_panel_ready', '_dismiss_cookie_banner',
                 '_handle_birthday_modal', 'BirthdayVerificationRequired',
                 '_visible_composer', '_find_composer_video_entry'}
        nodes = [node for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                 and node.name in names]
        self.ns = dict(re=re, time=time, page=self.page, VIDEO_BTN=re.compile(r'^Videos erstellen$'),
                       _dismiss_login_modal=AsyncMock(return_value=False),
                       BrowserSetupError=BrowserSetupError, terminal_setup_reason=terminal_setup_reason,
                       safe_navigation_reason=safe_navigation_reason,
                       BIRTHDAY_MODAL_RE=re.compile(r'Geburtstag|Geburtsdatum|date of birth|birthday|生日', re.I),
                       config=SimpleNamespace(GOTO_TRIES=2, GOTO_TIMEOUT_MS=5000, ENTRY_WAIT_SECONDS=1))
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     'worker-page-readiness', 'exec'), self.ns)
        controls = {'MODEL_RE', 'DUR_CHIP_RE', 'RATIO_CHIP_RE'}
        constants = [node for node in ast.walk(tree) if isinstance(node, ast.Assign)
                     and any(isinstance(target, ast.Name) and target.id in controls for target in node.targets)]
        exec(compile(ast.fix_missing_locations(ast.Module(body=constants, type_ignores=[])),
                     'worker-panel-controls', 'exec'), self.ns)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_http_200_error_page_is_retried_until_actual_page_loads(self):
        attempts = 0

        async def serve(route):
            nonlocal attempts
            attempts += 1
            await route.fulfill(status=200, content_type='text/html',
                                body=ERROR_PAGE if attempts == 1 else READY_PAGE)

        await self.page.route('https://dola.test/**', serve)
        await self.ns['goto_retry'](self.page, 'https://dola.test/chat', tries=2)
        self.assertTrue(await self.page.get_by_role('button', name='Videos erstellen').is_visible())

    async def test_persistent_error_page_reports_unavailable_instead_of_success(self):
        await self.page.route('https://dola.test/**',
                              lambda route: route.fulfill(status=200, content_type='text/html', body=ERROR_PAGE))
        with self.assertRaisesRegex(RuntimeError, '页面暂时不可用'):
            await self.ns['goto_retry'](self.page, 'https://dola.test/chat', tries=1)

    async def test_english_error_page_retries_instead_of_waiting_for_entry(self):
        attempts = 0
        async def serve(route):
            nonlocal attempts
            attempts += 1
            await route.fulfill(status=200, content_type='text/html',
                                body=ENGLISH_ERROR_PAGE if attempts == 1 else READY_PAGE)
        await self.page.route('https://dola.test/**', serve)
        await self.ns['goto_retry'](self.page, 'https://dola.test/chat', tries=2)
        self.assertTrue(await self.page.get_by_role('button', name='Videos erstellen').is_visible())
        self.assertEqual(attempts, 2)

    async def test_english_error_quote_in_working_chat_does_not_trigger_refresh(self):
        await self.page.set_content(READY_PAGE + ENGLISH_ERROR_PAGE)
        self.assertIsNone(await self.ns['_page_unavailable_reason'](self.page))

    async def test_error_appearing_after_navigation_interrupts_entry_wait(self):
        await self.page.set_content(ERROR_PAGE)
        with self.assertRaisesRegex(RuntimeError, '页面暂时不可用'):
            await self.ns['_find_entry'](max_seconds=1)

    async def test_error_after_clicking_video_entry_stops_panel_wait_without_reclicking(self):
        # The lazy JS request failed after the entry had been clicked. This error
        # must reach the pool as a setup failure, not another entry click/retry.
        await self.page.set_content(ENGLISH_ERROR_PAGE)
        with self.assertRaisesRegex(BrowserSetupError, '提示词未发送'):
            await self.ns['_panel_ready']()

    async def test_panel_detection_survives_language_change_and_error_quote_in_chat(self):
        for label in ('模型 Seedance 2.0', 'Model Seedance 2.0', 'Modell Seedance 2.0'):
            with self.subTest(label=label):
                await self.page.set_content(READY_PAGE + ENGLISH_ERROR_PAGE + f'<button>{label}</button>')
                self.assertTrue(await self.ns['_panel_ready']())

    async def test_birthday_gate_requires_user_verification_without_filling_or_submitting(self):
        await self.page.set_content('''<div role="dialog"><h2>Wann hast du Geburtstag?</h2>
            <input type="date"><button onclick="window.submitted=true;this.parentNode.remove()">Weiter</button></div>''')
        with self.assertRaisesRegex(RuntimeError, '生日验证'):
            await self.ns['_handle_birthday_modal'](self.page, 'fixture')
        self.assertEqual(await self.page.locator('input').input_value(), '')
        self.assertFalse(await self.page.evaluate('Boolean(window.submitted)'))

    async def test_birthday_gate_interrupts_entry_wait(self):
        await self.page.set_content('<h2>Wann hast du Geburtstag?</h2><input placeholder="YYYY-MM-DD"><button>Weiter</button>')
        with self.assertRaisesRegex(RuntimeError, '生日验证'):
            await self.ns['_find_entry'](max_seconds=1)

    async def test_birthday_text_in_chat_is_not_a_verification_gate(self):
        await self.page.set_content(READY_PAGE + '<p>Wann hast du Geburtstag?</p>')
        self.assertFalse(await self.ns['_handle_birthday_modal'](self.page, 'fixture'))

    async def test_normal_chat_quoting_error_text_is_not_treated_as_error_page(self):
        await self.page.set_content(READY_PAGE + '<div>该页面暂时不可用</div><button>刷新页面</button>')
        entry = await self.ns['_find_entry'](max_seconds=1)
        self.assertIsNotNone(entry)
        self.assertTrue(await entry.is_visible())


if __name__ == '__main__':
    unittest.main()
