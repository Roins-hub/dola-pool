"""dola 追问的兜底答话：通用回复、AI 代答（安全检查、出错放弃）、轮询里的先后顺序、设置接口。"""
import asyncio
import re
from types import SimpleNamespace

import pytest

import dola_responder as r
from generation_errors import UpstreamReplyError
from test_task_deadline import _run
from test_start_confirm import _poll, _stall_run, UNKNOWN_Q


@pytest.fixture(autouse=True)
def reset_settings():
    r.apply_settings({})
    yield
    r.apply_settings({})


def test_generic_reply_restates_parameters_and_says_new():
    first, second = r.generic_reply('16:9', 10, 2, 0), r.generic_reply('16:9', 10, 0, 1)
    assert '16:9' in first and '10 秒' in first and '2 张参考图' in first and '新的视频' in first
    assert '新的生成请求' in second and '参考图' not in second
    assert r.generic_reply(None, None, 0, 0).endswith('提示词。')


ALREADY = 'この動画はすでに生成されています。別の内容で作成しますか？'   # 2026-11-01 15:51 原话


@pytest.mark.parametrize('text,hit', [
    (ALREADY, True), ('既に作成済みです', True), ('这个视频已经生成过了', True),
    ('This video has already been generated.', True),
    (UNKNOWN_Q, False), ('動画を生成しています', False), ('', False), (None, False),
])
def test_says_already_generated(text, hit):
    assert r.says_already_generated(text) is hit


def test_already_generated_gets_a_new_request_reply():
    first = r.generic_reply('16:9', 10, 0, 0, ALREADY)
    second = r.generic_reply('16:9', 10, 0, 1, ALREADY)
    assert '和之前生成过的视频无关' in first and '重新生成' in first and '16:9' in first
    assert '全新的视频' in second and second != first
    assert '无关' not in r.generic_reply('16:9', 10, 0, 0, UNKNOWN_Q)


def test_poll_answers_already_generated_with_the_new_request_wording():
    err, typed = _stall_run([_poll(ALREADY)] * 8, ratio='16:9', with_typed=True)
    replies = [t for t in typed if t != 'Enter']
    assert replies and '和之前生成过的视频无关' in replies[0]


@pytest.mark.parametrize('reply,ok', [
    ('B. 16:9，请直接生成 10 秒视频', True),
    ('好的，改成 9:16 竖幅', False),        # 改了比例
    ('请生成 15 秒', False),                # 改了时长
    ('GIVE_UP', False),
    ('', False),
    ('长' * 300, False),
])
def test_ai_reply_must_match_the_parameters(reply, ok):
    assert (r.check_reply(reply, '16:9', 10) is not None) is ok


class FakeResp:
    def __init__(self, status, data): self.status, self.data = status, data
    async def __aenter__(self): return self
    async def __aexit__(self, *a): pass
    async def json(self, content_type=None): return self.data


class FakeSession:
    def __init__(self, resp): self.resp, self.calls = resp, []
    def post(self, url, data=None, headers=None):
        self.calls.append((url, headers))
        return self.resp


def test_ai_reply_calls_an_openai_compatible_endpoint_and_checks_the_answer():
    r.apply_settings({'enabled': True, 'base_url': 'https://gw.example/v1', 'api_key': 'sk-abcdef123456', 'model': 'm'})
    good = FakeSession(FakeResp(200, {'choices': [{'message': {'content': '这是新请求，请按 16:9、10 秒重新生成一条。'}}]}))
    assert _run(r.ai_reply('すでに生成されています', '16:9', 10, 1, session=good)).startswith('这是新请求')
    assert good.calls[0][0] == 'https://gw.example/v1/chat/completions'
    assert good.calls[0][1]['Authorization'] == 'Bearer sk-abcdef123456'
    bad = FakeSession(FakeResp(200, {'choices': [{'message': {'content': '那就改成 9:16 吧'}}]}))
    assert _run(r.ai_reply('どちら？', '16:9', 10, 0, session=bad)) is None
    down = FakeSession(FakeResp(500, {}))
    assert _run(r.ai_reply('どちら？', '16:9', 10, 0, session=down)) is None


def test_ai_is_skipped_when_not_configured():
    assert not r.enabled()
    assert _run(r.ai_reply('どちら？', '16:9', 10)) is None
    r.apply_settings({'enabled': True, 'base_url': 'https://gw.example', 'api_key': '', 'model': 'm'})
    assert not r.enabled()


def test_poll_uses_generic_replies_first_then_ai(monkeypatch):
    calls = []
    async def fake_ai(text, ratio, duration, attachments, **kw):
        calls.append((text, ratio, duration))
        return 'AI：请按 16:9 直接生成'
    monkeypatch.setattr(r, 'ai_reply', fake_ai)
    monkeypatch.setattr(r, 'enabled', lambda: True)
    err, typed = _stall_run([_poll(UNKNOWN_Q, clarifying=True)] * 10, ratio='16:9', with_typed=True)
    assert len(typed) == 3 and typed[2] == 'AI：请按 16:9 直接生成'       # 两次通用 + 一次 AI
    assert calls == [(UNKNOWN_Q, '16:9', 10)]
    assert err.category == '上游在等待回复' and '已尝试 3 次' in str(err)


def test_settings_endpoint_masks_the_key_and_keeps_it(tmp_path, monkeypatch):
    import server
    from store import TaskStore
    monkeypatch.setattr(server, 'store', TaskStore(str(tmp_path / 'tasks.db')))
    monkeypatch.setattr(server, '_admin_auth', lambda key: None)
    saved = _run(server.admin_responder_settings_save(server.ResponderPatch(
        enabled=True, base_url='https://gw.example/v1', api_key='sk-secret-9876', model='m'), None))
    assert saved['ready'] and saved['api_key_hint'] == '••••9876' and 'api_key' not in saved
    again = _run(server.admin_responder_settings_save(server.ResponderPatch(model='m2'), None))
    assert again['api_key_set'] and r.SETTINGS['api_key'] == 'sk-secret-9876' and r.SETTINGS['model'] == 'm2'
    r.apply_settings({})
    assert server._load_responder()['api_key'] == 'sk-secret-9876'                # 重启后从库里读回
    with pytest.raises(server.HTTPException):
        _run(server.admin_responder_settings_save(server.ResponderPatch(base_url='gw.example'), None))


class FakeErrResp(FakeResp):
    async def text(self): return '{"error":{"message":"invalid api key"}}'


def test_ai_reply_detail_explains_failures_and_strips_thinking():
    s = r.normalize({'base_url': 'https://gw.example', 'api_key': 'sk-abcdef123456', 'model': 'm'})
    reply, why = _run(r.ai_reply_detail('どちら？', '16:9', 10, 0, settings=s, session=FakeSession(FakeErrResp(401, {}))))
    assert reply is None and 'HTTP 401' in why and 'invalid api key' in why
    think = FakeSession(FakeResp(200, {'choices': [{'message': {'content': '<think>想一想 9:16？</think>请按 16:9 直接生成。'}}]}))
    reply, why = _run(r.ai_reply_detail('どちら？', '16:9', 10, 0, settings=s, session=think))
    assert reply == '请按 16:9 直接生成。' and not why
    reply, why = _run(r.ai_reply_detail('どちら？', '16:9', 10, 0, settings=r.normalize({'base_url': 'https://x'})))
    assert reply is None and '没填全' in why


def test_test_endpoint_uses_the_unsaved_form_values(tmp_path, monkeypatch):
    # 2026-11-01：填完直接点「测试」报「未开启或配置不全」——测试读的是已保存的空设置
    import server
    from store import TaskStore
    monkeypatch.setattr(server, 'store', TaskStore(str(tmp_path / 'tasks.db')))
    monkeypatch.setattr(server, '_admin_auth', lambda key: None)
    seen = {}
    async def fake_detail(text, ratio, duration, attachments, *, settings=None, session=None):
        seen.update(settings)
        return '这是一条新的请求，请按 16:9 重新生成。', ''
    monkeypatch.setattr(server.dola_responder, 'ai_reply_detail', fake_detail)
    out = _run(server.admin_responder_test(server.ResponderPatch(
        enabled=False, base_url='https://gw.example/', api_key='sk-typed-1234', model='deepseek'), None))
    assert seen['base_url'] == 'https://gw.example' and seen['api_key'] == 'sk-typed-1234' and seen['model'] == 'deepseek'
    assert out['reply'] and out['saved'] is False
    assert not r.configured()                                   # 测试不保存
    with pytest.raises(server.HTTPException):
        _run(server.admin_responder_test(server.ResponderPatch(base_url='gw.example', api_key='k', model='m'), None))
