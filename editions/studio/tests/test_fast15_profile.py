"""Keep the new 15-second output contract separate from its 30-second input."""
import tempfile
import unittest
from pathlib import Path

import video_capabilities as capabilities
from store import TaskStore
from task_results import complete_task
from test_video_delivery import original_result
from video_prompt import build_video_prompt
from test_video_metadata import mp4


class Fast15ProfileTests(unittest.TestCase):
    def submission_duration(self, model, duration, profile=None):
        helper = getattr(capabilities, 'submission_duration', None)
        self.assertTrue(callable(helper), 'submission duration must be resolved separately from output duration')
        return helper(model, duration, profile)

    def test_new_fast_fifteen_uses_thirty_without_changing_its_submission_instructions(self):
        for model in ('seedance-2.0', 'seedance_v2.0', 'SEEDANCE-V20'):
            with self.subTest(model=model):
                duration = self.submission_duration(model, 15, 'fast15-via30')
                self.assertEqual(duration, 30)
                self.assertEqual(build_video_prompt(' 原文\n保持 ', duration, model),
                                 build_video_prompt(' 原文\n保持 ', 30, model))
                self.assertNotIn('使用1个额度生成15秒', build_video_prompt('原文', duration, model))

    def test_no_profile_keeps_every_existing_model_and_duration_pair(self):
        for model, durations in (('seedance-1.0', (5, 10, 15)),
                                 ('seedance-2.0', (5, 10, 15, 30)),
                                 ('seedance-2.5', (5, 10, 15, 30))):
            for duration in durations:
                with self.subTest(model=model, duration=duration):
                    self.assertEqual(self.submission_duration(model, duration), duration)

    def test_profile_cannot_change_other_models_or_durations_or_silently_ignore_typos(self):
        for model, duration, profile in (
            ('seedance-1.0', 15, 'fast15-via30'),
            ('seedance-2.5', 15, 'fast15-via30'),
            ('seedance-2.5', 30, 'fast15-via30'),
            ('seedance-2.0', 5, 'fast15-via30'),
            ('seedance-2.0', 10, 'fast15-via30'),
            ('seedance-2.0', 30, 'fast15-via30'),
            ('seedance-2.0', 15, 'fast15-via300'),
            ('seedance-2.0', 15, ''),
        ):
            with self.subTest(model=model, duration=duration, profile=profile):
                with self.assertRaises(ValueError):
                    self.submission_duration(model, duration, profile)

    def test_new_profile_checks_actual_output_against_fifteen_not_upstream_thirty(self):
        with tempfile.TemporaryDirectory() as root:
            store = TaskStore(':memory:')
            try:
                store.create('new-fast15', 'seedance-2.0', '原文', '9:16', 15,
                             duration_profile='fast15-via30')
                path = Path(root) / 'video.mp4'
                for actual, expected in ((15.105, 'completed'), (10, 'failed'), (30, 'failed')):
                    with self.subTest(actual=actual):
                        path.write_bytes(mp4(actual))
                        complete_task(store, 'new-fast15', original_result(path), 'http://local')
                        row = store.get('new-fast15')
                        self.assertEqual(row['status'], expected)
                        self.assertEqual(row['duration'], 15)
                        self.assertEqual(row['duration_profile'], 'fast15-via30')
                        self.assertAlmostEqual(row['actual_duration'], actual, places=2)
            finally:
                store._conn.close()

    def test_unmarked_fast_thirty_still_rejects_a_fifteen_second_result(self):
        with tempfile.TemporaryDirectory() as root:
            store = TaskStore(':memory:')
            try:
                store.create('old-fast30', 'seedance-2.0', '原文', '9:16', 30)
                path = Path(root) / 'video.mp4'
                path.write_bytes(mp4(15.105))
                complete_task(store, 'old-fast30', original_result(path), 'http://local')
                row = store.get('old-fast30')
                self.assertEqual(row['status'], 'failed')
                self.assertEqual(row['phase'], 'duration_mismatch')
                self.assertEqual(row['duration'], 30)
            finally:
                store._conn.close()
