"""Exercise native editor rerenders without an account or network."""
import unittest

from patchright.async_api import async_playwright
from plugin_expert30 import _fill_plugin_draft


class ComposerReplacementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driver = await async_playwright().start()
        self.browser = await self.driver.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self.page.set_default_timeout(2000)
        await self.page.route('**/*', lambda route: route.abort())

    async def asyncTearDown(self):
        await self.browser.close()
        await self.driver.stop()

    async def test_rerender_removes_temporary_marker_after_native_fill(self):
        await self.page.set_content('''<div contenteditable="true" role="textbox"
          style="width:400px;height:80px" id="composer"></div><script>
          document.querySelector('#composer').addEventListener('input', event => {
            const fresh=event.target.cloneNode(true);
            fresh.removeAttribute('data-project-plugin-composer');
            event.target.replaceWith(fresh);
          });</script>''')
        await _fill_plugin_draft(self.page, '韩国美女跳舞')
        self.assertEqual(await self.page.locator('#composer').inner_text(), '韩国美女跳舞')

    async def test_lower_visible_composer_is_filled_without_submitting(self):
        await self.page.set_content('''<textarea id="upper"></textarea>
          <div contenteditable="true" role="textbox" id="lower"
            style="width:400px;height:80px;margin-top:100px"></div>
          <button onclick="window.sent=true">send</button>''')
        await _fill_plugin_draft(self.page, '原始提示词\n第二行')
        self.assertEqual(await self.page.locator('#lower').inner_text(), '原始提示词\n第二行')
        self.assertEqual(await self.page.locator('#upper').input_value(), '')
        self.assertFalse(await self.page.evaluate('Boolean(window.sent)'))

    async def test_native_paragraph_layout_does_not_hide_a_complete_draft(self):
        await self.page.set_content('''<div contenteditable="true" role="textbox"
          style="width:400px;height:120px" id="composer"></div><script>
          document.querySelector('#composer').addEventListener('input', event => {
            const parts=event.target.innerText.split(/\\n+/);
            event.target.replaceChildren(...parts.map(text=>{
              const p=document.createElement('p');p.textContent=text;return p;
            }));
          });</script>''')
        await _fill_plugin_draft(self.page, '生成跳舞视频\n直接显示原生视频卡片，不要链接')
        actual=await self.page.locator('#composer').inner_text()
        self.assertEqual(actual.split(), ['生成跳舞视频','直接显示原生视频卡片，不要链接'])


if __name__ == '__main__':
    unittest.main()
