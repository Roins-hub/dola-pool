"""2.1.5：出片等待跟着上游「预计等待 N 分钟」放长。

2026-10-13 线上：5 秒任务上游回「……预计等待 45 分钟」，我们按固定 15 分钟上限等，
到点自己判超时，报错还被误归成「上游要求 30 秒拆两段」。
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config


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
from protocol import dola_pure_api  # noqa: E402

REAL_ACK = [
    "视频生成目前支持 4 到 15 秒。我按你指定的 9:16 比例生成 5 秒版本。",
    "本次使用 **Dreamina Seedance 2.5** 生成，将消耗 2 个视频生成额度，预计等待 45 分钟。"
    "视频生成好后，我会主动发送给你，今日剩余 2 个视频生成额度。",
]


def test_parse_wait_from_real_upstream_text():
    assert pure_api_gen._wait_seconds_from_texts(dola_pure_api, REAL_ACK) == 45 * 60


def test_parse_wait_variants():
    parse = lambda t: pure_api_gen._wait_seconds_from_texts(dola_pure_api, [t])  # noqa: E731
    assert parse("预计等待 1 小时") == 3600
    assert parse("预计等待 30-40 分钟") == 40 * 60          # 区间取上限
    assert parse("预计等待 1小时20分钟") == 80 * 60
    assert parse("视频生成好后，我会主动发送给你") == 0      # 没写预计 → 0


def test_timeout_follows_upstream_estimate_with_cap(monkeypatch):
    """2.2.8：dola 说 X 就最多等 X × 1.5 + 30 分钟（不短于原超时，封顶 WAIT_HARD_MAX）。"""
    monkeypatch.setattr(config, "UPSTREAM_WAIT_FACTOR", 1.5)
    monkeypatch.setattr(config, "UPSTREAM_WAIT_MARGIN", 1800)
    monkeypatch.setattr(config, "WAIT_HARD_MAX", 21600)
    t = pure_api_gen._timeout_for_upstream_wait
    assert t(900, 0) == 900                       # 没有预计：保持原超时
    assert t(7200, 5 * 60) == 7200                # 预计很短：不缩短原超时
    assert t(900, 45 * 60) == 45 * 60 * 1.5 + 1800  # 45 分钟 → 97.5 分钟
    assert t(900, 3600) == 3600 * 1.5 + 1800      # dola 说 60 分钟 → 等 120 分钟（不再卡在 60 分钟上）
    assert t(900, 7200) == 7200 * 1.5 + 1800      # dola 说 2 小时 → 等 3.5 小时
    assert t(900, 10 * 3600) == 21600             # 封顶 6 小时
    monkeypatch.setattr(config, "WAIT_HARD_MAX", 0)
    assert t(900, 45 * 60) == 900                 # 上限比原超时还小：按原超时


def test_wait_extends_when_dola_raises_its_estimate(monkeypatch):
    """等到点时 dola 最新的预计变长了：顺延，而不是判超时。"""
    import time as _time
    monkeypatch.setattr(config, "UPSTREAM_WAIT_FACTOR", 1.5)
    monkeypatch.setattr(config, "UPSTREAM_WAIT_MARGIN", 1800)
    monkeypatch.setattr(config, "WAIT_HARD_MAX", 21600)
    calls = []

    class D:
        def extract_wait_minutes_from_text(self, t):
            return dola_pure_api.extract_wait_minutes_from_text(t)

        def get_video_result(self, session, ctx, conv, *, timeout, **k):
            calls.append(timeout)
            if len(calls) == 1:     # 第一次等到点：dola 改口「预计等待 3 小时」
                return {"status": "timeout", "texts": ["预计等待 60 分钟", "排队较多，预计等待 3 小时"]}
            return {"status": "succeeded", "texts": []}

    deadlines = []
    started = _time.time() - 2.5 * 3600              # 已经等了 2.5 小时（60 分钟的预计早过了）
    r = pure_api_gen._wait_for_video(
        D(), None, {}, "c1", account="acc1", started_at=started, base_timeout=900,
        wait_seconds=3600, poll_interval=0, remove_watermark=False, proxy="",
        on_deadline=deadlines.append)
    assert r["status"] == "succeeded" and len(calls) == 2
    assert deadlines and abs(deadlines[0] - (started + 3 * 3600 * 1.5 + 1800)) < 1


def test_wait_does_not_extend_without_new_estimate():
    class D:
        def extract_wait_minutes_from_text(self, t):
            return dola_pure_api.extract_wait_minutes_from_text(t)

        def get_video_result(self, *a, **k):
            return {"status": "timeout", "texts": ["预计等待 60 分钟"]}

    import time as _time
    r = pure_api_gen._wait_for_video(
        D(), None, {}, "c1", account="acc1", started_at=_time.time() - 3 * 3600, base_timeout=900,
        wait_seconds=3600, poll_interval=0, remove_watermark=False, proxy="")
    assert r["status"] == "timeout"


def test_four_to_fifteen_text_is_not_a_split_request_for_short_jobs():
    """「支持 4 到 15 秒」只对 30 秒任务意味着「要拆段」；5 秒任务里这句话只是上游的说明。"""
    src = Path(pure_api_gen.__file__).read_text(encoding="utf-8")
    assert "if int(duration) >= 30 and duration_refused and not config.ALLOW_30S_PAIR:" in src


QUOTA_GONE = "今日视频生成免费数量用完了，先使用快速模式和我聊聊别的吧。开通 Dola Plus，免等待，继续为你使用视频生成功能。"


def test_free_quota_used_up_is_recognised_upstream():
    """2026-10-13 线上新话术：以前认不出，任务干等 15 分钟才失败。"""
    assert dola_pure_api.looks_like_daily_quota_text(QUOTA_GONE)
    status, hits = dola_pure_api.classify_chain_outcome([], [QUOTA_GONE])
    assert status == "quota_exceeded" and hits


JA_LIMIT = ("動画生成の無料利用分（1日あたり）の上限に到達しました。高速モードで引き続きチャットできます。"
            "動画生成を使用し続けるには、Dola Plusプランにアップグレードしてください。")


def test_japanese_daily_limit_is_recognised_upstream():
    """2026-10-29 日文话术：纯 API 路径以前认不出，任务干等到总超时。"""
    assert dola_pure_api.classify_chain_outcome([], [JA_LIMIT])[0] == "quota_exceeded"
    accepted = ("動画は**Dreamina Seedance 2.0 高速モデル**を使用して、5分以内に生成されます。"
                "この生成では1日の無料クレジットが使用されます。")
    assert not dola_pure_api.looks_like_daily_quota_text(accepted)


def test_plus_upsell_alone_is_not_quota_exhausted():
    """排队久时上游也会推销 Plus，这句话本身不能当成额度用完。"""
    assert not dola_pure_api.looks_like_daily_quota_text("预计等待 45 分钟。开通 Dola Plus，免等待。")


def test_quota_exceeded_failure_marks_account_daily_limited():
    """纯 API 报 quota_exceeded → 号池归为 daily（进冷却到额度重置，并立即换号）。"""
    import browser_pool
    msg = f"acc3 出片未成功 status=quota_exceeded: {QUOTA_GONE}"
    assert browser_pool.classify_upstream_failure(msg) == "daily"
