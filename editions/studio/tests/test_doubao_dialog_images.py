"""Ordinary-chat image upload invariants use real, offline Chromium inputs."""
from pathlib import Path
import tempfile
import unittest

from patchright.async_api import async_playwright
from providers.base import ProviderError
from providers.doubao.dialog_images import (
    assert_image_references, image_references, prepare_image_references,
)


# Contract fixture only: selector values are injected, not production defaults.
HTML = '''<!doctype html><meta charset="utf-8"><body data-uploads="0" data-sends="0">
<input id="file" type="file" accept="image/png,image/jpeg,image/webp" multiple hidden>
<button id="plus" onclick="document.querySelector('#choose').hidden=false">+</button>
<button id="choose" hidden onclick="document.querySelector('#file').click()">上传文件</button>
<div id="refs"></div><div id="pending"></div><div id="errors"></div><script>
document.querySelector('#file').onchange=event=>{
  document.body.dataset.uploads=String(+document.body.dataset.uploads+1);
  document.querySelector('#pending').innerHTML='<div class="pending">uploading</div>';
  setTimeout(()=>{
    document.querySelector('#pending').innerHTML='';
    if(window.failUpload){document.querySelector('#errors').innerHTML='<div class="error">upload failed</div>';return;}
    for(const [index,file] of [...event.target.files].entries()){
      const card=document.createElement('div');card.className='ready';
      card.dataset.attachmentKey=window.duplicateKey?'duplicate':'image-key-'+index;
      card.dataset.fileName=window.wrongName||file.name;
      card.dataset.attachmentType=window.wrongType||'image';
      card.textContent=file.name;document.querySelector('#refs').append(card);
    }
  },20);
};</script>'''


class _ImageFixture:
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.driver = await async_playwright().start(); self.addAsyncCleanup(self.driver.stop)
        self.browser = await self.driver.chromium.launch(headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context()
        await self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=HTML))
        self.page = await self.context.new_page()
        await self.page.goto('https://www.doubao.com/chat/')
        self.cap = {'entry_mode': 'ordinary_dialog',
                    'references': {'images': {'enabled': True, 'max_count': 2, 'max_bytes': 1024}},
                    'selectors': {'upload_image': '#file', 'upload_ready': '.ready',
                                  'upload_pending': '.pending', 'upload_error': '.error'}}
        path = Path(self.temp.name) / 'cat.png'; path.write_bytes(b'image-fixture')
        self.refs = [{'path': path, 'name': path.name, 'kind': 'image', 'size_bytes': path.stat().st_size}]

    async def uploads(self):
        return await self.page.locator('body').get_attribute('data-uploads')


class DialogImageTests(_ImageFixture, unittest.IsolatedAsyncioTestCase):
    async def test_image_input_waits_for_native_completed_card_and_retains_accept(self):
        before = await self.page.locator('#file').get_attribute('accept')
        state = await prepare_image_references(self.page, self.refs, self.cap, timeout=2)
        self.assertEqual(state['cards'], [('image-key-0', 'cat.png', 'image')])
        self.assertEqual(await self.uploads(), '1')
        self.assertEqual(await self.page.locator('#file').get_attribute('accept'), before)
        await assert_image_references(self.page, state)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sends'), '0')

    async def test_native_file_chooser_attaches_images_without_changing_input(self):
        self.cap['selectors'].pop('upload_image')
        self.cap['selectors'].update(upload_trigger='#plus', upload_option='#choose')
        state = await prepare_image_references(self.page, self.refs, self.cap, timeout=2)
        self.assertEqual(state['cards'], [('image-key-0', 'cat.png', 'image')])
        self.assertEqual(await self.page.locator('#file').evaluate('(n)=>n.files.length'), 1)

    async def test_old_attachment_stops_before_touching_upload_control(self):
        await self.page.locator('#refs').evaluate('''n=>n.innerHTML='<div class="ready" data-attachment-key="old" data-file-name="cat.png" data-attachment-type="image">cat.png</div>' ''')
        with self.assertRaises(ProviderError) as caught:
            await prepare_image_references(self.page, self.refs, self.cap, timeout=1)
        self.assertEqual(caught.exception.code, 'reference_upload_unverified')
        self.assertEqual(await self.uploads(), '0')

    async def test_wrong_native_accept_and_missing_lifecycle_contract_stop_before_upload(self):
        await self.page.locator('#file').evaluate('(n)=>n.accept="video/*"')
        with self.assertRaises(ProviderError):
            await prepare_image_references(self.page, self.refs, self.cap, timeout=1)
        self.assertEqual(await self.page.locator('#file').get_attribute('accept'), 'video/*')
        self.assertEqual(await self.uploads(), '0')
        await self.page.locator('#file').evaluate('(n)=>n.accept="image/*"')
        self.cap['selectors'].pop('upload_error')
        with self.assertRaises(ProviderError):
            await prepare_image_references(self.page, self.refs, self.cap, timeout=1)
        self.assertEqual(await self.uploads(), '0')

    async def test_native_upload_failure_stops_without_text_fallback(self):
        await self.page.evaluate('()=>window.failUpload=true', isolated_context=False)
        with self.assertRaises(ProviderError) as caught:
            await prepare_image_references(self.page, self.refs, self.cap, timeout=1)
        self.assertEqual(caught.exception.code, 'reference_upload_failed')
        self.assertEqual(await self.page.locator('.ready').count(), 0)
        self.assertEqual(await self.page.locator('body').get_attribute('data-sends'), '0')

    async def test_filename_type_and_duplicate_reference_keys_are_not_success(self):
        second = Path(self.temp.name) / 'other.png'; second.write_bytes(b'other-image')
        refs = self.refs + [{'path': second, 'name': second.name, 'kind': 'image', 'size_bytes': 11}]
        for assignment in ('window.wrongName="different.png"', 'window.wrongType="file"', 'window.duplicateKey=true'):
            with self.subTest(assignment=assignment):
                await self.page.reload()
                await self.page.evaluate('()=>{' + assignment + '}', isolated_context=False)
                with self.assertRaises(ProviderError) as caught:
                    await prepare_image_references(self.page, refs, self.cap, timeout=1)
                self.assertEqual(caught.exception.code, 'reference_upload_unverified')

    async def test_attachment_and_conversation_mutation_are_rechecked_before_send(self):
        for mutation in ("document.querySelector('.ready').dataset.attachmentKey='replaced'",
                         "document.querySelector('.ready').remove()",
                         "document.querySelector('#pending').innerHTML='<div class=pending>uploading</div>'",
                         "document.querySelector('#errors').innerHTML='<div class=error>failed</div>'",
                         "history.pushState({},'', '/chat/other')"):
            with self.subTest(mutation=mutation):
                await self.page.goto('https://www.doubao.com/chat/')
                state = await prepare_image_references(self.page, self.refs, self.cap, timeout=1)
                await self.page.evaluate('()=>{' + mutation + '}', isolated_context=False)
                with self.assertRaises(ProviderError):
                    await assert_image_references(self.page, state)
                self.assertEqual(await self.page.locator('body').get_attribute('data-sends'), '0')

    async def test_image_thumbnail_without_native_reference_key_is_not_verified(self):
        state = await prepare_image_references(self.page, self.refs, self.cap, timeout=1)
        await self.page.locator('.ready').evaluate('''n=>{
          n.removeAttribute('data-attachment-key');
          n.innerHTML='<img src="https://cdn.example/cat.png">';
        }''')
        with self.assertRaises(ProviderError) as caught:
            await assert_image_references(self.page, state)
        self.assertEqual(caught.exception.code, 'reference_upload_unverified')

    async def test_capability_limits_and_media_kind_fail_before_any_upload(self):
        cases = [(self.refs, {**self.cap, 'native_media_bridge': True}),
                 (self.refs, {**self.cap, 'entry_mode': 'native_controls'}),
                 ([{**self.refs[0], 'kind': 'video'}], self.cap),
                 (self.refs, {**self.cap, 'references': {}}),
                 (self.refs, {**self.cap, 'references': {'images': {'enabled': True, 'max_count': 1, 'max_bytes': 2}}})]
        for references, cap in cases:
            with self.subTest(cap=cap), self.assertRaises(ProviderError):
                image_references(references, cap)
        self.assertEqual(await self.uploads(), '0')


# The structure below mirrors the locally observed native image card and its
# bounded component props. No request, signing, global store or chat is mocked.
NATIVE_HTML = '''<!doctype html><meta charset="utf-8"><body data-uploads="0">
<div class="guidance-input-surface"><input type="file" id="native" accept=".png,.jpeg,.jpg,.webp"></div>
<div data-testid="attachment_area"></div><script>
document.querySelector('#native').onchange=event=>{
  document.body.dataset.uploads=String(+document.body.dataset.uploads+1);
  const file=event.target.files[0], card=document.createElement('div');
  card.dataset.testid='attachment-image-card';card.dataset.kind='image';card.setAttribute('aria-label',file.name);
  card.textContent=file.name;
  const state={type:'image',fileName:file.name,fileKey:'native-image-key',localKey:'native-local-id',
    size:file.size,uploadPercent:0,status:'Uploading',imageList:[{key:''}],imageWidth:null,imageHeight:null};
  const owner={memoizedProps:{attachmentState:state},return:{memoizedProps:{attachmentState:state},return:null}};
  card.__reactFiber$fixture={memoizedProps:{'data-testid':'attachment-image-card'},return:{memoizedProps:{},return:owner}};
  window.nativeImageState=state;
  document.querySelector('[data-testid="attachment_area"]').append(card);
  if(!window.stayPending)setTimeout(()=>{
    state.status=window.finishStatus||'Normal';state.uploadPercent=100;
    state.imageWidth=128;state.imageHeight=128;
    state.imageList=[{key:window.wrongImageKey?'other-key':state.fileKey}];
  },50);
};</script>'''


class NativeImageStateTests(_ImageFixture, unittest.IsolatedAsyncioTestCase):
    async def test_nine_native_images_are_checked_in_order_before_send(self):
        self.cap['references']['images']['max_count']=9
        refs=[]
        for i in range(9):
            path=Path(self.temp.name)/f'{i+1:02d}_reference.png';path.write_bytes(b'fixture'+bytes([i]))
            refs.append({'path':path,'name':path.name,'kind':'image','size_bytes':path.stat().st_size})
        await self.page.locator('#native').evaluate('''input=>{
          input.multiple=true;input.onchange=()=>{
            document.body.dataset.uploads=String(+document.body.dataset.uploads+1);
            for(const [i,file] of [...input.files].entries()){
              const card=document.createElement('div');card.setAttribute('data-testid','attachment-image-card');
              card.setAttribute('data-kind','image');card.setAttribute('aria-label',file.name);card.textContent=file.name;
              card.__reactFiber$fixture={memoizedProps:{attachmentState:{type:'image',fileName:file.name,
                fileKey:'file-'+i,localKey:'local-'+i,size:file.size,status:'Normal',uploadPercent:100,
                imageList:[{key:'file-'+i}],imageWidth:64,imageHeight:64}}};
              document.body.append(card);
            }
          };
        }''',isolated_context=False)
        state=await prepare_image_references(self.page,refs,self.cap,timeout=2)
        self.assertEqual([c[1] for c in state['cards']],[r['name'] for r in refs])
        self.assertEqual(await self.uploads(),'1')
        await assert_image_references(self.page,state)
        await self.page.locator('[data-testid="attachment-image-card"]').nth(0).evaluate('n=>n.parentElement.append(n)')
        with self.assertRaises(ProviderError):await assert_image_references(self.page,state)

    async def asyncSetUp(self):
        await super().asyncSetUp()
        await self.context.unroute('**/*')
        await self.context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body=NATIVE_HTML))
        await self.page.goto('https://www.doubao.com/chat/')
        self.cap['references']['images']['native_state'] = 'attachment_state_v1'
        self.cap['selectors'] = {'upload_image': '.guidance-input-surface input[type="file"]',
                                 'upload_ready': '[data-testid="attachment-image-card"]'}

    async def test_menu_input_never_steals_the_primary_image_upload(self):
        await self.page.locator('.guidance-input-surface').evaluate('''surface=>{
          const host=document.createElement('div');host.dataset.testid='chat_input';surface.replaceWith(host);host.append(surface);
          const wrapper=document.createElement('div');wrapper.className='relative';
          wrapper.append(document.querySelector('#native'));surface.append(wrapper);
          const menu=document.createElement('div');menu.setAttribute('role','menu');
          menu.innerHTML='<input type="file" data-testid="upload-file-input" accept=".png">';
          surface.append(menu);
          menu.querySelector('input').onchange=()=>document.body.dataset.wrongUpload='yes';
        }''')
        # Observed main-input ancestry stays distinct after the menu adds an input.
        self.cap['selectors']['upload_image']='[data-testid="chat_input"] .guidance-input-surface > .relative > input[type="file"]'
        state=await prepare_image_references(self.page,self.refs,self.cap,timeout=1)
        self.assertEqual(state['cards'],[('native-image-key','cat.png','image')])
        self.assertEqual(await self.uploads(),'1')
        self.assertEqual(await self.page.locator('[data-testid="upload-file-input"]').evaluate('n=>n.files.length'),0)
        self.assertIsNone(await self.page.locator('body').get_attribute('data-wrong-upload'))

    async def test_hidden_native_input_may_arrive_after_page_hydration(self):
        await self.page.locator('#native').evaluate('''n=>{
          n.hidden=true;n.remove();setTimeout(()=>document.querySelector('.guidance-input-surface').append(n),250);
        }''')
        state=await prepare_image_references(self.page,self.refs,self.cap,timeout=2)
        self.assertEqual(state['cards'],[('native-image-key','cat.png','image')])
        self.assertEqual(await self.uploads(),'1')

    async def test_ambiguous_native_inputs_stop_before_any_upload(self):
        await self.page.locator('#native').evaluate('n=>n.parentElement.append(n.cloneNode())')
        with self.assertRaises(ProviderError):
            await prepare_image_references(self.page,self.refs,self.cap,timeout=.2)
        self.assertEqual(await self.uploads(),'0')

    async def test_missing_native_input_times_out_without_upload(self):
        await self.page.locator('#native').evaluate('n=>n.remove()')
        with self.assertRaises(ProviderError) as caught:
            await prepare_image_references(self.page,self.refs,self.cap,timeout=.15)
        self.assertEqual(caught.exception.code,'reference_upload_timeout')
        self.assertEqual(await self.uploads(),'0')

    async def test_observed_native_attachment_state_waits_until_normal_and_matching_image_key(self):
        state = await prepare_image_references(self.page, self.refs, self.cap, timeout=2)
        self.assertEqual(state['cards'], [('native-image-key', 'cat.png', 'image')])
        self.assertEqual(await self.page.locator('body').get_attribute('data-uploads'), '1')
        await assert_image_references(self.page, state)

    async def test_unknown_native_status_and_mismatching_uploaded_key_are_not_ready(self):
        for assignment in ('window.finishStatus="UnknownError"', 'window.wrongImageKey=true'):
            await self.page.reload()
            await self.page.evaluate('()=>{' + assignment + '}', isolated_context=False)
            with self.subTest(assignment=assignment), self.assertRaises(ProviderError):
                await prepare_image_references(self.page, self.refs, self.cap, timeout=1)

    async def test_uploading_native_state_times_out_instead_of_accepting_thumbnail(self):
        await self.page.evaluate('()=>window.stayPending=true', isolated_context=False)
        with self.assertRaises(ProviderError) as caught:
            await prepare_image_references(self.page, self.refs, self.cap, timeout=.3)
        self.assertEqual(caught.exception.code, 'reference_upload_timeout')

    async def test_native_owner_can_mount_after_its_thumbnail_without_false_failure(self):
        await self.page.locator('#native').evaluate('''input=>{
          const upload=input.onchange;
          input.onchange=event=>{
            upload(event);
            const card=document.querySelector('[data-testid="attachment-image-card"]');
            const owner=card.__reactFiber$fixture;delete card.__reactFiber$fixture;
            setTimeout(()=>card.__reactFiber$fixture=owner,350);
          };
        }''', isolated_context=False)
        state=await prepare_image_references(self.page,self.refs,self.cap,timeout=2)
        self.assertEqual(state['cards'],[('native-image-key','cat.png','image')])
        await assert_image_references(self.page,state)

    async def committed_attachment(self, *, current_pending):
        await self.page.locator('#native').evaluate('''(input,currentPending)=>{
          window.stayPending=true;
          const upload=input.onchange;
          input.onchange=event=>{
            upload(event);
            const card=document.querySelector('[data-testid="attachment-image-card"]');
            const pending={...window.nativeImageState};
            const complete={...pending,status:'Normal',uploadPercent:100,
              imageWidth:128,imageHeight:128,imageList:[{key:pending.fileKey}]};
            const root={current:null};
            const previousRoot={stateNode:root,return:null};
            const currentRoot={stateNode:root,return:null};
            previousRoot.alternate=currentRoot;currentRoot.alternate=previousRoot;
            const previous={memoizedProps:{attachmentState:currentPending?complete:pending},
              return:previousRoot};
            const current={memoizedProps:{attachmentState:currentPending?pending:complete},
              return:currentRoot};
            previous.alternate=current;current.alternate=previous;root.current=currentRoot;
            // React can leave the DOM's fiber pointer on the previous branch.
            card.__reactFiber$fixture=previous;
          };
        }''', current_pending, isolated_context=False)

    async def test_committed_upload_completion_does_not_wait_on_previous_pending_state(self):
        await self.committed_attachment(current_pending=False)
        state=await prepare_image_references(self.page,self.refs,self.cap,timeout=2)
        self.assertEqual(state['cards'],[('native-image-key','cat.png','image')])
        await assert_image_references(self.page,state)
        self.assertEqual(await self.uploads(),'1')

    async def test_previous_completion_does_not_hide_current_uploading_state(self):
        await self.committed_attachment(current_pending=True)
        with self.assertRaises(ProviderError) as caught:
            await prepare_image_references(self.page,self.refs,self.cap,timeout=2)
        self.assertEqual(caught.exception.code,'reference_upload_timeout')
        self.assertEqual(await self.uploads(),'1')

    async def test_reused_card_reads_committed_parent_after_immutable_upload_update(self):
        await self.committed_attachment(current_pending=False)
        await self.page.locator('#native').evaluate('''input=>{
          const upload=input.onchange;
          input.onchange=event=>{
            upload(event);
            const card=document.querySelector('[data-testid="attachment-image-card"]');
            const previous=card.__reactFiber$fixture,current=previous.alternate;
            const host={memoizedProps:{},return:previous};
            const alternate={memoizedProps:{},return:current};
            host.alternate=alternate;alternate.alternate=host;
            previous.child=current.child=host;card.__reactFiber$fixture=host;
          };
        }''', isolated_context=False)
        state=await prepare_image_references(self.page,self.refs,self.cap,timeout=2)
        self.assertEqual(state['cards'],[('native-image-key','cat.png','image')])

    async def test_fast_fifteen_six_images_prepare_and_send_after_committed_upload(self):
        import copy
        from providers.doubao.runtime import BrowserSession
        from test_doubao_ordinary_dialog_runtime import CAP, REQUEST

        await self.page.locator('body').evaluate('''body=>{
          body.dataset.sent='0';
          body.insertAdjacentHTML('beforeend','<button id="new">新对话</button>'+
            '<textarea id="prompt"></textarea><button id="send">发送</button>'+
            '<button data-testid="chat_input_action_mode">对话</button>');
          document.querySelector('#send').onclick=()=>body.dataset.sent=String(+body.dataset.sent+1);
          const input=document.querySelector('#native');input.multiple=true;
          input.onchange=()=>{
            body.dataset.uploads=String(+body.dataset.uploads+1);
            for(const [index,file] of [...input.files].entries()){
              const card=document.createElement('div');card.dataset.testid='attachment-image-card';
              card.dataset.kind='image';card.setAttribute('aria-label',file.name);card.textContent=file.name;
              const pending={type:'image',fileName:file.name,fileKey:'file-'+index,
                localKey:'local-'+index,size:file.size,status:'Uploading',uploadPercent:0,
                imageList:[{key:''}],imageWidth:null,imageHeight:null};
              const complete={...pending,status:'Normal',uploadPercent:100,
                imageList:[{key:pending.fileKey}],imageWidth:128,imageHeight:128};
              const root={current:null};
              const oldRoot={stateNode:root,return:null},newRoot={stateNode:root,return:null};
              oldRoot.alternate=newRoot;newRoot.alternate=oldRoot;root.current=newRoot;
              const oldOwner={memoizedProps:{attachmentState:pending},return:oldRoot};
              const owner={memoizedProps:{attachmentState:complete},return:newRoot};
              oldOwner.alternate=owner;owner.alternate=oldOwner;card.__reactFiber$fixture=oldOwner;
              document.querySelector('[data-testid="attachment_area"]').append(card);
            }
          };
        }''', isolated_context=False)
        refs=[]
        for index in range(6):
            path=Path(self.temp.name)/f'{index+1:02d}_reference.png'
            path.write_bytes(b'image-fixture'+bytes([index]))
            refs.append({'path':path,'name':path.name,'kind':'image','size_bytes':path.stat().st_size})
        cap=copy.deepcopy(CAP)
        cap['references']={'images':{**self.cap['references']['images'],'max_count':6}}
        cap['selectors'].update(self.cap['selectors'])
        session=BrowserSession(None,{})
        session.page,session.context=self.page,self.context
        await session.prepare(dict(REQUEST),cap,refs)
        prompt=await self.page.locator('#prompt').input_value()
        self.assertIn(REQUEST['prompt'],prompt)
        self.assertIn('Seedance 2.0 Fast',prompt)
        self.assertIn('15秒',prompt)
        self.assertIn('16:9',prompt)
        self.assertEqual([card[1] for card in session.dialog_media_state['cards']],[ref['name'] for ref in refs])
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'0')
        await session.submit()
        self.assertEqual(await self.page.locator('body').get_attribute('data-sent'),'1')
        self.assertEqual(await self.uploads(),'1')

    async def test_native_local_identity_size_dimensions_and_pending_are_rechecked(self):
        for mutation in ("window.nativeImageState.localKey='replacement'",
                         'window.nativeImageState.size=99',
                         'window.nativeImageState.imageWidth=0',
                         "window.nativeImageState.status='Uploading';window.nativeImageState.uploadPercent=0"):
            await self.page.reload()
            state = await prepare_image_references(self.page, self.refs, self.cap, timeout=1)
            await self.page.evaluate('()=>{' + mutation + '}', isolated_context=False)
            with self.subTest(mutation=mutation), self.assertRaises(ProviderError):
                await assert_image_references(self.page, state)

    async def test_missing_local_card_state_never_reuses_thread_or_global_attachment_metadata(self):
        state = await prepare_image_references(self.page, self.refs, self.cap, timeout=1)
        await self.page.locator('[data-testid="attachment-image-card"]').evaluate('''n=>{
          const own=n.__reactFiber$fixture;
          n.__reactFiber$fixture={memoizedProps:{},return:{memoizedProps:{},return:{memoizedProps:{},return:{memoizedProps:{},return:own}}}};
        }''', isolated_context=False)
        with self.assertRaises(ProviderError) as caught:
            await assert_image_references(self.page, state)
        self.assertEqual(caught.exception.code, 'reference_upload_unverified')


if __name__ == '__main__': unittest.main()
