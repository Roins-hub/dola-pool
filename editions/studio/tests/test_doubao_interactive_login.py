"""Official browser login captures credentials automatically and owns resources."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest

from fastapi import FastAPI, HTTPException

from providers.admin_api import create_admin_router
from providers.doubao import DoubaoProvider, ProviderError
from test_doubao_admin_api import ASGIClient
from test_doubao_provider import ck, PROXY


class ManualBrowser:
    """Replace only the external Chromium window; keep pool and leases real."""
    instances = []

    def __init__(self, provider, account, *, login):
        self.provider, self.account = provider, account
        self.closed = False
        self.authenticated = False
        self.session_ready = False
        self.capture_ready = False
        self.login_verified = False
        self.route_verified = True
        self.capture_started = asyncio.Event()
        self.capture_continue = None
        self.close_started = asyncio.Event()
        self.close_continue = None
        self.exports = 0
        self.capture_times = []
        self.__class__.instances.append(self)

    async def __aenter__(self):
        profile = self.provider.root / 'login' / self.account['account_id'] / 'profile'
        profile.mkdir(parents=True)
        (profile / 'user-state').write_text('real-profile-fixture', encoding='utf-8')
        return self

    async def __aexit__(self, *args):
        self.close_started.set()
        try:
            if self.close_continue is not None:
                await self.close_continue.wait()
        finally:
            self.closed = True

    async def login_step(self):
        raise AssertionError('Manual login must never click or export through automatic QR logic')

    async def interactive_login_status(self):
        return {'state': 'closed' if self.closed else ('authenticated' if self.authenticated else
                ('session_ready' if self.session_ready else 'waiting_login')),
                'login_verified': self.login_verified}

    async def capture_interactive_login(self):
        self.exports += 1
        self.capture_times.append(time.monotonic())
        self.capture_started.set()
        if self.capture_continue is not None:
            await self.capture_continue.wait()
        if self.closed:
            return {'state': 'closed'}
        if not self.capture_ready:
            raise ProviderError('login_not_ready', '请先在浏览器中完成官方登录。', 409)
        if not self.route_verified:
            raise ProviderError('proxy_unavailable', '无法确认所选线路，请稍后再试。', 503)
        return {'state': 'captured', 'cookies': json.loads(ck()),
                'checks': {'route_verified': True, 'login_verified': self.login_verified,
                           'egress': '8.8.8.9'}, 'login_verified': self.login_verified}


class ManualLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        ManualBrowser.instances = []
        self.provider = DoubaoProvider(root=Path(self.temp.name), enabled=False,
                                       login_session_factory=ManualBrowser)

    async def asyncTearDown(self):
        await self.provider.shutdown()

    async def wait_state(self, job_id, states):
        for _ in range(100):
            status = self.provider.login_status(job_id)
            if status['state'] in states:
                return status
            await asyncio.sleep(.01)
        self.fail(f'Login did not reach {states}: {status}')

    async def start(self, name='manual-account', proxy='direct'):
        job = self.provider.start_login(name, proxy, mode='browser')
        status = await self.wait_state(job['id'], {'browser_open', 'awaiting_confirmation', 'failed'})
        self.assertEqual(status['state'], 'browser_open', status)
        return job, ManualBrowser.instances[-1]

    async def test_authenticated_window_automatically_imports_once_without_confirmation(self):
        job, window = await self.start()
        self.assertTrue(window.account['_interactive'])
        window.authenticated = True
        window.capture_ready = True
        window.login_verified = True
        result = await self.wait_state(job['id'], {'completed', 'failed'})
        self.assertEqual(result['state'], 'completed')
        self.assertTrue(result['auto_capture'])
        self.assertEqual(result['mode'], 'browser')
        self.assertEqual(result['browser_location'], 'project_host')
        self.assertEqual(result['route_label'], '本机直连')
        self.assertGreater(result['expires_at'] - job['created_at'], 1190)
        self.assertTrue(result['account']['login_verified'])
        self.assertEqual(window.exports, 1)
        self.assertEqual(len(self.provider.list_accounts()), 1)
        self.assertEqual((await self.provider.confirm_login(job['id']))['account']['account_id'],
                         result['account']['account_id'])
        self.assertEqual(window.exports, 1)
        self.assertTrue(window.closed)
        account = result['account']
        profile = self.provider.root / 'accounts' / account['account_id'] / 'profile'
        self.assertEqual((profile / 'user-state').read_text(encoding='utf-8'), 'real-profile-fixture')
        self.assertNotIn('session-one', json.dumps(result))
        self.assertNotIn('secret', json.dumps(result))
        self.assertFalse(self.provider.scheduler.lane_slot({'server': 'direct'}).locked())

    async def test_automatic_capture_waits_for_official_login_status(self):
        job, window = await self.start()
        window.capture_ready = True
        await asyncio.sleep(.3)
        self.assertEqual(window.exports, 0)
        self.assertFalse(window.closed)
        self.assertEqual(self.provider.login_status(job['id'])['state'], 'browser_open')
        self.assertEqual(self.provider.list_accounts(), [])

    async def test_new_account_adopts_the_closed_login_windows_original_environment(self):
        from providers.doubao.accounts import atomic_json
        from providers.doubao.environment import create_environment
        job, window = await self.start()
        source = self.provider.root / 'login' / job['id'] / 'environment.json'
        env = create_environment(job['id'], {'server': 'direct'})
        atomic_json(source, env)
        original = source.read_bytes()
        window.authenticated = window.capture_ready = window.login_verified = True
        result = await self.wait_state(job['id'], {'completed', 'failed'})
        self.assertEqual(result['state'], 'completed', result)
        self.assertTrue(window.closed)
        account = result['account']
        self.assertEqual(account['account_id'], job['id'])
        saved = self.provider.root / 'accounts' / account['account_id'] / 'environment.json'
        self.assertEqual(saved.read_bytes(), original)
        self.assertEqual((saved.parent / 'profile' / 'user-state').read_text(encoding='utf-8'), 'real-profile-fixture')

    async def test_login_worker_prepares_environment_before_the_window_is_ready(self):
        job, window = await self.start()
        source = self.provider.root / 'login' / job['id'] / 'environment.json'
        self.assertTrue(source.is_file())
        self.assertEqual(json.loads(source.read_text(encoding='utf-8'))['environment_id'], window.account['account_id'])

    async def test_environment_lost_after_login_never_imports_a_different_device(self):
        job, window = await self.start()
        source = self.provider.root / 'login' / job['id'] / 'environment.json'
        source.unlink(missing_ok=True)
        window.authenticated = window.capture_ready = window.login_verified = True
        result = await self.wait_state(job['id'], {'completed', 'failed'})
        self.assertEqual(result['state'], 'failed')
        self.assertEqual(result['error_code'], 'environment_unavailable')
        self.assertEqual(self.provider.list_accounts(), [])

    async def test_session_cookie_ready_auto_imports_without_claiming_native_login_verified(self):
        job, window = await self.start()
        window.session_ready = True
        window.capture_ready = True
        # A status that only establishes valid session cookies cannot confer
        # native page verification, even if capture diagnostics are optimistic.
        window.login_verified = True
        result = await self.wait_state(job['id'], {'completed', 'failed'})
        self.assertEqual(result['state'], 'completed', result)
        self.assertFalse(result['account']['login_verified'])
        self.assertTrue(result['account']['route_verified'])
        self.assertEqual(result['account']['status'], 'unverified')
        self.assertIn('待环境验证', result['message'])
        self.assertFalse(self.provider.capabilities()['enabled'])

    async def test_automatic_route_capture_failure_keeps_window_and_retries_after_cooldown(self):
        job, window = await self.start()
        window.authenticated = True
        window.capture_ready = True
        window.route_verified = False
        await asyncio.wait_for(window.capture_started.wait(), timeout=1)
        await asyncio.sleep(.3)
        self.assertEqual(window.exports, 1)
        self.assertFalse(window.closed)
        self.assertEqual(self.provider.list_accounts(), [])
        self.assertEqual(self.provider.login_status(job['id'])['error_code'], 'proxy_unavailable')
        window.route_verified = True
        for _ in range(650):
            result = self.provider.login_status(job['id'])
            if result['state'] in ('completed', 'failed'):
                break
            await asyncio.sleep(.01)
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(window.exports, 2)
        self.assertGreaterEqual(window.capture_times[1] - window.capture_times[0], 4.9)
        self.assertEqual(len(self.provider.list_accounts()), 1)
        self.assertNotIn('error_code', result)

    async def test_cancel_during_automatic_capture_cannot_late_import(self):
        job, window = await self.start()
        window.authenticated = True
        window.capture_ready = True
        window.capture_continue = asyncio.Event()
        await asyncio.wait_for(window.capture_started.wait(), timeout=1)
        await self.provider.cancel_login(job['id'])
        window.capture_continue.set()
        await asyncio.sleep(.05)
        self.assertTrue(window.closed)
        self.assertEqual(self.provider.login_status(job['id'])['state'], 'cancelled')
        self.assertEqual(self.provider.list_accounts(), [])

    async def test_manual_retry_during_automatic_capture_does_not_double_export_or_import(self):
        job, window = await self.start()
        window.authenticated = True
        window.capture_ready = True
        window.capture_continue = asyncio.Event()
        await asyncio.wait_for(window.capture_started.wait(), timeout=1)
        self.assertEqual(self.provider.login_status(job['id'])['state'], 'capturing')
        confirm = asyncio.create_task(self.provider.confirm_login(job['id']))
        window.capture_continue.set()
        result = await asyncio.wait_for(confirm, timeout=1)
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(window.exports, 1)
        self.assertEqual(len(self.provider.list_accounts()), 1)

    async def test_missing_credentials_after_login_signal_keeps_window_for_retry(self):
        job, window = await self.start()
        window.authenticated = True
        await asyncio.wait_for(window.capture_started.wait(), timeout=1)
        await asyncio.sleep(.15)
        self.assertEqual(window.exports, 1)
        self.assertFalse(window.closed)
        self.assertEqual(self.provider.list_accounts(), [])
        self.assertEqual(self.provider.login_status(job['id'])['error_code'], 'login_not_ready')
        window.capture_ready = True
        result = await self.provider.confirm_login(job['id'])
        self.assertEqual(result['state'], 'completed', result)
        self.assertEqual(window.exports, 2)

    async def test_automatic_capture_of_existing_identity_keeps_original_account_only(self):
        existing = self.provider.import_ck('existing', ck(), 'direct')
        job, window = await self.start()
        window.authenticated = True
        window.capture_ready = True
        result = await self.wait_state(job['id'], {'completed', 'failed'})
        self.assertEqual(result['state'], 'failed', result)
        self.assertEqual(result['error_code'], 'duplicate_account')
        self.assertTrue(window.closed)
        self.assertEqual([a['account_id'] for a in self.provider.list_accounts()], [existing['account_id']])

    async def test_expiry_while_capture_is_in_progress_does_not_late_import(self):
        job, window = await self.start()
        window.authenticated = True
        window.capture_ready = True
        window.capture_continue = asyncio.Event()
        await asyncio.wait_for(window.capture_started.wait(), timeout=1)
        self.provider._login_jobs[job['id']]['expires_at'] = time.time() - 1
        window.capture_continue.set()
        result = await self.wait_state(job['id'], {'expired', 'failed'})
        self.assertEqual(result['state'], 'expired', result)
        self.assertTrue(window.closed)
        self.assertEqual(self.provider.list_accounts(), [])

    async def test_automatic_capture_does_not_import_before_profile_browser_has_closed(self):
        job, window = await self.start()
        window.authenticated = True
        window.capture_ready = True
        window.close_continue = asyncio.Event()
        await asyncio.wait_for(window.close_started.wait(), timeout=1)
        self.assertEqual(self.provider.login_status(job['id'])['state'], 'capturing')
        self.assertEqual(self.provider.list_accounts(), [])
        self.assertFalse(window.closed)
        window.close_continue.set()
        result = await self.wait_state(job['id'], {'completed', 'failed'})
        self.assertEqual(result['state'], 'completed', result)
        self.assertTrue(window.closed)

    async def test_confirmation_before_credentials_can_retry_in_same_browser(self):
        job, window = await self.start(proxy=PROXY)
        with self.assertRaises(ProviderError) as error:
            await self.provider.confirm_login(job['id'])
        self.assertEqual(error.exception.code, 'login_not_ready')
        self.assertEqual(error.exception.status_code, 409)
        self.assertFalse(window.closed)
        self.assertEqual(self.provider.list_accounts(), [])
        window.capture_ready = True
        result = await self.provider.confirm_login(job['id'])
        self.assertEqual(result['state'], 'completed')
        self.assertFalse(result['account']['login_verified'])
        self.assertEqual(result['account']['status'], 'unverified')
        self.assertIn('待环境验证', result['message'])
        self.assertFalse(self.provider.capabilities()['enabled'])

    async def test_failed_route_confirmation_keeps_window_and_saves_nothing(self):
        job, window = await self.start()
        window.capture_ready = True
        window.route_verified = False
        with self.assertRaises(ProviderError) as error:
            await self.provider.confirm_login(job['id'])
        self.assertEqual(error.exception.status_code, 503)
        self.assertFalse(window.closed)
        self.assertEqual(self.provider.list_accounts(), [])
        window.route_verified = True
        self.assertEqual((await self.provider.confirm_login(job['id']))['state'], 'completed')

    async def test_user_closing_window_cancels_job_and_releases_lane_without_import(self):
        job, window = await self.start()
        window.closed = True
        status = await self.wait_state(job['id'], {'cancelled', 'failed'})
        self.assertEqual(status['state'], 'cancelled')
        self.assertEqual(window.exports, 0)
        self.assertEqual(self.provider.list_accounts(), [])
        self.assertFalse(self.provider.scheduler.lane_slot({'server': 'direct'}).locked())
        with self.assertRaises(ProviderError):
            await self.provider.confirm_login(job['id'])

    async def test_expiry_and_cancel_close_browser_without_account_or_retained_lane(self):
        job, window = await self.start()
        self.provider._login_jobs[job['id']]['expires_at'] = time.time() - 1
        status = await self.wait_state(job['id'], {'expired', 'failed'})
        self.assertEqual(status['state'], 'expired')
        self.assertTrue(window.closed)
        self.assertFalse(self.provider.scheduler.lane_slot({'server': 'direct'}).locked())
        job, window = await self.start(name='second')
        self.assertEqual((await self.provider.cancel_login(job['id']))['state'], 'cancelled')
        self.assertTrue(window.closed)
        self.assertEqual(self.provider.list_accounts(), [])

    async def test_cancel_during_capture_cannot_save_after_browser_was_closed(self):
        job, window = await self.start()
        window.capture_ready = True
        window.capture_continue = asyncio.Event()
        confirm = asyncio.create_task(self.provider.confirm_login(job['id']))
        await window.capture_started.wait()
        await self.provider.cancel_login(job['id'])
        window.capture_continue.set()
        await asyncio.gather(confirm, return_exceptions=True)
        self.assertTrue(window.closed)
        self.assertEqual(self.provider.login_status(job['id'])['state'], 'cancelled')
        self.assertEqual(self.provider.list_accounts(), [])

    async def test_confirmation_is_idempotent_after_success(self):
        job, window = await self.start()
        window.capture_ready = True
        first = await self.provider.confirm_login(job['id'])
        second = await self.provider.confirm_login(job['id'])
        self.assertEqual(first['account']['account_id'], second['account']['account_id'])
        self.assertEqual(window.exports, 1)
        self.assertEqual(len(self.provider.list_accounts()), 1)

    async def test_browser_jobs_use_existing_capacity_limit_and_shutdown_cleanup(self):
        job, window = await self.start()
        second = self.provider.start_login('second', PROXY, mode='browser')
        with self.assertRaises(ProviderError) as error:
            self.provider.start_login('third', PROXY, mode='browser')
        self.assertEqual(error.exception.code, 'login_busy')
        await self.provider.shutdown()
        self.assertTrue(window.closed)
        self.assertEqual(self.provider.login_status(job['id'])['state'], 'cancelled')
        self.assertEqual(self.provider.login_status(second['id'])['state'], 'cancelled')

    async def test_unknown_mode_is_refused_before_any_browser_or_job(self):
        with self.assertRaises(ProviderError) as error:
            self.provider.start_login('wrong', 'direct', mode='automatic-import')
        self.assertEqual(error.exception.code, 'invalid_login_mode')
        self.assertEqual(self.provider._login_jobs, {})

    async def test_manual_confirm_cannot_force_automatic_qr_job(self):
        from test_doubao_login import FixtureQRSession
        FixtureQRSession.finish = asyncio.Event()
        self.provider.login_session_factory = FixtureQRSession
        job = self.provider.start_login('qr-account', PROXY)
        await self.wait_state(job['id'], {'waiting_scan'})
        with self.assertRaises(ProviderError) as error:
            await self.provider.confirm_login(job['id'])
        self.assertEqual(error.exception.code, 'login_mode_mismatch')
        self.assertEqual(self.provider.list_accounts(), [])

    async def test_admin_route_requires_auth_and_never_auto_confirms_from_status(self):
        def auth(key, authorization=None):
            if key != 'fixture-admin':
                raise HTTPException(401, 'admin required')
            return {'role': 'admin'}
        app = FastAPI()
        app.include_router(create_admin_router(self.provider, auth,
            resolve_proxy=lambda key: 'direct' if key == 'local:direct' else None))
        client = ASGIClient(app)
        headers = {'X-Admin-Key': 'fixture-admin'}
        base = '/api/admin/platforms/doubao/login'
        response = await client.post(base, headers=headers,
            json={'name': 'api-browser', 'proxy_ref': 'local:direct', 'mode': 'browser'})
        self.assertEqual(response.status_code, 200, response.text)
        job = response.json()
        await self.wait_state(job['id'], {'browser_open'})
        window = ManualBrowser.instances[-1]
        window.capture_ready = True
        response = await client.post(base + '/' + job['id'] + '/confirm')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(window.exports, 0)
        response = await client.get(base + '/' + job['id'], headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.provider.list_accounts(), [])
        response = await client.post(base + '/' + job['id'] + '/confirm', headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['state'], 'completed')
        self.assertNotIn('session-one', response.text)
        self.assertNotIn('cookies', response.text)


if __name__ == '__main__':
    unittest.main()
