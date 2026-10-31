"""Ordinary chat generation and completed-card recovery, entirely offline."""
import copy
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest

from patchright.async_api import async_playwright
from providers.base import ProviderError
from providers.doubao.runtime import BrowserSession, VIDEO_REQUEST_SUFFIX, receipt_state
from providers.doubao.confirmation import confirmation_due, ordinary_dialog_spec_matches


CAP = {'id':'dialog_seedance_2.0_fast', 'label':'Seedance 2.0 Fast（普通对话）',
       'entry_mode':'ordinary_dialog',
       'selectors':{'composer':'#prompt', 'send':'#send', 'assistant':'.assistant', 'new_chat':'#new'},
       'receipt_patterns':['视频生成中','开始生成'], 'rejection_patterns':['无法生成','生成失败']}
REQUEST = {'entry_mode':'ordinary_dialog','model':'dialog_seedance_2.0_fast',
           'duration':15,'ratio':'16:9','prompt':'一只小猫跳舞\n保留第二行。','lead_prompt':'前置说明',
           'auto_confirm':True}
QUESTION = '请先确认视频生成参数：模型 Seedance 2.0 Fast，时长15秒，比例16:9，确认后开始生成视频。'
HTML = '''<!doctype html><meta charset="utf-8"><body data-sent="0" data-played="0">
<button id="new">新对话</button><textarea id="prompt"></textarea><button id="send">发送</button>
<button data-testid="chat_input_action_mode">对话</button>
<div id="thread"></div><script>
newButton=document.querySelector('#new');newButton.onclick=()=>{
 history.pushState({},'', '/chat/');document.querySelector('#thread').innerHTML='';};
document.querySelector('#send').onclick=()=>{document.body.dataset.sent=String(+document.body.dataset.sent+1);
 history.pushState({},'', '/chat/current');};
function play(){document.body.dataset.played=String(+document.body.dataset.played+1);
 const video=document.createElement('video');video.src='https://cdn.example/current.mp4';document.body.append(video);}
</script>'''


class DialogConfirmationTests(unittest.TestCase):
    def state(self): return {'created_at':100,'request':dict(REQUEST)}

    def test_only_same_explicit_model_duration_ratio_question_can_be_confirmed(self):
        for text in (QUESTION.replace('15秒','10秒'), QUESTION.replace('16:9','9:16'),
                     QUESTION.replace('15秒','30 seconds'), QUESTION.replace('15秒','30 secs'),
                     QUESTION.replace('Seedance 2.0 Fast','Seedance 2.5'),
                     '请确认生成视频？', QUESTION+' 也可回复 A/B/C 选择5秒、10秒或15秒。'):
            state=self.state()
            confirmation_due(state,text,now=111)
            self.assertFalse(confirmation_due(state,text,now=115), text)
        state=self.state()
        self.assertFalse(confirmation_due(state,QUESTION,now=111))
        self.assertTrue(confirmation_due(state,QUESTION,now=115))
        state['confirmation_sent']=True
        self.assertFalse(confirmation_due(state,QUESTION,now=120))

    def test_ordinary_acknowledgement_requires_matching_actual_model(self):
        for text in ('视频生成中', '已使用 Seedance 2.5 生成视频，预计5分钟，完成后通知你。',
                     '本次使用 Seedance 2.0 生成视频，预计5分钟，完成后通知你。'):
            self.assertEqual(receipt_state(text,CAP),'unknown',text)
        self.assertEqual(receipt_state('本次使用 Seedance 2.0 Fast 生成，预计等待10分钟。视频生成好后，我会主动发送给你。',CAP),'accepted')

    def test_actual_english_seconds_cannot_override_requested_duration(self):
        for unit in ('seconds','second','secs','sec','s'):
            self.assertFalse(ordinary_dialog_spec_matches('Seedance 2.0 Fast, 30 '+unit+', 16:9',REQUEST,require_all=False),unit)
            self.assertTrue(ordinary_dialog_spec_matches('Seedance 2.0 Fast, 15 '+unit+', 16:9',REQUEST,require_all=False),unit)

    def test_25_receipt_requires_exact_model_and_real_thirty_second_spec(self):
        request={**REQUEST,'model':'dialog_seedance_2.5','duration':30}
        self.assertTrue(ordinary_dialog_spec_matches('Seedance 2.5，30秒，16:9',request))
        for text in ('Seedance 2.0 Fast，30秒，16:9','Seedance 2.0，30秒，16:9',
                     'Seedance 2.5 Fast，30秒，16:9','Seedance 2.5 Mini，30秒，16:9',
                     'Seedance 2.5 和 Seedance 2.0 Fast，30秒，16:9',
                     'Seedance 2.5，15秒，16:9','Seedance 2.5，30秒，9:16','视频生成中'):
            with self.subTest(text=text):
                self.assertFalse(ordinary_dialog_spec_matches(text,request))
        cap={**CAP,'id':'dialog_seedance_2.5'}
        self.assertEqual(receipt_state('本次使用 Seedance 2.5 生成，预计15分钟，生成好后发送给你。',cap),'accepted')
        self.assertEqual(receipt_state('本次使用 Seedance 2.0 Fast 生成，预计15分钟，生成好后发送给你。',cap),'unknown')


class DialogBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.driver=await async_playwright().start();self.addAsyncCleanup(self.driver.stop)
        self.context=await self.driver.chromium.launch_persistent_context(str(Path(self.temp.name)/'browser'),headless=True)
        self.addAsyncCleanup(self.context.close)
        await self.context.route('**/*', lambda route:route.fulfill(status=200,content_type='text/html',body=HTML))
        self.page=await self.context.new_page();await self.page.goto('https://www.doubao.com/chat/old')
        self.path=Path(self.temp.name)/'snapshot.json'
        provider=SimpleNamespace(_snapshot_path=lambda task:self.path)
        self.session=BrowserSession(provider,{'name':'fixture'})
        self.session.context,self.session.page=self.context,self.page

    async def add_card(self,mid='fresh',text='你的视频生成好了。',play=True):
        await self.page.evaluate('''({mid,text,play})=>{const block=document.createElement('div');block.className='assistant';
          const inner=document.createElement('div');inner.dataset.messageId=mid;inner.textContent=text;
          if(play){const b=document.createElement('button');b.className='play-icon-wrapper-fixture';b.onclick=window.play;inner.append(b)}
          block.append(inner);document.querySelector('#thread').append(block)}''', {'mid':mid,'text':text,'play':play},isolated_context=False)

    async def test_new_dialog_preserves_lead_body_specs_suffix_and_sends_once(self):
        await self.add_card('old')
        self.session.payloads=[{'conversation_id':'old'}]
        await self.session.prepare(dict(REQUEST),copy.deepcopy(CAP),[])
        self.assertEqual(self.page.url,'https://www.doubao.com/chat/')
        self.assertEqual(await self.page.locator('.assistant').count(),0)
        self.assertEqual(self.session.payloads,[])
        prompt=await self.page.locator('#prompt').input_value()
        self.assertTrue(prompt.endswith('\n'+VIDEO_REQUEST_SUFFIX))
        self.assertNotIn('我给的提示词在后面禁止修改用户提示词',prompt)
        self.assertNotIn('接下来我需要制作视频，郑重声明',prompt)
        self.assertIn('前置说明\n一只小猫跳舞\n保留第二行。',prompt)
        self.assertIn('请使用Seedance 2.0 Fast生成一条15秒、16:9的视频。',prompt)
        self.assertNotIn('dialog_seedance',prompt)
        await self.session.submit()
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'1')

    async def test_nonempty_composer_stops_before_send(self):
        await self.page.locator('#prompt').fill('unfinished draft')
        with self.assertRaises(ProviderError):
            await self.session.prepare(dict(REQUEST),copy.deepcopy(CAP),[])
        self.assertEqual(await self.page.locator('#prompt').input_value(),'unfinished draft')
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')

    async def test_25_ordinary_prompt_sends_real_thirty_seconds_once(self):
        request={**REQUEST,'model':'dialog_seedance_2.5','duration':30}
        cap={**copy.deepcopy(CAP),'id':'dialog_seedance_2.5','label':'Seedance 2.5（30秒试验）'}
        await self.session.prepare(request,cap,[])
        prompt=await self.page.locator('#prompt').input_value()
        self.assertIn('请使用Seedance 2.5生成一条30秒、16:9的视频。',prompt)
        self.assertTrue(prompt.endswith('\n'+VIDEO_REQUEST_SUFFIX))
        self.assertNotIn('我给的提示词在后面禁止修改用户提示词',prompt)
        self.assertNotIn('接下来我需要制作视频，郑重声明',prompt)
        self.assertNotIn('Seedance 2.0 Fast',prompt)
        self.assertNotIn('试验',prompt)
        await self.session.submit()
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'1')

    async def test_25_later_fast_acknowledgement_clears_model_verification(self):
        await self.page.goto('https://www.doubao.com/chat/current')
        request={**REQUEST,'model':'dialog_seedance_2.5','duration':30}
        cap={**copy.deepcopy(CAP),'id':'dialog_seedance_2.5'}
        await self.add_card('ack1','本次使用 Seedance 2.5 生成，预计15分钟，生成好后发送给你。',play=False)
        await self.add_card('ack2','本次使用 Seedance 2.0 Fast 生成，预计10分钟，生成好后发送给你。',play=False)
        result=await self.session.observe({'conversation_id':'current','sent':True,'request':request},cap,timeout=0)
        self.assertEqual(result['phase'],'unknown')
        self.assertFalse(result['dialog_model_verified'])

    async def test_existing_specs_in_body_do_not_drop_lead_prompt(self):
        request={**REQUEST,'prompt':'小猫跳舞\n【视频生成规格】请使用Seedance 2.0 Fast生成一条15秒、16:9的视频。'}
        await self.session.prepare(request,copy.deepcopy(CAP),[])
        self.assertIn('前置说明\n小猫跳舞\n',await self.page.locator('#prompt').input_value())

    async def test_exact_parameter_question_is_confirmed_once_with_durable_fence(self):
        from providers.doubao.confirmation import CONFIRM_TEXT
        await self.page.goto('https://www.doubao.com/chat/current')
        await self.add_card('question',QUESTION,play=False)
        snapshot={'id':'video_'+'b'*32,'conversation_id':'current','sent':True,
                  'created_at':time.time()-60,'request':dict(REQUEST)}
        self.assertFalse(confirmation_due(snapshot,QUESTION,now=time.time()-4))
        await self.session.observe(snapshot,copy.deepcopy(CAP),timeout=0)
        self.assertEqual(await self.page.locator('#prompt').input_value(),CONFIRM_TEXT)
        self.assertTrue(json.loads(self.path.read_text())['confirmation_sent'])
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'1')
        await self.session.observe(snapshot,copy.deepcopy(CAP),timeout=0)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'1')

    async def test_wrong_or_missing_ordinary_dialog_mode_stops_before_send(self):
        await self.page.locator('[data-testid="chat_input_action_mode"]').evaluate('(n)=>n.textContent="创作"')
        with self.assertRaises(ProviderError):
            await self.session.prepare(dict(REQUEST),copy.deepcopy(CAP),[])
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')
        await self.page.locator('[data-testid="chat_input_action_mode"]').evaluate('(n)=>n.remove()')
        with self.assertRaises(ProviderError):
            await self.session.prepare(dict(REQUEST),copy.deepcopy(CAP),[])
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')

    async def test_one_fresh_completed_card_opens_native_player_and_associates_video(self):
        await self.page.goto('https://www.doubao.com/chat/current')
        await self.add_card('old');await self.add_card('fresh')
        snapshot={'id':'video_'+'a'*32,'conversation_id':'current','sent':True,
                  'baseline_messages':['old'],'baseline_media':[], 'request':dict(REQUEST)}
        result=await self.session.observe(snapshot,copy.deepcopy(CAP),timeout=0)
        self.assertEqual(result['phase'],'media_ready')
        self.assertEqual(result['media'],['https://cdn.example/current.mp4'])
        self.assertEqual(result['media_metadata'][result['media'][0]]['message_id'],'fresh')
        self.assertEqual(result['media_metadata'][result['media'][0]]['conversation_id'],'current')
        self.assertEqual(await self.page.locator('body').get_attribute('data-played'),'1')
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')
        await self.session.observe(snapshot,copy.deepcopy(CAP),timeout=0)
        self.assertEqual(await self.page.locator('body').get_attribute('data-played'),'1')

    async def test_old_card_or_text_only_never_becomes_a_video(self):
        await self.page.goto('https://www.doubao.com/chat/current')
        await self.add_card('old');await self.add_card('fresh',play=False)
        snapshot={'conversation_id':'current','sent':True,'baseline_messages':['old'],'request':dict(REQUEST)}
        result=await self.session.observe(snapshot,copy.deepcopy(CAP),timeout=0)
        self.assertEqual(result['media'],[])
        self.assertNotEqual(result['phase'],'media_ready')
        self.assertEqual(await self.page.locator('body').get_attribute('data-played'),'0')

    async def test_traditional_completed_video_card_opens_its_player(self):
        await self.page.goto('https://www.doubao.com/chat/current')
        await self.add_card('fresh','影片生成完畢。')
        result=await self.session.observe({'conversation_id':'current','sent':True,
            'baseline_messages':[],'request':dict(REQUEST)},copy.deepcopy(CAP),timeout=0)
        self.assertEqual(result['phase'],'media_ready')
        self.assertEqual(result['media'],['https://cdn.example/current.mp4'])
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')

    async def test_ambiguous_fresh_completed_cards_are_not_opened(self):
        await self.page.goto('https://www.doubao.com/chat/current')
        await self.add_card('fresh1');await self.add_card('fresh2')
        result=await self.session.observe({'conversation_id':'current','sent':True,'request':dict(REQUEST)},copy.deepcopy(CAP),timeout=0)
        self.assertEqual(result['media'],[])
        self.assertEqual(await self.page.locator('body').get_attribute('data-played'),'0')

    async def test_later_wrong_model_acknowledgement_invalidates_earlier_matching_ack(self):
        await self.page.goto('https://www.doubao.com/chat/current')
        await self.add_card('ack1','本次使用 Seedance 2.0 Fast 生成，预计10分钟，生成好后发送给你。',play=False)
        await self.add_card('ack2','本次使用 Seedance 2.5 生成，预计5分钟，生成好后发送给你。',play=False)
        result=await self.session.observe({'conversation_id':'current','sent':True,'request':dict(REQUEST)},copy.deepcopy(CAP),timeout=0)
        self.assertEqual(result['phase'],'unknown')
        self.assertFalse(result['dialog_model_verified'])

    async def test_unknown_new_submission_still_persists_its_real_native_conversation(self):
        await self.page.goto('https://www.doubao.com/chat/current')
        snapshot={'id':'video_'+'c'*32,'sent':True,'created_at':time.time(),
                  'request':dict(REQUEST),'submission_page_url':'https://www.doubao.com/chat/'}
        result=await self.session.observe(snapshot,copy.deepcopy(CAP),timeout=0)
        self.assertEqual(result['phase'],'unknown')
        self.assertEqual(result['conversation_id'],'current')
        self.assertEqual(json.loads(self.path.read_text())['conversation_id'],'current')
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')

    async def test_recovery_missing_conversation_never_binds_arbitrary_native_page(self):
        await self.page.goto('https://www.doubao.com/chat/current')
        snapshot={'id':'video_'+'d'*32,'sent':True,'request':dict(REQUEST)}
        self.session.snapshot=snapshot
        result=await self.session.observe(snapshot,copy.deepcopy(CAP),timeout=0)
        self.assertEqual(result['phase'],'unknown')
        self.assertEqual(result['reason'],'original_chat_unknown')
        self.assertIsNone(result['conversation_id'])
        self.assertFalse(self.path.exists())


if __name__=='__main__':unittest.main()
