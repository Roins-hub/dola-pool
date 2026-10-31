"""Account browser environments are enforced before any network or page opens."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from providers.base import ProviderError
from providers.doubao.runtime import BrowserSession
import providers.doubao.runtime as runtime


class LaunchFixture:
    def __init__(self):
        self.page = SimpleNamespace(on=lambda *args: None, goto=AsyncMock(),
                                    url=runtime.HOME, is_closed=lambda: False)
        self.context = SimpleNamespace(new_page=AsyncMock(return_value=self.page), pages=[],
            route=AsyncMock(), close=AsyncMock(), cookies=AsyncMock(return_value=[]),
            add_cookies=AsyncMock(), add_init_script=AsyncMock())
        self.launch = AsyncMock(return_value=self.context)
        self.driver = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=self.launch), stop=AsyncMock())
        self.manager = SimpleNamespace(start=AsyncMock(return_value=self.driver))


class StableRuntimeEnvironmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_login_persists_one_environment_used_by_visible_and_background_windows(self):
        with tempfile.TemporaryDirectory() as temp:
            provider = SimpleNamespace(root=Path(temp), scheduler=None)
            account = {'name': 'login:fixture', 'account_id': '1' * 32,
                       '_login': True, '_binding': {'server': 'direct'}}
            saved_bytes = None
            first_options = None
            for interactive in (True, False, True):
                fixture = LaunchFixture()
                with patch.object(runtime, 'async_playwright', return_value=fixture.manager):
                    async with BrowserSession(provider, {**account, '_interactive': interactive}, login=True):
                        options = fixture.launch.call_args.kwargs
                        path = Path(temp) / 'login' / account['account_id'] / 'environment.json'
                        self.assertTrue(path.is_file(), 'The first login browser must save its environment before launch')
                        current_bytes = path.read_bytes()
                        saved = json.loads(current_bytes)
                        self.assertEqual(saved['environment_id'], account['account_id'])
                        self.assertEqual(options['channel'], 'chromium')
                        self.assertEqual(options['headless'], not interactive)
                        self.assertNotIn('user_agent', options)
                        self.assertNotIn('extra_http_headers', options)
                        self.assertEqual(options['service_workers'], 'block')
                        self.assertIn('--force-webrtc-ip-handling-policy=disable_non_proxied_udp', options['args'])
                        stable = {k: options[k] for k in ('locale', 'timezone_id', 'viewport', 'screen',
                            'device_scale_factor', 'is_mobile', 'has_touch', 'color_scheme')}
                        if first_options is None:
                            first_options, saved_bytes = stable, current_bytes
                        else:
                            self.assertEqual(stable, first_options)
                            self.assertEqual(current_bytes, saved_bytes)

    async def test_existing_login_environment_rejects_corruption_before_launch(self):
        with tempfile.TemporaryDirectory() as temp:
            provider = SimpleNamespace(root=Path(temp), scheduler=None)
            account = {'name': 'login:fixture', 'account_id': '2' * 32,
                       '_login': True, '_binding': {'server': 'direct'}}
            folder = Path(temp) / 'login' / account['account_id']
            folder.mkdir(parents=True)
            path = folder / 'environment.json'
            path.write_text('{broken', encoding='utf-8')
            fixture = LaunchFixture()
            with patch.object(runtime, 'async_playwright', return_value=fixture.manager):
                with self.assertRaises(ProviderError):
                    async with BrowserSession(provider, account, login=True):
                        self.fail('A corrupt account environment must not open a browser')
            fixture.launch.assert_not_awaited()
            self.assertEqual(path.read_text(encoding='utf-8'), '{broken')

    async def test_login_environment_cannot_be_copied_to_another_identity_or_reused_after_route_change(self):
        from providers.doubao.accounts import atomic_json
        from providers.doubao.environment import create_environment
        with tempfile.TemporaryDirectory() as temp:
            provider = SimpleNamespace(root=Path(temp), scheduler=None)
            account = {'name': 'login:fixture', 'account_id': '3' * 32,
                       '_login': True, '_binding': {'server': 'direct'}}
            folder = Path(temp) / 'login' / account['account_id']
            path = folder / 'environment.json'
            for wrong in (create_environment('4' * 32, {'server': 'direct'}),
                          create_environment(account['account_id'], {'server': 'http://fixture.example:8080'})):
                atomic_json(path, wrong)
                original = path.read_bytes()
                fixture = LaunchFixture()
                with patch.object(runtime, 'async_playwright', return_value=fixture.manager):
                    with self.assertRaises(ProviderError):
                        async with BrowserSession(provider, account, login=True):
                            self.fail('A mismatched environment must fail before opening a browser')
                fixture.launch.assert_not_awaited()
                self.assertEqual(path.read_bytes(), original)

    async def test_generation_and_restore_reuse_the_same_legacy_account_profile_and_migrated_environment(self):
        from providers.doubao.accounts import AccountStore, atomic_json
        from test_doubao_provider import ck
        with tempfile.TemporaryDirectory() as temp:
            store = AccountStore(Path(temp))
            account = store.import_ck('existing', ck(), 'direct')
            folder = Path(temp) / 'accounts' / account['account_id']
            cookie_bytes = (folder / 'cookies.json').read_bytes()
            atomic_json(folder / 'environment.json', {'schema': 1, 'locale': 'zh-CN',
                'timezone_id': 'Asia/Shanghai', 'required_proxy_server': 'direct', 'created_at': 12345})
            provider = SimpleNamespace(root=Path(temp), accounts=store, scheduler=None)
            first_options = None
            for restore in (False, True, False):
                fixture = LaunchFixture()
                with patch.object(runtime, 'async_playwright', return_value=fixture.manager):
                    async with BrowserSession(provider, {**account, '_interactive': restore}, login=restore):
                        self.assertEqual(fixture.launch.call_args.args[0], str(folder / 'profile'))
                        options = fixture.launch.call_args.kwargs
                        saved = json.loads((folder / 'environment.json').read_text(encoding='utf-8'))
                        self.assertEqual(saved['schema'], 2)
                        self.assertEqual(saved['created_at'], 12345)
                        self.assertEqual(saved['environment_id'], account['account_id'])
                        self.assertEqual(options['headless'], not restore)
                        self.assertEqual(options['channel'], 'chromium')
                        current = {k: options[k] for k in ('locale', 'timezone_id', 'viewport', 'screen')}
                        if first_options is None:
                            first_options = current
                        else:
                            self.assertEqual(current, first_options)
                self.assertEqual((folder / 'cookies.json').read_bytes(), cookie_bytes)


class NativeEnvironmentFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_browser_language_and_headers_match_the_saved_route_in_native_chromium(self):
        from patchright.async_api import async_playwright
        from providers.doubao.direct_bridge import DirectBridge
        registry = {
            'local:hk': {'server': 'http://127.0.0.1:17897', 'country': '香港'},
            'local:tw': {'server': 'http://127.0.0.1:17899', 'country': '台湾'},
        }
        # All page requests are fulfilled in memory. No browser sees an account
        # or contacts a proxy/Doubao; the real native context remains unmodified.
        with tempfile.TemporaryDirectory() as temp, patch('config.doubao_local_proxy_options', return_value=registry):
            async with async_playwright() as driver:
                seen = []
                async def launch(folder, **kwargs):
                    context = await driver.chromium.launch_persistent_context(folder, **kwargs)
                    async def offline(route):
                        seen.append(await route.request.all_headers())
                        await route.fulfill(status=200, content_type='text/html; charset=utf-8',
                            body='<!doctype html><p>Offline environment fixture</p>')
                    await context.route('**/*', offline)
                    return context
                manager = SimpleNamespace(start=AsyncMock(return_value=SimpleNamespace(
                    chromium=SimpleNamespace(launch_persistent_context=launch), stop=AsyncMock())))
                provider = SimpleNamespace(root=Path(temp), scheduler=None)
                cases = [('5' * 32, {'server': 'direct'}, 'zh-CN', 'Asia/Shanghai'),
                         ('6' * 32, {'server': 'http://127.0.0.1:17897'}, 'zh-HK', 'Asia/Hong_Kong'),
                         ('7' * 32, {'server': 'http://127.0.0.1:17899'}, 'zh-TW', 'Asia/Taipei')]
                for identifier, binding, language, timezone in cases:
                    account = {'name': 'offline', 'account_id': identifier, '_login': True, '_binding': binding}
                    with patch.object(runtime, 'async_playwright', return_value=manager), \
                         patch.object(runtime, 'ProxyChainBridge', side_effect=lambda _: DirectBridge()), \
                         patch.object(runtime.proxy_usage.usage, 'ensure_available'), \
                         patch.object(runtime.proxy_usage, 'attach_request_counter', new=AsyncMock()):
                        observations = []
                        for _ in range(2):
                            async with BrowserSession(provider, account, login=True) as session:
                                observed = await session.page.evaluate('''()=>({language:navigator.language,
                                    languages:navigator.languages,timeZone:Intl.DateTimeFormat().resolvedOptions().timeZone,
                                    viewport:{width:innerWidth,height:innerHeight},screen:{width:screen.width,height:screen.height},
                                    scale:devicePixelRatio,platform:navigator.platform,ua:navigator.userAgent})''',
                                    isolated_context=False)
                                self.assertEqual(observed['language'], language)
                                self.assertEqual(observed['languages'], [language])
                                self.assertEqual(observed['timeZone'], timezone)
                                self.assertEqual(seen[-1]['accept-language'], language)
                                self.assertEqual(observed['platform'], 'Win32')
                                self.assertIn('Windows NT', observed['ua'])
                                observations.append(observed)
                        self.assertEqual(observations[0], observations[1])


if __name__ == '__main__':
    unittest.main()
