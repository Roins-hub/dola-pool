"""Current-chat receipt and video evidence; no network, login or external sends."""
import json
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from providers.doubao.runtime import BrowserSession, evidence_from_payload


CAPS = {'selectors': {'assistant': '.assistant'},
        'receipt_patterns': ['视频生成中', '已提交生成'],
        'rejection_patterns': ['生成失败', '无法生成'], 'no_charge_patterns': ['未扣费']}


class Blocks:
    async def count(self): return 0


class Page:
    def __init__(self, chat): self.url = 'https://www.doubao.com/chat/' + chat
    def locator(self, selector): return Blocks()
    def is_closed(self): return False
    frames = ()  # Payload-only fixture has no verification frames.


def payload(chat, *messages):
    return {'conversation_id': chat, 'messages': list(messages)}


class CurrentChatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'snapshot.json'

    def session(self, chat='A', messages=(), recovery=None):
        provider = SimpleNamespace(_snapshot_path=lambda task: self.path)
        session = BrowserSession(provider, {'name': 'one'}, snapshot=recovery)
        session.page, session.payloads = Page(chat), list(messages)
        return session

    async def test_bound_chat_cannot_collect_another_chat_video(self):
        session = self.session('B', [payload('B', {'message_id': 'b', 'role': 'assistant',
                                                 'video_url': 'https://cdn.example/B.mp4'})])
        snapshot = {'id': 'video_' + '1'*32, 'conversation_id': 'A', 'page_url': 'https://www.doubao.com/chat/A'}
        result = await session.observe(snapshot, CAPS, timeout=0)
        self.assertEqual(result['phase'], 'unknown')
        self.assertEqual(result['conversation_id'], 'A')
        self.assertEqual(result['media'], [])
        self.assertEqual(snapshot['conversation_id'], 'A')

    async def test_conditional_failure_notice_does_not_override_receipt(self):
        session = self.session(messages=[payload('A', {'message_id': 'a', 'role': 'assistant',
            'content': '视频生成中。如果生成失败会退还积分，完成后通知你。'})])
        result = await session.observe({'conversation_id': 'A'}, CAPS, timeout=0)
        self.assertEqual(result['phase'], 'accepted')
        self.assertFalse(result['no_charge'])

    async def test_later_explicit_failure_overrides_earlier_accepted_message(self):
        session = self.session(messages=[payload('A',
            {'message_id': 'a', 'role': 'assistant', 'content': '视频生成中'},
            {'message_id': 'b', 'role': 'assistant', 'content': '本次生成失败，请修改内容。'})])
        result = await session.observe({'conversation_id': 'A'}, CAPS, timeout=0)
        self.assertEqual(result['phase'], 'rejected')

    async def test_later_accepted_message_replaces_previous_failure_without_new_send(self):
        session = self.session(messages=[payload('A',
            {'message_id': 'a', 'role': 'assistant', 'content': '上次生成失败'},
            {'message_id': 'b', 'role': 'assistant', 'content': '本次已提交生成，视频生成中'})])
        result = await session.observe({'conversation_id': 'A'}, CAPS, timeout=0)
        self.assertEqual(result['phase'], 'accepted')

    async def test_repeated_receipt_in_a_later_message_keeps_its_chronological_position(self):
        session = self.session(messages=[payload('A',
            {'message_id': 'a', 'role': 'assistant', 'content': '视频生成中'},
            {'message_id': 'b', 'role': 'assistant', 'content': '生成失败'},
            {'message_id': 'c', 'role': 'assistant', 'content': '视频生成中'})])
        result = await session.observe({'conversation_id': 'A'}, CAPS, timeout=0)
        self.assertEqual(result['phase'], 'accepted')

    async def test_first_chat_is_durable_while_the_long_observe_window_is_still_running(self):
        snapshot = {'id': 'video_' + '1'*32, 'sent': True}
        session = self.session(messages=[payload('A', {'message_id': 'a', 'role': 'assistant', 'content': '视频生成中'})])
        pending = asyncio.create_task(session.observe(snapshot, CAPS, timeout=90))
        try:
            for _ in range(30):
                if self.path.exists():
                    break
                await asyncio.sleep(.01)
            self.assertTrue(self.path.exists())
            self.assertFalse(pending.done())
            self.assertEqual(json.loads(self.path.read_text())['conversation_id'], 'A')
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    async def test_first_trusted_receipt_persists_original_chat_before_observe_returns(self):
        snapshot = {'id': 'video_' + '1'*32, 'sent': True}
        session = self.session(messages=[payload('A', {'message_id': 'a', 'role': 'assistant', 'content': '视频生成中'})])
        result = await session.observe(snapshot, CAPS, timeout=0)
        self.assertEqual(result['phase'], 'accepted')
        saved = json.loads(self.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['conversation_id'], 'A')
        self.assertEqual(saved['page_url'], 'https://www.doubao.com/chat/A')
        session.page = Page('B')
        result = await session.observe(snapshot, CAPS, timeout=0)
        self.assertEqual(result['phase'], 'unknown')
        self.assertEqual(json.loads(self.path.read_text())['conversation_id'], 'A')

    async def test_recovery_without_original_chat_does_not_bind_homepage_redirect(self):
        snapshot = {'id': 'video_' + '1'*32, 'sent': True}
        session = self.session('B', [payload('B', {'message_id': 'b', 'role': 'assistant',
            'content': '视频生成中', 'video_url': 'https://cdn.example/B.mp4'})], recovery=snapshot)
        result = await session.observe(snapshot, CAPS, timeout=0)
        self.assertEqual(result['phase'], 'unknown')
        self.assertEqual(result['media'], [])
        self.assertIsNone(snapshot.get('conversation_id'))

    async def test_current_video_evidence_is_not_hidden_by_an_older_rejection(self):
        session = self.session(messages=[payload('A',
            {'message_id': 'a', 'role': 'assistant', 'content': '上次生成失败'},
            {'message_id': 'b', 'role': 'assistant', 'video_url': 'https://cdn.example/new.mp4'})])
        result = await session.observe({'conversation_id': 'A'}, CAPS, timeout=0)
        self.assertEqual(result['phase'], 'media_ready')

    async def test_known_submission_page_is_saved_before_external_send(self):
        snapshot = {'id': 'video_' + '1'*32, 'sent': False}
        session = self.session()
        await session.capture_submission_page(snapshot)
        self.assertEqual(json.loads(self.path.read_text())['conversation_id'], 'A')
        self.assertFalse(json.loads(self.path.read_text())['sent'])


class MediaKindTests(unittest.TestCase):
    def test_user_videos_and_assistant_reference_images_are_not_generated_video(self):
        for message in (
            {'role': 'user', 'main_url': 'https://cdn.example/ref.mp4'},
            {'role': 'user', 'main_url': 'https://cdn.example/ref.png'},
            {'role': 'assistant', 'main_url': 'https://cdn.example/ref.png'},
            {'main_url': 'https://cdn.example/unknown.mp4'},
            {'role': 'assistant', 'fallback_api': 'https://cdn.example/opaque-api'},
        ):
            with self.subTest(message=message):
                result = evidence_from_payload(payload('A', {'message_id': 'm', **message}), 'A', set(), set())
                self.assertEqual(result['media'], [])

    def test_assistant_video_block_allows_fallback_and_direct_mp4(self):
        result = evidence_from_payload(payload('A', {'message_id': 'm', 'role': 'assistant',
            'content': {'video': {'fallback_api': 'https://cdn.example/video-api',
                                  'main_url': 'https://cdn.example/opaque-signed'}}}), 'A', set(), set())
        self.assertEqual(set(result['media']), {'https://cdn.example/video-api', 'https://cdn.example/opaque-signed'})
        direct = evidence_from_payload(payload('A', {'message_id': 'm', 'role': 'assistant',
            'main_url': 'https://cdn.example/new.mp4'}), 'A', set(), set())
        self.assertEqual(direct['media'], ['https://cdn.example/new.mp4'])


if __name__ == '__main__': unittest.main()
