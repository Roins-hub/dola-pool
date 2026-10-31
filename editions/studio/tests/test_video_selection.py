"""Replay the production selection-to-send boundary without spending video credits."""
import ast
import copy
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from test_prompt_input import Page as InputPage, Editor
from test_video_prompt import PREFIX, REFERENCE_DECLARATION
from test_video_pipeline import pool_module
import video_worker_ui as worker
from navigation_errors import BrowserSetupError


def actual_pre_send_flow():
    tree = ast.parse(Path(worker.__file__).read_text(encoding='utf-8'))
    function = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == 'generate_video')
    first_visible = next(node for node in ast.walk(function)
                         if isinstance(node, ast.AsyncFunctionDef) and node.name == '_first_visible')
    for block in ast.walk(function):
        body = getattr(block, 'body', None)
        if not isinstance(body, list):
            continue
        for start, node in enumerate(body):
            if not (isinstance(node, ast.If) and isinstance(node.test, ast.Name)
                    and node.test.id == 'duration'):
                continue
            end = next((index for index in range(start, len(body))
                        if isinstance(body[index], ast.Expr) and isinstance(body[index].value, ast.Await)
                        and isinstance(body[index].value.value, ast.Call)
                        and isinstance(body[index].value.value.func, ast.Name)
                        and body[index].value.value.func.id == '_submit_prompt_once'), None)
            if end is None:
                continue
            wrapper = ast.parse('async def run(page, account, prompt, duration, model, ratio=None, on_prompt_sent=None, reference_image_paths=None): pass').body[0]
            wrapper.body = [copy.deepcopy(first_visible)] + ast.parse('model_key = model\non_event = None').body + body[start:end + 1]
            namespace = dict(vars(worker), DUR_CHIP_RE=re.compile(r'^自动.*秒'), prepare_only=False)
            exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])),
                         'actual-selection-to-send', 'exec'), namespace)
            return namespace['run']
    raise AssertionError('Cannot locate the production selection-to-send boundary')


class Chip:
    def __init__(self, text):
        self.text = text
    async def is_visible(self): return True
    async def inner_text(self): return self.text
    async def click(self, **kwargs): pass


class Chips:
    def __init__(self, chips): self.chips = chips
    async def count(self): return len(self.chips)
    def nth(self, index): return self.chips[index]
    @property
    def last(self): return self.chips[-1]


class SelectionPage(InputPage):
    url = 'https://www.dola.com/chat'
    def __init__(self, model='模型 Seedance 2.5', duration=10, ratio='9:16', states=None):
        super().__init__(Editor('textarea'))
        self.state = {'modelText': model, 'durationText': f'自动 · {duration}秒', 'ratioText': ratio}
        self.states = list(states or [])
        self.duration = duration
        self.state_reads = 0
        self.native = {'ready': True, 'model': 'seedance_v2.0', 'duration': duration, 'pipelineReady': True}
        self.native_steps = []
        self.native_states = []
    def get_by_text(self, pattern, exact=False):
        text = self.state['durationText']
        matches = pattern.search(text) if hasattr(pattern, 'search') else text == pattern
        return Chips([Chip(text)] if matches else [])
    async def evaluate(self, script, value=None):
        if '__DOLA_FAST_NATIVE__' in script or '__DOLA_FAST15_NATIVE__' in script:
            self.native_steps.append('prepare' if '.prepare()' in script else 'check')
            if self.native_states:
                self.native = self.native_states.pop(0)
            if isinstance(self.native, Exception):
                raise self.native
            return self.native
        if isinstance(value, int) and 'sliders' in script:
            return {'applied': self.duration, 'min': 4, 'max': self.duration, 'step': 1}
        if 'data-dola-duration' in script:
            return None
        self.state_reads += 1
        if self.states:
            self.state = self.states.pop(0)
        return self.state
    def on(self, event, callback): pass


class VideoSelectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_fast30_lazy_input_ready_after_three_seconds_can_send_once(self):
        page = SelectionPage(model='Model 2.0 Fast', duration=30, ratio='')
        page.native_states = [
            {'ready': False, 'model': '', 'duration': None, 'pipelineReady': False,
             'reasonCode': 'input_store_unavailable'},
        ] * 11 + [page.native]
        try:
            await self.run_flow(page, model='seedance_v2.0', duration=30, ratio=None)
        except BrowserSetupError as error:
            self.fail(f'Lazy native input was rejected before it became ready: {error}')
        self.assertEqual(len(page.sent), 1)
        self.assertTrue(page.sent[0].endswith('保持原文\n第二行'))

    async def test_native_failure_explains_safe_reason_without_leaking_bridge_data(self):
        page = SelectionPage(model='Model 2.0 Fast', duration=30, ratio='')
        page.native = {'ready': False, 'pipelineReady': True,
            'model': 'https://private.example/secret?token=unsafe-model', 'duration': 'unsafe-duration',
            'reasonCode': 'input_store_unavailable',
            'reason': 'http://proxy-user:proxy-password@private.example/?token=unsafe-reason'}
        with self.assertRaises(BrowserSetupError) as caught:
            await self.run_flow(page, model='seedance_v2.0', duration=30, ratio=None)
        reason = str(caught.exception)
        self.assertIn('原生输入状态未就绪', reason)
        self.assertIn('模型=未确认', reason)
        self.assertIn('时长=未确认', reason)
        self.assertIn('提示词未发送', reason)
        for secret in ('unsafe-', 'private.example', 'proxy-user', 'proxy-password'):
            self.assertNotIn(secret, reason)
        self.assertEqual(page.sent, [])

    async def test_native_evaluate_error_has_safe_diagnosis_and_never_sends(self):
        page = SelectionPage(model='Model 2.0 Fast', duration=30, ratio='')
        page.native = RuntimeError('https://proxy-user:proxy-password@private.example/?token=secret')
        with self.assertRaises(BrowserSetupError) as caught:
            await self.run_flow(page, model='seedance_v2.0', duration=30, ratio=None)
        self.assertIn('原生核验状态不可读', str(caught.exception))
        self.assertNotIn('private.example', str(caught.exception))
        self.assertNotIn('proxy-password', str(caught.exception))
        self.assertEqual(page.sent, [])

    async def test_native_selection_change_after_material_upload_is_read_only_and_never_sends(self):
        page = SelectionPage(model='Model 2.0 Fast', duration=30, ratio='')
        async def upload(target, paths):
            target.native = {'ready': False, 'pipelineReady': True, 'model': 'seedance_v2.5',
                             'duration': 30, 'reasonCode': 'selection_changed'}
        with patch.object(worker, 'attach_reference_images', upload):
            with self.assertRaises(BrowserSetupError) as caught:
                await self.run_flow(page, model='seedance_v2.0', duration=30, ratio=None,
                                    reference_image_paths=['reference.png'])
        self.assertIn('原生选项已变化', str(caught.exception))
        self.assertEqual(page.native_steps.count('prepare'), 1)
        self.assertEqual(page.sent, [])

    async def test_fast30_stays_fast_and_sends_once_after_native_check(self):
        page = SelectionPage(model='Model 2.0 Fast', duration=30, ratio='')
        await self.run_flow(page, model='seedance_v2.0', duration=30, ratio=None)
        self.assertEqual(page.native_steps, ['prepare', 'check'])
        from test_video_prompt import REFERENCE_DECLARATION
        self.assertEqual(page.sent, [PREFIX + '\n\n' + REFERENCE_DECLARATION + '\n\n用户提示词：\n\n保持原文\n第二行'])
        self.assertNotIn('使用1个额度', page.sent[0])

    async def test_fast30_selection_clamped_to_15_or_25_never_sends(self):
        for label, seconds in [('Model 2.0 Fast', 15), ('Model 2.5', 30)]:
            page = SelectionPage(model=label, duration=seconds, ratio='')
            with self.assertRaises(BrowserSetupError):
                await self.run_flow(page, model='seedance_v2.0', duration=30, ratio=None)
            self.assertEqual(page.sent, [])
    async def test_fast15_prepares_before_material_upload_and_only_reads_after_it(self):
        page = SelectionPage(model='Model 2.0 Fast', duration=15, ratio='')
        async def upload(target, paths):
            self.assertEqual(target.native_steps, ['prepare'])
            target.native_steps.append('upload')
        with patch.object(worker, 'attach_reference_images', upload):
            await self.run_flow(page, model='seedance_v2.0', duration=15, ratio=None, reference_image_paths=['reference.png'])
        self.assertEqual(page.native_steps, ['prepare', 'upload', 'check'])
        self.assertEqual(len(page.sent), 1)

    async def test_fast15_unconfirmed_native_state_never_sends_even_if_chips_match(self):
        for state in (None, {'ready': False}, {'ready': True, 'model': 'seedance_v2.0_std', 'duration': 15, 'pipelineReady': True},
                      {'ready': True, 'model': 'seedance_v2.0', 'duration': 10, 'pipelineReady': True}):
            with self.subTest(state=state):
                page = SelectionPage(model='Model 2.0 Fast', duration=15, ratio='')
                page.native = state
                with self.assertRaises(BrowserSetupError):
                    await self.run_flow(page, model='seedance_v2.0', duration=15, ratio=None)
                self.assertEqual(page.sent, [])

    async def test_standard_20_label_cannot_pass_as_fast(self):
        for label in ('Model Seedance 2.0', 'Seedance 2.0 Standard', 'Seedance 2.0 std'):
            page = SelectionPage(model=label, duration=15, ratio='')
            with self.assertRaises(BrowserSetupError):
                await self.run_flow(page, model='seedance_v2.0', duration=15, ratio=None)
            self.assertEqual(page.sent, [])
    async def run_flow(self, page, model='seedance_v2.5', duration=30, ratio='9:16', reference_image_paths=None):
        async def no_modal(*args): return False
        with patch.object(worker, '_handle_birthday_modal', no_modal), patch.object(worker, '_dismiss_login_modal', no_modal):
            await actual_pre_send_flow()(page, 'fixture', '保持原文\n第二行', duration, model, ratio,
                                         reference_image_paths=reference_image_paths)

    async def test_clamped_30_second_selection_never_sends_the_prompt(self):
        for seconds in (10, 15):
            with self.subTest(actual_seconds=seconds):
                page = SelectionPage(duration=seconds)
                with self.assertRaises(BrowserSetupError):
                    await self.run_flow(page)
                self.assertEqual(page.sent, [])

    async def test_wrong_model_never_sends_even_when_the_duration_is_correct(self):
        page = SelectionPage(model='Model Seedance 2.0 Fast', duration=30)
        with self.assertRaises(BrowserSetupError):
            await self.run_flow(page)
        self.assertEqual(page.sent, [])

    async def test_wrong_explicit_ratio_never_sends(self):
        page = SelectionPage(duration=30, ratio='16:9')
        with self.assertRaises(BrowserSetupError):
            await self.run_flow(page)
        self.assertEqual(page.sent, [])

    async def test_model_menu_options_are_not_a_selected_model(self):
        page = SelectionPage(model='Seedance 2.0 Fast Seedance 2.5', duration=30)
        with self.assertRaises(BrowserSetupError):
            await self.run_flow(page)
        self.assertEqual(page.sent, [])

    async def test_unreadable_controls_never_send(self):
        page = SelectionPage(duration=30)
        page.state = {}
        with self.assertRaises(BrowserSetupError):
            await self.run_flow(page, duration=None, ratio=None)
        self.assertEqual(page.sent, [])

    async def test_correct_25_30_controls_send_once_and_preserve_user_text(self):
        page = SelectionPage(duration=30)
        await self.run_flow(page)
        self.assertEqual(len(page.sent), 1)
        self.assertTrue(page.sent[0].endswith('保持原文\n第二行'))

    async def test_two_five_ten_trial_requires_the_actual_model_and_duration(self):
        page=SelectionPage(duration=10)
        await self.run_flow(page,duration=10)
        self.assertEqual(page.sent,['生成参数：视频时长10秒\n\n用户提示词：\n\n保持原文\n第二行'])
        for label,seconds in [('Model 2.0 Fast',10),('Model 2.5',15)]:
            page=SelectionPage(model=label,duration=seconds)
            with self.assertRaises(BrowserSetupError):await self.run_flow(page,duration=10)
            self.assertEqual(page.sent,[])

    async def test_correct_25_15_controls_send_once_and_preserve_user_text(self):
        page = SelectionPage(duration=15)
        await self.run_flow(page, duration=15)
        self.assertEqual(page.sent, ['生成参数：视频时长15秒\n\n用户提示词：\n\n保持原文\n第二行'])

    async def test_15_second_request_clamped_to_10_never_sends_the_prompt(self):
        page = SelectionPage(duration=10)
        with self.assertRaises(BrowserSetupError):
            await self.run_flow(page, duration=15)
        self.assertEqual(page.sent, [])

    async def test_fast_ten_prepends_requested_paragraphs_without_changing_duration(self):
        page = SelectionPage(model='Modell Dreamina Seedance 2.0 Schnell', duration=10, ratio='')
        await self.run_flow(page, model='seedance_v2.0', duration=10, ratio=None)
        self.assertEqual(page.sent, [PREFIX + '\n\n' + REFERENCE_DECLARATION + '\n\n生成参数：视频时长10秒\n\n用户提示词：\n\n保持原文\n第二行'])

    async def test_fast_request_never_sends_a_mini_model(self):
        page = SelectionPage(model='Seedance2.0Mini', duration=10, ratio='')
        with self.assertRaises(BrowserSetupError):
            await self.run_flow(page, model='seedance_v2.0', duration=10, ratio=None)
        self.assertEqual(page.sent, [])

    async def test_fast_fifteen_sends_once_only_when_both_controls_match(self):
        page = SelectionPage(model='Model 2.0 Fast', duration=15, ratio='')
        await self.run_flow(page, model='seedance_v2.0', duration=15, ratio=None)
        self.assertEqual(page.sent, [PREFIX + '\n\n' + REFERENCE_DECLARATION + '\n\n生成参数：视频时长15秒\n\n用户提示词：\n\n保持原文\n第二行'])
        for label, seconds in [('Model 2.0 Fast',10),('Model 2.5',15)]:
            page=SelectionPage(model=label,duration=seconds,ratio='')
            with self.assertRaises(BrowserSetupError):
                await self.run_flow(page,model='seedance_v2.0',duration=15,ratio=None)
            self.assertEqual(page.sent,[])

    async def test_seedance_one_sends_once_with_matching_five_or_ten_second_controls(self):
        for seconds in (5, 10, 15):
            for label in ('Model 1.0', 'Dreamina Seedance 1.0 Fast'):
                with self.subTest(seconds=seconds, label=label):
                    page = SelectionPage(model=label, duration=seconds, ratio='')
                    try:
                        await self.run_flow(page, model='ic_mini', duration=seconds, ratio=None)
                    except BrowserSetupError as error:
                        self.fail(f'Valid Seedance 1.0 selection was rejected: {error}')
                    self.assertEqual(page.sent, [f'生成参数：视频时长{seconds}秒\n\n用户提示词：\n\n保持原文\n第二行'])

    async def test_seedance_one_never_sends_when_another_model_is_selected(self):
        for label in ('Model 2.0 Fast', 'Model 2.5', 'Seedance 2.0 Mini', 'Seedance 1.0 Seedance 2.0 Fast'):
            with self.subTest(label=label):
                page = SelectionPage(model=label, duration=10, ratio='')
                with self.assertRaises(BrowserSetupError):
                    await self.run_flow(page, model='ic_mini', duration=10, ratio=None)
                self.assertEqual(page.sent, [])


if __name__ == '__main__':
    unittest.main()
