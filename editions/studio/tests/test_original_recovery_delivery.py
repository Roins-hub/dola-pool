"""Existing files only short-circuit recovery after their provenance passes."""
from pathlib import Path
import unittest

import test_task_evidence_api as evidence_api
from task_results import complete_task
from test_video_metadata import mp4


class OriginalRecoveryDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Reuse the real route/store harness without inheriting its test cases.
        self.fixture = evidence_api.TaskEvidenceAPITests()
        await self.fixture.asyncSetUp()
        self.addAsyncCleanup(self.fixture.asyncTearDown)
        self.path = Path(self.fixture.root.name) / 'fixture.mp4'
        self.path.write_bytes(mp4(30))
        self.url = '/api/admin/tasks/video_fixture/download'

    def publish_verified(self):
        complete_task(self.fixture.store, 'video_fixture', {
            'local_path': str(self.path), 'account': 'original',
            'download_source': 'dola_video_model', 'watermark': 'verified_download',
            'video_url': 'https://v16-dola.dola.com/original.mp4?lr=unwatermarked',
        }, 'http://local')

    async def test_unknown_legacy_cache_continues_original_collection_in_same_request(self):
        self.fixture.store.update('video_fixture', status='completed', phase='completed',
                                  video_url='http://local/videos/fixture.mp4')
        self.fixture.poll['videos'] = ['https://v16-dola.dola.com/original.mp4']

        result = await self.fixture.request(self.url, 'POST')

        self.assertEqual(result.status_code, 200)
        self.assertEqual(self.fixture.resumes, [('original', '123')])
        task = result.json()['task']
        self.assertEqual(task['status'], 'completed')
        self.assertEqual(task['video_url'], 'http://local/videos/fixture.mp4')
        self.assertEqual(task['conversation_id'], '123')
        self.assertTrue(self.fixture.resume_options[0]['accepted'])
        self.assertEqual(len(self.fixture.store.recent_tasks()), 1)

    async def test_verified_cache_remains_idempotent_without_query_or_download(self):
        self.publish_verified()
        self.fixture.poll_error = AssertionError('Verified cache must not query the platform')

        for _ in range(2):
            result = await self.fixture.request(self.url, 'POST')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['task']['status'], 'completed')
            self.assertEqual(result.json()['task']['video_url'], 'http://local/videos/fixture.mp4')
        self.assertEqual(self.fixture.polls, [])
        self.assertEqual(self.fixture.resumes, [])

    async def test_verified_duration_mismatch_keeps_original_without_redownload(self):
        self.path.write_bytes(mp4(15))
        self.publish_verified()
        self.fixture.poll_error = AssertionError('Qualified mismatched original must not be downloaded again')

        result = await self.fixture.request(self.url, 'POST')

        self.assertEqual(result.status_code, 200)
        task = result.json()['task']
        self.assertEqual(task['status'], 'failed')
        self.assertEqual(task['failure_code'], 'duration_mismatch')
        self.assertEqual(task['actual_duration'], 15)
        self.assertEqual(task['video_url'], 'http://local/videos/fixture.mp4')
        self.assertEqual(self.fixture.polls, [])
        self.assertEqual(self.fixture.resumes, [])
