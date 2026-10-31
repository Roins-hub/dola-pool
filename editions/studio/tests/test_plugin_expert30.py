"""Expert30 must collect all original cards without treating a prompt as a receipt."""
import base64
import json
import tempfile
import unittest
from pathlib import Path

from video_polling import parse_poll_response


def card(identity, *, fallback=True):
    return {'block_type': 2074, 'content': {'creation_block': {'creations': [
        {'id': identity, 'type': 2, 'video': {
            'video_id': 'v' + identity * 24,
            'download_url': f'https://v16-dola.dola.com/{identity}.mp4?lr=video_gen_watermark_dyn',
            'video_model': json.dumps({'video_list': {}}),
            **({'fallback_api': f'https://vod-urls-mya.byteintlapi.com/video?video_id=v{identity * 24}'} if fallback else {})}}
    ]}}}


def response(blocks, *, user_type=2, cid='123456789'):
    return {'downlink_body': {'pull_singe_chain_downlink_body': {
        'conversation_id': cid, 'messages': [{'conversation_id': cid, 'user_type': user_type,
            'message_id': 'assistant-1', 'content': json.dumps(blocks)}]}}}


class ExpertCardTests(unittest.TestCase):
    def test_two_cards_keep_their_own_fallback_and_identity(self):
        result = parse_poll_response(response([card('a'), card('b')]), '123456789').get('plugin_expert30', {})
        self.assertEqual([c['creation_id'] for c in result.get('clips', [])], ['a', 'b'])
        self.assertIn('video_id=v' + 'b'*24, result['clips'][1]['fallback_apis'][0])

    def test_later_video_id_and_refreshed_signature_do_not_rename_the_same_creation(self):
        before = card('a')
        before['content']['creation_block']['creations'][0]['video'].pop('video_id')
        a = parse_poll_response(response([before]),'123456789')['plugin_expert30']['clips'][0]
        b = parse_poll_response(response([card('a')]),'123456789')['plugin_expert30']['clips'][0]
        self.assertEqual(a['clip_id'],b['clip_id'])

    def test_user_echo_cannot_create_clips_or_assistant_receipts(self):
        result = parse_poll_response(response([card('a'), {'content': {'text_block': {'text': '生成完成'}}}], user_type=1), '123456789')
        self.assertEqual(result['videos'], [])
        self.assertEqual(result['assistant_texts'], [])
        self.assertEqual(result.get('plugin_expert30', {}).get('clips', []), [])

    def test_json_example_inside_assistant_text_is_not_a_native_video_card(self):
        example={'block_type':10000,'content':{'text_block':{'text':json.dumps(card('a'))}}}
        parsed=parse_poll_response(response([example]),'123456789')['plugin_expert30']
        self.assertEqual(parsed['clips'],[])
        self.assertFalse(parsed['accepted'])

    def test_simple_json_text_object_is_a_reply_not_a_video(self):
        result = parse_poll_response(response({'text': 'Nur für Dola Plus verfügbar.'}), '123456789')
        self.assertEqual(result['assistant_texts'], ['Nur für Dola Plus verfügbar.'])
        self.assertEqual(result.get('plugin_expert30', {}).get('clips', []), [])

    def test_other_conversation_and_baseline_message_are_ignored(self):
        data = response([card('a')])
        data['downlink_body']['pull_singe_chain_downlink_body']['messages'][0]['conversation_id'] = '999'
        result = parse_poll_response(data, '123456789').get('plugin_expert30', {})
        self.assertEqual(result.get('clips', []), [])
        result = parse_poll_response(response([card('a')]), '123456789', baseline_messages=['assistant-1'])
        self.assertEqual(result.get('plugin_expert30', {}).get('clips', []), [])

    def test_completed_tools_do_not_turn_a_single_fifteen_second_clip_into_a_full_result(self):
        tools = {'content': {'generic_tool_block': {'tool_name': 'text_to_video', 'status': 4}}}
        result = parse_poll_response(response([tools, card('a')]), '123456789').get('plugin_expert30', {})
        self.assertTrue(result.get('accepted'))
        self.assertTrue(result.get('tools_finished'))
        self.assertEqual(len(result.get('clips', [])), 1)


class PluginResourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_original_parser_resolves_each_fallback_in_its_original_slot(self):
        try:
            from plugin_resources import run_resolver
        except ImportError:
            self.fail('Expert30 needs the fixed original-plugin resource resolver')
        original = 'https://v16-dola.dola.com/b.mp4?lr=unwatermarked&signature=unchanged'
        fallback = 'https://vod-urls-mya.byteintlapi.com/video?video_id=vbbbbbbbbbbbbbbbbbbbbbbbb'
        prepared = await run_resolver({'conversation_id': '123456789', 'fallback_apis': [fallback], 'video_models': []})
        request = prepared['fallback_requests'][0]['request_url']
        self.assertIn('logo_type=unwatermarked', request)
        decoded = await run_resolver({'conversation_id': '123456789', 'fallback_apis': [fallback],
            'video_models': [], 'fallback_responses': [{'request_url': request, 'status': 200,
            'payload': {'video_list': {'main': {'main_url': base64.b64encode(original.encode()).decode(),
                                             'vwidth': 1280, 'vheight': 720}}}}]})
        self.assertEqual(decoded['fallback_results'][0]['url'], original)

    async def test_model_container_alone_does_not_qualify_unknown_or_marked_urls(self):
        try:
            from plugin_resources import validated_resource
        except ImportError:
            self.fail('Expert30 must validate original-parser candidates before publication')
        for url in ('https://v16-dola.dola.com/v?lr=video_gen_watermark_dyn',
                    'https://v16-dola.dola.com/v', 'https://127.0.0.1/v?lr=unwatermarked'):
            self.assertIsNone(validated_resource({'url': url}, 'dola_video_model'))
        original = 'http://v16-dola.dola.com/v?lr=unwatermarked&token=untouched'
        self.assertEqual(validated_resource({'url': original}, 'dola_video_model')['url'], original)

    async def test_exact_official_vid_confirmation_can_verify_an_untouched_url_without_lr(self):
        from plugin_resources import validated_resource
        original='https://v16-dola.dola.com/official.mp4?signature=untouched'
        self.assertIsNone(validated_resource({'url':original},'dola_original_api'))
        try:
            resource=validated_resource({'url':original},'dola_original_api',official_identity_verified=True)
        except TypeError:
            self.fail('An explicit official without-watermark confirmation for this VID is valid provenance')
        self.assertEqual(resource['url'],original)
        self.assertEqual(resource['watermark'],'verified_download')
        marked=original+'&lr=video_gen_watermark_dyn'
        self.assertIsNone(validated_resource({'url':marked},'dola_original_api',official_identity_verified=True))


class MultiClipPublicationTests(unittest.TestCase):
    def setUp(self):
        from store import TaskStore
        self.store = TaskStore(':memory:')
        self.addCleanup(self.store._conn.close)
        self.store.create('multi', 'seedance-2.5', 'exact prompt', '16:9', 30, duration_profile='plugin-expert30')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def clip(self, key, seconds):
        from test_video_metadata import mp4
        path = Path(self.temp.name) / (key + '.mp4')
        path.write_bytes(mp4(seconds))
        return {'clip_id': key, 'index': 1 if key == 'a' else 2, 'vid': 'v'+key*24,
            'local_path': str(path), 'video_url': f'https://v16-dola.dola.com/{key}?lr=unwatermarked',
            'download_source': 'dola_plugin_fallback', 'watermark': 'verified_download',
            'plugin_provenance': {'clip_id': key, 'conversation_id': '123456789', 'original_response_url': True,
                                  'fallback_host': 'vod-urls-mya.byteintlapi.com'}}

    def publish(self, clips, *, finished=True):
        from task_results import complete_task
        try:
            complete_task(self.store, 'multi', {'clips': clips, 'generation_finished': finished,
                'account': 'fixture', 'conversation_id': '123456789'}, 'http://local')
        except KeyError:
            self.fail('Multi-result publication must not require one top-level local_path')
        return self.store.get('multi')

    def test_two_independent_originals_publish_without_a_fake_single_video(self):
        row = self.publish([self.clip('a', 15), self.clip('b', 15)])
        self.assertEqual(row['status'], 'completed')
        self.assertIsNone(row['video_url'])
        self.assertIsNone(row['actual_duration'])
        self.assertEqual(row['total_actual_duration'], 30)
        self.assertEqual([c['video_url'] for c in row['clips']], ['http://local/videos/a.mp4', 'http://local/videos/b.mp4'])

    def test_single_fifteen_second_result_is_partial_and_stays_downloadable(self):
        row = self.publish([self.clip('a', 15)])
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['failure_code'], 'partial_results')
        self.assertEqual(len(row['clips']), 1)
        self.assertEqual(row['clips'][0]['video_url'], 'http://local/videos/a.mp4')

    def test_single_native_thirty_second_result_can_finish(self):
        row = self.publish([self.clip('a', 30)])
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['total_actual_duration'], 30)

    def test_short_expert_result_is_complete_only_for_its_requested_duration(self):
        for duration in (5, 10, 15):
            with self.subTest(duration=duration):
                self.store.update('multi', duration=duration)
                row = self.publish([self.clip('a', duration)])
                self.assertEqual(row['status'], 'completed')
                self.assertEqual(row['total_actual_duration'], duration)

    def test_two_part_request_does_not_accept_a_single_thirty_second_clip(self):
        self.store.update('multi', generation_options='{"two_part":true}')
        row = self.publish([self.clip('a', 30)])
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['failure_code'], 'partial_results')

    def test_actual_clip_dimensions_come_from_the_file(self):
        from test_doubao_result_validation import mp4
        clip = self.clip('a', 30)
        Path(clip['local_path']).write_bytes(mp4(width=1280,height=720,seconds=30))
        row = self.publish([clip])
        self.assertEqual((row['clips'][0].get('width'),row['clips'][0].get('height')),(1280,720))

    def test_partial_progress_then_second_clip_preserves_first_provenance(self):
        self.publish([self.clip('a', 15)], finished=False)
        row = self.publish([self.clip('b', 15)])
        self.assertEqual(row['status'], 'completed')
        self.assertEqual([c['clip_id'] for c in row['clips']], ['a', 'b'])


class ContextExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def test_expert_extension_is_selected_per_context_without_changing_global_config(self):
        from unittest.mock import AsyncMock, patch
        import browser
        from test_browser_account_settings import Engine
        from plugin_resources import EXTENSION_DIR
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / 'fixture').mkdir()
            engine = Engine()
            old = browser.config.EXTENSION_DIR
            with patch.object(browser, 'Path', wraps=Path) as paths, \
                 patch.object(browser.config, 'proxy_for', return_value=None), \
                 patch.object(browser.config, 'ISP_ONLY', False), \
                 patch.object(browser.config, 'account_proxy_raw', return_value=('', 'default')), \
                 patch.object(browser, '_inject_saved_cookies', new_callable=AsyncMock), \
                 patch.object(browser, 'sync_site_language', new_callable=AsyncMock):
                paths.side_effect = lambda value: Path(root) if value == 'accounts' else Path(value)
                try:
                    await browser._launch_account_context(engine, 'fixture', use_extension=True,
                                                           extension_dir=str(EXTENSION_DIR))
                except TypeError:
                    self.fail('Expert plugin selection must be a context option, not a global config mutation')
            self.assertIn('--load-extension=' + str(EXTENSION_DIR.resolve()), engine.launches[0][1]['args'])
            self.assertEqual(browser.config.EXTENSION_DIR, old)


class ExpertSubmitAndObserveTests(unittest.IsolatedAsyncioTestCase):
    def test_worker_event_names_are_accepted_by_the_project_store(self):
        import ast
        import inspect
        import plugin_expert30
        from video_evidence import PHASES
        tree = ast.parse(inspect.getsource(plugin_expert30))
        phases = {node.args[0].value for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == 'on_event' and node.args
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)}
        self.assertEqual(phases - PHASES, set())

    async def test_explicit_plus_rejection_is_separate_from_accepted_tool_results(self):
        from plugin_expert30 import poll_outcome
        reply='Dieses Feature ist nur für Dola Plus-Abonnent*innen verfügbar. Führe ein Upgrade auf Dola Plus durch, um es zu verwenden.'
        _, outcome=poll_outcome({'plugin_expert30':{'accepted':False,'clips':[],'reply':reply}})
        self.assertEqual(getattr(outcome.get('error'),'task_phase',None),'subscription_required')
        _, accepted=poll_outcome({'plugin_expert30':{'accepted':True,'clips':[],'reply':reply}})
        self.assertIsNone(accepted['error'])

    async def test_submission_gate_rejects_changed_prompt_and_reordered_images(self):
        import plugin_expert30 as worker
        if not hasattr(worker, 'audit_request'):
            self.fail('Expert30 must audit the exact original-plugin prompt and image identities')
        expected = 'original plugin prefix\nexact user text'
        body = {'option': {'agent_mode': 1, 'model_config': {'model_item_key': '4'}}, 'messages': [
            {'content_block': [{'content': {'text_block': {'text': expected}}},
                               {'content': {'attachment_block': {'attachments': [
                                   {'image': {'uri': 'first'}}, {'image': {'uri': 'second'}}]}}}]}]}
        import hashlib
        keys = [hashlib.sha256(v.encode()).hexdigest() for v in ('first', 'second')]
        self.assertTrue(worker.audit_request(body, expected, keys)['matches'])
        self.assertFalse(worker.audit_request(body, expected + 'changed', keys)['matches'])
        self.assertFalse(worker.audit_request(body, expected, keys[::-1])['matches'])
        body['option']['agent_mode'] = 0
        self.assertFalse(worker.audit_request(body, expected, keys)['matches'])

    async def test_observer_waits_for_second_card_and_never_regenerates_first(self):
        import plugin_expert30 as worker
        if not hasattr(worker, 'resume_video'):
            self.fail('Expert30 needs a multiple-card observer on the original conversation')
        from unittest.mock import patch
        source_a = parse_poll_response(response([card('a')]), '123456789')['plugin_expert30']['clips'][0]
        source_b = parse_poll_response(response([card('b')]), '123456789')['plugin_expert30']['clips'][0]
        source_b['index'] = 2
        polls = iter([{'plugin_expert30': {'accepted': True, 'tools_finished': False, 'clips': [source_a], 'reply': ''}},
                      {'plugin_expert30': {'accepted': True, 'tools_finished': True, 'clips': [source_a, source_b], 'reply': ''}}])
        downloads = []
        class Session:
            account='fixture'; conversation_id='123456789'; proxy_url=None; own_isp=False
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def poll(self): return next(polls)
        async def resolve(session, clip, resolution):
            downloads.append(clip['clip_id'])
            return {**clip, 'actual_duration': 15, 'local_path': 'verified-fixture.mp4'}
        with patch('video_polling.HttpVideoSession', return_value=Session()), \
             patch('plugin_resources.resolve_clip', resolve), patch('config.POLL_INTERVAL', .001):
            result = await worker.resume_video('fixture', '123456789', 2)
        self.assertEqual(len(result['clips']), 2)
        self.assertEqual(downloads, [source_a['clip_id'], source_b['clip_id']])
        self.assertTrue(result['generation_finished'])


if __name__ == '__main__':
    unittest.main()
