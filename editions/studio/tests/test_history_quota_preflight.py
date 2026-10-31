"""Old conversation notices must not block preparation of a new video request."""
from contextlib import asynccontextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

# Reuse the suite's isolated proxy-usage database before importing the worker.
from test_video_pipeline import pool_module
import video_worker_ui as worker


class ReachedNewChat(Exception):
    """Stop at the preparation boundary, before any prompt can be entered."""


class HistoryQuotaPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_old_limit_and_balance_notices_do_not_block_new_chat_preparation(self):
        for historical_reply in (
            "You've reached daily usage limit for video generation.",
            'Remaining: 0 points',
        ):
            with self.subTest(historical_reply=historical_reply):
                page = SimpleNamespace(wait_for_timeout=AsyncMock(),
                    evaluate=AsyncMock(return_value={'ok': True, 'texts': [historical_reply]}))
                context = SimpleNamespace(pages=[page], route=AsyncMock(),
                    cookies=AsyncMock(return_value=[]))

                @asynccontextmanager
                async def driver():
                    yield object()

                async def enter_new_chat(target):
                    self.assertIs(target, page)
                    raise ReachedNewChat()

                with patch.object(worker, '_submission_playwright', driver), \
                     patch.object(worker, 'launch_account_context', AsyncMock(return_value=context)), \
                     patch.object(worker, '_close_submission_context', AsyncMock()), \
                     patch.object(worker, '_install_payload_log'), \
                     patch.object(worker, 'goto_retry', AsyncMock()), \
                     patch.object(worker, '_handle_birthday_modal', AsyncMock()), \
                     patch.object(worker, '_dismiss_login_modal', AsyncMock(return_value=False)), \
                     patch.object(worker, '_dismiss_cookie_banner', AsyncMock(return_value=False)), \
                     patch.object(worker, '_ensure_new_chat', enter_new_chat), \
                     patch.object(worker.proxy_usage, 'begin_run', return_value=None), \
                     patch.object(worker.proxy_usage, 'finish_run', return_value=0), \
                     patch('dola_watermark.ensure_page_preference', AsyncMock()):
                    outcome = None
                    try:
                        await worker.generate_video('fixture', '', duration=10,
                            model='seedance-2.0', prepare_only=True)
                    except Exception as error:
                        outcome = error

                self.assertIsInstance(outcome, ReachedNewChat,
                    f'Historical notices must not reject a new request: {outcome!r}')
                page.evaluate.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
