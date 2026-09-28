"""分组判定单测（2026-09-23 大迭代 P1）。

口径（用户拍板）：满额=剩余4点（2.0 专用）、半额=剩余2点（2.5 优先）、冷却=剩余0或1点；
风控永久、待激活=未完成首次激活探测、异常=5 分钟无回执、生成中=被任务占用。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import browser_pool as bp

NOW = 1_700_000_000.0


def acct(remaining=4, **kw):
    """一个「面板 dict」形状的账号；limit 显式给 4，避免受别的测试改全局 DAILY_LIMIT 影响。"""
    a = {
        "name": "acc1", "remaining": remaining, "limit": 4, "login_ok": 1, "risk_control": False,
        "abnormal_until": 0, "scheduling": True, "busy": False,
    }
    a.update(kw)
    return a


def group(a, **kw):
    return bp.group_of(a, NOW, **kw)[0]


def test_quota_maps_to_groups():
    assert group(acct(4)) == bp.GROUP_FULL
    assert group(acct(2)) == bp.GROUP_HALF
    # 2.0 用剩的 1 点和 2×2.5 用尽的 0 点都进冷却
    assert group(acct(1)) == bp.GROUP_COOLING
    assert group(acct(0)) == bp.GROUP_COOLING
    # 3 点属于半额（2 ≤ 剩余 < 上限），2.5 可用、2.0 不够
    assert group(acct(3)) == bp.GROUP_HALF


def test_risk_is_sticky_and_wins():
    risky = acct(4, risk_control=True, risk_reason="你好无回复")
    assert group(risky) == bp.GROUP_RISK
    assert bp.group_of(risky, NOW)[1] == "你好无回复"
    # 风控优先于待激活/异常/生成中
    assert group(acct(4, risk_control=True, login_ok=None, abnormal_until=NOW + 60)) == bp.GROUP_RISK


def test_pending_before_activation():
    assert group(acct(4, login_ok=None)) == bp.GROUP_PENDING
    assert group(acct(2, login_ok=None)) == bp.GROUP_PENDING


def test_upstream_quota_and_limit_states_are_cooling():
    """上游自己报「额度不足/今日次数用完」的号不能出片 → 归冷却组，原因给人看得懂。"""
    quota = acct(4, quota_blocked=True, quota_reason="upstream", credit_balance=0)
    assert group(quota) == bp.GROUP_COOLING
    assert bp.group_of(quota, NOW)[1] == "上游报额度不足（今日剩余 0）"

    explained = acct(4, quota_blocked=True, quota_reason="额度不足: upstream: no credits")
    assert bp.group_of(explained, NOW)[1] == "额度不足: upstream: no credits"

    limited = acct(4, rate_limited=True, limit_reason="今天的生成次数已经达到上限")
    assert group(limited) == bp.GROUP_COOLING
    assert bp.group_of(limited, NOW)[1] == "今天的生成次数已经达到上限"


def test_abnormal_group_expires():
    assert group(acct(4, abnormal_until=NOW + 60)) == bp.GROUP_ABNORMAL
    assert group(acct(4, abnormal_until=NOW - 1)) == bp.GROUP_FULL   # 到期自动回满额


def test_busy_group_from_lock_or_task_table():
    assert group(acct(2), busy=True) == bp.GROUP_BUSY
    assert group(acct(2), processing_accounts={"acc1"}) == bp.GROUP_BUSY
    assert group(acct(2), processing_accounts={"acc2"}) == bp.GROUP_HALF


def test_priority_order_abnormal_beats_busy_and_cooling():
    assert group(acct(0, abnormal_until=NOW + 60), busy=True) == bp.GROUP_ABNORMAL
    assert group(acct(4), busy=True) == bp.GROUP_BUSY


def test_active_groups_are_the_valid_set():
    assert set(bp.ACTIVE_GROUPS) == {bp.GROUP_FULL, bp.GROUP_HALF, bp.GROUP_COOLING, bp.GROUP_BUSY}
    assert bp.GROUP_RISK not in bp.ACTIVE_GROUPS
    assert bp.GROUP_PENDING not in bp.ACTIVE_GROUPS
    assert bp.GROUP_ABNORMAL not in bp.ACTIVE_GROUPS


def test_group_order_covers_all_buttons():
    assert set(bp.GROUP_ORDER) == {
        bp.GROUP_FULL, bp.GROUP_HALF, bp.GROUP_COOLING, bp.GROUP_BUSY,
        bp.GROUP_RISK, bp.GROUP_PENDING, bp.GROUP_ABNORMAL}
