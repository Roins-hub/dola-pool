"""Real CDP file selection; all native upload responses are locally intercepted."""
import hashlib
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from patchright.async_api import async_playwright
from integrations.dola_ref_upload import upload_reference_media


class ImageOrderBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        self.tmp = tempfile.TemporaryDirectory()
        self.files = []
        for i in range(6):
            path = Path(self.tmp.name) / f'image_{i}.png'
            path.write_bytes(f'local image fixture {i}'.encode())
            self.files.append(str(path))
        async def respond(route):
            url = route.request.url
            data = ({'code':0, 'data':{'service_id':'fixture'}} if 'prepare_upload' in url else
                    {'Result':{'PluginResult':[{'SourceUri':url.split('=')[-1]}]}} if 'CommitImageUpload' in url else
                    {'code':2000})
            await route.fulfill(status=200, content_type='application/json',
                                headers={'Access-Control-Allow-Origin':'*'}, body=json.dumps(data))
        await self.page.route('https://upload.example.test/**', respond)
        fixture_html = '''<span>Seedance 2.0 Fast</span><input type="file"><div id="files"></div>
        <script>
        const states={attachmentsStore:{attachmentsMap:{fixture:{_17:[]}}},attachmentUploadStore:{preprocessTaskMap:{}}};
        const service={sendMessage:request=>{window.fixture.sent++;return request;}};
        const modules={qBy:{Bc:name=>states[name]},nR1:{K:()=>({getMessageService:()=>service})}};
        const runtime=id=>modules[id];runtime.m={};
        window.fixture={states,service,sent:0,reverse:false,holdKey:false,holdBusy:false,
          skipPrepare:-1,skipTransfer:-1,skipCommit:-1,counts:{prepare:0,transfer:0,commit:0}};
        window.__dolaReferenceRuntime=runtime;
        const input=document.querySelector('input');
        input.__reactFiber$fixture={memoizedProps:{chatId:'fixture',skill:{skill_type:17},inputUploadConfig:{}}};
        input.addEventListener('change',async event=>{
          let index=0;
          for(const f of event.target.files){
            const position=index++;
            const card=document.createElement('div');card.className='attachment';card.textContent=f.name;
            card.setAttribute('aria-busy','true');document.querySelector('#files').append(card);
            if(position!==window.fixture.skipPrepare){
              await fetch('https://upload.example.test/alice/resource/prepare_upload?file='+encodeURIComponent(f.name));
              window.fixture.counts.prepare++;
            }
            if(position!==window.fixture.skipTransfer){
              await fetch('https://upload.example.test/upload/v1/'+encodeURIComponent(f.name));
              window.fixture.counts.transfer++;
            }
            if(position!==window.fixture.skipCommit){
              await fetch('https://upload.example.test/?Action=CommitImageUpload&file='+encodeURIComponent(f.name));
              window.fixture.counts.commit++;
            }
            const row={localKey:f.name,fileName:f.name,type:'image',fileKey:window.fixture.holdKey?'':'asset/'+f.name};
            const rows=states.attachmentsStore.attachmentsMap.fixture._17;
            window.fixture.reverse?rows.unshift(row):rows.push(row);
            states.attachmentUploadStore.preprocessTaskMap[f.name]={status:'Normal'};
            card.setAttribute('aria-busy',window.fixture.holdBusy?'true':'false');
          }
        });
        window.fixture.request=()=>({messages:[{ext:{chat_ability:JSON.stringify({ability_type:17})},
          content_blocks_v2:[{block_type:10052,content:{attachment_block:{attachments:
            Array.from({length:6},(_,i)=>({type:1,image:{name:'image_'+i+'.png',uri:'asset/image_'+i+'.png'}}))}}},
            {block_type:10000,content:{text_block:{text:'Local fixture video prompt'}}}]}]});
        </script>'''
        await self.page.route('https://composer.example.test/', lambda route: route.fulfill(
            status=200, content_type='text/html', body=fixture_html))
        await self.page.goto('https://composer.example.test/')

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()
        self.tmp.cleanup()

    def seven_image_files(self):
        path = Path(self.tmp.name) / 'image_6.png'
        path.write_bytes(b'local image fixture 6')
        return [*self.files, str(path)]

    async def test_seven_images_accept_six_prepare_calls_with_all_final_receipts_and_order(self):
        await self.page.evaluate('() => {window.fixture.skipPrepare=6;}', isolated_context=False)
        with redirect_stdout(StringIO()):
            ok, message = await upload_reference_media(self.page, self.seven_image_files(), timeout_s=5)
        self.assertTrue(ok, message)
        self.assertEqual(await self.page.evaluate('window.fixture.counts', isolated_context=False),
                         {'prepare':6, 'transfer':7, 'commit':7})
        proof = await self.page.evaluate('() => window.__dolaReferenceOrderGuard.evidence()', isolated_context=False)
        self.assertTrue(proof['ready'])
        self.assertEqual(proof['expectedCount'], 7)
        self.assertEqual([row['filename'] for row in proof['entries']], [f'image_{i}.png' for i in range(7)])
        self.assertEqual(await self.page.evaluate('window.fixture.sent', isolated_context=False), 0)

    async def test_seven_images_still_require_every_transfer(self):
        await self.page.evaluate('() => {window.fixture.skipPrepare=6;window.fixture.skipTransfer=6;}',
                                 isolated_context=False)
        with redirect_stdout(StringIO()):
            ok, _ = await upload_reference_media(self.page, self.seven_image_files(), timeout_s=5)
        self.assertFalse(ok)
        self.assertEqual(await self.page.evaluate('window.fixture.counts', isolated_context=False),
                         {'prepare':6, 'transfer':6, 'commit':7})
        self.assertTrue(await self.page.evaluate('window.__dolaReferenceOrderGuard.status().ready', isolated_context=False))
        self.assertEqual(await self.page.evaluate('window.fixture.sent', isolated_context=False), 0)

    async def test_seven_images_still_require_every_commit(self):
        await self.page.evaluate('() => {window.fixture.skipPrepare=6;window.fixture.skipCommit=6;}',
                                 isolated_context=False)
        with redirect_stdout(StringIO()):
            ok, _ = await upload_reference_media(self.page, self.seven_image_files(), timeout_s=5)
        self.assertFalse(ok)
        self.assertEqual(await self.page.evaluate('window.fixture.counts', isolated_context=False),
                         {'prepare':6, 'transfer':7, 'commit':6})
        self.assertTrue(await self.page.evaluate('window.__dolaReferenceOrderGuard.status().ready', isolated_context=False))
        self.assertEqual(await self.page.evaluate('window.fixture.sent', isolated_context=False), 0)

    async def test_seven_images_still_reject_wrong_order_with_complete_final_receipts(self):
        await self.page.evaluate('() => {window.fixture.skipPrepare=6;window.fixture.reverse=true;}',
                                 isolated_context=False)
        with redirect_stdout(StringIO()):
            ok, message = await upload_reference_media(self.page, self.seven_image_files(), timeout_s=5)
        self.assertFalse(ok)
        self.assertIn('顺序', message)
        self.assertEqual(await self.page.evaluate('window.fixture.sent', isolated_context=False), 0)

    async def test_seven_images_still_reject_busy_widgets_with_complete_final_receipts(self):
        await self.page.evaluate('() => {window.fixture.skipPrepare=6;window.fixture.holdBusy=true;}',
                                 isolated_context=False)
        with redirect_stdout(StringIO()):
            ok, _ = await upload_reference_media(self.page, self.seven_image_files(), timeout_s=5)
        self.assertFalse(ok)
        self.assertEqual(await self.page.evaluate('window.fixture.counts', isolated_context=False),
                         {'prepare':6, 'transfer':7, 'commit':7})
        self.assertTrue(await self.page.evaluate('window.__dolaReferenceOrderGuard.status().ready', isolated_context=False))
        self.assertEqual(await self.page.evaluate('window.fixture.sent', isolated_context=False), 0)

    async def test_prepare_error_still_blocks_seven_otherwise_complete_images(self):
        await self.page.evaluate('() => {window.fixture.skipPrepare=6;}', isolated_context=False)
        await self.page.route('https://upload.example.test/alice/resource/prepare_upload?file=image_0.png',
                              lambda route: route.fulfill(status=500, body='failed prepare'))
        with redirect_stdout(StringIO()):
            ok, _ = await upload_reference_media(self.page, self.seven_image_files(), timeout_s=5)
        await self.page.wait_for_function('window.fixture.counts.commit===7')
        self.assertFalse(ok)
        self.assertEqual(await self.page.evaluate('window.fixture.counts', isolated_context=False),
                         {'prepare':6, 'transfer':7, 'commit':7})
        self.assertTrue(await self.page.evaluate('window.__dolaReferenceOrderGuard.status().ready', isolated_context=False))
        self.assertEqual(await self.page.evaluate('window.fixture.sent', isolated_context=False), 0)

    async def test_six_image_upload_binds_local_hashes_to_native_keys_before_send(self):
        ok, message = await upload_reference_media(self.page, self.files, timeout_s=5)
        self.assertTrue(ok, message)
        proof = await self.page.evaluate('() => window.__dolaReferenceOrderGuard?.evidence()', isolated_context=False)
        self.assertIsNotNone(proof, 'count-only upload must be replaced by identity/order proof')
        self.assertTrue(proof['ready'])
        self.assertFalse(proof['sendChecked'])
        self.assertEqual([row['filename'] for row in proof['entries']], [f'image_{i}.png' for i in range(6)])
        self.assertEqual(proof['entries'][0]['sha256'], hashlib.sha256(Path(self.files[0]).read_bytes()).hexdigest())
        self.assertEqual(proof['entries'][0]['upstreamKeySha256'], hashlib.sha256(b'asset/image_0.png').hexdigest())
        await self.page.evaluate('() => window.fixture.service.sendMessage(window.fixture.request())', isolated_context=False)
        proof = await self.page.evaluate('() => window.__dolaReferenceOrderGuard.evidence()', isolated_context=False)
        self.assertTrue(proof['sendChecked'])
        self.assertEqual(await self.page.evaluate('window.fixture.sent', isolated_context=False), 1)

    async def test_reversed_native_store_fails_even_with_six_upload_receipts(self):
        await self.page.evaluate('() => {window.fixture.reverse=true;}', isolated_context=False)
        ok, message = await upload_reference_media(self.page, self.files, timeout_s=5)
        self.assertFalse(ok, 'all six uploads completing must not hide reversed native image order')
        self.assertIn('顺序', message)
        self.assertEqual(await self.page.evaluate('window.fixture.sent', isolated_context=False), 0)

    async def test_reversed_final_request_never_calls_native_send(self):
        ok, message = await upload_reference_media(self.page, self.files, timeout_s=5)
        self.assertTrue(ok, message)
        with self.assertRaisesRegex(Exception, '参考图'):
            await self.page.evaluate('''() => {const request=window.fixture.request();
              request.messages[0].content_blocks_v2[0].content.attachment_block.attachments.reverse();
              return window.fixture.service.sendMessage(request);}''', isolated_context=False)
        self.assertEqual(await self.page.evaluate('window.fixture.sent', isolated_context=False), 0)

    async def test_missing_native_key_times_out_with_safe_schema_diagnostics(self):
        await self.page.evaluate('() => {window.fixture.holdKey=true;}', isolated_context=False)
        output = StringIO()
        with redirect_stdout(output):
            ok, message = await upload_reference_media(self.page, self.files, timeout_s=3)
        self.assertFalse(ok)
        lines = [line for line in output.getvalue().splitlines() if line.startswith('[reference-order-diagnostics] ')]
        self.assertEqual(len(lines), 1, 'upload timeout must retain safe native schema evidence')
        report = json.loads(lines[0].split('] ', 1)[1])
        self.assertEqual(report['rowCount'], 6)
        self.assertEqual(report['reason'], 'missing_key')
        self.assertEqual(report['rows'][0]['filename'], 'image_0.png')
        for key, value in {'prepareCount':6, 'transferCount':6, 'commitCount':6,
                           'visibleCount':6, 'busy':False, 'orderReady':False}.items():
            self.assertIn(key, report, 'timeout must record each independent upload-readiness condition')
            self.assertEqual(report[key], value)
        self.assertNotIn('asset/', lines[0])
        self.assertEqual(await self.page.evaluate('window.fixture.sent', isolated_context=False), 0)
