"""Validate delivered files independently of model labels or receipt text."""
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest

from platform_runtime import persist_result
from providers.base import ProviderError
from providers.doubao.result_validation import validate_delivered
from store import TaskStore


def box(kind, data):
    return struct.pack('>I4s', len(data)+8, kind)+data


def mp4(width=1920, height=1080, seconds=15):
    mvhd = bytes(4)+struct.pack('>IIII', 0, 0, 1000, seconds*1000)+bytes(80)
    matrix = struct.pack('>9i', 65536, 0, 0, 0, 65536, 0, 0, 0, 1073741824)
    tkhd = bytes(40)+matrix+struct.pack('>II', width<<16, height<<16)
    track = box(b'trak', box(b'tkhd', tkhd)+box(b'mdia', box(b'hdlr', bytes(8)+b'vide'+bytes(12))))
    return box(b'ftyp', b'isom'+bytes(4)+b'isom')+box(b'moov', box(b'mvhd', mvhd)+track)+box(b'mdat', bytes(100))


class DeliveredValidationTests(unittest.TestCase):
    def test_download_metadata_evidence_keeps_only_bounded_dimensions_and_known_markers(self):
        from video_evidence import public_details
        self.assertEqual(public_details({'dimensions': {'width': 1920, 'height': 1080,
            'private': 'session-cookie'}, 'download_source': 'official_download',
            'watermark': 'verified_download', 'private': 'session-cookie'}), {
                'dimensions': {'width': 1920, 'height': 1080},
                'download_source': 'official_download', 'watermark': 'verified_download'})
        self.assertEqual(public_details({'dimensions': {'width': True, 'height': 1080},
            'download_source': 'https://private.example?token=secret',
            'watermark': 'guaranteed_no_visible_watermark'}), {})

    def test_requested_aspect_is_compared_with_actual_dimensions_with_small_tolerance(self):
        request = {'ratio': '16:9', 'resolution': 'auto'}
        validate_delivered(request, {'dimensions': {'width': 1920, 'height': 1080}})
        validate_delivered(request, {'dimensions': {'width': 1900, 'height': 1080}})
        with self.assertRaises(ProviderError) as error:
            validate_delivered(request, {'dimensions': {'width': 1080, 'height': 1920}})
        self.assertEqual(error.exception.code, 'ratio_mismatch')

    def test_resolution_uses_actual_short_edge_and_accepts_larger_real_output(self):
        validate_delivered({'ratio': '9:16', 'resolution': '1080p'},
                           {'dimensions': {'width': 1080, 'height': 1920}})
        validate_delivered({'ratio': '16:9', 'resolution': '1080p'},
                           {'dimensions': {'width': 3840, 'height': 2160}})
        with self.assertRaises(ProviderError) as error:
            validate_delivered({'ratio': '16:9', 'resolution': '1080p'},
                               {'dimensions': {'width': 1280, 'height': 720}})
        self.assertEqual(error.exception.code, 'resolution_mismatch')

    def test_missing_or_invalid_dimensions_cannot_be_presented_as_verified(self):
        for dimensions in (None, {}, {'width': 0, 'height': 1080},
                           {'width': True, 'height': 1080}, {'width': '1920', 'height': 1080}):
            with self.assertRaises(ProviderError) as error:
                validate_delivered({'ratio': '16:9'}, {'dimensions': dimensions})
            self.assertEqual(error.exception.code, 'video_metadata_missing')

    def test_actual_mp4_dimensions_are_used_when_transport_did_not_provide_them(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'video.mp4'
            path.write_bytes(mp4(720, 1280))
            validate_delivered({'ratio': '9:16', 'resolution': '720p'}, {'output_path': str(path)})
            with self.assertRaises(ProviderError) as error:
                validate_delivered({'ratio': '16:9'}, {'local_path': str(path)})
            self.assertEqual(error.exception.code, 'ratio_mismatch')
            self.assertTrue(path.is_file())

    def test_required_official_download_never_accepts_preview_as_watermark_proof(self):
        media = {'dimensions': {'width': 1920, 'height': 1080}, 'watermark': 'unknown'}
        with self.assertRaises(ProviderError) as error:
            validate_delivered({'ratio': '16:9', 'require_no_watermark': True}, media)
        self.assertEqual(error.exception.code, 'no_watermark_unavailable')
        validate_delivered({'ratio': '16:9', 'require_no_watermark': True},
                           {**media, 'watermark': 'verified_download'})

    def test_legacy_cast_snapshot_can_still_validate_and_collect_its_video(self):
        validate_delivered({'ratio': '16:9', 'resolution': 'auto',
                            'cast': [{'name': '旧角色', 'image_indices': [1]}]},
                           {'dimensions': {'width': 1920, 'height': 1080}})


class DeliveredPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'fixture.mp4'
        self.path.write_bytes(mp4())
        self.store = TaskStore(':memory:')
        self.ns = {'store': self.store, 'config': SimpleNamespace(PUBLIC_BASE='https://project.example')}

    def tearDown(self):
        self.store._conn.close()
        self.temp.cleanup()

    def task(self, platform='doubao'):
        self.store.create('fixture', 'native', 'original', '16:9', 15, platform=platform)
        return self.store.get('fixture')

    def test_failed_delivery_keeps_file_and_download_url_instead_of_marking_completed(self):
        row = self.task()
        self.path.write_bytes(mp4(1280, 720))
        persist_result(self.ns, row, {'status': 'failed', 'phase': 'resolution_mismatch',
            'error_code': 'resolution_mismatch', 'error': '实际清晰度不足，文件已保留',
            'output_path': str(self.path), 'actual_duration': 15,
            'dimensions': {'width': 1280, 'height': 720},
            'download_source': 'official_download', 'watermark': 'verified_download'})
        saved = self.store.get('fixture')
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['failure_code'], 'resolution_mismatch')
        self.assertEqual(saved['phase'], 'resolution_mismatch')
        self.assertEqual(saved['video_url'], 'https://project.example/videos/fixture.mp4')
        self.assertTrue(self.path.exists())
        evidence = next(event['details'] for event in self.store.task_events('fixture')
                        if event['phase'] == 'download_metadata')
        self.assertEqual(evidence['dimensions'], {'width': 1280, 'height': 720})
        self.assertEqual(evidence['download_source'], 'official_download')
        self.assertEqual(evidence['watermark'], 'verified_download')

    def test_successful_doubao_delivery_keeps_metadata_and_uses_real_file_duration(self):
        row = self.task()
        persist_result(self.ns, row, {'status': 'completed', 'output_path': str(self.path),
            'actual_duration': 999, 'dimensions': {'width': 1920, 'height': 1080},
            'download_source': 'official_download', 'watermark': 'verified_download'})
        saved = self.store.get('fixture')
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(saved['actual_duration'], 15)
        self.assertTrue(any(event['phase'] == 'download_metadata'
                            for event in self.store.task_events('fixture')))

    def test_dola_completion_keeps_duration_checks_and_its_own_source_metadata(self):
        row = self.task(platform='dola')
        persist_result(self.ns, row, {'output_path': str(self.path),
            'download_source': 'dola_video_model', 'watermark': 'verified_download'})
        saved = self.store.get('fixture')
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(saved['actual_duration'], 15)
        metadata = next(event['details'] for event in self.store.task_events('fixture')
                        if event['phase'] == 'download_metadata')
        self.assertEqual(metadata['download_source'], 'dola_video_model')


if __name__ == '__main__':
    unittest.main()
