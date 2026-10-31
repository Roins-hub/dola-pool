"""Real account UI with isolated fixtures; no production account or task requests."""
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from patchright.async_api import async_playwright


ROOT = Path(__file__).resolve().parents[1]


class RotationUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.accounts = [
            dict(name='fixture-retry', scheduling=True, login_ok=1, used_today=0,
                 limit=4, send_count=1, dispatch_count=3, last_sent_at=1792560000,
                 proxy_cooling=True, proxy_cooling_until=1792560060,
                 proxy_cooling_reason='连接重置 <img src=x onerror="alert(1)">'),
            dict(name='fixture-ready', scheduling=True, login_ok=1, used_today=2,
                 limit=4, send_count=0, dispatch_count=2),
            dict(name='fixture-legacy', scheduling=True, login_ok=1, used_today=1,
                 limit=4, dispatch_count=9),
        ]
        self.errors = []
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch(headless=True)
        self.context = await self.browser.new_context(viewport={'width': 1440, 'height': 1000})
        await self.context.add_init_script("localStorage.setItem('dola_token','fixture-session')")

        async def intercept(route):
            request, path = route.request, urlsplit(route.request.url).path
            self.assertEqual(request.method, 'GET', 'This test must never submit a job or mutate accounts')
            if path == '/api/admin/accounts':
                await route.fulfill(json={
                    'accounts': self.accounts,
                    'categories': [{'id': 'long_term', 'enabled': True}],
                    'quota_policy': {'mode': 'fast', 'limit': 4},
                })
            elif path.startswith('/api/'):
                await route.fulfill(json={'jobs': {}, 'groups': [], 'accounts': [], 'tasks': []})
            else:
                file = ROOT / 'web' / ('index.html' if path == '/admin' else path.lstrip('/'))
                if file.is_file():
                    mime = 'text/javascript' if file.suffix == '.js' else 'text/css' if file.suffix == '.css' else 'text/html'
                    await route.fulfill(body=file.read_bytes(), content_type=mime)
                else:
                    await route.fulfill(status=404, body='')

        await self.context.route('**/*', intercept)
        self.page = await self.context.new_page()
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        await self.page.goto('https://rotation.test/admin#accounts')
        await self.page.locator('#accountsTable tbody tr').filter(has_text='fixture-retry').wait_for()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()

    async def test_current_quota_priority_keeps_lifetime_counts_in_details(self):
        row = self.page.locator('#accountsTable tbody tr').filter(has_text='fixture-retry')
        usage = await row.locator('td').nth(5).text_content()
        self.assertIn('0 / 4', usage)
        self.assertNotIn('累计发送', usage)
        ready = self.page.locator('#accountsTable tbody tr').filter(has_text='fixture-ready')
        self.assertNotIn('累计发送', await ready.text_content())
        legacy = self.page.locator('#accountsTable tbody tr').filter(has_text='fixture-legacy')
        self.assertNotIn('累计发送', await legacy.text_content(), 'An older response must not invent a zero count')
        await row.locator('[data-account-detail]').click()
        drawer = self.page.locator('#detailDrawer')
        await drawer.locator('dt', has_text='浏览器尝试').wait_for()
        self.assertEqual(await drawer.locator('dt', has_text='累计发送次数').count(), 1)
        async def field(label):
            return await drawer.locator('dt').filter(has_text=label).locator('+ dd').text_content()
        self.assertEqual(await field('累计发送次数'), '1 次')
        self.assertEqual(await field('浏览器尝试'), '3 次')
        self.assertNotEqual(await field('最近发送'), '—')
        self.assertIn('当前所选模式的额度占用最少', await drawer.text_content())
        self.assertIn('Fast 与专家额度分别计算', await drawer.text_content())
        self.assertIn('已用、预占和待核对均计入占用', await drawer.text_content())
        self.assertIn('发送前页面或线路失败不计入', await drawer.text_content())
        self.assertIn('不等于扣费或出片次数', await drawer.text_content())
        self.assertIn('累计发送次数仅作历史记录，不参与选号优先级', await drawer.text_content())
        self.assertEqual(self.errors, [])

    async def test_route_cooldown_is_visible_safe_and_excluded_from_valid_filter(self):
        row = self.page.locator('#accountsTable tbody tr').filter(has_text='fixture-retry')
        status = await row.locator('td').nth(2).text_content()
        self.assertIn('线路重试等待', status)
        self.assertIn('重试时间', status)
        self.assertIn('连接重置 <img', status)
        self.assertNotIn('有效', status)
        self.assertEqual(await row.locator('img').count(), 0, 'Failure details must remain text')
        await self.page.select_option('#accountState', 'limited')
        self.assertEqual(await self.page.locator('#accountsTable tbody tr').count(), 1)
        self.assertIn('fixture-retry', await self.page.locator('#accountsTable').text_content())
        await self.page.select_option('#accountState', 'valid')
        self.assertEqual(await self.page.locator('#accountsTable tbody tr').count(), 2)
        self.assertNotIn('fixture-retry', await self.page.locator('#accountsTable').text_content())
        self.assertEqual(self.errors, [])
