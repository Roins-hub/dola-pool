import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest

from providers.doubao import DoubaoProvider, ProviderError
from providers.doubao.runtime import evidence_from_payload
from test_doubao_provider import ck, PROXY


def verified_manifest():
    return {'version': 'fixture-1', 'verified_at': time.time(), 'expires_at': time.time()+86400,
            'models': [{'id': 'fixture-model', 'label': 'Fixture', 'enabled': True,
                'durations': [6], 'ratios': ['16:9'],
                'references': {'images': {'enabled': True, 'max_count': 2, 'max_bytes': 1024*1024}},
                'evidence': {'text_task_id': 'verified-text', 'image_task_id': 'verified-image'},
                'selectors': {'video_entry': '#video', 'composer': '#prompt', 'send': '#send',
                              'assistant': '.assistant', 'upload': 'input[type=file]', 'upload_ready': '.uploaded',
                              'model': '#model', 'duration': '#duration', 'ratio': '#ratio'},
                'receipt_patterns': ['开始生成'], 'rejection_patterns': ['无法生成']} ]}


class CapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = DoubaoProvider(root=Path(self.temp.name), enabled=True)
        self.provider.import_ck('one', ck(), PROXY)

    def test_corrupt_recovery_quarantines_its_lane_without_breaking_other_platform_startup(self):
        task_id = 'video_' + 'f' * 32
        path = self.provider._snapshot_path(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{broken', encoding='utf-8')
        error = None
        try:
            self.provider.reserve_recoveries([{'id':task_id, 'account':'one'}])
        except ProviderError as caught:
            error = caught
        self.assertIsNone(error, 'one corrupt Doubao snapshot must not abort the shared service startup')
        binding = self.provider.accounts.bound_proxy('one')
        self.assertIn(self.provider.scheduler.lane_id(binding), self.provider.scheduler.unsafe_lanes)

    def test_capability_requires_observed_evidence_and_live_route_before_admission(self):
        manifest = verified_manifest()
        self.provider.save_capabilities(manifest)
        self.assertFalse(self.provider.capabilities()['enabled'])
        self.provider.accounts.save_diagnostics('one', {'route_verified': True, 'login_verified': True})
        self.assertTrue(self.provider.capabilities()['enabled'])
        request = self.provider.validate_request({'prompt': 'cat', 'model': 'fixture-model', 'duration': 6, 'ratio': '16:9'})
        self.assertEqual(request['duration'], 6)
        self.assertEqual(request['submitted_duration'], 6)
        self.assertEqual(request['capability_version'], 'fixture-1')

    def test_manifest_cannot_claim_images_without_successful_image_evidence(self):
        manifest = verified_manifest()
        manifest['models'][0]['evidence'].pop('image_task_id')
        with self.assertRaises(ProviderError):
            self.provider.save_capabilities(manifest)
        self.assertEqual(self.provider.capabilities()['models'], [])

    def test_requested_account_category_is_validated_against_model(self):
        manifest=verified_manifest();manifest['models'][0]['account_category']='标准账号'
        self.provider.save_capabilities(manifest)
        self.provider.accounts.save_diagnostics('one', {'route_verified':True,'login_verified':True})
        req={'model':'fixture-model','prompt':'cat','duration':6,'ratio':'16:9'}
        for category in ['普通账号','anything']:
            with self.assertRaises(ProviderError):self.provider.validate_request({**req,'account_category':category})
        self.assertEqual(self.provider.validate_request({**req,'account_category':'标准账号'})['account_category'],'标准账号')

    def test_expired_manifest_disables_generation_and_does_not_default_model(self):
        manifest = verified_manifest()
        self.provider.save_capabilities(manifest)
        self.provider.accounts.save_diagnostics('one', {'route_verified': True, 'login_verified': True})
        with self.assertRaises(ProviderError) as error:
            self.provider.validate_request({'prompt': 'cat', 'duration': 6})
        self.assertEqual(error.exception.code, 'unsupported_model')
        manifest['expires_at'] = time.time()-1
        (self.provider.root / 'capabilities.json').write_text(json.dumps(manifest))
        self.assertFalse(self.provider.capabilities()['enabled'])


class ResultAssociationTests(unittest.TestCase):
    def test_other_conversation_and_historical_media_are_excluded(self):
        payload = {'conversations': [
            {'conversation_id': 'old', 'messages': [{'id': 'old-1', 'main_url': 'https://cdn.example/old.mp4'}]},
            {'conversation_id': 'new', 'messages': [
                {'id': 'baseline', 'main_url': 'https://cdn.example/baseline.mp4'},
                {'id': 'new-1', 'role': 'assistant', 'content': '开始生成',
                 'video': {'fallback_api': 'https://cdn.example/new.mp4'}}]}]}
        result = evidence_from_payload(payload, 'new', {'baseline'}, {'https://cdn.example/baseline.mp4'})
        self.assertEqual(result['media'], ['https://cdn.example/new.mp4'])
        self.assertNotIn('old-1', result['messages'])

    def test_missing_conversation_scope_never_returns_first_captured_video(self):
        payload = {'messages': [{'id': '1', 'main_url': 'https://cdn.example/first.mp4'}]}
        self.assertEqual(evidence_from_payload(payload, 'expected', set(), set())['media'], [])

    def test_user_prompt_cannot_be_misread_as_platform_receipt(self):
        payload={'conversation_id':'new','messages':[{'id':'u','role':'user','content':'开始生成，无法生成也告诉我'}]}
        result=evidence_from_payload(payload,'new',set(),set())
        self.assertEqual(result['texts'],[])


class FixtureSession:
    """External browser boundary; provider state/rotation/quota remain real."""
    sends = []
    outcome = 'unknown'
    no_charge = True

    def __init__(self, *args, **kwargs):
        self.prepared = False

    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None
    async def baseline(self): return {'messages': ['old'], 'media': ['https://cdn.example/old.mp4']}
    async def prepare(self, request, capability, images): self.prepared = True
    async def capture_submission_page(self, snapshot): pass
    async def submit(self):
        if not self.prepared: raise AssertionError('unprepared')
        self.sends.append('send')
    async def observe(self, snapshot, capability, timeout):
        return {'phase': self.outcome, 'conversation_id': 'new-123', 'platform_reply': 'fixture receipt', 'media': [], 'no_charge':self.no_charge}


class GenerationSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_only_reply_finishes_work_but_preserves_billing_uncertainty_and_recovery(self):
        FixtureSession.outcome='text_only_ack'
        result=await self.provider.run_task(self.row)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['phase'],'text_only_ack')
        account=self.provider.list_accounts()[0]
        self.assertEqual(account['quota_reserved'],1)
        self.assertEqual(account['inflight_tasks'],0)
        self.assertFalse(self.provider.scheduler.recovery_reserved(self.provider.accounts.bound_proxy('one')))
        FixtureSession.outcome='accepted'
        recovered=await self.provider.inspect_task(self.row, download=False)
        self.assertEqual(recovered['phase'],'accepted')
        self.assertEqual(len(FixtureSession.sends),1)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = DoubaoProvider(root=Path(self.temp.name), enabled=True, session_factory=FixtureSession)
        self.provider.import_ck('one', ck(), PROXY)
        self.provider.accounts.save_diagnostics('one', {'route_verified': True, 'login_verified': True})
        self.provider.save_capabilities(verified_manifest())
        FixtureSession.sends = []
        FixtureSession.outcome = 'unknown'
        FixtureSession.no_charge = True
        self.row = {'id': 'video_'+'1'*32, 'prompt': 'cat', 'model': 'fixture-model', 'duration': 6, 'ratio': '16:9', 'platform': 'doubao'}

    async def test_unknown_submit_persists_snapshot_then_resume_does_not_send_again(self):
        result = await self.provider.run_task(self.row)
        self.assertEqual(result['phase'], 'submission_unknown')
        self.assertEqual(self.provider.list_accounts()[0]['send_count'], 1)
        self.assertEqual(self.provider.list_accounts()[0]['quota_reserved'], 1)
        self.provider.enabled = False
        resumed = await self.provider.resume_task(self.row)
        self.assertEqual(resumed['conversation_id'], 'new-123')
        self.assertEqual(len(FixtureSession.sends), 1)

    async def test_historical_cast_snapshot_still_polls_without_sending_prompt_again(self):
        await self.provider.run_task(self.row)
        snapshot = self.provider._read_snapshot(self.row['id'])
        snapshot['request']['cast'] = [{'name': '旧角色', 'image_indices': [1]}]
        self.provider._snapshot_path(self.row['id']).write_text(json.dumps(snapshot), encoding='utf-8')
        resumed = await self.provider.resume_task(self.row)
        self.assertEqual(resumed['conversation_id'], 'new-123')
        self.assertEqual(len(FixtureSession.sends), 1)

    async def test_explicit_rejection_ends_task_and_releases_local_reservation(self):
        FixtureSession.outcome = 'rejected'
        result = await self.provider.run_task(self.row)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['error_code'], 'platform_rejected')
        account = self.provider.list_accounts()[0]
        self.assertEqual(account['quota_used'], 0)
        self.assertEqual(account['quota_reserved'], 0)
        self.assertEqual(account['send_count'], 1)

    async def test_verification_timeout_releases_work_without_invalidating_account_or_assuming_charge(self):
        FixtureSession.outcome = 'verification_incomplete'
        result = await self.provider.run_task(self.row)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['phase'], 'verification_incomplete')
        account = self.provider.list_accounts()[0]
        self.assertEqual(account['inflight_tasks'], 0)
        self.assertEqual(account['quota_reserved'], 1)
        self.assertEqual(account['quota_used'], 0)
        self.assertTrue(account['enabled'])
        self.assertFalse(self.provider.scheduler.recovery_reserved(self.provider.accounts.bound_proxy('one')))
        await self.provider.resume_task(self.row)
        self.assertEqual(len(FixtureSession.sends), 1)

    async def test_explicit_inspection_rechecks_expired_verification_without_sending(self):
        FixtureSession.outcome = 'verification_incomplete'
        await self.provider.run_task(self.row)
        original_binding = self.provider.accounts.bound_proxy('one')
        seen = []
        class RecheckSession(FixtureSession):
            async def observe(inner, snapshot, capability, timeout):
                seen.append(dict(snapshot))
                self.assertFalse(snapshot['request'].get('auto_confirm'))
                self.assertTrue(snapshot['verification_pending'])
                self.assertEqual(snapshot['conversation_id'], 'new-123')
                self.assertGreater(snapshot['verification_deadline_at'], time.time())
                self.assertNotIn('status', snapshot)
                return {'phase':'accepted', 'conversation_id':'new-123', 'media':[]}
        self.provider.session_factory = RecheckSession
        unchanged = await self.provider.resume_task(self.row)
        self.assertEqual(unchanged['phase'], 'verification_incomplete')
        self.assertEqual(seen, [])
        result = await self.provider.inspect_task(self.row, download=False)
        self.assertEqual(result['phase'], 'accepted')
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(FixtureSession.sends), 1)
        self.assertEqual(self.provider.accounts.bound_proxy('one'), original_binding)
        self.assertEqual(self.provider.list_accounts()[0]['send_count'], 1)
        self.assertEqual(self.provider.list_accounts()[0]['quota_reserved'], 1)

    async def test_explicit_inspection_cannot_recover_verification_without_original_chat(self):
        FixtureSession.outcome = 'verification_incomplete'
        await self.provider.run_task(self.row)
        snapshot = self.provider._read_snapshot(self.row['id'])
        snapshot['conversation_id'] = None
        self.provider._snapshot_path(self.row['id']).write_text(json.dumps(snapshot), encoding='utf-8')
        result = await self.provider.inspect_task(self.row, download=False)
        self.assertEqual(result['phase'], 'verification_incomplete')
        self.assertEqual(len(FixtureSession.sends), 1)

    async def test_interrupted_verification_without_chat_id_is_settled_without_reopening_or_resending(self):
        await self.provider.run_task(self.row)
        snapshot = self.provider._read_snapshot(self.row['id'])
        snapshot.update(verification_pending=True, state='waiting_verification', conversation_id=None)
        self.provider._snapshot_path(self.row['id']).write_text(json.dumps(snapshot), encoding='utf-8')
        resumed = await self.provider.resume_task(self.row)
        self.assertEqual(resumed['phase'], 'verification_incomplete')
        self.assertEqual(resumed['status'], 'failed')
        self.assertEqual(self.provider.list_accounts()[0]['inflight_tasks'], 0)
        self.assertEqual(len(FixtureSession.sends), 1)

    async def test_manual_wait_is_visible_and_holds_original_account_and_route(self):
        provider = self.provider
        phases = []
        original = provider.accounts.bound_proxy('one')
        seen = []
        class WaitingSession(FixtureSession):
            def __init__(self, owner, account, **kwargs):
                super().__init__()
                seen.append(account['_interactive'])
            async def observe(self, snapshot, capability, timeout):
                await self.on_progress('waiting_verification', account='one', reason='manual')
                self_account = provider.list_accounts()[0]
                if self_account['inflight_tasks'] != 1: raise AssertionError('lost account reservation')
                if provider.accounts.bound_proxy('one') != original: raise AssertionError('route changed')
                return {'phase': 'verification_incomplete', 'reason': 'fixture timeout', 'no_charge': False}
        provider.session_factory = WaitingSession
        await provider.run_task(self.row, on_event=lambda event: phases.append(event['phase']))
        self.assertEqual(seen, [True])
        self.assertIn('waiting_verification', phases)
        self.assertEqual(len(FixtureSession.sends), 1)

    async def test_restart_with_original_chat_but_no_receipt_ends_as_verification_incomplete(self):
        await self.provider.run_task(self.row)
        snapshot = self.provider._read_snapshot(self.row['id'])
        snapshot.update(verification_pending=True, state='checking_verification')
        self.provider._snapshot_path(self.row['id']).write_text(json.dumps(snapshot), encoding='utf-8')
        resumed = await self.provider.resume_task(self.row)
        self.assertEqual(resumed['phase'], 'verification_incomplete')
        self.assertEqual(resumed['conversation_id'], 'new-123')
        self.assertEqual(len(FixtureSession.sends), 1)

    async def test_crash_after_mark_sent_never_submits_on_a_second_run(self):
        await self.provider.run_task(self.row)
        await self.provider.run_task(self.row)
        self.assertEqual(len(FixtureSession.sends), 1)

    async def test_inspection_never_resubmits_or_releases_unknown_quota(self):
        await self.provider.run_task(self.row)
        await self.provider.inspect_task(self.row, download=False)
        self.assertEqual(len(FixtureSession.sends), 1)
        self.assertEqual(self.provider.list_accounts()[0]['quota_reserved'], 1)

    async def test_rejection_without_no_charge_evidence_preserves_quota_but_finishes_generation(self):
        FixtureSession.outcome='rejected'
        FixtureSession.no_charge=False
        result=await self.provider.run_task(self.row)
        self.assertEqual(result['phase'],'platform_rejected')
        account=self.provider.list_accounts()[0]
        self.assertEqual(account['quota_reserved'],1)
        self.assertEqual(account['inflight_tasks'],0)
        self.assertFalse(self.provider.scheduler.recovery_reserved(self.provider.accounts.bound_proxy('one')))


if __name__ == '__main__': unittest.main()
