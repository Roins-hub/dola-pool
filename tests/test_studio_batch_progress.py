"""Persisted batch acceptance and truthful upstream progress; no live services."""
import asyncio
import hashlib
import json
from types import SimpleNamespace
import pytest
from starlette.datastructures import FormData
import server
from store import TaskStore, PendingTaskLimitExceeded
from ai_preflight import record_preflight_result
from pure_api_gen import upstream_poll_update

@pytest.fixture
def studio(tmp_path,monkeypatch):
 store=TaskStore(str(tmp_path/'tasks.db'));monkeypatch.setattr(server,'store',store)
 monkeypatch.setattr(server,'_admin_auth',lambda _:None)
 monkeypatch.setattr(server,'ADMIN_SESSIONS',{'test':{'username':'owner'}})
 monkeypatch.setattr(server,'pool',SimpleNamespace(accounts={'a':{}},available=1,all_accounts_limited=False,all_accounts_quota_blocked=False))
 calls=[]
 async def worker(*args):calls.append(args)
 monkeypatch.setattr(server,'_run_task',worker)
 return store,calls

def request(prompts,request_id='batch-request',copies=2,status='allow'):
 session=hashlib.sha256(b'test').hexdigest()[:24]
 entries=[]
 for prompt in prompts:
  token=record_preflight_result(session,prompt,{'status':status,'reason':'reason'})['token']
  entries.append({'prompt':prompt,'preflight_token':token})
 data={'request_id':request_id,'prompts':entries,'copies':copies,'model':'seedance-2.5','duration':10,'ratio':'16:9'}
 async def form():return FormData({'batch':json.dumps(data)})
 return SimpleNamespace(form=form),data

@pytest.mark.asyncio
async def test_batch_durable_and_request_retry(studio):
 store,calls=studio;req,data=request(['cat','boat'])
 result=await server.admin_create_batch(req,'test')
 assert len(store.batch_tasks(result['batch_id']))==4
 # Workers have not run while the full batch is already persisted.
 assert not calls
 await asyncio.sleep(0);assert len(calls)==4
 data['prompts'][0]['preflight_token']='expired'
 retry=await server.admin_create_batch(req,'test')
 assert retry==result
 await asyncio.sleep(0);assert len(calls)==4
 reopened=TaskStore(store._conn.execute('PRAGMA database_list').fetchone()[2])
 assert reopened.batch_fingerprint(result['batch_id'])
 assert len(reopened.recoverable_queued_tasks())==4
 for row in store.batch_tasks(result['batch_id']):store.delete_task(row['id'])
 deleted=await server.admin_create_batch(req,'test');assert deleted['tasks']==[]
 await asyncio.sleep(0);assert len(calls)==4

@pytest.mark.asyncio
async def test_changed_batch_conflicts_without_duplicate(studio):
 req,_=request(['cat']);await server.admin_create_batch(req,'test');await asyncio.sleep(0)
 changed,_=request(['boat'])
 with pytest.raises(server.HTTPException) as err:await server.admin_create_batch(changed,'test')
 assert err.value.status_code==409
 assert len(studio[1])==2

@pytest.mark.asyncio
@pytest.mark.parametrize('status',['block','review'])
async def test_batch_preflight_gates_whole_batch(studio,status):
 req,_=request(['cat','boat'],status=status)
 with pytest.raises(server.HTTPException) as err:await server.admin_create_batch(req,'test')
 assert err.value.status_code==422
 assert studio[0].recent_tasks(50)==[] and not studio[1]

@pytest.mark.asyncio
async def test_capacity_failure_has_no_partial_batch(studio,monkeypatch):
 monkeypatch.setattr(server.config,'MAX_PENDING_TASKS',3)
 req,_=request(['cat','boat'])
 with pytest.raises(server.HTTPException) as err:await server.admin_create_batch(req,'test')
 assert err.value.status_code==429
 assert not studio[0].recent_tasks(50) and not studio[1]

@pytest.mark.asyncio
async def test_batch_rejects_more_than_50_jobs(studio):
 req,_=request(['cat']*6,copies=10)
 with pytest.raises(server.HTTPException) as err:await server.admin_create_batch(req,'test')
 assert err.value.status_code==422
 assert not studio[0].recent_tasks(50)

def test_upstream_payload_is_bounded_and_has_no_fake_percent():
 result=upstream_poll_update(SimpleNamespace(status='pending',texts=['working','working','sessionid=secret https://example.invalid/?token=abc'],
  creation_statuses=['creation status=1'],wait_minutes='3分钟',source_data={'progress':67,'cookie':'secret'}))
 assert result['wait_minutes']=='3 phút'
 assert result['texts']==['working','sessionid=[đã ẩn] [liên kết]']
 assert 'source_data' not in result and 'percent' not in result

def test_progress_history_deduplicates_and_survives_restart(tmp_path):
 path=str(tmp_path/'tasks.db');store=TaskStore(path);store.create('job','seedance-2.5','cat','16:9',10)
 update=upstream_poll_update(SimpleNamespace(texts=['working'],status='pending'))
 store.record_poll('job',1,update);store.record_poll('job',2,update)
 assert len(json.loads(store.get('job')['upstream_updates']))==1
 store.record_poll('job',3,{'connection':'interrupted'})
 assert json.loads(store.get('job')['upstream_updates'])[-1]['texts']==['working']
 store.record_poll('job',4,update)
 for i in range(30):store.record_poll('job',5+i,{**update,'texts':[str(i)]})
 reopened=TaskStore(path);history=json.loads(reopened.get('job')['upstream_updates'])
 assert len(history)==20 and history[-1]['texts']==['29']
 assert reopened.get('job')['last_poll_at']==34
