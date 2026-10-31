"""Credential imports must never become video workers before verified Dola login."""
import asyncio
import ast
import importlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from test_five_account_retry import pool_module


class GoogleAccountImportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.assertIsNotNone(importlib.util.find_spec('google_account_import'),
                             'Google credential batch import is not implemented')
        self.module = importlib.import_module('google_account_import')
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'accounts'
        self.pool = pool_module.BrowserPool(self.root, ':memory:')
        self.proxy = patch.object(self.pool, '_proxy_state', return_value={})
        self.proxy.start()
        self.addAsyncCleanup(self.cleanup)

    async def cleanup(self):
        self.proxy.stop()
        self.pool._conn.close()
        self.tmp.cleanup()

    def add(self, raw='owner@example.com----fixture-password----backup@example.net'):
        return self.module.import_google_accounts(self.pool, raw, proxy_pool=['http://isp.example:8080'])

    async def test_recovery_email_is_not_interpreted_as_totp(self):
        rows = self.module.parse_google_accounts('owner@example.com----fixture-password----backup@example.net')
        self.assertEqual(rows, [{'email': 'owner@example.com', 'password': 'fixture-password',
                                'recovery_email': 'backup@example.net', 'totp': ''}])
        row = self.module.parse_google_accounts('owner@example.com----fixture-password----JBSWY3DPEHPK3PXP')[0]
        self.assertEqual(row['totp'], 'JBSWY3DPEHPK3PXP')
        self.assertEqual(row['recovery_email'], '')

    async def test_malformed_batch_is_atomic_and_errors_do_not_echo_secrets(self):
        for raw in ('owner@example.com----fixture-password\nbad----private-secret----backup@example.net',
                    'owner@example.com----private-secret----not-a-seed!'):
            with self.assertRaises(ValueError) as caught:
                self.add(raw)
            self.assertNotIn('private-secret', str(caught.exception))
            self.assertEqual(self.pool.accounts, [])

    async def test_new_accounts_are_disposable_encrypted_and_cannot_be_force_enabled(self):
        result = self.add()
        self.assertEqual(result['imported'], 1)
        account = self.pool.list_accounts()[0]
        self.assertEqual(account['category'], 'disposable')
        self.assertEqual(account['onboarding_state'], 'queued')
        self.assertIsNone(account['login_ok'])
        self.pool.set_scheduling(account['name'], True, override_category=True)
        self.pool.reset_all_accounts()
        self.assertFalse(self.pool.available)
        self.assertFalse(self.pool.list_accounts()[0]['effective_scheduling'])
        saved = self.root / account['name'] / 'google-login.dpapi'
        self.assertNotIn(b'fixture-password', saved.read_bytes())
        self.assertEqual(self.module.read_credentials(saved)['recovery_email'], 'backup@example.net')
        self.assertNotIn('fixture-password', json.dumps(result))

    async def test_repeated_email_skips_without_overwriting_saved_credentials(self):
        first = self.add()['results'][0]['name']
        saved = self.root / first / 'google-login.dpapi'
        before = saved.read_bytes()
        result = self.add('OWNER@example.com----different-password----other@example.net')
        self.assertEqual(result['skipped'], 1)
        self.assertEqual(result['results'][0]['name'], first)
        self.assertEqual(saved.read_bytes(), before)

    async def test_worker_verifies_login_before_enabling_and_clears_saved_password(self):
        self.add()
        calls = []
        async def login(name, email, password, secret, **kwargs):
            self.assertFalse(self.pool.available)
            calls.append((email, password, secret, kwargs['recovery_email']))
            return True
        async def verify(name):
            self.assertFalse(self.pool.available)
            return True
        await self.module.GoogleImportQueue(self.pool, login=login, verify=verify).run()
        account = self.pool.list_accounts()[0]
        self.assertEqual(calls, [('owner@example.com', 'fixture-password', '', 'backup@example.net')])
        self.assertEqual(account['onboarding_state'], 'ready')
        self.assertEqual(account['login_ok'], 1)
        self.assertTrue(self.pool.available)
        self.assertFalse((self.root / account['name'] / 'google-login.dpapi').exists())

    async def test_google_login_uses_default_proxy_without_replacing_generation_isp(self):
        self.add()
        seen = []
        async def login(*args, **kwargs):
            seen.append(kwargs.get('login_proxy'))
            return True
        async def verify(name):
            self.assertEqual((self.root / name / 'proxy.txt').read_text().strip(), 'http://isp.example:8080')
            return True
        with patch.object(pool_module.config, 'PROXY', 'http://127.0.0.1:7897'):
            await self.module.GoogleImportQueue(self.pool, login=login, verify=verify).run()
        self.assertEqual(seen, ['http://127.0.0.1:7897'])

    async def test_failed_verification_is_never_marked_valid_and_raw_errors_are_not_published(self):
        self.add()
        async def login(*args, **kwargs):
            raise RuntimeError('password=fixture-password proxy-secret')
        await self.module.GoogleImportQueue(self.pool, login=login).run()
        account = self.pool.list_accounts()[0]
        self.assertEqual(account['onboarding_state'], 'attention')
        self.assertIsNone(account['login_ok'])
        self.assertNotIn('fixture-password', json.dumps(account))
        self.assertFalse(self.pool.available)

    async def test_worker_passes_only_the_birth_date_saved_for_that_account(self):
        name = self.add()['results'][0]['name']
        saved = self.root / name / 'google-login.dpapi'
        credentials = self.module.read_credentials(saved)
        credentials['birth_date'] = '2002-11-05'
        saved.write_bytes(self.module._dpapi(json.dumps(credentials).encode()))
        async def login(*args, **kwargs):
            from add_account import GoogleLoginAttention
            if kwargs.get('birth_date') != '2002-11-05':
                raise GoogleLoginAttention('Missing user-provided birthday')
        async def verify(name):
            return True
        await self.module.GoogleImportQueue(self.pool, login=login, verify=verify).run()
        self.assertEqual(self.pool.list_accounts()[0]['onboarding_state'], 'ready')

    async def test_unknown_verification_and_restarting_worker_remain_blocked(self):
        self.add()
        async def login(*args, **kwargs):
            return True
        async def verify(name):
            return None
        queue = self.module.GoogleImportQueue(self.pool, login=login, verify=verify)
        name = self.pool.accounts[0]
        self.pool.set_onboarding(name, 'running')
        queue.recover()
        self.assertEqual(self.pool.list_accounts()[0]['onboarding_state'], 'queued')
        await queue.run()
        self.assertEqual(self.pool.list_accounts()[0]['onboarding_state'], 'attention')
        self.assertFalse(self.pool.available)

    async def test_cancellation_requeues_and_releases_browser_permits(self):
        self.add()
        entered = asyncio.Event()
        async def login(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        worker = asyncio.create_task(self.module.GoogleImportQueue(self.pool, login=login).run())
        await asyncio.wait_for(entered.wait(), 2)
        worker.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await worker
        self.assertEqual(self.pool.list_accounts()[0]['onboarding_state'], 'queued')
        self.assertEqual(self.pool.semaphore._value, 1)
        self.assertTrue(all(not lock.locked() for lock in self.pool._locks.values()))

    async def test_import_endpoint_requires_admin_and_never_returns_passwords(self):
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        nodes = [n for n in tree.body if getattr(n, 'name', '') in {'GoogleAccountsImport', 'admin_import_google'}]
        self.assertEqual(len(nodes), 2, 'Google import endpoint is missing')
        app = FastAPI()
        def auth(key, authorization):
            if key != 'fixture-admin':
                raise HTTPException(401, 'unauthorized')
        namespace = dict(app=app, BaseModel=BaseModel, Field=Field, Header=Header,
            HTTPException=HTTPException, _admin_auth=auth, pool=self.pool,
            import_google_accounts=self.module.import_google_accounts,
            google_import_queue=SimpleNamespace(start=lambda: None),
            config=SimpleNamespace(_load_proxy_map=lambda: {}))
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])), 'google-endpoint', 'exec'), namespace)
        async def request(authorized):
            messages = []
            async def receive():
                return {'type':'http.request', 'body':json.dumps({'text':'owner@example.com----fixture-password----backup@example.net'}).encode()}
            async def send(message):
                messages.append(message)
            headers = [(b'content-type', b'application/json')]
            if authorized:
                headers.append((b'x-admin-key', b'fixture-admin'))
            await app(dict(type='http', asgi={'version':'3.0'}, http_version='1.1', method='POST', scheme='http',
                path='/api/admin/accounts/import-google', raw_path=b'/api/admin/accounts/import-google',
                query_string=b'', root_path='', headers=headers, client=('127.0.0.1',1), server=('test',80)), receive, send)
            return messages[0]['status'], b''.join(m.get('body', b'') for m in messages).decode()
        status, _ = await request(False)
        self.assertEqual(status, 401)
        self.assertEqual(self.pool.accounts, [])
        status, body = await request(True)
        self.assertEqual(status, 202, body)
        self.assertEqual(json.loads(body)['imported'], 1)
        self.assertNotIn('fixture-password', body)


if __name__ == '__main__':
    unittest.main()
