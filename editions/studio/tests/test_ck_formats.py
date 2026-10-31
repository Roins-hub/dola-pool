import json
import tempfile
import unittest
from pathlib import Path

from ck_import import import_ck, import_ck_batch


class CKFormatTests(unittest.TestCase):
    def test_browser_json_and_netscape_and_multiline_header_import(self):
        formats = [json.dumps([{'name': 'sessionid', 'value': 'fixture', 'domain': '.dola.com'}]),
                   '# Netscape HTTP Cookie File\n#HttpOnly_.dola.com\tTRUE\t/\tTRUE\t0\tsessionid\tfixture',
                   'Cookie: sessionid=fixture;\nuid_tt=user-fixture; token=a=b']
        for raw in formats:
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as root:
                result = import_ck(root, 'fixture', raw)
                self.assertFalse(result.get('existing'))
                cookies = json.loads((Path(root) / 'fixture/cookies.json').read_text(encoding='utf-8'))
                self.assertEqual(next(c['value'] for c in cookies if c['name'] == 'sessionid'), 'fixture')

    def test_multiple_accounts_in_one_txt_skip_duplicates_without_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            import_ck(root, 'original', 'sessionid=session-a; uid_tt=user-a')
            before = (Path(root) / 'original/cookies.json').read_bytes()
            result = import_ck_batch(root, 'batch', 'sessionid=session-a; uid_tt=user-a\nsessionid=session-b; uid_tt=user-b')
            self.assertEqual(result['imported'], 1)
            self.assertEqual(result['skipped'], 1)
            self.assertEqual((Path(root) / 'original/cookies.json').read_bytes(), before)
            self.assertNotIn('session-a', str(result))
            self.assertNotIn('session-b', str(result))

    def test_unrelated_cookie_domain_and_missing_credentials_are_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):
                import_ck(root, 'bad', json.dumps([{'name':'sessionid','value':'private','domain':'.other.test'}]))
            with self.assertRaises(ValueError):
                import_ck(root, 'bad', 'token=private')
            self.assertFalse((Path(root) / 'bad').exists())

    def test_named_headers_in_one_txt_remain_separate_accounts(self):
        with tempfile.TemporaryDirectory() as root:
            result = import_ck_batch(root, 'batch',
                'first----sessionid=fixture-a; uid_tt=user-a\n'
                'second----Cookie: sessionid=fixture-b; uid_tt=user-b')
            self.assertEqual(result['imported'], 2)
            self.assertEqual(result['failed'], 0)
            saved = [json.loads(p.read_text(encoding='utf-8'))
                     for p in Path(root).glob('*/cookies.json')]
            self.assertEqual(sorted(next(c['value'] for c in cs if c['name'] == 'sessionid')
                                    for cs in saved), ['fixture-a', 'fixture-b'])
