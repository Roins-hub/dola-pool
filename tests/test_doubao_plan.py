"""豆包明确拒绝套餐创作权限时结束等待，暂停账号并释放后续派单。"""
import json
import time
from types import SimpleNamespace

import pytest

import config
from test_doubao import CONFIRM, FREE_ACCEPTED, PAID, _FakeDP, _gen, _pool, _run, _stubs

DENIED = "当前免费体验套餐不可用于创作使用，可升级正式订阅，畅享创作。"


@pytest.mark.parametrize('text,expected', [
    (DENIED, True),
    ('当前**免费体验套餐**不可用于创作使用，可升级正式订阅。', True),
    ('当前套餐不支持视频生成，请升级订阅。', True),
    ('当前订阅无法进行创作。', True),
    (FREE_ACCEPTED, False), (PAID, False), (CONFIRM, False),
    ('当前免费套餐额度已用完，预计明天恢复。', False),
    ('当前套餐支持创作，可生成15秒视频。', False),
])
def test_plan_denial_is_separate_from_acceptance_and_quota(text, expected):
    from doubao_gen import looks_like_plan_unavailable
    assert looks_like_plan_unavailable(text) is expected


@pytest.mark.parametrize('confirmed', [False, True])
def test_generation_stops_on_plan_denial_without_additional_confirmation(tmp_path, monkeypatch, confirmed):
    import doubao_gen
    script = [(0, CONFIRM, []), (1, DENIED, [])] if confirmed else [(0, DENIED, [])]
    dp = _FakeDP(script)
    with pytest.raises(doubao_gen.DoubaoPlanUnavailableError, match=DENIED):
        _gen(monkeypatch, tmp_path, dp)
    assert dp.replies == ['确认生成'] * confirmed
    assert not dp.downloaded


def test_plan_denial_wins_over_an_acceptance_phrase(tmp_path, monkeypatch):
    import doubao_gen
    dp = _FakeDP([(0, FREE_ACCEPTED + DENIED, [])])
    with pytest.raises(doubao_gen.DoubaoPlanUnavailableError):
        _gen(monkeypatch, tmp_path, dp)
    assert dp.replies == []


def test_dispatch_pauses_denied_account_and_uses_another(tmp_path, monkeypatch):
    import doubao_gen
    browser_pool, pool = _pool(tmp_path, monkeypatch)
    calls = []
    async def generate(account, *args, **kwargs):
        calls.append(account)
        if account == 'db1':
            raise doubao_gen.DoubaoPlanUnavailableError('db1 ' + DENIED)
        return {'account': account, 'local_path': 'video.mp4'}
    pool._generate_effective = generate
    result = _run(pool.generate_video('猫', '16:9', 15, 'doubao-seedance-2.0-fast', max_attempts=3))
    assert result['account'] == 'db2' and calls == ['db1', 'db2']
    row = next(row for row in pool.list_accounts() if row['name'] == 'db1')
    assert row['group'] == browser_pool.GROUP_ABNORMAL and row['login_ok'] == 1
    assert row['abnormal_until'] > time.time() and DENIED in row['abnormal_reason']
    assert not row['rate_limited'] and pool.used_today('db1') == 0


def test_all_plan_denied_fails_fast_and_next_task_skips_the_account(tmp_path, monkeypatch):
    import doubao_gen
    _, pool = _pool(tmp_path, monkeypatch, doubao=('db1',))
    calls = []
    async def generate(account, *args, **kwargs):
        calls.append(account)
        raise doubao_gen.DoubaoPlanUnavailableError('db1 ' + DENIED)
    pool._generate_effective = generate
    for expected in (['db1'], ['db1']):
        with pytest.raises(RuntimeError, match='套餐'):
            _run(pool.generate_video('猫', '16:9', 15, 'doubao-seedance-2.0-fast', max_attempts=0,
                                     deadline=time.time() + 600))
        assert calls == expected


def test_fixed_account_test_marks_plan_failure_without_recovery(tmp_path, monkeypatch):
    import doubao_gen
    browser_pool, pool = _pool(tmp_path, monkeypatch, doubao=('db1',))
    async def generate(*args, **kwargs):
        raise doubao_gen.DoubaoPlanUnavailableError(DENIED)
    pool._generate_effective = generate
    result = _run(pool.test_generate('db1', '猫', '16:9', 15, 'doubao-seedance-2.0-fast'))
    assert not result['ok'] and not result.get('recoverable') and DENIED in result['error']
    row = next(row for row in pool.list_accounts() if row['name'] == 'db1')
    assert row['group'] == browser_pool.GROUP_ABNORMAL and row['login_ok'] == 1


@pytest.mark.parametrize('video,finished', [(False, True), (False, False), (True, True)])
def test_recovery_reads_final_denial_or_existing_video_without_reply(tmp_path, monkeypatch, video, finished):
    import doubao_gen
    dp = _FakeDP([(0, DENIED, [])], vid_after=0 if video else None)
    original_fetch = dp.fetch_single_chain
    def fetch(*args):
        chain = original_fetch(*args)
        if not finished:
            chain['downlink_body']['pull_singe_chain_downlink_body']['messages'][-1]['content_block'][0]['is_finish'] = False
        return chain
    dp.fetch_single_chain = fetch
    state = tmp_path / 'cookie_state.json'
    state.write_text(json.dumps({'cookies': {'sessionid': {'value': 'test'}}}), encoding='utf-8')
    monkeypatch.setattr(doubao_gen, '_site', lambda: dp)
    monkeypatch.setattr(doubao_gen, '_session', lambda *args: (SimpleNamespace(close=lambda: None), {}, {}))
    result = doubao_gen.fetch_conversation_video(state, 'conv1', output_dir=tmp_path)
    assert result['final'] is (not video and finished)
    assert bool(result.get('plan_unavailable')) is (not video and finished)
    assert bool(result.get('local_path')) is video
    assert dp.replies == []


def test_recovery_service_ends_watch_and_pauses_plan_denied_account(tmp_path, monkeypatch):
    from test_auto_recover import FakePool, _server
    pool = FakePool([{'status': 'failed', 'final': True, 'plan_unavailable': True, 'detail': DENIED, 'local_path': ''}])
    paused = []
    pool._mark_abnormal = lambda *args: paused.append(args)
    server = _server(tmp_path, monkeypatch, pool)
    server.store.update('video_t1', model='doubao-seedance-2.0-fast', duration=15, recover_state='watching',
                        recover_until=time.time() + 600)
    _run(server._recover_loop('video_t1'))
    row = server.store.get('video_t1')
    assert row['recover_state'] == 'gave_up' and paused == [('acc1', DENIED)]
    assert not pool.claimed and '豆包' in row['error']


def test_failure_label_preserves_the_plan_denial():
    import failure_text
    assert failure_text.label_for(DENIED) == '套餐权限'
    assert DENIED in failure_text.summarize(DENIED)
