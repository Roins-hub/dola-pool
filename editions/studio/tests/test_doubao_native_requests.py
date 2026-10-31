from pathlib import Path
import tempfile
import unittest

from providers.doubao import DoubaoProvider, ProviderError
from test_doubao_provider import ck, PROXY
from test_doubao_native_capabilities import native_manifest, enable_reference


class NativeRequestTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.provider=DoubaoProvider(root=Path(self.temp.name),enabled=True)
        self.provider.import_ck('one',ck(),PROXY)
        self.provider.accounts.save_diagnostics('one',{'route_verified':True,'login_verified':True})
        manifest=native_manifest();model=manifest['models'][0]
        for kind in ('images','audio','video'):enable_reference(model,kind)
        model['features']={'resolution':{'enabled':True,'values':['1080p']}}
        model['evidence'].update(resolution_task_id='real-resolution-proof')
        model['combinations']=[{'duration':15,'ratio':'16:9','resolution':'1080p'}]
        self.provider.manifest.save(manifest)

    def request(self,**values):
        return {'model':'seedance_2.5','duration':15,'ratio':'16:9','prompt':'virtual cat',**values}

    def test_verified_native_media_are_normalized_without_duration_remap(self):
        result=self.provider.validate_request(self.request(resolution='1080p',
            reference_uploads=['a'*32+'.png'],reference_media_uploads=['b'*32+'.wav','c'*32+'.mp4']))
        self.assertEqual(result['submitted_duration'],15)
        self.assertEqual(result['resolution'],'1080p')
        self.assertNotIn('cast',result)
        self.assertEqual(result['reference_uploads'],['a'*32+'.png'])
        self.assertEqual(result['reference_media_uploads'],['b'*32+'.wav','c'*32+'.mp4'])

    def test_unverified_resolution_and_download_mode_are_rejected(self):
        for values in ({'resolution':'720p'},{'require_no_watermark':True}):
            with self.subTest(values=values),self.assertRaises(ProviderError):
                self.provider.validate_request(self.request(**values))

    def test_removed_cast_requests_cannot_silently_generate_with_materials_only(self):
        for key in ('cast','cast_ids'):
            with self.subTest(key=key):
                with self.assertRaises(ProviderError) as error:
                    self.provider.validate_request(self.request(**{key:['old-role-id']}))
                self.assertEqual(error.exception.code,'unsupported_cast')

    def test_resolution_combination_does_not_open_an_untested_ratio(self):
        with self.assertRaises(ProviderError):
            self.provider.validate_request(self.request(ratio='9:16',resolution='1080p'))


if __name__=='__main__':unittest.main()
