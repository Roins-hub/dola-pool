import sqlite3
import tempfile
import unittest
from pathlib import Path
from store import TaskStore


class PlatformStoreTests(unittest.TestCase):
    def setUp(self):
        self.s = TaskStore(':memory:')
        self.addCleanup(self.s._conn.close)

    def test_legacy_tasks_default_to_dola_without_touching_target(self):
        self.s.create('old', 'seedance-2.0', 'old', 'default', 15)
        self.assertEqual(self.s.get('old').get('platform'), 'dola')
        self.assertEqual(self.s.get('old')['duration'], 15)

    def test_two_platforms_list_and_recover_independently(self):
        self.s.create('d', 'seedance-2.0', 'd', 'default', 10)
        self.assertEqual(self.s.get('d').get('platform'), 'dola')
        self.s.create('b', 'native-b', 'b', 'default', 10, platform='doubao')
        self.assertEqual([r['id'] for r in self.s.recent_tasks(platform='doubao')], ['b'])
        self.assertEqual([r['id'] for r in self.s.recent_tasks()], ['d'])
        self.s.update('b', status='processing', account='same')
        self.s.fail_unconfirmed_submissions()
        self.assertEqual(self.s.get('b')['account'], 'same')
        self.assertEqual(self.s.get('b')['status'], 'processing')

    def test_platform_is_immutable_and_unknown_is_rejected(self):
        self.s.create('d', 'model', 'p', 'default', 10)
        with self.assertRaises(ValueError):
            self.s.update('d', platform='doubao')
        with self.assertRaises(ValueError):
            self.s.create('x', 'model', 'p', 'default', 10, platform='typo')

    def test_key_platforms_default_dola_and_are_explicitly_granted(self):
        key = self.s.create_key('legacy')
        self.assertEqual(key.get('allowed_platforms'), ['dola'])
        self.s.update_key(key['key'], allowed_platforms=['dola', 'doubao'])
        self.assertEqual(self.s.get_key(key['key'])['allowed_platforms'], ['dola', 'doubao'])
        with self.assertRaises(ValueError):
            self.s.update_key(key['key'], allowed_platforms=['unknown'])

    def test_idempotency_claim_replays_and_conflicts_do_not_replace_original(self):
        args = dict(idempotency_scope='api:test', idempotency_key='intent-1', request_fingerprint='dola-body')
        self.s.create('d', 'model', 'p', 'default', 10, **args)
        replay = self.s.create('dup', 'model', 'p', 'default', 10, **args)
        self.assertEqual(replay['id'], 'd')
        with self.assertRaises(ValueError):
            self.s.create('wrong', 'model', 'p', 'default', 10,
                          **{**args, 'request_fingerprint': 'doubao-body'})
        self.assertIsNone(self.s.get('wrong'))
        self.assertEqual(self.s.find_idempotent('api:test', 'intent-1', 'dola-body')['id'], 'd')
