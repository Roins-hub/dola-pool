"""Create is independent of AI availability; real worker and scheduler, fake upstream."""
import asyncio,json,os,socket,subprocess,sys,tempfile,time
from pathlib import Path
import httpx
from patchright.async_api import async_playwright
REPO=Path(__file__).resolve().parents[1]
ROOT=Path(tempfile.mkdtemp(prefix='dola-direct-test-'))
OUT=Path(sys.argv[1]);OUT.mkdir(parents=True,exist_ok=True)
with socket.socket() as s:s.bind(('127.0.0.1',0));PORT=s.getsockname()[1]
BASE=f'http://127.0.0.1:{PORT}'
subprocess.run(['ffmpeg','-hide_banner','-loglevel','error','-f','lavfi','-i','color=c=blue:s=256x144:r=12','-t','15','-c:v','libx264','-pix_fmt','yuv420p',str(ROOT/'fixture.mp4')],check=True)
def events(kind):
 p=ROOT/'events.jsonl'
 return [json.loads(x) for x in p.read_text().splitlines() if json.loads(x)['kind']==kind] if p.exists() else []
async def journey():
 async with async_playwright() as pw:
  browser=await pw.chromium.launch(headless=True);page=await browser.new_page(viewport={'width':1440,'height':1000})
  errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  await page.goto(BASE);await page.locator('#loginUser').fill('admin');await page.locator('#loginPassword').fill('admin123');await page.locator('#loginPassword').press('Enter')
  await page.wait_for_selector('#tab-generate',state='visible')
  async def forbid_ai(route):raise AssertionError('Create requested AI preflight')
  await page.route('**/api/admin/prompts/preflight',forbid_ai)
  await page.locator('#gen_prompt').fill('Một chú mèo nhìn ra cửa sổ')
  await page.locator('#btn_submit_gen').dblclick()
  await page.wait_for_selector('#gen_job_content video')
  assert len(events('submit'))==1 and len(events('ai'))==0
  await page.screenshot(path=str(OUT/'direct-desktop.png'),full_page=True)
  await page.unroute('**/api/admin/prompts/preflight')
  await page.locator('#gen_prompt').fill('Một con thuyền trên hồ')
  await page.locator('#btn_preflight').click()
  await page.wait_for_function("!document.querySelector('#btn_preflight').disabled")
  assert len(events('ai'))==1 and len(events('submit'))==1
  await page.route('**/api/admin/prompts/preflight',forbid_ai)
  await page.locator('#gen_mode').select_option('list');await page.locator('#gen_batch_prompts').fill('Mèo ngồi\nThuyền đi');await page.locator('#gen_copies').fill('2')
  page.on('dialog',lambda d:asyncio.create_task(d.accept()))
  await page.locator('#btn_submit_gen').click()
  await page.wait_for_function("document.querySelector('#myBatchSummary').textContent.includes('4 hoàn tất')")
  assert len(events('submit'))==5 and len(events('ai'))==1
  await page.set_viewport_size({'width':390,'height':844});await page.screenshot(path=str(OUT/'direct-mobile.png'),full_page=True)
  assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
  await page.set_viewport_size({'width':1440,'height':1000})
  await page.locator('[onclick="switchTab(\'generate\')"]').first.click()
  await page.locator('#gen_mode').select_option('single');await page.locator('#gen_copies').fill('1')
  # Reset only fixture quota after completed scenarios, allowing both model groups.
  import sqlite3
  with sqlite3.connect(ROOT/'pool.db') as db:
   db.execute('DELETE FROM usage')
  (ROOT/'hold').touch()
  await page.locator('#gen_model').select_option('seedance-2.5');await page.locator('#gen_duration').select_option('30');await page.locator('#gen_ratio').select_option('9:16')
  await page.locator('#gen_prompt').fill('First settings job')
  async with page.expect_response(lambda r:'/api/admin/videos/generations' in r.url and r.request.method=='POST') as response:
   await page.locator('#btn_submit_gen').click()
  first=(await (await response.value).json())['id']
  await page.wait_for_selector(f'[data-running-job="{first}"]')
  await page.locator('#gen_model').select_option('seedance-2.0');await page.locator('#gen_duration').select_option('5');await page.locator('#gen_ratio').select_option('16:9')
  await page.locator('#gen_prompt').fill('Second settings job')
  async with page.expect_response(lambda r:'/api/admin/videos/generations' in r.url and r.request.method=='POST') as response:
   await page.locator('#btn_submit_gen').click()
  second=(await (await response.value).json())['id']
  await page.wait_for_selector(f'[data-running-job="{first}"]');await page.wait_for_selector(f'[data-running-job="{second}"]')
  with sqlite3.connect(ROOT/'tasks.db') as db:
   assert db.execute('SELECT model,duration,ratio,status FROM tasks WHERE id=?',(first,)).fetchone()==('seedance-2.5',30,'9:16','processing')
   assert db.execute('SELECT model,duration,ratio FROM tasks WHERE id=?',(second,)).fetchone()==('seedance-2.0',5,'16:9')
  await page.screenshot(path=str(OUT/'independent-jobs-desktop.png'),full_page=True)
  await page.set_viewport_size({'width':390,'height':844});await page.screenshot(path=str(OUT/'independent-jobs-mobile.png'),full_page=True)
  assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
  (ROOT/'hold').unlink()
  for _ in range(200):
   with sqlite3.connect(ROOT/'tasks.db') as db:
    done=db.execute('SELECT COUNT(*) FROM tasks WHERE id IN (?,?) AND status=?',(first,second,'completed')).fetchone()[0]
   if done==2:break
   await asyncio.sleep(.1)
  assert done==2 and len(events('submit'))==7 and len(events('ai'))==1
  assert not errors,errors
  (OUT/'result.json').write_text(json.dumps({'passed':True,'settings_change_keeps_active_job':True,'two_jobs_independent_configuration':True,'all_active_jobs_visible':True,'create_ai_calls':0,'explicit_check_ai_calls':1,'single_double_click_submissions':1,'batch_submissions':4,'real_worker_scheduler_database':True,'live_dola_generations':0,'errors':errors},indent=2))
  await browser.close()
log=(OUT/'mock.log').open('w')
p=subprocess.Popen([sys.executable,str(REPO/'tests/studio_mock_server.py')],env={**os.environ,'DOLA_STUDIO_TEST_ROOT':str(ROOT),'DOLA_TEST_PORT':str(PORT)},stdout=log,stderr=log)
try:
 for _ in range(100):
  if p.poll() is not None:raise RuntimeError('Fixture exited')
  try:
   if httpx.get(BASE,timeout=.2).status_code==200:break
  except httpx.TransportError:pass
  time.sleep(.1)
 asyncio.run(journey());print('PASS: direct single/batch creation, no AI gate, separate check, desktop/mobile')
finally:
 p.terminate();p.wait(timeout=10);log.close()
