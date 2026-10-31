"""Offline 2.5/30 regression: no live accounts, routes or generation requests."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from patchright.async_api import async_playwright
from providers.doubao import DoubaoProvider, ProviderError
from providers.doubao.runtime import BrowserSession, VIDEO_REQUEST_SUFFIX
from providers.doubao.confirmation import CONFIRM_TEXT
from platform_runtime import generation_options, task_generation_options
from test_doubao_execution_evidence import message, wire
from test_doubao_experimental25_capabilities import experimental25_manifest
from test_doubao_ordinary_dialog_runtime import CAP, HTML, REQUEST
from test_doubao_provider import ck, PROXY
from test_doubao_result_validation import mp4
from test_platform_generation_options import request_namespace

REQUEST30 = {**REQUEST, 'model': 'dialog_seedance_2.5', 'duration': 30}
CAP30 = {**CAP, 'id': 'dialog_seedance_2.5', 'label': 'Seedance 2.5（30秒试验）'}
QUESTION30 = '我已受理并开始生成视频。\n视频生成参数确认\n模型：Seedance 2.5\n时长：30 秒\n比例：16:9 横屏'
ACK30 = '本次使用 Seedance 2.5 生成，预计等待15分钟。影片生成好后，会发送给你。'


def submitted30():
    record = message(ACK30, mid='execution', execution=True)
    record['index_in_conv'] = 8
    record['ext']['chat_ability'] = json.dumps({'ability_type': 17, 'ability_param': json.dumps(
        {'model': 'seedance_v2.5', 'duration': 30, 'ratio': '16:9'})})
    return record


class ThirtyReceiptBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.driver = await async_playwright().start(); self.addAsyncCleanup(self.driver.stop)
        self.browser = await self.driver.chromium.launch(headless=True); self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()
        await self.page.route('**/*', lambda r: r.fulfill(content_type='text/html', body=HTML))
        await self.page.goto('https://www.doubao.com/chat/current')
        self.path = Path(self.temp.name) / 'snapshot.json'
        self.session = BrowserSession(SimpleNamespace(_snapshot_path=lambda _: self.path), {'name': 'fixture'})
        self.session.page, self.session.context = self.page, self.page.context
        self.snapshot = {'id': 'video_' + 'a'*32, 'conversation_id': 'current', 'sent': True,
            'created_at': time.time()-60, 'request': dict(REQUEST30), 'baseline_messages': [], 'baseline_texts': []}

    async def observe(self, *records):
        self.session.payloads = [wire(*records)]
        return await self.session.observe(self.snapshot, copy.deepcopy(CAP30), timeout=0)

    def stable(self, text=QUESTION30):
        self.snapshot['confirmation_question'] = {'hash': hashlib.sha256(text.encode()).hexdigest(),
                                                  'since': time.time()-5}

    async def test_thirty_prompt_then_one_confirmation_survive_recovery(self):
        await self.session.prepare(dict(REQUEST30), copy.deepcopy(CAP30), [])
        prompt = await self.page.locator('#prompt').input_value()
        self.assertTrue(prompt.endswith('\n'+VIDEO_REQUEST_SUFFIX))
        self.assertEqual(prompt.count(VIDEO_REQUEST_SUFFIX), 1)
        self.assertIn(REQUEST30['prompt'], prompt)
        self.assertIn('请使用Seedance 2.5生成一条30秒、16:9的视频。', prompt)
        self.assertNotIn('Seedance 2.0 Fast', prompt)
        await self.session.submit()
        # Real editors clear after send; this fixture only records clicks.
        await self.page.locator('#prompt').fill('')
        self.stable()
        result = await self.observe(message(QUESTION30))
        self.assertEqual(await self.page.locator('#prompt').input_value(), CONFIRM_TEXT)
        self.assertEqual(result['phase'], 'unknown')
        self.assertFalse(result['generation_evidence']['confirmed'])
        self.snapshot = json.loads(self.path.read_text())
        await self.page.reload()
        await self.observe(message(QUESTION30))
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')
        self.assertTrue(self.snapshot['confirmation_sent'])

    async def test_thirty_only_confirms_matching_spec_and_never_paid_or_manual_answer(self):
        for text in (QUESTION30.replace('30 秒', '15 秒'), QUESTION30.replace('2.5', '2.0 Fast'),
                     QUESTION30.replace('16:9', '9:16'), QUESTION30+'\n请升级付费后继续'):
            with self.subTest(text=text):
                self.stable(text)
                await self.observe(message(text))
                self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')
        self.stable()
        answer = message('确定生成', mid='human', user_type=1); answer['index_in_conv'] = 6
        await self.observe(message(QUESTION30), answer)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')
        self.assertTrue(self.snapshot['confirmation_answered'])

    async def test_thirty_acceptance_requires_execution_not_parameter_echo(self):
        record = submitted30()
        echo = copy.deepcopy(record); echo['content_block'] = echo['content_block'][1:]
        echo['ext'].pop('use_creation')
        self.assertEqual((await self.observe(echo))['phase'], 'text_only_ack')
        actual = await self.observe(record)
        self.assertEqual(actual['phase'], 'accepted')
        self.assertTrue(actual['dialog_model_verified'])
        wrong = await self.observe(message(execution=True))
        self.assertEqual(wrong['phase'], 'request_mismatch')
        self.assertFalse(wrong['dialog_model_verified'])

    async def test_thirty_confirmation_uses_spec_fields_not_storyboard_cut_times(self):
        text=('生成30秒视频，重点保证15秒硬切。\n**视频生成参数确认**\n'
              '- **模型：** Seedance 2.5（会员专属模型）\n- **时长：** 30 秒\n'
              '- **比例：** 16:9 横屏\n\n**分镜表**\n| 0–2.4s | 开场 |\n'
              '| 13.9–15s | 硬切前留白 |\n| 15–30s | 平行世界验证 |\n确认后我再开始生成视频。')
        self.stable(text)
        result=await self.observe(message(text))
        self.assertEqual(await self.page.locator('#prompt').input_value(),CONFIRM_TEXT)
        self.assertEqual(result['phase'],'unknown')

    async def test_conflicting_labeled_total_duration_is_never_ignored(self):
        text=QUESTION30+'\n时长：15秒\n分镜包含15秒硬切。'
        self.stable(text)
        await self.observe(message(text))
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')

    async def test_actual_account_verification_still_blocks_confirmation(self):
        for warning in ('请先完成人机验证','需要驗證帳號后再继续','请输入短信验证码','验证后继续生成'):
            text=QUESTION30+'\n'+warning
            self.stable(text)
            await self.observe(message(text))
            self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')

    async def test_thirty_untyped_actual_submission_does_not_need_fast_job_mapping(self):
        record = submitted30(); record['ext']['chat_ability'] = '{}'
        result = await self.observe(record)
        self.assertEqual(result['phase'], 'accepted')
        self.assertIsNone(result['generation_evidence']['parameters_match'])
        self.assertTrue(result['dialog_model_verified'])  # actual submission's matching model wording
        record['content_block'][-1]['content']['text_block']['text'] = '影片生成已提交'
        self.snapshot.pop('dialog_model_verified', None)
        result = await self.observe(record)
        self.assertEqual(result['phase'], 'accepted')
        self.assertFalse(result['dialog_model_verified'])  # never invent an unknown 2.5 req_key

    async def test_thirty_traditional_completed_card_cannot_mask_conflicting_fast_job(self):
        record = submitted30()
        record['ext']['ai_creation_tool_list'] = json.dumps([{'task_id': 12345,
            'tool_name': 'text_to_video', 'fail_code': 0, 'req_key': 'seedance_v20_fast_flow'}])
        await self.page.evaluate('''()=>{document.querySelector('#thread').innerHTML=
          '<div class="assistant"><div data-message-id="finished">影片生成完畢。'+
          '<button class="play-icon-wrapper-fixture" onclick="play()"></button></div></div>'}''', isolated_context=False)
        result = await self.observe(record)
        self.assertEqual(result['phase'], 'media_ready')  # preserve a delivered file, report mismatch later
        self.assertEqual(result['media'], ['https://cdn.example/current.mp4'])
        self.assertFalse(result['dialog_model_verified'])
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')


class ThirtyDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.provider = DoubaoProvider(root=self.root, enabled=True)
        self.provider.import_ck('fixture-standard', ck(), PROXY, category='标准账号')
        self.provider.accounts.save_diagnostics('fixture-standard', {'route_verified': True, 'login_verified': True})
        self.provider.save_capabilities(experimental25_manifest())
        self.task_id = 'video_'+'b'*32

    async def finish(self, seconds, *, verified=True):
        path = self.root / 'fixture.mp4'; path.write_bytes(mp4(1280, 720, seconds))
        async def download(url, task_id):
            self.assertEqual(task_id, self.task_id)
            return {'output_path': str(path), 'actual_duration': seconds,
                    'dimensions': {'width': 1280, 'height': 720}, 'download_source': 'preview'}
        self.provider.accounts.reserve(self.task_id, 'fixture-standard')
        snapshot = {'id': self.task_id, 'platform': 'doubao', 'account': 'fixture-standard',
            'request': dict(REQUEST30), 'capability': copy.deepcopy(CAP30), 'submitted_duration': 30,
            'conversation_id': 'current', 'sent': True}
        result = await self.provider._finish(SimpleNamespace(download=download), snapshot,
            {'phase': 'media_ready', 'media': ['https://cdn.example/current.mp4'], 'dialog_model_verified': verified})
        self.assertTrue(path.exists())
        self.assertEqual(result['submitted_duration'], 30)
        return result

    async def test_real_thirty_seconds_can_complete(self):
        self.assertEqual((await self.finish(30))['status'], 'completed')

    async def test_fifteen_second_video_is_saved_but_not_thirty_second_success(self):
        result = await self.finish(15)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'duration_mismatch')

    async def test_unverified_model_is_saved_but_not_success(self):
        result = await self.finish(30, verified=False)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'model_unverified')

    async def test_thirty_queue_defaults_and_standard_only_admission(self):
        request_type = request_namespace()['VideoGenRequest']
        values = request_type(platform='doubao', prompt='cat', model=REQUEST30['model'],
                              duration=30, ratio='16:9').model_dump()
        request = self.provider.validate_request(values)
        self.assertEqual((request['duration'], request['submitted_duration']), (30, 30))
        self.assertTrue(task_generation_options({'generation_options': json.dumps(generation_options(values))})['auto_confirm'])
        self.assertEqual((await self.provider._choose_account(category='标准账号'))['name'], 'fixture-standard')
        self.provider.accounts.patch('fixture-standard', category='普通账号')
        with self.assertRaises(ProviderError) as error:
            await self.provider._choose_account(category='标准账号')
        self.assertEqual(error.exception.code, 'account_unavailable')


if __name__ == '__main__':
    unittest.main()
