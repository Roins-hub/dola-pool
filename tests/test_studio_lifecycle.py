"""Behavior checks for uncertain results; no live providers or account data."""
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
import pytest
import config
import pure_api_gen as pure
import server
from store import TaskStore
from browser_pool import BrowserPool

@pytest.fixture
def task_store(tmp_path,monkeypatch):
 s=TaskStore(str(tmp_path/'tasks.db'));monkeypatch.setattr(server,'store',s)
 server.ACTIVE_STUDIO_JOBS.clear();server.WATCHDOG_DISPATCHED.clear();server.WATCHDOG_RETRIES.clear()
 return s

def test_restart_preserves_accepted_and_quarantines_unknown_send(task_store):
 for name in ['accepted','sending','waiting']:
  task_store.create(name,'seedance-2.5','cat','16:9',10)
 task_store.update('accepted',status='processing',account='mock1',conversation_id='c1',deadline_at=time.time()+500)
 task_store.update('sending',status='processing',account='mock2',submission_started_at=time.time())
 task_store.recover_runtime_jobs()
 assert task_store.get('accepted')['account']=='mock1'
 assert [r['id'] for r in task_store.recoverable_tasks()]==['accepted']
 assert task_store.get('sending')['status']=='unknown'
 assert [r['id'] for r in task_store.recoverable_queued_tasks()]==['waiting']

@pytest.mark.asyncio
async def test_timeout_keeps_conversation_not_failed(task_store,monkeypatch):
 task_store.create('job','seedance-2.5','cat','16:9',10)
 async def slow(*a,**k):
  k['on_account_try']('mock1',1);k['on_conversation_id']('mock1','c1',time.time()+1800)
  raise TimeoutError('still generating')
 monkeypatch.setattr(server.pool,'generate_video',slow)
 await server._run_task('job','seedance-2.5','cat','16:9',10,[],{})
 row=task_store.get('job');assert row['status']=='unknown'
 assert row['account']=='mock1' and row['conversation_id']=='c1'
 assert not server._watchdog_plan()

@pytest.mark.asyncio
async def test_cancel_preserves_account(task_store,monkeypatch):
 task_store.create('job','seedance-2.5','cat','16:9',10)
 async def cancelled(*a,**k):
  k['on_account_try']('mock1',1);k['on_conversation_id']('mock1','c1',time.time()+1800)
  raise asyncio.CancelledError()
 monkeypatch.setattr(server.pool,'generate_video',cancelled)
 with pytest.raises(asyncio.CancelledError):await server._run_task('job','seedance-2.5','cat','16:9',10,[],{})
 assert task_store.get('job')['account']=='mock1'
 assert task_store.get('job')['conversation_id']=='c1'

@pytest.mark.asyncio
async def test_manual_check_and_resume_only_existing(task_store,monkeypatch,tmp_path):
 task_store.create('job','seedance-2.5','cat','16:9',10)
 task_store.update('job',status='unknown',account='mock1',conversation_id='c1',deadline_at=time.time()-10)
 monkeypatch.setattr(server,"_admin_auth",lambda _:None)
 calls=[]
 async def resume(account,conv,timeout,**k):calls.append((account,conv));raise TimeoutError()
 async def forbidden(*a,**k):raise AssertionError('must never submit')
 monkeypatch.setattr(server.pool,'resume_video',resume);monkeypatch.setattr(server.pool,'generate_video',forbidden)
 await server.admin_check_result('job',x_admin_key=None);await asyncio.sleep(.02)
 assert calls==[('mock1','c1')]
 assert task_store.get('job')['status']=='unknown'

@pytest.mark.asyncio
async def test_ambiguous_submission_never_rotates(tmp_path,monkeypatch):
 pool=BrowserPool(accounts_dir=str(tmp_path/'accounts'),db_path=str(tmp_path/'pool.db'))
 for a in ['a','b']:(tmp_path/'accounts'/a).mkdir(parents=True);pool.ensure_account(a);pool.set_login_status(a,True)
 async def probe(a):return {'ok':True,'probed':False}
 calls=[]
 async def generate(account,*a,**k):calls.append(account);raise pure.PendingResultError('missing ack')
 monkeypatch.setattr(pool,'probe_hello_async',probe);monkeypatch.setattr(pool,'_generate_effective',generate)
 with pytest.raises(pure.PendingResultError):await pool.generate_video('cat',duration=10,model='seedance-2.5',max_attempts=3)
 assert calls==['a']
 assert all(not a.get('abnormal_until') for a in pool.list_accounts())

def test_after_300_seconds_pending_still_polls_same_conversation(monkeypatch):
 clock=[0];calls=[]
 def poll(*a):
  calls.append(a[-1]);return SimpleNamespace(status='pending' if len(calls)<4 else 'succeeded',texts=[],vids=[],urls=[],source_data=None)
 monkeypatch.setattr(pure.time,'time',lambda:clock[0]);monkeypatch.setattr(pure.time,'sleep',lambda s:clock.__setitem__(0,clock[0]+150))
 fake=SimpleNamespace(inspect_video_once=poll,poll_has_duration_split=lambda *a,**k:False)
 result,_=pure._wait_video_same_conversation(dola=fake,session=None,ctx=None,headers={},conversation_id='c1',account='a',duration=10,deadline=1800,poll_interval=150,on_poll=None)
 assert clock[0]==450 and result.status=='succeeded' and calls==['c1']*4

def test_observation_deadline_is_uncertain(monkeypatch):
 fake=SimpleNamespace(inspect_video_once=lambda *a:SimpleNamespace(status='pending',texts=[],vids=[],urls=[],source_data=None),poll_has_duration_split=lambda *a,**k:False)
 with pytest.raises(pure.PendingResultError):pure._wait_video_same_conversation(dola=fake,session=None,ctx=None,headers={},conversation_id='c1',account='a',duration=10,deadline=time.time()-1,poll_interval=0,on_poll=None)

def test_rewrite_instructions_limit():
 from pydantic import ValidationError
 assert server.PromptRewriteRequest(prompt='cat').instructions==''
 with pytest.raises(ValidationError):server.PromptRewriteRequest(prompt='cat',instructions='x'*4001)

@pytest.mark.asyncio
async def test_rewrite_preserves_custom_instruction(monkeypatch):
 import ai_provider
 capture={}
 async def ai(**k):capture.update(k);return 'edited cat'
 monkeypatch.setattr(ai_provider,'call_active_ai',ai)
 assert await ai_provider.rewrite_prompt_text('cat',instructions='Giữ hai cảnh, giảm lời văn')=='edited cat'
 assert 'Giữ hai cảnh' in capture['prompt']
 assert 'KHÔNG chia nhỏ' not in capture['system_instruction']

@pytest.mark.asyncio
async def test_facebook_existing_session_exports_pure_state(tmp_path,monkeypatch,capsys):
 import add_fb_account as fb
 pool=BrowserPool(accounts_dir=str(tmp_path/'accounts'),db_path=str(tmp_path/'fb.db'))
 monkeypatch.chdir(tmp_path);monkeypatch.setattr(fb,'pool',pool);monkeypatch.setattr(fb,'proxy_kwargs_for',lambda *a:None)
 class Context:
  async def cookies(self,*a):return [{'name':'sessionid','value':'fake-session','domain':'.dola.com','path':'/'}]
  async def storage_state(self,path):Path(path).write_text(json.dumps({'cookies':await self.cookies()}))
  async def close(self):pass
 class Chromium:
  async def launch_persistent_context(self,*a,**k):return Context()
 class Playwright:
  async def __aenter__(self):return SimpleNamespace(chromium=Chromium())
  async def __aexit__(self,*a):pass
 monkeypatch.setattr(fb,'async_playwright',Playwright)
 result=await fb.add_fb_account_flow('fb-new','c_user=123;xs=fake;')
 assert result['ok'] and pool.is_pure_account('fb-new')
 assert (tmp_path/'accounts/fb-new/cookie_state.json').stat().st_mode&0o777==0o600
 assert 'fake-session' not in capsys.readouterr().out

@pytest.mark.asyncio
async def test_request_retry_returns_same_task_without_second_dispatch(task_store,monkeypatch):
 calls=[]
 def dispatch(coro):calls.append(1);coro.close()
 monkeypatch.setattr(server.asyncio,'create_task',dispatch)
 # Existing persisted job is returned before pool selection/materialization.
 task_store.create('video_request','seedance-2.5','cat','16:9',10)
 client={'api_key_hash':'h','api_key_name':'t','allowed_durations':[5,10,15,30],'daily_limit':0,'concurrency_limit':0}
 result=await server._submit_task(client,'seedance-2.5','cat','16:9',10,[],task_id='video_request')
 assert result.id=='video_request' and not calls
 with pytest.raises(server.HTTPException) as error:
  await server._submit_task(client,'seedance-2.5','different','16:9',10,[],task_id='video_request')
 assert error.value.status_code==409

def test_proxy_keeps_explicit_route_even_if_unreachable():
 import importlib.util
 # Other old tests install module stubs; load the real parser explicitly.
 spec=importlib.util.spec_from_file_location('studio_browser_parser',Path(__file__).resolve().parents[1]/'browser.py')
 module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
 assert module._kwargs_from_url('http://127.0.0.1:1')['server']=='http://127.0.0.1:1'

def test_public_settings_do_not_expose_url_credentials(tmp_path):
 import ai_settings
 enc,key=tmp_path/'ai.enc',tmp_path/'ai.key'
 raw=ai_settings.load_raw_settings(enc,key);raw['openai']['base_url']='https://fake:secret@fixture.invalid/v1?key=secret'
 ai_settings.save_raw_settings(raw,enc,key)
 public=ai_settings.get_public_settings(enc,key)
 assert public['openai']['base_url']=='https://fixture.invalid/v1'
 with pytest.raises(ValueError):ai_settings.update_settings({'openai':{'base_url':raw['openai']['base_url']}},enc,key)

def test_continuation_keeps_original_target(task_store):
 task_store.create('root','seedance-2.5','cat','16:9',30)
 task_store.create('child','seedance-2.5','cat tiếp','16:9',15)
 task_store.update('root',continuation_task_id='child')
 task_store.update('child',parent_task_id='root')
 assert server._target_duration(task_store.get('child'))==30
 assert task_store.continuation_parent('child')['id']=='root'
