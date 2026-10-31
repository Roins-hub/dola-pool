"""Category switches gate new submissions without cancelling accepted work."""
import ast
import asyncio
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from test_five_account_retry import pool_module
from ck_import import import_ck, import_ck_batch
from navigation_errors import BrowserSetupError


class AccountCategoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'accounts'
        for name in ('a', 'b', 'c'):
            (self.root / name).mkdir(parents=True)
        self.db = str(Path(self.tmp.name) / 'pool.db')
        self.pool = pool_module.BrowserPool(self.root, self.db, max_concurrency=2)
        self.proxy = patch.object(self.pool, '_proxy_state', side_effect=lambda name: {
            'proxy_server': 'http://line-a:80' if name in ('a', 'c') else 'http://line-b:80',
            'proxy_blocked': False})
        self.proxy.start()
        self.pool.accounts

    async def asyncTearDown(self):
        self.proxy.stop()
        self.pool._conn.close()
        self.tmp.cleanup()

    async def test_existing_profiles_default_long_term_and_remain_schedulable(self):
        rows = self.pool.list_accounts()
        self.assertEqual([r.get('category') for r in rows], ['long_term'] * 3)
        self.assertTrue(all(r['effective_scheduling'] for r in rows))
        self.assertTrue(self.pool.available)

    async def test_legacy_metadata_migration_preserves_account_state(self):
        self.pool.set_scheduling('b', False)
        self.pool._mark_daily_limit('c')
        self.pool._conn.execute('ALTER TABLE accounts_meta DROP COLUMN category')
        columns = {row['name'] for row in self.pool._conn.execute('PRAGMA table_info(accounts_meta)')}
        if 'category_override' in columns:
            self.pool._conn.execute('ALTER TABLE accounts_meta DROP COLUMN category_override')
        self.pool._conn.execute('DROP TABLE account_categories')
        self.pool._conn.commit()
        self.pool._conn.close()
        self.pool = pool_module.BrowserPool(self.root, self.db)
        with patch.object(self.pool, '_proxy_state', return_value={'proxy_server': 'http://line-a:80'}):
            rows = {r['name']: r for r in self.pool.list_accounts()}
            self.assertTrue(rows['a']['effective_scheduling'])
            self.assertFalse(rows['b']['effective_scheduling'])
            self.assertTrue(rows['c']['rate_limited'])
            self.assertEqual({r['category'] for r in rows.values()}, {'long_term'})
            self.assertTrue(all(r.get('category_override') is False for r in rows.values()))

    async def test_group_pause_survives_restart_and_does_not_override_individual_pause(self):
        self.pool.set_category('c', 'disposable')
        self.pool.set_scheduling('b', False)
        self.pool.set_category_enabled('long_term', False)
        self.pool._conn.close()
        self.pool = pool_module.BrowserPool(self.root, self.db)
        with patch.object(self.pool, '_proxy_state', return_value={'proxy_server': 'http://line-a:80'}):
            rows = {r['name']: r for r in self.pool.list_accounts()}
            self.assertTrue(rows['a']['scheduling'])
            self.assertFalse(rows['a']['effective_scheduling'])
            self.assertTrue(rows['c']['effective_scheduling'])
            self.pool.set_category_enabled('long_term', True)
            rows = {r['name']: r for r in self.pool.list_accounts()}
            self.assertTrue(rows['a']['effective_scheduling'])
            self.assertFalse(rows['b']['effective_scheduling'])
            self.assertEqual({r['id']: r['count'] for r in self.pool.list_categories()},
                             {'long_term': 2, 'disposable': 1})

    async def test_pause_while_waiting_for_submission_slot_rechecks_category(self):
        self.pool.set_category('b', 'disposable')
        await self.pool.semaphore.acquire()
        await self.pool.semaphore.acquire()
        entered = asyncio.Event()
        original = self.pool._submission_candidates
        async def candidates(*args):
            async for row in original(*args):
                entered.set()
                yield row
        async def worker(account, *args, **kwargs):
            kwargs['on_browser_closed']()
            return {'account': account}
        with patch.object(self.pool, '_submission_candidates', candidates), patch.object(pool_module, 'generate_video', worker):
            task = asyncio.create_task(self.pool.generate_video('test', duration=10))
            await asyncio.wait_for(entered.wait(), 1)
            self.pool.set_category_enabled('long_term', False)
            self.pool.semaphore.release()
            self.pool.semaphore.release()
            result = await asyncio.wait_for(task, 1)
        self.assertEqual(result['account'], 'b')

    async def test_retry_skips_newly_paused_category_and_preserves_isp(self):
        self.pool.set_category('b', 'disposable')
        calls = []
        async def worker(account, *args, **kwargs):
            calls.append((account, self.pool._proxy_state(account)['proxy_server']))
            kwargs['on_browser_closed']()
            if account == 'a':
                self.pool.set_category_enabled('long_term', False)
                raise BrowserSetupError('fixture pre-send failure')
            return {'account': account}
        with patch.object(pool_module, 'generate_video', worker):
            result = await self.pool.generate_video('test', duration=10)
        self.assertEqual(result['account'], 'b')
        self.assertEqual(calls, [('a', 'http://line-a:80'), ('b', 'http://line-b:80')])

    async def test_paused_category_does_not_cancel_accepted_polling(self):
        self.pool.set_category_enabled('long_term', False)
        async def poll(account, conversation, timeout, **kwargs):
            return {'account': account, 'conversation_id': conversation, 'video_url': '/fixture.mp4'}
        with patch.object(pool_module, 'resume_video', poll):
            result = await self.pool.resume_video('a', '1234', 60, accepted=True)
        self.assertEqual(result['video_url'], '/fixture.mp4')
        self.assertFalse(self.pool.available)

    async def test_disabled_category_cannot_mask_enabled_category_quota_diagnostic(self):
        self.pool.set_category('b', 'disposable')
        self.pool.set_category_enabled('long_term', False)
        self.pool._mark_daily_limit('b')
        self.assertTrue(self.pool.all_accounts_limited)
        self.pool.set_category_enabled('disposable', False)
        self.assertFalse(self.pool.all_accounts_limited)
        self.assertFalse(self.pool.available)


async def api_request(app, method, path, payload=None, authorized=True):
    messages = []
    async def receive():
        return {'type': 'http.request', 'body': json.dumps(payload or {}).encode(), 'more_body': False}
    async def send(message):
        messages.append(message)
    headers = [(b'content-type', b'application/json')]
    if authorized:
        headers.append((b'x-admin-key', b'fixture-admin'))
    await app(dict(type='http', asgi={'version': '3.0'}, http_version='1.1', method=method,
        scheme='http', path=path, raw_path=path.encode(), query_string=b'', root_path='',
        headers=headers, client=('127.0.0.1', 1), server=('test', 80)), receive, send)
    status = next(m['status'] for m in messages if m['type'] == 'http.response.start')
    return status, json.loads(b''.join(m.get('body', b'') for m in messages))


class AccountCategoryAPITests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = AccountCategoryTests.asyncTearDown

    async def asyncSetUp(self):
        await AccountCategoryTests.asyncSetUp(self)
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'server.py').read_text(encoding='utf-8'))
        names = {'CKImport', 'AccountPatch', 'AccountCategoryPatch', 'admin_accounts',
                 'admin_account_patch', 'admin_account_category_patch', 'admin_import_ck', 'admin_import_ck_batch', 'admin_stats'}
        nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.AsyncFunctionDef)) and n.name in names]
        def auth(key, authorization):
            if key != 'fixture-admin':
                raise HTTPException(401, 'unauthorized')
        self.app = FastAPI()
        namespace = dict(app=self.app, BaseModel=BaseModel, Field=Field, Header=Header,
                         HTTPException=HTTPException, _admin_auth=auth, pool=self.pool,
                         import_ck=import_ck, import_ck_batch=import_ck_batch, JOBS={},
                         config=SimpleNamespace(_load_proxy_map=lambda: {}),
                         store=SimpleNamespace(stats=lambda: {}))
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     'category-endpoints', 'exec'), namespace)

    async def test_admin_get_exposes_category_counts(self):
        status, data = await api_request(self.app, 'GET', '/api/admin/accounts')
        self.assertEqual(status, 200)
        self.assertEqual({c['id']: c['count'] for c in data.get('categories', [])},
                         {'long_term': 3, 'disposable': 0})

    async def test_restore_one_account_in_paused_category_is_used_for_submission(self):
        for individually_paused in (False, True):
            with self.subTest(individually_paused=individually_paused):
                self.pool.set_category_enabled('long_term', False)
                self.pool.set_scheduling('b', not individually_paused)
                status, data = await api_request(self.app, 'PATCH', '/api/admin/accounts/b', {'scheduling': True})
                rows = {r['name']: r for r in self.pool.list_accounts()}
                self.assertEqual(status, 200)
                self.assertEqual([r['name'] for r in rows.values() if r['effective_scheduling']], ['b'])
                self.assertTrue(rows['b']['category_override'])
                self.assertFalse(rows['b']['category_enabled'])
                self.assertEqual(data['account'], rows['b'])
                self.assertTrue(data['ok'])
                async def worker(account, *args, **kwargs):
                    kwargs['on_browser_closed']()
                    return {'account': account}
                with patch.object(pool_module, 'generate_video', worker):
                    result = await asyncio.wait_for(self.pool.generate_video('fixture', duration=10), 1)
                self.assertEqual(result['account'], 'b')
                status, stats = await api_request(self.app, 'GET', '/api/admin/stats')
                self.assertEqual(stats['available_accounts'], 1)

    async def test_restore_persists_across_restart_without_opening_category(self):
        self.pool.set_category_enabled('long_term', False)
        await api_request(self.app, 'PATCH', '/api/admin/accounts/b', {'scheduling': True})
        self.pool._conn.close()
        self.pool = pool_module.BrowserPool(self.root, self.db)
        with patch.object(self.pool, '_proxy_state', return_value={'proxy_server': 'http://fixture:80'}):
            rows = {r['name']: r for r in self.pool.list_accounts()}
            self.assertEqual([r['name'] for r in rows.values() if r['effective_scheduling']], ['b'])
            self.assertTrue(rows['b']['category_override'])
            self.assertFalse(next(c for c in self.pool.list_categories() if c['id'] == 'long_term')['enabled'])

    async def test_category_batch_actions_clear_only_their_overrides(self):
        self.pool.set_category('c', 'disposable')
        self.pool.set_category_enabled('disposable', False)
        await api_request(self.app, 'PATCH', '/api/admin/accounts/c', {'scheduling': True})
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.pool.set_category_enabled('long_term', False)
                self.pool.set_scheduling('b', False)
                await api_request(self.app, 'PATCH', '/api/admin/accounts/a', {'scheduling': True})
                await api_request(self.app, 'PATCH', '/api/admin/account-categories/long_term', {'enabled': enabled})
                rows = {r['name']: r for r in self.pool.list_accounts()}
                self.assertIs(rows['a'].get('category_override'), False)
                self.assertIs(rows['a']['effective_scheduling'], enabled)
                self.assertFalse(rows['b']['effective_scheduling'])
                self.assertTrue(rows['c']['effective_scheduling'])
                self.assertTrue(rows['c']['category_override'])

    async def test_move_to_another_paused_category_clears_restore(self):
        self.pool.set_category_enabled('long_term', False)
        self.pool.set_category_enabled('disposable', False)
        await api_request(self.app, 'PATCH', '/api/admin/accounts/a', {'scheduling': True})
        status, data = await api_request(self.app, 'PATCH', '/api/admin/accounts/a', {'category': 'disposable'})
        self.assertEqual(status, 200)
        self.assertIn('account', data)
        self.assertFalse(data['account']['category_override'])
        self.assertFalse(data['account']['effective_scheduling'])

    async def test_individual_pause_clears_restore_and_internal_enable_cannot_recreate_it(self):
        self.pool.set_category_enabled('long_term', False)
        await api_request(self.app, 'PATCH', '/api/admin/accounts/a', {'scheduling': True})
        status, data = await api_request(self.app, 'PATCH', '/api/admin/accounts/a', {'scheduling': False})
        self.assertEqual(status, 200)
        self.assertIn('account', data)
        self.assertFalse(data['account']['category_override'])
        self.assertFalse(data['account']['effective_scheduling'])
        self.pool.set_scheduling('a', True)
        self.assertFalse(self.pool.available)

    async def test_birthday_pause_clears_restore_and_remains_paused_after_bulk_health_reset(self):
        self.pool.set_category_enabled('long_term', False)
        await api_request(self.app, 'PATCH', '/api/admin/accounts/a', {'scheduling': True})
        self.pool.mark_birthday_verification('a')
        self.pool.reset_all_accounts()
        account = next(a for a in self.pool.list_accounts() if a['name'] == 'a')
        self.assertFalse(account['category_override'])
        self.assertFalse(account['scheduling'])
        self.assertEqual(account['verification_required'], 'birthday')
        self.assertFalse(self.pool.available)

    async def test_category_override_reset_failure_rolls_back_category_switch(self):
        self.pool.set_category_enabled('long_term', False)
        await api_request(self.app, 'PATCH', '/api/admin/accounts/a', {'scheduling': True})
        self.pool._conn.execute("CREATE TRIGGER fail_override_reset BEFORE UPDATE OF category_override "
            "ON accounts_meta BEGIN SELECT RAISE(ABORT, 'fixture update failed'); END")
        self.pool._conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.pool.set_category_enabled('long_term', True)
        account = next(a for a in self.pool.list_accounts() if a['name'] == 'a')
        self.assertFalse(account['category_enabled'])
        self.assertTrue(account['category_override'])
        self.assertTrue(account['effective_scheduling'])

    async def test_restored_account_still_obeys_daily_usage_and_isp_blocks(self):
        self.pool.set_category_enabled('long_term', False)
        await api_request(self.app, 'PATCH', '/api/admin/accounts/a', {'scheduling': True})
        with patch.object(self.pool, '_proxy_state', return_value={
                'proxy_server': 'http://line-a:80', 'proxy_blocked': True}):
            self.assertFalse(self.pool.available)
            self.assertTrue(self.pool.all_accounts_proxy_blocked)
        self.pool._claim('a')
        self.pool._claim('a')
        self.assertFalse(self.pool.available)
        self.assertTrue(self.pool.all_accounts_limited)

    async def test_restored_accounts_keep_account_and_isp_mutual_exclusion(self):
        self.pool.set_category_enabled('long_term', False)
        for name in ('a', 'c'):
            await api_request(self.app, 'PATCH', f'/api/admin/accounts/{name}', {'scheduling': True})
        entered = asyncio.Event()
        second_waiting = asyncio.Event()
        release = asyncio.Event()
        submitted = []
        original_wait = self.pool._available.wait
        async def wait_available():
            second_waiting.set()
            await original_wait()
        async def worker(account, *args, **kwargs):
            submitted.append(account)
            entered.set()
            await release.wait()
            kwargs['on_browser_closed']()
            return {'account': account}
        with patch.object(pool_module, 'generate_video', worker), patch.object(self.pool._available, 'wait', wait_available):
            first = asyncio.create_task(self.pool.generate_video('first', duration=10))
            second = None
            try:
                await asyncio.wait_for(entered.wait(), 1)
                second = asyncio.create_task(self.pool.generate_video('second', duration=10))
                await asyncio.wait_for(second_waiting.wait(), 1)
                self.assertEqual(submitted, ['a'])
                self.assertTrue(self.pool._locks['a'].locked())
                release.set()
                await asyncio.wait_for(asyncio.gather(first, second), 1)
            finally:
                release.set()
                for task in (first, second):
                    if task and not task.done():
                        task.cancel()
                await asyncio.gather(*(t for t in (first, second) if t), return_exceptions=True)
        self.assertEqual(len(submitted), 2)
        self.assertTrue(all(not lock.locked() for lock in self.pool._locks.values()))
        self.assertTrue(all(slot._value == 1 for slot in self.pool._proxy_slots.values()))

    async def test_restore_does_not_clear_health_or_quota_blocks(self):
        self.pool.set_category_enabled('long_term', False)
        states = ({'login_ok': 0}, {'cooldown_until': time.time() + 3600},
                  {'rate_limited_until': time.time() + 3600},
                  {'quota_blocked_until': time.time() + 3600}, {'credit_balance': 1})
        for state in states:
            with self.subTest(state=state):
                self.pool._conn.execute('UPDATE accounts_meta SET login_ok=1, cooldown_until=0, '
                    'rate_limited_until=0, quota_blocked_until=0, credit_balance=NULL WHERE name=?', ('a',))
                column, value = next(iter(state.items()))
                self.pool._conn.execute(f'UPDATE accounts_meta SET {column}=? WHERE name=?', (value, 'a'))
                self.pool._conn.commit()
                await api_request(self.app, 'PATCH', '/api/admin/accounts/a', {'scheduling': True})
                account = next(a for a in self.pool.list_accounts() if a['name'] == 'a')
                self.assertTrue(account['effective_scheduling'])
                self.assertEqual(account[column], value)
                self.assertFalse(self.pool.available)

    async def test_repeated_batch_pause_rechecks_restored_account_waiting_for_slot(self):
        self.pool.set_category('b', 'disposable')
        self.pool.set_category_enabled('long_term', False)
        await api_request(self.app, 'PATCH', '/api/admin/accounts/a', {'scheduling': True})
        await self.pool.semaphore.acquire()
        await self.pool.semaphore.acquire()
        entered = asyncio.Event()
        selected = []
        submitted = []
        original = self.pool._submission_candidates
        async def candidates(*args):
            async for row in original(*args):
                selected.append(row['name'])
                entered.set()
                yield row
        async def worker(account, *args, **kwargs):
            submitted.append(account)
            kwargs['on_browser_closed']()
            return {'account': account}
        with patch.object(self.pool, '_submission_candidates', candidates), patch.object(pool_module, 'generate_video', worker):
            task = asyncio.create_task(self.pool.generate_video('fixture', duration=10))
            await asyncio.wait_for(entered.wait(), 1)
            await api_request(self.app, 'PATCH', '/api/admin/account-categories/long_term', {'enabled': False})
            self.pool.semaphore.release()
            self.pool.semaphore.release()
            result = await asyncio.wait_for(task, 1)
        self.assertEqual(selected[0], 'a')
        self.assertEqual(submitted, ['b'])
        self.assertEqual(result['account'], 'b')

    async def test_unauthorized_restore_does_not_change_category_or_account(self):
        self.pool.set_category_enabled('long_term', False)
        status, _ = await api_request(self.app, 'PATCH', '/api/admin/accounts/a', {'scheduling': True}, authorized=False)
        self.assertEqual(status, 401)
        self.assertFalse(self.pool.available)

    async def test_stats_excludes_category_paused_accounts(self):
        self.pool.set_category('b', 'disposable')
        self.pool.set_category_enabled('long_term', False)
        status, data = await api_request(self.app, 'GET', '/api/admin/stats')
        self.assertEqual(status, 200)
        self.assertEqual(data['total_accounts'], 3)
        self.assertEqual(data['available_accounts'], 1)
        self.assertEqual(data['total_remaining'], 2)

    async def test_category_and_assignment_mutations_require_admin_and_validate_values(self):
        url = '/api/admin/account-categories/long_term'
        status, _ = await api_request(self.app, 'PATCH', url, {'enabled': False}, authorized=False)
        self.assertEqual(status, 401)
        status, _ = await api_request(self.app, 'PATCH', url, {'enabled': False})
        self.assertEqual(status, 200)
        self.assertFalse(self.pool.available)
        status, _ = await api_request(self.app, 'PATCH', '/api/admin/accounts/a', {'category': 'disposable'})
        self.assertEqual(status, 200)
        self.assertTrue(self.pool.available)
        status, _ = await api_request(self.app, 'PATCH', '/api/admin/accounts/a', {'category': 'invalid'})
        self.assertEqual(status, 422)
        status, _ = await api_request(self.app, 'PATCH', '/api/admin/account-categories/invalid', {'enabled': False})
        self.assertEqual(status, 400)

    async def test_single_and_batch_import_set_category_but_duplicates_keep_theirs(self):
        single = '/api/admin/accounts/import-ck'
        status, _ = await api_request(self.app, 'POST', single,
            {'name': 'new', 'ck': 'sessionid=fixture-a', 'category': 'disposable'})
        self.assertEqual(status, 201)
        self.assertEqual(next(r for r in self.pool.list_accounts() if r['name'] == 'new').get('category'), 'disposable')
        status, data = await api_request(self.app, 'POST', single,
            {'name': 'duplicate', 'ck': 'sessionid=fixture-a', 'category': 'long_term'})
        self.assertEqual(status, 201)
        self.assertTrue(data['existing'])
        self.assertEqual(next(r for r in self.pool.list_accounts() if r['name'] == 'new')['category'], 'disposable')
        status, data = await api_request(self.app, 'POST', '/api/admin/accounts/import-ck-batch',
            {'name': 'batch', 'ck': 'sessionid=fixture-b\nsessionid=fixture-c', 'category': 'disposable'})
        self.assertEqual(status, 201)
        self.assertEqual(data['imported'], 2)
        self.assertEqual({r['category'] for r in self.pool.list_accounts() if r['name'].startswith('batch')}, {'disposable'})
        status, _ = await api_request(self.app, 'POST', single,
            {'name': 'bad', 'ck': 'sessionid=fixture-d', 'category': 'invalid'})
        self.assertEqual(status, 422)
        self.assertFalse((self.root / 'bad').exists())
