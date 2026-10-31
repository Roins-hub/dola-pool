"""The task download endpoint serves only verified platform originals."""

import ast
import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import urlsplit

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

from clean_download import CleanDownloadFiles, file_sha256
from store import TaskStore


TASK_ID = 'video_clean_fixture'
PREVIEW = {
    'download_source': 'preview',
    'watermark': 'unknown',
    'video_url': 'https://v16-dola.dola.com/video.mp4?lr=cici_ai',
}
OFFICIAL = {
    'download_source': 'dola_original_api',
    'watermark': 'verified_download',
    'video_url': 'https://v16-dola.dola.com/video.mp4?lr=unwatermarked',
}


class CleanDownloadFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.downloads = self.root / 'downloads'
        self.downloads.mkdir()
        self.file = self.downloads / 'video.mp4'
        self.file.write_bytes(b'platform video fixture')
        self.files = CleanDownloadFiles(str(self.root / 'tasks.db'), self.downloads)
        self.manifest_path = self.files.provenance_dir / (self.files._key(TASK_ID) + '.json')
        self.write_manifest(PREVIEW)

    def write_manifest(self, metadata, path=None, digest=None):
        path = path or self.file
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(json.dumps({
            'task_id': TASK_ID,
            'local_path': str(path),
            'sha256': digest or file_sha256(path),
            'metadata': metadata,
        }), encoding='utf-8')


class CleanDownloadFilesTests(CleanDownloadFixture):
    def test_only_verified_platform_original_is_downloadable(self):
        self.assertFalse(self.files.status(TASK_ID)['official']['available'])
        self.assertIsNone(self.files.download_path(TASK_ID, 'official'))
        self.assertIsNone(self.files.download_path(TASK_ID, 'processed'))

        # Claiming an original source cannot upgrade a known preview URL.
        self.write_manifest({**OFFICIAL, 'video_url': PREVIEW['video_url']})
        self.assertFalse(self.files.status(TASK_ID)['official']['available'])
        self.assertIsNone(self.files.download_path(TASK_ID, 'official'))

        # A watermark signal on either URL also blocks the download.
        self.write_manifest({**OFFICIAL, 'source_url': PREVIEW['video_url']})
        self.assertFalse(self.files.status(TASK_ID)['official']['available'])

        self.write_manifest(OFFICIAL)
        self.assertTrue(self.files.status(TASK_ID)['official']['available'])
        self.assertTrue(self.files.status(TASK_ID)['official']['verified'])
        self.assertEqual(self.files.download_path(TASK_ID, 'official'), self.file.resolve())
        self.assertIsNone(self.files.download_path(TASK_ID, 'processed'))

    def test_tampered_hash_or_path_never_becomes_a_verified_download(self):
        outside = self.root / 'outside.mp4'
        outside.write_bytes(self.file.read_bytes())
        self.write_manifest(OFFICIAL, path=outside)
        self.assertIsNone(self.files.source(TASK_ID))
        self.assertIsNone(self.files.download_path(TASK_ID, 'official'))

        self.write_manifest(OFFICIAL, digest='0' * 64)
        self.assertIsNone(self.files.source(TASK_ID))
        self.assertFalse(self.files.status(TASK_ID)['official']['available'])

        self.write_manifest(OFFICIAL)
        self.file.write_bytes(b'changed after provenance was written')
        self.assertIsNone(self.files.download_path(TASK_ID, 'official'))


class CleanDownloadAPITests(CleanDownloadFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = TaskStore(str(self.root / 'tasks.db'))
        self.addCleanup(self.store._conn.close)
        self.store.create(TASK_ID, 'seedance-2.0', 'original prompt', '16:9', 15)
        self.store.update(TASK_ID, status='failed', phase='no_watermark_unavailable',
                          failure_code='no_watermark_unavailable', account='original-account')
        self.app = FastAPI()
        names = {'_clean_download_task', 'admin_clean_download_status',
                 'admin_clean_download_file'}
        server_path = Path(__file__).resolve().parents[1] / 'server.py'
        tree = ast.parse(server_path.read_text(encoding='utf-8'))
        nodes = [node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                 and node.name in names]
        self.assertEqual({node.name for node in nodes}, names)

        def auth(key, bearer):
            if key != 'fixture-admin':
                raise HTTPException(401, 'unauthorized')

        self.ns = dict(app=self.app, Header=Header, HTTPException=HTTPException,
                       FileResponse=FileResponse, _admin_auth=auth,
                       store=self.store, clean_download_files=self.files)
        exec(compile(ast.fix_missing_locations(ast.Module(body=nodes, type_ignores=[])),
                     str(server_path), 'exec'), self.ns)

    async def request(self, path, method='GET', data=None, authorized=True):
        parsed = urlsplit(path)
        body = json.dumps(data).encode() if data is not None else b''
        sent = []

        async def receive():
            return {'type': 'http.request', 'body': body, 'more_body': False}

        async def send(message):
            sent.append(message)

        headers = [(b'content-type', b'application/json')]
        if authorized:
            headers.append((b'x-admin-key', b'fixture-admin'))
        await self.app({'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
                        'http_version': '1.1', 'method': method, 'scheme': 'http',
                        'path': parsed.path, 'raw_path': parsed.path.encode(),
                        'query_string': parsed.query.encode(), 'root_path': '',
                        'headers': headers, 'client': ('127.0.0.1', 1),
                        'server': ('local', 80)}, receive, send)
        started = next(message for message in sent if message['type'] == 'http.response.start')
        content = b''.join(message.get('body', b'') for message in sent)
        return started['status'], dict(started['headers']), content

    async def test_download_routes_require_admin_key(self):
        base = f'/api/admin/tasks/{TASK_ID}/clean-download'
        for path in (base + '/status', base + '/file?kind=official',
                     base + '/file?kind=processed'):
            with self.subTest(path=path):
                status, _, _ = await self.request(path, authorized=False)
                self.assertEqual(status, 401)

    async def test_repair_route_and_processed_download_are_unavailable(self):
        base = f'/api/admin/tasks/{TASK_ID}/clean-download'
        before = self.store.get(TASK_ID)
        status, _, _ = await self.request(base + '/repair', 'POST', {'regions': []})
        self.assertEqual(status, 404)
        status, _, _ = await self.request(base + '/file?kind=processed')
        self.assertEqual(status, 422)
        status, _, _ = await self.request(base + '/file?kind=preview')
        self.assertEqual(status, 422)
        self.assertEqual(self.store.get(TASK_ID), before)

    async def test_verified_original_serves_bytes_without_changing_task(self):
        base = f'/api/admin/tasks/{TASK_ID}/clean-download'
        before = self.store.get(TASK_ID)
        status, _, _ = await self.request(base + '/file?kind=official')
        self.assertEqual(status, 404)

        self.write_manifest(OFFICIAL)
        status, _, content = await self.request(base + '/status')
        self.assertEqual(status, 200)
        state = json.loads(content)
        self.assertTrue(state['official']['available'])
        self.assertTrue(state['official']['verified'])
        self.assertNotIn('processed', state)
        self.assertNotIn('repair', state)

        status, headers, content = await self.request(base + '/file?kind=official')
        self.assertEqual(status, 200)
        self.assertEqual(headers[b'x-video-provenance'], b'official_original')
        self.assertEqual(content, self.file.read_bytes())
        self.assertEqual(self.store.get(TASK_ID), before)


if __name__ == '__main__':
    unittest.main()
