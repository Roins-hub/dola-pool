"""Seedance 1.0 may try a nonzero balance without weakening other pool gates."""
import ast
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import uuid
from unittest.mock import patch

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from test_five_account_retry import pool_module
from store import TaskStore, TaskQuotaExceeded, PendingTaskLimitExceeded


class Seedance1PoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for name in ('a', 'b', 'c'):
            (self.root / name).mkdir()
        self.pool = pool_module.BrowserPool(self.root, ':memory:', max_concurrency=1)
        self.proxy = patch.object(self.pool, '_proxy_state', side_effect=lambda name: {
            'proxy_server': 'http://line-a:80' if name in ('a', 'c') else 'http://line-b:80',
            'proxy_blocked': False})
        self.proxy.start()
        self.pool.accounts
        self.pool.set_scheduling('b', False)
        self.pool.set_scheduling('c', False)

    async def asyncTearDown(self):
        self.proxy.stop()
        self.pool._conn.close()
        self.tmp.cleanup()

    async def generate(self, model='seedance-1.0'):
        async def worker(account, prompt, ratio, duration, **kwargs):
            kwargs['on_browser_closed']()
            return {'account': account, 'model': kwargs['model'],
                    'proxy': self.pool._submission_proxy({'name': account,
                        **self.pool._proxy_state(account)})}
        with patch.object(pool_module, 'generate_video', worker):
            return await self.pool.generate_video('fixture', duration=5, model=model)

    async def test_one_point_can_reach_seedance1_worker_without_becoming_globally_available(self):
        self.pool._set_credit_balance('a', 1, '今天还剩1点')
        try:
            result = await self.generate()
        except RuntimeError as exc:
            self.fail(f'A nonzero balance should allow the 1.0 experiment: {exc}')
        self.assertEqual(result, {'account': 'a', 'model': 'seedance-1.0',
                                  'proxy': 'http://line-a:80'})
        self.assertFalse(self.pool.available, 'existing Fast/2.5 eligibility stays at two points')
        self.assertEqual(self.pool.semaphore._value, 1)

    async def test_wire_alias_uses_same_one_point_eligibility(self):
        self.pool._set_credit_balance('a', 1)
        try:
            result = await self.generate('ic_mini')
        except RuntimeError as exc:
            self.fail(f'The wire model alias must keep 1.0 eligibility: {exc}')
        self.assertEqual(result['account'], 'a')

    async def test_confirmed_other_model_receipt_stops_account_rotation(self):
        from video_evidence import SubmissionUnconfirmedError
        from video_outcome import conversation_outcome, PlatformVideoError
        import time
        self.pool.set_scheduling('b', True)
        self.pool.set_scheduling('c', True)
        poll = {'assistant_texts': ['Seedance 2.0. It will use 3 points.'], 'videos': []}
        calls = []
        async def worker(account, *args, **kwargs):
            calls.append(account)
            kwargs['on_prompt_sent'](account)
            kwargs['on_conversation_id'](account, '12345', time.time() + 60)
            kwargs['on_browser_closed']()
            outcome = conversation_outcome(poll, 'ic_mini')
            if outcome['error']:
                raise outcome['error']
            raise SubmissionUnconfirmedError('No matching receipt before timeout')
        async def receipt(*args):
            return conversation_outcome(poll, 'ic_mini')
        with patch.object(pool_module, 'generate_video', worker), patch.object(self.pool, '_read_attempt_receipt', receipt):
            try:
                await self.pool.generate_video('fixture', duration=15, model='seedance-1.0')
            except PlatformVideoError as error:
                self.assertEqual(error.task_phase, 'request_mismatch')
            except Exception as error:
                self.fail(f'Confirmed generation must stop rotation: {type(error).__name__}')
            else:
                self.fail('A different-model receipt must be reported')
        self.assertEqual(calls, ['a'])

    async def test_new_one_point_observation_does_not_set_a_global_quota_lock(self):
        self.pool._set_credit_balance('a', 1)
        row = next(a for a in self.pool.list_accounts() if a['name'] == 'a')
        self.assertFalse(row['quota_blocked'])
        self.assertTrue(self.pool.all_accounts_quota_blocked,
                        'the default two-point route must still report insufficient balance')
        for model in ('seedance-2.0', 'seedance-2.5'):
            with self.subTest(model=model), self.assertRaises(pool_module.AllAccountsQuotaBlockedError):
                await self.generate(model)

    async def test_unknown_balance_is_allowed_but_zero_balance_is_not(self):
        self.assertEqual((await self.generate())['account'], 'a')
        self.pool._set_credit_balance('a', 0)
        with self.assertRaises(pool_module.AllAccountsQuotaBlockedError):
            await self.generate()

    async def test_one_point_does_not_clear_an_existing_platform_quota_lock(self):
        self.pool._mark_quota_blocked('a', 'platform confirmed insufficient quota')
        self.pool._set_credit_balance('a', 1)
        with self.assertRaises(pool_module.AllAccountsQuotaBlockedError):
            await self.generate()
        row = next(a for a in self.pool.list_accounts() if a['name'] == 'a')
        self.assertTrue(row['quota_blocked'])

    async def test_round_robin_and_pinned_isp_are_preserved_for_one_point_accounts(self):
        for name in ('a', 'b', 'c'):
            self.pool.set_scheduling(name, True)
            self.pool._set_credit_balance(name, 1)
        try:
            results = [await self.generate() for _ in range(3)]
        except RuntimeError as exc:
            self.fail(f'Model-specific balance must apply to candidates and rechecks: {exc}')
        self.assertEqual([(r['account'], r['proxy']) for r in results],
                         [('a', 'http://line-a:80'), ('b', 'http://line-b:80'),
                          ('c', 'http://line-a:80')])
        self.assertEqual([r['dispatch_count'] for r in self.pool.list_accounts()], [1, 1, 1])

    async def test_balance_is_rechecked_after_waiting_for_a_submission_slot(self):
        self.pool.set_scheduling('b', True)
        self.pool._set_credit_balance('a', 1)
        self.pool._set_credit_balance('b', 1)
        entered = asyncio.Event()
        original = self.pool._submission_candidates
        async def candidates(*args):
            async for row in original(*args):
                entered.set()
                yield row
        await self.pool.semaphore.acquire()
        with patch.object(self.pool, '_submission_candidates', candidates):
            task = asyncio.create_task(self.generate())
            try:
                await asyncio.wait_for(entered.wait(), .5)
            except asyncio.TimeoutError:
                self.pool.semaphore.release()
                await asyncio.gather(task, return_exceptions=True)
                self.fail('A one-point account never entered the submission queue')
            self.pool._set_credit_balance('a', 0)
            self.pool.semaphore.release()
            result = await asyncio.wait_for(task, 1)
        self.assertEqual(result['account'], 'b')

    async def test_api_accepts_seedance1_when_only_one_point_accounts_remain(self):
        self.pool._set_credit_balance('a', 1)
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.AsyncFunctionDef))
                 and n.name in ('TaskResponse', 'create_video')]
        for node in nodes:
            if isinstance(node, ast.AsyncFunctionDef):
                node.decorator_list = []
        store = TaskStore(':memory:')
        async def validate_urls(urls): return list(urls)
        runs = []
        async def run(*args): runs.append(args)
        client = {'api_key_hash': 'fixture', 'api_key_name': 'fixture',
                  'daily_limit': 0, 'concurrency_limit': 0, 'allowed_durations': [5, 10],
                  'owner_user_id': None}
        namespace = dict(app=FastAPI(), BaseModel=BaseModel, Field=Field, Header=Header,
            HTTPException=HTTPException, VideoGenRequest=SimpleNamespace, asyncio=asyncio,
            uuid=uuid, json=json, _auth=lambda _: client, pool=self.pool, store=store,
            SUPPORTED_DURATIONS=(5, 10, 15, 30), validate_video_parameters=lambda *args: None,
            submission_duration=__import__('video_capabilities').submission_duration,
            validate_reference_urls=validate_urls, attach_images=lambda *args, **kwargs: [],
            cleanup_task_images=lambda *args: None, _resolve_ratio=lambda size, ratio: ratio,
            _run_task=run, TaskQuotaExceeded=TaskQuotaExceeded,
            PendingTaskLimitExceeded=PendingTaskLimitExceeded,
            config=SimpleNamespace(REFERENCE_IMAGE_MAX_COUNT=30, REFERENCE_UPLOAD_DIR=self.root,
                                   MAX_PENDING_TASKS=10))
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     'seedance1-api', 'exec'), namespace)
        req = SimpleNamespace(model='seedance-1.0', prompt='fixture', duration=5,
            size=None, ratio=None, reference_images=[], reference_uploads=[],
            reference_media_uploads=[], idempotency_key=None, expert_account=None)
        try:
            try:
                response = await namespace['create_video'](req, 'Bearer fixture')
            except HTTPException as exc:
                self.fail(f'API rejected model-specific eligible balance: {exc.detail}')
            await asyncio.sleep(0)
            self.assertEqual(response.status, 'queued')
            self.assertEqual(store.get(response.id)['model'], 'seedance-1.0')
            self.assertEqual(runs[0][1], 'seedance-1.0')
        finally:
            store._conn.close()


if __name__ == '__main__':
    unittest.main()
