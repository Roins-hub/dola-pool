import unittest
from unittest.mock import patch

from test_video_pipeline import pool_module
import video_worker_ui as worker


class ModelSubmissionGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_seedance_one_rejects_unsupported_durations_before_browser_launch(self):
        for duration in (30,):
            with self.subTest(duration=duration), patch.object(worker, 'async_playwright') as launch:
                with self.assertRaises(ValueError):
                    await worker.generate_video('fixture', '原文', duration=duration, model='seedance-1.0')
                launch.assert_not_called()

    async def test_fast_unsupported_duration_never_opens_a_submission_browser(self):
        for duration in (20, 35):
            with self.subTest(duration=duration), patch.object(worker, 'async_playwright') as launch:
                launch.side_effect = AssertionError('Unsupported Fast duration reached browser launch')
                with self.assertRaises(ValueError):
                    await worker.generate_video('fixture', '原文', duration=duration, model='seedance-2.0')
                launch.assert_not_called()
