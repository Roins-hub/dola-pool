"""A text-only promise is not proof that video generation has been accepted."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from dola_expert import expert_outcome
from plugin_expert30 import poll_outcome, receipt_outcome
from video_outcome import conversation_outcome
from video_polling import PollingResponseError, parse_poll_response


START_RECEIPT = (
    "Only two videos can be generated at a time. I'll start by generating the video "
    "for you. The video duration is 10 seconds, which is within the supported range "
    "of 4–15 seconds. I will generate it as requested."
)


def response(texts, *, user_type=2, cid='123', baseline_messages=(), separate_messages=False):
    return parse_poll_response({'downlink_body': {'pull_singe_chain_downlink_body': {
        'conversation_id': cid, 'messages': [{'conversation_id': cid, 'user_type': user_type,
            'message_id': 'reply-one' if index == 0 else f'reply-{index + 1}', 'content': json.dumps([
                {'block_type': 10000, 'content': {'text_block': {'text': text}}}
                for text in group])} for index, group in enumerate(
                    [[text] for text in texts] if separate_messages else [texts])]}}},
        '123', baseline_messages=baseline_messages)


class GenerationStartReceiptTests(unittest.TestCase):
    def test_text_promise_is_not_accepted_in_normal_or_expert_routes(self):
        for reply in (START_RECEIPT, START_RECEIPT.replace("I'll", 'I’ll'),
                      START_RECEIPT.replace('10 seconds', '15 seconds').replace('4–15', '4-15'),
                      '**' + START_RECEIPT.replace('. ', '.\n\n') + '**'):
            with self.subTest(reply=reply):
                poll = response([reply])
                outcomes = [conversation_outcome(poll, 'seedance-2.0'), expert_outcome(poll)[1],
                            poll_outcome(poll)[1], receipt_outcome(poll)[1]]
                self.assertEqual([outcome['state'] for outcome in outcomes], ['awaiting_receipt'] * 4)
                self.assertTrue(all(outcome['error'] is None for outcome in outcomes))
                self.assertEqual(poll['videos'], [])
                self.assertFalse(poll['plugin_expert30']['tools_finished'])

    def test_splitting_a_text_promise_into_native_text_blocks_does_not_confirm_generation(self):
        # Preserve the punctuation removed by split, as native text blocks do.
        poll = response([part + '. ' for part in START_RECEIPT.split('. ')[:-1]] +
                        [START_RECEIPT.split('. ')[-1]])
        self.assertEqual(conversation_outcome(poll, 'seedance-2.0')['state'], 'awaiting_receipt')
        self.assertEqual(receipt_outcome(poll)[1]['state'], 'awaiting_receipt')

    def test_limit_alone_examples_confirmation_and_negation_do_not_acknowledge_generation(self):
        for reply in (
            'Only two videos can be generated at a time.',
            'I will generate it as requested.',
            'Example: ' + START_RECEIPT,
            '> ' + START_RECEIPT,
            '“' + START_RECEIPT + '”',
            '```text\n' + START_RECEIPT + '\n```',
            START_RECEIPT + ' Please confirm before I start.',
            START_RECEIPT.replace('I will generate', 'I will not generate'),
            START_RECEIPT.replace('within the supported', 'outside the supported'),
        ):
            with self.subTest(reply=reply):
                poll = response([reply])
                self.assertNotIn(conversation_outcome(poll)['state'], ('accepted', 'completed'))
                self.assertNotIn(receipt_outcome(poll)[1]['state'], ('accepted', 'completed'))

    def test_user_echo_other_conversation_and_baseline_are_excluded(self):
        for options in ({'user_type': 1}, {'baseline_messages': ['reply-one']}):
            with self.subTest(options=options):
                poll = response([START_RECEIPT], **options)
                self.assertEqual(conversation_outcome(poll)['state'], 'awaiting_receipt')
                self.assertEqual(expert_outcome(poll)[1]['state'], 'awaiting_receipt')
                self.assertEqual(receipt_outcome(poll)[1]['state'], 'awaiting_receipt')
        with self.assertRaises(PollingResponseError):
            response([START_RECEIPT], cid='999')
        self.assertEqual(conversation_outcome(response([START_RECEIPT]),
                         submitted_prompt=START_RECEIPT)['state'], 'awaiting_receipt')

    def test_platform_refusal_takes_precedence_over_textual_start_receipt(self):
        for separate in (False, True):
            with self.subTest(separate_messages=separate):
                poll = response([START_RECEIPT,
                    'Du hast das tägliche kostenlose Nutzungslimit für Videogenerierung erreicht.'],
                    separate_messages=separate)
                for outcome in (conversation_outcome(poll), expert_outcome(poll)[1],
                                receipt_outcome(poll)[1]):
                    self.assertEqual(outcome['state'], 'failed')

    def test_other_assistant_messages_cannot_turn_a_promise_into_a_receipt(self):
        first, rest = START_RECEIPT.split('The video duration', 1)
        for texts, expected in (([first, 'The video duration' + rest], 'awaiting_receipt'),
                                (['Preparing your request.', START_RECEIPT], 'awaiting_receipt'),
                                (['Example:', START_RECEIPT], 'awaiting_receipt')):
            with self.subTest(texts=texts):
                poll = response(texts, separate_messages=True)
                for outcome in (conversation_outcome(poll), expert_outcome(poll)[1],
                                poll_outcome(poll)[1], receipt_outcome(poll)[1]):
                    self.assertEqual(outcome['state'], expected)


class GenerationStartRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_expert_recovery_does_not_report_acceptance_for_a_text_promise(self):
        import dola_expert
        import plugin_expert30

        class StopPolling(Exception):
            pass

        for worker in (dola_expert, plugin_expert30):
            with self.subTest(worker=worker.__name__):
                poll = response([START_RECEIPT])
                session = SimpleNamespace(poll=AsyncMock(side_effect=[poll, poll, StopPolling()]))
                context = AsyncMock()
                context.__aenter__.return_value = session
                events = []
                with patch('video_polling.HttpVideoSession', return_value=context), \
                     patch('expert_trace.record_poll'), patch.object(worker.asyncio, 'sleep', AsyncMock()):
                    with self.assertRaises(StopPolling):
                        await worker.resume_video('fixture', '123', 5,
                            on_event=lambda phase, details: events.append(phase))
                self.assertEqual(events.count('accepted'), 0)
                self.assertNotIn('downloading', events)
                self.assertNotIn('completed', events)


if __name__ == '__main__':
    unittest.main()
