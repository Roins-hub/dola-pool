"""Release only a proven no-charge conversation through real pool bookkeeping."""
import asyncio
import time
import unittest
from unittest.mock import patch

import test_five_account_retry as retry_fixture
import video_outcome
from navigation_errors import BrowserCleanupError

pool_module = retry_fixture.pool_module


class PoolNoChargeTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = retry_fixture.FiveAccountRetryTests.asyncSetUp
    asyncTearDown = retry_fixture.FiveAccountRetryTests.asyncTearDown
    assert_all_slots_released = retry_fixture.FiveAccountRetryTests.assert_all_slots_released

    def no_charge(self):
        error_type = getattr(video_outcome, 'PlatformNoChargeError', None)
        self.assertTrue(callable(error_type), 'Explicit no-charge refusals need a distinct error')
        return error_type('Video generation failed. No credits were used.')

    def reservations(self):
        return [tuple(row) for row in self.pool._conn.execute(
            'SELECT conversation_id,account,day FROM accepted_videos ORDER BY conversation_id')]

    def protected_state(self):
        fields = ('name,scheduling,category,category_override,onboarding_state,onboarding_error,'
                  'login_ok,login_checked_at,login_reason,verification_required,cooldown_until,'
                  'rate_limited_until,limit_reason,quota_blocked_until,quota_reason,'
                  'credit_balance,credit_checked_at')
        return [tuple(row) for row in self.pool._conn.execute(
            'SELECT ' + fields + ' FROM accounts_meta ORDER BY name')]

    async def test_initial_refusal_releases_only_its_claim_even_after_an_accepted_receipt(self):
        self.pool.accounts
        self.pool._claim_conversation('b', 'unrelated')
        self.pool._set_credit_balance('a', 3, 'observed balance')
        before = self.protected_state()
        for accepted, account in ((False, 'a'), (True, 'c')):
            with self.subTest(accepted=accepted):
                # b has current usage; after a's refusal, c is the oldest unused account.
                calls, events = [], []
                error = self.no_charge()
                async def worker(name, *args, **callbacks):
                    calls.append(name)
                    callbacks['on_prompt_sent'](name)
                    callbacks['on_conversation_id'](name, 'rejected-' + name, time.time() + 60)
                    if accepted:
                        callbacks['on_event']('accepted', {'reply': 'Seedance 2.0 will generate the video'})
                    callbacks['on_browser_closed']()
                    raise error
                with patch.object(pool_module, 'generate_video', worker):
                    with self.assertRaises(type(error)) as caught:
                        await self.pool.generate_video('fixture', model='seedance-2.0',
                            on_event=lambda phase, details: events.append(phase))
                self.assertIs(caught.exception, error)
                self.assertEqual(calls, [account], 'A proven refusal is terminal, never an account retry')
                self.assertEqual((error.account, error.conversation_id), (account, 'rejected-' + account))
                self.assertEqual(self.pool.used_today(account), int(account == 'b'))
                self.assertEqual([row[0] for row in self.reservations()], ['unrelated'])
                self.assertNotIn('account_retry', events)
                self.assertEqual(self.protected_state(), before)
                self.assertEqual(self.pool._meta(account)['dispatch_count'], 1)
                self.assert_all_slots_released()

    async def test_resume_releases_the_original_day_once_without_touching_other_usage_or_locks(self):
        self.pool.accounts
        self.pool._conn.executemany('INSERT INTO usage VALUES (?,?,?)',
            [('a', '2001-01-01', 2), ('a', pool_module._today_key(), 1)])
        self.pool._conn.executemany('INSERT INTO accepted_videos VALUES (?,?,?)',
            [('rejected', 'a', '2001-01-01'), ('keep-old', 'a', '2001-01-01')])
        self.pool._conn.execute('UPDATE accounts_meta SET credit_balance=3,cooldown_until=?,'
            "rate_limited_until=?,limit_reason='preserve daily',quota_blocked_until=?,"
            "quota_reason='preserve quota',login_ok=0 WHERE name='a'",
            (time.time()+600, time.time()+600, time.time()+600))
        self.pool._conn.commit()
        before = self.protected_state()
        rotation = self.pool._meta('a')['dispatch_count']
        error = self.no_charge()
        async def resume(*args, **kwargs):
            raise error
        with patch.object(pool_module, 'resume_video', resume):
            with self.assertRaises(type(error)):
                await self.pool.resume_video('a', 'rejected', 10, accepted=True)
        self.assertEqual((error.account, error.conversation_id), ('a', 'rejected'))
        self.assertEqual(self.pool._conn.execute(
            'SELECT used FROM usage WHERE account=? AND day=?', ('a', '2001-01-01')).fetchone()[0], 1)
        self.assertEqual(self.pool.used_today('a'), 1)
        self.assertFalse(self.pool.release_no_charge_conversation('a', 'rejected', error))
        self.assertEqual(self.protected_state(), before)
        self.assertEqual(self.pool._meta('a')['dispatch_count'], rotation)
        self.assertEqual([row[0] for row in self.reservations()], ['keep-old'])
        self.assert_all_slots_released()

    async def test_refund_requires_a_matching_account_and_real_no_charge_type(self):
        self.pool.accounts
        self.pool._claim_conversation('b', 'belongs-to-b')
        error = self.no_charge()
        self.assertFalse(self.pool.release_no_charge_conversation('a', 'belongs-to-b', error))
        self.assertFalse(self.pool.release_no_charge_conversation('b', '', error))
        generic = video_outcome.PlatformVideoError('platform_rejected', 'rejected', 'No credits were used.')
        self.assertFalse(self.pool.release_no_charge_conversation('b', 'belongs-to-b', generic))
        self.assertEqual(self.pool.used_today('b'), 1)
        self.assertEqual([row[0] for row in self.reservations()], ['belongs-to-b'])

    async def test_generic_platform_failure_keeps_its_reservation_and_never_rotates(self):
        calls = []
        error = video_outcome.PlatformVideoError('platform_rejected', 'rejected', 'Cannot generate video')
        async def worker(account, *args, **callbacks):
            calls.append(account)
            callbacks['on_conversation_id'](account, 'generic-failure', time.time()+60)
            callbacks['on_event']('accepted', {'reply': 'generation started'})
            callbacks['on_browser_closed']()
            raise error
        with patch.object(pool_module, 'generate_video', worker):
            with self.assertRaises(type(error)):
                await self.pool.generate_video('fixture')
        self.assertEqual(calls, ['a'])
        self.assertEqual(self.pool.used_today('a'), 1)
        self.assertEqual([row[0] for row in self.reservations()], ['generic-failure'])
        self.assert_all_slots_released()

    async def test_last_original_receipt_refusal_releases_without_trying_the_next_account(self):
        calls, events = [], []
        error = self.no_charge()
        async def worker(account, *args, **callbacks):
            calls.append(account)
            callbacks['on_conversation_id'](account, 'original-refused', time.time()+60)
            callbacks['on_browser_closed']()
            raise TimeoutError('receipt delayed')
        async def read(*args):
            return {'state': 'failed', 'reply': error.reply, 'error': error}
        with patch.object(pool_module, 'generate_video', worker), \
                patch.object(self.pool, '_read_attempt_receipt', read):
            with self.assertRaises(type(error)):
                await self.pool.generate_video('fixture', on_event=lambda phase, details: events.append(phase))
        self.assertEqual(calls, ['a'])
        self.assertEqual((error.account, error.conversation_id), ('a', 'original-refused'))
        self.assertEqual(self.pool.used_today('a'), 0)
        self.assertEqual(self.reservations(), [])
        self.assertNotIn('account_retry', events)
        self.assert_all_slots_released()

    async def test_no_charge_error_thrown_by_last_original_read_is_not_swallowed_into_retry(self):
        calls = []
        error = self.no_charge()
        async def worker(account, *args, **callbacks):
            calls.append(account)
            callbacks['on_conversation_id'](account, 'read-refusal', time.time()+60)
            callbacks['on_browser_closed']()
            raise TimeoutError('receipt delayed')
        async def read(*args):
            raise error
        with patch.object(pool_module, 'generate_video', worker), \
                patch.object(self.pool, '_read_attempt_receipt', read):
            with self.assertRaises(type(error)):
                await self.pool.generate_video('fixture')
        self.assertEqual(calls, ['a'])
        self.assertEqual(self.pool.used_today('a'), 0)
        self.assertEqual(self.reservations(), [])
        self.assert_all_slots_released()

    async def test_inner_resume_after_last_receipt_releases_a_later_no_charge_failure(self):
        calls, resumes = [], []
        error = self.no_charge()
        async def worker(account, *args, **callbacks):
            calls.append(account)
            callbacks['on_conversation_id'](account, 'late-refusal', time.time()+60)
            callbacks['on_browser_closed']()
            raise TimeoutError('receipt delayed')
        async def read(*args):
            return {'state': 'accepted', 'reply': 'Seedance 2.0 generation started'}
        async def resume(account, conversation_id, *args, **kwargs):
            resumes.append((account, conversation_id))
            raise error
        with patch.object(pool_module, 'generate_video', worker), \
                patch.object(self.pool, '_read_attempt_receipt', read), \
                patch.object(pool_module, 'resume_video', resume):
            with self.assertRaises(type(error)):
                await self.pool.generate_video('fixture')
        self.assertEqual(calls, ['a'])
        self.assertEqual(resumes, [('a', 'late-refusal')])
        self.assertEqual((error.account, error.conversation_id), ('a', 'late-refusal'))
        self.assertEqual(self.pool.used_today('a'), 0)
        self.assertEqual(self.reservations(), [])
        self.assert_all_slots_released()

    async def test_terminal_event_refunds_before_forwarding_and_cleanup_failure_keeps_isp_quarantined(self):
        self.pool.accounts
        calls, forwarded = [], []
        error = self.no_charge()
        async def worker(account, *args, **callbacks):
            calls.append(account)
            callbacks['on_conversation_id'](account, 'refused-before-cleanup', time.time()+60)
            callbacks['on_event']('accepted', {'reply': 'generation started'})
            callbacks['on_event']('platform_rejected_no_charge', {'reply': error.reply, 'reason': str(error)})
            self.assertEqual(self.pool.used_today(account), 0, 'Refund must precede slow browser cleanup')
            raise BrowserCleanupError('browser process could not be closed')
        def event(phase, details):
            if phase == 'platform_rejected_no_charge':
                forwarded.append((phase, self.pool.used_today('a')))
        with patch.object(pool_module, 'generate_video', worker):
            with self.assertRaises(type(error)) as caught:
                await self.pool.generate_video('fixture', on_event=event)
        self.assertEqual(calls, ['a'])
        self.assertEqual(forwarded, [('platform_rejected_no_charge', 0)])
        self.assertEqual((caught.exception.account, caught.exception.conversation_id),
                         ('a', 'refused-before-cleanup'))
        self.assertIn('a', self.pool._recovering)
        self.assertIn('http://line-a:80', self.pool._unsafe_proxies)
        self.assertFalse(self.pool.release_no_charge_conversation('a', 'refused-before-cleanup', caught.exception))
        self.assertIn('a', self.pool._recovering, 'A repeated refund must never clear failed-cleanup quarantine')
        self.assertIn('http://line-a:80', self.pool._unsafe_proxies)
        self.assert_all_slots_released()

    async def test_resume_terminal_event_refunds_before_forwarding(self):
        self.pool.accounts
        self.pool._claim_conversation('a', 'resume-event')
        error = self.no_charge()
        forwarded = []
        async def resume(account, conversation_id, *args, **callbacks):
            callbacks['on_event']('platform_rejected_no_charge', {'reply': error.reply, 'reason': str(error)})
            self.assertEqual(self.pool.used_today(account), 0)
            raise error
        with patch.object(pool_module, 'resume_video', resume):
            with self.assertRaises(type(error)):
                await self.pool.resume_video('a', 'resume-event', 10, accepted=True,
                    on_event=lambda phase, details: forwarded.append((phase, self.pool.used_today('a'))))
        self.assertEqual(forwarded, [('platform_rejected_no_charge', 0)])
        self.assertEqual(self.pool.used_today('a'), 0)
        self.assert_all_slots_released()

    async def test_cancellation_after_refusal_keeps_cancellation_and_cleanup_quarantine(self):
        error = self.no_charge()
        async def worker(account, *args, **callbacks):
            callbacks['on_conversation_id'](account, 'cancel-refusal', time.time()+60)
            callbacks['on_event']('platform_rejected_no_charge', {'reply': error.reply})
            cancelled = asyncio.CancelledError()
            cancelled.browser_cleanup_failed = True
            raise cancelled
        with patch.object(pool_module, 'generate_video', worker):
            with self.assertRaises(asyncio.CancelledError):
                await self.pool.generate_video('fixture')
        self.assertEqual(self.pool.used_today('a'), 0)
        self.assertIn('a', self.pool._recovering)
        self.assertIn('http://line-a:80', self.pool._unsafe_proxies)
        self.assert_all_slots_released()


if __name__ == '__main__':
    unittest.main()
