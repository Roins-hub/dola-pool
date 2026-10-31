import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
from providers.base import ProviderError
from providers.doubao.accounts import AccountStore, parse_proxy

LOCAL = 'http://127.0.0.1:17897'


class DoubaoLocalProxyTests(unittest.TestCase):
    def setUp(self):
        self.allow = patch.object(config, 'DOUBAO_LOCAL_PROXY_URL', LOCAL, create=True)
        self.allow.start()
        self.addCleanup(self.allow.stop)
        self.mapping = patch.object(config, 'DOUBAO_LOCAL_PROXY_MAP', '{}', create=True)
        self.mapping.start()
        self.addCleanup(self.mapping.stop)

    def test_only_explicitly_configured_exact_http_loopback_proxy_is_accepted(self):
        self.assertEqual(parse_proxy(LOCAL), (LOCAL, {'server': LOCAL}))
        for other in ['http://127.0.0.1:7897', 'http://127.0.0.2:17897',
                      'http://localhost:17897', 'http://192.168.1.1:17897',
                      'http://user:secret@127.0.0.1:17897', LOCAL+'/?bypass=1']:
            with self.subTest(other=other), self.assertRaises(ProviderError):
                parse_proxy(other)

    def test_unconfigured_or_malformed_allowlist_cannot_admit_a_local_proxy(self):
        for setting in ['', 'http://192.168.1.1:17897', 'http://127.0.0.1:17897/path',
                        'socks5://127.0.0.1:17897', 'http://u:p@127.0.0.1:17897']:
            with patch.object(config, 'DOUBAO_LOCAL_PROXY_URL', setting):
                with self.subTest(setting=setting), self.assertRaises(ProviderError):
                    parse_proxy(LOCAL)

    def test_saved_binding_rechecks_allowlist_and_rejects_tampered_browser_options(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AccountStore(Path(temp))
            account = store.import_ck('local', 'sessionid=fixture', LOCAL)
            self.assertEqual(store.bound_proxy('local'), {'server': LOCAL})
            proxy_file = Path(temp)/'accounts'/account['account_id']/'proxy.json'
            proxy_file.write_text(json.dumps({'server': LOCAL, 'bypass': '*'}), encoding='utf-8')
            with self.assertRaises(ProviderError):
                store.bound_proxy('local')
            proxy_file.write_text(json.dumps({'server': LOCAL}), encoding='utf-8')
            with patch.object(config, 'DOUBAO_LOCAL_PROXY_URL', 'http://127.0.0.1:17898'):
                with self.assertRaises(ProviderError):
                    store.bound_proxy('local')

    def test_dola_isp_validation_stays_strict_and_public_doubao_isp_remains_supported(self):
        with self.assertRaises(ValueError):
            config.require_isp_proxy({'server': LOCAL})
        normalized, parsed = parse_proxy('http://user:secret@8.8.8.8:9000')
        self.assertEqual(parsed['server'], 'http://8.8.8.8:9000')

    def test_admin_choices_expose_only_a_label_and_reference_for_the_local_proxy(self):
        import platform_runtime
        self.assertTrue(hasattr(platform_runtime, 'doubao_proxy_options'))
        ns = {'config':config, 'pool':SimpleNamespace(accounts=[]), '_proxy_display':lambda raw:{}}
        with patch.object(config, '_load_proxy_map', return_value={}), \
             patch.object(config, 'DOUBAO_LOCAL_PROXY_LABEL', '香港代理（豆包专用）', create=True):
            choices = platform_runtime.doubao_proxy_options(ns)
        self.assertEqual(choices['local:hk'][0], {'id':'local:hk','label':'香港代理（豆包专用）','server':LOCAL,'country':'香港'})
        self.assertEqual(choices['local:hk'][1], LOCAL)


class DoubaoMultipleLocalProxyTests(unittest.TestCase):
    def setUp(self):
        self.options = {
            'hk-pro': {'server': 'http://127.0.0.1:17898', 'label': '香港Pro（豆包专用）', 'country': '香港'},
            'tw': {'server': 'http://127.0.0.1:17899', 'label': '台湾台北（豆包专用）', 'country': '台湾'},
        }
        for key, value in [('DOUBAO_LOCAL_PROXY_URL', LOCAL),
                           ('DOUBAO_LOCAL_PROXY_LABEL', '香港代理（豆包专用）'),
                           ('DOUBAO_LOCAL_PROXY_MAP', json.dumps(self.options))]:
            item = patch.object(config, key, value, create=True)
            item.start()
            self.addCleanup(item.stop)

    def test_explicit_named_local_routes_persist_and_reopen_with_chinese_labels(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AccountStore(Path(temp))
            for key, option in self.options.items():
                with self.subTest(key=key):
                    raw = option['server']
                    self.assertEqual(parse_proxy(raw), (raw, {'server': raw}))
                    account = store.import_ck(key, 'sessionid=fixture-' + key, raw)
                    self.assertEqual(account['proxy_label'], option['label'])
                    self.assertEqual(account['route_region'], option['country'])
                    env = json.loads((Path(temp)/'accounts'/account['account_id']/'environment.json').read_text(encoding='utf-8'))
                    self.assertEqual(env['required_proxy_server'], raw)
            reopened = AccountStore(Path(temp))
            for key, option in self.options.items():
                self.assertEqual(reopened.bound_proxy(key), {'server': option['server']})
            for raw in [LOCAL, 'http://127.0.0.1:17898', 'http://127.0.0.1:17899']:
                with self.subTest(raw=raw), self.assertRaises(ValueError):
                    config.require_isp_proxy({'server': raw})

    def test_named_bindings_stop_if_removed_or_tampered_without_route_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AccountStore(Path(temp))
            account = store.import_ck('tw', 'sessionid=fixture', self.options['tw']['server'])
            path = Path(temp)/'accounts'/account['account_id']/'proxy.json'
            path.write_text(json.dumps({'server': self.options['tw']['server'], 'bypass': '*'}), encoding='utf-8')
            with self.assertRaises(ProviderError):
                store.bound_proxy('tw')
            path.write_text(json.dumps({'server': self.options['tw']['server']}), encoding='utf-8')
            with patch.object(config, 'DOUBAO_LOCAL_PROXY_MAP', '{}'):
                with self.assertRaises(ProviderError):
                    store.bound_proxy('tw')

    def test_admin_choices_include_all_explicit_routes_direct_and_existing_isp(self):
        from platform_runtime import doubao_proxy_options
        ns = {'config': config, 'pool': SimpleNamespace(accounts=[]), '_proxy_display': lambda _: {
            'scheme': 'http', 'route_label': '英国 ISP', 'server': '8.8.8.8:9000'}}
        with patch.object(config, '_load_proxy_map', return_value={'one': 'http://u:secret@8.8.8.8:9000'}):
            choices = doubao_proxy_options(ns)
        self.assertEqual(len(choices), 5)
        self.assertIn('local:hk', choices)
        self.assertIn('local:direct', choices)
        for key, option in self.options.items():
            self.assertEqual(choices['local:' + key], ({'id': 'local:' + key, **option}, option['server']))
        self.assertNotIn('secret', json.dumps([value[0] for value in choices.values()]))

    def test_malformed_map_fails_closed_without_losing_legacy_route(self):
        valid_tw = self.options['tw']
        bad_options = [
            {'server': 'http://localhost:17898', 'label': '坏线路', 'country': '香港'},
            {'server': 'http://127.0.0.2:17898', 'label': '坏线路', 'country': '香港'},
            {'server': 'http://192.168.1.1:17898', 'label': '坏线路', 'country': '香港'},
            {'server': 'http://8.8.8.8:17898', 'label': '坏线路', 'country': '香港'},
            {'server': 'socks5://127.0.0.1:17898', 'label': '坏线路', 'country': '香港'},
            {'server': 'http://user:secret@127.0.0.1:17898', 'label': '坏线路', 'country': '香港'},
            {'server': 'http://127.0.0.1:17898/', 'label': '坏线路', 'country': '香港'},
            {'server': 'http://127.0.0.1:17898?bypass=1', 'label': '坏线路', 'country': '香港'},
            {'server': 'http://127.0.0.1:17898', 'label': '坏线路', 'country': '香港', 'bypass': '*'},
            {'server': 'http://127.0.0.1:17898', 'label': '坏\n线路', 'country': '香港'},
            {'server': 'http://127.0.0.1:17898', 'label': '坏线路', 'country': ''},
        ]
        malformed = ['', '[]', '{bad', json.dumps({'tw': valid_tw, 'hk': valid_tw}),
                     json.dumps({'tw': valid_tw, 'direct': valid_tw}),
                     json.dumps({'tw': valid_tw, '../escape': valid_tw}),
                     json.dumps({'tw': valid_tw, 'also-tw': valid_tw}),
                     json.dumps({'tw': valid_tw, 'legacy': {**valid_tw, 'server': LOCAL}}),
                     '{"tw":' + json.dumps(valid_tw) + ',"tw":' + json.dumps(valid_tw) + '}']
        malformed.extend(json.dumps({'tw': valid_tw, 'bad': option}) for option in bad_options)
        for raw in malformed:
            with self.subTest(raw=raw), patch.object(config, 'DOUBAO_LOCAL_PROXY_MAP', raw):
                with self.assertRaises(ProviderError):
                    parse_proxy(valid_tw['server'])
                self.assertEqual(parse_proxy(LOCAL), (LOCAL, {'server': LOCAL}))

    def test_unlisted_local_endpoint_is_not_implicitly_allowed(self):
        for raw in ['http://127.0.0.1:7897', 'http://localhost:17899', 'http://127.0.0.1:17900']:
            with self.subTest(raw=raw), self.assertRaises(ProviderError):
                parse_proxy(raw)

    def test_legacy_single_proxy_api_does_not_change_when_extra_routes_are_present(self):
        self.assertEqual(config.doubao_local_proxy(), {'server': LOCAL})
        with patch.object(config, 'DOUBAO_LOCAL_PROXY_URL', ''):
            self.assertIsNone(config.doubao_local_proxy())
            self.assertEqual(parse_proxy(self.options['tw']['server']),
                             (self.options['tw']['server'], {'server': self.options['tw']['server']}))

    def test_new_route_patch_persists_exact_binding_and_invalidates_previous_verification(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AccountStore(Path(temp))
            store.import_ck('one', 'sessionid=fixture', LOCAL)
            with store.connect() as conn:
                conn.execute("UPDATE accounts SET status='ready',diagnostics=? WHERE name='one'",
                             (json.dumps({'login_verified': True, 'route_verified': True}),))
            changed = store.patch('one', proxy=self.options['tw']['server'])
            self.assertEqual(changed['proxy_label'], self.options['tw']['label'])
            self.assertEqual(changed['route_region'], '台湾')
            self.assertFalse(changed['route_verified'])
            self.assertEqual(AccountStore(Path(temp)).bound_proxy('one'), {'server': self.options['tw']['server']})
