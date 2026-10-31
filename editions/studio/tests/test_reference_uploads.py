from io import BytesIO
import ast
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
import media
from store import TaskQuotaExceeded, PendingTaskLimitExceeded
from video_capabilities import submission_duration, validate_video_parameters

from reference_uploads import admin_owner, save_image, attach_images, copy_task_image, cleanup_task_images


def png():
    stream = BytesIO()
    Image.new('RGB', (16, 16), '#008866').save(stream, format='PNG')
    return stream.getvalue()


class ReferenceUploadTests(unittest.TestCase):
    def test_private_upload_attaches_to_task_without_public_url_and_survives_source_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'private'
            item = save_image(png(), root, 'user:1', 1024)
            self.assertIsNotNone(item, 'A valid image must be stored privately')
            self.assertTrue(item['id'].endswith('.png'))
            task = 'video_' + 'a' * 32
            refs = attach_images([item['id']], root, 'user:1', task)
            self.assertEqual(len(refs), 1)
            (root / 'incoming' / item['id']).unlink()
            destination = Path(tmp) / 'prepared'
            result = copy_task_image(refs[0], root, task, destination)
            self.assertEqual(result.read_bytes(), png())
            cleanup_task_images(root, task)
            self.assertFalse((root / 'tasks' / task).exists())

    def test_unowned_and_traversal_upload_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            item = save_image(png(), root, 'user:1', 1024)
            self.assertIsNotNone(item)
            for ids, owner in [([item['id']], 'user:2'), (['../private.png'], 'user:1')]:
                with self.subTest(ids=ids, owner=owner), self.assertRaises(ValueError):
                    attach_images(ids, root, owner, 'video_' + 'b' * 32)
            self.assertFalse((root / 'tasks').exists())

    def test_invalid_or_oversized_image_creates_no_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            for data, limit in [(b'<svg>not an accepted image</svg>', 1024), (png(), 8)]:
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    save_image(data, Path(tmp), 'user:1', limit)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_task_reference_cannot_read_another_task_or_arbitrary_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            for ref in ['upload:../../secret.txt', 'upload:video_'+'a'*32+'/'+'b'*32+'.png']:
                with self.subTest(reference=ref), self.assertRaises(ValueError):
                    copy_task_image(ref, Path(tmp), 'video_'+'c'*32, Path(tmp)/'dest')


class UploadEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_server_shutdown_preserves_uploaded_images_for_task_recovery(self):
        tree = ast.parse((Path(__file__).resolve().parents[1] / 'server.py').read_text(encoding='utf-8'))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == '_run_task')
        entered = asyncio.Event()
        released = []
        async def acquire(*args): pass
        async def release(key): released.append(key)
        async def generate(*args, **kwargs):
            entered.set()
            await asyncio.Future()
        with tempfile.TemporaryDirectory() as tmp:
            item = save_image(png(), tmp, 'user:1', 1024)
            task_id = 'video_' + 'd' * 32
            refs = attach_images([item['id']], tmp, 'user:1', task_id)
            async def prepare(*args): return None, []
            namespace = dict(asyncio=asyncio, time=__import__('time'),
                config=SimpleNamespace(REFERENCE_UPLOAD_DIR=tmp),
                key_limiter=SimpleNamespace(acquire=acquire, release=release),
                pool=SimpleNamespace(generate_video=generate),
                store=SimpleNamespace(update=lambda *args, **kwargs: None, task_events=lambda task: [], get=lambda task: {}),
                submission_duration=submission_duration,
                download_reference_images=prepare, cleanup_task_images=cleanup_task_images,
                AllAccountsLimitedError=ValueError, AllAccountsQuotaBlockedError=ValueError,
                AllAccountsProxyBlockedError=ValueError)
            exec(compile(ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[])),
                'image-task-recovery', 'exec'), namespace)
            task = asyncio.create_task(namespace['_run_task'](task_id, 'seedance-2.0', 'test', None, 10, refs, {}))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(released, [None])
            self.assertTrue((Path(tmp) / 'tasks' / task_id / item['id']).is_file(),
                            'A stopped server must preserve reference images needed on recovery')

    async def test_authenticated_upload_reaches_video_worker_as_a_local_image(self):
        tree=ast.parse((Path(__file__).resolve().parents[1]/'server.py').read_text(encoding='utf-8'))
        names={'VideoGenRequest','TaskResponse','admin_generate','admin_reference_upload','admin_reference_config'}
        nodes=[n for n in tree.body if isinstance(n,(ast.ClassDef,ast.AsyncFunctionDef)) and n.name in names]
        self.assertEqual(len(nodes),len(names))
        application=FastAPI()
        records=[]
        runs=[]
        class Store:
            def create(self,*args,**kwargs): records.append((args,kwargs))
        def auth(key,authorization):
            if key not in ('fixture-admin','other-admin'): raise HTTPException(401,'unauthorized')
            return {'kind':'session','user':{'id':1 if key=='fixture-admin' else 2}}
        async def worker(*args): runs.append(args)
        async def request(path,data,key=None):
            messages=[]
            is_json=isinstance(data,dict)
            raw=json.dumps(data).encode() if is_json else data
            async def receive(): return {'type':'http.request','body':raw,'more_body':False}
            async def send(message): messages.append(message)
            headers=[(b'content-type',b'application/json' if is_json else b'image/png')]
            if key: headers.append((b'x-admin-key',key.encode()))
            await application(dict(type='http',asgi={'version':'3.0'},http_version='1.1',method='POST',
                scheme='http',path=path,raw_path=path.encode(),query_string=b'',root_path='',headers=headers,
                client=('127.0.0.1',1),server=('test',80)),receive,send)
            status=next(m['status'] for m in messages if m['type']=='http.response.start')
            body=json.loads(b''.join(m.get('body',b'') for m in messages))
            return status,body
        with tempfile.TemporaryDirectory() as tmp:
            config=SimpleNamespace(DEFAULT_MODEL='seedance-2.5',REFERENCE_UPLOAD_DIR=tmp,
                REFERENCE_IMAGE_MAX_BYTES=1024,REFERENCE_IMAGE_MAX_COUNT=2,MAX_PENDING_TASKS=10)
            namespace=dict(app=application,config=config,asyncio=asyncio,uuid=uuid,json=json,
                BaseModel=BaseModel,Field=Field,model_validator=model_validator,Header=Header,HTTPException=HTTPException,Request=Request,
                _admin_auth=auth,admin_owner=admin_owner,save_image=save_image,attach_images=attach_images,
                validate_video_parameters=validate_video_parameters,
                submission_duration=submission_duration,
                cleanup_task_images=cleanup_task_images,validate_reference_urls=media.validate_reference_urls,
                pool=SimpleNamespace(accounts=['fixture']),store=Store(),SUPPORTED_DURATIONS=(5,10,15,30),
                _resolve_ratio=lambda size,ratio:ratio,_anonymous_client=lambda:{'api_key_hash':None,'api_key_name':'test'},
                _run_task=worker,TaskQuotaExceeded=TaskQuotaExceeded,PendingTaskLimitExceeded=PendingTaskLimitExceeded)
            exec(compile(ast.fix_missing_locations(ast.Module(body=nodes,type_ignores=[])),'image-endpoints','exec'),namespace)
            self.assertEqual((await request('/api/admin/reference-images',png()))[0],401)
            self.assertEqual(list(Path(tmp).iterdir()),[])
            self.assertEqual((await request('/api/admin/reference-images',b'x'*1025,'fixture-admin'))[0],413)
            self.assertEqual((await request('/api/admin/reference-images',b'not an image','fixture-admin'))[0],422)
            status,uploaded=await request('/api/admin/reference-images',png(),'fixture-admin')
            self.assertEqual(status,201)
            payload={'prompt':'synthetic test','reference_uploads':[uploaded['id']]}
            self.assertEqual((await request('/api/admin/generate',payload,'other-admin'))[0],422)
            self.assertFalse(records)
            status,task=await request('/api/admin/generate',payload,'fixture-admin')
            self.assertEqual(status,200)
            await asyncio.sleep(0)
            self.assertEqual(len(runs),1)
            references=json.loads(records[0][1]['reference_images'])
            with patch.object(media.config,'REFERENCE_UPLOAD_DIR',tmp):
                temporary,files=await media.download_reference_images(references,task['id'])
            try:
                self.assertEqual(len(files),1)
                self.assertEqual(Path(files[0]).read_bytes(),png())
            finally:
                for path in files: Path(path).unlink()
                temporary.rmdir()
            self.assertEqual(runs[0][5],references)


if __name__ == '__main__':
    unittest.main()
