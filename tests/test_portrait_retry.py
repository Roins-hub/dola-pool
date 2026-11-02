"""肖像保护拒绝（未开始生成）时换号重提：2026-11-02 同图同词 4 个号 2 过 2 拒。"""
import time

import pytest

import config
from generation_errors import UpstreamGenerationFailed, UpstreamReplyError, portrait_refusal
from test_task_deadline import _pool, _run
from test_start_confirm import _poll, _stall_run

PORTRAIT = ('肖像保護のため、Dreamina Seedance 2.0は実在する人物の画像からの動画生成には対応していません。'
            '別の画像を使用するか、テキストから動画を作成してください。')
COPYRIGHT = '版权限制：请更换输入内容'


def test_portrait_refusal_needs_a_confirmed_unstarted_conversation():
    assert portrait_refusal(UpstreamReplyError('内容拒绝', PORTRAIT, generation_started=False))
    assert not portrait_refusal(UpstreamReplyError('内容拒绝', PORTRAIT, generation_started=True))
    assert not portrait_refusal(UpstreamReplyError('内容拒绝', PORTRAIT))            # 不知道是否已开始
    assert not portrait_refusal(UpstreamReplyError('内容拒绝', COPYRIGHT, generation_started=False))
    assert portrait_refusal(UpstreamGenerationFailed('内容拒绝', PORTRAIT, code='710082031', conversation_id='c',
                                                     retryable=False, not_charged=True))
    assert not portrait_refusal(RuntimeError(PORTRAIT))


def test_poll_marks_a_text_refusal_as_not_started():
    err, _typed = _stall_run([_poll(PORTRAIT)] * 4, ratio='16:9', with_typed=True)
    assert isinstance(err, UpstreamReplyError) and err.generation_started is False


def _dispatch(tmp_path, monkeypatch, fail, retries=3):
    module, bp = _pool(tmp_path, monkeypatch, ('acc1', 'acc2', 'acc3'))
    monkeypatch.setattr(config, 'PORTRAIT_RETRIES', retries)
    calls = []
    async def generate(account, *a, **kw):
        calls.append(account)
        if account in fail:
            raise fail[account]
        return {'account': account, 'local_path': 'fake.mp4'}
    bp._generate_effective = generate
    run = bp.generate_video('p', None, 10, 'seedance-2.0-fast', reference_image_paths=['fake.png'],
                            deadline=time.time() + 60)
    return calls, run


def test_portrait_refusal_moves_to_another_account(tmp_path, monkeypatch):
    calls, run = _dispatch(tmp_path, monkeypatch,
                           {'acc1': UpstreamReplyError('内容拒绝', PORTRAIT, generation_started=False)})
    result = _run(run)
    assert calls == ['acc1', 'acc2'] and result['account'] == 'acc2'


@pytest.mark.parametrize('error,retries', [
    (UpstreamReplyError('内容拒绝', PORTRAIT, generation_started=True), 3),     # 已开始生成：可能重复出片
    (UpstreamReplyError('内容拒绝', PORTRAIT, generation_started=False), 0),    # 关闭了
    (UpstreamReplyError('内容拒绝', COPYRIGHT, generation_started=False), 3),   # 版权不重提
])
def test_other_refusals_still_end_the_task(tmp_path, monkeypatch, error, retries):
    calls, run = _dispatch(tmp_path, monkeypatch, {'acc1': error}, retries=retries)
    with pytest.raises(UpstreamReplyError):
        _run(run)
    assert calls == ['acc1']


def test_retries_are_capped_and_the_refusal_is_reported(tmp_path, monkeypatch):
    refuse = lambda: UpstreamReplyError('内容拒绝', PORTRAIT, generation_started=False)
    calls, run = _dispatch(tmp_path, monkeypatch, {'acc1': refuse(), 'acc2': refuse(), 'acc3': refuse()}, retries=1)
    with pytest.raises(UpstreamReplyError) as err:
        _run(run)
    assert calls == ['acc1', 'acc2'] and '肖像保護' in str(err.value)
