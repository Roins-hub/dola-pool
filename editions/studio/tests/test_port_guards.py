import asyncio
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import web
from test_video_pipeline import pool_module
from test_video_metadata import mp4
import browser
import video_worker_ui as worker
from yarl import URL


@contextmanager
def directory(path):
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


class PortGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_download_login_cookies_stay_on_dola_domains(self):
        from video_worker import download_cookie_jar
        jar = download_cookie_jar([{'name':'sessionid','value':'fixture-private','domain':'.dola.com','path':'/'}])
        self.assertEqual(jar.filter_cookies(URL('https://www.dola.com/video'))['sessionid'].value, 'fixture-private')
        self.assertFalse(jar.filter_cookies(URL('https://cdn.other.test/video')))

    async def test_native30_installs_before_navigation_without_a_submission_rewrite_or_debugger_extension(self):
        class Context:
            def __init__(self): self.scripts, self.routes = [], []
            async def add_init_script(self, script): self.scripts.append(script)
            async def route(self, *args): self.routes.append(args)
        context = Context()
        launches = []
        class Chromium:
            async def launch_persistent_context(self, profile, **kwargs):
                launches.append(kwargs)
                return context
        class Playwright:
            chromium = Chromium()
        with tempfile.TemporaryDirectory() as root, directory(root):
            Path('accounts/fixture').mkdir(parents=True)
            with patch.object(browser.config, 'proxy_for', return_value=None), \
                 patch.object(browser.config, 'account_proxy_raw', return_value=('', 'default')):
                result = await browser.launch_account_context(Playwright(), 'fixture', use_extension=True, duration=30, native30=True)
        self.assertIs(result, context)
        self.assertEqual(len(context.scripts), 1)
        self.assertEqual(context.routes, [])
        self.assertFalse(any('load-extension' in arg for arg in launches[0]['args']))

    async def test_failed_baseline_read_cannot_be_used_as_empty_history(self):
        class Page:
            async def evaluate(self, *args): return {'ok': False, 'status': 502}
        with self.assertRaises(worker.PollingResponseError):
            await worker._read_page_conversation(Page(), '123')

    async def test_download_retries_only_the_existing_media_and_publishes_no_partial_file(self):
        attempts = []
        directory_name = tempfile.TemporaryDirectory()
        root = Path(directory_name.name)
        async def serve(request):
            attempts.append(request.path)
            self.assertEqual(list(root.glob('*.mp4')), [])
            return web.Response(body=b'<html>not a video</html>' if len(attempts) == 1 else mp4(15))
        app = web.Application()
        app.router.add_get('/existing.mp4', serve)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            with patch.object(worker.config, 'DOWNLOAD_DIR', str(root)):
                path = await worker._download(f'http://127.0.0.1:{port}/existing.mp4', 'fixture', proxy_url=None, own_isp=False)
            self.assertEqual(attempts, ['/existing.mp4', '/existing.mp4'])
            self.assertEqual(path.read_bytes(), mp4(15))
            self.assertEqual(list(root.glob('*.part')), [])
        finally:
            await runner.cleanup()
            directory_name.cleanup()
