import importlib
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


class HttpSubmissionTests(unittest.TestCase):
    def worker(self):
        self.assertIsNotNone(importlib.util.find_spec('video_worker_http'),
                             'pure HTTP video submission is not installed')
        return importlib.import_module('video_worker_http')

    def test_newer_browser_session_is_used_without_overriding_a_later_ck_import(self):
        w = self.worker()
        load = getattr(w, 'load_account_cookies', None)
        self.assertIsNotNone(load, 'HTTP must use a refreshed browser session when the imported CK is older')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            base = root / 'cookies.json'
            base.write_text('[{"name":"sessionid","value":"imported"}]', encoding='utf-8')
            (root / 'polling').mkdir()
            refreshed = root / 'polling' / '12345.json'
            refreshed.write_text('{"cookies":[{"name":"sessionid","value":"refreshed"}]}', encoding='utf-8')
            os.utime(base, (100, 100)); os.utime(refreshed, (200, 200))
            self.assertEqual(load(root)[0]['value'], 'refreshed')
            os.utime(base, (300, 300))
            self.assertEqual(load(root)[0]['value'], 'imported')

    def test_request_preserves_prompt_and_reference_with_explicit_model_and_duration(self):
        w = self.worker()
        raw = '  第一镜头 0—15 秒\n第二镜头 15—30 秒，禁止删改。  '
        body = w.build_request(raw, 'seedance-2.5', 30, '9:16',
                               [{'name': 'reference.jpg', 'uri': 'fixture-image', 'width': 720, 'height': 1280}],
                               {'fp': 'fixture'})
        params = json.loads(body['chat_ability']['ability_param'])
        self.assertEqual(params, {'model': 'seedance_v2.5', 'duration': 30, 'ratio': '9:16'})
        texts = [b['content']['text_block']['text'] for m in body['messages']
                 for b in m['content_block'] if 'text_block' in b['content']]
        self.assertEqual(len(texts), 1)
        self.assertTrue(texts[0].endswith('\n\n用户提示词：\n\n' + raw))
        self.assertNotIn('生成参数：', texts[0])
        self.assertIn('fixture-image', json.dumps(body))

    def test_short_request_gets_no_thirty_second_prefix_and_model_is_not_silently_changed(self):
        w = self.worker()
        body = w.build_request('原始提示词', 'seedance-2.0', 10, None, [], {'fp': 'fixture'})
        self.assertEqual(json.loads(body['chat_ability']['ability_param'])['model'], 'seedance_v2.0')
        text = body['messages'][0]['content_block'][0]['content']['text_block']['text']
        self.assertIn('生成参数：视频时长10秒', text)
        self.assertNotIn('生成参数：视频时长30秒', text)
        self.assertNotIn('如平台单次只支持15秒，请按原文顺序生成两条独立15秒视频。', text)
        self.assertTrue(text.endswith('\n\n用户提示词：\n\n原始提示词'))
        with self.assertRaises(ValueError):
            w.build_request('test', 'seedance-2.0', 20, None, [], {'fp': 'fixture'})

    def test_transport_failure_does_not_repeat_a_post_or_expose_credentials(self):
        w = self.worker()
        import requests
        attempts = []
        class Session:
            def send(self, *args, **kwargs):
                attempts.append(1)
                raise requests.ConnectionError('https://user:secret@proxy.test?msToken=private')
        prepared = requests.Request('POST', 'https://www.dola.com/chat/completion', json={}).prepare()
        with self.assertRaisesRegex(w.HttpSubmissionError, 'ConnectionError') as caught:
            w.send_once(Session(), prepared)
        self.assertEqual(len(attempts), 1)
        self.assertNotIn('secret', str(caught.exception))
        self.assertNotIn('private', str(caught.exception))

    def test_fast_unsupported_duration_is_rejected_before_building_a_payload(self):
        w = self.worker()
        for duration in (20, 35):
            with self.subTest(duration=duration), patch.object(w.protocol, 'build_payload') as payload:
                with self.assertRaises(ValueError):
                    w.build_request('原文', 'seedance-2.0', duration, None, [], {'fp': 'fixture'})
                payload.assert_not_called()

    def test_fast_thirty_payload_keeps_fast_model_and_explicit_duration(self):
        body = self.worker().build_request('完整原文', 'seedance-2.0', 30, '9:16', [], {'fp': 'fixture'})
        params = json.loads(body['chat_ability']['ability_param'])
        self.assertEqual(params, {'model': 'seedance_v2.0', 'duration': 30, 'ratio': '9:16'})
        from test_video_prompt import PREFIX, REFERENCE_DECLARATION
        text = body['messages'][0]['content_block'][0]['content']['text_block']['text']
        self.assertEqual(text, PREFIX + '\n\n' + REFERENCE_DECLARATION + '\n\n用户提示词：\n\n完整原文')

    def test_sse_ack_is_persisted_even_when_reading_later_fails(self):
        w = self.worker()
        import requests
        class Response:
            status_code = 200
            def iter_lines(self, **kwargs):
                yield b'event: SSE_ACK'
                yield b'data: {"ack_client_meta":{"conversation_id":"123456"}}'
                yield b''
                raise requests.ConnectionError('private')
            def close(self): pass
        class Session:
            def send(self, *args, **kwargs): return Response()
        seen = []
        result = w.send_once(Session(), None, on_ack=seen.append)
        self.assertEqual(result['conversation_id'], '123456')
        self.assertEqual(seen, ['123456'])

    def test_split_confirmation_ends_without_sending_another_request(self):
        w = self.worker()
        class Response:
            status_code = 200
            def iter_lines(self, **kwargs):
                lines = ['event: SSE_ACK', 'data: {"ack_client_meta":{"conversation_id":"98765"}}', '',
                         'event: SSE_REPLY_END', 'data: {"msg_finish_attr":{"brief":"视频生成目前支持 4 到 15 秒。"},"end_type":3}', '']
                yield from (line.encode('utf-8') for line in lines)
            def close(self): pass
        posts = []
        class Session:
            def send(self, *args, **kwargs): posts.append(1); return Response()
        result = w.send_once(Session(), None)
        self.assertEqual(result['conversation_id'], '98765')
        self.assertEqual(result['reply'], '视频生成目前支持 4 到 15 秒。')
        self.assertEqual(len(posts), 1)


if __name__ == '__main__':
    unittest.main()
