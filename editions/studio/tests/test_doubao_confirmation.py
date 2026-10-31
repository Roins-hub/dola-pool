import unittest
from providers.doubao.confirmation import confirmation_due
from test_doubao_receipts import PARAMETER_CONFIRMATION


class ConfirmationTests(unittest.TestCase):
    def state(self):return {'created_at':100,'request':{'auto_confirm':True}}

    def test_confirmation_is_opt_in_stable_and_delayed(self):
        state=self.state();question='请确认是否按这个方案生成视频？'
        self.assertFalse(confirmation_due(state,question,now=108))
        self.assertFalse(confirmation_due(state,question,now=110))
        self.assertTrue(confirmation_due(state,question,now=111))
        state['confirmation_sent']=True
        self.assertFalse(confirmation_due(state,question,now=200))

    def test_default_policy_refusal_and_generated_text_never_trigger_confirmation(self):
        for text in ('视频生成中，请稍候。','无法生成视频，请修改后确认。','请升级专业版后确认生成视频。','如果生成失败会通知你。'):
            self.assertFalse(confirmation_due(self.state(),text,now=200))
        self.assertFalse(confirmation_due({'created_at':0,'request':{}},'请确认生成视频',now=200))

    def test_changed_question_restarts_stability_window(self):
        state=self.state()
        confirmation_due(state,'请确认生成视频A',now=111)
        self.assertFalse(confirmation_due(state,'请确认生成视频B',now=115))
        self.assertTrue(confirmation_due(state,'请确认生成视频B',now=118))

    def test_real_parameter_confirmation_is_detected_once_after_existing_delay(self):
        state=self.state()
        self.assertFalse(confirmation_due(state,PARAMETER_CONFIRMATION,now=111))
        self.assertTrue(confirmation_due(state,PARAMETER_CONFIRMATION,now=114))
        state['confirmation_sent']=True
        self.assertFalse(confirmation_due(state,PARAMETER_CONFIRMATION,now=120))

    def test_later_actual_generation_prevents_confirming_an_old_question(self):
        state=self.state()
        text=PARAMETER_CONFIRMATION+'\n已开始生成视频，预计5分钟完成。'
        confirmation_due(state,text,now=111)
        self.assertFalse(confirmation_due(state,text,now=114))


if __name__=='__main__':unittest.main()
