import asyncio
import unittest
from unittest.mock import patch

from test_video_pipeline import pool_module
import video_worker_ui as worker
from video_outcome import conversation_outcome
from video_polling import parse_poll_response


FAST_REPLY = (
    'Die maximale unterstützte Videolänge beträgt 15 Sekunden. Ich generiere das Video in 9:16 '
    'mit einer zusammengefassten 15-Sekunden-Fassung, die alle Kerninformationen enthält.\n\n'
    'Das Video wird mithilfe des **Seedance\u00a02.0\u00a0Modells** generiert. Dafür werden 2\u00a0Punkte verwendet. '
    'Es ist in 5\u00a0Minuten fertig. Ich informiere dich, wenn es fertig ist. '
    'Dir bleiben heute noch 2\u00a0Video-Credits.'
)


def original_conversation(text, role='assistant'):
    return parse_poll_response({'downlink_body': {'pull_singe_chain_downlink_body': {
        'conversation_id': '123', 'messages': [{'id': 'reply', 'role': role,
            'content': [{'content': {'text_block': {'text': text}}}]}]}}}, '123')


class FastPointsReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_screenshot_points_receipt_confirms_original_fast_task(self):
        poll = original_conversation(FAST_REPLY)
        self.assertEqual(conversation_outcome(poll, 'seedance-2.0', '完整用户提示词')['state'], 'accepted')

    async def test_points_receipt_releases_fast_browser_on_first_read(self):
        class Page:
            reads = 0
            async def evaluate(self, *args):
                self.reads += 1
                return original_conversation(FAST_REPLY)
            async def wait_for_timeout(self, *args):
                await asyncio.sleep(0)
        page = Page()
        events = []
        with patch.object(worker, '_dismiss_login_modal', return_value=False):
            await worker._wait_for_generation_receipt(page, 'fixture', '123', '完整用户提示词',
                timeout=.02, model='seedance_v2.0', on_event=lambda phase, details: events.append(phase))
        self.assertEqual(page.reads, 1)
        self.assertIn('accepted', events)

    async def test_remaining_points_confirmation_and_prompt_echo_are_not_receipts(self):
        for text, role in (
            ('Seedance 2.0. Dir bleiben heute noch 2 Punkte.', 'assistant'),
            ('Seedance 2.0 benötigt 2 Punkte. Soll ich das Video generieren?', 'assistant'),
            ('Seedance 2.0: 2 Punkte werden nicht verbraucht, die Generierung wurde nicht gestartet.', 'assistant'),
            ('Das Video wird mit Seedance 2.0 generiert. Dafür werden 2 Punkte nicht verwendet.', 'assistant'),
            ('Das Video wird nicht mit Seedance 2.0 generiert. Dafür werden 2 Punkte verwendet.', 'assistant'),
            (FAST_REPLY, 'user'),
        ):
            with self.subTest(text=text, role=role):
                self.assertNotEqual(conversation_outcome(original_conversation(text, role), 'seedance-2.0')['state'], 'accepted')

    async def test_fast_receipt_does_not_confirm_a_seedance25_task(self):
        self.assertEqual(conversation_outcome(original_conversation(FAST_REPLY), 'seedance-2.5')['state'], 'awaiting_receipt')
