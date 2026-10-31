"""Exercise published model choices and payloads without touching live services."""
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]


class PublishedModelsUITests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_catalog_falls_back_to_fast_while_delisted_history_remains_viewable(self):
        requests, errors = [], []
        member_key_durations=[5,10,15,30]
        history=[{'id':'video_history_10','model':'seedance-1.0','duration':10,'prompt':'历史1.0提示词','status':'completed','created_at':1,'video_url':'/videos/history-10.mp4'},
                 {'id':'video_history_25','model':'seedance-2.5','duration':30,'prompt':'历史2.5提示词','status':'completed','created_at':2,'video_url':'/videos/history-25.mp4'},
                 {'id':'video_new15','model':'seedance-2.0','duration':15,'duration_profile':'fast15-via30','actual_duration':15.105,'prompt':'新15秒历史记录','status':'completed','created_at':3,'video_url':'/videos/new15.mp4','request_evidence':{'model':'seedance_v2.0','duration':30,'audit_status':'passed'}},
                 {'id':'video_legacy30','model':'seedance-2.0','duration':30,'actual_duration':15.1,'prompt':'旧30秒记录','status':'completed','created_at':4,'video_url':'/videos/legacy30.mp4'}]

        async def intercept(route):
            request = route.request
            path = urlsplit(request.url).path
            if path in ('/api/admin/generate', '/v1/videos/generations'):
                body = request.post_data_json
                requests.append((path, body))
                data = {'id': 'video_' + 'c' * 32, 'status': 'queued', **body}
            elif path == '/api/me':
                data = {'user': {'email': 'fixture@example.test', 'role': 'member'},
                        'keys': [{'key': 'fixture-key', 'enabled': True, 'allowed_durations': member_key_durations}],
                        'usage': {}, 'supported_durations': [5, 10, 15, 30],
                        'supported_durations_by_model': {'seedance-1.0': [5, 10, 15], 'seedance-2.0': [5, 10, 15, 30],
                                                         'seedance-2.5': [5, 10, 15, 30]}}
            elif path.endswith('/reference-images/config'):
                data = {'max_count': 30, 'max_bytes': 15 * 1024 * 1024}
            elif path.endswith('/reference-media/config'):
                data = {'max_count': 10, 'max_bytes': 50 * 1024 * 1024}
            elif path in ('/api/admin/tasks','/api/me/tasks'):
                data={'tasks':history}
            elif path.startswith('/api/admin/tasks/') and path.count('/') == 4:
                task=next(task for task in history if task['id']==path.rsplit('/',1)[1])
                data={'task':task,'events':[]}
            elif path.startswith('/videos/'):
                await route.fulfill(content_type='video/mp4',body=b'')
                return
            elif path.startswith('/api/'):
                data = {'tasks': [], 'accounts': [], 'categories': []}
            else:
                filename = {'/admin': 'index.html', '/member': 'member.html'}.get(path, path.lstrip('/'))
                file = ROOT / 'web' / filename
                if not file.is_file():
                    await route.fulfill(status=404, body='')
                    return
                mime = {'.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css'}[file.suffix]
                await route.fulfill(body=file.read_bytes(), content_type=mime)
                return
            await route.fulfill(json=data)

        async with async_playwright() as runtime:
            browser = await runtime.chromium.launch(headless=True)
            context = await browser.new_context(viewport={'width': 1440, 'height': 1000})
            await context.add_init_script("localStorage.setItem('dola_token','fixture-session')")
            await context.route('**/*', intercept)
            page = await context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            out = ROOT / '.impeccable/review/fast15-profile-v058'
            out.mkdir(parents=True, exist_ok=True)
            try:
                for role, prefix, submit, endpoint in (
                    ('admin', 'gen', 'genBtn', '/api/admin/generate'),
                    ('member', 'member', 'memberSubmit', '/v1/videos/generations'),
                ):
                    await page.goto('https://fixture.test/' + role + ('#gallery' if role == 'admin' else ''))
                    await page.locator('#' + submit).wait_for(state='visible')
                    model, duration = '#' + prefix + 'Model', '#' + prefix + 'Duration'
                    self.assertEqual(await page.locator(model+' option').evaluate_all('(nodes)=>nodes.filter(n=>!n.hidden&&!n.disabled).map(n=>n.value)'),['seedance-2.0'])
                    for delisted in ('seedance-1.0','seedance-2.5'):
                        option=page.locator(model+f' option[value="{delisted}"]')
                        self.assertEqual(await option.count(),1,'configuration stays available for restoration')
                        self.assertTrue(await option.evaluate('(node)=>node.hidden&&node.disabled'))
                    for delisted in ('seedance-1.0','seedance-2.5'):
                        await page.locator(model).evaluate('(node,value)=>{node.value=value;node.dispatchEvent(new Event("change",{bubbles:true}));}',delisted)
                        self.assertEqual(await page.locator(model).input_value(),'seedance-2.0')
                    self.assertEqual(await page.locator(model).input_value(),'seedance-2.0')
                    self.assertEqual(await page.locator(duration).input_value(),'10')
                    self.assertEqual(sorted(await page.locator(duration+' option').evaluate_all('(nodes)=>nodes.map(n=>n.value)')),['10','5'])
                    self.assertEqual(await page.locator(duration+' option[value="30"]').count(),0)
                    self.assertEqual(await page.locator(duration+' option[value="15"]').count(),0)
                    self.assertEqual(await page.locator(duration+' option[value="15-normal"]').count(),0)
                    await page.select_option(duration,'10')
                    hint='#'+('genFast15Hint' if role=='admin' else 'memberFast15Hint')
                    self.assertTrue(await page.locator(hint).is_hidden())
                    await page.fill('#'+prefix+'Prompt','  Fast普通10秒原文\n保持内容  ')
                    for width in (1440,375):
                        await page.set_viewport_size({'width':width,'height':1000})
                        self.assertTrue(await page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                        await page.locator(duration).scroll_into_view_if_needed()
                        await page.screenshot(path=str(out/f'{role}-{width}.png'))
                    await page.locator('#'+submit).click()
                    await page.locator('#'+('genStatus' if role=='admin' else 'memberStatus')).filter(has_text='已提交').wait_for()
                    body=next(body for path,body in reversed(requests) if path==endpoint)
                    self.assertEqual((body['model'],body['duration'],body['prompt']),('seedance-2.0',10,'  Fast普通10秒原文\n保持内容  '))
                    self.assertNotIn('duration_profile',body)
                    if role=='member':
                        key_text=await page.locator('#memberKeys').text_content()
                        self.assertIn('Fast：',key_text)
                        self.assertNotIn('1.0：',key_text);self.assertNotIn('2.5：',key_text)
                    for task in history:
                        button='#'+('adminRecentTasks' if role=='admin' else 'memberRecent')+' ['+('data-task-detail' if role=='admin' else 'data-member-task')+f'="{task["id"]}"]'
                        await page.locator(button).click()
                        drawer=page.locator('#detailDrawer')
                        await drawer.filter(has_text=task['model']).wait_for()
                        self.assertIn(task['prompt'],await drawer.text_content())
                        if task.get('duration_profile')=='fast15-via30':
                            self.assertIn('目标 15 秒',await drawer.text_content())
                            self.assertIn('实际 15.105 秒',await drawer.text_content())
                            if role=='admin':self.assertIn('上游时长参数30秒（新15秒链路）',await drawer.text_content())
                        elif task['id']=='video_legacy30':
                            self.assertIn('请求 30 秒',await drawer.text_content())
                            self.assertNotIn('新15秒链路',await drawer.text_content())
                        video_src=urlsplit(await drawer.locator('video').get_attribute('src'))
                        self.assertEqual(video_src.path,task['video_url'])
                        self.assertIn(video_src.netloc,('', 'fixture.test'))
                        await page.keyboard.press('Escape')
                    if role=='member':
                        member_key_durations[:]=[30]
                        await page.goto('https://fixture.test/member')
                        await page.locator('#memberDuration option[value=""]').wait_for(state='attached')
                        self.assertTrue(await page.locator('#memberSubmit').is_disabled())
                        self.assertIn('无可用',await page.locator('#memberDuration').text_content())
                self.assertEqual(errors, [])
            finally:
                await browser.close()
