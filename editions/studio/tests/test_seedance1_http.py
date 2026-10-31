"""Exercise request construction only; no network or generation quota is used."""
import json
import unittest
from unittest.mock import patch

import video_worker_http as worker


class SeedanceOneHttpTests(unittest.TestCase):
    def test_seedance_one_builds_ic_mini_request_with_exact_requested_duration(self):
        for alias in ('seedance-1.0', 'ic_mini', 'seedance_v1.0'):
            for duration in (5, 10, 15):
                with self.subTest(alias=alias, duration=duration):
                    body = worker.build_request('用户原文', alias, duration, '9:16', [], {'fp': 'fixture'})
                    params = json.loads(body['chat_ability']['ability_param'])
                    self.assertEqual(params, {'model': 'ic_mini', 'duration': duration, 'ratio': '9:16'})
                    text = body['messages'][0]['content_block'][0]['content']['text_block']['text']
                    self.assertTrue(text.endswith('\n\n用户提示词：\n\n用户原文'))

    def test_seedance_one_long_duration_never_reaches_payload_builder(self):
        for duration in (30,):
            with patch.object(worker.protocol, 'build_payload') as payload:
                with self.assertRaises(ValueError):
                    worker.build_request('原文', 'seedance-1.0', duration, None, [], {'fp': 'fixture'})
                payload.assert_not_called()


if __name__ == '__main__':
    unittest.main()
