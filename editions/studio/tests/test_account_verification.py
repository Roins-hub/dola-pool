"""A stalled chat must never become evidence that an account logged out."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import requests

from test_video_pipeline import pool_module
import browser


class AccountVerificationRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_passport_expiry_marks_invalid_without_resetting_usage(self):
        import account_login
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'fixture'
            root.mkdir()
            (root / 'cookies.json').write_text(json.dumps([{'name': 'sessionid', 'value': 'fixture', 'domain': '.dola.com', 'path': '/'}]), encoding='utf-8')
            pool = pool_module.BrowserPool(directory, ':memory:')
            pool.set_login_status('fixture', True)
            pool._claim('fixture')
            used_before = pool.used_today('fixture')
            response = requests.Response()
            response.status_code = 200
            response._content = b'{"message":"error","data":{"error_code":13,"description":"session expired, please sign in again"}}'
            response._content_consumed = True
            try:
                with patch.object(account_login, 'ACCOUNTS_ROOT', Path(directory)), \
                     patch.object(account_login.config, 'proxy_for', return_value=None), \
                     patch.object(account_login.proxy_usage, 'reserve_for_account'), \
                     patch.object(requests.Session, 'send', return_value=response) as send:
                    self.assertIs(await pool.verify_account('fixture'), False)
                row = pool.list_accounts()[0]
                self.assertEqual(row['login_ok'], 0)
                self.assertIn('登录失效', row['login_reason'])
                self.assertEqual(pool.used_today('fixture'), used_before)
                send.assert_called_once()
            finally:
                pool._conn.close()

    async def test_silent_send_cannot_mark_logged_in_account_invalid(self):
        # Reproduce the old 16-second branch without contacting Dola or sending chat.
        empty = SimpleNamespace(count=AsyncMock(return_value=0))
        box = SimpleNamespace(click=AsyncMock(), fill=AsyncMock(),
                              input_value=AsyncMock(return_value='Hallo'))
        page = SimpleNamespace(
            url='https://www.dola.com/chat',
            goto=AsyncMock(return_value=SimpleNamespace(status=200)),
            evaluate=AsyncMock(return_value=False), wait_for_selector=AsyncMock(),
            wait_for_timeout=AsyncMock(), query_selector=AsyncMock(return_value=box),
            get_by_text=lambda *a, **kw: empty,
            locator=lambda *a, **kw: SimpleNamespace(first=empty),
            keyboard=SimpleNamespace(press=AsyncMock(), type=AsyncMock()),
        )
        context = SimpleNamespace(pages=[page], close=AsyncMock(),
                                  cookies=AsyncMock(return_value=[{'name': 'sessionid', 'value': 'fixture'}]))

        @asynccontextmanager
        async def playwright():
            yield object()

        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'fixture').mkdir()
            pool = pool_module.BrowserPool(directory, ':memory:')
            pool.set_login_status('fixture', True)
            try:
                with patch('patchright.async_api.async_playwright', playwright), \
                     patch.object(browser, 'launch_account_context', AsyncMock(return_value=context)):
                    result = await pool.verify_account('fixture')
                self.assertIsNone(result, 'No authentication evidence means unknown, never invalid')
                self.assertEqual(pool.list_accounts()[0]['login_ok'], 1)
                box.fill.assert_not_awaited()
                page.keyboard.press.assert_not_awaited()
            finally:
                pool._conn.close()


class AccountInfoTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.find_spec('account_login')
        self.assertIsNotNone(spec, 'Verification needs a read-only account-info check')
        import account_login
        self.probe = account_login

    def test_success_requires_an_authenticated_identity(self):
        self.assertIs(self.probe.classify_login_response(200, {'message': 'success', 'data': {'user_id': 123, 'is_blocked': False}}), True)
        for body in ({'message': 'success'}, {'message': 'success', 'data': {}}, {'message': 'success', 'data': {'user_id': 0}}):
            with self.subTest(body=body):
                self.assertIsNone(self.probe.classify_login_response(200, body))

    def test_only_explicit_authentication_denial_marks_invalid(self):
        self.assertIs(self.probe.classify_login_response(200, {'message': 'error', 'data': {}}, '0'), False)
        self.assertIsNone(self.probe.classify_login_response(200, {'message': 'error', 'data': {}}))
        self.assertIsNone(self.probe.classify_login_response(200, {'message': 'success', 'data': {'user_id': 123}}, '0'))

    def test_observed_passport_expiry_does_not_need_missing_gateway_header(self):
        # Actual anonymous control response from this read-only Dola endpoint.
        expiry = {'message': 'error', 'data': {'error_code': 13, 'description': 'session expired, please sign in again'}}
        self.assertIs(self.probe.classify_login_response(200, expiry), False)
        for data in ({'error_code': 13}, {'error_code': 13, 'description': 'request temporarily unavailable'}):
            self.assertIsNone(self.probe.classify_login_response(200, {'message': 'error', 'data': data}))
        expiry['data']['user_id'] = 123
        self.assertIsNone(self.probe.classify_login_response(200, expiry))

    def test_transport_and_unknown_responses_never_mark_invalid(self):
        for status in (302, 403, 407, 429, 500, 503):
            with self.subTest(status=status):
                self.assertIsNone(self.probe.classify_login_response(status, {'message': 'error'}, '0'))
        for body in (None, [], 'html', {'data': {'error_code': 99999}}):
            with self.subTest(body=body):
                self.assertIsNone(self.probe.classify_login_response(200, body, '0'))
        self.assertIsNone(self.probe.classify_login_response(200, {'message': 'success', 'data': {'user_id': 123, 'is_blocked': True}}))

    def fixture(self, directory, cookies):
        root = Path(directory) / 'fixture'
        root.mkdir()
        (root / 'cookies.json').write_text(json.dumps(cookies), encoding='utf-8')
        return root

    def test_one_get_uses_original_isp_scoped_cookies_and_no_redirects(self):
        seen = []

        def send(session, request, **kwargs):
            seen.append(request)
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url, self.probe.ACCOUNT_INFO_URL)
            self.assertEqual(session.proxies, {'http': 'http://fixture-proxy:80', 'https': 'http://fixture-proxy:80'})
            self.assertFalse(session.trust_env)
            self.assertFalse(kwargs['allow_redirects'])
            header = request.headers.get('Cookie', '')
            self.assertIn('sessionid=fixture-session', header)
            self.assertNotIn('unrelated', header)
            self.assertNotIn('private_path', header)
            response = requests.Response()
            response.status_code = 200
            response._content = b'{"message":"success","data":{"user_id":123}}'
            response._content_consumed = True
            return response

        with tempfile.TemporaryDirectory() as directory:
            self.fixture(directory, [
                {'name': 'sessionid', 'value': 'fixture-session', 'domain': '.dola.com', 'path': '/'},
                {'name': 'unrelated', 'value': 'fixture', 'domain': 'evil-dola.com', 'path': '/'},
                {'name': 'private_path', 'value': 'fixture', 'domain': '.dola.com', 'path': '/chat'},
            ])
            with patch.object(self.probe, 'ACCOUNTS_ROOT', Path(directory)), \
                 patch.object(self.probe.config, 'proxy_for', return_value={'server': 'http://fixture-proxy:80'}), \
                 patch.object(self.probe.proxy_usage, 'reserve_for_account') as reserve, \
                 patch.object(requests.Session, 'send', send):
                self.assertIs(self.probe.read_account_login('fixture'), True)
            reserve.assert_called_once_with('fixture', proxy={'server': 'http://fixture-proxy:80'})
            self.assertEqual(len(seen), 1)

    def test_missing_cookies_is_unknown_and_sends_nothing(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(self.probe, 'ACCOUNTS_ROOT', Path(directory)), \
             patch.object(requests.Session, 'send') as send:
            (Path(directory) / 'fixture').mkdir()
            self.assertIsNone(self.probe.read_account_login('fixture'))
            send.assert_not_called()

    def test_latest_cookies_support_alternate_session_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'fixture'
            (root / 'polling').mkdir(parents=True)
            (root / 'polling' / '123.json').write_text(json.dumps({'cookies': [{'name': 'sessionid_ss', 'value': 'fresh', 'domain': '.dola.com', 'path': '/'}]}), encoding='utf-8')
            self.assertEqual(self.probe.load_login_cookies(root)[0]['value'], 'fresh')

    def test_network_failure_keeps_unknown_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            self.fixture(directory, [{'name': 'sessionid', 'value': 'fixture', 'domain': '.dola.com', 'path': '/'}])
            with patch.object(self.probe, 'ACCOUNTS_ROOT', Path(directory)), \
                 patch.object(self.probe.config, 'proxy_for', return_value=None), \
                 patch.object(self.probe.proxy_usage, 'reserve_for_account'), \
                 patch.object(requests.Session, 'send', side_effect=requests.Timeout) as send:
                self.assertIsNone(self.probe.read_account_login('fixture'))
                send.assert_called_once()
