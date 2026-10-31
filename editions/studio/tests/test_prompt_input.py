"""Replay the actual pre-submit flow without accounts, browsers, or network."""
import ast
from pathlib import Path
import unittest
from unittest.mock import patch

from test_video_pipeline import pool_module
import video_worker_ui as worker
from navigation_errors import BrowserSetupError


class Editor:
    def __init__(self, kind='editable', *, visible=True, editable=True,
                 paragraph_breaks=False, fill_fails=False, truncate=False):
        self.kind, self.visible, self.editable = kind, visible, editable
        self.paragraph_breaks, self.fill_fails, self.truncate = paragraph_breaks, fill_fails, truncate
        self.value = ''
        self.page = None

    async def is_visible(self): return self.visible
    async def is_editable(self): return self.editable

    async def click(self, **kwargs):
        if not self.visible or not self.editable:
            raise RuntimeError('not actionable')
        self.page.active = self

    async def fill(self, text, **kwargs):
        await self.click()
        if self.fill_fails:
            raise RuntimeError('editor rejected fill')
        self.value = text[:25] if self.truncate else text

    async def input_value(self, **kwargs):
        if self.kind != 'textarea':
            raise RuntimeError('not a textarea')
        return self.value

    async def inner_text(self, **kwargs):
        return self.value.replace('\n\n', '\n\n\n') if self.paragraph_breaks else self.value

    async def press(self, key, **kwargs):
        await self.click()
        await self.page.keyboard.press(key)


class Locator:
    def __init__(self, elements): self.elements = elements
    async def count(self): return len(self.elements)
    def nth(self, index): return self.elements[index]


class SendButton:
    def __init__(self, page): self.page = page
    async def is_visible(self): return True
    async def is_enabled(self): return True
    async def get_attribute(self, name): return None
    async def click(self, **kwargs):
        self.page.sent.append(self.page.active.value)
        self.page.active.value = ''


class Keyboard:
    def __init__(self, page): self.page, self.selected = page, False
    async def press(self, key):
        if key == 'Enter':
            self.page.sent.append(self.page.active.value)
            self.page.active.value = ''
        elif key in ('Control+A', 'ControlOrMeta+A', 'Meta+A'):
            self.selected = True
    async def type(self, text, **kwargs):
        if self.page.active is None:
            raise RuntimeError('no focused editor')
        editor = self.page.active
        text = text[:25] if editor.truncate else text
        editor.value = text if self.selected else editor.value + text
        self.selected = False
    async def insert_text(self, text): await self.type(text)


class Page:
    def __init__(self, *editors):
        self.editors, self.active, self.sent = list(editors), None, []
        self.keyboard = Keyboard(self)
        for editor in editors: editor.page = self
    def matching(self, selector):
        return [e for e in self.editors if (e.kind == 'textarea') == (selector == 'textarea')]
    async def query_selector(self, selector):
        return next(iter(self.matching(selector)), None)
    def locator(self, selector):
        if selector == '#flow-end-msg-send':
            return Locator([SendButton(self)])
        return Locator(self.matching(selector))
    async def wait_for_timeout(self, ms): pass
    async def screenshot(self, **kwargs): pass
    async def evaluate(self, script):
        return {'modelText': 'Model Seedance 2.0 Fast', 'durationText': '10s', 'ratioText': ''}


def actual_input_flow():
    tree = ast.parse(Path(worker.__file__).read_text(encoding='utf-8'))
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'generate_video')
    for block in ast.walk(fn):
        body = getattr(block, 'body', None)
        if not isinstance(body, list): continue
        for start, node in enumerate(body):
            if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name) and node.value.func.id == 'build_video_prompt'):
                end = next(i for i in range(start, len(body))
                           if isinstance(body[i], ast.Expr) and isinstance(body[i].value, ast.Await)
                           and isinstance(body[i].value.value, ast.Call)
                           and isinstance(body[i].value.value.func, ast.Name)
                           and body[i].value.value.func.id == '_submit_prompt_once')
                wrapper = ast.parse('async def run(page, account, prompt, duration, model, on_prompt_sent, ratio=None): pass').body[0]
                wrapper.body = body[start:end + 1]
                namespace = dict(vars(worker))
                exec(compile(ast.fix_missing_locations(ast.Module(body=[wrapper], type_ignores=[])),
                             'actual-prompt-input-flow', 'exec'), namespace)
                return namespace['run']
    raise AssertionError('Cannot locate the production prompt submission flow')


class PromptInputTests(unittest.IsolatedAsyncioTestCase):
    async def run_flow(self, page, prompt='一个韩国美女在跳舞'):
        async def no_modal(*args): return False
        with patch.object(worker, '_handle_birthday_modal', no_modal), patch.object(worker, '_dismiss_login_modal', no_modal):
            await actual_input_flow()(page, 'fixture', prompt, 10, 'seedance-2.0', None)

    async def test_rendered_paragraph_spacing_does_not_reject_a_fully_written_prompt(self):
        page = Page(Editor(paragraph_breaks=True))
        await self.run_flow(page)
        self.assertEqual(page.sent, [worker.build_video_prompt('一个韩国美女在跳舞', 10, 'seedance-2.0')])

    async def test_hidden_and_readonly_textareas_do_not_shadow_the_real_editor(self):
        hidden, readonly, visible = Editor('textarea', visible=False), Editor('textarea', editable=False), Editor()
        page = Page(hidden, readonly, visible)
        await self.run_flow(page)
        self.assertEqual(page.sent, [worker.build_video_prompt('一个韩国美女在跳舞', 10, 'seedance-2.0')])
        self.assertEqual((hidden.value, readonly.value), ('', ''))

    async def test_truncated_prompt_is_never_submitted_or_treated_as_an_account_failure(self):
        page = Page(Editor('textarea', truncate=True))
        with self.assertRaises(BrowserSetupError):
            await self.run_flow(page, '请完整保留这段很长的舞蹈提示词。' * 15)
        self.assertEqual(page.sent, [])

    async def test_keyboard_fallback_replaces_the_draft_instead_of_appending(self):
        editor = Editor(fill_fails=True)
        editor.value = '旧草稿不能跟着发送'
        page = Page(editor)
        await self.run_flow(page)
        self.assertEqual(page.sent, [worker.build_video_prompt('一个韩国美女在跳舞', 10, 'seedance-2.0')])

    async def test_normal_textarea_preserves_entire_user_text_and_only_sends_once(self):
        page = Page(Editor('textarea'))
        await self.run_flow(page, '  第一行\n第二行  两个空格\n\n结束  ')
        self.assertEqual(page.sent, [worker.build_video_prompt('  第一行\n第二行  两个空格\n\n结束  ', 10, 'seedance-2.0')])


if __name__ == '__main__': unittest.main()
