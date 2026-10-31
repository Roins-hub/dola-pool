"""Replay mixed-language controls without a browser, account, or network."""
import ast
import re
import unittest
from pathlib import Path
from navigation_errors import BrowserSetupError


class Element:
    def __init__(self, text='', placeholder=None):
        self.text, self.placeholder = text, placeholder

    async def is_visible(self):
        return True

    async def get_attribute(self, name):
        return self.placeholder if name == 'placeholder' else None


class Locator:
    def __init__(self, elements):
        self.elements = elements

    async def count(self):
        return len(self.elements)

    def nth(self, i):
        return self.elements[i]


class Page:
    def __init__(self, placeholder, controls=()):
        self.input = Element(placeholder=placeholder)
        self.controls = [Element(text=t) for t in controls]

    async def query_selector(self, selector):
        return self.input if selector == 'textarea' else None

    def get_by_text(self, pattern, exact=False):
        return Locator([e for e in self.controls if pattern.search(e.text)])


def worker_scope(page):
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'video_worker_ui.py').read_text(encoding='utf-8'))
    functions = {'_first_visible', '_video_mode_on', '_panel_ready', '_dur_ok', '_page_unavailable_reason'}
    constants = {'VIDEO_BTN', 'MODEL_RE', 'DUR_CHIP_RE', 'RATIO_CHIP_RE', 'VIDEO_PLACEHOLDER_RE'}
    nodes = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in functions:
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id in constants for t in node.targets):
            nodes.append(node)
    ns = {'re': re, 'page': page, '_ph_before': 'Nachricht senden...', 'duration': 30,
          'BrowserSetupError': BrowserSetupError}
    exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                 'video-worker-locale', 'exec'), ns)
    return ns


class VideoLocaleReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_translated_chat_placeholder_does_not_mean_video_mode_is_open(self):
        for placeholder in ('发消息...', '发送消息...', 'Message...', 'Send a message...'):
            with self.subTest(placeholder=placeholder):
                ns = worker_scope(Page(placeholder, ['Videos erstellen', 'Schnell']))
                self.assertFalse(await ns['_video_mode_on']())

    async def test_real_video_placeholder_keeps_waiting_for_controls(self):
        for placeholder in ('添加照片，描述你想生成的视频',
                            'Add an image and describe actions in the video',
                            'Beschreibe die Handlungen im Video'):
            with self.subTest(placeholder=placeholder):
                ns = worker_scope(Page(placeholder))
                self.assertTrue(await ns['_video_mode_on']())

    async def test_chinese_model_control_marks_video_panel_ready(self):
        ns = worker_scope(Page('发消息...', ['模型 2.5']))
        self.assertTrue(await ns['_panel_ready']())
        self.assertTrue(await ns['_video_mode_on']())

    async def test_localized_video_entry_and_duration_are_recognized(self):
        ns = worker_scope(Page('发送消息', ['自动 · 30 秒']))
        self.assertTrue(await ns['_panel_ready']())
        self.assertIsNotNone(ns['VIDEO_BTN'].fullmatch('视频生成'))
        self.assertIsNone(ns['VIDEO_BTN'].fullmatch('我的视频生成历史'))

    def test_duration_validation_handles_localized_units_without_accepting_ten(self):
        ns = worker_scope(Page(''))
        for label in ('30s', '自动 · 30 秒', '30秒', 'Auto · 30 seconds', '30 Sekunden'):
            with self.subTest(label=label):
                self.assertTrue(ns['_dur_ok'](label))
        for label in ('自动 · 10 秒', '130s', '30:10'):
            self.assertFalse(ns['_dur_ok'](label))


if __name__ == '__main__':
    unittest.main()
