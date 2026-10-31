"""Unlimited ISP statistics render without fictitious remaining request balances."""
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]


class ProxyStatisticsUITests(unittest.IsolatedAsyncioTestCase):
    async def test_unlimited_route_hides_limit_controls_and_preserves_pause_reset(self):
        quota = dict(key='fixture', used=12345, limit=1000, remaining=None,
                     unlimited=True, paused=False, blocked=False, exhausted=False, warn=False)
        data = dict(configured=1, total=1, independent=1, shared_groups=0, missing=0,
                    network_mode='isp_only',
                    summary=dict(used=12345, limit=None, unlimited=True, count=1, available=1),
                    accounts=[dict(name='fixture', own=True, source='file', host='192.0.2.10', port=11000, quota=quota)],
                    groups=[dict(server='http://192.0.2.10:11000', own=True, count=1, accounts=['fixture'], quota=quota)])
        errors = []
        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.add_init_script("localStorage.setItem('dola_token','fixture-session')")
                async def intercept(route):
                    path = urlsplit(route.request.url).path
                    self.assertEqual(route.request.method, 'GET')
                    if path == '/api/admin/proxies':
                        await route.fulfill(json=data)
                    elif path.startswith('/api/'):
                        await route.fulfill(json={'accounts': [], 'jobs': {}, 'tasks': []})
                    else:
                        file = ROOT / 'web' / ('index.html' if path == '/admin' else path.lstrip('/'))
                        if file.is_file():
                            mime = {'.js':'text/javascript', '.css':'text/css'}.get(file.suffix, 'text/html')
                            await route.fulfill(body=file.read_bytes(), content_type=mime)
                        else:
                            await route.fulfill(status=404, body='')
                await context.route('**/*', intercept)
                page = await context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                await page.goto('https://statistics.test/admin#proxies')
                await page.locator('.proxy-group').wait_for()
                summary = await page.locator('#proxyQuota').inner_text()
                self.assertIn('次数不限', summary)
                self.assertIn('12,345', summary)
                self.assertNotIn('剩余请求', summary)
                self.assertIn('供应商', summary)
                await page.locator('.proxy-group summary').click()
                group = page.locator('.proxy-group')
                self.assertIn('次数不限', await group.inner_text())
                self.assertEqual(await group.get_by_role('button', name='改上限').count(), 0)
                self.assertTrue(await group.get_by_role('button', name='暂停', exact=True).is_visible())
                self.assertTrue(await group.get_by_role('button', name='重置已用').is_visible())
                self.assertEqual(errors, [])
            finally:
                await browser.close()
