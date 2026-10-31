from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from store import TaskStore
from providers.doubao import DoubaoProvider
from test_doubao_provider import ck


class UnknownDeadlineTests(unittest.TestCase):
    def setUp(self):
        self.store=TaskStore(':memory:'); self.addCleanup(self.store._conn.close)

    def task(self, name, platform='doubao', phase='submission_unknown', deadline=100, status='processing'):
        self.store.create(name,'model','cat','16:9',15,platform=platform)
        self.store.update(name,status=status,phase=phase,deadline_at=deadline,account='one',conversation_id='original-chat')

    def test_only_overdue_doubao_unknown_becomes_timeout_and_keeps_original_recovery(self):
        self.task('expired')
        self.task('future',deadline=300)
        self.task('accepted',phase='accepted')
        self.task('completed',status='completed')
        self.task('dola',platform='dola')
        self.assertTrue(hasattr(self.store,'expire_unknown_platform_tasks'),'Unknown submissions have no deadline sweep')
        rows=self.store.expire_unknown_platform_tasks(200)
        self.assertEqual([r['id'] for r in rows],['expired'])
        row=self.store.get('expired')
        self.assertEqual((row['status'],row['phase'],row['failure_code']),('failed','query_timeout','query_timeout'))
        self.assertEqual((row['account'],row['conversation_id']),('one','original-chat'))
        self.assertEqual(self.store.get('future')['status'],'processing')
        self.assertEqual(self.store.get('accepted')['status'],'processing')
        self.assertEqual(self.store.get('completed')['status'],'completed')
        self.assertEqual(self.store.get('dola')['status'],'processing')
        self.assertEqual(self.store.expire_unknown_platform_tasks(200),[])

    def test_timeout_releases_only_route_recovery_and_keeps_uncertain_account_charge(self):
        import platform_runtime
        self.assertTrue(hasattr(platform_runtime,'expire_unknown_platform_tasks'),'No runtime deadline cleanup hook')
        with tempfile.TemporaryDirectory() as temp:
            provider=DoubaoProvider(root=Path(temp),enabled=False)
            provider.import_ck('one',ck(),'direct')
            provider.accounts.reserve('expired','one');provider.accounts.mark_sent('expired')
            provider.accounts.settle('expired','unknown')
            before=provider.list_accounts()[0]
            provider.scheduler.reserve_recovery('doubao','one',{'server':'direct'})
            self.task('expired')
            rows=platform_runtime.expire_unknown_platform_tasks({'store':self.store,'doubao_provider':provider},now=200)
            self.assertEqual([r['id'] for r in rows],['expired'])
            self.assertFalse(provider.scheduler.recovery_reserved({'server':'direct'}))
            self.assertEqual(provider.list_accounts()[0],before)

    def test_manual_verification_progress_is_persisted_and_not_expired_as_unknown(self):
        from platform_runtime import persist_event
        self.task('manual')
        for phase in ('checking_verification', 'waiting_verification', 'verification_continue'):
            persist_event({'store':self.store}, 'manual', {'phase':phase,'account':'one','reason':'manual'})
            self.assertEqual(self.store.get('manual')['status'], 'processing')
            self.assertEqual(self.store.get('manual')['phase'], phase)
            self.assertEqual(self.store.expire_unknown_platform_tasks(200), [])


if __name__=='__main__':unittest.main()
