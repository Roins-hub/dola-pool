"""Process/browser fixture. Mock protocol/AI boundaries, keep scheduler/stores/workers real.
Run only with DOLA_STUDIO_TEST_ROOT set to a private test directory.
"""
import os
import sys
from pathlib import Path
root = Path(os.environ['DOLA_STUDIO_TEST_ROOT']).resolve()
repo = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(repo))
os.chdir(root)
os.environ.update({'DOLA_SKIP_LOCAL_ENV':'1','DOLA_DB_PATH':str(root/'tasks.db'),
 'DOLA_POOL_DB_PATH':str(root/'pool.db'),'DOLA_DOWNLOAD_DIR':str(root/'downloads'),
 'DOLA_AI_ENC_FILE':str(root/'ai.enc'),'DOLA_AI_KEY_FILE':str(root/'ai.key'),
 'DOLA_CRED_FILE':str(root/'creds.enc'),'DOLA_CRED_KEY_FILE':str(root/'cred.key'),
 'DOLA_PROXY':'','DOLA_ADMIN_KEY':'','DOLA_API_KEYS':'', 'DOLA_HELLO_PROBE_SUBMIT_GAP_SECONDS':'0','DOLA_DAILY_LIMIT':'100',
 'DOLA_OBSERVATION_SECONDS':os.getenv('DOLA_TEST_WINDOW','1800'),
 'DOLA_PUBLIC_BASE':'http://127.0.0.1:'+os.environ['DOLA_TEST_PORT']})
(root/'web').symlink_to(repo/'web',target_is_directory=True) if not (root/'web').exists() else None
(root/'downloads').mkdir(exist_ok=True)
import json
import shutil
from types import SimpleNamespace
import httpx
import config
import ai_settings
import ai_provider
import pure_api_gen
import server
import uvicorn

# Every protocol network operation is substituted; signing does not run.
def event(kind, **data):
 with (root/'events.jsonl').open('a') as f:f.write(json.dumps({'kind':kind,**data})+'\n')
def submit(*args, **kwargs):
 cid='conversation-'+str(sum(1 for line in (root/'events.jsonl').read_text().splitlines() if json.loads(line)['kind']=='submit')+1) if (root/'events.jsonl').exists() else 'conversation-1'
 event('submit',conversation_id=cid,prompt=kwargs['prompt'],images=len(kwargs.get('images') or []))
 return SimpleNamespace(error='',error_code='',conversation_id=cid)
def inspect(session,ctx,cid):
 event('poll',conversation_id=cid)
 status='pending' if (root/'hold').exists() else 'succeeded'
 return SimpleNamespace(status=status,vids=[],urls=['https://fixture.invalid/video.mp4'] if status=='succeeded' else [],
  failure_reasons=[],texts=['Đã nhận yêu cầu. Video đang được dựng.' if status=='pending' else 'Video đã sẵn sàng.','今日剩余 100 个视频生成额度'],creation_statuses=['creation status=1' if status=='pending' else 'creation status=2'],wait_minutes='3分钟' if status=='pending' else '',source_data={})
def download(url,path,**kwargs):
 event('download');shutil.copyfile(root/'fixture.mp4',path);return path
fake=SimpleNamespace(normalize_image_paths=lambda paths:[Path(p) for p in paths],upload_image=lambda *a:{'uri':'fixture-image'},enrich_multi_image_prompt=lambda prompt,count:prompt,BASE='https://fixture.invalid',setup_session=lambda **k:(None,None,{}),
 warmup_login_session=lambda *a:None,dola_api_post_json=lambda *a,**k: {},
 submit_chat_completion=submit,inspect_video_once=inspect,
 poll_has_duration_split=lambda *a,**k:False,find_video_info_objects=lambda x:[],
 choose_video_download_url=lambda url,**k:{'download_url':url},proxy_dict=lambda x:{},download_video=download)
pure_api_gen._dola=lambda:fake
pure_api_gen.install=lambda:None
config.PURE_API_POLL_INTERVAL=0.1
# Pool health probe is also an external boundary, with realistic login result.
import browser_pool
browser_pool.hello_probe=lambda *a,**k:{'ok':True,'status':'ok'}
async def ai_transport(request):
 data=json.loads(request.content)
 system=data['messages'][0]['content'];user=data['messages'][-1]['content']
 event('ai',rewrite='đạo diễn' in system.lower())
 if 'đạo diễn' in system.lower():
  result='Một chú mèo trên cửa sổ, máy quay tiến nhẹ, giữ ánh sáng ban đầu.'
 elif 'BLOCK_ME' in user:result=json.dumps({'status':'block','reason':'Nội dung cần chỉnh sửa','suggestions':'Thử mô tả khác'})
 elif 'REVIEW_ME' in user:result=json.dumps({'status':'review','reason':'Cần xác nhận cảnh hành động','suggestions':''})
 elif 'ANALYZE_ME' in user:result=json.dumps({'status':'allow','reason':'','suggestions':'',
  'quality_summary':'Giữ cảnh mèo; làm rõ góc máy và ánh sáng.', 'issues':[
   {'id':'camera','kind':'quality','severity':'suggestion','quote':'quay đẹp','occurrence':1,
    'replacement':'máy quay tiến chậm','reason_vi':'Góc máy chưa rõ.','confidence':'high','source_ids':['visual-action']},
   {'id':'light','kind':'quality','severity':'suggestion','quote':'ánh sáng đẹp','occurrence':1,
    'replacement':'ánh sáng sớm dịu','reason_vi':'Ánh sáng chưa cụ thể.','confidence':'medium','source_ids':['visual-action']}]})
 else:result=json.dumps({'status':'allow','reason':'','suggestions':''})
 return httpx.Response(200,json={'choices':[{'message':{'content':result}}]})
RealClient=httpx.AsyncClient
ai_provider.httpx.AsyncClient=lambda **kwargs:RealClient(transport=httpx.MockTransport(ai_transport),**kwargs)
ai_settings.update_settings({'active_provider':'openai','openai':{'base_url':'https://fixture.invalid/v1','api_key':'test-only-key','model':'fixture'}})
for name in ['mock1','mock2']:
 path=root/'accounts'/name;path.mkdir(parents=True,exist_ok=True)
 (path/'cookie_state.json').write_text(json.dumps({'cookies':[{'name':'sessionid','value':'fake-'+name,'domain':'.dola.com','path':'/'}]}))
 server.pool.ensure_account(name);server.pool.set_source(name,'cookie');server.pool.set_login_status(name,True)
 server.pool._set_credit_balance(name,100,'fixture')
uvicorn.run(server.app,host='127.0.0.1',port=int(os.environ['DOLA_TEST_PORT']),log_level='warning')
