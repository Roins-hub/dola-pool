"""Image references traverse the real ordinary-chat prepare/send boundary."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from patchright.async_api import async_playwright
from providers.base import ProviderError
from providers.doubao.runtime import BrowserSession, VIDEO_REQUEST_SUFFIX, BeforeSendError
from test_doubao_ordinary_dialog_runtime import CAP, REQUEST
from test_doubao_dialog_video_runtime import HTML
from video_prompt import VIDEO_GENERATION_PREFIX, FAST_THIRTY_REFERENCE_DECLARATION


class OrdinaryImageRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.driver = await async_playwright().start(); self.addAsyncCleanup(self.driver.stop)
        self.browser = await self.driver.chromium.launch(headless=True); self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context()
        html = HTML.replace('video/mp4,video/quicktime,video/webm', 'image/png,image/jpeg,image/webp').replace("window.wrongType||'video'", "window.wrongType||'image'")
        await self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=html))
        self.page = await self.context.new_page(); await self.page.goto('https://www.doubao.com/chat/')
        self.session = BrowserSession(SimpleNamespace(), {'name': 'fixture'})
        self.session.context, self.session.page = self.context, self.page
        self.cap = copy.deepcopy(CAP)
        self.cap.update(references={'images': {'enabled': True, 'max_count': 1, 'max_bytes': 1024}})
        self.cap['selectors'].update(upload_image='#file', upload_ready='.ready', upload_pending='.pending', upload_error='.error')
        path = Path(self.temp.name)/'cat.png'; path.write_bytes(b'owned-image-fixture')
        self.refs = [{'path': path, 'name': path.name, 'kind': 'image', 'size_bytes': path.stat().st_size}]

    async def sent(self):
        return await self.page.locator('body').get_attribute('data-sends')

    async def test_text_request_never_discards_an_existing_external_attachment(self):
        await self.page.locator('body').evaluate('''n=>{
          const card=document.createElement('div');card.className='ready';n.append(card);
          document.querySelector('#new').onclick=()=>{card.remove();document.body.dataset.newClicked='yes'};
        }''')
        with self.assertRaises(ProviderError):
            await self.session.prepare(dict(REQUEST),self.cap,[])
        self.assertEqual(await self.page.locator('.ready').count(),1)
        self.assertIsNone(await self.page.locator('body').get_attribute('data-new-clicked'))
        self.assertEqual(await self.sent(),'0')

    async def test_owned_image_prepares_once_and_send_keeps_image_and_suffix(self):
        await self.session.prepare(dict(REQUEST), self.cap, self.refs)
        self.assertEqual(await self.sent(), '0')
        self.assertEqual(await self.page.locator('body').get_attribute('data-uploads'), '1')
        self.assertTrue((await self.page.locator('#prompt').input_value()).endswith('\n'+VIDEO_REQUEST_SUFFIX))
        outgoing=await self.page.locator('#prompt').input_value()
        self.assertNotIn(VIDEO_GENERATION_PREFIX,outgoing)
        self.assertNotIn(FAST_THIRTY_REFERENCE_DECLARATION,outgoing)
        self.assertEqual(outgoing.count(REQUEST['prompt']),1)
        self.assertIn('Seedance 2.0 Fast生成一条15秒、16:9的视频',outgoing)
        self.assertNotIn('使用1个额度',outgoing)
        self.assertEqual(await self.page.locator('.ready').get_attribute('data-file-name'), 'cat.png')
        await self.session.submit()
        self.assertEqual(await self.sent(), '1')

    async def test_image_and_send_use_active_composer_with_hidden_legacy_input(self):
        self.cap['selectors']['composer']='#prompt, #hidden-old-prompt'
        await self.page.locator('body').evaluate('''body=>{
          const old=document.createElement('textarea');old.id='hidden-old-prompt';
          old.hidden=true;old.value='unrelated hidden content';body.prepend(old);
        }''')
        await self.session.prepare(dict(REQUEST),self.cap,self.refs)
        self.assertEqual(await self.sent(),'0')
        self.assertEqual(await self.page.locator('body').get_attribute('data-uploads'),'1')
        await self.session.submit()
        self.assertEqual(await self.sent(),'1')
        self.assertEqual(await self.page.locator('#hidden-old-prompt').input_value(),'unrelated hidden content')
        self.assertEqual(await self.page.locator('.ready').get_attribute('data-file-name'),'cat.png')

    async def test_25_text_and_image_keep_requested_suffix_and_real_thirty_second_spec(self):
        request={**REQUEST,'model':'dialog_seedance_2.5','duration':30}
        cap={**copy.deepcopy(self.cap),'id':'dialog_seedance_2.5'}
        for refs in ([],self.refs):
            with self.subTest(with_image=bool(refs)):
                await self.page.reload()
                await self.session.prepare(request,cap,refs)
                outgoing=await self.page.locator('#prompt').input_value()
                self.assertTrue(outgoing.endswith('\n'+VIDEO_REQUEST_SUFFIX))
                self.assertNotIn(VIDEO_GENERATION_PREFIX,outgoing)
                self.assertNotIn(FAST_THIRTY_REFERENCE_DECLARATION,outgoing)
                self.assertEqual(outgoing.count(REQUEST['prompt']),1)
                self.assertIn('Seedance 2.5生成一条30秒、16:9的视频',outgoing)
                for wrong in ('使用1个额度','Dreamina','生成一条15秒'):
                    self.assertNotIn(wrong,outgoing)
                await self.session.submit()
                self.assertEqual(await self.sent(),'1')

    async def test_image_changed_or_removed_before_dispatch_never_sends(self):
        for mutation in ("document.querySelector('.ready').remove()", "document.querySelector('.ready').dataset.attachmentKey='other'", "document.querySelector('#prompt').value='changed prompt'"):
            with self.subTest(mutation=mutation):
                await self.page.reload()
                await self.session.prepare(dict(REQUEST), self.cap, self.refs)
                await self.page.evaluate('()=>{'+mutation+'}', isolated_context=False)
                with self.assertRaises(BeforeSendError): await self.session.submit()
                self.assertEqual(await self.sent(), '0')

    async def test_failed_upload_has_no_text_fallback(self):
        await self.page.evaluate('()=>window.failUpload=true', isolated_context=False)
        with self.assertRaises(ProviderError): await self.session.prepare(dict(REQUEST), self.cap, self.refs)
        self.assertEqual(await self.sent(), '0')

    async def test_mixed_image_video_is_rejected_before_upload(self):
        self.cap['references']['video'] = {'enabled': True, 'max_count': 1, 'max_bytes': 1024}
        video = Path(self.temp.name)/'motion.mp4'; video.write_bytes(b'video')
        self.refs.append({'path': video, 'name': video.name, 'kind': 'video', 'size_bytes': 5})
        with self.assertRaises(ProviderError): await self.session.prepare(dict(REQUEST), self.cap, self.refs)
        self.assertEqual(await self.page.locator('body').get_attribute('data-uploads'), '0')
        self.assertEqual(await self.sent(), '0')


if __name__ == '__main__': unittest.main()
