"""补测：5 分钟「第一次回执」规则对所有时长生效。"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _stubs():
    v = types.ModuleType("video_worker_ui")
    for exc in ("AccountLimitedError", "CreditInsufficientError", "LoginExpiredError",
                "RiskControlError"):
        setattr(v, exc, type(exc, (Exception,), {}))
    v.generate_video = lambda *a, **k: None
    v.resume_video = lambda *a, **k: None
    sys.modules.setdefault("video_worker_ui", v)
    d = types.ModuleType("dola_client")
    d.CreditError = type("CreditError", (Exception,), {})
    sys.modules.setdefault("dola_client", d)


_stubs()
import pure_api_gen  # noqa: E402


class FakePoll:
    def __init__(self, status="pending", texts=None, failure_reasons=None):
        self.status = status
        self.texts = texts or []
        self.failure_reasons = failure_reasons or []


class FakeDola:
    def __init__(self, polls):
        self.polls = list(polls)
        self.calls = 0

    def inspect_video_once(self, session, ctx, conversation_id):
        self.calls += 1
        return self.polls.pop(0) if self.polls else FakePoll()

    @staticmethod
    def is_prompt_echo_text(text):
        return str(text).startswith("生成视频：") or "分镜脚本" in str(text)


def _await(dola, monkeypatch, seconds):
    monkeypatch.setattr(pure_api_gen, "NO_ACK_SECONDS", seconds)
    return pure_api_gen._await_first_ack(
        dola=dola, session=None, ctx=None, account="acc1", conversation_id="c1",
        ack_deadline=__import__("time").time() + seconds, poll_interval=0.01,
    )


def test_prompt_echo_is_not_an_ack(monkeypatch):
    """只回显了自己的提示词不算"有人答话" → 到点判异常。"""
    dola = FakeDola([FakePoll(texts=["生成视频：一只橘猫"]), FakePoll(texts=["生成视频：一只橘猫"])])

    with pytest.raises(pure_api_gen.AbnormalNoAckError) as exc:
        _await(dola, monkeypatch, 0.05)

    assert "请重试" in str(exc.value)
    assert "没有任何回应" in str(exc.value)


def test_credit_broadcast_counts_as_ack(monkeypatch):
    """额度播报就是回执 → 立刻放行，不再等。"""
    dola = FakeDola([FakePoll(texts=["本次使用 Dreamina Seedance 2.5 生成，将消耗 2 个视频生成额度"])])

    _await(dola, monkeypatch, 5)

    assert dola.calls == 1


def test_failure_reason_counts_as_ack(monkeypatch):
    dola = FakeDola([FakePoll(status="failed", failure_reasons=["涉嫌侵权"])])

    _await(dola, monkeypatch, 5)

    assert dola.calls == 1


def test_non_pending_status_counts_as_ack(monkeypatch):
    dola = FakeDola([FakePoll(status="succeeded")])

    _await(dola, monkeypatch, 5)

    assert dola.calls == 1


def test_progress_heartbeat_keeps_last_poll_fresh(monkeypatch):
    """等待出片期间必须持续刷进度：否则看门狗会把正常等待判成「卡住」并重新派发（白扣额度）。"""
    ticks = []
    with pure_api_gen._progress_heartbeat(ticks.append, interval=0.02):
        __import__("time").sleep(0.12)

    assert len(ticks) >= 2, ticks


def test_progress_heartbeat_without_callback_is_noop():
    with pure_api_gen._progress_heartbeat(None, interval=0.01):
        pass
