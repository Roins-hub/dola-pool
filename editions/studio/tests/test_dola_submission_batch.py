import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from store import TaskStore


class BatchExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = TaskStore(':memory:')
        self.addCleanup(self.store._conn.close)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.ns = {'store': self.store, 'config': SimpleNamespace(REFERENCE_UPLOAD_DIR=self.temp.name,
            REFERENCE_IMAGE_MAX_COUNT=30, MAX_PENDING_TASKS=100),
            'pool': SimpleNamespace(accounts=['fixture']),
            '_batch_receipts': {}, '_batch_runners': {}, '_active_task_ids': set(),
            '_submission_identity': lambda req, scope: {'idempotency_scope': scope,
                'idempotency_key': req.idempotency_key, 'request_fingerprint': 'one-intent'},
            'admin_owner': lambda value: 'admin', '_resolve_ratio': lambda size, ratio: ratio,
            '_member_task': lambda row: {k: row[k] for k in ('id','status','batch_id','batch_index','clips')},
            'TaskResponse': lambda **row: SimpleNamespace(model_dump=lambda: row)}
        self.client = {'api_key_hash': 'member-key', 'api_key_name': 'member',
            'allowed_durations': [10,30], 'allowed_platforms':['dola'], 'daily_limit': 100,
            'concurrency_limit': 0, 'owner_user_id': 1}

    def request(self, count=3):
        return SimpleNamespace(platform='dola', model='seedance-2.0', duration=30, ratio='16:9', size=None,
            duration_profile='plugin-expert30', resolution='720p', prompt='exact user prompt',
            batch_count=count, reference_images=[], reference_uploads=[], reference_media_uploads=[],
            idempotency_key='one-intent-0001', expert_account=None)

    async def test_member_cannot_hold_a_submission_browser_open(self):
        from dola_submission import create_tasks
        from fastapi import HTTPException
        req = self.request(1)
        req.keep_browser_open = True
        with self.assertRaises(HTTPException) as caught:
            await create_tasks(self.ns, req, self.client)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self.store.recent_tasks(platform='all'), [])

    async def test_authorized_single_test_persists_window_hold(self):
        from dola_submission import create_tasks
        req = self.request(1)
        req.keep_browser_open = True
        self.ns['config'].API_KEYS = ['fixture-system-key']
        client = {**self.client, 'api_key_hash': self.store.hash_api_key('fixture-system-key')}
        started = asyncio.Event()
        async def run(task_id, *args):
            started.set()
        self.ns['_run_task'] = run
        await create_tasks(self.ns, req, client)
        await asyncio.wait_for(started.wait(), 1)
        row = self.store.recent_tasks(platform='all')[0]
        self.assertTrue(json.loads(row['generation_options'])['keep_browser_open'])

    async def test_short_expert_request_preserves_duration_and_options(self):
        from dola_submission import create_tasks
        req = self.request(1)
        req.duration, req.two_part = 15, False
        client = {**self.client, 'allowed_durations':[15]}
        started = asyncio.Event()
        async def run(*args):
            started.set()
        self.ns['_run_task'] = run
        await create_tasks(self.ns, req, client)
        await asyncio.wait_for(started.wait(), 1)
        row = self.store.recent_tasks(platform='all')[0]
        self.assertEqual(row['duration'], 15)
        self.assertEqual(json.loads(row['generation_options']).get('requested_duration'), 15)

    async def test_expert_media_upload_keeps_owned_private_file_for_submission(self):
        from dola_submission import create_tasks
        from reference_uploads import save_media
        from test_video_metadata import mp4
        upload = save_media(mp4(5), 'clip.mp4', self.temp.name, 'user:1', 100000)
        req = self.request(1)
        req.reference_media_uploads = [upload['id']]
        started = asyncio.Event()
        async def run(*args):
            started.set()
        self.ns['_run_task'] = run
        await create_tasks(self.ns, req, self.client)
        await asyncio.wait_for(started.wait(), 1)
        row = self.store.recent_tasks(platform='all')[0]
        references = json.loads(row['reference_images'])
        self.assertEqual(references, ['upload:'+row['id']+'/'+upload['id']])
        self.assertTrue((Path(self.temp.name)/'tasks'/row['id']/upload['id']).is_file())

    async def test_explicit_two_part_option_is_persisted(self):
        from dola_submission import create_tasks
        req = self.request(1)
        req.two_part = True
        started = asyncio.Event()
        async def run(*args):
            started.set()
        self.ns['_run_task'] = run
        await create_tasks(self.ns, req, self.client)
        await asyncio.wait_for(started.wait(), 1)
        row = self.store.recent_tasks(platform='all')[0]
        self.assertTrue(json.loads(row['generation_options'])['two_part'])

    async def test_next_child_submits_after_receipt_without_waiting_for_first_result(self):
        try:
            import dola_submission as module
        except ImportError:
            self.fail('Project batches must schedule independent persisted child submissions')
        started = []
        first_results = asyncio.Event()
        second_started = asyncio.Event()
        async def run(task_id, *args):
            started.append(task_id)
            self.store.update(task_id, status='processing', account='fixture', conversation_id=str(123456789+len(started)))
            self.ns['_batch_receipts'][task_id].set()
            if len(started) == 2:
                second_started.set()
            await first_results.wait()
        self.ns['_run_task'] = run
        result = await module.create_tasks(self.ns, self.request(2), self.client)
        data = json.loads(result.body)
        await asyncio.wait_for(second_started.wait(), 1)
        self.assertEqual(len(started), 2)
        self.assertEqual(len(data['tasks']), 2)
        first_results.set()
        await asyncio.gather(*self.ns['_batch_runners'].values())

    async def test_batch_replay_does_not_create_more_children_or_execute_again(self):
        try:
            import dola_submission as module
        except ImportError:
            self.fail('Batch idempotency must replay the entire original batch')
        async def run(task_id, *args):
            self.store.update(task_id,status='failed')
        self.ns['_run_task'] = run
        first = json.loads((await module.create_tasks(self.ns, self.request(), self.client)).body)
        await asyncio.gather(*self.ns['_batch_runners'].values())
        second = json.loads((await module.create_tasks(self.ns, self.request(), self.client)).body)
        self.assertEqual(first['batch_id'], second['batch_id'])
        self.assertEqual([t['id'] for t in first['tasks']], [t['id'] for t in second['tasks']])
        self.assertEqual(len(self.store.recent_tasks(platform='all')), 3)
        self.assertEqual(self.store.get_batch(first['batch_id'])['status'], 'paused')

    async def test_summary_does_not_mix_day_and_week_balances(self):
        import dola_submission as module
        if not hasattr(module, 'quota_summary'):
            self.fail('The dashboard must expose independent Fast day/week balances')
        summary = module.quota_summary([
            {'quota_pools': {'ordinary_fast10': {'limit':1,'used':1,'reserved':0,'remaining':0},
                             'expert_fast': {'limit':4,'used':1,'reserved':0,'remaining':3}}},
            {'quota_pools': {'ordinary_fast10': {'limit':1,'used':0,'reserved':0,'remaining':1},
                             'expert_fast': {'limit':4,'used':2,'reserved':1,'remaining':1}}}])
        self.assertEqual(summary['ordinary_fast10']['limit'],2)
        self.assertEqual(summary['ordinary_fast10']['remaining'],1)
        self.assertEqual(summary['expert_fast']['remaining'],4)
        self.assertEqual(summary['expert_fast']['reserved'],1)

    async def test_empty_summary_still_explains_each_pool_reset_rule(self):
        from dola_submission import quota_summary
        summary = quota_summary([])
        daily, expert = summary['ordinary_fast10'], summary['expert_fast']
        self.assertEqual(daily.get('period'), 'day')
        self.assertEqual(daily.get('timezone'), 'Asia/Shanghai')
        self.assertEqual(daily.get('reset_hour'), 1)
        self.assertEqual(expert.get('period'), 'rolling_7_days')
        self.assertEqual(expert.get('window_days'), 7)
        self.assertEqual((daily['limit'], expert['limit']), (0, 0))

    async def test_paused_batch_can_continue_while_an_earlier_child_is_still_collecting(self):
        import dola_submission as module
        seen=[]
        first_finished=asyncio.Event()
        third_started=asyncio.Event()
        async def run(task_id,*args):
            seen.append(task_id)
            if len(seen)==2:
                self.store.update(task_id,status='failed')
                return
            self.store.update(task_id,status='processing',conversation_id=str(123456789+len(seen)))
            self.ns['_batch_receipts'][task_id].set()
            if len(seen)==1:
                await first_finished.wait()
            else:
                third_started.set()
        self.ns['_run_task']=run
        created=json.loads((await module.create_tasks(self.ns,self.request(),self.client)).body)
        await asyncio.sleep(.7)
        self.assertEqual(self.store.get_batch(created['batch_id'])['status'],'paused')
        self.store.set_batch_state(created['batch_id'],'queued')
        module.start_batch(self.ns,created['batch_id'],self.client)
        try:
            await asyncio.wait_for(third_started.wait(),1)
        except TimeoutError:
            self.fail('Continuing unsent children must not wait for an earlier video to finish')
        finally:
            first_finished.set()
            await asyncio.gather(*self.ns['_batch_runners'].values())


if __name__ == '__main__':
    unittest.main()
