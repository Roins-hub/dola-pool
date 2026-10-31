import unittest

from video_evidence import public_details
from video_outcome import conversation_outcome, VideoQueryTimeout


FAST_ENGLISH_POINTS_RECEIPT = (
    'The video will be generated using the **Seedance 2.0 model**. '
    'It will use 3 points and be ready in 10 minutes. '
    "I'll let you know when it's done. You still have 1 video credits left today."
)
SEEDANCE_ONE_UNNAMED_RECEIPT = (
    'The video will use 2 credits and be ready in 5 minutes. '
    "I'll let you know when it's done.You still have 2 video credits left today."
)
REAL_PERSON_ENGLISH_REFUSAL = (
    "For likeness protection, Dreamina Seedance 2.0 doesn't support generating videos from images of real people. "
    'Use another image, or create a video from text.'
)
GERMAN_WITHHELD_VIDEO_REFUSAL = (
    'Ich kann dir das generierte Video nicht anzeigen, da es möglicherweise urheberrechtlich '
    'geschützte oder richtlinienwidrige Inhalte enthält. Versuche es mit etwas anderem. '
    'Für dieses Video wurden keine Credits verwendet.'
)
ENGLISH_INPUT_POLICY_REFUSAL = (
    "I can't generate the video because your input contains content that may violate our policies. "
    'Modify it and try again.'
)


class OutcomeTests(unittest.TestCase):
    def test_real_seedance_one_unnamed_consumption_and_completion_receipt_is_accepted(self):
        for model in ('ic_mini', 'seedance-1.0', 'seedance_v1.0', 'seedance_10'):
            with self.subTest(model=model):
                outcome = conversation_outcome({'assistant_messages': [[SEEDANCE_ONE_UNNAMED_RECEIPT]]}, model)
                self.assertEqual(outcome['state'], 'accepted')
                self.assertIsNone(outcome['error'])

    def test_unnamed_receipt_is_not_assigned_to_seedance_one_without_that_request_context(self):
        for model in (None, 'seedance_v2.0', 'seedance_v2.5'):
            outcome = conversation_outcome({'assistant_texts': [SEEDANCE_ONE_UNNAMED_RECEIPT]}, model)
            self.assertEqual(outcome['state'], 'awaiting_receipt')
            self.assertIsNone(outcome['error'])
        # A generic receipt must not become a named 1.0 receipt in the cross-model guard.
        outcome = conversation_outcome({'assistant_messages': [[SEEDANCE_ONE_UNNAMED_RECEIPT],
            ['Please confirm whether I should generate the video.']]}, 'seedance_v2.0')
        self.assertEqual(outcome['state'], 'not_accepted')

    def test_unnamed_consumption_and_completion_receipt_survives_later_confirmation(self):
        outcome = conversation_outcome({'assistant_messages': [[SEEDANCE_ONE_UNNAMED_RECEIPT],
            ['Please confirm whether I should generate the video.']]}, 'ic_mini')
        self.assertEqual(outcome['state'], 'accepted')
        self.assertIsNone(outcome['error'])

    def test_unnamed_receipt_rejects_negation_confirmation_balance_and_incomplete_estimates(self):
        for receipt in (
            'The video will not use 2 credits and be ready in 5 minutes.',
            'The video will use 2 credits and not be ready in 5 minutes.',
            'The video will use 2 credits and be ready in 5 minutes. Generation has not started.',
            "The video will use 2 credits and be ready in 5 minutes. It hasn't started generating.",
            'The video will use 2 credits and be ready in 5 minutes. No credits have been used.',
            'The video will use 2 credits and be ready in 5 minutes. Please confirm before I start generation.',
            'The video will use 2 credits and be ready in 5 minutes. Would you like me to generate the video?',
            'You still have 2 video credits left today. The video may be ready in 5 minutes.',
            'The video will use 2 credits.',
            'The video will be ready in 5 minutes.',
        ):
            with self.subTest(receipt=receipt):
                outcome = conversation_outcome({'assistant_texts': [receipt]}, 'ic_mini')
                self.assertNotEqual(outcome['state'], 'accepted')

    def test_unnamed_receipt_confirmation_in_another_block_of_the_same_message_is_not_acceptance(self):
        outcome = conversation_outcome({'assistant_messages': [[SEEDANCE_ONE_UNNAMED_RECEIPT,
            'Please confirm whether I should generate the video.']]}, 'ic_mini')
        self.assertEqual(outcome['state'], 'not_accepted')

    def test_explicit_other_model_still_stops_rotation_with_unnamed_style_consumption(self):
        for model in ('Seedance 2.0', 'Dreamina Seedance 2.5', 'Seedance 3.0'):
            for message in (model + '. ' + SEEDANCE_ONE_UNNAMED_RECEIPT,
                            SEEDANCE_ONE_UNNAMED_RECEIPT + ' Model: ' + model + '.'):
                with self.subTest(message=message):
                    outcome = conversation_outcome({'assistant_messages': [[message]]}, 'ic_mini')
                    self.assertEqual(outcome['state'], 'failed')
                    self.assertEqual(outcome['error'].task_phase, 'request_mismatch')
        outcome = conversation_outcome({'assistant_messages': [[SEEDANCE_ONE_UNNAMED_RECEIPT],
            ['Seedance 2.0. It will use 3 points.']]}, 'ic_mini')
        self.assertEqual(outcome['state'], 'failed')
        self.assertEqual(outcome['error'].task_phase, 'request_mismatch')

    def test_unnamed_receipt_echo_of_submitted_prompt_is_not_accepted(self):
        outcome = conversation_outcome({'assistant_texts': [SEEDANCE_ONE_UNNAMED_RECEIPT]},
            'ic_mini', SEEDANCE_ONE_UNNAMED_RECEIPT)
        self.assertEqual(outcome['state'], 'awaiting_receipt')

    def test_seedance_one_affirmative_credit_receipts_are_accepted(self):
        for receipt in (
            'The video will be generated using Seedance 1.0. It will use 1 point and be ready soon.',
            'The video will be generated using Seedance 1.0. It will use 1 credit.',
            'Das Video wird mit Dreamina Seedance 1.0 generiert. Dafür wird 1 Punkt verwendet.',
            '视频将使用 Seedance 1.0 生成，本次消耗1个额度。',
        ):
            with self.subTest(receipt=receipt):
                outcome = conversation_outcome({'assistant_messages': [[receipt]], 'videos': []}, 'ic_mini')
                self.assertEqual(outcome['state'], 'accepted')
                self.assertIsNone(outcome['error'])

    def test_seedance_one_receipt_prevents_switch_after_later_confirmation(self):
        poll = {'assistant_messages': [
            ['The video will be generated using Seedance 1.0. It will use 1 point.'],
            ['Please confirm whether I should generate the video in 5 or 10 seconds.']], 'videos': []}
        outcome = conversation_outcome(poll, 'seedance-1.0')
        self.assertEqual(outcome['state'], 'accepted')
        self.assertIsNone(outcome['error'])

    def test_seedance_one_balance_pending_and_negated_points_are_not_receipts(self):
        for receipt in (
            'Seedance 1.0. You still have 1 point left today.',
            'Seedance 1.0 will not use 1 point because generation has not started.',
            'Seedance 1.0 will use 1 point. Please confirm before I start generation.',
        ):
            with self.subTest(receipt=receipt):
                self.assertNotEqual(conversation_outcome({'assistant_texts': [receipt]}, 'ic_mini')['state'], 'accepted')

    def test_other_model_receipt_is_not_seedance_one_acceptance_but_prevents_retry(self):
        for receipt in ('Seedance 2.0. It will use 3 points.',
                        'Seedance 2.5. It will use 2 credits.'):
            poll = {'assistant_messages': [[receipt], ['Please confirm whether I should generate the video.']]}
            outcome = conversation_outcome(poll, 'ic_mini')
            self.assertEqual(outcome['state'], 'failed')
            self.assertEqual(outcome['error'].task_phase, 'request_mismatch')

    def test_seedance_one_receipt_cannot_be_accepted_as_fast_two(self):
        poll = {'assistant_messages': [
            ['Seedance 1.0. It will use 1 point.'], ['Please confirm whether I should generate the video.']]}
        outcome = conversation_outcome(poll, 'seedance_v2.0')
        self.assertEqual(outcome['state'], 'awaiting_receipt')
        self.assertIsNone(outcome['error'])

    def test_real_fast_english_points_receipt_is_accepted(self):
        outcome = conversation_outcome(
            {'assistant_messages': [[FAST_ENGLISH_POINTS_RECEIPT]], 'videos': []}, 'seedance_v2.0')
        self.assertEqual(outcome['state'], 'accepted')
        self.assertIsNone(outcome['error'])

    def test_real_fast_points_receipt_prevents_switch_on_later_confirmation(self):
        poll = {'assistant_messages': [[FAST_ENGLISH_POINTS_RECEIPT],
                ['Please confirm whether I should generate the video in 5 or 15 seconds.']], 'videos': []}
        outcome = conversation_outcome(poll, 'seedance_v2.0')
        self.assertEqual(outcome['state'], 'accepted')
        self.assertIsNone(outcome['error'])

    def test_fast_english_points_balance_negation_confirmation_and_echo_are_not_acceptance(self):
        for text in ('Seedance 2.0. You still have 3 points left today.',
                     'Seedance 2.0. It will not use 3 points; generation has not started.',
                     'Seedance 2.0. It will use 3 points. Would you like me to generate the video?',
                     'Seedance 2.0. It will use 3 points. Please confirm before I start generation.'):
            with self.subTest(text=text):
                outcome = conversation_outcome({'assistant_texts': [text], 'videos': []}, 'seedance_v2.0')
                self.assertNotEqual(outcome['state'], 'accepted')
        outcome = conversation_outcome({'assistant_texts': [FAST_ENGLISH_POINTS_RECEIPT]},
                                       'seedance_v2.0', FAST_ENGLISH_POINTS_RECEIPT)
        self.assertEqual(outcome['state'], 'awaiting_receipt')

    def test_english_withheld_video_is_terminal_after_earlier_acceptance(self):
        refusal = ("I can't show you the generated video because it may contain copyrighted or "
                   'policy-violating content. Try something else. No credits were used for this video.')
        receipt = 'The video will be generated using Dreamina Seedance 2.5. It will use 2 credits.'
        outcome = conversation_outcome({'assistant_messages': [[receipt], [refusal]], 'videos': []}, 'seedance_v2.5')
        self.assertEqual(outcome['state'], 'failed')
        self.assertEqual(outcome['error'].task_phase, 'platform_rejected_no_charge')
        self.assertEqual(conversation_outcome({'assistant_texts': [refusal]},
                         submitted_prompt=refusal)['state'], 'awaiting_receipt')
        self.assertEqual(conversation_outcome({'assistant_texts': [refusal],
                         'videos': [{'url': 'https://cdn.test/video.mp4'}]})['state'], 'completed')

    def test_observed_german_withheld_video_overrides_historical_fast_receipt(self):
        receipt = ('Dieses Video wird mithilfe des **Seedance\u00a02.0 Modells** in '
                   '15\u00a0Minuten generiert. Ich informiere dich, wenn es fertig ist.')
        # Native history is newest-first; outcome priority must not depend on list order.
        for messages in ([[GERMAN_WITHHELD_VIDEO_REFUSAL], [receipt]],
                         [[receipt], [GERMAN_WITHHELD_VIDEO_REFUSAL]]):
            with self.subTest(messages=messages):
                outcome = conversation_outcome({'assistant_messages': messages}, 'seedance_v2.0')
                self.assertEqual(outcome['state'], 'failed')
                self.assertEqual(outcome['error'].task_phase, 'platform_rejected_no_charge')

    def test_observed_german_withheld_video_is_terminal_without_a_receipt(self):
        outcome = conversation_outcome({'assistant_messages': [[GERMAN_WITHHELD_VIDEO_REFUSAL]]},
                                       'seedance_v2.0')
        self.assertEqual(outcome['state'], 'failed')
        self.assertEqual(outcome['error'].task_phase, 'platform_rejected_no_charge')

    def test_german_withheld_video_user_echo_and_quoted_examples_are_not_refusals(self):
        echo = conversation_outcome({'assistant_texts': [GERMAN_WITHHELD_VIDEO_REFUSAL]},
                                    'seedance_v2.0', GERMAN_WITHHELD_VIDEO_REFUSAL)
        self.assertEqual(echo['state'], 'awaiting_receipt')
        self.assertIsNone(echo['error'])
        for text in (
            'Generiertes Video: ' + GERMAN_WITHHELD_VIDEO_REFUSAL,
            'Generated video: ' + GERMAN_WITHHELD_VIDEO_REFUSAL,
            'Example:\n' + GERMAN_WITHHELD_VIDEO_REFUSAL,
            'Zitat:\n' + GERMAN_WITHHELD_VIDEO_REFUSAL,
            '> ' + GERMAN_WITHHELD_VIDEO_REFUSAL,
            '“' + GERMAN_WITHHELD_VIDEO_REFUSAL + '”',
            '```text\n' + GERMAN_WITHHELD_VIDEO_REFUSAL + '\n```',
        ):
            with self.subTest(text=text):
                outcome = conversation_outcome({'assistant_texts': [text]}, 'seedance_v2.0')
                self.assertEqual(outcome['state'], 'awaiting_receipt')
                self.assertIsNone(outcome['error'])

    def test_existing_video_takes_precedence_over_german_withheld_video(self):
        outcome = conversation_outcome({'assistant_texts': [GERMAN_WITHHELD_VIDEO_REFUSAL],
                                        'videos': [{'url': 'fixture-video'}]}, 'seedance_v2.0')
        self.assertEqual(outcome['state'], 'completed')
        self.assertIsNone(outcome['error'])

    def test_observed_input_policy_refusal_is_terminal_despite_historical_receipt(self):
        for messages in ([[ENGLISH_INPUT_POLICY_REFUSAL]],
                         [[ENGLISH_INPUT_POLICY_REFUSAL], [FAST_ENGLISH_POINTS_RECEIPT]],
                         [[FAST_ENGLISH_POINTS_RECEIPT], [ENGLISH_INPUT_POLICY_REFUSAL]]):
            with self.subTest(messages=messages):
                outcome = conversation_outcome({'assistant_messages': messages}, 'seedance_v2.0')
                self.assertEqual(outcome['state'], 'failed')
                self.assertEqual(outcome['error'].task_phase, 'platform_rejected_no_charge')

    def test_input_policy_refusal_user_echo_and_quoted_examples_are_not_refusals(self):
        echo = conversation_outcome({'assistant_texts': [ENGLISH_INPUT_POLICY_REFUSAL]},
                                    'seedance_v2.0', ENGLISH_INPUT_POLICY_REFUSAL)
        self.assertEqual(echo['state'], 'awaiting_receipt')
        self.assertIsNone(echo['error'])
        for text in (
            'Generiertes Video: ' + ENGLISH_INPUT_POLICY_REFUSAL,
            'Generated video: ' + ENGLISH_INPUT_POLICY_REFUSAL,
            'Example:\n' + ENGLISH_INPUT_POLICY_REFUSAL,
            'Zitat:\n' + ENGLISH_INPUT_POLICY_REFUSAL,
            '> ' + ENGLISH_INPUT_POLICY_REFUSAL,
            '“' + ENGLISH_INPUT_POLICY_REFUSAL + '”',
            '```text\n' + ENGLISH_INPUT_POLICY_REFUSAL + '\n```',
        ):
            with self.subTest(text=text):
                outcome = conversation_outcome({'assistant_texts': [text]}, 'seedance_v2.0')
                self.assertEqual(outcome['state'], 'awaiting_receipt')
                self.assertIsNone(outcome['error'])

    def test_existing_video_takes_precedence_over_input_policy_refusal(self):
        outcome = conversation_outcome({'assistant_texts': [ENGLISH_INPUT_POLICY_REFUSAL],
                                        'videos': [{'url': 'fixture-video'}]}, 'seedance_v2.0')
        self.assertEqual(outcome['state'], 'completed')
        self.assertIsNone(outcome['error'])

    def test_english_generation_failure_overrides_earlier_two_five_receipt(self):
        receipt = 'The video will be generated using Dreamina Seedance 2.5. It will use 2 credits.'
        for refusal in ("I can't generate the video and didn't use any credit for this generation.",
                        'I cannot generate the video and did not use any credits for this generation.',
                        'I can’t generate the video and didn’t use any credit for this generation.'):
            with self.subTest(refusal=refusal):
                outcome = conversation_outcome({'assistant_messages': [[receipt], [refusal]], 'videos': []},
                                               'seedance_v2.5')
                self.assertEqual(outcome['state'], 'failed')
                self.assertEqual(outcome['error'].task_phase, 'platform_rejected_no_charge')
                echo = conversation_outcome({'assistant_texts': [refusal], 'videos': []},
                                            'seedance_v2.5', refusal)
                self.assertEqual(echo['state'], 'awaiting_receipt')
                available = conversation_outcome({'assistant_texts': [refusal], 'videos': [{'url':'https://cdn.test/video.mp4'}]},
                                                 'seedance_v2.5')
                self.assertEqual(available['state'], 'completed')

    def test_real_person_image_refusal_is_terminal_not_a_receipt_wait(self):
        reply = ('Zum Schutz des Erscheinungsbilds unterstützt Dreamina Seedance\u00a02.0 '
                 'das Generieren von Videos aus Bildern von realen Personen nicht. '
                 'Verwende ein anderes Bild oder erstelle ein Video aus Text.')
        outcome = conversation_outcome({'assistant_texts': [reply]}, 'seedance_v2.0')
        self.assertEqual(outcome['state'], 'failed')
        self.assertEqual(outcome['error'].task_phase, 'platform_rejected')
        self.assertEqual(conversation_outcome({'assistant_texts': [reply]},
                         submitted_prompt=reply)['state'], 'awaiting_receipt')

    def test_observed_english_real_person_refusal_uses_the_existing_terminal_label(self):
        outcome = conversation_outcome({'assistant_messages': [[REAL_PERSON_ENGLISH_REFUSAL]]}, 'seedance_v2.0')
        self.assertEqual(outcome['state'], 'failed')
        self.assertEqual(outcome['error'].task_phase, 'platform_rejected')
        self.assertIn('该模型不支持真人参考图，请更换图片或使用纯文字生成', str(outcome['error']))

    def test_english_real_person_refusal_echo_and_examples_are_not_terminal(self):
        echo = conversation_outcome({'assistant_texts': [REAL_PERSON_ENGLISH_REFUSAL]},
                                    'seedance_v2.0', REAL_PERSON_ENGLISH_REFUSAL)
        self.assertEqual(echo['state'], 'awaiting_receipt')
        for text in (
            'Generiertes Video: ' + REAL_PERSON_ENGLISH_REFUSAL,
            'Generated video: ' + REAL_PERSON_ENGLISH_REFUSAL,
            'Example:\n' + REAL_PERSON_ENGLISH_REFUSAL,
            '以下只是拒绝信息示例：\n' + REAL_PERSON_ENGLISH_REFUSAL,
            '> ' + REAL_PERSON_ENGLISH_REFUSAL,
            '“' + REAL_PERSON_ENGLISH_REFUSAL + '”',
            '```text\n' + REAL_PERSON_ENGLISH_REFUSAL + '\n```',
        ):
            with self.subTest(text=text):
                outcome = conversation_outcome({'assistant_texts': [text]}, 'seedance_v2.0')
                self.assertEqual(outcome['state'], 'awaiting_receipt')
                self.assertIsNone(outcome['error'])

    def test_existing_video_takes_precedence_over_english_real_person_refusal(self):
        outcome = conversation_outcome({'assistant_texts': [REAL_PERSON_ENGLISH_REFUSAL],
                                        'videos': [{'url':'fixture-video'}]}, 'seedance_v2.0')
        self.assertEqual(outcome['state'], 'completed')
        self.assertIsNone(outcome['error'])

    def test_confirmation_in_another_block_of_same_message_is_not_acceptance(self):
        from video_polling import parse_poll_response
        blocks = lambda texts: [{'content': {'text_block': {'text': text}}} for text in texts]
        messages = [{'role': 'assistant', 'content': blocks([
            'Seedance 2.5 需要2个额度。', '请确认是否开始生成两个15秒视频。'])}]
        payload = {'downlink_body': {'pull_singe_chain_downlink_body': {'messages': messages}}}
        poll = parse_poll_response(payload)
        self.assertEqual(conversation_outcome(poll, 'seedance_v2.5')['state'], 'not_accepted')
        messages.append({'role': 'assistant', 'content': blocks([
            'Das Video wird mit Dreamina Seedance 2.5 generiert. Dafür werden 2 Credits verwendet.'])})
        self.assertEqual(conversation_outcome(parse_poll_response(payload), 'seedance_v2.5')['state'], 'accepted')

    def test_unmatched_user_composer_echo_is_never_a_receipt_or_platform_refusal(self):
        for text in ('Generiertes Video: Seedance 2.5 消耗2个额度, 16:9',
                     'Generiertes Video: Zum Schutz des Urheberrechts kann ich dir das generierte Video nicht anzeigen.'):
            outcome = conversation_outcome({'assistant_texts': [text], 'videos': []}, 'seedance_v2.5', '另一份原文')
            self.assertEqual(outcome['state'], 'awaiting_receipt')
            self.assertEqual(outcome['reply'], '')

    def test_unknown_format_does_not_turn_into_generic_platform_failure(self):
        outcome = conversation_outcome({'assistant_texts': ['提到版权并不等于拒绝生成。'], 'videos': []}, 'seedance_v2.5')
        self.assertEqual(outcome['state'], 'awaiting_receipt')
        self.assertIsNone(outcome['error'])

    def test_explicit_duration_confirmation_is_safe_to_try_another_account(self):
        text = ('请确认：用 **5 秒** 生成，还是指定 **4–15 秒** 内的其他时长？'
                '确认后我直接生成，不改你的提示词。 https://cdn.test/a?token=private')
        outcome = conversation_outcome({'assistant_texts': [text], 'videos': []}, 'seedance_v2.5')
        self.assertEqual(outcome['state'], 'not_accepted')
        self.assertEqual(type(outcome['error']).__name__, 'ExplicitNotAcceptedError')
        self.assertEqual(outcome['error'].task_phase, 'not_accepted')
        self.assertNotIn('private', str(outcome['error']))

    def test_direct_generation_confirmation_is_recognized_in_english_and_german(self):
        for text in ('Please choose 5 seconds or a duration between 4 and 15 seconds. '
                     'Once you confirm, I will generate the video.',
                     'Would you like me to generate the video in 5 seconds?',
                     'Bitte wählen Sie 5 Sekunden oder 4–15 Sekunden. '
                     'Nach Ihrer Bestätigung generiere ich das Video.',
                     'Bitte bestätige, ob ich das Video mit Seedance 2.5 generieren soll. '
                     'Dafür werden 2 Credits verwendet.',
                     'Soll ich das Video mit 5 Sekunden generieren?'):
            with self.subTest(text=text):
                outcome = conversation_outcome({'assistant_texts': [text], 'videos': []}, 'seedance_v2.5')
                self.assertEqual(outcome['state'], 'not_accepted')

    def test_duration_statement_and_unknown_no_charge_are_not_confirmation(self):
        for text in ('视频生成目前支持4到15秒的时长。我可以按最近的支持时长15秒生成。',
                     'I can generate a 15-second video. Video generation supports 4 to 15 seconds.',
                     '尚未扣除额度。', 'No credits have been consumed.',
                     'Derzeit werden keine Credits abgezogen.',
                     '请确认你的账号显示名称。', 'Please choose your preferred language.'):
            with self.subTest(text=text):
                outcome = conversation_outcome({'assistant_texts': [text], 'videos': []}, 'seedance_v2.5')
                self.assertEqual(outcome['state'], 'awaiting_receipt')
                self.assertIsNone(outcome['error'])

    def test_confirming_receipt_or_viewing_of_a_video_does_not_trigger_switch(self):
        for text in ('请确认你能看见视频。', '请确认你已经收到视频。',
                     'Please confirm you received the video.',
                     'Please choose which video you want to view.',
                     'Bitte bestätige, dass du das Video siehst.',
                     'Bitte wählen Sie das Video, das Sie ansehen möchten.'):
            with self.subTest(text=text):
                outcome = conversation_outcome({'assistant_texts': [text], 'videos': []}, 'seedance_v2.5')
                self.assertEqual(outcome['state'], 'awaiting_receipt')
                self.assertIsNone(outcome['error'])

    def test_historical_generation_receipt_prevents_switch_on_later_confirmation(self):
        poll = {'assistant_messages': [
            ['Das Video wird mit Dreamina Seedance 2.5 generiert. Dafür werden 2 Credits verwendet.'],
            ['请确认：用5秒生成，还是指定4–15秒？确认后我直接生成。']], 'videos': []}
        outcome = conversation_outcome(poll, 'seedance_v2.5')
        self.assertEqual(outcome['state'], 'accepted')
        self.assertIsNone(outcome['error'])

    def test_other_model_receipt_blocks_switch_even_when_requested_model_is_not_accepted(self):
        poll = {'assistant_messages': [
            ['Das Video wird mit Dreamina Seedance 2.0 generiert. Dafür werden 1 Credits verwendet.'],
            ['请确认：用5秒生成，还是指定4–15秒？确认后我直接生成。']], 'videos': []}
        outcome = conversation_outcome(poll, 'seedance_v2.5')
        self.assertEqual(outcome['state'], 'awaiting_receipt')
        self.assertIsNone(outcome['error'])

    def test_affirmative_generation_with_unexpected_credit_count_blocks_switch(self):
        for receipt in ('Das Video wird mit Dreamina Seedance 2.5 generiert. Dafür wird 1 Credit verwendet.',
                        'The video is being generated with Seedance 2.5 using 1 credit.',
                        'Seedance 2.5 正在生成视频，本次消耗1个额度。'):
            with self.subTest(receipt=receipt):
                poll = {'assistant_messages': [[receipt], ['请确认是否生成视频。']], 'videos': []}
                outcome = conversation_outcome(poll, 'seedance_v2.5')
                self.assertEqual(outcome['state'], 'awaiting_receipt')
                self.assertIsNone(outcome['error'])

    def test_late_copyright_refusal_does_not_trigger_account_switch(self):
        poll = {'assistant_messages': [
            ['Das Video wird mit Dreamina Seedance 2.5 generiert. Dafür werden 2 Credits verwendet.'],
            ['请确认：用5秒生成？'],
            ['Zum Schutz des Urheberrechts kann ich dir das generierte Video aufgrund seines Audios nicht anzeigen.']],
            'videos': []}
        outcome = conversation_outcome(poll, 'seedance_v2.5')
        self.assertEqual(outcome['state'], 'failed')
        self.assertEqual(outcome['error'].task_phase, 'platform_audio_copyright')

    def test_existing_video_prevents_switch_even_with_confirmation_text(self):
        poll = {'assistant_texts': ['请确认是否开始生成视频。'], 'videos': [{'url': 'https://cdn.test/video.mp4'}]}
        outcome = conversation_outcome(poll, 'seedance_v2.5')
        self.assertEqual(outcome['state'], 'completed')
        self.assertIsNone(outcome['error'])

    def test_native_prompt_echo_cannot_trigger_account_switch(self):
        prompt = '请选择5秒生成视频，确认后直接生成。'
        for text in (prompt, 'Generiertes Video: ' + prompt):
            outcome = conversation_outcome({'assistant_texts': [text], 'videos': []}, 'seedance_v2.5', prompt)
            self.assertEqual(outcome['state'], 'awaiting_receipt')
            self.assertEqual(outcome['reply'], '')

    def test_platform_failure_evidence_is_persistable_and_redacted(self):
        text = 'Zum Schutz des Urheberrechts kann ich dir das generierte Video nicht anzeigen. https://cdn.test/a?token=private'
        result = conversation_outcome({'assistant_texts': [text], 'videos': []}, 'seedance_v2.5')
        self.assertEqual(result['error'].task_phase, 'platform_copyright')
        self.assertNotIn('private', str(result['error']))
        self.assertNotIn('private', str(public_details({'reply': result['reply']})))
        self.assertEqual(VideoQueryTimeout.task_phase, 'query_timeout')
