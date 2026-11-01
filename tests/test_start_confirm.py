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


# ---------------------------------------------------------------- 兜底：认不出的追问
UNKNOWN_Q = '生成の前に一つ教えてください：背景は夜と昼のどちらがよろしいでしょうか？'


def _stall_run(polls, *, after=None, seconds=180):
    """按 polls 依次返回；每次轮询时钟走 60 秒。polls 用完后连续抛网络错误让循环结束。"""
    clock = {'t': 1_000_000.0}
    typed = []

    class Keyboard:
        async def type(self, text, delay=0): typed.append(text)
        async def press(self, key): pass

    class Context:
        async def cookies(self, *a): return []

    class Page:
        keyboard = Keyboard()
        calls = 0
        async def evaluate(self, *a):
            self.calls += 1
            clock['t'] += 60
            if self.calls > len(polls):
                raise ConnectionError('end of script')
            return polls[self.calls - 1]
        async def query_selector(self, *a): raise AssertionError('must not answer an unknown question')
        async def wait_for_timeout(self, *a): pass

    async def no_wait(*a): pass
    import config
    old = config.STALL_REPLY_SECONDS
    config.STALL_REPLY_SECONDS = seconds
    try:
        fn = load_worker_function(
            'poll_conversation', cookie_value=lambda *a: '',
            asyncio=SimpleNamespace(sleep=no_wait, wait_for=asyncio.wait_for),
            time=SimpleNamespace(time=lambda: clock['t']),
            POLL_JS='', emit_feedback=lambda cb, **kw: None,
            DAILY_LIMIT_PATTERN=re.compile('x^'), FREE_LIMIT_REPLY=re.compile('x^'),
            _parse_balance_texts=lambda x: (None, None, ''))
        with pytest.raises(UpstreamReplyError) as exc:
            _run(fn('acc1', Page(), Context(), 'c1', 7200, duration=10, on_poll=lambda *a, **k: None))
    finally:
        config.STALL_REPLY_SECONDS = old
    assert not typed
    return exc.value


def _poll(text, *, finished=True, tools=(), any_tool=False, index='5', video_gen=False, any_video_gen=None,
          clarifying=False, attachments=0):
    return {'texts': [text], 'assistantTexts': [text], 'assistantCount': 1, 'anyTool': any_tool,
            'anyVideoGen': video_gen if any_video_gen is None else any_video_gen, 'userAttachments': attachments,
            'latestAssistant': {'id': 'm' + index, 'index': index, 'text': text, 'finished': finished,
                                'videoGen': video_gen, 'clarifying': clarifying,
                                'tools': [{'status': s, 'failCode': '0'} for s in tools]},
            'videos': []}


def test_unknown_question_ends_the_task_after_the_stall_window():
    err = _stall_run([_poll(UNKNOWN_Q)] * 5)
    assert err.category == '上游在等待回复' and '未扣点' in str(err) and UNKNOWN_Q in str(err)


@pytest.mark.parametrize('poll', [
    _poll('動画は**Dreamina Seedance 2.0高速モデル**を使用して、5分分後に生成されます。'),   # 报了预计时间 = 已受理
    _poll('動画が生成されました。', any_tool=True),                                         # 会话里挂过生成卡片
    _poll(UNKNOWN_Q, tools=(2,)),                                                          # 最新回复带生成中卡片
    _poll(UNKNOWN_Q, finished=False),                                                      # 还没说完
])
def test_accepted_or_running_generations_are_never_treated_as_stalled(poll):
    err = _stall_run([poll] * 8)                                  # 8 分钟，远超 3 分钟窗口
    assert err.category == '网络失败'                              # 只因脚本结束而停，不是判卡住


def test_stall_window_restarts_when_dola_says_something_new():
    polls = [_poll(UNKNOWN_Q)] * 2 + [_poll('もう一つ質問です：音楽は必要ですか？', index='6')] * 2
    err = _stall_run(polls)                                       # 每段都没满 3 分钟
    assert err.category == '网络失败'


def test_stall_watchdog_can_be_disabled():
    err = _stall_run([_poll(UNKNOWN_Q)] * 6, seconds=0)
    assert err.category == '网络失败'


# 2026-10-31 实测链（0ebb25d700 / a1077925c2）：受理消息已说完、没有卡片、has_video_gen=1，
# 卡片要等出片后在新消息里才出现。下面这条连预计时间都没说——只靠 has_video_gen 也绝不能判卡住。
def test_accepted_message_without_eta_is_protected_by_has_video_gen():
    err = _stall_run([_poll('動画の生成を開始しました。完了したらお送りします。', video_gen=True)] * 8)
    assert err.category == '网络失败'
    err = _stall_run([_poll('補足：背景を少し明るくします。', index='7', any_video_gen=True)] * 8)
    assert err.category == '网络失败'                 # 受理在前一条，最新一条是纯文字


def test_clarifying_flag_ends_the_task_within_two_polls():
    """e6e8 实测：追问消息 is_creation_clarifying=1，不必等 3 分钟。"""
    clock_polls = [_poll(UNKNOWN_Q, clarifying=True)] * 3
    err = _stall_run(clock_polls, seconds=180)
    assert err.category == '上游在等待回复' and '无法代为回答' in str(err)


def test_reuse_question_is_answered_with_a_when_we_uploaded_attachments():
    typed = []

    class Keyboard:
        async def type(self, text, delay=0): typed.append(text)
        async def press(self, key): typed.append(key)

    class Box:
        async def click(self): pass

    class Context:
        async def cookies(self, *a): return []

    question = _poll(MISSING_REF, clarifying=True, attachments=2, index='4')
    accepted = _poll('動画は5分以内に生成されます。', video_gen=True, index='6', attachments=2)

    class Page:
        keyboard = Keyboard()
        calls = 0
        async def evaluate(self, *a):
            self.calls += 1
            if self.calls > 4:
                raise ConnectionError('end')
            return question if self.calls == 1 else accepted
        async def query_selector(self, *a): return Box()
        async def wait_for_timeout(self, *a): pass

    async def no_wait(*a): pass
    progress = []
    fn = load_worker_function(
        'poll_conversation', cookie_value=lambda *a: '',
        asyncio=SimpleNamespace(sleep=no_wait, wait_for=asyncio.wait_for),
        POLL_JS='', emit_feedback=lambda cb, **kw: progress.append(kw.get('progress')),
        DAILY_LIMIT_PATTERN=re.compile('x^'), FREE_LIMIT_REPLY=re.compile('x^'),
        _parse_balance_texts=lambda x: (None, None, ''))
    with pytest.raises(UpstreamReplyError, match='网络失败'):          # 回了 A 之后正常等出片，直到脚本结束
        _run(fn('acc141', Page(), Context(), 'c1', 1800, duration=10, on_poll=lambda *a, **k: None))
    assert typed == ['A', 'Enter']
    assert any(p and '已回复「A」' in p for p in progress)


RATIO_Q = ('この動画生成リクエストには複数のアスペクト比が記載されています。どちらで生成しますか？ '
           'A. 9:16 縦幅 B. 16:9 横幅 C. その他（指定してください）')
PROMPT_916 = '8K写实古风写真，9:16竖幅，蝶梦主题。女子穿淡粉青纱唐风齐胸襦裙'


def test_ratio_question_follows_the_client_parameter():
    """2026-11-01：参数 16:9、提示词 9:16。以客户端参数为准；参数没给有效比例才看提示词。"""
    from upstream_reply import ratio_choice_reply, prompt_ratio
    assert ratio_choice_reply(RATIO_Q, PROMPT_916, '16:9') == 'B. 16:9'
    assert ratio_choice_reply(RATIO_Q, PROMPT_916, None) == 'A. 9:16'           # 没有参数：用提示词
    assert ratio_choice_reply(RATIO_Q, PROMPT_916, 'default') == 'A. 9:16'
    assert ratio_choice_reply(RATIO_Q, '海边日落', None) is None                  # 无从判断：不乱答
    assert ratio_choice_reply(RATIO_Q, '1:1 方图', '1:1') is None                 # 选项里没有：不答
    assert ratio_choice_reply('動画が生成されました。', PROMPT_916, '16:9') is None
    assert prompt_ratio('竖屏人像') == '9:16' and prompt_ratio('竖屏 16:9') is None
    assert prompt_ratio('12:55 开拍的海边') is None


def test_browser_poll_answers_the_ratio_question_and_keeps_waiting():
    typed = []

    class Keyboard:
        async def type(self, text, delay=0): typed.append(text)
        async def press(self, key): typed.append(key)

    class Box:
        async def click(self): pass

    class Context:
        async def cookies(self, *a): return []

    question = _poll(RATIO_Q, clarifying=True, index='4')
    accepted = _poll('動画は5分以内に生成されます。', video_gen=True, index='6')

    class Page:
        keyboard = Keyboard()
        calls = 0
        async def evaluate(self, *a):
            self.calls += 1
            if self.calls > 4:
                raise ConnectionError('end')
            return question if self.calls == 1 else accepted
        async def query_selector(self, *a): return Box()
        async def wait_for_timeout(self, *a): pass

    async def no_wait(*a): pass
    fn = load_worker_function(
        'poll_conversation', cookie_value=lambda *a: '',
        asyncio=SimpleNamespace(sleep=no_wait, wait_for=asyncio.wait_for),
        POLL_JS='', emit_feedback=lambda cb, **kw: None,
        DAILY_LIMIT_PATTERN=re.compile('x^'), FREE_LIMIT_REPLY=re.compile('x^'),
        _parse_balance_texts=lambda x: (None, None, ''))
    with pytest.raises(UpstreamReplyError, match='网络失败'):       # 答完后正常等出片，直到脚本结束
        _run(fn('acc13', Page(), Context(), 'c1', 1800, duration=10, ratio='16:9', prompt=PROMPT_916,
                on_poll=lambda *a, **k: None))
    assert typed == ['B. 16:9', 'Enter']


def test_ratio_note_makes_the_parameter_win_only_on_conflict():
    """提示词写的比例和参数冲突时，在发给 dola 的文字末尾注明以参数为准；客户原文不改。"""
    from prompt_context import ratio_note, dispatch_prompt
    note = ratio_note(PROMPT_916, '16:9')
    assert note == '【画面比例】以 16:9（横幅）为准，忽略上文中的其他比例描述。'
    assert ratio_note('竖屏人像', '16:9') and ratio_note('9:16 竖屏', '9:16') == ''
    assert ratio_note('海边日落', '16:9') == '' and ratio_note('16:9 横幅海景', '16:9') == ''
    assert ratio_note(PROMPT_916, None) == '' and ratio_note(PROMPT_916, 'default') == ''
    sent = dispatch_prompt(PROMPT_916, '素材说明', '16:9')
    assert sent.startswith('素材说明\n\n' + PROMPT_916) and sent.endswith(note)
    assert dispatch_prompt('海边日落', '', '16:9') == '海边日落'


def test_ratio_not_listed_uses_the_other_option():
    """参数 21:9、提示词 9:16，dola 只列 9:16 / 16:9 / その他：选「その他」并写明 21:9。"""
    from upstream_reply import ratio_choice_reply
    listed = RATIO_Q.replace('B. 16:9 横幅', 'B. 21:9 横幅')
    assert ratio_choice_reply(listed, PROMPT_916, '21:9') == 'B. 21:9'
    assert ratio_choice_reply(RATIO_Q, PROMPT_916, '21:9') == 'C. 21:9'
    no_other = 'どちらで生成しますか？ A. 9:16 縦幅 B. 16:9 横幅'
    assert ratio_choice_reply(no_other, PROMPT_916, '21:9') is None
