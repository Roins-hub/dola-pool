"""Native message evidence, including the observed text-only acknowledgement."""
import copy
import json
import unittest
import hashlib
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from patchright.async_api import async_playwright
from providers.doubao.runtime import BrowserSession
from test_doubao_ordinary_dialog_runtime import CAP, HTML, REQUEST

ACK='本次使用 Seedance 2.0 Fast 生成，预计等待5分钟。视频生成好后，我会主动发送给你。'
REPORTED='已受理，正在按你的原始提示词，使用 Seedance 2.0 Fast 生成 15 秒、16:9 视频。'
PARAMETER_ACK='我已受理并开始生成视频。\n视频生成参数确认\n模型：Seedance 2.0 Fast\n时长：15 秒\n比例：16:9 横屏'

def message(text=ACK, *, mid='new', conversation='current', user_type=2, execution=False):
    m={'conversation_id':conversation,'message_id':mid,'index_in_conv':2,'user_type':user_type,
       'content_type':9999,'ext':{'is_finish':'1','chat_ability':'{}'},
       'content_block':[{'block_type':10000,'is_finish':True,'content':{'text_block':{'text':text}}}]}
    if execution:
        m['ext'].update(use_creation='1',chat_ability=json.dumps({'ability_type':17,'ability_param':json.dumps(
            {'model':'seedance_v2.0','duration':15,'ratio':'16:9'})}))
        m['content_block'].insert(0,{'block_type':10040,'is_finish':True,
            'content':{'thinking_block':{'finish_title':'影片生成已提交','streaming_title':'影片生成中'}}})
    return m

def wire(*messages):
    return {'downlink_body':{'pull_singe_chain_downlink_body':{'messages':list(messages)}}}

class ExecutionEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver=await async_playwright().start();self.addAsyncCleanup(self.driver.stop)
        self.browser=await self.driver.chromium.launch(headless=True);self.addAsyncCleanup(self.browser.close)
        self.page=await self.browser.new_page()
        await self.page.route('**/*',lambda r:r.fulfill(status=200,content_type='text/html',body=HTML))
        await self.page.goto('https://www.doubao.com/chat/current')
        self.session=BrowserSession(None,{})
        self.session.page,self.session.context=self.page,self.page.context
        self.snapshot={'conversation_id':'current','sent':True,'request':dict(REQUEST),
                       'baseline_messages':[],'baseline_texts':[]}

    async def observe(self,*messages):
        self.session.payloads=[wire(*messages)]
        text=messages[0]['content_block'][-1]['content']['text_block']['text']
        await self.page.locator('#thread').evaluate('(n,t)=>{n.innerHTML="<div class=assistant></div>";n.firstChild.textContent=t}',text)
        return await self.session.observe(self.snapshot,copy.deepcopy(CAP),timeout=0)

    async def test_observed_fake_and_convincing_eta_do_not_become_accepted(self):
        for text in [REPORTED,ACK,'已开始生成 Seedance 2.0 Fast 视频，预计5分钟，完成后通知你。']:
            with self.subTest(text=text):
                result=await self.observe(message(text))
                self.assertEqual(result['phase'],'text_only_ack')
                self.assertFalse(result.get('generation_evidence',{}).get('confirmed'))

    async def test_native_creation_submission_confirms_matching_request(self):
        result=await self.observe(message(execution=True))
        self.assertEqual(result['phase'],'accepted')
        self.assertTrue(result['generation_evidence']['confirmed'])

    async def test_old_other_conversation_and_user_echo_are_not_execution(self):
        for extra in [message(execution=True,mid='old'),message(execution=True,conversation='other'),
                      message(execution=True,user_type=1)]:
            self.snapshot['baseline_messages']=['old']
            result=await self.observe(message(),extra)
            self.assertEqual(result['phase'],'text_only_ack')

    async def test_native_duration_mismatch_is_not_matching_acceptance(self):
        m=message(execution=True)
        ability=json.loads(m['ext']['chat_ability']);params=json.loads(ability['ability_param'])
        params['duration']=10;ability['ability_param']=json.dumps(params);m['ext']['chat_ability']=json.dumps(ability)
        result=await self.observe(m)
        self.assertEqual(result['phase'],'request_mismatch')

    async def test_dom_only_ack_is_unknown_not_a_proven_fake_or_acceptance(self):
        await self.page.locator('#thread').evaluate('(n,t)=>{n.innerHTML="<div class=assistant></div>";n.firstChild.textContent=t}',ACK)
        result=await self.session.observe(self.snapshot,copy.deepcopy(CAP),timeout=0)
        self.assertEqual(result['phase'],'unknown')

    async def test_readonly_reload_checks_original_chain_without_sending(self):
        self.page.on('response', self.session._on_response)
        seen=[]
        async def route(r):
            seen.append(r.request.url)
            if '/im/chain/single' in r.request.url:
                await r.fulfill(content_type='application/json',body=json.dumps(wire(message())))
            else:
                await r.fulfill(content_type='text/html',body=HTML+'<div class="assistant">'+ACK+'</div><script>fetch("/im/chain/single")</script>')
        await self.page.unroute('**/*')
        await self.page.route('**/*',route)
        result=await self.session._verify_execution_once(self.snapshot,copy.deepcopy(CAP))
        self.assertEqual(result['phase'],'text_only_ack')
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')
        self.assertEqual(len([url for url in seen if '/chat/current' in url]),1)

    async def test_explicit_no_generation_tool_reply_ends_without_a_native_record(self):
        text='很抱歉，我这边没有可以直接调用的视频生成工具，无法帮你生成这条视频。你可以前往支持 Seedance 2.0 Fast 的生成平台。'
        await self.page.locator('#thread').evaluate('(n,t)=>{n.innerHTML="<div class=assistant></div>";n.firstChild.textContent=t}',text)
        result=await self.session.observe(self.snapshot,copy.deepcopy(CAP),timeout=0)
        self.assertEqual(result['phase'],'rejected')
        self.assertFalse(result['no_charge'])

    async def test_hypothetical_tool_failure_is_not_an_explicit_rejection(self):
        from providers.doubao.runtime import receipt_state
        self.assertEqual(receipt_state('如果我这边没有可以直接调用的视频生成工具，会告诉你。',CAP),'unknown')

    async def test_opted_in_native_trial_also_requires_execution_proof(self):
        cap={**CAP,'entry_mode':'native_controls','require_native_execution':True}
        self.session.payloads=[wire(message(REPORTED))]
        result=await self.session.observe(self.snapshot,cap,timeout=0)
        self.assertEqual(result['phase'],'text_only_ack')
        self.session.payloads=[wire(message(execution=True))]
        result=await self.session.observe(self.snapshot,cap,timeout=0)
        self.assertEqual(result['phase'],'accepted')

    async def test_native_parameters_echo_without_invocation_is_still_text_only(self):
        m=message(REPORTED,execution=True)
        del m['ext']['use_creation']
        m['content_block']=m['content_block'][1:]
        result=await self.observe(m)
        self.assertEqual(result['phase'],'text_only_ack')
        proof=result['generation_evidence']
        self.assertFalse(proof['confirmed'])
        self.assertTrue(proof['request_parameters_echoed'])
        self.assertIsNone(proof['parameters_match'])
        self.assertEqual(proof['echoed_parameters'],[{'model':'seedance_v2.0','duration':15,'ratio':'16:9'}])

    def enable_confirmation(self,text=PARAMETER_ACK):
        folder=tempfile.TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.session.provider=SimpleNamespace(_snapshot_path=lambda _:Path(folder.name)/'snapshot.json')
        self.snapshot.update(id='video_'+'e'*32,created_at=time.time()-60,
            confirmation_question={'hash':hashlib.sha256(text.encode()).hexdigest(),'since':time.time()-5})

    async def test_parameter_only_ack_gets_exactly_one_confirm_then_waits(self):
        self.enable_confirmation()
        result=await self.observe(message(PARAMETER_ACK))
        self.assertEqual(await self.page.locator('#prompt').input_value(),
                         '直接生成视频 禁止问我确认 直接开始 强制执行 不需要问我二次确认 直接生成')
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'1')
        self.assertEqual(result['phase'],'unknown')
        saved=json.loads(self.session.provider._snapshot_path('').read_text())
        self.snapshot=saved
        await self.observe(message(PARAMETER_ACK))
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'1')

    async def test_native_ui_confirmation_requires_same_complete_spec_and_no_job(self):
        self.enable_confirmation()
        cap={**CAP,'entry_mode':'native_controls','require_native_execution':True}
        for text in (PARAMETER_ACK.replace('15 秒','10 秒'),PARAMETER_ACK.replace('16:9','9:16'),
                     PARAMETER_ACK.replace('2.0 Fast','2.5'),PARAMETER_ACK+'\n需要升级付费后才能生成'):
            self.snapshot['confirmation_question']={'hash':hashlib.sha256(text.encode()).hexdigest(),'since':time.time()-5}
            self.session.payloads=[wire(message(text))]
            await self.session.observe(self.snapshot,cap,timeout=0)
            self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')
        self.session.payloads=[wire(message(PARAMETER_ACK,execution=True))]
        await self.session.observe(self.snapshot,cap,timeout=0)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')

    async def test_manual_answer_after_parameter_ack_prevents_auto_confirmation(self):
        self.enable_confirmation()
        question=message(PARAMETER_ACK);question['index_in_conv']='3'
        answer=message('确定生成',mid='human-answer',user_type=1);answer['index_in_conv']='6'
        self.session.payloads=[wire(question,answer)]
        await self.session.observe(self.snapshot,copy.deepcopy(CAP),timeout=0)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')
        repeated=message(PARAMETER_ACK,mid='later-ack');repeated['index_in_conv']='9'
        self.session.payloads=[wire(question,answer,repeated)]
        await self.session.observe(self.snapshot,copy.deepcopy(CAP),timeout=0)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')

class NativeRecordTests(unittest.TestCase):
    def proof(self, *records):
        from providers.doubao.execution_evidence import native_execution_evidence
        return native_execution_evidence([wire(*records)], 'current', set(), REQUEST)[0]

    def test_malformed_records_do_not_crash_or_confirm(self):
        from providers.doubao.execution_evidence import native_execution_evidence
        for value in [None, '[]', 'null', 12, [], {'messages':None}]:
            with self.subTest(value=value):
                proof,_=native_execution_evidence([{'downlink_body':value}], 'current', set(), REQUEST)
                self.assertFalse(proof['confirmed'])
                m=message();m['ext'].update(chat_ability=value,ai_creation_tool_list=value)
                m['content_block']=[{'content':value}]
                self.assertFalse(self.proof(m)['confirmed'])

    def test_native_job_id_confirms_but_failed_job_does_not(self):
        m=message();job={'task_id':12345,'tool_name':'text_to_video','fail_code':0}
        m['ext']['ai_creation_tool_list']=json.dumps([job])
        self.assertEqual(self.proof(m)['task_ids'],['12345'])
        job['fail_code']=1;m['ext']['ai_creation_tool_list']=json.dumps([job])
        self.assertFalse(self.proof(m)['confirmed'])

    def test_actual_native_fast_job_verifies_model_without_inventing_duration(self):
        m=message()
        m['ext']['ai_creation_tool_list']=json.dumps([{'task_id':12345,'tool_name':'text_to_video',
            'fail_code':0,'req_key':'seedance_v20_fast_flow'}])
        proof=self.proof(m)
        self.assertTrue(proof.get('model_matches'))
        self.assertIsNone(proof['parameters_match'])
        from providers.doubao.execution_evidence import native_execution_evidence
        other,_=native_execution_evidence([wire(m)],'current',set(),{**REQUEST,'model':'dialog_seedance_2.5'})
        self.assertFalse(other.get('model_matches'))

    def test_native_creation_confirms_without_using_chat_prose(self):
        m=message();m['content_block']=[{'content':{'creation_block':{'creations':[
            {'type':2,'id':123,'video':{'vid':'fixture'}}]}}}]
        self.assertTrue(self.proof(m)['confirmed'])

    def test_native_video_submission_after_confirmation_may_omit_ability(self):
        m=message(execution=True)
        m['ext']['chat_ability']='{}'
        proof=self.proof(m)
        self.assertTrue(proof['confirmed'])
        self.assertEqual(proof['kinds'],['native_submission'])
        self.assertIsNone(proof['parameters_match'])

    def test_missing_ability_does_not_promote_an_earlier_parameter_echo(self):
        echo=message(mid='echo',execution=True)
        echo['content_block']=echo['content_block'][1:]
        actual=message(mid='actual',execution=True)
        actual['ext']['chat_ability']='{}'
        proof=self.proof(echo,actual)
        self.assertTrue(proof['confirmed'])
        self.assertEqual(proof['message_ids'],['actual'])
        self.assertIsNone(proof['parameters_match'])

    def test_untyped_submission_still_requires_native_flag_and_video_title(self):
        for change in ('no_flag','no_title','different_ability'):
            with self.subTest(change=change):
                m=message(execution=True)
                m['ext']['chat_ability']='{}'
                if change=='no_flag':m['ext'].pop('use_creation')
                elif change=='no_title':m['content_block']=m['content_block'][1:]
                else:m['ext']['chat_ability']='{"ability_type": 2}'
                self.assertFalse(self.proof(m)['confirmed'])

if __name__=='__main__':unittest.main()
