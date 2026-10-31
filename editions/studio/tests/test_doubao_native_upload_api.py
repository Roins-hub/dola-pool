"""Native video calibration uploads inherit the authenticated owner's scope."""
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI, HTTPException

from providers.admin_api import create_admin_router
from test_doubao_admin_api import ASGIClient


class CaptureProvider:
    def __init__(self):
        self.calls = []

    def start_calibration(self, name, request, **kwargs):
        self.calls.append((name, request, kwargs))
        return {'id': 'calibration-fixture', 'state': 'queued'}


class CalibrationUploadApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'private-references'
        self.provider = CaptureProvider()
        self.who = {'kind': 'session', 'user': {'id': 73, 'role': 'admin'}}
        def authenticate(key, authorization=None):
            if authorization != 'Bearer fixture-admin':
                raise HTTPException(401, 'admin required')
            return self.who
        self.app = FastAPI()
        self.app.include_router(create_admin_router(self.provider, authenticate,
                                                     reference_root=self.root))
        self.client = ASGIClient(self.app)
        self.headers = {'Authorization': 'Bearer fixture-admin'}
        self.path = '/api/admin/platforms/doubao/accounts/one/calibrate'
        self.fields = {'model': 'observed-work-skill', 'duration': 15,
                       'ratio': '9:16', 'prompt': 'virtual fixture'}

    async def test_uploaded_image_and_media_tokens_get_server_owned_root_and_session_owner(self):
        for references in ({'reference_uploads': ['a'*32+'.png']},
                           {'reference_media_uploads': ['b'*32+'.mp3']},
                           {'reference_uploads': ['a'*32+'.png'],
                            'reference_media_uploads': ['b'*32+'.mp3']}):
            with self.subTest(references=references):
                body = {**self.fields, **references}
                result = await self.client.post(self.path, headers=self.headers, json=body)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(self.provider.calls[-1], ('one', body,
                    {'upload_owner': 'user:73', 'upload_root': self.root}))
                self.assertNotIn(str(self.root), result.text)
                self.assertNotIn('user:73', result.text)

    async def test_retired_cast_fields_fail_before_provider_or_browser_start(self):
        for legacy in ({'cast_ids': ['a'*32]}, {'cast': [{'name': 'cat'}]}):
            result = await self.client.post(self.path, headers=self.headers,
                json={**self.fields, **legacy})
            self.assertEqual(result.status_code, 422, result.text)
            self.assertEqual(result.json()['detail']['error_code'], 'unsupported_cast')
        self.assertEqual(self.provider.calls, [])

    async def test_empty_retired_defaults_are_removed_from_calibration_request(self):
        result = await self.client.post(self.path, headers=self.headers,
            json={**self.fields, 'cast': [], 'cast_ids': []})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.provider.calls, [('one', self.fields, {})])

    async def test_uploads_require_authentication_and_cannot_supply_a_private_filesystem_context(self):
        result = await self.client.post(self.path,
            json={**self.fields, 'reference_uploads': ['a'*32+'.png']})
        self.assertEqual(result.status_code, 401)
        for key in ('upload_owner', 'upload_root', '_reference_paths', '_validated',
                    'reference_image_paths', 'reference_media_paths', 'reference_upload_root'):
            with self.subTest(key=key):
                result = await self.client.post(self.path, headers=self.headers,
                    json={**self.fields, 'reference_uploads': ['a'*32+'.png'],
                          key: 'C:/private/secret'})
                self.assertEqual(result.status_code, 422, result.text)
                self.assertNotIn('C:/private/secret', result.text)
        self.assertEqual(self.provider.calls, [])

    async def test_text_only_calibration_retains_the_old_provider_call_signature(self):
        result = await self.client.post(self.path, headers=self.headers, json=self.fields)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(self.provider.calls, [('one', self.fields, {})])

    async def test_missing_server_upload_root_fails_before_provider_or_browser_start(self):
        app = FastAPI()
        app.include_router(create_admin_router(self.provider, lambda *_: {'kind': 'admin_key'}))
        response = await ASGIClient(app).post(self.path,
            json={**self.fields, 'reference_uploads': ['a'*32+'.png']})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.provider.calls, [])

    async def test_admin_key_upload_ownership_is_distinct_from_user_sessions(self):
        self.who = {'kind': 'admin_key', 'role': 'admin'}
        response = await self.client.post(self.path, headers=self.headers,
            json={**self.fields, 'reference_uploads': ['a'*32+'.png']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.provider.calls[-1][2]['upload_owner'], 'admin_key')


if __name__ == '__main__':
    unittest.main()
