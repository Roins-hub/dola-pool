import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import config
from providers.base import ProviderError
from providers.doubao.accounts import AccountStore, parse_proxy
from platform_runtime import doubao_proxy_options


class DoubaoDirectAccountsTests(unittest.TestCase):
    def setUp(self):
        mapping = patch.object(config, 'DOUBAO_LOCAL_PROXY_MAP', '{}', create=True)
        mapping.start()
        self.addCleanup(mapping.stop)

    def test_exact_direct_is_explicit_but_empty_or_missing_proxy_never_defaults_to_direct(self):
        self.assertEqual(parse_proxy('direct'), ('direct', {'server':'direct'}))
        for value in ['', None, ' direct', 'DIRECT', 'direct/', 'http://direct']:
            with self.subTest(value=value), self.assertRaises(ProviderError):
                parse_proxy(value)

    def test_direct_account_persists_explicit_environment_and_readable_label(self):
        with tempfile.TemporaryDirectory() as temp:
            accounts=AccountStore(Path(temp))
            saved=accounts.import_ck('direct-account', 'sessionid=fixture', 'direct')
            self.assertEqual(saved['proxy_label'], '本机直连（豆包专用）')
            self.assertEqual(saved['route_region'], '本机网络')
            self.assertEqual(accounts.bound_proxy('direct-account'), {'server':'direct'})
            env=json.loads((Path(temp)/'accounts'/saved['account_id']/'environment.json').read_text(encoding='utf-8'))
            self.assertEqual(env['required_proxy_server'],'direct')

    def test_saved_direct_binding_rejects_added_bypass_credentials_and_environment_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            accounts=AccountStore(Path(temp))
            saved=accounts.import_ck('direct-account','sessionid=fixture','direct')
            folder=Path(temp)/'accounts'/saved['account_id']
            for bad in [{'server':'direct','bypass':'*'},{'server':'direct','username':'x'},{}]:
                (folder/'proxy.json').write_text(json.dumps(bad),encoding='utf-8')
                with self.subTest(bad=bad),self.assertRaises(ProviderError):
                    accounts.bound_proxy('direct-account')
            (folder/'proxy.json').write_text('{"server":"direct"}',encoding='utf-8')
            env=json.loads((folder/'environment.json').read_text(encoding='utf-8'))
            env['required_proxy_server']='http://127.0.0.1:7897'
            (folder/'environment.json').write_text(json.dumps(env),encoding='utf-8')
            with self.assertRaises(ProviderError):accounts.bound_proxy('direct-account')

    def test_direct_choice_does_not_replace_hong_kong_or_public_isp_and_never_claims_a_country(self):
        ns={'config':config,'pool':SimpleNamespace(accounts=[]),'_proxy_display':lambda _: {
            'scheme':'http','route_label':'英国 ISP','server':'8.8.8.8:9000'}}
        with patch.object(config,'DOUBAO_LOCAL_PROXY_URL','http://127.0.0.1:17897'), \
             patch.object(config,'_load_proxy_map',return_value={'one':'http://u:p@8.8.8.8:9000'}):
            choices=doubao_proxy_options(ns)
        self.assertEqual(choices['local:direct'], ({'id':'local:direct','label':'本机直连（豆包专用）','server':'direct','country':'本机网络'},'direct'))
        self.assertIn('local:hk',choices)
        self.assertEqual(len(choices),3)

    def test_dola_isp_guard_still_rejects_direct(self):
        with self.assertRaises(ValueError):config.require_isp_proxy({'server':'direct'})
