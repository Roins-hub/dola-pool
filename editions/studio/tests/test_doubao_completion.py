import json
import unittest

from providers.doubao.completion import parse_completion_response


class CompletionResponseTests(unittest.TestCase):
    def test_immediate_top_level_rejection_retains_code_and_message(self):
        result = parse_completion_response('{"code":710001,"msg":"该请求暂时无法处理"}')
        self.assertEqual(result['payloads'], [{'code': 710001, 'msg': '该请求暂时无法处理'}])
        self.assertEqual(result['error']['code'], '710001')
        self.assertEqual(result['error']['message'], '该请求暂时无法处理')
        self.assertIs(result['error']['no_charge'], False)

    def test_sse_preserves_order_and_joins_valid_multiline_data_while_ignoring_partial_events(self):
        raw = ('event: message\r\ndata: {"code":0,\r\ndata: "data":{"text":"first"}}\r\n\r\n'
               'data: {"data":{"text":"second"}}\n\n'
               'data: {"code":"710002","message":"later error"}\n\n'
               'data: [DONE]\n\ndata: {"code":')
        result = parse_completion_response(raw)
        self.assertEqual(result['payloads'], [{'code': 0, 'data': {'text': 'first'}},
            {'data': {'text': 'second'}}, {'code': '710002', 'message': 'later error'}])
        self.assertEqual(result['error'], {'code': '710002', 'message': 'later error',
                                           'no_charge': False})

    def test_success_or_nested_business_content_never_becomes_transport_rejection(self):
        for payload in ({'code': 0, 'msg': 'ok'}, {'code': '0'},
                        {'code': True, 'msg': 'boolean is not an error code'},
                        {'data': {'content': {'code': 710003, 'msg': 'quoted example'}}}):
            with self.subTest(payload=payload):
                self.assertIsNone(parse_completion_response(json.dumps(payload))['error'])

    def test_non_json_and_invalid_or_oversized_streams_produce_no_payload_or_fabricated_error(self):
        for raw in ('<html>temporary unavailable</html>', 'data: not json\n\n',
                    'data: [DONE]\n\n', '{"code":', 'x' * (2*1024*1024 + 1)):
            with self.subTest(length=len(raw)):
                self.assertEqual(parse_completion_response(raw), {'payloads': [], 'error': None})

    def test_error_messages_redact_credentials_and_are_bounded_without_mutating_internal_payload(self):
        message = ('Bearer secret-access-token; token=secret-token; sessionid=secret-session; '
                   'Cookie: sessionid=secret-cookie; email=private@example.com; ' + '长' * 800)
        raw = json.dumps({'error_code': '710004', 'message': message})
        result = parse_completion_response(raw)
        self.assertEqual(result['payloads'][0]['message'], message)
        for secret in ('secret-access-token', 'secret-token', 'secret-session', 'secret-cookie',
                       'private@example.com'):
            self.assertNotIn(secret, result['error']['message'])
        self.assertLessEqual(len(result['error']['message']), 500)
        self.assertEqual(result['error']['code'], '710004')

    def test_first_error_is_preserved_for_caller_to_apply_receipt_chronology(self):
        result = parse_completion_response('data: {"errorcode":710005,"msg":"first"}\n\n'
            'data: {"code":710006,"msg":"second"}\n\n')
        self.assertEqual(result['error']['code'], '710005')
        self.assertEqual(len(result['payloads']), 2)

    def test_long_string_code_cannot_overflow_integer_conversion_or_error_output(self):
        result = parse_completion_response(json.dumps({'code': '7' * 5000, 'msg': 'bounded'}))
        self.assertIsNotNone(result['error'])
        self.assertLessEqual(len(result['error']['code']), 100)
        self.assertIsNone(parse_completion_response(json.dumps({'code': '0' * 5000}))['error'])

    def test_captured_stream_error_reports_the_native_error_msg_without_guessing_billing(self):
        raw = ('id:0\nevent:STREAM_ERROR\n'
               'data:{"error_code":710022002,"error_msg":"We are experiencing high demand '
               'right now. Please try again later.","extra":{"ack":"1"}}\n\n'
               'id:1\nevent:SSE_REPLY_END\ndata:{"end_type":3}\n\n')
        result = parse_completion_response(raw)
        self.assertEqual(result['error'], {'code': '710022002',
            'message': 'We are experiencing high demand right now. Please try again later.',
            'no_charge': False})
        self.assertEqual(result['payloads'][1], {'end_type': 3})

    def test_utf8_chinese_completion_errors_survive_latin1_and_browser_windows1252_mojibake(self):
        expected = '当前服务访问频繁，请稍后重试。'
        wire = expected.encode('utf-8')
        windows = ''
        for byte in wire:
            try:
                windows += bytes([byte]).decode('cp1252')
            except UnicodeDecodeError:
                windows += chr(byte)
        # The captured diagnostic kept its final Chinese punctuation intact.
        for text in (expected, wire.decode('latin1'), windows, windows[:-3] + '。'):
            for streaming in (False, True):
                with self.subTest(text=text, streaming=streaming):
                    payload = {'error_code': 710022002, 'error_msg': text}
                    raw = json.dumps(payload, ensure_ascii=False)
                    if streaming:
                        raw = 'id:0\nevent:STREAM_ERROR\ndata:' + raw + '\n\n'
                    result = parse_completion_response(raw)
                    self.assertEqual(result['error'], {'code': '710022002',
                        'message': expected, 'no_charge': False})
                    self.assertEqual(result['payloads'], [payload])

    def test_mojibake_nel_does_not_split_an_sse_json_data_line(self):
        expected = '请求入口繁忙，请稍后重试。'
        broken = expected.encode('utf-8').decode('latin1')
        self.assertIn('\x85', broken)
        raw = ('event:STREAM_ERROR\r\ndata:' + json.dumps(
            {'error_code': 710022002, 'error_msg': broken}, ensure_ascii=False) + '\r\n\r\n')
        result = parse_completion_response(raw)
        self.assertEqual(result['error']['message'], expected)
        self.assertEqual(len(result['payloads']), 1)

    def test_correct_non_ascii_error_messages_are_not_reinterpreted(self):
        for expected in ('Réessayez après Noël.', 'Bitte später prüfen.', '当前服务繁忙。',
                         'English message with £ and € currency.'):
            with self.subTest(expected=expected):
                result = parse_completion_response(json.dumps(
                    {'error_code': 710022002, 'error_msg': expected}, ensure_ascii=False))
                self.assertEqual(result['error']['message'], expected)


if __name__ == '__main__':
    unittest.main()
