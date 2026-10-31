"""Residual cookies must not authorize a guest page to edit or send a task."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from patchright.async_api import async_playwright
from providers.base import ProviderError
from providers.doubao import DoubaoProvider
from providers.doubao.runtime import BrowserSession, BeforeSendError
from test_doubao_ordinary_dialog_runtime import HTML, CAP, REQUEST
from test_doubao_provider import ck


class LiveAuthGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.provider = DoubaoProvider(root=Path(self.temp.name)/'pool', enabled=False)
        self.account = self.provider.import_ck('one', ck(), 'direct')
        self.provider.accounts.observe_identity('one', 'bound-user')
        self.driver = await async_playwright().start(); self.addAsyncCleanup(self.driver.stop)
        self.browser = await self.driver.chromium.launch(headless=True); self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context()
        # Cookie expiry is deliberately valid. Only the official page can prove login.
        await self.context.add_cookies([{'name':'sessionid','value':'residual-cookie','domain':'.doubao.com','path':'/'}])
        self.html = HTML + '''<script>window._ROUTER_DATA={loaderData:{chat_layout:{chat_layout:{
          accountInfo:{data:{user_id:'bound-user'}}}}}};</script>'''
        await self.context.route('**/*', lambda route:route.fulfill(status=200,content_type='text/html',body=self.html))
        self.page = await self.context.new_page(); await self.page.goto('https://www.doubao.com/chat/')
        self.session = BrowserSession(self.provider, self.account)
        self.session.page, self.session.context = self.page, self.context
        self.folder = self.provider.root/'accounts'/self.account['account_id']

    async def test_missing_identity_with_residual_cookie_stops_before_editing(self):
        await self.page.evaluate('window._ROUTER_DATA.loaderData.chat_layout.chat_layout.accountInfo.data={}',isolated_context=False)
        with patch('providers.doubao.runtime.NATIVE_READY_TIMEOUT', .15):
            with self.assertRaises(ProviderError) as error:
                await self.session.prepare(dict(REQUEST),copy.deepcopy(CAP),[])
        self.assertEqual(error.exception.code,'login_unverified')
        self.assertEqual(await self.page.locator('#prompt').input_value(),'')
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')
        self.assertTrue(self.provider.list_accounts()[0]['enabled'])

    async def test_delayed_real_identity_is_waited_for_without_replacing_the_session(self):
        await self.page.evaluate('''()=>{const data=window._ROUTER_DATA.loaderData.chat_layout.chat_layout.accountInfo.data;
          delete data.user_id;setTimeout(()=>data.user_id='bound-user',150)}''',isolated_context=False)
        await self.session.prepare(dict(REQUEST),copy.deepcopy(CAP),[])
        self.assertIn(REQUEST['prompt'],await self.page.locator('#prompt').input_value())
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')
        self.assertTrue(self.provider.list_accounts()[0]['enabled'])

    async def test_lost_identity_after_preparation_stops_the_native_click(self):
        await self.session.prepare(dict(REQUEST),copy.deepcopy(CAP),[])
        await self.page.evaluate('window._ROUTER_DATA.loaderData.chat_layout.chat_layout.accountInfo.data={}',isolated_context=False)
        with patch('providers.doubao.runtime.NATIVE_READY_TIMEOUT', .15):
            with self.assertRaises(BeforeSendError) as error:
                await self.session.submit()
        self.assertEqual(error.exception.code,'login_unverified')
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')

    async def test_explicit_logout_on_close_pauses_account_and_preserves_ck_and_accounting(self):
        self.provider.accounts.reserve('existing-task','one')
        self.provider.accounts.mark_sent('existing-task')
        self.provider.accounts.settle('existing-task','unknown')
        before = self.provider.list_accounts()[0]
        cookie_bytes = (self.folder/'cookies.json').read_bytes()
        await self.page.locator('body').evaluate("n=>n.insertAdjacentHTML('beforeend','<button>登入</button>')")
        self.assertFalse(await self.session.persist_authenticated_session())
        after = self.provider.list_accounts()[0]
        self.assertFalse(after['enabled'])
        self.assertEqual(after['status'],'login_required')
        for key in ('send_count','quota_used','quota_reserved','proxy_label'):
            self.assertEqual(after[key],before[key])
        self.assertEqual((self.folder/'cookies.json').read_bytes(),cookie_bytes)

    async def test_incomplete_page_on_close_does_not_disable_an_account(self):
        await self.page.evaluate('delete window._ROUTER_DATA',isolated_context=False)
        self.assertFalse(await self.session.persist_authenticated_session())
        self.assertTrue(self.provider.list_accounts()[0]['enabled'])


if __name__ == '__main__': unittest.main()
