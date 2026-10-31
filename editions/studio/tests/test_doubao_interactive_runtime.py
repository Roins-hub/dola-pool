"""Interactive official login is observed without controls or exposing credentials."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from patchright.async_api import async_playwright
from providers.base import ProviderError
from providers.doubao.runtime import BrowserSession, HOME
import providers.doubao.runtime as runtime


class InteractiveLaunchTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_explicit_interactive_uses_visible_full_chromium(self):
        with tempfile.TemporaryDirectory() as folder:
            for interactive in (True, False):
                page = SimpleNamespace(on=lambda *args: None, goto=AsyncMock())
                context = SimpleNamespace(new_page=AsyncMock(return_value=page), pages=[],
                    route=AsyncMock(), close=AsyncMock())
                launch = AsyncMock(return_value=context)
                driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch), stop=AsyncMock())
                manager = SimpleNamespace(start=AsyncMock(return_value=driver))
                provider = SimpleNamespace(root=Path(folder), scheduler=None)
                account = {'name':'manual', 'account_id':'manual', '_login':True,
                           '_binding':{'server':'direct'}, '_interactive':interactive}
                with patch.object(runtime, 'async_playwright', return_value=manager):
                    async with BrowserSession(provider, account, login=True):
                        options = launch.call_args.kwargs
                        self.assertEqual(options['headless'], not interactive)
                        self.assertEqual(options['channel'], 'chromium')


class InteractiveRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(Path(self.temp.name)/'profile'), headless=True, channel='chromium')
        self.addAsyncCleanup(self.context.close)
        self.html = '<button onclick="window.clicked=true">登录</button><textarea></textarea>'
        await self.context.route('**/*', lambda route: route.fulfill(status=200,
            content_type='text/html; charset=utf-8', body=self.html))
        self.page = await self.context.new_page()
        await self.page.goto(HOME)
        self.session = BrowserSession(None, {'name':'manual', '_interactive':True})
        self.session.context, self.session.page = self.context, self.page
        self.session.binding = {'server':'direct'}

    async def test_status_waits_without_clicking_navigating_or_emitting_qr(self):
        with patch.object(self.session, 'login_step', side_effect=AssertionError('No automatic login')):
            status = await self.session.interactive_login_status()
        self.assertEqual(status, {'state':'waiting_login', 'login_verified':False})
        self.assertEqual(self.page.url, HOME)
        self.assertIsNone(await self.page.evaluate('()=>window.clicked', isolated_context=False))

    async def test_capture_missing_credentials_is_retryable_and_keeps_window(self):
        with self.assertRaises(ProviderError) as caught:
            await self.session.capture_interactive_login()
        self.assertEqual(caught.exception.code, 'login_not_ready')
        self.assertFalse(self.page.is_closed())
        self.assertEqual((await self.session.interactive_login_status())['state'], 'waiting_login')

    async def test_capture_keeps_real_cookie_unverified_and_excludes_other_sites(self):
        await self.context.add_cookies([
            {'name':'sessionid', 'value':'real-fixture-session', 'domain':'.doubao.com', 'path':'/'},
            {'name':'private', 'value':'other-site', 'domain':'.example.com', 'path':'/'}])
        checks={'route_verified':True,'route_mode':'direct','login_verified':False,'capabilities':'unverified'}
        with patch.object(self.session, 'verify', AsyncMock(return_value=checks)) as verify:
            captured = await self.session.capture_interactive_login()
        self.assertEqual(captured['state'], 'captured')
        self.assertFalse(captured['checks']['login_verified'])
        self.assertFalse(captured['login_verified'])
        self.assertEqual([cookie['name'] for cookie in captured['cookies']], ['sessionid'])
        verify.assert_awaited_once_with(observe_only=True)

    async def test_verified_native_status_requires_real_session_and_native_avatar(self):
        self.html='<textarea></textarea><div data-testid="user-menu">我</div>'
        await self.page.reload()
        await self.context.add_cookies([{'name':'sessionid','value':'real-fixture-session','domain':'.doubao.com','path':'/'}])
        self.assertEqual(await self.session.interactive_login_status(),
                         {'state':'authenticated','login_verified':True})

    async def test_real_official_session_is_ready_when_native_dom_is_uncertain(self):
        await self.context.add_cookies([
            {'name':'sessionid', 'value':'real-fixture-session', 'domain':'.doubao.com', 'path':'/'},
            {'name':'private', 'value':'other-site-secret', 'domain':'.example.com', 'path':'/'}])
        status = await self.session.interactive_login_status()
        self.assertEqual(status, {'state':'session_ready', 'login_verified':False})
        self.assertNotIn('real-fixture-session', json.dumps(status))
        self.assertNotIn('other-site-secret', json.dumps(status))
        self.assertIsNone(await self.page.evaluate('()=>window.clicked', isolated_context=False))
        self.assertEqual(self.page.url, HOME)

    async def test_guest_cookies_never_mark_interactive_login_ready(self):
        await self.context.add_cookies([
            {'name':'uid_tt', 'value':'guest-user-id', 'domain':'.doubao.com', 'path':'/'},
            {'name':'sessionid', 'value':'unrelated-session', 'domain':'.example.com', 'path':'/'}])
        self.assertEqual(await self.session.interactive_login_status(),
                         {'state':'waiting_login', 'login_verified':False})

    async def test_status_outside_official_page_does_not_read_cookies(self):
        await self.page.goto('https://example.com/')
        self.session.context=SimpleNamespace(cookies=AsyncMock(side_effect=AssertionError('No cookie read outside Doubao')))
        self.assertEqual(await self.session.interactive_login_status(),
                         {'state':'waiting_login', 'login_verified':False})

    async def test_closed_window_is_reported_without_throwing(self):
        await self.context.close()
        self.assertEqual(await self.session.interactive_login_status(), {'state':'closed','login_verified':False})
        self.assertEqual(await self.session.capture_interactive_login(), {'state':'closed'})

    async def test_capture_outside_official_page_cannot_read_other_site_cookies(self):
        await self.page.goto('https://example.com/')
        self.session.context=SimpleNamespace(cookies=AsyncMock(side_effect=AssertionError('No cookie read outside Doubao')))
        with self.assertRaises(ProviderError) as caught:
            await self.session.capture_interactive_login()
        self.assertEqual(caught.exception.code, 'login_not_ready')

    async def test_observe_only_verification_does_not_click_native_video_entry(self):
        self.html='<textarea></textarea><button onclick="window.clicked=true">视频生成</button>'
        await self.page.reload()
        response=SimpleNamespace(status=200,json=AsyncMock(return_value={'ip':'93.184.216.34'}),dispose=AsyncMock())
        self.session.binding={'server':'http://fixture-proxy.example:8080'}
        self.session.context=SimpleNamespace(cookies=self.context.cookies,
            request=SimpleNamespace(get=AsyncMock(return_value=response)))
        checks=await self.session.verify(observe_only=True)
        self.assertTrue(checks['route_verified'])
        self.assertTrue(checks['video_entry'])
        self.assertIsNone(await self.page.evaluate('()=>window.clicked', isolated_context=False))

    async def test_route_failure_is_not_saved_as_verified_login(self):
        await self.context.add_cookies([{'name':'sessionid','value':'real-fixture-session','domain':'.doubao.com','path':'/'}])
        with patch.object(self.session, 'verify', AsyncMock(return_value={'route_verified':False,'login_verified':False})):
            with self.assertRaises(ProviderError) as caught:
                await self.session.capture_interactive_login()
        self.assertEqual(caught.exception.code, 'proxy_unavailable')
        self.assertFalse(self.page.is_closed())
