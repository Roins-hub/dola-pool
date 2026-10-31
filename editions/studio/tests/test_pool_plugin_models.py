"""New plugin-only models must keep their wire identity through submission and recovery."""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from test_five_account_retry import pool_module
from video_capabilities import validate_plugin_expert30_parameters


class PluginPoolModelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.accounts=Path(self.tmp.name)/'accounts'
        (self.accounts/'a').mkdir(parents=True)
        self.pool=pool_module.BrowserPool(self.accounts,':memory:',max_concurrency=2)
        self.pool._proxy_state=lambda name:{'proxy_server':'http://line-a:80','proxy_blocked':False}
        self.pool.accounts

    async def asyncTearDown(self):
        self.pool._conn.close()
        self.tmp.cleanup()

    async def exercise(self,model):
        params=validate_plugin_expert30_parameters(model,30,'21:9','480p')
        options={'resolution':'480p','protocol':'expert','two_part':False}
        async def plugin(account,prompt,ratio,duration,**kw):
            kw['on_prompt_sent'](account)
            kw['on_conversation_id'](account,'123456789',time.time()+60)
            kw['on_browser_closed']()
            return {'model':kw['model'],'ratio':ratio,'duration':duration,
                    'options':kw['generation_options'],'clips':[{'clip_id':'native-original'}]}
        async def ordinary(*args,**kw):
            raise AssertionError('Plugin-only models must never reach the ordinary worker')
        with patch.dict(sys.modules,{'plugin_expert30':SimpleNamespace(generate_video=plugin)}), \
             patch.object(pool_module,'generate_video',ordinary):
            result=await self.pool.generate_video('keep user text',ratio=params['ratio'],duration=30,
                model=params['model'],duration_profile='plugin-expert30',generation_options=options,
                task_id='plugin-model-task')
        self.assertEqual(result['model'],model)
        self.assertEqual(result['options'],options)
        self.assertEqual((result['ratio'],result['duration']),('21:9',30))
        self.assertEqual(self.pool.quota_ledger.snapshot('a','expert_fast')['remaining'],4,
                         'Standard/Mini must not take a Fast-specific weekly unit')

    async def test_standard_uses_plugin_worker_with_standard_model(self):
        await self.exercise('seedance-2.0-std')

    async def test_mini_uses_plugin_worker_without_becoming_seedance10(self):
        await self.exercise('seedance-2.0-mini')

    async def test_standard_and_mini_recovery_only_query_original_conversation(self):
        async def resume(account,cid,timeout,**kw):
            return {'account':account,'conversation_id':cid,'model':kw['model'],
                    'existing_clips':kw['generation_options']['existing_clips']}
        with patch.dict(sys.modules,{'plugin_expert30':SimpleNamespace(resume_video=resume)}):
            for model in ('seedance-2.0-std','seedance-2.0-mini'):
                with self.subTest(model=model):
                    result=await self.pool.resume_video('a','original-cid',60,model=model,
                        duration_profile='plugin-expert30',task_id='original-task',
                        generation_options={'existing_clips':[{'clip_id':'first'}]})
                    self.assertEqual((result['account'],result['conversation_id'],result['model']),('a','original-cid',model))
                    self.assertEqual(result['existing_clips'],[{'clip_id':'first'}])
        self.assertEqual(self.pool.used_today('a'),1)
        self.assertEqual(self.pool.quota_ledger.snapshot('a','expert_fast')['remaining'],4)


if __name__=='__main__':unittest.main()
