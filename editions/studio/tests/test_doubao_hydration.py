"""Native authentication and controls may hydrate after DOMContentLoaded."""
from pathlib import Path
import asyncio
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from patchright.async_api import async_playwright
from providers.base import ProviderError
from providers.doubao.runtime import BrowserSession, HOME


IDENTITY="window._ROUTER_DATA={loaderData:{chat_layout:{chat_layout:{accountInfo:{data:{user_id:'official-fixture'}}}}}};"


class NativeHydrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.driver=await async_playwright().start();self.addAsyncCleanup(self.driver.stop)
        self.context=await self.driver.chromium.launch_persistent_context(str(Path(self.temp.name)/'browser'),headless=True)
        self.addAsyncCleanup(self.context.close)
        self.html='<html><body></body></html>'
        await self.context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=self.html))
        self.page=await self.context.new_page();await self.page.goto(HOME)
        await self.context.add_cookies([{'name':'sessionid','value':'fixture','domain':'.doubao.com','path':'/'}])
        response=SimpleNamespace(status=200,json=AsyncMock(return_value={'ip':'8.8.8.8'}),dispose=AsyncMock())
        self.session=BrowserSession(None,{})
        self.session.page=self.page
        self.session.context=SimpleNamespace(cookies=self.context.cookies,request=SimpleNamespace(get=AsyncMock(return_value=response)))

    async def load(self,html):
        self.html='<!doctype html><meta charset="utf-8">'+html
        await self.page.reload(wait_until='domcontentloaded')

    async def test_verify_waits_for_native_composer_hydration_instead_of_saving_false_login(self):
        await self.load('<script>'+IDENTITY+"setTimeout(()=>{const input=document.createElement('textarea');document.body.append(input)},250)</script>")
        self.assertIsNotNone(await self.session.official_user_id())
        self.assertEqual(await self.page.locator('textarea').count(),0)
        result=await self.session.verify(observe_only=True)
        self.assertTrue(result['login_verified'])
        self.assertEqual(result['observed_selectors']['composer'],'textarea')

    async def test_official_logged_in_identity_does_not_require_legacy_avatar_layout(self):
        await self.load('<textarea></textarea><script>'+IDENTITY+'</script>')
        self.assertTrue((await self.session._logged_in())[0])

    async def test_explicit_anonymous_native_identity_overrides_stale_cookie_and_avatar(self):
        await self.load('<textarea></textarea><div data-testid="user-menu">stale avatar</div><script>'+IDENTITY+
                        "window._ROUTER_DATA.loaderData.chat_layout.chat_layout.accountInfo.data.user_id='0';</script>")
        self.assertFalse((await self.session._logged_in())[0])

    async def test_absent_official_identity_retains_existing_legacy_dom_fallback(self):
        await self.load('<textarea></textarea><div data-testid="user-menu">legacy account</div>')
        self.assertTrue((await self.session._logged_in())[0])

    async def test_single_native_control_can_appear_after_dom_content_loaded(self):
        await self.load('<script>setTimeout(()=>{const button=document.createElement("button");button.id="delayed";button.textContent="发送";document.body.append(button)},150)</script>')
        try:control=await self.session._one('#delayed')
        except ProviderError as error:self.fail('A hydrating native control was rejected before it appeared: '+error.code)
        self.assertTrue(await control.is_visible())

    async def test_delayed_hidden_control_waits_until_visible(self):
        await self.load('<button id="delayed" hidden>发送</button><script>setTimeout(()=>document.querySelector("#delayed").hidden=false,150)</script>')
        try:control=await self.session._one('#delayed')
        except ProviderError as error:self.fail('A hydrating hidden control was rejected before it became visible: '+error.code)
        self.assertTrue(await control.is_visible())

    async def test_duplicate_native_controls_are_never_accepted(self):
        await self.load('<button class="send">发送</button><button class="send">发送</button>')
        with self.assertRaises(ProviderError) as caught:await self.session._one('.send')
        self.assertEqual(caught.exception.code,'native_state_changed')

    async def test_missing_or_permanently_hidden_control_times_out_with_provider_error(self):
        await self.load('<button id="hidden" hidden>发送</button>')
        for selector in ('#missing','#hidden'):
            with self.subTest(selector=selector),self.assertRaises(ProviderError) as caught:
                await asyncio.wait_for(self.session._one(selector,timeout=.1),1)
            self.assertEqual(caught.exception.code,'native_state_changed')

    async def test_verify_waits_for_native_identity_hydration_too(self):
        await self.load('<textarea></textarea><script>'+IDENTITY+
                        "const data=window._ROUTER_DATA.loaderData.chat_layout.chat_layout.accountInfo.data;data.user_id='0';setTimeout(()=>data.user_id='official-fixture',200)</script>")
        result=await self.session.verify(observe_only=True)
        self.assertTrue(result['login_verified'])

    async def test_official_identity_without_auth_cookie_is_not_login(self):
        await self.load('<textarea></textarea><script>'+IDENTITY+'</script>')
        await self.context.clear_cookies()
        self.assertFalse((await self.session._logged_in())[0])

    async def test_new_dialog_mode_chip_may_hydrate_after_the_new_chat_click(self):
        import copy
        from test_doubao_ordinary_dialog_runtime import CAP,REQUEST
        await self.load('''<button id="new">新对话</button><textarea id="prompt"></textarea><button id="send">发送</button>
          <script>document.querySelector('#new').onclick=()=>{
            history.pushState({},'', '/chat/');setTimeout(()=>{
              const mode=document.createElement('button');mode.dataset.testid='chat_input_action_mode';
              mode.textContent='对话';document.body.append(mode);
            },200);
          };</script>''')
        try:await self.session.prepare(dict(REQUEST),copy.deepcopy(CAP),[])
        except ProviderError as error:self.fail('The new dialog mode chip was rejected before hydration: '+error.code)
        self.assertEqual(await self.page.locator('[data-testid="chat_input_action_mode"]').inner_text(),'对话')
        self.assertIn(REQUEST['prompt'],await self.page.locator('#prompt').input_value())

    async def test_traditional_dialog_mode_prepares_without_sending(self):
        import copy
        from test_doubao_ordinary_dialog_runtime import CAP, REQUEST, HTML
        await self.load(HTML.replace('>对话<','>  對話  <'))
        await self.session.prepare(dict(REQUEST),copy.deepcopy(CAP),[])
        self.assertIn(REQUEST['prompt'],await self.page.locator('#prompt').input_value())
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')

    async def test_empty_mode_label_may_hydrate_after_its_button(self):
        import copy
        from test_doubao_ordinary_dialog_runtime import CAP, REQUEST, HTML
        await self.load(HTML.replace('>对话<','><')+'''<script>setTimeout(()=>{
          document.querySelector('[data-testid="chat_input_action_mode"]').textContent='對話';
        },400)</script>''')
        await self.session.prepare(dict(REQUEST),copy.deepcopy(CAP),[])
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')

    async def test_send_appears_then_enables_only_after_composer_fill(self):
        import copy
        from test_doubao_ordinary_dialog_runtime import CAP, REQUEST, HTML
        await self.load(HTML+'''<script>
          document.querySelector('#send').remove();
          document.querySelector('#prompt').oninput=()=>{
            if(window.creatingSend||document.querySelector('#send'))return;window.creatingSend=true;
            setTimeout(()=>{const send=document.createElement('button');send.id='send';
              send.disabled=true;document.body.append(send);
              setTimeout(()=>send.disabled=false,300);
            },200);
          };
        </script>''')
        self.assertEqual(await self.page.locator('#send').count(),0)
        await self.session.prepare(dict(REQUEST),copy.deepcopy(CAP),[])
        self.assertTrue(await self.page.locator('#send').is_enabled())

    async def test_input_exists_before_it_is_editable(self):
        import copy
        from test_doubao_ordinary_dialog_runtime import CAP, REQUEST, HTML
        await self.load(HTML+'''<script>
          const input=document.querySelector('#prompt');input.readOnly=true;
          setTimeout(()=>input.readOnly=false,400);
        </script>''')
        await self.session.prepare(dict(REQUEST),copy.deepcopy(CAP),[])
        self.assertIn(REQUEST['prompt'],await self.page.locator('#prompt').input_value())


    async def test_observed_sidebar_identity_excludes_history_titles_in_both_languages(self):
        import copy
        from test_doubao_ordinary_dialog_runtime import CAP, REQUEST
        for label in ('新对话','新對話'):
            await self.load('''<div data-testid="flow_chat_sidebar">
              <div data-testid="create_conversation_button"><div role="button"><span>LABEL</span></div></div>
              <div data-testid="conversation-list-v2-item"><span>LABEL</span></div>
            </div><textarea data-testid="chat_input_input"></textarea><div id="thread"></div>
            <script>document.querySelector('[role="button"]').onclick=()=>{
              document.querySelector('textarea').remove();
              document.querySelector('#thread').innerHTML='<div data-testid="chat_input"><div class="ProseMirror" contenteditable="true"></div><button data-testid="chat_input_action_mode">對話</button><button id="send" onclick="document.body.dataset.sent=1">send</button></div>';
            };</script>'''.replace('LABEL',label))
            cap=copy.deepcopy(CAP)
            cap['selectors'].update(new_chat='[data-testid="flow_chat_sidebar"] [data-testid="create_conversation_button"] [role="button"]',
              composer='[data-testid="chat_input"] [contenteditable="true"].ProseMirror, textarea[data-testid="chat_input_input"]')
            await self.session.prepare(dict(REQUEST),cap,[])
            self.assertIn(REQUEST['prompt'],await self.page.locator('.ProseMirror').inner_text())
            self.assertIsNone(await self.page.locator('body').get_attribute('data-sent'))



if __name__=='__main__':unittest.main()
