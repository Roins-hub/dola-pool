"""Dola's own wait estimate is read from its reply (ja/zh/en) and reaches the task as upstream_wait."""
import pytest

from browser_feedback import emit
from upstream_reply import wait_seconds_from_reply


@pytest.mark.parametrize('text,seconds', [
    ('動画は**Dreamina Seedance 2.0高速モデル**を使用して、5分分後に生成されます。', 300),
    ('動画は**Seedance 2.0モデル**を使用して、1-3分以内に生成されます。', 180),
    ('视频正在生成，预计等待 30-40 分钟', 2400),
    ('大约 8 分钟后完成', 480),
    ('Estimated about 12 minutes', 720),
    ('生成には約2時間かかります', 7200),
])
def test_estimates_in_dola_replies(text, seconds):
    assert wait_seconds_from_reply(text) == seconds


@pytest.mark.parametrize('text', [
    '動画が生成されました。',
    '10秒の動画を生成します',
    '生成を開始します。\n```json\n{"prompt": "镜头持续3分钟，约5分钟后转场", "duration": 10}\n```',
    '5 minutesque',
    '',
])
def test_no_estimate_from_durations_or_echoed_prompts(text):
    assert wait_seconds_from_reply(text) == 0


def test_emit_passes_the_estimate_with_the_note():
    seen = []
    def on_poll(now, note=None, wait_seconds=None, progress=None):
        seen.append((note, wait_seconds, progress))
    emit(on_poll, note='5分後に生成されます')
    emit(on_poll, note='動画が生成されました。')
    emit(on_poll, progress='正在上传参考图')
    assert seen == [('5分後に生成されます', 300, None), ('動画が生成されました。', None, None),
                    (None, None, '正在上传参考图')]


def test_emit_keeps_working_with_callbacks_without_wait_seconds():
    seen = []
    def on_poll(now, note=None):
        seen.append(note)
    emit(on_poll, note='5分後に生成されます', progress='ignored')
    assert seen == ['5分後に生成されます']
