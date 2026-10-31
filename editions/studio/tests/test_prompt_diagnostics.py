"""Safe mismatch diagnostics through the real production audit/public boundary."""
import hashlib
import json
import unittest
from unittest.mock import patch

import video_evidence
from video_evidence import audit_submission, public_details


def digest(text):
    return hashlib.sha256(text.encode('utf-8', errors='surrogatepass')).hexdigest()


def request_body(texts, **params):
    return json.dumps({
        'chat_ability': {'ability_param': {'model': 'seedance_v2.0', 'duration': 30,
                                          'ratio': '16:9', **params}},
        'messages': [{'content_block': [{'content': {'text_block': {'text': text}}}
                                        for text in texts]}],
    }, ensure_ascii=False)


class PromptDiagnosticsTests(unittest.TestCase):
    def audit(self, observed, expected, **params):
        return audit_submission(request_body([observed], **params),
                                'seedance_v2.0', 30, '16:9', expected)

    def assert_mismatch(self, evidence):
        self.assertFalse(evidence['matches'])
        self.assertFalse(evidence['prompt_matches'])
        self.assertEqual(evidence['audit_status'], 'mismatch')

    def test_changed_text_reports_only_safe_lengths_digests_and_zero_based_offset(self):
        expected, observed = '第一行\n\n第二行  原样。', '第一行\n\n第二行  改写。'
        evidence = self.audit(observed, expected)
        self.assert_mismatch(evidence)
        self.assertEqual(evidence.get('prompt_expected_chars'), len(expected))
        self.assertEqual(evidence.get('prompt_observed_chars'), len(observed))
        self.assertEqual(evidence.get('prompt_expected_sha256'), digest(expected))
        self.assertEqual(evidence.get('prompt_observed_sha256'), digest(observed))
        self.assertEqual(evidence.get('prompt_first_diff_index'), expected.index('原'))
        self.assertIs(evidence.get('prompt_line_endings_equal'), False)
        self.assertIs(evidence.get('prompt_render_equal'), False)

    def test_line_endings_are_diagnosed_but_still_mismatch(self):
        expected = '第一行\n\n第二行。'
        for ending in ('\r\n', '\r'):
            with self.subTest(ending=repr(ending)):
                observed = expected.replace('\n', ending)
                evidence = self.audit(observed, expected)
                self.assert_mismatch(evidence)
                self.assertIs(evidence.get('prompt_line_endings_equal'), True)
                self.assertIs(evidence.get('prompt_render_equal'), True)
                self.assertEqual(evidence.get('prompt_first_diff_index'), 3)

    def test_blank_lines_and_nbsp_are_diagnostic_equivalence_only(self):
        expected = '第一行\n\n第二行  原样。'
        for observed in (expected.replace('\n\n', '\n'), expected.replace('  ', '\xa0 ')):
            with self.subTest(digest=digest(observed)):
                evidence = self.audit(observed, expected)
                self.assert_mismatch(evidence)
                self.assertIs(evidence.get('prompt_line_endings_equal'), False)
                self.assertIs(evidence.get('prompt_render_equal'), True)

    def test_known_native_wrapper_has_separate_raw_and_compared_digests(self):
        expected = '第一行\n\n第二行。'
        compared = expected.replace('\n', '\r\n')
        observed = 'Generated video: ' + compared + ', 16:9'
        evidence = self.audit(observed, expected)
        self.assert_mismatch(evidence)
        self.assertEqual(evidence.get('prompt_diagnostic_scope'), 'known_native_unwrapped')
        self.assertEqual(evidence.get('prompt_observed_chars'), len(observed))
        self.assertEqual(evidence.get('prompt_observed_sha256'), digest(observed))
        self.assertEqual(evidence.get('prompt_compared_chars'), len(compared))
        self.assertEqual(evidence.get('prompt_compared_sha256'), digest(compared))
        self.assertIs(evidence.get('prompt_line_endings_equal'), True)

    def test_wrong_ratio_or_unknown_wrapper_does_not_become_equivalent(self):
        expected = '第一行\n第二行。'
        for observed in ('Generated video: ' + expected + ', 9:16', '额外任意包装：' + expected):
            evidence = self.audit(observed, expected)
            self.assert_mismatch(evidence)
            self.assertIs(evidence.get('prompt_render_equal'), False)

    def test_omitted_tail_and_substituted_words_stay_rejected(self):
        expected = '第一句。\n必须抬手。'
        for observed in ('第一句。', expected.replace('抬手', '转身')):
            evidence = self.audit(observed, expected)
            self.assert_mismatch(evidence)
            self.assertIs(evidence.get('prompt_render_equal'), False)
        evidence = self.audit('第一句。', expected)
        self.assertEqual(evidence.get('prompt_first_diff_index'), len('第一句。'))

    def test_existing_acceptance_rules_and_statuses_are_unchanged(self):
        expected = '原文\n第二行。 '
        for observed, status, exact in ((expected, 'passed', True),
                                        ('Generated video: ' + expected + ', 16:9', 'format_equivalent', False),
                                        ('Generiertes Video: ' + expected.rstrip(' ') + ', 16:9',
                                         'format_equivalent', False)):
            evidence = self.audit(observed, expected)
            self.assertTrue(evidence['matches'])
            self.assertTrue(evidence['prompt_matches'])
            self.assertEqual(evidence['prompt_exact'], exact)
            self.assertEqual(evidence['audit_status'], status)
            self.assertNotIn('prompt_expected_sha256', evidence)

    def test_model_duration_ratio_mismatches_remain_failures_even_when_text_is_exact(self):
        for params in ({'model': 'seedance_v2.5'}, {'duration': 15}, {'ratio': '9:16'}):
            evidence = self.audit('完整原文。', '完整原文。', **params)
            self.assertFalse(evidence['matches'])
            self.assertTrue(evidence['prompt_exact'])
            self.assertTrue(evidence['prompt_matches'])
            self.assertEqual(evidence['audit_status'], 'mismatch')
            self.assertIs(evidence.get('prompt_render_equal'), True)
            self.assertIsNone(evidence.get('prompt_first_diff_index'))

    def test_multiple_blocks_are_not_concatenated_or_arbitrarily_selected(self):
        evidence = audit_submission(request_body(['前半', '后半']),
                                    'seedance_v2.0', 30, '16:9', '前半后半')
        self.assert_mismatch(evidence)
        self.assertEqual(evidence.get('prompt_text_block_count'), 2)
        self.assertEqual(evidence.get('prompt_diagnostic_scope'), 'multiple_text_blocks')
        for key in ('prompt_observed_sha256', 'prompt_observed_chars', 'prompt_first_diff_index',
                    'prompt_render_equal'):
            self.assertNotIn(key, evidence)

    def test_missing_text_schema_stays_unknown_without_invented_comparison(self):
        evidence = audit_submission(request_body([]), 'seedance_v2.0', 30, '16:9', '完整原文。')
        self.assertFalse(evidence['matches'])
        self.assertIsNone(evidence['prompt_matches'])
        self.assertEqual(evidence['audit_status'], 'unknown')
        self.assertEqual(evidence.get('prompt_text_block_count'), 0)
        self.assertEqual(evidence.get('prompt_diagnostic_scope'), 'missing_text_blocks')
        self.assertNotIn('prompt_render_equal', evidence)

    def test_diagnostics_reuse_the_already_parsed_body_and_collected_texts(self):
        raw = request_body(['实际文本。'])
        with patch.object(video_evidence.json, 'loads', wraps=json.loads) as loads:
            evidence = audit_submission(raw, 'seedance_v2.0', 30, '16:9', '期待文本。')
        self.assertEqual(loads.call_count, 1)
        self.assertEqual(evidence.get('prompt_text_block_count'), 1)

    def test_public_details_preserves_whitelisted_diagnostic_scalars(self):
        evidence = self.audit('第一行\r\n第二行。', '第一行\n第二行。')
        public = public_details(evidence)
        for key in ('prompt_expected_chars', 'prompt_observed_chars', 'prompt_expected_sha256',
                    'prompt_observed_sha256', 'prompt_compared_chars', 'prompt_compared_sha256',
                    'prompt_first_diff_index', 'prompt_line_endings_equal', 'prompt_render_equal',
                    'prompt_text_block_count', 'prompt_diagnostic_scope'):
            self.assertIsNotNone(public.get(key), key)
            self.assertEqual(public[key], evidence[key])

    def test_public_details_rejects_invalid_diagnostic_types_values_and_raw_data(self):
        invalid = {
            'prompt_expected_chars': True, 'prompt_observed_chars': {'private': 'SYNTHETIC_SECRET'},
            'prompt_compared_chars': -1, 'prompt_first_diff_index': 1.5,
            'prompt_text_block_count': 2 ** 31, 'prompt_expected_sha256': 'SYNTHETIC_SECRET',
            'prompt_observed_sha256': ['SYNTHETIC_SECRET'], 'prompt_compared_sha256': 'A' * 64,
            'prompt_line_endings_equal': 1, 'prompt_render_equal': 'true',
            'prompt_diagnostic_scope': ['SYNTHETIC_SECRET'],
            'raw': 'SYNTHETIC_SECRET', 'headers': {'Cookie': 'SYNTHETIC_SECRET'},
            'prompt': 'SYNTHETIC_SECRET', 'prompt_fragment': 'SYNTHETIC_SECRET',
        }
        public = public_details(invalid)
        self.assertEqual(public, {})
        self.assertNotIn('SYNTHETIC_SECRET', json.dumps(public))

    def test_no_source_text_fragment_or_headers_escape_the_audit_public_boundary(self):
        expected = 'SYNTHETIC_PRIVATE_PROMPT_0123456789\n\n必须完成动作。'
        body = json.loads(request_body([expected[:-1] + '改']))
        body['headers'] = {'Cookie': 'SYNTHETIC_HEADER_SECRET'}
        evidence = audit_submission(json.dumps(body), 'seedance_v2.0', 30, '16:9', expected)
        public = public_details(evidence)
        self.assertEqual(public.get('prompt_expected_sha256'), digest(expected))
        serialized = json.dumps((evidence, public), ensure_ascii=False)
        for private in ('SYNTHETIC_PRIVATE_PROMPT', 'SYNTHETIC_HEADER_SECRET', '必须完成动作'):
            self.assertNotIn(private, serialized)


if __name__ == '__main__':
    unittest.main()
