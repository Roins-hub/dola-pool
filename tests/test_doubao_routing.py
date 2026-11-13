"""截图回归：创作参数、普通助手误接单、AI 原文格式和客户端问号编码。"""
import json
from types import SimpleNamespace

import pytest

import dola_responder as r
from generation_errors import UpstreamReplyError
from test_ai_dialogue import bounded_gen, decision, stub_ai
from test_dola_responder import FakeResp, FakeSession
from test_doubao import FREE_ACCEPTED, _FakeDP, _pool, _run, _stubs


@pytest.fixture(autouse=True)
def settings_reset():
    r.apply_settings({})
    yield
    r.apply_settings({})


@pytest.mark.parametrize('model', ['seedance_v2.0_mini', 'seedance_v2.0', 'seedance_v2.0_std', 'seedance_v2.5'])
def test_doubao_video_ability_includes_resolution_for_original_and_followup(model):
    from protocol import doubao_site
    dp = doubao_site.load()
    for conv in ('', 'original-conversation'):
        payload, _, _ = dp.build_payload([], '清晨湖面', {'fp':'test'}, 10, model, '16:9', conversation_id=conv)
        param = json.loads(payload['chat_ability']['ability_param'])
        assert payload['chat_ability']['ability_type'] == 17
        assert param['resolution'] == '720p' and param['model'] == model
        assert param['duration'] == 10 and param['ratio'] == '16:9'
        assert payload['client_meta']['conversation_id'] == conv


def test_doubao_native_video_payload_includes_current_web_creation_routing_fields():
    from protocol import dola_pure_api, doubao_site

    dp = doubao_site.load()
    payload, _, _ = dp.build_payload([], '一只猫微笑', {'fp':'test'}, 10,
                                     'seedance_v2.0', 'auto')
    assert payload['client_meta']['bot_id'] == '7338286299411103781'
    assert payload['chat_ability']['ability_type'] == 17
    assert payload['option']['conversation_mode'] == 1
    assert payload['option']['is_old_user'] is True
    assert payload['option']['support_lazy_fetch_stream'] is True
    assert payload['option']['collect_id'] == ''
    assert json.loads(payload['ext']['general_task_param']) == {'server_do_compare': True}
    assert payload['ext']['is_finish'] == '1' and payload['ext']['collection_id'] == ''
    ability = json.loads(payload['chat_ability']['ability_param'])
    assert ability['model'] == 'seedance_v2.0' and ability['duration'] == 10
    assert ability['ratio'] == 'auto' and ability['resolution'] == '720p'

    dola_payload, _, _ = dola_pure_api.build_payload([], '一只猫微笑', {'fp':'test'}, 10,
                                                     'seedance_v2.0', '16:9')
    assert 'conversation_mode' not in dola_payload['option']
    assert 'general_task_param' not in dola_payload['ext']


def test_markdown_removed_from_ai_evidence_is_still_the_same_original_quote():
    text = '很抱歉，我目前**不能直接生成视频文件**，只能提供提示词。'
    data = decision('stop', 'other', text='很抱歉，我目前不能直接生成视频文件')
    errors = []
    out = r.check_decision(data, text, errors=errors)
    assert out['action'] == 'stop' and not errors


def test_ai_evidence_ignores_line_wraps_but_still_rejects_invented_claim():
    text = '免费额度\n**已用完**，请稍后再试。'
    data = decision('switch_account', 'quota', text='免费额度已用完')
    assert r.check_decision(data,text)['action'] == 'switch_account'
    data['evidence'] = '已扣除20点额度'
    errors = []
    assert r.check_decision(data,text, errors=errors) is None
    assert '证据与上游原文不匹配' in errors[0]


def test_ai_validation_reason_distinguishes_unsupported_action_and_model_change():
    text = '请选择方案'
    data = decision(text=text)
    data['action'] = 'change_model'
    errors = []
    assert r.check_decision(data, text, errors=errors) is None
    assert errors == ['AI 判断的动作或分类不受支持']
    data.update(action='reply', reply='改用 Seedance 2.5')
    errors = []
    assert r.check_decision(data,text,model='Seedance 2.0 Fast',errors=errors) is None
    assert errors == ['AI 代答改变了请求模型，已停止发送']


def test_default_ratio_accepts_request_consistent_reply_and_explicit_ratio_is_strict():
    assert r.check_reply('请按16:9生成10秒视频','default',10)
    assert r.check_reply('请按16:9生成10秒视频','9:16',10) is None


def test_ai_receives_the_end_of_a_long_finished_reply_and_detailed_validation_error():
    text = '已整理提示词：' + '场景描述。' * 350 + '\n要不要我再帮你写一个中文版？'
    seen = []
    class Session(FakeSession):
        def post(self, url, data=None, headers=None):
            seen.append(json.loads(data))
            return super().post(url,data,headers)
    data = decision('stop', 'other', text='要不要我再帮你写一个中文版？')
    sess = Session(FakeResp(200, {'choices':[{'message':{'content':json.dumps(data)}}]}))
    cfg = {'base_url':'https://gw.example','api_key':'test','model':'m'}
    out,why = _run(r.ai_decision_detail(text,'16:9',15,settings=cfg,session=sess))
    assert out['action'] == 'stop' and not why
    assert '要不要我再帮你写一个中文版？' in json.loads(seen[0]['messages'][1]['content'])['assistant_text']
    data['evidence'] = '助手没有说的内容'
    bad = Session(FakeResp(200, {'choices':[{'message':{'content':json.dumps(data)}}]}))
    out,why = _run(r.ai_decision_detail(text,settings=cfg,session=bad))
    assert out is None and '证据与上游原文不匹配' in why


@pytest.mark.parametrize('enabled',[True,False])
@pytest.mark.parametrize('text',[
    '我目前**不能直接生成视频文件**，可以给你提示词。',
    'Seedance 2.5 仅向200元档和500元档会员开放，请升级套餐。',
    '本次使用Seedance生成。以下是最终提示词，要不要中文版？',
])
def test_plain_assistant_reports_route_failure_without_claiming_quota_or_plan(tmp_path, monkeypatch, enabled, text):
    calls = stub_ai(monkeypatch,decision('switch_account','plan',text))
    monkeypatch.setattr(r,'enabled',lambda:enabled)
    dp = _FakeDP([(0,text,[],{'agent_name':'豆包','use_creation':'0','has_video_gen':'0'})])
    with pytest.raises(UpstreamReplyError) as caught:
        bounded_gen(monkeypatch,tmp_path,dp)
    assert caught.value.category == '视频技能未生效'
    from generation_errors import VideoSkillUnavailableError
    assert isinstance(caught.value, VideoSkillUnavailableError)
    assert caught.value.generation_started is False
    assert not calls and not dp.replies and not dp.downloaded


def test_plain_assistant_reply_stops_dispatch_without_disabling_the_account(tmp_path, monkeypatch):
    _,pool = _pool(tmp_path,monkeypatch)
    calls = []
    async def generate(account,*args,**kwargs):
        calls.append(account)
        raise UpstreamReplyError('视频技能未生效','普通助手没有调用创作',
                                 generation_started=False,allow_account_retry=False)
    pool._generate_effective=generate
    with pytest.raises(UpstreamReplyError,match='视频技能未生效'):
        _run(pool.generate_video('猫','16:9',10,'doubao-seedance-2.0-fast'))
    row=next(a for a in pool.list_accounts() if a['name']=='db1')
    assert calls==['db1'] and row['login_ok']==1 and not row['abnormal_until'] and not row['rate_limited']
    assert pool.used_today('db1')==0


def test_readonly_recovery_ends_plain_chat_but_keeps_genuine_generation_pending(tmp_path,monkeypatch):
    import doubao_gen as g
    state=tmp_path/'cookie_state.json'
    state.write_text(json.dumps({'cookies':{'sessionid':{'value':'test'}}}),encoding='utf-8')
    dp=_FakeDP([(0,'当前套餐不可用于创作使用。',[],{'agent_name':'豆包'})])
    monkeypatch.setattr(g,'_site',lambda:dp)
    monkeypatch.setattr(g,'_session',lambda *args:(SimpleNamespace(close=lambda:None),{},{}))
    out=g.fetch_conversation_video(state,'conv1',output_dir=tmp_path)
    assert out['final'] is True and not out['plan_unavailable'] and not dp.replies
    dp.script=[(0,FREE_ACCEPTED,[],{'agent_name':'Agent-Creation','use_creation':'1'})]
    assert g.fetch_conversation_video(state,'conv1',output_dir=tmp_path)['final'] is False
    dp.script=[(0,'仅整理提示词',[],{'agent_name':'豆包'})]
    dp.vid_after=0
    assert g.fetch_conversation_video(state,'conv1',output_dir=tmp_path)['local_path']
    assert not dp.replies


@pytest.mark.parametrize('prompt',[
    '???????????,????????????,??????????,????????,??????,????,???????',
    '??F-16??????????????,????????,?????????,????,??????,??????',
    '\ufffd'*25,
])
def test_corrupted_client_prompt_is_rejected_before_any_task_or_account_access(prompt):
    import server
    with pytest.raises(server.HTTPException) as caught:
        _run(server._submit_task({},'doubao-seedance-2.5',prompt,None,10,[]))
    assert caught.value.status_code==422 and 'UTF-8' in caught.value.detail


@pytest.mark.parametrize('prompt',['test','一只白鹭贴着水面起飞','What happens next? A bird flies away.',
                                  '人物问：“你去哪儿？”然后转身。','屏幕显示??????这六个问号，随后画面切换到海边'])
def test_valid_prompts_and_normal_questions_keep_their_text(prompt):
    import server
    assert server._check_prompt_encoding(prompt) is None
