"""Real provider/parser/cache/API contract, with only AI HTTP replaced."""
import copy
import asyncio
import json
import httpx
import pytest
import ai_provider as ai
import ai_preflight as gate
import server

CONFIG={'provider':'openai','api_key':'dummy','model':'fixture','base_url':'https://ai.invalid/v1'}
RICH={'status':'allow','reason':'','suggestions':'Làm rõ góc máy',
      'quality_summary':'Ý tưởng rõ; góc máy còn mơ hồ.',
      'issues':[{'id':'camera','kind':'quality','severity':'suggestion','quote':'quay đẹp',
                 'occurrence':1,'replacement':'máy quay tiến chậm về phía chú mèo',
                 'reason_vi':'Góc máy chưa cụ thể.','confidence':'high',
                 'source_ids':['visual-action','invented-source']} ]}

@pytest.fixture
def ai_http(monkeypatch):
    state={'response':copy.deepcopy(RICH),'requests':[]}
    async def handler(request):
        await asyncio.sleep(state.get("delay",0))
        state['requests'].append(json.loads(request.content))
        return httpx.Response(200,json={'choices':[{'message':{'content':json.dumps(state['response'])}}]})
    cls=httpx.AsyncClient
    monkeypatch.setattr(ai,'get_active_provider_config',lambda:CONFIG)
    monkeypatch.setattr(ai.httpx,'AsyncClient',lambda *a,**k:cls(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(server,'_admin_auth',lambda _:None)
    return state

@pytest.mark.asyncio
async def test_rich_response_survives_provider_cache_api(ai_http):
    result=await server.admin_prompt_preflight(server.PromptPreflightRequest(prompt='Chú mèo, quay đẹp'),x_admin_key=None)
    assert result['quality_summary']=='Ý tưởng rõ; góc máy còn mơ hồ.'
    issue=result['issues'][0]
    assert issue['quote']=='quay đẹp' and issue['replacement'].startswith('máy quay')
    assert issue['confidence']=='high'
    assert issue['source_ids']==['visual-action']
    assert gate._PREFLIGHT_TOKENS[result['token']]['issues']==result['issues']
    assert result['rules_version']

@pytest.mark.asyncio
async def test_quality_warning_does_not_block_safe_prompt(ai_http):
    result=await ai.preflight_check_prompt('Chú mèo, quay đẹp',context={'model':'seedance-2.5','duration':10})
    assert result['status']=='allow'
    assert result['issues'][0]['kind']=='quality'
    content=ai_http['requests'][0]['messages'][-1]['content']
    assert 'seedance-2.5' in content and '10' in content

@pytest.mark.asyncio
@pytest.mark.parametrize('bad',[[], {'status':'allow','issues':'oops'},
    {'status':'allow','issues':[{'kind':'made-up'}]},
    {'status':'allow','quality_summary':{'text':'not a string'}},
    {'status':'allow','issues':[dict(RICH['issues'][0],quote='x'*4001)]}])
async def test_invalid_schema_never_issues_token(ai_http,bad):
    ai_http['response']=bad
    before=set(gate._PREFLIGHT_TOKENS)
    with pytest.raises(server.HTTPException) as error:
        await server.admin_prompt_preflight(server.PromptPreflightRequest(prompt='cat'),x_admin_key=None)
    assert error.value.status_code==502
    assert set(gate._PREFLIGHT_TOKENS)==before


def test_surface_rule_does_not_claim_dola_api_limits():
    import prompt_rules
    rules=prompt_rules.rules_for_context({'model':'seedance-2.5','duration':30})
    assert rules and all(r['id'] and r['source'] and r['evidence'] for r in rules)
    limits=next(r for r in rules if r['id']=='backend-capabilities')
    assert limits['scope']=='dola-studio-backend'
    assert 'không bảo đảm' in limits['text']
    assert not any(r['scope']=='byteplus-api' for r in rules)

@pytest.mark.asyncio
async def test_rewrite_preserves_requested_storyboard_contract(ai_http):
    ai_http['response']='unused'
    # This transport captures the actual instruction delivered to the external service.
    await ai.rewrite_prompt_text('00:00 - 00:05: mèo bước vào',instructions='Giữ cấu trúc cảnh')
    instructions=ai_http['requests'][0]['messages'][0]['content']
    assert 'TUYỆT ĐỐI KHÔNG ghi thời lượng' not in instructions
    assert 'Giữ mốc thời gian' in instructions
