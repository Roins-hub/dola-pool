import ast
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from ck_import import import_ck


def post(application, payload, authorized=False):
    async def request():
        messages = []
        async def receive():
            return {'type': 'http.request', 'body': json.dumps(payload).encode(), 'more_body': False}
        async def send(message):
            messages.append(message)
        headers = [(b'content-type', b'application/json')]
        if authorized:
            headers.append((b'x-admin-key', b'fixture-admin'))
        await application(dict(type='http', asgi={'version': '3.0'}, http_version='1.1',
            method='POST', scheme='http', path='/api/admin/accounts/import-ck',
            raw_path=b'/api/admin/accounts/import-ck', query_string=b'', root_path='',
            headers=headers, client=('127.0.0.1', 1), server=('test', 80)), receive, send)
        status = next(m['status'] for m in messages if m['type'] == 'http.response.start')
        text = b''.join(m.get('body', b'') for m in messages).decode()
        return SimpleNamespace(status_code=status, text=text, json=lambda: json.loads(text))
    return asyncio.run(request())


class ImportCKTests(unittest.TestCase):
    def save_browser_template(self, root, name, proxy, options):
        path = Path(root) / name
        path.mkdir()
        (path / 'proxy.txt').write_text(proxy, encoding='utf-8')
        (path / 'browser.json').write_text(json.dumps(options), encoding='utf-8')
        return path

    def test_new_account_inherits_only_transport_and_region_from_exact_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            options = {'isp_transport': True, 'locale': 'en-GB',
                       'accept_language': 'en-GB,en;q=0.9', 'timezone_id': 'Europe/London',
                       'site_language': 'en'}
            old = self.save_browser_template(tmp, 'old', 'http://old:secret@ISP.EXAMPLE:11000/',
                {**options, 'proxy_chain': 'http://127.0.0.1:7897', 'cdn_via_local': True,
                 'required_proxy_server': 'http://ISP.EXAMPLE:11000',
                 'viewport': {'width': 1440, 'height': 900}})
            before = {p.name: p.read_bytes() for p in old.iterdir()}
            proxy = 'isp.example:11000:new:new-secret'
            import_ck(tmp, 'fresh', 'sessionid=fresh', proxy)
            target = Path(tmp) / 'fresh'
            self.assertTrue((target / 'browser.json').exists(),
                            'New UK accounts must inherit the verified ISP transport')
            self.assertEqual(json.loads((target / 'browser.json').read_text()), options)
            self.assertEqual((target / 'proxy.txt').read_text().strip(), proxy)
            self.assertEqual({p.name: p.read_bytes() for p in old.iterdir()}, before)

    def test_auto_assigned_proxy_inherits_its_transport(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.save_browser_template(tmp, 'old', 'http://user:secret@isp.example:11000',
                                       {'isp_transport': True})
            import_ck(tmp, 'fresh', 'sessionid=fresh')
            settings = Path(tmp) / 'fresh' / 'browser.json'
            self.assertTrue(settings.exists(), 'Automatic proxy assignment also needs ISP transport')
            self.assertEqual(json.loads(settings.read_text()), {'isp_transport': True})

    def test_other_endpoint_and_hidden_templates_are_not_inherited(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name, proxy in [('other_host', 'http://other.example:11000'),
                                ('other_port', 'http://isp.example:11001'),
                                ('other_scheme', 'https://isp.example:11000'),
                                ('.old_backup', 'http://isp.example:11000')]:
                self.save_browser_template(tmp, name, proxy, {'isp_transport': True})
            import_ck(tmp, 'fresh', 'sessionid=fresh', 'http://isp.example:11000')
            self.assertFalse((Path(tmp) / 'fresh' / 'browser.json').exists())

    def test_conflicting_templates_stop_import_without_partial_files_or_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.save_browser_template(tmp, 'first', 'http://one:private-one@isp.example:11000',
                                       {'isp_transport': True, 'locale': 'en-GB'})
            self.save_browser_template(tmp, 'second', 'http://two:private-two@isp.example:11000',
                                       {'isp_transport': False, 'locale': 'en-GB'})
            before = sorted(p.name for p in Path(tmp).iterdir())
            with self.assertRaisesRegex(ValueError, '冲突') as error:
                import_ck(tmp, 'fresh', 'sessionid=fresh', 'http://new:private-new@isp.example:11000')
            self.assertNotIn('private', str(error.exception))
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), before)

    def test_invalid_matching_template_does_not_silently_use_default_transport(self):
        for options in ({'isp_transport': 'true'}, {'locale': ''},
                        {'timezone_id': 'Europe/London\n'}, ['not-a-settings-object']):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                self.save_browser_template(tmp, 'old', 'http://isp.example:11000', options)
                with self.assertRaisesRegex(ValueError, '浏览器配置'):
                    import_ck(tmp, 'fresh', 'sessionid=fresh', 'http://isp.example:11000')
                self.assertFalse((Path(tmp) / 'fresh').exists())

    def test_duplicate_import_preserves_existing_settings_even_if_templates_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = self.save_browser_template(tmp, 'old', 'http://isp.example:11000',
                                             {'isp_transport': True})
            (old / 'cookies.json').write_text('[{"name":"sessionid","value":"same"}]')
            self.save_browser_template(tmp, 'other', 'http://isp.example:11000',
                                       {'isp_transport': False})
            before = {p.name: p.read_bytes() for p in old.iterdir()}
            result = import_ck(tmp, 'renamed', 'sessionid=same', 'http://isp.example:11000')
            self.assertTrue(result.get('existing'))
            self.assertEqual({p.name: p.read_bytes() for p in old.iterdir()}, before)
            self.assertFalse((Path(tmp) / 'renamed').exists())

    def test_batch_repeated_files_skip_matching_session_or_user_under_other_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            import_ck(tmp, 'first', 'sessionid=session-a; uid_tt=user-a; transient=old')
            before = (Path(tmp) / 'first' / 'cookies.json').read_bytes()
            for name, raw in (
                ('renamed', 'transient=new; sessionid_ss=session-a'),
                ('new_login', 'sessionid=session-b; uid_tt_ss=user-a'),
            ):
                result = import_ck(tmp, name, raw)
                self.assertTrue(result.get('existing'))
                self.assertEqual(result['name'], 'first')
                self.assertFalse((Path(tmp) / name).exists())
            self.assertEqual((Path(tmp) / 'first' / 'cookies.json').read_bytes(), before)
            result = import_ck(tmp, 'different', 'sessionid=session-c; uid_tt=user-c')
            self.assertFalse(result.get('existing', False))

    def test_reimporting_same_txt_returns_existing_account_without_changing_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            import_ck(tmp, 'same', 'sessionid=fixture; token=a=b', 'proxy.example:8080')
            target = Path(tmp) / 'same'
            before = {p.name: p.read_bytes() for p in target.iterdir()}
            result = import_ck(tmp, 'same', 'token=a=b; sessionid=fixture')
            self.assertTrue(result.get('existing'))
            self.assertEqual(result['name'], 'same')
            self.assertEqual({p.name: p.read_bytes() for p in target.iterdir()}, before)

    def test_blank_proxy_assigns_a_saved_isp_and_explicit_proxy_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = Path(tmp) / 'old'
            saved.mkdir()
            (saved / 'proxy.txt').write_text('http://user:pass@isp.example:8000')
            import_ck(tmp, 'fresh', 'sessionid=fixture')
            self.assertTrue((Path(tmp) / 'fresh' / 'proxy.txt').exists())
            self.assertEqual((Path(tmp) / 'fresh' / 'proxy.txt').read_text().strip(),
                             'http://user:pass@isp.example:8000')
            import_ck(tmp, 'manual', 'sessionid=fixture2', 'manual.example:9000')
            self.assertEqual((Path(tmp) / 'manual' / 'proxy.txt').read_text().strip(), 'manual.example:9000')

    def test_imports_full_txt_cookie_and_optional_isp_without_claiming_verified_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = import_ck(Path(tmp), 'scan_example', '\ufeffCookie: sessionid=fake-session; token=a=b; locale=zh-CN\n', 'http://user:fake@proxy.example:8080')
            self.assertIsNotNone(result, 'Import must create an account from CK')
            self.assertEqual(result, {'name': 'scan_example', 'cookie_count': 3, 'status': 'unverified'})
            account = Path(tmp) / 'scan_example'
            self.assertEqual(json.loads((account / 'cookies.json').read_text()), [
                {'name': 'sessionid', 'value': 'fake-session'}, {'name': 'token', 'value': 'a=b'},
                {'name': 'locale', 'value': 'zh-CN'}])
            self.assertEqual((account / 'proxy.txt').read_text().strip(), 'http://user:fake@proxy.example:8080')
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ['scan_example'])

    def test_existing_account_cookie_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'existing'
            target.mkdir()
            (target / 'cookies.json').write_text('preserve-this')
            with self.assertRaises(FileExistsError):
                import_ck(Path(tmp), 'existing', 'sessionid=replacement')
            self.assertEqual((target / 'cookies.json').read_text(), 'preserve-this')

    def test_rejects_invalid_cookie_or_unsafe_name_without_writing(self):
        invalid = [('okay', ''), ('okay', 'anon=only'), ('okay', 'sessionid='),
                   ('../escape', 'sessionid=fake'), ('CON', 'sessionid=fake'),
                   ('okay', 'sessionid=fake\r\nAuthorization: other'),
                   ('okay', '[{"cookie":"sessionid","value":"fake"}]')]
        with tempfile.TemporaryDirectory() as tmp:
            for name, raw in invalid:
                with self.subTest(name=name, raw=raw), self.assertRaises(ValueError):
                    import_ck(Path(tmp), name, raw)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_sessionid_ss_is_accepted_and_missing_proxy_creates_no_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = import_ck(Path(tmp), 'ss', 'sessionid_ss=alternative;')
            self.assertIsNotNone(result)
            self.assertFalse((Path(tmp) / 'ss' / 'proxy.txt').exists())

    def test_invalid_proxy_does_not_leave_partial_account(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                import_ck(Path(tmp), 'new', 'sessionid=fake', 'http://bad\nheader:80')
            self.assertEqual(list(Path(tmp).iterdir()), [])


class ImportEndpointTests(unittest.TestCase):
    def test_admin_only_import_and_conflict_handling(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'server.py').read_text(encoding='utf-8'))
        nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.AsyncFunctionDef))
                 and n.name in {'CKImport', 'admin_import_ck'}]
        self.assertEqual(len(nodes), 2, 'CK import model and admin endpoint must exist')
        def auth(key, authorization):
            if key != 'fixture-admin':
                raise HTTPException(401, 'unauthorized')
        with tempfile.TemporaryDirectory() as tmp:
            application = FastAPI()
            from test_five_account_retry import pool_module
            account_pool = pool_module.BrowserPool(tmp, ':memory:')
            self.addCleanup(account_pool._conn.close)
            namespace = dict(app=application, BaseModel=BaseModel, Field=Field,
                Header=Header, HTTPException=HTTPException, _admin_auth=auth,
                pool=account_pool, import_ck=import_ck, JOBS={},
                config=SimpleNamespace(_load_proxy_map=lambda: {'old': 'isp.example:8080'}))
            exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                         'ck-endpoint', 'exec'), namespace)
            payload = {'name': 'fresh', 'ck': 'sessionid=fixture-secret; locale=zh'}
            response = post(application, payload)
            self.assertEqual(response.status_code, 401)
            self.assertEqual(list(Path(tmp).iterdir()), [])
            response = post(application, payload, True)
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()['name'], 'fresh')
            self.assertNotIn('fixture-secret', response.text)
            self.assertTrue((Path(tmp) / 'fresh' / 'proxy.txt').exists())
            repeated = post(application, payload, True)
            self.assertEqual(repeated.status_code, 201, repeated.text)
            self.assertTrue(repeated.json().get('existing'))
            self.assertEqual(post(application, {**payload, 'ck': 'sessionid=different'}, True).status_code, 409)


if __name__ == '__main__':
    unittest.main()
