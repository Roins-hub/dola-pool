"""Stable official identity and cookie persistence; no live accounts or network."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from patchright.async_api import async_playwright
from providers.base import ProviderError
from providers.doubao.accounts import AccountStore
from providers.doubao.runtime import BrowserSession, HOME
from test_doubao_provider import ck


class SessionRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AccountStore(Path(self.temp.name) / 'pool')
        self.account = self.store.import_ck('one', ck('old-session', 'old-cookie-uid'), 'direct')
        self.folder = self.store.root / 'accounts' / self.account['account_id']

    def test_same_official_identity_accepts_rotating_cookie_uid_and_keeps_task_quota_route(self):
        self.store.reserve('pending-video', 'one')
        self.store.mark_sent('pending-video')
        self.store.settle('pending-video', 'unknown')
        self.store.refresh_session('one', ck('session-before', 'cookie-uid-before'), 'stable-user')
        before = self.store.list_accounts()[0]
        proxy = (self.folder / 'proxy.json').read_bytes()
        environment = (self.folder / 'environment.json').read_bytes()
        with self.store.connect() as conn:
            attempts = [dict(row) for row in conn.execute('SELECT * FROM attempts')]
        result = self.store.refresh_session('one', ck('session-after', 'cookie-uid-after'), 'stable-user')
        self.assertEqual(result, before)
        self.assertEqual(self.store.official_user_id('one'), 'stable-user')
        self.assertEqual(json.loads((self.folder / 'cookies.json').read_text())[0]['value'], 'session-after')
        self.assertEqual((self.folder / 'proxy.json').read_bytes(), proxy)
        self.assertEqual((self.folder / 'environment.json').read_bytes(), environment)
        with self.store.connect() as conn:
            self.assertEqual([dict(row) for row in conn.execute('SELECT * FROM attempts')], attempts)
        self.assertNotIn('stable-user', json.dumps(result))
        self.assertNotIn('session-after', json.dumps(result))

    def test_different_stable_identity_cannot_overwrite_saved_cookies(self):
        self.store.refresh_session('one', ck(), 'first-user')
        before = (self.folder / 'cookies.json').read_bytes()
        with self.assertRaises(ProviderError) as error:
            self.store.refresh_session('one', ck('another-session', 'rotated-cookie'), 'different-user')
        self.assertEqual(error.exception.code, 'account_identity_mismatch')
        self.assertEqual((self.folder / 'cookies.json').read_bytes(), before)
        self.assertEqual(self.store.official_user_id('one'), 'first-user')

    def test_other_account_or_trash_identity_conflict_is_rejected(self):
        self.store.import_ck('two', ck('two-session', 'two-cookie'), 'direct')
        self.store.refresh_session('two', ck('two-refreshed', 'two-rotated'), 'already-owned-user')
        self.store.delete('two')
        before = (self.folder / 'cookies.json').read_bytes()
        with self.assertRaises(ProviderError) as error:
            self.store.refresh_session('one', ck('new-one-session', 'new-one-cookie'), 'already-owned-user')
        self.assertEqual(error.exception.code, 'duplicate_account')
        self.assertEqual((self.folder / 'cookies.json').read_bytes(), before)
        self.assertFalse((self.folder / 'official-identity.json').exists())

    def test_other_account_cookie_collision_is_rejected_without_resetting_identity(self):
        self.store.import_ck('two', ck('two-session', 'two-cookie'), 'direct')
        with self.assertRaises(ProviderError) as error:
            self.store.refresh_session('one', ck('two-session', 'changed-cookie'), 'new-official-user')
        self.assertEqual(error.exception.code, 'duplicate_account')
        self.assertEqual(len(self.store.list_accounts()), 2)

    def test_invalid_or_unsafe_official_identity_and_non_doubao_cookies_are_rejected(self):
        for identity in (None, '', True, 9007199254740993, 1.5, 'bad\nuser'):
            with self.subTest(identity=identity), self.assertRaises(ProviderError):
                self.store.refresh_session('one', ck(), identity)
        with self.assertRaises(ProviderError):
            self.store.refresh_session('one', ck(domain='.example.com'), 'safe-user')

    def test_corrupt_existing_stable_record_is_not_treated_as_unbound(self):
        (self.folder / 'official-identity.json').write_text('{broken', encoding='utf-8')
        with self.assertRaises(ProviderError):
            self.store.refresh_session('one', ck(), 'new-user')

    def test_failed_identity_write_restores_previous_canonical_cookies(self):
        from providers.doubao import accounts as module
        before = (self.folder / 'cookies.json').read_bytes()
        original = module.atomic_json
        def write(path, value):
            if Path(path).name == 'official-identity.json':
                raise OSError('fixture disk failure')
            return original(path, value)
        with patch.object(module, 'atomic_json', side_effect=write), self.assertRaises(OSError):
            self.store.refresh_session('one', ck('fresh', 'fresh-cookie'), 'new-user')
        self.assertEqual((self.folder / 'cookies.json').read_bytes(), before)
        self.assertFalse((self.folder / 'official-identity.json').exists())


class NativeAuthPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AccountStore(Path(self.temp.name) / 'pool')
        self.account = self.store.import_ck('one', ck('canonical-old', 'canonical-cookie'), 'direct')
        self.folder = self.store.root / 'accounts' / self.account['account_id']
        self.playwright = await async_playwright().start()
        self.context = await self.playwright.chromium.launch_persistent_context(str(Path(self.temp.name) / 'profile'), headless=True)
        self.addAsyncCleanup(self.playwright.stop)
        self.addAsyncCleanup(self.context.close)
        await self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body='''<textarea></textarea><div data-testid="user-menu">fixture</div><script>window._ROUTER_DATA={loaderData:{chat_layout:{chat_layout:{accountInfo:{data:{user_id:'stable-user'}}}}}};</script>'''))
        self.page = await self.context.new_page()
        await self.page.goto(HOME)
        provider = SimpleNamespace(root=self.store.root, accounts=self.store, scheduler=None)
        self.session = BrowserSession(provider, self.account)
        self.session.context, self.session.page = self.context, self.page

    async def fresh_profile(self, value='profile-fresh'):
        await self.context.add_cookies([{'name':'sessionid','value':value,'domain':'.doubao.com','path':'/',
                                        'secure':True,'httpOnly':True,'sameSite':'Lax'},
                                       {'name':'uid_tt','value':'profile-rotated-cookie','domain':'.doubao.com','path':'/'}])

    async def test_official_user_id_reads_only_observed_main_world_path(self):
        self.assertEqual(await self.session.official_user_id(), 'stable-user')
        await self.page.evaluate('window._ROUTER_DATA.loaderData.chat_layout.chat_layout.accountInfo.data.user_id=42', isolated_context=False)
        self.assertEqual(await self.session.official_user_id(), '42')
        await self.page.evaluate("window._ROUTER_DATA.loaderData.chat_layout.chat_layout.accountInfo.data.user_id='12345678901234567890'", isolated_context=False)
        self.assertEqual(await self.session.official_user_id(), '12345678901234567890')
        await self.page.evaluate('window._ROUTER_DATA.loaderData.chat_layout.chat_layout.accountInfo.data.user_id=9007199254740993', isolated_context=False)
        self.assertIsNone(await self.session.official_user_id())
        await self.page.goto('https://example.com/')
        self.assertIsNone(await self.session.official_user_id())

    async def test_existing_native_profile_wins_even_if_canonical_file_is_invalid(self):
        await self.fresh_profile()
        (self.folder / 'cookies.json').write_text('{broken', encoding='utf-8')
        self.assertFalse(await self.session.seed_profile_session(self.folder / 'cookies.json'))
        cookies = await self.context.cookies(HOME)
        self.assertEqual(next(c['value'] for c in cookies if c['name']=='sessionid'), 'profile-fresh')

    async def test_empty_profile_is_seeded_from_preserved_canonical(self):
        self.assertTrue(await self.session.seed_profile_session(self.folder / 'cookies.json'))
        cookies = await self.context.cookies(HOME)
        self.assertEqual(next(c['value'] for c in cookies if c['name']=='sessionid'), 'canonical-old')

    async def test_explicit_existing_account_login_opens_without_reading_missing_canonical(self):
        import providers.doubao.runtime as runtime
        (self.folder / 'cookies.json').unlink()
        page = SimpleNamespace(on=lambda *args:None, goto=AsyncMock(), is_closed=lambda:False)
        context = SimpleNamespace(new_page=AsyncMock(return_value=page), pages=[], route=AsyncMock(), close=AsyncMock(),
                                  cookies=AsyncMock(side_effect=AssertionError('Explicit restore must not read/seed canonical')))
        driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=AsyncMock(return_value=context)), stop=AsyncMock())
        class Bridge:
            browser_proxy = {'server':'http://127.0.0.1:1'}
            authorize_request = AsyncMock()
            async def __aenter__(self):return self
            async def close(self):return None
        account = {**self.account, '_interactive':True}
        session = BrowserSession(SimpleNamespace(root=self.store.root,accounts=self.store,scheduler=None), account, login=True)
        with patch.object(runtime,'DirectBridge',Bridge), patch.object(runtime,'async_playwright',return_value=SimpleNamespace(start=AsyncMock(return_value=driver))):
            async with session:
                page.goto.assert_awaited_once()
                self.assertFalse(driver.chromium.launch_persistent_context.call_args.kwargs['headless'])
        context.cookies.assert_not_called()

    async def test_authenticated_matching_profile_syncs_its_fresh_session(self):
        self.store.refresh_session('one', ck('canonical-before', 'cookie-before'), 'stable-user')
        await self.fresh_profile()
        self.assertTrue(await self.session.persist_authenticated_session())
        saved = json.loads((self.folder / 'cookies.json').read_text())
        self.assertEqual(next(c['value'] for c in saved if c['name']=='sessionid'), 'profile-fresh')

    async def test_context_exit_saves_fresh_authenticated_session_before_closing(self):
        self.store.refresh_session('one', ck('canonical-before', 'cookie-before'), 'stable-user')
        await self.fresh_profile()
        await self.session.__aexit__(None, None, None)
        self.assertTrue(self.page.is_closed())
        self.assertTrue(self.session.auth_sync['saved'])
        saved = json.loads((self.folder / 'cookies.json').read_text())
        self.assertEqual(next(c['value'] for c in saved if c['name']=='sessionid'), 'profile-fresh')

    async def test_cancel_during_cookie_sync_still_closes_the_browser(self):
        with patch.object(self.session, 'persist_authenticated_session', AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await self.session.__aexit__(None, None, None)
        self.assertTrue(self.page.is_closed())

    async def test_absent_native_identity_does_not_save_residual_session_cookie(self):
        self.store.refresh_session('one', ck('canonical-before', 'cookie-before'), 'stable-user')
        await self.fresh_profile()
        await self.page.evaluate('delete window._ROUTER_DATA', isolated_context=False)
        before = (self.folder / 'cookies.json').read_bytes()
        self.assertFalse(await self.session.persist_authenticated_session())
        self.assertEqual((self.folder / 'cookies.json').read_bytes(), before)

    async def test_logged_out_profile_never_overwrites_valid_canonical(self):
        self.store.refresh_session('one', ck('valid-canonical', 'valid-cookie'), 'stable-user')
        await self.fresh_profile()
        await self.page.locator('body').evaluate("n=>n.insertAdjacentHTML('afterbegin','<button>登录</button>')")
        before = (self.folder / 'cookies.json').read_bytes()
        self.assertFalse(await self.session.persist_authenticated_session())
        self.assertEqual((self.folder / 'cookies.json').read_bytes(), before)

    async def test_other_profile_identity_and_unbound_profile_cannot_auto_save(self):
        await self.fresh_profile()
        before = (self.folder / 'cookies.json').read_bytes()
        self.assertFalse(await self.session.persist_authenticated_session())
        self.assertFalse((self.folder / 'official-identity.json').exists())
        self.store.refresh_session('one', ck('bound-canonical', 'bound-cookie'), 'other-user')
        bound = (self.folder / 'cookies.json').read_bytes()
        self.assertFalse(await self.session.persist_authenticated_session())
        self.assertEqual((self.folder / 'cookies.json').read_bytes(), bound)

    async def test_capture_and_qr_login_return_internal_official_identity(self):
        await self.fresh_profile()
        checks = {'route_verified':True,'login_verified':True}
        with patch.object(self.session, 'verify', AsyncMock(return_value=checks)):
            captured = await self.session.capture_interactive_login()
            step = await self.session.login_step()
        self.assertEqual(captured['official_user_id'], 'stable-user')
        self.assertEqual(step['official_user_id'], 'stable-user')


if __name__ == '__main__':
    unittest.main()
