"""Request statistics must not impose a request cap on traffic/time ISP plans."""
import tempfile
import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Header

import config
from proxy_usage import ProxyUsageStore, ProxyQuotaExceeded


class ProxyUsageUnlimitedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.proxy = {'server': 'http://192.0.2.10:11000', 'username': 'fixture'}
        self.path = str(Path(self.tmp.name) / 'usage.db')

    def store(self, limited=False):
        with patch.object(config, 'PROXY_REQUEST_LIMIT_ENABLED', limited, create=True):
            store = ProxyUsageStore(self.path, default_limit=3, warn_at=2)
        self.addCleanup(store._conn.close)
        return store

    def test_requests_continue_past_saved_limit_and_persist_counts(self):
        store = self.store()
        for _ in range(5):
            state = store.reserve(self.proxy)
        self.assertEqual(state['used'], 5)
        self.assertTrue(state['unlimited'])
        self.assertIsNone(state['remaining'])
        self.assertFalse(state['blocked'])
        self.assertFalse(state['warn'])
        self.assertTrue(store.is_available(self.proxy))
        restored = self.store().get(self.proxy)
        self.assertEqual(restored['used'], 5)
        self.assertFalse(restored['blocked'])

    def test_old_exhausted_pause_does_not_block_statistics_only_mode(self):
        limited = self.store(limited=True)
        limited.reserve(self.proxy, 3)
        self.assertTrue(limited.get(self.proxy)['blocked'])
        store = self.store()
        self.assertFalse(store.ensure_available(self.proxy)['blocked'])
        state = store.reserve(self.proxy)
        self.assertEqual(state['used'], 4)
        self.assertFalse(state['paused'])
        self.assertEqual(state['paused_reason'], '')

    def test_manual_pause_still_blocks_and_resume_preserves_statistics(self):
        store = self.store()
        store.reserve(self.proxy, 2)
        store.set_paused(self.proxy, True)
        with self.assertRaises(ProxyQuotaExceeded):
            store.reserve(self.proxy)
        self.assertEqual(store.get(self.proxy)['used'], 2)
        store.set_paused(self.proxy, False)
        store.add(self.proxy, 8)
        self.assertFalse(store.get(self.proxy)['blocked'])
        self.assertEqual(store.get(self.proxy)['used'], 10)

    def test_explicit_limited_mode_retains_existing_protection(self):
        store = self.store(limited=True)
        store.reserve(self.proxy, 3)
        with self.assertRaises(ProxyQuotaExceeded):
            store.reserve(self.proxy)
        self.assertEqual(store.get(self.proxy)['used'], 3)
        self.assertFalse(store.is_available(self.proxy))

    def test_reset_is_persistent_and_does_not_reinstate_limit(self):
        store = self.store()
        store.reserve(self.proxy, 10)
        state = store.reset(self.proxy)
        self.assertEqual(state['used'], 0)
        self.assertTrue(state['unlimited'])
        self.assertFalse(self.store().get(self.proxy)['blocked'])
        self.assertEqual(store.reserve(self.proxy, 10)['used'], 10)

    def test_proxy_summary_counts_unlimited_routes_as_available(self):
        store = self.store()
        store.reserve(self.proxy, 10)
        second = {'server': 'http://192.0.2.11:11000', 'username': 'fixture'}
        store.reserve(second, 2)
        store.set_paused(second, True)
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'admin_proxies')
        namespace = dict(app=FastAPI(), Header=Header, config=config,
                         pool=SimpleNamespace(list_accounts=lambda: []),
                         _admin_auth=lambda *args: None,
                         proxy_usage=SimpleNamespace(usage=store))
        with patch.object(config, 'ISP_ONLY', True):
            exec(compile(ast.Module(body=[node], type_ignores=[]), 'proxy-summary', 'exec'), namespace)
            response = asyncio.run(namespace['admin_proxies']())
        summary = response['summary']
        self.assertEqual((summary['used'], summary['available'], summary['paused']), (12, 1, 1))
        self.assertTrue(summary['unlimited'])
        self.assertIsNone(summary['limit'])
