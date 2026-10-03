"""Explicit design details checked during the final completion audit."""
import asyncio,hashlib,json,sqlite3
from pathlib import Path
from types import SimpleNamespace
import pytest
from starlette.datastructures import FormData
import server
from store import TaskStore
from test_studio_batch_progress import studio,request
from test_account_inventory import make_pool
from ai_preflight import record_preflight_result

@pytest.mark.asyncio
async def test_batch_name_persists_after_reopen_and_safe_zip_filename(studio):
    req,data=request(['cat'],request_id='named');data['name']='../Bản thử / A'
    result=await server.admin_create_batch(req,'test');await asyncio.sleep(0)
    batch_id=result['batch_id'];store=studio[0]
    reopened=TaskStore(store._conn.execute('PRAGMA database_list').fetchone()[2])
    assert reopened.batch_name(batch_id)=='../Bản thử / A'
    detail=await server.admin_get_batch(batch_id,'test')
    assert detail['name']=='../Bản thử / A'
    assert '/' not in detail['download_name'] and '..' not in detail['download_name']
    data['name']='Changed after unknown POST'
    assert await server.admin_create_batch(req,'test')==result
    assert store.batch_name(batch_id)=='../Bản thử / A'


def test_batch_name_migration_keeps_old_receipt(tmp_path):
    path=tmp_path/'old.db'
    with sqlite3.connect(path) as c:
        c.execute('CREATE TABLE video_batches(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,created_at REAL)')
        c.execute("INSERT INTO video_batches VALUES ('old','fingerprint',123)")
    store=TaskStore(str(path))
    assert store.batch_fingerprint('old')=='fingerprint' and store.batch_name('old')==''

@pytest.mark.asyncio
async def test_diagnostic_time_and_kind_filters(studio):
    store=studio[0];store.create('old','seedance-2.5','cat','16:9',15)
    store.update('old',status='failed',error='Failure',finished_at=123,updated_at=456)
    result=await server.admin_diagnostic_events(task_id='old',after=100,before=200,kind='failure',x_admin_key='test')
    assert len(result['events'])==1 and result['events'][0]['time']==123
    assert not (await server.admin_diagnostic_events(task_id='old',after=200,x_admin_key='test'))['events'] or all(e['time']>=200 for e in (await server.admin_diagnostic_events(task_id='old',after=200,x_admin_key='test'))['events'])
    with pytest.raises(server.HTTPException):await server.admin_diagnostic_events(after=float('nan'),x_admin_key='test')

@pytest.mark.asyncio
async def test_repaired_prompt_keeps_failed_task_link(studio):
    store=studio[0];store.create('refused','seedance-2.5','old','16:9',15);store.update('refused',status='failed')
    context=server._video_context('seedance-2.5',15,'16:9',[])
    token=record_preflight_result(hashlib.sha256(b'test').hexdigest()[:24],'edited',{'status':'allow'},context=context)['token']
    async def form():return FormData({'request_id':'repair-linked','prompt':'edited','model':'seedance-2.5','duration':'15','ratio':'16:9','preflight_token':token,'source_task_id':'refused'})
    result=await server.admin_create_video(SimpleNamespace(form=form),'test');await asyncio.sleep(0)
    assert store.get(result.id)['source_task_id']=='refused'
    assert (await server.admin_get_task(result.id,'test'))['source_task_id']=='refused'


def test_legacy_facebook_cookie_identity_not_reimported(tmp_path):
    pool=make_pool(tmp_path)
    name=next(iter(pool.accounts));directory=pool.accounts_dir/name
    (directory/'state.json').write_text(json.dumps({'cookies':[{'name':'c_user','value':'123456789','domain':'.facebook.com'}]}))
    preview=server._preview_account_import(pool,'c_user=123456789; xs=test')
    assert preview['rows'][0]['status']=='duplicate'

@pytest.mark.asyncio
async def test_repaired_prompt_copies_keep_link_and_invalid_source_never_submits(studio):
    store,calls=studio;store.create('refused','seedance-2.5','old','16:9',15);store.update('refused',status='failed')
    req,data=request(['edited'],request_id='linked-copies');data['source_task_id']='refused'
    result=await server.admin_create_batch(req,'test');await asyncio.sleep(0)
    assert all(r['source_task_id']=='refused' for r in store.batch_tasks(result['batch_id']))
    req,data=request(['edited'],request_id='invalid-source');data['source_task_id']='missing'
    with pytest.raises(server.HTTPException) as e:await server.admin_create_batch(req,'test')
    assert e.value.status_code==422 and len(calls)==2
