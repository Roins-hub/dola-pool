import tempfile
import unittest
from pathlib import Path
from store import TaskStore
from test_video_metadata import mp4
from task_results import complete_task, fail_task
from test_video_delivery import original_result


class TaskResultTests(unittest.TestCase):
    def test_watermark_failure_keeps_attempted_account_without_claiming_submission(self):
        from dola_watermark import WatermarkPreferenceError
        store = TaskStore(':memory:')
        try:
            store.create('fixture', 'seedance-2.0', '原文', '9:16', 10)
            error = WatermarkPreferenceError('无水印设置读取超时，提示词未发送')
            error.account = 'fixture-account'
            fail_task(store, 'fixture', error)
            row = store.get('fixture')
            self.assertEqual(row['account'], 'fixture-account')
            self.assertEqual(row['phase'], 'watermark_setup_failed')
            self.assertIsNone(row['conversation_id'])
            self.assertIsNone(row['accepted_at'])
            self.assertIsNone(row['submitted_duration'])
        finally:
            store._conn.close()

    def test_a_15_second_result_does_not_report_30_second_success_but_remains_viewable(self):
        with tempfile.TemporaryDirectory() as root:
            store = TaskStore(':memory:')
            try:
                store.create('fixture', 'seedance-2.5', '原文', '9:16', 30)
                path = Path(root) / 'video.mp4'
                path.write_bytes(mp4(15))
                complete_task(store, 'fixture', {**original_result(path), 'account': 'fixture'}, 'http://local')
                row = store.get('fixture')
                self.assertEqual(row['status'], 'failed')
                self.assertEqual(row['phase'], 'duration_mismatch')
                self.assertEqual(row['actual_duration'], 15)
                self.assertEqual(row['video_url'], 'http://local/videos/video.mp4')
                path.write_bytes(mp4(29.97))
                complete_task(store, 'fixture', {**original_result(path), 'account': 'fixture'}, 'http://local')
                self.assertEqual(store.get('fixture')['status'], 'completed')
                self.assertEqual(store.get('fixture')['phase'], 'completed')
            finally:
                store._conn.close()
