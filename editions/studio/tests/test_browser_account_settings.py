import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from test_video_pipeline import pool_module
import browser


UK = {'server':'http://192.0.2.11:11000', 'username':'fixture', 'password':'private'}


class Context:
    def __init__(self):
        self.scripts, self.handlers = [], {}
    async def add_init_script(self, script):
        self.scripts.append(script)
    def on(self, event, handler):
        self.handlers[event] = handler
    async def route(self, *args):
        pass
    async def close(self):
        if handler := self.handlers.get('close'):
            await handler(self)


class Engine:
    def __init__(self):
        self.chromium = self
        self.context = Context()
        self.launches = []
    async def launch_persistent_context(self, profile, **kwargs):
        self.launches.append((profile, kwargs))
        return self.context


class Bridge:
    def __init__(self, isp, first_hop):
        self.isp, self.first_hop = isp, first_hop
        self.browser_proxy = {'server':'http://127.0.0.1:31001'}
        self.closed = False
    async def __aenter__(self):
        return self
    async def close(self):
        self.closed = True
    async def authorize_request(self, route):
        await route.fallback()


class AccountBrowserSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def run_launch(self, options, override=None, proxy=UK):
        with tempfile.TemporaryDirectory() as root:
            account = Path(root)/'fixture'
            account.mkdir()
            (account/'browser.json').write_text(json.dumps(options), encoding='utf-8')
            engine = Engine()
            with patch.object(browser, 'Path', wraps=Path) as paths, \
                 patch.object(browser.config, 'proxy_for', return_value=proxy), \
                 patch.object(browser.config, 'account_proxy_raw', return_value=('', 'file')), \
                 patch.object(browser.proxy_usage.usage, 'ensure_available', return_value={'remaining':99,'limit':100}), \
                 patch.object(browser.proxy_usage, 'attach_request_counter', new_callable=AsyncMock) as counter, \
                 patch.object(browser, 'install_cdn_accelerator', new_callable=AsyncMock) as accelerator, \
                 patch.object(browser, '_inject_saved_cookies', new_callable=AsyncMock), \
                 patch.object(browser, 'ProxyChainBridge', side_effect=Bridge, create=True) as bridge:
                paths.side_effect = lambda value: Path(root) if value == 'accounts' else Path(value)
                context = await browser.launch_account_context(engine, 'fixture', headless=True,
                                                               native_duration=5, proxy_override=override)
                preferences = json.loads((account/'Default/Preferences').read_text())
                return engine, context, preferences, counter.call_args, accelerator.called, bridge.call_args

    async def test_account_uses_british_environment_and_a_pinned_owned_chain(self):
        options = {'locale':'en-GB','accept_language':'en-GB,en;q=0.9',
                   'timezone_id':'Europe/London','viewport':{'width':1365,'height':768},
                   'required_proxy_server':UK['server'], 'proxy_chain':'http://127.0.0.1:7897',
                   'cdn_via_local':False}
        engine, context, prefs, counter, accelerated, bridge = await self.run_launch(options)
        profile, launch = engine.launches[0]
        self.assertEqual(Path(profile).name, 'fixture')
        self.assertEqual(launch['locale'],'en-GB')
        self.assertEqual(launch['timezone_id'],'Europe/London')
        self.assertEqual(launch['viewport'],options['viewport'])
        self.assertEqual(launch['extra_http_headers']['Accept-Language'],options['accept_language'])
        self.assertEqual(launch['proxy'],{'server':'http://127.0.0.1:31001'})
        self.assertNotIn('user_agent',launch)
        self.assertEqual(prefs['intl']['accept_languages'],'en-GB,en')
        self.assertEqual(counter.args[2], UK)
        self.assertFalse(accelerated)
        self.assertEqual(bridge.args[0], UK)
        self.assertEqual(bridge.args[1], {'server':'http://127.0.0.1:7897'})
        await context.close()

    async def test_missing_or_other_country_proxy_is_rejected_before_browser_launch(self):
        for proxy in (None, {'server':'http://192.0.2.14:11000'}):
            with self.subTest(proxy=proxy), self.assertRaisesRegex(ValueError,'指定|绑定|pinned'):
                await self.run_launch({'required_proxy_server':UK['server']},proxy=proxy)

    async def test_explicit_proxy_override_cannot_replace_british_binding(self):
        with self.assertRaisesRegex(ValueError,'指定|绑定|pinned'):
            await self.run_launch({'required_proxy_server':UK['server']},
                                  override='http://192.0.2.14:11000')

    async def test_direct_authenticated_isp_transport_does_not_use_global_front_proxy(self):
        _, _, _, _, _, bridge = await self.run_launch({'isp_transport':True})
        self.assertEqual(bridge.args[0], UK)
        self.assertIsNone(bridge.args[1])

    async def test_language_sync_changes_only_the_interface_cookie(self):
        cookies = [{'name':'i18next','value':'zh','domain':'.dola.com','path':'/'},
                   {'name':'sessionid','value':'secret','domain':'.dola.com','path':'/'},
                   {'name':'store-country-code','value':'mo','domain':'.dola.com','path':'/'}]
        class Cookies:
            updated = []
            async def cookies(self, *_): return cookies
            async def add_cookies(self, values): self.updated += values
        context = Cookies()
        self.assertTrue(hasattr(browser, 'sync_site_language'))
        await browser.sync_site_language(context, 'en')
        self.assertEqual(context.updated,[{**cookies[0],'value':'en'}])
        self.assertEqual(cookies[1]['value'],'secret')
        self.assertEqual(cookies[2]['value'],'mo')
