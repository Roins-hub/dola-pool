"""Exercise real pool scheduling and persistence while replacing external video IO."""
import asyncio
import inspect
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_five_account_retry import pool_module


class PoolDualQuotaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.accounts = Path(self.tmp.name)/'accounts'
        (self.accounts/'a').mkdir(parents=True)
        self.pool = pool_module.BrowserPool(self.accounts,str(Path(self.tmp.name)/'pool.db'),max_concurrency=2)
        self.pool._proxy_state = lambda name: {'proxy_server':'http://line-'+name+':80','proxy_blocked':False}
        self.pool.accounts

    async def asyncTearDown(self):
        self.pool._conn.close()
        self.tmp.cleanup()

    def assert_contract(self):
        self.assertIn('duration_profile',inspect.signature(self.pool.generate_video).parameters,
                      'The pool must classify requests using their effective route')

    def quota(self,kind):
        self.assertTrue(hasattr(self.pool,'quota_ledger'),'Missing request quota ledger')
        return self.pool.quota_ledger.snapshot('a',kind)

    async def test_daily_fast10_rejects_second_request_on_same_account(self):
        self.assert_contract()
        self.pool.set_quota_policy('fast')
        async def worker(account,*args,**kw):
            kw['on_conversation_id'](account,str(time.monotonic_ns()),time.time()+60)
            kw['on_browser_closed']()
            return {'account':account}
        with patch.object(pool_module,'generate_video',worker):
            await self.pool.generate_video('first',duration=10,model='seedance-2.0',task_id='ordinary-first')
            with self.assertRaises(pool_module.AllAccountsLimitedError):
                await self.pool.generate_video('second',duration=10,model='seedance-2.0',task_id='ordinary-second')
        self.assertEqual(self.quota('ordinary_fast10')['used'],1)
        self.assertEqual(self.quota('expert_fast')['remaining'],4)

    async def test_expert_two_clips_cost_one_and_do_not_use_ordinary_limit(self):
        self.assert_contract()
        async def worker(account,*args,**kw):
            kw['on_prompt_sent'](account)
            kw['on_conversation_id'](account,str(time.monotonic_ns()),time.time()+60)
            kw['on_browser_closed']()
            return {'account':account,'clips':[{'clip_id':'first'},{'clip_id':'second'}]}
        with patch.dict(sys.modules,{'plugin_expert30':SimpleNamespace(generate_video=worker)}):
            for i in range(4):
                result=await self.pool.generate_video('same',duration=30,model='seedance-2.0',
                    duration_profile='plugin-expert30',task_id='expert-'+str(i))
                self.assertEqual(len(result['clips']),2)
            with self.assertRaises(pool_module.AllAccountsLimitedError):
                await self.pool.generate_video('fifth',duration=30,model='seedance-2.0',duration_profile='plugin-expert30',task_id='fifth')
        self.assertEqual(self.quota('expert_fast')['used'],4)
        self.assertEqual(self.quota('ordinary_fast10')['remaining'],1)

    async def test_uncertain_plugin_send_is_not_rotated_or_released(self):
        self.assert_contract()
        (self.accounts/'b').mkdir()
        async def worker(account,*args,**kw):
            kw['on_prompt_sent'](account)
            kw['on_browser_closed']()
            raise TimeoutError('Native request sent without receipt')
        with patch.dict(sys.modules,{'plugin_expert30':SimpleNamespace(generate_video=worker)}):
            with self.assertRaises(TimeoutError):
                await self.pool.generate_video('same',duration=30,model='seedance-2.0',duration_profile='plugin-expert30',task_id='unknown')
        self.assertEqual(self.quota('expert_fast')['uncertain'],1)
        self.assertEqual(sum(a['send_count'] for a in self.pool.list_accounts()),1)

    async def test_confirmed_browser_close_allows_next_submit_while_first_polls(self):
        self.assert_contract()
        first_closed,finish = asyncio.Event(),asyncio.Event()
        async def worker(account,prompt,*args,**kw):
            kw['on_conversation_id'](account,'cid-'+prompt,time.time()+60)
            kw['on_browser_closed']()
            if prompt=='first':
                first_closed.set()
                await finish.wait()
            return {'account':account}
        with patch.dict(sys.modules,{'plugin_expert30':SimpleNamespace(generate_video=worker)}):
            first=asyncio.create_task(self.pool.generate_video('first',duration=30,model='seedance-2.0',duration_profile='plugin-expert30',task_id='first'))
            try:
                await asyncio.wait_for(first_closed.wait(),2)
                second=await asyncio.wait_for(self.pool.generate_video('second',duration=30,model='seedance-2.0',duration_profile='plugin-expert30',task_id='second'),2)
                self.assertEqual(second['account'],'a')
                self.assertFalse(first.done())
            finally:
                finish.set()
                await first
        self.assertTrue(all(not item.locked() for item in self.pool._locks.values()))
        self.assertTrue(all(item._value==1 for item in self.pool._proxy_slots.values()))

    async def test_pre_send_failure_leaves_no_quota_reservation(self):
        self.assert_contract()
        async def worker(account,*args,**kw):
            kw['on_browser_closed']()
            raise RuntimeError('Draft was empty; no request')
        with patch.dict(sys.modules,{'plugin_expert30':SimpleNamespace(generate_video=worker)}):
            with self.assertRaises(RuntimeError):
                await self.pool.generate_video('same',duration=30,model='seedance-2.0',duration_profile='plugin-expert30',task_id='before-send')
        self.assertEqual(self.quota('expert_fast')['remaining'],4)

    async def test_account_response_includes_both_independent_quota_pools(self):
        row=self.pool.list_accounts()[0]
        self.assertIn('quota_pools',row)
        self.assertEqual(row['quota_pools']['ordinary_fast10']['limit'],1)
        self.assertEqual(row['quota_pools']['expert_fast']['limit'],4)

    async def test_original_expert15_recovery_does_not_charge_legacy_daily_pool(self):
        async def resume(account,cid,*args,**kw): return {'account':account,'conversation_id':cid}
        with patch('dola_expert.resume_video',resume):
            await self.pool.resume_video('a','pro-cid',30,model='seedance-2.0',entry_mode='pro-fast15')
            await self.pool.resume_video('a','pro-cid',30,model='seedance-2.0',entry_mode='pro-fast15')
        self.assertEqual(self.pool.used_today('a'),0)
        self.assertEqual(self.quota('expert_fast')['used'],1)

    async def test_existing_daily_conversation_recovery_keeps_original_quota_identity(self):
        self.pool.quota_ledger.confirm('a','ordinary_fast10','original','cid')
        async def resume(account,cid,*args,**kw): return {'account':account}
        with patch.object(pool_module,'resume_video',resume):
            await self.pool.resume_video('a','cid',30,model='seedance-2.0')
        self.assertEqual(self.quota('ordinary_fast10')['used'],1)
        self.assertEqual(self.pool.used_today('a'),0)

    async def test_request_availability_checks_route_instead_of_legacy_daily_counts(self):
        self.assertIn('duration_profile',inspect.signature(self.pool.available_for_model).parameters)
        self.pool._claim('a');self.pool._claim('a')
        self.assertTrue(self.pool.available_for_model('seedance-2.0',30,'plugin-expert30'))
        self.pool.quota_ledger.block('a','expert_fast',time.time()+86400,'expert response')
        self.assertFalse(self.pool.available_for_model('seedance-2.0',30,'plugin-expert30'))
        self.assertTrue(self.pool.available_for_model('seedance-2.0',10))

    async def test_low_engine_credit_does_not_remove_free_expert_allowance(self):
        self.assertIn('duration_profile',inspect.signature(self.pool.available_for_model).parameters)
        self.pool._set_credit_balance('a',0,'ordinary video balance')
        self.assertFalse(self.pool.available_for_model('seedance-2.0',10))
        self.assertTrue(self.pool.available_for_model('seedance-2.0',30,'plugin-expert30'))

    async def test_recovery_weekly_rejection_preserves_expert_block_independent_of_daily(self):
        from dola_expert import ExpertWeeklyLimitError
        async def resume(*args,**kw):
            raise ExpertWeeklyLimitError('Du hast das kostenlose 7-Tage-Limit erreicht. Komm am 1. November 2026, 16:33 (GMT+08:00) wieder oder chatte im Fast-Modus.')
        with patch('dola_expert.resume_video',resume):
            with self.assertRaises(ExpertWeeklyLimitError):
                await self.pool.resume_video('a','cid',30,model='seedance-2.0',entry_mode='pro-fast15')
        self.assertTrue(self.quota('expert_fast')['blocked'])
        self.assertFalse(self.quota('ordinary_fast10')['blocked'])
        self.assertEqual(self.quota('expert_fast')['used'],0)

    async def test_same_sent_task_cannot_be_started_again_on_another_account(self):
        from video_evidence import SubmissionUnconfirmedError
        (self.accounts/'b').mkdir()
        self.pool.quota_ledger.mark_sent('a','expert_fast','same-intent')
        self.pool.set_scheduling('a',False)
        async def worker(account,*args,**kw):
            kw['on_prompt_sent'](account)
            return {'account':account}
        with patch.dict(sys.modules,{'plugin_expert30':SimpleNamespace(generate_video=worker)}):
            with self.assertRaises(SubmissionUnconfirmedError):
                await self.pool.generate_video('same',duration=30,model='seedance-2.0',duration_profile='plugin-expert30',task_id='same-intent')
        self.assertEqual(sum(row['send_count'] for row in self.pool.list_accounts()),0)
        self.assertEqual(self.quota('expert_fast')['uncertain'],1)

    async def test_startup_unconfirmed_hook_does_not_add_a_second_legacy_charge(self):
        self.pool.quota_ledger.mark_sent('a','expert_fast','same-intent')
        self.pool.reserve_unconfirmed_submission('a','same-intent')
        self.pool.reserve_unconfirmed_submission('a','same-intent')
        self.assertEqual(self.quota('expert_fast')['uncertain'],1)
        self.assertEqual(self.pool.used_today('a'),0)

    async def test_subscription_refusal_blocks_only_expert_route_and_keeps_charge(self):
        from video_outcome import PlatformVideoError
        async def worker(account,*args,**kw):
            kw['on_prompt_sent'](account)
            kw['on_conversation_id'](account,'plus-refusal',time.time()+60)
            kw['on_browser_closed']()
            kw['on_event']('subscription_required',{'reply':'Dola Plus erforderlich'})
            error=PlatformVideoError('subscription_required','需要会员','Dola Plus erforderlich')
            error.task_phase='subscription_required'
            raise error
        with patch.dict(sys.modules,{'plugin_expert30':SimpleNamespace(generate_video=worker)}):
            with self.assertRaises(PlatformVideoError):
                await self.pool.generate_video('same',duration=30,model='seedance-2.0',duration_profile='plugin-expert30',task_id='plus')
        self.assertTrue(self.quota('expert_fast')['blocked'])
        self.assertEqual(self.quota('expert_fast')['used'],1)
        self.assertFalse(self.quota('ordinary_fast10')['blocked'])


if __name__=='__main__':unittest.main()
