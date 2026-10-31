"""Ordinary-chat video references exercise real offline Chromium file inputs."""
import copy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from patchright.async_api import async_playwright
from providers.base import ProviderError
from providers.doubao.runtime import BrowserSession, VIDEO_REQUEST_SUFFIX, BeforeSendError
from test_doubao_ordinary_dialog_runtime import CAP, REQUEST


HTML='''<!doctype html><meta charset="utf-8"><body data-sends="0" data-uploads="0">
<button id="new">新对话</button><button data-testid="chat_input_action_mode">对话</button>
<textarea id="prompt"></textarea><button id="send">发送</button>
<button id="plus" onclick="document.querySelector('#choose').hidden=false">+</button>
<button id="choose" hidden onclick="document.querySelector('#file').click()">上传文件</button>
<input type="file" id="file" accept="video/mp4,video/quicktime,video/webm" multiple hidden>
<div id="refs"></div><div id="pending"></div><div id="errors"></div><script>
document.querySelector('#new').onclick=()=>{history.pushState({},'', '/chat/');
 if(!window.keepOld)document.querySelector('#refs').innerHTML='';};
document.querySelector('#send').onclick=()=>document.body.dataset.sends=String(+document.body.dataset.sends+1);
document.querySelector('#file').onchange=event=>{
 document.body.dataset.uploads=String(+document.body.dataset.uploads+1);
 document.querySelector('#pending').innerHTML='<div class="pending">uploading</div>';
 setTimeout(()=>{document.querySelector('#pending').innerHTML='';
   if(window.failUpload){document.querySelector('#errors').innerHTML='<div class="error">upload failed</div>';return;}
   for(const [index,file] of [...event.target.files].entries()){
     const card=document.createElement('div');card.className='ready';card.textContent=file.name;
     card.dataset.attachmentKey='native-key-'+index;card.dataset.fileName=window.wrongName||file.name;
     card.dataset.attachmentType=window.wrongType||'video';
     if(window.inline){card.contentEditable='false';document.querySelector('#prompt').append(card);}
     else document.querySelector('#refs').append(card);
   }
 },10);
};</script>'''


class OrdinaryVideoRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.driver=await async_playwright().start();self.addAsyncCleanup(self.driver.stop)
        self.context=await self.driver.chromium.launch_persistent_context(str(Path(self.temp.name)/'browser'),headless=True)
        self.addAsyncCleanup(self.context.close)
        await self.context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=HTML))
        self.page=await self.context.new_page();await self.page.goto('https://www.doubao.com/chat/')
        self.session=BrowserSession(SimpleNamespace(),{'name':'fixture'})
        self.session.context,self.session.page=self.context,self.page
        self.cap=copy.deepcopy(CAP)
        self.cap.update(references={'video':{'enabled':True,'max_count':2,'max_bytes':1024}})
        self.cap['selectors'].update(upload='#file',upload_ready='.ready',upload_pending='.pending',upload_error='.error')
        path=Path(self.temp.name)/'motion.mp4';path.write_bytes(b'owned video fixture')
        self.refs=[{'path':path,'name':path.name,'kind':'video','size_bytes':path.stat().st_size}]

    async def accepted_prepare(self):
        try:
            await self.session.prepare(dict(REQUEST),self.cap,self.refs)
        except ProviderError as error:
            self.fail('The verified native video upload should prepare successfully: '+error.code)

    async def sent(self):return await self.page.locator('body').get_attribute('data-sends')

    async def test_prepares_video_through_native_input_and_rechecks_before_one_send(self):
        original_accept=await self.page.locator('#file').get_attribute('accept')
        await self.accepted_prepare()
        self.assertEqual(await self.page.locator('body').get_attribute('data-uploads'),'1')
        self.assertEqual(await self.page.locator('#file').get_attribute('accept'),original_accept)
        self.assertEqual(await self.sent(),'0')
        self.assertTrue((await self.page.locator('#prompt').input_value()).endswith('\n'+VIDEO_REQUEST_SUFFIX))
        self.assertEqual(await self.page.locator('.ready').get_attribute('data-file-name'),'motion.mp4')
        await self.session.submit()
        self.assertEqual(await self.sent(),'1')

    async def test_native_file_chooser_path_waits_for_real_video_card(self):
        self.cap['selectors'].pop('upload')
        self.cap['selectors'].update(upload_trigger='#plus',upload_option='#choose')
        await self.accepted_prepare()
        self.assertEqual(await self.page.locator('#file').evaluate('(n)=>n.files.length'),1)
        await self.session.submit()
        self.assertEqual(await self.sent(),'1')

    async def test_image_only_native_input_rejects_video_without_upload_or_rewriting_accept(self):
        await self.page.locator('#file').evaluate('(n)=>n.accept="image/*"')
        with self.assertRaises(ProviderError):await self.session.prepare(dict(REQUEST),self.cap,self.refs)
        self.assertEqual(await self.page.locator('#file').get_attribute('accept'),'image/*')
        self.assertEqual(await self.page.locator('body').get_attribute('data-uploads'),'0')
        self.assertEqual(await self.sent(),'0')

    async def test_uploaded_generic_file_is_never_treated_as_video_reference(self):
        await self.page.evaluate('()=>window.wrongType="file"',isolated_context=False)
        with self.assertRaises(ProviderError):await self.session.prepare(dict(REQUEST),self.cap,self.refs)
        self.assertEqual(await self.sent(),'0')

    async def test_mismatched_native_filename_does_not_send_prompt(self):
        await self.page.evaluate('()=>window.wrongName="other.mp4"',isolated_context=False)
        with self.assertRaises(ProviderError):await self.session.prepare(dict(REQUEST),self.cap,self.refs)
        self.assertEqual(await self.sent(),'0')

    async def test_inline_native_attachment_is_added_after_filling_and_never_wiped_by_submit(self):
        await self.page.locator('#prompt').evaluate('(n)=>n.outerHTML=\'<div id="prompt" contenteditable="true"></div>\'')
        await self.page.evaluate('()=>window.inline=true',isolated_context=False)
        await self.accepted_prepare()
        self.assertEqual(await self.page.locator('#prompt [contenteditable="false"]').count(),1)
        self.assertIn(REQUEST['prompt'],await self.page.locator('#prompt').inner_text())
        await self.session.submit()
        self.assertEqual(await self.page.locator('#prompt [contenteditable="false"]').count(),1)
        self.assertEqual(await self.sent(),'1')

    async def test_submit_rejects_changed_prompt_without_sending_replacement(self):
        await self.accepted_prepare()
        await self.page.locator('#prompt').fill('modified prompt')
        with self.assertRaises(ProviderError) as caught:await self.session.submit()
        self.assertIsInstance(caught.exception,BeforeSendError)
        self.assertEqual(await self.sent(),'0')

    async def test_upload_failure_does_not_send_a_text_only_fallback(self):
        await self.page.evaluate('()=>window.failUpload=true',isolated_context=False)
        with self.assertRaises(ProviderError):await self.session.prepare(dict(REQUEST),self.cap,self.refs)
        self.assertEqual(await self.sent(),'0')

    async def test_missing_native_key_prevents_send_even_with_completed_thumbnail(self):
        await self.accepted_prepare()
        await self.page.locator('.ready').evaluate('(n)=>n.removeAttribute("data-attachment-key")')
        with self.assertRaises(ProviderError):await self.session.submit()
        self.assertEqual(await self.sent(),'0')

    async def test_exact_native_react_attachment_metadata_can_confirm_same_card(self):
        await self.accepted_prepare()
        await self.page.locator('.ready').evaluate('''n=>{
          n.__reactProps$fixture={attachment:{attachmentKey:n.dataset.attachmentKey,
            name:n.dataset.fileName,type:2,media:{media_type:2}}};
          for(const key of ['data-attachment-key','data-file-name','data-attachment-type'])n.removeAttribute(key);
        }''',isolated_context=False)
        await self.session.submit()
        self.assertEqual(await self.sent(),'1')

    async def test_extra_unrequested_reference_cannot_be_sent(self):
        await self.accepted_prepare()
        await self.page.locator('.ready').evaluate('''n=>{
          const extra=n.cloneNode(true);extra.dataset.attachmentKey='unrequested';n.parentNode.append(extra);
        }''')
        with self.assertRaises(ProviderError):await self.session.submit()
        self.assertEqual(await self.sent(),'0')

    async def test_missing_ready_or_error_contract_stops_before_upload(self):
        self.cap['selectors'].pop('upload_error')
        with self.assertRaises(ProviderError):await self.session.prepare(dict(REQUEST),self.cap,self.refs)
        self.assertEqual(await self.page.locator('body').get_attribute('data-uploads'),'0')
        self.assertEqual(await self.sent(),'0')

    async def test_old_attachment_in_new_dialog_is_not_reused_as_upload_success(self):
        await self.page.evaluate('''()=>{window.keepOld=true;document.querySelector('#refs').innerHTML=
          '<div class="ready" data-attachment-key="old" data-file-name="motion.mp4" data-attachment-type="video">motion.mp4</div>'}''',isolated_context=False)
        with self.assertRaises(ProviderError):await self.session.prepare(dict(REQUEST),self.cap,self.refs)
        self.assertEqual(await self.page.locator('body').get_attribute('data-uploads'),'0')
        self.assertEqual(await self.sent(),'0')

    async def test_submit_rechecks_changed_key_pending_error_or_missing_native_reference(self):
        for mutation in ("document.querySelector('.ready').dataset.attachmentKey='changed'",
                         "document.querySelector('.ready').remove()",
                         "document.querySelector('#pending').innerHTML='<span class=pending>uploading</span>'",
                         "document.querySelector('#errors').innerHTML='<span class=error>upload failed</span>'"):
            with self.subTest(mutation=mutation):
                await self.page.reload()
                await self.accepted_prepare()
                await self.page.evaluate('()=>{'+mutation+'}',isolated_context=False)
                with self.assertRaises(ProviderError) as caught:await self.session.submit()
                self.assertIsInstance(caught.exception,BeforeSendError)
                self.assertEqual(await self.sent(),'0')


if __name__=='__main__':unittest.main()
