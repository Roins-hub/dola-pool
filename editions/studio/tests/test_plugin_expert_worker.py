"""Drive the real worker against an isolated native-send fixture, without HTTP."""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
import unittest


class ExpertWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_selected_protocol_reaches_one_native_request_and_original_conversation_poll(self):
        import plugin_expert30 as worker
        from plugin_resources import run_resolver
        for model, seconds, two_part in [('seedance-2.0-mini', 5, False),
                                         ('seedance-2.0-std', 10, False),
                                         ('seedance-2.0', 15, False),
                                         ('seedance-2.5', 30, True)]:
            with self.subTest(model=model, seconds=seconds):
                sent, events, fallbacks, init_scripts = [], [], [], []
                prompt = '原始提示词\n逐行保留'

                class Page:
                    url = 'https://www.dola.com/chat'
                    draft = ''
                    wait_for_function = AsyncMock()
                    async def evaluate(self, script, arg=None):
                        if "data-dola-protocol-send-request','project-'" in script:
                            prefix = (await run_resolver({'action': 'parameter_prompt', 'context': arg['p']}))['prefix']
                            body = {'option': {'agent_mode': 1, 'model_config': {'model_item_key': '4'}},
                                    'messages': [{'content_blocks_v2': [{'block_type': 10000,
                                        'content': {'text_block': {'text': prefix + self.draft}}}]}]}

                            class Request:
                                method = 'POST'
                                post_data_json = body

                            class Route:
                                request = Request()
                                async def fallback(self): fallbacks.append(body)
                                async def abort(self, reason): raise AssertionError('Valid native request was blocked: ' + reason)

                            await context.guard(Route())
                            self.url = 'https://www.dola.com/chat/123456789'
                        if script == 'navigator.userAgent': return 'fixture-agent'
                        return None
                    def locator(self, selector):
                        class Locator:
                            async def all(self): return []
                        return Locator()

                page = Page()

                class Context:
                    pages = [page]
                    def on(self, *args): pass
                    async def route(self, url, callback): self.guard = callback
                    async def add_init_script(self, script): init_scripts.append(script)
                    async def cookies(self, *args): return [{'name': 'sessionid', 'value': 'fixture-session'}]

                context = Context()

                @asynccontextmanager
                async def driver(): yield object()

                async def fill(_page, text): _page.draft = text

                resume = AsyncMock(return_value={'clips': [], 'fixture': True})
                from test_plugin_expert_receipt import packet
                with patch('video_worker_ui._submission_playwright', driver), \
                     patch('browser.launch_account_context', AsyncMock(return_value=context)), \
                     patch('video_worker_ui._close_submission_context', AsyncMock()), \
                     patch('video_worker_ui.goto_retry', AsyncMock()), \
                     patch('video_worker_ui._dismiss_cookie_banner', AsyncMock()), \
                     patch('video_worker_ui._dismiss_login_modal', AsyncMock()), \
                     patch('video_worker_ui._ensure_new_chat', AsyncMock()), \
                     patch('video_worker_ui._read_page_conversation', AsyncMock(return_value=packet(status=2))), \
                     patch('video_polling.save_poll_state'), \
                     patch.object(worker, '_fill_plugin_draft', fill), \
                     patch.object(worker, 'resume_video', resume):
                    result = await worker.generate_video('fixture', prompt, '9:16', seconds, model=model,
                        generation_options={'resolution': '1080p', 'two_part': two_part},
                        on_prompt_sent=lambda account: sent.append(account),
                        on_event=lambda phase, details: events.append((phase, details)))
                self.assertTrue(result['fixture'])
                self.assertEqual(sent, ['fixture'])
                self.assertEqual(len(fallbacks), 1)
                self.assertEqual(events[0][0], 'submitted')
                self.assertEqual(sum(phase == 'submitted' for phase, _ in events), 1)
                self.assertEqual(sum(phase == 'accepted' for phase, _ in events), 1)
                self.assertIn(f'时长：{seconds}秒', fallbacks[0]['messages'][0]['content_blocks_v2'][0]['content']['text_block']['text'])
                self.assertEqual(page.draft.count('【插件两段式生成规则 v2】'), int(two_part))
                self.assertTrue(page.draft.startswith(prompt))
                self.assertIn('String(p.duration)', init_scripts[0])
                self.assertIn(f'"duration": {seconds}', init_scripts[0])
                self.assertEqual(resume.call_args.args[:2], ('fixture', '123456789'))
                self.assertEqual(resume.call_args.kwargs['generation_options']['requested_duration'], seconds)
                self.assertEqual(resume.call_args.kwargs['generation_options']['two_part'], two_part)


if __name__ == '__main__':
    unittest.main()
