import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from store import TaskStore


class PlatformRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_dola_two_five_is_delisted_without_erasing_its_parameters(self):
        from platform_runtime import dola_capabilities,filtered_capabilities
        caps=dola_capabilities()
        models={m['id']:m for m in caps['models']}
        self.assertEqual(set(models),{'seedance-2.0','seedance-2.5'})
        self.assertFalse(models['seedance-2.5']['enabled'])
        self.assertEqual([m['id'] for m in caps['models'] if m['enabled']],['seedance-2.0'])
        self.assertEqual(models['seedance-2.5']['durations'],[10])
        self.assertEqual(models['seedance-2.5']['validation_status'],'experimental')
        self.assertEqual(models['seedance-2.0']['durations'],[5,10,15])
        denied=filtered_capabilities(caps,{'allowed_platforms':['dola'],'allowed_durations':[15]})
        self.assertFalse(next(m for m in denied['models'] if m['id']=='seedance-2.5')['enabled'])
        self.assertEqual(models['seedance-2.5']['durations'],[10],'filtering must not mutate the published catalog')

    async def test_manual_acceptance_resumes_original_poll_and_records_later_rejection(self):
        from platform_runtime import inspect_platform_task, run_platform_task
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as temp:
            s=TaskStore(':memory:');self.addCleanup(s._conn.close)
            s.create('fixture','dialog_seedance_2.5','cat','16:9',30,platform='doubao')
            s.update('fixture',status='processing',phase='submission_unknown')
            class Provider:
                async def inspect_task(self,row,download=False):
                    return {'status':'running','phase':'accepted','conversation_id':'original'}
                async def resume_task(self,row,on_event=None):
                    self.resumed=row['id']
                    return {'status':'failed','phase':'platform_rejected','error_code':'platform_review_rejected',
                            'platform_reply':'生成的影片可能涉及版權限制，請更換輸入內容後重試'}
            p=Provider()
            ns={'store':s,'doubao_provider':p,'_result_checks':{},
                'key_limiter':SimpleNamespace(acquire=AsyncMock(),release=AsyncMock()),
                'config':SimpleNamespace(PUBLIC_BASE='http://test',DOWNLOAD_DIR=temp,REFERENCE_UPLOAD_DIR=temp)}
            async def resume(row):
                async with ns['_result_checks'][row['id']]:
                    await run_platform_task(ns,row,{},resume=True)
            ns['_resume_task']=resume
            result=await inspect_platform_task(ns,s.get('fixture'))
            self.assertEqual(result['state'],'accepted')
            for _ in range(20):
                await asyncio.sleep(0)
                if s.get('fixture')['status']=='failed':break
            self.assertEqual(s.get('fixture')['status'],'failed')
            self.assertEqual(s.get('fixture')['failure_code'],'platform_review_rejected')
            self.assertEqual(p.resumed,'fixture')

    async def test_query_media_ready_is_recoverable_and_can_download_original(self):
        from platform_runtime import inspect_platform_task
        from test_doubao_result_validation import mp4
        with tempfile.TemporaryDirectory() as temp:
            s=TaskStore(':memory:');self.addCleanup(s._conn.close)
            s.create('fixture','dialog_seedance_2.5','cat','16:9',30,platform='doubao')
            s.update('fixture',status='processing',phase='submission_unknown')
            output=Path(temp)/'fixture.mp4';output.write_bytes(mp4(seconds=30))
            class Provider:
                async def inspect_task(self,row,download=False):
                    if download:return {'status':'completed','phase':'completed','output_path':str(output),'actual_duration':30,
                                        'download_source':'official_download','watermark':'verified_download'}
                    return {'status':'running','phase':'media_ready','conversation_id':'original','media_count':1}
            ns={'store':s,'doubao_provider':Provider(),'_result_checks':{},
                'config':SimpleNamespace(PUBLIC_BASE='http://test',DOWNLOAD_DIR=temp,REFERENCE_UPLOAD_DIR=temp)}
            await inspect_platform_task(ns,s.get('fixture'))
            self.assertEqual(s.get('fixture')['phase'],'media_ready')
            result=await inspect_platform_task(ns,s.get('fixture'),download=True)
            self.assertEqual(result['task']['status'],'completed')
            self.assertIn('fixture.mp4',result['task']['video_url'])

    async def test_account_category_survives_persisted_options(self):
        import json
        from platform_runtime import generation_options,task_generation_options
        options=generation_options({'account_category':'普通账号'})
        self.assertEqual(task_generation_options({'generation_options':json.dumps(options)}),options)
        self.assertEqual(generation_options({'account_category':None}),{})

    async def test_text_only_receipt_is_terminal_and_native_evidence_is_stored(self):
        from platform_runtime import persist_result
        import json
        s=TaskStore(':memory:');self.addCleanup(s._conn.close)
        s.create('fixture','dialog_seedance_2.0_fast','cat','16:9',15,platform='doubao')
        result={'status':'failed','phase':'text_only_ack','error_code':'text_only_ack',
                'generation_evidence':{'confirmed':False,'final_text_only':True}}
        persist_result({'store':s},s.get('fixture'),result)
        row=s.get('fixture')
        self.assertEqual(row['status'],'failed')
        self.assertEqual(json.loads(row['request_evidence'])['generation_evidence'],result['generation_evidence'])

    async def test_legacy_account_query_routes_to_independent_namespace(self):
        import platform_runtime as runtime
        self.assertTrue(hasattr(runtime, 'PlatformAccountRouting'), 'platform query dispatch is missing')
        seen = []
        async def app(scope, receive, send):
            seen.append(scope['path'])
        router = runtime.PlatformAccountRouting(app)
        async def receive(): return {'type':'http.request', 'body':b''}
        async def send(message): pass
        await router({'type':'http','path':'/api/admin/accounts/reset-all',
            'query_string':b'platform=doubao','headers':[]}, receive, send)
        self.assertEqual(seen, ['/api/admin/platforms/doubao/accounts/reset-all'])
    async def test_disabled_platform_cannot_create_task_or_touch_dola(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('platform_runtime'), 'independent platform dispatcher is missing')
        from platform_runtime import create_platform_task
        from providers.base import ProviderError
        class Provider:
            def validate_request(self, req):
                raise ProviderError('platform_disabled', '未完成验证', 503)
        s = TaskStore(':memory:')
        self.addCleanup(s._conn.close)
        ns = {'store': s, 'doubao_provider': Provider(), '_submission_identity': lambda *_: {},
              '_find_submission': lambda *_: None}
        with self.assertRaises(ProviderError):
            await create_platform_task(ns, SimpleNamespace(platform='doubao',model='b',prompt='p',duration=10,
                reference_uploads=[],reference_media_uploads=[],reference_images=[],ratio=None,size=None),
                {'allowed_platforms':['doubao']})
        self.assertEqual(s.recent_tasks(platform='all'), [])

    async def test_key_cannot_cross_platform_boundary(self):
        import importlib.util
        self.assertIsNotNone(importlib.util.find_spec('platform_runtime'), 'independent platform dispatcher is missing')
        from platform_runtime import create_platform_task
        from providers.base import ProviderError
        with self.assertRaises(ProviderError) as caught:
            await create_platform_task({}, SimpleNamespace(platform='doubao'), {'allowed_platforms':['dola']})
        self.assertEqual(caught.exception.code, 'platform_not_allowed')
