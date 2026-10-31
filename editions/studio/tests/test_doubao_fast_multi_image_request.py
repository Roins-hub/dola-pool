"""Observed native multi-input also admits ordinary Fast's 15-second requests.

These are admission tests, not claims that a reference video has been generated.
Fixtures use the existing measured-video schema and separate upload observation.
"""
import copy
from pathlib import Path
import tempfile
import unittest

from providers.base import ProviderError
from providers.doubao import DoubaoProvider
from providers.doubao.capabilities import Capabilities
from test_doubao_dialog_image_request import native_image_manifest
from test_doubao_provider import ck, PROXY


def fast_multi_manifest():
    manifest = native_image_manifest()
    manifest['models'][0]['references']['images'].update(
        max_count=9, multi_upload_observation_id='b' * 64)
    return manifest


class FastMultiImageCapabilityTests(unittest.TestCase):
    def test_observed_native_nine_image_input_admits_fast_without_changing_video_spec(self):
        manifest = fast_multi_manifest()
        Capabilities.validate(manifest)
        model = manifest['models'][0]
        self.assertEqual(model['id'], 'dialog_seedance_2.0_fast')
        self.assertEqual(model['durations'], [15])
        self.assertEqual(model['ratios'], ['16:9'])
        self.assertEqual(model['references']['images']['validation_status'], 'experimental')
        self.assertEqual(model['combinations'][0]['evidence']['actual_duration'], 15.073)

    def test_unobserved_or_non_native_multi_input_and_more_than_nine_remain_invalid(self):
        base = fast_multi_manifest()
        cases = []
        missing = copy.deepcopy(base)
        del missing['models'][0]['references']['images']['multi_upload_observation_id']
        cases.append(missing)
        for changes in ({'max_count': 10}, {'multi_upload_observation_id': ''},
                        {'multi_upload_observation_id': 'not-an-observation'},
                        {'native_state': None}):
            broken = copy.deepcopy(base)
            broken['models'][0]['references']['images'].update(changes)
            cases.append(broken)
        for manifest in cases:
            with self.subTest(reference=manifest['models'][0]['references']['images']):
                with self.assertRaises(ProviderError) as caught:
                    Capabilities.validate(manifest)
                self.assertEqual(caught.exception.code, 'invalid_capabilities')


class FastMultiImageRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.provider = DoubaoProvider(root=self.root / 'provider', enabled=True)
        self.provider.import_ck('ordinary-fixture', ck(), PROXY)
        self.provider.accounts.save_diagnostics(
            'ordinary-fixture', {'route_verified': True, 'login_verified': True})
        # Non-sorted order catches accidental normalization of the upload order.
        self.tokens = [f'{i:032x}.png' for i in (9, 2, 7, 1, 6, 4, 8, 3, 5)]

    def request(self, **values):
        return {'model': 'dialog_seedance_2.0_fast', 'duration': 15,
                'ratio': '16:9', 'account_category': '普通账号',
                'prompt': '按上传图序生成十五秒连续动作。', **values}

    def test_fast_nine_image_upload_tokens_keep_exact_order_and_ordinary_category(self):
        self.provider.manifest.save(fast_multi_manifest())
        result = self.provider.validate_request(self.request(reference_uploads=self.tokens))
        self.assertEqual(result['reference_uploads'], self.tokens)
        self.assertEqual(result['reference_image_paths'], [])
        self.assertEqual(result['account_category'], '普通账号')
        self.assertEqual(result['submitted_duration'], 15)

    def test_owned_nine_paths_keep_token_order_without_double_counting(self):
        self.provider.manifest.save(fast_multi_manifest())
        folder = self.root / 'owned-task'
        folder.mkdir()
        paths = []
        for token in self.tokens:
            path = folder / token
            path.write_bytes(b'image-file-content-for-admission')
            paths.append(str(path.resolve()))
        result = self.provider.validate_request(self.request(
            reference_uploads=self.tokens, reference_image_paths=paths,
            reference_upload_root=str(folder)))
        self.assertEqual(result['reference_uploads'], self.tokens)
        self.assertEqual(result['reference_image_paths'], paths)
        self.assertEqual(result['submitted_duration'], 15)

    def test_tenth_image_is_rejected_by_request_admission(self):
        self.provider.manifest.save(fast_multi_manifest())
        with self.assertRaises(ProviderError) as caught:
            self.provider.validate_request(self.request(
                reference_uploads=self.tokens + [f'{10:032x}.png']))
        self.assertEqual(caught.exception.code, 'unsupported_reference')

    def test_existing_single_image_config_does_not_implicitly_open_multiple_uploads(self):
        self.provider.manifest.save(native_image_manifest())
        with self.assertRaises(ProviderError) as caught:
            self.provider.validate_request(self.request(reference_uploads=self.tokens[:2]))
        self.assertEqual(caught.exception.code, 'unsupported_reference')


if __name__ == '__main__':
    unittest.main()
