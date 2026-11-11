"""用假上游验证未知回执判断、有限换号和代答，不提交真实生成。"""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import dola_responder as r
from generation_errors import AIAccountUnavailableError, UpstreamReplyError
from test_dola_responder import FakeResp, FakeSession
from test_doubao import FREE_ACCEPTED, _FakeDP, _gen, _pool, _run, _stubs

UNKNOWN_LIMIT = '当前创作余额已不足本次任务所需，请补足可用资源后再试。'
QUESTION = '请选镜头方案：A 保持原分镜，B 另写剧情？'


@pytest.fixture(autouse=True)
def responder_settings():
    r.apply_settings({})
    yield
    r.apply_settings({})


def decision(action='reply', category='other', text=QUESTION, reply='A，保持原分镜'):
    return {'action': action, 'category': category, 'reply': reply,
            'reason': '按原任务和上游回执处理', 'evidence': text}


def stub_ai(monkeypatch, result):
    calls = []
    async def analyze(text, **kwargs):
        calls.append((text, kwargs))
        return result, ''
    monkeypatch.setattr(r, 'enabled', lambda: True)
    monkeypatch.setattr(r, 'ai_decision_detail', analyze)
    return calls


@pytest.mark.parametrize('change', [
    {'action': 'delete_account'}, {'category': 'unknown'}, {'action': []},
    {'action': 'switch_account', 'category': 'quota', 'evidence': '原文没说过'},
    {'action': 'switch_account', 'category': 'missing_input'},
    {'reply': '同意使用付费额度'}, {'reply': '改成9:16'}, {'reply': '改成10秒'},
    {'reply': '改用 Seedance 2.5'}, {'reply': '请按 Seedance 2.0 Mini 生成'},
    {'reply': {'text': '确认'}},
])
def test_invalid_or_unsafe_ai_decision_is_rejected(change):
    data = decision()
    data.update(change)
    assert r.check_decision(data, QUESTION, '16:9', 15, 'Seedance 2.0 Fast') is None


def test_ai_cannot_switch_or_reply_to_a_content_refusal():
    text = '内容审核不通过，无法生成视频。'
    data = r.check_decision(decision('switch_account', 'quota', text), text, '16:9', 15)
    assert data['action'] == 'stop' and data['category'] == 'content'
    with pytest.raises(UpstreamReplyError) as caught:
        r.decision_reply(data, text)
    assert not isinstance(caught.value, AIAccountUnavailableError)
    assert caught.value.allow_account_retry is False
    assert text in str(caught.value)


def test_structured_call_sends_task_context_without_transport_secrets():
    settings = r.normalize({'base_url': 'https://gw.example', 'api_key': 'test-key', 'model': 'text-model'})
    text = QUESTION + ' Cookie=secret-session https://cdn.example/video?signature=secret'
    seen = []
    class Session(FakeSession):
        def post(self, url, data=None, headers=None):
            seen.append(json.loads(data))
            return super().post(url, data, headers)
    sess = Session(FakeResp(200, {'choices': [{'message': {'content': json.dumps(decision())}}]}))
    out, why = _run(r.ai_decision_detail(text, '16:9', 15, 2, prompt='小猫睡觉',
                                        model='Seedance 2.0 Fast', site='豆包', settings=settings, session=sess))
    assert out['reply'] == 'A，保持原分镜' and not why
    payload = json.loads(seen[0]['messages'][1]['content'])
    assert payload['original_prompt'] == '小猫睡觉' and payload['reference_images'] == 2
    assert payload['site'] == '豆包' and payload['model'] == 'Seedance 2.0 Fast'
    assert 'secret-session' not in payload['assistant_text'] and 'signature=secret' not in payload['assistant_text']


def test_decisions_are_bounded_and_identical_text_is_not_sent_twice(monkeypatch):
    calls = stub_ai(monkeypatch, decision('wait'))
    item = r.DialogueResponder(prompt='猫', ratio='16:9', duration=15)
    for _ in range(20):
        assert _run(item.decide(QUESTION))['action'] == 'wait'
    assert len(calls) == 1
    _run(item.decide('第二个问题'))
    _run(item.decide('第三个问题'))
    with pytest.raises(UpstreamReplyError, match='3 次上限'):
        _run(item.decide('第四个问题'))
    assert len(calls) == 3


def bounded_gen(monkeypatch, tmp_path, dp):
    fetch = dp.fetch_single_chain
    calls = []
    class PollingLimit(BaseException):
        pass
    def read(*args, **kwargs):
        calls.append(1)
        if len(calls) > 20:
            raise PollingLimit('假上游轮询超过20次')
        return fetch(*args, **kwargs)
    dp.fetch_single_chain = read
    return _run(asyncio.to_thread(_gen, monkeypatch, tmp_path, dp))


def test_doubao_unknown_question_is_answered_with_same_video_skill(monkeypatch, tmp_path):
    calls = stub_ai(monkeypatch, decision())
    dp = _FakeDP([(0, QUESTION, []), (1, FREE_ACCEPTED, [])], vid_after=1)
    result = bounded_gen(monkeypatch, tmp_path, dp)
    assert result['site'] == 'doubao' and dp.replies == ['A，保持原分镜'] and len(calls) == 1
    param = json.loads(dp.reply_payloads[0]['chat_ability']['ability_param'])
    assert param['duration'] == 5 and param['ratio'] == '16:9'
    assert param['input_box_content']['user_input_content'] == 'A，保持原分镜'


def test_doubao_unknown_quota_can_request_a_switch_without_confirmation(monkeypatch, tmp_path):
    stub_ai(monkeypatch, decision('switch_account', 'quota', UNKNOWN_LIMIT))
    dp = _FakeDP([(0, UNKNOWN_LIMIT, [])])
    with pytest.raises(AIAccountUnavailableError, match=UNKNOWN_LIMIT):
        bounded_gen(monkeypatch, tmp_path, dp)
    assert not dp.replies and not dp.downloaded


def test_doubao_generic_inability_is_not_misclassified_as_quota(monkeypatch, tmp_path):
    text = '目前无法生成这个视频，请提供具体场景。'
    import doubao_gen
    assert not doubao_gen.looks_like_quota_exhausted(text)
    stub_ai(monkeypatch, decision('stop', 'missing_input', text))
    dp = _FakeDP([(0, text, [])])
    with pytest.raises(UpstreamReplyError) as caught:
        bounded_gen(monkeypatch, tmp_path, dp)
    assert caught.value.category == '需要补充信息' and not dp.replies


def test_doubao_historical_acceptance_prevents_ai_reply_or_switch(monkeypatch, tmp_path):
    calls = stub_ai(monkeypatch, decision('switch_account', 'quota', UNKNOWN_LIMIT))
    dp = _FakeDP([(0, FREE_ACCEPTED, []), (0, UNKNOWN_LIMIT, [])])
    fetch = dp.fetch_single_chain
    count = []
    def read(*args, **kwargs):
        count.append(1)
        if len(count) >= 3:
            dp.vid_after = 0
        return fetch(*args, **kwargs)
    dp.fetch_single_chain = read
    result = bounded_gen(monkeypatch, tmp_path, dp)
    assert result['site'] == 'doubao' and not dp.replies and not calls


def test_doubao_does_not_answer_an_old_question_before_latest_denial(monkeypatch, tmp_path):
    calls = stub_ai(monkeypatch, decision())
    dp = _FakeDP([(0, QUESTION, []), (0, '内容审核不通过，无法生成视频。', [])])
    with pytest.raises(UpstreamReplyError, match='内容拒绝'):
        bounded_gen(monkeypatch, tmp_path, dp)
    assert not dp.replies and not calls


@pytest.mark.parametrize('text', ['受内容安全策略限制，此次请求被拒绝。', 'This violates content policy.'])
def test_doubao_known_policy_denial_stops_without_ai_or_quota_lock(monkeypatch, tmp_path, text):
    calls = stub_ai(monkeypatch, decision('switch_account', 'quota', text))
    dp = _FakeDP([(0, text, [])])
    with pytest.raises(UpstreamReplyError) as caught:
        bounded_gen(monkeypatch, tmp_path, dp)
    assert caught.value.category == '内容拒绝' and not dp.replies and not calls


def test_doubao_unfinished_turn_is_not_sent_to_ai(monkeypatch, tmp_path):
    calls = stub_ai(monkeypatch, decision())
    dp = _FakeDP([(0, QUESTION, [], {'is_finish': '0'})])
    fetch = dp.fetch_single_chain
    reads = []
    def read(*args, **kwargs):
        reads.append(1)
        if len(reads) >= 3:
            dp.vid_after = 0
        return fetch(*args, **kwargs)
    dp.fetch_single_chain = read
    assert bounded_gen(monkeypatch, tmp_path, dp)['site'] == 'doubao'
    assert not calls and not dp.replies


def test_scheduler_pauses_ai_denied_account_and_uses_next_without_charging(tmp_path, monkeypatch):
    _, pool = _pool(tmp_path, monkeypatch)
    calls, events = [], []
    async def generate(account, *args, **kwargs):
        calls.append(account)
        if account == 'db1':
            raise AIAccountUnavailableError('套餐权限', UNKNOWN_LIMIT)
        return {'account': account, 'local_path': 'video.mp4'}
    pool._generate_effective = generate
    out = _run(pool.generate_video('猫', '16:9', 5, 'doubao-seedance-2.0-mini', on_wait=events.append))
    row = next(a for a in pool.list_accounts() if a['name'] == 'db1')
    assert out['account'] == 'db2' and calls == ['db1', 'db2']
    assert row['abnormal_until'] > time.time() and row['login_ok'] == 1 and pool.used_today('db1') == 0
    assert any('AI 换号 1/2' in e and UNKNOWN_LIMIT in e for e in events)


def test_scheduler_limits_ai_rotation_even_with_unlimited_attempts(tmp_path, monkeypatch):
    _, pool = _pool(tmp_path, monkeypatch, doubao=('db1', 'db2', 'db3', 'db4'))
    calls = []
    async def generate(account, *args, **kwargs):
        calls.append(account)
        raise AIAccountUnavailableError('额度不足', UNKNOWN_LIMIT)
    pool._generate_effective = generate
    with pytest.raises(AIAccountUnavailableError):
        _run(pool.generate_video('猫', '16:9', 5, 'doubao-seedance-2.0-mini', max_attempts=0,
                                 deadline=time.time() + 60))
    assert len(calls) == 3 and len(set(calls)) == 3


def test_fixed_account_test_never_rotates_on_ai_decision(tmp_path, monkeypatch):
    _, pool = _pool(tmp_path, monkeypatch)
    calls = []
    async def generate(account, *args, **kwargs):
        calls.append(account)
        raise AIAccountUnavailableError('额度不足', UNKNOWN_LIMIT)
    pool._generate_effective = generate
    out = _run(pool.test_generate('db1', '猫', '16:9', 5, 'doubao-seedance-2.0-mini'))
    assert not out['ok'] and calls == ['db1'] and not out.get('recoverable')


def test_ai_stop_cannot_be_retried_by_portrait_fallback(tmp_path, monkeypatch):
    _, pool = _pool(tmp_path, monkeypatch)
    calls = []
    text = '肖像保护审核拒绝，无法生成视频。'
    async def generate(account, *args, **kwargs):
        calls.append(account)
        r.decision_reply(decision('stop', 'content', text), text)
    pool._generate_effective = generate
    with pytest.raises(UpstreamReplyError, match='内容拒绝'):
        _run(pool.generate_video('猫', '16:9', 5, 'doubao-seedance-2.0-mini'))
    assert calls == ['db1']


def test_work_pair_shares_and_saves_ai_switch_limit_across_segments(tmp_path, monkeypatch):
    from test_work_mode import _agent, _work_pool
    from pathlib import Path
    import config
    import work_pair
    browser_pool, pool = _work_pool(tmp_path, monkeypatch, ['acc1', 'acc2', 'acc3', 'acc4', 'acc5'])
    monkeypatch.setattr(config, 'DOWNLOAD_DIR', str(tmp_path))
    monkeypatch.setattr(browser_pool, 'ensure_web_playable', lambda path: path)
    def frame(source, path):
        Path(path).write_bytes(b'frame')
        return Path(path)
    monkeypatch.setattr(work_pair, 'last_frame', frame)
    calls, states = [], []
    async def generate(account, *args, **kwargs):
        calls.append(account)
        if len(calls) != 3:
            raise AIAccountUnavailableError('额度不足', UNKNOWN_LIMIT)
        path = tmp_path / 'first-result.mp4'
        path.write_bytes(b'first')
        return {'account': account, 'local_path': str(path)}
    _agent(monkeypatch, generate)
    with pytest.raises(RuntimeError, match='第 2 段失败'):
        _run(pool.generate_video('猫', '16:9', 30, 'seedance-2.0', max_attempts=0,
            deadline=time.time() + 60, on_pair_state=lambda state: states.append(json.loads(json.dumps(state)))))
    assert len(calls) == 4 and states[-1]['ai_switches'] == 2
    assert Path(states[-1]['first']['local_path']).read_bytes() == b'first'


def test_browser_dola_unknown_limit_reaches_ai_and_does_not_type(monkeypatch):
    from test_start_confirm import _poll, _stall_run
    calls = stub_ai(monkeypatch, decision('switch_account', 'quota', UNKNOWN_LIMIT))
    error, typed = _stall_run([_poll(UNKNOWN_LIMIT, clarifying=True)] * 6, with_typed=True)
    assert isinstance(error, AIAccountUnavailableError) and not typed and len(calls) == 1


def test_browser_dola_started_job_does_not_ask_ai(monkeypatch):
    from test_start_confirm import _poll, _stall_run
    calls = stub_ai(monkeypatch, decision('switch_account', 'quota', UNKNOWN_LIMIT))
    _, typed = _stall_run([_poll(UNKNOWN_LIMIT, clarifying=True, any_video_gen=True)] * 6, with_typed=True)
    assert not calls and not typed


def test_work_dola_unknown_limit_reaches_ai_and_does_not_type(monkeypatch):
    from test_work_session import Page, bot, session, wait
    page = Page([bot(UNKNOWN_LIMIT)] * 5)
    _, item = session(monkeypatch, page)
    calls = stub_ai(monkeypatch, decision('switch_account', 'quota', UNKNOWN_LIMIT))
    with pytest.raises(AIAccountUnavailableError):
        wait(item)
    assert len(calls) == 1 and not page.typed


def test_work_dola_generating_with_paused_ui_does_not_ask_ai(monkeypatch):
    from test_work_session import Page, bot, session, video, wait
    page = Page([bot('视频处理中', False, tool=True)] * 10 + [video()], paused=True, question=UNKNOWN_LIMIT)
    _, item = session(monkeypatch, page)
    calls = stub_ai(monkeypatch, decision('switch_account', 'quota', UNKNOWN_LIMIT))
    wait(item)
    assert not calls and not page.typed


def pure_handler(monkeypatch, text=QUESTION):
    import pure_api_gen as pure
    from test_work_session import bot
    responder = r.DialogueResponder(prompt='原始分镜', ratio='16:9', duration=15, model='Seedance 2.0 Fast')
    sent = []
    monkeypatch.setattr(pure, '_chat_reply', lambda **kwargs: sent.append(kwargs) or True)
    dp = SimpleNamespace(looks_like_generation_accepted=lambda text: False)
    callback = pure._dialogue_handler(dola=dp, session=None, ctx={}, headers={}, conversation_id='conv1',
                                      account='acc1', responder=responder)
    poll = SimpleNamespace(status='pending', source_data=bot(text), creation_statuses=[])
    return callback, poll, sent


def test_pure_dola_unknown_reply_is_answered_once_in_original_conversation(monkeypatch):
    calls = stub_ai(monkeypatch, decision())
    callback, poll, sent = pure_handler(monkeypatch)
    _run(asyncio.to_thread(callback, poll))
    _run(asyncio.to_thread(callback, poll))
    assert len(calls) == len(sent) == 1 and sent[0]['conversation_id'] == 'conv1'


@pytest.mark.parametrize('started', ['has_video_gen', 'ai_creation_tool_list', 'user_after', 'unfinished', 'missing_chain'])
def test_pure_dola_only_analyzes_latest_finished_unaccepted_reply(monkeypatch, started):
    calls = stub_ai(monkeypatch, decision())
    callback, poll, sent = pure_handler(monkeypatch)
    latest = poll.source_data['messages'][0]
    if started == 'has_video_gen':
        latest['ext'][started] = '1'
    elif started == 'ai_creation_tool_list':
        latest['ext'][started] = '[{"status":1}]'
    elif started == 'user_after':
        poll.source_data['messages'].append(dict(latest, user_type=1, index_in_conv=3, message_id='u3'))
    elif started == 'unfinished':
        latest['ext'] = {}
    else:
        poll.source_data = None
    _run(asyncio.to_thread(callback, poll))
    assert not calls and not sent


def test_protocol_polling_calls_pending_handler_before_next_poll(monkeypatch):
    from protocol import dola_pure_api as dp
    polls = iter([dp.PollResult([], [], 'pending', [], [], []), dp.PollResult(['video'], [], 'succeeded', [], [], [])])
    monkeypatch.setattr(dp, 'inspect_video_once', lambda *args: next(polls))
    monkeypatch.setattr(dp.time, 'sleep', lambda seconds: None)
    pending = []
    result = dp.get_video_result(SimpleNamespace(proxies={}), {}, 'conv1', on_pending=pending.append)
    assert result['status'] == 'succeeded' and len(pending) == 1


def test_pure_generic_failure_can_be_judged_as_a_question_and_answered(monkeypatch):
    from protocol import dola_pure_api as dp
    calls = stub_ai(monkeypatch, decision())
    callback, poll, sent = pure_handler(monkeypatch)
    poll.status = 'failed'
    polls = iter([poll, poll, dp.PollResult(['video'], [], 'succeeded', [], [], [])])
    monkeypatch.setattr(dp, 'inspect_video_once', lambda *args: next(polls))
    monkeypatch.setattr(dp.time, 'sleep', lambda seconds: None)
    out = dp.wait_for_video(None, {}, 'conv1', on_pending=lambda poll: _run(asyncio.to_thread(callback, poll)))
    assert out.status == 'succeeded' and len(calls) == len(sent) == 1
