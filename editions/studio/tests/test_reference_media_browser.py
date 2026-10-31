"""Exercise the real CDP file selection against a local native-uploader fixture."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from patchright.async_api import async_playwright
from integrations.dola_ref_upload import upload_reference_media


class NativeReferenceBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self.tmp = tempfile.TemporaryDirectory()
        self.files = []
        for name in ('voice.mp3','motion.mp4'):
            path = Path(self.tmp.name)/name
            path.write_bytes(b'local fixture content')
            self.files.append(str(path))
        self.requests = []
        self.commit = asyncio.Event()
        self.reject_upload = False
        async def respond(route):
            url = route.request.url
            self.requests.append(url)
            if 'prepare_upload' in url:
                data = {'code':0,'data':{'service_id':'fixture'}}
            elif 'CommitImageUpload' in url:
                await self.commit.wait()
                data = {'Result':{'PluginResult':[{'SourceUri':url.split('=')[-1]}]}}
            else:
                data = {'code':2000}
            await route.fulfill(status=422 if self.reject_upload else 200, content_type='application/json',
                                headers={'Access-Control-Allow-Origin':'*'},body=json.dumps(data))
        await self.page.route('https://upload.example.test/**', respond)
        await self.page.set_content('''<span>Dreamina Seedance 2.5</span><div id="host"></div>
        <script>
        const root=document.querySelector('#host').attachShadow({mode:'open'});
        root.innerHTML='<input type="file" accept=".jpg,.png,.jpeg,.webp,.apng"><div id="files"></div>';
        const pipeline={acceptedAttachmentTypes:['image'],acceptedFileExtensions:['png']};
        const states={attachmentsStore:{attachmentsMap:{fixture:{_17:[]}}},attachmentUploadStore:{preprocessTaskMap:{}}};
        const messageService={sendMessage:request=>{window.fixture.sent++;return request;}};
        window.fixture={states,messageService,pipeline,sent:0,holdKey:false};
        const modules={qBy:{Bc:name=>states[name]},nR1:{K:()=>({getMessageService:()=>messageService})},
          builder:{S:({attachments})=>({block_type:10052,content:{attachment_block:{attachments:attachments.map(a=>({type:2,media:{media_type:a.type==='audio'?1:2}}))}}})}};
        const runtime=id=>modules[id];runtime.m={builder:()=>{const attachmentStates='attachmentStates',attachmentKeys='attachmentKeys',attachment_block='attachment_block';}};
        window.fixture.modules=modules;
        window.__LOADABLE_LOADED_CHUNKS__=[];window.__LOADABLE_LOADED_CHUNKS__.push=items=>items[2](runtime);
        root.querySelector('input').__reactFiber$fixture={memoizedProps:{chatId:'fixture',skill:{skill_type:17},inputUploadConfig:{uploadPipelineConfig:async()=>pipeline}}};
        const upload=async files=>{
          window.changes=(window.changes||0)+1;
          root.querySelector('input').dataset.changes=String(window.changes);
          for(const f of files){
            const kind=f.name.endsWith('.mp3')?'audio':'video';
            if(!pipeline.acceptedAttachmentTypes.includes(kind))continue;
            const card=document.createElement('div');card.className='attachment';
            card.textContent=f.name;card.setAttribute('aria-busy','true');root.querySelector('#files').append(card);
            await fetch('https://upload.example.test/alice/resource/prepare_upload?file='+encodeURIComponent(f.name));
            await fetch('https://upload.example.test/upload/v1/'+encodeURIComponent(f.name));
            await fetch('https://upload.example.test/?Action=CommitImageUpload&file='+encodeURIComponent(f.name));
            states.attachmentsStore.attachmentsMap.fixture._17.push({localKey:f.name,fileName:f.name,type:kind,fileKey:window.fixture.holdKey?'':'key-'+f.name});
            states.attachmentUploadStore.preprocessTaskMap[f.name]={status:'Normal'};
            card.setAttribute('aria-busy','false');
          }
        };
        modules.IK={runConfiguredUploadPipeline:({files})=>upload(files)};
        root.querySelector('input').addEventListener('change',e=>upload(e.target.files));</script>''')

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()
        self.tmp.cleanup()

    async def test_mixed_batch_waits_for_commit_and_shadow_attachments_then_is_idempotent(self):
        task=asyncio.create_task(upload_reference_media(self.page,self.files,timeout_s=5))
        await self.page.wait_for_function('() => window.changes===1')
        await asyncio.sleep(.3)
        self.assertFalse(task.done(), 'Selected files and visible chips must not count as upload completion')
        self.commit.set()
        ok, message=await task
        self.assertTrue(ok,message)
        before=len(self.requests)
        self.assertTrue((await upload_reference_media(self.page,self.files,timeout_s=1))[0])
        self.assertEqual(len(self.requests),before)
        original=self.page.locator('#host').locator('input')
        self.assertEqual(await original.get_attribute('data-changes'),'2')
        self.assertTrue(await original.evaluate('(el)=>el.multiple'))
        self.assertIn('.webm',await original.get_attribute('accept'))

    async def test_rejected_upload_and_missing_commit_are_not_ready_even_without_auto_send(self):
        self.reject_upload=True
        self.commit.set()
        ok, message=await upload_reference_media(self.page,self.files[:1],auto_send=False,timeout_s=2)
        self.assertFalse(ok,message)
        self.assertIn('未发送',message)
        self.assertNotIn('ready:true',str(await self.page.evaluate('window.__dolaReferenceBatch')))

    async def test_selected_file_only_never_counts_as_success(self):
        await self.page.set_content('<span>Dreamina Seedance 2.5</span><input type="file"><div class="chip">30 秒</div>')
        ok, message=await upload_reference_media(self.page,self.files[:1],timeout_s=.4)
        self.assertFalse(ok,message)
        self.assertIn('未发送',message)

    async def test_generic_chat_input_is_not_video_upload_slot(self):
        await self.page.set_content('<span>New chat</span><input type="file" accept="audio/*">')
        ok, message=await upload_reference_media(self.page,self.files,timeout_s=1)
        self.assertFalse(ok,message)
        self.assertIn('视频生成',message)

    async def test_native_block_builder_keeps_prompt_and_guards_missing_file_keys(self):
        self.commit.set()
        ok,message=await upload_reference_media(self.page,self.files,timeout_s=3)
        self.assertTrue(ok,message)
        request={'marker':'unchanged','messages':[{'ext':{'chat_ability':json.dumps({'ability_type':17,'ability_param':'native'})},
                  'content_blocks_v2':[{'block_type':10000,'content':{'text_block':{'text':' 用户原文\n保留空格 '}}}]}]}
        body=await self.page.evaluate('(request)=>window.fixture.messageService.sendMessage(request)',request,isolated_context=False)
        self.assertEqual(body['marker'],'unchanged')
        self.assertEqual(body['messages'][0]['ext'],request['messages'][0]['ext'])
        self.assertEqual(body['messages'][0]['content_blocks_v2'][1],request['messages'][0]['content_blocks_v2'][0])
        self.assertEqual(len(body['messages'][0]['content_blocks_v2'][0]['content']['attachment_block']['attachments']),2)
        await self.page.evaluate("() => {window.fixture.states.attachmentsStore.attachmentsMap.fixture._17[0].fileKey='';}",isolated_context=False)
        with self.assertRaisesRegex(Exception,'素材尚未全部就绪'):
            await self.page.evaluate('(request)=>window.fixture.messageService.sendMessage(request)',request,isolated_context=False)
        self.assertEqual(await self.page.evaluate('window.fixture.sent',isolated_context=False),1)

    async def test_native_uploaded_bytes_without_attachment_file_key_are_not_ready(self):
        self.commit.set()
        await self.page.evaluate('() => {window.fixture.holdKey=true;}',isolated_context=False)
        ok,message=await upload_reference_media(self.page,self.files[:1],timeout_s=.5)
        self.assertFalse(ok,message)
        self.assertIn('未发送',message)

    async def test_native_builder_that_drops_media_stops_before_prompt(self):
        self.commit.set()
        await self.page.evaluate('() => {window.fixture.modules.builder.S=()=>null;}',isolated_context=False)
        ok,message=await upload_reference_media(self.page,self.files[:1],timeout_s=2)
        self.assertFalse(ok,message)
        self.assertEqual(await self.page.evaluate('window.fixture.sent',isolated_context=False),0)

    async def test_lazy_upload_input_mounts_after_composer_add_button(self):
        self.commit.set()
        await self.page.evaluate('''() => {
          const host=document.querySelector('#host'),input=host.shadowRoot.querySelector('input');input.remove();
          const button=document.createElement('button');button.setAttribute('aria-label','Add files');button.textContent='+';
          button.onclick=()=>{host.shadowRoot.prepend(input);button.remove();};document.body.append(button);
        }''',isolated_context=False)
        ok,message=await upload_reference_media(self.page,self.files[:1],timeout_s=3)
        self.assertTrue(ok,message)

    async def test_named_native_block_builder_survives_export_renaming(self):
        self.commit.set()
        await self.page.evaluate('''() => {
          const original=window.fixture.modules.builder.S;
          window.fixture.modules.builder={buildAttachmentBlock:({attachments,attachmentKeys,attachmentStates})=>original({attachments,attachmentKeys,attachmentStates})};
        }''',isolated_context=False)
        ok,message=await upload_reference_media(self.page,self.files[:1],timeout_s=3)
        self.assertTrue(ok,message)
