"""Confirmed upstream generation failures (2026-10-29 chain dumps) and safe account switching."""
import asyncio
import json
import re
import time
from types import SimpleNamespace

import pytest

from generation_errors import UpstreamGenerationFailed, UpstreamReplyError
from upstream_reply import final_generation_failure
from test_browser_poll import poll_messages, text_block
from test_task_deadline import _pool, _run
from test_task_ui_regressions import ROOT, load_worker_function


def load_worker_def(name, **bindings):
    """Load one (sync or async) top-level function from the real worker source.

    Other test modules replace sys.modules['video_worker_ui'] with stubs, so never import it here.
    """
    import ast
    tree = ast.parse((ROOT / 'video_worker_ui.py').read_text(encoding='utf-8'))
    fn = next(n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    scope = {'asyncio': asyncio, 'time': time, **bindings}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), 'worker-test', 'exec'), scope)
    return scope[name]


def _region_check():
    from generation_errors import RegionRestrictedError
    return load_worker_def('raise_if_region_restricted', RegionRestrictedError=RegionRestrictedError,
                           REGION_RESTRICTED_PATH='/security/region-restricted')

FAILED_TEXT = '動画を生成できません。今回の生成でクレジットは使用されませんでした。'
ACCEPTED_TEXT = '動画は**Dreamina Seedance 2.0高速モデル**を使用して、5分分後に生成されます。'


def failed_card(index=7, code='710082023', status=5, text=FAILED_TEXT, finished='1'):
    """Shape of the real failed card: brief [カード], tool status 5, fail_code set, no video."""
    return {'user_type': 2, 'index_in_conv': str(index), 'brief': '[カード]',
            'content_block': [text_block(text)],
            'ext': {'is_finish': finished, 'ai_creation_res_code': code,
                    'ai_creation_tool_list': json.dumps([{'status': status, 'fail_code': int(code)}])}}


def accepted_card(index=4):
    return {'user_type': 2, 'index_in_conv': str(index), 'content_block': [text_block(ACCEPTED_TEXT)],
            'ext': {'is_finish': '1', 'has_video_gen': '1'}}


def test_poll_exposes_structured_creation_outcome():
    result = poll_messages([failed_card(), accepted_card(),
                            {'user_type': 1, 'index_in_conv': '2', 'content_block': [text_block('生成された動画：prompt')]}])
    latest = result['latestAssistant']
    assert latest['index'] == '7' and latest['finished'] is True
    assert latest['resCode'] == '710082023'
    assert latest['tools'] == [{'status': 5, 'failCode': '710082023'}]
    assert final_generation_failure(latest, bool(result['videos']))['retryable'] is True


@pytest.mark.parametrize('latest,has_video', [
    # Success card: status 4 with a video.
    ({'finished': True, 'resCode': '0', 'tools': [{'status': 4, 'failCode': '0'}], 'text': '動画が生成されました。'}, True),
    # Text alone is never final (SSE/intermediate wording has preceded real videos).
    ({'finished': True, 'resCode': '', 'tools': [], 'text': FAILED_TEXT}, False),
    # Portrait refusal: res code but no executed creation task.
    ({'finished': True, 'resCode': '710082031', 'tools': [], 'text': '肖像保護のため…'}, False),
    # Not finished yet.
    ({'finished': False, 'resCode': '710082023', 'tools': [{'status': 5, 'failCode': '710082023'}], 'text': FAILED_TEXT}, False),
    # Any running/successful tool keeps the conversation alive.
    ({'finished': True, 'resCode': '710082023', 'tools': [{'status': 5, 'failCode': '1'}, {'status': 2, 'failCode': '0'}], 'text': ''}, False),
    # A video anywhere in the conversation wins.
    ({'finished': True, 'resCode': '710082023', 'tools': [{'status': 5, 'failCode': '710082023'}], 'text': FAILED_TEXT}, True),
])
def test_only_finished_failed_card_is_final(latest, has_video):
    assert final_generation_failure(latest, has_video) is None


def test_content_refusal_card_is_final_but_not_retryable():
    latest = {'finished': True, 'resCode': '710082031', 'tools': [{'status': 5, 'failCode': '710082031'}],
              'text': '肖像保護のため、実在する人物の画像からの動画生成には対応していません。'}
    final = final_generation_failure(latest, False)
    assert final and final['retryable'] is False and final['category'] == '内容拒绝'


def _poll_fn(responses, downloads=None):
    from browser_feedback import emit
    class Context:
        async def cookies(self, *a): return []
    class Page:
        calls = 0
        async def evaluate(self, *a):
            self.calls += 1
            return responses[min(self.calls, len(responses)) - 1]
        async def query_selector(self, *a): raise AssertionError('must not type replies')
    async def no_wait(*a): pass
    class Local:
        def stat(self): return SimpleNamespace(st_size=1)
        def __str__(self): return 'v.mp4'
    async def download(url, account):
        downloads.append(url)
        return Local()
    async def no_hi(*a): return None
    fn = load_worker_function('poll_conversation', cookie_value=lambda *a: '',
        asyncio=SimpleNamespace(sleep=no_wait, wait_for=asyncio.wait_for), POLL_JS='',
        emit_feedback=emit, DAILY_LIMIT_PATTERN=re.compile('動画生成の\\s*1日あたりの上限'),
        _parse_balance_texts=lambda x: (None, None, ''), fetch_higher_bitrate_url=no_hi,
        extract_unwatermarked_url=lambda vm, url: url, _download=download, CreditError=RuntimeError,
        AccountLimitedError=RuntimeError)
    page = Page()
    page.cookies = Context().cookies
    return fn, page


def _latest(**kw):
    base = {'id': 'm7', 'index': '7', 'text': FAILED_TEXT, 'finished': True, 'resCode': '710082023',
            'tools': [{'status': 5, 'failCode': '710082023'}]}
    base.update(kw)
    return {'latestAssistant': base, 'videos': [], 'texts': [base['text']], 'assistantTexts': [base['text']]}


def test_confirmed_failure_needs_two_identical_polls_and_names_conversation():
    fn, page = _poll_fn([_latest(), _latest()])
    with pytest.raises(UpstreamGenerationFailed) as error:
        _run(fn('acc27', page, page, 'c-38418236963968785', 1800, duration=10))
    assert page.calls == 2
    e = error.value
    assert e.retryable and e.not_charged and e.code == '710082023'
    assert 'c-38418236963968785' in str(e) and '上游生成失败' in str(e)


def test_interim_failure_or_quota_text_then_video_is_downloaded_not_failed():
    """2026-10-28 real counterexample: quota wording mid-poll, then a video arrived."""
    interim = {'latestAssistant': {'id': 'm4', 'index': '4', 'finished': True, 'resCode': '', 'tools': [],
                                   'text': '動画生成の無料利用分（1日あたり）の上限に到達しました。'},
               'videos': [], 'texts': ['x'], 'assistantTexts': ['x']}
    done = {'latestAssistant': {'id': 'm7', 'index': '7', 'finished': True, 'resCode': '0',
                                'tools': [{'status': 4, 'failCode': '0'}], 'text': '動画が生成されました。'},
            'videos': ['https://cdn.invalid/v.mp4'], 'videoModels': [''], 'texts': [], 'assistantTexts': []}
    downloads = []
    fn, page = _poll_fn([interim, done], downloads)
    result = _run(fn('acc148', page, page, 'c1', 1800, duration=10))
    assert downloads == ['https://cdn.invalid/v.mp4'] and result['conversation_id'] == 'c1'


def test_failed_card_changing_between_polls_is_not_confirmed_yet():
    downloads = []
    video = {'latestAssistant': {'id': 'm8', 'index': '8', 'finished': True, 'resCode': '0',
                                 'tools': [{'status': 4, 'failCode': '0'}], 'text': ''},
             'videos': ['https://cdn.invalid/v.mp4'], 'videoModels': [''], 'texts': [], 'assistantTexts': []}
    fn, page = _poll_fn([_latest(), video], downloads)
    _run(fn('acc1', page, page, 'c1', 1800))
    assert downloads


def _failed(account, retryable=True):
    return UpstreamGenerationFailed('上游生成失败', FAILED_TEXT, code='710082023',
                                    conversation_id=f'conv-{account}', retryable=retryable, not_charged=True)


def test_confirmed_failure_switches_account_without_charging(tmp_path, monkeypatch):
    module, bp = _pool(tmp_path, monkeypatch, ('acc1', 'acc2', 'acc3'))
    monkeypatch.setattr(module.config, 'GENERATION_FAILURE_RETRIES', 2)
    calls, history = [], []
    async def generate(account, *a, **kw):
        calls.append(account)
        if account == 'acc1':
            raise _failed(account)
        return {'account': account, 'local_path': 'fake.mp4'}
    bp._generate_effective = generate
    result = _run(bp.generate_video('p', None, 5, 'seedance-2.5', deadline=time.time() + 30,
                                    on_attempt_end=lambda *args: history.append(args)))
    assert calls == ['acc1', 'acc2'] and result['account'] == 'acc2'
    assert bp.used_today('acc1') == 0
    assert 'conv-acc1' in history[0][-1] and '710082023' in history[0][-1]
    meta = bp._meta('acc1')
    assert not meta['risk_control'] and not meta['quota_blocked_until']


def test_confirmed_failure_retries_are_bounded(tmp_path, monkeypatch):
    module, bp = _pool(tmp_path, monkeypatch, ('acc1', 'acc2', 'acc3', 'acc4'))
    monkeypatch.setattr(module.config, 'GENERATION_FAILURE_RETRIES', 2)
    calls = []
    async def generate(account, *a, **kw):
        calls.append(account)
        raise _failed(account)
    bp._generate_effective = generate
    with pytest.raises(UpstreamGenerationFailed):
        _run(bp.generate_video('p', None, 5, 'seedance-2.5', deadline=time.time() + 30))
    assert calls == ['acc1', 'acc2', 'acc3']      # one submission + two bounded retries


@pytest.mark.parametrize('error', [
    _failed('acc1', retryable=False),
    UpstreamReplyError('上游生成失败', FAILED_TEXT),      # text-only evidence: never resubmit
])
def test_unconfirmed_or_refused_failure_never_switches(tmp_path, monkeypatch, error):
    _, bp = _pool(tmp_path, monkeypatch, ('acc1', 'acc2'))
    calls = []
    async def generate(account, *a, **kw):
        calls.append(account)
        raise error
    bp._generate_effective = generate
    with pytest.raises(UpstreamReplyError):
        _run(bp.generate_video('p', None, 5, 'seedance-2.5', deadline=time.time() + 30))
    assert calls == ['acc1']


def test_retry_disabled_by_config(tmp_path, monkeypatch):
    module, bp = _pool(tmp_path, monkeypatch, ('acc1', 'acc2'))
    monkeypatch.setattr(module.config, 'GENERATION_FAILURE_RETRIES', 0)
    calls = []
    async def generate(account, *a, **kw):
        calls.append(account)
        raise _failed(account)
    bp._generate_effective = generate
    with pytest.raises(UpstreamGenerationFailed):
        _run(bp.generate_video('p', None, 5, 'seedance-2.5', deadline=time.time() + 30))
    assert calls == ['acc1']


# ---- pre-submission: same-account upload retry and region-restricted pages ----

def _upload_fn(outcomes, url='https://www.dola.com/chat', logged_out=False, retries=1):
    from generation_errors import PreSubmissionUploadError, RegionRestrictedError
    class LoginExpiredError(Exception): pass
    raise_if_region_restricted = _region_check()
    calls = {'upload': 0, 'reload': 0, 'mode': 0}
    progress = []
    async def attach(page, paths):
        calls['upload'] += 1
        result = outcomes[calls['upload'] - 1]
        if result is not None:
            raise result
    async def enter(page, account): calls['mode'] += 1
    async def popups(page): pass
    async def is_out(page): return logged_out
    class Page:
        def __init__(self): self.url = 'https://www.dola.com/chat'
        async def goto(self, *a, **kw): calls['reload'] += 1; self.url = url
        async def wait_for_timeout(self, ms): pass
    from browser_feedback import emit
    fn = load_worker_def('upload_with_retry', attach_reference_images=attach, enter_video_mode=enter,
        dismiss_popups=popups, is_logged_out=is_out, emit_feedback=emit,
        raise_if_region_restricted=raise_if_region_restricted, LoginExpiredError=LoginExpiredError,
        config=SimpleNamespace(UPLOAD_RETRIES=retries))
    return fn, Page(), calls, progress, PreSubmissionUploadError, RegionRestrictedError, LoginExpiredError


def test_upload_failure_reloads_and_retries_same_account():
    from generation_errors import UploadRouteError
    err = UploadRouteError('imagex-ap-southeast-1.bytevcloudapi.com', 'net::ERR_FAILED')
    fn, page, calls, *_ = _upload_fn([err, None])
    notes = []
    _run(fn(page, 'acc13', ['ref.png'], lambda now, progress=None, **kw: notes.append(progress)))
    assert calls == {'upload': 2, 'reload': 1, 'mode': 1}
    assert any('同号重试上传' in (n or '') for n in notes)


def test_upload_retry_is_bounded_then_raises_for_account_switch():
    from generation_errors import PreSubmissionUploadError as PreErr
    fn, page, calls, *_ = _upload_fn([PreErr('x'), PreErr('y')])
    with pytest.raises(PreErr, match='y'):
        _run(fn(page, 'acc13', ['ref.png']))
    assert calls['upload'] == 2 and calls['reload'] == 1


def test_reload_into_region_restricted_page_stops_with_region_error():
    from generation_errors import PreSubmissionUploadError as PreErr
    fn, page, calls, _, _, RegionErr, _ = _upload_fn(
        [PreErr('x'), None], url='https://www.dola.com/security/region-restricted')
    with pytest.raises(RegionErr):
        _run(fn(page, 'acc24', ['ref.png']))
    assert calls['upload'] == 1


def test_region_restricted_rotates_dynamic_exit_and_never_marks_login(tmp_path, monkeypatch):
    from generation_errors import RegionRestrictedError
    module, bp = _pool(tmp_path, monkeypatch, ('acc24', 'acc13'))
    rotated = []
    monkeypatch.setattr(module.proxy_store, 'rotate_account_session',
                        lambda account, **kw: rotated.append(account) or 'new-session')
    monkeypatch.setattr(module.proxy_store, 'egress_key_for', lambda account: f'egress-{account}')
    calls = []
    async def generate(account, *a, **kw):
        calls.append(account)
        if account == 'acc24':
            raise RegionRestrictedError('acc24 代理出口所在地区被 Dola 限制')
        return {'account': account, 'local_path': 'fake.mp4'}
    bp._generate_effective = generate
    bp._ordered_candidates = lambda groups: sorted(bp.list_accounts(), key=lambda a: a['name'] != 'acc24')
    result = _run(bp.generate_video('p', None, 5, 'seedance-2.5', deadline=time.time() + 30))
    assert calls == ['acc24', 'acc13'] and result['account'] == 'acc13'
    assert rotated == ['acc24']
    meta = bp._meta('acc24')
    assert meta['login_ok'] != 0 and not meta['risk_control']


def test_login_check_reports_region_page_instead_of_login_expiry():
    from generation_errors import RegionRestrictedError
    raise_if_region_restricted = _region_check()
    page = SimpleNamespace(url='https://www.dola.com/security/region-restricted?from=chat')
    with pytest.raises(RegionRestrictedError, match='地区'):
        raise_if_region_restricted(page, 'acc24')
    raise_if_region_restricted(SimpleNamespace(url='https://www.dola.com/chat'), 'acc24')
