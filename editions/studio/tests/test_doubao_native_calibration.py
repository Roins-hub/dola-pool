import asyncio
from pathlib import Path
import tempfile
import unittest

from providers.doubao import DoubaoProvider, ProviderError
from test_doubao_calibration import CalibrationSession
from test_doubao_provider import ck, PROXY


class NativeCalibrationSession(CalibrationSession):
    async def download(self,url,task_id):
        return {**await super().download(url,task_id),'dimensions':self.provider.fixture_dimensions}


class NativeCalibrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.provider=DoubaoProvider(root=Path(self.temp.name)/'pool',downloads=Path(self.temp.name)/'out',
            enabled=False,session_factory=NativeCalibrationSession)
        self.provider.fixture_dimensions={'width':1920,'height':1080}
        self.addAsyncCleanup(self.provider.shutdown)
        self.provider.import_ck('one',ck(),PROXY)
        self.provider.accounts.save_diagnostics('one',{'route_verified':True,'login_verified':True,
            'native_media_bridge_available':True,
            'observed_native_options':[
                {'kind':'model','selector':'#model','options':[{'value':'seedance_2.5','label':'Seedance 2.5'}]},
                {'kind':'duration','selector':'#duration','options':[{'value':'6','label':'6 秒'}]},
                {'kind':'ratio','selector':'#ratio','options':[{'value':'16:9','label':'16:9'}]}],
            'observed_selectors':{'video_entry':'#video','composer':'#prompt',
                'send':'#send','assistant':'.assistant',
                'upload':'input[type=file]','upload_ready':'.uploaded'}})

    def request(self,**values):
        return {'model':'seedance_2.5','duration':6,'ratio':'16:9','prompt':'virtual orange cat',**values}

    async def test_completed_native_video17_registers_only_the_actual_matching_combination(self):
        job=self.provider.start_calibration('one',self.request(resolution='1080p'))
        await self.provider._calibration_jobs[job['id']]['_task']
        result=self.provider.calibration_status(job['id'])
        self.assertEqual(result['state'],'completed',result)
        model=self.provider.manifest.load()['models'][0]
        self.assertTrue(model['native_media_bridge'])
        self.assertNotIn('skill',model)
        self.assertEqual(model['durations'],[6])
        self.assertEqual(model['ratios'],['16:9'])
        self.assertEqual(model['features']['resolution']['values'],['1080p'])
        self.assertEqual(model['label'],'Seedance 2.5')
        self.assertFalse(self.provider.capabilities()['enabled'])

    async def test_wrong_actual_aspect_does_not_advertise_the_requested_ratio(self):
        self.provider.fixture_dimensions={'width':1080,'height':1920}
        job=self.provider.start_calibration('one',self.request())
        await self.provider._calibration_jobs[job['id']]['_task']
        result=self.provider.calibration_status(job['id'])
        self.assertEqual(result['state'],'failed',result)
        self.assertEqual(result['error_code'],'ratio_mismatch')
        self.assertEqual(self.provider.manifest.load()['models'],[])

    async def test_new_resolution_keeps_previous_verified_resolution_and_combinations(self):
        for resolution in ('720p','1080p'):
            job=self.provider.start_calibration('one',self.request(resolution=resolution))
            await self.provider._calibration_jobs[job['id']]['_task']
            self.assertEqual(self.provider.calibration_status(job['id'])['state'],'completed')
        model=self.provider.manifest.load()['models'][0]
        self.assertEqual(model['features']['resolution']['values'],['720p','1080p'])
        self.assertEqual({c['resolution'] for c in model['combinations']},{'720p','1080p'})

    async def test_unobserved_or_over_limit_native_request_stops_without_any_job(self):
        for changes in ({'model':'invented-model'},{'duration':31},{'resolution':'8k'}):
            with self.subTest(changes=changes),self.assertRaises(ProviderError):
                self.provider.start_calibration('one',self.request(**changes))
        self.assertEqual(self.provider._calibration_jobs,{})

    async def test_local_reference_tokens_require_authenticated_owner_and_root(self):
        with self.assertRaises(ProviderError):
            self.provider.start_calibration('one',self.request(reference_uploads=['img_bad.png']))
        self.assertEqual(self.provider._calibration_jobs,{})

    async def test_removed_cast_cannot_start_calibration_or_create_task_files(self):
        root=Path(self.temp.name)/'uploads'
        for key in ('cast','cast_ids'):
            with self.subTest(key=key):
                with self.assertRaises(ProviderError) as error:
                    self.provider.start_calibration('one',self.request(**{key:['old-role-id']}),
                        upload_owner='user:73',upload_root=root)
                self.assertEqual(error.exception.code,'unsupported_cast')
        self.assertEqual(self.provider._calibration_jobs,{})
        self.assertFalse(root.exists())


if __name__=='__main__':unittest.main()
