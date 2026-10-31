"""Replay fake acknowledgements without contacting Doubao or spending credits."""
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from patchright.async_api import async_playwright
from providers.doubao.runtime import BrowserSession
from providers.doubao.confirmation import CONFIRM_TEXT
from test_doubao_execution_evidence import message, wire
from test_doubao_ordinary_dialog_runtime import CAP, HTML, REQUEST

ACK30 = ('✅ 已受理，开始使用 Seedance 2.5 生成这条 30 秒、16:9 横屏、720p 的 3D 二创动画，'
         '严格按照你的完整时间线、人物参考、15 秒硬切分单元、对白与音效要求执行。\n等待视频渲染完成。')
ACK15 = '已受理，开始使用 Seedance 2.0 Fast 生成这条15秒、16:9的视频，完成后通知你。'


def record(text, mid, index, user_type=2, execution=False):
    row = message(text, mid=mid, user_type=user_type, execution=execution)
    row['index_in_conv'] = index
    return row


class ConfirmedStateNudgeTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {'sent':True, 'created_at':0, 'text_ack_nudge_enabled':True,
            'request':{**REQUEST, 'model':'dialog_seedance_2.5', 'duration':30},
            'confirmation_sent':True, 'confirmation_sent_at':1, 'confirmation_message_id':'question'}

    def due(self, text, *, creation=False):
        from providers.doubao.confirmation import native_nudge_reply, nudge_due
        from providers.doubao.execution_evidence import native_execution_evidence
        rows = [record('视频生成参数确认\n模型：Seedance 2.5\n时长：30秒\n比例：16:9', 'question', 2),
            record('确定生成','confirmation',3,user_type=1),record(text,'reply',4)]
        rows[0]['ext'].update(use_creation='1',ai_creation_tool_list='[]')
        if creation: rows[-1]['ext'].update(use_creation='1',ai_creation_tool_list='[]')
        payloads=[wire(*rows)]
        evidence,_=native_execution_evidence(payloads,'current',set(),self.snapshot['request'])
        reply=native_nudge_reply(payloads,'current',set(),self.snapshot)
        nudge_due(self.snapshot,reply,evidence,now=100)
        return nudge_due(self.snapshot,reply,evidence,now=106)

    def test_unseen_finished_replies_after_confirmation_depend_on_state(self):
        for text in ('好的，我记下了。','需求已确认，稍后给你结果。','请把描述发过来。',
                     '抱歉，前面只是复述了参数，还没有实际发起生成。',
                     '确认后我再开始生成视频。','Okay, your instructions are noted.'):
            with self.subTest(text=text):
                self.setUp();self.assertTrue(self.due(text))

    def test_creation_flag_without_tool_or_native_block_is_still_plain_reply(self):
        self.assertTrue(self.due('好的，这就按你确认的要求处理。',creation=True))

    def test_confirmed_context_never_overrides_new_conflicting_parameters(self):
        for text in ('改成 Seedance 2.0 Fast。','我将生成10秒的视频。','这次比例9:16。',
                     '好的\n时长：10秒','好的\n模型：Seedance 2.0 Fast',
                     '参数已确认\n- **时长**：10秒'):
            with self.subTest(text=text):
                self.setUp();self.assertFalse(self.due(text))

    def test_remaining_credit_information_is_not_a_payment_request(self):
        self.assertTrue(self.due('Your request is noted. You have 3 credits remaining.'))

    def test_refusal_verification_quota_and_payment_always_block(self):
        for text in ('由于版权限制，本次不予生成。','该素材受内容政策限制。','余额不足，请充值。',
                     '请先登录账号。','请完成安全验证。','请购买会员后继续。',
                     "I can't generate this because of copyright restrictions.",
                     'Please sign in to continue.','Please complete the captcha.',
                     'Insufficient credits. Upgrade your plan.'):
            with self.subTest(text=text):
                self.setUp();self.assertFalse(self.due(text))


class AckNudgeBrowserTests(unittest.IsolatedAsyncioTestCase):
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
        self.now = time.time()
        self.snapshot = {'id': 'video_'+'b'*32, 'conversation_id': 'current', 'sent': True,
            'created_at': self.now-60, 'request': {**REQUEST, 'model': 'dialog_seedance_2.5', 'duration': 30},
            'baseline_messages': [], 'baseline_texts': [], 'text_ack_nudge_enabled': True,
            'confirmation_sent': True, 'confirmation_sent_at': self.now-20, 'confirmation_message_id': 'question'}
        self.cap = {**CAP, 'id': 'dialog_seedance_2.5'}

    def chain(self, text=ACK30):
        return [record('视频生成参数确认\n模型：Seedance 2.5\n时长：30秒\n比例：16:9', 'question', 2),
                record('确定生成', 'confirmation', 3, user_type=1), record(text, 'fake-ack', 4)]

    async def observe(self, records, elapsed=0):
        self.session.payloads = [wire(*records)]
        with patch('providers.doubao.runtime.time.time', return_value=self.now+elapsed):
            return await self.session.observe(self.snapshot, copy.deepcopy(self.cap), timeout=0)

    async def stable_observe(self, records):
        await self.observe(records)
        return await self.observe(records, 6)

    async def test_thirty_fake_ack_gets_one_nudge_and_stays_unconfirmed(self):
        await self.observe(self.chain())
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')
        result = await self.observe(self.chain(), 6)
        self.assertEqual(await self.page.locator('#prompt').input_value(), '你倒是生成啊')
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '1')
        self.assertEqual(result['phase'], 'unknown')
        self.assertFalse(result['generation_evidence']['confirmed'])
        self.snapshot = json.loads(self.path.read_text())
        await self.page.reload()
        await self.observe(self.chain(), 60)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')

    async def test_confirmed_parameter_question_then_lost_context_gets_one_nudge(self):
        rows = self.chain('收到！✅\n你想要生成什么内容呀，把需求发我，我直接开始。')
        rows[0]['ext']['use_creation'] = '1'
        rows[0]['ext']['ai_creation_tool_list'] = '[]'
        result = await self.stable_observe(rows)
        self.assertEqual(await self.page.locator('#prompt').input_value(), '你倒是生成啊')
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '1')
        self.assertFalse(result['generation_evidence']['confirmed'])
        self.snapshot = json.loads(self.path.read_text())
        await self.page.reload()
        await self.observe(rows, 60)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')

    async def test_lost_context_does_not_bypass_unknown_creation_or_wrong_parameters(self):
        for change in ('wrong-duration', 'not-a-question', 'unknown-tool', 'extra-opaque'):
            rows = self.chain('收到！✅\n你想要生成什么内容呀，把需求发我，我直接开始。')
            rows[0]['ext']['use_creation'] = '1'
            if change == 'wrong-duration':
                rows[0] = record('视频生成参数确认\n模型：Seedance 2.5\n时长：10秒\n比例：16:9', 'question', 2)
            elif change == 'not-a-question':
                rows[0] = record(ACK30, 'question', 2)
                rows[0]['ext']['use_creation'] = '1'
            elif change == 'unknown-tool':
                rows[0]['ext']['ai_creation_tool_list'] = '[{"tool_name":"unknown"}]'
            else:
                extra = record('正在处理', 'opaque', 1)
                extra['ext']['use_creation'] = '1'
                rows.insert(0, extra)
            await self.stable_observe(rows)
            self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')

    async def test_lost_context_after_dom_confirmation_uses_unique_native_question(self):
        self.snapshot['confirmation_message_id'] = None
        rows = self.chain('收到！✅\n你想要生成什么内容呀，把需求发我，我直接开始。')
        rows[0]['ext']['use_creation'] = '1'
        await self.stable_observe(rows)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '1')

    async def test_group_three_confirmed_but_asks_for_prompt_gets_one_nudge(self):
        self.snapshot['confirmation_message_id'] = None
        rows = self.chain('收到～已经确认【确定生成】✅\n你把要生成的提示词发给我，我直接按你的要求执行，不改动指令。')
        rows[0]['ext']['use_creation'] = '1'
        rows[0]['ext']['ai_creation_tool_list'] = '[]'
        result = await self.stable_observe(rows)
        self.assertEqual(await self.page.locator('#prompt').input_value(), '你倒是生成啊')
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '1')
        self.assertFalse(result['generation_evidence']['confirmed'])
        self.snapshot = json.loads(self.path.read_text())
        await self.page.reload()
        await self.observe(rows, 60)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')

    async def test_unseen_reply_after_confirmation_sends_once_through_native_composer(self):
        rows=self.chain('需求已经记下，我这边准备就绪。')
        rows[0]['ext']['use_creation']='1'
        rows[-1]['ext']['use_creation']='1'
        result=await self.stable_observe(rows)
        self.assertEqual(await self.page.locator('#prompt').input_value(),'你倒是生成啊')
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'1')
        self.assertFalse(result['generation_evidence']['confirmed'])
        self.snapshot=json.loads(self.path.read_text())
        await self.page.reload();await self.observe(rows,60)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')

    async def test_fast_fifteen_fake_ack_can_nudge_without_a_parameter_question(self):
        self.snapshot['request'] = dict(REQUEST); self.cap = copy.deepcopy(CAP)
        self.snapshot.pop('confirmation_sent'); self.snapshot.pop('confirmation_message_id')
        result = await self.stable_observe([record(ACK15, 'ack15', 2)])
        self.assertEqual(await self.page.locator('#prompt').input_value(), '你倒是生成啊')
        self.assertEqual(result['phase'], 'unknown')

    async def test_dom_confirmation_can_be_reconciled_from_the_native_user_turn(self):
        self.snapshot['confirmation_message_id'] = None
        await self.stable_observe(self.chain())
        self.assertEqual(await self.page.locator('#prompt').input_value(), '你倒是生成啊')

    async def test_manual_reminder_and_new_user_turn_prevent_another_nudge(self):
        for user_text in ('你倒是生成啊', '先别生成', '改成10秒'):
            rows = self.chain()+[record(user_text, 'manual', 5, user_type=1), record(ACK30, 'later', 6)]
            await self.stable_observe(rows)
            self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')

    async def test_native_execution_or_saved_acceptance_always_blocks_nudge(self):
        rows = self.chain()+[record('影片生成已提交', 'execution', 6, execution=True)]
        await self.stable_observe(rows)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')
        self.snapshot['generation_evidence'] = {'confirmed': True}
        await self.stable_observe(self.chain())
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')

    async def test_no_nudge_for_unfinished_opaque_missing_or_wrong_chat_records(self):
        variants = [[], self.chain()]
        variants[1][-1]['ext']['is_finish'] = '0'
        opaque = self.chain(); opaque[-1]['ext']['use_creation'] = '1'
        opaque[-1]['content_block'].append({'content':{'unknown_tool_block':{}}}); variants.append(opaque)
        other = self.chain()
        for item in other: item['conversation_id'] = 'other'
        variants.append(other)
        for rows in variants:
            await self.stable_observe(rows)
            self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')

    async def test_refusals_payment_verification_or_wrong_specs_never_get_nudged(self):
        for text in (ACK30+'请先完成人机验证', ACK30+'请开通会员付费',
                     ACK30+'该内容违反政策，不能生成', ACK30.replace('2.5', '2.0 Fast'),
                     ACK30.replace('30 秒', '10 秒'), ACK30.replace('16:9', '9:16')):
            await self.stable_observe(self.chain(text))
            self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')

    async def test_old_tasks_disabled_automation_and_edited_composer_are_untouched(self):
        self.snapshot['text_ack_nudge_enabled'] = False
        await self.stable_observe(self.chain())
        self.snapshot['text_ack_nudge_enabled'] = True
        self.snapshot['request']['auto_confirm'] = False
        await self.stable_observe(self.chain())
        self.snapshot['request']['auto_confirm'] = True
        await self.page.locator('#prompt').fill('用户正在输入')
        await self.stable_observe(self.chain())
        self.assertEqual(await self.page.locator('#prompt').input_value(), '用户正在输入')
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'), '0')

    async def test_ambiguous_native_records_do_not_fall_back_to_dom_confirmation(self):
        import hashlib
        text='视频生成参数确认\n模型：Seedance 2.5\n时长：30秒\n比例：16:9'
        self.snapshot.pop('confirmation_sent')
        self.snapshot['confirmation_question']={'hash':hashlib.sha256(text.encode()).hexdigest(),'since':self.now-20}
        await self.page.locator('#thread').evaluate('(n,t)=>{n.innerHTML="<div class=assistant></div>";n.firstChild.textContent=t}',text)
        await self.stable_observe([record(text,'a',2),record(text,'b',2)])
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')

    async def test_execution_arriving_while_filling_cancels_the_nudge_click(self):
        from providers.doubao.composer import DialogComposer
        await self.observe(self.chain())
        real_fill=DialogComposer.fill
        async def fill_and_receive(composer,text):
            await real_fill(composer,text)
            if text=='你倒是生成啊':
                actual=record('影片生成已提交','execution',6,execution=True)
                actual['ext']['chat_ability']='{}'
                self.session.payloads=[wire(*self.chain(),actual)]
        with patch.object(DialogComposer,'fill',fill_and_receive):
            result=await self.observe(self.chain(),6)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')
        self.assertEqual(result['phase'],'accepted')
        self.assertEqual(await self.page.locator('#prompt').input_value(),'')

    async def refresh_sequence(self, execution=True):
        """Chain updates arrive on navigation, while live replies only render DOM."""
        import asyncio
        import providers.doubao.runtime as runtime
        sent=[]
        question='视频生成参数确认\n模型：Seedance 2.5\n时长：30秒\n比例：16:9\n确认后我再开始生成视频。'
        lost='收到👍\n你直接把要生成的提示词发给我，我按你的要求来。\n如果有尺寸、风格、模型这些要求也一起带上。'
        rows=[record(question,'question',2)]
        rows[0]['ext'].update(use_creation='1',ai_creation_tool_list='[]')
        script='''<script>
        async function show(){const r=await fetch('/im/chain/single');const p=await r.json();
          const a=p.downlink_body.pull_singe_chain_downlink_body.messages.filter(m=>m.user_type===2).at(-1);
          document.querySelector('#thread').innerHTML='<div class="assistant"></div>';
          document.querySelector('.assistant').textContent=a.content_block.at(-1).content.text_block.text;}
        document.querySelector('#send').onclick=async()=>{const input=document.querySelector('#prompt');
          const response=await fetch('/fixture/send',{method:'POST',body:input.value});input.value='';
          document.querySelector('.assistant').textContent=await response.text();};show();
        </script>'''
        async def route(r):
            if '/im/chain/single' in r.request.url:
                await r.fulfill(content_type='application/json',body=json.dumps(wire(*rows)))
            elif '/fixture/send' in r.request.url:
                text=r.request.post_data;sent.append(text)
                rows.append(record(text,'user'+str(len(sent)),3 if len(sent)==1 else 5,user_type=1))
                answer=record(lost if len(sent)==1 else '影片生成已提交','reply'+str(len(sent)),
                              4 if len(sent)==1 else 6,execution=execution and len(sent)==2)
                if len(sent)==2:answer['ext']['chat_ability']='{}'
                rows.append(answer)
                await r.fulfill(body=lost if len(sent)==1 else '视频生成中')
            else:await r.fulfill(content_type='text/html',body=HTML+script)
        await self.page.unroute('**/*');await self.page.route('**/*',route)
        self.page.on('response',self.session._on_response)
        for key in ('confirmation_sent','confirmation_sent_at','confirmation_message_id'):
            self.snapshot.pop(key,None)
        clock=SimpleNamespace(value=0)
        class FastAsyncio:
            def __getattr__(self,key):return getattr(asyncio,key)
            async def sleep(self,seconds):
                clock.value+=seconds
                await asyncio.sleep(.05)
        timer=SimpleNamespace(time=lambda:self.now+clock.value,monotonic=lambda:clock.value)
        with patch.object(runtime,'time',timer),patch.object(runtime,'asyncio',FastAsyncio()):
            result=await self.session._verify_execution_once(self.snapshot,copy.deepcopy(self.cap))
        return sent,result

    async def test_reload_confirmation_then_lost_context_reloads_and_nudges_once(self):
        sent,result=await self.refresh_sequence()
        self.assertEqual(sent,[CONFIRM_TEXT,'你倒是生成啊'])
        self.assertTrue(result['generation_evidence']['confirmed'])
        self.assertEqual(result['phase'],'accepted')
        self.assertTrue(self.snapshot['nudge_sent'])

    async def test_refresh_stops_after_one_nudge_even_when_every_reply_is_plain_text(self):
        sent,result=await self.refresh_sequence(execution=False)
        self.assertEqual(sent,[CONFIRM_TEXT,'你倒是生成啊'])
        self.assertFalse(result['generation_evidence']['confirmed'])
        self.assertEqual(self.session.execution_refresh_stages,{'submission','confirmation','nudge'})
        self.assertIsNone(await self.session._verify_execution_once(self.snapshot,self.cap))

    async def test_execution_refresh_preserves_manual_draft_without_reloading(self):
        await self.page.locator('#prompt').fill('用户正在修改原会话')
        result=await self.session._verify_execution_once(self.snapshot,self.cap)
        self.assertIsNone(result)
        self.assertEqual(await self.page.locator('#prompt').input_value(),'用户正在修改原会话')


if __name__ == '__main__': unittest.main()
