"""Exercise the administrator's quota preset against a private real pool database."""
import ast
from pathlib import Path
import unittest

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
import test_account_categories as fixtures
from test_account_categories import api_request


class QuotaPolicyApiTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = fixtures.AccountCategoryTests.asyncTearDown

    async def asyncSetUp(self):
        await fixtures.AccountCategoryTests.asyncSetUp(self)
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        names = {'QuotaPolicyPatch', 'admin_accounts', 'admin_quota_policy_patch'}
        nodes = [node for node in tree.body
                 if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef)) and node.name in names]
        self.app = FastAPI()

        def auth(key, authorization):
            if key != 'fixture-admin':
                raise HTTPException(401, 'unauthorized')

        namespace = dict(app=self.app, BaseModel=BaseModel, Field=Field, Header=Header,
                         HTTPException=HTTPException, pool=self.pool, _admin_auth=auth)
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     'quota-policy-api', 'exec'), namespace)

    async def test_account_list_exposes_actual_existing_two_per_day_preset(self):
        status, result = await api_request(self.app, 'GET', '/api/admin/accounts')
        self.assertEqual(status, 200)
        self.assertEqual(result.get('quota_policy'), {'mode': 'seedance25', 'limit': 2})
        self.assertTrue(all(row['limit'] == 2 for row in result['accounts']))

    async def test_switch_updates_every_account_limit_without_resetting_usage_or_categories(self):
        for index in range(3):
            self.pool._claim_conversation('a', 'fixture-' + str(index))
        self.pool.set_category('c', 'disposable')
        before = {table: [tuple(row) for row in self.pool._conn.execute('SELECT * FROM ' + table)]
                  for table in ('usage', 'accepted_videos', 'accounts_meta', 'account_categories')}
        for mode, limit, remaining in (('fast', 4, 1), ('seedance25', 2, 0)):
            status, result = await api_request(self.app, 'PATCH', '/api/admin/quota-policy', {'mode': mode})
            self.assertEqual(status, 200, result)
            self.assertTrue(result['ok'])
            self.assertEqual(result['quota_policy'], {'mode': mode, 'limit': limit})
            self.assertTrue(all(row['limit'] == limit for row in result['accounts']))
            account = next(row for row in result['accounts'] if row['name'] == 'a')
            self.assertEqual((account['used_today'], account['remaining']), (3, remaining))
            self.assertEqual({value['id']: value['count'] for value in result['categories']},
                             {'long_term': 2, 'disposable': 1})
            for table, rows in before.items():
                self.assertEqual([tuple(row) for row in self.pool._conn.execute('SELECT * FROM ' + table)], rows, table)
            status, read = await api_request(self.app, 'GET', '/api/admin/accounts')
            self.assertEqual(read['quota_policy'], result['quota_policy'])

    async def test_only_administrator_can_select_fixed_presets(self):
        path = '/api/admin/quota-policy'
        status, _ = await api_request(self.app, 'PATCH', path, {'mode': 'fast'}, authorized=False)
        self.assertEqual(status, 401)
        for payload in ({}, {'mode': 'invalid'}, {'mode': 4}, {'mode': 'fast', 'limit': 999}):
            status, result = await api_request(self.app, 'PATCH', path, payload)
            self.assertEqual(status, 422, result)
        status, result = await api_request(self.app, 'GET', '/api/admin/accounts')
        self.assertEqual(result['quota_policy'], {'mode': 'seedance25', 'limit': 2})
