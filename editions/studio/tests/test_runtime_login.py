"""Login loss must reach the account pool without causing duplicate generation."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import requests

from test_video_pipeline import pool_module
import video_worker_http as http_worker
import video_worker_ui as worker
from navigation_errors import BrowserSetupError


class RuntimeLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.root = tempfile.TemporaryDirectory()
        (Path(self.root.name) / 'fixture').mkdir()
        self.pool = pool_module.BrowserPool(self.root.name, ':memory:')
        self.pool.accounts
        self.pool.set_login_status('fixture', True)

    async def asyncTearDown(self):
        self.pool._conn.close()
        self.root.cleanup()

    async def test_resumed_task_marks_login_lost_and_stops_scheduling(self):
        async def expired(*args, **kwargs):
            raise worker.LoginExpiredError('登录已过期')
        with patch.object(pool_module, 'resume_video', expired):
            with self.assertRaises(worker.LoginExpiredError):
                await self.pool.resume_video('fixture', '123', 60)
        row = self.pool.list_accounts()[0]
        self.assertEqual(row['login_ok'], 0)
        self.assertIn('登录', row.get('login_reason', ''))
        self.assertGreater(row['login_checked_at'], 0)
        self.assertFalse(self.pool._schedulable(row))
        self.assertFalse(row['busy'])
        self.pool.set_login_status('fixture', True)
        self.assertEqual(self.pool.list_accounts()[0]['login_reason'], '')

    async def test_network_failure_does_not_mark_account_invalid(self):
        async def unavailable(*args, **kwargs):
            raise TimeoutError('proxy timeout')
        with patch.object(pool_module, 'resume_video', unavailable):
            with self.assertRaises(TimeoutError):
                await self.pool.resume_video('fixture', '123', 60)
        self.assertEqual(self.pool.list_accounts()[0]['login_ok'], 1)

    async def test_ui_route_preserves_25_30_request_and_reference(self):
        async def browser(account, prompt, ratio, duration, **kwargs):
            self.assertEqual((prompt, ratio, duration, kwargs['model']), ('原文', '9:16', 30, 'seedance-2.5'))
            self.assertEqual(kwargs['reference_image_paths'], ['reference.jpg'])
            kwargs['on_conversation_id'](account, '123', time.time() + 60)
            kwargs['on_browser_closed']()
            return {'account': account, 'conversation_id': '123'}
        with patch.object(pool_module.config, 'PURE_HTTP_VIDEO', True), \
             patch.object(self.pool, '_proxy_state', return_value={'proxy_server': 'http://line:80', 'proxy_blocked': False}), \
             patch.object(pool_module, 'generate_video', browser), \
             patch.object(http_worker, 'generate_video', side_effect=AssertionError('HTTP submit must stay disabled')):
            result = await self.pool.generate_video('原文', '9:16', 30, 'seedance-2.5', reference_image_paths=['reference.jpg'])
        self.assertEqual(result['conversation_id'], '123')


class HttpLoginClassificationTests(unittest.TestCase):
    def prepare_with_login_flag(self, flag):
        def send(session, request, **kwargs):
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps({'code': 0, 'data': {}}).encode()
            response.headers['Content-Type'] = 'application/json'
            if flag is not None:
                response.headers['x-tt-agw-login'] = flag
            response._content_consumed = True
            return response
        with patch.object(http_worker, 'load_account_cookies', return_value=[{'name': 'sessionid', 'value': 'fixture', 'domain': '.dola.com', 'path': '/'}]), \
             patch.object(http_worker.config, 'proxy_url_for', return_value=None), \
             patch.object(http_worker.config, 'account_proxy_raw', return_value=('', 'none')), \
             patch.object(requests.Session, 'send', send):
            return http_worker.prepare('fixture', '原文', 'seedance-2.5', 30, '9:16', [])

    def test_explicit_logged_out_response_reaches_pool_as_login_error(self):
        with self.assertRaises(worker.LoginExpiredError):
            self.prepare_with_login_flag('0')

    def test_missing_login_header_is_unknown_not_confirmed_expired(self):
        with self.assertRaises(BrowserSetupError):
            self.prepare_with_login_flag(None)
