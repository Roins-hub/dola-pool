"""Offline replay of Dola's 2026-10-22 composer; never loads an account/site."""
import ast
from pathlib import Path
import re
import unittest

from patchright.async_api import async_playwright


SOURCE = Path(__file__).resolve().parents[1]
SELECTION_JS = (SOURCE / 'web/dola-video-selection.js').read_text(encoding='utf-8')


def duration_chip_pattern():
    tree = ast.parse((SOURCE / 'video_worker_ui.py').read_text(encoding='utf-8'))
    generate = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef)
                    and node.name == 'generate_video')
    assignment = next(node for node in ast.walk(generate) if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == 'DUR_CHIP_RE'
                              for target in node.targets))
    return eval(compile(ast.Expression(assignment.value), '<production-duration-chip>', 'eval'), {'re': re})


def composer(*, depth=10, label='16:9 · 30s', model='Model Dreamina Seedance 2.0 Fast',
             keyed=False, identifier='current', editor=True):
    # The observed ProseMirror input now has more than seven wrappers before the
    # shared toolbar ancestor. The combined params button has no control key.
    inner = '<div contenteditable="true" style="height:48px">提示词</div>' if editor else '<p>旧对话</p>'
    inner = '<div>' * depth + inner + '</div>' * depth
    duration_key = 'data-input-engine-actionbar-control-key="video-duration"' if keyed else ''
    return f'''<section id="{identifier}" class="composer">{inner}<div class="toolbar">
      <button data-input-engine-actionbar-control-key="video-model">{model}</button>
      <button {duration_key}>{label}</button></div></section>'''


class DurationChipPatternTests(unittest.TestCase):
    def test_combined_ratio_duration_chips_are_actionable(self):
        pattern = duration_chip_pattern()
        for label in ('16:9 · 30s', '9:16 · 15 seconds', '16:9 · 30 Sekunden',
                      '自动 · 10秒', 'Automatisch · 10 Sek.', '10s', '15 秒'):
            with self.subTest(label=label):
                self.assertRegex(label, pattern)
        for label in ('vor 10 Sekunden', '一段30秒的视频', '16:9', '16:9 video 30s'):
            with self.subTest(label=label):
                self.assertNotRegex(label, pattern)


class VideoSelectionBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await async_playwright().start()
        self.browser = await self.runtime.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        # Even an accidental fixture resource cannot leave this offline context.
        await self.page.route('**/*', lambda route: route.abort())

    async def asyncTearDown(self):
        await self.browser.close()
        await self.runtime.stop()

    async def read(self, markup):
        await self.page.set_content('''<style>
          .composer {width:640px; padding:8px; margin:8px; border:1px solid}
          .toolbar {display:flex; gap:8px} button {min-height:32px}
        </style>''' + markup)
        return await self.page.evaluate(SELECTION_JS)

    async def test_deep_current_composer_reads_unkeyed_combined_params(self):
        state = await self.read(composer())
        self.assertEqual(state, {'modelText': 'Model Dreamina Seedance 2.0 Fast',
                                 'durationText': '16:9 · 30s', 'ratioText': '16:9 · 30s'})

    async def test_split_spans_are_one_combined_control(self):
        state = await self.read(composer(label='<span>16:9</span><span> · </span><span>30s</span>'))
        self.assertEqual(state['durationText'], '16:9 · 30s')
        self.assertEqual(re.findall(r'\d+:\d+', state['ratioText']), ['16:9'])

    async def test_legacy_keyed_and_unkeyed_composers_still_read(self):
        for keyed in (True, False):
            with self.subTest(keyed=keyed):
                state = await self.read(composer(depth=2, label='Automatisch · 10 Sekunden', keyed=keyed))
                self.assertEqual(state['durationText'], 'Automatisch · 10 Sekunden')
                self.assertEqual(state['modelText'], 'Model Dreamina Seedance 2.0 Fast')

    async def test_history_menus_and_hidden_controls_cannot_supply_current_values(self):
        distractors = ('<div data-message-id="old">' + composer(label='9:16 · 15s') + '</div>'
                       '<div role="menu">' + composer(label='1:1 · 5s') + '</div>'
                       '<div style="display:none">' + composer(label='4:3 · 10s') + '</div>')
        state = await self.read(distractors + composer())
        self.assertEqual(state['durationText'], '16:9 · 30s')
        self.assertEqual(re.findall(r'\d+:\d+', state['ratioText']), ['16:9'])
        self.assertEqual(state['modelText'], 'Model Dreamina Seedance 2.0 Fast')

    async def test_another_live_composer_cannot_change_bottom_editor_values(self):
        state = await self.read(composer(identifier='first', model='Model Seedance 2.5', label='9:16 · 15s')
                                + composer(identifier='second'))
        self.assertEqual(state['modelText'], 'Model Dreamina Seedance 2.0 Fast')
        self.assertEqual(state['durationText'], '16:9 · 30s')

    async def test_page_wide_controls_are_never_used_when_editor_has_no_toolbar(self):
        state = await self.read(composer(editor=False) + composer(model='', label=''))
        self.assertEqual(state, {'modelText': '', 'durationText': '', 'ratioText': ''})

    async def test_ambiguous_live_duration_controls_cannot_confirm_selection(self):
        markup = composer().replace('</div></section>', '<button>9:16 · 15s</button></div></section>')
        state = await self.read(markup)
        self.assertEqual(state['durationText'], '')
        self.assertEqual(state['ratioText'], '')


if __name__ == '__main__':
    unittest.main()
