import asyncio
import json
import re
import time
from types import SimpleNamespace

import pytest

from generation_errors import UpstreamReplyError, UploadRouteError
from upstream_reply import duration_reply, reply_failure, QUOTA_RE
from test_task_deadline import _pool, _run
from test_task_ui_regressions import load_worker_function


@pytest.mark.parametrize('text,category', [
    ('動画を生成できません。今回の生成でクレジットは使用されませんでした。', '上游生成失败'),
    ('肖像保護のため、Dreamina Seedance 2.0は実在する人物の画像からの動画生成には対応していません。', '内容拒绝'),
    ('出于肖像保护，请换其他图片', '内容拒绝'),
    ('还缺少核心内容。请选择一个方向 A. 输入文字 B. 上传图片', '需要补充信息'),
    ('insufficient credits', '额度不足'),
    ('network error', '网络失败'),
    ('视频正在生成，预计 3 分钟', None),
    ('内容审核已通过，正在生成', None),
    ('使用 copyright-free music，正在生成', None),
    ('エラーが発生しました。もう一度お試しください。', '上游生成失败'),
])
def test_failure_categories(text, category):
    assert reply_failure(text) == category
    if category != '额度不足': assert not QUOTA_RE.search(text)


@pytest.mark.parametrize('text', [
    '请选择一个方向：A. 输入文字 B. 上传图片',
    '別の画像を使用するか、テキストから動画を作成してください。',
    '動画生成には参考画像が必要です。',
    '我将生成 5 秒的视频，请稍候。',
    '肖像保護のため生成できません。秒数を指定してください。',
])
def test_no_unsolicited_duration_reply(text):
    assert duration_reply(text, 5, '16:9') is None


def test_duration_question_requires_matching_option():
    text = '秒数を選んでください：A. 5 秒で生成 B. 10 秒で生成'
    assert duration_reply(text, 10) == 'B. 10秒'
    assert duration_reply(text, 15) is None
    assert duration_reply('Please select a duration', 5, '4:3') == '4:3、5秒'


def test_latest_refusal_beats_old_acceptance_and_never_types():
    from browser_feedback import emit
    latest = '肖像保護のため生成できません。'
    notes = []
    class Context:
        async def cookies(self, *a): return []
    class Page:
        calls = 0
        async def evaluate(self, *a):
            self.calls += 1
            assert self.calls <= 2
            return {'texts': ['動画を生成します', latest],
                    'assistantTexts': ['動画を生成します', latest],
                    'latestAssistant': {'id': 'last', 'text': latest}, 'videos': []}
        async def query_selector(self, *a): raise AssertionError('Must not auto reply')
    async def no_wait(*a): pass
    fn = load_worker_function('poll_conversation', cookie_value=lambda *a: '',
        asyncio=SimpleNamespace(sleep=no_wait, wait_for=asyncio.wait_for),
        POLL_JS='', emit_feedback=emit, DAILY_LIMIT_PATTERN=re.compile('daily quota'), FREE_LIMIT_REPLY=re.compile('x^'),
        _parse_balance_texts=lambda x: (None, None, ''))
    with pytest.raises(UpstreamReplyError, match='肖像保護'):
        _run(fn('acc1', Page(), Context(), 'c1', 60, duration=5,
                on_poll=lambda now, note=None, **kw: notes.append(note) if note else None))
    assert notes == [latest]


def test_explicit_refusal_does_not_rotate_or_charge(tmp_path, monkeypatch):
    _, bp = _pool(tmp_path, monkeypatch)
    calls, history = [], []
    async def generate(account, *a, **kw):
        calls.append(account)
        raise UpstreamReplyError('内容拒绝', '肖像保護')
    bp._generate_effective = generate
    with pytest.raises(UpstreamReplyError):
        _run(bp.generate_video('p', None, 5, 'seedance-2.5',
                               on_attempt_end=lambda *args: history.append(args)))
    assert len(calls) == len(history) == 1
    assert bp.used_today(calls[0]) == 0
    assert '肖像保護' in history[0][-1]
    assert not bp._meta(calls[0])['quota_blocked_until']


def test_upload_network_failure_skips_shared_route(tmp_path, monkeypatch):
    module, bp = _pool(tmp_path, monkeypatch, ('acc1', 'acc2', 'acc3'))
    bp._safe_egress = lambda name: 'shared' if name in ('acc1', 'acc2') else 'other'
    calls, probes = [], []
    def diagnose(account, host):
        probes.append(account)
        return {'reachable': False, 'detail': 'SOCKS connection failed'}
    monkeypatch.setattr(module, 'diagnose_upload_route', diagnose)
    async def generate(account, *a, **kw):
        calls.append(account)
        if account != 'acc3':
            raise UploadRouteError('imagex-ap-southeast-1.bytevcloudapi.com', 'net::ERR_EMPTY_RESPONSE')
        return {'account': account, 'local_path': 'fake.mp4'}
    bp._generate_effective = generate
    result = _run(bp.generate_video('p', None, 5, 'seedance-2.5',
                                    reference_image_paths=['fake.png'], deadline=time.time()+30))
    assert calls == ['acc1', 'acc3'] and probes == ['acc1']
    assert result['account'] == 'acc3'
    assert not bp._meta('acc1')['risk_control']


def test_verification_is_busy_but_not_generation_and_cancellation_cleans_state(tmp_path, monkeypatch):
    _, bp = _pool(tmp_path, monkeypatch)
    async def verify(name):
        await asyncio.Event().wait()
    bp._verify_account_detail_locked = verify
    async def scenario():
        task = asyncio.create_task(bp.verify_account_detail('acc1'))
        await asyncio.sleep(0)
        row = next(a for a in bp.list_accounts() if a['name'] == 'acc1')
        assert row['busy'] and row['operation'] == 'verifying'
        assert row['group'] != '生成中'
        with pytest.raises(RuntimeError): await bp.verify_account_detail('acc1')
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        row = next(a for a in bp.list_accounts() if a['name'] == 'acc1')
        assert not row['busy'] and not row['operation']
    _run(scenario())


def test_slot_queue_does_not_spend_attempt_budget(tmp_path, monkeypatch):
    _, bp = _pool(tmp_path, monkeypatch)
    calls = []
    async def generate(account, *a, **kw):
        calls.append(account)
        return {'account': account, 'local_path': 'fake.mp4'}
    bp._generate_effective = generate
    async def scenario():
        await bp.semaphore.acquire()
        task = asyncio.create_task(bp.generate_video('p', None, 5, 'seedance-2.5', attempt_timeout=.03))
        await asyncio.sleep(.06)
        assert not calls
        bp.semaphore.release()
        await asyncio.wait_for(task, 2)
        assert len(calls) == 1
    _run(scenario())


def test_browser_preparation_timeout_stops_without_resubmission(tmp_path, monkeypatch):
    module, bp = _pool(tmp_path, monkeypatch)
    monkeypatch.setattr(module.config, 'PREPARE_TIMEOUT', .02)
    calls=[]
    async def generate(account, *a, **kw):
        calls.append(account)
        await asyncio.sleep(10)
    bp._generate_effective=generate
    with pytest.raises(TimeoutError, match='提交准备超时'):
        _run(bp.generate_video('p',None,5,'seedance-2.5',attempt_timeout=1))
    assert len(calls)==1 and not bp._activities


def test_acknowledgement_stops_preparation_clock(tmp_path, monkeypatch):
    module,bp=_pool(tmp_path,monkeypatch)
    monkeypatch.setattr(module.config,'PREPARE_TIMEOUT',.02)
    async def generate(account,*a,**kw):
        kw['on_conversation_id'](account,'existing',time.time()+60)
        await asyncio.sleep(.04)
        return {'account':account,'local_path':'fake.mp4'}
    bp._generate_effective=generate
    assert _run(bp.generate_video('p',None,5,'seedance-2.5',attempt_timeout=1))['account']


def test_attempt_records_keep_failure_after_next_progress(tmp_path, monkeypatch):
    import server
    from store import TaskStore
    monkeypatch.setattr(server, 'store', TaskStore(str(tmp_path/'tasks.db')))
    monkeypatch.setattr(server, '_maybe_start_recovery', lambda *a: False)
    class Pool:
        async def generate_video(self, *a, **kw):
            kw['on_dispatch']()
            kw['on_account_try']('acc1', 1)
            kw['on_poll'](time.time(), progress='正在上传参考图')
            kw['on_attempt_end']('acc1', 1, time.time(), '上传网络失败 https://invalid/?token=SECRET')
            kw['on_account_try']('acc2', 2)
            kw['on_poll'](time.time(), progress='正在检查登录态')
            raise UpstreamReplyError('需要补充信息', '请提供主题')
    monkeypatch.setattr(server, 'pool', Pool())
    server.store.create('history', 'seedance-2.0-fast', 'p', '16:9', 5)
    _run(server._run_task('history', 'seedance-2.0-fast', 'p', '16:9', 5, [], {}))
    rows = json.loads(server.store.get('history')['attempt_history'])
    assert len(rows) == 2
    assert rows[0]['stage'] == '正在上传参考图' and '网络失败' in rows[0]['error']
    assert 'SECRET' not in rows[0]['error']
    assert rows[1]['stage'] == '正在检查登录态' and rows[1]['outcome'] == 'failed'


def test_strict_missing_proxy_never_direct(monkeypatch):
    import account_transport as transport
    monkeypatch.setattr(transport.config, 'ISOLATE_SHARED_EGRESS', True)
    monkeypatch.setattr(transport.proxy_store, 'get_account_proxy', lambda a: None)
    with pytest.raises(RuntimeError, match='禁止直连'):
        transport.http_proxy('acc1')


def test_socks_media_transport_uses_relay(monkeypatch):
    import account_transport as transport
    monkeypatch.setattr(transport.proxy_store, 'get_account_proxy', lambda a: {'mode':'static'})
    monkeypatch.setattr(transport.proxy_store, 'account_proxy_url', lambda a: 'socks5://private:secret@proxy:1080')
    monkeypatch.setattr(transport.socks_relay, 'local_http_proxy_for', lambda url: 'http://127.0.0.1:1234')
    assert transport.http_proxy('acc1') == 'http://127.0.0.1:1234'


def _high_bitrate_fn(session_cls):
    import json
    async def no_wait(*a): pass
    return load_worker_function('fetch_higher_bitrate_url', json=json,
        urlsplit=__import__('urllib.parse',fromlist=['urlsplit']).urlsplit,
        urlunsplit=__import__('urllib.parse',fromlist=['urlunsplit']).urlunsplit,
        urlencode=__import__('urllib.parse',fromlist=['urlencode']).urlencode,
        config=SimpleNamespace(HIGH_BITRATE_ATTEMPTS=3, PROXY=''),
        asyncio=SimpleNamespace(sleep=no_wait),
        _best_decoded_source=lambda data, seed: data.get('best'),
        aiohttp=SimpleNamespace(ClientSession=session_cls,ClientTimeout=lambda **kw: None))


def test_high_bitrate_failure_retries_same_proxy_never_direct(monkeypatch, capsys):
    import json
    import account_transport
    calls=[]
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def get(self, url, **kw):
            calls.append(kw['proxy'])
            raise RuntimeError('proxy failed')
    monkeypatch.setattr(account_transport, 'http_proxy', lambda a: 'http://bound-proxy')
    fn=_high_bitrate_fn(Session)
    assert _run(fn(json.dumps({'fallback_api':'https://example.invalid/?token=secret'}),'acc1')) is None
    assert calls == ['http://bound-proxy'] * 3          # 同一出口重试，绝不直连
    out = capsys.readouterr().out
    assert '高码率原片获取失败' in out and 'RuntimeError' in out and 'secret' not in out


def test_high_bitrate_second_attempt_succeeds(monkeypatch, capsys):
    import json
    import account_transport
    class Resp:
        def __init__(self, status): self.status = status
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def text(self): return json.dumps({'best': 'https://cdn.invalid/h264.mp4'})
    statuses = iter([502, 200])
    class Session:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        def get(self, url, **kw): return Resp(next(statuses))
    monkeypatch.setattr(account_transport, 'http_proxy', lambda a: 'http://bound-proxy')
    fn=_high_bitrate_fn(Session)
    assert _run(fn(json.dumps({'fallback_api':'https://example.invalid/?token=secret'}),'acc1')) == 'https://cdn.invalid/h264.mp4'
    assert 'HTTP 502' in capsys.readouterr().out


def test_http_poll_failures_do_not_wait_until_generation_timeout():
    from browser_feedback import emit
    class Context:
        async def cookies(self,*a): return []
    class Page:
        calls=0
        async def evaluate(self,*a):
            self.calls+=1
            assert self.calls<=3
            return {'ok':False,'status':502}
    async def no_wait(*a): pass
    fn=load_worker_function('poll_conversation',cookie_value=lambda *a:'',
        asyncio=SimpleNamespace(sleep=no_wait,wait_for=asyncio.wait_for),POLL_JS='',emit_feedback=emit)
    with pytest.raises(UpstreamReplyError,match='网络失败'):
        _run(fn('acc1',Page(),Context(),'c1',1800))


def test_failed_download_removes_partial_file_and_never_resubmits(tmp_path, monkeypatch):
    import ast
    from pathlib import Path
    import account_transport
    monkeypatch.setattr(account_transport,'http_proxy',lambda a:'http://bound-proxy')
    class Response:
        content=None
        async def __aenter__(self): return self
        async def __aexit__(self,*a): pass
        def raise_for_status(self): pass
        async def iter_chunked(self,*a):
            yield b'partial'
            raise RuntimeError('https://cdn.invalid/?token=PRIVATE')
    response=Response();response.content=response
    class Session:
        def __init__(self,**kw): pass
        async def __aenter__(self):return self
        async def __aexit__(self,*a):pass
        def get(self,url,**kw):
            assert kw['proxy']=='http://bound-proxy'
            return response
    root=Path(__file__).resolve().parents[1]
    node=next(n for n in ast.parse((root/'video_worker.py').read_text(encoding='utf8')).body
              if isinstance(n,ast.AsyncFunctionDef) and n.name=='_download')
    scope={'Path':Path,'asyncio':asyncio,'time':time,'config':SimpleNamespace(DOWNLOAD_DIR=str(tmp_path)),
           'aiohttp':SimpleNamespace(ClientSession=Session,ClientTimeout=lambda **k:None)}
    exec(compile(ast.Module(body=[node],type_ignores=[]),'download-test','exec'),scope)
    with pytest.raises(UpstreamReplyError) as error:
        _run(scope['_download']('https://cdn.invalid/?token=PRIVATE','acc1'))
    assert 'PRIVATE' not in str(error.value)
    assert list(tmp_path.iterdir())==[]


def test_relogin_keeps_account_locked_and_shows_verifying(tmp_path,monkeypatch):
    import server
    _,bp=_pool(tmp_path,monkeypatch)
    monkeypatch.setattr(server,'pool',bp)
    monkeypatch.setattr(server,'ADD_ACCOUNT_SEM',asyncio.Semaphore(1))
    monkeypatch.setattr(server.cred_store,'load',lambda name:{'email':'test@example.invalid','password':'synthetic'})
    checks=[]
    async def check(name):
        checks.append(name)
        assert bp._locks[name].locked()
        return (len(checks)>1,'')
    async def login(name,*a,**kw):
        row=next(r for r in bp.list_accounts() if r['name']==name)
        assert row['operation']=='verifying' and row['busy']
        assert row['group']!='生成中'
    monkeypatch.setattr(bp,'_verify_account_detail_locked',check)
    monkeypatch.setattr(server,'add_account_flow',login)
    result=_run(server._verify_account_once('acc1'))
    assert result['ok'] and result['refreshed'] and len(checks)==2
    assert not bp._locks['acc1'].locked()
