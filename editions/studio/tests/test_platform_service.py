"""Exercise the real HTTP app in a subprocess with entirely temporary state.

The outer test never imports server/config. The child has no lifespan startup,
all durable paths point into its temporary working directory, and its external
generation worker is replaced at the network boundary before requests run.
"""
import asyncio
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


PROJECT = Path(__file__).resolve().parents[1]


class PlatformServiceTests(unittest.TestCase):
    def test_real_service_contracts_with_temporary_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('DOLA_', 'DOUBAO_'))}
            env.update({
                'PYTHONIOENCODING': 'utf-8',
                'PYTHONPATH': str(PROJECT),
                'DOLA_DB_PATH': str(root / 'tasks.db'),
                'DOLA_POOL_DB_PATH': str(root / 'pool.db'),
                'DOLA_ACCOUNTS_DIR': str(root / 'accounts'),
                'DOLA_PROXY_USAGE_DB': str(root / 'proxy-usage.db'),
                'DOLA_DOWNLOAD_DIR': str(root / 'downloads'),
                'DOLA_REFERENCE_UPLOAD_DIR': str(root / 'references'),
                'DOUBAO_DATA_DIR': str(root / 'doubao'),
                'DOUBAO_ENABLED': '0',
                'DOLA_ISP_ONLY': '1',
                'DOLA_API_KEYS': '',
                'DOLA_ADMIN_KEY': 'fixture-admin',
                'DOLA_PROXY': '',
            })
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--service-probe'],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0,
                             'Temporary real-service probe failed:\n' + result.stdout + result.stderr)


def service_probe():
    """Import-time state creation is deliberately confined to the child process."""
    sys.path.insert(0, str(PROJECT))
    assert Path.cwd().resolve() != PROJECT.resolve(), 'Service probe must run in temporary cwd'
    for key in ('DOLA_DB_PATH', 'DOLA_POOL_DB_PATH', 'DOLA_ACCOUNTS_DIR',
                'DOLA_PROXY_USAGE_DB', 'DOLA_DOWNLOAD_DIR', 'DOLA_REFERENCE_UPLOAD_DIR', 'DOUBAO_DATA_DIR'):
        assert Path(os.environ[key]).resolve().is_relative_to(Path.cwd()), key + ' escaped temporary cwd'
    profile = Path(os.environ['DOLA_ACCOUNTS_DIR']) / 'fixture'
    profile.mkdir(parents=True)
    (profile / 'proxy.txt').write_text('http://fixture:fixture@isp-fixture.example:11000', encoding='utf-8')
    import server
    from PIL import Image
    from store import TaskStore

    class LocalResponse:
        def __init__(self, status, raw):
            self.status_code = status
            self.text = raw.decode('utf-8')
        def json(self):
            return json.loads(self.text)

    class LocalClient:
        async def request(self, method, url, headers=None, content=None, **kwargs):
            from urllib.parse import urlsplit
            target = urlsplit(url)
            headers = {k.lower(): v for k, v in (headers or {}).items()}
            if 'json' in kwargs:
                content = json.dumps(kwargs['json']).encode()
                headers['content-type'] = 'application/json'
            messages = []
            async def receive():
                return {'type': 'http.request', 'body': content or b'', 'more_body': False}
            async def send(message):
                messages.append(message)
            await server.app({'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
                'method': method, 'scheme': 'http', 'path': target.path, 'raw_path': target.path.encode(),
                'query_string': target.query.encode(), 'root_path': '',
                'headers': [(k.encode(), str(v).encode()) for k, v in headers.items()],
                'client': ('127.0.0.1', 1), 'server': ('fixture', 80)}, receive, send)
            return LocalResponse(next(m['status'] for m in messages if m['type'] == 'http.response.start'),
                b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body'))
        async def get(self, url, **kwargs):
            return await self.request('GET', url, **kwargs)
        async def post(self, url, **kwargs):
            return await self.request('POST', url, **kwargs)
        async def patch(self, url, **kwargs):
            return await self.request('PATCH', url, **kwargs)
        async def aclose(self):
            pass

    assert Path(server.store.db_path).resolve().is_relative_to(Path.cwd())
    assert server.pool.accounts_dir.resolve().is_relative_to(Path.cwd())
    # Close the initially-created temporary task store before per-test stores.
    server.store._conn.close()

    class ServiceContracts(unittest.IsolatedAsyncioTestCase):
        async def asyncSetUp(self):
            self.store = TaskStore(':memory:')
            server.store = self.store
            server.pool.set_scheduling('fixture', True)
            self.runs = []
            async def local_worker(*args, **kwargs):
                self.runs.append(args)
            self.old_worker = server._run_task
            server._run_task = local_worker
            self.client = LocalClient()
            self.admin = {'X-Admin-Key': 'fixture-admin'}
            self.key = self.store.create_key('both', allowed_platforms=['dola', 'doubao'])['key']
            self.bearer = {'Authorization': 'Bearer ' + self.key}

        async def asyncTearDown(self):
            await asyncio.sleep(0)
            await self.client.aclose()
            server._run_task = self.old_worker
            self.store._conn.close()

        async def test_admin_capabilities_require_auth_and_doubao_is_unverified(self):
            response = await self.client.get('/api/admin/video-capabilities?platform=doubao')
            self.assertEqual(response.status_code, 401, response.text)
            response = await self.client.get('/api/admin/video-capabilities?platform=doubao', headers=self.admin)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['platform'], 'doubao')
            self.assertFalse(response.json()['enabled'])
            self.assertEqual(response.json()['models'], [])
            response = await self.client.get('/api/admin/video-capabilities', headers=self.admin)
            self.assertEqual(response.json()['platform'], 'dola')
            self.assertTrue(response.json()['enabled'])

        async def test_retired_cast_library_and_review_routes_are_not_served(self):
            base = '/api/admin/platforms/doubao'
            for method, path, body in (
                ('GET', base + '/cast', None),
                ('POST', base + '/cast', {'name': 'cat'}),
                ('GET', base + '/cast/' + 'a'*32 + '/image', None),
                ('POST', base + '/calibration/' + 'a'*32 + '/cast-review', {'confirmed': True}),
            ):
                kwargs = {'headers': self.admin}
                if body is not None:
                    kwargs['json'] = body
                response = await self.client.request(method, path, **kwargs)
                self.assertIn(response.status_code, (404,) if method == 'GET' else (404, 405),
                              (path, response.text))
            advertised = (await self.client.get('/openapi.json')).json()['paths']
            self.assertNotIn(base + '/cast', advertised)
            self.assertNotIn(base + '/calibration/{identifier}/cast-review', advertised)
            self.assertFalse((Path(server.config.DOUBAO_DATA_DIR) / 'cast').exists())

        async def test_nonempty_retired_cast_fields_stop_api_and_admin_before_queue_creation(self):
            for platform in ('dola', 'doubao'):
                for legacy in ({'cast_ids': ['a'*32]}, {'cast': [{'name': 'cat'}]}):
                    for path, headers in (('/v1/videos/generations', self.bearer),
                                          ('/api/admin/generate', self.admin)):
                        response = await self.client.post(path, headers=headers,
                            json={'platform': platform, 'prompt': 'fixture cat', **legacy})
                        self.assertEqual(response.status_code, 422, (path, response.text))
                        self.assertEqual(response.json()['detail']['error_code'], 'unsupported_cast')
            self.assertEqual(self.store.recent_tasks(platform='all'), [])
            self.assertEqual(self.runs, [])

        async def test_empty_legacy_cast_defaults_leave_normal_dola_generation_working(self):
            response = await self.client.post('/v1/videos/generations', headers=self.bearer,
                json={'model': 'seedance-2.0', 'prompt': 'fixture cat', 'duration': 10,
                      'cast': [], 'cast_ids': []})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.store.get(response.json()['id'])['platform'], 'dola')
            await asyncio.sleep(0)
            self.assertEqual(len(self.runs), 1)

        async def test_query_alias_cannot_reset_dola_or_reach_dola_quota_policy(self):
            server.pool.set_scheduling('fixture', False)
            before = server.pool.get_quota_policy()
            response = await self.client.get('/api/admin/accounts?platform=doubao', headers=self.admin)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['platform'], 'doubao')
            self.assertEqual(response.json()['accounts'], [])
            response = await self.client.post('/api/admin/accounts/reset-all?platform=doubao', headers=self.admin)
            self.assertIn(response.status_code, (404, 405, 422), response.text)
            response = await self.client.patch('/api/admin/quota-policy?platform=doubao',
                headers=self.admin, json={'mode': 'fast'})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertFalse(server.pool.list_accounts()[0]['scheduling'])
            self.assertEqual(server.pool.get_quota_policy(), before)

        async def test_unknown_and_conflicting_platforms_stop_before_mutation(self):
            for url in ('/api/admin/accounts?platform=typo', '/api/admin/video-capabilities?platform=typo',
                        '/api/admin/tasks?platform=typo'):
                response = await self.client.get(url, headers=self.admin)
                self.assertEqual(response.status_code, 422, (url, response.text))
            response = await self.client.post('/v1/videos/generations', headers=self.bearer,
                json={'platform': 'typo', 'prompt': 'fixture cat'})
            self.assertEqual(response.status_code, 422, response.text)
            response = await self.client.patch('/api/admin/quota-policy?platform=doubao',
                headers=self.admin, json={'platform': 'dola', 'limit': 5})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(self.store.recent_tasks(platform='all'), [])

        async def test_doubao_only_key_cannot_submit_default_dola(self):
            key = self.store.create_key('doubao-only', allowed_platforms=['doubao'])['key']
            for body in ({'prompt': 'fixture cat'}, {'platform': 'dola', 'prompt': 'fixture cat'}):
                response = await self.client.post('/v1/videos/generations',
                    headers={'Authorization': 'Bearer ' + key}, json=body)
                self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(self.store.recent_tasks(platform='all'), [])
            self.assertEqual(self.runs, [])

        async def test_invalid_allowed_platforms_return_422(self):
            response = await self.client.post('/api/admin/keys', headers=self.admin,
                json={'name': 'invalid', 'allowed_platforms': ['dola', 'typo']})
            self.assertEqual(response.status_code, 422, response.text)
            response = await self.client.patch('/api/admin/keys/' + self.key, headers=self.admin,
                json={'allowed_platforms': ['typo']})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(self.store.get_key(self.key)['allowed_platforms'], ['dola', 'doubao'])

        async def test_uploads_are_platform_scoped_and_cannot_cross_into_dola_task(self):
            stream = BytesIO()
            Image.new('RGB', (8, 8)).save(stream, format='PNG')
            response = await self.client.post('/api/admin/reference-images?platform=doubao',
                headers={**self.admin, 'Content-Type': 'image/png'}, content=stream.getvalue())
            self.assertEqual(response.status_code, 201, response.text)
            identifier = response.json()['id']
            root = Path(server.config.REFERENCE_UPLOAD_DIR)
            self.assertTrue((root / 'doubao' / 'incoming' / identifier).is_file())
            self.assertFalse((root / 'incoming' / identifier).exists())
            response = await self.client.post('/api/admin/generate', headers=self.admin,
                json={'model': 'seedance-2.0', 'prompt': 'fixture cat', 'duration': 10,
                      'reference_uploads': [identifier]})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(self.store.recent_tasks(platform='all'), [])

        async def test_task_filter_defaults_to_dola_and_all_does_not_drop_platform(self):
            self.store.create('dola-one', 'seedance-2.0', 'd', 'default', 10)
            self.store.create('doubao-one', 'native', 'b', 'default', 10, platform='doubao')
            response = await self.client.get('/api/admin/tasks', headers=self.admin)
            self.assertEqual([r['id'] for r in response.json()['tasks']], ['dola-one'])
            response = await self.client.get('/api/admin/tasks?platform=doubao', headers=self.admin)
            self.assertEqual([r['id'] for r in response.json()['tasks']], ['doubao-one'])
            response = await self.client.get('/api/admin/tasks?platform=all', headers=self.admin)
            self.assertEqual({r['platform'] for r in response.json()['tasks']}, {'dola', 'doubao'})

        async def test_cross_platform_idempotency_does_not_replace_original_task(self):
            body = {'model': 'seedance-2.0', 'prompt': 'fixture cat', 'duration': 10,
                    'idempotency_key': 'service-intent-0001'}
            first = await self.client.post('/v1/videos/generations', headers=self.bearer, json=body)
            self.assertEqual(first.status_code, 200, first.text)
            task = first.json()['id']
            replay = await self.client.post('/v1/videos/generations', headers=self.bearer,
                                           json={**body, 'platform': 'dola'})
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()['id'], task)
            self.assertTrue(replay.json()['replayed'])
            conflict = await self.client.post('/v1/videos/generations', headers=self.bearer,
                                              json={**body, 'platform': 'doubao'})
            self.assertEqual(conflict.status_code, 409, conflict.text)
            self.assertEqual(self.store.get(task)['platform'], 'dola')
            self.assertEqual(len(self.store.recent_tasks(platform='all')), 1)

    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ServiceContracts)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    server.pool._conn.close()
    server.proxy_usage.usage._conn.close()
    return result.wasSuccessful()


if __name__ == '__main__':
    if '--service-probe' in sys.argv:
        raise SystemExit(0 if service_probe() else 1)
    unittest.main()
