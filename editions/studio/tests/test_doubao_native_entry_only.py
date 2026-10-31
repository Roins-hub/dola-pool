"""The removed Skills UI may neither admit models nor recover old sends."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, AsyncMock

from providers.base import ProviderError
from providers.doubao import DoubaoProvider
from providers.doubao.capabilities import Capabilities
from providers.doubao.runtime import BrowserSession
from test_doubao_provider import ck, PROXY
from test_doubao_runtime import verified_manifest


class NativeEntryOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_environment_check_does_not_search_skills_when_native_video_is_absent(self):
        class Empty:
            async def count(self): return 0
            def filter(self, **kwargs): return self
        def role_query(role, **kwargs):
            if role not in ('dialog','button') or '技能' in str(kwargs.get('name','')) or 'Skills' in str(kwargs.get('name','')):
                self.fail('Skills discovery must not run')
            return Empty()
        page = SimpleNamespace(get_by_text=lambda *_args, **_kw: Empty(),
            locator=lambda *_args, **_kw: Empty(), evaluate=AsyncMock(return_value=[]),
            get_by_role=role_query,is_closed=lambda:False,url='https://www.doubao.com/chat/')
        response = SimpleNamespace(status=200, json=AsyncMock(return_value={'ip':'8.8.8.8'}), dispose=AsyncMock())
        session = BrowserSession(None, {})
        session.page = page
        session.context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=response)))
        session._logged_in = AsyncMock(return_value=(True, []))
        checks = await session.verify()
        self.assertFalse(checks['video_entry'])
        self.assertNotIn('observed_skills', checks)

    async def test_old_skills_diagnostics_cannot_schedule_a_calibration(self):
        with tempfile.TemporaryDirectory() as root:
            provider = DoubaoProvider(root=Path(root), enabled=False)
            provider.import_ck('one', ck(), PROXY)
            provider.accounts.save_diagnostics('one', {'route_verified':True, 'login_verified':True,
                'entry_mode':'work_skill', 'observed_skills':[{'id':'old-model','label':'Old','max_duration':30}]})
            with self.assertRaises(ProviderError) as error:
                provider.start_calibration('one', {'model':'old-model','prompt':'cat','duration':30,'ratio':'16:9'})
            self.assertEqual(error.exception.code, 'unsupported_entry')
            self.assertEqual(provider._calibration_jobs, {})

    async def test_skills_fields_and_skills_mode_are_rejected_even_with_native_selectors(self):
        for changes in ({'entry_mode': 'work_skill'}, {'skill': {'id': 'fixture-model', 'label': 'Fixture', 'max_duration': 30}}):
            manifest = verified_manifest()
            manifest['models'][0].update(changes)
            with self.assertRaises(ProviderError):
                Capabilities.validate(manifest)

    async def test_runtime_rejects_removed_entry_before_touching_any_page_controls(self):
        session = BrowserSession(None, {})
        session.page = SimpleNamespace(get_by_role=lambda *_args, **_kw: self.fail('Removed Skills UI was consulted'))
        with self.assertRaises(ProviderError) as error:
            await session.prepare({'prompt': 'cat', 'duration': 30, 'ratio': '16:9', 'model': 'old-model'},
                                  {'entry_mode': 'work_skill', 'skill': {}, 'label': 'Old'}, [])
        self.assertEqual(error.exception.code, 'unsupported_entry')

    async def test_removed_cast_cannot_prepare_or_resubmit_a_historical_prompt(self):
        session = BrowserSession(None, {})
        session.page = SimpleNamespace(locator=lambda *_args, **_kw: self.fail('Page controls were touched'))
        for key in ('cast', 'cast_ids'):
            with self.subTest(key=key):
                with self.assertRaises(ProviderError) as error:
                    await session.prepare({'prompt': 'cat', 'duration': 6, 'ratio': '16:9',
                                           'model': 'fixture-model', key: ['old-role-id']},
                                          verified_manifest()['models'][0], [])
                self.assertEqual(error.exception.code, 'unsupported_cast')

    async def test_old_unknown_task_is_not_opened_or_resent_through_another_entry(self):
        with tempfile.TemporaryDirectory() as root:
            factory = Mock(side_effect=AssertionError('A browser must not be opened'))
            provider = DoubaoProvider(root=Path(root), enabled=False, session_factory=factory)
            account = provider.import_ck('one', ck(), PROXY)
            task = 'video_'+'a'*32
            snapshot = {'id': task, 'platform': 'doubao', 'state': 'unknown', 'sent': True,
                'account': 'one', 'account_id': account['account_id'],
                'binding_digest': provider._binding_digest(provider.accounts.bound_proxy('one')),
                'capability': {'entry_mode': 'work_skill', 'skill': {'id': 'old-model'}},
                'request': {'model': 'old-model', 'duration': 30}}
            path = provider._snapshot_path(task)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(snapshot), encoding='utf-8')
            with self.assertRaises(ProviderError) as error:
                await provider.resume_task({'id': task})
            self.assertEqual(error.exception.code, 'unsupported_entry')
            self.assertEqual(path.read_text(encoding='utf-8'), json.dumps(snapshot))
            factory.assert_not_called()

    async def test_removed_entry_does_not_hold_a_lane_recovery_forever_or_clear_account_fence(self):
        with tempfile.TemporaryDirectory() as root:
            provider = DoubaoProvider(root=Path(root), enabled=False)
            account = provider.import_ck('one', ck(), PROXY)
            task = 'video_'+'c'*32
            provider.accounts.reserve(task, 'one')
            provider.accounts.mark_sent(task)
            provider.accounts.settle(task, 'unknown')
            snapshot = {'id': task, 'platform': 'doubao', 'state': 'unknown', 'sent': True,
                'account': 'one', 'account_id': account['account_id'],
                'binding_digest': provider._binding_digest(provider.accounts.bound_proxy('one')),
                'capability': {'entry_mode': 'work_skill', 'skill': {'id': 'old-model'}}}
            path = provider._snapshot_path(task)
            path.parent.mkdir(parents=True)
            raw = json.dumps(snapshot)
            path.write_text(raw, encoding='utf-8')
            provider.reserve_recoveries([{'id': task, 'account': 'one'}])
            self.assertFalse(provider.scheduler.recovery_reserved(provider.accounts.bound_proxy('one')))
            self.assertEqual(provider.list_accounts()[0]['inflight_tasks'], 1)
            self.assertEqual(path.read_text(encoding='utf-8'), raw)

    async def test_completed_historical_file_metadata_remains_readable_without_a_browser(self):
        with tempfile.TemporaryDirectory() as root:
            provider = DoubaoProvider(root=Path(root), enabled=False)
            task = 'video_'+'b'*32
            snapshot = {'id': task, 'platform': 'doubao', 'state': 'completed', 'status': 'completed',
                'sent': True, 'output_path': 'existing.mp4', 'phase': 'completed',
                'capability': {'entry_mode': 'work_skill'}}
            path = provider._snapshot_path(task)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(snapshot), encoding='utf-8')
            result = await provider.resume_task({'id': task})
            self.assertEqual(result['output_path'], 'existing.mp4')


if __name__ == '__main__':
    unittest.main()
