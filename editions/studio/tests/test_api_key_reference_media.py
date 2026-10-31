"""Pure API keys can upload owned audio/video without a browser session."""
import ast
import hashlib
import json
from pathlib import Path
import unittest
from urllib.parse import quote

from reference_uploads import save_media
import test_api_key_reference_uploads as image_api
from test_reference_media import wav_bytes
from test_video_metadata import mp4


class ApiKeyReferenceMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = image_api.ApiKeyReferenceUploadTests()
        await self.fixture.asyncSetUp()
        self.ns = self.fixture.ns
        self.ns['config'].REFERENCE_MEDIA_MAX_BYTES = 4096
        self.ns['config'].REFERENCE_MEDIA_MAX_COUNT = 2
        self.ns['save_media'] = save_media
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        names = {'_reference_media_limits', '_save_reference_media_request',
                 'api_reference_media_config', 'api_reference_media_upload'}
        nodes = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in names]
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     'real-key-media-api', 'exec'), self.ns)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def request(self, method='POST', data=None, token='Bearer env-a',
                      filename='voice.wav', platform='dola'):
        path = '/v1/reference-media' + ('/config' if method == 'GET' else '')
        headers = [(b'content-type', b'application/octet-stream'),
                   (b'x-file-name', quote(filename).encode())]
        if token:
            headers.append((b'authorization', token.encode()))
        messages = []
        async def receive():
            return {'type':'http.request', 'body':wav_bytes() if data is None else data, 'more_body':False}
        async def send(message):
            messages.append(message)
        await self.fixture.f.app(dict(type='http', asgi={'version':'3.0'}, http_version='1.1',
            method=method, scheme='http', path=path, raw_path=path.encode(),
            query_string=('platform='+quote(platform)).encode(), root_path='', headers=headers,
            client=('127.0.0.1',1), server=('test',80)), receive, send)
        return (next(message['status'] for message in messages if message['type']=='http.response.start'),
                json.loads(b''.join(message.get('body',b'') for message in messages)))

    async def test_key_only_audio_and_video_uploads_keep_owner_and_feed_expert_request(self):
        for data, filename, kind in ((wav_bytes(), 'voice.wav', 'audio'), (mp4(5), 'clip.mp4', 'video')):
            with self.subTest(kind=kind):
                status, item = await self.request(data=data, filename=filename)
                self.assertEqual(status, 201, item)
                self.assertEqual(item['kind'], kind)
                path = self.fixture.f.root/'incoming'/item['id']
                self.assertEqual(path.read_bytes(), data)
                metadata = json.loads(path.with_suffix(path.suffix+'.json').read_text())
                self.assertEqual(metadata['owner'], 'api-key:'+hashlib.sha256(b'Bearer env-a').hexdigest())
                status, task = await self.fixture.task([], duration_profile='plugin-expert30',
                    reference_media_uploads=[item['id']])
                self.assertEqual(status, 200, task)
                refs = json.loads(self.fixture.f.store.get(task['id'])['reference_images'])
                self.assertEqual(refs, ['upload:'+task['id']+'/'+item['id']])

    async def test_config_requires_a_real_key_and_reports_existing_media_limits(self):
        status, limits = await self.request('GET')
        self.assertEqual(status, 200, limits)
        self.assertEqual((limits['max_count'], limits['max_bytes']), (2,4096))
        self.assertIn('mp4', limits['extensions'])
        for token in (None, 'Bearer invalid', 'Bearer anonymous'):
            for method in ('POST','GET'):
                with self.subTest(token=token, method=method):
                    self.assertEqual((await self.request(method, token=token))[0], 401)
        self.assertFalse(list(self.fixture.f.root.rglob('*.wav')))

    async def test_cross_key_and_member_media_cannot_be_attached(self):
        status, item = await self.request()
        self.assertEqual(status, 201, item)
        for token in ('Bearer env-b', 'Bearer member-a'):
            status, task = await self.fixture.task([], token, duration_profile='plugin-expert30',
                                                  reference_media_uploads=[item['id']])
            self.assertEqual(status, 422, task)
        self.assertEqual(self.fixture.f.runs, [])

    async def test_platform_permission_is_checked_for_upload_and_config_before_saving(self):
        for method in ('POST', 'GET'):
            status, result = await self.request(method, token='Bearer dola-only', platform='doubao')
            self.assertEqual(status, 403, result)
        self.assertFalse(list(self.fixture.f.root.rglob('*.wav')))
        status, item = await self.request(platform='doubao')
        self.assertEqual(status, 201, item)
        self.assertTrue((self.fixture.f.root/'doubao'/'incoming'/item['id']).is_file())
        self.assertFalse((self.fixture.f.root/'incoming'/item['id']).exists())

    async def test_body_validation_and_filename_cannot_supply_a_local_path(self):
        for data, filename, expected in ((b'fake', 'x.mp4', 422), (wav_bytes(), 'x.txt', 422),
                                          (b'x'*4097, 'x.wav', 413)):
            status, result = await self.request(data=data, filename=filename)
            self.assertEqual(status, expected, result)
        status, item = await self.request(filename='../../private.wav')
        self.assertEqual(status, 201, item)
        self.assertEqual(item['name'], 'private.wav')
        self.assertTrue((self.fixture.f.root/'incoming'/item['id']).is_file())
        status, task = await self.fixture.task([], duration_profile='plugin-expert30',
                                              reference_media_uploads=['../private.wav'])
        self.assertEqual(status, 422, task)
