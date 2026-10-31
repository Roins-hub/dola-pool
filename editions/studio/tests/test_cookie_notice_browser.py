import unittest
from patchright.async_api import async_playwright
from test_video_pipeline import pool_module
from video_worker_ui import _dismiss_cookie_banner


class CookieNoticeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.p = await async_playwright().start()
        self.browser = await self.p.chromium.launch(headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.p.stop()

    async def test_plain_ok_is_scoped_to_cookie_notice(self):
        await self.page.set_content('''<section><p>Delete this task?</p>
            <button onclick="this.id='wrong'">OK</button></section>
            <aside><p>Dola uses cookies and similar technologies to provide our services.</p>
            <button onclick="this.id='acknowledged'">OK</button></aside>''')
        self.assertTrue(await _dismiss_cookie_banner(self.page))
        self.assertEqual(await self.page.locator('#acknowledged').count(), 1)
        self.assertEqual(await self.page.locator('#wrong').count(), 0)

    async def test_an_unrelated_confirmation_is_never_accepted(self):
        await self.page.set_content('''<section><p>Delete this task?</p>
            <button onclick="this.id='wrong'">OK</button></section>''')
        self.assertFalse(await _dismiss_cookie_banner(self.page))
        self.assertEqual(await self.page.locator('#wrong').count(), 0)
