import ast
import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
import time

from test_video_pipeline import pool_module
import video_worker_ui as worker


class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_confirmation_after_saved_acceptance_keeps_polling_to_the_result(self):
        from test_video_metadata import mp4
        import tempfile
        fetch = AsyncMock(side_effect=[{'assistant_texts': ['请确认生成下一个视频？'], 'videos': []},
                                       {'assistant_texts': [], 'videos': ['https://cdn.test/result.mp4']}])
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'video.mp4'
            path.write_bytes(mp4(30.08))
            with patch.object(worker.asyncio, 'sleep', AsyncMock()):
                result = await worker._wait_for_video('fixture', '123', time.time()+1, fetch,
                    AsyncMock(return_value=path), model='seedance_v2.5', accepted=True)
        self.assertAlmostEqual(result['actual_duration'], 30.08)
        self.assertEqual(fetch.await_count, 2)

    async def test_browser_close_failure_prevents_the_explicit_no_start_retry(self):
        from navigation_errors import BrowserCleanupError
        from video_outcome import ExplicitNotAcceptedError
        from types import SimpleNamespace
        context = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError('browser close failed')))
        async def close_failed_attempt():
            try:
                raise ExplicitNotAcceptedError('请确认时长')
            finally:
                await worker._close_submission_context(context)
        # A retryable no-start reply must be superseded by an unsafe-cleanup
        # error, so the pool cannot open another browser on the old ISP.
        with self.assertRaises(BrowserCleanupError):
            await close_failed_attempt()
        context.close.assert_awaited_once()

    async def test_saved_acceptance_is_kept_when_receipt_is_not_repeated(self):
        fetch = AsyncMock(return_value={'assistant_texts': [], 'videos': []})
        with self.assertRaises(worker.VideoQueryTimeout):
            await worker._wait_for_video('fixture', '123', time.time() + .005, fetch, AsyncMock(), accepted=True)

    async def test_only_query_faults_end_as_query_timeout_instead_of_unaccepted(self):
        fetch = AsyncMock(side_effect=TimeoutError())
        with self.assertRaises(Exception) as caught:
            await worker._wait_for_video('fixture', '123', time.time() + .005, fetch, AsyncMock())
        self.assertEqual(getattr(caught.exception, 'task_phase', None), 'query_timeout')

    async def test_receipt_then_late_generic_copyright_is_terminal_with_specific_reason(self):
        receipt = 'Das Video wird mit Dreamina Seedance 2.5 generiert. Dafür werden 2 Credits verwendet.'
        copyright = 'Zum Schutz des Urheberrechts kann ich dir das generierte Video nicht anzeigen.'
        reads = 0
        async def replies():
            nonlocal reads
            reads += 1
            return {'assistant_texts': [receipt] if reads == 1 else [copyright, receipt], 'videos': []}
        fetch = AsyncMock(side_effect=replies)
        download = AsyncMock()
        events = []
        with patch.object(worker.asyncio, 'sleep', AsyncMock()):
            with self.assertRaises(Exception) as caught:
                await worker._wait_for_video('fixture', '123', time.time() + 1, fetch, download,
                    on_event=lambda phase, details: events.append(phase), model='seedance_v2.5')
        self.assertEqual(getattr(caught.exception, 'task_phase', None), 'platform_copyright')
        self.assertEqual(fetch.await_count, 2)
        self.assertIn('accepted', events)
        download.assert_not_awaited()

    async def test_receipt_wait_copyright_overrides_historical_receipt(self):
        receipt = 'Seedance 2.5 将消耗2个视频生成额度。'
        copyright = 'Zum Schutz des Urheberrechts kann ich dir das generierte Video aufgrund seines Audios nicht anzeigen.'
        with self.assertRaises(RuntimeError) as caught:
            await self.wait([{'assistant_texts': [receipt, copyright], 'videos': []}])
        self.assertEqual(getattr(caught.exception, 'task_phase', None), 'platform_audio_copyright')

    async def test_result_overrides_old_credit_and_copyright_text(self):
        from test_video_metadata import mp4
        import tempfile
        fetch = AsyncMock(return_value={'videos': ['https://cdn.test/result.mp4'], 'assistant_texts': [
            'daily video limit reached',
            'Zum Schutz des Urheberrechts kann ich dir das generierte Video nicht anzeigen.']})
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'result.mp4'
            path.write_bytes(mp4(30.08))
            download = AsyncMock(return_value=path)
            result = await worker._wait_for_video('fixture', '123', time.time() + 1,
                                                 fetch, download, model='seedance_v2.5')
        self.assertAlmostEqual(result['actual_duration'], 30.08)
        download.assert_awaited_once()

    async def test_native_ratio_prompt_echo_cannot_count_as_receipt(self):
        prompt = '原文 Seedance 2.5 消耗2个额度'
        with self.assertRaisesRegex(RuntimeError, '未确认进入生成'):
            await self.wait([{'texts': ['Generiertes Video: ' + prompt + ', 16:9']}], prompt=prompt)

    async def test_native_video_label_cannot_turn_user_prompt_echo_into_generation_receipt(self):
        prompt = '原文 Seedance 2.5 消耗2个额度'
        with self.assertRaisesRegex(RuntimeError, '未确认进入生成'):
            await self.wait([{'texts': ['Generiertes Video: ' + prompt]}], prompt=prompt)

    async def test_real_audio_copyright_reply_ends_polling_without_download_or_resubmit(self):
        text = ('Zum Schutz des Urheberrechts kann ich dir das generierte Video aufgrund seines Audios '
                'nicht anzeigen. Verwende andere Referenzen oder bearbeite den Prompt und versuche es erneut.')
        fetch = AsyncMock(return_value={'assistant_texts': [text], 'videos': []})
        download = AsyncMock()
        with patch.object(worker.asyncio, 'sleep', AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, 'Dola 返回无法生成视频'):
                await worker._wait_for_video('fixture', '123', time.time() + .03,
                                             fetch, download, model='seedance_v2.5')
        fetch.assert_awaited_once()
        download.assert_not_awaited()

    async def wait(self, responses, prompt='原文'):
        waiter = getattr(worker, '_wait_for_generation_receipt', None)
        self.assertIsNotNone(waiter, '2.5/30 must wait for a model-and-credit receipt before closing its UI')
        class Page:
            def __init__(self): self.reads = 0
            async def evaluate(self, *args):
                item = responses[min(self.reads, len(responses)-1)]
                self.reads += 1
                return item
            async def wait_for_timeout(self, *args): await asyncio.sleep(0)
        page = Page()
        with patch.object(worker, '_dismiss_login_modal', return_value=False):
            await waiter(page, 'fixture', '123', prompt, timeout=.02)
        return page.reads

    async def test_only_model_and_two_credits_receipt_releases_browser(self):
        reads = await self.wait([
            {'texts': ['视频生成目前支持4到15秒的时长。我可以按最近的支持时长15秒生成。']},
            {'texts': ['本次使用 Dreamina Seedance 2.5 生成，将消耗 2 个视频生成额度，预计等待55分钟。']},
        ])
        self.assertEqual(reads, 2)

    async def test_prompt_echo_or_wrong_receipt_does_not_confirm_generation(self):
        for text in ('原文 Seedance 2.5 消耗2个额度', 'Seedance 2.0 将消耗2个视频生成额度',
                     'Seedance 2.5 将消耗1个视频生成额度', 'Seedance 2.5 将消耗20个视频生成额度',
                     'Seedance 2.5 已进入生成', '我可以生成15秒'):
            with self.subTest(text=text):
                with self.assertRaisesRegex(RuntimeError, '未确认进入生成'):
                    await self.wait([{'texts': [text]}], prompt='原文 Seedance 2.5 消耗2个额度')

    async def test_german_and_english_receipts_are_recognized(self):
        for text in ('Das Video wird mit Dreamina Seedance 2.5 generiert. Es werden 2 Credits verbraucht.',
                     'Generating with Seedance 2.5. This will use 2 video generation credits.'):
            with self.subTest(text=text):
                self.assertEqual(await self.wait([{'texts': [text]}]), 1)

    async def test_confirmation_choices_and_no_charge_statements_are_not_receipts(self):
        for text in (
            'Seedance 2.5 需要2个额度，请确认是否开始生成两个15秒视频。',
            'Seedance 2.5 将消耗2个额度，是否继续生成？',
            'Seedance 2.5 尚未开始生成，不会消耗2个额度。',
            'Seedance 2.5 costs 2 credits. Would you like me to generate two 15-second videos?',
            'Seedance 2.5 uses 2 credits. Please confirm before I start.',
            'Seedance 2.5: 2 Credits. Soll ich das Video generieren?',
            'Seedance 2.5: 2 Credits werden nicht verbraucht, die Generierung wurde nicht gestartet.',
        ):
            with self.subTest(text=text), self.assertRaises(RuntimeError) as caught:
                await self.wait([{'texts': [text]}])
            self.assertEqual(getattr(caught.exception, 'task_phase', None), 'not_accepted')

    async def test_login_loss_during_receipt_wait_is_propagated(self):
        waiter = getattr(worker, '_wait_for_generation_receipt', None)
        self.assertIsNotNone(waiter)
        with patch.object(worker, '_dismiss_login_modal', side_effect=worker.LoginExpiredError('登录墙')):
            with self.assertRaises(worker.LoginExpiredError):
                await waiter(object(), 'fixture', '123', '原文', timeout=1)

    async def test_generate_tail_uses_actual_submitted_text_before_leaving_browser(self):
        # Run the production acceptance tail without starting Chromium or sending a request.
        tree = ast.parse(Path('video_worker_ui.py').read_text(encoding='utf-8'))
        generate = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'generate_video')
        block = next(n for n in ast.walk(generate) if isinstance(n, ast.Try) and any(
            isinstance(x, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'deadline' for t in x.targets)
            for x in n.body))
        start = next(i for i, n in enumerate(block.body) if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == 'deadline' for t in n.targets))
        fn = ast.parse('async def run(): pass').body[0]
        fn.body = block.body[start:]
        observed = []
        class Context:
            async def cookies(self, *args): return []
        class Page:
            async def evaluate(self, expression, *args):
                if expression == 'navigator.userAgent': return 'fixture'
                observed.append('polled while browser open')
                return {'texts': ['本次使用 Seedance 2.5 生成，将消耗2个视频生成额度。']}
        ns = dict(vars(worker), page=Page(), context=Context(), account='fixture', conv_id='123',
                  timeout=1, duration=30, model='seedance-2.5', send_text='完整原文',
                  model_key='seedance_v2.5', on_event=None, request_evidence=[{'matches': True}],
                  previous_conversation=None, baseline_media=[], baseline_messages=[],
                  on_conversation_id=lambda *args: observed.append('saved conversation'),
                  background_handoff=False,
                  save_poll_state=lambda *args: None)
        exec(compile(ast.fix_missing_locations(ast.Module(body=[fn], type_ignores=[])), 'acceptance-tail', 'exec'), ns)
        for audit in ([{'matches': True}], [{'matches': False, 'prompt_matches': False}], []):
            with self.subTest(audit=audit):
                observed.clear()
                ns['request_evidence'] = audit
                with patch.object(worker, '_dismiss_login_modal', return_value=False):
                    await ns['run']()
                self.assertEqual(observed, ['saved conversation', 'polled while browser open'])
