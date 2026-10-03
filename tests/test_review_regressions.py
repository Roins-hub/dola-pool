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
    node("""let aiCalls=0,recovered=false;let pendingRequestId='accepted',pendingSnapshot='same',currentPreflight=null,batchPreflight=null,currentBatchId='',currentActiveTaskId='',studioPollTimer=null;
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
