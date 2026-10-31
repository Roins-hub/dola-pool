"""Ordinary chat admits only completed, measured video combinations."""
import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from providers.base import ProviderError
from providers.doubao import DoubaoProvider
from providers.doubao.capabilities import Capabilities
from test_doubao_provider import ck, PROXY


def dialog_manifest():
    now = time.time()
    return {'version': 'dialog-fixture', 'verified_at': now, 'expires_at': now + 86400,
            'models': [{'id': 'dialog_seedance_2.0_fast',
                        'label': 'Seedance 2.0 Fast（普通对话）', 'enabled': True,
                        'entry_mode': 'ordinary_dialog', 'durations': [15], 'ratios': ['16:9'],
                        'combinations': [{'duration': 15, 'ratio': '16:9', 'evidence': {
                            'task_id': 'completed-dialog-video', 'conversation_id': '70000000000000001',
                            'receipt_model': 'Seedance 2.0 Fast', 'actual_duration': 15.073,
                            'width': 1280, 'height': 720, 'artifact_sha256': 'a' * 64}}],
                        'selectors': {'new_chat': '[data-testid="create_conversation_button"]',
                                      'composer': '[contenteditable="true"]', 'send': '#send',
                                      'assistant': '[data-testid="receive_message"]'},
                        'receipt_patterns': ['本次使用 Seedance 2.0 Fast 生成'],
                        'rejection_patterns': ['无法生成视频'],
                        'evidence': {'text_task_id': 'completed-dialog-video'},
                        'references': {}, 'features': {}}]}


class DialogCapabilityTests(unittest.TestCase):
    def assert_invalid(self, manifest):
        with self.assertRaises(ProviderError) as error:
            Capabilities.validate(manifest)
        self.assertEqual(error.exception.code, 'invalid_capabilities')

    def test_completed_dialog_video_can_admit_without_invented_native_controls(self):
        Capabilities.validate(dialog_manifest())
        for key in ('composer', 'send', 'assistant', 'new_chat'):
            manifest = dialog_manifest()
            manifest['models'][0]['selectors'].pop(key)
            with self.subTest(key=key):
                self.assert_invalid(manifest)

    def test_dialog_modes_and_model_labels_cannot_reenable_removed_or_arbitrary_entries(self):
        for changes in ({'entry_mode': 'work_skill'}, {'entry_mode': 'unknown'},
                        {'id': 'seedance_2.5'}, {'skill': {'id': 'old'}},
                        {'prompt_model_label': 'some other model'}, {'native_media_bridge': True}):
            manifest = dialog_manifest()
            manifest['models'][0].update(changes)
            with self.subTest(changes=changes):
                self.assert_invalid(manifest)

    def test_every_combination_needs_its_own_receipt_and_delivered_video_proof(self):
        for field in ('task_id', 'conversation_id', 'receipt_model', 'actual_duration',
                      'width', 'height', 'artifact_sha256'):
            manifest = dialog_manifest()
            del manifest['models'][0]['combinations'][0]['evidence'][field]
            with self.subTest(field=field):
                self.assert_invalid(manifest)
        manifest = dialog_manifest()
        manifest['models'][0]['durations'].append(30)
        manifest['models'][0]['combinations'].append({'duration': 30, 'ratio': '16:9'})
        self.assert_invalid(manifest)

    def test_model_text_evidence_must_link_to_one_of_its_completed_combinations(self):
        manifest = dialog_manifest()
        manifest['models'][0]['evidence']['text_task_id'] = 'an-unrelated-task'
        self.assert_invalid(manifest)

    def test_incorrect_media_measurements_or_model_receipt_do_not_open_dialog(self):
        for changes in ({'actual_duration': 30}, {'actual_duration': True},
                        {'actual_duration': float('nan')}, {'width': 720, 'height': 1280},
                        {'width': 0}, {'height': True}, {'artifact_sha256': 'not-a-hash'},
                        {'receipt_model': 'Seedance 2.5'}):
            manifest = dialog_manifest()
            manifest['models'][0]['combinations'][0]['evidence'].update(changes)
            with self.subTest(changes=changes):
                self.assert_invalid(manifest)

    def test_dialog_advertised_durations_and_ratios_have_no_unproven_values(self):
        for key, extra in (('durations', 30), ('ratios', '9:16')):
            manifest = dialog_manifest()
            manifest['models'][0][key].append(extra)
            with self.subTest(key=key):
                self.assert_invalid(manifest)

    def test_text_video_does_not_unlock_native_controls_or_material_features(self):
        for key in ('video_entry', 'model', 'duration', 'ratio'):
            manifest = dialog_manifest()
            manifest['models'][0]['selectors'][key] = '#invented-native-control'
            with self.subTest(selector=key):
                self.assert_invalid(manifest)
        for key in ('resolution', 'no_watermark'):
            manifest = dialog_manifest()
            manifest['models'][0]['features'][key] = {'enabled': True, 'values': ['720p']}
            manifest['models'][0]['evidence'][key + '_task_id'] = 'text-only-video'
            with self.subTest(feature=key):
                self.assert_invalid(manifest)
        for kind in ('images', 'audio', 'video'):
            manifest = dialog_manifest()
            manifest['models'][0]['references'][kind] = {'enabled': True, 'max_count': 1, 'max_bytes': 1024}
            with self.subTest(reference=kind):
                self.assert_invalid(manifest)

    def test_public_combinations_keep_choices_without_private_task_or_artifact_proof(self):
        with tempfile.TemporaryDirectory() as root:
            caps = Capabilities(Path(root) / 'capabilities.json')
            caps.save(dialog_manifest())
            public = caps.public_models()[0]
        self.assertEqual(public['entry_mode'], 'ordinary_dialog')
        self.assertEqual(public['combinations'], [{'duration': 15, 'ratio': '16:9'}])
        self.assertNotIn('evidence', public)
        self.assertNotIn('selectors', public)
        self.assertNotIn('artifact_sha256', json.dumps(public))


class DialogRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = DoubaoProvider(root=Path(self.temp.name), enabled=True)
        self.provider.import_ck('one', ck(), PROXY)
        self.provider.accounts.save_diagnostics('one', {'route_verified': True, 'login_verified': True})
        # The manifest validator has separate tests; admission failures must expose the request gate.
        self.provider.manifest.path.write_text(json.dumps(dialog_manifest()), encoding='utf-8')

    def request(self, **values):
        return {'model': 'dialog_seedance_2.0_fast', 'duration': 15, 'ratio': '16:9',
                'prompt': '  小猫跳舞  ', **values}

    def test_request_keeps_actual_fifteen_seconds_and_explicit_dialog_mode(self):
        result = self.provider.validate_request(self.request())
        self.assertEqual(result['model'], 'dialog_seedance_2.0_fast')
        self.assertEqual(result['duration'], 15)
        self.assertEqual(result['submitted_duration'], 15)
        self.assertEqual(result['ratio'], '16:9')
        self.assertEqual(result['entry_mode'], 'ordinary_dialog')
        self.assertEqual(result['prompt'], '小猫跳舞')
        self.assertTrue(result['auto_confirm'])
        self.assertEqual(result['resolution'], 'auto')
        self.assertNotIn('chat_ability', result)
        self.assertNotIn('ability_param', result)
        self.assertFalse(self.provider.validate_request(self.request(auto_confirm=False))['auto_confirm'])

    def test_unverified_request_choices_cannot_be_remapped_or_claim_native_ability(self):
        for changes in ({'duration': 30}, {'duration_profile': 'fast15-via30'}, {'ratio': '9:16'},
                        {'entry_mode': 'work_skill'}, {'entry_mode': 'native_controls'},
                        {'auto_confirm': 'true'}, {'chat_ability': {'model': 'seedance_2.5'}},
                        {'ability_param': {'duration': 30}}):
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                self.provider.validate_request(self.request(**changes))

    def test_unverified_references_resolution_and_download_options_are_rejected(self):
        for changes in ({'reference_images': ['https://example.test/cat.png']},
                        {'reference_image_paths': ['private.png']}, {'reference_uploads': ['a' * 32 + '.png']},
                        {'reference_media_paths': [{'kind': 'audio', 'path': 'private.wav'}]},
                        {'reference_media_uploads': ['b' * 32 + '.mp4']}, {'reference_audio': 'audio'},
                        {'reference_video': 'video'},
                        {'resolution': '720p'}, {'require_no_watermark': True},
                        {'require_no_watermark': 'false'}):
            with self.subTest(changes=changes), self.assertRaises(ProviderError):
                self.provider.validate_request(self.request(**changes))

    def test_removed_cast_is_explicitly_rejected_instead_of_ignoring_the_selection(self):
        for key in ('cast', 'cast_ids'):
            with self.subTest(key=key):
                with self.assertRaises(ProviderError) as error:
                    self.provider.validate_request(self.request(**{key: ['old-role-id']}))
                self.assertEqual(error.exception.code, 'unsupported_cast')

    def test_only_measured_pairs_are_admitted_in_a_multi_choice_manifest(self):
        manifest = dialog_manifest()
        model = manifest['models'][0]
        model['durations'].append(10)
        model['ratios'].append('9:16')
        other = copy.deepcopy(model['combinations'][0])
        other.update(duration=10, ratio='9:16')
        other['evidence'].update(task_id='other-completed-video', actual_duration=10.073,
                                 width=720, height=1280)
        model['combinations'].append(other)
        self.provider.manifest.save(manifest)
        self.assertEqual(self.provider.validate_request(self.request(duration=10, ratio='9:16'))['duration'], 10)
        for duration, ratio in ((15, '9:16'), (10, '16:9')):
            with self.subTest(duration=duration, ratio=ratio), self.assertRaises(ProviderError):
                self.provider.validate_request(self.request(duration=duration, ratio=ratio))


class DialogAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = DoubaoProvider(root=Path(self.temp.name), enabled=True)
        self.provider.manifest.path.write_text(json.dumps(dialog_manifest()), encoding='utf-8')

    def test_verified_choices_explain_missing_account_instead_of_claiming_video_unverified(self):
        capability = self.provider.capabilities()
        self.assertFalse(capability['enabled'])
        self.assertEqual(capability['models'][0]['durations'], [15])
        self.assertIn('已验证生成参数', capability['reason'])
        self.assertIn('没有已启用', capability['reason'])
        self.assertIn('恢复登录', capability['reason'])
        self.provider.import_ck('one', ck(), PROXY)
        self.provider.accounts.save_diagnostics('one', {'route_verified': True, 'login_verified': True})
        self.assertTrue(self.provider.capabilities()['enabled'])
        self.assertEqual(self.provider.capabilities()['reason'], '')

    def test_disabled_models_do_not_enable_generation_even_with_ready_account(self):
        self.provider.import_ck('one', ck(), PROXY)
        self.provider.accounts.save_diagnostics('one', {'route_verified': True, 'login_verified': True})
        manifest = dialog_manifest()
        manifest['models'][0]['enabled'] = False
        self.provider.manifest.path.write_text(json.dumps(manifest), encoding='utf-8')
        capability = self.provider.capabilities()
        self.assertFalse(capability['enabled'])
        self.assertIn('生成参数', capability['reason'])
        self.assertNotIn('没有已启用且登录', capability['reason'])

    def test_disabled_platform_has_its_own_actionable_reason(self):
        self.provider.enabled = False
        capability = self.provider.capabilities()
        self.assertFalse(capability['enabled'])
        self.assertIn('入口未启用', capability['reason'])


if __name__ == '__main__':
    unittest.main()
