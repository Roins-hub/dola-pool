"""Fail closed on missing bindings; no reference fetch may choose a second exit."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import config
import media
import test_video_pipeline as pipeline
import video_polling
import video_worker
import test_task_account_retry as task_fixture
from test_browser_account_settings import AccountBrowserSettingsTests, UK


class BindingTests(unittest.TestCase):
    def test_strict_mode_never_uses_global_default_or_skips_broken_binding(self):
        with tempfile.TemporaryDirectory() as root, patch.object(config, 'ISP_ONLY', True, create=True), \
             patch.object(config, 'PROXY', 'http://127.0.0.1:7897'), \
             patch.object(config, 'Path', wraps=Path) as paths:
            paths.side_effect = lambda value: Path(root) if value == 'accounts' else Path(value)
            for mapping in ({}, {'_default':'http://8.8.8.8:11000'}, {'x':'broken'}, {'x':'http://127.0.0.1:7897'}):
                with self.subTest(mapping=mapping), patch.object(config, '_load_proxy_map', return_value=mapping):
                    with self.assertRaisesRegex(ValueError, 'ISP|绑定'):
                        config.proxy_for('x')
            with patch.object(config, '_load_proxy_map', return_value={'x':'http://u:p@8.8.8.8:11000'}):
                self.assertEqual(config.proxy_for('x')['server'], 'http://8.8.8.8:11000')
                (Path(root)/'x').mkdir()
                (Path(root)/'x/proxy.txt').write_text('broken')
                with self.assertRaises(ValueError): config.proxy_for('x')
                (Path(root)/'x/proxy.txt').write_text('')
                with self.assertRaises(ValueError): config.proxy_for('x')


class StrictBrowserTests(AccountBrowserSettingsTests):
    async def test_strict_browser_ignores_old_cdn_switch_and_passes_its_isp_to_cache(self):
        with patch.object(config, 'ISP_ONLY', True, create=True):
            engine, context, _, _, accelerated, _ = await self.run_launch({'cdn_via_local': False})
        self.assertTrue(accelerated, 'strict mode must install the same-ISP cache even on legacy profiles')
        self.assertEqual(engine.launches[0][1].get('service_workers'), 'block')
        self.assertIn('--force-webrtc-ip-handling-policy=disable_non_proxied_udp', engine.launches[0][1]['args'])


class ReferenceRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_reference_failure_never_retries_global_or_direct(self):
        calls = []
        class Session:
            def get(self, url, **kwargs):
                calls.append(kwargs['proxy'])
                raise OSError('ISP unavailable')
        with tempfile.TemporaryDirectory() as root, patch.object(config, 'ISP_ONLY', True, create=True):
            try:
                await media.download_one_image(Session(), 'https://8.8.8.8/a.png', Path(root)/'image',
                                               proxy_url='http://u:p@8.8.4.4:11000')
            except TypeError:
                self.fail('Reference downloader cannot yet accept the selected account ISP')
            except ValueError:
                pass
        self.assertTrue(calls)
        self.assertEqual(set(calls), {'http://u:p@8.8.4.4:11000'})

    async def test_missing_account_for_remote_reference_fails_before_network(self):
        with patch.object(config, 'ISP_ONLY', True, create=True), \
             patch.object(media.aiohttp, 'ClientSession', side_effect=AssertionError('network reached before binding')):
            with self.assertRaisesRegex(ValueError, 'ISP|账号|绑定'):
                await media.download_reference_images(['https://8.8.8.8/a.png'], 'fixture')


class BoundMaterialTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = pipeline.PipelineTests.asyncSetUp
    asyncTearDown = pipeline.PipelineTests.asyncTearDown

    async def test_each_retry_prepares_materials_under_its_own_reserved_account(self):
        prepared=[]
        async def prepare(account):
            self.assertTrue(self.pool._locks[account].locked())
            prepared.append(account)
            return [str(Path(self.root.name)/account/'image.png')]
        async def worker(account, *args, **kwargs):
            self.assertEqual(kwargs['reference_image_paths'], [str(Path(self.root.name)/account/'image.png')])
            if account == 'a': raise RuntimeError('first ISP failed before send')
            return {'account':account}
        with patch.object(pipeline.pool_module, 'generate_video', worker):
            try:
                result = await self.pool.generate_video('fixture', prepare_references=prepare)
            except TypeError:
                self.fail('Pool has no account-bound reference preparation boundary')
        self.assertEqual(prepared, ['a','b'])
        self.assertEqual(result['account'], 'b')

    async def test_unbound_account_is_not_selected_for_strict_pool(self):
        self.proxies.stop()
        with patch.object(config,'ISP_ONLY',True,create=True), \
             patch.object(config,'account_proxy_raw',return_value=('http://127.0.0.1:7897','global')):
            self.assertTrue(self.pool._proxy_state('a')['proxy_blocked'])


class SavedRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_unbound_poll_snapshot_stops_before_network(self):
        with patch.object(config,'ISP_ONLY',True,create=True), \
             patch.object(video_polling,'_load_state',return_value={'proxy_url':'http://127.0.0.1:7897','own_isp':False}):
            with self.assertRaisesRegex(ValueError,'ISP|线路'):
                video_polling.HttpVideoSession('a','123')

    async def test_explicit_missing_download_route_never_connects_direct(self):
        with tempfile.TemporaryDirectory() as root, patch.object(config,'DOWNLOAD_DIR',root), \
             patch.object(config,'ISP_ONLY',True,create=True), \
             patch.object(video_worker.aiohttp,'ClientSession',side_effect=AssertionError('network without ISP')):
            with self.assertRaisesRegex(ValueError,'ISP|线路'):
                await video_worker._download('https://8.8.8.8/v.mp4','a',proxy_url=None,own_isp=False)


class TaskMaterialTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_defers_reference_fetch_and_cleans_each_attempt(self):
        fixture=task_fixture.TaskAccountRetryTests()
        await fixture.asyncSetUp()
        try:
            fixture.ns['config'].ISP_ONLY=True
            prepared=[]
            files=[]
            async def download(refs, task, *, account):
                prepared.append(account)
                root=Path(fixture.root.name)/account
                root.mkdir()
                file=root/'image.png'
                file.write_bytes(b'owned fixture')
                files.append(file)
                return root,[str(file)]
            async def worker(*args, **kwargs):
                self.assertEqual(prepared,[], 'No remote material fetch before account assignment')
                self.assertEqual(kwargs['reference_image_paths'],[])
                first=await kwargs['prepare_references']('a')
                self.assertTrue(Path(first[0]).is_file())
                second=await kwargs['prepare_references']('b')
                self.assertFalse(Path(first[0]).exists())
                self.assertTrue(Path(second[0]).is_file())
                raise RuntimeError('stop before generation')
            fixture.ns['download_reference_images']=download
            await fixture.run_task(worker)
            self.assertEqual(prepared,['a','b'])
            self.assertTrue(all(not f.exists() for f in files))
        finally:
            await fixture.asyncTearDown()
