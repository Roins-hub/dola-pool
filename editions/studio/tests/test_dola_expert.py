import base64
import json
import unittest

from video_capabilities import submission_duration
from dola_expert import (ExpertWeeklyLimitError, build_expert_prompt, expert_outcome,
                         parse_expert_messages, validate_native_request)


def chain(blocks, user_type=2, conversation='123'):
    return {'downlink_body': {'pull_singe_chain_downlink_body': {'messages': [
        {'conversation_id': conversation, 'user_type': user_type, 'content': json.dumps(blocks)}]}}}


def text(value):
    return {'content': {'text_block': {'text': value}}}


def tool(status=4):
    return {'content': {'generic_tool_block': {'tool_name': 'text_to_video', 'status': status}}}


class ExpertTests(unittest.TestCase):
    def test_explicit_german_pro_weekly_limit_stops_only_expert_conversation(self):
        from video_outcome import PlatformNoChargeError, conversation_outcome

        reply = ('Du hast das kostenlose 7-Tage-Limit erreicht. Komm am '
                 '1. November 2026, 16:33 (GMT+08:00) wieder oder chatte im Fast-Modus.')
        _, outcome = expert_outcome({'expert': {'accepted': False, 'videos': [], 'reply': reply}})
        self.assertEqual(outcome['state'], 'failed')
        self.assertIsInstance(outcome['error'], ExpertWeeklyLimitError)
        self.assertIsInstance(outcome['error'], PlatformNoChargeError)
        self.assertEqual(outcome['error'].task_phase, 'expert_weekly_limit')
        self.assertEqual(outcome['error'].reply, reply)

        ordinary = conversation_outcome({'assistant_messages': [[reply]], 'videos': []},
                                        model='seedance-2.0')
        self.assertIsNone(ordinary['error'])
        _, already_started = expert_outcome({'expert': {'accepted': True, 'videos': [], 'reply': reply}})
        self.assertIsNone(already_started['error'])

    def test_weekly_limit_needs_exact_platform_reply(self):
        quoted = ('示例：Du hast das kostenlose 7-Tage-Limit erreicht. Komm am '
                  '1. November 2026 wieder oder chatte im Fast-Modus.')
        _, outcome = expert_outcome({'expert': {'accepted': False, 'videos': [], 'reply': quoted}})
        self.assertIsNone(outcome['error'])

    def test_expert_profile_is_distinct_from_normal_and_thirty_parameter_profile(self):
        self.assertEqual(submission_duration('seedance-2.0', 15, 'pro-fast15'), 15)
        self.assertEqual(submission_duration('seedance-2.0', 15, 'fast15-via30'), 30)
        self.assertEqual(submission_duration('seedance-2.0', 15), 15)
        for model, seconds in [('seedance-2.5', 15), ('seedance-2.0', 10), ('seedance-2.0', 30)]:
            with self.assertRaises(ValueError):submission_duration(model, seconds, 'pro-fast15')

    def test_text_only_and_user_echo_do_not_become_receipts(self):
        for data in [chain([text('视频已生成 https://v16-dola.dola.com/test.mp4')]),
                     chain([tool(), text('https://v16-dola.dola.com/test.mp4')], user_type=1)]:
            self.assertFalse(parse_expert_messages(data, '123')['accepted'])
            self.assertEqual(parse_expert_messages(data, '123')['videos'], [])

    def test_completed_tool_extracts_only_its_own_conversation_platform_media(self):
        data = chain([tool(), text('视频已生成：![视频](https://v16-dola.dola.com/a.mp4?lr=video_gen_watermark_dyn)')])
        parsed = parse_expert_messages(data, '123')
        self.assertTrue(parsed['accepted'])
        self.assertEqual(parsed['videos'], ['https://v16-dola.dola.com/a.mp4?lr=video_gen_watermark_dyn'])
        self.assertFalse(parse_expert_messages(data, 'other')['accepted'])
        self.assertEqual(parse_expert_messages(chain([tool(), text('https://evil.example/video.mp4')]), '123')['videos'], [])
        self.assertEqual(parse_expert_messages(chain([tool(), text('[视频](http://v16-dola.dola.com/a.mp4)')]), '123')['videos'], ['http://v16-dola.dola.com/a.mp4'])
        self.assertEqual(parse_expert_messages(chain([tool(), text('http://v16-dola.dola.com.evil.example/a.mp4')]), '123')['videos'], [])

    def test_running_tool_does_not_publish_a_link_before_completion(self):
        parsed = parse_expert_messages(chain([tool(2), text('https://v16-dola.dola.com/a.mp4')]), '123')
        self.assertTrue(parsed['accepted'])
        self.assertEqual(parsed['videos'], [])

    def test_parser_does_not_change_normal_receipt_or_downloads(self):
        from video_polling import parse_poll_response
        data=chain([tool(), text('https://v16-dola.dola.com/test.mp4')])
        result=parse_poll_response(data, '123')
        self.assertTrue(result['expert']['accepted'])
        self.assertEqual(len(result['expert']['videos']), 1)
        self.assertEqual(result['videos'], [])
        for malformed in [None, {}, {'downlink_body': []}, chain([None, {'content': None}])]:
            self.assertFalse(parse_expert_messages(malformed, '123')['accepted'])

    def test_native_request_requires_pro_mode_and_unchanged_prompt(self):
        body={'option': {'model_config': {'model_item_key': '4'}, 'agent_mode': 1, 'conversation_mode': 2},
              'messages': [{'content_block': [{'content': {'text_block': {'text': 'exact'}}}]}]}
        self.assertTrue(validate_native_request(body, 'exact'))
        self.assertFalse(validate_native_request(body, 'different'))
        body['option'].pop('conversation_mode')
        self.assertTrue(validate_native_request(body, 'exact'))
        body['option']['conversation_mode']=0
        self.assertFalse(validate_native_request(body, 'exact'))
        body['option'].pop('conversation_mode')
        body['option']['agent_mode']=0
        self.assertFalse(validate_native_request(body, 'exact'))

    def test_prompt_keeps_user_text_and_has_no_one_credit_claim(self):
        prompt=' 原始提示词\n不要修改末尾空格 '
        wrapped=build_expert_prompt(prompt, '16:9')
        self.assertTrue(wrapped.endswith(prompt))
        self.assertIn('Seedance 2.0 Fast',wrapped)
        self.assertIn('15秒',wrapped)
        self.assertNotIn('1个额度',wrapped)

class ExpertDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_expert_poll_stops_after_explicit_weekly_limit_without_repolling(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from dola_expert import resume_video

        reply = ('Du hast das kostenlose 7-Tage-Limit erreicht. Komm am '
                 '1. November 2026, 16:33 (GMT+08:00) wieder oder chatte im Fast-Modus.')
        session = SimpleNamespace(poll=AsyncMock(return_value={
            'expert': {'accepted': False, 'videos': [], 'reply': reply}}))
        context = AsyncMock()
        context.__aenter__.return_value = session
        events = []
        with patch('video_polling.HttpVideoSession', return_value=context), patch('expert_trace.record_poll'):
            with self.assertRaises(ExpertWeeklyLimitError):
                await resume_video('account', 'conversation', 60,
                                   on_event=lambda phase, details: events.append((phase, details)))
        self.assertEqual(session.poll.await_count, 1)
        self.assertEqual([phase for phase, _ in events], ['awaiting_receipt'])

    async def _collect_original_payload(self, payload, status=200):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock, patch
        from dola_expert import collect_result
        vid = 'v186a3gm000cdbeqslvog65t5298n99g'
        with TemporaryDirectory() as root:
            preview = Path(root) / 'preview.mp4'
            preview.write_bytes(('vid:' + vid).encode())
            original = Path(root) / 'original.mp4'
            response = SimpleNamespace(status=status, json=AsyncMock(return_value=payload))
            context = AsyncMock()
            context.__aenter__.return_value = response
            transport = SimpleNamespace(post=Mock(return_value=context))
            session = SimpleNamespace(account='original-account', conversation_id='123',
                proxy_url='http://bound:1', own_isp=True, state={'cookies': []}, session=transport)
            download = AsyncMock(side_effect=[preview, original])
            with patch('video_worker._download', download), patch('proxy_usage.usage.reserve'), patch('config._parse_proxy'):
                result = await collect_result(session, 'https://v16-dola.dola.com/preview.mp4?lr=video_gen_watermark_dyn')
            return result, download.await_args_list

    async def test_official_download_url_field_collects_original_without_changing_bound_route(self):
        vid = 'v186a3gm000cdbeqslvog65t5298n99g'
        for scheme in ('https', 'http'):
            with self.subTest(scheme=scheme):
                original = scheme + '://v16-dola.dola.com/original.mp4?lr=unwatermarked&signature=keep-exact'
                result, calls = await self._collect_original_payload({'code': 0, 'data': {
                    'without_watermark': True, 'download_video': {vid: {'download_url': original}}}})
                self.assertEqual(result['video_url'], original)
                self.assertEqual(result['watermark'], 'verified_download')
                self.assertEqual(result['download_source'], 'dola_original_api')
                self.assertEqual(len(calls), 2)
                self.assertEqual(calls[1].args[0], original)
                self.assertEqual(calls[1].kwargs['proxy_url'], 'http://bound:1')
                self.assertTrue(calls[1].kwargs['own_isp'])

    async def test_official_video_model_original_is_preferred_when_same_vid_is_confirmed(self):
        vid = 'v186a3gm000cdbeqslvog65t5298n99g'
        original = 'https://v16-dola.dola.com/original.mp4?lr=unwatermarked&signature=keep-exact'
        model = json.dumps({'video_list': {'normal': {'main_url': base64.b64encode(original.encode()).decode(),
                                                      'bitrate': 1200}}})
        payload = {'code': 0, 'data': {'without_watermark': True, 'download_video': {vid: {
            'video_model': model,
            'download_url': 'https://v16-dola.dola.com/preview.mp4?lr=video_gen_watermark_dyn'}}}}
        result, calls = await self._collect_original_payload(payload)
        self.assertEqual(result['video_url'], original)
        self.assertEqual(result['watermark'], 'verified_download')
        self.assertEqual(result['download_source'], 'dola_original_api')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1].args[0], original)
        self.assertEqual(calls[1].kwargs['proxy_url'], 'http://bound:1')
        self.assertTrue(calls[1].kwargs['own_isp'])

    async def test_video_model_without_explicit_original_is_not_upgraded(self):
        vid = 'v186a3gm000cdbeqslvog65t5298n99g'
        for original, available in (
            ('https://v16-dola.dola.com/preview.mp4?lr=cici_ai', True),
            ('https://v16-dola.dola.com/ambiguous.mp4', True),
            ('https://v16-dola.dola.com/original.mp4?lr=unwatermarked', False),
            ('https://v16-dola.dola.com.evil.example/original.mp4?lr=unwatermarked', True),
        ):
            with self.subTest(url=original, available=available):
                model = {'video_list': {'one': {'main_url': base64.b64encode(original.encode()).decode()}}}
                result, calls = await self._collect_original_payload({'code': 0, 'data': {
                    'without_watermark': available, 'download_video': {vid: {'video_model': model}}}})
                self.assertEqual(result['watermark'], 'unknown')
                self.assertEqual(len(calls), 1)

    async def test_invalid_video_model_does_not_hide_official_download_url(self):
        vid = 'v186a3gm000cdbeqslvog65t5298n99g'
        original = 'https://v16-dola.dola.com/original.mp4?lr=unwatermarked&signature=exact'
        result, calls = await self._collect_original_payload({'code': 0, 'data': {
            'without_watermark': True,
            'download_video': {vid: {'video_model': '{broken', 'download_url': original}}}})
        self.assertEqual(result['video_url'], original)
        self.assertEqual(result['watermark'], 'verified_download')
        self.assertEqual(len(calls), 2)

    async def test_original_response_does_not_upgrade_false_missing_or_other_video_results(self):
        vid = 'v186a3gm000cdbeqslvog65t5298n99g'
        original = 'https://v16-dola.dola.com/original.mp4'
        payloads = [
            {'code': 0, 'data': {'without_watermark': False, 'download_video': {vid: {'download_url': original}}}},
            {'code': 1, 'data': {'without_watermark': True, 'download_video': {vid: {'download_url': original}}}},
            {'code': 0, 'data': {'without_watermark': True, 'download_video': {'different-vid': {'download_url': original}}}},
            {'code': 0, 'data': {'without_watermark': True, 'download_video': {vid: {'download_url': 1}}}},
            {'code': 0, 'data': {'without_watermark': True, 'download_video': {vid: {}}}},
        ]
        for index, payload in enumerate(payloads):
            with self.subTest(case=index):
                result, calls = await self._collect_original_payload(payload)
                self.assertEqual(result['watermark'], 'unknown')
                self.assertEqual(result['download_source'], 'preview')
                self.assertEqual(len(calls), 1)

    async def test_original_response_rejects_known_watermark_and_nonofficial_downloads(self):
        vid = 'v186a3gm000cdbeqslvog65t5298n99g'
        for url in (
            'https://v16-dola.dola.com/a.mp4?lr=video_gen_watermark_dyn',
            'https://v16-dola.dola.com/a.mp4?lr=cici_ai',
            'https://v16-dola.dola.com/a.mp4?with_watermark=true',
            'https://v16-dola.dola.com.evil.example/a.mp4',
            'https://user:password@v16-dola.dola.com/a.mp4',
            'https://v16-dola.dola.com:8080/a.mp4',
            'https://v16-dola.dola.com:invalid/a.mp4',
        ):
            with self.subTest(url=url):
                result, calls = await self._collect_original_payload({'code': 0, 'data': {
                    'without_watermark': True, 'download_video': {vid: {'url': url}}}})
                self.assertEqual(result['watermark'], 'unknown')
                self.assertEqual(len(calls), 1)

    async def test_only_explicit_original_resource_for_same_video_passes(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock, patch
        from dola_expert import collect_result
        vid='v186a3gm000cdbeqslvog65t5298n99g'
        source='https://v16-dola.dola.com/preview.mp4?lr=video_gen_watermark_dyn'
        original='https://v16-dola.dola.com/original.mp4'
        with TemporaryDirectory() as root:
            preview=Path(root)/'preview.mp4'; preview.write_bytes(('vid:'+vid).encode())
            clean=Path(root)/'original.mp4'
            for available in (False, True):
                response=SimpleNamespace(status=200,json=AsyncMock(return_value={'code':0,'data':{
                    'without_watermark':available,'download_video':{vid:{'url':original}}}}))
                ctx=AsyncMock();ctx.__aenter__.return_value=response
                transport=SimpleNamespace(post=Mock(return_value=ctx))
                session=SimpleNamespace(account='a',conversation_id='123',proxy_url='http://bound:1',
                    own_isp=True,state={'cookies':[]},session=transport)
                download=AsyncMock(side_effect=[preview,clean])
                with patch('video_worker._download',download),patch('proxy_usage.usage.reserve'),patch('config._parse_proxy'):
                    result=await collect_result(session,source)
                self.assertEqual(result['watermark'],'verified_download' if available else 'unknown')
                self.assertEqual(download.await_count,2 if available else 1)
                for call in download.await_args_list:
                    self.assertEqual(call.kwargs['proxy_url'],'http://bound:1')
                    self.assertTrue(call.kwargs['own_isp'])
                self.assertEqual(transport.post.call_args.kwargs['json'],{'vid':[vid]})

    async def test_expert_refuses_reference_material_before_opening_browser(self):
        from dola_expert import generate_video, ExpertSubmissionError
        from unittest.mock import AsyncMock, patch
        launch=AsyncMock()
        with patch('browser.launch_account_context',launch):
            with self.assertRaises(ExpertSubmissionError):
                await generate_video('a','prompt',reference_image_paths=['reference.jpg'])
        launch.assert_not_awaited()

class ExpertLabelsTests(unittest.TestCase):
    def test_native_expert_labels_support_regional_interfaces(self):
        from dola_expert import EXPERT_LABEL
        for label in ('Pro','Expert','专家','專家'):
            self.assertIsNotNone(EXPERT_LABEL.fullmatch(label))
        for label in ('Fast','Seedance 2.0 Fast','Try Pro for a fee'):
            self.assertIsNone(EXPERT_LABEL.fullmatch(label))


class ExpertMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_blank_initial_menu_is_reopened_once_before_selecting_pro(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from dola_expert import select_expert_mode, EXPERT_LABEL

        active = SimpleNamespace(is_visible=AsyncMock(return_value=False), wait_for=AsyncMock())
        fast = SimpleNamespace(click=AsyncMock())
        fast.last = fast
        choice = SimpleNamespace(wait_for=AsyncMock(side_effect=[TimeoutError('menu not ready'), None]),
                                 click=AsyncMock())
        page = SimpleNamespace(
            get_by_role=Mock(side_effect=lambda role, name: active if name is EXPERT_LABEL else fast),
            get_by_text=Mock(return_value=choice),
            keyboard=SimpleNamespace(press=AsyncMock()), wait_for_timeout=AsyncMock())

        await select_expert_mode(page)

        self.assertEqual(fast.click.await_count, 2)
        page.keyboard.press.assert_awaited_once_with('Escape')
        choice.click.assert_awaited_once()
        active.wait_for.assert_awaited_once_with(state='visible', timeout=20000)

class ExpertRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_disconnect_queries_same_conversation_without_resubmission(self):
        import time
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock, patch
        from dola_expert import resume_video
        poll={'expert':{'accepted':True,'reply':'视频完成','videos':['https://v16-dola.dola.com/one.mp4']}}
        session=SimpleNamespace(poll=AsyncMock(side_effect=[OSError(64,'temporary disconnect'),poll]))
        ctx=AsyncMock();ctx.__aenter__.return_value=session
        factory=Mock(return_value=ctx);collect=AsyncMock(return_value={'result':'original'})
        with patch('video_polling.HttpVideoSession',factory),patch('dola_expert.collect_result',collect),patch('dola_expert.asyncio.sleep',AsyncMock()):
            result=await resume_video('original-account','123',60,deadline_at=time.time()+60)
        self.assertEqual(result,{'result':'original'})
        factory.assert_called_once_with('original-account','123')
        self.assertEqual(session.poll.await_count,2)
        collect.assert_awaited_once_with(session,'https://v16-dola.dola.com/one.mp4')
