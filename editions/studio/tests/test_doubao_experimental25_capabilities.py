"""Experimental options never masquerade as a measured video capability."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from providers.base import ProviderError
from providers.doubao.capabilities import Capabilities
from test_doubao_dialog_capabilities import dialog_manifest
from test_doubao_native_capabilities import native_manifest


def experimental25_manifest():
    manifest = dialog_manifest()
    model = manifest['models'][0]
    model.update(id='dialog_seedance_2.5', label='Seedance 2.5（30秒试验）',
                 validation_status='experimental', account_category='标准账号',
                 durations=[30], combinations=[{'duration': 30, 'ratio': '16:9'}],
                 receipt_patterns=['本次使用 Seedance 2.5 生成'],
                 evidence={'native_options_observation_id': 'native-options-observation-fixture'})
    return manifest


class Experimental25CapabilityTests(unittest.TestCase):
    def assert_invalid(self, manifest):
        with self.assertRaises(ProviderError) as caught:
            Capabilities.validate(manifest)
        self.assertEqual(caught.exception.code, 'invalid_capabilities')

    def test_observed_option_is_exposed_as_experimental_without_fabricated_video_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            caps = Capabilities(Path(root) / 'capabilities.json')
            caps.save(experimental25_manifest())
            public = caps.public_models()[0]
        self.assertEqual(public['id'], 'dialog_seedance_2.5')
        self.assertEqual(public['validation_status'], 'experimental')
        self.assertEqual(public['account_category'], '标准账号')
        self.assertIn('试验', public['label'])
        self.assertEqual(public['durations'], [30])
        self.assertEqual(public['ratios'], ['16:9'])
        self.assertEqual(public['combinations'], [{'duration': 30, 'ratio': '16:9'}])
        self.assertNotIn('evidence', public)
        self.assertNotIn('selectors', public)
        self.assertNotIn('native-options-observation-fixture', json.dumps(public))

    def test_experiment_cannot_change_model_mode_category_duration_or_ratio(self):
        for changes in ({'id': 'dialog_seedance_2.0_fast'}, {'id': 'seedance_2.5'},
                        {'entry_mode': 'native_controls'}, {'entry_mode': 'work_skill'},
                        {'skill': {'id': 'removed'}}, {'account_category': '普通账号'},
                        {'account_category': ''}, {'durations': [15]}, {'durations': [15, 30]},
                        {'durations': [30, 30]}, {'ratios': ['9:16']},
                        {'ratios': ['16:9', '9:16']}, {'prompt_model_label': 'Seedance 2.0 Fast'},
                        {'native_media_bridge': True}, {'label': 'Seedance 2.5（已验证）'}):
            manifest = experimental25_manifest()
            manifest['models'][0].update(changes)
            with self.subTest(changes=changes):
                self.assert_invalid(manifest)

    def test_native_observation_requires_a_nonempty_string_not_text_video_claim(self):
        for value in (None, '', '   ', True, 17, [], {}):
            manifest = experimental25_manifest()
            manifest['models'][0]['evidence'] = {'native_options_observation_id': value,
                                               'text_task_id': 'not-native-option-proof'}
            with self.subTest(value=value):
                self.assert_invalid(manifest)
        manifest = experimental25_manifest()
        manifest['models'][0]['evidence'] = {'text_task_id': 'not-native-option-proof'}
        self.assert_invalid(manifest)

    def test_experiment_needs_exactly_one_plain_thirty_second_pair(self):
        for combinations in (None, [], [{'duration': 15, 'ratio': '16:9'}],
                             [{'duration': 30, 'ratio': '9:16'}],
                             [{'duration': 30, 'ratio': '16:9', 'resolution': 'auto'}],
                             [{'duration': 30, 'ratio': '16:9', 'evidence': {}}],
                             [{'duration': 30, 'ratio': '16:9'}] * 2):
            manifest = experimental25_manifest()
            manifest['models'][0]['combinations'] = combinations
            with self.subTest(combinations=combinations):
                self.assert_invalid(manifest)

    def test_only_ordinary_composer_send_assistant_and_new_chat_are_available(self):
        for key in ('composer', 'send', 'assistant', 'new_chat'):
            manifest = experimental25_manifest()
            manifest['models'][0]['selectors'].pop(key)
            with self.subTest(missing=key):
                self.assert_invalid(manifest)
        for key in ('video_entry', 'model', 'duration', 'ratio', 'skill_entry', 'work_entry', 'upload'):
            manifest = experimental25_manifest()
            manifest['models'][0]['selectors'][key] = '#unverified-control'
            with self.subTest(extra=key):
                self.assert_invalid(manifest)

    def test_experimental_flag_never_opens_materials_or_optional_features(self):
        for key in ('images', 'audio', 'video'):
            manifest = experimental25_manifest()
            model = manifest['models'][0]
            model['references'][key] = {'enabled': True, 'max_count': 1, 'max_bytes': 1024}
            model['evidence'][{'images': 'image', 'audio': 'audio', 'video': 'video'}[key] + '_task_id'] = 'some-task'
            with self.subTest(reference=key):
                self.assert_invalid(manifest)
        for key in ('resolution', 'no_watermark'):
            manifest = experimental25_manifest()
            model = manifest['models'][0]
            model['features'][key] = {'enabled': True, 'values': ['720p']}
            model['evidence'][key + '_task_id'] = 'some-task'
            with self.subTest(feature=key):
                self.assert_invalid(manifest)
        manifest = experimental25_manifest()
        manifest['models'][0]['references'] = {'video': {'enabled': False}}
        manifest['models'][0]['features'] = {'resolution': {'enabled': False}}
        Capabilities.validate(manifest)

    def test_verified_models_keep_measured_artifact_checks_and_reject_unknown_status(self):
        for factory in (dialog_manifest, native_manifest):
            for status in ('experimental', 'unverified', 'preview', None, True, ''):
                manifest = factory()
                manifest['models'][0]['validation_status'] = status
                with self.subTest(factory=factory.__name__, status=status):
                    self.assert_invalid(manifest)
            manifest = factory()
            manifest['models'][0]['validation_status'] = 'verified'
            Capabilities.validate(manifest)
            del manifest['models'][0]['evidence']['text_task_id']
            self.assert_invalid(manifest)
        manifest = experimental25_manifest()
        manifest['models'][0]['validation_status'] = 'verified'
        self.assert_invalid(manifest)
        manifest = dialog_manifest()
        model = manifest['models'][0]
        model.update(id='dialog_seedance_2.5', label='Seedance 2.5（已验证）',
                     validation_status='verified', durations=[30])
        model['combinations'][0].update(duration=30)
        model['combinations'][0]['evidence'].update(receipt_model='Seedance 2.5', actual_duration=30.1)
        Capabilities.validate(manifest)
        for field in ('artifact_sha256', 'conversation_id', 'actual_duration', 'receipt_model'):
            broken = copy.deepcopy(manifest)
            del broken['models'][0]['combinations'][0]['evidence'][field]
            with self.subTest(missing=field):
                self.assert_invalid(broken)


if __name__ == '__main__':
    unittest.main()
