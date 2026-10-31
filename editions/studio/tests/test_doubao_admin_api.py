import json
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI, HTTPException
from types import SimpleNamespace

from providers.doubao import DoubaoProvider
from providers.admin_api import create_admin_router
from test_doubao_provider import ck, PROXY


class ASGIClient:
    def __init__(self, app): self.app = app
    async def request(self, method, path, headers=None, json=None):
        import json as codec
        body = codec.dumps(json).encode() if json is not None else b''
        messages = []
        async def receive():
            return {'type':'http.request','body':body,'more_body':False}
        async def send(message): messages.append(message)
        scope={'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':method,
               'scheme':'http','path':path,'raw_path':path.encode(),'query_string':b'',
               'root_path':'','headers':[(k.lower().encode(),v.encode()) for k,v in
               {'Content-Type':'application/json',**(headers or {})}.items()],
               'server':('fixture',80),'client':('127.0.0.1',1234)}
        await self.app(scope,receive,send)
        start=next(m for m in messages if m['type']=='http.response.start')
        raw=b''.join(m.get('body',b'') for m in messages if m['type']=='http.response.body')
        return SimpleNamespace(status_code=start['status'],text=raw.decode(),json=lambda:codec.loads(raw))
    async def get(self,path,**kwargs): return await self.request('GET',path,**kwargs)
    async def post(self,path,**kwargs): return await self.request('POST',path,**kwargs)
    async def patch(self,path,**kwargs): return await self.request('PATCH',path,**kwargs)
    async def delete(self,path,**kwargs): return await self.request('DELETE',path,**kwargs)


class AdminApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.provider = DoubaoProvider(root=Path(self.temp.name), enabled=False)
        def auth(key, authorization=None):
            if key != 'fixture-admin': raise HTTPException(401, 'admin required')
            return {'kind': 'admin_key', 'role': 'admin'}
        app = FastAPI()
        app.include_router(create_admin_router(self.provider, auth))
        self.client = ASGIClient(app)
        self.headers = {'X-Admin-Key': 'fixture-admin'}

    async def test_every_account_route_authenticates_and_import_returns_no_secrets(self):
        path = '/api/admin/platforms/doubao/accounts'
        self.assertEqual((await self.client.get(path)).status_code, 401)
        result = await self.client.post(path+'/import-ck', headers=self.headers,
                                        json={'name':'one','ck':ck(),'proxy':PROXY})
        self.assertEqual(result.status_code, 200)
        self.assertNotIn('secret', result.text)
        self.assertNotIn('session-one', result.text)
        listing = (await self.client.get(path,headers=self.headers)).json()
        self.assertEqual([a['name'] for a in listing['accounts']], ['one'])
        self.assertFalse(listing['capabilities']['enabled'])

    async def test_same_named_recycle_restore_and_quota_policy_stay_in_provider(self):
        self.provider.import_ck('one', ck(), PROXY)
        base = '/api/admin/platforms/doubao'
        result = await self.client.patch(base+'/quota-policy',headers=self.headers,json={'limit':7})
        self.assertEqual(result.json()['quota_policy']['limit'],7)
        result = await self.client.delete(base+'/accounts/one',headers=self.headers)
        self.assertEqual(result.status_code,200)
        trash=(await self.client.get(base+'/trash',headers=self.headers)).json()['backups']
        self.assertEqual(trash[0]['name'],'one')
        result=await self.client.post(base+'/trash/restore',headers=self.headers,json={'token':trash[0]['token']})
        self.assertEqual(result.status_code,200)
        self.assertEqual(len(self.provider.list_accounts()),1)

    async def test_wrong_platform_declaration_rejected_and_bad_proxy_redacted(self):
        path='/api/admin/platforms/doubao/accounts/import-ck'
        result=await self.client.post(path,headers=self.headers,json={'platform':'dola','name':'one','ck':ck(),'proxy':PROXY})
        self.assertEqual(result.status_code,422)
        result=await self.client.post(path,headers=self.headers,json={'name':'one','ck':ck(),'proxy':'http://user:secret@127.0.0.1:9'})
        self.assertEqual(result.status_code,503)
        self.assertNotIn('secret',result.text)

    async def test_invalid_preview_is_bound_to_current_target_set(self):
        self.provider.import_ck('one', ck(), PROXY)
        base='/api/admin/platforms/doubao'
        result=await self.client.post(base+'/accounts/invalid-preview',headers=self.headers,json={})
        self.assertEqual(result.status_code,200)
        self.assertEqual(result.json()['accounts'],[])
        self.assertEqual(len(self.provider.list_accounts()),1)

    async def test_existing_isp_reference_resolves_privately_without_exposing_password(self):
        app=FastAPI()
        app.include_router(create_admin_router(self.provider,lambda *_:{'kind':'admin_key'},
            proxy_choices=lambda:[{'id':'line-1','label':'德国 ISP'}], resolve_proxy=lambda identifier:PROXY if identifier=='line-1' else ''))
        client=ASGIClient(app)
        choices=await client.get('/api/admin/platforms/doubao/available-proxies')
        self.assertNotIn('secret',choices.text)
        result=await client.post('/api/admin/platforms/doubao/accounts/import-ck',json={'name':'one','ck':ck(),'proxy_ref':'line-1'})
        self.assertEqual(result.status_code,200,result.text)
        self.assertNotIn('secret',result.text)


if __name__=='__main__': unittest.main()
