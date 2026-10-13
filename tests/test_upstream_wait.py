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
    monkeypatch.setattr(config, "PURE_API_TIMEOUT_MAX", 3600)
    t = pure_api_gen._timeout_for_upstream_wait
    assert t(900, 0) == 900                       # 没有预计：保持原超时
    assert t(900, 5 * 60) == 900                  # 预计很短：不缩短原超时
    assert t(900, 45 * 60) == 45 * 60 * 1.2 + 300  # 45 分钟 → 59 分钟
    assert t(900, 3 * 3600) == 3600               # 封顶
    monkeypatch.setattr(config, "PURE_API_TIMEOUT_MAX", 0)
    assert t(900, 45 * 60) == 900                 # 上限比原超时还小：按原超时


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


def test_plus_upsell_alone_is_not_quota_exhausted():
    """排队久时上游也会推销 Plus，这句话本身不能当成额度用完。"""
    assert not dola_pure_api.looks_like_daily_quota_text("预计等待 45 分钟。开通 Dola Plus，免等待。")


def test_quota_exceeded_failure_marks_account_daily_limited():
    """纯 API 报 quota_exceeded → 号池归为 daily（进冷却到额度重置，并立即换号）。"""
    import browser_pool
    msg = f"acc3 出片未成功 status=quota_exceeded: {QUOTA_GONE}"
    assert browser_pool.classify_upstream_failure(msg) == "daily"
