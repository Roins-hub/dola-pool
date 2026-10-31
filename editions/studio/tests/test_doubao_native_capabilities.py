"""Capability admission requires successful evidence, never UI preferences."""
from pathlib import Path
import json
import tempfile
import time
import unittest

from providers.base import ProviderError
from providers.doubao.capabilities import Capabilities
from test_doubao_runtime import verified_manifest


def native_manifest():
    now = time.time()
    return {'version': 'native-fixture', 'verified_at': now, 'expires_at': now + 86400,
            'models': [{'id': 'seedance_2.5', 'label': 'Seedance 2.5', 'enabled': True,
                        'entry_mode': 'native_controls', 'native_media_bridge': True,
                        'durations': [15], 'ratios': ['16:9'],
                        'combinations': [{'duration': 15, 'ratio': '16:9'}],
                        'selectors': {'video_entry': '#video', 'model': '#model', 'duration': '#duration',
                                      'ratio': '#ratio', 'composer': '#prompt', 'send': '#send',
                                      'assistant': '.assistant'},
                        'receipt_patterns': ['已开始生成'], 'rejection_patterns': ['无法生成'],
                        'evidence': {'text_task_id': 'verified-native-text', 'native_store_ready_task_id': 'verified-native-store'},
                        'references': {}, 'features': {}}]}


def enable_reference(model, kind):
    model['references'][kind] = {'enabled': True, 'max_count': 2, 'max_bytes': 1024 * 1024}
    model['evidence'][{'images': 'image', 'audio': 'audio', 'video': 'video'}[kind] + '_task_id'] = 'verified-' + kind
    model['selectors'].update(upload_trigger='[data-testid="upload_file_button"]',
                              upload_option='[role="option"]:text-is("上传文件或图片")',
                              upload_ready='[data-testid="uploaded-file"]')


class NativeCapabilityTests(unittest.TestCase):
    def assert_invalid(self, manifest):
        with self.assertRaises(ProviderError) as caught:
            Capabilities.validate(manifest)
        self.assertEqual(caught.exception.code, 'invalid_capabilities')

    def test_native_video17_model_needs_observed_native_controls(self):
        Capabilities.validate(native_manifest())

    def test_legacy_native_controls_still_all_required(self):
        Capabilities.validate(verified_manifest())
        for selector in ('video_entry', 'model', 'duration', 'ratio', 'composer', 'send', 'assistant'):
            manifest = verified_manifest()
            manifest['models'][0]['selectors'].pop(selector)
            with self.subTest(selector=selector):
                self.assert_invalid(manifest)

    def test_native_entry_mode_is_not_guessed_from_prompt(self):
        manifest = native_manifest()
        manifest['models'][0]['entry_mode'] = 'guess_from_prompt'
        self.assert_invalid(manifest)

    def test_native_combinations_are_explicit_and_bounded_by_observed_values(self):
        for combinations in (None, [], [{'duration': 30, 'ratio': '16:9'}],
                             [{'duration': 15, 'ratio': '9:16'}],
                             [{'duration': True, 'ratio': '16:9'}],
                             [{'duration': 15, 'ratio': '16:9', 'resolution': '4k'}]):
            manifest = native_manifest()
            manifest['models'][0]['combinations'] = combinations
            with self.subTest(combinations=combinations):
                self.assert_invalid(manifest)

    def test_each_material_kind_requires_its_own_completed_task_and_native_upload(self):
        for kind in ('images', 'audio', 'video'):
            manifest = native_manifest()
            model = manifest['models'][0]
            enable_reference(model, kind)
            Capabilities.validate(manifest)
            evidence = {'images': 'image', 'audio': 'audio', 'video': 'video'}[kind] + '_task_id'
            del model['evidence'][evidence]
            with self.subTest(kind=kind, missing='evidence'):
                self.assert_invalid(manifest)
            model['evidence'][evidence] = 'verified'
            del model['evidence']['native_store_ready_task_id']
            with self.subTest(kind=kind, missing='native-store-proof'):
                self.assert_invalid(manifest)

    def test_material_count_and_bytes_have_typed_safety_limits(self):
        for key, value in (('max_count', True), ('max_count', 0), ('max_count', 51),
                           ('max_bytes', True), ('max_bytes', 0), ('max_bytes', 201 * 1024 * 1024)):
            manifest = native_manifest()
            model = manifest['models'][0]
            enable_reference(model, 'video')
            model['references']['video'][key] = value
            with self.subTest(key=key, value=value):
                self.assert_invalid(manifest)

    def test_legacy_audio_and_video_need_the_native_video17_bridge(self):
        for kind in ('audio', 'video'):
            manifest = verified_manifest()
            enable_reference(manifest['models'][0], kind)
            with self.subTest(kind=kind):
                self.assert_invalid(manifest)

    def test_video17_native_input_is_supported_and_legacy_image_chooser_is_not(self):
        manifest = native_manifest()
        model = manifest['models'][0]
        enable_reference(model, 'images')
        model['selectors'].pop('upload_trigger')
        model['selectors'].pop('upload_option')
        model['selectors']['upload'] = 'input[type="file"]'
        Capabilities.validate(manifest)
        legacy = verified_manifest()
        legacy_model = legacy['models'][0]
        legacy_model['selectors'].pop('upload')
        legacy_model['selectors'].update(upload_trigger='#upload', upload_option='#option')
        self.assert_invalid(legacy)

    def test_enabled_resolution_and_no_watermark_each_need_evidence(self):
        for key, values in (('resolution', {'enabled': True, 'values': ['720p', '1080p']}),
                            ('no_watermark', {'enabled': True})):
            manifest = native_manifest()
            model = manifest['models'][0]
            model['features'][key] = values
            with self.subTest(feature=key, evidence=False):
                self.assert_invalid(manifest)
            model['evidence'][key + '_task_id'] = 'verified-' + key
            Capabilities.validate(manifest)

    def test_resolution_combo_requires_the_same_verified_resolution(self):
        manifest = native_manifest()
        model = manifest['models'][0]
        model['combinations'][0]['resolution'] = '1080p'
        self.assert_invalid(manifest)
        model['features']['resolution'] = {'enabled': True, 'values': ['1080p']}
        model['evidence']['resolution_task_id'] = 'verified-resolution'
        Capabilities.validate(manifest)
        model['features']['resolution']['values'] = ['720p']
        self.assert_invalid(manifest)

    def test_removed_cast_cannot_be_advertised_even_with_old_valid_evidence(self):
        manifest = native_manifest()
        model = manifest['models'][0]
        enable_reference(model, 'images')
        model['features']['cast'] = {'enabled': True}
        model['evidence']['cast_task_id'] = 'verified-cast'
        self.assert_invalid(manifest)

    def test_loading_legacy_cast_metadata_keeps_other_verified_parameters(self):
        manifest = native_manifest()
        model = manifest['models'][0]
        model['features']['cast'] = {'enabled': True}
        model['evidence']['cast_task_id'] = 'obsolete-review'
        with tempfile.TemporaryDirectory() as root:
            caps = Capabilities(Path(root) / 'capabilities.json')
            caps.path.write_text(json.dumps(manifest), encoding='utf-8')
            loaded = caps.load()
            self.assertEqual(len(loaded['models']), 1)
            public = caps.public_models()[0]
        self.assertEqual(public['durations'], [15])
        self.assertEqual(public['ratios'], ['16:9'])
        self.assertNotIn('cast', loaded['models'][0]['features'])
        self.assertNotIn('cast_task_id', loaded['models'][0]['evidence'])
        self.assertEqual(loaded['models'][0]['evidence']['text_task_id'], 'verified-native-text')

    def test_feature_types_and_evidence_values_are_not_truthy_claims(self):
        for evidence in (True, {}, [], 123, '   '):
            manifest = native_manifest()
            manifest['models'][0]['evidence']['text_task_id'] = evidence
            with self.subTest(evidence=evidence):
                self.assert_invalid(manifest)
        for feature in ({'enabled': 'yes'}, {'enabled': True, 'values': ['4k']},
                        {'enabled': True, 'values': []}, {'enabled': True, 'values': ['720p', '720p']}):
            manifest = native_manifest()
            model = manifest['models'][0]
            model['features']['resolution'] = feature
            model['evidence']['resolution_task_id'] = 'verified'
            with self.subTest(feature=feature):
                self.assert_invalid(manifest)

    def test_public_native_features_exclude_private_selectors_and_evidence(self):
        manifest = native_manifest()
        model = manifest['models'][0]
        model['features']['resolution'] = {'enabled': True, 'values': ['1080p'], 'evidence': 'private'}
        model['evidence']['resolution_task_id'] = 'private-resolution-task'
        with tempfile.TemporaryDirectory() as root:
            caps = Capabilities(Path(root) / 'capabilities.json')
            caps.save(manifest)
            public = caps.public_models()[0]
        self.assertEqual(public['entry_mode'], 'native_controls')
        self.assertEqual(public['features'], {'resolution': {'enabled': True, 'values': ['1080p']}})
        self.assertNotIn('selectors', public)
        self.assertNotIn('skill', public)
        self.assertNotIn('evidence', public)


if __name__ == '__main__':
    unittest.main()
