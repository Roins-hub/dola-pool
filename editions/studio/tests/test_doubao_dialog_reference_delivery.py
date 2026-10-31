"""Owned video references reach the ordinary native uploader before one send."""
from pathlib import Path
import tempfile
import unittest

from providers.doubao import DoubaoProvider, ProviderError
from test_doubao_provider import ck, PROXY
from test_doubao_dialog_delivery import DialogSession
from test_doubao_dialog_media_request import dialog_video_manifest


class ReferenceSession(DialogSession):
    prepared_references = None
    submit_error = None

    async def prepare(self, request, capability, images):
        self.prepared = True
        type(self).prepared_references = images

    async def submit(self):
        if self.submit_error: raise self.submit_error
        await super().submit()


class DialogReferenceDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.provider = DoubaoProvider(root=self.root/'doubao', enabled=True, session_factory=ReferenceSession)
        self.provider.import_ck('one', ck(), PROXY)
        self.provider.accounts.save_diagnostics('one', {'route_verified': True, 'login_verified': True})
        self.folder = self.root/'owned-task'
        self.folder.mkdir()
        self.path = self.folder/('a'*32+'.mp4')
        self.path.write_bytes(b'owned-video-fixture')
        self.request = {'id':'video_'+'d'*32, 'platform':'doubao', 'prompt':'virtual cat',
            'model':'dialog_seedance_2.0_fast', 'duration':15, 'ratio':'16:9',
            'capability_version':'reference-fixture', 'reference_images':[], 'reference_image_paths':[],
            'reference_upload_root':str(self.folder),
            'reference_media_paths':[{'kind':'video','path':str(self.path)}]}
        self.capability = {'id':self.request['model'], 'entry_mode':'ordinary_dialog',
            'references':{'video':{'enabled':True,'max_count':1,'max_bytes':1024}}}
        ReferenceSession.sends, ReferenceSession.prepared_references = 0, None
        ReferenceSession.submit_error = None

    async def run_owned(self):
        # Only the external browser boundary is replaced. The normal task body,
        # ownership checks, normalization, durable fence and quota remain real.
        return await self.provider.run_task(self.request, _calibration={
            'request':self.request, 'capability':self.capability,
            'account':self.provider.list_accounts()[0]})

    async def test_owned_video_reaches_native_prepare_as_typed_reference(self):
        await self.run_owned()
        self.assertEqual(ReferenceSession.prepared_references, [
            {'kind':'video','path':self.path.resolve(),'name':self.path.name,
             'size_bytes':19}])
        self.assertEqual(ReferenceSession.sends, 1)

    async def test_formal_generation_preserves_owned_video_and_json_snapshot(self):
        self.provider.save_capabilities(dialog_video_manifest())
        await self.provider.run_task(self.request)
        self.assertEqual(ReferenceSession.prepared_references[0]['path'], self.path.resolve())
        snapshot=self.provider._read_snapshot(self.request['id'])
        self.assertEqual(snapshot['request']['reference_media_paths'],
                         [{'kind':'video','path':str(self.path.resolve())}])
        self.assertEqual(ReferenceSession.sends,1)

    async def test_other_task_file_is_rejected_before_any_submission(self):
        outside=self.root/'other-task.mp4'
        outside.write_bytes(b'other-video')
        self.request['reference_media_paths'][0]['path']=str(outside)
        with self.assertRaises(ProviderError) as caught:
            await self.run_owned()
        self.assertEqual(caught.exception.code,'unsupported_reference')
        self.assertEqual(ReferenceSession.sends,0)
        self.assertEqual(self.provider.list_accounts()[0]['quota_reserved'],0)

    async def test_mislabeled_audio_cannot_be_sent_as_video(self):
        audio=self.folder/('b'*32+'.mp3')
        audio.write_bytes(b'audio-fixture')
        self.request['reference_media_paths'][0]['path']=str(audio)
        with self.assertRaises(ProviderError): await self.run_owned()
        self.assertEqual(ReferenceSession.sends,0)

    async def test_reference_limits_stop_before_send(self):
        self.capability['references']['video']['max_bytes']=2
        with self.assertRaises(ProviderError): await self.run_owned()
        self.assertEqual(ReferenceSession.sends,0)

    async def test_unstaged_token_cannot_be_silently_dropped_for_text_generation(self):
        self.request['reference_media_paths']=[]
        self.request['reference_media_uploads']=[self.path.name]
        with self.assertRaises(ProviderError): await self.run_owned()
        self.assertEqual(ReferenceSession.sends,0)

    async def test_known_before_send_failure_releases_reservation_and_preserves_rotation(self):
        from providers.doubao.runtime import BeforeSendError
        accounts=self.provider.accounts
        accounts.reserve('earlier-real-send','one')
        accounts.mark_sent('earlier-real-send')
        accounts.settle('earlier-real-send','rejected')
        previous=self.provider.list_accounts()[0]
        ReferenceSession.submit_error=BeforeSendError('reference_upload_changed','附件在发送前改变。')
        result=await self.run_owned()
        current=self.provider.list_accounts()[0]
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['phase'],'failed_before_submit')
        self.assertEqual(result['error_code'],'reference_upload_changed')
        self.assertEqual((current['quota_reserved'],current['inflight_tasks']),(0,0))
        self.assertEqual((current['send_count'],current['last_sent']),
                         (previous['send_count'],previous['last_sent']))
        snapshot=self.provider._read_snapshot(self.request['id'])
        self.assertFalse(snapshot['sent'])
        self.assertFalse(self.provider.scheduler.recovery_reserved(accounts.bound_proxy('one')))
        resumed=await self.provider.resume_task(self.request)
        self.assertEqual(resumed['phase'],'failed_before_submit')
        self.assertEqual(ReferenceSession.sends,0)

    async def test_ambiguous_native_click_error_keeps_original_fence_and_reservation(self):
        ReferenceSession.submit_error=RuntimeError('click interrupted after it may have been dispatched')
        result=await self.run_owned()
        self.assertEqual(result['phase'],'submission_unknown')
        current=self.provider.list_accounts()[0]
        self.assertEqual((current['send_count'],current['quota_reserved'],current['inflight_tasks']),(1,1,1))
        self.assertTrue(self.provider._read_snapshot(self.request['id'])['sent'])
        self.assertTrue(self.provider.scheduler.recovery_reserved(self.provider.accounts.bound_proxy('one')))


if __name__ == '__main__': unittest.main()
