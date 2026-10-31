"""Count requests, preserve uncertain effects, and keep daily/weekly pools independent."""
import importlib
import importlib.util
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


class DualQuotaLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'quota.db')
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.now = 1_800_000_000.0

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def ledger(self, conn=None):
        self.assertIsNotNone(importlib.util.find_spec('quota_ledger'), 'Missing durable independent quota ledger')
        return importlib.import_module('quota_ledger').QuotaLedger(conn or self.conn, timezone=timezone.utc)

    def test_daily_one_and_weekly_four_are_independent_request_caps(self):
        ledger = self.ledger()
        self.assertTrue(ledger.reserve('a', 'ordinary_fast10', 'daily', now=self.now))
        ledger.confirm('a', 'ordinary_fast10', 'daily', 'daily-cid', now=self.now)
        self.assertFalse(ledger.reserve('a', 'ordinary_fast10', 'daily-second', now=self.now))
        self.assertEqual(ledger.snapshot('a', 'expert_fast', now=self.now)['remaining'], 4)
        for i in range(4):
            self.assertTrue(ledger.reserve('a', 'expert_fast', 'expert-'+str(i), now=self.now))
            ledger.confirm('a', 'expert_fast', 'expert-'+str(i), 'expert-cid-'+str(i), now=self.now)
            # The second result and recovery of the same conversation do not cost another request.
            ledger.confirm('a', 'expert_fast', 'expert-'+str(i), 'expert-cid-'+str(i), now=self.now)
        self.assertFalse(ledger.reserve('a', 'expert_fast', 'expert-fifth', now=self.now))
        self.assertEqual(ledger.snapshot('a', 'expert_fast', now=self.now)['used'], 4)

    def test_snapshots_describe_fixed_daily_and_rolling_weekly_rules(self):
        ledger = self.ledger()
        ordinary = ledger.snapshot('a', 'ordinary_fast10', now=self.now)
        expert = ledger.snapshot('a', 'expert_fast', now=self.now)
        self.assertEqual((ordinary['limit'], ordinary['period'], ordinary.get('timezone'),
                          ordinary.get('reset_hour')), (1, 'day', 'Asia/Shanghai', 1))
        self.assertEqual((expert['limit'], expert['period'], expert.get('window_days')),
                         (4, 'rolling_7_days', 7))

    def test_weekly_does_not_reset_at_next_daily_boundary(self):
        ledger = self.ledger()
        for i in range(4):
            ledger.reserve('a', 'expert_fast', 'r'+str(i), now=self.now+i)
            ledger.mark_sent('a', 'expert_fast', 'r'+str(i), now=self.now+i)
        self.assertEqual(ledger.snapshot('a', 'expert_fast', now=self.now+86400)['remaining'], 0)
        self.assertEqual(ledger.snapshot('a', 'expert_fast', now=self.now+7*86400)['remaining'], 1)
        self.assertEqual(ledger.snapshot('a', 'expert_fast', now=self.now+7*86400+3)['remaining'], 4)

    def test_unknown_send_survives_restart_and_no_evidence_cannot_release(self):
        ledger = self.ledger()
        ledger.reserve('a', 'expert_fast', 'intent', now=self.now)
        ledger.mark_sent('a', 'expert_fast', 'intent', now=self.now)
        self.assertFalse(ledger.release('a', 'expert_fast', 'intent', proven_unconsumed=False))
        self.conn.close()
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        restored = self.ledger()
        result = restored.snapshot('a', 'expert_fast', now=self.now+10)
        self.assertEqual((result['uncertain'], result['remaining']), (1, 3))
        restored.confirm('a', 'expert_fast', 'intent', 'original-cid', now=self.now+10)
        self.assertEqual(restored.snapshot('a', 'expert_fast', now=self.now+10)['used'], 1)

    def test_preparation_failure_releases_but_confirmed_send_requires_proof(self):
        ledger = self.ledger()
        ledger.reserve('a', 'ordinary_fast10', 'before-send', now=self.now)
        self.assertTrue(ledger.release('a', 'ordinary_fast10', 'before-send', proven_unconsumed=False))
        self.assertEqual(ledger.snapshot('a', 'ordinary_fast10', now=self.now)['remaining'], 1)
        ledger.reserve('a', 'expert_fast', 'sent', now=self.now)
        ledger.confirm('a', 'expert_fast', 'sent', 'cid', now=self.now)
        self.assertFalse(ledger.release('a', 'expert_fast', 'sent', proven_unconsumed=False))
        self.assertTrue(ledger.release('a', 'expert_fast', 'sent', proven_unconsumed=True))
        self.assertEqual(ledger.snapshot('a', 'expert_fast', now=self.now)['remaining'], 4)

    def test_two_connections_cannot_reserve_same_last_capacity(self):
        ledger = self.ledger()
        for i in range(3): ledger.reserve('a', 'expert_fast', 'r'+str(i), now=self.now)
        other = sqlite3.connect(self.path)
        other.row_factory = sqlite3.Row
        try:
            second = self.ledger(other)
            self.assertTrue(ledger.reserve('a', 'expert_fast', 'fourth', now=self.now))
            self.assertFalse(second.reserve('a', 'expert_fast', 'fifth', now=self.now))
            self.assertTrue(second.reserve('a', 'expert_fast', 'fourth', now=self.now))
            self.assertEqual(second.snapshot('a', 'expert_fast', now=self.now)['reserved'], 4)
        finally:
            other.close()

    def test_route_block_does_not_block_other_pool_or_expire_next_day(self):
        ledger = self.ledger()
        ledger.block('a', 'expert_fast', self.now+7*86400, 'weekly response', now=self.now)
        self.assertFalse(ledger.reserve('a', 'expert_fast', 'blocked', now=self.now+86400))
        self.assertTrue(ledger.reserve('a', 'ordinary_fast10', 'allowed', now=self.now+86400))
        self.assertTrue(ledger.snapshot('a', 'expert_fast', now=self.now+86400)['blocked'])

    def test_recovered_conversation_is_one_charge_even_with_different_task_identity(self):
        ledger = self.ledger()
        ledger.confirm('a', 'expert_fast', 'original', 'same-cid', now=self.now)
        ledger.confirm('a', 'expert_fast', 'recovered', 'same-cid', now=self.now)
        self.assertEqual(ledger.snapshot('a', 'expert_fast', now=self.now)['used'], 1)

    def test_legacy_unknown_usage_is_not_zeroed_or_charged_again_for_same_conversation(self):
        from datetime import datetime
        day=datetime.fromtimestamp(self.now,timezone.utc).date().isoformat()
        self.conn.execute('CREATE TABLE usage(account TEXT,day TEXT,used INTEGER)')
        self.conn.execute('CREATE TABLE accepted_videos(conversation_id TEXT PRIMARY KEY,account TEXT,day TEXT)')
        self.conn.execute('INSERT INTO usage VALUES (?,?,2)',('a',day))
        self.conn.execute('INSERT INTO accepted_videos VALUES (?,?,?)',('existing-cid','a',day))
        self.conn.commit()
        ledger=self.ledger()
        ledger.migrate_legacy(now=self.now)
        self.assertEqual(ledger.snapshot('a','expert_fast',now=self.now)['remaining'],2)
        self.assertEqual(ledger.snapshot('a','ordinary_fast10',now=self.now)['remaining'],0)
        ledger.confirm('a','expert_fast','recovered','existing-cid',now=self.now)
        self.assertEqual(ledger.snapshot('a','expert_fast',now=self.now)['occupied'],2)
        ledger.migrate_legacy(now=self.now+60)
        self.assertEqual(ledger.snapshot('a','expert_fast',now=self.now+60)['occupied'],2)

    def test_weekly_platform_reset_timestamp_is_parsed_from_observed_reply(self):
        ledger=self.ledger()
        module=importlib.import_module('quota_ledger')
        self.assertTrue(hasattr(module,'reset_at_from_reply'))
        reply='Du hast das kostenlose 7-Tage-Limit erreicht. Komm am 1. November 2026, 16:33 (GMT+08:00) wieder oder chatte im Fast-Modus.'
        from datetime import datetime,timedelta
        expected=datetime(2026,11,1,16,33,tzinfo=timezone(timedelta(hours=8))).timestamp()
        self.assertEqual(module.reset_at_from_reply(reply),expected)

    def test_empty_weekly_snapshot_has_no_fabricated_moving_reset_window(self):
        ledger=self.ledger()
        self.assertEqual(ledger.snapshot('a','expert_fast',now=self.now),
                         ledger.snapshot('a','expert_fast',now=self.now+1))

    def test_daily_capacity_resets_at_beijing_one_am_instead_of_midnight(self):
        ledger = self.ledger()  # The legacy migration timezone is deliberately UTC.
        start = datetime.fromisoformat('2026-10-31T01:00:00+08:00').timestamp()
        reset = datetime.fromisoformat('2026-11-01T01:00:00+08:00').timestamp()
        ledger.confirm('a', 'ordinary_fast10', 'daily', 'daily-cid', now=start)
        for moment in (
            '2026-10-31T23:59:59+08:00',
            '2026-11-01T00:00:00+08:00',
            '2026-11-01T00:59:59.999+08:00',
        ):
            with self.subTest(moment=moment):
                snapshot = ledger.snapshot('a', 'ordinary_fast10',
                    now=datetime.fromisoformat(moment).timestamp())
                self.assertEqual(snapshot['remaining'], 0)
                self.assertEqual((snapshot['window_start'], snapshot['window_end']), (start, reset))
                self.assertEqual(snapshot['next_available_at'], reset)
        self.assertEqual(ledger.snapshot('a', 'ordinary_fast10', now=reset)['remaining'], 1)
        self.assertTrue(ledger.reserve('a', 'ordinary_fast10', 'next-day', now=reset))
        self.assertEqual(ledger.snapshot('a', 'ordinary_fast10', now=reset)['remaining'], 0)

    def test_daily_default_block_lifts_at_beijing_one_am(self):
        ledger = self.ledger()
        now = datetime.fromisoformat('2026-11-01T00:30:00+08:00').timestamp()
        reset = datetime.fromisoformat('2026-11-01T01:00:00+08:00').timestamp()
        ledger.block('a', 'ordinary_fast10', None, 'current daily limit', now=now)
        self.assertEqual(ledger.snapshot('a', 'ordinary_fast10', now=now)['next_available_at'], reset)
        self.assertFalse(ledger.reserve('a', 'ordinary_fast10', 'before-reset', now=reset-.001))
        self.assertTrue(ledger.reserve('a', 'ordinary_fast10', 'at-reset', now=reset))

    def test_daily_explicit_platform_block_survives_the_local_reset(self):
        ledger = self.ledger()
        now = datetime.fromisoformat('2026-11-01T00:30:00+08:00').timestamp()
        local_reset = datetime.fromisoformat('2026-11-01T01:00:00+08:00').timestamp()
        platform_reset = datetime.fromisoformat('2026-11-01T07:00:00+08:00').timestamp()
        ledger.block('a', 'ordinary_fast10', platform_reset, 'explicit platform reset', now=now)
        snapshot = ledger.snapshot('a', 'ordinary_fast10', now=local_reset)
        self.assertEqual(snapshot['remaining'], 1)
        self.assertEqual(snapshot['next_available_at'], platform_reset)
        self.assertFalse(ledger.reserve('a', 'ordinary_fast10', 'still-blocked', now=local_reset))
        self.assertTrue(ledger.reserve('a', 'ordinary_fast10', 'after-platform-reset', now=platform_reset))

    def test_weekly_refunds_each_use_at_its_own_send_time_after_exactly_168_hours(self):
        ledger = self.ledger()
        first = datetime.fromisoformat('2026-10-31T13:25:16+08:00').timestamp()
        uses = (first, first+3600, first+86400, first+2*86400)
        for index, sent_at in enumerate(uses):
            identity = 'expert-' + str(index)
            self.assertTrue(ledger.reserve('a', 'expert_fast', identity, now=sent_at-60))
            ledger.mark_sent('a', 'expert_fast', identity, now=sent_at)
            ledger.confirm('a', 'expert_fast', identity, identity, now=sent_at+30)
        refund = first + 168*3600
        snapshot = ledger.snapshot('a', 'expert_fast', now=refund-.001)
        self.assertEqual(snapshot['remaining'], 0)
        self.assertEqual(snapshot['next_available_at'], refund)
        self.assertEqual(ledger.snapshot('a', 'expert_fast', now=refund)['remaining'], 1)
        self.assertEqual(ledger.snapshot('a', 'expert_fast', now=refund)['reset_at'], uses[1]+168*3600)
        self.assertEqual(ledger.snapshot('a', 'expert_fast', now=uses[1]+168*3600)['remaining'], 2)
        self.assertEqual(ledger.snapshot('a', 'expert_fast', now=uses[-1]+168*3600)['remaining'], 4)

    def test_next_available_uses_block_expiry_when_capacity_is_already_free(self):
        ledger = self.ledger()
        ledger.confirm('a', 'expert_fast', 'one', 'one-cid', now=self.now)
        unblock = self.now + 3600
        ledger.block('a', 'expert_fast', unblock, 'temporary platform block', now=self.now)
        self.assertEqual(ledger.snapshot('a', 'expert_fast', now=self.now)['next_available_at'], unblock)
        self.assertTrue(ledger.reserve('a', 'expert_fast', 'two', now=unblock))

    def test_over_limit_legacy_weekly_usage_waits_until_a_slot_is_really_free(self):
        ledger = self.ledger()
        # Legacy aggregates may exceed today's configured capacity; keep every use.
        for index in range(5):
            ledger.confirm('a', 'expert_fast', str(index), str(index), now=self.now+index)
        snapshot = ledger.snapshot('a', 'expert_fast', now=self.now+10)
        self.assertEqual(snapshot['reset_at'], self.now+168*3600)
        self.assertEqual(snapshot['next_available_at'], self.now+1+168*3600)
        self.assertFalse(ledger.reserve('a', 'expert_fast', 'new', now=self.now+168*3600))
        self.assertTrue(ledger.reserve('a', 'expert_fast', 'new', now=self.now+1+168*3600))


class LegacyClassificationTests(unittest.TestCase):
    def setUp(self):
        from datetime import datetime
        self.tmp=tempfile.TemporaryDirectory()
        self.path=str(Path(self.tmp.name)/'legacy.db')
        self.conn=sqlite3.connect(self.path);self.conn.row_factory=sqlite3.Row
        self.now=1_800_000_000.0
        self.day=datetime.fromtimestamp(self.now,timezone.utc).date().isoformat()
        self.conn.execute('CREATE TABLE usage(account TEXT,day TEXT,used INTEGER,PRIMARY KEY(account,day))')
        self.conn.execute('CREATE TABLE accepted_videos(conversation_id TEXT PRIMARY KEY,account TEXT,day TEXT)')
        self.conn.execute('INSERT INTO usage VALUES (?,?,3)',('a',self.day))
        self.conn.executemany('INSERT INTO accepted_videos VALUES (?,?,?)',[(cid,'a',self.day) for cid in ('expert','other-model','unknown')])
        self.conn.commit()
        self.ledger=importlib.import_module('quota_ledger').QuotaLedger(self.conn,timezone=timezone.utc)
        self.ledger.migrate_legacy(now=self.now)

    def tearDown(self):
        self.conn.close();self.tmp.cleanup()

    def classify(self,rows):
        self.assertTrue(hasattr(self.ledger,'classify_legacy'),'Known historical routes need an exact-conversation migration')
        return self.ledger.classify_legacy(rows,now=self.now)

    def rows(self):
        return [dict(id='old-expert',account='a',conversation_id='expert',model='seedance-2.0',
                     duration=30,duration_profile='plugin-expert30',accepted_at=self.now-30,
                     clips=[{'clip_id':'first'},{'clip_id':'second'}]),
                dict(id='old25',account='a',conversation_id='other-model',model='seedance-2.5',
                     duration=30,duration_profile=None,started_at=self.now-60),
                dict(id='unknown-task',account='a',conversation_id='unknown')]

    def test_known_double_clip_expert_is_one_weekly_unit_and_unknown_stays_reserved(self):
        self.classify(self.rows())
        expert=self.ledger.snapshot('a','expert_fast',now=self.now)
        daily=self.ledger.snapshot('a','ordinary_fast10',now=self.now)
        self.assertEqual((expert['used'],expert['uncertain'],expert['occupied']),(1,1,2))
        self.assertEqual((daily['used'],daily['uncertain'],daily['occupied']),(0,1,1))
        self.assertEqual(self.conn.execute('SELECT used FROM usage').fetchone()[0],3)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM accepted_videos').fetchone()[0],3)

    def test_missing_or_mismatched_identity_never_clears_unknown_history(self):
        rows=[dict(account='wrong-account',conversation_id='expert',model='seedance-2.5',duration=30),
              dict(account='a',conversation_id='not-in-accepted',model='seedance-2.5',duration=30),
              dict(account='a',conversation_id='unknown',model='seedance-2.5')]
        self.classify(rows)
        self.assertEqual(self.ledger.snapshot('a','expert_fast',now=self.now)['uncertain'],3)
        self.assertEqual(self.ledger.snapshot('a','ordinary_fast10',now=self.now)['uncertain'],3)

    def test_duplicate_task_rows_and_restart_never_consume_another_unknown_slot(self):
        rows=self.rows()
        self.classify(rows+[dict(rows[0],id='duplicate-task')])
        before={kind:self.ledger.snapshot('a',kind,now=self.now) for kind in ('expert_fast','ordinary_fast10')}
        self.conn.close()
        self.conn=sqlite3.connect(self.path);self.conn.row_factory=sqlite3.Row
        self.ledger=importlib.import_module('quota_ledger').QuotaLedger(self.conn,timezone=timezone.utc)
        self.ledger.migrate_legacy(now=self.now)
        self.classify(rows)
        after={kind:self.ledger.snapshot('a',kind,now=self.now) for kind in ('expert_fast','ordinary_fast10')}
        self.assertEqual(after,before)

    def test_ordinary_fast10_moves_only_its_own_legacy_unit_to_daily_used(self):
        rows=[dict(account='a',conversation_id='expert',model='seedance-2.0',duration=10,duration_profile=None,accepted_at=self.now-5)]
        self.classify(rows)
        daily=self.ledger.snapshot('a','ordinary_fast10',now=self.now)
        expert=self.ledger.snapshot('a','expert_fast',now=self.now)
        self.assertEqual((daily['used'],daily['uncertain']),(1,2))
        self.assertEqual((expert['used'],expert['uncertain']),(0,2))

    def test_conflicting_model_metadata_for_one_conversation_leaves_its_unknown_slot(self):
        rows=self.rows()[:1]
        rows += [dict(rows[0],id='conflicting',model='seedance-2.5')]
        self.classify(rows)
        self.assertEqual(self.ledger.snapshot('a','expert_fast',now=self.now)['uncertain'],3)
        self.assertEqual(self.ledger.snapshot('a','ordinary_fast10',now=self.now)['uncertain'],3)


if __name__ == '__main__': unittest.main()
