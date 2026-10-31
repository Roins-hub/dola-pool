"""Formal ordinary-chat tasks preserve owned images through the native boundary."""
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from providers.base import ProviderError
from providers.doubao import DoubaoProvider
from test_doubao_dialog_image_request import native_image_manifest
from test_doubao_dialog_reference_delivery import ReferenceSession
from test_doubao_experimental25_capabilities import experimental25_manifest
from test_doubao_provider import ck, PROXY


class DialogImageDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.provider = DoubaoProvider(root=self.root / 'provider', enabled=True,
                                       session_factory=ReferenceSession)
        self.addAsyncCleanup(self.provider.shutdown)
        self.provider.import_ck('one', ck(), PROXY, category='标准账号')
        self.provider.accounts.save_diagnostics('one', {'route_verified': True, 'login_verified': True})
        self.provider.save_capabilities(native_image_manifest())
        self.folder = self.root / 'owned-task'
        self.folder.mkdir()
        self.path = self.folder / ('a' * 32 + '.png')
        Image.new('RGB', (16, 16), color=(30, 100, 150)).save(self.path)
        self.row = {'id': 'video_' + 'd' * 32, 'platform': 'doubao',
                    'model': 'dialog_seedance_2.0_fast', 'duration': 15, 'ratio': '16:9',
                    'prompt': '参考图片中的虚拟小猫跳舞', 'reference_images': '[]',
                    'reference_image_paths': [str(self.path)],
                    'reference_upload_root': str(self.folder)}
        ReferenceSession.sends = 0
        ReferenceSession.prepared_references = None
        ReferenceSession.submit_error = None

    def assert_unsent_and_uncharged(self):
        account = self.provider.list_accounts()[0]
        self.assertEqual((ReferenceSession.sends, account['send_count'], account['quota_used'],
                          account['quota_reserved'], account['inflight_tasks']), (0, 0, 0, 0, 0))

    async def test_real_validation_and_images_reach_native_prepare_as_one_typed_owned_reference(self):
        result = await self.provider.run_task(self.row)
        self.assertEqual(result['phase'], 'accepted')
        self.assertEqual(ReferenceSession.prepared_references, [{
            'kind': 'image', 'path': self.path.resolve(), 'name': self.path.name,
            'size_bytes': self.path.stat().st_size}])
        snapshot = self.provider._read_snapshot(self.row['id'])
        self.assertEqual(snapshot['request']['reference_image_paths'], [str(self.path.resolve())])
        self.assertEqual((snapshot['request']['duration'], snapshot['submitted_duration']), (15, 15))
        self.assertEqual(snapshot['request']['reference_images'], [])
        self.assertEqual(ReferenceSession.sends, 1)

    async def test_image_delivery_preserves_25_real_thirty_and_standard_account(self):
        self.provider.save_capabilities(native_image_manifest(experimental25_manifest))
        self.row.update(model='dialog_seedance_2.5', duration=30)
        result = await self.provider.run_task(self.row)
        self.assertEqual(result['phase'], 'accepted')
        snapshot = self.provider._read_snapshot(self.row['id'])
        self.assertEqual((snapshot['request']['duration'], snapshot['submitted_duration']), (30, 30))
        self.assertEqual(snapshot['request']['model'], 'dialog_seedance_2.5')
        self.assertEqual(snapshot['capability']['account_category'], '标准账号')
        self.assertEqual(ReferenceSession.prepared_references[0]['kind'], 'image')
        self.assertNotIn('duration_profile', snapshot['request'])
        self.assertEqual(ReferenceSession.sends, 1)

    async def test_foreign_path_never_reaches_browser_prepare_or_spends_quota(self):
        foreign = self.root / ('b' * 32 + '.png')
        Image.new('RGB', (16, 16)).save(foreign)
        self.row['reference_image_paths'] = [str(foreign)]
        with self.assertRaises(ProviderError) as caught:
            await self.provider.run_task(self.row)
        self.assertEqual(caught.exception.code, 'unsupported_reference')
        self.assertIsNone(ReferenceSession.prepared_references)
        self.assert_unsent_and_uncharged()

    async def test_unstaged_image_token_cannot_be_silently_sent_as_text_only(self):
        self.row['reference_image_paths'] = []
        self.row['reference_uploads'] = [self.path.name]
        with self.assertRaises(ProviderError) as caught:
            await self.provider.run_task(self.row)
        self.assertEqual(caught.exception.code, 'unsupported_reference')
        self.assertIsNone(ReferenceSession.prepared_references)
        self.assert_unsent_and_uncharged()
        snapshot = self.provider._read_snapshot(self.row['id'])
        if snapshot is not None:
            self.assertFalse(snapshot['sent'])
            self.assertEqual(snapshot['state'], 'failed_before_submit')

    async def test_invalid_image_bytes_fail_before_prepare_and_release_reservation(self):
        self.path.write_bytes(b'not a PNG image despite its extension')
        with self.assertRaises(ProviderError) as caught:
            await self.provider.run_task(self.row)
        self.assertEqual(caught.exception.code, 'unsupported_reference')
        self.assertIsNone(ReferenceSession.prepared_references)
        self.assert_unsent_and_uncharged()
        self.assertFalse(self.provider._read_snapshot(self.row['id'])['sent'])


if __name__ == '__main__':
    unittest.main()
