"""API callers upload private reference bytes under their authenticated scope."""
import ast
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import HTTPException, Request
from reference_uploads import save_image
from test_member_reference_uploads import MemberUploadTests
from test_reference_uploads import png


class ApiKeyReferenceUploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = MemberUploadTests()
        await self.f.asyncSetUp()
        self.ns = self.f.ns
        self.ns['_member_task'] = lambda row: row
        def authenticate(authorization):
            names = {'Bearer env-a', 'Bearer env-b', 'Bearer member-a', 'Bearer member-b', 'Bearer expert-a',
                     'Bearer dola-only', 'Bearer anonymous'}
            if authorization not in names:
                raise HTTPException(401, 'invalid key')
            if authorization == 'Bearer anonymous':
                return {'api_key_hash': None, 'allowed_durations': [10]}
            return {'api_key_hash': hashlib.sha256(authorization.encode()).hexdigest(),
                    'api_key_name': 'fixture', 'daily_limit': 10, 'concurrency_limit': 1,
                    'allowed_durations': [30] if authorization.endswith('expert-a') else [10],
                    'allowed_platforms': ['dola'] if authorization.endswith('dola-only') else ['dola','doubao'],
                    'owner_user_id': 1 if authorization.endswith('member-a') else
                                     2 if authorization.endswith('member-b') else None}
        self.ns['_auth'] = authenticate
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        names = {'api_reference_limits', 'api_reference_upload', 'get_video'}
        nodes = [n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'api-key-upload','exec'),self.ns)

    async def asyncTearDown(self):
        await self.f.asyncTearDown()

    async def upload(self, token='Bearer env-a'):
        status, data = await self.f.request('/v1/reference-images',png(),token)
        self.assertEqual(status,201,data)
        return data['id']

    async def task(self, images, token='Bearer env-a', **extra):
        return await self.f.request('/v1/videos/generations',
            {'prompt':'原始提示词\n尾部 ', 'model':'seedance-2.0','duration':10,
             'ratio':'16:9','reference_uploads':images, **extra},token)

    async def test_api_key_upload_and_limits_do_not_need_browser_or_member_session(self):
        identifier = await self.upload()
        file = self.f.root/'incoming'/identifier
        self.assertEqual(file.read_bytes(),png())
        owner = json.loads(file.with_suffix('.png.json').read_text())['owner']
        self.assertEqual(owner,'api-key:'+hashlib.sha256(b'Bearer env-a').hexdigest())
        status, limits = await self.f.request('/v1/reference-images/limits',
            authorization='Bearer env-a',method='GET')
        self.assertEqual((status,limits),(200,{'max_count':2,'max_bytes':1024}))

    async def test_no_credentials_invalid_key_and_development_anonymous_cannot_upload(self):
        for token in (None,'Bearer invalid','Bearer anonymous'):
            for path,method in (('/v1/reference-images','POST'),('/v1/reference-images/limits','GET')):
                status,_=await self.f.request(path,png(),token,method=method)
                self.assertIn(status,(401,403))
        self.assertFalse(list(self.f.root.rglob('*.png')))

    async def test_image_bytes_size_and_platform_entitlement_are_checked_before_saving(self):
        for data,expected in ((b'not an image',422),(b'x'*1025,413)):
            status,_=await self.f.request('/v1/reference-images',data,'Bearer env-a')
            self.assertEqual(status,expected)
        # Exercise the query parameter without weakening the common ASGI fixture.
        scope_query = b'platform=doubao'
        async def request(method):
            messages=[]
            async def receive():return {'type':'http.request','body':png(),'more_body':False}
            async def send(message):messages.append(message)
            path='/v1/reference-images' if method=='POST' else '/v1/reference-images/limits'
            await self.f.app(dict(type='http',asgi={'version':'3.0'},http_version='1.1',method=method,
                scheme='http',path=path,raw_path=path.encode(),query_string=scope_query,root_path='',
                headers=[(b'authorization',b'Bearer dola-only')],client=('127.0.0.1',1),server=('test',80)),receive,send)
            return next(m['status'] for m in messages if m['type']=='http.response.start')
        self.assertEqual(await request('POST'),403)
        self.assertEqual(await request('GET'),403)
        self.assertFalse(list(self.f.root.rglob('*.png')))

    async def test_owned_images_keep_order_and_exact_prompt_in_fast_ten_task(self):
        a,b=await self.upload(),await self.upload()
        status,data=await self.task([b,a])
        self.assertEqual(status,200,data)
        row=self.f.store.get(data['id'])
        refs=json.loads(row['reference_images'])
        self.assertEqual(refs,[f'upload:{data["id"]}/{b}',f'upload:{data["id"]}/{a}'])
        self.assertEqual((row['prompt'],row['model'],row['duration'],row['ratio']),
                         ('原始提示词\n尾部 ','seedance-2.0',10,'16:9'))
        self.assertEqual(len(self.f.runs),1)

    async def test_key_and_member_ownership_are_isolated_without_admin_privilege(self):
        env=await self.upload()
        member=await self.upload('Bearer member-a')
        for token,identifier in [('Bearer env-b',env),('Bearer member-a',env),
                                 ('Bearer env-a',member),('Bearer member-b',member),
                                 ('Bearer env-a','../private.png')]:
            status,_=await self.task([identifier],token)
            self.assertEqual(status,422,(token,identifier))
        self.assertEqual(self.f.runs,[])
        status,data=await self.task([member],'Bearer member-a')
        self.assertEqual(status,200,data)

    async def test_duration_permission_is_still_checked_before_task_creation(self):
        identifier=await self.upload()
        status,_=await self.task([identifier],duration=5)
        self.assertEqual(status,422)
        self.assertEqual(self.f.store.pending_task_count(),0)

    async def test_batch_fast_uses_same_api_key_owner_and_blocks_cross_key_uploads(self):
        identifier=await self.upload()
        with patch('dola_submission.start_batch'):
            status,data=await self.task([identifier],batch_count=2,idempotency_key='api-owned-batch-01')
            self.assertEqual(status,200,data)
            self.assertEqual(len(data['tasks']),2)
            for row in data['tasks']:
                stored=self.f.store.get(row['id'])
                self.assertEqual(json.loads(stored['reference_images']),[f'upload:{row["id"]}/{identifier}'])
            status,_=await self.task([identifier],'Bearer env-b',batch_count=2,idempotency_key='api-cross-key-01')
            self.assertEqual(status,422)

    async def test_own_task_exposes_failure_and_conversation_fields_without_cross_key_access(self):
        identifier='video_'+'d'*32
        self.f.store.create(identifier,'seedance-2.0','private prompt','16:9',10,
            api_key_hash=hashlib.sha256(b'Bearer env-a').hexdigest())
        self.f.store.update(identifier,status='failed',phase='failed',failure_code='presend_network',conversation_id='')
        status,data=await self.f.request('/v1/videos/'+identifier,authorization='Bearer env-a',method='GET')
        self.assertEqual(status,200,data)
        self.assertEqual(data.get('failure_code'),'presend_network')
        self.assertEqual(data.get('conversation_id'),'')
        status,_=await self.f.request('/v1/videos/'+identifier,authorization='Bearer env-b',method='GET')
        self.assertEqual(status,404)

    async def test_expert_single_task_keeps_key_upload_owner_and_duration_permission(self):
        identifier=await self.upload('Bearer expert-a')
        status,data=await self.task([identifier],'Bearer expert-a',duration=30,
            duration_profile='plugin-expert30',resolution='720p')
        self.assertEqual(status,200,data)
        row=self.f.store.get(data['id'])
        self.assertEqual(row['duration_profile'],'plugin-expert30')
        self.assertEqual(json.loads(row['reference_images']),[f'upload:{data["id"]}/{identifier}'])

    async def test_doubao_upload_and_generation_share_the_same_platform_and_key_owner(self):
        async def receive():return {'type':'http.request','body':png(),'more_body':False}
        item=await self.ns['api_reference_upload'](Request({'type':'http'},receive),
            authorization='Bearer env-a',platform='doubao')
        self.assertTrue((self.f.root/'doubao'/'incoming'/item['id']).is_file())
        self.assertFalse((self.f.root/'incoming'/item['id']).exists())
        class Provider:
            def list_accounts(self):return ['fixture']
            def validate_request(self,values):
                return {'model':'fixture-doubao','duration':10,'ratio':'16:9'}
        self.ns['doubao_provider']=Provider()
        with patch('platform_runtime.run_platform_task'):
            status,data=await self.task([item['id']],platform='doubao',model='fixture-doubao')
            self.assertEqual(status,200,data)
            row=self.f.store.get(data['id'])
            self.assertEqual(row['platform'],'doubao')
            self.assertEqual(json.loads(row['reference_images']),[f'upload:{data["id"]}/{item["id"]}'])
            status,_=await self.task([item['id']],'Bearer env-b',platform='doubao',model='fixture-doubao')
            self.assertEqual(status,422)


if __name__=='__main__':
    unittest.main()
