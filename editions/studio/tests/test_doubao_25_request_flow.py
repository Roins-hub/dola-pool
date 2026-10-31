"""Offline experimental 2.5 routing and measured-output checks."""
import copy
from pathlib import Path
import tempfile
import unittest

from providers.base import ProviderError
from providers.doubao import DoubaoProvider
from providers.doubao.accounts import atomic_json
from test_doubao_dialog_capabilities import dialog_manifest
from test_doubao_dialog_delivery import DialogSession
from test_doubao_provider import ck, PROXY


def manifest25():
    manifest=dialog_manifest()
    model=copy.deepcopy(manifest['models'][0])
    model.update(id='dialog_seedance_2.5',label='Seedance 2.5（30秒试验）',
                 validation_status='experimental',account_category='标准账号',
                 durations=[30],combinations=[{'duration':30,'ratio':'16:9'}],
                 receipt_patterns=['本次使用 Seedance 2.5 生成'],
                 evidence={'native_options_observation_id':'offline-native-options-fixture'})
    manifest['models'].append(model)
    return manifest


class Experimental25FlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.provider=DoubaoProvider(root=Path(self.temp.name),enabled=True,session_factory=DialogSession)
        self.addAsyncCleanup(self.provider.shutdown)
        self.provider.import_ck('normal',ck(),PROXY,category='普通账号')
        self.provider.import_ck('standard',ck('session-standard','uid-standard'),PROXY,category='标准账号')
        for name in ('normal','standard'):
            self.provider.accounts.save_diagnostics(name,{'route_verified':True,'login_verified':True})
        self.provider.save_capabilities(manifest25())
        self.row={'id':'video_'+'2'*32,'platform':'doubao','model':'dialog_seedance_2.5',
                  'duration':30,'ratio':'16:9','prompt':'小猫跳舞'}
        DialogSession.sends=0;DialogSession.observed_baselines=[]
        DialogSession.media={'actual_duration':30.073,'output_path':'retained-30.mp4',
                             'dimensions':{'width':1280,'height':720}}

    async def test_experimental_request_keeps_thirty_and_only_uses_standard_account(self):
        accounts=self.provider.accounts
        accounts.reserve('past-send','standard');accounts.mark_sent('past-send');accounts.settle('past-send','rejected')
        request=self.provider.validate_request(self.row)
        self.assertEqual(request['duration'],30)
        self.assertEqual(request['submitted_duration'],30)
        self.assertNotIn('duration_profile',request)
        await self.provider.run_task({**self.row,'account_category':'标准账号'})
        snapshot=self.provider._read_snapshot(self.row['id'])
        self.assertEqual(snapshot['account'],'standard')
        self.assertEqual(snapshot['request']['duration'],30)
        self.assertEqual(DialogSession.sends,1)
        self.assertEqual(next(a for a in self.provider.list_accounts() if a['name']=='normal')['send_count'],0)

    async def test_explicit_normal_category_is_rejected_before_sending_instead_of_silently_changed(self):
        with self.assertRaises(ProviderError) as error:
            await self.provider.run_task({**self.row,'account_category':'普通账号'})
        self.assertEqual(error.exception.code,'invalid_account_category')
        self.assertEqual(DialogSession.sends,0)
        self.assertIsNone(self.provider._read_snapshot(self.row['id']))
        self.assertTrue(all(account['send_count']==0 for account in self.provider.list_accounts()))

    async def test_no_standard_account_never_falls_back_to_normal(self):
        self.provider.patch_account('standard',enabled=False)
        with self.assertRaises(ProviderError) as error:
            await self.provider.run_task(self.row)
        self.assertEqual(error.exception.code,'account_unavailable')
        self.assertIn('标准账号',str(error.exception))
        self.assertEqual(DialogSession.sends,0)

    async def test_fast_fifteen_still_uses_normal_accounts(self):
        self.provider.patch_account('standard',enabled=False)
        row={**self.row,'model':'dialog_seedance_2.0_fast','duration':15}
        result=await self.provider.run_task(row)
        self.assertEqual(result['phase'],'accepted')
        self.assertEqual(self.provider._read_snapshot(row['id'])['account'],'normal')
        self.assertEqual(DialogSession.sends,1)

    async def finish(self,verified=True):
        await self.provider.run_task(self.row)
        snapshot=self.provider._read_snapshot(self.row['id'])
        snapshot['dialog_model_verified']=verified
        atomic_json(self.provider._snapshot_path(self.row['id']),snapshot)
        return await self.provider._finish(DialogSession(),snapshot,
            {'phase':'media_ready','media':['https://cdn.example/30.mp4']})

    async def test_fifteen_second_output_is_retained_and_not_called_thirty(self):
        DialogSession.media['actual_duration']=15.073
        result=await self.finish()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['phase'],'duration_mismatch')
        self.assertEqual(result['output_path'],'retained-30.mp4')

    async def test_thirty_second_output_still_needs_actual_25_receipt(self):
        result=await self.finish(verified=False)
        self.assertEqual(result['error_code'],'model_unverified')
        self.assertIn('Seedance 2.5',result['error'])
        self.assertEqual(result['output_path'],'retained-30.mp4')

    async def test_matching_output_does_not_silently_promote_experimental_catalog(self):
        result=await self.finish()
        self.assertEqual(result['status'],'completed')
        model=next(m for m in self.provider.manifest.load()['models'] if m['id']=='dialog_seedance_2.5')
        self.assertEqual(model['validation_status'],'experimental')


if __name__=='__main__':unittest.main()
