"""Authenticated platform-scoped account operations for the Doubao adapter."""
import hashlib
import inspect
import json
import time
import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from providers.base import ProviderError
from reference_uploads import admin_owner


def create_admin_router(provider, admin_auth, proxy_choices=None, resolve_proxy=None, reference_root=None):
    router = APIRouter(prefix='/api/admin/platforms/doubao', tags=['doubao-admin'])

    async def identity(x_admin_key: str | None = Header(None), authorization: str | None = Header(None)):
        result = admin_auth(x_admin_key, authorization)
        return await result if inspect.isawaitable(result) else result

    async def body(request):
        try:
            value = await request.json()
            if not isinstance(value, dict) or value.get('platform', 'doubao') != 'doubao':
                raise ValueError()
            value={k:v for k,v in value.items() if k != 'platform'}
            if 'proxy_ref' in value:
                reference=value.pop('proxy_ref')
                if not resolve_proxy or not isinstance(reference,str):raise ValueError()
                try:
                    resolved=resolve_proxy(reference)
                    if inspect.isawaitable(resolved):resolved=await resolved
                except Exception:
                    raise ValueError() from None
                if not resolved or (value.get('proxy') and value['proxy']!=resolved):raise ValueError()
                value['proxy']=resolved
            return value
        except (ValueError, TypeError):
            raise HTTPException(422, {'error_code':'invalid_request','message':'请求格式或平台声明不正确。'}) from None

    async def call(fn, *args, **kwargs):
        try:
            result = fn(*args, **kwargs)
            return await result if inspect.isawaitable(result) else result
        except ProviderError as error:
            raise HTTPException(error.status_code, error.detail()) from None
        except TypeError:
            raise HTTPException(422, {'error_code':'invalid_request','message':'请求字段不正确。'}) from None

    @router.get('/capabilities')
    async def capabilities(_=Depends(identity)):
        return provider.capabilities()

    @router.get('/available-proxies')
    async def available_proxies(_=Depends(identity)):
        return {'platform':'doubao','proxies':await call(proxy_choices) if proxy_choices else []}

    @router.patch('/capabilities')
    async def update_capabilities(request:Request, _=Depends(identity)):
        return await call(provider.save_capabilities, await body(request))

    @router.get('/accounts')
    async def accounts(_=Depends(identity)):
        return {'platform':'doubao','accounts':provider.list_accounts(), 'quota_policy':provider.quota_policy(),
                'capabilities':provider.capabilities()}

    @router.post('/accounts/import-ck')
    async def import_account(request:Request, _=Depends(identity)):
        fields=await body(request)
        return await call(provider.import_ck, **fields)

    @router.post('/accounts/import-ck-batch')
    async def import_batch(request:Request, _=Depends(identity)):
        fields=await body(request)
        files=fields.get('files',[])
        if not isinstance(files,list) or not 1 <= len(files) <= 100:
            raise HTTPException(422,'一次导入 1～100 个豆包账号。')
        results=[]
        for index, item in enumerate(files):
            try:
                result=provider.import_ck(item.get('name') or ('doubao-'+uuid.uuid4().hex[:12]),
                    item.get('ck',item.get('content','')), item.get('proxy') or fields.get('proxy',''), item.get('category') or fields.get('category','default'))
                results.append({'index':index,'ok':True,'account':result})
            except ProviderError as error:
                results.append({'index':index,'ok':False,**error.detail()})
        return {'platform':'doubao','results':results,'imported':sum(r['ok'] for r in results)}

    def actor_hash(who):
        return hashlib.sha256(json.dumps(who,sort_keys=True,default=str).encode()).hexdigest()

    @router.post('/accounts/invalid-preview')
    async def invalid_preview(request:Request, who=Depends(identity)):
        await body(request)
        targets=[a['name'] for a in provider.list_accounts() if a['status']=='invalid']
        token=uuid.uuid4().hex
        provider._invalid_previews[token]={'actor':actor_hash(who),'targets':targets,'expires':time.time()+300}
        return {'platform':'doubao','token':token,'accounts':targets,'count':len(targets)}

    @router.post('/accounts/delete-invalid')
    async def delete_invalid(request:Request, who=Depends(identity)):
        fields=await body(request)
        preview=provider._invalid_previews.get(fields.get('token',''))
        current=[a['name'] for a in provider.list_accounts() if a['status']=='invalid']
        if not preview or preview['expires'] < time.time() or preview['actor']!=actor_hash(who) or sorted(current)!=sorted(preview['targets']):
            raise HTTPException(409,{'error_code':'preview_expired','message':'预览已过期、执行身份或目标账号发生变化，请重新预览。'})
        provider._invalid_previews.pop(fields['token'],None)
        results=[await call(provider.delete_account,name) for name in preview['targets']]
        return {'platform':'doubao','deleted':results,'count':len(results)}

    @router.post('/accounts/verify-batch')
    async def verify_batch(request:Request, _=Depends(identity)):
        fields=await body(request)
        names=fields.get('names') or [a['name'] for a in provider.list_accounts()]
        if not isinstance(names,list) or len(names)>100:
            raise HTTPException(422,'检查账号列表无效。')
        return {'platform':'doubao','results':[await call(provider.verify_account,name) for name in names]}

    @router.patch('/accounts/{name}/proxy')
    async def patch_proxy(name:str, request:Request, _=Depends(identity)):
        fields=await body(request)
        return await call(provider.patch_account,name,proxy=fields.get('proxy',''))

    @router.post('/accounts/{name}/verify')
    async def verify(name:str, _=Depends(identity)):
        return await call(provider.verify_account,name)

    @router.post('/accounts/{name}/calibrate')
    async def calibrate(name:str, request:Request, who=Depends(identity)):
        fields = await body(request)
        if fields.get('cast') or fields.get('cast_ids'):
            raise HTTPException(422, {'error_code': 'unsupported_cast',
                                      'message': '角色素材库已移除，请使用独立参考素材上传。'})
        fields = {key: value for key, value in fields.items() if key not in ('cast', 'cast_ids')}
        private_fields = {'upload_owner', 'upload_root', 'reference_image_paths',
                          'reference_media_paths', 'reference_upload_root'}
        if any(key.startswith('_') or key in private_fields for key in fields):
            raise HTTPException(422, {'error_code': 'invalid_request',
                                      'message': '验证请求只能提交素材上传凭据，不能指定本地路径。'})
        if fields.get('reference_uploads') or fields.get('reference_media_uploads'):
            if reference_root is None:
                raise HTTPException(503, {'error_code': 'reference_uploads_unavailable',
                                          'message': '豆包验证素材上传尚未配置，请更新项目服务。'})
            return await call(provider.start_calibration, name, fields,
                              upload_owner=admin_owner(who), upload_root=reference_root)
        return await call(provider.start_calibration, name, fields)

    @router.get('/calibration/{identifier}')
    async def calibration(identifier:str, _=Depends(identity)):
        return await call(provider.calibration_status,identifier)

    @router.patch('/accounts/{name}')
    async def patch_account(name:str, request:Request, _=Depends(identity)):
        return await call(provider.patch_account,name,**(await body(request)))

    @router.delete('/accounts/{name}')
    async def delete_account(name:str, _=Depends(identity)):
        return await call(provider.delete_account,name)

    @router.get('/trash')
    async def trash(_=Depends(identity)):
        return {'platform':'doubao','backups':provider.list_trash()}

    @router.post('/trash/restore')
    async def restore(request:Request, _=Depends(identity)):
        fields=await body(request)
        return await call(provider.restore_account,fields.get('token',''))

    @router.get('/quota-policy')
    async def quota(_=Depends(identity)):
        return {'platform':'doubao','quota_policy':provider.quota_policy()}

    @router.patch('/quota-policy')
    async def set_quota(request:Request, _=Depends(identity)):
        fields=await body(request)
        if set(fields)!={'limit'}:
            raise HTTPException(422,'请明确填写豆包本地次数 limit。')
        return {'platform':'doubao','quota_policy':await call(provider.quota_policy,fields['limit'])}

    @router.get('/categories')
    async def categories(_=Depends(identity)):
        counts={}
        for item in provider.list_accounts():
            group=counts.setdefault(item['category'],{'name':item['category'],'count':0,'enabled':0})
            group['count']+=1
            group['enabled']+=int(item['enabled'])
        return {'platform':'doubao','categories':list(counts.values())}

    @router.patch('/categories/{category}')
    async def category_patch(category:str, request:Request, _=Depends(identity)):
        fields=await body(request)
        if set(fields)!={'enabled'} or type(fields['enabled']) is not bool:
            raise HTTPException(422,'分类操作只支持明确启用或停用。')
        items=[await call(provider.patch_account,a['name'],enabled=fields['enabled']) for a in provider.list_accounts() if a['category']==category]
        return {'platform':'doubao','updated':len(items)}

    @router.post('/login')
    async def login(request:Request, _=Depends(identity)):
        return await call(provider.start_login,**(await body(request)))

    @router.post('/accounts/{name}/login')
    async def restore_login(name:str, request:Request, _=Depends(identity)):
        fields = await body(request)
        if set(fields) - {'mode'}:
            raise HTTPException(422, {'error_code':'invalid_request',
                                      'message':'恢复登录只能使用原账号及原线路，不能提交其他凭证或代理。'})
        return await call(provider.start_account_login, name, mode=fields.get('mode','browser'))

    @router.get('/login/{identifier}')
    async def login_status(identifier:str, _=Depends(identity)):
        return await call(provider.login_status,identifier)

    @router.post('/login/{identifier}/confirm')
    async def confirm_login(identifier:str, _=Depends(identity)):
        return await call(provider.confirm_login,identifier)

    @router.delete('/login/{identifier}')
    async def cancel_login(identifier:str, _=Depends(identity)):
        return await call(provider.cancel_login,identifier)

    return router
