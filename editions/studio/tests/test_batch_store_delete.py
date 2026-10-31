import unittest
import time
from store import TaskStore, TaskQuotaExceeded, PendingTaskLimitExceeded


def task(number, **overrides):
    return dict(id='child-' + str(number), model='seedance-2.0', prompt='原文', ratio='16:9',
                duration=30, duration_profile='plugin-expert30', **overrides)


class BatchStoreTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(':memory:')

    def tearDown(self):
        self.store._conn.close()

    def test_bulk_creation_uses_all_member_slots_and_is_atomic_when_limit_is_short(self):
        with self.assertRaises(TaskQuotaExceeded):
            self.store.create_batch('batch-1', [task(i) for i in range(3)],
                                    api_key_hash='key', daily_limit=2)
        self.assertEqual(self.store.recent_tasks(platform='all'), [])
        self.assertIsNone(self.store.get_batch('batch-1'))
        batch = self.store.create_batch('batch-1', [task(i) for i in range(2)],
                                       api_key_hash='key', daily_limit=2)
        self.assertEqual(batch['requested_count'], 2)
        self.assertEqual([t['batch_index'] for t in batch['tasks']], [1, 2])
        self.assertEqual(self.store.key_usage('key')['total'], 2)

    def test_pending_capacity_rejects_entire_batch(self):
        self.store.create('existing', 'seedance-2.0', 'x', '16:9', 10)
        with self.assertRaises(PendingTaskLimitExceeded):
            self.store.create_batch('batch-1', [task(1), task(2)], max_pending=2)
        self.assertIsNone(self.store.get('child-1'))
        self.assertEqual(self.store.pending_task_count(), 1)

    def test_batch_replay_preserves_children_even_after_one_is_deleted(self):
        identity = dict(idempotency_scope='api:key', idempotency_key='intent', request_fingerprint='body')
        original = self.store.create_batch('first', [task(1), task(2)], **identity)
        self.store.update('child-1', status='completed')
        self.store.soft_delete('child-1', 'admin')
        replay = self.store.create_batch('second', [task(3), task(4)], **identity)
        self.assertEqual(replay['batch_id'], original['batch_id'])
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['tasks'][0]['id'], 'child-1')
        self.assertTrue(replay['tasks'][0]['deleted_at'])
        self.assertIsNone(self.store.get('child-3'))
        with self.assertRaises(ValueError):
            self.store.create_batch('third', [task(3), task(4)], **{**identity, 'request_fingerprint': 'changed'})

    def test_invalid_batch_count_has_no_side_effects(self):
        for rows in ([], [task(i) for i in range(21)]):
            with self.assertRaises(ValueError):
                self.store.create_batch('invalid', rows)
        with self.assertRaises(ValueError):
            self.store.create_batch('duplicate', [task(1), task(1)])
        self.assertEqual(self.store.recent_tasks(platform='all'), [])

    def test_clip_merge_retains_identity_order_and_counts_two_results_for_one_request(self):
        self.store.create_batch('batch', [task(1), task(2)])
        self.store.merge_clips('child-1', [dict(clip_id='vid-a', vid='a', status='completed', actual_duration=15.1, video_url='/videos/a.mp4')])
        self.store.merge_clips('child-1', [dict(clip_id='vid-b', vid='b', status='downloading')])
        row = self.store.merge_clips('child-1', [dict(clip_id='vid-b', vid='b', status='completed', actual_duration=15.1, video_url='/videos/b.mp4')])
        self.assertEqual([c['clip_id'] for c in row['clips']], ['vid-a', 'vid-b'])
        self.assertEqual([c['index'] for c in row['clips']], [1, 2])
        self.assertAlmostEqual(row['total_actual_duration'], 30.2)
        self.assertIsNone(row['actual_duration'])
        self.store.update('child-1', conversation_id='conv', status='completed')
        self.store.soft_delete('child-1', 'admin')
        batch = self.store.get_batch('batch')
        self.assertEqual(batch['counts']['requested'], 2)
        self.assertEqual(batch['counts']['conversations'], 1)
        self.assertEqual(batch['counts']['completed'], 1)
        self.assertEqual(batch['counts']['videos'], 2)
        with self.assertRaises(ValueError):
            self.store.merge_clips('child-1', [dict(clip_id='vid-c')])

    def test_soft_delete_hides_rows_but_preserves_usage_identity_and_restore_does_not_queue(self):
        identity = dict(idempotency_scope='api:key', idempotency_key='intent', request_fingerprint='body')
        self.store.create('one', 'seedance-2.0', 'x', '16:9', 10, api_key_hash='key', **identity)
        self.store.update('one', status='failed', conversation_id='conv', failure_code='query_timeout')
        self.store.soft_delete('one', 'user:1')
        self.assertIsNone(self.store.get('one'))
        self.assertIsNone(self.store.get_for_client('one', 'key'))
        self.assertEqual(self.store.recent_tasks(api_key_hash='key'), [])
        self.assertEqual(self.store.key_usage('key')['total'], 1)
        replay = self.store.find_idempotent('api:key', 'intent', 'body')
        self.assertEqual(replay['id'], 'one')
        self.assertTrue(replay['deleted_at'])
        self.store.update('one', status='processing')
        self.assertEqual(self.store.get('one', include_deleted=True)['status'], 'failed')
        restored = self.store.restore('one', 'user:1')
        self.assertEqual(restored['status'], 'failed')
        self.assertIsNone(restored['deleted_at'])
        self.assertEqual(self.store.key_usage('key')['total'], 1)

    def test_active_or_non_terminal_task_cannot_be_deleted(self):
        self.store.create('one', 'seedance-2.0', 'x', '16:9', 10)
        with self.assertRaises(ValueError): self.store.soft_delete('one', 'admin')
        self.store.update('one', status='failed')
        with self.assertRaises(ValueError): self.store.soft_delete('one', 'admin', active=True)
        self.assertIsNotNone(self.store.get('one'))

    def test_uncertain_expert_or_batch_send_is_never_requeued_after_restart(self):
        self.store.create_batch('batch', [task(1)])
        self.store.update('child-1', status='processing', account='account', phase='sending')
        self.store.fail_unconfirmed_submissions()
        row = self.store.get('child-1')
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(row['account'], 'account')
        self.assertEqual(row['failure_code'], 'submission_unknown')
        self.assertEqual(self.store.recoverable_queued_tasks(), [])

    def test_paused_batch_is_not_automatically_requeued_and_can_be_explicitly_continued(self):
        self.store.create_batch('batch', [task(1), task(2)])
        self.store.set_batch_state('batch', 'paused', error='核对上次发送')
        batch = self.store.get_batch('batch')
        self.assertEqual((batch['status'], batch['reason']), ('paused', '核对上次发送'))
        self.assertEqual(self.store.recoverable_queued_tasks(), [])
        self.store.set_batch_state('batch', 'queued')
        self.assertEqual(len(self.store.recoverable_queued_tasks()), 2)

    def test_quota_wait_state_preserves_queued_children_and_reason(self):
        self.store.create_batch('batch', [task(1)])
        self.store.set_batch_state('batch', 'waiting_quota', error='等待专家周额度')
        batch = self.store.get_batch('batch')
        self.assertEqual(batch['status'], 'waiting_quota')
        self.assertEqual(batch['reason'], '等待专家周额度')
        self.assertEqual(self.store.get('child-1')['status'], 'queued')

    def test_all_failed_batch_is_failed_not_partial_completion(self):
        self.store.create_batch('batch', [task(1), task(2)])
        for identifier in ('child-1', 'child-2'):
            self.store.update(identifier, status='failed')
        self.assertEqual(self.store.get_batch('batch')['status'], 'failed')

    def test_second_resolver_of_same_vid_updates_one_clip_without_renumbering(self):
        self.store.create('one', 'seedance-2.0', 'x', '16:9', 30)
        self.store.merge_clips('one', [dict(clip_id='first-path',vid='v1',status='processing')])
        row = self.store.merge_clips('one', [dict(clip_id='other-path',vid='v1',status='completed',video_url='/videos/one.mp4')])
        self.assertEqual(len(row['clips']), 1)
        self.assertEqual(row['clips'][0]['clip_id'], 'first-path')
        self.assertEqual(row['clips'][0]['status'], 'completed')

    def test_waiting_quota_is_not_killed_by_queue_watchdog_but_can_resume_after_restart(self):
        self.store.create_batch('batch', [task(1)])
        self.store.update('child-1', phase='waiting_quota')
        self.assertEqual(self.store.stalled_queued_tasks(time.time()+1), [])
        self.assertEqual(len(self.store.recoverable_queued_tasks()), 1)
        self.store.update('child-1', phase='queued')
        self.store.set_batch_state('batch', 'waiting_quota')
        self.assertEqual(self.store.stalled_queued_tasks(time.time()+1), [])

    def test_terminal_batch_does_not_keep_an_obsolete_pause_label(self):
        self.store.create_batch('batch', [task(1)])
        self.store.update('child-1', status='failed')
        self.store.set_batch_state('batch','paused',error='last item failed')
        self.assertEqual(self.store.get_batch('batch')['status'],'failed')

    def test_aged_batch_children_wait_for_coordinator_without_queue_watchdog_expiry(self):
        self.store.create_batch('batch', [task(1), task(2), task(3)])
        old = time.time() - 86400
        for identity in ('child-1', 'child-2', 'child-3'):
            self.store.update(identity, created_at=old)
        self.store.update('child-1', status='processing', conversation_id='confirmed', started_at=time.time())
        self.store.update('child-3', started_at=old)
        self.store.set_batch_state('batch', 'processing')
        self.store.create('legacy', 'seedance-2.0', 'legacy', '16:9', 10)
        self.store.update('legacy', created_at=old)
        expired = {row['id'] for row in self.store.stalled_queued_tasks(time.time()-60)}
        self.assertEqual(expired, {'child-3', 'legacy'})
        self.assertEqual(self.store.get('child-2')['status'], 'queued')


if __name__ == '__main__': unittest.main()
