"""Observed image upload is a separate trial, never delivered-video evidence."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from providers.base import ProviderError
from providers.doubao import DoubaoProvider
from providers.doubao.capabilities import Capabilities
from test_doubao_dialog_capabilities import dialog_manifest
from test_doubao_experimental25_capabilities import experimental25_manifest
from test_doubao_dialog_media_request import dialog_video_manifest
from test_doubao_provider import ck, PROXY


IMAGE_TOKEN = 'c' * 32 + '.png'


def dialog_image_manifest(factory=dialog_manifest):
    manifest = factory()
    model = manifest['models'][0]
    model['references']['images'] = {
        'enabled': True, 'validation_status': 'experimental', 'local_only': True,
        'max_count': 1, 'max_bytes': 15 * 1024 * 1024,
        'upload_observation_id': 'f' * 64,
    }
    model['selectors'].update(upload_image='input[type="file"][accept="image/*"]',
                              upload_ready='.image-upload-ready',
                              upload_pending='.upload-pending', upload_error='.upload-error')
    return manifest


def native_image_manifest(factory=dialog_manifest):
    manifest = dialog_image_manifest(factory)
    model = manifest['models'][0]
    model['references']['images']['native_state'] = 'attachment_state_v1'
    model['selectors'].pop('upload_pending')
    model['selectors'].pop('upload_error')
    model['selectors']['upload_ready'] = '[data-testid="attachment-image-card"]'
    return manifest


class DialogImageCapabilitiesTests(unittest.TestCase):
    def test_thirty_second_nine_image_trial_requires_observed_native_multi_input(self):
        manifest=native_image_manifest(experimental25_manifest)
        refs=manifest['models'][0]['references']['images']
        refs.update(max_count=9,multi_upload_observation_id='a'*64)
        Capabilities.validate(manifest)
        for changes in ({'max_count':10},{'multi_upload_observation_id':''},{'native_state':None}):
            broken=copy.deepcopy(manifest)
            broken['models'][0]['references']['images'].update(changes)
            self.assert_invalid(broken)
        broken=copy.deepcopy(manifest)
        del broken['models'][0]['references']['images']['multi_upload_observation_id']
        self.assert_invalid(broken)

    def assert_invalid(self, manifest):
        with self.assertRaises(ProviderError) as caught:
            Capabilities.validate(manifest)
        self.assertEqual(caught.exception.code, 'invalid_capabilities')

    def test_observed_image_trial_does_not_require_or_invent_completed_image_video(self):
        for factory, duration in ((dialog_manifest, 15), (experimental25_manifest, 30)):
            with self.subTest(model=factory.__name__):
                manifest = dialog_image_manifest(factory)
                try:
                    Capabilities.validate(manifest)
                except ProviderError as error:
                    self.fail('A separately identified native image-upload trial was rejected: ' + str(error))
                model = manifest['models'][0]
                self.assertEqual(model['durations'], [duration])
                self.assertNotIn('image_task_id', model['evidence'])
                self.assertEqual(model.get('validation_status', 'verified'),
                                 'experimental' if duration == 30 else 'verified')

    def test_public_reference_reports_trial_and_local_only_without_private_observation(self):
        with tempfile.TemporaryDirectory() as folder:
            caps = Capabilities(Path(folder) / 'capabilities.json')
            try:
                caps.save(dialog_image_manifest())
            except ProviderError as error:
                self.fail('Image trial could not be saved: ' + str(error))
            public = caps.public_models()[0]
        self.assertEqual(public['references']['images'], {
            'enabled': True, 'validation_status': 'experimental', 'local_only': True,
            'max_count': 1, 'max_bytes': 15 * 1024 * 1024})
        self.assertNotIn('upload_observation_id', json.dumps(public))
        self.assertNotIn('selectors', public)

    def test_upload_observation_cannot_be_replaced_by_text_or_image_task_claim(self):
        for proof in (None, '', 'not-a-hash', True, 17, 'g' * 64, 'f' * 63):
            manifest = dialog_image_manifest()
            model = manifest['models'][0]
            model['references']['images']['upload_observation_id'] = proof
            model['evidence']['image_task_id'] = 'text-only-task-is-not-upload-proof'
            with self.subTest(proof=proof):
                self.assert_invalid(manifest)

    def test_image_trial_is_narrow_local_single_image_and_explicitly_unverified(self):
        for changes in ({'validation_status': 'verified'}, {'validation_status': None},
                        {'local_only': False}, {'local_only': 1}, {'max_count': 2},
                        {'max_count': True}, {'max_bytes': 15 * 1024 * 1024 + 1},
                        {'max_bytes': 0}, {'assume_ready': True}):
            manifest = dialog_image_manifest()
            manifest['models'][0]['references']['images'].update(changes)
            with self.subTest(changes=changes):
                self.assert_invalid(manifest)
        for kind in ('audio', 'video'):
            manifest = dialog_image_manifest()
            model = manifest['models'][0]
            model['references'][kind] = model['references'].pop('images')
            with self.subTest(kind=kind):
                self.assert_invalid(manifest)

    def test_every_upload_lifecycle_selector_is_required_and_only_observed_entry_forms_work(self):
        for missing in ('upload_image', 'upload_ready', 'upload_pending', 'upload_error'):
            manifest = dialog_image_manifest()
            del manifest['models'][0]['selectors'][missing]
            with self.subTest(missing=missing):
                self.assert_invalid(manifest)
        for factory in (dialog_manifest, experimental25_manifest):
            for changes in ({'upload': '#native-image-file'},
                            {'upload_trigger': '#attach', 'upload_option': '#image-file'}):
                manifest = dialog_image_manifest(factory)
                selectors = manifest['models'][0]['selectors']
                del selectors['upload_image']
                selectors.update(changes)
                with self.subTest(factory=factory.__name__, changes=changes):
                    try:
                        Capabilities.validate(manifest)
                    except ProviderError as error:
                        self.fail('Observed direct file or chooser entry was rejected: ' + str(error))

    def test_image_trial_does_not_widen_experimental_thirty_second_generation_spec(self):
        for changes in ({'durations': [15]}, {'ratios': ['9:16']},
                        {'account_category': '普通账号'}, {'native_media_bridge': True}):
            manifest = dialog_image_manifest(experimental25_manifest)
            manifest['models'][0].update(changes)
            with self.subTest(changes=changes):
                self.assert_invalid(manifest)
        for key in ('video_entry', 'model', 'duration', 'ratio', 'skill_entry', 'work_entry', 'upload_video'):
            manifest = dialog_image_manifest(experimental25_manifest)
            manifest['models'][0]['selectors'][key] = '#not-an-image-lifecycle-control'
            with self.subTest(key=key):
                self.assert_invalid(manifest)

    def test_observed_native_attachment_state_does_not_need_invented_error_or_pending_css(self):
        for factory in (dialog_manifest, experimental25_manifest):
            manifest = native_image_manifest(factory)
            with self.subTest(factory=factory.__name__):
                try:
                    Capabilities.validate(manifest)
                except ProviderError as error:
                    self.fail('Observed native attachment-state adapter was rejected: ' + str(error))
        with tempfile.TemporaryDirectory() as folder:
            caps = Capabilities(Path(folder) / 'capabilities.json')
            try:
                caps.save(native_image_manifest())
            except ProviderError as error:
                self.fail('Observed native attachment-state adapter could not be saved: ' + str(error))
            public = caps.public_models()[0]
        self.assertNotIn('native_state', json.dumps(public))
        self.assertEqual(public['references']['images']['validation_status'], 'experimental')

    def test_native_attachment_state_is_only_available_to_explicitly_observed_image_trial(self):
        for native_state in (None, '', True, 'some-native-state', 'assume_ready'):
            manifest = native_image_manifest()
            manifest['models'][0]['references']['images']['native_state'] = native_state
            with self.subTest(native_state=native_state):
                self.assert_invalid(manifest)
        for missing in ('upload_image', 'upload_ready'):
            manifest = native_image_manifest()
            model = manifest['models'][0]
            del model['selectors'][missing]
            model['selectors'].update(upload='#other', upload_trigger='#button', upload_option='#file')
            with self.subTest(missing=missing):
                self.assert_invalid(manifest)
        manifest = dialog_video_manifest()
        manifest['models'][0]['references']['video']['native_state'] = 'attachment_state_v1'
        self.assert_invalid(manifest)
        for extra in ('upload', 'upload_error', 'upload_pending', 'upload_trigger', 'upload_option'):
            manifest = native_image_manifest(experimental25_manifest)
            manifest['models'][0]['selectors'][extra] = '#unobserved-control'
            with self.subTest(extra=extra):
                self.assert_invalid(manifest)


class DialogImageRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.provider = DoubaoProvider(root=self.root / 'provider', enabled=True)
        self.provider.import_ck('one', ck(), PROXY)
        self.provider.accounts.save_diagnostics('one', {'route_verified': True, 'login_verified': True})
        self.provider.manifest.save(dialog_image_manifest())
        self.folder = self.root / 'owned-task'
        self.folder.mkdir()
        self.image = self.folder / IMAGE_TOKEN
        self.image.write_bytes(b'image-file-content-for-admission')

    def request(self, **values):
        return {'model': 'dialog_seedance_2.0_fast', 'duration': 15, 'ratio': '16:9',
                'prompt': '  按参考图生成小猫跳舞  ', **values}

    def test_nine_image_request_retains_all_ordered_tokens_and_rejects_tenth(self):
        manifest = native_image_manifest(experimental25_manifest)
        manifest['models'][0]['references']['images'].update(
            max_count=9, multi_upload_observation_id='a'*64)
        try:
            self.provider.manifest.save(manifest)
            tokens = [f'{i:032x}.png' for i in range(1, 10)]
            result = self.provider.validate_request(self.request(
                model='dialog_seedance_2.5', duration=30, reference_uploads=tokens))
        except ProviderError as error:
            self.fail('Authorized nine-image 30-second request was rejected: ' + str(error))
        self.assertEqual(result['reference_uploads'], tokens)
        self.assertEqual(result['submitted_duration'], 30)
        with self.assertRaises(ProviderError) as caught:
            self.provider.validate_request(self.request(model='dialog_seedance_2.5', duration=30,
                reference_uploads=tokens + ['0000000000000000000000000000000a.png']))
        self.assertEqual(caught.exception.code, 'unsupported_reference')

    def owned_request(self, **values):
        return self.request(reference_uploads=[IMAGE_TOKEN], reference_image_paths=[str(self.image)],
                            reference_upload_root=str(self.folder), **values)

    def test_image_token_is_admitted_before_staging_without_duration_remap(self):
        try:
            result = self.provider.validate_request(self.request(reference_uploads=[IMAGE_TOKEN]))
        except ProviderError as error:
            self.fail('Owned image-upload token was rejected before task staging: ' + str(error))
        self.assertEqual(result['reference_uploads'], [IMAGE_TOKEN])
        self.assertEqual(result['reference_image_paths'], [])
        self.assertEqual(result['reference_images'], [])
        self.assertEqual(result['submitted_duration'], 15)
        self.assertEqual(result['prompt'], '按参考图生成小猫跳舞')

    def test_server_staged_image_paths_do_not_double_count_the_upload_token(self):
        try:
            result = self.provider.validate_request(self.owned_request())
        except ProviderError as error:
            self.fail('Current task image was rejected after server staging: ' + str(error))
        self.assertEqual(result['reference_uploads'], [IMAGE_TOKEN])
        self.assertEqual(result['reference_image_paths'], [str(self.image.resolve())])
        self.assertEqual(result['reference_media_paths'], [])
        self.assertEqual(result['entry_mode'], 'ordinary_dialog')
        # The worker reconstructs owned task paths from durable refs, without
        # carrying the original incoming token list through the task database.
        paths_only = self.owned_request()
        paths_only.pop('reference_uploads')
        self.assertEqual(self.provider.validate_request(paths_only)['reference_image_paths'],
                         [str(self.image.resolve())])

    def test_experimental_25_image_keeps_real_thirty_seconds_and_category_requirement(self):
        self.provider.manifest.save(dialog_image_manifest(experimental25_manifest))
        try:
            result = self.provider.validate_request(self.owned_request(model='dialog_seedance_2.5', duration=30))
        except ProviderError as error:
            self.fail('Native image upload trial was rejected for 2.5: ' + str(error))
        self.assertEqual(result['submitted_duration'], 30)
        self.assertEqual(result['duration'], 30)
        self.assertNotIn('duration_profile', result)
        self.assertEqual(self.provider.manifest.load()['models'][0]['account_category'], '标准账号')
        with self.assertRaises(ProviderError) as caught:
            self.provider.validate_request(self.request(model='dialog_seedance_2.5', duration=15,
                                                        reference_uploads=[IMAGE_TOKEN]))
        self.assertEqual(caught.exception.code, 'unsupported_duration')

    def test_image_lists_reject_wrong_type_duplicates_non_image_tokens_and_excess_count(self):
        for tokens in (IMAGE_TOKEN, [IMAGE_TOKEN, IMAGE_TOKEN], ['a' * 32 + '.mp4'],
                       ['https://example.test/cat.png'], ['bad.png'], [None],
                       [IMAGE_TOKEN, 'd' * 32 + '.jpg']):
            with self.subTest(tokens=tokens), self.assertRaises(ProviderError) as caught:
                self.provider.validate_request(self.request(reference_uploads=tokens))
            self.assertEqual(caught.exception.code, 'unsupported_reference')

    def test_raw_urls_and_foreign_mismatched_or_unowned_paths_are_rejected(self):
        outside = self.root / ('d' * 32 + '.png')
        outside.write_bytes(b'foreign-image')
        bad_requests = [self.request(reference_images=['https://example.test/cat.png']),
                        self.request(reference_images='["https://example.test/cat.png"]'),
                        self.request(reference_image_paths=[str(self.image)]),
                        self.request(reference_image_paths=[str(outside)], reference_upload_root=str(self.folder)),
                        self.request(reference_image_paths=[self.image], reference_upload_root=str(self.folder)),
                        self.request(reference_image_paths=[str(self.image), str(self.image)], reference_upload_root=str(self.folder)),
                        self.request(reference_image_paths=[str(self.image)], reference_upload_root=str(self.folder),
                                     reference_uploads=['d' * 32 + '.png'])]
        for request in bad_requests:
            with self.subTest(fields=sorted(request)), self.assertRaises(ProviderError) as caught:
                self.provider.validate_request(request)
            self.assertEqual(caught.exception.code, 'unsupported_reference')

    def test_owned_image_size_limit_is_checked_before_admitting_a_task(self):
        for size in (0, 15 * 1024 * 1024 + 1):
            self.image.write_bytes(b'x' * size)
            with self.subTest(size=size), self.assertRaises(ProviderError) as caught:
                self.provider.validate_request(self.owned_request())
            self.assertEqual(caught.exception.code, 'unsupported_reference')

    def test_unobserved_or_disabled_images_and_mixed_video_stay_rejected(self):
        for manifest in (dialog_manifest(), dialog_image_manifest(), dialog_video_manifest()):
            if manifest['models'][0]['references'].get('images'):
                manifest['models'][0]['references']['images']['enabled'] = False
            self.provider.manifest.save(manifest)
            with self.subTest(model=manifest['models'][0]['id']), self.assertRaises(ProviderError) as caught:
                self.provider.validate_request(self.request(reference_uploads=[IMAGE_TOKEN]))
            self.assertEqual(caught.exception.code, 'unsupported_reference')
        manifest = dialog_image_manifest()
        video = dialog_video_manifest()['models'][0]
        manifest['models'][0]['references']['video'] = copy.deepcopy(video['references']['video'])
        manifest['models'][0]['evidence']['video_task_id'] = 'completed-reference-video'
        manifest['models'][0]['selectors']['upload_video'] = '#video-file'
        self.provider.manifest.save(manifest)
        with self.assertRaises(ProviderError) as caught:
            self.provider.validate_request(self.request(reference_uploads=[IMAGE_TOKEN],
                reference_media_uploads=['b' * 32 + '.mp4']))
        self.assertEqual(caught.exception.code, 'unsupported_reference')


if __name__ == '__main__':
    unittest.main()
