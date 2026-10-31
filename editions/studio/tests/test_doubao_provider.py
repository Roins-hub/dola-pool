import json
import tempfile
import unittest
from pathlib import Path

from providers.doubao import DoubaoProvider, ProviderError


def ck(session='session-one', uid='user-one', domain='.doubao.com'):
    return json.dumps([
        {'name': 'sessionid', 'value': session, 'domain': domain, 'path': '/'},
        {'name': 'uid_tt', 'value': uid, 'domain': domain, 'path': '/'},
    ])


PROXY = 'http://user:secret@8.8.8.8:9000'


class IndependentAccountsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'doubao'
        self.provider = DoubaoProvider(root=self.root, enabled=False)

    def test_disabled_empty_pool_does_not_advertise_guessed_models(self):
        result = self.provider.capabilities()
        self.assertFalse(result['enabled'])
        self.assertEqual(result['models'], [])
        with self.assertRaises(ProviderError) as error:
            self.provider.validate_request({'prompt': 'cat', 'duration': 15})
        self.assertEqual(error.exception.code, 'platform_disabled')

    def test_import_isolated_redacted_and_reopens_persisted_account(self):
        result = self.provider.import_ck('same-name', ck(), PROXY)
        self.assertEqual(result['name'], 'same-name')
        self.assertEqual(result['platform'], 'doubao')
        self.assertFalse(result['login_verified'])
        self.assertIn('8.8.8.8', result['proxy_label'])
        self.assertNotIn('secret', json.dumps(result))
        self.assertNotIn('session-one', json.dumps(self.provider.list_accounts()))
        self.assertTrue((self.root / 'accounts' / result['account_id'] / 'cookies.json').exists())
        reopened = DoubaoProvider(root=self.root, enabled=False)
        self.assertEqual(len(reopened.list_accounts()), 1)

    def test_dola_cookie_and_missing_binding_fail_before_writing_account(self):
        for raw, proxy in [(ck(domain='.dola.com'), PROXY), (ck(), ''), (ck(), 'http://127.0.0.1:9000')]:
            with self.subTest(raw=raw[:15], proxy=proxy), self.assertRaises(ProviderError):
                self.provider.import_ck('invalid', raw, proxy)
        self.assertEqual(self.provider.list_accounts(), [])

    def test_same_user_with_rotated_cookie_is_duplicate_even_when_trashed(self):
        self.provider.import_ck('one', ck(), PROXY)
        deleted = self.provider.delete_account('one')
        with self.assertRaises(ProviderError) as error:
            self.provider.import_ck('two', ck(session='rotated-session'), PROXY)
        self.assertEqual(error.exception.code, 'duplicate_account')
        self.provider.restore_account(deleted['token'])
        self.assertEqual([a['name'] for a in self.provider.list_accounts()], ['one'])

    def test_duplicate_session_without_uid_is_blocked(self):
        raw = json.dumps([{'name': 'sessionid', 'value': 'same', 'domain': '.doubao.com'}])
        self.provider.import_ck('one', raw, PROXY)
        with self.assertRaises(ProviderError) as error:
            self.provider.import_ck('two', raw, PROXY)
        self.assertEqual(error.exception.code, 'duplicate_account')

    def test_changing_route_invalidates_checks_and_cannot_mutate_busy_account(self):
        self.provider.import_ck('one', ck(), PROXY)
        changed = self.provider.patch_account('one', proxy='http://user:changed@8.8.4.4:9000')
        self.assertFalse(changed['route_verified'])
        self.assertIn('8.8.4.4', changed['proxy_label'])
        self.provider.accounts.reserve('task-1', 'one')
        with self.assertRaises(ProviderError) as error:
            self.provider.patch_account('one', proxy=PROXY)
        self.assertEqual(error.exception.code, 'account_busy')
        with self.assertRaises(ProviderError):
            self.provider.delete_account('one')

    def test_send_rotation_and_quota_reservations_are_task_idempotent(self):
        self.provider.import_ck('one', ck(), PROXY)
        self.provider.import_ck('two', ck('session-two', 'user-two'), PROXY)
        account = self.provider.accounts
        account.reserve('task-1', 'one')
        account.mark_sent('task-1')
        account.mark_sent('task-1')
        account.settle('task-1', 'rejected')
        account.settle('task-1', 'rejected')
        items = {a['name']: a for a in account.list_accounts()}
        self.assertEqual(items['one']['send_count'], 1)
        self.assertEqual(items['one']['quota_used'], 0)
        self.assertEqual(items['one']['quota_reserved'], 0)
        self.assertEqual(items['two']['send_count'], 0)

    def test_unknown_submit_retains_reservation_across_restart(self):
        self.provider.import_ck('one', ck(), PROXY)
        self.provider.accounts.reserve('task-1', 'one')
        self.provider.accounts.mark_sent('task-1')
        self.provider.accounts.settle('task-1', 'unknown')
        reopened = DoubaoProvider(root=self.root, enabled=False)
        item = reopened.list_accounts()[0]
        self.assertEqual(item['quota_reserved'], 1)
        with self.assertRaises(ProviderError):
            reopened.delete_account('one')

    def test_doubao_rejects_dola_duration_profile_even_when_disabled(self):
        with self.assertRaises(ProviderError) as error:
            self.provider.validate_request({'prompt': 'cat', 'duration': 15, 'duration_profile': 'fast15-via30'})
        self.assertEqual(error.exception.code, 'unsupported_duration')

    def test_name_traversal_and_broken_route_never_fall_back(self):
        with self.assertRaises(ProviderError):
            self.provider.import_ck('../escaped', ck(), PROXY)
        item = self.provider.import_ck('one', ck(), PROXY)
        path = self.root / 'accounts' / item['account_id'] / 'proxy.json'
        path.write_text('{broken', encoding='utf-8')
        with self.assertRaises(ProviderError) as error:
            self.provider.accounts.bound_proxy('one')
        self.assertEqual(error.exception.code, 'proxy_unavailable')

    def test_local_quota_has_no_dola_default_and_can_be_configured_independently(self):
        self.provider.import_ck('one', ck(), PROXY)
        self.assertIsNone(self.provider.list_accounts()[0]['quota_limit'])
        item = self.provider.patch_account('one', quota_limit=7)
        self.assertEqual(item['quota_remaining'], 7)
        self.assertEqual(item['quota_limit'], 7)


if __name__ == '__main__':
    unittest.main()
