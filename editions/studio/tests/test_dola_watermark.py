import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import dola_watermark as wm


def configuration(on=None, upgrade=False):
    values = {} if on is None else {'1': {'watermark_option': {
        'is_on': on, 'subscribe_config': {'need_upgrade': upgrade}}}}
    return {'code': 0, 'data': {'config_map': values}}


class WatermarkPreferenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_read_timeout_rechecks_same_account_before_success(self):
        post = AsyncMock(side_effect=[TimeoutError('token=private'), configuration(True)])
        with patch('dola_watermark.asyncio.sleep', new=AsyncMock()):
            result = await wm.ensure_preference('account-a', post)
        self.assertTrue(result['enabled'])
        self.assertFalse(result['changed'])
        self.assertEqual([call.args[0] for call in post.await_args_list], [wm.GET, wm.GET])

    async def test_lost_save_reply_reads_back_without_sending_setting_twice(self):
        post = AsyncMock(side_effect=[configuration(False), ConnectionResetError('private'), configuration(True)])
        result = await wm.ensure_preference('account-a', post)
        self.assertTrue(result['enabled'])
        self.assertTrue(result['changed'])
        self.assertEqual([call.args[0] for call in post.await_args_list], [wm.GET, wm.SET, wm.GET])

    async def test_transient_readback_failure_does_not_resend_setting(self):
        post = AsyncMock(side_effect=[configuration(False), {'code': 0}, TimeoutError('private'), configuration(True)])
        with patch('dola_watermark.asyncio.sleep', new=AsyncMock()):
            result = await wm.ensure_preference('account-a', post)
        self.assertTrue(result['enabled'])
        self.assertEqual([call.args[0] for call in post.await_args_list], [wm.GET, wm.SET, wm.GET, wm.GET])

    async def test_exhausted_timeout_records_cause_stage_and_account_without_secrets(self):
        post = AsyncMock(side_effect=TimeoutError('Cookie: sessionid=private'))
        with patch('dola_watermark.asyncio.sleep', new=AsyncMock()):
            with self.assertRaises(wm.WatermarkPreferenceError) as caught:
                await wm.ensure_preference('account-a', post)
        self.assertEqual(post.await_count, 3)
        self.assertEqual(caught.exception.account, 'account-a')
        self.assertIn('超时', str(caught.exception))
        saved = json.loads((self.root/'account-a'/'watermark-status.json').read_text())
        self.assertFalse(saved['enabled'])
        self.assertEqual(saved['failure_code'], 'timeout')
        self.assertEqual(saved['stage'], 'read')
        self.assertNotIn('private', json.dumps(saved)+str(caught.exception))

    async def test_lost_save_reply_is_not_success_without_confirmed_readback(self):
        post = AsyncMock(side_effect=[configuration(False), TimeoutError('private'), configuration(False)])
        with self.assertRaisesRegex(wm.WatermarkPreferenceError, '回读'):
            await wm.ensure_preference('account-a', post)
        self.assertEqual(post.await_count, 3)

    async def test_temporary_http_unavailability_is_retried(self):
        post = AsyncMock(side_effect=[RuntimeError('watermark_http_503 private'), configuration(True)])
        with patch('dola_watermark.asyncio.sleep', new=AsyncMock()):
            self.assertTrue((await wm.ensure_preference('account-a', post))['enabled'])
        self.assertEqual(post.await_count, 2)

    async def test_explicit_login_failure_is_not_retried_or_treated_as_enabled(self):
        post = AsyncMock(side_effect=RuntimeError('watermark_login_expired token=private'))
        with self.assertRaisesRegex(wm.WatermarkPreferenceError, '未登录'):
            await wm.ensure_preference('account-a', post)
        self.assertEqual(post.await_count, 1)
        saved = json.loads((self.root/'account-a'/'watermark-status.json').read_text())
        self.assertEqual(saved['failure_code'], 'login_expired')
        self.assertFalse(saved['enabled'])
        self.assertNotIn('private', json.dumps(saved))

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'account-a').mkdir()
        self.patch = patch.object(wm, 'ACCOUNTS_DIR', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_new_account_is_enabled_and_read_back_before_success(self):
        post = AsyncMock(side_effect=[configuration(), {'code': 0}, configuration(True)])
        result = await wm.ensure_preference('account-a', post)
        self.assertTrue(result['enabled'])
        self.assertTrue(result['changed'])
        self.assertEqual([x.args[0] for x in post.await_args_list], [wm.GET, wm.SET, wm.GET])
        self.assertEqual(post.await_args_list[1].args[1], {
            'config_type': 1, 'config_value': {'watermark_option': {'is_on': True}}})
        saved = json.loads((self.root / 'account-a' / 'watermark-status.json').read_text())
        self.assertTrue(saved['enabled'])

    async def test_every_use_rechecks_even_if_local_status_says_enabled(self):
        post = AsyncMock(return_value=configuration(True))
        await wm.ensure_preference('account-a', post)
        await wm.ensure_preference('account-a', post)
        self.assertEqual(post.await_count, 2)
        self.assertTrue(all(call.args[0] == wm.GET for call in post.await_args_list))

    async def test_account_status_is_isolated_and_no_cookie_is_persisted(self):
        (self.root / 'account-b').mkdir()
        await wm.ensure_preference('account-a', AsyncMock(return_value=configuration(True)))
        self.assertFalse((self.root / 'account-b' / 'watermark-status.json').exists())
        text = (self.root / 'account-a' / 'watermark-status.json').read_text()
        self.assertNotIn('cookie', text.lower())
        self.assertNotIn('http', text)

    async def test_upgrade_required_does_not_attempt_to_override_it(self):
        post = AsyncMock(return_value=configuration(False, upgrade=True))
        with self.assertRaisesRegex(wm.WatermarkPreferenceError, '升级'):
            await wm.ensure_preference('account-a', post)
        self.assertEqual(post.await_count, 1)

    async def test_ack_without_persisted_setting_is_failure(self):
        post = AsyncMock(side_effect=[configuration(False), {'code': 0}, configuration(False)])
        with self.assertRaisesRegex(wm.WatermarkPreferenceError, '回读'):
            await wm.ensure_preference('account-a', post)
        saved = json.loads((self.root / 'account-a' / 'watermark-status.json').read_text())
        self.assertFalse(saved['enabled'])

    async def test_errors_and_bad_responses_never_enable_or_leak_secrets(self):
        for value in ({'code': 1, 'msg': 'token=secret'}, {'code': 0}, None):
            with self.subTest(value=value):
                with self.assertRaises(wm.WatermarkPreferenceError) as error:
                    await wm.ensure_preference('account-a', AsyncMock(return_value=value))
                self.assertNotIn('secret', str(error.exception))
        with self.assertRaises(wm.WatermarkPreferenceError) as error:
            await wm.ensure_preference('account-a', AsyncMock(side_effect=TimeoutError('secret')))
        self.assertNotIn('secret', str(error.exception))

    async def test_browser_transport_uses_current_context_and_only_fixed_config_endpoint(self):
        page = type('Page', (), {'evaluate': AsyncMock(return_value=configuration(True))})()
        await wm.ensure_page_preference(page, 'account-a')
        script, params = page.evaluate.await_args.args
        self.assertEqual(params['path'], wm.GET)
        self.assertIn("credentials: 'include'", script)
        self.assertIn("redirect: 'error'", script)
        self.assertNotIn('proxy', params)
        self.assertNotIn('cookie', params)
