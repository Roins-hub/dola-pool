"""Render terminal policy refusals in both workspaces without live services."""
from pathlib import Path
import unittest
from urllib.parse import urlsplit

from patchright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
NOTICE = '平台已拒绝生成，本地本次额度已释放；请修改内容后手动提交。'
BADGE = '生成失败 · 内容限制（未计额度）'


class PolicyFailureUITests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_no_charge_failure_is_clear_without_mislabeling_other_failures_or_old_videos(self):
        tasks = [
            {'id': 'video_policy', 'status': 'failed', 'phase': 'platform_rejected_no_charge', 'prompt': '明确政策拒绝', 'error': 'Platform input policy rejected the request.'},
            {'id': 'video_rejected', 'status': 'failed', 'phase': 'platform_rejected', 'prompt': '普通拒绝'},
            {'id': 'video_copyright', 'status': 'failed', 'phase': 'platform_copyright', 'prompt': '版权限制'},
            {'id': 'video_unknown', 'status': 'failed', 'prompt': '旧失败记录', 'dola_reply': 'Policy denied. 0 credits.'},
            {'id': 'video_history', 'status': 'completed', 'phase': 'platform_rejected_no_charge', 'prompt': '历史可用视频', 'actual_duration': 15.105, 'video_url': '/videos/history.mp4'},
        ]
        for task in tasks:
            task.update(model='seedance-2.0', duration=15, created_at=1)
        requests, errors = [], []

        async def intercept(route):
            request, path = route.request, urlsplit(route.request.url).path
            requests.append((path, request.method))
            if path == '/api/me':
                data = {'user': {'email': 'fixture@example.test', 'role': 'member'},
                        'keys': [{'key': 'fixture-key', 'enabled': True, 'allowed_durations': [15]}],
                        'usage': {}, 'supported_durations': [5, 10, 15, 30]}
            elif path in ('/api/admin/tasks', '/api/me/tasks'):
                data = {'tasks': tasks}
            elif path.startswith('/api/admin/tasks/') and path.count('/') == 4:
                data = {'task': next(task for task in tasks if task['id'] == path.rsplit('/', 1)[1]), 'events': []}
            elif path.startswith('/api/'):
                data = {'tasks': [], 'accounts': [], 'jobs': {}, 'categories': [], 'groups': []}
            elif path.startswith('/videos/'):
                await route.fulfill(content_type='video/mp4', body=b'')
                return
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
            out = ROOT / '.impeccable/review/policy-failure-v060'
            out.mkdir(parents=True, exist_ok=True)
            try:
                for role in ('admin', 'member'):
                    await page.goto('https://fixture.test/' + role + ('#gallery' if role == 'admin' else ''))
                    result = page.locator('#' + ('adminResult' if role == 'admin' else 'memberResult'))
                    await result.filter(has_text=BADGE).wait_for(timeout=5000)
                    self.assertIn(NOTICE, await result.text_content())
                    self.assertTrue(await result.locator('.badge.b-red').filter(has_text=BADGE).is_visible())
                    container = '#adminRecentTasks' if role == 'admin' else '#memberRecent'
                    attribute = 'data-task-detail' if role == 'admin' else 'data-member-task'
                    for task in tasks:
                        row = page.locator(container + f' tr:has([{attribute}="{task["id"]}"])')
                        if task['id'] == 'video_policy':
                            self.assertIn(BADGE, await row.text_content())
                        else:
                            self.assertNotIn('未计额度', await row.text_content())
                        await row.locator(f'[{attribute}]').click()
                        drawer = page.locator('#detailDrawer')
                        await drawer.filter(has_text=task['prompt']).wait_for()
                        if task['id'] == 'video_policy':
                            self.assertIn(NOTICE, await drawer.text_content())
                            self.assertTrue(await drawer.locator('.badge.b-red').filter(has_text=BADGE).first.is_visible())
                            for width in (1440, 375):
                                await page.set_viewport_size({'width': width, 'height': 1000})
                                await drawer.get_by_text(NOTICE, exact=True).scroll_into_view_if_needed()
                                self.assertTrue(await page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                                await page.screenshot(path=str(out / f'{role}-failure-{width}.png'))
                        else:
                            self.assertNotIn('本地本次额度已释放', await drawer.text_content())
                        if task['id'] == 'video_history':
                            self.assertEqual(urlsplit(await drawer.locator('video').get_attribute('src')).path, '/videos/history.mp4')
                        await page.keyboard.press('Escape')
                    await page.set_viewport_size({'width': 1440, 'height': 1000})
                    await page.locator('.sidebar button[' + ('data-tab="tasks"' if role == 'admin' else 'data-view="tasks"') + ']').click()
                    await page.select_option('#' + ('taskState' if role == 'admin' else 'memberTaskState'), 'failed')
                    table = page.locator('#' + ('tasksTable' if role == 'admin' else 'memberTasks'))
                    self.assertIn(BADGE, await table.text_content())
                self.assertEqual(errors, [])
                self.assertFalse(any(method != 'GET' for path, method in requests if path.startswith('/api/')))
            finally:
                await browser.close()
