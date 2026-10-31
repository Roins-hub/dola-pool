"""Use the bundled expert protocol without spending generation credits."""
import unittest
from unittest.mock import patch


class ExpertProtocolTests(unittest.IsolatedAsyncioTestCase):
    def test_submission_gate_checks_native_media_identity_with_the_protocol(self):
        import hashlib
        from plugin_expert30 import audit_request
        digest = lambda text: hashlib.sha256(text.encode()).hexdigest()
        manifest = {'version': 1, 'has_media': True, 'counts': {'image': 0, 'audio': 1, 'video': 1},
                    'files': [{'index': i + 1, 'kind': kind, 'sha256': digest(kind + '-bytes'),
                               'resource_sha256': digest(kind + '-native-uri')}
                              for i, kind in enumerate(('audio', 'video'))]}
        attachments = [{'type': 2, 'media': {'media_type': i + 1, 'uri': kind + '-native-uri'}}
                       for i, kind in enumerate(('audio', 'video'))]
        body = {'option': {'agent_mode': 1, 'model_config': {'model_item_key': '4'}}, 'messages': [
            {'content_blocks_v2': [{'block_type': 10000, 'content': {'text_block': {'text': 'exact protocol'}}},
                                  {'block_type': 10052, 'content': {'attachment_block': {'attachments': attachments}}}]}]}
        self.assertTrue(audit_request(body, 'exact protocol', attachment_manifest=manifest)['matches'])
        self.assertFalse(audit_request(body, 'changed protocol', attachment_manifest=manifest)['matches'])
        attachments.reverse()
        self.assertFalse(audit_request(body, 'exact protocol', attachment_manifest=manifest)['matches'])
        attachments.reverse()
        attachments[0]['media']['uri'] = 'unrelated-file'
        self.assertFalse(audit_request(body, 'exact protocol', attachment_manifest=manifest)['matches'])

    async def test_each_duration_keeps_selected_parameters_and_exact_user_text(self):
        from plugin_expert30 import prepare_protocol
        prompt = '第一行\n第二行  保留原文'
        for model, wire in [('seedance-2.0', 'seedance_v2.0'),
                            ('seedance-2.5', 'seedance_v2.5'),
                            ('seedance-2.0-std', 'seedance_v2.0_std'),
                            ('seedance-2.0-mini', 'seedance_v2.0_mini')]:
            for duration in (5, 10, 15, 30):
                with self.subTest(model=model, duration=duration):
                    plan = await prepare_protocol(prompt, model, duration, '9:16', {'resolution': '1080p'})
                    self.assertEqual(plan['selection'], dict(model=wire, duration=duration,
                                                             ratio='9:16', resolution='1080p'))
                    self.assertEqual(plan['draft_text'], prompt)
                    self.assertTrue(plan['expected_text'].endswith(prompt))
                    self.assertIn(f'时长：{duration}秒', plan['expected_text'])
                    self.assertEqual('两条独立15秒视频' in plan['expected_text'], duration == 30)

    async def test_two_part_uses_original_plugin_rule_once_and_keeps_original_prompt(self):
        from plugin_expert30 import prepare_protocol
        prompt = '第01段 原文\n第02段 原文'
        plan = await prepare_protocol(prompt, 'seedance-2.5', 30, '16:9', {'two_part': True})
        self.assertTrue(plan['draft_text'].startswith(prompt + '\n\n'))
        self.assertIn('每条15秒，合计30秒', plan['draft_text'])
        self.assertEqual(plan['expected_text'].count('【插件两段式生成规则 v2】'), 1)
        self.assertTrue(plan['expected_text'].endswith(plan['draft_text']))
        duplicate = await prepare_protocol(plan['draft_text'], 'seedance-2.5', 30, '16:9', {'two_part': True})
        self.assertEqual(duplicate['draft_text'], plan['draft_text'])

    async def test_two_part_is_strict_boolean_and_thirty_seconds_only(self):
        from plugin_expert30 import prepare_protocol
        for value in ('false', 1, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                await prepare_protocol('原文', 'seedance-2.5', 30, None, {'two_part': value})
        with self.assertRaises(ValueError):
            await prepare_protocol('原文', 'seedance-2.5', 15, None, {'two_part': True})

    async def test_short_result_finishes_observing_without_regeneration(self):
        import plugin_expert30 as worker
        for duration in (5, 10, 15):
            with self.subTest(duration=duration):
                source = {'clip_id': 'one', 'index': 1, 'vid': 'v' + 'a' * 24}
                polls = []

                class Session:
                    async def __aenter__(self): return self
                    async def __aexit__(self, *args): pass
                    async def poll(self):
                        polls.append(1)
                        return {'plugin_expert30': {'accepted': True, 'tools_finished': True,
                                                    'clips': [source], 'reply': ''}}

                async def resolve(*args):
                    return {**source, 'actual_duration': duration, 'local_path': 'verified-fixture.mp4'}

                with patch('video_polling.HttpVideoSession', return_value=Session()), \
                     patch('plugin_resources.resolve_clip', resolve), patch('config.POLL_INTERVAL', .001):
                    result = await worker.resume_video('fixture', '123456789', .02,
                                                       generation_options={'requested_duration': duration})
                self.assertTrue(result['generation_finished'])
                self.assertEqual(len(polls), 1)


if __name__ == '__main__':
    unittest.main()
