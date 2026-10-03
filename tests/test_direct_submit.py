import asyncio
from types import SimpleNamespace
import pytest
from starlette.datastructures import FormData
import server
from test_studio_batch_progress import studio,request

@pytest.mark.asyncio
async def test_direct_admin_single_skips_ai_and_keeps_idempotency(studio,monkeypatch):
    async def unavailable(*a,**kw):raise AssertionError('Create must not call AI')
    monkeypatch.setattr(server,'preflight_check_prompt',unavailable)
    async def form():return FormData({'prompt':'cat','model':'seedance-2.5','duration':'15','ratio':'16:9','request_id':'direct','skip_preflight':'true'})
    req=SimpleNamespace(form=form)
    first=await server.admin_create_video(req,'test')
    again=await server.admin_create_video(req,'test');await asyncio.sleep(0)
    assert first.id==again.id and len(studio[1])==1

@pytest.mark.asyncio
async def test_direct_admin_batch_skips_ai_and_creates_all_rows(studio,monkeypatch):
    async def unavailable(*a,**kw):raise AssertionError('Create must not call AI')
    monkeypatch.setattr(server,'preflight_check_prompt',unavailable)
    req,data=request(['cat','boat'],request_id='direct-batch');data['skip_preflight']=True
    for p in data['prompts']:p['preflight_token']=''
    first=await server.admin_create_batch(req,'test');await asyncio.sleep(0)
    assert len(studio[0].batch_tasks(first['batch_id']))==4 and len(studio[1])==4

def test_create_buttons_never_invoke_optional_ai_check():
    from pathlib import Path
    s=(Path(__file__).resolve().parents[1]/'web/index.html').read_text()
    single=s.split('async function doSubmitGenerate(){',1)[1].split('function trackStudioJob',1)[0]
    batch=s.split('async function submitVideoBatch(){',1)[1].split('function generationSnapshot',1)[0]
    assert 'doPreflightCheck()' not in single and 'checkBatchPrompts()' not in batch
    assert "fd.append('skip_preflight','true')" in single and 'skip_preflight:true' in batch
