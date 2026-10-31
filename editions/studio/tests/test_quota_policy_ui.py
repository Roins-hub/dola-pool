"""Render independent Fast day/week quotas with isolated HTTP responses."""
from copy import deepcopy
from pathlib import Path
import unittest
from urllib.parse import urlsplit
from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]

class QuotaPolicyUITests(unittest.IsolatedAsyncioTestCase):
    async def test_day_and_week_balances_coexist_without_a_global_quota_switch(self):
        accounts = [dict(name='fixture-active', category='long_term', scheduling=True,
            login_ok=1, used_today=9, limit=2, created_at=1,
            quota_pools={
                'ordinary_fast10': dict(limit=1, used=1, reserved=0, uncertain=0, remaining=0, period='day'),
                'expert_fast': dict(limit=4, used=1, reserved=0, uncertain=1, remaining=2, period='rolling_7_days')}),
            dict(name='fixture-exhausted', category='long_term', scheduling=True, login_ok=1,
                 quota_pools={
                     'ordinary_fast10': dict(limit=1, used=0, reserved=0, uncertain=0, remaining=1,
                         period='day', blocked=True,
                         reason='Dola 返回无法生成视频（每日免费视频额度已用完），未重新提交：quota receipt'),
                     'expert_fast': dict(limit=4, used=0, reserved=0, uncertain=0, remaining=4,
                         period='rolling_7_days')})]
        before = deepcopy(accounts)
        requests, errors = [], []
        omit_legacy_policy = False

        async def intercept(route):
            request, path = route.request, urlsplit(route.request.url).path
            if path.startswith('/api/'):
                requests.append((path, request.method))
                if request.method != 'GET':
                    await route.fulfill(status=409, json={'detail': 'Read-only UI fixture'})
                    return
                if path == '/api/admin/accounts':
                    data = {'accounts': accounts, 'categories': [{'id': 'long_term', 'enabled': True}]}
                    if not omit_legacy_policy:
                        data['quota_policy'] = {'mode': 'seedance25', 'limit': 2}
                else:
                    data = {'tasks': [], 'jobs': {}, 'accounts': [], 'groups': []}
                await route.fulfill(json=data)
                return
            file = ROOT / 'web' / ('index.html' if path == '/admin' else path.lstrip('/'))
            if not file.is_file():
                await route.fulfill(status=404, body='')
                return
            mime = {'.html': 'text/html', '.css': 'text/css', '.js': 'application/javascript'}[file.suffix]
            await route.fulfill(body=file.read_bytes(), content_type=mime)

        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch(headless=True)
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
            await context.add_init_script("localStorage.setItem('dola_token','fixture-session')")
            await context.route('**/*', intercept)
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            try:
                await page.goto('https://fixture.test/admin#accounts')
                current = page.locator('#accountQuotaCurrent')
                await current.filter(has_text='每次使用满7天恢复1次').wait_for()
                self.assertIn('普通10秒每号每天1次', await current.text_content())
                self.assertIn('北京时间凌晨01:00重置', await current.text_content())
                self.assertIn('两条15秒只计1次', await current.text_content())
                self.assertTrue(await page.locator('#accountQuotaFast').is_hidden())
                self.assertTrue(await page.locator('#accountQuota25').is_hidden())
                row = page.locator('#accountsTable tbody tr').filter(has_text='fixture-active')
                cell = row.locator('td').nth(5)
                quota = await cell.inner_text()
                self.assertEqual('Fast 1/1\n专家 1/4', quota)
                ordinary_detail = await cell.locator('div').nth(0).get_attribute('title')
                self.assertIn('剩余 0 / 1 次/日', ordinary_detail)
                self.assertIn('北京时间凌晨01:00重置', ordinary_detail)
                expert_detail = await cell.locator('div').nth(1).get_attribute('title')
                self.assertIn('已用 1', expert_detail)
                self.assertIn('待核对 1', expert_detail)
                self.assertIn('剩余 2 / 4 次/7天', expert_detail)
                self.assertIn('每次使用满7天恢复1次', expert_detail)
                self.assertNotIn('9 / 2', quota)
                exhausted = page.locator('#accountsTable tbody tr').filter(has_text='fixture-exhausted').locator('td').nth(5)
                self.assertEqual('Fast 1/1\n专家 0/4', await exhausted.inner_text())
                exhausted_detail = await exhausted.locator('div').first.get_attribute('title')
                self.assertIn('平台已确认额度用完', exhausted_detail)
                self.assertIn('已用 0', exhausted_detail)
                self.assertIn('剩余 0 / 1', exhausted_detail)
                for width in (1440, 375):
                    await page.set_viewport_size({'width': width, 'height': 1000})
                    self.assertTrue(await page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                omit_legacy_policy = True
                await page.reload()
                await page.locator('#accountsTable tbody tr').filter(has_text='fixture-active').filter(has_text='专家 1/4').wait_for()
                self.assertEqual(accounts, before)
                self.assertFalse(any(method != 'GET' for _, method in requests))
                self.assertEqual(errors, [])
            finally:
                await browser.close()
