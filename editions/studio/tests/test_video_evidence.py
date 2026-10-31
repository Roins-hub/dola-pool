import json
import tempfile
import unittest
from pathlib import Path

from store import TaskStore
from video_evidence import audit_submission, observe_submission, safe_text


class EvidenceTests(unittest.TestCase):
    @staticmethod
    def request_body(text, **params):
        return json.dumps({
            'chat_ability': {'ability_param': {'model': 'seedance_v2.5', 'duration': 30,
                                              'ratio': '16:9', **params}},
            'messages': [{'content_block': [{'content': {'text_block': {'text': text}}}]}],
        })

    def test_seedance_one_aliases_audit_as_ic_mini_without_treating_fast_as_the_same_model(self):
        for alias in ('seedance-1.0', 'seedance_v1.0', 'seedance_10', 'ic_mini'):
            for duration in (5, 10, 15):
                evidence = audit_submission(self.request_body('原文', model='ic_mini', duration=duration),
                                            alias, duration, '16:9', '原文')
                self.assertEqual(evidence['model'], 'ic_mini')
                self.assertTrue(evidence['matches'], alias)
        for actual_model in ('seedance_v2.0', 'seedance_v2.0_std', 'mini', 'fast'):
            evidence = audit_submission(self.request_body('原文', model=actual_model, duration=10),
                                        'seedance-1.0', 10, '16:9', '原文')
            self.assertFalse(evidence['model_matches'], actual_model)
            self.assertFalse(evidence['matches'])
            self.assertEqual(evidence['model'], actual_model)

    def test_real_trailing_ascii_space_is_format_equivalent_without_rewriting_prompt(self):
        from video_prompt import build_video_prompt
        prompt = build_video_prompt('美女跳舞，不要任何的背景音乐 ', 30, 'seedance-2.5')
        original = prompt
        evidence = audit_submission(self.request_body('Generiertes Video: ' + prompt.rstrip(' ') + ', 16:9'),
                                    'seedance_v2.5', 30, '16:9', prompt)
        self.assertTrue(evidence['prompt_matches'], 'Only the observed trailing ASCII space was removed')
        self.assertTrue(evidence['matches'])
        self.assertEqual(evidence.get('audit_status'), 'format_equivalent')
        self.assertFalse(evidence.get('prompt_exact'))
        self.assertTrue(evidence.get('model_matches'))
        self.assertTrue(evidence.get('duration_matches'))
        self.assertTrue(evidence.get('ratio_matches'))
        self.assertEqual(prompt, original)
        self.assertTrue(prompt.endswith('背景音乐 '), 'The actual submitted prompt retains the user space')

    def test_fast_fifteen_english_native_label_is_format_equivalent_only_for_complete_prompt(self):
        from video_prompt import build_video_prompt
        prompt=build_video_prompt('一只橘猫在窗台缓缓转头，固定镜头。',15,'seedance-2.0')
        for tail,expected in [(prompt,True),(prompt+', 16:9',True),
                              (prompt.replace('橘猫','黑猫'),False),(prompt.replace('\n\n',' '),False),
                              (prompt+' extra',False)]:
            evidence=audit_submission(self.request_body('Generated video: '+tail,
                model='seedance_v2.0',duration=15,ratio=None),'seedance_v2.0',15,None,prompt)
            self.assertEqual(evidence['prompt_matches'],expected)
            self.assertEqual(evidence['matches'],expected)
            if expected:self.assertEqual(evidence['audit_status'],'format_equivalent')

    def test_exact_request_and_known_native_formats_have_distinct_audit_status(self):
        prompt = '保留正文\n30 秒'
        evidence = audit_submission(self.request_body(prompt), 'seedance_v2.5', 30, '16:9', prompt)
        self.assertEqual(evidence.get('audit_status'), 'passed')
        self.assertTrue(evidence.get('prompt_exact'))
        for ratio in ('21:9', '16:9', '4:3', '1:1', '3:4', '9:16'):
            with self.subTest(ratio=ratio):
                evidence = audit_submission(self.request_body('Generiertes Video: ' + prompt + ', ' + ratio,
                                                              ratio=ratio), 'seedance_v2.5', 30, ratio, prompt)
                self.assertEqual(evidence.get('audit_status'), 'format_equivalent')
                self.assertTrue(evidence['matches'])
                self.assertFalse(evidence.get('prompt_exact'))

    def test_format_equivalence_does_not_hide_changed_body_whitespace_or_content(self):
        prompt = '保留  两个空格\n30 秒，原样。 '
        changed = ('保留 两个空格\n30 秒，原样。', '保留  两个空格 30 秒，原样。',
                   '保留  两个空格\n15 秒，原样。', '保留  两个空格\n30 秒,原样。',
                   ' 保留  两个空格\n30 秒，原样。', '保留  两个空格\n30 秒，原样。\n',
                   '保留  两个空格\n30 秒，原样。\t', '保留  两个空格\n30 秒，原样。\u00a0')
        for text in changed:
            with self.subTest(text=text):
                evidence = audit_submission(self.request_body('Generiertes Video: ' + text + ', 16:9'),
                                            'seedance_v2.5', 30, '16:9', prompt)
                self.assertEqual(evidence.get('audit_status'), 'mismatch')
                self.assertFalse(evidence['prompt_matches'])
                self.assertFalse(evidence['matches'])

    def test_each_actual_parameter_mismatch_is_reported_independently(self):
        for field, changed in (('model', 'seedance_v2.0'), ('duration', 15), ('ratio', '9:16')):
            with self.subTest(field=field):
                evidence = audit_submission(self.request_body('原文', **{field: changed}),
                                            'seedance_v2.5', 30, '16:9', '原文')
                self.assertEqual(evidence.get('audit_status'), 'mismatch')
                self.assertFalse(evidence.get(field + '_matches'))
                self.assertTrue(evidence['prompt_matches'])
                self.assertFalse(evidence['matches'])

    def test_numeric_duration_and_absent_unrequested_ratio_do_not_invent_a_mismatch(self):
        for duration in (30, 30.0, '30'):
            evidence = audit_submission(self.request_body('原文', duration=duration, ratio=None),
                                        'seedance_v2.5', 30, None, '原文')
            self.assertEqual(evidence.get('audit_status'), 'passed')
            self.assertTrue(evidence['matches'])

    def test_missing_required_fields_preserve_other_confirmed_request_fields(self):
        for field in ('model', 'duration', 'ratio'):
            body = json.loads(self.request_body('原文'))
            del body['chat_ability']['ability_param'][field]
            evidence = audit_submission(json.dumps(body), 'seedance_v2.5', 30, '16:9', '原文')
            self.assertEqual(evidence.get('audit_status'), 'unknown')
            self.assertIsNone(evidence.get(field + '_matches'))
            self.assertTrue(evidence['prompt_matches'])
            self.assertFalse(evidence['matches'])

    def test_missing_or_malformed_request_evidence_is_unknown_not_prompt_modification(self):
        cases = ('not JSON', '{}', 'null', '[]',
                 json.dumps({'chat_ability': {'ability_param': 'not JSON'}}),
                 json.dumps({'chat_ability': {'ability_param': []}, 'messages': [{'text': '原文'}]}),
                 json.dumps({'chat_ability': {'ability_param': {'model': 'seedance_v2.5', 'duration': 30,
                                                               'ratio': '16:9'}},
                             'messages': [{'content_block': [{'content': {'new_text_schema': '原文'}}]}]}))
        for raw in cases:
            with self.subTest(raw=raw):
                evidence = audit_submission(raw, 'seedance_v2.5', 30, '16:9', '原文')
                self.assertEqual(evidence.get('audit_status'), 'unknown')
                self.assertIsNone(evidence.get('prompt_exact'))
                self.assertFalse(evidence['matches'])

    def test_unknown_text_schema_still_preserves_a_known_parameter_mismatch(self):
        raw = json.dumps({'chat_ability': {'ability_param': {'model': 'seedance_v2.5', 'duration': 15,
                                                             'ratio': '16:9'}},
                          'messages': [{'new_text_schema': '原文'}]})
        evidence = audit_submission(raw, 'seedance_v2.5', 30, '16:9', '原文')
        self.assertEqual(evidence.get('audit_status'), 'mismatch')
        self.assertFalse(evidence.get('duration_matches'))
        self.assertIsNone(evidence.get('prompt_exact'))

    def test_unobserved_or_malformed_parameter_values_do_not_escape_as_public_objects(self):
        for params in ({'duration': {'private': 'secret'}}, {'model': []}, {'ratio': ['16:9']},
                       {'duration': True}, {'duration': float('nan')}):
            with self.subTest(params=params):
                evidence = audit_submission(self.request_body('原文', **params),
                                            'seedance_v2.5', 30, '16:9', '原文')
                self.assertEqual(evidence.get('audit_status'), 'unknown')
                self.assertFalse(evidence['matches'])
                self.assertNotIn('secret', str(evidence))

    def test_missing_capture_can_be_recorded_as_unknown_without_inventing_success(self):
        import video_evidence
        helper = getattr(video_evidence, 'submission_audit', None)
        self.assertTrue(callable(helper), 'Submission audit must represent a missing request capture')
        evidence = helper([])
        self.assertEqual(evidence['audit_status'], 'unknown')
        self.assertFalse(evidence['matches'])
        self.assertIsNone(evidence['prompt_exact'])
        self.assertEqual(helper([{'audit_status': 'passed', 'matches': True}])['audit_status'], 'passed')

    def test_new_audit_scalar_fields_survive_public_evidence_storage_without_raw_request(self):
        details = {'audit_status': 'format_equivalent', 'model_matches': True, 'duration_matches': True,
                   'ratio_matches': True, 'prompt_matches': True, 'prompt_exact': False, 'matches': True,
                   'reason': '平台格式差异', 'raw': {'Cookie': 'private-cookie'}, 'prompt': 'private-prompt'}
        store = TaskStore(':memory:')
        try:
            store.create('fixture', 'seedance-2.5', '原文', '16:9', 30)
            store.record_evidence('fixture', 'submitted', details)
            stored = store.task_events('fixture')[0]['details']
            self.assertEqual(stored.get('audit_status'), 'format_equivalent')
            for field in ('model_matches', 'duration_matches', 'ratio_matches', 'prompt_matches', 'prompt_exact'):
                self.assertEqual(stored.get(field), details[field])
            self.assertNotIn('private-cookie', str(stored))
            self.assertNotIn('private-prompt', str(stored))
        finally:
            store._conn.close()

    def test_native_ratio_suffix_passes_request_audit_without_changing_prompt(self):
        from types import SimpleNamespace
        from video_prompt import build_video_prompt
        prompt = build_video_prompt('一个韩国美女跳热门舞蹈', 30, 'seedance-2.5')
        callbacks = []
        page = SimpleNamespace(on=lambda event, callback: callbacks.append(callback))
        observed = observe_submission(page, 'seedance_v2.5', 30, '16:9', prompt)
        body = {'chat_ability': {'ability_param': {'model': 'seedance_v2.5', 'duration': 30, 'ratio': '16:9'}},
                'messages': [{'content_block': [{'content': {'text_block': {
                    'text': 'Generiertes Video: ' + prompt + ', 16:9'}}}]}]}
        callbacks[0](SimpleNamespace(method='POST', url='https://www.dola.com/chat/completion',
                                    post_data=json.dumps(body)))
        self.assertTrue(observed[0]['matches'], 'Native aspect ratio suffix must not reject an unchanged prompt')
        for text in ('Generiertes Video: ' + prompt + ', 9:16',
                     'Generiertes Video: ' + prompt + ', 16:9 改动',
                     'Generiertes Video: ' + prompt.replace('韩国', '日本') + ', 16:9'):
            body['messages'][0]['content_block'][0]['content']['text_block']['text'] = text
            self.assertFalse(audit_submission(json.dumps(body), 'seedance_v2.5', 30, '16:9', prompt)['matches'])

    def test_native_video_label_keeps_exact_user_prompt_in_actual_request_observer(self):
        # This label precedes the unchanged user text in the inspected Dola conversation.
        from video_prompt import build_video_prompt
        from types import SimpleNamespace
        prompt = build_video_prompt('韩国美女跳舞三十秒视频', 30, 'seedance-2.5')
        page = SimpleNamespace(on=lambda event, callback: callbacks.append(callback))
        callbacks, events = [], []
        observed = observe_submission(page, 'seedance_v2.5', 30, None, prompt,
                                      lambda phase, details: events.append((phase, details)))
        body = {'chat_ability': {'ability_param': json.dumps({'model': 'seedance_v2.5', 'duration': 30})},
                'messages': [{'content_block': [{'content': {'text_block': {'text': 'Generiertes Video: ' + prompt}}}]}]}
        callbacks[0](SimpleNamespace(method='POST', url='https://www.dola.com/chat/completion', post_data=json.dumps(body)))
        self.assertTrue(observed[0]['prompt_matches'], 'Dola label must not count as rewriting the user prompt')
        self.assertTrue(observed[0]['matches'])
        self.assertEqual(events[0][0], 'submitted')
        self.assertNotIn(prompt, str(observed))

    def test_native_label_does_not_allow_changed_text_or_wrong_30_second_parameters(self):
        prompt = '保持用户原文\n第二行'
        body = {'chat_ability': {'ability_param': {'model': 'seedance_v2.5', 'duration': 30, 'ratio': '9:16'}},
                'messages': [{'content_block': [{'content': {'text_block': {'text': 'Generiertes Video: ' + prompt}}}]}]}
        for text in ('任意额外说明：' + prompt, 'Generiertes Video: ' + prompt + '改动', 'Generiertes Video: 保持用户原文 第二行'):
            with self.subTest(text=text):
                body['messages'][0]['content_block'][0]['content']['text_block']['text'] = text
                self.assertFalse(audit_submission(json.dumps(body), 'seedance_v2.5', 30, '9:16', prompt)['matches'])
        body['messages'][0]['content_block'][0]['content']['text_block']['text'] = 'Generiertes Video: ' + prompt
        for key, value in (('duration', 15), ('model', 'seedance_v2.0'), ('ratio', '16:9')):
            with self.subTest(key=key):
                original = body['chat_ability']['ability_param'][key]
                body['chat_ability']['ability_param'][key] = value
                self.assertFalse(audit_submission(json.dumps(body), 'seedance_v2.5', 30, '9:16', prompt)['matches'])
                body['chat_ability']['ability_param'][key] = original

    def test_exact_outgoing_parameters_and_prompt_are_checked_without_secrets(self):
        prompt = '原文\n第二行，不要改。'
        body = {'chat_ability': {'ability_param': json.dumps(
            {'model': 'seedance_v2.5', 'duration': 30, 'ratio': '9:16'})},
            'messages': [{'content_block': [{'content': {'text_block': {'text': prompt}}}]}],
            'token': 'private-token'}
        evidence = audit_submission(json.dumps(body), 'seedance_v2.5', 30, '9:16', prompt)
        self.assertTrue(evidence['matches'])
        self.assertNotIn('private-token', str(evidence))
        self.assertNotIn(prompt, str(evidence))
        body['chat_ability']['ability_param'] = json.dumps(
            {'model': 'seedance_v2.5', 'duration': 15, 'ratio': '9:16'})
        self.assertFalse(audit_submission(json.dumps(body), 'seedance_v2.5', 30, '9:16', prompt)['matches'])
        self.assertFalse(audit_submission('{}', 'seedance_v2.5', 30, '9:16', prompt)['matches'])

    def test_task_evidence_survives_restart_and_deduplicates_poll_replies(self):
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / 'tasks.db'
            store = TaskStore(str(db))
            store.create('fixture', 'seedance-2.5', '原文', '9:16', 30)
            store.record_evidence('fixture', 'awaiting_receipt', {'reply': '我可以生成15秒'})
            self.assertIsNone(store.get('fixture')['accepted_at'])
            store.record_evidence('fixture', 'accepted', {'reply': 'Seedance 2.5 消耗2个额度'})
            store.record_evidence('fixture', 'accepted', {'reply': 'Seedance 2.5 消耗2个额度'})
            self.assertEqual(len(store.task_events('fixture')), 2)
            store._conn.close()
            store = TaskStore(str(db))
            self.assertIsNotNone(store.get('fixture')['accepted_at'])
            self.assertEqual(store.get('fixture')['phase'], 'accepted')
            store.record_evidence('fixture', 'polling', {'reply': 'https://cdn.test/v.mp4?token=secret',
                'Cookie': 'sessionid=private', 'proxy': 'http://u:password@isp:80'})
            events = str(store.task_events('fixture'))
            self.assertNotIn('secret', events)
            self.assertNotIn('password', events)
            self.assertNotIn('private', events)
            store._conn.close()

    def test_secrets_are_redacted_from_upstream_errors(self):
        text = safe_text('Cookie: sessionid=abc; uid_tt=def\nfailed https://u:pwd@isp.test:80/a?token=xyz')
        for secret in ('abc', 'def', 'pwd', 'xyz'):
            self.assertNotIn(secret, text)

    def test_socks_proxy_credentials_are_redacted_from_errors(self):
        text = safe_text('connection failed socks5://proxy-user:private-pass@isp.test:1080')
        self.assertNotIn('proxy-user', text)
        self.assertNotIn('private-pass', text)

    def test_watchdog_failure_and_restart_without_conversation_cannot_show_waiting_receipt(self):
        store = TaskStore(':memory:')
        try:
            store.create('fixture', 'seedance-2.5', '原文', '9:16', 30)
            store.record_evidence('fixture', 'awaiting_receipt', {})
            store.update('fixture', status='failed', error='等待超时')
            self.assertEqual(store.get('fixture')['phase'], 'failed')
            store.update('fixture', status='processing', account='fixture')
            store.record_evidence('fixture', 'sending', {})
            store.fail_unconfirmed_submissions()
            self.assertEqual(store.get('fixture')['phase'], 'account_retry')
            self.assertEqual([r['id'] for r in store.recoverable_queued_tasks()], ['fixture'])
        finally:
            store._conn.close()
