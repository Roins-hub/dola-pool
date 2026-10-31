"""Replay the existing native Enter send after parameter checks steal focus."""
import unittest
from unittest.mock import patch

from test_prompt_input import Editor, Page, actual_input_flow
import video_worker_ui as worker
from navigation_errors import BrowserSetupError


class ObscuredSendButton:
    async def is_visible(self): return True
    async def is_enabled(self): return True
    async def get_attribute(self, name): return None
    async def click(self, **kwargs):
        raise RuntimeError('resource panel intercepts pointer events')


class FocusPage(Page):
    def locator(self, selector):
        from test_prompt_input import Locator
        if selector == '#flow-end-msg-send':
            return Locator([ObscuredSendButton()])
        return super().locator(selector)


class SendFocusTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_enter_flow_refocuses_filled_editor_after_selection_checks(self):
        page = FocusPage(Editor('textarea'))
        events = []
        async def no_modal(*args): return False
        async def selection(*args): page.active = None
        with patch.object(worker, '_handle_birthday_modal', no_modal), \
             patch.object(worker, '_dismiss_login_modal', no_modal), \
             patch.object(worker, '_verify_video_selection', selection):
            await actual_input_flow()(page, 'fixture', '第一行\n完整提示词', 10, 'seedance-2.0', lambda a: events.append(a))
        self.assertEqual(events, ['fixture'])
        self.assertEqual(page.sent, [worker.build_video_prompt('第一行\n完整提示词', 10, 'seedance-2.0')])

    async def test_empty_or_hidden_draft_never_reaches_send_boundary(self):
        async def no_modal(*args): return False
        for editor in (Editor('textarea'), Editor('textarea', visible=False)):
            page = FocusPage(editor)
            events = []
            with self.subTest(visible=editor.visible), \
                 patch.object(worker, '_handle_birthday_modal', no_modal), \
                 patch.object(worker, '_dismiss_login_modal', no_modal):
                with self.assertRaises(BrowserSetupError):
                    await worker._submit_prompt_once(page, 'fixture', events.append)
            self.assertEqual(events, [])
            self.assertEqual(page.sent, [])

    async def test_uncertain_enter_does_not_fall_back_to_click_or_repeat(self):
        page = FocusPage(Editor('textarea'))
        page.editors[0].value = 'confirmed draft'
        events = []
        async def no_modal(*args): return False
        async def fail_press(key):
            events.append(key)
            raise RuntimeError('unknown send outcome')
        page.keyboard.press = fail_press
        with patch.object(worker, '_handle_birthday_modal', no_modal), \
             patch.object(worker, '_dismiss_login_modal', no_modal):
            with self.assertRaisesRegex(RuntimeError, 'unknown send outcome'):
                await worker._submit_prompt_once(page, 'fixture', events.append)
        self.assertEqual(events, ['fixture', 'Enter'])


if __name__ == '__main__': unittest.main()
