"""One ordinary-chat input contract, exercised on offline browser DOMs.

All requests are intercepted; no account, cookie file or external service is used.
The mixed-layout fixtures cover candidate states, not an asserted live DOM capture.
"""
import copy
import unittest
from unittest.mock import patch

from patchright.async_api import async_playwright
from providers.base import ProviderError
from providers.doubao.runtime import BrowserSession, BeforeSendError
from test_doubao_ordinary_dialog_runtime import CAP, REQUEST, HTML


SELECTOR = ('[data-testid="chat_input"] [contenteditable="true"].ProseMirror, '
            'textarea[data-testid="chat_input_input"]')


class UnifiedComposerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver = await async_playwright().start()
        self.addAsyncCleanup(self.driver.stop)
        self.browser = await self.driver.chromium.launch(headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context()
        self.html = HTML
        await self.context.route('**/*', lambda route: route.fulfill(
            status=200, content_type='text/html', body=self.html))
        self.page = await self.context.new_page()
        await self.page.goto('https://www.doubao.com/chat/')
        self.session = BrowserSession(None, {})
        self.session.page, self.session.context = self.page, self.context
        self.cap = copy.deepcopy(CAP)
        self.cap['selectors']['composer'] = SELECTOR

    async def load(self, editor, script=''):
        self.html = HTML.replace('<textarea id="prompt"></textarea>', editor) + '<script>' + script + '</script>'
        await self.page.reload()

    async def prepare(self):
        await self.session.prepare(dict(REQUEST), self.cap, [])

    async def sends(self):
        return await self.page.locator('body').get_attribute('data-sent')

    async def test_hidden_old_textarea_does_not_block_active_rich_editor(self):
        await self.load('''<textarea data-testid="chat_input_input" hidden>hidden old content</textarea>
          <div data-testid="chat_input"><div id="active" class="ProseMirror" contenteditable="true"></div></div>''')
        await self.prepare()
        self.assertIn(REQUEST['prompt'], await self.page.locator('#active').inner_text())
        self.assertEqual(await self.page.locator('textarea').input_value(), 'hidden old content')
        self.assertEqual(await self.sends(), '0')
        await self.session.submit()
        self.assertEqual(await self.sends(), '1')

    async def test_initial_textarea_ignores_hidden_rich_editor(self):
        await self.load('''<textarea id="active" data-testid="chat_input_input"></textarea>
          <div data-testid="chat_input" hidden><div class="ProseMirror" contenteditable="true"></div></div>''')
        await self.prepare()
        self.assertIn(REQUEST['prompt'], await self.page.locator('#active').input_value())
        self.assertEqual(await self.page.locator('.ProseMirror').text_content(), '')

    async def test_native_toolbar_is_initialized_by_focusing_the_empty_new_editor(self):
        await self.load('<textarea id="active" data-testid="chat_input_input"></textarea>', '''
          const mode=document.querySelector('[data-testid="chat_input_action_mode"]');mode.hidden=true;
          document.querySelector('#active').addEventListener('focus',()=>mode.hidden=false,{once:true});
        ''')
        original=self.session._one
        async def quick_control(selector,**kwargs):
            kwargs.setdefault('timeout',.6)
            return await original(selector,**kwargs)
        with patch.object(self.session,'_one',side_effect=quick_control):
            await self.prepare()
        self.assertIn(REQUEST['prompt'],await self.page.locator('#active').input_value())
        self.assertEqual(await self.sends(),'0')

    async def test_disabled_or_readonly_placeholder_is_never_filled(self):
        for attribute in ('disabled', 'readonly', 'aria-disabled="true"', 'aria-readonly="true"'):
            with self.subTest(attribute=attribute):
                await self.load(f'''<textarea data-testid="chat_input_input" {attribute}>old placeholder</textarea>
                  <div data-testid="chat_input"><div id="active" class="ProseMirror" contenteditable="true"></div></div>''')
                await self.prepare()
                self.assertIn(REQUEST['prompt'], await self.page.locator('#active').inner_text())
                self.assertEqual(await self.page.locator('textarea').input_value(), 'old placeholder')
                self.assertEqual(await self.sends(), '0')

    async def test_new_dialog_waits_for_old_editor_to_disappear(self):
        await self.load('<textarea data-testid="chat_input_input"></textarea>', '''
          const newChat=document.querySelector('#new').onclick;
          document.querySelector('#new').onclick=()=>{
            newChat();
            const wrap=document.createElement('div');wrap.dataset.testid='chat_input';
            wrap.innerHTML='<div id="active" class="ProseMirror" contenteditable="true"></div>';
            document.body.append(wrap);
            setTimeout(()=>document.querySelector('textarea').remove(),250);
          };''')
        await self.prepare()
        self.assertEqual(await self.page.locator('textarea').count(), 0)
        self.assertIn(REQUEST['prompt'], await self.page.locator('#active').inner_text())
        self.assertEqual(await self.sends(), '0')

    async def test_two_persistent_editable_inputs_fail_without_writing_or_sending(self):
        await self.load('''<textarea data-testid="chat_input_input"></textarea>
          <div data-testid="chat_input"><div class="ProseMirror" contenteditable="true"></div></div>''')
        with self.assertRaises(ProviderError):
            await self.prepare()
        self.assertEqual(await self.page.locator('textarea').input_value(), '')
        self.assertEqual(await self.page.locator('.ProseMirror').inner_text(), '')
        self.assertEqual(await self.sends(), '0')

    async def test_prompt_truncated_by_input_handler_is_not_ready(self):
        await self.load('<textarea data-testid="chat_input_input" oninput="this.value=this.value.slice(0,20)"></textarea>')
        with self.assertRaises(ProviderError):
            await self.prepare()
        self.assertEqual(await self.sends(), '0')

    async def test_plain_text_changed_after_prepare_is_not_sent(self):
        await self.load('<textarea data-testid="chat_input_input"></textarea>')
        await self.prepare()
        await self.page.locator('textarea').fill('other text')
        with self.assertRaises(BeforeSendError):
            await self.session.submit()
        self.assertEqual(await self.sends(), '0')

    async def test_send_rechecks_current_visible_editor_not_hidden_prepared_editor(self):
        await self.load('<textarea data-testid="chat_input_input"></textarea>')
        await self.prepare()
        await self.page.evaluate('''()=>{
          document.querySelector('textarea').hidden=true;
          const root=document.createElement('div');root.dataset.testid='chat_input';
          root.innerHTML='<div class="ProseMirror" contenteditable="true">wrong draft</div>';
          document.body.append(root);
        }''')
        with self.assertRaises(BeforeSendError):
            await self.session.submit()
        self.assertEqual(await self.sends(), '0')


if __name__ == '__main__':
    unittest.main()
