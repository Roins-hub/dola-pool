"""Fast10 can hand off after one saved conversation while HTTP leases its original lane."""
import asyncio
import inspect
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from test_five_account_retry import pool_module
from test_video_metadata import mp4


class Fast10PoolHandoffTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.accounts=Path(self.tmp.name)/'accounts'
        (self.accounts/'a').mkdir(parents=True)
        self.pool=pool_module.BrowserPool(self.accounts,':memory:',max_concurrency=2)
        self.pool._proxy_state=lambda name:{'proxy_server':'http://original-line:80','proxy_blocked':False}
        self.pool.accounts

    async def asyncTearDown(self):
        self.pool._conn.close()
        self.tmp.cleanup()

    async def test_second_account_on_same_lane_submits_before_first_video_finishes(self):
        (self.accounts/'b').mkdir()
        first_background,finish=asyncio.Event(),asyncio.Event()
        async def worker(account,prompt,*args,**kw):
            self.assertIn('background_lane',kw,'Fast10 must carry the original lane to its HTTP observer')
            kw['on_conversation_id'](account,'cid-'+prompt,time.time()+60)
            kw['on_browser_closed']()
            # Actual HTTP requests run under this lease, between browser submissions.
            async with kw['background_lane']:
                await asyncio.sleep(0)
            if prompt=='first':
                first_background.set()
                await finish.wait()
            return {'account':account}
        with patch.object(pool_module,'generate_video',worker):
            first=asyncio.create_task(self.pool.generate_video('first',duration=10,model='seedance-2.0',task_id='first'))
            try:
                await asyncio.wait_for(first_background.wait(),2)
                second=await asyncio.wait_for(self.pool.generate_video('second',duration=10,model='seedance-2.0',task_id='second'),2)
                self.assertEqual(second['account'],'b')
                self.assertFalse(first.done())
            finally:
                finish.set()
                first_result=await first
        self.assertEqual(first_result['account'],'a')
        for account in ('a','b'):
            self.assertEqual(self.pool.quota_ledger.snapshot(account,'ordinary_fast10')['used'],1)
        self.assertTrue(all(not lock.locked() for lock in self.pool._locks.values()))
        self.assertTrue(all(slot._value==1 for slot in self.pool._proxy_slots.values()))

    async def test_fast10_recovery_does_not_hold_lane_around_http_worker(self):
        self.pool.quota_ledger.confirm('a','ordinary_fast10','original','original-cid')
        async def observe(account,cid,*args,**kw):
            self.assertIn('background_lane',kw)
            async with kw['background_lane']:
                return {'account':account,'conversation_id':cid}
        with patch.object(pool_module,'resume_video',observe):
            result=await asyncio.wait_for(self.pool.resume_video('a','original-cid',10,model='seedance-2.0'),2)
        self.assertEqual(result['conversation_id'],'original-cid')
        self.assertEqual(self.pool.quota_ledger.snapshot('a','ordinary_fast10')['used'],1)


class Fast10ObserverLaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_poll_and_download_lease_lane_separately_without_nesting(self):
        import video_worker_ui as worker
        from shared_scheduler import SharedScheduler
        self.assertIn('scheduler',inspect.signature(worker.resume_video).parameters)
        scheduler=SharedScheduler(1,2)
        lane=scheduler.lane_slot('http://original-line:80')
        phases=[]
        class Session:
            proxy_url='http://original-line:80'
            own_isp=True
            state={'cookies':[],'user_agent':'fixture'}
            def __init__(self,*args):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def poll(self):
                phases.append(('poll',lane.locked()))
                return {'videos':['https://v16-dola.dola.com/result?lr=unwatermarked'],'videoModels':[],'texts':[]}
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'clip.mp4';path.write_bytes(mp4(10))
            def source(model,url):
                phases.append(('between_requests',lane.locked()))
                return {'url':url,'watermark':'verified_download','download_source':'dola_video_model'}
            async def download(url,account,**kw):
                phases.append(('download',lane.locked()))
                return path
            with patch.object(worker,'HttpVideoSession',Session),patch.object(worker,'_download',download),patch.object(worker,'dola_download',source):
                result=await asyncio.wait_for(worker.resume_video('a','original-cid',10,scheduler=scheduler),2)
        self.assertEqual(phases,[('poll',True),('between_requests',False),('download',True)])
        self.assertEqual(result['actual_duration'],10)
        self.assertFalse(lane.locked())


if __name__=='__main__':unittest.main()
