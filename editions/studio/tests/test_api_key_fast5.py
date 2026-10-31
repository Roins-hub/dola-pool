import json
import tempfile
import unittest
from pathlib import Path

from store import TaskStore


class FastFiveSecondKeyTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.path=str(Path(self.folder.name)/'keys.db')
        self.store=TaskStore(self.path)

    def tearDown(self):
        self.store._conn.close()
        self.folder.cleanup()

    def test_explicit_five_second_permission_survives_store_round_trip(self):
        key=self.store.create_key(name='fixture',allowed_durations=[5,10])
        self.assertEqual(key['allowed_durations'],[5,10])
        self.store.update_key(key['key'],allowed_durations=[5])
        self.assertEqual(self.store.get_key(key['key'])['allowed_durations'],[5])

    def test_new_default_key_includes_five_seconds(self):
        self.assertIn(5,self.store.create_key(name='fixture')['allowed_durations'])

    def test_only_legacy_full_catalog_permission_is_upgraded(self):
        full=self.store.create_key(name='legacy-all')
        limited=self.store.create_key(name='limited',allowed_durations=[10])
        self.store._conn.execute('UPDATE api_keys SET allowed_durations=? WHERE key=?',(json.dumps([10,15,30]),full['key']))
        self.store._conn.commit()
        self.store._conn.close()
        self.store=TaskStore(self.path)
        self.assertEqual(self.store.get_key(full['key'])['allowed_durations'],[5,10,15,30])
        self.assertEqual(self.store.get_key(limited['key'])['allowed_durations'],[10])


if __name__=='__main__':
    unittest.main()
