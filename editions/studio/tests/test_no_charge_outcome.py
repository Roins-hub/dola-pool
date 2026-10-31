"""No-charge classification is bounded to observed platform refusals, never account balances."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

import video_outcome as outcome

INPUT_REFUSAL = (
    "I can't generate the video because your input contains content that may violate our policies. "
    'Modify it and try again.'
)
WITHHELD = ("I can't show you the generated video because it may contain copyrighted or "
            'policy-violating content. Try something else.')
NO_CREDITS = 'No credits were used for this video.'


class NoChargeOutcomeTests(unittest.TestCase):
    def assert_no_charge(self, poll, expected_reply=None):
        result = outcome.conversation_outcome(poll, 'seedance_v2.0', 'unrelated original prompt')
        self.assertEqual(result['state'], 'failed')
        error = result['error']
        self.assertEqual(getattr(error, 'task_phase', None), 'platform_rejected_no_charge')
        self.assertTrue(getattr(error, 'quota_not_consumed', False))
        self.assertIsInstance(error, outcome.PlatformVideoError)
        self.assertEqual(type(error).__name__, 'PlatformNoChargeError')
        if expected_reply is not None:
            self.assertEqual(error.reply, expected_reply)
        return error

    def test_observed_input_refusal_has_explicit_no_charge_contract(self):
        self.assert_no_charge({'assistant_messages': [[INPUT_REFUSAL]]}, INPUT_REFUSAL)

    def test_input_refusal_handles_cannot_smart_apostrophe_nbsp_and_light_emphasis(self):
        for text in (INPUT_REFUSAL.replace("can't", 'cannot'), INPUT_REFUSAL.replace("can't", 'can’t'),
                     '**' + INPUT_REFUSAL.replace(' ', '\u00a0') + '**'):
            with self.subTest(text=text):
                self.assert_no_charge({'assistant_messages': [[text]]}, text)

    def test_refusal_sentence_split_over_blocks_in_one_message_is_terminal(self):
        parts = ["I can't generate the video because your input", 'contains content that may violate our policies.',
                 'Modify it and try again.']
        self.assert_no_charge({'assistant_messages': [parts]}, '\n'.join(parts))

    def test_known_withheld_refusal_requires_same_message_known_no_credit_statement(self):
        self.assert_no_charge({'assistant_messages': [[WITHHELD, NO_CREDITS]]}, WITHHELD + '\n' + NO_CREDITS)
        self.assert_no_charge({'assistant_messages': [[WITHHELD + ' ' + NO_CREDITS]]})
        for text in (
            "I can't generate the video and didn't use any credit for this generation.",
            'I cannot generate the video and did not use any credits for this generation.',
            'Ich kann dir das generierte Video nicht anzeigen, da es möglicherweise urheberrechtlich '
            'geschützte oder richtlinienwidrige Inhalte enthält. Für dieses Video wurden keine Credits verwendet.',
        ):
            with self.subTest(text=text):
                self.assert_no_charge({'assistant_messages': [[text]]})

    def test_no_charge_reply_is_safe_without_credentials_or_signed_links(self):
        error = self.assert_no_charge({'assistant_messages': [[INPUT_REFUSAL +
            '\nCookie: private-cookie\nhttps://site.test/?token=private-token']]})
        self.assertNotIn('private-cookie', error.reply)
        self.assertNotIn('private-token', error.reply)
        self.assertNotIn('private-cookie', str(error))

    def test_ordinary_refusals_without_no_charge_proof_keep_unknown_billing(self):
        for text in (WITHHELD, 'Ich kann das Video nicht generieren.',
                     WITHHELD + ' You still have 0 video credits left today.',
                     WITHHELD + ' No credits were used for another video.',
                     WITHHELD + ' No credits will be used for this video.',
                     WITHHELD + ' > ' + NO_CREDITS):
            with self.subTest(text=text):
                result = outcome.conversation_outcome({'assistant_messages': [[text]]}, 'seedance_v2.0')
                self.assertEqual(result['state'], 'failed')
                self.assertFalse(getattr(result['error'], 'quota_not_consumed', False))
                self.assertEqual(result['error'].task_phase, 'platform_rejected')

    def test_no_credit_statement_without_a_refusal_is_not_terminal_or_refundable(self):
        result = outcome.conversation_outcome({'assistant_messages': [[NO_CREDITS]]}, 'seedance_v2.0')
        self.assertEqual(result['state'], 'awaiting_receipt')
        self.assertIsNone(result['error'])

    def test_no_credit_statement_is_never_borrowed_from_another_message_or_flat_unknown_boundaries(self):
        for poll in ({'assistant_messages': [[NO_CREDITS], [WITHHELD]]},
                     {'assistant_messages': [[WITHHELD], [NO_CREDITS]]},
                     {'assistant_texts': [WITHHELD, NO_CREDITS]}):
            result = outcome.conversation_outcome(poll, 'seedance_v2.0')
            self.assertEqual(result['state'], 'failed')
            self.assertFalse(getattr(result['error'], 'quota_not_consumed', False))

    def test_echo_quotations_fenced_code_and_examples_are_not_refusals(self):
        echoed = outcome.conversation_outcome({'assistant_texts': [INPUT_REFUSAL]}, 'seedance_v2.0', INPUT_REFUSAL)
        self.assertEqual(echoed['state'], 'awaiting_receipt')
        for blocks in (["Example:", INPUT_REFUSAL], ['Zitat:', INPUT_REFUSAL],
                       ['> ' + INPUT_REFUSAL], ['“' + INPUT_REFUSAL + '”'],
                       ['```text', INPUT_REFUSAL, '```'], ['`' + INPUT_REFUSAL + '`'],
                       ['Generated video: ' + INPUT_REFUSAL], ['Generiertes Video: ' + INPUT_REFUSAL]):
            with self.subTest(blocks=blocks):
                result = outcome.conversation_outcome({'assistant_messages': [blocks]}, 'seedance_v2.0')
                self.assertEqual(result['state'], 'awaiting_receipt')
                self.assertIsNone(result['error'])

    def test_late_input_refusal_overrides_old_receipt_but_a_video_always_wins(self):
        receipt = 'Generating with Seedance 2.5. This will use 2 credits.'
        for groups in ([[receipt], [INPUT_REFUSAL]], [[INPUT_REFUSAL], [receipt]]):
            self.assert_no_charge({'assistant_messages': groups})
            result = outcome.conversation_outcome({'assistant_messages': groups, 'videos': ['fixture']}, 'seedance_v2.5')
            self.assertEqual(result['state'], 'completed')
            self.assertIsNone(result['error'])


class NoChargeWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_wait_emits_no_charge_before_raising_without_sleeping(self):
        from test_video_pipeline import pool_module
        import video_worker_ui as worker
        class Page:
            evaluate = AsyncMock(return_value={'assistant_messages': [[INPUT_REFUSAL]], 'videos': []})
            wait_for_timeout = AsyncMock()
        page, events = Page(), []
        with patch.object(worker, '_dismiss_login_modal', AsyncMock(return_value=False)):
            with self.assertRaises(outcome.PlatformVideoError) as caught:
                await worker._wait_for_generation_receipt(page, 'fixture', '123', 'original prompt', timeout=.1,
                    model='seedance_v2.0', on_event=lambda phase, details: events.append((phase, details)))
        self.assertEqual(caught.exception.task_phase, 'platform_rejected_no_charge')
        self.assertEqual(events[-1][0], 'platform_rejected_no_charge')
        self.assertEqual(events[-1][1]['reply'], INPUT_REFUSAL)
        page.evaluate.assert_awaited_once()
        page.wait_for_timeout.assert_not_awaited()

    async def test_background_poll_emits_no_charge_after_saved_acceptance_and_does_not_download(self):
        from test_video_pipeline import pool_module
        import video_worker_ui as worker
        fetch = AsyncMock(return_value={'assistant_messages': [[INPUT_REFUSAL]], 'videos': []})
        download, events = AsyncMock(), []
        with self.assertRaises(outcome.PlatformVideoError) as caught:
            await worker._wait_for_video('fixture', '123', time.time()+1, fetch, download,
                on_event=lambda phase, details: events.append((phase, details)), model='seedance_v2.0', accepted=True)
        self.assertEqual(caught.exception.task_phase, 'platform_rejected_no_charge')
        self.assertEqual(events[-1][0], 'platform_rejected_no_charge')
        self.assertEqual(events[-1][1]['reply'], INPUT_REFUSAL)
        fetch.assert_awaited_once()
        download.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
