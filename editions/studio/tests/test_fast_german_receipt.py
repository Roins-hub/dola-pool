"""Real Dola Fast receipt with repeated German minutes, 2026-10-27."""
import time
import unittest
from unittest.mock import AsyncMock, patch

import video_worker_ui as worker
from video_outcome import conversation_outcome


RECEIPT = ('Das Video wird mit dem **Dreamina Seedance\u00a02.0 Fast-Modell** '
           'in 5\u00a0Minuten\u00a0Minuten generiert. Ich sende es dir, wenn es fertig ist. '
           'Für diese Generierung werden deine täglichen kostenlosen Credits verwendet.')


class GermanFastReceiptTests(unittest.IsolatedAsyncioTestCase):
    def test_observed_fast_receipt_and_native_block_variants_are_accepted(self):
        for text in (RECEIPT, RECEIPT.replace('\u00a0Minuten\u00a0Minuten', ' Minuten'),
                     RECEIPT.replace('Fast-Modell', 'Fast‑Modell'), RECEIPT.replace('**', '')):
            for blocks in ([text], ['正在为您生成视频，请稍等片刻。', text]):
                with self.subTest(text=text, blocks=blocks):
                    self.assertEqual(conversation_outcome({'assistant_messages': [blocks]},
                                                          'seedance-2.0')['state'], 'accepted')

    def test_other_models_and_non_receipts_are_not_promoted(self):
        for model in ('seedance_v2.5', 'ic_mini', None):
            self.assertNotEqual(conversation_outcome({'assistant_texts': [RECEIPT]}, model)['state'], 'accepted')
        for text in ('Example:\n' + RECEIPT, '> ' + RECEIPT, '```\n' + RECEIPT + '\n```',
                     RECEIPT.replace('wird mit dem', 'wird nicht mit dem'),
                     RECEIPT + ' Die Generierung hat noch nicht begonnen.',
                     RECEIPT + ' Bitte bestätige, ob ich das Video generieren soll.',
                     RECEIPT + ' The selected model is Seedance 2.5.',
                     'Dreamina Seedance 2.0 Fast: 5 Minuten, tägliche kostenlose Credits.'):
            with self.subTest(text=text):
                self.assertNotEqual(conversation_outcome({'assistant_texts': [text]},
                                                         'seedance-2.0')['state'], 'accepted')

    async def test_visible_receipt_wait_accepts_immediately_without_resending(self):
        events = []
        page = type('Page', (), {'wait_for_timeout': AsyncMock()})()
        with patch.object(worker, '_dismiss_login_modal', AsyncMock()), \
             patch.object(worker, '_read_page_conversation', AsyncMock(return_value={
                 'assistant_messages': [['正在为您生成视频，请稍等片刻。', RECEIPT]], 'videos': []})):
            await worker._wait_for_generation_receipt(page, 'fixture', '123', 'original prompt',
                model='seedance-2.0', timeout=.02,
                on_event=lambda phase, details: events.append(phase))
        self.assertEqual(events.count('accepted'), 1)
        page.wait_for_timeout.assert_not_awaited()

    async def test_background_query_keeps_accepted_until_video_is_ready(self):
        events = []
        with patch.object(worker.asyncio, 'sleep', AsyncMock()):
            with self.assertRaises(worker.VideoQueryTimeout):
                await worker._wait_for_video('fixture', '123', time.time()+.01,
                    AsyncMock(return_value={'assistant_texts': [RECEIPT], 'videos': []}), AsyncMock(),
                    model='seedance-2.0', on_event=lambda phase, details: events.append(phase))
        self.assertIn('accepted', events)
        self.assertNotIn('completed', events)


if __name__ == '__main__': unittest.main()
