"""dola 问「按 N 秒直接开始生成吗？」：识别、只确认请求时长，浏览器 / 纯协议两条路径都会回话。"""
import asyncio
import re
from types import SimpleNamespace

import pytest

import pure_api_gen
from generation_errors import UpstreamReplyError
from test_task_deadline import _run
from test_workflow_hardening import load_worker_function
from upstream_reply import duration_reply, start_confirm_reply

JA = ('この動画は 10 秒を指定されていますが、現在の動画生成は 4～15 秒に対応しています。'
      '直接生成を開始してよろしいですか？')


def test_confirms_the_requested_duration_in_the_reply_language():
    assert duration_reply(JA, 10) is None                       # 旧规则认不出，正是线上卡住的原因
    assert start_confirm_reply(JA, 10) == 'はい、10秒で生成を開始してください。'
    assert start_confirm_reply('视频支持 4-15 秒，你指定的是 10 秒，是否直接开始生成？', 10) == '确认，直接按10秒开始生成。'
    assert start_confirm_reply('You asked for 5 seconds (supported: 4-15 seconds). Do you want me to proceed?', 5) \
        == 'Yes, please start generating the 5-second video now.'


@pytest.mark.parametrize('text,duration', [
    (JA, 5),                                                    # dola 说的不是我们请求的时长
    (JA, 30),                                                   # 30 秒走拆段逻辑，不在这里答应
    ('この動画は 10 秒を指定されていますが、15 秒で生成してもよろしいですか？', 10),   # 提议改时长
    ('I can generate it at the nearest supported duration of 15 seconds. '
     'Do you want me to proceed with a 15-second version?', 10),
    ('動画を生成します。10 秒、クレジットを 2 消費します。', 10),          # 已受理，不是提问
    ('実在する人物の肖像保護のため対応していません。10 秒で生成してよろしいですか？', 10),  # 拒绝优先
    ('', 10),
])
def test_does_not_answer_anything_else(text, duration):
    assert start_confirm_reply(text, duration) is None


def test_browser_poll_types_the_confirmation_once():
    typed, progress = [], []

    class Keyboard:
        async def type(self, text, delay=0): typed.append(text)
        async def press(self, key): typed.append(key)

    class Box:
        async def click(self): pass

    class Context:
        async def cookies(self, *a): return []

    class Page:
        keyboard = Keyboard()
        calls = 0
        async def evaluate(self, *a):
            self.calls += 1
            if self.calls > 2:
                raise ConnectionError('stop the loop')
            return {'texts': [JA], 'assistantTexts': [JA], 'assistantCount': 1,
                    'latestAssistant': {'id': 'q', 'text': JA, 'finished': True}, 'videos': []}
        async def query_selector(self, *a): return Box()
        async def wait_for_timeout(self, *a): pass

    async def no_wait(*a): pass
    fn = load_worker_function(
        'poll_conversation', cookie_value=lambda *a: '',
        asyncio=SimpleNamespace(sleep=no_wait, wait_for=asyncio.wait_for),
        POLL_JS='', emit_feedback=lambda cb, **kw: progress.append(kw.get('progress')),
        DAILY_LIMIT_PATTERN=re.compile('x^'), FREE_LIMIT_REPLY=re.compile('x^'),
        _parse_balance_texts=lambda x: (None, None, ''))
    with pytest.raises(UpstreamReplyError):
        _run(fn('acc1', Page(), Context(), 'c1', 600, duration=10, on_poll=lambda *a, **k: None))
    assert typed == ['はい、10秒で生成を開始してください。', 'Enter']
    assert any(p and '已回复确认' in p for p in progress)


def test_pure_path_picks_the_confirmation_but_not_after_acceptance():
    dola = SimpleNamespace(is_prompt_echo_text=lambda t: t.startswith('PROMPT'),
                           looks_like_generation_accepted=lambda t: 'クレジット' in t)
    assert pure_api_gen._start_confirm_text(dola, ['PROMPT 10 秒', JA], 10) == 'はい、10秒で生成を開始してください。'
    assert pure_api_gen._start_confirm_text(dola, [JA, '生成を開始しました。クレジットを 2 消費'], 10) is None
    assert pure_api_gen._start_confirm_text(dola, ['PROMPT only'], 10) is None


MISSING_REF = ('只ひとつ確認させてください：現在のところ動画生成は4秒から15秒まで対応しており、10秒は有効です。'
               'ただし、リクエストに「図1」「図2」と記載されていますが、今回のラウンドでは参照画像が提供されていません。'
               '正しく生成するにはどちらかを選んでください： A. 前回アップロードした画像2点を使用する '
               'B. 今回のプロンプトからゼロで10秒動画を生成する A を選ぶ場合は「A」とだけ返信してください。'
               '確認後、10秒・16:9で直接生成します。')


def test_missing_reference_question_fails_fast_without_answering():
    """2026-10-31 线上：dola 说这一轮没收到参照图、让选 A/B；之前不认识，任务空等 30 分钟超时。"""
    from upstream_reply import reply_failure
    assert reply_failure(MISSING_REF) == '需要补充信息'
    assert start_confirm_reply(MISSING_REF, 10) is None and duration_reply(MISSING_REF, 10) is None
    # 只是顺带说明、照常生成的不算
    assert reply_failure('参照画像が提供されていませんので、テキストから動画を生成します。') is None

    class Context:
        async def cookies(self, *a): return []

    class Page:
        calls = 0
        async def evaluate(self, *a):
            self.calls += 1
            assert self.calls <= 2                     # 两次确认就结束，不等到超时
            return {'texts': [MISSING_REF], 'assistantTexts': [MISSING_REF], 'assistantCount': 1,
                    'latestAssistant': {'id': 'q', 'text': MISSING_REF, 'finished': True}, 'videos': []}
        async def query_selector(self, *a): raise AssertionError('must not answer A/B for the customer')

    async def no_wait(*a): pass
    fn = load_worker_function(
        'poll_conversation', cookie_value=lambda *a: '',
        asyncio=SimpleNamespace(sleep=no_wait, wait_for=asyncio.wait_for),
        POLL_JS='', emit_feedback=lambda cb, **kw: None,
        DAILY_LIMIT_PATTERN=re.compile('x^'), FREE_LIMIT_REPLY=re.compile('x^'),
        _parse_balance_texts=lambda x: (None, None, ''))
    with pytest.raises(UpstreamReplyError, match='需要补充信息'):
        _run(fn('acc141', Page(), Context(), 'c1', 1800, duration=10, on_poll=lambda *a, **k: None))
