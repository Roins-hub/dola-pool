"""The observed Dola daily-limit reply is terminal, not a pending receipt."""
import time
import unittest
from unittest.mock import AsyncMock, patch

from video_outcome import conversation_outcome
import video_worker_ui as worker

REPLY = ('Du hast das tägliche kostenlose Nutzungslimit für Videogenerierung erreicht. '
         'Du kannst aber weiterhin im Fast-Modus chatten.')


class DailyVideoLimitTests(unittest.TestCase):
    def test_observed_reply_stops_waiting_for_a_generation_receipt(self):
        for model in ('seedance-2.0', 'seedance_v2.0', 'seedance-2.5', 'ic_mini'):
            with self.subTest(model=model):
                result = conversation_outcome({'assistant_messages': [[REPLY]], 'videos': []}, model)
                self.assertEqual(result['state'], 'failed')
                self.assertEqual(result['error'].task_phase, 'platform_daily_limit')
                self.assertIn('每日', str(result['error']))
                self.assertEqual(result['error'].reply, REPLY)
                self.assertFalse(getattr(result['error'], 'quota_not_consumed', False))

    def test_split_and_markdown_reply_is_still_terminal(self):
        result = conversation_outcome({'assistant_messages': [
            ['**Du hast das tägliche kostenlose Nutzungslimit', 'für Videogenerierung erreicht.**',
             'Du kannst aber weiterhin im Fast-Modus chatten.']]}, 'seedance-2.0')
        self.assertEqual(result['state'], 'failed')

    def test_quotes_prompts_and_actual_completed_videos_are_not_quota_failures(self):
        for quoted in ('Beispiel: '+REPLY, '> '+REPLY, '```\n'+REPLY+'\n```',
                       'Wenn du später das tägliche kostenlose Nutzungslimit erreichst, kannst du chatten.'):
            with self.subTest(reply=quoted):
                self.assertIsNone(conversation_outcome({'assistant_messages':[[quoted]]})['error'])
        self.assertIsNone(conversation_outcome({'assistant_messages':[[REPLY]]}, submitted_prompt=REPLY)['error'])
        result = conversation_outcome({'assistant_messages':[[REPLY]], 'videos':['https://cdn.test/real.mp4']})
        self.assertEqual(result['state'], 'completed')


class DailyVideoLimitWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_background_polling_stops_on_first_daily_limit_reply(self):
        fetch = AsyncMock(return_value={'assistant_messages': [[REPLY]], 'videos': []})
        download = AsyncMock()
        with patch.object(worker.asyncio, 'sleep', new=AsyncMock()):
            with self.assertRaises(Exception) as caught:
                await worker._wait_for_video('fixture', '123', time.time()+.05,
                    fetch, download, model='seedance-2.0')
        self.assertEqual(getattr(caught.exception,'task_phase',None), 'platform_daily_limit')
        self.assertEqual(fetch.await_count, 1)
        download.assert_not_awaited()
