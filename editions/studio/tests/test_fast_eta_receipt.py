"""Observed Fast ETA receipts, scoped to an explicitly requested Fast model."""
import unittest
from unittest.mock import AsyncMock, patch

from video_outcome import conversation_outcome, is_fast_receipt, is_seedance25_receipt


FAST_ETA_RECEIPT = (
    'This video will be generated using the Seedance 2.0 model in 15 minutes. '
    "I'll let you know when it's ready. You have 3 video credits left today."
)
FAST_GERMAN_ETA_RECEIPT = (
    'Dieses Video wird mithilfe des **Seedance\u00a02.0 Modells** in 15\u00a0Minuten generiert. '
    'Ich informiere dich, wenn es fertig ist. Du hast heute noch 3\u00a0Video-Credits übrig.'
)
FAST_CHINESE_INTRO = '我将按你的要求直接生成一条15秒横屏16:9视频，完整使用你给出的提示词，不修改内容。'
FAST_CHINESE_VARIANT = '我将按你的要求生成一条15秒的横屏视频，不修改提示词，直接原样输入。'


class FastEtaReceiptTests(unittest.TestCase):
    def test_native_receipt_block_survives_variable_chinese_preamble(self):
        for intro in (FAST_CHINESE_INTRO, FAST_CHINESE_VARIANT, '好的，正在为你生成视频。'):
            blocks = [intro + '\n\n', '\n' + FAST_GERMAN_ETA_RECEIPT]
            for poll in ({'assistant_messages': [blocks]}, {'assistant_texts': blocks}):
                with self.subTest(intro=intro, poll=poll):
                    self.assertEqual(conversation_outcome(poll, 'seedance_v2.0')['state'], 'accepted')

    def test_native_block_match_keeps_guards_for_the_entire_assistant_message(self):
        for guard in (
            'Die Generierung hat noch nicht begonnen.',
            'Generation has not started.',
            'Bitte bestätige, ob ich das Video generieren soll.',
            'Please confirm before I start generation.',
            'The selected model is Seedance 2.5.',
        ):
            for blocks in ([guard, FAST_GERMAN_ETA_RECEIPT], [FAST_GERMAN_ETA_RECEIPT, guard]):
                with self.subTest(blocks=blocks):
                    self.assertNotEqual(conversation_outcome({'assistant_messages': [blocks]},
                                                            'seedance_v2.0')['state'], 'accepted')

    def test_example_context_in_another_native_block_is_not_receipt_evidence(self):
        for intro in ('以下只是回复示例：', 'Example:', 'Zitat:', '引用：', '> 引用', '```'):
            with self.subTest(intro=intro):
                self.assertNotEqual(conversation_outcome({'assistant_messages': [[
                    intro, FAST_GERMAN_ETA_RECEIPT]]}, 'seedance_v2.0')['state'], 'accepted')

    def test_observed_chinese_intro_followed_by_german_receipt_confirms_fast_task(self):
        text = FAST_CHINESE_INTRO + '\n\n\n\n\n' + FAST_GERMAN_ETA_RECEIPT
        self.assertEqual(conversation_outcome({'assistant_messages': [[text]]}, 'seedance_v2.0')['state'],
                         'accepted')
        for model in ('seedance_v2.5', 'ic_mini', None):
            with self.subTest(model=model):
                self.assertEqual(conversation_outcome({'assistant_texts': [text]}, model)['state'],
                                 'awaiting_receipt')

    def test_chinese_intro_does_not_override_pending_negation_or_another_model(self):
        for receipt in (
            FAST_GERMAN_ETA_RECEIPT.replace('wird mithilfe', 'wird nicht mithilfe'),
            FAST_GERMAN_ETA_RECEIPT + ' Die Generierung hat noch nicht begonnen.',
            FAST_GERMAN_ETA_RECEIPT + ' Bitte bestätige, ob ich das Video generieren soll.',
            FAST_GERMAN_ETA_RECEIPT.replace('Seedance\u00a02.0', 'Seedance\u00a02.5'),
            FAST_GERMAN_ETA_RECEIPT + ' The selected model is Seedance 2.5.',
            'Seedance 2.0: Du hast heute noch 3 Video-Credits übrig.',
        ):
            with self.subTest(receipt=receipt):
                self.assertNotEqual(conversation_outcome({'assistant_texts': [
                    FAST_CHINESE_INTRO + '\n\n' + receipt]}, 'seedance_v2.0')['state'], 'accepted')

    def test_quoted_or_example_receipts_after_an_intro_are_not_generation_evidence(self):
        receipt = FAST_CHINESE_INTRO + '\n\n' + FAST_GERMAN_ETA_RECEIPT
        for text in (
            '以下只是回复示例，当前任务尚未提交：\n\n' + receipt,
            'Example:\n\n' + receipt,
            'Zitat:\n\n' + receipt,
            '“' + receipt + '”',
            '> ' + receipt,
            '```text\n' + receipt + '\n```',
            FAST_CHINESE_INTRO + '\n\n> ' + FAST_GERMAN_ETA_RECEIPT,
            FAST_CHINESE_INTRO + '\n\n```\n' + FAST_GERMAN_ETA_RECEIPT + '\n```',
        ):
            with self.subTest(text=text):
                self.assertNotEqual(conversation_outcome({'assistant_texts': [text]},
                                                        'seedance_v2.0')['state'], 'accepted')

    def test_observed_german_model_promise_and_eta_are_accepted_only_in_fast_context(self):
        for text in (FAST_GERMAN_ETA_RECEIPT, FAST_GERMAN_ETA_RECEIPT.split(' Ich ')[0]):
            self.assertEqual(conversation_outcome({'assistant_texts': [text]}, 'seedance_v2.0')['state'],
                             'accepted')
            for model in ('seedance_v2.5', 'ic_mini', None):
                self.assertEqual(conversation_outcome({'assistant_texts': [text]}, model)['state'],
                                 'awaiting_receipt')

    def test_german_confirmation_negation_and_balance_only_do_not_create_fast_acceptance(self):
        for text in (
            FAST_GERMAN_ETA_RECEIPT.replace('wird mithilfe', 'wird nicht mithilfe'),
            FAST_GERMAN_ETA_RECEIPT + ' Die Generierung hat noch nicht begonnen.',
            FAST_GERMAN_ETA_RECEIPT + ' Bitte bestätige, ob ich das Video generieren soll.',
            'Seedance 2.0: Du hast heute noch 3 Video-Credits übrig.',
        ):
            self.assertNotEqual(conversation_outcome({'assistant_texts': [text]}, 'seedance_v2.0')['state'],
                                'accepted')

    def test_observed_fast_generation_promise_and_eta_are_accepted_without_a_consumption_count(self):
        for model in ('seedance-2.0', 'seedance_v2.0', 'seedance_20', 'SEEDANCE-V20'):
            for text in (FAST_ETA_RECEIPT, FAST_ETA_RECEIPT.split(" I'll")[0],
                         FAST_ETA_RECEIPT.replace('3 video credits', '0 video credits')):
                with self.subTest(model=model, text=text):
                    outcome = conversation_outcome({'assistant_messages': [[text]]}, model)
                    self.assertEqual(outcome['state'], 'accepted')
                    self.assertIsNone(outcome['error'])

    def test_markdown_and_layout_whitespace_keep_the_same_named_model_commitment(self):
        for text in (
            '**This video** will be generated using the **Seedance 2.0 model** in **15 minutes**.',
            ' \nThis video\n will be generated using the `Seedance 2.0` model\n in 15\u00a0minutes. ',
            'The video will be generated using Dreamina Seedance 2.0 Fast in 15 minutes.',
        ):
            with self.subTest(text=text):
                self.assertEqual(conversation_outcome({'assistant_texts': [text]}, 'seedance_v2.0')['state'],
                                 'accepted')

    def test_new_fast_format_does_not_change_other_model_or_missing_context_behavior(self):
        for model in ('seedance_v2.5', 'seedance-2.5', 'ic_mini', 'seedance-1.0',
                      'seedance_v2.0_std', None):
            with self.subTest(model=model):
                outcome = conversation_outcome({'assistant_messages': [[FAST_ETA_RECEIPT]]}, model)
                self.assertEqual(outcome['state'], 'awaiting_receipt')
                self.assertIsNone(outcome['error'])
        # The new format cannot silently alter the final cross-model guard for 2.5/1.0.
        for model in ('seedance_v2.5', 'ic_mini'):
            outcome = conversation_outcome({'assistant_messages': [[FAST_ETA_RECEIPT],
                ['Please confirm whether I should generate the video.']]}, model)
            self.assertEqual(outcome['state'], 'not_accepted')

    def test_negative_pending_balance_and_estimate_text_are_not_new_fast_acceptance(self):
        for text in (
            FAST_ETA_RECEIPT.replace('will be generated', 'will not be generated'),
            FAST_ETA_RECEIPT + ' Generation has not started.',
            FAST_ETA_RECEIPT + " It hasn't started generating.",
            FAST_ETA_RECEIPT + ' Please confirm before I start generation.',
            FAST_ETA_RECEIPT + ' Would you like me to generate the video?',
            'Seedance 2.0. You have 3 video credits left today.',
            'Seedance 2.0 video generation typically takes 15 minutes.',
            'This video can be generated using the Seedance 2.0 model in 15 minutes.',
            FAST_ETA_RECEIPT.replace('Seedance 2.0', 'Seedance 2.5'),
            FAST_ETA_RECEIPT + ' The selected model is Seedance 2.5.',
        ):
            with self.subTest(text=text):
                self.assertNotEqual(conversation_outcome({'assistant_texts': [text]}, 'seedance_v2.0')['state'],
                                    'accepted')

    def test_confirmation_in_another_block_of_the_same_message_still_requires_confirmation(self):
        outcome = conversation_outcome({'assistant_messages': [[FAST_ETA_RECEIPT,
            'Please confirm whether I should generate the video.']]}, 'seedance_v2.0')
        self.assertEqual(outcome['state'], 'not_accepted')

    def test_real_fast_receipt_survives_a_later_confirmation_in_another_message(self):
        outcome = conversation_outcome({'assistant_messages': [[FAST_ETA_RECEIPT],
            ['Please confirm whether I should generate the next video.']]}, 'seedance_v2.0')
        self.assertEqual(outcome['state'], 'accepted')

    def test_submitted_prompt_echo_or_native_user_label_cannot_be_receipt_evidence(self):
        for text, prompt in ((FAST_ETA_RECEIPT, FAST_ETA_RECEIPT),
                             ('Generated video: ' + FAST_ETA_RECEIPT, 'different user prompt')):
            with self.subTest(text=text):
                outcome = conversation_outcome({'assistant_texts': [text]}, 'seedance_v2.0', prompt)
                self.assertEqual(outcome['state'], 'awaiting_receipt')

    def test_legacy_model_and_credit_receipt_functions_keep_their_original_rules(self):
        self.assertFalse(is_fast_receipt(FAST_ETA_RECEIPT))
        self.assertFalse(is_seedance25_receipt(FAST_ETA_RECEIPT))
        self.assertTrue(is_fast_receipt('Seedance 2.0. It will use 3 points.'))
        self.assertTrue(is_seedance25_receipt('Generating with Seedance 2.5. This will use 2 credits.'))
        self.assertFalse(is_seedance25_receipt('Generating with Seedance 2.5. This will use 1 credit.'))


class FastEtaReceiptWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_original_conversation_read_accepts_the_observed_fast_receipt(self):
        from test_video_pipeline import pool_module  # Isolate the imported ISP-usage database.
        import video_worker_ui as worker

        class Page:
            def __init__(self):
                self.reads = 0
                self.waits = 0

            async def evaluate(self, *_):
                self.reads += 1
                return {'assistant_messages': [[FAST_ETA_RECEIPT]], 'videos': []}

            async def wait_for_timeout(self, *_):
                self.waits += 1

        page, events = Page(), []
        with patch.object(worker, '_dismiss_login_modal', AsyncMock(return_value=False)):
            await worker._wait_for_generation_receipt(page, 'fixture', '123', '用户原文', timeout=.05,
                model='seedance_v2.0', on_event=lambda phase, details: events.append((phase, details)))
        self.assertEqual(page.reads, 1)
        self.assertEqual(page.waits, 0)
        self.assertEqual([phase for phase, _ in events].count('accepted'), 1)


if __name__ == '__main__':
    unittest.main()
