"""Full admin page import smoke test, with all network traffic confined to fixtures."""
import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit
from patchright.async_api import async_playwright


class GoogleImportPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_dialog_posts_credentials_and_pending_account_stays_disabled(self):
        accounts, posted, errors = [], [], []
        categories = [{'id':'disposable','label':'日抛号','enabled':True,'count':0},
                      {'id':'long_term','label':'接码长期号','enabled':True,'count':0}]
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={'width':1440,'height':1000})
                page.on('pageerror', lambda error: errors.append(str(error)))
                async def route_request(route):
                    path = urlsplit(route.request.url).path
                    data = {}
                    if path == '/api/admin/accounts/import-google':
                        posted.append(route.request.post_data_json)
                        accounts.append(dict(name='google_fixture',email='owner@example.com',category='disposable',
                            scheduling=True,category_enabled=True,category_override=False,effective_scheduling=False,
                            onboarding_state='queued',onboarding_error='',login_ok=None,verification_required='',
                            used_today=0,limit=2,busy=False,cooling=False,rate_limited=False,quota_blocked=False))
                        data = {'ok':True,'imported':1,'skipped':0}
                    elif path == '/api/admin/accounts':
                        data = {'accounts':accounts,'categories':categories}
                    elif path == '/api/admin/jobs':
                        data = {'jobs':{}}
                    elif path == '/api/admin/proxies':
                        data = {'accounts':[],'groups':[]}
                    elif not path.startswith('/api/'):
                        name = 'index.html' if path in ('/','/admin') else path.lstrip('/')
                        source = Path('web') / name
                        if source.is_file() and source.resolve().parent == Path('web').resolve():
                            kind = 'text/javascript' if name.endswith('.js') else 'text/css' if name.endswith('.css') else 'text/html'
                            await route.fulfill(body=source.read_bytes(),content_type=kind+'; charset=utf-8')
                        else:
                            await route.fulfill(status=404,body='fixture not found')
                        return
                    await route.fulfill(body=json.dumps(data),content_type='application/json')
                await page.route('**/*',route_request)
                await page.goto('http://fixture.test/admin#accounts')
                await page.get_by_role('button',name='导入 Google 账号',exact=True).click()
                await page.locator('#googleImportText').fill('owner@example.com----fixture-password----backup@example.net')
                await page.get_by_role('button',name='导入并登录',exact=True).click()
                await page.wait_for_function("document.querySelector('#googleImportStatus').textContent.includes('已导入 1 个')")
                self.assertEqual(posted, [{'text':'owner@example.com----fixture-password----backup@example.net','category':'disposable'}])
                self.assertEqual(await page.locator('#googleImportText').input_value(),'')
                await page.locator('#dlg').get_by_role('button',name='关闭',exact=True).click()
                await page.locator('[data-account-view="disabled"]').click()
                await page.locator('[data-account-category="google_fixture"]').wait_for()
                self.assertIn('等待登录',await page.locator('#accountsTable').inner_text())
                self.assertEqual(await page.locator('[data-account-restore="google_fixture"]').count(),0)
                self.assertEqual(errors,[])
            finally:
                await browser.close()


if __name__ == '__main__':
    unittest.main()
