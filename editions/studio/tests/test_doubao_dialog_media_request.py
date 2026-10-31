"""Ordinary-chat reference videos require their own proof and owned uploads."""
import copy
from pathlib import Path
import tempfile
import unittest

from providers.base import ProviderError
from providers.doubao import DoubaoProvider
from providers.doubao.capabilities import Capabilities
from test_doubao_dialog_capabilities import dialog_manifest
from test_doubao_provider import ck, PROXY


TOKEN = 'b' * 32 + '.mp4'


def dialog_video_manifest():
    manifest = dialog_manifest()
    model = manifest['models'][0]
    model['references']['video'] = {'enabled': True, 'max_count': 2, 'max_bytes': 1024}
    model['evidence']['video_task_id'] = 'completed-reference-video'
    model['selectors'].update(upload_video='input[type="file"][accept="video/*"]',
                              upload_ready='.uploaded-video-ready',
                              upload_pending='.upload-pending', upload_error='.upload-error')
    return manifest


class DialogMediaCapabilitiesTests(unittest.TestCase):
    def assert_invalid(self, manifest):
        with self.assertRaises(ProviderError) as caught:
            Capabilities.validate(manifest)
        self.assertEqual(caught.exception.code, 'invalid_capabilities')

    def test_ordinary_video_can_use_a_verified_input_or_file_chooser_without_video17(self):
        for changes in ({}, {'upload': '#upload'},
                        {'upload_trigger': '#upload-button', 'upload_option': '#upload-file'}):
            manifest = dialog_video_manifest()
            model = manifest['models'][0]
            if changes:
                model['selectors'].pop('upload_video')
                model['selectors'].update(changes)
            with self.subTest(changes=changes):
                Capabilities.validate(manifest)
                self.assertIsNot(model.get('native_media_bridge'), True)

    def test_text_only_success_never_unlocks_reference_video(self):
        for missing in ('video_task_id', 'text_task_id'):
            manifest = dialog_video_manifest()
            del manifest['models'][0]['evidence'][missing]
            with self.subTest(missing=missing):
                self.assert_invalid(manifest)
        manifest = dialog_video_manifest()
        model = manifest['models'][0]
        model['evidence']['video_task_id'] = model['evidence']['text_task_id']
        self.assert_invalid(manifest)
        manifest = dialog_video_manifest()
        manifest['models'][0]['combinations'][0].pop('evidence')
        self.assert_invalid(manifest)

    def test_all_upload_lifecycle_selectors_are_required(self):
        for key in ('upload_video', 'upload_ready', 'upload_pending', 'upload_error'):
            manifest = dialog_video_manifest()
            del manifest['models'][0]['selectors'][key]
            with self.subTest(missing=key):
                self.assert_invalid(manifest)

    def test_video_proof_does_not_admit_images_audio_features_or_video17(self):
        for kind in ('images', 'audio'):
            manifest = dialog_video_manifest()
            model = manifest['models'][0]
            model['references'][kind] = copy.deepcopy(model['references']['video'])
            model['evidence'][('image' if kind == 'images' else kind) + '_task_id'] = 'other-proof'
            with self.subTest(kind=kind):
                self.assert_invalid(manifest)
        for change in ({'native_media_bridge': True},
                       {'features': {'resolution': {'enabled': True, 'values': ['720p']}}}):
            manifest = dialog_video_manifest()
            manifest['models'][0].update(change)
            self.assert_invalid(manifest)

    def test_unknown_reference_options_do_not_create_an_upload_bypass(self):
        manifest = dialog_video_manifest()
        manifest['models'][0]['references']['video']['assume_ready'] = True
        self.assert_invalid(manifest)

    def test_public_manifest_exposes_limits_without_private_proof_or_selectors(self):
        with tempfile.TemporaryDirectory() as root:
            caps = Capabilities(Path(root) / 'capabilities.json')
            caps.save(dialog_video_manifest())
            model = caps.public_models()[0]
        self.assertEqual(model['references']['video'],
                         {'enabled': True, 'max_count': 2, 'max_bytes': 1024})
        self.assertNotIn('selectors', model)
        self.assertNotIn('evidence', model)


class DialogMediaRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = DoubaoProvider(root=Path(self.temp.name), enabled=True)
        self.provider.import_ck('one', ck(), PROXY)
        self.provider.accounts.save_diagnostics('one', {'route_verified': True, 'login_verified': True})
        self.provider.manifest.save(dialog_video_manifest())

    def request(self, **changes):
        if 'reference_media_paths' in changes:
            changes.setdefault('reference_upload_root', self.temp.name)
        return {'model': 'dialog_seedance_2.0_fast', 'duration': 15, 'ratio': '16:9',
                'prompt': '小猫按参考视频跳舞', **changes}

    def reference(self, **changes):
        path = Path(self.temp.name) / TOKEN
        path.write_bytes(b'video-fixture' * 4)
        return {'kind': 'video', 'path': str(path), **changes}

    def test_upload_tokens_admitted_before_task_staging_keep_the_real_duration(self):
        result = self.provider.validate_request(self.request(reference_media_uploads=[TOKEN]))
        self.assertEqual(result['reference_media_uploads'], [TOKEN])
        self.assertEqual(result['reference_media_paths'], [])
        self.assertEqual(result['submitted_duration'], 15)
        self.assertEqual(result['entry_mode'], 'ordinary_dialog')

    def test_server_resolved_paths_are_normalized_and_staged_tokens_not_double_counted(self):
        other = Path(self.temp.name) / ('c' * 32 + '.webm')
        other.write_bytes(b'another-video-fixture')
        references = [self.reference(), self.reference(path=str(other))]
        result = self.provider.validate_request(self.request(reference_media_paths=references,
            reference_media_uploads=[Path(value['path']).name for value in references]))
        self.assertEqual(len(result['reference_media_paths']), 2)
        self.assertEqual(result['reference_media_paths'][0]['path'], references[0]['path'])

    def test_wrong_kind_unowned_raw_paths_invalid_size_and_duplicates_are_rejected(self):
        for references in ([self.reference(kind='audio')], [self.reference(path='C:/private/movie.mp4')],
                           [self.reference(path=Path(self.temp.name) / TOKEN)],
                           [self.reference(size_bytes=0)], [self.reference(), self.reference()]):
            with self.subTest(references=references), self.assertRaises(ProviderError):
                self.provider.validate_request(self.request(reference_media_paths=references))
        reference = self.reference()
        for size in (0, 1025):
            Path(reference['path']).write_bytes(b'x' * size)
            with self.subTest(size=size), self.assertRaises(ProviderError):
                self.provider.validate_request(self.request(reference_media_paths=[reference]))
        for root in (None, ''):
            with self.subTest(root=root), self.assertRaises(ProviderError):
                self.provider.validate_request(self.request(reference_media_paths=[self.reference()],
                    reference_upload_root=root))

    def test_token_format_kind_count_duplicates_and_conflicting_staged_paths_are_rejected(self):
        for tokens in ([TOKEN, TOKEN], ['a' * 32 + '.mp3'], ['C:/private/movie.mp4'],
                       ['https://example.test/movie.mp4'], ['bad.mp4'],
                       [f'{number:032x}.mp4' for number in range(3)]):
            with self.subTest(tokens=tokens), self.assertRaises(ProviderError):
                self.provider.validate_request(self.request(reference_media_uploads=tokens))
        with self.assertRaises(ProviderError):
            self.provider.validate_request(self.request(reference_media_uploads=['d' * 32 + '.mp4'],
                reference_media_paths=[self.reference()]))

    def test_no_media_alias_or_native_parameter_shortcuts_are_accepted(self):
        for changes in ({'reference_video': 'https://example.test/a.mp4'},
                        {'reference_videos': [TOKEN]}, {'reference_media': [self.reference()]},
                        {'reference_images': ['https://example.test/cat.png']},
                        {'reference_uploads': ['a' * 32 + '.png']},
                        {'cast_ids': ['virtual-cat']}, {'chat_ability': {}},
                        {'ability_param': {'duration': 15}}):
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                self.provider.validate_request(self.request(**changes))

    def test_unverified_or_disabled_video_stays_rejected(self):
        for manifest in (dialog_manifest(), dialog_video_manifest()):
            if 'video' in manifest['models'][0]['references']:
                manifest['models'][0]['references']['video']['enabled'] = False
            self.provider.manifest.save(manifest)
            with self.subTest(manifest=manifest['version']), self.assertRaises(ProviderError) as caught:
                self.provider.validate_request(self.request(reference_media_uploads=[TOKEN]))
            self.assertEqual(caught.exception.code, 'unsupported_reference')


if __name__ == '__main__':
    unittest.main()
