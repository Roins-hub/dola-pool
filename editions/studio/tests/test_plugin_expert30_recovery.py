"""Recovery must preserve verified clips and obey the shared download budget."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from store import TaskStore
from test_video_metadata import mp4


class CacheRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = TaskStore(str(self.root / 'tasks.db'))
        self.addCleanup(self.store._conn.close)
        self.store.create('multi', 'seedance-2.0', 'exact', '16:9', 30, duration_profile='plugin-expert30')

    def clip(self, identity):
        path = self.root / (identity + '.mp4')
        path.write_bytes(mp4(15))
        return dict(clip_id=identity, vid='v'+identity*24, local_path=str(path), actual_duration=15,
                    video_url='https://v16-dola.dola.com/'+identity+'?lr=unwatermarked',
                    download_source='dola_plugin_fallback', watermark='verified_download',
                    plugin_provenance=dict(clip_id=identity,conversation_id='123456789',original_response_url=True,
                                           fallback_host='vod-urls-mya.byteintlapi.com'))

    def test_cached_clips_bind_each_identity_and_reject_only_the_tampered_file(self):
        from multi_clip_results import publish, cached_clips
        first, second = self.clip('a'), self.clip('b')
        publish(self.store, 'multi', dict(clips=[first,second], generation_finished=True), 'http://local')
        row = self.store.get('multi')
        self.assertEqual([c['clip_id'] for c in cached_clips(self.store,row)], ['a','b'])
        Path(first['local_path']).write_bytes(mp4(12))
        self.assertEqual([c['clip_id'] for c in cached_clips(self.store,row)], ['b'])

    def test_a_failed_replacement_does_not_destroy_the_previously_verified_clip(self):
        from multi_clip_results import publish
        first, second = self.clip('a'), self.clip('b')
        publish(self.store, 'multi', dict(clips=[first], generation_finished=False, observation_finished=False), 'http://local')
        replacement = {**first, 'local_path': str(self.root / 'missing-new-download.mp4')}
        publish(self.store, 'multi', dict(clips=[replacement,second], generation_finished=True), 'http://local')
        row = self.store.get('multi')
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['clips'][0]['video_url'], 'http://local/videos/a.mp4')

    def test_cached_provenance_cannot_be_associated_with_another_video_id(self):
        from multi_clip_results import publish, cached_clips
        publish(self.store,'multi',dict(clips=[self.clip('a')],generation_finished=False),'http://local')
        row=self.store.get('multi')
        row['clips'][0]['vid']='v'+'b'*24
        self.assertEqual(cached_clips(self.store,row),[])

    def test_completed_multi_result_cache_requires_every_clip_and_keeps_success(self):
        import ast
        from types import SimpleNamespace
        from multi_clip_results import publish
        from task_results import complete_task
        first,second=self.clip('a'),self.clip('b')
        publish(self.store,'multi',dict(clips=[first,second],generation_finished=True),'http://local')
        tree=ast.parse(Path('server.py').read_text(encoding='utf-8'))
        function=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='_complete_existing_video')
        ns={'Path':Path,'store':self.store,'complete_task':complete_task,
            'config':SimpleNamespace(PUBLIC_BASE='http://local',DOWNLOAD_DIR=str(self.root))}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[function],type_ignores=[])),'real-cache-publication','exec'),ns)
        self.assertTrue(ns['_complete_existing_video'](self.store.get('multi')))
        self.assertEqual(self.store.get('multi')['status'],'completed')
        Path(second['local_path']).write_bytes(mp4(12))
        self.assertFalse(ns['_complete_existing_video'](self.store.get('multi')))

    def test_tampered_cached_file_is_not_kept_as_a_download_when_repair_fails(self):
        from multi_clip_results import publish
        first,second=self.clip('a'),self.clip('b')
        publish(self.store,'multi',dict(clips=[first,second],generation_finished=True),'http://local')
        Path(second['local_path']).write_bytes(mp4(12))
        publish(self.store,'multi',dict(clips=[{'clip_id':'b','status':'failed','error':'source unavailable'}],
            generation_finished=False,observation_finished=True),'http://local')
        row=self.store.get('multi')
        self.assertEqual(row['status'],'failed')
        self.assertEqual(row['clips'][0]['status'],'completed')
        self.assertIsNone(row['clips'][1].get('video_url'))


class ObserverRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_clip_resolver_can_acquire_download_budget_before_its_network_lane(self):
        import plugin_expert30, plugin_resources
        from types import SimpleNamespace
        lane=asyncio.Semaphore(1)
        budget=asyncio.Semaphore(1)
        class Session:
            account='fixture';conversation_id='123456789';proxy_url=None;own_isp=False
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def poll(self):return {'plugin_expert30':{'accepted':True,'tools_finished':True,'reply':'','clips':[
                {'clip_id':'a','index':1},{'clip_id':'b','index':2}]}}
        async def resolve(session,clip,resolution):
            async with budget,lane:
                return {**clip,'actual_duration':15,'local_path':'already-validated.mp4'}
        scheduler=SimpleNamespace(lane_slot=lambda *args:lane)
        with patch('video_polling.HttpVideoSession',return_value=Session()), \
             patch.object(plugin_resources,'resolve_clip',resolve),patch('config.proxy_for',return_value=None):
            try:
                result=await asyncio.wait_for(plugin_expert30.resume_video('fixture','123456789',2,scheduler=scheduler),.5)
            except TimeoutError:
                self.fail('Expert resolver must not hold the ISP lane while waiting for the shared download budget')
        self.assertTrue(result['generation_finished'])

    async def test_cached_first_clip_and_truncated_poll_can_finish_without_regenerating_or_redownloading_it(self):
        import plugin_expert30
        from types import SimpleNamespace
        existing = dict(clip_id='first',index=1,vid='v-first',local_path='verified-first.mp4',actual_duration=15)
        later = dict(clip_id='second',index=2,vid='v-second',video_models=[],fallback_apis=[],preview_urls=[])
        parsed = dict(accepted=True,tools_finished=True,clips=[later],reply='')
        class Session:
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def poll(self): return {'plugin_expert30':parsed}
        resolved=[]
        async def resolve(session,clip,resolution):
            resolved.append(clip['clip_id'])
            return {**clip,'local_path':'verified-second.mp4','actual_duration':15}
        with patch('video_polling.HttpVideoSession',return_value=Session()), \
             patch('plugin_resources.resolve_clip',resolve), patch('config.POLL_INTERVAL',.001):
            result = await plugin_expert30.resume_video('fixture','123456789',.05,
                generation_options={'existing_clips':[existing]})
        self.assertTrue(result['generation_finished'])
        self.assertEqual([c['clip_id'] for c in result['clips']], ['first','second'])
        self.assertEqual(resolved,['second'])

    async def test_parallel_expert_downloads_share_the_existing_one_file_budget(self):
        import plugin_resources
        import video_worker_ui
        from types import SimpleNamespace
        data = mp4(15)
        active=0
        maximum=0
        class Content:
            async def iter_chunked(self, size):
                await asyncio.sleep(.02)
                yield data
        class Response:
            status=200
            content_length=len(data)
            content=Content()
            async def __aenter__(self):
                nonlocal active,maximum
                active+=1
                maximum=max(maximum,active)
                return self
            async def __aexit__(self,*args):
                nonlocal active
                active-=1
        class Client:
            def __init__(self,*args,**kwargs): pass
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            def get(self,*args,**kwargs): return Response()
        state = SimpleNamespace(proxy_url=None,own_isp=False,state={},conversation_id='123456789')
        with tempfile.TemporaryDirectory() as temp, patch('config.DOWNLOAD_DIR',temp), \
             patch.object(video_worker_ui,'_download_slots',asyncio.Semaphore(1)), \
             patch.object(plugin_resources.aiohttp,'ClientSession',Client):
            paths = await asyncio.gather(*(plugin_resources._download(state,'https://v16-dola.dola.com/a?lr=unwatermarked') for _ in range(2)))
            self.assertEqual(len(paths),2)
            self.assertTrue(all(Path(path).is_file() for path in paths))
        self.assertEqual(maximum,1)


if __name__ == '__main__': unittest.main()
