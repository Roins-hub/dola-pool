"""Prevent stale context, unsafe anchors and duplicate requests across gate changes."""
import base64
import asyncio
import copy
import hashlib
import io
import json
from types import SimpleNamespace
import pytest
from PIL import Image
from starlette.datastructures import FormData
import ai_preflight as gate
import server
from store import TaskStore
from test_prompt_analysis import ai_http

CONTEXT={'model':'seedance-2.5','duration':10,'ratio':'16:9','references':[]}

@pytest.mark.parametrize('change',[{'model':'seedance-2.0'},{'duration':30},{'ratio':'9:16'},
    {'references':[{'sha256':'a'*64,'role':'character'}]}])
def test_context_change_invalidates_token(change):
    record=gate.record_preflight_result('s','cat',{'status':'allow'},custom_fingerprint='fp',context=CONTEXT)
    ok,_,_=gate.validate_preflight_gate('s','cat',record['token'],custom_fingerprint='fp',context={**CONTEXT,**change})
    assert not ok
    ok,_,_=gate.validate_preflight_gate('s','cat',record['token'],custom_fingerprint='fp',context=CONTEXT,rules_version='changed')
    assert not ok


def test_repeated_unicode_quote_requires_occurrence():
    prompt='🐈 mèo chạy; mèo chạy'
    issues=[{'id':'a','quote':'mèo chạy','replacement':'mèo bước','occurrence':None}]
    assert gate.validate_issue_anchors(prompt,issues)[0]['applicable'] is False
    issues[0]['occurrence']=2
    assert gate.validate_issue_anchors(prompt,issues)[0]['applicable'] is True
    issues[0]['quote']='mèo bay'
    assert gate.validate_issue_anchors(prompt,issues)[0]['applicable'] is False


def test_overlapping_edits_not_applied_together():
    issues=[{'id':'a','quote':'mèo chạy','replacement':'mèo đi'},
            {'id':'b','quote':'chạy nhanh','replacement':'đi chậm'}]
    checked=gate.validate_issue_anchors('mèo chạy nhanh',issues)
    assert all(not issue['applicable'] for issue in checked)


def image_url(color):
    buf=io.BytesIO();Image.new('RGB',(12,12),color).save(buf,format='PNG')
    return 'data:image/png;base64,'+base64.b64encode(buf.getvalue()).decode()

@pytest.mark.asyncio
async def test_same_filename_different_bytes_invalidates():
    one,_=await server._reference_context([image_url('blue')],['character'])
    two,_=await server._reference_context([image_url('red')],['character'])
    assert one[0]['sha256']!=two[0]['sha256']
    assert one[0]['role']=='character'

@pytest.mark.asyncio
async def test_changed_remote_image_invalidates(tmp_path,monkeypatch):
    state={'color':'blue'}
    async def download(urls,task_id):
        root=tmp_path/task_id;root.mkdir();p=root/'ref.png';Image.new('RGB',(12,12),state['color']).save(p)
        return root,[str(p)]
    monkeypatch.setattr(server,'download_reference_images',download)
    one,_=await server._reference_context(['https://fixture.invalid/ref.png'],[])
    state['color']='red'
    two,_=await server._reference_context(['https://fixture.invalid/ref.png'],[])
    assert one!=two

@pytest.fixture
def studio(tmp_path,monkeypatch):
    store=TaskStore(str(tmp_path/'tasks.db'));monkeypatch.setattr(server,'store',store)
    monkeypatch.setattr(server,'ADMIN_SESSIONS',{'test':{'username':'owner'}})
    monkeypatch.setattr(server,'_admin_auth',lambda _:None)
    monkeypatch.setattr(server,'pool',SimpleNamespace(accounts={'a':{}},available=1,all_accounts_limited=False,all_accounts_quota_blocked=False))
    return store

@pytest.mark.asyncio
@pytest.mark.parametrize('status',['allow','review','block','invalid'])
async def test_text_only_token_gets_real_context_check(studio,ai_http,status):
    session=hashlib.sha256(b'test').hexdigest()[:24]
    token=gate.record_preflight_result(session,'cat',{'status':'allow'})['token']
    ai_http['response']={'status':status,'reason':'cần xem','suggestions':''}
    if status=='allow':
        checked=await server._ensure_video_preflight(session,'cat',token,False,CONTEXT)
        assert checked['status']=='allow'
    else:
        with pytest.raises(server.HTTPException) as error:
            await server._ensure_video_preflight(session,'cat',token,False,CONTEXT)
        assert error.value.status_code==(409 if status=='review' else 502 if status=='invalid' else 422)
    assert len(ai_http['requests'])==1
    assert not studio.recent_tasks(50)

@pytest.mark.asyncio
async def test_existing_receipt_skips_expired_gate_and_ai(studio,ai_http):
    task_id='video_'+hashlib.sha256(b'studio:owner:existing').hexdigest()[:32]
    studio.create(task_id,'seedance-2.5','cat','16:9',10)
    async def form():return FormData({'request_id':'existing','prompt':'cat','model':'seedance-2.5','duration':'10','ratio':'16:9','preflight_token':'expired'})
    result=await server.admin_create_video(SimpleNamespace(form=form),'test')
    assert result.id==task_id
    assert ai_http['requests']==[]
    assert len(studio.recent_tasks(50))==1

@pytest.mark.asyncio
@pytest.mark.parametrize('roles',['not json','{}','["invented"]'])
async def test_invalid_reference_roles_rejected_before_acceptance(studio,ai_http,roles):
    async def form():return FormData({'prompt':'cat','model':'seedance-2.5','duration':'10',
        'reference_roles':roles,'preflight_token':'missing'})
    with pytest.raises(server.HTTPException) as error:
        await server.admin_create_video(SimpleNamespace(form=form),'test')
    assert error.value.status_code==422
    assert not studio.recent_tasks(50)
    assert not ai_http['requests']

@pytest.mark.asyncio
async def test_multipart_preflight_verifies_actual_uploaded_bytes(studio,ai_http,monkeypatch):
    from httpx import ASGITransport
    # Retrieve the unpatched client class from the factory's closure, only for ASGI requests.
    from httpx._client import AsyncClient
    calls=[]
    async def worker(*args):calls.append(args)
    monkeypatch.setattr(server,'_run_task',worker)
    blue=base64.b64decode(image_url('blue').split(',',1)[1])
    red=base64.b64decode(image_url('red').split(',',1)[1])
    headers={'x-admin-key':'test'}
    async with AsyncClient(transport=ASGITransport(app=server.app),base_url='http://studio') as client:
        checked=await client.post('/api/admin/prompts/preflight',headers=headers,
            data={'analysis':json.dumps({'prompt':'cat','model':'seedance-2.5','duration':10,
                'ratio':'16:9','reference_roles':['character']})},files={'images':('same.png',blue,'image/png')})
        assert checked.status_code==200
        form={'prompt':'cat','model':'seedance-2.5','duration':'10','ratio':'16:9',
              'reference_roles':'["character"]','preflight_token':checked.json()['token'],'request_id':'same-image'}
        wrong=await client.post('/api/admin/videos/generations',headers=headers,data=form,
                               files={'images':('same.png',red,'image/png')})
        assert wrong.status_code==422 and not studio.recent_tasks(50)
        accepted=await client.post('/api/admin/videos/generations',headers=headers,data=form,
                                  files={'images':('same.png',blue,'image/png')})
        assert accepted.status_code==200
        assert len(studio.recent_tasks(50))==1
        # Same receipt is read-only after expiry and with no additional AI.
        before=len(ai_http['requests']);form['preflight_token']='expired'
        repeated=await client.post('/api/admin/videos/generations',headers=headers,data=form,
                                  files={'images':('same.png',blue,'image/png')})
        assert repeated.json()['id']==accepted.json()['id']
        assert len(ai_http['requests'])==before
        changed=await client.post('/api/admin/videos/generations',headers=headers,data=form,
                                 files={'images':('same.png',red,'image/png')})
        assert changed.status_code==409


@pytest.mark.asyncio
async def test_double_click_legacy_only_one_extra_ai_request(studio,ai_http,monkeypatch):
    ai_http['delay']=0.02
    session=hashlib.sha256(b'test').hexdigest()[:24]
    token=gate.record_preflight_result(session,'cat',{'status':'allow'})['token']
    calls=[]
    async def worker(*args):calls.append(args)
    monkeypatch.setattr(server,'_run_task',worker)
    async def form():return FormData({'request_id':'double-click','prompt':'cat','model':'seedance-2.5',
        'duration':'10','ratio':'16:9','preflight_token':token})
    request=SimpleNamespace(form=form)
    one,two=await asyncio.gather(server.admin_create_video(request,'test'),server.admin_create_video(request,'test'))
    await asyncio.sleep(0)
    assert one.id==two.id and len(studio.recent_tasks(50))==1
    assert len(calls)==1
    assert len(ai_http['requests'])==1


def test_explicit_empty_replacement_can_delete_exact_second_occurrence():
    prompt='🐈 Mèo chạy. Mèo chạy.'
    issue=gate.validate_issue_anchors(prompt,[{'id':'delete-repeat','quote':'Mèo chạy.','occurrence':2,'replacement':''}])[0]
    assert issue['applicable'] is True
    missing=gate.validate_issue_anchors(prompt,[{'id':'comment','quote':'Mèo chạy.','occurrence':2}])[0]
    assert missing['applicable'] is False
