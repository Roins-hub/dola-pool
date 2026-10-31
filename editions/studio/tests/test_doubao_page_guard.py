"""Regressions from the visual audit; every browser request is offline."""
import asyncio
import copy
import json
from pathlib import Path
import tempfile
import unittest

from patchright.async_api import async_playwright, TimeoutError as BrowserTimeoutError
from providers.base import ProviderError
from providers.doubao import DoubaoProvider
from providers.doubao.composer import DialogComposer
from providers.doubao.runtime import BrowserSession
from providers.doubao.page_guard import install_download_promotion_guard, clear_download_promotion
from test_doubao_ordinary_dialog_runtime import CAP, REQUEST, HTML
from test_doubao_provider import ck


# The observed native Radix promotion: an h2, the download test id and a
# labelled close button. No auth or security dialogs are represented here.
PROMOTION = '''<div role="dialog" data-slot="dialog-content" style="position:fixed;inset:0;z-index:99;background:white">
<h2>下載電腦版\n使用完整功能</h2>
<button data-testid="desktop_download_guide_popup_btn" onclick="document.body.dataset.downloaded=1">下載電腦版</button>
<button aria-label="關閉" onclick="this.parentNode.remove()">×</button></div>'''


class PageGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.driver = await async_playwright().start(); self.addAsyncCleanup(self.driver.stop)
        self.browser = await self.driver.chromium.launch(headless=True); self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context()
        self.html = HTML
        await self.context.route('**/*', lambda r: r.fulfill(status=200, content_type='text/html', body=self.html))
        self.page = await self.context.new_page(); await self.page.goto('https://www.doubao.com/chat/')
        self.session = BrowserSession(None, {})
        self.session.page, self.session.context = self.page, self.context

    async def load(self, extra):
        self.html = HTML + extra
        await self.page.reload(wait_until='domcontentloaded')

    async def test_known_download_promotion_is_closed_before_new_chat_without_downloading(self):
        await self.load(PROMOTION)
        try:
            await asyncio.wait_for(self.session.prepare(dict(REQUEST), copy.deepcopy(CAP), []), 3)
        except Exception as error:
            self.fail('Known promotion blocked ordinary preparation: ' + type(error).__name__)
        self.assertEqual(await self.page.locator('[role="dialog"]').count(), 0)
        self.assertIsNone(await self.page.locator('body').get_attribute('data-downloaded'))
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')
        self.assertIn(REQUEST['prompt'], await self.page.locator('#prompt').input_value())

    async def test_promotion_appearing_after_prepare_is_closed_before_the_single_send(self):
        await self.session.prepare(dict(REQUEST), copy.deepcopy(CAP), [])
        await self.page.locator('body').evaluate('(n,html)=>n.insertAdjacentHTML("beforeend",html)', PROMOTION)
        try:
            await asyncio.wait_for(self.session.submit(), 3)
        except Exception as error:
            self.fail('Late promotion blocked the one native click: ' + type(error).__name__)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '1')
        self.assertIsNone(await self.page.locator('body').get_attribute('data-downloaded'))

    async def test_delayed_promotion_between_readiness_and_new_chat_click_is_closed(self):
        # The real failure window: preparation checked for dialogs, then the
        # official download overlay appeared just as the sidebar was clicked.
        original_one = self.session._one

        async def delayed_overlay(selector, **kwargs):
            locator = await original_one(selector, **kwargs)
            if selector == CAP['selectors']['new_chat']:
                await self.page.locator('body').evaluate(
                    '(n,html)=>n.insertAdjacentHTML("beforeend",html)', PROMOTION)
            return locator

        self.session._one = delayed_overlay
        await asyncio.wait_for(self.session.prepare(dict(REQUEST), copy.deepcopy(CAP), []), 4)
        self.assertEqual(await self.page.locator('[role="dialog"]').count(), 0)
        self.assertIsNone(await self.page.locator('body').get_attribute('data-downloaded'))
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')
        self.assertIn(REQUEST['prompt'], await self.page.locator('#prompt').input_value())

    async def test_delayed_promotion_during_send_action_is_closed_without_resend(self):
        await self.session.prepare(dict(REQUEST), copy.deepcopy(CAP), [])
        original_one = self.session._one

        async def delayed_overlay(selector, **kwargs):
            locator = await original_one(selector, **kwargs)
            if selector == CAP['selectors']['send']:
                await self.page.locator('body').evaluate(
                    '(n,html)=>n.insertAdjacentHTML("beforeend",html)', PROMOTION)
            return locator

        self.session._one = delayed_overlay
        await asyncio.wait_for(self.session.submit(), 4)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '1')
        self.assertIsNone(await self.page.locator('body').get_attribute('data-downloaded'))

    async def test_unknown_or_auth_dialog_is_not_dismissed_or_treated_as_ready(self):
        for heading in ('请登录', '安全验证', '其他确认窗口'):
            await self.load(PROMOTION.replace('下載電腦版\n使用完整功能', heading))
            with self.subTest(heading=heading):
                with self.assertRaises(ProviderError) as error:
                    await DialogComposer(self.page, '#prompt').fill('must remain unsent')
                self.assertEqual(error.exception.code, 'page_blocked')
                self.assertEqual(await self.page.locator('[role="dialog"]').count(), 1)
                self.assertEqual(await self.page.locator('#prompt').input_value(), '')

    async def test_late_auth_dialog_is_never_closed_by_installed_handler(self):
        await self.session.prepare(dict(REQUEST), copy.deepcopy(CAP), [])
        await self.load(PROMOTION.replace('下載電腦版\n使用完整功能', '安全验证'))
        with self.assertRaises(BrowserTimeoutError):
            await self.page.locator('#new').click(timeout=600)
        self.assertEqual(await self.page.locator('[role="dialog"]').count(), 1)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')
        self.assertIsNone(await self.page.locator('body').get_attribute('data-downloaded'))

    async def test_guard_can_be_installed_twice_and_close_repeated_promotions(self):
        await install_download_promotion_guard(self.page)
        await install_download_promotion_guard(self.page)
        for _ in range(2):
            await self.page.locator('body').evaluate(
                '(n,html)=>n.insertAdjacentHTML("beforeend",html)', PROMOTION)
            await self.page.locator('#new').click(timeout=2000)
            self.assertEqual(await self.page.locator('[role="dialog"]').count(), 0)
        self.assertIsNone(await self.page.locator('body').get_attribute('data-downloaded'))

    async def test_multiple_promotions_are_closed_without_index_skipping(self):
        # Separate, clickable dialogs exercise locator-index shifting. Stacked
        # dialogs that intercept each other's close buttons should fail closed.
        separate = PROMOTION.replace('position:fixed;inset:0;', 'position:relative;')
        await self.load(separate + separate)
        await clear_download_promotion(self.page)
        self.assertEqual(await self.page.locator('[role="dialog"]').count(), 0)

    async def test_ambiguous_close_stops_without_download_or_send(self):
        await self.load(PROMOTION.replace('</div>', '<button aria-label="關閉">other</button></div>'))
        with self.assertRaises(ProviderError):
            await self.session.prepare(dict(REQUEST), copy.deepcopy(CAP), [])
        self.assertEqual(await self.page.locator('[role="dialog"]').count(), 1)
        self.assertIsNone(await self.page.locator('body').get_attribute('data-downloaded'))
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')

    async def test_explicit_traditional_login_entry_stops_before_editing(self):
        await self.load('<button>登入</button>')
        with self.assertRaises(ProviderError) as error:
            await self.session.prepare(dict(REQUEST), copy.deepcopy(CAP), [])
        self.assertEqual(error.exception.code, 'login_required')
        self.assertEqual(await self.page.locator('#prompt').input_value(), '')

    async def test_composer_hydrating_after_old_eight_second_deadline_is_waited_for(self):
        await self.load('''<script>document.querySelector('#prompt').remove();
        setTimeout(()=>{const n=document.createElement('textarea');n.id='prompt';document.body.append(n)},8500)</script>''')
        try:
            await self.session.prepare(dict(REQUEST), copy.deepcopy(CAP), [])
        except ProviderError as error:
            self.fail('Still stopped at the old hydration deadline: ' + error.code)
        self.assertIn(REQUEST['prompt'], await self.page.locator('#prompt').input_value())
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')

    async def test_same_official_identity_in_another_record_stops_before_prompt(self):
        provider = DoubaoProvider(root=Path(self.temp.name)/'pool', enabled=False)
        owner = provider.import_ck('owner', ck('owner-session', 'owner-cookie'), 'direct')
        target = provider.import_ck('target', ck('target-session', 'target-cookie'), 'direct')
        provider.accounts.refresh_session('owner', ck('owner-session', 'owner-cookie'), 'same-official-user')
        self.session.provider, self.session.account = provider, target
        await self.load('''<script>window._ROUTER_DATA={loaderData:{chat_layout:{chat_layout:{
          accountInfo:{data:{user_id:'same-official-user'}}}}}};</script>''')
        with self.assertRaises(ProviderError) as error:
            await self.session.prepare(dict(REQUEST), copy.deepcopy(CAP), [])
        self.assertEqual(error.exception.code, 'duplicate_account')
        self.assertEqual(await self.page.locator('#prompt').input_value(), '')
        records={a['name']:a for a in provider.list_accounts()}
        self.assertFalse(records['target']['enabled'])
        self.assertEqual(records['target']['diagnostics']['duplicate_of'], 'owner')
        self.assertEqual(records['target']['send_count'], 0)
        self.assertTrue(records['owner']['enabled'])


if __name__ == '__main__': unittest.main()
