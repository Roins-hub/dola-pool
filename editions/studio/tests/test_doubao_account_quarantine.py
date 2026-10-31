import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from providers.base import ProviderError
from providers.doubao.accounts import AccountStore, atomic_json
from providers.doubao.runtime import BrowserSession
from test_doubao_provider import ck


class QuarantineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store=AccountStore(Path(self.temp.name))
        self.account=self.store.import_ck('one',ck(),'direct')

    async def test_expired_saved_and_native_sessions_pause_account_without_spending(self):
        folder=self.store.root/'accounts'/self.account['account_id']
        cookies=json.loads((folder/'cookies.json').read_text())
        for cookie in cookies: cookie['expires']=1
        atomic_json(folder/'cookies.json',cookies)
        session=BrowserSession(SimpleNamespace(accounts=self.store),self.account)
        session.context=SimpleNamespace(cookies=AsyncMock(return_value=[]))
        with self.assertRaises(ProviderError) as error:
            await session.seed_profile_session(folder/'cookies.json')
        self.assertEqual(error.exception.code,'invalid_cookie')
        result=self.store.list_accounts()[0]
        self.assertFalse(result['enabled'])
        self.assertEqual(result['status'],'login_required')
        self.assertEqual(result['send_count'],0)

    async def test_confirmed_signed_out_verification_is_actionable_and_pauses_scheduling(self):
        self.store.save_diagnostics('one',{'route_verified':True,'login_verified':False,'login_state':'signed_out'})
        result=self.store.list_accounts()[0]
        self.assertFalse(result['enabled'])
        self.assertEqual(result['status'],'login_required')
        self.assertEqual(result['quota_used'],0)
        self.assertEqual(self.store.bound_proxy('one'),{'server':'direct'})

    async def test_transient_page_failure_does_not_claim_expired_login_or_delete_account(self):
        self.store.save_diagnostics('one',{'route_verified':True,'login_verified':False,'login_state':'not_ready'})
        result=self.store.list_accounts()[0]
        self.assertTrue(result['enabled'])
        self.assertEqual(result['status'],'unverified')

    async def test_quarantined_legacy_alias_cannot_take_ownership_away_from_canonical_record(self):
        alias=self.store.import_ck('alias',ck('alias-session','alias-cookie'),'direct')
        folder=self.store.root/'accounts'/alias['account_id']
        atomic_json(folder/'official-identity.json',{'user_id':'same-user'})
        self.store.mark_unavailable('alias','duplicate',duplicate_of='one')
        self.store.observe_identity('one','same-user')
        self.assertEqual(self.store.official_user_id('one'),'same-user')
        try:
            self.store.refresh_session('one',ck('owner-new-session','owner-new-cookie'),'same-user')
        except ProviderError as error:
            self.fail('Quarantined alias prevented canonical session persistence: '+error.code)
        self.assertTrue(next(a for a in self.store.list_accounts() if a['name']=='one')['enabled'])
        with self.assertRaises(ProviderError): self.store.observe_identity('alias','same-user')
        self.assertFalse(next(a for a in self.store.list_accounts() if a['name']=='alias')['enabled'])


if __name__=='__main__': unittest.main()
