"""Read-back and permit regressions; no external requests or video generation."""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, patch

import test_video_pipeline as fixtures
pool_module = fixtures.pool_module
from video_evidence import SubmissionUnconfirmedError


class UnconfirmedRetryTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.PipelineTests.asyncSetUp
    asyncTearDown = fixtures.PipelineTests.asyncTearDown

    def session(self, poll=None, error=None):
        session = AsyncMock()
        session.__aenter__.return_value = session
        session.poll.return_value = poll or {'assistant_texts': ['可以按15秒生成'], 'videos': []}
        session.poll.side_effect = error
        return session

    async def test_uncertain_send_reservation_is_idempotent_across_recovery(self):
        self.pool.reserve_unconfirmed_submission('a', 'task-id')
        self.pool.reserve_unconfirmed_submission('a', 'task-id')
        self.assertEqual(self.pool.used_today('a'), 1)
        self.pool.reserve_unconfirmed_submission('a', 'another-task')
        self.assertEqual(self.pool.used_today('a'), 2)

    async def test_unconfirmed_checks_each_original_then_tries_five_without_refunding(self):
        calls, events = [], []
        session = self.session()
        async def worker(account, *args, **cb):
            calls.append(account)
            cb['on_prompt_sent'](account)
            cb['on_conversation_id'](account, str(100 + len(calls)), time.time()+7200)
            raise SubmissionUnconfirmedError('没有生成回执')
        with patch.object(pool_module, 'HttpVideoSession', return_value=session, create=True) as opened, \
             patch.object(pool_module, 'generate_video', worker):
            with self.assertRaisesRegex(RuntimeError, '5.*账号'):
                await self.pool.generate_video('原文', model='seedance-2.5',
                    on_event=lambda p,d: events.append((p,d)))
        self.assertEqual(len(set(calls)), 5)
        self.assertEqual(opened.call_count, 5)
        self.assertEqual(session.poll.await_count, 5)
        self.assertEqual([self.pool.used_today(a) for a in calls], [1]*5)
        retries = [d for p,d in events if p == 'account_retry']
        self.assertEqual(len(retries), 5)
        self.assertTrue(all('未确认' in d['reason'] for d in retries))
        self.assertEqual(self.pool.semaphore._value, 2)

    async def test_late_receipt_resumes_original_without_resending(self):
        calls, events = [], []
        session = self.session({'assistant_texts': ['Seedance 2.5，消耗2个额度。'], 'videos': []})
        async def worker(account, *args, **cb):
            calls.append(account)
            cb['on_prompt_sent'](account)
            cb['on_conversation_id'](account, '111', time.time()+7200)
            raise SubmissionUnconfirmedError('未见回执')
        async def resume(account, conv, timeout, **kw):
            self.assertEqual((account,conv), ('a','111'))
            self.assertTrue(kw['accepted'])
            self.assertTrue(self.pool._locks['a'].locked())
            self.assertEqual(self.pool.semaphore._value, 2)
            self.assertEqual(self.pool._proxy_slots['http://line-a:80']._value, 0,
                             'The original ISP stays reserved through late-receipt polling')
            return {'account':account, 'conversation_id':conv}
        with patch.object(pool_module, 'HttpVideoSession', return_value=session, create=True), \
             patch.object(pool_module, 'generate_video', worker), patch.object(pool_module, 'resume_video', resume):
            result = await self.pool.generate_video('原文', model='seedance-2.5',
                on_event=lambda p,d: events.append((p,d)))
        self.assertEqual(calls, ['a'])
        self.assertEqual(result['conversation_id'], '111')
        self.assertIn('accepted', [p for p,d in events])
        self.assertNotIn('account_retry', [p for p,d in events])

    async def test_readback_network_failure_still_rotates_with_uncertainty_recorded(self):
        calls, events = [], []
        session = self.session(error=TimeoutError('fixture'))
        async def worker(account, *args, **cb):
            calls.append(account)
            if len(calls) == 1:
                cb['on_prompt_sent'](account)
                cb['on_conversation_id'](account, '111', time.time()+60)
                raise SubmissionUnconfirmedError('未见回执')
            return {'account':account}
        with patch.object(pool_module, 'HttpVideoSession', return_value=session, create=True), \
             patch.object(pool_module, 'generate_video', worker):
            await self.pool.generate_video('原文', on_event=lambda p,d: events.append((p,d)))
        self.assertEqual(calls, ['a','b'])
        self.assertEqual(self.pool.used_today('a'), 1)
        self.assertIn('核对失败', next(d['reason'] for p,d in events if p=='account_retry'))

    async def test_german_fast_receipt_prevents_a_second_account_send(self):
        from test_fast_german_receipt import RECEIPT
        calls, events = [], []
        session = self.session({'assistant_messages': [['正在为您生成视频。', RECEIPT]], 'videos': []})
        async def worker(account, *args, **cb):
            calls.append(account)
            cb['on_prompt_sent'](account)
            cb['on_conversation_id'](account, '111', time.time()+7200)
            raise SubmissionUnconfirmedError('模拟回执到达稍晚')
        resume = AsyncMock(return_value={'account': 'a', 'conversation_id': '111'})
        with patch.object(pool_module, 'HttpVideoSession', return_value=session, create=True), \
             patch.object(pool_module, 'generate_video', worker), patch.object(pool_module, 'resume_video', resume):
            result = await self.pool.generate_video('原文', model='seedance-2.0',
                on_event=lambda p,d: events.append((p,d)))
        self.assertEqual(calls, ['a'])
        self.assertEqual(result['conversation_id'], '111')
        self.assertTrue(resume.await_args.kwargs['accepted'])
        self.assertEqual(self.pool.used_today('a'), 1)
        self.assertIn('accepted', [p for p,d in events])
        self.assertNotIn('account_retry', [p for p,d in events])

    async def test_worker_releasing_permit_on_error_reacquires_before_next_attempt(self):
        self.pool.semaphore = asyncio.Semaphore(1)
        calls = []
        async def worker(account, *args, **cb):
            calls.append(account)
            self.assertEqual(self.pool.semaphore._value, 0)
            cb['on_browser_closed']()
            if len(calls) == 1:
                raise pool_module.ExplicitNotAcceptedError('请确认生成15秒')
            return {'account':account}
        with patch.object(pool_module, 'generate_video', worker):
            await self.pool.generate_video('原文')
        self.assertEqual(calls, ['a','b'])
        self.assertEqual(self.pool.semaphore._value, 1)


if __name__ == '__main__':
    unittest.main()
