"""Targeted checks for the independent final review's reproductions."""
import asyncio
import hashlib
import json
import subprocess
from pathlib import Path
import pytest
import server
from test_studio_batch_progress import studio, request


def js_function(name):
    html=(Path(__file__).resolve().parents[1]/'web/index.html').read_text()
    start=html.index(('async ' if 'async function '+name+'(' in html else '')+'function '+name+'(')
    end=html.index('\n}',start)+2
    return html[start:end]


def node(script):
    r=subprocess.run(['node','-e',script],capture_output=True,text=True)
    assert r.returncode==0,r.stderr


def test_analysis_preserves_boundary_whitespace_for_occurrence_anchors():
    node("""let preflightVersion=0,promptVersion=0,currentPreflight;const raw=' mèo chạy; mèo chạy ';
let sent;const input={value:raw};function q(s){return s==='#gen_prompt'?input:{};}
const referenceHashPromise=Promise.resolve();function isBatchGeneration(){return false;}
function analysisSnapshot(){return 'snapshot';}function generationSnapshot(){return 'snapshot';}
function studioMessage(){}function renderPreflightBanner(){}function renderPromptAnalysis(){}
async function requestPreflight(p){sent=p;return {status:'allow'};}
"""+js_function('doPreflightCheck')+"\ndoPreflightCheck().then(()=>{if(sent!==raw)throw Error('Analyzed text differs from editor: '+JSON.stringify(sent));});")


@pytest.mark.parametrize('kind',['single','batch'])
def test_pending_receipt_recovery_precedes_any_ai_refresh(kind):
    func='doSubmitGenerate' if kind=='single' else 'submitVideoBatch'
    node("""let aiCalls=0,recovered=false,analysisTaskId='';function renderDraftSource(){}let pendingRequestId='accepted',pendingSnapshot='same',currentPreflight=null,batchPreflight=null,currentBatchId='',currentActiveTaskId='',studioPollTimer=null;
const fields={'#btn_submit_gen':{},'#gen_prompt':{value:'cat'},'#gen_model':{value:'seedance-2.5'},'#gen_duration':{value:'15'},'#gen_ratio':{value:'16:9'},'#gen_copies':{value:'1'}};
function q(s){return fields[s]||{};}function isBatchGeneration(){return false;}
const referenceHashPromise=Promise.resolve();function generationSnapshot(){return 'same';}
function saveStudioDraft(){}function studioMessage(){}function trackStudioJob(){recovered=true;}
function hide(){}function switchTab(){recovered=true;}
async function recoverPendingReceipt(){return {id:'saved',batch_id:'saved-batch',tasks:[{id:'saved'}]};}
async function doPreflightCheck(){aiCalls++;throw Error('AI unavailable');}
async function checkBatchPrompts(){aiCalls++;throw Error('AI unavailable');}
"""+js_function(func)+f"\n{func}().then(()=>{{if(aiCalls||!recovered)throw Error('Did not recover before AI; calls='+aiCalls);}});")


def test_non_chinese_exception_primary_message_redacts_session():
    response=asyncio.run(server.localized_request_error(None,server.HTTPException(502,'Provider failed sessionid=PRIVATE Cookie: c_user=COOKIE')))
    assert b'PRIVATE' not in response.body and b'COOKIE' not in response.body


@pytest.mark.asyncio
async def test_receipt_lookup_is_read_only_user_bound_and_keeps_deleted_batch_receipt(studio):
    store,calls=studio
    req,_=request(['cat'],request_id='receipt-review')
    result=await server.admin_create_batch(req,'test')
    await asyncio.sleep(0)
    assert await server.admin_video_receipt('receipt-review','batch','test')==result
    for row in store.batch_tasks(result['batch_id']):store.delete_task(row['id'])
    assert await server.admin_video_receipt('receipt-review','batch','test')=={'batch_id':result['batch_id'],'tasks':[]}
    server.ADMIN_SESSIONS['other']={'username':'someone-else'}
    assert await server.admin_video_receipt('receipt-review','batch','other') is None
    assert await server.admin_video_receipt('not-accepted','single','test') is None
    task_id='video_'+hashlib.sha256(b'studio:owner:single-review').hexdigest()[:32]
    store.create(task_id,'seedance-2.5','cat','16:9',15)
    assert (await server.admin_video_receipt('single-review','single','test'))['id']==task_id
    assert len(calls)==2

@pytest.mark.asyncio
async def test_exact_whitespace_prompt_passes_same_context_token_at_submission(studio):
    from types import SimpleNamespace
    from starlette.datastructures import FormData
    from ai_preflight import record_preflight_result
    raw=' mèo chạy; mèo chạy '
    context=server._video_context('seedance-2.5',15,'16:9',[])
    token=record_preflight_result(hashlib.sha256(b'test').hexdigest()[:24],raw,{'status':'allow'},context=context)['token']
    async def form():return FormData({'request_id':'raw-review','prompt':raw,'model':'seedance-2.5','duration':'15','ratio':'16:9','preflight_token':token})
    result=await server.admin_create_video(SimpleNamespace(form=form),'test')
    assert studio[0].get(result.id)['prompt']==raw
    await asyncio.sleep(0)

from test_prompt_analysis import ai_http

@pytest.mark.asyncio
async def test_ai_receives_exact_editor_text_for_anchor_occurrences(ai_http):
    import ai_provider
    raw=' mèo chạy; mèo chạy '
    await ai_provider.preflight_check_prompt(raw)
    sent=json.loads(ai_http['requests'][0]['messages'][-1]['content'])
    assert sent['prompt_data']==raw


def test_pure_observation_window_starts_before_submission_ack(monkeypatch):
    import pure_api_gen as pure
    from types import SimpleNamespace
    clock=[100.0];captured={}
    monkeypatch.setattr(pure,'time',SimpleNamespace(time=lambda:clock[0]))
    def submit(*a,**k):
        captured['submitted_at']=clock[0];clock[0]+=60
        return SimpleNamespace(error='',conversation_id='same')
    fake=SimpleNamespace(setup_session=lambda **k:(None,None,{}),warmup_login_session=lambda *a:None,dola_api_post_json=lambda *a,**k:None,submit_chat_completion=submit)
    monkeypatch.setattr(pure,'_dola',lambda:fake);monkeypatch.setattr(pure,'install',lambda:None);monkeypatch.setattr(pure,'_load_state',lambda _:None)
    monkeypatch.setattr(pure,'_finish_pure_video',lambda **k:k)
    r=pure.generate_pure_video(account='a',prompt='cat',ratio='16:9',duration=15,model='seedance-2.5',cookie_state_path='unused',timeout=1800)
    assert r['deadline']==captured['submitted_at']+1800


@pytest.mark.asyncio
async def test_browser_observation_window_includes_ack_wait(monkeypatch):
    # Legacy tests replace sys.modules['video_worker_ui']; execute the actual
    # function source against platform boundaries rather than inheriting their stub.
    import ast, re, types
    from types import SimpleNamespace
    path=Path(__file__).resolve().parents[1]/'video_worker_ui.py'
    definition=next(n for n in ast.parse(path.read_text()).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='generate_video')
    ui=types.ModuleType('_deadline_browser_source')
    ui.__dict__.update(asyncio=asyncio,config=server.config,re=re,VIDEO_BTN='fixture',cookie_value=lambda *a:'')
    exec(compile(ast.Module(body=[definition],type_ignores=[]),str(path),'exec'),ui.__dict__)
    clock=[100.0];captured={}
    async def noop(*a,**k):pass
    class Element:
        first=None
        def __init__(self):self.first=self
        count=lambda self:asyncio.sleep(0,result=1)
        is_visible=lambda self:asyncio.sleep(0,result=True)
        click=noop
    class Page:
        url='https://www.dola.com/chat/123'
        goto=click=noop
        get_by_text=lambda *a,**k:Element()
        query_selector=lambda *a,**k:asyncio.sleep(0,result=Element())
        async def wait_for_timeout(self,ms):clock[0]+=ms/1000
    async def press(*a,**k):captured['submitted_at']=clock[0];clock[0]+=60
    page=Page();page.keyboard=SimpleNamespace(type=noop,press=press)
    context=SimpleNamespace(pages=[page],cookies=lambda *a:asyncio.sleep(0,result=[]),close=noop)
    class Platform:
        async def __aenter__(self):return None
        async def __aexit__(self,*a):pass
    monkeypatch.setattr(ui,'time',SimpleNamespace(time=lambda:clock[0]),raising=False)
    monkeypatch.setattr(ui,'async_playwright',Platform,raising=False)
    monkeypatch.setattr(ui,'launch_account_context',lambda *a,**k:asyncio.sleep(0,result=context),raising=False)
    for name in ['dismiss_popups','ensure_login_stable','_preflight_balance']:monkeypatch.setattr(ui,name,noop,raising=False)
    monkeypatch.setattr(ui,'find_captcha_frame',lambda _:None,raising=False)
    monkeypatch.setattr(ui,'is_logged_out',lambda _:asyncio.sleep(0,result=False),raising=False)
    async def poll(*args):captured['remaining']=args[4];return {}
    monkeypatch.setattr(ui,'poll_conversation',poll,raising=False)
    await ui.generate_video('a','cat',duration=15,timeout=1800,on_conversation_id=lambda a,c,d:captured.update(deadline=d))
    assert captured['deadline']==captured['submitted_at']+1800
    assert captured['remaining']==captured['deadline']-clock[0]


def test_reusing_prompt_enters_single_creator_instead_of_old_folder_batch():
    node("""let analysisTaskId='old',rewriteHistory=[];function batchInputsChanged(){}const fields={'#gen_mode':{value:'folder'},'#gen_copies':{value:'2'},'#gen_prompt':{},'#gen_model':{},'#gen_duration':{},'#gen_ratio':{}};
function q(s){return fields[s];}async function api(){return {prompt:'Chosen prompt',model:'seedance-2.5',duration:15,ratio:'16:9'};}
function closeLibraryDetail(){}function onModelChange(){}function onPromptInput(){}function switchTab(){}
"""+js_function('useTaskPrompt')+"\nuseTaskPrompt('selected').then(()=>{if(fields['#gen_mode'].value!=='single'||fields['#gen_copies'].value!=='1')throw Error('Old batch inputs still control generation');});")


def test_shared_batch_analysis_preserves_selected_image_roles():
    node("""let sent,adminKey='fixture';const referenceHashPromise=Promise.resolve();let selectedImages=[],referenceRoles=['character'],analysisTaskId='';
selectedImages=[new Blob(['image'])];function q(s){return {value:s==='#gen_duration'?'15':s==='#gen_ratio'?'16:9':'seedance-2.5'};}
function isBatchGeneration(){return true;}function showLogin(){}
async function fetch(url,args){sent=JSON.parse(args.body.get('analysis'));return {ok:true,status:200,json:async()=>({status:'allow'})};}
"""+js_function('requestPreflight')+"\nrequestPreflight('cat').then(()=>{if(sent.reference_roles[0]!=='character')throw Error('Selected batch role was ignored');});")


def test_safe_stdout_print_redacts_joined_credentials(capsys):
    import failure_text
    failure_text.print_safe('request sessionid=', 'PRIVATE', 'Cookie: c_user=COOKIE', flush=True)
    text=capsys.readouterr().out
    assert 'PRIVATE' not in text and 'COOKIE' not in text and 'request' in text


def test_server_stdout_uses_redaction(capsys):
    getattr(server,'print',print)('operation failed sessionid=PRIVATE')
    assert 'PRIVATE' not in capsys.readouterr().out


def test_template_clears_failed_video_provenance():
    node("""let analysisTaskId='old-failure',rewriteHistory=[];const fields={'#gen_prompt':{value:'old'},'#gen_mode':{},'#gen_copies':{},'#gen_model':{},'#gen_duration':{},'#gen_ratio':{},'#gen_rewrite_instructions':{},'#templateMessage':{},'#promptTemplates':{value:'one'}};
function q(s){return fields[s];}function promptTemplates(){return [{id:'one',prompt:'new template',model:'seedance-2.5',duration:30,ratio:'9:16'}];}function confirm(){return true;}function renderDraftSource(){}function batchInputsChanged(){}function onModelChange(){}function onPromptInput(){}
"""+js_function('applyPromptTemplate')+"\napplyPromptTemplate();if(analysisTaskId||fields['#gen_prompt'].value!=='new template')throw Error('Stale failure provenance');")


def test_selecting_another_video_clears_old_actions_before_response():
    node("""let mySelectedId='A';const preview={textContent:'old video A',style:{}},drawer={open:true},title={dataset:{}},field={};function q(s){return s==='#mySelectedPreview'?preview:s==='#libraryVideoTitle'?title:s==='#libraryDrawer'?drawer:field;}function api(){return new Promise(()=>{});}function renderJobInto(){throw Error('Response has not arrived');}
"""+js_function('selectMyVideo')+"\nselectMyVideo('B');if(!preview.textContent.includes('Đang tải')||preview.textContent.includes('old video A'))throw Error('Previous video controls still visible');")


def test_analysis_only_replacements_are_selectable():
    node("""let promptAnalysis;const box={innerHTML:''};function q(s){return s==='#gen_analysis'?box:{value:'original'};}function esc(s){return String(s);}function hide(){}function show(){}
"""+js_function('renderPromptAnalysis')+"""
renderPromptAnalysis({quality_summary:'review',issues:[{id:'note',kind:'capability',applicable:false,reason_vi:'uncertain',replacement:null,confidence:'high'},{id:'edit',kind:'quality',applicable:true,reason_vi:'clear',replacement:'edited',confidence:'medium'}]},'snapshot');
if(box.innerHTML.includes('data-issue-id="note"')||!box.innerHTML.includes('data-issue-id="edit"')||!box.innerHTML.includes('AI tự đánh giá'))throw Error('Information note was selectable or AI confidence overstated');
""")


def test_navigation_closes_native_library_dialog():
    node("""let curTab,settingsTab;const drawer={open:true},field={style:{}};function q(s){return s==='#libraryDrawer'?drawer:field;}const document={querySelectorAll:()=>[]};function closeLibraryDetail(){drawer.open=false;}function refresh(){}
"""+js_function('switchTab')+"\nswitchTab('generate');if(drawer.open||curTab!=='generate')throw Error('Native dialog still blocks navigation');")


def test_library_dates_use_local_midnight_and_exclusive_end():
 node("""process.env.TZ='Asia/Ho_Chi_Minh';const fields={'#libraryFrom':{value:'2026-10-01'},'#libraryTo':{value:'2026-10-01'}};function q(s){return fields[s];}
"""+js_function('libraryDateRange')+"""
const r=libraryDateRange();if(r.created_after!==1790787600||r.created_before-r.created_after!==86400)throw Error(JSON.stringify(r));
fields['#libraryTo'].value='2026-09-30';let rejected=false;try{libraryDateRange();}catch(e){rejected=true;}if(!rejected)throw Error('Reversed range accepted');
""")


def test_batch_review_includes_configuration_and_reference_scope():
 node("""const fields={'#gen_model':{value:'seedance-2.5'},'#gen_duration':{value:'30'},'#gen_ratio':{value:'9:16'},'#gen_mode':{value:'folder'}};function q(s){return fields[s];}const selectedImages=[{},{}];
"""+js_function('batchConfirmationText')+"""
const s=batchConfirmationText([{reference_indices:[0]},{reference_indices:[1]}],2);for(const piece of ['2 prompt','4 video','30 giây','9:16','ảnh riêng','seedance-2.5'])if(!s.includes(piece))throw Error(s);
fields['#gen_mode'].value='list';if(!batchConfirmationText([{},{}],1).includes('2 ảnh dùng chung'))throw Error('Missing shared image scope');
""")


def test_notice_sound_is_opt_in_and_handles_suspended_browser():
 node("""let checked=false,count=0;function q(s){return {checked,textContent:''};}const noticeAudio={state:'running',currentTime:0,destination:{},createOscillator(){count++;return {frequency:{value:0},connect(){},start(){},stop(){}};},createGain(){return {gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){}};}};
"""+js_function('playNoticeSound')+"""
playNoticeSound();if(count)throw Error('Sound started without permission');checked=true;playNoticeSound();if(count!==1)throw Error('Enabled sound missing');noticeAudio.state='suspended';playNoticeSound();if(count!==1)throw Error('Suspended audio should not play');
""")


def test_title_draft_survives_same_job_poll():
 node("""let mySelectedId='A';const preview={style:{}},drawer={open:true},title={value:'Bản đang gõ',dataset:{taskId:'A'}};function q(s){return s==='#mySelectedPreview'?preview:s==='#libraryVideoTitle'?title:drawer;}async function api(){return {id:'A',title:'Tên đã lưu',status:'completed'};}function renderJobInto(){}
"""+js_function('selectMyVideo')+"\nselectMyVideo('A').then(()=>{if(title.value!=='Bản đang gõ')throw Error('Polling lost title draft');});")


def test_sound_observes_terminal_jobs_while_settings_open():
 node("""let noticePolling=false;const fields={'#noticeSound':{checked:true},'#loginMask':{style:{display:'none'}}};function q(s){return fields[s];}let reads=0,seen=false,missing=false;async function api(){reads++;return {tasks:[{id:'A',status:'completed'}],running_tasks:[]};}function observeJobs(rows){seen=rows.some(r=>r.id==='A'&&r.status==='completed');}async function observeMissingJobs(){missing=true;}
"""+js_function('pollNoticeJobs')+"\npollNoticeJobs().then(()=>{if(!seen||!missing||noticePolling||reads!==1)throw Error('Settings lost completion observation');});")
