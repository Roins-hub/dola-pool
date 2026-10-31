"""Offline delivery policy: platform provenance, never query rewriting as proof."""
import base64
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

from store import TaskStore
from task_results import complete_task
from test_video_metadata import mp4
from video_delivery import NoWatermarkAvailable, dola_download, known_watermarked


ORIGINAL = 'https://v16-dola.example/video.mp4?lr=unwatermarked&token=original'
WATERMARKED = 'https://v16-dola.example/video.mp4?lr=video_gen_watermark_dyn'


def model(*entries):
    return json.dumps({'video_list': {str(i): {
        'main_url': base64.b64encode(url.encode()).decode(), 'bitrate': rate}
        for i, (url, rate) in enumerate(entries)}})


def original_result(path):
    return {'local_path': str(path), 'video_url': ORIGINAL,
            'download_source': 'dola_video_model', 'watermark': 'verified_download'}


class SourceSelectionTests(unittest.TestCase):
    def test_official_http_original_is_preserved_from_encoded_and_plain_models(self):
        original = 'http://v16-dola.dola.com/video.mp4?lr=unwatermarked&token=signed'
        encoded = model((original, 1000))
        plain = json.dumps({'video_list': {'video_1': {'main_url': original}}})
        for payload in (encoded, plain):
            with self.subTest(payload=payload):
                try:
                    result = dola_download(payload, WATERMARKED)
                except NoWatermarkAvailable:
                    self.fail('The platform HTTP original was incorrectly rejected')
                self.assertEqual(result['url'], original)
                self.assertEqual(result['watermark'], 'verified_download')
                self.assertEqual(result['download_source'], 'dola_video_model')

    def test_http_original_compatibility_does_not_allow_other_hosts_or_marked_video(self):
        for url in ('http://v16-dola.dola.com.evil.example/v?lr=unwatermarked',
                    'http://127.0.0.1/v?lr=unwatermarked',
                    'http://user:secret@v16-dola.dola.com/v?lr=unwatermarked',
                    'http://v16-dola.dola.com:8080/v?lr=unwatermarked',
                    'http://v16-dola.dola.com/v?lr=video_gen_watermark_dyn'):
            with self.subTest(url=url):
                with self.assertRaises(NoWatermarkAvailable):
                    dola_download(model((url, 1)), '')

    def test_selects_platform_original_without_rewriting_signed_url(self):
        result = dola_download(model((WATERMARKED, 3000), (ORIGINAL, 1000)), WATERMARKED)
        self.assertEqual(result['url'], ORIGINAL)
        self.assertEqual(result['download_source'], 'dola_video_model')
        self.assertEqual(result['watermark'], 'verified_download')

    def test_fallback_query_flags_and_unknown_main_url_are_not_proof(self):
        for vm, url in [('', ORIGINAL), (model(('https://cdn.example/unknown.mp4', 1)), '')]:
            with self.subTest(url=url):
                self.assertEqual(dola_download(vm, url)['watermark'], 'unknown')

    def test_bad_entry_does_not_hide_another_qualified_platform_original(self):
        vm = json.loads(model((ORIGINAL, 1)))
        vm['video_list']['broken'] = {'main_url': 'badbase64', 'bitrate': 'unknown'}
        self.assertEqual(dola_download(vm, WATERMARKED)['url'], ORIGINAL)

    def test_known_watermark_cannot_be_selected_even_if_it_is_the_only_url(self):
        for url in (WATERMARKED, 'https://cdn.example/video?lr=cici_ai',
                    'https://cdn.example/video?watermark=true',
                    'https://cdn.example/video?lr=unwatermarked&watermark=1'):
            with self.subTest(url=url):
                self.assertTrue(known_watermarked(url))
                with self.assertRaises(NoWatermarkAvailable):
                    dola_download(model((url, 1)), url)


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'video.mp4'
        self.path.write_bytes(mp4(15))
        self.store = TaskStore(':memory:')
        self.addCleanup(self.store._conn.close)
        self.store.create('fixture', 'seedance-2.0', 'original', '16:9', 15)
        self.store.update('fixture', conversation_id='123456', account='original-account')

    def publish(self, **values):
        complete_task(self.store, 'fixture', {'local_path': str(self.path), **values}, 'http://local')
        return self.store.get('fixture')

    def test_unknown_delivery_is_not_published_or_regenerated_and_file_survives(self):
        row = self.publish(video_url=ORIGINAL)
        self.assertEqual(row['failure_code'], 'no_watermark_unavailable')
        self.assertEqual(row['phase'], 'no_watermark_unavailable')
        self.assertEqual(row['status'], 'failed')
        self.assertIsNone(row['video_url'])
        self.assertEqual(row['conversation_id'], '123456')
        self.assertEqual(row['account'], 'original-account')
        self.assertEqual(self.path.read_bytes(), mp4(15))

    def test_qualified_platform_original_is_published_with_source_evidence(self):
        row = self.publish(**original_result(self.path))
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['video_url'], 'http://local/videos/video.mp4')
        metadata = [e['details'] for e in self.store.task_events('fixture') if e['phase'] == 'download_metadata'][-1]
        self.assertEqual(metadata['download_source'], 'dola_video_model')
        self.assertEqual(metadata['verification_scope'], 'platform_download_metadata')

    def test_known_watermarked_url_overrides_claimed_verified_source(self):
        result = original_result(self.path)
        result['video_url'] = WATERMARKED
        row = self.publish(**result)
        self.assertEqual(row['failure_code'], 'no_watermark_unavailable')
        self.assertIsNone(row['video_url'])

    def test_provider_source_url_cannot_hide_known_watermark(self):
        row = self.publish(download_source='official_download', watermark='verified_download',
                           source_url=WATERMARKED)
        self.assertEqual(row['failure_code'], 'no_watermark_unavailable')
        self.assertIsNone(row['video_url'])
        row = self.publish(download_source='official_download', watermark='verified_download',
                           video_url='http://local/videos/video.mp4', source_url=WATERMARKED)
        self.assertEqual(row['failure_code'], 'no_watermark_unavailable')

    def test_original_api_source_is_recorded_without_claiming_frame_analysis(self):
        row = self.publish(download_source='dola_original_api', watermark='verified_download',
                           source_url='https://cdn.example/platform-original.mp4')
        self.assertEqual(row['status'], 'completed')
        metadata = [e['details'] for e in self.store.task_events('fixture') if e['phase'] == 'download_metadata'][-1]
        self.assertEqual(metadata['verification_scope'], 'platform_download_metadata')

    def test_known_watermark_error_clears_stale_public_link(self):
        from task_results import fail_task
        self.store.update('fixture', status='completed', video_url='http://local/videos/video.mp4')
        fail_task(self.store, 'fixture', NoWatermarkAvailable())
        self.assertIsNone(self.store.get('fixture')['video_url'])
        self.assertEqual(self.store.get('fixture')['phase'], 'no_watermark_unavailable')
        self.assertTrue(self.path.exists())

    def test_cached_file_reuses_only_matching_content_provenance(self):
        self.publish(**original_result(self.path))
        self.assertEqual(self.publish()['status'], 'completed')
        self.path.write_bytes(mp4(10))
        self.assertEqual(self.publish()['failure_code'], 'no_watermark_unavailable')

    def test_conversation_metadata_alone_does_not_hide_valid_cached_source_evidence(self):
        self.publish(**original_result(self.path))
        self.assertEqual(self.publish(conversation_id='123456')['status'],'completed')

    def test_preview_does_not_pass_with_verified_flag(self):
        row = self.publish(download_source='preview', watermark='verified_download')
        self.assertEqual(row['failure_code'], 'no_watermark_unavailable')

    def test_doubao_failed_result_cannot_publish_preview_through_failure_branch(self):
        from platform_runtime import persist_result
        from types import SimpleNamespace
        self.store.create('doubao', 'native', 'original', '16:9', 15, platform='doubao')
        row = self.store.get('doubao')
        persist_result({'store': self.store, 'config': SimpleNamespace(PUBLIC_BASE='http://local')}, row,
            {'status': 'failed', 'phase': 'resolution_mismatch', 'error_code': 'resolution_mismatch',
             'output_path': str(self.path), 'download_source': 'preview', 'watermark': 'unknown'})
        saved = self.store.get('doubao')
        self.assertEqual(saved['failure_code'], 'no_watermark_unavailable')
        self.assertIsNone(saved['video_url'])
        self.assertTrue(self.path.exists())


class AcceptedDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_receipt_downloads_official_http_original_instead_of_preview(self):
        from video_worker_ui import _wait_for_video
        original = 'http://v19-dola.dola.com/video.mp4?lr=unwatermarked&token=signed'
        fetch = AsyncMock(return_value={'videos': [WATERMARKED], 'texts': [],
                                       'videoModels': [model((original, 1000))]})
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'normal.mp4'
            path.write_bytes(mp4(15))
            download = AsyncMock(return_value=path)
            result = await _wait_for_video('fixture', '123', time.time()+10, fetch, download)
        download.assert_awaited_once_with(original)
        self.assertEqual(result['watermark'], 'verified_download')
        self.assertEqual(result['actual_duration'], 15)

    async def test_doubao_missing_original_finishes_without_refund_or_blocking_the_lane(self):
        from providers.doubao import DoubaoProvider
        from providers.base import ProviderError
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as root:
            provider = object.__new__(DoubaoProvider)
            provider.root = Path(root)
            provider.accounts = SimpleNamespace(settle=Mock())
            provider.scheduler = SimpleNamespace(release_recovery=Mock())
            session = SimpleNamespace(download=AsyncMock(side_effect=ProviderError(
                'no_watermark_unavailable', '官方原片不可用')))
            task_id = 'video_' + 'a' * 32
            snapshot = {'id': task_id, 'platform': 'doubao', 'account': 'original',
                'conversation_id': '123', 'submitted_duration': 15, 'request': {'duration': 15}, 'capability': {}}
            result = await provider._finish(session, snapshot,
                {'phase': 'media_ready', 'media': ['https://cdn.example/preview.mp4']})
            self.assertEqual(result['phase'], 'no_watermark_unavailable')
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['conversation_id'], '123')
            provider.accounts.settle.assert_called_once_with(task_id, 'completed')
            provider.scheduler.release_recovery.assert_called_once_with('doubao', 'original')
            self.assertEqual(provider._read_snapshot(task_id)['state'], 'no_watermark_unavailable')

    async def test_known_watermarked_result_keeps_receipt_but_is_not_downloaded(self):
        from video_worker_ui import _wait_for_video
        events = []
        fetch = AsyncMock(return_value={'videos': [WATERMARKED], 'texts': []})
        download = AsyncMock()
        with self.assertRaises(NoWatermarkAvailable):
            await _wait_for_video('fixture', '123', time.time()+10, fetch, download,
                                  on_event=lambda phase, details: events.append(phase))
        self.assertIn('accepted', events)
        download.assert_not_awaited()
        self.assertEqual(fetch.await_count, 1)

    async def test_doubao_explicit_watermarked_download_cannot_hide_behind_official_marker(self):
        from providers.doubao.downloads import resolve_download
        from providers.base import ProviderError
        page = type('Page', (), {'url': 'https://www.doubao.com/chat/fixture',
            'evaluate': AsyncMock(return_value={'candidates': [
                {'url': 'https://videoweb-download.doubao.com/video?download=true&watermark=1'}]})})()
        with patch('providers.doubao.downloads._validate_url_async', AsyncMock()):
            with self.assertRaises(ProviderError) as caught:
                await resolve_download(page, 'https://cdn.example/preview.mp4', require_no_watermark=True)
        self.assertEqual(caught.exception.code, 'no_watermark_unavailable')
