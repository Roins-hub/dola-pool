"""每日额度日界与额度重置验证（以日本时间 LIMIT_RESET_HOUR 点为界）。

注入桩模块（video_worker_ui / dola_client），避免依赖 patchright/fastapi。
"""
from __future__ import annotations

import sys
import time
import types
from datetime import datetime, timedelta, timezone

import pytest


@pytest.fixture(autouse=True)
def _stubs():
    v = types.ModuleType("video_worker_ui")
    for exc in ("AccountLimitedError", "CreditInsufficientError", "LoginExpiredError", "RiskControlError"):
        setattr(v, exc, type(exc, (Exception,), {}))

    async def _gen(*_a, **_k):
        raise RuntimeError("stub")

    async def _resume(*_a, **_k):
        raise RuntimeError("stub")

    v.generate_video = _gen
    v.resume_video = _resume
    sys.modules["video_worker_ui"] = v

    d = types.ModuleType("dola_client")
    d.CreditError = type("CreditError", (Exception,), {})
    sys.modules["dola_client"] = d
    yield


def _pool(tmp_path):
    import browser_pool
    import config

    config.POOL_DB_PATH = str(tmp_path / "pool_usage.db")
    accounts_dir = tmp_path / "accounts"
    accounts_dir.mkdir(exist_ok=True)
    return browser_pool, browser_pool.BrowserPool(accounts_dir=str(accounts_dir))


def test_quota_day_rolls_at_reset_hour(tmp_path):
    browser_pool, _ = _pool(tmp_path)
    hour = browser_pool.config.LIMIT_RESET_HOUR
    tz = timezone(timedelta(hours=9))

    anchor = datetime(2026, 9, 14, hour, 0, 0, tzinfo=tz)
    before = anchor - timedelta(minutes=1)
    after = anchor + timedelta(minutes=1)

    # 重置点之前仍算上一个额度日，之后进入新额度日，且锚点小时 == LIMIT_RESET_HOUR。
    assert browser_pool.quota_day_anchor(before) == anchor - timedelta(days=1)
    assert browser_pool.quota_day_anchor(after) == anchor


def test_next_quota_reset_points_at_reset_hour(tmp_path):
    browser_pool, _ = _pool(tmp_path)
    hour = browser_pool.config.LIMIT_RESET_HOUR

    nxt = browser_pool.next_quota_reset_at()
    local = datetime.fromtimestamp(nxt, browser_pool._reset_tz())

    assert (local.hour, local.minute) == (hour, 0)
    assert nxt > time.time()


def test_reset_daily_quotas_restores_blocked_accounts(tmp_path):
    browser_pool, pool = _pool(tmp_path)
    # reset_daily_quotas 只认 accounts/ 下的真实目录（避免把已删账号的残留元数据算进去），
    # 所以这里必须把目录也建出来，否则恒返回 0 —— 这条用例以前一直是红的。
    for name in ("acc1", "acc2"):
        (tmp_path / "accounts" / name).mkdir(parents=True, exist_ok=True)
    pool.ensure_account("acc1")
    pool.ensure_account("acc2")
    future = 4102444800.0  # 2100-01-01
    pool._conn.execute(
        "UPDATE accounts_meta SET rate_limited_until=?, limit_reason='每日上限', "
        "quota_blocked_until=?, quota_reason='积分不足', "
        "credit_balance=0 WHERE name='acc1'",
        (future, future),
    )
    pool._conn.execute("UPDATE accounts_meta SET login_ok=0 WHERE name='acc2'")
    pool._conn.commit()

    assert pool.reset_daily_quotas() >= 2

    row = pool._meta("acc1")
    assert row["rate_limited_until"] == 0
    assert row["quota_blocked_until"] == 0
    # failed_at/failed_reason 是已废弃列（2.1.0 P3 删除「叠加失败」），重置不再碰它
    assert row["credit_balance"] is None
    # 登录态不属于「额度」，重置后仍需重新验证才会恢复调度。
    assert pool._meta("acc2")["login_ok"] == 0


def test_classify_upstream_failure(tmp_path):
    browser_pool, _ = _pool(tmp_path)

    # 瞬时限流：只冷却一小段时间，不能按失败锁到次日
    assert browser_pool.classify_upstream_failure(
        "ck459 提交失败: 710022002: 当前服务访问频繁，请稍后重试"
    ) == "transient"
    assert browser_pool.classify_upstream_failure("Too Many Requests") == "transient"

    # 当日次数用完 / 本单额度不够 / 其它
    assert browser_pool.classify_upstream_failure(
        "status=quota_exceeded: 今天的生成次数已经达到上限，明天再来免费生成吧"
    ) == "daily"
    assert browser_pool.classify_upstream_failure(
        "需要消耗 3 个视频生成额度，今日剩余 2 个视频生成额度，无法生成该视频"
    ) == "quota"
    assert browser_pool.classify_upstream_failure("出片超时：1800s 内未出片") == "other"


def test_content_refusal_is_not_quota(tmp_path):
    """链路播报里也有「今日剩余 0 个视频生成额度」，不能把版权拒稿误判成额度不足。"""
    browser_pool, _ = _pool(tmp_path)
    copyright_msg = (
        "ck10 出片未成功 status=failed: 生成的视频可能涉及版权限制，请更换输入内容后重试"
        " | 正在为您生成视频... | 本次使用 **Dreamina Seedance 2.5** 生成，将消耗 4 个视频生成额度，"
        "预计等待 5 分钟。视频生成好后，我会主动发送给你，今日剩余 0 个视频生成额度。"
    )

    assert browser_pool.classify_upstream_failure(copyright_msg) == "content"


def test_pure_credit_broadcast_without_refusal_is_not_quota(tmp_path):
    """只有额度播报（没有「需要消耗/无法生成」）不能算「本单额度不够」。"""
    browser_pool, _ = _pool(tmp_path)

    assert browser_pool.classify_upstream_failure(
        "本次使用 Dreamina Seedance 2.5 生成，将消耗 2 个视频生成额度，今日剩余 0 个视频生成额度。"
    ) == "other"


def test_content_refusal_does_not_mark_account(tmp_path):
    """内容/版权拒稿不该把账号标成失败或额度不足。"""
    browser_pool, pool = _pool(tmp_path)
    pool.ensure_account("ckc")

    kind = pool._note_upstream_failure(
        "ckc", "生成的视频可能涉及版权限制，请更换输入内容后重试", 0
    )

    assert kind == "content"
    row = pool._meta("ckc")
    assert row["failed_at"] == 0
    assert not row["quota_blocked_until"]


PORTRAIT_REFUSAL = (
    "连续 3 次生成均失败（依次尝试账号: ckp、ckp2）: ckp 出片未成功 "
    "status=failed: 出于肖像保护考虑，未认证人脸暂不支持用 Dreamina Seedance 2.5 "
    "生成视频。你可以尝试换其它参考图或文生视频。"
)


def test_portrait_refusal_writes_account_note(tmp_path):
    """肖像保护拒稿：归类 content（不标账号失败），且上游原话落到账号备注。"""
    browser_pool, pool = _pool(tmp_path)
    pool.ensure_account("ckp")

    assert browser_pool.classify_upstream_failure(PORTRAIT_REFUSAL) == "content"
    assert pool._note_upstream_failure("ckp", PORTRAIT_REFUSAL, 0) == "content"

    row = pool._meta("ckp")
    assert row["failed_at"] == 0
    note = row["note"]
    assert "肖像保护：出于肖像保护考虑" in note
    assert "status=failed" not in note          # 备注里不带技术前缀


def test_rate_limit_does_not_touch_account_note(tmp_path):
    """限流不是审核拒稿，别把备注刷成一堆日志。"""
    browser_pool, pool = _pool(tmp_path)
    pool.ensure_account("ckr")

    pool._note_upstream_failure("ckr", "ckr 出片未成功 status=failed: 710022002: 当前服务访问频繁，请稍后重试", 0)

    assert pool._meta("ckr")["note"] == ""


def test_append_note_keeps_manual_note_and_caps_length(tmp_path):
    """备注是人工字段：追加时不能把人写的备注冲掉，超长只保留最新内容。"""
    browser_pool, pool = _pool(tmp_path)
    pool.ensure_account("ckn")
    pool.set_note("ckn", "人工备注：本号只用于 2.5 模型")

    pool.append_note("ckn", "[2026-09-23 01:05] 肖像保护：出于肖像保护考虑…")
    note = pool._meta("ckn")["note"]
    assert note.startswith("人工备注：")
    assert "肖像保护：" in note

    pool.append_note("ckn", "底" * 600, limit=500)
    assert len(pool._meta("ckn")["note"]) == 500
    assert pool._meta("ckn")["note"].endswith("底")


def test_short_clip_does_not_mark_account(tmp_path):
    """「30 秒只给了 15 秒」按不拼接策略重试，不该把账号标成失败或额度不足。"""
    browser_pool, pool = _pool(tmp_path)
    pool.ensure_account("cks")
    message = "cks 上游本次只给了 15.0 秒成片（目标 30 秒），按「30 秒不拼接」策略换号重试"

    assert browser_pool.classify_upstream_failure(message) == "short"
    assert pool._note_upstream_failure("cks", message, 0) == "short"
    row = pool._meta("cks")
    assert row["failed_at"] == 0
    assert not row["quota_blocked_until"]


def test_model_point_table(tmp_path):
    """记账口径：2.5 一条 2 点（不分时长），2.0 一条 3 点。"""
    browser_pool, pool = _pool(tmp_path)

    assert pool._duration_cost("seedance-2.5", 10) == 2
    assert pool._duration_cost("seedance-2.5", 30) == 2
    assert pool._duration_cost("seedance-2.0", 5) == 3
    assert pool._duration_cost("seedance-2.0", 10) == 3
    assert pool._duration_cost("seedance-2.0", 15) == 3


def test_claim_uses_table_not_upstream_quote(tmp_path):
    """上游回执「将消耗 4 个」只是报价：2.5 仍按额度表记 2 点。"""
    browser_pool, pool = _pool(tmp_path)
    pool.ensure_account("acc9")

    charged = pool._claim_for("acc9", 2, {"credits_used": 4})

    assert charged == 2
    assert pool.used_today("acc9") == 2


def test_upstream_failure_marks_by_kind(tmp_path):
    """新规则（2.1.0 P3）：限流只换号、不写状态；额度类进冷却；普通失败连续 3 次进异常组。"""
    browser_pool, pool = _pool(tmp_path)
    for name in ("t1", "t2", "t3", "t4"):
        pool.ensure_account(name)
    now = time.time()

    # 上游「访问频繁」：概念已删除 —— 不冷却、不标失败，只换号重试
    assert pool._note_upstream_failure("t1", "710022002: 当前服务访问频繁，请稍后重试") == "transient"
    t1 = pool._meta("t1")
    assert t1["failed_at"] == 0
    assert t1["cooldown_until"] == 0
    assert t1["rate_limited_until"] == 0

    # 当日次数用完 / 本单额度不够 → 额度口径的冷却（面板归「冷却」组）
    assert pool._note_upstream_failure("t2", "今天的生成次数已经达到上限，明天再来免费生成吧") == "daily"
    assert pool._meta("t2")["rate_limited_until"] > now

    assert pool._note_upstream_failure("t3", "今日剩余 2 个视频生成额度，无法生成该视频") == "quota"
    assert pool._meta("t3")["quota_blocked_until"] > now

    # 普通失败：前两次不动状态，第 3 次进【异常】组（到期自动恢复）
    for _ in range(2):
        assert pool._note_upstream_failure("t4", "出片超时：1800s 内未出片") == "other"
    assert pool._meta("t4")["abnormal_until"] == 0
    assert pool._note_upstream_failure("t4", "出片超时：1800s 内未出片") == "other"
    row = pool._meta("t4")
    assert row["failed_at"] == 0          # 「叠加失败」概念已删除，不再写这个字段
    assert row["abnormal_until"] > now    # 改走【异常】组
    assert "未出片" in row["abnormal_reason"]
