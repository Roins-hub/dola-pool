import asyncio
from pathlib import Path
import tempfile
import unittest

from providers.doubao import DoubaoProvider, ProviderError
from test_doubao_provider import ck, PROXY
from test_doubao_runtime import FixtureSession
from test_video_metadata import mp4


class CalibrationSession(FixtureSession):
    async def observe(self, snapshot, capability, timeout):
        return {'phase':'media_ready','conversation_id':'calibration-1','platform_reply':'开始生成',
                'media':['https://cdn.example/current.mp4']}
    async def download(self, url, task_id):
        path=self.provider.downloads/(task_id+'.mp4')
        path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(mp4(6))
        return {'output_path':str(path),'actual_duration':6}
    def __init__(self, provider, *args, **kwargs):
        super().__init__(provider,*args,**kwargs);self.provider=provider


class CalibrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_real_completed_matching_file_creates_verified_capability(self):
        with tempfile.TemporaryDirectory() as root:
            provider=DoubaoProvider(root=Path(root)/'pool',downloads=Path(root)/'out',enabled=False,session_factory=CalibrationSession)
            provider.import_ck('one',ck(),PROXY)
            checks={'route_verified':True,'login_verified':True,
                'observed_native_options':[
                    {'kind':'model','selector':'#model','options':[{'value':'fixture-model','label':'Fixture'}]},
                    {'kind':'duration','selector':'#duration','options':[{'value':'6','label':'6 秒'}]},
                    {'kind':'ratio','selector':'#ratio','options':[{'value':'16:9','label':'16:9'}]}],
                'observed_selectors':{'video_entry':'#video','composer':'#prompt','send':'#send','assistant':'.assistant'}}
            provider.accounts.save_diagnostics('one',checks)
            job=provider.start_calibration('one',{'model':'fixture-model','duration':6,'ratio':'16:9','prompt':'fixture cat'})
            for _ in range(100):
                result=provider.calibration_status(job['id'])
                if result['state'] in ('completed','failed'):break
                await asyncio.sleep(.01)
            self.assertEqual(result['state'],'completed',result)
            manifest=provider.manifest.load()
            self.assertEqual(manifest['models'][0]['durations'],[6])
            self.assertEqual(manifest['models'][0]['evidence']['text_task_id'],job['id'])
            self.assertFalse(manifest['models'][0]['references']['images']['enabled'])
            self.assertFalse(provider.capabilities()['enabled'])

    async def test_unobserved_native_model_cannot_start_calibration(self):
        with tempfile.TemporaryDirectory() as root:
            provider=DoubaoProvider(root=Path(root),enabled=False)
            provider.import_ck('one',ck(),PROXY)
            with self.assertRaises(ProviderError):
                provider.start_calibration('one',{'model':'guessed-seedance','duration':15,'prompt':'cat'})


if __name__=='__main__': unittest.main()
