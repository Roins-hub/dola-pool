"""Ordinary-chat submission keeps its own baseline and validates the real output."""
from pathlib import Path
import tempfile
import unittest

from providers.doubao import DoubaoProvider
from providers.doubao.accounts import atomic_json
from test_doubao_provider import ck, PROXY
from test_doubao_dialog_capabilities import dialog_manifest


class DialogSession:
    sends = 0
    observed_baselines = []
    media = None

    def __init__(self, *args, **kwargs):
        self.prepared = False

    async def __aenter__(self): return self
    async def __aexit__(self, *args): pass
    async def baseline(self):
        return {'messages': [] if self.prepared else ['previous-chat-message'], 'media': []}
    async def prepare(self, *args): self.prepared = True
    async def capture_submission_page(self, snapshot): snapshot['conversation_id'] = 'original-chat'
    async def submit(self): type(self).sends += 1
    async def observe(self, snapshot, capability, timeout):
        self.observed_baselines.append(snapshot['baseline_messages'])
        return {'phase': 'accepted', 'conversation_id': 'original-chat', 'dialog_model_verified': True}
    async def download(self, media, task_id): return dict(self.media)


class DialogDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = DoubaoProvider(root=Path(self.temp.name), enabled=True, session_factory=DialogSession)
        self.provider.import_ck('one', ck(), PROXY)
        self.provider.accounts.save_diagnostics('one', {'route_verified': True, 'login_verified': True})
        self.provider.save_capabilities(dialog_manifest())
        self.row = {'id': 'video_'+'e'*32, 'prompt': 'cat', 'model': 'dialog_seedance_2.0_fast',
                    'duration': 15, 'ratio': '16:9', 'platform': 'doubao'}
        DialogSession.sends, DialogSession.observed_baselines = 0, []
        DialogSession.media = {'actual_duration': 15.073, 'output_path': 'retained.mp4',
                               'dimensions': {'width': 1280, 'height': 720}}

    async def test_new_chat_baseline_and_resume_keep_one_original_submission(self):
        result = await self.provider.run_task(self.row)
        self.assertEqual(result['phase'], 'accepted')
        self.assertEqual(DialogSession.observed_baselines, [[]])
        resumed = await self.provider.resume_task(self.row)
        self.assertEqual(resumed['conversation_id'], 'original-chat')
        self.assertEqual(DialogSession.sends, 1)
        self.assertEqual(self.provider.list_accounts()[0]['send_count'], 1)

    async def test_restart_reserves_ordinary_dialog_original_lane(self):
        await self.provider.run_task(self.row)
        self.provider.scheduler.release_recovery('doubao', 'one')
        self.provider.reserve_recoveries([self.row])
        binding = self.provider.accounts.bound_proxy('one')
        self.assertTrue(self.provider.scheduler.recovery_reserved(binding))

    async def finish_video(self, verified=True):
        await self.provider.run_task(self.row)
        snapshot = self.provider._read_snapshot(self.row['id'])
        snapshot['dialog_model_verified'] = verified
        atomic_json(self.provider._snapshot_path(self.row['id']), snapshot)
        return await self.provider._finish(DialogSession(), snapshot,
            {'phase': 'media_ready', 'media': ['https://cdn.example/new.mp4']})

    async def test_dialog_download_compares_actual_aspect_and_preserves_wrong_file(self):
        DialogSession.media['dimensions'] = {'width': 720, 'height': 1280}
        result = await self.finish_video()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'ratio_mismatch')
        self.assertEqual(result['output_path'], 'retained.mp4')

    async def test_video_without_actual_fast_receipt_is_kept_but_model_is_unverified(self):
        result = await self.finish_video(verified=False)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'model_unverified')
        self.assertEqual(result['output_path'], 'retained.mp4')

    async def test_matching_receipt_and_measured_file_complete_normally(self):
        result = await self.finish_video()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(self.provider.list_accounts()[0]['quota_used'], 1)


if __name__ == '__main__': unittest.main()
