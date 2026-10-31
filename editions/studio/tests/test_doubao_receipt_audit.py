import unittest
from providers.doubao.confirmation import ordinary_dialog_spec_matches
from providers.doubao.runtime import receipt_state
from test_doubao_ordinary_dialog_runtime import CAP, REQUEST


class AuditedReceiptTests(unittest.TestCase):
    def test_chat_timestamp_does_not_become_an_extra_video_ratio(self):
        text='本次使用 Seedance 2.0 Fast 生成15秒、16:9视频。\n今天 14:33'
        self.assertTrue(ordinary_dialog_spec_matches(text, REQUEST))
        self.assertFalse(ordinary_dialog_spec_matches(text.replace('16:9','9:16'), REQUEST))

    def test_standalone_hour_minute_does_not_become_a_video_ratio(self):
        text='本次使用 Seedance 2.0 Fast 生成15秒、16:9视频。\n09:16'
        self.assertTrue(ordinary_dialog_spec_matches(text, REQUEST))

    def test_standalone_valid_ratio_is_not_discarded_as_a_clock(self):
        text='本次使用 Seedance 2.0 Fast 生成15秒视频。\n9:16'
        self.assertFalse(ordinary_dialog_spec_matches(text, REQUEST, require_all=False))

    def test_code_generation_acknowledgement_is_not_a_video_receipt(self):
        text='已生成程式碼\n✅ 已受理，正在使用 Seedance 2.0 Fast 按你原始提示词生成 15 秒、16:9 视频。'
        self.assertEqual(receipt_state(text, CAP), 'unknown')
        self.assertEqual(receipt_state(text.replace('已生成程式碼','已生成代码'), CAP), 'unknown')
        self.assertEqual(receipt_state('本次使用 Seedance 2.0 Fast 生成，预计10分钟，完成后通知你。', CAP), 'accepted')


if __name__ == '__main__': unittest.main()
