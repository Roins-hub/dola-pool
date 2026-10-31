"""Real desktop/mobile forms, with HTTP boundaries isolated from production."""
import json
from pathlib import Path
import unittest
from urllib.parse import urlsplit
from patchright.async_api import async_playwright

ROOT=Path(__file__).resolve().parents[1]


class ReferenceMediaUITests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_and_member_preview_upload_and_submit_with_no_overflow(self):
        requests,errors=[],[]
        async def route_request(route):
            req=route.request
            path=urlsplit(req.url).path
            requests.append((path,req.method,req.post_data_json if req.headers.get('content-type')=='application/json' and req.post_data else None,req.headers))
            if path.endswith('/reference-media/config'):
                data={'max_count':10,'max_bytes':50*1024*1024}
            elif path.endswith('/reference-images/config'):
                data={'max_count':30,'max_bytes':15*1024*1024}
            elif path.endswith('/reference-media'):
                data={'id':'a'*32+'.wav','kind':'audio','name':'旁白.wav','size':100}
            elif path in ('/api/admin/generate','/v1/videos/generations'):
                data={'id':'video_'+'b'*32,'status':'queued','model':req.post_data_json['model']}
            elif path=='/api/me':
                data={'user':{'email':'fixture@example.test','role':'member'},'keys':[
                    {'key':'fixture-api-key','enabled':True,'allowed_durations':[5,10,15,30]}],
                    'usage':{},'supported_durations':[5,10,15,30]}
            elif path.startswith('/api/'):
                data={'accounts':[],'tasks':[],'categories':[]}
            else:
                name={'/admin':'index.html','/member':'member.html'}.get(path,path.lstrip('/'))
                file=ROOT/'web'/name
                if not file.is_file():
                    await route.fulfill(status=404,body='');return
                mime={'.html':'text/html','.js':'application/javascript','.css':'text/css'}.get(file.suffix,'application/octet-stream')
                await route.fulfill(body=file.read_bytes(),content_type=mime);return
            await route.fulfill(json=data)
        async with async_playwright() as runtime:
            browser=await runtime.chromium.launch(headless=True)
            context=await browser.new_context(viewport={'width':1440,'height':1000})
            await context.add_init_script("localStorage.setItem('dola_token','fixture-session')")
            await context.route('**/*',route_request)
            page=await context.new_page()
            page.on('pageerror',lambda error:errors.append(str(error)))
            out=ROOT/'.impeccable/review/reference-media'
            out.mkdir(parents=True,exist_ok=True)
            try:
                for role,prefix,prompt,button,status,endpoint in [
                    ('admin','genMedia','genPrompt','genBtn','genStatus','/api/admin/generate'),
                    ('member','memberMedia','memberPrompt','memberSubmit','memberStatus','/v1/videos/generations')]:
                    await page.goto('https://fixture.test/'+role+('#gallery' if role=='admin' else ''))
                    await page.locator('#'+button).wait_for(state='visible')
                    duration='genDuration' if role=='admin' else 'memberDuration'
                    await page.locator('#'+duration+' option[value="10"]').wait_for(state='attached')
                    await page.select_option('#'+duration,'10')
                    hint='#'+('genFast15Hint' if role=='admin' else 'memberFast15Hint')
                    self.assertTrue(await page.locator(hint).is_hidden())
                    await page.locator('#'+prefix+'Files').set_input_files(
                        {'name':'旁白.wav','mimeType':'audio/wav','buffer':b'fixture'})
                    await page.locator('#'+prefix+'Preview audio').wait_for()
                    self.assertFalse(any(p.endswith('/reference-media') and method=='POST' for p,method,_,_ in requests))
                    for width in (1440,375):
                        await page.set_viewport_size({'width':width,'height':1000})
                        self.assertTrue(await page.evaluate('document.documentElement.scrollWidth<=innerWidth'))
                        await page.locator('#'+prefix+'Files').scroll_into_view_if_needed()
                        await page.screenshot(path=str(out/f'{role}-{width}.png'))
                    await page.fill('#'+prompt,'参考音频节奏')
                    await page.locator('#'+button).click()
                    await page.locator('#'+status).filter(has_text='已提交').wait_for(timeout=5000)
                    posted=next(body for path,method,body,_ in reversed(requests) if path==endpoint)
                    self.assertEqual(posted['reference_media_uploads'],['a'*32+'.wav'])
                    self.assertEqual((posted['model'],posted['duration']),('seedance-2.0',10))
                    self.assertNotIn('duration_profile',posted)
                    uploads=[r for r in requests if r[0].endswith('/reference-media') and r[1]=='POST']
                    self.assertEqual(uploads[-1][3]['x-file-name'],'%E6%97%81%E7%99%BD.wav')
                    self.assertEqual(await page.locator('#'+prefix+'Preview audio').count(),0)
                    requests.clear()
                self.assertEqual(errors,[])
            finally:
                await browser.close()
