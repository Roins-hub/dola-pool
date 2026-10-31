"""Native login QR extraction against the observed SVG container markup."""
import base64
from io import BytesIO
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from patchright.async_api import async_playwright
from providers.doubao import DoubaoProvider, ProviderError
from providers.doubao.runtime import BrowserSession


class NativeQRTests(unittest.IsolatedAsyncioTestCase):
    async def fixture(self, html):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.context = await self.playwright.chromium.launch_persistent_context(
            str(Path(self.temp.name) / 'profile'), headless=True)
        self.addAsyncCleanup(self.context.close)
        await self.context.route('**/*', lambda route: route.fulfill(status=200,
            content_type='text/html; charset=utf-8', body=html))
        page = await self.context.new_page()
        await page.goto('https://www.doubao.com/chat/')
        self.session = BrowserSession(DoubaoProvider(root=Path(self.temp.name) / 'pool', enabled=False),
                                      {'name': 'qr-fixture', 'account_id': 'qr-fixture'})
        self.session.context, self.session.page = self.context, page

    async def test_official_svg_qr_container_is_returned_without_phone_submit(self):
        # Structure and data-testid observed on the official UK-ISP login dialog.
        await self.fixture('''<button>登录</button><div role="dialog">
            <h2>使用豆包或飞书账号登录</h2><button onclick="window.phoneClicked=true">手机号登录</button>
            <div data-testid="qrcode_image" style="width:162px;height:162px"><svg width="162" height="162" viewBox="0 0 162 162"
                xmlns="http://www.w3.org/2000/svg" shape-rendering="crispEdges">
                <defs><clipPath id="clip-path-dot-color-0"><circle cx="10" cy="10" r="2"/></clipPath></defs>
                <rect width="162" height="162" fill="white"/><rect x="8" y="8" width="40" height="40"/>
            </svg></div><p>打开 豆包 / 飞书 App 扫码登录</p></div>''')
        result = await self.session.login_step()
        self.assertEqual(result['state'], 'waiting_scan')
        raw = base64.b64decode(result['qr_image'].split(',', 1)[1])
        with Image.open(BytesIO(raw)) as image:
            self.assertEqual(image.format, 'PNG')
            self.assertGreaterEqual(image.width, 160)
            self.assertGreaterEqual(image.height, 160)
            self.assertLess(abs(image.width - image.height), 3)
        self.assertIsNone(await self.session.page.evaluate('()=>window.phoneClicked', isolated_context=False))

    async def test_unrelated_large_svg_is_not_returned_as_login_qr(self):
        await self.fixture('''<button>登录</button><div role="dialog"><h2>登录</h2>
            <svg width="162" height="162"><rect width="162" height="162"/></svg></div>''')
        result = await self.session.login_step()
        self.assertEqual(result['state'], 'preparing')
        self.assertNotIn('qr_image', result)

    async def test_official_qr_error_stops_waiting_with_readable_network_reason(self):
        await self.fixture('''<div role="dialog"><h2>使用豆包或飞书账号登录</h2>
            <button data-testid="login_phone_verify_button">手机号登录</button>
            <div><p>二维码获取失败</p></div><p>打开 豆包 / 飞书 App 扫码登录</p></div>''')
        with self.assertRaises(ProviderError) as caught:
            await self.session.login_step()
        self.assertEqual(caught.exception.code, 'qr_unavailable')
        self.assertEqual(caught.exception.status_code, 503)
        self.assertIn('账号服务', str(caught.exception))
        self.assertIn('未自动切换', str(caught.exception))

    async def test_user_text_and_unrelated_dialog_qr_error_do_not_fail_login(self):
        await self.fixture('''<main><p>二维码获取失败</p></main>
            <div role="dialog"><h2>查看消息</h2><p>二维码获取失败</p></div>''')
        result = await self.session.login_step()
        self.assertEqual(result['state'], 'preparing')

    async def test_loading_official_login_dialog_remains_preparing(self):
        await self.fixture('''<div role="dialog"><h2>使用豆包或飞书账号登录</h2>
            <button data-testid="login_phone_verify_button">手机号登录</button>
            <div data-testid="qrcode_image">正在加载</div><p>打开 豆包 / 飞书 App 扫码登录</p></div>''')
        result = await self.session.login_step()
        self.assertEqual(result['state'], 'preparing')


if __name__ == '__main__':
    unittest.main()
