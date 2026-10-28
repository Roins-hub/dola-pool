import ast
import asyncio
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from login_verification import LoginEvidence

ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.mark.parametrize('flag,status,confirmed,logged_out', [
    ('1',200,True,False), ('0',200,False,True), ('',200,False,False),
    ('1',500,False,False), ('0',401,False,True),
])
def test_evidence_requires_successful_login_header(flag,status,confirmed,logged_out):
    async def headers(): return {'x-tt-agw-login':flag}
    async def scenario():
        evidence=LoginEvidence(None)
        await evidence.read(SimpleNamespace(all_headers=headers,status=status))
        assert (evidence.confirmed,evidence.logged_out)==(confirmed,logged_out)
    run(scenario())


@pytest.mark.parametrize('url',[
    'https://evil.invalid/alice/profile/self',
    'https://www.dola.com/chat/completion',
    'https://www.dola.com/static/file.js',
    'http://www.dola.com/alice/profile/self',
])
def test_unrelated_responses_cannot_confirm_login(url):
    evidence=LoginEvidence(None)
    evidence.on_response(SimpleNamespace(url=url))
    assert not evidence.tasks and not evidence.confirmed


@pytest.mark.parametrize('mode,expected,seconds', [
    ('confirmed', True, 2), ('no-header', True, 26),
    ('late-logout', False, 21), ('header-logout', False, 2),
    ('no-ready-page', None, 35),
])
def test_fast_path_preserves_fallback_and_cleanup(monkeypatch,mode,expected,seconds):
    elapsed=[0.0]
    stages=[]
    class Page:
        url='https://www.dola.com/chat'
        first=property(lambda self:self)
        def on(self,event,callback): self.callback=callback
        def remove_listener(self,event,callback): self.removed=True
        async def goto(self,*a,**kw):
            if mode in ('confirmed','header-logout'):
                async def headers():return {'x-tt-agw-login':'0' if mode=='header-logout' else '1'}
                self.callback(SimpleNamespace(url='https://www.dola.com/alice/profile/self',status=200,all_headers=headers))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
        def get_by_text(self,*a,**kw): return self
        async def count(self): return 0
        async def evaluate(self,*a):
            return {'has_chat':mode!='no-ready-page','hits':['login'] if mode=='late-logout' and elapsed[0]>=20 else [],'modal':False}
        async def wait_for_timeout(self,ms):
            elapsed[0]+=ms/1000
            await asyncio.sleep(0)
    page=Page()
    class Context:
        pages=[page]
        closed=False
        async def cookies(self,*a): return [{'name':'sessionid','value':'synthetic'}]
        async def close(self): self.closed=True
    context=Context()
    class Playwright:
        async def __aenter__(self): return self
        async def __aexit__(self,*a): pass
    async def launch(*a):return context
    monkeypatch.setitem(sys.modules,'patchright.async_api',SimpleNamespace(async_playwright=Playwright))
    tree=ast.parse((ROOT/'browser.py').read_text(encoding='utf8'))
    fn=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='check_login_state')
    scope={'time':SimpleNamespace(monotonic=lambda:elapsed[0]),'launch_account_context':launch,
           'cookie_value':lambda cookies,name:next((c['value'] for c in cookies if c['name']==name),'')}
    exec(compile(ast.Module(body=[fn],type_ignores=[]),'verification-test','exec'),scope)
    if expected is None:
        with pytest.raises(RuntimeError,match='验证未确认'):
            run(scope['check_login_state']('acc1',stages.append))
    else:
        assert run(scope['check_login_state']('acc1',stages.append)) is expected
    assert elapsed[0]<=seconds and context.closed and page.removed
    if mode=='no-header':assert elapsed[0]>=24
    if mode=='confirmed':assert '已取得上游登录确认' in stages


def test_ui_updates_immediately_and_ignores_duplicate_clicks():
    node=shutil.which('node')
    if not node:pytest.skip('node required')
    html=(ROOT/'web/index.html').read_text(encoding='utf8')
    code=html[html.index('const pendingVerifications=new Map();'):html.index('let testGenPollTimer=null;')]
    # Slice using the next declaration, retaining the actual source unchanged.
    badge=html[html.index('function statusBadge(a)'):html.index('function table(heads,rows)')]
    script='''
const assert=require('assert');let calls=0,renders=0,resolve;
const allAccounts=[{name:'acc1',busy:false}],curTab='accounts';
const esc=x=>x,alert=()=>{},dialogText=(s,...v)=>s.reduce((a,p,i)=>a+p+(v[i]||''),'');
const renderAccounts=()=>{renders++;},refresh=()=>{},loadAccounts=async()=>{};
const api=()=>{calls++;return new Promise(r=>resolve=r);};
''' + code + badge + '''
(async()=>{
 const button={disabled:false,textContent:'验证'};
 const job=verify('acc1',button);
 assert(button.disabled&&button.textContent==='验证中…');
 assert(pendingVerifications.has('acc1')&&renders>0);
 assert(statusBadge({name:'acc1',busy:false}).includes('等待服务器开始验证'));
 await verify('acc1',{});assert.equal(calls,1);
 resolve({ok:true});await job;
 assert.equal(pendingVerifications.size,0);assert.equal(verificationRefreshTimer,null);
})().catch(e=>{console.error(e);process.exit(1)});
'''
    subprocess.run([node],input=script,text=True,encoding='utf8',capture_output=True,check=True,timeout=10)
