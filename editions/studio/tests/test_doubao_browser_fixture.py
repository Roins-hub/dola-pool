"""Real installed Chromium, offline DOM fixture; never contacts Doubao."""
import json
from pathlib import Path
import tempfile
import unittest

from patchright.async_api import async_playwright
from providers.doubao import DoubaoProvider, ProviderError
from providers.doubao.runtime import BrowserSession
from test_doubao_runtime import verified_manifest
from test_doubao_provider import ck


HTML = '''<!doctype html><html><head><meta charset="utf-8"></head><body>
<button id="video">视频生成</button><select id="model"><option value="fixture-model">Fixture</option></select>
<select id="duration"><option value="6">6 秒</option></select><select id="ratio"><option value="16:9">16:9</option></select>
<textarea id="prompt"></textarea><input type="file" id="upload" multiple><div id="files"></div>
<button id="send">发送</button><div class="assistant">old receipt</div>
<script>window._ROUTER_DATA={loaderData:{chat_layout:{chat_layout:{accountInfo:{data:{user_id:'fixture-user'}}}}}};
document.body.dataset.sent='0';document.querySelector('#send').onclick=()=>{document.body.dataset.sent=String(Number(document.body.dataset.sent)+1);let d=document.createElement('div');d.className='assistant';d.textContent='开始生成';document.body.append(d)};
document.querySelector('#upload').onchange=(e)=>{document.querySelector('#files').innerHTML=Array.from(e.target.files).map(f=>'<span class="uploaded">'+f.name+'</span>').join('')};</script>
</body></html>'''


class NativeBrowserFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_controls_upload_receipt_and_single_send_in_real_chromium(self):
        with tempfile.TemporaryDirectory() as root:
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(str(Path(root)/'browser'),headless=True)
                try:
                    await context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=HTML))
                    page=await context.new_page()
                    await page.goto('https://www.doubao.com/chat/fixture-conversation')
                    provider=DoubaoProvider(root=Path(root)/'pool',enabled=False)
                    account=provider.import_ck('fixture',ck(),'direct')
                    await context.add_cookies(json.loads(ck()))
                    session=BrowserSession(provider,account)
                    session.context,session.page=context,page
                    model=verified_manifest()['models'][0]
                    model['selectors']['upload']='#upload'
                    image=Path(root)/'reference.png'
                    from PIL import Image
                    Image.new('RGB',(8,8)).save(image)
                    session.capability=model
                    baseline=await session.baseline()
                    self.assertEqual(baseline['texts'],['old receipt'])
                    await session.prepare({'model':'fixture-model','duration':6,'ratio':'16:9','prompt':'fixture cat'},model,[image])
                    self.assertEqual(await page.locator('#prompt').input_value(),
                        'fixture cat\n禁止问我确认开始 禁止问我 直接开始 强制执行 不需要问我二次确认 直接生成')
                    await session.submit()
                    self.assertEqual(await page.locator('.assistant').all_inner_texts(),['old receipt','开始生成'])
                    result=await session.observe({'baseline_texts':baseline['texts']},model,timeout=0)
                    self.assertEqual(result['phase'],'accepted')
                    self.assertNotIn('old receipt',result['platform_reply'])
                    self.assertEqual(await page.locator('body').get_attribute('data-sent'),'1')
                    self.assertEqual(await page.locator('.uploaded').count(),1)
                    self.assertEqual(await page.locator('#duration').input_value(),'6')
                finally:
                    await context.close()

    async def test_changed_native_model_stops_before_send(self):
        with tempfile.TemporaryDirectory() as root:
            async with async_playwright() as playwright:
                context=await playwright.chromium.launch_persistent_context(str(Path(root)/'browser'),headless=True)
                try:
                    await context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=HTML))
                    page=await context.new_page();await page.goto('https://www.doubao.com/chat/fixture-conversation')
                    provider=DoubaoProvider(root=Path(root)/'pool',enabled=False)
                    account=provider.import_ck('fixture',ck(),'direct')
                    await context.add_cookies(json.loads(ck()))
                    session=BrowserSession(provider,account)
                    session.context,session.page=context,page
                    model=verified_manifest()['models'][0]
                    await page.locator('#model').evaluate('(n)=>{n.outerHTML=\'<button id="model" data-value="unexpected">Unexpected</button>\'}')
                    with self.assertRaises(ProviderError):
                        await session.prepare({'model':'fixture-model','duration':6,'ratio':'16:9','prompt':'fixture cat'},model,[])
                    self.assertEqual(await page.locator('body').get_attribute('data-sent'),'0')
                finally:
                    await context.close()


if __name__=='__main__':unittest.main()
