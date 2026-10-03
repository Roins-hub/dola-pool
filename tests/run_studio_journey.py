"""Real-process restart + desktop/mobile journey with protocol-only fixtures.
Usage: .venv/bin/python tests/run_studio_journey.py [evidence-directory]
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
import httpx
from patchright.async_api import async_playwright

REPO=Path(__file__).resolve().parents[1]
OUT=Path(sys.argv[1] if len(sys.argv)>1 else tempfile.mkdtemp(prefix='dola-studio-evidence-')).resolve()
OUT.mkdir(parents=True,exist_ok=True)
ROOT=Path(tempfile.mkdtemp(prefix='dola-studio-runtime-'))
from PIL import Image
Image.new('RGB',(160,90),'blue').save(ROOT/'reference.png')
with socket.socket() as sock:sock.bind(('127.0.0.1',0));PORT=sock.getsockname()[1]
BASE=f'http://127.0.0.1:{PORT}'
subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=darkblue:s=256x144:r=12','-t','15','-c:v','libx264','-pix_fmt','yuv420p',str(ROOT/'fixture.mp4')],check=True)
process=None
log=(OUT/'mock-server.log').open('w')
def start(window=1800):
 global process
 env={**os.environ,'DOLA_STUDIO_TEST_ROOT':str(ROOT),'DOLA_TEST_PORT':str(PORT),'DOLA_TEST_WINDOW':str(window)}
 process=subprocess.Popen([sys.executable,str(REPO/'tests/studio_mock_server.py')],env=env,stdout=log,stderr=log)
 for _ in range(100):
  if process.poll() is not None:raise RuntimeError('Mock server exited; see mock-server.log')
  try:
   if httpx.get(BASE+'/',timeout=.3).status_code==200:return process.pid
  except httpx.TransportError:pass
  time.sleep(.1)
 raise RuntimeError('Server did not start')
def events(kind):
 p=ROOT/'events.jsonl'
 return [json.loads(s) for s in p.read_text().splitlines() if json.loads(s)['kind']==kind] if p.exists() else []
def login():
 r=httpx.post(BASE+'/api/admin/login',json={'username':'admin','password':'admin123'});r.raise_for_status();return {'x-admin-key':r.json()['token']}
def create(prompt='cat',duration=30,request_id=''):
 headers=login();r=httpx.post(BASE+'/api/admin/prompts/preflight',headers=headers,json={'prompt':prompt});r.raise_for_status()
 r=httpx.post(BASE+'/api/admin/videos/generations',headers=headers,data={'prompt':prompt,'duration':str(duration),'model':'seedance-2.5','ratio':'16:9','request_id':request_id,'preflight_token':r.json()['token']});r.raise_for_status();return r.json()['id']
def row(id):
 r=httpx.get(BASE+'/api/admin/tasks/'+id,headers=login());r.raise_for_status();return r.json()
def wait_row(id,status):
 for _ in range(100):
  data=row(id)
  if data['status']==status:return data
  time.sleep(.1)
 raise AssertionError(data)
async def browser_journey():
 async with async_playwright() as p:
  browser=await p.chromium.launch(headless=True)
  page=await browser.new_page(viewport={'width':1440,'height':1000})
  errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  page.on('console',lambda m:errors.append(m.text) if m.type=='error' and '401' not in m.text else None)
  await page.goto(BASE);await page.locator('#loginUser').fill('admin');await page.locator('#loginPassword').fill('admin123');await page.locator('#loginPassword').press('Enter')
  await page.wait_for_selector('#tab-generate',state='visible')
  assert await page.locator('.side-nav button').count()==3
  # Rich AI suggestions are selectable; applying one never edits another phrase.
  await page.locator('#gen_prompt').fill('ANALYZE_ME 🐈 quay đẹp; ánh sáng đẹp')
  await page.locator('#btn_preflight').click()
  await page.wait_for_selector('#gen_analysis .analysis-issue')
  assert await page.locator('#gen_analysis .analysis-issue').count()==2
  await page.screenshot(path=str(OUT/'analysis-desktop.png'),full_page=True)
  await page.locator('#gen_analysis input[data-issue-id="camera"]').check()
  await page.get_by_role('button',name='Tạo bản đề xuất từ lựa chọn',exact=True).click()
  assert await page.locator('#gen_rewrite_before').input_value()=='ANALYZE_ME 🐈 quay đẹp; ánh sáng đẹp'
  assert await page.locator('#gen_rewrite_draft').input_value()=='ANALYZE_ME 🐈 máy quay tiến chậm; ánh sáng đẹp'
  assert await page.locator('#gen_prompt').input_value()=='ANALYZE_ME 🐈 quay đẹp; ánh sáng đẹp'
  await page.get_by_role('button',name='Áp dụng prompt này',exact=True).click()
  assert await page.locator('#gen_analysis').is_hidden()
  await page.get_by_role('button',name='Hoàn tác',exact=True).click()
  assert await page.locator('#gen_prompt').input_value()=='ANALYZE_ME 🐈 quay đẹp; ánh sáng đẹp'
  await page.evaluate("localStorage.removeItem('dola-studio-draft-v1')")
  await page.reload();await page.wait_for_selector('#tab-generate',state='visible')
  await page.locator('#gen_prompt').fill('Một chú mèo nhìn ra cửa sổ')
  await page.locator('#gen_rewrite_instructions').fill('Giữ nhân vật, máy quay tiến nhẹ')
  await page.locator('#btn_rewrite').click();await page.wait_for_selector('#gen_rewrite_box',state='visible')
  assert await page.locator('#gen_prompt').input_value()=='Một chú mèo nhìn ra cửa sổ'
  first=await page.locator('#gen_rewrite_draft').input_value()
  await page.locator('#gen_rewrite_instructions').fill('Giữ cảnh và giảm lời văn')
  await page.get_by_role('button',name='Sửa tiếp bản đề xuất',exact=True).click()
  await page.wait_for_function("document.querySelector('#gen_history').options.length===3")
  assert await page.locator('#gen_rewrite_before').input_value()==first
  await page.get_by_role('button',name='Áp dụng prompt này',exact=True).click()
  assert 'máy quay tiến' in await page.locator('#gen_prompt').input_value()
  await page.get_by_role('button',name='Hoàn tác',exact=True).click()
  assert await page.locator('#gen_prompt').input_value()=='Một chú mèo nhìn ra cửa sổ'
  await page.reload();await page.wait_for_selector('#tab-generate',state='visible')
  assert await page.locator('#gen_prompt').input_value()=='Một chú mèo nhìn ra cửa sổ'
  await page.locator('#gen_file_input').set_input_files(str(ROOT/'reference.png'))
  await page.wait_for_function("document.body.dataset.imagesSaved==='saved'")
  await page.reload();await page.wait_for_selector('#tab-generate',state='visible')
  await page.wait_for_selector('.image-preview-item img')
  # Blocked preflight never submits; draft survives.
  before=len(events('submit'));await page.locator('#gen_prompt').fill('BLOCK_ME')
  await page.locator('#btn_submit_gen').click();await page.wait_for_selector('.alert-block',state='visible')
  assert len(events('submit'))==before
  assert await page.locator('#gen_prompt').input_value()=='BLOCK_ME'
  # Review requires user confirmation.
  await page.locator('#gen_prompt').fill('REVIEW_ME hành động giả tưởng')
  await page.locator('#btn_submit_gen').click();await page.wait_for_selector('#gen_confirm_review')
  assert len(events('submit'))==before
  (ROOT/'hold').touch()
  await page.locator('#gen_confirm_review').check();await page.locator('#btn_submit_gen').dblclick()
  await page.wait_for_selector('#gen_job_content progress:not([value])')
  await page.wait_for_function("document.querySelector('#gen_job_content').textContent.includes('Dola đã nhận yêu cầu và đang xử lý video.')")
  assert '3 phút' in await page.locator('#gen_job_content').inner_text()
  assert not await page.locator('#gen_job_content .upstream-main').evaluate("el=>/[\u3400-\u9fff]/.test(el.textContent)")
  assert not await page.locator('#gen_job_content progress').get_attribute('value')
  await page.screenshot(path=str(OUT/'progress-desktop.png'),full_page=True)
  await page.set_viewport_size({'width':390,'height':844});await page.screenshot(path=str(OUT/'progress-mobile.png'),full_page=True)
  assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
  await page.set_viewport_size({'width':1440,'height':1000})
  # Disabling inventory does not interrupt the accepted conversation.
  auth=login()
  httpx.patch(BASE+'/api/admin/account-pools/default',headers=auth,json={'enabled':False}).raise_for_status()
  (ROOT/'hold').unlink()
  await page.wait_for_function("document.querySelector('#gen_job_content video')!==null")
  waiting_id=create('Chờ kho được bật',duration=15,request_id='inventory-wait')
  await page.wait_for_timeout(1200)
  assert row(waiting_id)['status']=='queued'
  assert row(waiting_id)['error'].startswith('Chờ tài khoản')
  assert len(events('submit'))==before+1
  httpx.patch(BASE+'/api/admin/account-pools/default',headers=auth,json={'enabled':True}).raise_for_status()
  await asyncio.to_thread(wait_row,waiting_id,'completed')

  assert len(events('submit'))==before+2
  assert events('submit')[-2]['images']==1
  assert await page.get_by_role('button',name='Tạo tiếp và ghép',exact=True).count()==1
  await page.screenshot(path=str(OUT/'studio-desktop.png'),full_page=True)
  # A stale rewrite must not overwrite a newer idea.
  await page.route('**/api/admin/prompts/rewrite',lambda route: asyncio.create_task(delayed_rewrite(route)))
  await page.locator('#gen_prompt').fill('Ý tưởng trước');await page.locator('#btn_rewrite').click()
  await page.locator('#gen_prompt').fill('Ý tưởng mới');await page.wait_for_timeout(600)
  assert await page.locator('#gen_rewrite_box').is_hidden()
  assert await page.locator('#gen_prompt').input_value()=='Ý tưởng mới'
  await page.unroute('**/api/admin/prompts/rewrite')
  # Race: old preflight returns after prompt edit, must be discarded.
  await page.route('**/api/admin/prompts/preflight',lambda route: asyncio.create_task(delayed_preflight(route)))
  await page.locator('#gen_prompt').fill('prompt trước');await page.locator('#btn_preflight').click()
  await page.locator('#gen_prompt').fill('prompt mới');await page.wait_for_timeout(600)
  assert await page.locator('#gen_preflight_alert').inner_text()==''
  await page.unroute('**/api/admin/prompts/preflight')
  # Editing parameters during analysis invalidates a response for the old context.
  await page.route('**/api/admin/prompts/preflight',lambda route: asyncio.create_task(delayed_preflight(route)))
  await page.locator('#gen_prompt').fill('prompt trước khi đổi thời lượng');await page.locator('#btn_preflight').click()
  await page.locator('#gen_duration').select_option('10');await page.wait_for_timeout(600)
  assert await page.locator('#gen_preflight_alert').inner_text()==''
  assert await page.locator('#gen_analysis').is_hidden()
  await page.unroute('**/api/admin/prompts/preflight')
  # Reference roles are selectable with an accessible name; changed roles invalidate checking.
  await page.get_by_label('Vai trò ảnh reference.png').select_option('character')
  assert await page.locator('#gen_preflight_alert').inner_text()==''
  # Old job response cannot stop a newer job polling timer.
  await page.evaluate('''()=>{const script=document.createElement('script');script.textContent=`(async ()=>{const real=api;let release;api=()=>new Promise(r=>release=r);
    currentActiveTaskId='old';const pending=pollStudioJob('old');currentActiveTaskId='new';studioPollTimer=setInterval(()=>{},10000);
    const timer=studioPollTimer;release({id:'old',status:'completed'});await pending;const ok=studioPollTimer===timer;
    clearInterval(studioPollTimer);studioPollTimer=null;api=real;document.body.dataset.pollRace=String(ok);})();`;document.head.appendChild(script);}''')
  await page.wait_for_function("document.body.dataset.pollRace==='true'")
  # A confirmed HTTP rejection is distinct from an unknown network send.
  await page.route('**/api/admin/videos/generations',lambda route: asyncio.create_task(route.fulfill(status=422,content_type='application/json',body=json.dumps({'detail':'Token kiểm tra đã hết hạn'}))))
  await page.locator('#gen_prompt').fill('Prompt giữ nguyên khi bị từ chối')
  await page.locator('#btn_submit_gen').click()
  await page.wait_for_function("document.querySelector('#gen_action_msg').textContent.startsWith('Chưa gửi video:')")
  assert await page.locator('#gen_prompt').input_value()=='Prompt giữ nguyên khi bị từ chối'
  assert errors.count('Failed to load resource: the server responded with a status of 422 (Unprocessable Entity)')==1
  errors.remove('Failed to load resource: the server responded with a status of 422 (Unprocessable Entity)')
  await page.unroute('**/api/admin/videos/generations')
  # TXT import, blocked batch, review gate and four separate persisted jobs.
  await page.locator('#gen_mode').select_option('list')
  await page.locator('#gen_batch_prompts').fill('cat\nBLOCK_ME')
  before=len(events('submit'));await page.locator('#btn_submit_gen').click()
  await page.wait_for_function("document.querySelector('#gen_batch_check').textContent.includes('Mẻ chưa được gửi')")
  assert len(events('submit'))==before
  (ROOT/'prompts.txt').write_text('\ufeffMột chú mèo\r\n\r\nREVIEW_ME Một con thuyền\r\n',encoding='utf-8')
  await page.locator('#gen_txt_file').set_input_files(str(ROOT/'prompts.txt'))
  await page.wait_for_function("document.querySelector('#gen_batch_prompts').value.includes('Một con thuyền')")
  await page.locator('#gen_copies').fill('2')
  assert '4 video' in await page.locator('#gen_batch_summary').inner_text()
  await page.locator('#btn_submit_gen').click();await page.wait_for_selector('#gen_batch_review')
  assert len(events('submit'))==before
  await page.locator('#gen_batch_review').check()
  page.on('dialog',lambda dialog:asyncio.create_task(dialog.accept()))
  await page.locator('#btn_submit_gen').dblclick()
  await page.wait_for_selector('#tab-videos',state='visible')
  await page.wait_for_function("document.querySelectorAll('#myVideoJobs .video-job').length===4")
  await page.wait_for_function("document.querySelector('#myBatchSummary').textContent.includes('4 hoàn tất')")
  assert len(events('submit'))==before+4
  assert all(e['images']==1 for e in events('submit')[-4:])
  await page.screenshot(path=str(OUT/'batch-desktop.png'),full_page=True)
  await page.reload();await page.wait_for_selector('#tab-generate',state='visible')
  assert await page.locator('#gen_copies').input_value()=='2'
  assert 'Một con thuyền' in await page.locator('#gen_batch_prompts').input_value()
  await page.get_by_role('button',name='▤ Video của tôi',exact=True).click();await page.wait_for_selector('.video-job')
  assert await page.locator('#myVideoJobs .video-job').count()==4
  await page.locator('#myBatchClear').click()
  await page.get_by_role('button',name='▤ Video của tôi',exact=True).click();await page.wait_for_selector('.video-job')
  await page.locator('#tab-videos').get_by_role('button',name='Bảng',exact=True).click()
  await page.wait_for_selector('#myVideoTable table')
  await page.locator('#myVideoTable button').first.click();await page.wait_for_selector('#mySelectedPreview video')
  assert await page.locator('#mySelectedPreview').evaluate('el=>el.scrollWidth<=el.clientWidth')
  await page.locator('#tab-videos').get_by_role('button',name='Lưới',exact=True).click()
  await page.screenshot(path=str(OUT/'videos-desktop.png'),full_page=True)
  await page.get_by_role('button',name='⚙ Cài đặt',exact=True).click();await page.wait_for_selector('#tab-accounts',state='visible');await page.wait_for_selector('#accountsTable table')
  assert not await page.get_by_text('Quản lý API Key',exact=True).count()
  await page.screenshot(path=str(OUT/'settings-desktop.png'),full_page=True)
  await page.get_by_role('button',name='Nâng cao: proxy',exact=True).click()
  await page.locator('#tab-proxies details summary').click()
  await page.get_by_role('button',name='Lấy thông tin',exact=True).click()
  await page.wait_for_function("document.querySelector('#diagnosticEvents').textContent.includes('Relay cho kết nối Dola')")
  assert await page.locator('#diagnosticEvents article').count()>0
  assert 'fake-mock' not in await page.locator('#diagnosticEvents').inner_text()

  await page.get_by_role('button',name='▶ Tạo video',exact=True).click()
  await page.set_viewport_size({'width':390,'height':844});await page.screenshot(path=str(OUT/'studio-mobile.png'),full_page=True)
  assert await page.evaluate('document.documentElement.scrollWidth<=window.innerWidth'), 'Mobile horizontal overflow'
  assert await page.locator('main').evaluate('(el)=>el.getBoundingClientRect().width>350'), 'Mobile content too narrow'
  assert not errors, errors
  await browser.close()
async def delayed_rewrite(route):
 await asyncio.sleep(.4);await route.fulfill(status=200,content_type='application/json',body=json.dumps({'rewritten_prompt':'Bản cũ không được áp dụng'}))
async def delayed_preflight(route):
 await asyncio.sleep(.4);await route.fulfill(status=200,content_type='application/json',body=json.dumps({'status':'allow','token':'stale'}))
try:
 (ROOT/'hold').touch();pid1=start();id=create()
 for _ in range(100):
  if row(id).get('phase')=='generating':break
  time.sleep(.1)
 assert len(events('submit'))==1
 process.kill();process.wait(timeout=10)
 (ROOT/'hold').unlink();pid2=start();assert pid1!=pid2
 result=wait_row(id,'completed');assert len(events('submit'))==1
 assert 14.9<=result['video_duration']<=15.1
 headers=login()
 r=httpx.post(BASE+'/api/admin/tasks/'+id+'/continue',headers=headers,json={'confirm_cost':False});assert r.status_code==400
 assert len(events('submit'))==1
 r=httpx.post(BASE+'/api/admin/tasks/'+id+'/continue',headers=headers,json={'confirm_cost':True});r.raise_for_status();child=r.json()['id']
 r=httpx.post(BASE+'/api/admin/tasks/'+id+'/continue',headers=headers,json={'confirm_cost':True});r.raise_for_status();assert r.json()['id']==child
 merged=wait_row(child,'completed');assert 29.9<=merged['video_duration']<=30.2
 assert len(events('submit'))==2
 # Expired observation -> same-conversation manual check, no new submission.
 process.kill();process.wait(timeout=10);(ROOT/'hold').touch();start(window=1)
 unknown=create('slow cat',duration=10);wait_row(unknown,'unknown');count=len(events('submit'))
 (ROOT/'hold').unlink();r=httpx.post(BASE+'/api/admin/tasks/'+unknown+'/check-result',headers=login());r.raise_for_status()
 wait_row(unknown,'completed');assert len(events('submit'))==count
 before=len(events('submit'));retry=create('idempotent cat',10,'same-request')
 assert create('idempotent cat',10,'same-request')==retry
 wait_row(retry,'completed');assert len(events('submit'))==before+1
 process.kill();process.wait(timeout=10);start(window=1800)
 # A held batch spans an actual process restart; receipt survives new auth/token state.
 (ROOT/'hold').touch();headers=login();entries=[]
 for prompt in ['batch restart cat','batch restart boat']:
  pf=httpx.post(BASE+'/api/admin/prompts/preflight',headers=headers,json={'prompt':prompt});pf.raise_for_status()
  entries.append({'prompt':prompt,'preflight_token':pf.json()['token']})
 spec={'request_id':'restart-batch','prompts':entries,'copies':2,'model':'seedance-2.5','duration':10,'ratio':'16:9'}
 response=httpx.post(BASE+'/api/admin/videos/batches',headers=headers,data={'batch':json.dumps(spec)});response.raise_for_status();batch=response.json();assert len(batch['tasks'])==4
 deadline=time.time()+10
 while time.time()<deadline:
  view=httpx.get(BASE+'/api/admin/videos/batches/'+batch['batch_id'],headers=headers).json()
  accepted=[t for t in view['tasks'] if t.get('conversation_id')]
  if len(accepted)==2:break
  time.sleep(.1)
 assert len(accepted)==2
 count_before_restart=len(events('submit'));process.kill();process.wait(timeout=10)
 (ROOT/'hold').unlink();start(window=1800)
 for task in batch['tasks']:wait_row(task['id'],'completed')
 assert len(events('submit'))==count_before_restart+2
 retry=httpx.post(BASE+'/api/admin/videos/batches',headers=login(),data={'batch':json.dumps(spec)});retry.raise_for_status()
 assert [t['id'] for t in retry.json()['tasks']]==[t['id'] for t in batch['tasks']]
 assert len(events('submit'))==count_before_restart+2
 asyncio.run(browser_journey())
 (OUT/'result.json').write_text(json.dumps({'passed':True,'process_pids':[pid1,pid2],
  'restart_submission_count':1,'continuation_submission_count':2,'manual_check_no_resubmit':True,'idempotent_request_retry':True,'reference_image_reload':True,'rewrite_iterations':2,'stale_rewrite_discarded':True,'upstream_progress_browser':True,'txt_batch_four_jobs':True,'batch_process_restart_no_duplicate':True,
  'rich_ai_selective_edit':True,'context_stale_discarded':True,'vietnamese_progress':True,'queue_table_preview':True,
  'mock_boundary':'Dola protocol operations and AI HTTP transport; real scheduler/store/download/ffmpeg/UI',
  'runtime':str(ROOT)},indent=2))
 print('PASS: process restart, short clip/confirmed merge, observation expiry/manual recovery, desktop/mobile, draft/rewrite/preflight/UI races')
finally:
 if process and process.poll() is None:process.kill();process.wait(timeout=10)
 log.close()
