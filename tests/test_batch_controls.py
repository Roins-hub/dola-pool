"""Durable operator actions never resubmit an uncertain conversation."""
import asyncio
import pytest
import server
from store import TaskStore
from test_studio_batch_progress import studio, request
from pathlib import Path
import subprocess

def test_old_retry_response_does_not_overwrite_edited_prompt():
    html=(Path(__file__).resolve().parents[1]/'web/index.html').read_text()
    assert '// retry-controls-start' in html
    script=html.split('// retry-controls-start',1)[1].split('// retry-controls-end',1)[0]
    js="""let resolve; const input={value:'Giữ cảnh'};
    const q=()=>input; const api=()=>new Promise(r=>resolve=r);
    const localStorage={setItem(){},getItem(){return null}};
    const document={querySelectorAll(){return []}};
    const esc=x=>x; const saveStudioDraft=()=>{};
    """+script+"""
    retryRows=[{source_task_id:'old',original_prompt:'cat',prompt:'cat',selected:true,history:[]}];
    renderRetryRows=()=>{}; retryMessage=()=>{};
    (async()=>{const work=prepareRetryRows(true);await Promise.resolve();editRetryPrompt(0,'boat');
    resolve({prompt:'stale cat',status:'allow',token:'old'});await work;
    if(retryRows[0].prompt!=='boat'||retryRows[0].checked)process.exit(1);})().catch(e=>{console.error(e);process.exit(1)});
    """
    result=subprocess.run(['node','-e',js],text=True,capture_output=True)
    assert result.returncode==0,result.stderr

def test_pause_cancel_and_submission_claim_are_atomic_and_durable(tmp_path):
    store=TaskStore(str(tmp_path/'tasks.db'))
    store.create_batch('b','fp',[{'id':'a','prompt':'cat'},{'id':'sent','prompt':'boat'}])
    store.set_batch_paused('b',True)
    assert not store.begin_submission('a',account='one')
    reopened=TaskStore(str(tmp_path/'tasks.db'))
    assert reopened.batch_paused('b')
    store.set_batch_paused('b',False)
    assert store.begin_submission('sent',account='one')
    assert store.cancel_unsent(['a','sent'])==['a']
    assert not store.begin_submission('a',account='two')
    assert store.get('sent')['status']=='processing'
    assert store.get('a')['status']=='cancelled'

@pytest.mark.asyncio
async def test_controls_reject_unknown_batch_and_never_cancel_sent(studio):
    store,_=studio
    store.create('sent','seedance-2.5','cat','16:9',15)
    store.update('sent',status='unknown',conversation_id='old',submission_started_at=123)
    result=await server.admin_cancel_tasks(server.TaskSelection(ids=['sent']), 'test')
    assert result['cancelled']==[] and store.get('sent')['status']=='unknown'
    with pytest.raises(server.HTTPException):
        await server.admin_control_batch('missing',server.BatchControl(paused=True),'test')

@pytest.mark.asyncio
async def test_retry_preview_rejects_unknown_and_submission_is_idempotent(studio):
    store,calls=studio
    store.create('old','seedance-2.5','cat','16:9',15)
    store.update('old',status='unknown',conversation_id='conv')
    with pytest.raises(server.HTTPException):
        await server.admin_retry_preview(server.RetryDraft(source_task_id='old',prompt='cat'),'test')
    store.update('old',status='failed')
    draft=server.RetryDraft(source_task_id='old',prompt='cat')
    checked=await server.admin_retry_preview(draft,'test')
    body=server.RetryBatch(request_id='retry',entries=[server.RetryEntry(**draft.model_dump(),preflight_token=checked['token'])])
    first=await server.admin_retry_batch(body,'test')
    await asyncio.sleep(0)
    assert len(calls)==1
    again=await server.admin_retry_batch(body,'test')
    await asyncio.sleep(0)
    assert again==first and len(calls)==1
    assert store.batch_tasks(first['batch_id'])[0]['source_task_id']=='old'
    assert store.get('old')['status']=='failed'

@pytest.mark.asyncio
async def test_retry_changed_prompt_blocked_row_and_concurrent_acceptance(studio):
    store,calls=studio
    store.create('old','seedance-2.5','cat','16:9',15);store.update('old',status='failed')
    checked=await server.admin_retry_preview(server.RetryDraft(source_task_id='old',prompt='cat'),'test')
    wrong=server.RetryBatch(request_id='wrong',entries=[server.RetryEntry(source_task_id='old',prompt='boat',preflight_token=checked['token'])])
    with pytest.raises(server.HTTPException):await server.admin_retry_batch(wrong,'test')
    assert not calls and store.batch_fingerprint('wrong') is None
    body=server.RetryBatch(request_id='concurrent',entries=[server.RetryEntry(source_task_id='old',prompt='cat',preflight_token=checked['token'])])
    first,second=await asyncio.gather(server.admin_retry_batch(body,'test'),server.admin_retry_batch(body,'test'))
    await asyncio.sleep(0)
    assert first==second and len(calls)==1

@pytest.mark.asyncio
async def test_retry_preserves_reference_bytes_roles_and_independent_ownership(studio):
    import json
    from test_folder_batch_output import image
    store,_=studio
    paths=server.materialize_data_urls([image('red')])
    store.create('old','seedance-2.5','cat','16:9',15,reference_images=json.dumps(paths))
    store.update('old',status='failed',reference_roles='["character"]')
    checked=await server.admin_retry_preview(server.RetryDraft(source_task_id='old',prompt='cat'),'test')
    body=server.RetryBatch(request_id='image-retry',entries=[server.RetryEntry(source_task_id='old',prompt='cat',preflight_token=checked['token'])])
    result=await server.admin_retry_batch(body,'test')
    child=store.batch_tasks(result['batch_id'])[0]
    child_path=json.loads(child['reference_images'])[0]
    assert child_path!=paths[0] and Path(child_path).read_bytes()==Path(paths[0]).read_bytes()
    assert child['reference_roles']=='["character"]'
    server.cleanup_local_references([child_path])
    assert Path(paths[0]).exists()

def test_paused_batch_is_not_failed_by_watchdog(studio):
    store,_=studio
    store.create_batch('paused','fp',[{'id':'waiting','prompt':'cat'}])
    store.set_batch_paused('paused',True)
    store._conn.execute('UPDATE tasks SET updated_at=1');store._conn.commit()
    assert server._watchdog_plan()==[]
    assert store.get('waiting')['status']=='queued'

def test_watchdog_cannot_overwrite_cancel_or_a_submission_claim(tmp_path):
    store=TaskStore(str(tmp_path/'tasks.db'))
    for id in ('cancelled','sent','waiting'):store.create(id,'seedance-2.5','cat','16:9',15)
    store.cancel_unsent(['cancelled']);store.begin_submission('sent',account='one')
    assert not store.fail_unsent_waiting('cancelled','old timeout')
    assert not store.fail_unsent_waiting('sent','old timeout')
    assert store.fail_unsent_waiting('waiting','timeout')
    assert store.get('cancelled')['status']=='cancelled' and store.get('sent')['status']=='processing'

@pytest.mark.asyncio
async def test_terminal_worker_retires_before_another_pool_attempt(studio):
    store,_=studio
    store.create('old','seedance-2.5','cat','16:9',15);store.update('old',status='failed')
    assert await server._wait_task_submission('old') is False

def test_expired_failure_cleanup_returns_original_references(tmp_path):
    store=TaskStore(str(tmp_path/'tasks.db'))
    store.create('old','seedance-2.5','cat','16:9',15,reference_images='["refs/original.png"]')
    store.update('old',status='failed',finished_at=1)
    rows=store.prune_finished(1)
    assert rows[0]['reference_images']=='["refs/original.png"]'
