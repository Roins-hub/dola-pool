import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from providers.doubao import DoubaoProvider, ProviderError
from test_doubao_provider import ck, PROXY


class FixtureQRSession:
    closed = False
    finish = asyncio.Event

    def __init__(self, *args, **kwargs): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *args): type(self).closed = True
    async def login_step(self):
        if not self.finish.is_set():
            return {'state': 'waiting_scan', 'qr_image': 'data:image/png;base64,fixture'}
        return {'state': 'authenticated', 'cookies': json.loads(ck()),
                'checks': {'route_verified': True, 'login_verified': True, 'egress': '8.8.8.9'}}


class QRLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        FixtureQRSession.finish = asyncio.Event()
        FixtureQRSession.closed = False
        self.provider = DoubaoProvider(root=Path(self.temp.name), enabled=False, login_session_factory=FixtureQRSession)

    async def wait_state(self, job_id, states):
        for _ in range(100):
            status = self.provider.login_status(job_id)
            if status['state'] in states:
                return status
            await asyncio.sleep(0.01)
        self.fail('login worker did not reach expected state')

    async def test_scanning_imports_deduplicated_cookie_without_revealing_credentials(self):
        job = self.provider.start_login('scan-account', PROXY)
        status = await self.wait_state(job['id'], {'waiting_scan'})
        self.assertTrue(status['qr_image'].startswith('data:image/png;'))
        self.assertEqual(self.provider.list_accounts(), [])
        FixtureQRSession.finish.set()
        status = await self.wait_state(job['id'], {'completed', 'failed'})
        self.assertEqual(status['state'], 'completed')
        self.assertEqual(status['account']['name'], 'scan-account')
        self.assertNotIn('session-one', json.dumps(status))
        self.assertNotIn('secret', json.dumps(status))
        self.assertEqual(len(self.provider.list_accounts()), 1)

    async def test_cancelled_qr_job_closes_browser_without_creating_account(self):
        job = self.provider.start_login('scan-account', PROXY)
        await self.wait_state(job['id'], {'waiting_scan'})
        await self.provider.cancel_login(job['id'])
        self.assertEqual(self.provider.login_status(job['id'])['state'], 'cancelled')
        self.assertTrue(FixtureQRSession.closed)
        self.assertEqual(self.provider.list_accounts(), [])

    async def test_missing_proxy_refused_before_any_login_job(self):
        with self.assertRaises(ProviderError) as error:
            self.provider.start_login('scan-account', '')
        self.assertEqual(error.exception.code, 'proxy_unavailable')

    async def test_duplicate_logged_in_identity_is_not_imported_twice(self):
        self.provider.import_ck('existing', ck(), PROXY)
        FixtureQRSession.finish.set()
        job = self.provider.start_login('new-name', PROXY)
        status = await self.wait_state(job['id'], {'completed', 'failed'})
        self.assertEqual(status['state'], 'failed')
        self.assertEqual(status['error_code'], 'duplicate_account')
        self.assertEqual(len(self.provider.list_accounts()), 1)


if __name__ == '__main__': unittest.main()
