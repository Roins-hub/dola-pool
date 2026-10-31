import unittest
from providers.doubao.runtime import receipt_state

PARAMETER_CONFIRMATION = ('我将为你生成一条可爱原创虚拟橘猫跳舞的单镜头视频，请先确认参数：\n'
    '视频生成参数确认\n模型： Seedance 2.0 Fast\n时长： 15 秒\n比例： 16:9 横屏\n'
    '创作方向： 原创虚拟橘色小猫在明亮客厅开心跳舞，摆动身体、挥动前爪，最后转一圈站稳；'
    '全身入镜，固定镜头，动作连贯自然，无字幕\n确认后我再开始生成视频。')


class ReceiptScoreTests(unittest.TestCase):
    cap={'receipt_patterns':['视频生成中'],'rejection_patterns':['无法生成','额度不足']}
    def test_model_eta_and_delivery_promise_identify_acknowledgement(self):
        text='已使用 Seedance 2.5 为你制作视频，预计需要5分钟，完成后我会通知你。'
        self.assertEqual(receipt_state(text,self.cap),'accepted')

    def test_a_question_or_hypothetical_is_not_acceptance(self):
        for text in ('请确认是否使用 Seedance 2.5 制作视频，预计5分钟，完成后通知你。',
                     '如果使用 Seedance 2.5 生成视频，预计5分钟，完成后通知你。'):
            self.assertEqual(receipt_state(text,self.cap),'unknown')

    def test_later_explicit_failure_wins_over_an_earlier_eta(self):
        self.assertEqual(receipt_state('视频生成中，预计5分钟完成。现在额度不足，无法生成。',self.cap),'rejected')

    def test_real_parameter_confirmation_is_not_acceptance_despite_begin_generation_words(self):
        cap={**self.cap,'receipt_patterns':['开始生成','视频生成中','排队中']}
        self.assertEqual(receipt_state(PARAMETER_CONFIRMATION,cap),'unknown')
        self.assertEqual(receipt_state('请确认参数后开始生成视频。',cap),'unknown')

    def test_later_real_progress_or_submission_can_supersede_confirmation_text(self):
        cap={**self.cap,'receipt_patterns':['开始生成','视频生成中']}
        for progress in ('已开始生成视频，预计5分钟完成。',
                         '确认已收到，视频任务已提交，预计5分钟完成，生成好后通知你。'):
            self.assertEqual(receipt_state(PARAMETER_CONFIRMATION+'\n'+progress,cap),'accepted')


if __name__=='__main__':unittest.main()
