from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import hashlib
import time

from patchright.async_api import async_playwright
from providers.doubao.runtime import BrowserSession, chat_identity


class CompletionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_unacknowledged_local_page_is_not_a_server_conversation(self):
        self.assertIsNone(chat_identity('https://www.doubao.com/chat/local_8021561618233136'))
        self.assertEqual(chat_identity('https://www.doubao.com/chat/acknowledged-123'), 'acknowledged-123')

    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.driver=await async_playwright().start();self.addAsyncCleanup(self.driver.stop)
        self.context=await self.driver.chromium.launch_persistent_context(str(Path(self.temp.name)/'profile'),headless=True)
        self.addAsyncCleanup(self.context.close)
        self.page=await self.context.new_page()
        await self.page.route('**/*',lambda r:r.fulfill(content_type='text/html',body='<div class="assistant"></div>'))
        await self.page.goto('https://www.doubao.com/chat/local_fixture')
        self.session=BrowserSession(None,{'name':'fixture'})
        self.session.context,self.session.page=self.context,self.page
        self.cap={'selectors':{'assistant':'.assistant'},'receipt_patterns':['视频生成中'],'rejection_patterns':['无法生成']}

    async def error_response(self):
        response=SimpleNamespace(url='https://www.doubao.com/chat/completion',text=AsyncMock(return_value=
            'event: STREAM_ERROR\ndata: {"error_code":710022002,"error_msg":"native rejection"}\n\n'))
        self.session._on_response(response)
        await __import__('asyncio').gather(*list(self.session.pending))

    async def test_native_completion_rejection_is_not_left_waiting_for_a_receipt(self):
        await self.error_response()
        result=await self.session.observe({'baseline_messages':[],'baseline_media':[]},self.cap,.01)
        self.assertEqual(result['phase'],'rejected')
        self.assertEqual(result['error_code'],'completion_710022002')
        self.assertFalse(result['no_charge'])

    async def test_utf8_chinese_error_reaches_task_reply_without_mojibake_or_billing_change(self):
        import json
        import asyncio
        expected='当前服务访问频繁，请稍后重试。'
        wrong=expected.encode('utf-8').decode('latin1')
        raw='event:STREAM_ERROR\ndata:'+json.dumps(
            {'error_code':710022002,'error_msg':wrong},ensure_ascii=False)+'\n\n'
        response=SimpleNamespace(url='https://www.doubao.com/chat/completion',
                                 text=AsyncMock(return_value=raw))
        self.session._on_response(response)
        await asyncio.gather(*list(self.session.pending))
        result=await self.session.observe({'baseline_messages':[],'baseline_media':[]},self.cap,.01)
        self.assertEqual(result['phase'],'rejected')
        self.assertEqual(result['platform_reply'],expected)
        self.assertEqual(result['error_code'],'completion_710022002')
        self.assertFalse(result['no_charge'])
        self.assertEqual(self.session.payloads[0]['error_msg'],wrong)

    async def test_trusted_accepted_message_is_not_overridden_by_a_generic_transport_error(self):
        await self.page.goto('https://www.doubao.com/chat/server-fixture')
        await self.page.locator('.assistant').evaluate('(n)=>n.innerText="视频生成中"')
        await self.error_response()
        result=await self.session.observe({'baseline_messages':[],'baseline_media':[]},self.cap,.01)
        self.assertEqual(result['phase'],'accepted')
        self.assertEqual(result['conversation_id'],'server-fixture')

    async def test_opted_in_native_confirmation_has_a_durable_single_send_fence(self):
        await self.page.goto('https://www.doubao.com/chat/server-fixture')
        await self.page.locator('.assistant').evaluate('(n)=>n.innerText="请确认生成视频"')
        await self.page.evaluate('''()=>{const input=document.createElement('textarea');input.id='composer';document.body.append(input);
          const send=document.createElement('button');send.id='send';send.onclick=()=>window.sends=(window.sends||0)+1;document.body.append(send)}''',isolated_context=False)
        self.session.provider=SimpleNamespace(_snapshot_path=lambda _:Path(self.temp.name)/'snapshot.json')
        cap={**self.cap,'selectors':{**self.cap['selectors'],'composer':'#composer','send':'#send'}}
        snapshot={'id':'fixture','created_at':time.time()-15,'request':{'auto_confirm':True},
            'baseline_messages':[],'baseline_media':[],
            'confirmation_question':{'hash':hashlib.sha256('请确认生成视频'.encode()).hexdigest(),'since':time.time()-5}}
        await self.session.observe(snapshot,cap,.01)
        await self.session.observe(snapshot,cap,.01)
        self.assertEqual(await self.page.evaluate('()=>window.sends',isolated_context=False),1)
        import json
        self.assertTrue(json.loads((Path(self.temp.name)/'snapshot.json').read_text())['confirmation_sent'])


if __name__=='__main__':unittest.main()
