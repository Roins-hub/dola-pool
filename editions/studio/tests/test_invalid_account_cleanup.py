"""Exercise cleanup endpoints without importing the running production server."""
import ast
from copy import deepcopy
from pathlib import Path
import time
import unittest
import uuid

from fastapi import HTTPException


class PoolFixture:
    def __init__(self):
        self.rows = [self.account('expired-a', 0), self.account('expired-b', 0),
                     self.account('valid', 1), self.account('unknown', None)]
        self.deleted = []

    @staticmethod
    def account(name, login_ok, **fields):
        return dict(name=name, login_ok=login_ok, login_checked_at=100,
                    busy=False, proxy_blocked=False, proxy_exhausted=False, **fields)

    def list_accounts(self):
        return deepcopy(self.rows)

    def delete_account(self, name):
        self.deleted.append(name)
        self.rows = [a for a in self.rows if a['name'] != name]


class InvalidCleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'server.py').read_text(encoding='utf-8'))
        names = {'admin_accounts_delete_invalid_preview', 'admin_accounts_delete_invalid'}
        functions = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name in names]
        for node in functions:
            node.decorator_list = []
        self.pool = PoolFixture()
        self.auth = []
        self.ns = dict(pool=self.pool, time=time, uuid=uuid, HTTPException=HTTPException,
                       Header=lambda **kw: None, _admin_auth=lambda *args: self.auth.append(args),
                       _nat_key=lambda name: name, VERIFY_JOB=None, VERIFY_RUNS={},
                       DELETE_INVALID_RUNS={}, VERIFY_RUNS_TTL=3600)
        exec(compile(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])),
                     'cleanup-endpoints', 'exec'), self.ns)

    async def preview(self):
        self.assertTrue('admin_accounts_delete_invalid_preview' in self.ns,
                      'generation-marked invalid accounts need a cleanup preview without a full scan')
        return await self.ns['admin_accounts_delete_invalid_preview']('admin-key', 'session')

    async def delete(self, token):
        return await self.ns['admin_accounts_delete_invalid'](token, 'admin-key', 'session')

    async def test_generation_marked_accounts_can_be_previewed_and_cleaned_without_full_scan(self):
        result = await self.preview()
        self.assertEqual(result['accounts'], ['expired-a', 'expired-b'])
        self.assertEqual(self.pool.deleted, [], 'preview must not remove profiles')
        cleaned = await self.delete(result['token'])
        self.assertEqual(cleaned['deleted'], ['expired-a', 'expired-b'])
        self.assertEqual([r['name'] for r in self.pool.rows], ['valid', 'unknown'])
        self.assertEqual(self.auth, [('admin-key', 'session')] * 2)

    async def test_preview_excludes_busy_and_blocked_accounts(self):
        self.pool.rows[0]['busy'] = True
        self.pool.rows[1]['proxy_blocked'] = True
        result = await self.preview()
        self.assertEqual(result['accounts'], [])
        self.assertIsNone(result['token'])
        self.assertEqual({r['name'] for r in result['skipped']}, {'expired-a', 'expired-b'})
        self.assertEqual(self.pool.deleted, [])

    async def test_new_invalid_accounts_are_not_added_after_confirmation(self):
        preview = await self.preview()
        self.pool.rows[2]['login_ok'] = 0
        result = await self.delete(preview['token'])
        self.assertEqual(result['deleted'], ['expired-a', 'expired-b'])
        self.assertNotIn('valid', self.pool.deleted)

    async def test_changed_or_recovered_accounts_are_skipped(self):
        preview = await self.preview()
        self.pool.rows[0]['login_ok'] = 1
        self.pool.rows[1]['login_checked_at'] = 200
        result = await self.delete(preview['token'])
        self.assertEqual(result['deleted'], [])
        self.assertEqual(len(result['skipped']), 2)

    async def test_busy_state_is_checked_again_before_removal(self):
        preview = await self.preview()
        for row in self.pool.rows[:2]:
            row['busy'] = True
        result = await self.delete(preview['token'])
        self.assertEqual(result['deleted'], [])
        self.assertEqual(len(result['skipped']), 2)

    async def test_expired_and_consumed_previews_cannot_be_reused(self):
        preview = await self.preview()
        self.ns['DELETE_INVALID_RUNS'][preview['token']]['at'] = time.time() - 3601
        with self.assertRaises(HTTPException):
            await self.delete(preview['token'])
        self.assertEqual(self.pool.deleted, [])
        preview = await self.preview()
        await self.delete(preview['token'])
        with self.assertRaises(HTTPException):
            await self.delete(preview['token'])

    async def test_full_scan_in_progress_blocks_cleanup(self):
        self.ns['VERIFY_JOB'] = {'running': True}
        with self.assertRaises(HTTPException) as error:
            await self.preview()
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.pool.deleted, [])

    async def test_legacy_scan_token_cannot_remove_a_now_valid_account(self):
        self.ns['VERIFY_RUNS']['scan'] = {'at': time.time(), 'invalid': {'valid'}}
        result = await self.delete('scan')
        self.assertEqual(result['deleted'], [])
        self.assertEqual(self.pool.deleted, [])
