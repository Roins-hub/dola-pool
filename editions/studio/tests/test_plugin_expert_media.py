"""Offline proof that Expert attachments refer to the selected local bytes."""
import asyncio
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
import unittest


def module():
    try:
        return importlib.import_module('plugin_expert_media')
    except ModuleNotFoundError:
        raise AssertionError('Expert attachment identity validation is not implemented') from None


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def attachment(kind, key):
    return ({'type': 1, 'image': {'uri': key}} if kind == 'image' else
            {'type': 2, 'media': {'media_type': 1 if kind == 'audio' else 2, 'uri': key}})


def body(items):
    return {'messages': [{'content': json.dumps([
        {'block_type': 10052, 'content': {'attachment_block': {'attachments': items}}},
        {'block_type': 10000, 'content': {'text_block': {'text': 'Do not change me'}}}])}]}


class AttachmentAuditTests(unittest.TestCase):
    def setUp(self):
        self.manifest = {'version': 1, 'has_media': True, 'files': [
            {'index': 1, 'kind': 'audio', 'sha256': 'a' * 64, 'resource_sha256': digest('audio-1')},
            {'index': 2, 'kind': 'video', 'sha256': 'b' * 64, 'resource_sha256': digest('video-2')}],
            'counts': {'image': 0, 'audio': 1, 'video': 1}}

    def test_same_names_and_counts_cannot_hide_replaced_or_reordered_resources(self):
        audit = module().audit_plugin_attachments
        original = [attachment('audio', 'audio-1'), attachment('video', 'video-2')]
        self.assertEqual(audit(body(original), self.manifest)[:2], (True, 2))
        for bad in [list(reversed(original)), [original[0], attachment('video', 'other')],
                    [original[0]], original + [original[1]],
                    [attachment('video', 'audio-1'), original[1]]]:
            with self.subTest(bad=bad):
                self.assertFalse(audit(body(bad), self.manifest)[0])

    def test_uri_in_prompt_text_is_not_an_attachment_and_diagnostics_do_not_reveal_it(self):
        audit = module().audit_plugin_attachments
        malicious = body([])
        malicious['messages'][0]['text_block'] = {'text': json.dumps(attachment('audio', 'audio-1'))}
        matches, count, diagnostics = audit(malicious, self.manifest)
        self.assertFalse(matches)
        self.assertEqual(count, 0)
        self.assertNotIn('audio-1', json.dumps(diagnostics))

    def test_bad_manifest_or_unrecognized_attachment_fails_closed(self):
        audit = module().audit_plugin_attachments
        self.assertFalse(audit(body([{'type': 99, 'key': 'audio-1'}]), self.manifest)[0])
        self.assertFalse(audit(body([]), {'version': 1, 'files': []})[0])
        self.assertTrue(audit(body([]), {'version': 1, 'files': [], 'counts': {
            'image': 0, 'audio': 0, 'video': 0}, 'has_media': False})[0])

    def test_two_distinct_files_cannot_share_an_unverifiable_resource_identity(self):
        manifest = {**self.manifest, 'files': [dict(item) for item in self.manifest['files']]}
        manifest['files'][1]['resource_sha256'] = digest('audio-1')
        self.assertFalse(module().audit_plugin_attachments(body([
            attachment('audio', 'audio-1'), attachment('video', 'audio-1')]), manifest)[0])


class NativeExpertMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from patchright.async_api import async_playwright
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self.tmp = tempfile.TemporaryDirectory()
        self.files = []
        for index, name in enumerate(('voice.wav', 'motion.mp4')):
            path = Path(self.tmp.name) / name
            path.write_bytes(b'local fixture ' + bytes([index]))
            self.files.append(str(path))
        self.receipt_matches = True
        async def respond(route):
            url = route.request.url
            if '/CommitUpload/' in url:
                key = url.rsplit('/', 1)[1] if self.receipt_matches else 'wrong-receipt'
                response = {'Result': {'Results': [{'SourceUri': key}]}}
            elif '/prepare_upload/' in url:
                response = {'code': 0, 'data': {'service_id': 'offline'}}
            else:
                response = {'code': 2000}
            await route.fulfill(status=200, content_type='application/json', body=json.dumps(response))
        await self.page.route('https://upload.fixture.test/**', respond)
        await self.page.route('https://fixture.test/**', lambda route: route.fulfill(
            content_type='text/html', body='<span>Dreamina Seedance 2.5</span><input type="file" accept="image/*">'))
        await self.page.goto('https://fixture.test/')
        await self.page.evaluate(r'''() => {
          const input=document.querySelector('input');
          const state={attachmentsStore:{attachmentsMap:{fixture:{_17:[]}}},attachmentUploadStore:{preprocessTaskMap:{}}};
          const service={sendMessage:r=>r};
          const pipeline={acceptedAttachmentTypes:['image'],acceptedFileExtensions:['png']};
          const modules={qBy:{Bc:name=>state[name]},nR1:{K:()=>({getMessageService:()=>service})},
            builder:{S:({attachments,attachmentKeys,attachmentStates})=>({block_type:10052,content:{attachment_block:{attachments:attachments.map(a=>
              a.type==='image'?{type:1,image:{uri:a.key}}:{type:2,media:{media_type:a.type==='audio'?1:2,uri:a.key}})}}})}};
          const runtime=id=>modules[id];runtime.m={builder:()=>{const attachmentStates='attachmentStates',attachmentKeys='attachmentKeys',attachment_block='attachment_block';}};
          window.__LOADABLE_LOADED_CHUNKS__=[];window.__LOADABLE_LOADED_CHUNKS__.push=items=>items[2](runtime);
          input.__reactFiber$fixture={memoizedProps:{chatId:'fixture',skill:{skill_type:17},inputUploadConfig:{uploadPipelineConfig:async()=>pipeline}}};
          modules.IK={runConfiguredUploadPipeline:async({files})=>{
            for(const file of files){
              const kind=file.name.endsWith('.wav')?'audio':file.name.endsWith('.png')?'image':'video',key='resource-'+(state.attachmentsStore.attachmentsMap.fixture._17.length+1);
              await fetch('https://upload.fixture.test/alice/resource/prepare_upload/'+key);
              await fetch('https://upload.fixture.test/upload/v1/'+key);
              await fetch('https://upload.fixture.test/CommitUpload/'+key);
              state.attachmentsStore.attachmentsMap.fixture._17.push({localKey:key,type:kind,key,fileName:file.name,file});
              state.attachmentUploadStore.preprocessTaskMap[key]={status:'Normal',key,uploadResult:{SourceUri:key}};
              const card=document.createElement('div');card.className='attachment';card.textContent=file.name;document.body.append(card);
            }
          }};
          input.addEventListener('change', e=>modules.IK.runConfiguredUploadPipeline({files:Array.from(e.target.files)}));
          window.fixture={state,modules,service,runtime,replaceBytes:false};
        }''', isolated_context=False)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()
        self.tmp.cleanup()

    async def test_local_bytes_bind_native_resources_in_order_without_sending_a_prompt(self):
        manifest = await module().upload_plugin_references(self.page, self.files)
        self.assertEqual([x['sha256'] for x in manifest['files']], [
            hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in self.files])
        self.assertEqual([x['resource_sha256'] for x in manifest['files']], [digest('resource-1'), digest('resource-2')])
        self.assertTrue(all(x['receipt_verified'] for x in manifest['files']))
        submitted = body([attachment('audio', 'resource-1'), attachment('video', 'resource-2')])
        self.assertTrue(module().audit_plugin_attachments(submitted, manifest)[0])

    async def test_native_upload_receipt_for_another_resource_cannot_authorize_submission(self):
        self.receipt_matches = False
        with self.assertRaisesRegex(Exception, '素材|附件|身份'):
            await module().upload_plugin_references(self.page, self.files)

    async def test_image_only_uses_existing_image_guard_identity(self):
        image = Path(self.tmp.name) / 'image.png'
        image.write_bytes(b'local image fixture')
        manifest = await module().upload_plugin_references(self.page, [str(image)])
        self.assertFalse(manifest['has_media'])
        self.assertEqual(manifest['files'][0]['resource_sha256'], digest('resource-1'))
        self.assertTrue(module().audit_plugin_attachments(body([attachment('image', 'resource-1')]), manifest)[0])

    async def test_native_runner_receiving_other_bytes_cannot_authorize_same_filename(self):
        # Replacing the selected bytes at the native pipeline boundary must fail,
        # even when every filename, type and count remains identical.
        await self.page.evaluate('''() => {
          const original=window.fixture.modules.IK.runConfiguredUploadPipeline;
          window.fixture.modules.IK.runConfiguredUploadPipeline=async args=>original({...args,files:args.files.map(f=>new File(['other bytes'],f.name,{type:f.type}))});
        }''', isolated_context=False)
        with self.assertRaisesRegex(Exception, '素材|附件|身份'):
            await module().upload_plugin_references(self.page, self.files)

    async def test_readonly_webpack_exports_do_not_block_native_media(self):
        await self.page.evaluate('''() => {
          for(const [mod,key] of [[window.fixture.modules.IK,'runConfiguredUploadPipeline'],[window.fixture.modules.builder,'S']]){
            const fn=mod[key];Object.defineProperty(mod,key,{configurable:false,enumerable:true,get:()=>fn});
          }
        }''', isolated_context=False)
        manifest = await module().upload_plugin_references(self.page, self.files)
        self.assertEqual(manifest['counts'], {'image': 0, 'audio': 1, 'video': 1})

    async def test_invalid_first_builder_candidate_does_not_poison_valid_fallback(self):
        await self.page.evaluate('''() => {
          const original=window.fixture.modules.builder.S;
          window.fixture.modules.builder={S:()=>null,
            buildAttachmentBlock:({attachments,attachmentKeys,attachmentStates})=>original({attachments,attachmentKeys,attachmentStates})};
        }''', isolated_context=False)
        manifest = await module().upload_plugin_references(self.page, self.files)
        self.assertTrue(module().audit_plugin_attachments(body([
            attachment('audio', 'resource-1'), attachment('video', 'resource-2')]), manifest)[0])

    async def test_modern_native_store_and_service_resolvers_match_existing_image_guard(self):
        await self.page.evaluate('''() => {
          const {modules,runtime,state,service}=window.fixture;
          delete modules.qBy;delete modules.nR1;
          const stores=name=>({getState:()=>state[name]});
          modules.modern={b:function(name){var value;return null==(value=stores(name))?void 0:value.getState();}};
          runtime.m.modern=()=>{const storeContainer='storeContainer',getAsyncStore='getAsyncStore';};
          modules.service={a:async function(name){const resource={load:async()=>({getMessageService:()=>service})};return await resource.load(name);}};
          runtime.m.service=()=>{const serviceContainer='serviceContainer',resourceStore='resourceStore',manager='chat-ioc-manager';};
        }''', isolated_context=False)
        manifest = await module().upload_plugin_references(self.page, self.files)
        self.assertTrue(module().audit_plugin_attachments(body([
            attachment('audio', 'resource-1'), attachment('video', 'resource-2')]), manifest)[0])


if __name__ == '__main__':
    unittest.main()
