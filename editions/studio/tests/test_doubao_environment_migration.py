import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import config
from providers.doubao.accounts import AccountStore
from providers.base import ProviderError
from test_doubao_provider import ck

try:
    from providers.doubao.environment_migration import migrate_environments
except ImportError:
    migrate_environments = None


class EnvironmentMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'provider'
        self.backup = Path(self.temp.name) / 'backup'
        self.store = AccountStore(self.root)
        self.addCleanup(patch.stopall)
        patch.object(config, 'DOUBAO_LOCAL_PROXY_URL', 'http://127.0.0.1:17897').start()
        patch.object(config, 'DOUBAO_LOCAL_PROXY_MAP', json.dumps({
            'tw': {'server': 'http://127.0.0.1:17899', 'label': '台湾', 'country': '台湾'}})).start()

    def legacy(self, name, proxy):
        item = self.store.import_ck(name, ck(name, uid=name), proxy)
        folder = self.root / 'accounts' / item['account_id']
        (folder / 'environment.json').write_text(json.dumps({'schema': 1, 'locale': 'zh-CN',
            'timezone_id': 'Asia/Shanghai', 'created_at': 1700000000,
            'required_proxy_server': self.store.bound_proxy(name)['server']}), encoding='utf-8')
        (folder / 'profile').mkdir()
        (folder / 'profile' / 'Local State').write_text('existing browser state', encoding='utf-8')
        return item, folder

    def migrate(self):
        self.assertIsNotNone(migrate_environments, 'Environment migration must stage, back up and validate before changing live files.')
        return migrate_environments(self.root, self.backup)

    def database_rows(self):
        with self.store.connect() as conn:
            return {table: conn.execute('SELECT * FROM ' + table + ' ORDER BY 1').fetchall()
                    for table in ('accounts', 'attempts', 'settings')}

    def test_active_accounts_gain_route_environments_without_touching_auth_history_or_trash(self):
        pairs = [self.legacy('direct', 'direct'), self.legacy('hk', 'http://127.0.0.1:17897'),
                 self.legacy('tw', 'http://127.0.0.1:17899')]
        removed, old_folder = self.legacy('removed', 'direct')
        self.store.delete('removed')
        trash_env = self.root / 'trash' / removed['account_id'] / 'environment.json'
        old_trash = trash_env.read_bytes()
        before = self.database_rows()
        auth = {folder: (folder / 'cookies.json').read_bytes() for _, folder in pairs}
        report = self.migrate()
        self.assertEqual(report['migrated'], 3)
        for (item, folder), (locale, timezone) in zip(pairs, [
                ('zh-CN', 'Asia/Shanghai'), ('zh-HK', 'Asia/Hong_Kong'), ('zh-TW', 'Asia/Taipei')]):
            env = json.loads((folder / 'environment.json').read_text(encoding='utf-8'))
            self.assertEqual((env['schema'], env['locale'], env['timezone_id']), (2, locale, timezone))
            self.assertEqual(env['environment_id'], item['account_id'])
            self.assertEqual(env['created_at'], 1700000000)
            self.assertEqual((folder / 'cookies.json').read_bytes(), auth[folder])
            self.assertEqual((folder / 'profile' / 'Local State').read_text(), 'existing browser state')
            self.assertTrue((self.backup / item['account_id'] / 'environment.json').is_file())
        self.assertEqual(trash_env.read_bytes(), old_trash)
        self.assertEqual(self.database_rows(), before)

    def test_migration_is_idempotent_and_keeps_existing_device_configuration(self):
        _, folder = self.legacy('one', 'direct')
        self.migrate()
        before = (folder / 'environment.json').read_bytes()
        self.assertEqual(self.migrate()['migrated'], 0)
        self.assertEqual((folder / 'environment.json').read_bytes(), before)

    def test_one_corrupt_environment_prevents_partial_batch_migration(self):
        _, first = self.legacy('first', 'direct')
        _, second = self.legacy('second', 'http://127.0.0.1:17897')
        old_first = (first / 'environment.json').read_bytes()
        (second / 'environment.json').write_text('{broken', encoding='utf-8')
        with self.assertRaises(ProviderError):
            self.migrate()
        self.assertEqual((first / 'environment.json').read_bytes(), old_first)

    def test_inflight_task_prevents_any_live_file_change(self):
        _, folder = self.legacy('busy', 'direct')
        self.store.reserve('video_' + 'a' * 32, 'busy')
        before = (folder / 'environment.json').read_bytes()
        with self.assertRaises(ProviderError):
            self.migrate()
        self.assertEqual((folder / 'environment.json').read_bytes(), before)

    def test_backup_must_not_alias_an_active_account_environment(self):
        _, folder = self.legacy('one', 'direct')
        before = (folder / 'environment.json').read_bytes()
        self.backup = self.root / 'accounts' / '..' / 'accounts'
        with self.assertRaises(ProviderError):
            self.migrate()
        self.assertEqual((folder / 'environment.json').read_bytes(), before)
