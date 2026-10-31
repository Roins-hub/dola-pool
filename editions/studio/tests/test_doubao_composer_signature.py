"""Native editor paragraph changes must not look like modified prompt text."""
import unittest
from patchright.async_api import async_playwright
from providers.doubao.dialog_media import composer_signature

class ComposerSignatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver=await async_playwright().start()
        self.addAsyncCleanup(self.driver.stop)
        self.browser=await self.driver.chromium.launch(headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.page=await self.browser.new_page()
        await self.page.route('**/*',lambda route:route.abort())
        await self.page.set_content('<textarea id="plain"></textarea><div id="editor" contenteditable="true"></div>')

    async def test_same_prompt_survives_native_paragraph_re_render_after_upload(self):
        prompt='首行\n第二行\n\n原始末行。'
        editor=self.page.locator('#editor')
        await editor.fill(prompt)
        before=await composer_signature(editor)
        await editor.evaluate('''(element,lines)=>{
          element.replaceChildren();
          for(const line of lines){
            const paragraph=document.createElement('p');
            paragraph.textContent=line;
            if(!line)paragraph.innerHTML='<br class="ProseMirror-trailingBreak">';
            element.append(paragraph);
          }
        }''',prompt.split('\n'))
        self.assertEqual(await composer_signature(editor),before)
        self.assertEqual(before,prompt)

    async def test_soft_breaks_blank_paragraphs_and_noneditable_references(self):
        prompt='第一行\n第二行\n\n末行'
        await self.page.locator('#plain').fill(prompt)
        await self.page.locator('#editor').evaluate('''element=>element.innerHTML=
          '<p>第一行<br>第二行<br class="ProseMirror-trailingBreak"></p>'+
          '<p><br class="ProseMirror-trailingBreak"></p>'+
          '<p><span contenteditable="false">参考素材标签</span>末行</p>' ''')
        self.assertEqual(await composer_signature(self.page.locator('#editor')),
                         await composer_signature(self.page.locator('#plain')))

    async def test_real_character_or_paragraph_changes_are_still_detected(self):
        await self.page.locator('#editor').fill('第一行\n第二行')
        original=await composer_signature(self.page.locator('#editor'))
        for html in ('<p>第一行</p><p>改过的第二行</p>','<p>第一行第二行</p>'):
            await self.page.locator('#editor').evaluate('(element,html)=>element.innerHTML=html',html)
            self.assertNotEqual(await composer_signature(self.page.locator('#editor')),original)
