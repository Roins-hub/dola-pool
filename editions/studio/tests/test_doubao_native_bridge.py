"""Old video-17 adapter fixtures; no account, external network, or signed rewrite."""
import json
from pathlib import Path
import tempfile
import unittest

from patchright.async_api import async_playwright
from providers.base import ProviderError
from providers.doubao.native_bridge import (
    assert_native_ready, install_native_media, native_media_status,
    prepare_native_media, wait_native_uploads,
)
from providers.doubao.materials import attach_native
from providers.doubao.capabilities import Capabilities
from test_doubao_runtime import verified_manifest


FIXTURE = r'''<!doctype html><html><body><div id="fixture"></div><script>
const fixture = {
  attachmentsStore:{attachmentsMap:{chat:{_17:{}}}},
  attachmentUploadStore:{preprocessTaskMap:{}},
  selection:{value:'720p',submitKey:'resolution',messageText:''},
  inputUploadConfig:{uploadPipelineConfig:async()=>({acceptedAttachmentTypes:['image'],acceptedFileExtensions:['png'],maxCount:10,subscription_tag:'native'})},
  pipeline:{submitNodes:[{id:'video-creation:input-skill-node',execute(ctx){return {content:{abilityType:17,abilityParams:{model:'seedance_v2.5',duration:15,resolution:fixture.selection.value}}}}}]},
  messageService:{sendMessage(request){document.body.dataset.sends=String(Number(document.body.dataset.sends||'0')+1);document.body.dataset.sent=JSON.stringify(request);return request}},
  builder({attachments,attachmentKeys,attachmentStates}){
    const attachment_block = {block_type:10052,attachments:attachments.map((a,i)=>({type:a.type==='image'?1:2,media:a.type==='image'?undefined:{media_type:a.type==='audio'?1:2},attachment_key:attachmentKeys[i]}))};
    return attachment_block;
  }
};
const fileInput=document.createElement('input');fileInput.type='file';fileInput.accept='image/*';fileInput.id='native-upload';fileInput.multiple=true;
fileInput.__reactProps$fixture={skill:{skill_type:17},inputUploadConfig:fixture.inputUploadConfig};
fileInput.onchange=()=>{const files=Array.from(fileInput.files);setFiles(files.map((f,i)=>({localKey:'new-'+i,type:f.name.endsWith('.mp3')?'audio':'image',name:f.name,attachmentKey:'uploaded-'+i,task:{status:'completed'}})))};
document.body.append(fileInput);
const wrongInput=document.createElement('input');wrongInput.type='file';wrongInput.accept='image/*';wrongInput.id='unrelated-upload';document.body.append(wrongInput);
const factories={
  stores:function(){/* attachmentsMap preprocessTaskMap attachmentsStore attachmentUploadStore */},
  service:function(){/* messageService sendMessage content_blocks_v2 chat_ability */},
  input:function(){/* __input_action_control_values__ chatInputStore */},
  pipeline:function(){/* video-creation:input-skill-node uploadPipelineConfig */},
  builder:function(){/* attachmentStates attachmentKeys attachment_block */}
};
const modules={stores:{attachmentsStore:fixture.attachmentsStore,attachmentUploadStore:fixture.attachmentUploadStore},
  service:{messageService:fixture.messageService},pipeline:{pipeline:fixture.pipeline,inputUploadConfig:fixture.inputUploadConfig},
  builder:{S:fixture.builder},input:{qM:(key,value)=>fixture.selection=value,gl:key=>fixture.selection.value,N5:key=>fixture.selection}};
function nativeRequire(id){return modules[id]}; nativeRequire.m=factories;
window.webpackChunkFixture=[];window.webpackChunkFixture.push=function(chunk){if(typeof chunk[2]==='function')chunk[2](nativeRequire);return 1};
function request(blocks=[]){return {messages:[{role:'user',ext:{chat_ability:{ability_type:17,ability_param:{model:'seedance_v2.5',duration:15,resolution:fixture.selection.value}}},content_blocks_v2:blocks}]}};
function setFiles(records){fixture.attachmentsStore.attachmentsMap.chat._17={};fixture.attachmentUploadStore.preprocessTaskMap={};for(const r of records){fixture.attachmentsStore.attachmentsMap.chat._17[r.localKey]=r;if(r.task)fixture.attachmentUploadStore.preprocessTaskMap[r.localKey]=r.task}}
document.addEventListener('fixture-command',()=>{
  let result;
  try {const cmd=JSON.parse(document.body.dataset.command);
    if(cmd.op==='files'){setFiles(cmd.records);result={ok:true}}
    if(cmd.op==='send'){result=fixture.messageService.sendMessage(request(cmd.blocks||[]))}
    if(cmd.op==='pipeline'){result=fixture.pipeline.submitNodes[0].execute({content:{abilityType:17}})}
    if(cmd.op==='config'){fixture.inputUploadConfig.uploadPipelineConfig().then(r=>document.body.dataset.result=JSON.stringify(r));return}
    if(cmd.op==='break-builder'){modules.builder.S=()=>null;result={ok:true}}
    if(cmd.op==='minified-builder'){modules.builder.S=a=>fixture.builder(a);result={ok:true}}
    if(cmd.op==='resolution-output'){fixture.pipeline.submitNodes[0].execute=()=>({content:{abilityType:17,abilityParams:{resolution:'720p'}}});result={ok:true}}
    if(cmd.op==='fetch'){fetch(cmd.url,{method:cmd.method||'GET',body:cmd.body}).then(r=>r.text()).then(text=>document.body.dataset.result=JSON.stringify({text}));return}
    if(cmd.op==='xhr'){const x=new XMLHttpRequest();x.open(cmd.method||'GET',cmd.url);x.onload=()=>document.body.dataset.result=JSON.stringify({text:x.responseText});x.send(cmd.body||null);return}
    document.body.dataset.result=JSON.stringify({ok:true,value:result});
  }catch(e){document.body.dataset.result=JSON.stringify({ok:false,code:e.code||e.message})}
});
</script></body></html>'''


class NativeBridgeFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.context = await self.browser.new_context()
        await install_native_media(self.context)
        self.requests = []
        self.fixture_html = FIXTURE
        async def route(request_route):
            request = request_route.request
            self.requests.append({'url': request.url, 'body': request.post_data})
            if '/samantha/skill/pack' in request.url or '/get_item_conf' in request.url:
                body = self.capability_raw
            elif '/chat/completion' in request.url:
                body = '{"ok":true}'
            else:
                body = self.fixture_html
            await request_route.fulfill(status=200, content_type='text/html' if body == self.fixture_html else 'application/json', body=body)
        self.capability_raw = ' { "data": {"video_generation":{"meta":{"option_list":[{"value":"duration","options":[{"value":"15","is_default":true}]}]},"model_capability":{"seedance_v2.5":{"supported_resolutions":["720p"]}}},"image_generation":{"meta":{"option_list":[{"value":"resolution","options":[{"value":"720p","is_default":true}]}]}}} } '
        await self.context.route('**/*', route)
        self.page = await self.context.new_page()
        await self.page.goto('https://www.doubao.com/chat/fixture')

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def command(self, op, **values):
        await self.page.evaluate('''value=>{document.body.removeAttribute('data-result');document.body.dataset.command=JSON.stringify(value);document.dispatchEvent(new Event('fixture-command'))}''', {'op': op, **values})
        await self.page.wait_for_function("document.body.hasAttribute('data-result')")
        return json.loads(await self.page.locator('body').get_attribute('data-result'))

    async def prepare(self, **changes):
        return await prepare_native_media(self.page, **changes)

    async def test_auto_leaves_capability_response_bytes_and_defaults_unchanged(self):
        await self.prepare(resolution='auto')
        result = await self.command('fetch', url='/samantha/skill/pack')
        self.assertEqual(result['text'], self.capability_raw)

    async def test_resolution_changes_only_video_configuration_and_native_selection(self):
        result = await self.prepare(resolution='1080p')
        self.assertTrue(result['supported'], result)
        response = await self.command('fetch', url='/samantha/skill/pack')
        body = json.loads(response['text'])['data']
        options = body['video_generation']['meta']['option_list']
        resolution = next(item for item in options if item['value'] == 'resolution')
        self.assertEqual([item['value'] for item in resolution['options'] if item['is_default']], ['1080p'])
        self.assertEqual(body['image_generation']['meta']['option_list'][0]['options'][0]['value'], '720p')
        self.assertEqual(body['video_generation']['model_capability']['seedance_v2.5']['supported_resolutions'], ['720p', '1080p'])
        output = await self.command('pipeline')
        self.assertEqual(output['value']['content']['abilityParams']['resolution'], '1080p')

    async def test_xhr_configuration_and_readonly_signed_request_observer(self):
        await self.prepare(resolution='1080p')
        response = await self.command('xhr', url='/samantha/skill/pack')
        self.assertIn('1080p', response['text'])
        raw = '{ "messages":[{"ext":{"chat_ability":{"ability_type":17,"ability_param":{"model":"native","duration":15,"resolution":"1080p"}}}}] }'
        url = '/chat/completion?a_bogus=fixture&msToken=fixture&fp=fixture'
        await self.command('fetch', url=url, method='POST', body=raw)
        await self.command('xhr', url=url, method='POST', body=raw)
        sent = [item for item in self.requests if '/chat/completion' in item['url']]
        self.assertEqual([item['body'] for item in sent], [raw, raw])
        state = await native_media_status(self.page)
        self.assertEqual(state['last_request']['resolution'], '1080p')
        self.assertNotIn('fixture', json.dumps(state['last_request']))

    async def test_action_bar_resolution_control_strips_duration_only_properties(self):
        self.capability_raw = json.dumps({'data': {'items': [
            {'key':'video-duration','label':'时长','submit_key':'duration','param_key':'duration',
             'upper_bound':30,'step':5,'default_selected_duration':15,'subscription_tag':'native-duration-only'},
            {'key':'image-resolution','option_list':[{'id':1,'option_key':'720p'}]}]}})
        await self.prepare(resolution='1080p')
        response = await self.command('fetch', url='/alice/slot/action_bar_v3/get_item_conf')
        controls = json.loads(response['text'])['data']['items']
        self.assertEqual([control['key'] for control in controls], ['video-duration','video-resolution','image-resolution'])
        selected = controls[1]
        self.assertNotIn('upper_bound', selected)
        self.assertNotIn('step', selected)
        self.assertNotIn('default_selected_duration', selected)
        self.assertEqual(selected['submit_key'], 'resolution')
        self.assertEqual(selected['param_key'], 'resolution')
        self.assertEqual(selected['option_list'][0]['option_key'], '1080p')
        self.assertEqual(controls[0]['subscription_tag'], 'native-duration-only')

    async def test_missing_native_runtime_returns_unsupported(self):
        await self.page.goto('https://www.dola.com/chat/fixture')
        result = await self.prepare(resolution='1080p')
        self.assertFalse(result['supported'])
        self.assertEqual(result['reason_code'], 'unsupported_origin')

    async def test_new_work_page_does_not_pretend_to_have_old17_pipeline(self):
        self.fixture_html = '<html><body><div contenteditable=true>Seedance 2.5 工作</div></body></html>'
        await self.page.goto('https://www.doubao.com/chat/new-work-fixture')
        state = await self.prepare(resolution='1080p')
        self.assertFalse(state['supported'])
        self.assertEqual(state['reason_code'], 'runtime_unavailable')

    async def test_native_store_can_confirm_upload_without_a_dom_preview_selector(self):
        expected = [{'kind': 'audio', 'name': 'voice.mp3'}]
        prepared = await self.prepare(expected=expected)
        self.assertTrue(prepared['supported'], prepared)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'voice.mp3'
            path.write_bytes(b'offline-fixture')
            result = await attach_native(self.page, [{'kind':'audio','path':path,'name':'voice.mp3','size_bytes':15}],
                                         {'upload':'#native-upload'}, native_state=prepared, timeout=2)
        self.assertTrue(result['ok'])
        self.assertIn('audio/', await self.page.locator('#native-upload').get_attribute('accept'))
        self.assertEqual(await self.page.locator('#unrelated-upload').get_attribute('accept'), 'image/*')

    async def test_supported_pipeline_never_authorizes_an_unrelated_file_input(self):
        prepared = await self.prepare(expected=[{'kind': 'audio', 'name': 'voice.mp3'}])
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'voice.mp3'
            path.write_bytes(b'offline-fixture')
            with self.assertRaises(ProviderError):
                await attach_native(self.page, [{'kind':'audio','path':path,'name':'voice.mp3','size_bytes':15}],
                                    {'upload':'#unrelated-upload'}, native_state=prepared, timeout=2)
        self.assertEqual(await self.page.locator('#unrelated-upload').get_attribute('accept'), 'image/*')
        self.assertEqual(await self.page.locator('#unrelated-upload').evaluate('(n)=>n.files.length'), 0)

    async def test_upload_factory_adds_requested_kinds_without_subscription_changes(self):
        result = await self.prepare(expected=[{'kind': 'audio', 'name': 'voice.mp3'}])
        self.assertTrue(result['supported'], result)
        config = await self.command('config')
        self.assertIn('audio', config['acceptedAttachmentTypes'])
        self.assertEqual(config['subscription_tag'], 'native')

    async def test_native_store_waits_for_keys_and_preprocessing_completion(self):
        expected = [{'kind': 'audio', 'name': 'voice.mp3'}]
        before = await self.prepare(expected=expected)
        await self.command('files', records=[{'localKey': 'new-1', 'type': 'audio', 'name': 'voice.mp3',
                                             'attachmentKey': 'uploaded-1', 'task': {'status': 'uploading'}}])
        state = await native_media_status(self.page)
        self.assertFalse(state['ready'])
        self.assertEqual(state['pending'], 1)
        await self.command('files', records=[{'localKey': 'new-1', 'type': 'audio', 'name': 'voice.mp3',
                                             'attachmentKey': 'uploaded-1', 'task': {'status': 'completed'}}])
        state = await wait_native_uploads(self.page, expected, before['before_keys'], timeout=.5)
        self.assertTrue(state['ready'])

    async def test_native_attachment_block_is_not_duplicated(self):
        await self.prepare(expected=[{'kind': 'image', 'name': 'cat.png'}])
        await self.command('files', records=[{'localKey': 'a', 'type': 'image', 'name': 'cat.png', 'attachmentKey': 'image-key'}])
        block = {'block_type': 10052, 'attachments': [{'type': 1, 'attachment_key': 'image-key'}]}
        result = await self.command('send', blocks=[block])
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['value']['messages'][0]['content_blocks_v2'], [block])
        self.assertEqual(await self.page.locator('body').get_attribute('data-sends'), '1')

    async def test_missing_native_block_is_built_once_before_the_native_send(self):
        await self.prepare(expected=[{'kind': 'audio', 'name': 'voice.mp3'}])
        await self.command('files', records=[{'localKey': 'a', 'type': 'audio', 'name': 'voice.mp3', 'attachmentKey': 'audio-key'}])
        result = await self.command('send')
        self.assertTrue(result['ok'], result)
        blocks = result['value']['messages'][0]['content_blocks_v2']
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]['attachments'][0]['attachment_key'], 'audio-key')

    async def test_factory_fingerprint_supports_its_minified_native_s_export(self):
        await self.command('minified-builder')
        prepared = await self.prepare(expected=[{'kind':'image','name':'cat.png'}])
        self.assertTrue(prepared['supported'], prepared)
        await self.command('files', records=[{'localKey':'a','type':'image','name':'cat.png','attachmentKey':'image-key'}])
        result = await self.command('send')
        self.assertTrue(result['ok'], result)

    async def test_pending_failed_or_reordered_materials_stop_before_send(self):
        expected = [{'kind': 'image', 'name': 'cat.png'}, {'kind': 'audio', 'name': 'voice.mp3'}]
        await self.prepare(expected=expected)
        cases = [
            [{'localKey': 'a', 'type': 'image', 'name': 'cat.png', 'task': {'status': 'uploading'}}],
            [{'localKey': 'a', 'type': 'image', 'name': 'cat.png', 'attachmentKey': 'k', 'task': {'status': 'failed'}}],
            [{'localKey': 'a', 'type': 'audio', 'name': 'voice.mp3', 'attachmentKey': 'audio-key'},
             {'localKey': 'b', 'type': 'image', 'name': 'cat.png', 'attachmentKey': 'image-key'}],
        ]
        for records in cases:
            await self.command('files', records=records)
            result = await self.command('send')
            self.assertFalse(result['ok'], result)
            self.assertIsNone(await self.page.locator('body').get_attribute('data-sends'))
        with self.assertRaises(ProviderError):
            await assert_native_ready(self.page)

    async def test_wrong_or_duplicate_native_attachment_order_is_rejected(self):
        await self.prepare(expected=[{'kind': 'image', 'name': 'one.png'}, {'kind': 'image', 'name': 'two.png'}])
        await self.command('files', records=[{'localKey': 'a', 'type': 'image', 'name': 'one.png', 'attachmentKey': 'one'},
                                             {'localKey': 'b', 'type': 'image', 'name': 'two.png', 'attachmentKey': 'two'}])
        for keys in (['two', 'one'], ['one', 'one'], ['one', 'two', 'two']):
            block = {'block_type': 10052, 'attachments': [{'type': 1, 'attachment_key': key} for key in keys]}
            result = await self.command('send', blocks=[block])
            self.assertFalse(result['ok'], result)
        self.assertIsNone(await self.page.locator('body').get_attribute('data-sends'))


class NativeCapabilityTests(unittest.TestCase):
    def test_legacy_media_needs_bridge_store_and_each_kind_task_evidence(self):
        for kind in ('images', 'audio', 'video'):
            manifest = verified_manifest()
            model = manifest['models'][0]
            model['native_media_bridge'] = True
            model['selectors'].pop('upload_ready')
            model['references'] = {kind: {'enabled':True,'max_count':2,'max_bytes':1024}}
            model['evidence'][{'images':'image','audio':'audio','video':'video'}[kind]+'_task_id'] = 'verified-media-task'
            with self.assertRaises(ProviderError):
                Capabilities.validate(manifest)
            model['evidence']['native_store_ready_task_id'] = 'verified-media-task'
            Capabilities.validate(manifest)
            del model['evidence'][{'images':'image','audio':'audio','video':'video'}[kind]+'_task_id']
            with self.assertRaises(ProviderError):
                Capabilities.validate(manifest)


if __name__ == '__main__':
    unittest.main()
