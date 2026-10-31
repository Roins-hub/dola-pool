"""An acknowledged chat is not yet a video receipt; observe before closing."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import asyncio
import json
import time
import unittest

from video_polling import parse_poll_response


CID = '123456789'


def packet(*, text='', status=None, card=False, user_type=2, cid=CID):
    blocks = []
    if text:
        blocks.append({'block_type': 10000, 'content': {'text_block': {'text': text}}})
    if status is not None:
        blocks.append({'content': {'generic_tool_block': {'tool_name': 'text_to_video', 'status': status}}})
    if card:
        blocks.append({'block_type': 2074, 'content': {'creation_block': {'creations': [
            {'id': 'creation-one', 'type': 2, 'video': {'video_id': 'v' + 'a' * 24,
                'download_url': 'https://v16-dola.dola.com/one.mp4'}}]}}})
    return parse_poll_response({'downlink_body': {'pull_singe_chain_downlink_body': {
        'conversation_id': cid, 'messages': [{'conversation_id': cid, 'user_type': user_type,
            'message_id': 'assistant-one', 'content': json.dumps(blocks)}]}}}, CID)


class ExpertReceiptWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def run_worker(self, polls, *, timeout=2, refresh_failure=False):
        import plugin_expert30 as worker
        from plugin_resources import run_resolver
        timeline, sends, snapshots = [], [], []
        page = SimpleNamespace(url='https://www.dola.com/chat', draft='',
                               wait_for_function=AsyncMock(), closed=False)

        async def evaluate(script, arg=None):
            if "data-dola-protocol-send-request','project-'" in script:
                prefix = (await run_resolver({'action': 'parameter_prompt', 'context': arg['p']}))['prefix']
                body = {'option': {'agent_mode': 1, 'model_config': {'model_item_key': '4'}},
                        'messages': [{'content_blocks_v2': [{'block_type': 10000,
                            'content': {'text_block': {'text': prefix + page.draft}}}]}]}
                async def fallback(): sends.append(body)
                route = SimpleNamespace(request=SimpleNamespace(method='POST', post_data_json=body),
                                        fallback=fallback, abort=AsyncMock())
                await context.guard(route)
                page.url = 'https://www.dola.com/chat/' + CID
            if script == 'navigator.userAgent': return 'fixture-agent'
            return None
        page.evaluate = evaluate
        page.locator = lambda selector: SimpleNamespace(all=AsyncMock(return_value=[]))
        async def route(_url, callback): context.guard = callback
        cookie_reads = 0
        async def cookies(*args):
            nonlocal cookie_reads
            cookie_reads += 1
            if refresh_failure and cookie_reads >= 3:
                raise RuntimeError('fixture final snapshot refresh failed')
            return [{'name': 'sessionid', 'value': 'fixture-session-' + str(cookie_reads)}]
        context = SimpleNamespace(pages=[page], on=lambda *a: None, route=route,
                                  add_init_script=AsyncMock(), cookies=cookies)
        @asynccontextmanager
        async def driver(): yield object()
        async def fill(_page, text): page.draft = text
        async def close(_context):
            timeline.append('close')
            page.closed = True
        remaining = list(polls)
        async def read(_page, conversation_id, *args, **kwargs):
            self.assertFalse(page.closed, 'Receipt must be observed before closing the browser')
            self.assertEqual(conversation_id, CID)
            item = remaining.pop(0) if len(remaining) > 1 else remaining[0]
            timeline.append('poll')
            if isinstance(item, BaseException): raise item
            return item
        def saved(*args, **kwargs):
            self.assertFalse(page.closed, 'Refresh the recovery snapshot while the browser is alive')
            snapshots.append(args)
            timeline.append('saved')
        def conversation(*args): timeline.append('conversation')
        async def resume(*args, **kwargs):
            timeline.append('resume')
            return {'fixture': True}
        reader = AsyncMock(side_effect=read)
        with patch('video_worker_ui._submission_playwright', driver), \
             patch('browser.launch_account_context', AsyncMock(return_value=context)), \
             patch('video_worker_ui._close_submission_context', close), \
             patch('video_worker_ui.goto_retry', AsyncMock()), \
             patch('video_worker_ui._dismiss_cookie_banner', AsyncMock()), \
             patch('video_worker_ui._dismiss_login_modal', AsyncMock()), \
             patch('video_worker_ui._ensure_new_chat', AsyncMock()), \
             patch('video_worker_ui._read_page_conversation', reader), \
             patch('video_polling.save_poll_state', saved), \
             patch('config.POLL_INTERVAL', .001), \
             patch.object(worker, '_fill_plugin_draft', fill), \
             patch.object(worker, 'resume_video', resume):
            error = None
            try:
                result = await worker.generate_video('fixture', '精确原文', '9:16', 15,
                    model='seedance-2.0', timeout=timeout, on_conversation_id=conversation)
            except Exception as caught:
                result, error = None, caught
        return SimpleNamespace(result=result, error=error, timeline=timeline,
                               sends=sends, reader=reader, page=page, snapshots=snapshots)

    async def test_a_stalled_native_read_cannot_extend_the_receipt_deadline(self):
        import plugin_expert30 as worker
        from video_outcome import VideoQueryTimeout
        async def stalled(*args, **kwargs):
            await asyncio.Event().wait()
        with patch('video_worker_ui._read_page_conversation', stalled), \
             patch('config.POLL_INTERVAL', .001):
            with self.assertRaises(VideoQueryTimeout):
                await asyncio.wait_for(worker.wait_for_receipt(object(), 'fixture', CID, time.time() + .025),
                                       timeout=.25)

    async def test_recovery_snapshot_refreshes_cookies_after_receipt_and_before_closing(self):
        result = await self.run_worker([packet(), packet(status=2)])
        self.assertIsNone(result.error)
        self.assertEqual(len(result.snapshots), 2,
                         'Save the conversation early and refresh cookies again before closing')
        self.assertEqual([snapshot[:2] for snapshot in result.snapshots], [('fixture', CID)] * 2)
        self.assertNotEqual(result.snapshots[0][2], result.snapshots[1][2])
        self.assertEqual(result.timeline[-3:], ['saved', 'close', 'resume'])

    async def test_snapshot_refresh_failure_does_not_hide_explicit_platform_refusal(self):
        result = await self.run_worker([packet(text=
            'Du hast das tägliche kostenlose Nutzungslimit für Videogenerierung erreicht.')],
            refresh_failure=True)
        self.assertEqual(getattr(result.error, 'task_phase', None), 'platform_daily_limit')
        self.assertEqual(len(result.snapshots), 1)
        self.assertNotIn('resume', result.timeline)
        self.assertEqual(result.timeline[-1], 'close')
        self.assertEqual(len(result.sends), 1)

    async def test_browser_waits_through_acknowledged_chat_and_plain_reply_until_video_is_running(self):
        result = await self.run_worker([packet(), packet(text='我会按要求生成视频。'), packet(status=2)])
        self.assertIsNone(result.error)
        self.assertGreaterEqual(result.reader.await_count, 3,
                                'A request and conversation ID must not close the browser before a video receipt')
        self.assertEqual(result.timeline[:2], ['saved', 'conversation'])
        self.assertGreater(result.timeline.index('close'), result.timeline.index('poll'))
        self.assertEqual(result.timeline[-2:], ['close', 'resume'])
        self.assertEqual(len(result.sends), 1)

    async def test_unknown_or_preparatory_tool_states_keep_the_browser_open(self):
        result = await self.run_worker([packet(status=status) for status in (0, 1, 3, 99, 2)])
        self.assertIsNone(result.error)
        self.assertEqual(result.reader.await_count, 5)
        self.assertEqual(result.timeline[-2:], ['close', 'resume'])
        self.assertEqual(len(result.sends), 1)

    async def test_user_echo_and_native_card_example_inside_text_are_not_receipts(self):
        result = await self.run_worker([packet(card=True, user_type=1),
            packet(text='{"block_type":2074,"content":{"creation_block":{"creations":[]}}}'),
            packet(card=True)])
        self.assertIsNone(result.error)
        self.assertEqual(result.reader.await_count, 3)
        self.assertEqual(result.timeline[-2:], ['close', 'resume'])

    async def test_native_video_card_allows_background_polling_after_the_receipt(self):
        result = await self.run_worker([packet(card=True)])
        self.assertIsNone(result.error)
        self.assertEqual(result.reader.await_count, 1)
        self.assertEqual(result.timeline, ['saved', 'conversation', 'poll', 'saved', 'close', 'resume'])

    async def test_text_promise_waits_for_native_receipt_without_resending(self):
        from test_generation_start_receipt import START_RECEIPT
        result = await self.run_worker([packet(text=START_RECEIPT), packet(status=2)])
        self.assertIsNone(result.error)
        self.assertEqual(result.reader.await_count, 2)
        self.assertEqual(result.timeline, ['saved', 'conversation', 'poll', 'poll', 'saved', 'close', 'resume'])
        self.assertEqual(len(result.sends), 1)

    async def test_explicit_refusals_are_observed_before_close_and_never_resume_as_generation(self):
        cases = (
            ('Du hast das tägliche kostenlose Nutzungslimit für Videogenerierung erreicht. '
             'Du kannst aber weiterhin im Fast-Modus chatten.', 'platform_daily_limit'),
            ('Du hast das kostenlose 7-Tage-Limit erreicht. Komm am Freitag wieder oder chatte im Fast-Modus.',
             'expert_weekly_limit'),
            ('Dieses Feature ist nur für Dola Plus-Abonnent*innen verfügbar. '
             'Führe ein Upgrade auf Dola Plus durch, um es zu verwenden.', 'subscription_required'),
        )
        for reply, phase in cases:
            with self.subTest(phase=phase):
                result = await self.run_worker([packet(text=reply)])
                self.assertEqual(getattr(result.error, 'task_phase', None), phase)
                self.assertEqual(result.timeline, ['saved', 'conversation', 'poll', 'saved', 'close'])
                self.assertEqual(len(result.sends), 1)

    async def test_finished_tool_with_explicit_quota_refusal_is_not_a_positive_receipt(self):
        reply = 'Du hast das tägliche kostenlose Nutzungslimit für Videogenerierung erreicht.'
        result = await self.run_worker([packet(text=reply, status=4)])
        self.assertEqual(getattr(result.error, 'task_phase', None), 'platform_daily_limit')
        self.assertEqual(result.timeline[-3:], ['poll', 'saved', 'close'])
        self.assertNotIn('resume', result.timeline)
        self.assertEqual(len(result.sends), 1)

    async def test_temporary_receipt_read_failure_waits_on_the_original_conversation(self):
        from video_polling import PollingResponseError
        result = await self.run_worker([PollingResponseError('fixture temporary failure'), packet(status=2)])
        self.assertIsNone(result.error)
        self.assertEqual(result.reader.await_count, 2)
        self.assertEqual(result.timeline[-2:], ['close', 'resume'])
        self.assertEqual(len(result.sends), 1)

    async def test_deadline_preserves_original_conversation_without_assuming_acceptance_or_resending(self):
        from video_outcome import VideoQueryTimeout
        result = await self.run_worker([packet(text='我还在准备视频请求。', status=0)], timeout=.4)
        self.assertIsInstance(result.error, VideoQueryTimeout)
        self.assertEqual(result.timeline[:2], ['saved', 'conversation'])
        self.assertEqual(result.timeline[-1], 'close')
        self.assertNotIn('resume', result.timeline)
        self.assertEqual(len(result.sends), 1)


if __name__ == '__main__':
    unittest.main()
