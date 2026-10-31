import ast
import asyncio
import json
from pathlib import Path
import unittest
from urllib.parse import quote

import test_member_reference_uploads as images
from reference_uploads import admin_owner, save_media
from test_reference_media import wav_bytes


class ReferenceMediaAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=images.MemberUploadTests()
        await self.fixture.asyncSetUp()
        self.ns=self.fixture.ns
        self.ns['config'].REFERENCE_MEDIA_MAX_BYTES=4096
        self.ns['config'].REFERENCE_MEDIA_MAX_COUNT=2
        self.ns.update(save_media=save_media,admin_owner=admin_owner)
        def admin(key, bearer):
            if key!='fixture-admin':raise self.ns['HTTPException'](401,'unauthorized')
            return {'kind':'session','user':{'id':1}}
        self.ns['_admin_auth']=admin
        names={'_reference_media_limits','_save_reference_media_request','member_reference_media_upload',
               'admin_reference_media_upload','member_reference_media_config','admin_reference_media_config'}
        tree=ast.parse(Path('server.py').read_text(encoding='utf-8'))
        nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
        exec(compile(ast.Module(body=nodes,type_ignores=[]),'<media-api>','exec'),self.ns)

    async def asyncTearDown(self):
        await self.fixture.asyncTearDown()

    async def request(self,path,data=b'',token='Bearer session1',filename='旁白.wav',admin=False,platform=None):
        headers=[(b'content-type',b'application/octet-stream'),(b'x-file-name',quote(filename).encode())]
        if token: headers.append((b'x-admin-key' if admin else b'authorization',token.encode()))
        messages=[]
        async def receive():return {'type':'http.request','body':data,'more_body':False}
        async def send(message):messages.append(message)
        await self.fixture.app(dict(type='http',asgi={'version':'3.0'},http_version='1.1',method='POST',
            scheme='http',path=path,raw_path=path.encode(),query_string=('platform='+platform).encode() if platform else b'',root_path='',headers=headers,
            client=('127.0.0.1',1),server=('test',80)),receive,send)
        return (next(m['status'] for m in messages if m['type']=='http.response.start'),
                json.loads(b''.join(m.get('body',b'') for m in messages)))

    async def test_member_and_admin_upload_then_task_snapshot_and_idempotency(self):
        for path,token,admin in [('/api/me/reference-media','Bearer session1',False),
                                 ('/api/admin/reference-media','fixture-admin',True)]:
            status,item=await self.request(path,wav_bytes(),token,admin=admin)
            self.assertEqual(status,201,item)
            self.assertEqual(item['kind'],'audio')
            self.assertEqual(item['name'],'旁白.wav')
            body={'prompt':'用户原文','reference_media_uploads':[item['id']],
                  'idempotency_key':'media-key-'+item['id']}
            status,task=await self.fixture.request('/v1/videos/generations',body,'Bearer key1')
            self.assertEqual(status,200,task)
            refs=json.loads(self.fixture.store.get(task['id'])['reference_images'])
            self.assertEqual(len(refs),1)
            self.assertTrue(refs[0].endswith('.wav'))
            status,replayed=await self.fixture.request('/v1/videos/generations',body,'Bearer key1')
            self.assertEqual(replayed['id'],task['id'])
            body['reference_media_uploads']=[]
            self.assertEqual((await self.fixture.request('/v1/videos/generations',body,'Bearer key1'))[0],409)
        self.assertEqual(len(self.fixture.runs),2)

    async def test_auth_format_size_owner_and_unowned_api_key_are_checked(self):
        self.assertEqual((await self.request('/api/me/reference-media',wav_bytes(),token=None))[0],401)
        self.assertEqual((await self.request('/api/admin/reference-media',wav_bytes(),token=None,admin=True))[0],401)
        for data,name,status in [(b'fake','voice.mp3',422),(wav_bytes(),'voice.txt',422),
                                 (b'x'*4097,'big.wav',413)]:
            self.assertEqual((await self.request('/api/me/reference-media',data,filename=name))[0],status)
        status,item=await self.request('/api/me/reference-media',wav_bytes())
        self.assertEqual(status,201)
        for key in ('key2','unowned'):
            self.assertEqual((await self.fixture.request('/v1/videos/generations',
                {'prompt':'test','reference_media_uploads':[item['id']]},'Bearer '+key))[0],422)
        self.assertEqual(self.fixture.runs,[])

    async def test_doubao_audio_uploads_use_the_same_platform_root_as_images(self):
        root=Path(self.ns['config'].REFERENCE_UPLOAD_DIR)
        for path,token,admin in [('/api/me/reference-media','Bearer session1',False),
                                 ('/api/admin/reference-media','fixture-admin',True)]:
            status,item=await self.request(path,wav_bytes(),token,admin=admin,platform='doubao')
            self.assertEqual(status,201,item)
            self.assertTrue((root/'doubao'/'incoming'/item['id']).is_file())
            self.assertFalse((root/'incoming'/item['id']).exists())
            status,_=await self.request(path,wav_bytes(),token,admin=admin,platform='unknown')
            self.assertEqual(status,422)
