"""Restore a saved identity without making another pool entry or losing tasks."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI, HTTPException
from providers.admin_api import create_admin_router
from providers.doubao import DoubaoProvider, ProviderError
from test_doubao_admin_api import ASGIClient
from test_doubao_provider import ck


class RestoreWindow:
    instances=[]
    def __init__(self,provider,account,*,login):
        self.provider,self.account=provider,account
        self.closed=False
        self.__class__.instances.append(self)
    async def __aenter__(self):return self
    async def __aexit__(self,*_):self.closed=True
    async def interactive_login_status(self):
        return {'state':'authenticated' if self.provider.fixture_authenticated else 'waiting_login',
                'login_verified':self.provider.fixture_authenticated}
    async def capture_interactive_login(self):
        return {'state':'captured','cookies':json.loads(ck('new-session','new-cookie-uid')),
                'official_user_id':self.provider.fixture_user_id,
                'checks':{'route_verified':True,'login_verified':True},'login_verified':True}


class ExistingLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.provider=DoubaoProvider(root=Path(self.temp.name),enabled=False,login_session_factory=RestoreWindow)
        self.provider.fixture_authenticated=True
        self.provider.fixture_user_id='1234567890123456789'
        self.account=self.provider.import_ck('one',ck(),'direct','keepers')
        self.folder=self.provider.root/'accounts'/self.account['account_id']
        (self.folder/'profile').mkdir()
        (self.folder/'profile'/'marker').write_text('original-profile',encoding='utf-8')
        RestoreWindow.instances=[]
    async def asyncTearDown(self):await self.provider.shutdown()
    async def finished(self,job):
        for _ in range(100):
            result=self.provider.login_status(job['id'])
            if result['state'] in ('completed','failed','expired','cancelled'):return result
            await asyncio.sleep(.01)
        self.fail('Saved-account restoration did not finish')
    async def test_already_logged_in_profile_refreshes_same_account_and_preserves_active_attempt(self):
        self.provider.accounts.patch('one',quota_limit=4,enabled=False)
        # Reserve before disabling; the fence is preserved throughout restoration.
        with self.provider.accounts.connect() as conn:
            conn.execute("INSERT INTO attempts(task_id,account_id,sent,state,created_at) VALUES(?,?,1,'accepted',1)",
                         ('existing-task',self.account['account_id']))
        self.provider.scheduler.reserve_recovery('doubao','one',{'server':'direct'})
        before=self.provider.list_accounts()[0]
        result=await self.finished(self.provider.start_account_login('one'))
        self.assertEqual(result['state'],'completed',result)
        self.assertTrue(result['restored'])
        after=self.provider.list_accounts()[0]
        self.assertEqual(len(self.provider.list_accounts()),1)
        for key in ('account_id','name','enabled','quota_limit','quota_used','quota_reserved','send_count','inflight_tasks','category'):
            self.assertEqual(after[key],before[key],key)
        self.assertEqual((self.folder/'profile'/'marker').read_text(encoding='utf-8'),'original-profile')
        window=RestoreWindow.instances[0]
        self.assertNotIn('_login',window.account)
        self.assertEqual(window.account['account_id'],self.account['account_id'])
        self.assertTrue(window.closed)
        self.assertNotIn('new-session',json.dumps(result))
    async def test_conflicting_official_identity_does_not_replace_saved_credentials(self):
        (self.folder/'official-identity.json').write_text(json.dumps({'user_id':'9876543210987654321'}),encoding='utf-8')
        before=(self.folder/'cookies.json').read_bytes()
        result=await self.finished(self.provider.start_account_login('one'))
        self.assertEqual(result['state'],'failed',result)
        self.assertEqual(result['error_code'],'account_identity_mismatch')
        self.assertEqual((self.folder/'cookies.json').read_bytes(),before)
        self.assertEqual(len(self.provider.list_accounts()),1)
    async def test_missing_official_identity_keeps_window_without_saving_cookie(self):
        self.provider.fixture_user_id=None
        before=(self.folder/'cookies.json').read_bytes()
        job=self.provider.start_account_login('one')
        await asyncio.sleep(.15)
        self.assertNotEqual(self.provider.login_status(job['id'])['state'],'completed')
        self.assertEqual((self.folder/'cookies.json').read_bytes(),before)
    async def test_same_account_cannot_open_two_restore_windows(self):
        self.provider.fixture_authenticated=False
        self.provider.start_account_login('one')
        with self.assertRaises(ProviderError) as error:self.provider.start_account_login('one')
        self.assertEqual(error.exception.code,'login_busy')
    async def test_new_native_login_binds_official_identity_for_future_session_sync(self):
        result=await self.finished(self.provider.start_login('new-account','direct',mode='browser'))
        self.assertEqual(result['state'],'completed',result)
        self.assertEqual(self.provider.accounts.official_user_id('new-account'),self.provider.fixture_user_id)
    async def test_add_login_cannot_create_duplicate_when_cookie_ciphertext_rotates(self):
        self.provider.accounts.refresh_session('one',ck(),self.provider.fixture_user_id)
        before=(self.folder/'cookies.json').read_bytes()
        result=await self.finished(self.provider.start_login('accidental-duplicate','direct',mode='browser'))
        self.assertEqual(result['state'],'failed',result)
        self.assertEqual(result['error_code'],'duplicate_account')
        self.assertEqual(len(self.provider.list_accounts()),1)
        self.assertEqual((self.folder/'cookies.json').read_bytes(),before)
    async def test_existing_login_route_authenticates_and_does_not_accept_proxy_or_cookie_fields(self):
        def auth(key,authorization=None):
            if key!='fixture-admin':raise HTTPException(401,'admin required')
            return {'kind':'admin_key','role':'admin'}
        app=FastAPI();app.include_router(create_admin_router(self.provider,auth))
        client=ASGIClient(app);path='/api/admin/platforms/doubao/accounts/one/login'
        self.assertEqual((await client.post(path,json={})).status_code,401)
        headers={'X-Admin-Key':'fixture-admin'}
        bad=await client.post(path,headers=headers,json={'proxy':'direct','mode':'browser'})
        self.assertEqual(bad.status_code,422)
        response=await client.post(path,headers=headers,json={'mode':'browser'})
        self.assertEqual(response.status_code,200,response.text)
        result=await self.finished(response.json())
        self.assertEqual(result['state'],'completed',result)
        self.assertEqual(result['account']['account_id'],self.account['account_id'])


if __name__=='__main__':unittest.main()
