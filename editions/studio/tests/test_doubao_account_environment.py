import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import config
from providers.base import ProviderError
from providers.doubao.accounts import AccountStore
from providers.doubao.environment import browser_options, create_environment, validate_environment


ACCOUNT = 'a' * 32
HK = {'server': 'http://127.0.0.1:17897'}
TW = {'server': 'http://127.0.0.1:17899'}
DIRECT = {'server': 'direct'}
UNKNOWN = {'server': 'http://8.8.8.8:9000', 'username': 'fixture', 'password': 'private'}
OPTIONS = {'hk': {**HK, 'label': '香港', 'country': '香港'},
           'tw': {**TW, 'label': '台湾', 'country': '台湾'}}


class EnvironmentPolicyTests(unittest.TestCase):
    def setUp(self):
        local = patch.object(config, 'doubao_local_proxy_options', return_value=OPTIONS)
        local.start()
        self.addCleanup(local.stop)

    def test_exact_configured_routes_select_region_language_and_timezone(self):
        for binding, region, locale, timezone in [
                (DIRECT, 'CN', 'zh-CN', 'Asia/Shanghai'),
                (HK, 'HK', 'zh-HK', 'Asia/Hong_Kong'),
                (TW, 'TW', 'zh-TW', 'Asia/Taipei')]:
            with self.subTest(region=region):
                env = create_environment(ACCOUNT, binding)
                self.assertEqual((env['route_region'], env['locale'], env['timezone_id']),
                                 (region, locale, timezone))
                self.assertEqual(env['status'], 'matched')
                self.assertEqual(env['environment_id'], ACCOUNT)
                self.assertEqual(env['required_proxy_server'], binding['server'])
                self.assertEqual(validate_environment(env, ACCOUNT, binding), env)

    def test_route_switch_preserves_creation_and_device_settings(self):
        original = create_environment(ACCOUNT, HK)
        switched = create_environment(ACCOUNT, TW, previous=original)
        for key in ('created_at', 'environment_id', 'viewport', 'screen', 'device_scale_factor',
                    'is_mobile', 'has_touch', 'hardware_mode', 'color_scheme'):
            self.assertEqual(switched[key], original[key], key)
        self.assertEqual(switched['locale'], 'zh-TW')
        self.assertEqual(original['locale'], 'zh-HK')

    def test_repeated_generation_of_same_account_has_stable_device_window(self):
        original = create_environment(ACCOUNT, DIRECT)
        reopened = create_environment(ACCOUNT, DIRECT, previous=copy.deepcopy(original))
        self.assertEqual(original, reopened)
        self.assertEqual(browser_options(original), browser_options(create_environment(ACCOUNT, DIRECT)))

    def test_unknown_public_proxy_is_not_guessed_by_port_or_previous_region(self):
        previous = create_environment(ACCOUNT, TW)
        env = create_environment(ACCOUNT, UNKNOWN, previous=previous)
        self.assertEqual(env['route_region'], 'unknown')
        self.assertEqual(env['status'], 'unmatched')
        self.assertEqual(env['locale'], 'zh-TW')
        self.assertEqual(env['timezone_id'], 'Asia/Taipei')
        self.assertNotIn('username', env)
        self.assertNotIn('password', env)
        self.assertEqual(validate_environment(env, ACCOUNT, UNKNOWN), env)

    def test_legacy_migration_preserves_created_time_and_unknown_route_locale(self):
        legacy = {'schema': 1, 'created_at': 1000.5, 'locale': 'en-US',
                  'timezone_id': 'Europe/London', 'required_proxy_server': UNKNOWN['server']}
        env = create_environment(ACCOUNT, UNKNOWN, previous=legacy)
        self.assertEqual(env['schema'], 2)
        self.assertEqual((env['created_at'], env['locale'], env['timezone_id']),
                         (1000.5, 'en-US', 'Europe/London'))

    def test_validation_rejects_identity_binding_region_and_device_tampering(self):
        env = create_environment(ACCOUNT, HK)
        mutations = [
            ('environment_id', 'b' * 32), ('required_proxy_server', TW['server']),
            ('schema', True), ('schema', 1), ('created_at', True), ('created_at', float('nan')),
            ('route_region', 'TW'), ('route_region', []), ('locale', 'zh-CN'), ('timezone_id', 'Asia/Shanghai'),
            ('status', 'unmatched'), ('viewport', {'width': True, 'height': 720}),
            ('screen', {'width': 100, 'height': 100}), ('device_scale_factor', 2),
            ('is_mobile', True), ('has_touch', True), ('hardware_mode', 'spoofed'),
            ('color_scheme', 'dark'), ('user_agent', 'invented'),
        ]
        for key, value in mutations:
            with self.subTest(key=key, value=value), self.assertRaises(ProviderError):
                validate_environment({**env, key: value}, ACCOUNT, HK)
        with self.assertRaises(ProviderError):
            create_environment('b' * 32, HK, previous=env)

    def test_browser_options_are_supported_settings_without_hardware_overrides(self):
        options = browser_options(create_environment(ACCOUNT, HK))
        self.assertEqual(set(options), {'locale', 'timezone_id', 'viewport', 'screen',
                                      'device_scale_factor', 'is_mobile', 'has_touch', 'color_scheme'})
        self.assertEqual(options['locale'], 'zh-HK')
        self.assertFalse(options['is_mobile'])
        self.assertFalse(options['has_touch'])
        self.assertEqual(options['device_scale_factor'], 1)


class AccountEnvironmentTests(unittest.TestCase):
    def setUp(self):
        EnvironmentPolicyTests.setUp(self)
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = AccountStore(self.root)
        self.account = self.store.import_ck('one', 'sessionid=fixture', HK['server'])
        self.folder = self.root / 'accounts' / self.account['account_id']

    def write_environment(self, env):
        (self.folder / 'environment.json').write_text(json.dumps(env), encoding='utf-8')

    def test_import_uses_schema2_and_public_summary_omits_private_binding(self):
        env = self.store.browser_environment('one')
        public = self.store.list_accounts()[0]['browser_environment']
        self.assertEqual(env['schema'], 2)
        self.assertEqual(public, {'status': 'matched', 'region': '香港', 'locale': 'zh-HK',
            'timezone_id': 'Asia/Hong_Kong', 'viewport': env['viewport'],
            'hardware_mode': 'native', 'isolated': True})
        self.assertNotIn('required_proxy_server', public)

    def test_login_import_retains_the_temporary_browser_identity_and_window(self):
        temporary = create_environment('c' * 32, TW)
        account = self.store.import_ck('login', 'sessionid=fixture-login', TW['server'], environment=temporary)
        self.assertEqual(account['account_id'], temporary['environment_id'])
        self.assertEqual(self.store.browser_environment('login'), temporary)

    def test_login_import_rejects_bad_identity_or_route_before_writing(self):
        for change in ({'environment_id': '../escape'}, {'required_proxy_server': TW['server']}):
            env = {**create_environment('c' * 32, HK), **change}
            with self.subTest(change=change), self.assertRaises(ProviderError):
                self.store.import_ck('login', 'sessionid=fixture-login', HK['server'], environment=env)
        self.assertEqual(len(self.store.list_accounts()), 1)

    def test_login_import_existing_identity_or_folder_never_deletes_saved_files(self):
        existing = self.store.browser_environment('one')
        before = (self.folder / 'cookies.json').read_bytes()
        with self.assertRaises(ProviderError):
            self.store.import_ck('other', 'sessionid=fixture-other', HK['server'], environment=existing)
        self.assertEqual((self.folder / 'cookies.json').read_bytes(), before)
        orphan = self.root / 'accounts' / ('c' * 32)
        orphan.mkdir()
        marker = orphan / 'keep.txt'
        marker.write_text('existing private profile', encoding='utf-8')
        with self.assertRaises(FileExistsError):
            self.store.import_ck('other', 'sessionid=fixture-other', HK['server'],
                                 environment=create_environment('c' * 32, HK))
        self.assertEqual(marker.read_text(encoding='utf-8'), 'existing private profile')
        self.assertEqual(len(self.store.list_accounts()), 1)

    def test_legacy_wrong_binding_is_rejected_before_migration_or_network_use(self):
        legacy = {'schema': 1, 'created_at': 1000.5, 'locale': 'zh-CN',
                  'timezone_id': 'Asia/Shanghai', 'required_proxy_server': TW['server']}
        self.write_environment(legacy)
        content = (self.folder / 'environment.json').read_bytes()
        for action in (lambda: self.store.browser_environment('one'), lambda: self.store.bound_proxy('one')):
            with self.assertRaises(ProviderError):
                action()
        self.assertEqual((self.folder / 'environment.json').read_bytes(), content)
        self.assertEqual(self.store.list_accounts()[0]['browser_environment']['status'], 'invalid')

    def test_legacy_public_is_read_only_and_migration_preserves_account_records(self):
        legacy = {'schema': 1, 'created_at': 1000.5, 'locale': 'zh-CN',
                  'timezone_id': 'Asia/Shanghai', 'required_proxy_server': HK['server']}
        self.write_environment(legacy)
        with self.store.connect() as conn:
            conn.execute("UPDATE accounts SET quota_used=2,send_count=3,last_sent=999 WHERE name='one'")
            before = dict(self.store._row('one', conn))
        (self.folder / 'official-identity.json').write_text('{"user_id":"fixture123"}', encoding='utf-8')
        cookie_before = (self.folder / 'cookies.json').read_bytes()
        self.assertEqual(self.store.list_accounts()[0]['browser_environment']['status'], 'legacy')
        self.assertEqual(json.loads((self.folder / 'environment.json').read_text(encoding='utf-8')), legacy)
        migrated = self.store.browser_environment('one')
        self.assertEqual(migrated['created_at'], 1000.5)
        self.assertEqual((migrated['locale'], migrated['timezone_id']), ('zh-HK', 'Asia/Hong_Kong'))
        self.assertEqual(dict(self.store._row('one')), before)
        self.assertEqual((self.folder / 'cookies.json').read_bytes(), cookie_before)
        self.assertEqual(self.store.official_user_id('one'), 'fixture123')
        content = (self.folder / 'environment.json').read_bytes()
        self.store.bound_proxy('one')
        self.store.browser_environment('one')
        self.assertEqual((self.folder / 'environment.json').read_bytes(), content)

    def test_proxy_change_preserves_device_cookie_and_usage_but_invalidates_checks(self):
        before = self.store.browser_environment('one')
        cookie = (self.folder / 'cookies.json').read_bytes()
        with self.store.connect() as conn:
            conn.execute("UPDATE accounts SET quota_used=2,send_count=3,last_sent=999,status='ready',diagnostics=? WHERE name='one'",
                         (json.dumps({'login_verified': True}),))
        changed = self.store.patch('one', proxy=TW['server'])
        env = self.store.browser_environment('one')
        self.assertEqual(env['viewport'], before['viewport'])
        self.assertEqual(env['screen'], before['screen'])
        self.assertEqual(env['created_at'], before['created_at'])
        self.assertEqual(env['locale'], 'zh-TW')
        self.assertEqual((changed['send_count'], changed['quota_used'], changed['last_sent']), (3, 2, 999))
        self.assertFalse(changed['login_verified'])
        self.assertEqual((self.folder / 'cookies.json').read_bytes(), cookie)

    def test_route_change_busy_or_invalid_other_field_writes_nothing(self):
        before = {name: (self.folder / name).read_bytes() for name in ('proxy.json', 'environment.json')}
        with self.assertRaises(ProviderError):
            self.store.patch('one', proxy=TW['server'], quota_limit=-1)
        self.store.reserve('fixture-task', 'one')
        with self.assertRaises(ProviderError):
            self.store.patch('one', proxy=TW['server'])
        self.assertEqual({name: (self.folder / name).read_bytes() for name in before}, before)

    def test_route_change_failed_second_file_write_rolls_back_everything(self):
        import providers.doubao.accounts as module
        before = {name: (self.folder / name).read_bytes() for name in ('proxy.json', 'environment.json')}
        row = dict(self.store._row('one'))
        atomic = module.atomic_json
        def fail_environment(path, value):
            if Path(path).name == 'environment.json':
                raise OSError('fixture write failed')
            return atomic(path, value)
        with patch.object(module, 'atomic_json', side_effect=fail_environment), self.assertRaises(OSError):
            self.store.patch('one', proxy=TW['server'])
        self.assertEqual({name: (self.folder / name).read_bytes() for name in before}, before)
        self.assertEqual(dict(self.store._row('one')), row)

    def test_mismatched_environment_fails_closed_and_is_not_repaired_silently(self):
        env = self.store.browser_environment('one')
        env['required_proxy_server'] = TW['server']
        self.write_environment(env)
        content = (self.folder / 'environment.json').read_bytes()
        for action in (lambda: self.store.bound_proxy('one'), lambda: self.store.browser_environment('one'),
                       lambda: self.store.patch('one', proxy=DIRECT['server'])):
            with self.assertRaises(ProviderError):
                action()
        self.assertEqual(self.store.list_accounts()[0]['browser_environment']['status'], 'invalid')
        self.assertEqual((self.folder / 'environment.json').read_bytes(), content)

    def test_trash_listing_describes_legacy_without_migration_or_restore(self):
        self.write_environment({'schema': 1, 'created_at': 1000, 'locale': 'zh-CN',
            'timezone_id': 'Asia/Shanghai', 'required_proxy_server': HK['server']})
        self.store.delete('one')
        trash = self.root / 'trash' / self.account['account_id'] / 'environment.json'
        before = trash.read_bytes()
        result = self.store.list_accounts(trash=True)[0]
        self.assertEqual(result['browser_environment']['status'], 'legacy')
        self.assertFalse(result['enabled'])
        self.assertEqual(trash.read_bytes(), before)
